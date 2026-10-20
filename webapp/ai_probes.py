"""Administrator-only, single-key diagnostics; never replay or retain secrets."""
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from src.ai_transport import NoRedirect, TransportError, _content
from src.settings import AISettings
from src import ai_reasoning

PROVIDERS = {
    'deepseek': ('DeepSeek', 'https://api.deepseek.com', 'deepseek-flash'),
    'openrouter': ('OpenRouter', 'https://openrouter.ai/api/v1', ''),
}
MESSAGES = {
    'invalid_key': '密钥被拒绝或已失效。',
    'insufficient_balance': '服务商返回余额或额度不足。',
    'forbidden': '该密钥没有接口或模型权限。',
    'rate_limited': '服务商限流；不能据此认定密钥失效。',
    'not_found': '接口或模型不存在，请核对服务配置。',
    'upstream_error': '服务商暂时异常。',
    'http_error': '服务商拒绝请求，请核对接口与模型参数。',
    'timeout': '请求超时，未自动重试；调用是否计费请以服务商为准。',
    'network': '网络或 TLS 连接失败。',
    'schema': '响应格式不符合要求。',
    'incomplete': '模型输出未完整结束。',
    'empty': '模型未返回有效内容。',
    'oversized': '服务商响应超过大小上限。',
    'unsupported': '该服务地址未适配余额查询，请选择其支持的余额协议或到服务商后台查看。',
}


def identity(settings):
    # Configuration changes invalidate the old diagnostic, including model flags.
    value = [settings.base_url, settings.api_key, settings.effective_model,
             settings.json_mode, settings.disable_thinking, settings.vision,
             settings.reasoning_effort, settings.reasoning_protocol]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def balance_protocol(settings):
    parsed = urlsplit(settings.base_url)
    if parsed.port not in (None, 443):
        return 'unsupported'
    host = (parsed.hostname or '').lower()
    if host == 'api.deepseek.com' and parsed.path.rstrip('/') in ('', '/v1'):
        return 'deepseek'
    if host == 'openrouter.ai' and parsed.path.rstrip('/') == '/api/v1':
        return 'openrouter'
    return 'unsupported'


def configured(settings):
    return [replace(settings, enabled=True, api_key=key, backup_api_keys=())
            for key in settings.api_keys]


def catalog(settings, logs):
    last = {}
    for row in logs:
        if row['action'] != 'ai_key_probe':
            continue
        try:
            result = json.loads(row['detail'])
            last.setdefault((row['target_id'], result['operation']), result)
        except (ValueError, KeyError, TypeError):
            continue
    return {'enabled': settings.enabled, 'providers': [
        {'id': key, 'name': item[0], 'model': item[2]} for key, item in PROVIDERS.items()
    ], 'keys': [
        {'id': identity(item), 'name': '主用密钥' if item.api_key == settings.api_key.strip()
         else f'备用密钥 {index if settings.api_key.strip() else index + 1}',
         'masked_key': '••••••••', 'base_url': item.base_url if not item.problem() else '',
         'model': item.effective_model, 'balance_protocol': balance_protocol(item),
         'problem': item.problem(), 'health': last.get((identity(item), 'health')),
         'balance': last.get((identity(item), 'balance'))}
        for index, item in enumerate(configured(settings))
    ]}


def temporary(settings, provider, key, model):
    if provider == 'configured':
        return replace(settings, enabled=True, api_key=key, backup_api_keys=(),
                       model=model or settings.model)
    _, base, default_model = PROVIDERS[provider]
    return AISettings(enabled=True, api_key=key, base_url=base,
                      model=model or default_model, json_mode=True,
                      disable_thinking=provider == 'deepseek', vision=False)


def _http_json(url, key, *, max_bytes=256 * 1024):
    request = Request(url, headers={'Authorization': 'Bearer ' + key,
                                   'Accept': 'application/json'})
    try:
        with build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise TransportError('oversized')
        return json.loads(raw)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        raise TransportError('http', status) from None
    except TimeoutError:
        raise TransportError('timeout') from None
    except URLError:
        raise TransportError('network') from None
    except TransportError:
        raise
    except (ValueError, UnicodeError):
        raise TransportError('schema') from None


def reasoning_capability(row):
    unknown = {'state': 'unknown', 'levels': [], 'can_enable': None, 'can_disable': None, 'protocol': ''}
    effort, reasoning = row.get('effort'), row.get('reasoning')
    if isinstance(effort, dict) and isinstance(effort.get('supported_levels'), list):
        levels = effort['supported_levels']
        protocol, can_enable, can_disable = 'deepseek', True, True
    elif isinstance(reasoning, dict):
        if 'mandatory' in reasoning and type(reasoning['mandatory']) is not bool:
            return unknown
        levels = reasoning.get('supported_efforts', [])
        if 'supported_efforts' in reasoning and levels is None:
            levels = list(ai_reasoning.EFFORTS)
        protocol, can_enable, can_disable = 'openrouter', True, reasoning.get('mandatory') is not True
    elif row.get('reasoning') is False:
        return {**unknown, 'state': 'unsupported', 'can_enable': False, 'can_disable': False}
    elif isinstance(row.get('supported_parameters'), list):
        if 'reasoning' in row['supported_parameters']:
            return {**unknown, 'state': 'toggle', 'can_enable': True, 'can_disable': True, 'protocol': 'openrouter'}
        if 'reasoning_effort' in row['supported_parameters']:
            return unknown
        return {**unknown, 'state': 'unsupported', 'can_enable': False, 'can_disable': False}
    else:
        return unknown
    if not isinstance(levels, list) or any(level not in (*ai_reasoning.EFFORTS, 'none') for level in levels):
        return unknown
    levels = [level for level in ai_reasoning.EFFORTS if level in levels]
    return {'state': 'levels' if levels else 'toggle', 'levels': levels, 'can_enable': can_enable,
            'can_disable': can_disable, 'protocol': protocol}


def context_capability(row):
    """Retain only bounded provider-declared token limits, never infer by name."""
    result = {}
    for name, value in [('context_window', row.get('context_window', row.get('context_length'))),
                        ('max_output_tokens', row.get('max_output_tokens'))]:
        if type(value) is int and 256 <= value <= 16 * 1024 * 1024:
            result[name] = value
    return result


def models(settings, *, with_capabilities=False):
    data = _http_json(settings.base_url.rstrip('/') + '/models', settings.api_key,
                      max_bytes=2 * 1024 * 1024)
    try:
        rows = data['data']
        if not isinstance(rows, list) or len(rows) > 2000:
            raise ValueError('models')
        ids, details = [], {}
        for row in rows:
            model = row['id']
            if (not isinstance(model, str) or not model.strip() or len(model) > 200 or
                    any(ord(char) < 32 or ord(char) == 127 for char in model) or settings.api_key in model):
                raise ValueError('model')
            ids.append(model.strip())
            if model.strip() in details:
                # Conflicting duplicate entries cannot establish model capabilities.
                details[model.strip()] = reasoning_capability({})
            else:
                details[model.strip()] = {**reasoning_capability(row), **context_capability(row)}
        return {'models': sorted(set(ids)), 'capabilities': details} if with_capabilities else sorted(set(ids))
    except (ValueError, KeyError, TypeError):
        raise TransportError('schema') from None


def _amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or len(str(value)) > 64:
        raise ValueError('amount')
    amount = Decimal(str(value))
    if not amount.is_finite() or amount.copy_abs() > Decimal('1e12') or amount.as_tuple().exponent < -12:
        raise ValueError('amount')
    return format(amount, 'f')


def _balance(settings, protocol):
    base = settings.base_url.rstrip('/')
    if protocol == 'deepseek':
        if base.endswith('/v1'):
            base = base[:-3]
        data = _http_json(base + '/user/balance', settings.api_key)
        if type(data['is_available']) is not bool or not 1 <= len(data['balance_infos']) <= 4:
            raise ValueError('schema')
        balances = []
        for item in data['balance_infos']:
            if item['currency'] not in ('CNY', 'USD'):
                raise ValueError('currency')
            balances.append({'currency': item['currency'], 'total': _amount(item['total_balance']),
                             'granted': _amount(item['granted_balance']),
                             'topped_up': _amount(item['topped_up_balance'])})
        return {'scope': 'account', 'available': data['is_available'], 'balances': balances,
                'message': '账户余额；同一账户的多个 key 可能共享此余额，请勿相加。'}
    if protocol == 'openrouter':
        data = _http_json(base + '/key', settings.api_key)['data']
        remaining = data['limit_remaining']
        return {'scope': 'key_limit', 'balances': [
            {'currency': 'USD', 'total': None if remaining is None else _amount(remaining),
             'usage': _amount(data['usage'])}],
            'message': '密钥剩余额度与累计用量；未设额度上限不代表账户余额无限。'}
    if protocol == 'credit_grants':
        data = _http_json(base + '/dashboard/billing/credit_grants', settings.api_key)
        return {'scope': 'provider_reported', 'balances': [
            {'currency': 'USD', 'total': _amount(data['total_available'])}],
            'message': '兼容接口报告的可用金额（USD）；金额范围以该服务商定义为准。'}
    raise TransportError('unsupported')


def run(settings, operation, protocol='auto'):
    start = time.monotonic()
    result = {'operation': operation, 'checked_at': datetime.now(UTC).isoformat(timespec='seconds'),
              'status': 'ok', 'http_status': None}
    try:
        if operation == 'health':
            payload = {'model': settings.effective_model, 'stream': False, 'temperature': 0,
                       'max_tokens': 64, 'messages': [
                           {'role': 'user', 'content': 'Return only this JSON object: {"probe":"ok"}'}]}
            if settings.json_mode:
                payload['response_format'] = {'type': 'json_object'}
            payload.update(ai_reasoning.parameters(settings))
            if settings.reasoning_effort not in ('', 'default', 'none'):
                payload['max_tokens'] = settings.max_tokens
            request = Request(settings.base_url.rstrip('/') + '/chat/completions',
                              data=json.dumps(payload).encode(), method='POST',
                              headers={'Authorization': 'Bearer ' + settings.api_key,
                                       'Content-Type': 'application/json'})
            # Bypass production cooldown/failover: prove this exact key, with one request.
            content = _content(build_opener(NoRedirect()), request, 20)
            if json.loads(content) != {'probe': 'ok'}:
                raise TransportError('schema')
            result['message'] = '该密钥已完成指定模型的文字与 JSON 调用；图片能力尚未检测。'
        else:
            protocol = balance_protocol(settings) if protocol == 'auto' else protocol
            result['protocol'] = protocol
            result.update(_balance(settings, protocol))
    except TransportError as exc:
        status = exc.status
        kind = exc.kind
        if kind == 'http':
            kind = {401: 'invalid_key', 402: 'insufficient_balance', 403: 'forbidden',
                    404: 'not_found', 429: 'rate_limited'}.get(status,
                        'upstream_error' if status and status >= 500 else 'http_error')
        result.update(status=kind, http_status=status, message=MESSAGES.get(kind, MESSAGES['schema']))
    except (ValueError, KeyError, IndexError, TypeError, InvalidOperation, UnicodeError):
        result.update(status='schema', message=MESSAGES['schema'])
    result['elapsed_ms'] = round((time.monotonic() - start) * 1000)
    return result
