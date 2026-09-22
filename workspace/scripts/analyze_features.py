#!/usr/bin/env python3
"""
analyze_features.py — 特征归因周报（量化升级 Phase 1 / Phase 2.6 迁移至 SQLite）

数据源：trade_data.db（见 trade_db.py）
  trades          开仓特征 + AI 观点快照
  trade_closes    平仓盈亏（主平仓唯一，残余仓清理不参与统计）
  ai_scans        整点 AI 观点 + 当时价格，用于 AI 命中率（对比 1h 后走势）
  vetoes          被 AI 拦截的信号 + 反事实价格（Phase 2.6 新增）

输出：Markdown 报告（stdout，或 --out 写入文件）
用法：
  python3 analyze_features.py                  # 打印报告
  python3 analyze_features.py --out 周报.md    # 写入文件
"""
import argparse
import os
from collections import defaultdict
from datetime import datetime

import trade_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────
# 数据加载
# ─────────────────────────────────────────────
def load_trades() -> tuple[list, int]:
    """读归因视图，一笔交易一行主平仓结果。返回 (已平仓交易, 未平仓数)。"""
    if not os.path.exists(trade_db.DB_PATH):
        return [], 0
    with trade_db.connect(readonly=True) as conn:
        rows = conn.execute("SELECT * FROM v_trade_attribution ORDER BY ts").fetchall()

    trades, pending = [], 0
    for r in rows:
        d = dict(r)
        if d.get("is_unpaired"):
            pending += 1
            continue
        # 还原为旧版 JSONL 的嵌套结构，避免下游统计逻辑改动
        d["features"] = {
            "breakout": d.pop("f_breakout", None),
            "volume_ratio": d.pop("f_volume_ratio", None),
            "atr_pct": d.pop("f_atr_pct", None),
            "btc_bull": d.pop("f_btc_bull", None),
            "sentiment": d.pop("f_sentiment", None),
        }
        d["ai"] = {
            "direction": d.pop("ai_direction", None),
            "confidence": d.pop("ai_confidence", None),
        }
        d["pnl_usdt"] = float(d.get("pnl_usdt") or 0)
        d["pnl_pct"] = float(d.get("pnl_pct") or 0)
        d["reason"] = d.get("close_reason") or ""
        trades.append(d)
    return trades, pending


def load_ai_scans() -> list:
    """读 ai_scans 表，按整点时间戳还原为 {ts, fg, prices, scan} 结构供命中率计算。"""
    if not os.path.exists(trade_db.DB_PATH):
        return []
    with trade_db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT ts, symbol, fg, price, direction, confidence, reason FROM ai_scans ORDER BY ts"
        ).fetchall()

    by_ts = {}
    for r in rows:
        rec = by_ts.setdefault(r["ts"], {"ts": r["ts"], "fg": r["fg"], "prices": {}, "scan": {}})
        if r["price"] is not None:
            rec["prices"][r["symbol"]] = r["price"]
        rec["scan"][r["symbol"]] = {
            "direction": r["direction"],
            "confidence": r["confidence"],
            "reason": r["reason"],
        }
    return [by_ts[k] for k in sorted(by_ts)]


# ─────────────────────────────────────────────
# 统计工具
# ─────────────────────────────────────────────
def stats(rows: list) -> dict:
    """rows: [{pnl_pct, pnl_usdt}] → 胜率/盈亏比/期望值。样本过少返回 None。"""
    if len(rows) < 1:
        return None
    wins   = [r["pnl_pct"] for r in rows if r["pnl_pct"] > 0]
    losses = [r["pnl_pct"] for r in rows if r["pnl_pct"] <= 0]
    win_rate = len(wins) / len(rows)
    avg_win  = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    plr = (avg_win / abs(avg_loss)) if avg_loss != 0 else (float("inf") if avg_win > 0 else 0.0)
    return {
        "n":        len(rows),
        "win_rate": win_rate,
        "avg_win":  avg_win,
        "avg_loss": avg_loss,
        "plr":      plr,
        "exp_pct":  sum(r["pnl_pct"] for r in rows) / len(rows),
        "exp_usdt": sum(r["pnl_usdt"] for r in rows) / len(rows),
        "profit":   sum(1 for r in rows if r["pnl_usdt"] > 0),
        "loss":     sum(1 for r in rows if r["pnl_usdt"] <= 0),
    }


def stats_row(s: dict) -> str:
    """统计 → markdown 表格行。"""
    if s is None:
        return "`样本不足` | - | - | - | -"
    plr = f"{s['plr']:.2f}" if s["plr"] != float("inf") else "∞"
    return (f"{s['n']} | {s['win_rate']*100:.1f}% | {plr} | "
            f"{s['exp_pct']*100:+.2f}% | {s['exp_usdt']:+.2f}U")


# ─────────────────────────────────────────────
# 特征区间 vs 胜率
# ─────────────────────────────────────────────
def bucket_report(trades: list) -> str:
    """各特征区间 vs 胜率/盈亏比/期望值。"""
    get_feat = lambda t, key: (t.get("features") or {}).get(key)

    buckets = [
        ("突破分",      get_feat, "breakout",     [("0（无突破）", lambda v: v == 0), ("40（突破）", lambda v: v == 40)]),
        ("量能倍数",    get_feat, "volume_ratio", [("<1.2", lambda v: v < 1.2), ("1.2~1.5", lambda v: 1.2 <= v < 1.5), ("≥1.5", lambda v: v >= 1.5)]),
        ("波动率ATR%",  get_feat, "atr_pct",      [("<1%", lambda v: v < 0.01), ("1%~2%", lambda v: 0.01 <= v < 0.02), ("2%~3%", lambda v: 0.02 <= v < 0.03), ("≥3%", lambda v: v >= 0.03)]),
        ("BTC方向",     get_feat, "btc_bull",     [("看跌(0)", lambda v: v == 0), ("看涨(1)", lambda v: v == 1)]),
        ("情绪加分",    get_feat, "sentiment",    [("0", lambda v: v == 0), ("5", lambda v: v == 5), ("10", lambda v: v == 10), ("15", lambda v: v == 15)]),
    ]

    lines = []
    for title, accessor, key, group_list in buckets:
        lines.append(f"### {title}")
        lines.append("")
        lines.append("| 区间 | 样本 | 胜率 | 盈亏比 | 期望值/笔 |")
        lines.append("|------|------|------|--------|-----------|")
        for label, pred in group_list:
            sub = [t for t in trades if accessor(t, key) is not None and pred(accessor(t, key))]
            lines.append(f"| {label} | {stats_row(stats(sub))} |")
        lines.append("")

    # 总分段
    lines.append("### 触发评分（score）")
    lines.append("")
    lines.append("| 区间 | 样本 | 胜率 | 盈亏比 | 期望值/笔 |")
    lines.append("|------|------|------|--------|-----------|")
    for label, pred in [
        ("<70", lambda v: v < 70), ("70~79", lambda v: 70 <= v < 80),
        ("80~89", lambda v: 80 <= v < 90), ("≥90", lambda v: v >= 90),
    ]:
        sub = [t for t in trades if t.get("score") is not None and pred(t.get("score"))]
        lines.append(f"| {label} | {stats_row(stats(sub))} |")
    lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# AI 观点命中率（整点扫描 vs 1h 走势）
# ─────────────────────────────────────────────
def compute_ai_hit(scans: list = None) -> dict:
    """AI 观点命中率纯计算（渲染由调用方决定：CLI / 飞书卡片共用一份数据）。

    返回：{"n": 有效样本数, "groups": [{"name", "total", "hit", "rate"|None}]}
    """
    if scans is None:
        scans = load_ai_scans()
    stats_map = defaultdict(lambda: {"total": 0, "hit": 0})
    total = 0
    for i, rec in enumerate(scans):
        scan   = rec.get("scan") or {}
        prices = rec.get("prices") or {}
        for sym, v in scan.items():
            p_now = prices.get(sym)
            if not p_now or p_now <= 0:
                continue
            p_next = None
            for nxt in scans[i + 1:]:
                np_ = (nxt.get("prices") or {}).get(sym)
                if np_:
                    p_next = np_
                    break
            if not p_next or p_next <= 0:
                continue
            direction = v.get("direction", "")
            conf      = v.get("confidence", 0)
            chg = (p_next - p_now) / p_now
            if direction == "LONG":
                hit = chg > 0
            elif direction == "SHORT":
                hit = chg < 0
            elif direction == "震荡":
                hit = abs(chg) < 0.003
            else:
                continue
            total += 1
            stats_map["全部"]["total"] += 1
            if hit:
                stats_map["全部"]["hit"] += 1
            stats_map[direction]["total"] += 1
            if hit:
                stats_map[direction]["hit"] += 1
            conf_bucket = "高(≥0.65)" if conf >= 0.65 else ("中(0.60~0.65)" if conf >= 0.60 else "低(<0.60)")
            stats_map[f"置信度 {conf_bucket}"]["total"] += 1
            if hit:
                stats_map[f"置信度 {conf_bucket}"]["hit"] += 1

    groups = []
    for label in ("全部", "LONG", "SHORT", "震荡", "置信度 高(≥0.65)", "置信度 中(0.60~0.65)", "置信度 低(<0.60)"):
        s = stats_map.get(label)
        if not s or s["total"] == 0:
            groups.append({"name": label, "total": 0, "hit": 0, "rate": None})
        else:
            groups.append({"name": label, "total": s["total"], "hit": s["hit"], "rate": s["hit"] / s["total"]})
    return {"n": total, "groups": groups}


def ai_hit_rate(scans: list) -> tuple[str, int]:
    """对比整点 AI 观点与下一整点实际价格方向。返回 (markdown, 样本数)。"""
    data = compute_ai_hit(scans)
    lines = ["| 分组 | 样本 | 命中 | 命中率 |", "|------|------|------|--------|"]
    for g in data["groups"]:
        if g["total"] == 0:
            lines.append(f"| {g['name']} | - | - | - |")
        else:
            lines.append(f"| {g['name']} | {g['total']} | {g['hit']} | {g['rate']*100:.1f}% |")
    return "\n".join(lines), data["n"]


def load_vetoes() -> list:
    """读被拦信号 + 已回填的前瞻价格（Phase 2.6）。"""
    if not os.path.exists(trade_db.DB_PATH):
        return []
    with trade_db.connect(readonly=True) as conn:
        rows = conn.execute(
            """SELECT v.*, o.price_1h, o.price_4h, o.price_24h
               FROM vetoes v
               LEFT JOIN veto_outcomes o ON o.veto_id = v.id
               ORDER BY v.ts"""
        ).fetchall()
    return [dict(r) for r in rows]


def veto_report(vetoes: list) -> str:
    """被拦信号的反事实评估：若按数学信号方向开仓会怎样。

    这是 Phase 2.6 的核心产出 —— AI 干预交易的唯一路径是观望拦截，
    此前被拦信号零记录，「AI 到底有没有用」无法回答。
    """
    if not vetoes:
        return "暂无否决记录（Phase 2.6 上线后开始积累；1h 前瞻价格由整点扫描回填）。"

    by_reason = defaultdict(list)
    for v in vetoes:
        by_reason[v["veto_reason"]].append(v)

    out = ["| 否决原因 | 次数 | 已回填 | 若开仓的 1h 胜率 | 平均 1h 收益 |",
           "|----------|------|--------|------------------|--------------|"]
    for reason, vs in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        rets = []
        for v in vs:
            p0, p1, d = v.get("price"), v.get("price_1h"), v.get("direction")
            if not p0 or not p1 or d not in ("LONG", "SHORT"):
                continue
            chg = (p1 - p0) / p0
            rets.append(chg if d == "LONG" else -chg)
        if rets:
            win = sum(1 for r in rets if r > 0)
            out.append(f"| {reason} | {len(vs)} | {len(rets)} | "
                       f"{win / len(rets) * 100:.1f}% | {sum(rets) / len(rets) * 100:+.3f}% |")
        else:
            out.append(f"| {reason} | {len(vs)} | 0 | - | - |")
    out.append("")
    out.append("> 「若开仓的 1h 胜率」偏高 = AI 拦错了（错失机会）；偏低 = 拦对了。"
               "样本 <30 时仅供参考。")
    return "\n".join(out)


# ─────────────────────────────────────────────
# 报告主流程
# ─────────────────────────────────────────────
def build_report() -> str:
    trades, pending = load_trades()
    closed = [t for t in trades if t.get("reason") != "下单失败"]
    scans  = load_ai_scans()
    vetoes = load_vetoes()
    now    = datetime.now().strftime("%Y-%m-%d %H:%M")

    out = [f"# 特征归因周报（Phase 1 / Phase 2.6）", f"", f"> 生成时间：{now}", ""]

    # 一、样本总览
    out.append("## 一、样本总览")
    out.append("")
    out.append("| 指标 | 值 |")
    out.append("|------|----|")
    s = stats(closed)
    if s:
        out.append(f"| 已平仓样本 | {s['n']} 笔（盈 {s['profit']} / 亏 {s['loss']}） |")
        out.append(f"| 胜率 | {s['win_rate']*100:.1f}% |")
        out.append(f"| 平均盈利 / 平均亏损 | {s['avg_win']*100:+.2f}% / {s['avg_loss']*100:+.2f}% |")
        plr = f"{s['plr']:.2f}" if s["plr"] != float("inf") else "∞"
        out.append(f"| 盈亏比 | {plr} |")
        out.append(f"| 期望值 | {s['exp_pct']*100:+.3f}% / 笔（{s['exp_usdt']:+.3f}U） |")
    else:
        out.append("| 已平仓样本 | 0（特征记录刚上线，尚未积累） |")
    out.append(f"| 未平仓在持 | {pending} 笔 |")
    out.append(f"| AI 扫描行 | {len(scans)} 行（ai_scans 表） |")
    out.append(f"| 被拦信号 | {len(vetoes)} 条（vetoes 表） |")
    out.append("")

    # 二、特征区间
    out.append("## 二、特征区间 vs 胜率")
    out.append("")
    if not closed:
        out.append("暂无已平仓样本。")
        out.append("")
    else:
        out.append(bucket_report(closed))
        out.append("")

    # 三、AI 命中率
    out.append("## 三、AI 观点命中率（整点扫描 vs 1h 实际走势）")
    out.append("")
    if not scans:
        out.append("暂无 AI 扫描数据。")
    else:
        ai_tbl, ai_n = ai_hit_rate(scans)
        out.append(ai_tbl)
        out.append("")
        if ai_n < 20:
            out.append(f"> ⏳ AI 命中率样本仅 {ai_n} 个，需积累（≥20 才有参考价值，≥100 结论可靠）。")
            out.append("")

    # 四、被拦信号反事实（Phase 2.6）
    out.append("## 四、被拦信号反事实（AI 观望拦截）")
    out.append("")
    out.append(veto_report(vetoes))
    out.append("")

    # 五、结论与建议
    out.append("## 五、结论与建议")
    out.append("")
    if not closed and not scans:
        out.append("数据尚未积累。开仓成功后写入 `trade_data.db` 的 trades 表，`analyze_features.py` 每周运行一次。")
    elif len(closed) < 30:
        out.append(f"- 已平仓样本 {len(closed)} 笔 < 30，统计仅有参考意义，继续积累数据。")
        out.append("- 达到 200+ 笔后输出第一份**正式特征归因报告**（Phase 1 验收线）。")
    else:
        out.append("- 样本已达 30+ 笔，可观察特征区间之间的胜率/盈亏比差异。")
        out.append("- 建议对比「突破分 40」与「无突破」、以及 AI 高置信度组的命中率。")
    out.append("")

    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description="特征归因周报（Phase 1）")
    parser.add_argument("--out", "-o", help="输出文件路径（默认打印到 stdout）")
    args = parser.parse_args()

    report = build_report()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"✅ 周报已写入 {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
