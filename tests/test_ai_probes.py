"""Single-key transport, monetary semantics, permissions and secret boundaries."""
from dataclasses import replace
from contextlib import closing
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from src.ai_transport import _key_failures, _key_identity
from src.settings import AISettings
from src import ai_reasoning
from src.ai_transport import chat_content
from webapp import app as module, ai_probes, ai_keys
from webapp.login_guard import RateLimiter
from webapp.storage import Store

SECRET = 'synthetic-probe-secret'
SETTINGS = AISettings(enabled=False, api_key=SECRET, backup_api_keys=('synthetic-backup',))
BALANCE = {'is_available': True, 'balance_infos': [
    {'currency':'CNY','total_balance':'12.30','granted_balance':'2.30','topped_up_balance':'10.00'},
    {'currency':'USD','total_balance':'1.00','granted_balance':'0.00','topped_up_balance':'1.00'}]}


class FakeUpstream(BaseHTTPRequestHandler):
    response = {}
    status = 200
    calls = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.respond(payload)

    def do_GET(self):
        self.respond(None)

    def respond(self, payload):
        type(self).calls.append((self.path, self.headers.get('Authorization'), payload))
        self.send_response(self.status)
        self.send_header('Content-Type', 'application/json')
        if self.status == 302:
            self.send_header('Location', '/must-not-follow')
        self.end_headers()
        self.wfile.write(json.dumps(self.response).encode())


class ProbeTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeUpstream)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.settings = replace(SETTINGS, enabled=True, backup_api_keys=(),
                               base_url=f'http://127.0.0.1:{cls.server.server_port}/v1')

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.thread.join(); cls.server.server_close()

    def setUp(self):
        FakeUpstream.status=200; FakeUpstream.calls=[]; FakeUpstream.response={}

    def test_exact_key_one_request_ignores_and_preserves_production_cooldown(self):
        FakeUpstream.response={'choices':[{'finish_reason':'stop','message':{'content':'{"probe":"ok"}'}}]}
        key=_key_identity(self.settings.base_url, SECRET)
        old=_key_failures.copy(); _key_failures[key]=(float('inf'),401)
        try:
            result=ai_probes.run(self.settings,'health')
            self.assertEqual(result['status'],'ok')
            self.assertEqual(_key_failures[key],(float('inf'),401))
            self.assertEqual(len(FakeUpstream.calls),1)
            path,auth,payload=FakeUpstream.calls[0]
            self.assertEqual(path,'/v1/chat/completions'); self.assertEqual(auth,'Bearer '+SECRET)
            self.assertEqual(payload['max_tokens'],64); self.assertIn('response_format',payload)
            self.assertNotIn(SECRET,json.dumps(result))
        finally:
            _key_failures.clear(); _key_failures.update(old)

    def test_models_real_http_deduplicates_and_only_uses_selected_key(self):
        FakeUpstream.response={'data':[{'id':'z/model','extra':SECRET},{'id':'a-model'},{'id':'a-model'}]}
        self.assertEqual(ai_probes.models(self.settings),['a-model','z/model'])
        self.assertEqual(FakeUpstream.calls, [('/v1/models','Bearer '+SECRET,None)])
        FakeUpstream.response={'data':[]}
        self.assertEqual(ai_probes.models(self.settings),[])
        for data in [{'data':{}},{'data':[{'id':'bad\nmodel'}]},{'data':[{'id':SECRET}]},
                     {'data':[{'id':None}]},{'data':[{'id':'x'*201}]},{'data':[{}]},
                     {'data':[{'id':'model'}]*2001}]:
            FakeUpstream.response=data
            with self.assertRaises(ai_probes.TransportError) as failed: ai_probes.models(self.settings)
            self.assertEqual(failed.exception.kind,'schema')
        FakeUpstream.status=302; FakeUpstream.response={'error':SECRET}; FakeUpstream.calls=[]
        with self.assertRaises(ai_probes.TransportError) as failed: ai_probes.models(self.settings)
        self.assertEqual(failed.exception.status,302); self.assertEqual(len(FakeUpstream.calls),1)

    def test_models_response_size_is_bounded(self):
        FakeUpstream.response={'data':[], 'large':'x'*(2*1024*1024)}
        with self.assertRaises(ai_probes.TransportError) as failed: ai_probes.models(self.settings)
        self.assertEqual(failed.exception.kind,'oversized')

    def test_reasoning_capabilities_distinguish_levels_toggle_unknown_and_mandatory(self):
        data={'data':[
            {'id':'deep','effort':{'supported_levels':['low','high','max']}},
            {'id':'mandatory','reasoning':{'supported_efforts':['medium','high'],'mandatory':True}},
            {'id':'toggle','reasoning':{}},
            {'id':'plain','supported_parameters':['temperature']},
            {'id':'unknown'}, {'id':'unlisted','supported_parameters':['reasoning_effort']},
            {'id':'bad','reasoning':{'supported_efforts':['invented']}},
            {'id':'bad-bool','reasoning':{'mandatory':'false','supported_efforts':['high']}},
            {'id':'duplicate','effort':{'supported_levels':['high']}}, {'id':'duplicate'},
            {'id':'all-levels','reasoning':{'supported_efforts':None}}]}
        FakeUpstream.response=data
        caps=ai_probes.models(self.settings,with_capabilities=True)['capabilities']
        self.assertEqual(caps['deep']['levels'],['low','high','max']); self.assertEqual(caps['deep']['protocol'],'deepseek')
        self.assertFalse(caps['mandatory']['can_disable']); self.assertEqual(caps['mandatory']['protocol'],'openrouter')
        self.assertEqual(caps['toggle']['state'],'toggle'); self.assertEqual(caps['toggle']['levels'],[])
        self.assertEqual(caps['plain']['state'],'unsupported')
        for model in ['unknown','unlisted','bad','bad-bool','duplicate']:
            self.assertEqual(caps[model]['state'],'unknown',model)
        self.assertEqual(caps['all-levels']['levels'],list(ai_reasoning.EFFORTS))

    def test_real_reasoning_business_and_probe_payloads_are_provider_specific(self):
        FakeUpstream.response={'choices':[{'finish_reason':'stop','message':{'content':'{"probe":"ok"}'}}]}
        for protocol,effort,expected in [
            ('deepseek','max',{'thinking':{'type':'enabled'},'reasoning_effort':'max'}),
            ('deepseek','none',{'thinking':{'type':'disabled'}}),
            ('deepseek','enabled',{'thinking':{'type':'enabled'}}),
            ('openrouter','high',{'reasoning':{'effort':'high'}}),
            ('openrouter','none',{'reasoning':{'effort':'none'}}),
            ('openrouter','enabled',{'reasoning':{'enabled':True}}),('','default',{})]:
            with self.subTest(protocol=protocol,effort=effort):
                configured=replace(self.settings,reasoning_effort=effort,reasoning_protocol=protocol)
                self.assertEqual(chat_content(configured,[{'role':'user','content':'synthetic'}],5),'{"probe":"ok"}')
                payload=FakeUpstream.calls[-1][2]
                self.assertEqual({key:payload[key] for key in ['thinking','reasoning','reasoning_effort'] if key in payload},expected)
                self.assertEqual(ai_probes.run(configured,'health')['status'],'ok')
                payload=FakeUpstream.calls[-1][2]
                self.assertEqual({key:payload[key] for key in ['thinking','reasoning','reasoning_effort'] if key in payload},expected)
                self.assertEqual(payload['max_tokens'],64 if effort in ('default','none') else configured.max_tokens)

    def test_http_failure_classification_no_failover_no_redirect_no_secret_echo(self):
        for code,expected in [(401,'invalid_key'),(402,'insufficient_balance'),(403,'forbidden'),
                              (404,'not_found'),(429,'rate_limited'),(500,'upstream_error'),(302,'http_error')]:
            with self.subTest(code=code):
                FakeUpstream.status=code; FakeUpstream.response={'error':SECRET}; FakeUpstream.calls=[]
                result=ai_probes.run(replace(self.settings,backup_api_keys=('other-secret',)),'health')
                self.assertEqual(result['status'],expected); self.assertEqual(result['http_status'],code)
                self.assertEqual(len(FakeUpstream.calls),1); self.assertNotIn(SECRET,json.dumps(result))

    def test_health_rejects_invalid_json_empty_truncated_or_wrong_probe(self):
        for content,finish,expected in [('not-json','stop','schema'),('{"probe":"wrong"}','stop','schema'),
                                         ('','stop','empty'),('{"probe":"ok"}','length','incomplete')]:
            FakeUpstream.response={'choices':[{'finish_reason':finish,'message':{'content':content}}]}
            self.assertEqual(ai_probes.run(self.settings,'health')['status'],expected)

    def test_deepseek_account_currencies_and_zero_are_preserved(self):
        FakeUpstream.response=BALANCE
        result=ai_probes.run(self.settings,'balance','deepseek')
        self.assertEqual(result['status'],'ok'); self.assertEqual(result['scope'],'account')
        self.assertEqual([b['currency'] for b in result['balances']],['CNY','USD'])
        self.assertEqual(result['balances'][0]['total'],'12.30')
        self.assertEqual(FakeUpstream.calls[0][0],'/user/balance')
        FakeUpstream.response={'is_available':False,'balance_infos':[
            {**BALANCE['balance_infos'][0],'total_balance':'0.00'}]}
        result=ai_probes.run(self.settings,'balance','deepseek')
        self.assertEqual(result['status'],'ok'); self.assertFalse(result['available'])
        self.assertEqual(result['balances'][0]['total'],'0.00')

    def test_openrouter_key_limit_is_not_account_balance(self):
        for remaining in [None,0,7.5]:
            FakeUpstream.response={'data':{'limit_remaining':remaining,'usage':2.5,'label':SECRET}}
            result=ai_probes.run(self.settings,'balance','openrouter')
            self.assertEqual(result['status'],'ok'); self.assertEqual(result['scope'],'key_limit')
            self.assertEqual(result['balances'][0]['total'],None if remaining is None else str(remaining))
            self.assertNotIn(SECRET,json.dumps(result))

    def test_compatible_balance_and_unsupported_never_become_zero(self):
        FakeUpstream.response={'total_available':'3.45','secret':SECRET}
        result=ai_probes.run(self.settings,'balance','credit_grants')
        self.assertEqual(result['balances'][0],{'currency':'USD','total':'3.45'})
        self.assertEqual(FakeUpstream.calls[0][0],'/v1/dashboard/billing/credit_grants')
        FakeUpstream.calls=[]
        result=ai_probes.run(self.settings,'balance')
        self.assertEqual(result['status'],'unsupported'); self.assertNotIn('balances',result)
        self.assertEqual(FakeUpstream.calls,[])
        for amount in ['NaN','Infinity',True,'1e999999999','1e-999999999',None,'bad']:
            FakeUpstream.response={'total_available':amount}
            result=ai_probes.run(self.settings,'balance','credit_grants')
            self.assertEqual(result['status'],'schema'); self.assertNotIn('balances',result)

    def test_auto_protocol_requires_exact_official_address(self):
        for url,expected in [('https://api.deepseek.com/v1','deepseek'),
                             ('https://openrouter.ai/api/v1','openrouter'),
                             ('https://api.deepseek.com.attacker.test','unsupported'),
                             ('https://api.deepseek.com/custom','unsupported')]:
            self.assertEqual(ai_probes.balance_protocol(replace(SETTINGS,base_url=url)),expected)


class KeySchemaMigrationTests(unittest.TestCase):
    def test_existing_key_rows_are_preserved_and_migration_is_repeatable(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory=sqlite3.Row
            db.execute('''CREATE TABLE ai_service_keys(id TEXT PRIMARY KEY,name TEXT,base_url TEXT,
                       model TEXT,secret_value TEXT,disable_thinking INTEGER,revision TEXT,created_at TEXT)''')
            db.execute("INSERT INTO ai_service_keys VALUES ('old','legacy','https://example.test',"
                       "'legacy-model','encrypted-synthetic',0,'revision','2026-10-12')")
            ai_keys.migrate(db); ai_keys.migrate(db)
            row=dict(db.execute('SELECT * FROM ai_service_keys').fetchone())
            self.assertEqual(row['secret_value'],'encrypted-synthetic')
            self.assertEqual(row['revision'],'revision')
            self.assertEqual(row['usage'],'all'); self.assertEqual(row['balance_protocol'],'auto')
            self.assertEqual(row['json_mode'],1); self.assertEqual(row['vision'],1)
            self.assertEqual(row['disable_thinking'],0)
            self.assertEqual(row['reasoning_effort'],''); self.assertEqual(row['reasoning_protocol'],'')


class ProbeAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.old=module.store; self.old_limiter=module.ai_probe_limiter
        module.ai_probe_limiter=RateLimiter({'actor':(30,60),'target':(4,60)})
        self.env=patch.dict(os.environ,{'TAXPEARLS_NOTIFICATION_EMAIL_ENABLED':'0','TAXPEARLS_MATERIAL_QUEUE':'0',
                                       'TAXPEARLS_FIELD_KEY':base64.b64encode(os.urandom(32)).decode()})
        self.env.start()
        self.store=Store(Path(self.tmp.name)/'probe.db'); module.store=self.store
        self.password='Probe-testing-2026!'
        self.root=self.store.create_user('platform',self.password,'平台','platform_admin','platform')
        self.owner=self.store.create_user('owner',self.password,'机构','org_admin','alpha')
        self.settings=patch.object(module._environment_settings.AISettings,'from_env',return_value=SETTINGS)
        self.settings.start()
        self.client=TestClient(module.app); self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None); module.store=self.old; module.ai_probe_limiter=self.old_limiter
        self.settings.stop(); self.env.stop(); self.tmp.cleanup()

    def login(self,who):
        self.client.cookies.clear()
        self.client.cookies.set(module.COOKIE_NAME,self.store.authenticate(who['username'],self.password)[1])

    def test_root_only_both_endpoints_before_any_network(self):
        with patch.object(ai_probes,'run') as run:
            self.assertEqual(self.client.get('/api/admin/ai-keys').status_code,401)
            self.assertEqual(self.client.post('/api/admin/ai-keys/probe',json={'operation':'health'}).status_code,401)
            roles=[self.owner]+[self.store.create_user(role,self.password,role,role,'alpha')
                               for role in ['teacher','student','accountant']]
            for who in roles:
                self.login(who)
                self.assertEqual(self.client.get('/api/admin/ai-keys').status_code,403)
                self.assertEqual(self.client.post('/api/admin/ai-keys/probe',json={'operation':'health'}).status_code,403)
            run.assert_not_called()

    def test_catalog_masking_disabled_ai_single_selection_persistence_and_config_change(self):
        self.login(self.root); response=self.client.get('/api/admin/ai-keys')
        self.assertIn('no-store',response.headers['cache-control']); self.assertNotIn(SECRET,response.text)
        data=response.json(); self.assertFalse(data['enabled']); self.assertEqual(len(data['keys']),2)
        key=data['keys'][1]
        outcome={'operation':'health','status':'ok','message':'检测成功','checked_at':'2026-10-12T01:00:00Z','elapsed_ms':1}
        with patch.object(ai_probes,'run',return_value=outcome) as run:
            r=self.client.post('/api/admin/ai-keys/probe',json={'operation':'health','key_id':key['id']})
            self.assertEqual(r.status_code,200,r.text)
            selected=run.call_args.args[0]; self.assertEqual(selected.api_keys,('synthetic-backup',))
            self.assertTrue(selected.enabled)
        self.assertEqual(self.client.get('/api/admin/ai-keys').json()['keys'][1]['health'],outcome)
        for _ in range(301):
            self.store.log(self.root,'unrelated_action','system','synthetic')
        self.assertEqual(self.client.get('/api/admin/ai-keys').json()['keys'][1]['health'],outcome)
        self.assertNotIn(SECRET,json.dumps(self.store.list_logs(self.root)))
        self.settings.stop(); self.settings=patch.object(module._environment_settings.AISettings,'from_env',
                                                        return_value=replace(SETTINGS,model='new-model'))
        self.settings.start()
        self.assertIsNone(self.client.get('/api/admin/ai-keys').json()['keys'][1]['health'])
        self.assertEqual(self.client.post('/api/admin/ai-keys/probe',json={'operation':'health','key_id':key['id']}).status_code,404)

    def test_temporary_secrets_not_retained_origin_validation_and_rate_limit(self):
        self.login(self.root)
        body={'operation':'balance','api_key':SECRET,'provider':'openrouter'}
        outcome={'operation':'balance','status':'unsupported','message':'暂不支持'}
        with patch.object(ai_probes,'run',return_value=outcome) as run:
            self.assertEqual(self.client.post('/api/admin/ai-keys/probe',json=body,
                                              headers={'Origin':'https://attacker.test'}).status_code,403)
            run.assert_not_called()
            for _ in range(4):
                response=self.client.post('/api/admin/ai-keys/probe',json=body)
                self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(self.client.post('/api/admin/ai-keys/probe',json=body).status_code,429)
            self.assertEqual(run.call_count,4)
        logs=self.store.list_logs(self.root)
        self.assertNotIn(SECRET,json.dumps(logs)); self.assertNotIn('balances',json.dumps(logs))
        self.assertEqual(self.client.get('/api/admin/ai-keys').json()['keys'][0]['balance'],None)

    def test_invalid_and_mixed_requests_do_not_echo_or_send_credentials(self):
        self.login(self.root); key=self.client.get('/api/admin/ai-keys').json()['keys'][0]['id']
        with patch.object(ai_probes,'run') as run:
            for body in [{'operation':'health','key_id':key,'api_key':SECRET},
                         {'operation':'health','api_key':SECRET,'base_url':'http://private.test'},
                         {'operation':'health','api_key':'secret\nheader'},
                         {'operation':'health','api_key':SECRET,'provider':'openrouter'}]:
                response=self.client.post('/api/admin/ai-keys/probe',json=body)
                self.assertEqual(response.status_code,422,response.text); self.assertNotIn(SECRET,response.text)
            run.assert_not_called()

    def test_role_revocation_during_probe_discards_sensitive_result(self):
        self.login(self.root)
        key=self.client.get('/api/admin/ai-keys').json()['keys'][0]['id']
        def revoke(*_):
            with self.store.connect() as db:
                db.execute("UPDATE users SET role='org_admin' WHERE id=?",(self.root['id'],))
            return {'operation':'balance','status':'ok','balances':[{'currency':'CNY','total':'123.45'}]}
        with patch.object(ai_probes,'run',side_effect=revoke):
            response=self.client.post('/api/admin/ai-keys/probe',json={'operation':'balance','key_id':key})
        self.assertEqual(response.status_code,403)
        self.assertNotIn('123.45',response.text)

    def test_add_probe_encrypted_persistence_and_delete_updates_business_credentials(self):
        self.login(self.root)
        body={'name':'新增备用','api_key':'synthetic-added-key','provider':'configured'}
        result=self.client.post('/api/admin/ai-keys',json=body)
        self.assertEqual(result.status_code,200,result.text)
        self.assertNotIn(body['api_key'],result.text)
        data=self.client.get('/api/admin/ai-keys').json()
        added=next(row for row in data['keys'] if row['name']==body['name'])
        self.assertTrue(added['in_call_pool']); self.assertEqual(added['source'],'database')
        self.assertNotIn(body['api_key'],json.dumps(data))
        import sqlite3
        with closing(sqlite3.connect(self.store.path)) as db:
            stored=db.execute('SELECT secret_value FROM ai_service_keys').fetchone()[0]
            self.assertTrue(stored.startswith('tp-field-v1:')); self.assertNotIn(body['api_key'],stored)
        self.assertEqual(ai_keys.apply(SETTINGS,self.store).api_keys,(SECRET,'synthetic-backup',body['api_key']))
        with patch.object(ai_probes,'run',return_value={'operation':'health','status':'ok'}) as run:
            response=self.client.post('/api/admin/ai-keys/probe',json={'operation':'health','key_id':added['id']})
            self.assertEqual(response.status_code,200); self.assertEqual(run.call_args.args[0].api_keys,(body['api_key'],))
        self.assertEqual(self.client.post('/api/admin/ai-keys',json=body).status_code,409)
        self.assertEqual(self.client.request('DELETE','/api/admin/ai-keys/'+added['id'],json={'revision':added['revision']}).status_code,200)
        self.assertNotIn(body['api_key'],ai_keys.apply(SETTINGS,self.store).api_keys)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_service_keys').fetchone()[0],0)
        # An environment key stays excluded after reopening, even without editing .env.
        primary=data['keys'][0]
        self.assertEqual(self.client.request('DELETE','/api/admin/ai-keys/'+primary['id'],json={'revision':primary['revision']}).status_code,200)
        reopened=Store(self.store.path)
        self.assertEqual(ai_keys.apply(SETTINGS,reopened).api_keys,('synthetic-backup',))
        self.assertEqual(self.client.post('/api/admin/ai-keys/probe',json={'operation':'health','key_id':primary['id']}).status_code,404)
        readd=self.client.post('/api/admin/ai-keys',json={'name':'恢复主用','api_key':SECRET})
        self.assertEqual(readd.status_code,200,readd.text)
        current=next(row for row in self.client.get('/api/admin/ai-keys').json()['keys'] if row['id']==primary['id'])
        self.assertNotEqual(current['revision'],primary['revision'])
        self.assertEqual(self.client.request('DELETE','/api/admin/ai-keys/'+primary['id'],json={'revision':primary['revision']}).status_code,409)
        self.assertIn(SECRET,ai_keys.apply(SETTINGS,reopened).api_keys)
        self.assertNotIn(body['api_key'],json.dumps(self.store.list_logs(self.root)))
        # Rejoining the same environment credential with another model retains that model.
        self.assertEqual(self.client.request('DELETE','/api/admin/ai-keys/'+current['id'],json={'revision':current['revision']}).status_code,200)
        response=self.client.post('/api/admin/ai-keys',json={'name':'独立检测','api_key':SECRET,'model':'another-model'})
        self.assertEqual(response.status_code,200,response.text)
        standalone=next(row for row in self.client.get('/api/admin/ai-keys').json()['keys'] if row['name']=='独立检测')
        self.assertEqual(standalone['model'],'another-model'); self.assertFalse(standalone['in_call_pool'])
        self.assertNotIn(SECRET,ai_keys.apply(SETTINGS,reopened).api_keys)

    def test_management_permissions_validation_origin_and_off_pool(self):
        self.login(self.root)
        body={'name':'异模型检测','api_key':'synthetic-standalone','model':'different-model'}
        response=self.client.post('/api/admin/ai-keys',json=body)
        self.assertEqual(response.status_code,200,response.text)
        key=next(row for row in self.client.get('/api/admin/ai-keys').json()['keys'] if row['name']==body['name'])
        self.assertFalse(key['in_call_pool']); self.assertNotIn(body['api_key'],ai_keys.apply(SETTINGS,self.store).api_keys)
        for headers in [{'Origin':'https://attacker.test'},{'Sec-Fetch-Site':'cross-site'}]:
            self.assertEqual(self.client.post('/api/admin/ai-keys',json={**body,'api_key':'another'},headers=headers).status_code,403)
            self.assertEqual(self.client.request('DELETE','/api/admin/ai-keys/'+key['id'],json={'revision':key['revision']},headers=headers).status_code,403)
        for change in [{'name':' '},{'api_key':'invalid\nkey'},{'provider':'openrouter','model':''},{'extra':'NEVER-ECHO'}]:
            response=self.client.post('/api/admin/ai-keys',json={**body,**change})
            self.assertEqual(response.status_code,422,response.text); self.assertNotIn(body['api_key'],response.text)
        self.login(self.owner)
        self.assertEqual(self.client.post('/api/admin/ai-keys',json=body).status_code,403)
        self.assertEqual(self.client.request('DELETE','/api/admin/ai-keys/'+key['id'],json={'revision':key['revision']}).status_code,403)
        self.client.cookies.clear()
        self.assertEqual(self.client.post('/api/admin/ai-keys',json=body).status_code,401)
        self.assertEqual(self.client.request('DELETE','/api/admin/ai-keys/'+key['id'],json={'revision':key['revision']}).status_code,401)

    def test_last_key_deleted_no_business_fallback_and_material_config_reflects_additions(self):
        self.login(self.root)
        for key in self.client.get('/api/admin/ai-keys').json()['keys']:
            self.assertEqual(self.client.request('DELETE','/api/admin/ai-keys/'+key['id'],json={'revision':key['revision']}).status_code,200)
        effective=ai_keys.apply(replace(SETTINGS,enabled=True),self.store)
        self.assertEqual(effective.api_keys,()); self.assertTrue(effective.problem())
        with patch.object(module._environment_settings.AISettings,'from_env',return_value=replace(SETTINGS,enabled=True)):
            from webapp import knowledge
            with patch.object(knowledge,'chat_content') as transport:
                with self.assertRaises(Exception) as failed: knowledge._chat_json('synthetic',{},'failed')
                self.assertEqual(failed.exception.status_code,503); transport.assert_not_called()
        self.assertEqual(self.client.post('/api/admin/ai-keys',json={'name':'新备用','api_key':'synthetic-new-key'}).status_code,200)
        self.login(self.owner)
        config=self.client.get('/api/materials/config')
        self.assertEqual(config.status_code,200,config.text)
        self.assertEqual(config.json()['backup_key_count'],0)
        self.assertNotIn('synthetic-new-key',config.text)

    def test_purpose_assignment_routes_models_and_never_borrows_another_purpose(self):
        self.login(self.root)
        for usage in ['extraction','knowledge','report','tutor']:
            response=self.client.post('/api/admin/ai-keys',json={
                'name':usage,'api_key':'synthetic-'+usage,'base_url':'https://'+usage+'.example/v1',
                'model':usage+'-model','usage':usage,'disable_thinking':False,'vision':False})
            self.assertEqual(response.status_code,200,response.text)
        base=replace(SETTINGS,enabled=True)
        for usage in ['extraction','knowledge','report','tutor']:
            effective=ai_keys.apply(base,self.store,usage=usage)
            self.assertEqual(effective.api_keys,('synthetic-'+usage,))
            self.assertEqual(effective.base_url,'https://'+usage+'.example/v1')
            self.assertEqual(effective.model,usage+'-model')
            self.assertTrue(effective.enabled); self.assertFalse(effective.disable_thinking)
        # Compatible backups are included; another endpoint/model in the same purpose is not replayed.
        self.assertEqual(self.client.post('/api/admin/ai-keys',json={'name':'知识备用','api_key':'synthetic-knowledge-backup',
            'base_url':'https://knowledge.example/v1','model':'knowledge-model','usage':'knowledge',
            'disable_thinking':False,'vision':False}).status_code,200)
        self.assertEqual(ai_keys.apply(base,self.store,usage='knowledge').api_keys,
                         ('synthetic-knowledge','synthetic-knowledge-backup'))
        self.assertEqual(ai_keys.apply(base,self.store,usage='extraction').api_keys,('synthetic-extraction',))
        self.assertFalse(ai_keys.apply(SETTINGS,self.store,usage='knowledge').enabled)
        with patch.object(module._environment_settings.AISettings,'from_env',return_value=base):
            from webapp import knowledge
            with patch.object(knowledge,'chat_content',return_value='{"answer":"synthetic"}') as transport:
                knowledge._chat_json('synthetic',{},'failed')
                self.assertEqual(transport.call_args.args[0].model,'knowledge-model')
                knowledge._chat_json('synthetic',{},'failed',usage='report')
                self.assertEqual(transport.call_args.args[0].model,'report-model')
        self.login(self.owner)
        config=self.client.get('/api/materials/config').json()
        self.assertEqual(config['model'],'extraction-model')

    def test_one_key_can_serve_multiple_purposes_and_changes_survive_restart(self):
        self.login(self.root)
        body={'name':'提取和问答','api_key':'synthetic-multi-key','base_url':'https://multi.example/v1',
              'model':'multi-model','usages':['extraction','knowledge'],'reasoning_effort':'default'}
        response=self.client.post('/api/admin/ai-keys',json=body)
        self.assertEqual(response.status_code,200,response.text)
        row=next(key for key in self.client.get('/api/admin/ai-keys').json()['keys'] if key['name']==body['name'])
        self.assertEqual(row['usages'],['extraction','knowledge'])
        for purpose in ['extraction','knowledge']:
            effective=ai_keys.apply(replace(SETTINGS,enabled=True),Store(self.store.path),usage=purpose)
            self.assertEqual(effective.api_keys,(body['api_key'],))
            self.assertEqual(effective.model,'multi-model')
        for purpose in ['report','tutor']:
            self.assertNotIn(body['api_key'],ai_keys.apply(SETTINGS,self.store,usage=purpose).api_keys)
        response=self.client.put('/api/admin/ai-keys/'+row['id'],json={'revision':row['revision'],'usages':['knowledge','report','tutor']})
        self.assertEqual(response.status_code,200,response.text)
        updated=next(key for key in self.client.get('/api/admin/ai-keys').json()['keys'] if key['name']==body['name'])
        self.assertNotEqual(updated['revision'],row['revision'])
        self.assertNotIn(body['api_key'],ai_keys.apply(SETTINGS,self.store,usage='extraction').api_keys)
        for purpose in ['knowledge','report','tutor']:
            self.assertEqual(ai_keys.apply(SETTINGS,Store(self.store.path),usage=purpose).api_keys,(body['api_key'],))
        self.assertEqual(self.client.put('/api/admin/ai-keys/'+updated['id'],json={'revision':updated['revision'],'usages':[]}).status_code,200)
        for purpose in ['extraction','knowledge','report','tutor']:
            self.assertNotIn(body['api_key'],ai_keys.apply(SETTINGS,self.store,usage=purpose).api_keys)

    def test_multi_use_validation_legacy_compatibility_and_conflicts(self):
        self.login(self.root)
        row=self.client.get('/api/admin/ai-keys').json()['keys'][0]
        self.assertEqual(row['usages'],['extraction','knowledge','report','tutor'])
        for change in [{'usages':['unknown']},{'usages':['none','knowledge']},{'usages':['all']},
                       {'usages':['knowledge','knowledge']},{'usages':['knowledge'],'usage':'report'},
                       {'usages':'knowledge'},{'usages':None}]:
            response=self.client.put('/api/admin/ai-keys/'+row['id'],json={'revision':row['revision'],**change})
            self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.client.put('/api/admin/ai-keys/'+row['id'],json={'revision':row['revision'],'usages':['extraction','knowledge']}).status_code,200)
        fresh=self.client.get('/api/admin/ai-keys').json()['keys'][0]
        self.assertEqual(self.client.put('/api/admin/ai-keys/'+fresh['id'],json={'revision':row['revision'],'usages':['tutor']}).status_code,409)
        self.assertEqual(ai_keys.apply(SETTINGS,self.store,usage='extraction').api_keys,(SECRET,'synthetic-backup'))
        self.assertEqual(ai_keys.apply(SETTINGS,self.store,usage='report').api_keys,('synthetic-backup',))
        self.assertEqual(self.client.put('/api/admin/ai-keys/'+fresh['id'],json={'revision':fresh['revision'],'usage':'tutor'}).status_code,200)
        self.assertEqual(self.client.get('/api/admin/ai-keys').json()['keys'][0]['usages'],['tutor'])

    def test_edit_url_secret_preservation_reveal_and_revision_conflicts(self):
        self.login(self.root)
        primary=self.client.get('/api/admin/ai-keys').json()['keys'][0]
        original=primary['id']; url='/api/admin/ai-keys/'+original
        edit={'revision':primary['revision'],'name':'提取配置','usage':'extraction',
              'base_url':'https://custom.example/v1','model':'custom-model','balance_protocol':'credit_grants'}
        response=self.client.put(url,json=edit); self.assertEqual(response.status_code,200,response.text)
        row=next(item for item in self.client.get('/api/admin/ai-keys').json()['keys'] if item['name']=='提取配置')
        self.assertNotEqual(row['id'],original); self.assertNotEqual(row['revision'],primary['revision'])
        self.assertEqual(row['usage'],'extraction'); self.assertEqual(row['base_url'],edit['base_url'])
        revealed=self.client.post('/api/admin/ai-keys/'+row['id']+'/reveal',json={'revision':row['revision']})
        self.assertEqual(revealed.status_code,200); self.assertEqual(revealed.json()['api_key'],SECRET)
        self.assertIn('no-store',revealed.headers['cache-control'])
        self.assertNotIn(SECRET,self.client.get('/api/admin/ai-keys').text)
        self.assertNotIn(SECRET,json.dumps(self.store.list_logs(self.root)))
        with patch.object(ai_probes,'run',return_value={'status':'ok','operation':'balance'}) as run:
            self.assertEqual(self.client.post('/api/admin/ai-keys/probe',json={'key_id':row['id'],'operation':'balance'}).status_code,200)
            self.assertEqual(run.call_args.args[2],'credit_grants')
        self.assertEqual(self.client.put(url,json=edit).status_code,404)
        replacement=self.client.put('/api/admin/ai-keys/'+row['id'],json={
            'revision':row['revision'],'api_key':'synthetic-edited-secret'})
        self.assertEqual(replacement.status_code,200,replacement.text)
        updated=next(item for item in self.client.get('/api/admin/ai-keys').json()['keys'] if item['name']=='提取配置')
        self.assertEqual(ai_keys.apply(SETTINGS,self.store,usage='extraction').api_keys,('synthetic-edited-secret',))
        self.assertEqual(ai_keys.apply(SETTINGS,self.store,usage='knowledge').api_keys,('synthetic-backup',))
        reopened=Store(self.store.path)
        self.assertEqual(ai_keys.apply(SETTINGS,reopened,usage='extraction').base_url,'https://custom.example/v1')
        stale=self.client.put('/api/admin/ai-keys/'+updated['id'],json={'revision':row['revision'],'usage':'none'})
        self.assertEqual(stale.status_code,409)

    def test_edit_reveal_permissions_origin_validation_and_atomicity(self):
        self.login(self.root); row=self.client.get('/api/admin/ai-keys').json()['keys'][0]
        url='/api/admin/ai-keys/'+row['id']; body={'revision':row['revision']}
        for change in [{'base_url':'http://private.example'},{'base_url':'https://host.example/?secret=x'},
                       {'usage':'unknown'},{'api_key':'invalid\nkey'},{'json_mode':'false'}]:
            response=self.client.put(url,json={**body,**change})
            self.assertEqual(response.status_code,422,response.text); self.assertNotIn(SECRET,response.text)
        for path,method in [(url,'PUT'),(url+'/reveal','POST')]:
            response=self.client.request(method,path,json=body,headers={'Origin':'https://attacker.example'})
            self.assertEqual(response.status_code,403)
        with patch.object(self.store,'_log',side_effect=RuntimeError('synthetic audit failure')):
            with self.assertRaises(RuntimeError):
                self.client.put(url,json={**body,'usage':'none'})
        self.assertIn(SECRET,ai_keys.apply(SETTINGS,self.store,usage='knowledge').api_keys)
        self.login(self.owner)
        self.assertEqual(self.client.put(url,json=body).status_code,403)
        self.assertEqual(self.client.post(url+'/reveal',json=body).status_code,403)
        self.client.cookies.clear()
        self.assertEqual(self.client.put(url,json=body).status_code,401)
        self.assertEqual(self.client.post(url+'/reveal',json=body).status_code,401)

    def test_assign_none_suppresses_environment_credential_after_restart(self):
        self.login(self.root); row=self.client.get('/api/admin/ai-keys').json()['keys'][0]
        response=self.client.put('/api/admin/ai-keys/'+row['id'],json={'revision':row['revision'],'usage':'none'})
        self.assertEqual(response.status_code,200,response.text)
        for usage in ['extraction','knowledge','report','tutor']:
            self.assertEqual(ai_keys.apply(SETTINGS,Store(self.store.path),usage=usage).api_keys,('synthetic-backup',))

    def test_models_uses_saved_or_draft_credentials_without_saving_or_exposing_them(self):
        self.login(self.root); row=self.client.get('/api/admin/ai-keys').json()['keys'][0]
        body={'base_url':'https://custom.example/v1','key_id':row['id'],'revision':row['revision']}
        with patch.object(ai_probes,'_http_json',return_value={'data':[{'id':'custom-model','key':SECRET}]}) as fetch:
            response=self.client.post('/api/admin/ai-keys/models',json=body)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['models'],['custom-model'])
            self.assertEqual(response.json()['capabilities']['custom-model']['state'],'unknown')
            self.assertEqual(fetch.call_args.args,('https://custom.example/v1/models',SECRET))
            self.assertIn('no-store',response.headers['cache-control']); self.assertNotIn(SECRET,response.text)
            draft={**body,'api_key':'synthetic-draft-key'}
            self.assertEqual(self.client.post('/api/admin/ai-keys/models',json=draft).status_code,200)
            self.assertEqual(fetch.call_args.args[1],draft['api_key'])
            fresh={'base_url':body['base_url'],'api_key':'synthetic-new-key'}
            self.assertEqual(self.client.post('/api/admin/ai-keys/models',json=fresh).status_code,200)
            self.assertEqual(fetch.call_args.args[1],fresh['api_key'])
        self.assertEqual(self.client.get('/api/admin/ai-keys').json()['keys'][0]['revision'],row['revision'])
        self.assertNotIn('synthetic-draft-key',json.dumps(self.store.list_logs(self.root)))
        self.assertNotIn(SECRET,json.dumps(self.store.list_logs(self.root)))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_service_keys').fetchone()[0],0)

    def test_models_access_validation_stale_requests_and_sanitized_failures(self):
        body={'base_url':'https://custom.example/v1','api_key':SECRET}
        with patch.object(ai_probes,'models',return_value=['model']) as fetch:
            self.assertEqual(self.client.post('/api/admin/ai-keys/models',json=body).status_code,401)
            self.login(self.owner)
            self.assertEqual(self.client.post('/api/admin/ai-keys/models',json=body).status_code,403)
            self.login(self.root)
            self.assertEqual(self.client.post('/api/admin/ai-keys/models',json=body,
                headers={'Origin':'https://attacker.example'}).status_code,403)
            for change in [{'base_url':'http://private.example'},{'base_url':'https://host/?key=x'},
                           {'api_key':''},{'api_key':'bad\nkey'},{'revision':'invalid'}]:
                response=self.client.post('/api/admin/ai-keys/models',json={**body,**change})
                self.assertEqual(response.status_code,422,response.text); self.assertNotIn(SECRET,response.text)
            fetch.assert_not_called()
            row=self.client.get('/api/admin/ai-keys').json()['keys'][0]
            saved={**body,'api_key':'','key_id':row['id'],'revision':'stale'}
            self.assertEqual(self.client.post('/api/admin/ai-keys/models',json=saved).status_code,409)
            fetch.assert_not_called()
        with patch.object(ai_probes,'models',side_effect=ai_probes.TransportError('http',401)):
            response=self.client.post('/api/admin/ai-keys/models',json=body)
            self.assertEqual(response.status_code,502); self.assertNotIn(SECRET,response.text)
        self.client.cookies.clear()

    def test_models_role_revocation_or_key_change_during_network_discards_result(self):
        self.login(self.root); row=self.client.get('/api/admin/ai-keys').json()['keys'][0]
        body={'base_url':SETTINGS.base_url,'key_id':row['id'],'revision':row['revision']}
        def changed(_settings, **_kwargs):
            ai_keys.update(self.store,self.root,SETTINGS,row['id'],row['revision'],usage='none')
            return ['do-not-return']
        with patch.object(ai_probes,'models',side_effect=changed):
            response=self.client.post('/api/admin/ai-keys/models',json=body)
            self.assertEqual(response.status_code,409); self.assertNotIn('do-not-return',response.text)
        def revoked(_settings, **_kwargs):
            with self.store.connect() as db:
                db.execute("UPDATE users SET role='org_admin' WHERE id=?",(self.root['id'],))
            return ['do-not-return']
        with patch.object(ai_probes,'models',side_effect=revoked):
            response=self.client.post('/api/admin/ai-keys/models',json={'base_url':SETTINGS.base_url,'api_key':SECRET})
            self.assertEqual(response.status_code,403); self.assertNotIn('do-not-return',response.text)

    def test_reasoning_level_requires_matching_upstream_capability_and_persists_into_business_calls(self):
        self.login(self.root); row=self.client.get('/api/admin/ai-keys').json()['keys'][0]
        url='/api/admin/ai-keys/'+row['id']; model=row['model']
        edit={'revision':row['revision'],'reasoning_effort':'max'}
        self.assertEqual(self.client.put(url,json=edit).status_code,422)
        data={'data':[{'id':model,'effort':{'supported_levels':['low','high','max']}}]}
        with patch.object(ai_probes,'_http_json',return_value=data):
            response=self.client.post('/api/admin/ai-keys/models',json={
                'base_url':SETTINGS.base_url,'key_id':row['id'],'revision':row['revision']})
            self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.client.put(url,json={**edit,'reasoning_effort':'medium'}).status_code,422)
        self.assertEqual(self.client.put(url,json=edit).status_code,200)
        fresh=next(key for key in self.client.get('/api/admin/ai-keys').json()['keys'] if key['name']==row['name'])
        self.assertEqual(fresh['reasoning_effort'],'max'); self.assertFalse(fresh['disable_thinking'])
        self.assertEqual(fresh['reasoning_capability']['levels'],['low','high','max'])
        effective=ai_keys.apply(replace(SETTINGS,enabled=True),Store(self.store.path),usage='knowledge')
        self.assertEqual(effective.reasoning_effort,'max'); self.assertEqual(effective.reasoning_protocol,'deepseek')
        self.assertEqual(effective.api_keys,(SECRET,))
        self.assertNotIn(SECRET,self.client.get('/api/admin/ai-keys').text)
        # The same model on another credential or URL cannot reuse this key's capability evidence.
        for change in [{'api_key':'synthetic-other-secret'},{'base_url':'https://other.example/v1'},{'model':'unknown-model'}]:
            response=self.client.put('/api/admin/ai-keys/'+fresh['id'],json={
                'revision':fresh['revision'],'reasoning_effort':'max',**change})
            self.assertEqual(response.status_code,422,response.text)
        # An upstream capability refresh that removes a saved level fences further calls.
        with patch.object(ai_probes,'_http_json',return_value={'data':[{'id':model,'effort':{'supported_levels':['low']}}]}):
            self.assertEqual(self.client.post('/api/admin/ai-keys/models',json={
                'base_url':SETTINGS.base_url,'key_id':fresh['id'],'revision':fresh['revision']}).status_code,200)
        self.assertEqual(ai_keys.apply(replace(SETTINGS,enabled=True),self.store).api_keys,())
        with patch.object(ai_probes,'run') as run:
            response=self.client.post('/api/admin/ai-keys/probe',json={'key_id':fresh['id'],'operation':'health'})
            self.assertEqual(response.status_code,422); run.assert_not_called()
        self.assertEqual(self.client.put('/api/admin/ai-keys/'+fresh['id'],json={
            'revision':fresh['revision'],'reasoning_effort':'default'}).status_code,200)
        self.assertIn(SECRET,ai_keys.apply(replace(SETTINGS,enabled=True),self.store).api_keys)

    def test_mandatory_toggle_and_unknown_reasoning_validate_even_if_client_bypasses_dropdown(self):
        self.login(self.root); row=self.client.get('/api/admin/ai-keys').json()['keys'][0]
        models=[{'id':'mandatory','reasoning':{'supported_efforts':['medium','high'],'mandatory':True}},
                {'id':'toggle','reasoning':{}},{'id':'plain','supported_parameters':['temperature']},{'id':'unknown'}]
        with patch.object(ai_probes,'_http_json',return_value={'data':models}):
            fetched=self.client.post('/api/admin/ai-keys/models',json={
                'base_url':SETTINGS.base_url,'key_id':row['id'],'revision':row['revision']})
            self.assertEqual(fetched.status_code,200,fetched.text)
        url='/api/admin/ai-keys/'+row['id']
        for model,effort in [('mandatory','none'),('toggle','high'),('plain','enabled'),('unknown','high'),('unknown','none')]:
            response=self.client.put(url,json={'revision':row['revision'],'model':model,'reasoning_effort':effort})
            self.assertEqual(response.status_code,422,response.text)
        # Explicit upstream default sends no unsupported thinking parameter.
        self.assertEqual(self.client.put(url,json={'revision':row['revision'],'model':'mandatory','usage':'knowledge','reasoning_effort':'default'}).status_code,200)
        fresh=self.client.get('/api/admin/ai-keys').json()['keys'][0]
        self.assertEqual(fresh['reasoning_effort'],'default'); self.assertFalse(fresh['disable_thinking'])
        self.assertFalse(fresh['reasoning_capability']['can_disable'])
        self.assertEqual(self.client.put('/api/admin/ai-keys/'+fresh['id'],json={
            'revision':fresh['revision'],'reasoning_effort':'high'}).status_code,200)
        effective=ai_keys.apply(replace(SETTINGS,enabled=True),self.store,usage='knowledge')
        self.assertEqual(ai_reasoning.parameters(effective),{'reasoning':{'effort':'high'}})


if __name__ == '__main__':
    unittest.main()
