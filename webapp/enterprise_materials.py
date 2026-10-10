"""Persistent enterprise materials: analyze first, explicitly confirm/exe once.

Teaching keeps its independent legacy path. These endpoints never accept a
client-provided Dataset, rules, parser output or confirmation snapshot.
"""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import secrets
from urllib.parse import quote
from threading import BoundedSemaphore

from fastapi import Cookie, HTTPException, Request
from fastapi.responses import Response, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException

from src import config, engine, materials, related_graph, material_review, material_security, material_formats, material_format_guard, financial_import, material_provenance, periods
from src.ai_extraction import AIExtractor, ExtractionError
from src.input_errors import InputError
from src.models import Rule
from src.settings import AISettings
from src.snapshots import deserialize_dataset
from webapp import enterprise_analysis, enterprise_scope, material_batches as batches, material_operations
from webapp.access import AccessDenied
from webapp import material_jobs
from webapp.uploads import bounded_stream, multipart


def _latest(view):
    records = [v for v in view['versions'] if v['kind'] in {'analysis', 'edit'}]
    if not records:
        raise AccessDenied('材料尚未完成分析。', 409)
    return records[-1]


def _analysis_payload(documents, company, selections, base_rules, rules_for_dataset, scope_context=None):
    selections = deepcopy(selections)
    for document in documents:
        selection = selections.get(document['id'], {})
        if not isinstance(selection, dict):
            raise InputError('材料选择格式无效。')
        if 'standard_edits' in selection:
            selection['standard_edits'] = material_review.normalize(document, selection['standard_edits'])
    analysis = enterprise_analysis.analyze(documents, company, selections, base_rules)
    company = analysis['company']
    selected = base_rules
    if analysis['dataset'] is not None:
        try:
            selected = rules_for_dataset(deserialize_dataset(analysis['dataset']))
        except HTTPException as exc:
            if exc.status_code != 422:
                raise
            analysis['feedback']['blocking'].append({'code': 'rule_period', 'message': str(exc.detail),
                'file_id': None, 'impact': '当前规则版本不能覆盖整个主期间。', 'required': '核对或拆分检测期间。'})
            analysis['can_confirm'] = False
        else:
            # Use all supported metrics while limiting readiness to selected
            # effective rule versions; disabled rules are not "passed".
            analysis['checks'] = [check for check in analysis['checks'] if check.get('engine') == 'graph']
            analysis['feedback']['limited'] = [n for n in analysis['feedback']['limited'] if n['code'] != 'check_unavailable']
            data = deserialize_dataset(analysis['dataset'])
            for rule in selected:
                problems = engine.readiness(rule, data)
                analysis['checks'].append({'rule_id': rule.id, 'name': rule.name, 'version': rule.version,
                                          'ready': not problems, 'reasons': problems})
                if problems:
                    analysis['feedback']['limited'].append({'code': 'check_unavailable', 'message': '；'.join(problems),
                        'file_id': None, 'impact': rule.id + ' ' + rule.name, 'required': '；'.join(rule.inputs.values())})
    return enterprise_scope.decorate({'documents': documents, 'company': company, 'selections': selections, 'analysis': analysis,
            'rules': [asdict(r) for r in selected], 'graph_rule': asdict(related_graph.definition()),
            'parser_version': 'enterprise-materials-v2',
            'fields': {key: detail for rule in base_rules for key, detail in rule.inputs.items()}}, scope_context)


def _field_changes(previous, current):
    """Per-operation deltas, not just cumulative differences from the original.

    Actor and timestamp are supplied by the immutable revision transaction.
    """
    output = []
    for key, value in current['company'].items():
        if value != previous['company'].get(key):
            output.append({'field': 'company.' + key, 'old': previous['company'].get(key),
                           'new': value, 'source': '本组企业与期间核对', 'origin': 'user'})
    for document in current['documents']:
        file_id = document['id']
        before = previous['selections'].get(file_id, {})
        after = current['selections'].get(file_id, {})
        old_document = next(d for d in previous['documents'] if d['id'] == file_id)
        for field, default in [('purpose', 'current'), ('rows', document['rows']), ('evidence_reviews', []),
                               ('import_options', {}), ('pdf_range', None)]:
            old = before.get(field, old_document['rows'] if field == 'rows' else default)
            new = after.get(field, default)
            if old != new:
                output.append({'file_id': file_id, 'field': field, 'old': old, 'new': new,
                               'source': document['name'], 'origin': 'user'})
        for field in material_review.fields(document):
            address = field['id']
            old = before.get('standard_edits', {}).get(address, field['value'])
            new = after.get('standard_edits', {}).get(address, field['value'])
            if old != new:
                output.append({'file_id': file_id, 'field': address, 'old': old, 'new': new,
                               'original': field['value'], 'source': document['name'] + ' / ' + address,
                               'origin': 'user'})
    return output


def register(app, get_store, get_user, allow, rules_dir, rules_for_dataset, save_audit, audit_result):
    material_operations.register(app, get_store, get_user, allow)
    slots = BoundedSemaphore(material_jobs.QueueSettings.from_env().workers)
    def actor(session):
        user = get_user(session)
        allow(user, 'org_admin', 'accountant')
        return user

    def project(view, user):
        settings = AISettings.from_env()
        ai = {**settings.public_status(), 'max_pages': settings.max_pages,
              'queue_enabled': material_jobs.QueueSettings.from_env().enabled}
        jobs = view.get('jobs', [])
        records = [v for v in view['versions'] if v['kind'] in {'analysis', 'edit'}]
        if not records:
            return {'id':view['id'], 'revision':view['revision'], 'analysis_revision':None,
                'files':view['files'], 'jobs':jobs, 'ai':ai, 'documents':[], 'company':{}, 'selections':{},
                'analysis':{'can_confirm':False,'feedback':{'blocking':[],'limited':[],'suggested':[]},'checks':[]},
                'metrics':[], 'fields':{}, 'executions':view['executions'], 'client_id':view['client_id']}
        latest = records[-1]
        # Keep the raw grids server-side; only authorized, addressable numeric
        # cells needed for review are projected. Originals remain immutable.
        documents = [{**{k: v for k, v in doc.items() if k not in {
                         'accounts', 'declarations', 'standard_tables', 'import_model', 'account_cell_sources', 'declaration_cell_sources'}},
                      'standard_fields': material_review.fields(doc) if 'standard_tables' in doc or 'import_model' in doc else None,
                      'financial_reanalysis_required': financial_import.reanalysis_required(doc),
                      'pdf_reanalysis_required': material_provenance.pdf_reanalysis_required(doc)}
                     for doc in latest['payload']['documents']]
        dataset = latest['payload']['analysis'].get('dataset') or {}
        for doc in documents:
            if 'security' in doc:
                doc['security'] = deepcopy(doc['security'])
            material_security.scan_pages(doc)
            doc['evidence_reviews'] = material_security.review_requirements(doc,
                latest['payload']['selections'].get(doc['id'], {}), latest['payload']['company'])
            if doc['kind'] == 'pdf':
                scope = latest['payload']['company']
                if latest['payload']['selections'].get(doc['id'], {}).get('purpose') == 'history':
                    try:
                        period = periods.parse_period(doc['company'].get('period'), '历史材料期间')
                        scope = {'period_start': period.start.isoformat(), 'period_end': period.end.isoformat()}
                    except InputError:
                        scope = {}
                doc['ai_period'] = {key: scope.get(key, '') for key in ('period_start', 'period_end')}
        analysis = deepcopy(latest['payload']['analysis'])
        matched = None
        if not view['client_id']:
            tax = enterprise_scope.identity(latest['payload']['company'].get('taxpayer_id'))
            matched = next(({'client_id': c['id'], 'name': c['name'], 'taxpayer_id': c['taxpayer_id']}
                            for c in get_store().list_clients(user) if tax and enterprise_scope.identity(c['taxpayer_id']) == tax), None)
        # Older frozen analyses may contain readiness computed before the gate.
        blockers = enterprise_analysis.extraction_blockers(latest['payload']['documents'], latest['payload']['selections'], latest['payload']['company'])
        if blockers:
            analysis['can_confirm'] = False
            known = {(n['code'], n.get('file_id')) for n in analysis['feedback']['blocking']}
            analysis['feedback']['blocking'].extend(n for n in blockers if (n['code'], n['file_id']) not in known)
        if any(job['state'] in {'queued', 'running'} or job['state'] == 'failed' and
               job['input_revision'] == view['revision'] for job in jobs):
            analysis['can_confirm'] = False
        return {'id': view['id'], 'revision': view['revision'], 'analysis_revision': latest['revision'],
                'files': view['files'], 'jobs': jobs, 'ai': ai, 'documents': documents,
                'company': latest['payload']['company'], 'selections': latest['payload']['selections'],
                'client_id': view['client_id'], 'scope_context': latest['payload'].get('scope_context'),
                'existing_match': matched,
                'analysis': {k: v for k, v in analysis.items() if k != 'dataset'},
                'metrics': list(dataset.get('metrics', {}).values()),
                'fields': latest['payload'].get('fields', {}), 'executions': view['executions']}

    def parse_uploads(uploads, mode, settings=None, base_rules=None, extractor=None):
        # Fail before parsing or model calls when protected retention is not
        # configured. No real key is generated or disclosed by the service.
        batches._key()
        batches.validate_uploads(uploads, wait=True)
        names = [name for name, _ in uploads]
        if len(set(names)) != len(names):
            raise InputError('同批文件名重复，请区分文件名后重新上传，避免来源混淆。')
        if any('/' in name or '\\' in name for name in names):
            raise InputError('上传文件名不能包含目录路径。')
        base_rules = base_rules or engine.load_rules(rules_dir)
        keys = {key for rule in base_rules for key in rule.inputs}
        settings = settings or AISettings.from_env()
        if mode == 'ai' and settings.problem():
            raise InputError(settings.problem())
        if extractor is None and mode != 'local' and not settings.problem():
            extractor = AIExtractor(settings, {key: detail for r in base_rules for key, detail in r.inputs.items()})
        documents = materials.preview(uploads, keys, extractor, allow_incomplete_company=True, capture_standard=True)
        # Server-generated stable IDs survive reanalysis/restart, not indices
        # whose meaning could change after supplementing another upload.
        for doc in documents:
            doc['id'] = secrets.token_hex(12)
            source = next((name for name in names if doc['name'] == name), None)
            if source is None:
                source = next((name for name in names if name.lower().endswith('.zip') and doc['name'].startswith(name + '/')), None)
            if source is None:
                raise InputError('无法关联材料与上传原件。')
            doc['original_upload_name'] = source
        return documents, base_rules

    def upload_work(user, uploads, mode, client_id, scope_context):
        batches.authorize_client(get_store(), user, client_id)
        documents, base_rules = parse_uploads(uploads, mode)
        company, origins = enterprise_scope.prefill(enterprise_analysis.prefill(documents), scope_context)
        payload = _analysis_payload(documents, company, {}, base_rules, rules_for_dataset, scope_context)
        payload['scope_context']['prefill_origins'] = origins
        item = batches.create(get_store(), user, uploads, client_id, initial_payload=payload)
        return project(batches.read(get_store(), user, item['id']), user)

    def process_job(job, snapshot, previous, documents, uploads):
        configured = AISettings.from_env()
        frozen = {key: value for key, value in snapshot['settings'].items()
                  if key not in {'api_key', 'backup_api_keys'}}
        if snapshot['mode'] != 'local' and frozen['enabled'] and (configured.problem() or configured.base_url != frozen['base_url']):
            raise InputError('模型服务配置已变化，请检查后重试。')
        settings = AISettings(**frozen, api_key=configured.api_key,
                              backup_api_keys=configured.backup_api_keys)
        base_rules = [Rule(**rule) for rule in snapshot['rules']]
        extractor = AIExtractor(settings, {key:detail for rule in base_rules for key,detail in rule.inputs.items()}) \
            if snapshot['mode']!='local' and not settings.problem() else None
        if snapshot.get('kind') == 'pdf_extraction':
            if extractor is None or not previous:
                raise InputError('PDF 模型服务或分析版本不可用。')
            document_id, consent = snapshot['document_id'], snapshot['consent']
            original_document = next((d for d in previous['documents'] if d['id'] == document_id), None)
            if not original_document or original_document['sha256'] != consent['source_sha256']:
                raise InputError('PDF 原件或材料编号已变化。')
            expanded = dict(materials.expand_uploads([(name, raw) for _, name, raw in uploads]))
            raw = expanded.get(original_document['name'])
            if raw is None or hashlib.sha256(raw).hexdigest() != consent['source_sha256']:
                raise InputError('PDF 完整原件指纹不匹配。')
            candidate = deepcopy(original_document)
            candidate.update(company=dict.fromkeys(materials.COMPANY_KEYS, ''), rows=[], pages=[], warnings=[], error='')
            candidate.pop('pdf_selection', None)
            candidate['format_check'] = material_format_guard.metadata(candidate['name'], raw)
            started = material_provenance.now()
            materials._pdf(raw, candidate, set(previous['fields']) | set(config.PERIOD_SERIES), consent['range'])
            if candidate['company'].get('period'):
                try:
                    original_period = periods.parse_period(candidate['company']['period'], '原件期间')
                except InputError:
                    original_period = None
                if original_period and original_period.key != periods.parse_period(consent['period'], 'AI 核对期').key:
                    raise InputError('原件明确期间与 AI 核对期不同，未发送至模型。')
            candidate['extraction'] = {'method': 'local', 'local': {
                'program': material_provenance.program('local'), 'status': 'succeeded',
                'started_at': started, 'finished_at': material_provenance.now()}}
            try:
                extractor.enrich(candidate, raw, review_period=consent['period'])
            except ExtractionError as exc:
                candidate['warnings'].append(str(exc) + ' 本次结果未放行；请核对后重新授权提取或明确排除此材料。')
            candidate['extraction']['pdf_consent'] = deepcopy(consent)
            candidate['review_required'] = True
            updated = [candidate if d['id'] == document_id else deepcopy(d) for d in previous['documents']]
            selections = deepcopy(previous['selections'])
            selected = selections.setdefault(document_id, {})
            selected['rows'], selected['evidence_reviews'] = deepcopy(candidate['rows']), []
            payload = _analysis_payload(updated, deepcopy(previous['company']), selections, base_rules, rules_for_dataset, previous.get('scope_context'))
            payload['changes'] = [{'file_id': document_id, 'field': 'rows',
                'old': deepcopy(previous['selections'].get(document_id, {}).get('rows', original_document['rows'])),
                'new': deepcopy(candidate['rows']), 'origin': 'ai', 'source': candidate['name']}]
            return payload
        new_documents = []
        for original_id, name, raw in uploads:
            parsed, _ = parse_uploads([(name,raw)], snapshot['mode'],settings,base_rules,extractor)
            for document in parsed:
                document['original_id'] = original_id
            new_documents.extend(parsed)
        combined = documents + new_documents
        if len(combined)>20:
            raise InputError('单批次累计最多 20 份解析材料。')
        scope_context = previous.get('scope_context') if previous else snapshot.get('scope_context')
        company = deepcopy(previous['company']) if previous else enterprise_analysis.prefill(combined)
        if not previous and scope_context:
            company, origins = enterprise_scope.prefill(company, scope_context)
            scope_context = {**scope_context, 'prefill_origins': origins}
        selections = deepcopy(previous['selections']) if previous else {}
        differences = []
        candidate = enterprise_analysis.prefill(new_documents)
        for field,value in candidate.items():
            if value and company.get(field) and value != company[field]:
                differences.append({'field':field,'retained':company[field],'new_candidate':value})
            elif value and not company.get(field):
                company[field] = value
        payload = _analysis_payload(combined,company,selections,base_rules,rules_for_dataset,scope_context)
        payload['supplement_differences'] = differences
        payload['analysis']['supplement_differences'] = differences
        return payload

    app.state.material_worker = material_jobs.Worker(get_store,process_job)

    async def body(request, allowed):
        try:
            raw = b''.join([part async for part in bounded_stream(request, 2 * 1024 * 1024)])
            def invalid_constant(_value):
                raise ValueError()
            value = json.loads(raw, parse_constant=invalid_constant)
            if not isinstance(value, dict) or set(value) - allowed:
                raise ValueError()
            if type(value.get('expected_revision')) is not int or value['expected_revision'] < 0:
                raise ValueError()
            return value
        except (ValueError, MultiPartException):
            raise HTTPException(422, '材料请求格式无效、版本号无效或超过 2MB。') from None

    @app.get('/api/enterprise/materials/config')
    def configuration(session: str | None = Cookie(default=None, alias='taxpearls_session')):
        actor(session)
        try:
            batches._key()
            return {'retention_ready': True, 'message': '原件受控留存已配置；一次检测一家企业、一个主期间。'}
        except batches.MaterialStorageError:
            return {'retention_ready': False, 'message': '请由部署人员配置独立材料加密密钥，再使用企业上传。不会降级明文留存。'}

    @app.post('/api/enterprise/materials')
    async def upload(request: Request, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        try:
            async with multipart(request, total_limit=materials.MAX_TOTAL + 1024 * 1024, max_files=20, max_fields=4, material_scope=user['org_id']) as form:
                uploads = [(v.filename or '未命名', await v.read()) for _, v in form.multi_items() if isinstance(v, UploadFile)]
                mode, client_id = form.get('extraction', 'local'), form.get('client_id') or None
                company_mode = form.get('company_mode', 'existing' if client_id else 'new')
                raw_company = form.get('company', '{}')
                try:
                    company = json.loads(raw_company) if isinstance(raw_company, str) and len(raw_company) <= 4096 else None
                except ValueError:
                    raise InputError('企业信息JSON格式无效。') from None
                await form.close()
                if mode not in {'local', 'auto', 'ai'} or client_id is not None and not isinstance(client_id, str):
                    raise InputError('提取方式或客户档案格式无效。')
                scope_context = enterprise_scope.context(get_store(), user, company_mode, client_id, company)
                if material_jobs.QueueSettings.from_env().enabled:
                    queued = await run_in_threadpool(material_jobs.submit,get_store(),user,uploads,mode,engine.load_rules(rules_dir),client_id=client_id,scope_context=scope_context)
                    return JSONResponse(project(batches.read(get_store(),user,queued['id']),user),status_code=202)
                if not slots.acquire(blocking=False):
                    raise HTTPException(429, '材料分析任务较多，请稍后重试。')
                try:
                    return await run_in_threadpool(upload_work, user, uploads, mode, client_id, scope_context)
                finally:
                    slots.release()
        except (InputError, batches.MaterialStorageError, MultiPartException) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get('/api/enterprise/materials')
    def list_batches(session: str | None = Cookie(default=None, alias='taxpearls_session')):
        return batches.listing(get_store(), actor(session))

    @app.get('/api/enterprise/client-profile/{client_id}')
    def client_profile(client_id: str, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        return enterprise_scope.profile(get_store(), actor(session), client_id)

    @app.post('/api/enterprise/materials/{batch_id}/retry')
    async def retry_material_job(batch_id: str, request: Request, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        value = await body(request, {'expected_revision','job_id','consent'})
        if not isinstance(value.get('job_id'),str):
            raise HTTPException(422,'任务编号无效。')
        try:
            await run_in_threadpool(material_jobs.retry,get_store(),user,batch_id,value['job_id'],value['expected_revision'],consent=value.get('consent',False))
        except InputError as exc:
            raise HTTPException(422,str(exc)) from None
        return JSONResponse(project(batches.read(get_store(),user,batch_id),user),status_code=202)

    @app.post('/api/enterprise/materials/{batch_id}/extract')
    async def extract_pdf(batch_id: str, request: Request, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        value = await body(request, {'expected_revision', 'document_id', 'consent', 'period_start', 'period_end'})
        try:
            await run_in_threadpool(material_jobs.submit_pdf, get_store(), user, batch_id,
                value['expected_revision'], value, engine.load_rules(rules_dir))
            return JSONResponse(project(batches.read(get_store(), user, batch_id), user), status_code=202)
        except (InputError, batches.MaterialStorageError) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get('/api/enterprise/materials/{batch_id}')
    def details(batch_id: str, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        try:
            return project(batches.read(get_store(), user, batch_id), user)
        except batches.MaterialStorageError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get('/api/enterprise/materials/{batch_id}/status')
    def job_status(batch_id: str, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        return material_jobs.status(get_store(),actor(session),batch_id)

    @app.get('/api/enterprise/materials/{batch_id}/trace')
    def trace(batch_id: str, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        try:
            view = batches.read(get_store(), user, batch_id)
        except batches.MaterialStorageError as exc:
            raise HTTPException(409, str(exc)) from None
        # Scope and review details are visible only after current batch/client
        # authorization. Do not expose raw accounting/personnel datasets here.
        versions = []
        for record in view['versions']:
            payload = record['payload']
            detail = {k: payload[k] for k in ('company', 'selections', 'parser_version',
                      'supplement_differences', 'deleted_file_id', 'requires_confirmation',
                      'analysis_revision', 'audit_id', 'analysis_sha256', 'scope',
                      'files', 'edits', 'checks', 'feedback') if k in payload}
            if 'changes' in payload:
                detail['changes'] = payload['changes']
            if 'analysis' in payload:
                detail['analysis'] = {k: v for k, v in payload['analysis'].items() if k != 'dataset'}
            if 'documents' in payload:
                # Keep failed/incomplete-scope extraction visible as well: those
                # documents need not have reached analysis.files yet.
                detail['extractions'] = [{k: doc.get(k) for k in
                    ('id', 'name', 'sha256', 'original_id', 'extraction', 'security', 'error', 'pdf_selection')}
                    for doc in payload['documents']]
            versions.append({**{k: v for k, v in record.items() if k != 'payload'}, 'detail': detail})
        return {'id': batch_id, 'revision': view['revision'], 'versions': versions, 'events': view['events'], 'jobs':view['jobs']}

    @app.get('/api/audits/{audit_id}/materials')
    def confirmed_materials(audit_id: str, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        try:
            return {'confirmation': batches.confirmed(get_store(), actor(session), audit_id)}
        except batches.MaterialStorageError as exc:
            raise HTTPException(409, str(exc)) from None

    def upgrade_standard_sources(user, view, previous):
        """Older retained batches gain editable source coordinates on reanalysis.

        Read exact retained bytes with a dedicated event; never redownload from
        outside, invoke AI, replace originals or rewrite historical revisions.
        """
        from src.workbooks import open_workbook
        previous = deepcopy(previous)
        expanded = {}
        for doc in previous['documents']:
            rescan = doc.get('security', {}).get('version') != material_security.VERSION and doc['kind'] in {'xlsx', 'xls', 'xml'}
            format_rescan = doc.get('format_check', {}).get('version') != material_formats.VERSION if isinstance(doc.get('format_check'), dict) else True
            recapture = doc['kind'] in {'xlsx', 'xls'} and not doc.get('review_required') and 'standard_tables' not in doc
            if doc.get('error') or not (rescan or recapture or format_rescan):
                continue
            fid = doc.get('original_id')
            original_file = next((f for f in view['files'] if f['id'] == fid), None)
            if not original_file or original_file['deleted_at']:
                continue
            if fid not in expanded:
                meta, raw = batches.original(get_store(), user, view['id'], fid, purpose='read_for_reanalysis')
                expanded[fid] = dict(materials.expand_uploads([(meta['name'], raw)]))
            raw = expanded[fid].get(doc['name'])
            if raw is None or hashlib.sha256(raw).hexdigest()[:16] != doc['fingerprint']:
                raise InputError('原件与历史解析文件指纹不匹配，请核查材料。')
            doc['format_check'] = material_format_guard.metadata(doc['name'], raw)
            if doc['kind'] in {'xlsx', 'xls'}:
                book = open_workbook(raw)
                try:
                    if rescan:
                        material_security.scan_workbook(book, doc)
                    if recapture:
                        doc['standard_tables'] = material_review.capture(book)
                finally:
                    book.close()
                if recapture:
                    material_review.annotate(doc, doc['standard_tables'], {})
            elif doc['kind'] == 'xml' and rescan:
                material_security.add_findings(doc, material_security.detect(
                    '\n'.join(material_formats.xml_root(raw).itertext()), location='XML'))
        return previous

    def analyze_work(user, batch_id, value):
        view = batches.read(get_store(), user, batch_id)
        if view['revision'] != value['expected_revision']:
            raise AccessDenied('材料已变化，请重新读取。', 409)
        if any(j['state'] in {'queued','running'} for j in view.get('jobs',[])):
            raise AccessDenied('请等待当前材料分析完成后再保存修改。',409)
        if any(j['state']=='failed' and j['input_revision']==view['revision'] for j in view.get('jobs',[])):
            raise AccessDenied('补传材料分析失败，请先重试或继续补传，不能将旧分析保存为新版本。',409)
        previous = upgrade_standard_sources(user, view, _latest(view)['payload'])
        company = {**previous['company'], **value.get('company', {})}
        if set(company) - set(enterprise_scope.FIELDS):
            raise InputError('企业信息含未知字段。')
        if any(not isinstance(v, str) for v in company.values()):
            raise InputError('企业信息须为文本。')
        selections = deepcopy(previous['selections'])
        for file_id, change in value.get('selections', {}).items():
            if not isinstance(change, dict):
                raise InputError('材料选择格式无效。')
            selections[file_id] = {**selections.get(file_id, {}), **change}
        original_previous = deepcopy(previous)
        mapped_originals = {}
        for index, document in enumerate(previous['documents']):
            selection = selections.get(document['id'], {})
            current_options = selection.get('import_options', {})
            old_options = previous['selections'].get(document['id'], {}).get('import_options', {})
            stale_adapter = financial_import.reanalysis_required(document)
            if current_options == old_options and (not stale_adapter or selection.get('purpose') == 'excluded'):
                continue
            if not document.get('import_mapping'):
                raise InputError('只有原件识别出的财务导出工作表可以指定映射。')
            financial_import.options(current_options, [s['sheet'] for s in document['import_mapping']['sheets']])
            supplied = value.get('selections', {}).get(document['id'], {})
            if supplied.get('standard_edits'):
                raise InputError('更新财务适配或改变映射时不能同时沿用数值修正，请先读取新映射。')
            fid = document.get('original_id')
            if fid not in mapped_originals:
                meta, raw = batches.original(get_store(), user, batch_id, fid, purpose='read_for_reanalysis')
                mapped_originals[fid] = dict(materials.expand_uploads([(meta['name'], raw)]))
            raw = mapped_originals[fid].get(document['name'])
            if raw is None or hashlib.sha256(raw).hexdigest() != document['sha256']:
                raise InputError('财务映射的原件指纹不匹配。')
            if document['kind'] in {'csv', 'tsv'}:
                raw = materials._table_workbook(document['name'], raw, document['kind'])
            candidate = deepcopy(document)
            for key in ('import_model', 'import_mapping', 'account_cell_sources', 'declaration_cell_sources'):
                candidate.pop(key, None)
            # Reparse retained source bytes, not browser-provided amounts or metadata.
            candidate['warnings'] = []
            mapping_started = material_provenance.now()
            materials._excel(raw, candidate, allow_incomplete_company=True, capture_standard=True,
                keys=set(previous['fields']) | set(config.PERIOD_SERIES), import_options=current_options)
            candidate['extraction']['mapping_reanalysis'] = {
                'started_at': mapping_started, 'finished_at': material_provenance.now(),
                'program': material_provenance.program('local'),
                'adapter_version': financial_import.VERSION,
                'options_digest': material_provenance.digest(current_options), 'status': 'succeeded'}
            previous['documents'][index] = candidate
            # API clients must obey the same fresh-review boundary as the page.
            selection['standard_edits'] = {}
            selection['evidence_reviews'] = []
        for index, document in enumerate(previous['documents']):
            selection = selections.get(document['id'], {})
            current_range = selection.get('pdf_range')
            old_range = original_previous['selections'].get(document['id'], {}).get('pdf_range')
            stale_pdf = material_provenance.pdf_reanalysis_required(document)
            if current_range == old_range and (not stale_pdf or selection.get('purpose') == 'excluded'):
                continue
            if document['kind'] != 'pdf':
                raise InputError('只有 PDF 原件可以选择原始页码片段。')
            supplied = value.get('selections', {}).get(document['id'], {})
            if supplied.get('rows') or supplied.get('evidence_reviews'):
                raise InputError('更新 PDF 适配或改变片段时不能同时沿用金额或复核，请先重新读取原件。')
            fid = document.get('original_id')
            if fid not in mapped_originals:
                meta, raw = batches.original(get_store(), user, batch_id, fid, purpose='read_for_reanalysis')
                mapped_originals[fid] = dict(materials.expand_uploads([(meta['name'], raw)]))
            raw = mapped_originals[fid].get(document['name'])
            if raw is None or hashlib.sha256(raw).hexdigest() != document['sha256']:
                raise InputError('PDF 片段的完整原件指纹不匹配。')
            candidate = deepcopy(document)
            candidate.update(company=dict.fromkeys(materials.COMPANY_KEYS, ''), rows=[], pages=[], warnings=[], error='')
            candidate.pop('pdf_selection', None)
            started = material_provenance.now()
            materials._pdf(raw, candidate, set(previous['fields']) | set(config.PERIOD_SERIES), current_range)
            material_security.scan_pages(candidate)
            candidate['extraction'] = {'method': 'local', 'local': {
                'program': material_provenance.program('local'),
                'status': 'awaiting_selection' if candidate.get('pdf_selection', {}).get('pending') else 'succeeded',
                'started_at': started, 'finished_at': material_provenance.now()},
                'page_reanalysis': {'source_sha256': candidate['sha256'], 'range': current_range,
                                    'mode': 'local', 'model_calls': 0}}
            previous['documents'][index] = candidate
            # Old page edits/reviews never cross into a newly selected source interval.
            selection['rows'] = deepcopy(candidate['rows'])
            selection['evidence_reviews'] = []
        for doc in previous['documents']:
            original_file = next((f for f in view['files'] if f['id'] == doc.get('original_id')), None)
            if original_file is None or original_file['deleted_at']:
                # Removal must be explicit; never silently execute a stale
                # parsed candidate whose original has since been deleted.
                if selections.get(doc['id'], {}).get('purpose') != 'excluded':
                    raise AccessDenied('原件已删除，请明确排除关联材料后重新分析。', 409)
        payload = _analysis_payload(previous['documents'], company, selections,
                                    engine.load_rules(rules_dir), rules_for_dataset, previous.get('scope_context'))
        if payload.get('scope_context'):
            origins = payload['scope_context'].setdefault('prefill_origins', {})
            for key in value.get('company', {}):
                if payload['company'].get(key) != original_previous['company'].get(key):
                    origins[key] = 'user'
        payload['changes'] = _field_changes(original_previous, payload)
        revision = batches.append_revision(get_store(), user, batch_id, view['revision'], 'edit', payload)
        result = project(batches.read(get_store(), user, batch_id), user)
        if result['revision'] != revision:
            raise AccessDenied('材料随后发生变化，请重新读取。', 409)
        return result

    def supplement_work(user, batch_id, revision, uploads, mode):
        view = batches.read(get_store(), user, batch_id)
        if view['revision'] != revision:
            raise AccessDenied('材料已变化，请重新读取后补传。', 409)
        previous = upgrade_standard_sources(user, view, _latest(view)['payload'])
        documents, base_rules = parse_uploads(uploads, mode)
        combined = deepcopy(previous['documents']) + documents
        if len(combined) > 20:
            raise InputError('单批次累计最多 20 份解析材料；请另开检测，不覆盖旧材料。')
        selections = deepcopy(previous['selections'])
        for doc in combined:
            deleted = next((f for f in view['files'] if f['id'] == doc.get('original_id') and f['deleted_at']), None)
            if deleted and selections.get(doc['id'], {}).get('purpose') != 'excluded':
                raise AccessDenied('请先明确排除已删除原件关联的材料。', 409)
        candidate = enterprise_analysis.prefill(documents)
        company = deepcopy(previous['company'])
        differences = []
        # Previously entered values always survive. New candidate differences
        # are shown, not used to silently overwrite the last analysis.
        for field, value in candidate.items():
            if value and company.get(field) and value != company[field]:
                differences.append({'field': field, 'retained': company[field], 'new_candidate': value})
            elif value and not company.get(field):
                company[field] = value
        payload = _analysis_payload(combined, company, selections, base_rules, rules_for_dataset, previous.get('scope_context'))
        payload['supplement_differences'] = differences
        payload['analysis']['supplement_differences'] = differences
        batches.supplement(get_store(), user, batch_id, revision, uploads, payload)
        return project(batches.read(get_store(), user, batch_id), user)

    @app.post('/api/enterprise/materials/{batch_id}/supplement')
    async def supplement_materials(batch_id: str, request: Request,
                                   session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        # Deny inaccessible batches before reading uploaded bytes.
        await run_in_threadpool(batches.read, get_store(), user, batch_id)
        try:
            async with multipart(request, total_limit=materials.MAX_TOTAL + 1024 * 1024, max_files=20, max_fields=2, material_scope=user['org_id']) as form:
                uploads = [(v.filename or '未命名', await v.read()) for _, v in form.multi_items() if isinstance(v, UploadFile)]
                mode = form.get('extraction', 'local')
                raw_revision = form.get('expected_revision', '')
                await form.close()
                if not isinstance(raw_revision, str) or not raw_revision.isdecimal() or mode not in {'local', 'auto', 'ai'}:
                    raise InputError('补传版本号或提取方式无效。')
                if material_jobs.QueueSettings.from_env().enabled:
                    queued = await run_in_threadpool(material_jobs.submit,get_store(),user,uploads,mode,engine.load_rules(rules_dir),batch_id=batch_id,revision=int(raw_revision))
                    return JSONResponse(project(batches.read(get_store(),user,queued['id']),user),status_code=202)
                if not slots.acquire(blocking=False):
                    raise HTTPException(429, '材料分析任务较多，请稍后重试。')
                try:
                    return await run_in_threadpool(supplement_work, user, batch_id, int(raw_revision), uploads, mode)
                finally:
                    slots.release()
        except (InputError, batches.MaterialStorageError, MultiPartException) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post('/api/enterprise/materials/{batch_id}/analyze')
    async def analyze_materials(batch_id: str, request: Request,
                                session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        value = await body(request, {'expected_revision', 'company', 'selections'})
        if not isinstance(value.get('company', {}), dict) or not isinstance(value.get('selections', {}), dict):
            raise HTTPException(422, '企业信息和材料选择须为对象。')
        if not slots.acquire(blocking=False):
            raise HTTPException(429, '材料分析正在处理其他请求，请稍后重试。')
        try:
            return await run_in_threadpool(analyze_work, user, batch_id, value)
        except (InputError, batches.MaterialStorageError) as exc:
            raise HTTPException(422, str(exc)) from None
        finally:
            slots.release()

    def execute_work(user, batch_id, revision):
        store = get_store()
        existing = batches.execution(store, user, batch_id, revision)
        if existing:
            return audit_result(existing, user)
        view = batches.read(store, user, batch_id)
        latest = _latest(view)
        if view['revision'] != revision or latest['revision'] != revision:
            # A concurrent confirmation may have advanced the batch after the first lookup.
            existing = batches.execution(store, user, batch_id, revision)
            if existing:
                return audit_result(existing, user)
            raise AccessDenied('材料已变化，请重新核对并确认当前分析。', 409)
        payload = latest['payload']
        # Check retained originals independently of cached readiness, without changing the frozen payload.
        selected_originals = {d.get('original_id') for d in payload['documents']
            if payload['selections'].get(d['id'], {}).get('purpose', 'current') != 'excluded'}
        for fid in selected_originals:
            if not fid:
                raise AccessDenied('材料缺少原件关联，请重新上传核对。', 409)
            meta, raw = batches.original(store, user, batch_id, fid, purpose='read_for_reanalysis')
            try:
                batches.validate_uploads([(meta['name'], raw)])
            except batches.MaterialStorageError as exc:
                raise AccessDenied(str(exc), 409) from None
        if enterprise_analysis.extraction_blockers(payload['documents'], payload['selections'], payload['company']):
            raise AccessDenied('所选材料未通过提取、安全检查或逐项证据复核，请处理后重新分析。', 409)
        if payload['analysis']['can_confirm'] is not True:
            raise AccessDenied('请先处理阻止检测的材料问题。', 409)
        if payload.get('graph_rule') != asdict(related_graph.definition()):
            raise AccessDenied('关联方检查范围或版本已变化，请重新分析并确认材料。', 409)
        context = {'batch_id': batch_id, 'revision': revision, 'sha256': hashlib.sha256(batches._json(payload)).hexdigest()}
        try:
            return save_audit(deserialize_dataset(payload['analysis']['dataset']), user, view['client_id'],
                              frozen_rules=[Rule(**r) for r in payload['rules']],
                              frozen_graph_rule=Rule(**payload['graph_rule']), material_context=context)
        except AccessDenied as exc:
            if exc.status != 409:
                raise
            # Another confirmed request may have won the write transaction.
            existing = batches.execution(store, user, batch_id, revision)
            if existing:
                return audit_result(existing, user)
            raise

    @app.post('/api/enterprise/materials/{batch_id}/confirm')
    async def confirm(batch_id: str, request: Request, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        value = await body(request, {'expected_revision'})
        try:
            return await run_in_threadpool(execute_work, user, batch_id, value['expected_revision'])
        except batches.MaterialStorageError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get('/api/enterprise/materials/{batch_id}/originals/{file_id}')
    def download(batch_id: str, file_id: str, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        try:
            meta, raw = batches.original(get_store(), user, batch_id, file_id)
            batches.validate_uploads([(meta['name'], raw)])
            # Native validation may take seconds; recheck live permission and
            # deletion after it completes, immediately before returning bytes.
            with get_store().connect() as db:
                db.execute('BEGIN')
                batches._authorize(db, user, batch_id)
                current = db.execute('SELECT deleted_at FROM material_originals WHERE id=? AND batch_id=?',
                                     (file_id, batch_id)).fetchone()
                if not current:
                    raise AccessDenied()
                if current['deleted_at'] is not None:
                    raise AccessDenied('原始材料已删除，不能再下载。', 410)
        except batches.MaterialStorageError as exc:
            raise HTTPException(409, str(exc)) from None
        return Response(raw, media_type='application/octet-stream', headers={
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(meta['name'], safe=''),
            'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.get('/api/enterprise/materials/{batch_id}/originals/{file_id}/deletion-impact')
    def impact(batch_id: str, file_id: str, session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        return batches.deletion_impact(get_store(), user, batch_id, file_id)

    @app.delete('/api/enterprise/materials/{batch_id}/originals/{file_id}')
    async def remove(batch_id: str, file_id: str, request: Request,
                     session: str | None = Cookie(default=None, alias='taxpearls_session')):
        user = actor(session)
        value = await body(request, {'expected_revision'})
        try:
            deleted = await run_in_threadpool(batches.delete_original, get_store(), user, batch_id, file_id,
                                              value['expected_revision'])
        except batches.MaterialStorageError as exc:
            raise HTTPException(409, str(exc)) from None
        return {'deleted': deleted, 'backup_copies_may_remain': True}
