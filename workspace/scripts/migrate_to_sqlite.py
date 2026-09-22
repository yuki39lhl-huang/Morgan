#!/usr/bin/env python3
"""
migrate_to_sqlite.py — 一次性迁移（量化升级 Phase 2.6）

把 trade_features.jsonl / ai_scan.jsonl 导入 trade_data.db，原文件重命名为
*.jsonl.bak 保留（可回滚）。

用法：
  python3 migrate_to_sqlite.py            # 首次迁移（库非空时报错）
  python3 migrate_to_sqlite.py --dry-run  # 只统计不写入
  python3 migrate_to_sqlite.py --force    # 清空重建后重新导入

迁移后校验：行数一致 + 配对率与迁移前一致。
"""
import argparse
import json
import os
import sys
from collections import Counter

import trade_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FEATURES_FILE = os.path.join(SCRIPT_DIR, "trade_features.jsonl")
SCAN_FILE = os.path.join(SCRIPT_DIR, "ai_scan.jsonl")


def _read_jsonl(path: str) -> list[dict]:
    rows = []
    if not os.path.exists(path):
        return rows
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


def load_sources() -> tuple[list, list, list]:
    """返回 (opens, closes, scan_rows)。"""
    opens, closes, dup_close = [], [], Counter()
    for r in _read_jsonl(FEATURES_FILE):
        ev = r.get("event")
        if ev == "open":
            opens.append(r)
        elif ev == "close":
            tid = r.get("trade_id")
            if not tid:
                continue
            dup_close[tid] += 1
            closes.append((r, dup_close[tid] == 1))  # (记录, 是否主平仓)
    scans = _read_jsonl(SCAN_FILE)
    return opens, closes, scans


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计不写入")
    ap.add_argument("--force", action="store_true", help="清空重建后重新导入")
    args = ap.parse_args()

    opens, closes, scans = load_sources()
    open_ids = {o["trade_id"] for o in opens}
    orphan_closes = [r for r, _ in closes if r.get("trade_id") not in open_ids]
    # 按「交易」而非「行」计数：同一 trade_id 的残余清理行不重复计入
    paired = len({r["trade_id"] for r, _ in closes if r.get("trade_id") in open_ids})

    print("=== 源数据统计 ===")
    print(f"  trades（open）      : {len(opens)}")
    print(f"  trade_closes（close）: {len(closes)}（主平仓 {sum(1 for _, p in closes if p)}，"
          f"残余清理 {sum(1 for _, p in closes if not p)}）")
    print(f"  ai_scans（整点行）   : {len(scans)} → 展开为 {sum(len(r.get('scan') or {}) for r in scans)} 币行")
    print(f"  可配对交易          : {paired} / {len(opens)} "
          f"= {paired / len(opens) * 100:.1f}%（未配对 {len(opens) - paired} 笔为旧仓/漏记）")
    if orphan_closes:
        print(f"  ⚠️ 无对应 open 的 close: {len(orphan_closes)}（导入但视图中不可见）")

    if args.dry_run:
        print("\n[dry-run] 未写入。")
        return 0

    # 幂等保护：库非空时必须显式 --force
    if os.path.exists(trade_db.DB_PATH):
        with trade_db.connect(readonly=True) as conn:
            try:
                n = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
            except Exception:
                n = 0
        if n and not args.force:
            print(f"\n❌ 库中已有 {n} 条 trades，拒绝重复导入。如需重来请加 --force（会清空重建）。")
            return 1

    if args.force and os.path.exists(trade_db.DB_PATH):
        for suffix in ("", "-wal", "-shm"):
            p = trade_db.DB_PATH + suffix
            if os.path.exists(p):
                os.remove(p)
        print("\n🗑️ 已清空旧库（--force）")

    trade_db.init_db()
    print(f"\n✅ 建表完成：{trade_db.DB_PATH}")

    # ① trades
    for o in opens:
        trade_db.insert_trade(
            trade_id=o["trade_id"], ts=o.get("ts", ""), symbol=o.get("symbol", ""),
            direction=o.get("direction", "LONG"), entry_price=o.get("entry_price"),
            score=o.get("score"), score_raw=o.get("score_raw"),
            ai_score_adjust=o.get("ai_score_adjust", 0), regime=o.get("regime"),
            features=o.get("features") or {}, ai_result=o.get("ai") or {},
        )
    print(f"   → trades 写入 {len(opens)} 行")

    # ② trade_closes
    for r, is_primary in closes:
        trade_db.insert_close(
            trade_id=r["trade_id"], ts=r.get("ts", ""),
            pnl_usdt=r.get("pnl_usdt", 0), pnl_pct=r.get("pnl_pct", 0),
            reason=r.get("reason", ""), is_primary=is_primary,
        )
    print(f"   → trade_closes 写入 {len(closes)} 行")

    # ③ ai_scans（逐行展开，保留原始整点时间戳）
    written = 0
    for rec in scans:
        written += trade_db.insert_scan_rows(
            ts=rec.get("ts", ""), fg=rec.get("fg"),
            price_map=rec.get("prices") or {}, results=rec.get("scan") or {},
        )
    print(f"   → ai_scans 写入 {written} 行")

    # ④ 校验
    with trade_db.connect(readonly=True) as conn:
        t = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
        c = conn.execute("SELECT COUNT(*) AS n FROM trade_closes").fetchone()["n"]
        cp = conn.execute("SELECT COUNT(*) AS n FROM trade_closes WHERE is_primary=1").fetchone()["n"]
        s = conn.execute("SELECT COUNT(*) AS n FROM ai_scans").fetchone()["n"]
        vp = conn.execute("SELECT COUNT(*) AS n FROM v_trade_attribution WHERE is_unpaired=0").fetchone()["n"]

    print("\n=== 校验 ===")
    ok = True
    checks = [
        ("trades 行数", t, len(opens)),
        ("trade_closes 行数", c, len(closes)),
        ("主平仓条数", cp, len({r["trade_id"] for r, _ in closes})),
        ("ai_scans 行数", s, written),
        ("可配对交易", vp, paired),
    ]
    for name, got, want in checks:
        flag = "✅" if got == want else "❌"
        if got != want:
            ok = False
        print(f"  {flag} {name}: {got}（期望 {want}）")

    # ⑤ 备份源文件
    if ok:
        for p in (FEATURES_FILE, SCAN_FILE):
            if os.path.exists(p):
                bak = p + ".bak"
                os.replace(p, bak)
                print(f"\n📦 已备份 {os.path.basename(p)} → {os.path.basename(bak)}")
        print("\n🎉 迁移完成，校验全部通过。")
        return 0
    print("\n⚠️ 校验未通过，源文件未备份，请检查后 --force 重来。")
    return 1


if __name__ == "__main__":
    sys.exit(main())