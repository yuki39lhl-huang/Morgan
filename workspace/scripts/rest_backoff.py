#!/usr/bin/env python3
"""持仓 / 挂单 REST 的缓存和限频闸。

-1003、429、Retry-After：记下封禁结束时间，在那之前不再发 HTTP。
读成功的 positionRisk / openOrders / openAlgoOrders 短缓存，避免同一轮里重复打。
读失败按 1、2、4…秒指数退避，再乘 0.5～1.0 的抖动，封顶 60 秒。
"""
from __future__ import annotations

import random
import re
import time
from typing import Callable

READ_PATHS = (
    "/fapi/v2/positionRisk",
    "/fapi/v1/openOrders",
    "/fapi/v1/openAlgoOrders",
)
CACHE_TTL_SEC = 15.0
MAX_BACKOFF_SEC = 60.0
UNPARSED_BAN_SEC = 60.0
_IGNORE_PARAMS = {"timestamp", "signature", "recvWindow"}
_BAN_UNTIL = re.compile(r"banned until (\d+)")


class RateGate:
    def __init__(self, clock: Callable[[], float] | None = None, uniform: Callable[[], float] | None = None):
        self.clock = clock or time.time
        self.uniform = uniform or (lambda: random.uniform(0.5, 1.0))
        self.banned_until = 0.0
        self.ban_msg = ""
        self._announced = False
        self.cache: dict[str, tuple[float, object]] = {}
        self.fail_streak: dict[str, int] = {}
        self.next_try: dict[str, float] = {}

    def reset(self) -> None:
        self.__init__(clock=self.clock, uniform=self.uniform)

    def cache_key(self, method: str, path: str, params: dict | None) -> str | None:
        if method.upper() != "GET" or path not in READ_PATHS:
            return None
        items = []
        for k, v in sorted((params or {}).items()):
            if k in _IGNORE_PARAMS:
                continue
            items.append(f"{k}={v}")
        return path + "?" + "&".join(items)

    def before(self, method: str, path: str, params: dict | None = None):
        """返回 ('send', None) 或 ('stop', body)。stop 表示本轮不要打接口。"""
        now = float(self.clock())
        if now < self.banned_until:
            return "stop", self._ban_body()
        key = self.cache_key(method, path, params)
        if key and key in self.cache and now < self.cache[key][0]:
            return "stop", self.cache[key][1]
        if key and now < self.next_try.get(key, 0.0):
            return "stop", {
                "code": -1003,
                "msg": self.ban_msg or "backoff",
                "error": "backoff",
                "rate_limited": True,
                "retry_at": self.next_try[key],
            }
        return "send", None

    def after(self, method: str, path: str, params: dict | None, body, headers, status: int) -> None:
        now = float(self.clock())
        ban_at = ban_deadline(body, headers, now)
        if ban_at is not None and ban_at > now:
            self.banned_until = max(self.banned_until, ban_at)
            self.ban_msg = _msg(body) or "rate limited"
            self._announced = True
            return
        key = self.cache_key(method, path, params)
        if status == 200 and _ok_body(body) and key:
            self.cache[key] = (now + CACHE_TTL_SEC, body)
            self.fail_streak.pop(key, None)
            self.next_try.pop(key, None)
            self._announced = False
            return
        if key and not _ok_body(body):
            n = self.fail_streak.get(key, 0) + 1
            self.fail_streak[key] = n
            step = min(MAX_BACKOFF_SEC, float(2 ** (n - 1)))
            jitter = min(1.0, max(0.0, float(self.uniform())))
            self.next_try[key] = now + step * jitter

    def _ban_body(self) -> dict:
        return {
            "code": -1003,
            "msg": self.ban_msg or "banned",
            "error": self.ban_msg or "banned",
            "rate_limited": True,
            "banned_until": self.banned_until,
        }


def ban_deadline(body, headers, now: float) -> float | None:
    """封禁结束的 unix 秒。没有限频信息就返回 None。"""
    header_at = _retry_after(headers, now)
    msg_at = _banned_until_msg(body)
    code = body.get("code") if isinstance(body, dict) else None
    status_limited = code in (-1003, 429, "-1003", "429")
    if header_at is None and msg_at is None and not status_limited:
        return None
    if header_at is None and msg_at is None:
        return now + UNPARSED_BAN_SEC
    return max(x for x in (header_at, msg_at) if x is not None)


def _retry_after(headers, now: float) -> float | None:
    if not headers:
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return now + float(raw)
    except (TypeError, ValueError):
        return None


def _banned_until_msg(body) -> float | None:
    msg = _msg(body)
    if not msg:
        return None
    matched = _BAN_UNTIL.search(msg)
    if not matched:
        return None
    ts = int(matched.group(1))
    if ts > 10_000_000_000:
        return ts / 1000.0
    return float(ts)


def _msg(body) -> str:
    if isinstance(body, dict):
        return str(body.get("msg") or body.get("error") or "")
    return ""


def _ok_body(body) -> bool:
    if isinstance(body, dict) and ("code" in body or "error" in body):
        return False
    return isinstance(body, (list, dict))


GATE = RateGate()
