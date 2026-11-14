"""Shared bounded Chat Completions transport; errors never include payloads."""
import json
from contextlib import nullcontext
from contextvars import ContextVar
import hashlib
import logging
from threading import Lock
import time
from src.ai_reasoning import parameters as reasoning_parameters
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


_KEY_COOLDOWN_SECONDS = 300
_key_failures = {}
_key_lock = Lock()
_logger = logging.getLogger(__name__)
REQUEST_GUARD = ContextVar('material_ai_guard', default=None)
USAGE_OBSERVER = ContextVar('ai_usage_observer', default=None)


def _key_identity(base_url, key):
    return hashlib.sha256((base_url.rstrip("/") + "\0" + key).encode()).digest()


def _cooldown_status(identity, now):
    with _key_lock:
        for expired in [item for item, (until, _) in _key_failures.items() if until <= now]:
            del _key_failures[expired]
        return _key_failures.get(identity, (0, None))[1]


class TransportError(ValueError):
    def __init__(self, kind, status=None, *, attempts=0, finish_reason=None):
        super().__init__(kind)
        self.kind = kind
        self.status = status
        self.attempts = attempts
        self.finish_reason = finish_reason


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _context_rejection(exc):
    """Classify only explicit capacity rejections; never retain provider bodies."""
    if exc.code not in {400, 413, 422}:
        return False
    try:
        body = json.loads(exc.read(16384))
        error = body.get('error', {}) if isinstance(body, dict) else {}
        if not isinstance(error, dict):
            return False
        code = str(error.get('code', '')).lower()
        message = str(error.get('message', '')).lower()
        return code in {'context_length_exceeded', 'context_window_exceeded', 'max_context_length_exceeded'} or any(
            marker in message for marker in ('maximum context length', 'context length exceeded',
                                             'context window exceeded', '上下文长度超出', '上下文过长'))
    except (ValueError, OSError):
        return False


def chat_payload(settings, messages, *, temperature=0, max_tokens=None):
    payload = {"model": settings.effective_model, "messages": messages, "stream": False,
               "max_tokens": max_tokens or settings.max_tokens, "temperature": temperature}
    if settings.json_mode:
        payload["response_format"] = {"type": "json_object"}
    payload.update(reasoning_parameters(settings))
    return json.dumps(payload, ensure_ascii=False).encode()


def chat_content(settings, messages, timeout, *, temperature=0, max_tokens=None):
    data = chat_payload(settings, messages, temperature=temperature, max_tokens=max_tokens)
    deadline = time.monotonic() + timeout
    opener = build_opener(NoRedirect())
    attempts, last_status = 0, None
    keys = settings.api_keys
    for index, key in enumerate(keys, 1):
        identity = _key_identity(settings.base_url, key)
        now = time.monotonic()
        status = _cooldown_status(identity, now)
        if status is not None:
            last_status = status
            continue
        remaining = deadline - now
        if remaining <= 0:
            raise TransportError("timeout", attempts=attempts)
        request = Request(settings.base_url.rstrip("/") + "/chat/completions", data=data,
                          headers={"Content-Type":"application/json", "Authorization":"Bearer " + key},
                          method="POST")
        attempts += 1
        try:
            guard = REQUEST_GUARD.get()
            with guard(remaining) if guard else nullcontext():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransportError("timeout", attempts=attempts)
                return _content(opener, request, remaining)
        except TimeoutError:
            raise TransportError("timeout", attempts=attempts) from None
        except TransportError as exc:
            exc.attempts = attempts
            # Only explicit key/balance rejection permits another paid request.
            # Do not replay timeouts, rate limits, server errors or invalid output.
            if exc.kind != "http" or exc.status not in {401, 402}:
                raise
            last_status = exc.status
            if len(keys) > 1:
                with _key_lock:
                    _key_failures[identity] = (time.monotonic() + _KEY_COOLDOWN_SECONDS, exc.status)
                _logger.warning("AI key slot %d rejected (HTTP %d); skipped for 300 seconds", index, exc.status)
    if len(keys) == 1 and attempts:
        raise TransportError("http", last_status, attempts=attempts)
    raise TransportError("keys_unavailable", last_status, attempts=attempts)


def _content(opener, request, timeout):
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise TransportError("oversized")
        result = json.loads(raw)
        observer = USAGE_OBSERVER.get()
        if observer:
            usage = result.get("usage", {})
            usage = usage if isinstance(usage, dict) else {}
            safe = {k: v for k, v in usage.items() if k in {"prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
                    and type(v) is int and 0 <= v <= 100000000}
            observer({"model": str(result.get("model", ""))[:200], "usage": safe,
                      "elapsed_seconds": round(time.monotonic() - started, 3)})
        choice = result["choices"][0]
        if choice.get("finish_reason") != "stop":
            reason = choice.get('finish_reason')
            raise TransportError("incomplete", finish_reason=reason if reason in {'length', 'content_filter', 'tool_calls'} else None)
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise TransportError("empty")
        return content.strip()
    except HTTPError as exc:
        status = exc.code
        try:
            kind = 'context_length' if _context_rejection(exc) else 'http'
        finally:
            exc.close()
        raise TransportError(kind, status) from None
    except TimeoutError:
        raise TransportError("timeout") from None
    except URLError:
        raise TransportError("network") from None
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise TransportError("schema") from None
