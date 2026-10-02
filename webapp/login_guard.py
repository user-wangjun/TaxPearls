"""Bounded, single-process login failure throttling by account and client IP."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from hashlib import sha256
from math import ceil
from threading import RLock
import time


WINDOW_SECONDS = 15 * 60
MAX_KEYS = 8192


@dataclass
class _Failure:
    count: int
    last: float
    next_at: float
    locked_until: float


class LoginGuard:
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self._lock = RLock()
        self._failures: dict[tuple[str, str], _Failure] = {}
        self._inflight: set[tuple[str, str]] = set()

    @staticmethod
    def fingerprint(value: str) -> str:
        return sha256(value.encode("utf-8")).hexdigest()

    def _keys(self, username: str, ip: str):
        return (("account", self.fingerprint(username.strip().lower())),
                ("ip", self.fingerprint(ip)))

    def _fresh(self, key, now):
        current = self._failures.get(key)
        if current and now - current.last >= WINDOW_SECONDS:
            del self._failures[key]
            return None
        return current

    def retry_after(self, username: str, ip: str) -> int:
        with self._lock:
            now = self.clock()
            wait = 0
            for key in self._keys(username, ip):
                current = self._fresh(key, now)
                if current:
                    wait = max(wait, current.next_at - now, current.locked_until - now)
            return max(0, ceil(wait))

    @contextmanager
    def reserve(self, username: str, ip: str):
        """Only one verification per account or IP may run at a time."""
        keys = self._keys(username, ip)
        with self._lock:
            wait = self.retry_after(username, ip)
            if not wait and any(key in self._inflight for key in keys):
                wait = 1
            reserved = not wait
            if reserved:
                self._inflight.update(keys)
        try:
            yield wait
        finally:
            if reserved:
                with self._lock:
                    self._inflight.difference_update(keys)

    def record_failure(self, username: str, ip: str) -> int:
        with self._lock:
            now = self.clock()
            keys = self._keys(username, ip)
            for key in keys:
                self._fresh(key, now)
            if len(self._failures) >= MAX_KEYS - 1:
                self._failures = {key: value for key, value in self._failures.items()
                                  if now - value.last < WINDOW_SECONDS}
            needed = sum(key not in self._failures for key in keys)
            while len(self._failures) + needed > MAX_KEYS:
                oldest = min((key for key in self._failures if key not in keys),
                             key=lambda key: self._failures[key].last)
                del self._failures[oldest]
            counts = []
            for key in keys:
                current = self._failures.get(key)
                count = (current.count if current else 0) + 1
                delay = min(2 ** (count - 5), 32) if count >= 5 else 0
                locked = now + WINDOW_SECONDS if count >= 10 else 0
                self._failures[key] = _Failure(count, now, now + delay, locked)
                counts.append(count)
            return max(counts)

    def record_success(self, username: str) -> None:
        with self._lock:
            self._failures.pop(("account", self.fingerprint(username.strip().lower())), None)


class RateLimiter:
    """按命名维度限制请求频率（内存滑动窗口）。

    本项目约束为单进程单副本（FR-G08），内存态成立；若改为多进程，
    须迁移到集中式存储，否则计数失效。
    字典容量有上限并按最近活跃淘汰，防止被大量伪造键撑爆内存。
    """

    def __init__(self, rules: dict[str, tuple[int, int]], max_keys: int = 8192):
        # rules: 维度名 -> (窗口内最大次数, 窗口秒数)
        self._rules = dict(rules)
        self._max_keys = max_keys
        self._lock = RLock()
        self._hits: dict[tuple[str, str], list[float]] = {}

    def allow(self, **identifiers: str) -> bool:
        """所有维度均未超限时返回 True，并记录本次请求；任一超限返回 False。"""
        now = time.monotonic()
        keys = [(name, self._key(value)) for name, value in identifiers.items()]
        with self._lock:
            for name, key in keys:
                limit, window = self._rules[name]
                hits = [t for t in self._hits.get((name, key), []) if now - t < window]
                if len(hits) >= limit:
                    self._hits[(name, key)] = hits
                    return False
            for name, key in keys:
                limit, window = self._rules[name]
                hits = [t for t in self._hits.get((name, key), []) if now - t < window]
                hits.append(now)
                self._hits[(name, key)] = hits
            if len(self._hits) >= self._max_keys:
                for stale in [k for k, v in self._hits.items()
                              if not v or now - v[-1] >= max(w for _, w in self._rules.values())]:
                    del self._hits[stale]
            return True

    @staticmethod
    def _key(value: str) -> str:
        return sha256(value.strip().lower().encode("utf-8")).hexdigest()
