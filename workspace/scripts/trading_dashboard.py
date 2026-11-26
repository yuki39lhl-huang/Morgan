"""Serve Morgan snapshots over an SSH-forwarded loopback port."""

import argparse
import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import numpy as np

from rsi_fade import wilder_rsi
from news_replay import observation_reason, observation_status, replay

TZ = ZoneInfo("Asia/Shanghai")
HOUR_MS = 3_600_000
SHADOW_WAIT_MS = 3 * 60_000

ASSETS = Path(__file__).resolve().with_name("trading_dashboard")
ROUTES = {
    "/": ("index.html", "text/html"),
    "/dashboard.css": ("dashboard.css", "text/css"),
    "/dashboard.js": ("dashboard.js", "text/javascript"),
    "/terminal.js": ("terminal.js", "text/javascript"),
    "/shen-wanshuang.webp": ("shen-wanshuang.webp", "image/webp"),
    "/shen-wanshuang-still.webp": ("shen-wanshuang-still.webp", "image/webp"),
}


def number(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def cached_candles(directory, symbol, limit=120):
    config = json.loads((directory / "config.json").read_text())
    if symbol not in config["symbols"]:
        return None
    path = directory.parents[1] / "logs" / "binance_closed_1h_klines.json"
    try:
        cache = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if cache.get("market") != config.get("binance_futures"):
        return []
    now = int(datetime.now(TZ).timestamp() * 1000)
    candles = {}
    for row in cache.get("candles", {}).get(symbol, []):
        if not isinstance(row, list) or len(row) < 7:
            continue
        stamp, close = number(row[0]), number(row[6])
        values = [number(value) for value in row[1:6]]
        if stamp is None or close is None or stamp % 3600000 or close != stamp + 3599999 or close > now:
            continue
        if any(value is None for value in values):
            continue
        opened, high, low, last, volume = values
        if 0 < low <= min(opened, last) <= max(opened, last) <= high and volume >= 0:
            candles[int(stamp)] = [int(stamp), *values]
    ordered = [candles[stamp] for stamp in sorted(candles)]
    return ordered if limit is None else ordered[-limit:]


def cached_market_chart(directory, symbol):
    rows = cached_candles(directory, symbol, limit=None)
    if rows is None:
        return None
    closes = np.array([bar[4] for bar in rows], dtype=float)
    times = np.array([bar[0] for bar in rows], dtype=np.int64)
    rsi = [number(value) for value in wilder_rsi(closes, times)]
    return {"symbol": symbol, "candles": rows[-120:], "rsi": rsi[-120:]}


def read_news_pipeline(db):
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE name IN ('benzinga_transport','news_testnet_orders')"
    )}
    transport = {}
    acceptance = {"status": "waiting"}
    if "benzinga_transport" in tables:
        transport = {row["channel"]: dict(row) for row in db.execute("SELECT * FROM benzinga_transport")}
    if "news_testnet_orders" in tables:
        fields = {row[1] for row in db.execute("PRAGMA table_info(news_testnet_orders)")}
        news_filter = "AND n.strategy='news_trend'" if "strategy" in fields else ""
        row = db.execute(
            f"""SELECT n.symbol,n.direction,n.status,n.opened_ms,
                      EXISTS(SELECT 1 FROM trades t WHERE t.trade_id=n.client_id AND t.regime='news_trend') AS opened_booked,
                      EXISTS(SELECT 1 FROM trade_closes c WHERE c.trade_id=n.client_id AND c.pnl_usdt IS NOT NULL) AS close_booked
               FROM news_testnet_orders n
               WHERE n.order_id IS NOT NULL AND n.opened_ms IS NOT NULL {news_filter}
               ORDER BY n.opened_ms LIMIT 1"""
        ).fetchone()
        if row:
            status = {"closed": "reconciling", "closing": "closing"}.get(row["status"], "filled")
            if row["status"] == "closed" and row["opened_booked"] and row["close_booked"]:
                status = "verified"
            acceptance = {"status": status, "symbol": row["symbol"], "opened_ms": row["opened_ms"]}
        elif db.execute(f"SELECT 1 FROM news_testnet_orders n WHERE status IN ('queued','submitting') {news_filter} LIMIT 1").fetchone():
            acceptance = {"status": "pending"}
    return transport, acceptance


def read_news_entries(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(news_entry_decisions)")}
    error_column = "d.model_error_json" if "model_error_json" in columns else "NULL"
    has_live_orders = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='news_testnet_orders'"
    ).fetchone()
    testnet_columns = ("t.order_id, t.quantity, t.entry_price" if has_live_orders else
                       "NULL AS order_id, NULL AS quantity, NULL AS entry_price")
    testnet_join = "LEFT JOIN news_testnet_orders t ON t.event_key=d.event_key AND t.stage=d.stage" if has_live_orders else ""
    rows = db.execute(
        f"""SELECT d.symbol,d.stage,d.started_ms,d.decision_ms,d.action,d.status,d.reason,d.evidence_json,d.gate_json,
                  {error_column} AS model_error_json, p.result_json AS paper_json, {testnet_columns}
           FROM news_entry_decisions d LEFT JOIN news_entry_papers p ON p.event_key=d.event_key AND p.stage=d.stage
           {testnet_join} ORDER BY d.started_ms DESC LIMIT 16"""
    )
    entries = []
    for row in rows:
        item = dict(row)
        for key, target, default in (("evidence_json", "evidence", "[]"), ("gate_json", "gate", "null"),
                                     ("paper_json", "paper", "null"), ("model_error_json", "model_error", "null")):
            item[target] = json.loads(item.pop(key) or default)
        entries.append(item)
    return entries


def read_unassessed_news(db, now):
    now_ms = int(now.timestamp() * 1000)
    cutoff = now_ms - 24 * HOUR_MS
    recent = db.execute(
        """WITH ranked AS (
               SELECT o.*, ROW_NUMBER() OVER (
                   PARTITION BY event_key,stage ORDER BY observed_ms DESC
               ) AS ordinal
               FROM news_event_observations o WHERE observed_ms>=?
           )
           SELECT r.*, (SELECT url FROM news WHERE source=r.source AND title=r.title LIMIT 1) AS url
           FROM ranked r WHERE ordinal=1 AND NOT EXISTS (
               SELECT 1 FROM news_entry_decisions d WHERE d.event_key=r.event_key AND d.stage=r.stage
           )
           ORDER BY observed_ms DESC LIMIT 16""",
        (cutoff,),
    )
    entries = []
    for row in recent:
        status = observation_status(row["topic"], row["published_ms"], row["observed_ms"], now_ms)
        conclusion = observation_reason(status, row["published_ms"], row["observed_ms"])
        entries.append({"symbol": row["symbol"], "stage": row["stage"], "started_ms": row["observed_ms"],
                        "action": None, "status": status, "paper": None, "gate": None,
                        "observation_reason": conclusion,
                        "evidence": [{key: row[key] for key in ("source", "title", "url", "published_ms", "observed_ms")}]})
    return entries


def read_rsi_opens(db, now, catalog, positions):
    links_available = "rsi_trade_signal_links" in catalog
    join = "LEFT JOIN rsi_trade_signal_links l USING (trade_id)" if links_available else ""
    bar_column = "l.bar_open_ms" if links_available else "NULL"
    rows = db.execute(
        f"""SELECT t.trade_id,t.ts,t.symbol,t.direction,{bar_column} AS bar_open_ms
            FROM trades t {join} WHERE t.regime='rsi' ORDER BY t.ts DESC LIMIT 24"""
    )
    position_bars = {p.get("trade_id"): p.get("entry_bar_open") for p in positions}
    results = []
    now_ms = int(now.timestamp() * 1000)
    for row in rows:
        opened = datetime.fromisoformat(row["ts"])
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=TZ)
        bar_ms = row["bar_open_ms"] or position_bars.get(row["trade_id"])
        exact = type(bar_ms) is int and bar_ms > 0
        if not exact:
            bar_ms = (int(opened.timestamp() * 1000) // HOUR_MS - 1) * HOUR_MS
        signal_ms = bar_ms + HOUR_MS
        observation = None
        if "rsi_entry_shadow" in catalog:
            observation = db.execute(
                "SELECT inputs_json,result_json FROM rsi_entry_shadow WHERE symbol=? AND bar_open_ms=?",
                (row["symbol"], bar_ms),
            ).fetchone()
        status = "pending" if now_ms <= signal_ms + SHADOW_WAIT_MS else "missing"
        analysis = {}
        if observation:
            inputs = json.loads(observation["inputs_json"])
            if inputs.get("market", {}).get("side") == row["direction"]:
                analysis = json.loads(observation["result_json"] or "{}")
                market_status = (analysis.get("market_ai") or {}).get("status")
                news_status = (analysis.get("news_ai") or {}).get("status")
                if analysis:
                    if market_status != "ok" or news_status not in {"ok", "no_news"}:
                        status = "model_error"
                    else:
                        status = "no_news" if news_status == "no_news" else "complete"
            else:
                status = "missing"
        results.append({"symbol": row["symbol"], "side": row["direction"],
                        "opened_ms": int(opened.timestamp() * 1000), "signal_ms": signal_ms,
                        "exact": exact, "status": status,
                        "market": analysis.get("market_ai"), "with_news": analysis.get("news_ai")})
    return results


def read_news_by_source(db):
    feeds = {}
    rows = db.execute(
        """WITH ranked AS (
               SELECT source,title,url,published,published_ms,observed_ms,id,
                      row_number() OVER (PARTITION BY source ORDER BY observed_ms DESC,id DESC) AS recency
               FROM news WHERE observed_ms IS NOT NULL AND source <> ''
           )
           SELECT source,title,url,published,published_ms,observed_ms FROM ranked
           WHERE recency <= 15 ORDER BY source,recency"""
    )
    for row in rows:
        feeds.setdefault(row["source"], []).append(dict(row))
    return feeds


def read_closed_positions(db):
    base = """SELECT symbol,direction,close_ts,close_reason,pnl_usdt,regime
              FROM v_trade_attribution WHERE close_ts IS NOT NULL"""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='news_testnet_orders'"
    ).fetchone():
        return [dict(row) for row in db.execute(base + " ORDER BY close_ts DESC LIMIT 12")]
    return [dict(row) for row in db.execute(
        base + """ AND (regime IS NULL OR regime NOT IN ('news_trend','market_scan'))
           UNION ALL
           SELECT t.symbol,t.direction,MAX(c.ts),MAX(c.reason),SUM(c.pnl_usdt),t.regime
           FROM trades t JOIN trade_closes c USING (trade_id)
           JOIN news_testnet_orders n ON n.client_id=t.trade_id AND n.status='closed'
           WHERE t.regime IN ('news_trend','market_scan') AND c.pnl_usdt IS NOT NULL
           GROUP BY t.trade_id
           ORDER BY close_ts DESC LIMIT 12"""
    )]


def read_research(db, now, positions):
    cutoff = (now.date() - timedelta(days=29)).isoformat()
    ledger = [dict(row) for row in db.execute(
        """WITH closed_fills AS (
               SELECT trade_id,close_ts,pnl_usdt FROM v_trade_attribution
               WHERE (regime IS NULL OR regime NOT IN ('news_trend','market_scan')) AND close_ts>=?
               UNION ALL
               SELECT c.trade_id,c.ts,c.pnl_usdt FROM trade_closes c JOIN trades t USING (trade_id)
               WHERE t.regime IN ('news_trend','market_scan') AND c.ts>=?
           )
           SELECT substr(close_ts,1,10) AS day, SUM(pnl_usdt) AS pnl, COUNT(DISTINCT trade_id) AS trades
           FROM closed_fills WHERE pnl_usdt IS NOT NULL GROUP BY day ORDER BY day""", (cutoff, cutoff)
    )]
    catalog = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    news, news_by_source = [], {}
    if "news" in catalog:
        news = [dict(row) for row in db.execute(
            """SELECT source, title, url, published, published_ms, observed_ms FROM news
               WHERE observed_ms IS NOT NULL ORDER BY observed_ms DESC, id DESC LIMIT 40"""
        )]
        news_by_source = read_news_by_source(db)
    shadow = []
    if "rsi_entry_shadow" in catalog:
        for row in db.execute(
            """SELECT symbol, signal_ms, inputs_json, result_json, next_bar_json FROM rsi_entry_shadow
               ORDER BY signal_ms DESC, symbol LIMIT 24"""
        ):
            inputs = json.loads(row["inputs_json"] or "{}")
            result = json.loads(row["result_json"] or "{}")
            market = inputs.get("market", {})
            shadow.append({
                "symbol": row["symbol"], "signal_ms": row["signal_ms"], "rsi": market.get("rsi"),
                "side": market.get("side"), "signal_price": market.get("close"),
                "market": result.get("market_ai"), "with_news": result.get("news_ai"),
                "news_count": len(inputs.get("news") or []),
                "next": json.loads(row["next_bar_json"] or "null"),
            })
    events = []
    if "news_event_observations" in catalog:
        events = [dict(row) for row in db.execute(
            """SELECT event_key, symbol, topic, stage, source, title, published_ms, observed_ms
               FROM news_event_observations ORDER BY observed_ms DESC LIMIT 16"""
        )]
    if events and "news_event_targets" in catalog:
        for event in events:
            target = db.execute(
                "SELECT status,result_json FROM news_event_targets WHERE event_key=?",
                (event["event_key"],),
            ).fetchone()
            if target:
                event["quote_status"] = target["status"]
                event["book"] = json.loads(target["result_json"] or "null")
    digest = None
    if "news_digests" in catalog:
        entry = db.execute(
            """SELECT generated_ms,model,result_json FROM news_digests
               ORDER BY generated_ms DESC LIMIT 1"""
        ).fetchone()
        if entry:
            digest = {"generated_ms": entry["generated_ms"], "model": entry["model"],
                      **json.loads(entry["result_json"])}
    entries = read_news_entries(db) if {"news_entry_decisions", "news_entry_papers"} <= catalog else []
    if {"news", "news_event_observations", "news_entry_decisions"} <= catalog:
        entries.extend(read_unassessed_news(db, now))
        entries.sort(key=lambda item: item["started_ms"], reverse=True)
        entries = entries[:16]
    return ledger, news, news_by_source, shadow, events, digest, entries, read_rsi_opens(db, now, catalog, positions)


def projected_snapshot(directory):
    state = json.loads((directory / "crypto_state.json").read_text())
    positions = json.loads((directory / "crypto_positions.json").read_text())
    config = json.loads((directory / "config.json").read_text())
    now = datetime.now(TZ)
    updated = datetime.fromisoformat(state["last_update"])
    updated = updated.replace(tzinfo=TZ) if updated.tzinfo is None else updated
    age = round((now - updated).total_seconds())
    counts_match = state["positions"] == len(positions)
    prices = state["prices"]
    open_rows = []
    closed = []
    ledger, news, shadow, events = [], [], [], []
    news_by_source = {}
    digest = None
    news_entries, rsi_opens = [], []
    model_account = None
    benzinga, news_acceptance = {}, None
    scan = None
    db_ready = True
    entries = {}
    try:
        uri = (directory / "trade_data.db").resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.4)) as db:
            db.row_factory = sqlite3.Row
            for pos in positions:
                row = db.execute(
                    "SELECT symbol, direction, entry_price FROM trades WHERE trade_id = ?",
                    (pos.get("trade_id"),),
                ).fetchone()
                if row and row["symbol"] == pos["symbol"] and row["direction"] == pos["type"]:
                    entries[pos.get("trade_id")] = number(row["entry_price"])
            closed = read_closed_positions(db)
            ledger, news, news_by_source, shadow, events, digest, news_entries, rsi_opens = read_research(db, now, positions)
            benzinga, news_acceptance = read_news_pipeline(db)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='market_scan_health' AND type='table'").fetchone():
                status = db.execute("SELECT checked_ms,status,universe,subscriptions FROM market_scan_health WHERE id=1").fetchone()
                scan = {"health": dict(status) if status else None, "signals": [dict(row) for row in db.execute(
                    """SELECT symbol,detected_ms,setup,action,status,reason,observed_price,markouts_json
                       FROM market_scan_signals ORDER BY detected_ms DESC LIMIT 12"""
                )]}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='news_model_account' AND type='table'").fetchone():
                row = db.execute("SELECT checked_ms,status,http_status FROM news_model_account WHERE id=1").fetchone()
                if row:
                    model_account = dict(row)
                    if now.timestamp() * 1000 - row["checked_ms"] > 10 * 60_000:
                        model_account["status"] = "stale"
    except (sqlite3.Error, ValueError):
        db_ready = False

    for pos in positions:
        qty = number(pos.get("amount")) or number(pos.get("qty"))
        entry = entries.get(pos.get("trade_id")) or number(pos.get("entry_price"))
        last = number(prices.get(pos["symbol"]))
        side = pos["type"]
        pnl = (last - entry) * abs(qty) * (1 if side == "LONG" else -1) if all(
            value is not None and value > 0 for value in (last, entry, abs(qty or 0))
        ) and side in ("LONG", "SHORT") else None
        open_rows.append({
            "symbol": pos["symbol"], "side": side, "quantity": abs(qty) if qty is not None else None,
            "entry_price": entry, "entry_source": "成交归因" if entries.get(pos.get("trade_id")) else "持仓快照",
            "last_price": last, "estimated_pnl": number(pnl), "entry_time": pos.get("entry_time"), "regime": pos.get("regime"),
        })

    pnl_values = [row["estimated_pnl"] for row in open_rows]
    return {
        "market": "测试网" if config.get("binance_testnet") is True else "主网",
        "updated": updated.isoformat(timespec="seconds"),
        "fresh": -5 <= age <= 60 and counts_match,
        "age_seconds": max(0, age), "counts_match": counts_match, "ledger_ready": db_ready,
        "capacity": config.get("max_positions"),
        "positions": open_rows, "closed": closed,
        "ledger": ledger, "news": news, "news_by_source": news_by_source, "shadow": shadow, "events": events,
        "digest": digest, "quotes": {symbol: number(prices.get(symbol)) for symbol in config.get("symbols", [])},
        "benzinga": benzinga, "news_acceptance": news_acceptance, "market_scan": scan,
        "news_entries": news_entries, "news_model_account": model_account, "rsi_opens": rsi_opens,
        "estimated_pnl": sum(pnl_values) if all(p is not None for p in pnl_values) else None,
        "daily_pnl": number(state.get("daily_pnl")) if state.get("daily_pnl_date") == now.date().isoformat() else None,
    }


def read_today_news_audit(directory):
    now = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    uri = (directory / "trade_data.db").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=0.4)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        result = replay(db, int(start.timestamp() * 1000), int(now.timestamp() * 1000), include_current_status=True)
    result.pop("candidates")
    return {"start": start.isoformat(timespec="seconds"), "until": now.isoformat(timespec="seconds"), **result}


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        if host not in {"127.0.0.1", "localhost"}:
            self.send_error(403)
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/api/dashboard":
            try:
                data = projected_snapshot(self.server.data_dir)
                self.send_content(200, "application/json", json.dumps(data, ensure_ascii=False, allow_nan=False).encode())
            except (OSError, KeyError, TypeError, ValueError, AttributeError):
                self.send_content(503, "application/json", b'{"error":"snapshot_unavailable"}')
        elif path == "/api/news-audit":
            try:
                data = read_today_news_audit(self.server.data_dir)
                self.send_content(200, "application/json", json.dumps(data, ensure_ascii=False, allow_nan=False).encode())
            except (OSError, sqlite3.Error, ValueError):
                self.send_content(503, "application/json", b'{"error":"news_audit_unavailable"}')
        elif path == "/api/klines":
            symbol = parse_qs(parsed.query).get("symbol", [""])[0]
            try:
                data = cached_market_chart(self.server.data_dir, symbol)
                if data is None:
                    self.send_content(404, "application/json", b'{"error":"unknown_symbol"}')
                else:
                    self.send_content(200, "application/json", json.dumps(data, allow_nan=False).encode())
            except (OSError, KeyError, TypeError, ValueError, AttributeError):
                self.send_content(503, "application/json", b'{"error":"snapshot_unavailable"}')
        elif path in ROUTES:
            name, content_type = ROUTES[path]
            self.send_content(200, content_type, (ASSETS / name).read_bytes())
        else:
            self.send_error(404)

    def send_content(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_request(self, code="-", size="-"):
        if str(code) not in {"200", "503"}:
            super().log_request(code, size)


def main():
    parser = argparse.ArgumentParser(description="Morgan SSH 只读交易看板")
    parser.add_argument("--port", type=int, default=18652)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), DashboardHandler) as server:
        server.data_dir = args.data_dir.resolve()
        print(f"Morgan dashboard http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
