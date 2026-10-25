"""Bounded asynchronous Chat Completions stream; never replay a started response."""
import asyncio
import json
import time

import httpx2 as httpx

from src.ai_reasoning import parameters
from src.ai_transport import TransportError, _key_identity, _cooldown_status, _key_lock, _key_failures, _KEY_COOLDOWN_SECONDS


async def chat_stream(settings, messages, *, max_tokens):
    payload = {"model": settings.effective_model, "messages": messages, "stream": True,
               "max_tokens": max_tokens, "temperature": 0.2,
               **parameters(settings)}
    if settings.json_mode:
        payload['response_format'] = {'type': 'json_object'}
    # read timeout is separate from the total deadline. Cancellation closes the socket.
    try:
        async with asyncio.timeout(min(settings.timeout, 120)):
            async with httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(30, connect=10)) as client:
                last_status = None
                for key in settings.api_keys:
                    identity = _key_identity(settings.base_url, key)
                    status = _cooldown_status(identity, time.monotonic())
                    if status is not None:
                        last_status = status
                        continue
                    async with client.stream("POST", settings.base_url.rstrip('/') + '/chat/completions',
                                             json=payload, headers={"Authorization": "Bearer " + key}) as response:
                        if response.status_code in {401, 402}:
                            last_status = response.status_code
                            with _key_lock:
                                _key_failures[identity] = (time.monotonic() + _KEY_COOLDOWN_SECONDS, last_status)
                            continue
                        if response.status_code != 200:
                            raise TransportError('http', response.status_code)
                        if 'text/event-stream' not in response.headers.get('content-type', ''):
                            raise TransportError('schema')
                        buffer, total, finished, done = b'', 0, False, False
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > 1024 * 1024:
                                raise TransportError('oversized')
                            buffer += chunk
                            while b'\n' in buffer:
                                line, buffer = buffer.split(b'\n', 1)
                                line = line.rstrip(b'\r')
                                if not line.startswith(b'data:'):
                                    continue
                                value = line[5:].strip()
                                if value == b'[DONE]':
                                    done = True
                                    break
                                if not value:
                                    continue
                                try:
                                    item = json.loads(value)
                                except (ValueError, UnicodeDecodeError):
                                    raise TransportError('schema') from None
                                if item.get('error'):
                                    raise TransportError('schema')
                                for choice in item.get('choices', []):
                                    if choice.get('index', 0) != 0:
                                        continue
                                    delta = choice.get('delta', {}).get('content')
                                    # Provider reasoning_content is deliberately never forwarded.
                                    if delta is not None:
                                        if not isinstance(delta, str) or (finished and delta):
                                            raise TransportError('schema')
                                        if delta:
                                            yield delta
                                    reason = choice.get('finish_reason')
                                    if reason is not None:
                                        if reason != 'stop':
                                            raise TransportError('incomplete')
                                        finished = True
                            if done:
                                break
                        if not finished or not done:
                            raise TransportError('incomplete')
                        return
                raise TransportError('keys_unavailable', last_status)
    except (TimeoutError, httpx.TimeoutException):
        raise TransportError('timeout') from None
    except httpx.HTTPError:
        raise TransportError('network') from None
    # ValueError is deliberately NOT caught here: consumer-side validation
    # errors raised around `yield` would otherwise be masked as schema faults.
    except (KeyError, TypeError, UnicodeError):
        raise TransportError('schema') from None
