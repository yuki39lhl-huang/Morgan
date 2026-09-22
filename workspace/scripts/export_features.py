#!/usr/bin/env python3
"""
export_features.py — 归因库导出（量化升级 Phase 2.6）

把 trade_data.db 导出成 JSONL，供人工审阅（DB 是唯一事实源，导出产物按既有
约定不进 git —— .gitignore 已排除 *.jsonl 与 trade_data.db）。

用法：
  python3 export_features.py                        # 导出到默认路径
  python3 export_features.py --out-dir /tmp/export
"""
import argparse
import json
import os

import trade_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _dump(path: str, rows: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="归因库 → JSONL 导出")
    ap.add_argument("--out-dir", default=SCRIPT_DIR, help="输出目录（默认 scripts/）")
    args = ap.parse_args()

    if not os.path.exists(trade_db.DB_PATH):
        print(f"❌ 归因库不存在：{trade_db.DB_PATH}")
        return 1
    os.makedirs(args.out_dir, exist_ok=True)

    with trade_db.connect(readonly=True) as conn:
        trades = [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY ts")]
        closes = [dict(r) for r in conn.execute("SELECT * FROM trade_closes ORDER BY ts")]
        scans = [dict(r) for r in conn.execute("SELECT * FROM ai_scans ORDER BY ts")]
        vetoes = [dict(r) for r in conn.execute(
            """SELECT v.*, o.price_1h, o.price_4h, o.price_24h FROM vetoes v
               LEFT JOIN veto_outcomes o ON o.veto_id = v.id ORDER BY v.ts""")]

    # 还原为旧版事件流格式（open / close 混合按时间排序），便于与历史文件对比
    events = []
    for t in trades:
        events.append({
            "event": "open", "ts": t["ts"], "trade_id": t["trade_id"],
            "symbol": t["symbol"], "direction": t["direction"],
            "score": t["score"], "score_raw": t["score_raw"],
            "ai_score_adjust": t["ai_score_adjust"], "regime": t["regime"],
            "features": {
                "breakout": t["f_breakout"], "volume_ratio": t["f_volume_ratio"],
                "atr_pct": t["f_atr_pct"], "btc_bull": t["f_btc_bull"],
                "sentiment": t["f_sentiment"],
            },
            "ai": {"direction": t["ai_direction"], "confidence": t["ai_confidence"],
                   "reason": t["ai_reason"]},
            "entry_price": t["entry_price"],
        })
    for c in closes:
        events.append({
            "event": "close", "ts": c["ts"], "trade_id": c["trade_id"],
            "pnl_usdt": c["pnl_usdt"], "pnl_pct": c["pnl_pct"], "reason": c["reason"],
            "is_primary": bool(c["is_primary"]),
        })
    events.sort(key=lambda r: r["ts"])

    out_features = os.path.join(args.out_dir, "trade_features.jsonl")
    out_scan = os.path.join(args.out_dir, "ai_scan.jsonl")
    out_veto = os.path.join(args.out_dir, "vetoes.jsonl")

    _dump(out_features, events)
    # ai_scans 按整点重组，保持与旧文件一致的结构
    by_ts = {}
    for s in scans:
        rec = by_ts.setdefault(s["ts"], {"ts": s["ts"], "fg": s["fg"], "prices": {}, "scan": {}})
        if s["price"] is not None:
            rec["prices"][s["symbol"]] = s["price"]
        rec["scan"][s["symbol"]] = {"direction": s["direction"],
                                    "confidence": s["confidence"], "reason": s["reason"]}
    _dump(out_scan, [by_ts[k] for k in sorted(by_ts)])
    _dump(out_veto, vetoes)

    print(f"✅ 导出完成：")
    print(f"  {out_features}  {len(events)} 行")
    print(f"  {out_scan}      {len(by_ts)} 行")
    print(f"  {out_veto}      {len(vetoes)} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())