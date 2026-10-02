"""算术人机验证码（内存态，单进程）。

MVP 自建方案：后端签发算术题，答案只存 SHA-256 摘要，10 分钟有效、一次性
（无论答对答错，校验后即销毁，不给爆破机会）。商用阶段建议升级为
Cloudflare Turnstile（免费、用户无感），接口形态不变。
"""
from __future__ import annotations

from hashlib import sha256
from secrets import randbelow, token_hex
from threading import RLock
import time

MAX_KEYS = 8192
LIFETIME_SECONDS = 10 * 60


class _Puzzle:
    __slots__ = ("answer_hash", "expires_at")

    def __init__(self, answer_hash: str, expires_at: float) -> None:
        self.answer_hash = answer_hash
        self.expires_at = expires_at


_store: dict[str, _Puzzle] = {}
_lock = RLock()


def _digest(answer: str) -> str:
    return sha256(answer.strip().encode("utf-8")).hexdigest()


def issue() -> dict[str, str]:
    """签发一道算术题，返回 ``{"captcha_id": ..., "question": ...}``。"""
    a, b = randbelow(9) + 2, randbelow(9) + 2
    if randbelow(2) and a > b:
        question, answer = f"{a} − {b} = ?", a - b
    else:
        question, answer = f"{a} + {b} = ?", a + b
    captcha_id = token_hex(8)
    now = time.monotonic()
    with _lock:
        if len(_store) >= MAX_KEYS:
            for stale in [k for k, v in _store.items() if now > v.expires_at]:
                del _store[stale]
        _store[captcha_id] = _Puzzle(_digest(str(answer)), now + LIFETIME_SECONDS)
    return {"captcha_id": captcha_id, "question": question}


def verify(captcha_id: str, answer: str) -> bool:
    """校验并销毁。id 未知 / 已过期 / 答案错误均返回 False。"""
    if not captcha_id or not (answer or "").strip():
        return False
    with _lock:
        puzzle = _store.pop(captcha_id, None)  # 一次性：无论对错都销毁
    if not puzzle or time.monotonic() > puzzle.expires_at:
        return False
    return puzzle.answer_hash == _digest(answer)
