"""Replay stored news at Morgan's recorded arrival times without executing trades."""

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

from news_event_shadow import MARKET_ONLY_TOPICS, TZ, event_targets

ENTRY_WINDOW_MS = 120_000


def observation_status(topic, published_ms, observed_ms, now_ms):
    if topic in MARKET_ONLY_TOPICS:
        return "market_only"
    if observed_ms - published_ms >= ENTRY_WINDOW_MS:
        return "late_news"
    if now_ms - published_ms >= ENTRY_WINDOW_MS:
        return "window_expired"
    return "queued"


def observation_reason(status, published_ms, observed_ms):
    lag = max(0, observed_ms - published_ms) // 1000
    timing = f"来源时间至收到 {lag // 60} 分 {lag % 60} 秒" if lag >= 60 else f"来源时间至收到 {lag} 秒"
    if status == "market_only":
        return timing + "。已记录价格异动，等待原始事件公告或其他直接证据。"
    if status == "late_news":
        return timing + "，已到 2 分钟入场期限。保留作事件复盘。"
    if status == "window_expired":
        return timing + "。时窗内未生成方向判断，保留原文供复盘。"
    return None


def replay(db, start_ms, end_ms, *, include_current_status=False):
    by_source = defaultdict(Counter)
    totals = Counter()
    details = []
    headlines = []
    delays = defaultdict(list)
    evidence = {}
    for row in db.execute(
        """SELECT event_key,stage,started_ms,decision_ms,action,status,evidence_json
           FROM news_entry_decisions WHERE started_ms <= ?""", (end_ms,)
    ):
        for item in json.loads(row["evidence_json"]):
            if item["observed_ms"] <= row["started_ms"]:
                key = (row["event_key"], row["stage"], item["source"], item["title"],
                       item["published_ms"], item["observed_ms"])
                evidence[key] = row
    observations = {
        (row["source"], row["title"], row["published_ms"], row["symbol"], row["stage"]): row["event_key"]
        for row in db.execute(
            """SELECT source,title,published_ms,symbol,stage,event_key FROM news_event_observations
               WHERE observed_ms BETWEEN ? AND ?""", (start_ms, end_ms)
        )
    }
    for row in db.execute(
        """SELECT id,source,title,url,published_ms,observed_ms FROM news
           WHERE observed_ms BETWEEN ? AND ? ORDER BY observed_ms,id""",
        (start_ms, end_ms),
    ):
        article = dict(row)
        totals["headlines"] += 1
        counts = by_source[article["source"]]
        counts["headlines"] += 1
        published, observed = article["published_ms"], article["observed_ms"]
        headline = {"news_id": article["id"], "source": article["source"], "headline": article["title"],
                    "published_ms": published, "observed_ms": observed, "url": article["url"]}
        if published is None or published > observed:
            headlines.append({**headline, "targets": [], "gate": "invalid_timing"})
            totals["invalid_timing"] += 1
            counts["invalid_timing"] += 1
            continue
        delay = observed - published
        delays[article["source"]].append(delay)
        if delay < ENTRY_WINDOW_MS:
            counts["within_window"] += 1
        matches = event_targets(article)
        if not matches:
            totals["unmatched_headlines"] += 1
            counts["unmatched_headlines"] += 1
            headlines.append({**headline, "delay_ms": delay, "targets": [], "gate": "unmatched"})
            continue
        totals["recognized_headlines"] += 1
        counts["recognized_headlines"] += 1
        targets = []
        for symbol, topic, stage in matches:
            status = observation_status(topic, published, observed, observed)
            if status == "queued":
                status = "timely_confirmed" if stage == "confirmed" else "timely_unconfirmed"
            totals[status] += 1
            counts[status] += 1
            key = observations.get((article["source"], article["title"], published, symbol, stage))
            decision = evidence.get((key, stage, article["source"], article["title"], published, observed))
            decided = decision is not None and decision["decision_ms"] is not None and decision["decision_ms"] <= end_ms
            target = {"symbol": symbol, "topic": topic, "stage": stage, "gate": status,
                      "indexed": key is not None, "model_started": decision is not None,
                      "action": decision["action"] if decided else None}
            if include_current_status and decision is not None:
                target["recorded_status"] = decision["status"]
            targets.append(target)
            details.append({**headline, **target, "delay_ms": delay})
        headlines.append({**headline, "delay_ms": delay, "targets": targets})
    for source, values in delays.items():
        values.sort()
        counts = by_source[source]
        counts["timed"] = len(values)
        counts["p50_ms"] = round(median(values))
        counts["p90_ms"] = values[(9 * len(values) - 1) // 10]
    calls = Counter()
    for row in db.execute(
        """SELECT stage,action,decision_ms FROM news_entry_decisions
           WHERE started_ms BETWEEN ? AND ?""", (start_ms, end_ms)
    ):
        calls["started"] += 1
        if row["decision_ms"] is not None and row["decision_ms"] <= end_ms:
            action = row["action"]
            calls[action or "no_direction"] += 1
            if action in {"long", "short"} and row["stage"] == "confirmed":
                calls["confirmed_direction"] += 1
    orders = Counter()
    for row in db.execute(
        """SELECT submitted_ms,opened_ms FROM news_testnet_orders
           WHERE ready_ms BETWEEN ? AND ?""", (start_ms, end_ms),
    ):
        orders["queued"] += 1
        if row["submitted_ms"] is not None and row["submitted_ms"] <= end_ms:
            orders["claimed_for_submission"] += 1
        if row["opened_ms"] is not None and row["opened_ms"] <= end_ms:
            orders["filled"] += 1
    return {"counts": dict(totals), "by_source": dict(by_source), "recorded_model": dict(calls),
            "recorded_testnet": dict(orders), "candidates": details, "headlines": headlines}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(__file__).with_name("trade_data.db"))
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--today", action="store_true", help="从北京时间零点开始回放")
    parser.add_argument("--until", type=datetime.fromisoformat, help="Replay cut-off in ISO format (default: now)")
    args = parser.parse_args()
    if args.hours < 1:
        parser.error("--hours must be positive")
    until = args.until or datetime.now(TZ)
    until = until.astimezone(TZ) if until.tzinfo else until.replace(tzinfo=TZ)
    start = (until.replace(hour=0, minute=0, second=0, microsecond=0) if args.today
             else until - timedelta(hours=args.hours))
    with closing(sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        result = replay(db, int(start.timestamp() * 1000), int(until.timestamp() * 1000),
                        include_current_status=args.until is None)
    print(json.dumps({"start": start.isoformat(timespec="seconds"), "until": until.isoformat(timespec="seconds"),
                      **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
