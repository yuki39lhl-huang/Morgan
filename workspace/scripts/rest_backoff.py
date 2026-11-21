#!/usr/bin/env python3
"""持仓 / 挂单 REST 的缓存和限频闸。

-1003、429、Retry-After：记下封禁结束时间，在那之前不再发 HTTP。
读成功的 positionRisk / openOrders / openAlgoOrders 短缓存，避免同一轮里重复打。
读失败按 1、2、4…秒指数退避，再乘 0.5～1.0 的抖动，封顶 60 秒。
"""
from __future__ import annotations

import logging
import os
import random
import sqlite3
import sys
import threading
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
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
PROBE_LEASE_SEC = 45.0
BACKGROUND_WEIGHT_FRACTION = 0.8
BACKGROUND_RESUME_DELAY_SEC = 5.0
_LOG = logging.getLogger(__name__)
_IGNORE_PARAMS = {"timestamp", "signature", "recvWindow"}
_BAN_UNTIL = re.compile(r"banned until (\d+)")


class SharedCooldown:
    """同一 Morgan 实例的进程共享冷却状态，重启后仍有效。"""

    def __init__(self, path: Path):
        self.path = path
        self._ready = False

    @contextmanager
    def transaction(self, *, write=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=1.0)) as db, db:
            if not self._ready:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS cooldown (id INTEGER PRIMARY KEY, "
                    "deadline REAL NOT NULL, message TEXT NOT NULL, "
                    "probe_until REAL NOT NULL, probe_owner TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS usage (id INTEGER PRIMARY KEY, "
                    "minute INTEGER NOT NULL, weight INTEGER NOT NULL, "
                    "weight_limit INTEGER NOT NULL, resume_at REAL NOT NULL)"
                )
                self._ready = True
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def read(db):
        return db.execute(
            "SELECT deadline, message, probe_until, probe_owner FROM cooldown WHERE id = 1"
        ).fetchone() or (0.0, "", 0.0, "")

    @staticmethod
    def write(db, deadline, message, probe_until=0.0, probe_owner=""):
        db.execute(
            "INSERT OR REPLACE INTO cooldown VALUES (1, ?, ?, ?, ?)",
            (deadline, message, probe_until, probe_owner),
        )

    def deadline(self):
        with self.transaction() as db:
            return self.read(db)[0]

    @staticmethod
    def read_usage(db):
        return db.execute(
            "SELECT minute, weight, weight_limit, resume_at FROM usage WHERE id = 1"
        ).fetchone() or (0, 0, 0, 0.0)

    def admit(self, method, now, reserve_probe, background):
        # 常态和封禁期间只读；到期后的抢占才获取写锁。
        with self.transaction() as db:
            deadline, message, probe_until, _ = self.read(db)
            if background and not deadline:
                minute, weight, limit, resume_at = self.read_usage(db)
                if now < resume_at:
                    return (resume_at, "REST recovering"), None
                if limit and minute == int(now // 60) and weight >= limit * BACKGROUND_WEIGHT_FRACTION:
                    return ((minute + 1) * 60, "REST background weight limit"), None
        if not deadline:
            return None, None
        if deadline > now or probe_until > now or method != "GET" or not reserve_probe:
            return (max(deadline, probe_until), message), None
        with self.transaction(write=True) as db:
            deadline, message, probe_until, _ = self.read(db)
            if not deadline:
                return None, None
            if max(deadline, probe_until) > now:
                return (max(deadline, probe_until), message), None
            owner = uuid.uuid4().hex
            self.write(db, deadline, message, now + PROBE_LEASE_SEC, owner)
            return None, owner

    def record(self, now, deadline, message, owner, success, used_weight, weight_limit):
        with self.transaction(write=True) as db:
            old_deadline, old_message, _, old_owner = self.read(db)
            recovered = False
            if deadline is not None:
                if deadline >= old_deadline:
                    self.write(db, deadline, message)
            elif owner and owner == old_owner:
                if success:
                    db.execute("DELETE FROM cooldown WHERE id = 1")
                    recovered = True
                else:
                    self.write(db, now + UNPARSED_BAN_SEC, old_message)

            if recovered or used_weight is not None or weight_limit is not None:
                minute, weight, limit, resume_at = self.read_usage(db)
                if used_weight is not None:
                    current_minute = int(now // 60)
                    weight = max(weight, used_weight) if minute == current_minute else used_weight
                    minute = current_minute
                if recovered:
                    resume_at = now + BACKGROUND_RESUME_DELAY_SEC
                db.execute(
                    "INSERT OR REPLACE INTO usage VALUES (1, ?, ?, ?, ?)",
                    (minute, weight, weight_limit or limit, resume_at),
                )


def _minute_weight_limit(path, body):
    if path != "/fapi/v1/exchangeInfo" or not isinstance(body, dict):
        return None
    for limit in body.get("rateLimits") or ():
        if (
            isinstance(limit, dict)
            and limit.get("rateLimitType") == "REQUEST_WEIGHT"
            and limit.get("interval") == "MINUTE"
            and limit.get("intervalNum") == 1
            and isinstance(limit.get("limit"), int)
            and limit["limit"] > 0
        ):
            return limit["limit"]
    return None


class RateGate:
    def __init__(
        self,
        clock: Callable[[], float] | None = None,
        uniform: Callable[[], float] | None = None,
        shared: SharedCooldown | None = None,
    ):
        self.clock = clock or time.time
        self.uniform = uniform or (lambda: random.uniform(0.5, 1.0))
        self.shared = shared
        self._probe = threading.local()
        self._banned_until = 0.0
        self.ban_msg = ""
        self.cache: dict[str, tuple[float, object]] = {}
        self.fail_streak: dict[str, int] = {}
        self.next_try: dict[str, float] = {}

    def reset(self) -> None:
        self.__init__(clock=self.clock, uniform=self.uniform, shared=self.shared)

    @property
    def banned_until(self):
        if self.shared is None:
            return self._banned_until
        try:
            return max(self._banned_until, self.shared.deadline())
        except (sqlite3.Error, OSError):
            return max(self._banned_until, self.clock() + 1.0)

    @banned_until.setter
    def banned_until(self, deadline):
        self._banned_until = deadline

    def cache_key(self, method: str, path: str, params: dict | None) -> str | None:
        if method.upper() != "GET" or path not in READ_PATHS:
            return None
        items = []
        for k, v in sorted((params or {}).items()):
            if k in _IGNORE_PARAMS:
                continue
            items.append(f"{k}={v}")
        return path + "?" + "&".join(items)

    def before(self, method: str, path: str, params: dict | None = None, *, reserve_probe=False, background=False):
        """返回 ('send', None) 或 ('stop', body)。stop 表示本轮不要打接口。"""
        now = float(self.clock())
        if now < self._banned_until:
            return "stop", self._ban_body(self._banned_until, self.ban_msg)
        if self.shared is not None:
            try:
                blocked, owner = self.shared.admit(method.upper(), now, reserve_probe, background)
            except (sqlite3.Error, OSError) as exc:
                _LOG.error("Binance REST shared cooldown unavailable: %s", type(exc).__name__)
                return "stop", self._ban_body(now + 1.0, "shared cooldown unavailable")
            if blocked:
                self.cache.clear()
                return "stop", self._ban_body(*blocked)
            if owner:
                self._probe.owner = owner
                return "send", None
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
        ban_at = ban_deadline(body, headers, now, status)
        weights = {
            name.lower(): int(value)
            for name, value in (headers or {}).items()
            if name.lower().startswith("x-mbx-used-weight") and str(value).isdigit()
        }
        _LOG.info(
            "Binance REST source=%s pid=%s method=%s path=%s status=%s used_weight=%s",
            Path(sys.argv[0]).name, os.getpid(), method, path, status, weights,
        )
        owner = getattr(self._probe, "owner", None)
        self._probe.owner = None
        deadline = max(ban_at, now + 1.0) if ban_at is not None else None
        used_weight = weights.get("x-mbx-used-weight-1m")
        weight_limit = _minute_weight_limit(path, body)
        if self.shared is not None and (deadline is not None or owner or used_weight is not None or weight_limit):
            try:
                self.shared.record(
                    now, deadline, _msg(body), owner,
                    200 <= status < 300 and _ok_body(body), used_weight, weight_limit,
                )
            except (sqlite3.Error, OSError) as exc:
                _LOG.error("Binance REST could not persist cooldown: %s", type(exc).__name__)
        if deadline is not None:
            if deadline >= self._banned_until:
                self._banned_until = deadline
                self.ban_msg = _msg(body) or "rate limited"
            self.cache.clear()
            return
        key = self.cache_key(method, path, params)
        if status == 200 and _ok_body(body) and key:
            self.cache[key] = (now + CACHE_TTL_SEC, body)
            self.fail_streak.pop(key, None)
            self.next_try.pop(key, None)
            return
        if key and not _ok_body(body):
            n = self.fail_streak.get(key, 0) + 1
            self.fail_streak[key] = n
            step = min(MAX_BACKOFF_SEC, float(2 ** (n - 1)))
            jitter = min(1.0, max(0.0, float(self.uniform())))
            self.next_try[key] = now + step * jitter

    def _ban_body(self, deadline=None, message=None) -> dict:
        deadline = self.banned_until if deadline is None else deadline
        message = self.ban_msg if message is None else message
        return {
            "code": -1003,
            "msg": message or "banned",
            "error": message or "banned",
            "rate_limited": True,
            "banned_until": deadline,
        }


def ban_deadline(body, headers, now: float, status: int = 0) -> float | None:
    """封禁结束的 unix 秒。没有限频信息就返回 None。

    HTTP 418/429 与业务码 -1003/429 同等对待。Retry-After 和正文里的
    banned until 优先；两者都解析不到时用保守等待，禁止立刻重试。
    """
    header_at = _retry_after(headers, now)
    msg_at = _banned_until_msg(body)
    code = body.get("code") if isinstance(body, dict) else None
    try:
        http_status = int(status or 0)
    except (TypeError, ValueError):
        http_status = 0
    status_limited = code in (-1003, 429, "-1003", "429") or http_status in (418, 429)
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


class PublicResponse:
    """闸门挡下或改写后的响应。成功时 public_get 仍返回原始响应。"""

    def __init__(self, status_code, body, headers=None, skipped=False):
        self.status_code = int(status_code or 0)
        self._body = body if isinstance(body, (dict, list)) else {}
        self.headers = headers or {}
        self.skipped = skipped
        if isinstance(body, dict):
            self.text = str(body.get("msg") or body.get("error") or "")
        else:
            self.text = str(body or "")

    def json(self):
        return self._body


def response_limited(resp) -> bool:
    if getattr(resp, "skipped", False):
        return True
    try:
        status = int(getattr(resp, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    if status in (418, 429):
        return True
    try:
        body = resp.json()
    except Exception:
        return False
    if not isinstance(body, dict):
        return False
    return bool(body.get("rate_limited")) or body.get("code") in (-1003, 429, "-1003", "429")


def public_get(send, path: str, *, background=False):
    """公开 GET。封禁窗口内不调用 send。418/-1003 写入全局限流闸。"""
    action, cached = GATE.before("GET", path, None, reserve_probe=True, background=background)
    if action == "stop":
        body = cached if isinstance(cached, dict) else {}
        return PublicResponse(418, body, skipped=True)
    try:
        resp = send()
    except Exception:
        GATE.after("GET", path, None, {"error": "transport failure"}, {}, 0)
        raise
    try:
        status = int(getattr(resp, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    headers = getattr(resp, "headers", {}) or {}
    try:
        body = resp.json()
    except Exception:
        raw = getattr(resp, "text", "") or ""
        body = {"error": "non_json", "status": status, "msg": str(raw)[:300]}
    record = body if isinstance(body, dict) else {"msg": str(body)[:300]}
    GATE.after("GET", path, None, record, headers, status)
    limited = status in (418, 429) or record.get("code") in (-1003, 429, "-1003", "429")
    if not limited:
        return resp
    if not isinstance(body, dict):
        body = {"code": -1003, "msg": str(body)[:300]}
    else:
        body = dict(body)
        if status in (418, 429):
            body.setdefault("code", -1003)
    body["rate_limited"] = True
    body.setdefault("error", body.get("msg") or "rate_limited")
    return PublicResponse(status or 418, body, headers=dict(headers))


def fapi_public_get(session, url, path, *, background=False, **kwargs):
    return public_get(lambda: session.get(url, **kwargs), path, background=background)


GATE = RateGate(
    shared=SharedCooldown(Path(__file__).resolve().parents[2] / "logs" / "binance_rest_cooldown.sqlite3"),
)
