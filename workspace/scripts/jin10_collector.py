"""Continuously collect Jin10 flashes outside the trading monitor."""

import argparse
import getpass
import json
import os
import sqlite3
import stat
import tempfile
import time
from pathlib import Path

from news_event_shadow import Jin10QuotaError, Jin10Client, active_contracts, ingest_flashes

ROOT = Path(__file__).resolve().parents[2]
TOKEN_FILE = ROOT / ".credentials" / "jin10_mcp.token"
CATALOG = ROOT / "logs" / "news" / "jin10_exchange_info.json"
POLL_SECONDS = 30
REFRESH_SECONDS = 6 * 3600
MAX_CATALOG_AGE = 24 * 3600
BINANCE_PATH = "/fapi/v1/exchangeInfo"
BINANCE_TESTNET = "https://testnet.binancefuture.com"


def atomic_write(path, data):
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(data)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def configure(path):
    token = getpass.getpass("输入新金十 MCP Token（隐藏）: ").strip()
    if len(token) < 12 or any(char.isspace() for char in token):
        raise ValueError("Token 格式无效")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    info = path.parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077 or path.parent.is_symlink():
        raise ValueError("凭证目录必须由当前用户独占")
    atomic_write(path, token + "\n")


def read_token(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("凭证文件需要当前用户独占权限")
        return stream.read(4096).strip() or None


def read_catalog(path, now):
    try:
        if now - path.stat().st_mtime <= MAX_CATALOG_AGE:
            result = json.loads(path.read_text())
            if active_contracts(result):
                return result
    except (OSError, KeyError, ValueError, TypeError):
        pass
    return None


def refresh_catalog(session, path, now):
    import config
    from rest_backoff import GATE, fapi_public_get

    if path.exists() and now - path.stat().st_mtime < REFRESH_SECONDS and read_catalog(path, now):
        return True
    if GATE.banned_until > now:
        return False
    proxy = config.CONFIG.get("proxy")
    response = fapi_public_get(
        session, BINANCE_TESTNET + BINANCE_PATH, BINANCE_PATH, background=True,
        timeout=(3, 4), proxies={"https": proxy} if proxy else None,
    )
    if response.status_code == 200:
        result = response.json()
        if active_contracts(result):
            atomic_write(path, json.dumps(result))
            return True
    return False


def catalog_loop(path):
    import requests

    with requests.Session() as session:
        session.trust_env = False
        while True:
            refreshed = False
            try:
                refreshed = refresh_catalog(session, path, time.time())
            except (OSError, ValueError, KeyError, TypeError) as exc:
                print(f"testnet catalog refresh failed ({type(exc).__name__})", flush=True)
            time.sleep(3600 if refreshed else 300)


def serve(token_path):
    import requests
    import trade_db

    token = None
    client = None
    setup_state = None
    retry = POLL_SECONDS
    with requests.Session() as session:
        while True:
            try:
                current = read_token(token_path)
                if current != token:
                    token, client = current, None
                    setup_state = None
                    retry = POLL_SECONDS
                if not token:
                    if setup_state != "waiting_for_token":
                        print("waiting_for_token", flush=True)
                        setup_state = "waiting_for_token"
                    time.sleep(5)
                    continue
                client = client or Jin10Client(session, token)
                result = ingest_flashes(trade_db, None, client.list_flash())
                trade_db.record_news_source("金十数据", {
                    "checked_ms": time.time_ns() // 1_000_000, "status": "ok",
                    "item_count": result["received"], "latest_published_ms": result["latest_published_ms"],
                })
                if setup_state != "running" or result["added_news"] or result["events"]:
                    print(f"running received={result['received']} new={result['added_news']} events={len(result['events'])}",
                          flush=True)
                setup_state = "running"
                retry = POLL_SECONDS
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                client = None
                setup_state = "error"
                quota = isinstance(exc, Jin10QuotaError)
                retry = 1800 if quota else min(retry * 2, 600)
                error = "quota_exhausted" if quota else type(exc).__name__
                try:
                    trade_db.record_news_source("金十数据", {
                        "checked_ms": time.time_ns() // 1_000_000,
                        "status": "error",
                        "error": error,
                    })
                except sqlite3.Error:
                    print("金十来源状态写入失败", flush=True)
                print(f"金十采集失败 ({error})，{retry} 秒后重试", flush=True)
            time.sleep(retry)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--configure", action="store_true", help="在本机隐藏输入并安全保存新 Token")
    mode.add_argument("--watch", action="store_true", help="持续采集")
    parser.add_argument("--token-file", type=Path, default=TOKEN_FILE)
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    args = parser.parse_args()
    if args.configure:
        configure(args.token_file)
        print("金十 Token 已保存，采集服务将在下一轮加载。")
    else:
        from threading import Thread

        Thread(target=catalog_loop, args=(args.catalog,), name="testnet-catalog", daemon=True).start()
        serve(args.token_file)


if __name__ == "__main__":
    main()
