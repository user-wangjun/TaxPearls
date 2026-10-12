"""Reasoning configuration and provider-specific Chat Completions parameters."""
EFFORTS = ('minimal', 'low', 'medium', 'high', 'xhigh', 'max')
MODES = ('default', 'none', 'enabled', *EFFORTS)


def mode(settings):
    return settings.reasoning_effort or ('none' if settings.disable_thinking else 'default')


def parameters(settings):
    effort = settings.reasoning_effort
    if not effort:
        return {'thinking': {'type': 'disabled'}} if settings.disable_thinking else {}
    if effort == 'default':
        return {}
    if settings.reasoning_protocol == 'openrouter':
        return {'reasoning': {'enabled': True} if effort == 'enabled' else {'effort': effort}}
    if settings.reasoning_protocol == 'deepseek':
        data = {'thinking': {'type': 'disabled' if effort == 'none' else 'enabled'}}
        if effort in EFFORTS:
            data['reasoning_effort'] = effort
        return data
    raise ValueError('思考配置缺少已确认的上游协议。')


def allowed(capability, effort):
    if effort == 'default':
        return True
    if effort == 'none':
        return capability.get('can_disable') is True
    if effort == 'enabled':
        return capability.get('can_enable') is True
    return effort in capability.get('levels', [])
