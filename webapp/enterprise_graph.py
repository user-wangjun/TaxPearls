"""Taxpayer-scoped history views built from immutable, authorized audit snapshots."""
from fastapi import HTTPException

from webapp.access import audit_scope, current_actor
from webapp.enterprise_scope import identity
from webapp.knowledge import build_graph, node_id


def read_enterprise_history(store, user, taxpayer_id=None, audit_id=None):
    """Read identity, permissions and snapshots in one database transaction."""
    requested = identity(taxpayer_id) if taxpayer_id is not None else None
    if requested == "":
        raise HTTPException(422, "请提供企业纳税人识别号。")
    where, args = audit_scope(user)
    with store.connect() as db:
        db.execute("BEGIN")
        current_actor(db, user, {'org_admin', 'accountant', 'teacher'})
        groups = {}

        def group(tax_id, name):
            key = identity(tax_id)
            if not key:
                return None
            item = groups.setdefault(key, {"taxpayer_id": key, "name": name,
                                          "names": [], "audits": []})
            if name and name not in item['names']:
                item['names'].append(name)
            return item

        if user['role'] in {'org_admin', 'accountant'}:
            condition = 'org_id=?'
            client_args = [user['org_id']]
            if user['role'] == 'accountant':
                condition += ' AND accountant_id=?'
                client_args.append(user['id'])
            for row in db.execute('SELECT id,name,taxpayer_id FROM clients WHERE ' + condition + ' ORDER BY created_at DESC,rowid DESC', client_args):
                group(row['taxpayer_id'], row['name'])
        rows = db.execute('''SELECT a.id,a.company_name,a.taxpayer_id,a.period,a.audited_at
            FROM audits a LEFT JOIN clients c ON c.id=a.client_id WHERE ''' + where +
                          ' ORDER BY a.audited_at DESC,a.rowid DESC', args)
        for row in rows:
            item = group(row['taxpayer_id'], row['company_name'])
            if item is not None:
                item['audits'].append({key: row[key] for key in ('id', 'period', 'audited_at')})
        if requested is None:
            return sorted(groups.values(), key=lambda item: (item['name'], item['taxpayer_id']))
        if requested not in groups:
            raise HTTPException(404, "企业不存在或无权查看。")
        company = groups[requested]
        audit_ids = [row['id'] for row in company['audits']]
        if audit_id and audit_id not in audit_ids:
            raise HTTPException(404, "该审查记录不属于所选企业或无权查看。")
        entries = []
        for key in ([audit_id] if audit_id else audit_ids):
            entry = store._audit_dict(db.execute('SELECT * FROM audits WHERE id=?', (key,)).fetchone())
            if identity(entry['dataset'].company.taxpayer_id) != requested:
                raise HTTPException(409, "审查快照税号与企业归属不一致，请先核对原始记录。")
            entries.append(entry)
        return company, entries


def build_enterprise_graph(company, entries, org_id, audit_id=None):
    """Never merge period facts, frozen rules or repeated findings into one node."""
    company_id = node_id('company', org_id + ':' + company['taxpayer_id'])
    nodes = [{"id": company_id, "kind": "company", "label": company['name'],
              "taxpayer_id": company['taxpayer_id'], "company_names": company['names'],
              "audit_count": len(company['audits'])}]
    edges, previous = [], {}
    coverage = {"shown": 0, "limit": 0, "truncated": False}
    for entry in entries:
        graph = build_graph([], entry)
        record_id = node_id('audit', entry['id'])
        record = {"id": record_id, "kind": "audit", "label": entry['dataset'].company.period,
                  "period": entry['dataset'].company.period, "audit_id": entry['id'],
                  "audited_at": entry['audited_at']}
        nodes.append(record)
        edges.append({"source": company_id, "target": record_id, "label": "审查记录"})
        ids = {node['id']: company_id if node['kind'] == 'company' else record_id + ':' + node['id']
               for node in graph['nodes']}
        for node in graph['nodes']:
            if node['kind'] == 'company':
                continue
            copied = {**node, "id": ids[node['id']], "audit_id": entry['id'],
                      "audit_period": record['period'], "audited_at": entry['audited_at']}
            if 'rule_id' in copied:
                copied['rule_code'] = copied['rule_id']
                copied['rule_id'] = ids[copied['rule_id']]
            elif copied['kind'] == 'rule':
                copied['rule_code'] = node['id']
            nodes.append(copied)
            if node['kind'] == 'risk':
                edges.append({"source": record_id, "target": copied['id'], "label": "核对结果"})
                code = node['rule_id']
                if code in previous:
                    edges.append({"source": previous[code], "target": copied['id'], "label": "同项历史记录"})
                previous[code] = copied['id']
        edges.extend({**edge, "source": ids[edge['source']], "target": ids[edge['target']]}
                     for edge in graph['edges'])
        for field in ('shown', 'limit'):
            coverage[field] += graph['related_paths'][field]
        coverage['truncated'] |= graph['related_paths']['truncated']
    return {"nodes": nodes, "edges": edges, "taxpayer_id": company['taxpayer_id'],
            "audit_id": audit_id, "audits": company['audits'], "related_paths": coverage,
            "empty": not entries}
