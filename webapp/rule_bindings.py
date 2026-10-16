"""Authorized, CAS-protected bindings from retained evidence to converted rules."""
from copy import deepcopy

from fastapi import Cookie, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src import rule_bindings
from webapp import core_inputs, material_batches as batches


class BindingBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision:int=Field(ge=1)
    name:str=Field(min_length=1,max_length=200)
    record_ids:list[str]=Field(min_length=1,max_length=5000)
    field:str=Field(min_length=1,max_length=80)
    aggregation:str=Field(default='sum')
    basis:str=Field(min_length=3,max_length=1500)


class AuditBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision:int=Field(ge=1)
    audit_id:str=Field(min_length=1,max_length=100)


class CorpusBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    manifest_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')


def register(app,get_store,get_user,allow,get_rules):
    def actor(session):
        user=get_user(session)
        allow(user,'org_admin','accountant')
        return user

    @app.get('/rule-bindings.js')
    def script():
        from pathlib import Path
        from fastapi.responses import FileResponse
        return FileResponse(Path(__file__).parent/'static/rule-bindings.js',media_type='application/javascript')

    @app.get('/api/knowledge/corpus/status')
    def corpus_status(session:str|None=Cookie(default=None,alias='taxpearls_session')):
        get_user(session)
        from pathlib import Path
        import hashlib
        path=Path(__file__).resolve().parents[1]/'docs/法规原文/来源清单.json'
        with get_store().connect() as db:
            counts={table:db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in ['Policy','Clause']}
        return {'manifest_available':path.exists(),'manifest_sha256':hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None,
                'counts':counts,'approval':'原文整理不等于政策审核；候选只能作为待核实线索。'}

    @app.post('/api/knowledge/corpus/import')
    def import_corpus(body:CorpusBody,session:str|None=Cookie(default=None,alias='taxpearls_session')):
        user=get_user(session)
        allow(user,'platform_admin')
        from pathlib import Path
        import hashlib
        from src import policy_corpus
        from webapp import policy_graph
        root=Path(__file__).resolve().parents[1]
        path=root/'docs/法规原文/来源清单.json'
        if not path.exists():
            raise HTTPException(409,'没有随代码提供的政策来源清单。')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=body.manifest_sha256:
            raise HTTPException(409,'政策来源清单已变化，请重新查看后导入。')
        try:
            plan=policy_corpus.prepare(path)
            store=get_store()
            from webapp.access import current_actor
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current_actor(db,user,{'platform_admin'})
                from src.rule_conversion import compile_rule
                rules=get_rules()
                policy_graph.sync_rules(db,rules)
                ids={rule.id:compile_rule(rule)['rule_id'] for rule in rules}
                store._log(db,user,'import_candidate_corpus','policy_corpus',body.manifest_sha256)
                counts=policy_corpus.import_plan(db,plan,root/'instance/policy-originals',rule_ids=ids)
            return {'counts':counts,'formal_approvals':0,'external_ai_calls':0}
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None

    @app.get('/api/knowledge/business-metrics')
    def catalog(session:str|None=Cookie(default=None,alias='taxpearls_session')):
        actor(session)
        definitions={}
        for rule in get_rules():
            for name,requirement in rule.inputs.items():
                item=definitions.setdefault(name,{'name':name,'unit':rule_bindings.metric_unit(name),
                    'source_kind':rule_bindings.source_kind(name),'requirements':[],'rule_codes':[]})
                if requirement not in item['requirements']:
                    item['requirements'].append(requirement)
                item['rule_codes'].append(rule.id)
        return sorted(definitions.values(),key=lambda x:x['name'])

    @app.post('/api/enterprise/input-tasks/{task_id}/metric-bindings')
    def bind(task_id:str,body:BindingBody,session:str|None=Cookie(default=None,alias='taxpearls_session')):
        user,store=actor(session),get_store()
        view=batches.read(store,user,task_id)
        if view['revision']!=body.expected_revision:
            raise HTTPException(409,'资料版本已改变，请重新读取后核对。')
        payload=deepcopy(core_inputs.latest(view))
        allowed={name for rule in get_rules() for name in rule.inputs}
        if body.name not in allowed:
            raise HTTPException(422,'指标不在当前规则字典中。')
        try:
            value=rule_bindings.binding(body.name,body.record_ids,body.field,body.aggregation,body.basis,
                                       payload['processing'],payload['task'],user['id'])
            payload['task'].setdefault('metric_bindings',{})[body.name]=value
            batches.append_revision(store,user,task_id,body.expected_revision,'confirmation',payload)
            return core_inputs.project(batches.read(store,user,task_id))
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None

    @app.post('/api/enterprise/input-tasks/{task_id}/audit-metrics')
    def import_audit(task_id:str,body:AuditBody,session:str|None=Cookie(default=None,alias='taxpearls_session')):
        user,store=actor(session),get_store()
        view=batches.read(store,user,task_id)
        if view['revision']!=body.expected_revision:
            raise HTTPException(409,'资料版本已改变，请重新读取后关联。')
        payload=deepcopy(core_inputs.latest(view))
        entry=store.get_audit_for_user(body.audit_id,user)
        if entry is None:
            raise HTTPException(404,'材料不存在或无权访问。')
        try:
            rows=rule_bindings.audit_records(entry,payload['task'])
            payload['task']['audit_metric_records']=rows
            payload['task']['audit_metric_source_signature']=rule_bindings.material_signature(payload['processing'])
            # A new authorized source replaces the previous imported snapshot.
            payload['processing']['records']=[r for r in payload['processing']['records'] if r.get('material_type')!='business_metric']
            rule_bindings.restore(payload)
            batches.append_revision(store,user,task_id,body.expected_revision,'confirmation',payload)
            return core_inputs.project(batches.read(store,user,task_id))
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None
