"""Local consent and authenticated adapter regression; never calls Feishu."""
import base64
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from webapp import app as module, channel_bindings, feishu_binding as linking
from webapp.storage import Store


class FeishuBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            'TAXPEARLS_FEISHU_BINDING_ENABLED': '1', 'TAXPEARLS_FEISHU_APP_ID': 'cli_test',
            'TAXPEARLS_FEISHU_APP_TENANT_KEY': 'owner_tenant', 'TAXPEARLS_FEISHU_BRIDGE_KEY': 'ab' * 32,
            'TAXPEARLS_PUBLIC_BASE_URL': 'https://taxpearls.wxtech.site',
            'TAXPEARLS_FIELD_KEY': base64.b64encode(os.urandom(32)).decode(),
            'TAXPEARLS_MATERIAL_QUEUE_ENABLED': '0', 'TAXPEARLS_AI_ENABLED': '0',
            'TAXPEARLS_NOTIFICATION_EMAIL_ENABLED': '0',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = Store(Path(self.tmp.name) / 'binding.db', keepalive=True)
        self.addCleanup(self.store.close)
        replacement = patch.object(module, 'store', self.store)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.user = self.store.create_user('alice', 'Binding-safe-2026!', '示例会计', 'accountant', 'enterprise-a')
        self.other = self.store.create_user('bob', 'Binding-safe-2026!', '另一会计', 'accountant', 'enterprise-b')
        self.session = self.store.authenticate('alice', 'Binding-safe-2026!')[1]
        self.other_session = self.store.authenticate('bob', 'Binding-safe-2026!')[1]
        self.config = linking.settings()
        self.client = TestClient(module.app, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.cookies.set(module.COOKIE_NAME, self.session)
        self.proofs = {}

    def bridge(self, operation, *, nonce=None, stamp=None, key=None, **values):
        raw = json.dumps({'app_id': 'cli_test', 'operation': operation, **values}, separators=(',', ':')).encode()
        stamp, nonce = str(int(time.time()) if stamp is None else stamp), nonce or secrets.token_hex(16)
        message = '\n'.join(['POST', linking.INTERNAL_PATH, stamp, nonce, hashlib.sha256(raw).hexdigest()])
        signature = hmac.new(key or self.config.bridge_key, message.encode(), hashlib.sha256).hexdigest()
        return self.client.post(linking.INTERNAL_PATH, content=raw, headers={
            'Content-Type': 'application/json', 'x-tp-time': stamp, 'x-tp-nonce': nonce, 'x-tp-signature': signature})

    def issue(self, subject='ou_alice', chat='oc_alice', event_id='evt_1'):
        response = self.bridge('issue', subject=subject, chat=chat, event_id=event_id)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['url'].split('#ticket=')[1]

    def claim(self, ticket):
        if ticket not in self.proofs:
            response = self.client.post('/api/channels/feishu/status', json={'ticket': ticket})
            if response.status_code != 200:
                return response
            self.proofs[ticket] = response.json()['web_proof']
        return self.client.post('/api/channels/feishu/claim', json={'ticket': ticket, 'web_proof': self.proofs[ticket]})

    def prompt(self):
        response = self.bridge('pending')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['items'][0]

    def finish(self, item, operation='confirm', **overrides):
        values = {key: item[key] for key in ('id', 'subject', 'chat', 'confirmation')}
        return self.bridge(operation, **{**values, **overrides})

    def complete(self):
        ticket = self.issue()
        self.assertEqual(self.claim(ticket).status_code, 200)
        prompt = self.prompt()
        self.assertEqual(self.finish(prompt).status_code, 200)
        return ticket, prompt

    def test_two_consents_are_required_and_identity_resolves_only_at_end(self):
        ticket = self.issue()
        self.assertIsNone(channel_bindings.actor_for_subject(self.store, self.config.scope, 'ou_alice'))
        self.assertEqual(self.claim(ticket).json()['state'], 'web_confirmed')
        self.assertIsNone(channel_bindings.actor_for_subject(self.store, self.config.scope, 'ou_alice'))
        prompt = self.prompt()
        self.assertEqual(prompt['account_hint'], 'al***e')
        self.assertEqual(self.finish(prompt).json()['state'], 'completed')
        actor = channel_bindings.actor_for_subject(self.store, self.config.scope, 'ou_alice')
        self.assertEqual(actor['id'], self.user['id'])
        self.assertFalse(channel_bindings.list_bindings(self.store, self.session)[0]['notifications_enabled'])
        result = self.client.post('/api/channels/feishu/status', json={'ticket': ticket})
        self.assertEqual(result.json()['state'], 'completed')

    def oauth_begin(self):
        account = self.client.get('/api/channels/feishu/oauth/account')
        self.assertEqual(account.status_code, 200, account.text)
        result = self.client.post('/api/channels/feishu/oauth/begin', json={'account_proof': account.json()['account_proof']})
        self.assertEqual(result.status_code, 200, result.text)
        return parse_qs(urlsplit(result.json()['url']).query)

    def oauth_complete(self, state, **values):
        return self.client.post('/api/channels/feishu/oauth/complete', json={'state': state, 'code': 'provider-code', **values})

    def test_oauth_binds_current_web_account_and_bot_recognizes_it(self):
        with patch.dict(os.environ, {'TAXPEARLS_FEISHU_APP_SECRET': 'server-only-secret'}):
            entry = self.client.get('/api/channels/feishu/entry').json()
            self.assertTrue(entry['oauth_ready'])
            self.assertNotIn('server-only-secret', json.dumps(entry))
            params = self.oauth_begin()
            state = params['state'][0]
            self.assertEqual(params['redirect_uri'], ['https://taxpearls.wxtech.site/connect/feishu/callback'])
            self.assertEqual(params['code_challenge_method'], ['S256'])
            self.assertEqual(params['client_id'], ['cli_test'])
            self.assertEqual(self.bridge('lookup', subject='ou_authorized').json()['state'], 'unbound')
            with patch.object(linking, '_oauth_json', side_effect=[{'access_token': 'provider-token'},
                    {'code': 0, 'data': {'open_id': 'ou_authorized', 'tenant_key': 'external_tenant', 'email': 'bob@other.test'}}]) as provider:
                result = self.oauth_complete(state)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['state'], 'completed')
            request = provider.call_args_list[0].kwargs['body']
            self.assertEqual(provider.call_args_list[0].args[0], 'https://open.feishu.cn/open-apis/authen/v2/oauth/token')
            self.assertEqual(request['client_secret'], 'server-only-secret')
            challenge = base64.urlsafe_b64encode(hashlib.sha256(request['code_verifier'].encode()).digest()).decode().rstrip('=')
            self.assertEqual(challenge, params['code_challenge'][0])
            self.assertEqual(provider.call_args_list[1].kwargs['token'], 'provider-token')
            self.assertNotIn('provider-token', result.text)
            self.assertEqual(channel_bindings.actor_for_subject(self.store, self.config.scope, 'ou_authorized')['id'], self.user['id'])
            self.assertEqual(self.bridge('lookup', subject='ou_authorized').json()['state'], 'bound')
            self.assertFalse(channel_bindings.list_bindings(self.store, self.session)[0]['notifications_enabled'])
            with patch.object(linking, '_oauth_identity') as exchange:
                self.assertEqual(self.oauth_complete(state).status_code, 409)
                exchange.assert_not_called()
            with closing(sqlite3.connect(self.store.path)) as db:
                dump = '\n'.join(db.iterdump())
            for secret in (state, 'provider-code', 'provider-token', 'server-only-secret', request['code_verifier'], self.session):
                self.assertNotIn(secret, dump)

    def test_oauth_account_switch_state_tampering_and_csrf_do_not_exchange_code(self):
        with patch.dict(os.environ, {'TAXPEARLS_FEISHU_APP_SECRET': 'secret'}), patch.object(linking, '_oauth_identity') as exchange:
            account = self.client.get('/api/channels/feishu/oauth/account').json()
            self.client.cookies.set(module.COOKIE_NAME, self.other_session)
            self.assertEqual(self.client.post('/api/channels/feishu/oauth/begin', json={'account_proof':account['account_proof']}).status_code, 409)
            self.client.cookies.set(module.COOKIE_NAME, self.session)
            state = self.oauth_begin()['state'][0]
            tampered = state[:-1] + ('0' if state[-1] != '0' else '1')
            self.assertEqual(self.oauth_complete(tampered).status_code, 409)
            response = self.client.post('/api/channels/feishu/oauth/complete', json={'state':state, 'code':'code'}, headers={'Origin':'https://evil.test'})
            self.assertEqual(response.status_code, 403)
            self.client.cookies.set(module.COOKIE_NAME, self.other_session)
            self.assertEqual(self.oauth_complete(state).status_code, 409)
            self.client.cookies.clear()
            self.assertEqual(self.oauth_complete(state).status_code, 401)
            exchange.assert_not_called()

    def test_oauth_denial_is_terminal_and_new_authorization_is_possible(self):
        with patch.dict(os.environ, {'TAXPEARLS_FEISHU_APP_SECRET': 'secret'}), patch.object(linking, '_oauth_identity') as exchange:
            state = self.oauth_begin()['state'][0]
            self.assertEqual(self.oauth_complete(state, code='', error='access_denied').json()['state'], 'cancelled')
            self.assertEqual(self.oauth_complete(state).status_code, 409)
            self.assertEqual(channel_bindings.list_bindings(self.store, self.session), [])
            exchange.assert_not_called()

    def test_oauth_expiry_permission_and_config_changes_are_rechecked(self):
        with patch.dict(os.environ, {'TAXPEARLS_FEISHU_APP_SECRET': 'secret'}), patch.object(linking, '_oauth_identity') as exchange:
            state = self.oauth_begin()['state'][0]
            with patch.dict(os.environ, {'TAXPEARLS_FEISHU_APP_TENANT_KEY':'different-owner'}):
                self.assertEqual(self.oauth_complete(state).status_code, 409)
            with self.store.connect() as db:
                db.execute('UPDATE users SET active=0 WHERE id=?', (self.user['id'],))
            self.assertEqual(self.oauth_complete(state).status_code, 401)
            with self.store.connect() as db:
                db.execute('UPDATE users SET active=1 WHERE id=?', (self.user['id'],))
                db.execute('UPDATE channel_binding_flows SET expires_at=0 WHERE id=?', (state[:32],))
            self.assertEqual(self.oauth_complete(state).status_code, 409)
            exchange.assert_not_called()

    def test_oauth_failure_does_not_bind_or_replay(self):
        from fastapi import HTTPException
        with patch.dict(os.environ, {'TAXPEARLS_FEISHU_APP_SECRET': 'secret'}):
            state = self.oauth_begin()['state'][0]
            with patch.object(linking, '_oauth_identity', side_effect=HTTPException(502, '授权校验失败')) as exchange:
                self.assertEqual(self.oauth_complete(state).status_code, 502)
                self.assertEqual(self.oauth_complete(state).status_code, 409)
                self.assertEqual(exchange.call_count, 1)
            self.assertEqual(channel_bindings.list_bindings(self.store, self.session), [])

    def test_oauth_rechecks_session_after_provider_exchange(self):
        with patch.dict(os.environ, {'TAXPEARLS_FEISHU_APP_SECRET': 'secret'}):
            state = self.oauth_begin()['state'][0]
            def revoke(*args):
                with self.store.connect() as db:
                    db.execute('DELETE FROM sessions WHERE token_hash=?', (channel_bindings._digest(self.session),))
                return 'ou_authorized'
            with patch.object(linking, '_oauth_identity', side_effect=revoke):
                self.assertEqual(self.oauth_complete(state).status_code, 401)
            self.assertEqual(self.bridge('lookup', subject='ou_authorized').json()['state'], 'unbound')

    def test_oauth_parallel_callback_and_unlinked_callback_cannot_restore_binding(self):
        with patch.dict(os.environ, {'TAXPEARLS_FEISHU_APP_SECRET': 'secret'}):
            state = self.oauth_begin()['state'][0]
            def exchange(*args):
                self.assertEqual(self.oauth_complete(state).status_code, 409)
                return 'ou_authorized'
            with patch.object(linking, '_oauth_identity', side_effect=exchange) as provider:
                result = self.oauth_complete(state)
                self.assertEqual(result.status_code, 200, result.text)
                provider.assert_called_once()
            binding_id = result.json()['binding']['id']
            self.assertEqual(self.client.delete('/api/channels/feishu/bindings/'+binding_id, headers={'Content-Type':'application/json'}).status_code, 200)
            with patch.object(linking, '_oauth_identity') as provider:
                self.assertEqual(self.oauth_complete(state).status_code, 409)
                provider.assert_not_called()
            self.assertEqual(self.bridge('lookup', subject='ou_authorized').json()['state'], 'unbound')

    def test_oauth_member_revocation_during_exchange_cancels_authorization(self):
        with patch.dict(os.environ, {'TAXPEARLS_FEISHU_APP_SECRET': 'secret'}):
            state = self.oauth_begin()['state'][0]
            def revoke(*args):
                with self.store.connect() as db:
                    channel_bindings.revoke_pending(db, self.user['id'])
                return 'ou_authorized'
            with patch.object(linking, '_oauth_identity', side_effect=revoke):
                self.assertEqual(self.oauth_complete(state).status_code, 409)
            self.assertEqual(self.bridge('lookup', subject='ou_authorized').json()['state'], 'unbound')

    def test_oauth_provider_transport_is_bounded_and_errors_do_not_expose_secrets(self):
        from fastapi import HTTPException
        from urllib.error import URLError
        for value in (b'not-json', b'[]', b'{"error":"bad-secret"}', b'{"code":123,"msg":"bad-secret"}', b'x'*65537):
            with self.subTest(value=value[:40]), patch.object(linking, 'build_opener') as opener:
                opener.return_value.open.return_value.__enter__.return_value.status = 200
                opener.return_value.open.return_value.__enter__.return_value.read.return_value = value
                with self.assertRaises(HTTPException) as error:
                    linking._oauth_json('https://open.feishu.cn/open-apis/authen/v2/oauth/token', body={'client_secret':'bad-secret'})
                self.assertNotIn('bad-secret', error.exception.detail)
                self.assertEqual(error.exception.status_code, 502)
        with patch.object(linking, 'build_opener') as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b'{"access_token":"token"}'
            self.assertEqual(linking._oauth_json('https://open.feishu.cn/open-apis/authen/v2/oauth/token', body={'client_secret':'secret'}), {'access_token':'token'})
            self.assertIsInstance(opener.call_args.args[0], linking._NoOAuthRedirect)
            self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], 300)
            response.read.assert_called_once_with(65537)
        with patch.object(linking, 'build_opener') as opener:
            opener.return_value.open.side_effect = URLError('contains-sensitive-secret')
            with self.assertRaises(HTTPException) as error:
                linking._oauth_json('https://open.feishu.cn/open-apis/authen/v2/oauth/token')
            self.assertNotIn('contains-sensitive-secret', error.exception.detail)

    def test_oauth_http_error_retains_safe_code_stage_and_category_without_secrets(self):
        from fastapi import HTTPException
        from urllib.error import HTTPError
        secret = 'do-not-log-client-secret-or-token'
        payload = {'code':20130, 'error':'invalid_client', 'error_description':secret, 'access_token':secret}
        failure = HTTPError('https://open.feishu.cn/open-apis/authen/v2/oauth/token?secret='+secret, 400, secret, {}, io.BytesIO(json.dumps(payload).encode()))
        with patch.object(linking, 'build_opener') as opener, self.assertLogs(linking.logger, level='WARNING') as logs:
            opener.return_value.open.side_effect = failure
            with self.assertRaises(HTTPException) as result:
                linking._oauth_json('https://open.feishu.cn/open-apis/authen/v2/oauth/token', body={'client_secret':secret})
        self.assertEqual(result.exception.status_code, 502)
        self.assertIn('换取授权凭据', result.exception.detail)
        self.assertIn('HTTP 400', result.exception.detail)
        self.assertIn('20130', result.exception.detail)
        self.assertIn('invalid_client', result.exception.detail)
        self.assertIn('App Secret', result.exception.detail)
        self.assertIn('stage=token', logs.output[0])
        self.assertNotIn(secret, result.exception.detail + ''.join(logs.output))
        self.assertTrue(failure.closed)

    def test_oauth_user_info_and_network_failures_have_distinct_safe_diagnostics(self):
        from fastapi import HTTPException
        from urllib.error import URLError
        import ssl
        for source, expected in ((TimeoutError('private-value'), '超时'),
                                 (URLError(ssl.SSLError('private-value')), '证书'),
                                 (URLError('private-value'), '出站网络')):
            with self.subTest(expected=expected), patch.object(linking, 'build_opener') as opener, self.assertLogs(linking.logger, level='WARNING') as logs:
                opener.return_value.open.side_effect = source
                with self.assertRaises(HTTPException) as result:
                    linking._oauth_json('https://open.feishu.cn/open-apis/authen/v1/user_info', token='private-value')
                self.assertIn('读取用户身份', result.exception.detail)
                self.assertIn(expected, result.exception.detail)
                self.assertNotIn('private-value', result.exception.detail + ''.join(logs.output))

    def test_oauth_grant_errors_and_untrusted_provider_text_are_safely_reported(self):
        from fastapi import HTTPException
        for value in ({'code':20003, 'error':'invalid_grant', 'msg':'private-value'},
                      {'code':'private-value','error':['private-value']},
                      {'error':'private-value'}):
            with self.subTest(value=value), patch.object(linking, 'build_opener') as opener, self.assertLogs(linking.logger, level='WARNING') as logs:
                response = opener.return_value.open.return_value.__enter__.return_value
                response.status = 400
                response.read.return_value = json.dumps(value).encode()
                with self.assertRaises(HTTPException) as result:
                    linking._oauth_json('https://open.feishu.cn/open-apis/authen/v2/oauth/token', body={'code':'private-value'})
                self.assertNotIn('private-value', result.exception.detail + ''.join(logs.output))
                if value.get('code') == 20003:
                    self.assertIn('20003', result.exception.detail)
                    self.assertIn('PKCE', result.exception.detail)

    def test_oauth_missing_user_identity_and_nonenterprise_roles_cannot_bind(self):
        with patch.dict(os.environ, {'TAXPEARLS_FEISHU_APP_SECRET': 'secret'}):
            state = self.oauth_begin()['state'][0]
            with patch.object(linking, '_oauth_json', side_effect=[{'access_token':'token'}, {'data':{'email':'alice@example.test'}}]):
                self.assertEqual(self.oauth_complete(state).status_code, 502)
            with self.store.connect() as db:
                db.execute("UPDATE users SET role='teacher' WHERE id=?", (self.user['id'],))
            proof = self.client.get('/api/channels/feishu/oauth/account').json()['account_proof']
            result = self.client.post('/api/channels/feishu/oauth/begin', json={'account_proof':proof})
            self.assertEqual(result.status_code, 403)

    def test_oauth_duplicate_subject_never_moves_existing_binding(self):
        self.complete()
        self.client.cookies.set(module.COOKIE_NAME, self.other_session)
        with patch.dict(os.environ, {'TAXPEARLS_FEISHU_APP_SECRET': 'secret'}):
            state = self.oauth_begin()['state'][0]
            with patch.object(linking, '_oauth_identity', return_value='ou_alice'):
                self.assertEqual(self.oauth_complete(state).status_code, 409)
        self.assertEqual(channel_bindings.actor_for_subject(self.store, self.config.scope, 'ou_alice')['id'], self.user['id'])

    def test_oauth_requires_server_credentials_and_callback_has_no_get_side_effect(self):
        with patch.dict(os.environ, {'TAXPEARLS_FEISHU_APP_SECRET': ''}):
            self.assertFalse(self.client.get('/api/channels/feishu/entry').json()['oauth_ready'])
            self.assertEqual(self.client.get('/api/channels/feishu/oauth/account').status_code, 503)
            response = self.client.post('/api/channels/feishu/oauth/begin', json={'account_proof':'a'*64})
            self.assertEqual(response.status_code, 503)
        self.client.cookies.clear()
        page = self.client.get('/connect/feishu/callback?code=synthetic&state=invalid')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers['referrer-policy'], 'no-referrer')
        self.assertIn("frame-ancestors 'none'", page.headers['content-security-policy'])
        self.assertEqual(self.bridge('lookup', subject='ou_alice').json()['state'], 'unbound')

    def test_shortcut_lookup_is_scoped_and_does_not_create_binding_requests(self):
        self.assertEqual(self.bridge('lookup', subject='ou_alice').json(), {'state': 'unbound'})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM feishu_binding_requests').fetchone()[0], 0)
        self.complete()
        self.assertEqual(self.bridge('lookup', subject='ou_alice').json(), {'state': 'bound'})
        self.assertEqual(self.bridge('lookup', subject='ou_other').json(), {'state': 'unbound'})
        self.assertEqual(self.bridge('lookup', subject='bad').status_code, 422)
        with patch.dict(os.environ, {'TAXPEARLS_FEISHU_APP_TENANT_KEY': 'other-tenant'}):
            self.assertEqual(self.bridge('lookup', subject='ou_alice').json(), {'state': 'unbound'})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM feishu_binding_requests').fetchone()[0], 1)

    def test_bot_unlink_preserves_tasks_and_old_card_cannot_unlink_a_new_binding(self):
        with patch.dict(os.environ, {'TAXPEARLS_MATERIAL_KEY': base64.b64encode(b'c' * 32).decode()}):
            self.complete()
            tag = self.bridge('task_menu', subject='ou_alice').json()['binding_tag']
            task = self.bridge('task_create', subject='ou_alice', binding_tag=tag, operation_id='9' * 32)
            self.assertEqual(task.status_code, 200, task.text)
            task_id = task.json()['id']
            self.assertEqual(self.bridge('binding_unlink', subject='ou_alice').status_code, 422)
            self.assertEqual(self.bridge('binding_unlink', subject='ou_alice', binding_tag='0' * 64).status_code, 403)
            self.assertEqual(self.bridge('lookup', subject='ou_alice').json()['state'], 'bound')
            result = self.bridge('binding_unlink', subject='ou_alice', binding_tag=tag)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json(), {'state': 'unbound'})
            self.assertEqual(self.client.get('/api/channels/feishu/bindings').json(), [])
            self.assertEqual(self.client.get('/api/enterprise/input-tasks/' + task_id).status_code, 200)
            self.assertEqual(self.bridge('task_list', subject='ou_alice', binding_tag=tag, view='ongoing').status_code, 403)
            self.assertEqual(self.bridge('binding_unlink', subject='ou_alice', binding_tag=tag).json(), {'state': 'unbound'})
            future = time.time() + 20
            with patch('webapp.feishu_binding.time.time', return_value=future):
                ticket = self.issue(event_id='evt_rebind')
                self.assertEqual(self.claim(ticket).status_code, 200)
                self.assertEqual(self.finish(self.prompt()).status_code, 200)
                self.assertEqual(self.bridge('binding_unlink', subject='ou_alice', binding_tag=tag).status_code, 403)
                self.assertEqual(self.bridge('lookup', subject='ou_alice').json()['state'], 'bound')

    def test_real_task_creation_query_progress_and_account_scope(self):
        from webapp import material_batches as batches, members
        with patch.dict(os.environ, {'TAXPEARLS_MATERIAL_KEY': base64.b64encode(b'c' * 32).decode()}):
            self.complete()
            tag = self.bridge('task_menu', subject='ou_alice').json()['binding_tag']
            values = {'subject': 'ou_alice', 'binding_tag': tag, 'operation_id': '1' * 32}
            created = self.bridge('task_create', **values)
            self.assertEqual(created.status_code, 200, created.text)
            task_id = created.json()['id']
            self.assertRegex(created.json()['display_name'], r'^新建任务 · \d{4}-\d{2}-\d{2} \d{2}:\d{2}$')
            self.assertNotIn(task_id[:8], created.json()['display_name'])
            self.assertEqual(self.bridge('task_create', **values).json()['id'], task_id)
            self.assertIn('input_task=' + task_id, created.json()['url'])
            self.assertEqual(self.client.get('/api/enterprise/input-tasks/' + task_id).status_code, 200)
            self.client.cookies.set(module.COOKIE_NAME, self.other_session)
            other = self.client.post('/api/enterprise/input-tasks', json={})
            self.assertEqual(other.status_code, 200, other.text)
            self.client.cookies.set(module.COOKIE_NAME, self.session)
            with self.store.connect() as db:
                db.execute('''INSERT INTO material_jobs(id,org_id,batch_id,input_revision,actor_id,actor_role,state,available_at,created_at,input_cipher)
                    VALUES ('task_job','enterprise-a',?,1,?,'accountant','running',0,?,?)''', (task_id,self.user['id'],members.now(),b'synthetic'))
                for index, state in enumerate(('done', 'failed', 'queued')):
                    db.execute('INSERT INTO material_job_files(job_id,file_id,fingerprint,state) VALUES (?,?,?,?)',
                               ('task_job', str(index), 'synthetic', state))
            query = {'subject': 'ou_alice', 'binding_tag': tag, 'view': 'ongoing'}
            response = self.bridge('task_list', **query)
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual([item['id'] for item in result['items']], [task_id])
            self.assertEqual(result['items'][0]['progress'], {'state':'running','done':1,'failed':1,'total':3})
            self.assertNotIn('company_name', response.text)
            self.assertNotIn('tax_no', response.text)
            payload = batches.listing(self.store,self.user,workflow='core',include_latest=True)[0]['payload']
            payload['task'].update(company_name='示例企业', start_date='2026-01-01', end_date='2026-01-31', tax_no='PRIVATE-TAX-NUMBER')
            payload['completion'] = {'report_version':1,'completed_at':members.now()}
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                batch = batches._authorize(db,self.user,task_id)
                batches._append(db,batch,self.user,'edit',payload)
            self.assertEqual(self.bridge('task_list', **query).json()['items'], [])
            query['view'] = 'completed'
            archived = self.bridge('task_list', **query)
            self.assertEqual(archived.json()['items'][0]['status'], 'archived')
            self.assertEqual(archived.json()['items'][0]['display_name'], '示例企业 · 2026-01-01 至 2026-01-31')
            self.assertNotIn('PRIVATE-TAX-NUMBER', archived.text)
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM core_input_tasks').fetchone()[0], 2)

    def test_task_cards_reject_revoked_bindings_and_deleted_task_replays(self):
        from webapp import core_inputs
        with patch.dict(os.environ, {'TAXPEARLS_MATERIAL_KEY': base64.b64encode(b'c' * 32).decode()}):
            with self.store.connect() as db:
                db.execute("UPDATE users SET role='org_admin' WHERE id=?", (self.user['id'],))
            self.user['role'] = 'org_admin'
            self.complete()
            tag = self.bridge('task_menu',subject='ou_alice').json()['binding_tag']
            values = {'subject':'ou_alice','binding_tag':tag,'operation_id':'2'*32}
            created = self.bridge('task_create',**values)
            self.assertEqual(created.status_code,200,created.text)
            task_id = created.json()['id']
            core_inputs.delete_task(self.store,self.user,task_id,1)
            self.assertEqual(self.bridge('task_create',**values).status_code,410)
            with self.store.connect() as db:
                db.execute("UPDATE channel_bindings SET revision=? WHERE user_id=?", ('new-generation',self.user['id']))
            self.assertEqual(self.bridge('task_create',**values).status_code,403)
            self.assertEqual(self.bridge('task_list',subject='ou_alice',binding_tag=tag,view='ongoing').status_code,403)
            self.assertEqual(self.bridge('task_create',subject='ou_alice',operation_id='3'*32).status_code,422)
            self.assertEqual(self.bridge('task_menu',subject='ou_other').status_code,403)
            with self.store.connect() as db:
                db.execute('UPDATE users SET active=0 WHERE id=?', (self.user['id'],))
            self.assertEqual(self.bridge('task_menu',subject='ou_alice').status_code,403)

    def test_entry_qr_uses_only_fixed_app_url_and_no_secret_or_identity(self):
        response = self.client.get('/api/channels/feishu/entry')
        self.assertEqual(response.json()['url'], 'https://applink.feishu.cn/client/bot/open?appId=cli_test')
        self.assertTrue(response.json()['binding_ready'])
        qr = self.client.get('/api/channels/feishu/qr.svg')
        self.assertEqual(qr.status_code, 200)
        self.assertIn('<svg', qr.text)
        self.assertNotIn('ou_alice', qr.text)
        self.assertNotIn(self.session, response.text)

    def test_no_public_route_accepts_provider_identity_or_grants_login(self):
        ticket = self.issue()
        self.client.cookies.clear()
        self.assertEqual(self.claim(ticket).status_code, 401)
        self.assertEqual(self.client.post('/api/channels/feishu/claim', json={'ticket': ticket, 'open_id': 'ou_attacker'}).status_code, 422)
        self.assertEqual(self.client.post(linking.INTERNAL_PATH, json={'operation': 'issue'}).status_code, 401)

    def test_bridge_rejects_wrong_key_timestamp_replay_and_cross_app(self):
        self.assertEqual(self.bridge('pending', key=b'x' * 32).status_code, 401)
        self.assertEqual(self.bridge('pending', stamp=1).status_code, 401)
        nonce = secrets.token_hex(16)
        self.assertEqual(self.bridge('pending', nonce=nonce).status_code, 200)
        self.assertEqual(self.bridge('pending', nonce=nonce).status_code, 409)
        self.assertEqual(self.bridge('pending', app_id='cli_other').status_code, 403)

    def test_claim_cannot_be_replaced_by_a_second_account_or_session(self):
        ticket = self.issue()
        self.claim(ticket)
        self.client.cookies.set(module.COOKIE_NAME, self.other_session)
        self.assertEqual(self.claim(ticket).status_code, 409)
        self.assertEqual(self.client.post('/api/channels/feishu/status', json={'ticket': ticket}).status_code, 404)
        self.assertEqual(self.prompt()['account_hint'], 'al***e')

    def test_account_switched_in_another_tab_requires_a_fresh_display_confirmation(self):
        ticket = self.issue()
        old = self.client.post('/api/channels/feishu/status', json={'ticket': ticket}).json()['web_proof']
        self.client.cookies.set(module.COOKIE_NAME, self.other_session)
        result = self.client.post('/api/channels/feishu/claim', json={'ticket': ticket, 'web_proof': old})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.bridge('pending').json()['items'], [])
        new = self.client.post('/api/channels/feishu/status', json={'ticket': ticket}).json()['web_proof']
        self.assertEqual(self.client.post('/api/channels/feishu/claim', json={'ticket': ticket, 'web_proof': new}).status_code, 200)
        self.assertEqual(self.prompt()['account_hint'], 'bo***b')

    def test_forwarded_confirm_card_wrong_chat_and_wrong_proof_fail(self):
        ticket = self.issue(); self.claim(ticket); item = self.prompt()
        for changes in ({'subject': 'ou_other'}, {'chat': 'oc_other'}, {'confirmation': 'ff' * 32}):
            self.assertEqual(self.finish(item, **changes).status_code, 404)
        self.assertEqual(channel_bindings.list_bindings(self.store, self.session), [])

    def test_expiry_and_tampered_ticket(self):
        ticket = self.issue()
        self.assertEqual(self.claim(ticket[:-1] + ('0' if ticket[-1] != '0' else '1')).status_code, 404)
        with patch.object(linking.time, 'time', return_value=time.time() + 601):
            self.assertEqual(self.claim(ticket).status_code, 404)

    def test_logout_and_role_change_invalidate_final_confirmation(self):
        ticket = self.issue(); self.claim(ticket); item = self.prompt()
        self.store.logout(self.session)
        self.assertEqual(self.finish(item).status_code, 409)
        self.assertIsNone(channel_bindings.actor_for_subject(self.store, self.config.scope, 'ou_alice'))

    def test_permission_change_between_confirmations_is_rejected(self):
        ticket = self.issue(); self.claim(ticket); item = self.prompt()
        with self.store.connect() as db:
            db.execute("UPDATE users SET org_id='changed' WHERE id=?", (self.user['id'],))
        self.assertEqual(self.finish(item).status_code, 409)

    def test_retries_are_idempotent_and_concurrent_confirmations_make_one_binding(self):
        ticket = self.issue()
        self.assertEqual(self.issue(), ticket)
        self.assertEqual(self.claim(ticket).status_code, 200)
        self.assertEqual(self.claim(ticket).status_code, 200)
        item = self.prompt()
        args = (self.store, self.config, item['id'], item['subject'], item['chat'], item['confirmation'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: linking.finish(*args), range(2)))
        self.assertEqual(results, [{'state': 'completed'}] * 2)
        self.assertEqual(len(channel_bindings.list_bindings(self.store, self.session)), 1)

    def test_cancellation_from_either_side_prevents_binding(self):
        ticket = self.issue(); self.claim(ticket); item = self.prompt()
        self.assertEqual(self.client.post('/api/channels/feishu/cancel', json={'ticket': ticket}).status_code, 200)
        self.assertEqual(self.finish(item).status_code, 409)
        second = self.issue('ou_second', 'oc_second', 'evt_2'); self.claim(second); item = self.prompt()
        self.assertEqual(self.finish(item, 'cancel').json()['state'], 'cancelled')
        self.assertEqual(self.finish(item).status_code, 409)

    def test_one_identity_and_one_account_cannot_be_silently_rebound(self):
        self.complete()
        self.assertEqual(self.bridge('issue', subject='ou_alice', chat='oc_alice', event_id='evt_2').json()['state'], 'bound')
        ticket = self.issue('ou_second', 'oc_second', 'evt_3')
        self.assertEqual(self.claim(ticket).status_code, 409)

    def test_unlink_revokes_old_confirmation_without_resurrection(self):
        ticket, item = self.complete()
        binding = self.client.get('/api/channels/feishu/bindings').json()[0]
        response = self.client.delete('/api/channels/feishu/bindings/' + binding['id'], headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.finish(item).status_code, 409)
        self.assertIsNone(channel_bindings.actor_for_subject(self.store, self.config.scope, 'ou_alice'))
        self.assertEqual(self.client.post('/api/channels/feishu/status', json={'ticket': ticket}).json()['state'], 'cancelled')

    def test_replacement_request_invalidates_previous_accounts_confirm_card(self):
        first = self.issue(); self.claim(first); old = self.prompt()
        second = self.issue('ou_second', 'oc_second', 'evt_second'); self.claim(second)
        self.assertEqual(self.finish(old).status_code, 409)
        current = self.prompt()
        self.assertEqual(current['subject'], 'ou_second')

    def test_encrypted_identity_and_no_tickets_sessions_or_raw_identity_in_audit_log(self):
        ticket = self.issue(); self.claim(ticket)
        with closing(sqlite3.connect(self.store.path)) as db:
            identity = db.execute('SELECT identity_json FROM feishu_binding_requests').fetchone()[0]
            dump = '\n'.join(db.iterdump())
        self.assertTrue(identity.startswith('tp-field-v1:'))
        for secret in (ticket, self.session, 'ou_alice', 'oc_alice'):
            self.assertNotIn(secret, dump)

    def test_csrf_and_invalid_public_origin(self):
        ticket = self.issue()
        self.assertEqual(self.client.post('/api/channels/feishu/claim', json={'ticket': ticket, 'web_proof': 'a' * 64}, headers={'Origin': 'https://evil.test'}).status_code, 403)
        with patch.dict(os.environ, {'TAXPEARLS_PUBLIC_BASE_URL': 'https://evil.test/path'}):
            self.assertFalse(self.client.get('/api/channels/feishu/entry').json()['binding_ready'])
            self.assertEqual(self.client.get('/api/channels/feishu/qr.svg').status_code, 200)
            self.assertEqual(self.client.post('/api/channels/feishu/status', json={'ticket': ticket}).status_code, 503)

    def test_feature_disabled_and_static_page_security(self):
        with patch.dict(os.environ, {'TAXPEARLS_FEISHU_BINDING_ENABLED': '0'}):
            response = self.client.get('/api/channels/feishu/entry')
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()['binding_ready'])
            self.assertEqual(self.client.get('/api/channels/feishu/qr.svg').status_code, 200)
            self.assertEqual(self.client.get('/api/channels/feishu/bindings').json(), [])
        response = self.client.get('/connect/feishu')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['referrer-policy'], 'no-referrer')
        self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])
        self.assertEqual(self.client.get('/feishu-connect.js').status_code, 200)

    def test_missing_or_invalid_bot_id_cannot_produce_an_entry_or_qr(self):
        for app_id in ('', 'not-an-app', 'cli_x&appId=another'):
            with self.subTest(app_id=app_id), patch.dict(os.environ, {'TAXPEARLS_FEISHU_APP_ID': app_id}):
                for path in ('/api/channels/feishu/entry', '/api/channels/feishu/qr.svg'):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 503)
                    self.assertIn('尚未配置飞书助手入口', response.json()['detail'])

    def test_bot_entry_needs_no_binding_credentials_or_login(self):
        self.client.cookies.clear()
        with patch.dict(os.environ, {'TAXPEARLS_FEISHU_APP_TENANT_KEY': '', 'TAXPEARLS_FEISHU_BRIDGE_KEY': ''}):
            response = self.client.get('/api/channels/feishu/entry')
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()['binding_ready'])
            self.assertEqual(self.client.get('/api/channels/feishu/qr.svg').status_code, 200)
            self.assertEqual(self.client.get('/api/channels/feishu/bindings').status_code, 401)

    def test_existing_binding_status_is_self_only_even_when_new_binding_is_disabled(self):
        self.complete()
        with patch.dict(os.environ, {'TAXPEARLS_FEISHU_BINDING_ENABLED': '0',
                                     'TAXPEARLS_FEISHU_BRIDGE_KEY': '', 'TAXPEARLS_FEISHU_APP_TENANT_KEY': ''}):
            response = self.client.get('/api/channels/feishu/bindings')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()), 1)
            self.assertEqual(response.json()[0]['channel'], 'feishu')
            self.assertNotIn('ou_alice', response.text)
            self.client.cookies.set(module.COOKIE_NAME, self.other_session)
            self.assertEqual(self.client.get('/api/channels/feishu/bindings').json(), [])
            self.client.cookies.clear()
            self.assertEqual(self.client.get('/api/channels/feishu/bindings').status_code, 401)


if __name__ == '__main__':
    unittest.main()
