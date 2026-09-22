#!/usr/bin/env python3
"""
query_db.py - 归因库（trade_data.db）查看工具

用法：
    python3 query_db.py                     # 库概览（表行数 + 最近活动）
    python3 query_db.py trades [N]          # 最近 N 笔开仓（默认 10）
    python3 query_db.py closes [N]          # 最近 N 笔平仓
    python3 query_db.py vetoes [N]          # 最近 N 条被拦信号
    python3 query_db.py scans [N]           # 最近 N 轮 AI 整点扫描（按币展开）
    python3 query_db.py summary             # 归因汇总（按 regime / AI 调整 / 平仓原因）
    python3 query_db.py sql "SELECT ..."    # 任意只读 SQL

若需图形化查看，可直接用 sqlite3：
    sqlite3 scripts/trade_data.db
    sqlite> .tables
    sqlite> SELECT * FROM v_trade_attribution LIMIT 5;
"""
import os
import sys

import trade_db


def _short_ts(v) -> str:
    """时间戳截到秒，'T' 换成空格，便于目视。"""
    if not v:
        return ""
    return str(v)[:19].replace("T", " ")


def _print_table(rows: list, headers: list) -> None:
    """自适应列宽（上限 40 字符）。rows 为 dict 列表。"""
    widths = []
    for h in headers:
        vals = [len(str(r.get(h) if r.get(h) is not None else "")) for r in rows]
        widths.append(min(max([len(h)] + vals), 40))
    print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        cells = []
        for h, w in zip(headers, widths):
            s = str(r.get(h) if r.get(h) is not None else "")
            cells.append(s[:w].ljust(w))
        print("  " + "  ".join(cells).rstrip())


def cmd_overview() -> None:
    with trade_db.connect(readonly=True) as c:
        print(f"\n📦 {trade_db.DB_PATH}")
        size = os.path.getsize(trade_db.DB_PATH) / 1024
        print(f"   大小 {size:.1f} KB | journal_mode={c.execute('PRAGMA journal_mode').fetchone()[0]}\n")

        print("  表 / 视图                         行数")
        print("  ------------------------------  ------")
        for t in ("trades", "trade_closes", "ai_scans", "vetoes", "veto_outcomes"):
            n = c.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            print(f"  {t:<30}  {n:>6}")
        v = c.execute("SELECT COUNT(*) AS n FROM v_trade_attribution").fetchone()["n"]
        up = c.execute("SELECT COUNT(*) AS n FROM v_trade_attribution WHERE is_unpaired=1").fetchone()["n"]
        print(f"  {'v_trade_attribution（视图）':<30}  {v:>6}   未配对 {up}\n")

        last = c.execute("SELECT MAX(ts) AS t FROM trades").fetchone()["t"]
        lastc = c.execute("SELECT MAX(ts) AS t FROM trade_closes").fetchone()["t"]
        print(f"  最近开仓 {last}")
        print(f"  最近平仓 {lastc}")
        print()


def cmd_trades(n: int) -> None:
    with trade_db.connect(readonly=True) as c:
        rows = [dict(r) for r in c.execute(
            """SELECT trade_id, ts, symbol, direction, score_raw, ai_score_adjust,
                      regime, f_atr_pct, entry_price
               FROM trades ORDER BY ts DESC LIMIT ?""", (n,))]
    if not rows:
        print("（暂无数据）")
        return
    for r in rows:
        r["ts"] = _short_ts(r["ts"])
    print(f"\n最近 {len(rows)} 笔开仓：\n")
    _print_table(rows, ["ts", "symbol", "direction", "score_raw", "ai_score_adjust",
                        "regime", "f_atr_pct", "entry_price"])
    print()


def cmd_closes(n: int) -> None:
    with trade_db.connect(readonly=True) as c:
        rows = [dict(r) for r in c.execute(
            """SELECT trade_id, ts, pnl_usdt, pnl_pct, reason, is_primary
               FROM trade_closes ORDER BY ts DESC LIMIT ?""", (n,))]
    if not rows:
        print("（暂无数据）")
        return
    for r in rows:
        r["ts"] = _short_ts(r["ts"])
    print(f"\n最近 {len(rows)} 笔平仓：\n")
    _print_table(rows, ["ts", "trade_id", "reason", "pnl_usdt", "pnl_pct", "is_primary"])
    print()


def cmd_vetoes(n: int) -> None:
    with trade_db.connect(readonly=True) as c:
        rows = [dict(r) for r in c.execute(
            """SELECT v.ts, v.symbol, v.direction, v.score_raw, v.ai_direction,
                      v.veto_reason, v.price, o.price_1h
               FROM vetoes v LEFT JOIN veto_outcomes o ON o.veto_id = v.id
               ORDER BY v.ts DESC LIMIT ?""", (n,))]
    if not rows:
        print("（暂无数据 —— AI 观望拦截触发后才有记录，1h 前瞻价由整点扫描回填）")
        return
    for r in rows:
        r["ts"] = _short_ts(r["ts"])
    print(f"\n最近 {len(rows)} 条被拦信号：\n")
    _print_table(rows, ["ts", "symbol", "direction", "score_raw", "ai_direction",
                        "veto_reason", "price", "price_1h"])
    print()


def cmd_scans(n: int) -> None:
    with trade_db.connect(readonly=True) as c:
        rows = [dict(r) for r in c.execute(
            """SELECT ts, symbol, price, direction, confidence
               FROM ai_scans ORDER BY ts DESC LIMIT ?""", (n,))]
    if not rows:
        print("（暂无数据）")
        return
    for r in rows:
        r["ts"] = _short_ts(r["ts"])
    print(f"\n最近 {len(rows)} 条整点扫描（按币展开）：\n")
    _print_table(rows, ["ts", "symbol", "price", "direction", "confidence"])
    print()


def cmd_summary() -> None:
    with trade_db.connect(readonly=True) as c:
        print("\n【按 regime】")
        for r in c.execute(
            """SELECT COALESCE(regime,'?') AS k, COUNT(*) AS n,
                      ROUND(AVG(pnl_pct)*100, 3) AS avg_pct,
                      ROUND(SUM(pnl_usdt), 2) AS sum_u
               FROM v_trade_attribution WHERE is_unpaired=0 GROUP BY k ORDER BY n DESC"""):
            print(f"  {str(r['k']):<10} {r['n']:>4} 笔  均单 {r['avg_pct']:>7.3f}%  累计 {r['sum_u']:>8.2f}U")

        print("\n【按 AI 一致性调整】")
        for r in c.execute(
            """SELECT COALESCE(ai_score_adjust,0) AS k, COUNT(*) AS n,
                      ROUND(AVG(pnl_pct)*100, 3) AS avg_pct,
                      ROUND(SUM(pnl_usdt), 2) AS sum_u
               FROM v_trade_attribution WHERE is_unpaired=0 GROUP BY k ORDER BY k DESC"""):
            label = {5: "+5 同向", 0: " 0 无AI", -3: "-3 反向"}.get(r["k"], str(r["k"]))
            print(f"  {label:<10} {r['n']:>4} 笔  均单 {r['avg_pct']:>7.3f}%  累计 {r['sum_u']:>8.2f}U")

        print("\n【按平仓原因】")
        for r in c.execute(
            """SELECT COALESCE(close_reason,'?') AS k, COUNT(*) AS n,
                      ROUND(AVG(pnl_pct)*100, 3) AS avg_pct,
                      ROUND(SUM(pnl_usdt), 2) AS sum_u
               FROM v_trade_attribution WHERE is_unpaired=0 GROUP BY k ORDER BY n DESC"""):
            print(f"  {str(r['k']):<10} {r['n']:>4} 笔  均单 {r['avg_pct']:>7.3f}%  累计 {r['sum_u']:>8.2f}U")
        print()


def cmd_sql(query: str) -> None:
    q = query.strip()
    if not q.lower().startswith(("select", "pragma", "with")):
        print("❌ 只允许只读查询（SELECT / PRAGMA / WITH）")
        return
    with trade_db.connect(readonly=True) as c:
        rows = c.execute(q).fetchall()
    if not rows:
        print("（无结果）")
        return
    headers = list(rows[0].keys())
    rows = [dict(r) for r in rows]
    _print_table(rows, headers)


def main() -> int:
    if not os.path.exists(trade_db.DB_PATH):
        print(f"❌ 归因库不存在：{trade_db.DB_PATH}")
        return 1

    args = sys.argv[1:]
    cmd = args[0] if args else "overview"
    rest = args[1:]

    def num(default: int) -> int:
        try:
            return int(rest[0])
        except (IndexError, ValueError):
            return default

    if cmd == "overview":
        cmd_overview()
    elif cmd == "trades":
        cmd_trades(num(10))
    elif cmd == "closes":
        cmd_closes(num(10))
    elif cmd == "vetoes":
        cmd_vetoes(num(10))
    elif cmd == "scans":
        cmd_scans(num(7))
    elif cmd == "summary":
        cmd_summary()
    elif cmd == "sql":
        if not rest:
            print("用法：python3 query_db.py sql \"SELECT ...\"")
            return 1
        cmd_sql(" ".join(rest))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())