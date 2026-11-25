"""Collect official Jin10 headlines and replay news events without placing orders."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
JIN10 = "https://mcp.jin10.com/mcp"
HORIZONS = (60, 300, 900)
MINUTE_MS = 60_000
CATALOG_WINDOW_MS = 36 * 60 * 60 * 1000
MARKET_ONLY_TOPICS = {"market_move"}
CEO_REINSTATEMENT = re.compile(
    r"复任|复职|回归|回任|重返|恢复.{0,28}(?:CEO|首席执行官)|"
    r"reinstat\w*|rehir\w*|return\w*\s+(?:as|to)", re.IGNORECASE
)
LEADERSHIP = re.compile(r"CEO|首席执行官|黄仁勋|Jensen\s+Huang", re.IGNORECASE)
CONFIRMATION = re.compile(r"复任|恢复|重新出任|reinstates?|reinstated|returns as", re.IGNORECASE)
MAY_BE = re.compile(
    r"可能|考虑|希望|尚未|能否|如果|据传|据称|知情人士|不反对|不再排除|不排除|暗示|"
    r"\b(?:could|may|mulls?|hopes?|reportedly|not rule out|calls? for|urges?)\b", re.IGNORECASE
)
EXPLICIT_TICKER = re.compile(r"(?<![A-Z0-9])(?:\$([A-Z0-9]{2,19})(?![A-Z0-9])|([A-Z0-9]{2,19})[/\s]?USDT(?![A-Z0-9]))")
ALIASES = {
    "NVDA": r"英伟达|\bNVIDIA\b|\bNVDA\b",
    "BTC": r"比特币|\b(?:Bitcoin|BTC)\b",
    "ETH": r"以太坊|\b(?:Ethereum|ETH)\b",
    "SOL": r"索拉纳|\bSolana\b|(?-i:\bSOL\b)",
    "XRP": r"瑞波币|\bXRP\b",
    "AAVE": r"\bAave\b",
    "DOGE": r"狗狗币|\bDogecoin\b",
}
STABLECOINS = re.compile(r"\b(?:USDC|USDT)\b", re.IGNORECASE)
MARKET_ALERT = re.compile(r"合约监测|行情监测|价格警报|\b(?:price alert|market monitor)\b", re.IGNORECASE)
CRYPTO_EVENTS = (
    ("enforcement", re.compile(r"没收|扣押|查封|\b(?:forfeit\w*|seiz(?:e|ed|ure))\b", re.IGNORECASE)),
    ("regulation", re.compile(r"增值税|征税|税率|出台.{0,10}监管|\b(?:VAT|taxation|tax levy|regulatory approval)\b", re.IGNORECASE)),

    ("delisting", re.compile(r"下架|停止交易|\bdelist\w*\b", re.IGNORECASE)),
    ("listing", re.compile(r"上线|开放交易|上架|\b(?:will list|new listing|lists?|will launch)\b", re.IGNORECASE)),
    ("security", re.compile(r"攻击|被盗|漏洞|\b(?:hacked|exploit\w*|security breach)\b", re.IGNORECASE)),
    ("upgrade", re.compile(r"升级|硬分叉|\b(?:hard fork|network upgrade)\b", re.IGNORECASE)),
    ("fund_flow", re.compile(r"资金流入|资金流出|增持|减持|抛售|\b(?:ETF (?:inflows?|outflows?)|buys? more|offloads?|acquires?)\b", re.IGNORECASE)),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_event_observations (
    event_key TEXT NOT NULL, symbol TEXT NOT NULL, topic TEXT NOT NULL,
    stage TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL,
    title TEXT NOT NULL, published_ms INTEGER NOT NULL, observed_ms INTEGER NOT NULL,
    PRIMARY KEY(source, source_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_news_event_symbol_time
ON news_event_observations(symbol, topic, observed_ms);
CREATE TABLE IF NOT EXISTS news_event_paper (
    event_key TEXT NOT NULL, stage TEXT NOT NULL, method TEXT NOT NULL,
    signal_ms INTEGER NOT NULL, result_json TEXT NOT NULL,
    PRIMARY KEY(event_key, stage, method)
);
CREATE TABLE IF NOT EXISTS news_event_targets (
    event_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, observed_ms INTEGER NOT NULL,
    status TEXT NOT NULL, result_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_event_targets_status_time ON news_event_targets(status, observed_ms);
"""


class Jin10Client:
    def __init__(self, session, token):
        self.session = session
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
        self.request_id = 0
        self.initialized = False

    def call(self, method, params, *, notification=False):
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            self.request_id += 1
            payload["id"] = self.request_id
        with self.session.post(JIN10, headers=self.headers, json=payload, timeout=(5, 12), stream=True) as response:
            if response.status_code >= 400:
                raise ValueError(f"金十 MCP HTTP {response.status_code}")
            if session_id := response.headers.get("Mcp-Session-Id"):
                self.headers["Mcp-Session-Id"] = session_id
            if notification:
                return None
            if "text/event-stream" in response.headers.get("Content-Type", ""):
                lines = []
                for line in response.iter_lines(chunk_size=1):
                    if line.startswith(b"data:"):
                        lines.append(line[5:].lstrip())
                    if not line and lines:
                        message = json.loads(b"\n".join(lines))
                        lines.clear()
                        if message.get("id") == self.request_id:
                            break
                else:
                    raise ValueError("金十 MCP 未返回请求结果")
            else:
                message = response.json()
        if message.get("id") != self.request_id or "result" not in message:
            raise ValueError("金十 MCP 调用失败")
        return message["result"]

    def list_flash(self):
        if not self.initialized:
            info = self.call("initialize", {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "morgan-news-shadow", "version": "1.0"},
            })
            if version := info.get("protocolVersion"):
                self.headers["MCP-Protocol-Version"] = version
            self.call("notifications/initialized", {}, notification=True)
            self.initialized = True
        result = self.call("tools/call", {"name": "list_flash", "arguments": {}})
        if result.get("isError"):
            raise ValueError("金十 list_flash 工具返回错误")
        payload = result.get("structuredContent")
        if not payload:
            for item in result.get("content", []):
                if item.get("type") == "text":
                    payload = json.loads(item["text"])
                    break
        if not isinstance(payload, dict) or payload.get("status", 200) != 200:
            raise ValueError("金十快讯返回异常")
        items = payload.get("data", {}).get("items")
        if not isinstance(items, list):
            raise ValueError("金十快讯响应缺少 items")
        return items


def flash_article(row, observed_ms):
    published = datetime.fromisoformat(row["time"])
    title = " ".join((row.get("title") or row.get("content") or "").split())
    if published.tzinfo is None or not title:
        raise ValueError("快讯缺少带时区的发布时间或标题")
    published_ms = int(published.timestamp() * 1000)
    url = row.get("url")
    provider_id = row.get("id")
    if not provider_id and url:
        path = urlsplit(url).path
        if match := re.fullmatch(r"/detail/([0-9]+)/?", path):
            provider_id = match[1]
    if not provider_id:
        provider_id = hashlib.sha256(f"{published_ms}\n{title}".encode()).hexdigest()
    return {
        "source": "金十数据", "source_id": str(provider_id), "title": title,
        "url": url, "time": published.astimezone(TZ).strftime("%Y-%m-%d %H:%M"),
        "published_ms": published_ms, "observed_ms": observed_ms,
    }


def active_contracts(payload):
    return {row["symbol"] for row in payload["symbols"]
            if row.get("status") == "TRADING" and row.get("quoteAsset") == "USDT"
            and row.get("contractType") in {"PERPETUAL", "TRADIFI_PERPETUAL"}}


def event_targets(article):
    title = article["title"]
    named = {symbol for symbol, pattern in ALIASES.items() if re.search(pattern, title, re.IGNORECASE)}
    for ticker, base in EXPLICIT_TICKER.findall(title):
        if ticker and re.fullmatch(r"[0-9]+[KMBT]?", ticker):
            continue  # Dollar amounts such as $13.5M are not cashtags.
        symbol = ticker.removesuffix("USDT") or base
        if len(symbol) >= 2:
            named.add(symbol)
    named.update(symbol for symbol in (article.get("candidate_symbols") or [])
                 if isinstance(symbol, str) and re.fullmatch(r"[A-Z0-9]{2,19}", symbol))
    ceo_event = LEADERSHIP.search(title) and CEO_REINSTATEMENT.search(title)
    topic = "market_move" if MARKET_ALERT.search(title) else (
        "ceo_return" if ceo_event else next((key for key, pattern in CRYPTO_EVENTS if pattern.search(title)), None))
    if topic in {"enforcement", "regulation"}:
        named.update(code.upper() for code in STABLECOINS.findall(title))
    if not topic or not named or len(named) > 4:
        return []
    url = urlsplit(article.get("url") or "")
    confident = not MAY_BE.search(title) and (not ceo_event or CONFIRMATION.search(title))
    nvidia_source = url.scheme == "https" and (
        url.hostname in {"nvidianews.nvidia.com", "investor.nvidia.com"}
        or (url.hostname in {"www.sec.gov", "sec.gov"} and url.path.lower().startswith("/archives/edgar/data/1045810/"))
    )
    exchange_source = url.scheme == "https" and article.get("source") == "币安" and (
        (url.hostname in {"www.binance.com", "binance.com"} and "/support/announcement/" in url.path)
        or (url.hostname == "t.me" and re.fullmatch(r"/binance_announcements/[0-9]+/?", url.path))
    )
    futures_notice = topic in {"listing", "delisting"} and re.search(r"\bfutures\b|\bperpetual\b|合约", title, re.IGNORECASE)
    candidates = []
    for symbol in sorted(named):
        contract = symbol if symbol == "USDT" else symbol + "USDT"
        official = (ceo_event and symbol == "NVDA" and nvidia_source) or (exchange_source and futures_notice)
        stage = ("confirmed" if official else "reported") if confident and topic not in MARKET_ONLY_TOPICS else "watch"
        candidates.append((contract, topic, stage))
    return candidates


def event_candidates(article, contracts):
    return [(symbol, stage) for symbol, _, stage in event_targets(article) if symbol in contracts]


def store_events(db, articles, contracts):
    db.executescript(SCHEMA)
    new = []
    for article in sorted(articles, key=lambda item: (item["observed_ms"], item["published_ms"])):
        observed = article["observed_ms"]
        published = article["published_ms"]
        if observed < published:
            continue
        for symbol, topic, stage in event_targets(article):
            group_ms = CATALOG_WINDOW_MS if topic == "ceo_return" else 90 * MINUTE_MS
            previous = db.execute(
                """SELECT event_key FROM news_event_observations
                   WHERE symbol=? AND topic=? AND observed_ms BETWEEN ? AND ?
                   ORDER BY observed_ms DESC LIMIT 1""",
                (symbol, topic, observed - group_ms, observed),
            ).fetchone()
            key = previous[0] if previous else f"{symbol}:{topic}:{published}"
            result = db.execute(
                """INSERT INTO news_event_observations
                   (event_key,symbol,topic,stage,source,source_id,title,published_ms,observed_ms)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source,source_id,symbol) DO UPDATE SET stage=excluded.stage
                   WHERE news_event_observations.stage<>excluded.stage
                   AND news_event_observations.title=excluded.title
                   AND news_event_observations.observed_ms=excluded.observed_ms""",
                (key, symbol, topic, stage, article["source"], article["source_id"], article["title"], published, observed),
            )
            if symbol not in contracts:
                status = "no_testnet_contract"
            elif stage == "watch":
                status = "watch"
            elif observed - published > 120_000:
                status = "late_news"
            else:
                status = "pending"
            db.execute(
                """INSERT INTO news_event_targets(event_key,symbol,observed_ms,status)
                   SELECT event_key,symbol,observed_ms,? FROM news_event_observations
                   WHERE source=? AND source_id=? AND symbol=? AND observed_ms=?
                   ON CONFLICT(event_key) DO UPDATE SET observed_ms=excluded.observed_ms,status=excluded.status
                   WHERE news_event_targets.status IN ('watch','late_news','no_testnet_contract')
                   AND excluded.status='pending'""",
                (status, article["source"], article["source_id"], symbol, observed),
            )
            if result.rowcount:
                new.append({"event_key": key, "symbol": symbol, "stage": stage, "observed_ms": observed,
                            "contract_status": "available" if symbol in contracts else "no_testnet_contract"})
    return new


def paper_followup(quotes, signal_ms):
    """Estimate a long using fresh ask at signal and executable bids at fixed horizons."""
    valid = sorted((q for q in quotes if 0 < q["bid"] <= q["ask"]), key=lambda q: q["timestamp_ms"])

    def first_quote(start):
        return next((q for q in valid if start <= q["timestamp_ms"] <= start + 5000), None)

    entry = first_quote(signal_ms)
    if entry is None:
        return {"status": "no_book"}
    returns = {}
    for seconds in HORIZONS:
        quote = first_quote(signal_ms + seconds * 1000)
        returns[str(seconds)] = round((quote["bid"] / entry["ask"] - 1) * 100, 6) if quote else None
    return {
        "status": "book_simulation", "entry_ask": entry["ask"], "time_ms": entry["timestamp_ms"],
        "spread_bps": round((entry["ask"] - entry["bid"]) / entry["ask"] * 10000, 4),
        "returns_pct_before_fees": returns,
    }


def minute_followup(candles, signal_ms):
    """Approximate a long with future minute openings when historical book data is missing."""
    prices = {int(row[0]): float(row[1]) for row in candles}
    entry_ms = ((signal_ms + MINUTE_MS - 1) // MINUTE_MS) * MINUTE_MS
    entry = prices.get(entry_ms)
    if not entry or entry <= 0:
        return {"status": "no_minute_data"}
    returns = {}
    for seconds in HORIZONS:
        later = prices.get(entry_ms + seconds * 1000)
        returns[str(seconds)] = round((later / entry - 1) * 100, 6) if later and later > 0 else None
    return {
        "status": "minute_open_approximation", "entry_price": entry, "time_ms": entry_ms,
        "returns_pct_before_fees": returns,
    }


def replay(path, db_path):
    payload = json.loads(path.read_text())
    quotes = payload.get("quotes", {})
    candles = payload.get("minute_candles", {})
    with closing(sqlite3.connect(db_path)) as db, db:
        events = store_events(db, payload["articles"], active_contracts(payload["exchange_info"]))
        for event in events:
            if event["stage"] == "watch" or event["contract_status"] != "available":
                continue
            symbol, signal_ms = event["symbol"], event["observed_ms"]
            result = paper_followup(quotes.get(symbol, []), signal_ms)
            method = "book"
            if result["status"] == "no_book" and symbol in candles:
                method = "minute_open"
                result = minute_followup(candles[symbol], signal_ms)
            row = db.execute(
                """INSERT OR IGNORE INTO news_event_paper
                   (event_key,stage,method,signal_ms,result_json) VALUES (?,?,?,?,?)""",
                (event["event_key"], event["stage"], method, signal_ms, json.dumps(result, ensure_ascii=False)),
            )
            if row.rowcount:
                event["paper"] = result
    return events


def ingest_flashes(db_module, exchange_info, rows):
    seen = time.time_ns() // 1_000_000
    articles = []
    for row in rows:
        try:
            article = flash_article(row, seen)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if article["published_ms"] <= seen:
            articles.append(article)
    if rows and not articles:
        raise ValueError("金十快讯均缺少可用正文或发布时间")
    added = db_module.insert_news(articles)
    events = []
    if exchange_info is not None:
        with closing(db_module.connect()) as db, db:
            events = store_events(db, articles, active_contracts(exchange_info))
    return {"received": len(rows), "skipped": len(rows) - len(articles), "added_news": added,
            "latest_published_ms": max((row["published_ms"] for row in articles), default=None), "events": events}


def collect_once(db_module, exchange_info, token):
    import requests

    with requests.Session() as session:
        rows = Jin10Client(session, token).list_flash()
    return ingest_flashes(db_module, exchange_info, rows)


def index_existing(db_module, exchange_info):
    cutoff = time.time_ns() // 1_000_000 - 2 * 24 * 60 * 60 * 1000
    with closing(db_module.connect()) as db, db:
        articles = [dict(row) for row in db.execute(
            """SELECT CAST(id AS TEXT) AS source_id, source, title, url, published_ms, observed_ms
               FROM news WHERE observed_ms >= ? AND published_ms IS NOT NULL
               AND source <> '金十数据' ORDER BY observed_ms, id""",
            (cutoff,),
        )]
        return store_events(db, articles, active_contracts(exchange_info))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    options = parser.add_mutually_exclusive_group(required=True)
    options.add_argument("--replay", type=Path, help="在独立的临时 SQLite 回放 JSON")
    options.add_argument("--collect-once", metavar="EXCHANGE_INFO", type=Path,
                         help="用已保存的 exchangeInfo 做一次金十采集")
    options.add_argument("--index-news", metavar="EXCHANGE_INFO", type=Path,
                         help="用现有新闻的首次采集时间回填近 48 小时的观察记录")
    args = parser.parse_args()
    try:
        if args.replay:
            with tempfile.TemporaryDirectory() as temp:
                result = replay(args.replay, Path(temp) / "news.db")
        else:
            import trade_db

            exchange_info = json.loads((args.collect_once or args.index_news).read_text())
            active_contracts(exchange_info)
            if args.index_news:
                result = index_existing(trade_db, exchange_info)
            else:
                token = os.environ.get("JIN10_MCP_TOKEN")
                if not token:
                    raise ValueError("缺少 JIN10_MCP_TOKEN")
                result = collect_once(trade_db, exchange_info, token)
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"新闻观察未完成：{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
