"""Bind the independent policy review to current retained core-task evidence."""
from copy import deepcopy

from src import policies
from src.input_errors import InputError
from src.kg_retrieval.common import digest
from webapp.access import AccessDenied


def signature(payload):
    return digest({key: payload.get(key) for key in ('task', 'documents', 'source_options')})


def assess(payload, value=None):
    inputs = policies.normalize(payload.get('policy_input') if value is None else value)
    task = payload['task']
    scope = {'name': task['company_name'], 'taxpayer_id': task['tax_no'],
             'period_start': task['start_date'], 'period_end': task['end_date']}
    # The task retains only explicitly confirmed taxpayer metadata. Never infer
    # qualification from a VAT mapping review or a similarly named metric.
    raw = policies.assess(scope, {**inputs, 'reviews': {}})
    if raw is None:
        return None
    source_signature = signature(payload)
    tokens = {r['id']: digest({'review': r['review_token'], 'task_sources': source_signature})
              for r in raw['results'] if r['review_token']}
    accepted = {r['id']: r['review_token'] for r in raw['results']
                if r['id'] in tokens and inputs['reviews'].get(r['id']) == tokens[r['id']]}
    result = policies.assess(scope, {**inputs, 'reviews': accepted})
    for row in result['results']:
        row['review_token'] = tokens.get(row['id'])
    return result


def update(payload, value=None):
    inputs = policies.normalize(payload.get('policy_input') if value is None else value)
    payload['policy_assessment'] = assess(payload, inputs)
    payload['policy_input'] = inputs
    payload['policy_source_signature'] = signature(payload)
    return payload


def project(payload):
    if not payload.get('policy_input', {}).get('selected'):
        return payload
    try:
        fresh = assess(payload)
        payload['policy_result_current'] = fresh == payload.get('policy_assessment')
        payload['policy_assessment'] = fresh
    except (InputError, ValueError, OSError) as exc:
        payload['policy_result_current'] = False
        payload['policy_error'] = str(exc)
        old = deepcopy(payload.get('policy_assessment'))
        if old:
            for row in old['results']:
                row.update(status='needs_review', status_label='待核对', reviewed=False, review_token=None)
            old['consistency'].update(status='needs_review', status_label='政策版本已变化，须重新保存核对。')
        payload['policy_assessment'] = old
    return payload


def current_for_report(payload):
    try:
        result = assess(payload)
    except (InputError, ValueError, OSError) as exc:
        raise AccessDenied('政策版本无法核对，请重新读取并保存：' + str(exc), 409) from None
    if result != payload.get('policy_assessment'):
        raise AccessDenied('政策来源或判断版本已变化，请重新保存政策核对；旧报告保留。', 409)
    return deepcopy(result)
