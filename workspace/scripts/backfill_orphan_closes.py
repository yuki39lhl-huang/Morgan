#!/usr/bin/env python3
"""
backfill_orphan_closes.py — 一次性补记「开仓有、平仓无」的历史孤儿仓。

根因：交易所 Algo 单先成交时本地不写 trade_closes。本脚本用 userTrades
的 realizedPnl 补主平仓，不改持仓、不下单。

用法：
  python3 backfill_orphan_closes.py --dry-run
  python3 backfill_orphan_closes.py --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime

from binance_auto_trade import request
import trade_db

DB = trade_db.DB_PATH


def _fills(symbol: str, direction: str, open_ts: str, until_ts: str | None):
    start_ms = int(datetime.fromisoformat(open_ts).timestamp() * 1000)
    end_ms = int(datetime.fromisoformat(until_ts).timestamp() * 1000) if until_ts else int(datetime.now().timestamp() * 1000)
    close_side = "SELL" if direction == "LONG" else "BUY"
    trades = request("GET", "/fapi/v1/userTrades", {
        "symbol": f"{symbol}USDT",
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 100,
    })
    if not isinstance(trades, list):
        return None
    hit = None
    for t in trades:
        if t.get("side") != close_side:
            continue
        try:
            rp = float(t.get("realizedPnl") or 0)
        except (TypeError, ValueError):
            rp = 0.0
        if abs(rp) > 1e-8:
            hit = t
    return hit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply = bool(args.apply)
    if apply:
        args.dry_run = False

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    orphans = conn.execute("""
        SELECT t.trade_id, t.ts, t.symbol, t.direction, t.entry_price
        FROM trades t
        LEFT JOIN trade_closes c ON c.trade_id=t.trade_id AND c.is_primary=1
        WHERE c.id IS NULL
        ORDER BY t.ts
    """).fetchall()

    # 仍在交易所的仓位不补（真的还开着）
    from binance_auto_trade import get_all_positions
    alive = set()
    for p in get_all_positions() or []:
        amt = float(p.get("amount", 0))
        entry = float(p.get("entry_price", 0))
        if amt != 0 and abs(amt) * entry >= 5.0:
            alive.add((p["symbol"], "SHORT" if amt < 0 else "LONG"))

    print(f"未配对开仓 {len(orphans)} 笔；交易所仍在仓 {sorted(alive)}")

    # 每个仍在仓的 (symbol, direction) 只跳过「入场价最接近」的那一笔，
    # 避免把同向历史孤儿也当成在仓（如当前 LINK LONG 不应挡住 8 月 LINK 孤儿）。
    skip_ids = set()
    for key in alive:
        cand = [r for r in orphans if (r["symbol"], r["direction"]) == key]
        if not cand:
            continue
        # 取交易所入场价
        api_entry = None
        for p in get_all_positions() or []:
            amt = float(p.get("amount", 0))
            entry = float(p.get("entry_price", 0))
            if amt == 0 or abs(amt) * entry < 5.0:
                continue
            d = "SHORT" if amt < 0 else "LONG"
            if (p["symbol"], d) == key:
                api_entry = entry
                break
        best = cand[-1]  # 默认最近一笔
        if api_entry:
            best = min(
                cand,
                key=lambda r: abs((r["entry_price"] or 0) - api_entry),
            )
        skip_ids.add(best["trade_id"])

    patched = 0
    for i, row in enumerate(orphans):
        if row["trade_id"] in skip_ids:
            print(f"  SKIP 仍在仓 {row['trade_id']} {(row['symbol'], row['direction'])}")
            continue
        # 截止到下一笔同币开仓（避免吃到后续仓的平仓成交）
        until = None
        for later in orphans[i + 1:]:
            if later["symbol"] == row["symbol"]:
                until = later["ts"]
                break
        hit = _fills(row["symbol"], row["direction"], row["ts"], until)
        if not hit:
            print(f"  MISS 无成交 {row['trade_id']} {key} open={row['ts']}")
            continue
        px = float(hit["price"])
        pnl = float(hit["realizedPnl"])
        entry = float(row["entry_price"] or 0)
        pnl_pct = (px - entry) / entry if row["direction"] == "LONG" else (entry - px) / entry
        close_ts = datetime.fromtimestamp(int(hit["time"]) / 1000).isoformat()
        print(f"  {'APPLY' if apply else 'DRY'} {row['trade_id']} {key} "
              f"exit={px} pnl={pnl:+.4f}U pct={pnl_pct*100:+.2f}% at {close_ts}")
        if apply:
            trade_db.insert_close(
                trade_id=row["trade_id"], ts=close_ts,
                pnl_usdt=round(pnl, 4), pnl_pct=round(pnl_pct, 6),
                reason="Algo成交", is_primary=True,
            )
            patched += 1
    conn.close()
    print(f"完成：补记 {patched} 笔" if apply else "干跑结束（加 --apply 写入）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
