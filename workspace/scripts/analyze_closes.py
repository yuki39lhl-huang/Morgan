#!/usr/bin/env python3
"""
analyze_closes.py — 平仓归因分析（诊断盈亏比）

数据源：当前 monitor.log + 归档日志（trading/archive/trading_*.log）。
解析每笔「平仓成功」事件，按平仓原因分组统计：
  - 总体：样本数 / 胜率 / 总盈利 / 总亏损 / 盈亏比 / 期望值
  - 按 reason（TP1 / SL / 迷你仓修复 / 其他）分组
  - SL 拆分：初始止损（亏损）vs 移动止盈保护（盈利）——两者混在同一个 reason 里
    正是盈亏比被拉低的头号嫌疑，必须拆开看。

解耦设计：
  - generate_report(days, sl) 返回纯文本（供 CLI / weekly_report 复用，不依赖 stdout）
  - 本模块不依赖 feishu 等推送层，推送由 weekly_report.py（装配层）完成

用法：python3 analyze_closes.py [--days N] [--sl]  默认统计全部历史。
"""
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

# 平仓成功日志格式：
# [时间] [INFO] {emoji} 平仓 {symbol} {reason} | 入场:{e} 出场:{x} | PnL:{pct}% ({usdt}USDT) | 平{r}%仓 | ✅ 平仓成功
CLOSE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[INFO\] [✅❌] 平仓 (\S+) (\S+)"
    r" \| 入场:([\d.]+) 出场:([\d.]+)"
    r" \| PnL:([+-]?[\d.]+)% \(([+-]?[\d.]+)USDT\) \| 平[\d.]+%仓 \| ✅ 平仓成功"
)

# 开仓成功日志格式（止损诊断用，需关联开仓评分/regime）：
# [时间] [INFO] 🟢 开多 SOL @73.94 | 评分:80 | 状态:trending | AI:做多(0.62%) | ✅ 下单成功
OPEN_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[INFO\] [🟢🔴] 开(多|空) (\S+) @([\d.]+)"
    r" \| 评分:(\d+) \| 状态:(\S+) \| AI:[^|]* \| ✅ 下单成功"
)

# 迷你仓修复 / 未成交恢复 等 PnL≈0 的噪声平仓，从盈亏统计中剔除（单独计数）
NOISE_REASONS = ("迷你仓修复", "未成交")

# 评分阈值用于低分占比统计（对齐 config.json score_threshold.trending=70）
SCORE_LOW_BOUND = 70


class _Sink:
    """报告文本收集器：函数内用 out() 代替 print()，最终统一返回文本。"""

    def __init__(self):
        self._lines: list[str] = []

    def __call__(self, *args):
        self._lines.append(" ".join(str(a) for a in args))

    def text(self) -> str:
        return "\n".join(self._lines) + "\n"


def iter_log_files():
    """按时间从旧到新返回日志文件路径：归档 + 当前。"""
    archive_dir = config.LOG_ROOT / "trading" / "archive"
    files = []
    if archive_dir.is_dir():
        files.extend(sorted(archive_dir.glob("trading_*.log")))
    current = config.LOG_ROOT / "trading" / "monitor.log"
    if current.exists():
        files.append(current)
    return files


def parse_logs(days: int | None):
    """解析全部日志，返回 (平仓列表, 开仓列表)。"""
    records, opens = [], []
    cutoff = datetime.now() - timedelta(days=days) if days else None
    for path in iter_log_files():
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = CLOSE_RE.search(line)
                    if m:
                        ts, symbol, reason = m.group(1), m.group(2), m.group(3)
                        pnl_pct, pnl_usdt = float(m.group(6)), float(m.group(7))
                        if cutoff:
                            try:
                                if datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") < cutoff:
                                    continue
                            except ValueError:
                                pass
                        records.append((ts, symbol, reason, pnl_pct, pnl_usdt))
                        continue
                    o = OPEN_RE.search(line)
                    if o:
                        opens.append((o.group(1), o.group(3), int(o.group(5)), o.group(6)))
        except OSError as e:
            print(f"⚠️ 读取 {path} 失败: {e}")
    return records, opens


def classify(reason: str, pnl_usdt: float) -> str:
    """归并 reason：TP1 / SL亏损(初始止损) / SL盈利(移动止盈保护) / 噪声"""
    if any(reason.startswith(r) for r in NOISE_REASONS):
        return "噪声(迷你仓修复等)"
    if reason.startswith("TP"):
        return "TP1 止盈"
    if reason.startswith("SL"):
        return "SL止损" if pnl_usdt < 0 else "SL保护(移动止盈落袋)"
    return f"其他({reason})"


def _link_open(opens: list, ts: str, symbol: str):
    """关联开仓：返回时间 <= ts 的最近一次同 symbol 开仓 (ts, sym, score, regime)。"""
    best = None
    for o in opens:
        if o[1] == symbol and o[0] <= ts:
            if best is None or o[0] > best[0]:
                best = o
    return best


def diagnose_sl(records: list, opens: list, days: int | None) -> str:
    """S1 止损端诊断：SL止损单 vs 盈利单的开仓评分/regime 对比。"""
    out = _Sink()
    trades = [t for t in records if not any(t[2].startswith(r) for r in NOISE_REASONS)]
    groups = defaultdict(list)  # reason组 -> [(score, regime)]
    no_open = 0
    for ts, sym, reason, pct, usdt in trades:
        o = _link_open(opens, ts, sym)
        if not o:
            no_open += 1
            continue
        groups[classify(reason, usdt)].append((o[2], o[3]))

    out("# 止损端诊断（Phase 0.5 S1）")
    out(f"> 关联平仓 {sum(len(v) for v in groups.values())}/{len(trades)} 笔（{no_open} 笔无开仓记录）\n")

    def _stats(items):
        scores = [s for s, _ in items]
        med = statistics.median(scores) if scores else 0
        low = sum(1 for s in scores if s < SCORE_LOW_BOUND) / len(scores) * 100 if scores else 0
        regimes = defaultdict(int)
        for _, r in items:
            regimes[r] += 1
        reg = " ".join(f"{k}:{v}" for k, v in sorted(regimes.items()))
        return len(scores), sum(scores) / len(scores) if scores else 0, med, low, reg

    out("## 一、按平仓类型对比开仓评分")
    out("| 平仓类型 | 次数 | 平均开仓评分 | 中位评分 | 低分(<70)占比 | regime分布 |")
    out("|----------|------|--------------|----------|---------------|------------|")
    for name in ("SL止损", "SL保护(移动止盈落袋)", "TP1 止盈", "其他"):
        items = groups.get(name, [])
        if not items:
            continue
        n, avg, med, low, reg = _stats(items)
        out(f"| {name} | {n} | {avg:.1f} | {med:.0f} | {low:.0f}% | {reg} |")

    sl_loss = groups.get("SL止损", [])
    winners = groups.get("TP1 止盈", []) + groups.get("SL保护(移动止盈落袋)", [])
    if not sl_loss or not winners:
        out("\n样本不足，无法判断。")
        return out.text()

    avg_sl = sum(s for s, _ in sl_loss) / len(sl_loss)
    avg_win = sum(s for s, _ in winners) / len(winners)
    diff = avg_sl - avg_win
    out("\n## 二、判断")
    out(f"SL止损单平均开仓评分 **{avg_sl:.1f}** vs 盈利单 **{avg_win:.1f}**，差 {diff:+.1f} 分")
    if diff <= -10:
        out("→ **入场信号弱**：止损单开仓评分显著低于盈利单，应先提高开仓阈值/加过滤（score_threshold 或 per_symbol_bonus），再谈止损距离。")
    elif diff >= 10:
        out("→ **止损单评分反而更高**：高评分单也频繁止损，说明止损距离/ATR 设置与行情不匹配，优先检查 atr_sl_tiers。")
    else:
        out("→ **止损距离问题**：两类单开仓评分相当，止损被触发的差异来自止损距离本身，优先调 atr_sl_tiers（止损倍数）或入场时的 ATR 过滤。")
    out(f"\n（未关联到开仓记录的平仓 {no_open} 笔，多为历史旧日志无开仓行）")
    return out.text()


def compute_summary(days: int | None = None) -> dict:
    """纯计算：返回结构化统计 dict（渲染由调用方决定，CLI/飞书卡片共用同一份数据）。"""
    records, _ = parse_logs(days)
    if not records:
        return {"n": 0}
    trades = [(ts, sym, reason, pct, usdt) for ts, sym, reason, pct, usdt in records
              if not any(reason.startswith(r) for r in NOISE_REASONS)]
    wins = [t for t in trades if t[4] > 0]
    losses = [t for t in trades if t[4] < 0]
    gross_win = sum(t[4] for t in wins)
    gross_loss = abs(sum(t[4] for t in losses))
    n = len(trades)
    plr = gross_win / gross_loss if gross_loss > 0 else float("inf")
    exp = (gross_win - gross_loss) / n if n else 0

    by_reason = defaultdict(list)
    for t in trades:
        by_reason[classify(t[2], t[4])].append(t)
    reason_rows = []
    for reason, group in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        g_wins = sum(1 for t in group if t[4] > 0)
        reason_rows.append({
            "name": reason,
            "count": len(group),
            "pct": len(group) / n * 100,
            "avg_pct": sum(t[3] for t in group) / len(group),
            "sum": sum(t[4] for t in group),
            "win_rate": g_wins / len(group) * 100,
        })

    sl_loss = by_reason.get("SL止损", [])
    sl_protect = by_reason.get("SL保护(移动止盈落袋)", [])
    p_sum = sum(t[4] for t in sl_protect)
    sl_protect_avg = sum(t[3] for t in sl_protect) / len(sl_protect) if sl_protect else 0

    if plr == float("inf"):
        conclusion = "暂无亏损单。"
    elif plr >= 2.0:
        conclusion = f"盈亏比 {plr:.2f} 达标（≥2:1），无需调整。"
    else:
        biggest = max(by_reason.items(), key=lambda kv: abs(sum(t[4] for t in kv[1])) if sum(t[4] for t in kv[1]) < 0 else 0)
        lines = [f"盈亏比 {plr:.2f} < 2:1 设计目标，主要拖累来自："]
        if biggest[1]:
            lines.append(f"- **{biggest[0]}** 合计 {sum(t[4] for t in biggest[1]):+.2f} USDT，是最大亏损来源")
        lines.append("- 若'SL保护'占比高，优先调整 trailing_tiers（放松回撤 gap 让利润多跑）")
        conclusion = "\n".join(lines)

    return {
        "n": n,
        "noise_count": len(records) - n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n * 100 if n else 0,
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "plr": plr,
        "expected": exp,
        "reason_rows": reason_rows,
        "sl_loss": {"count": len(sl_loss), "sum": sum(t[4] for t in sl_loss)},
        "sl_protect": {"count": len(sl_protect), "sum": p_sum, "avg_pct": sl_protect_avg},
        "conclusion": conclusion,
    }


def generate_summary(days: int | None = None) -> dict:
    """结构化周报数据（供 weekly_report 卡片渲染，不含任何展示层依赖）。"""
    return compute_summary(days)


def generate_report(days: int | None = None, sl: bool = False) -> str:
    """生成归因报告 markdown 文本（CLI 用，数据与 compute_summary 单源）。"""
    if sl:
        records, opens = parse_logs(days)
        if not records:
            return "未解析到任何平仓成功记录。\n"
        return diagnose_sl(records, opens, days)

    s = compute_summary(days)
    if s["n"] == 0:
        return "未解析到任何平仓成功记录。\n"

    out = _Sink()
    out("# 平仓归因报告")
    out(f"> 统计范围：{'全部历史' if not days else f'近 {days} 天'} · 日志文件 {len(iter_log_files())} 份 · {datetime.now():%Y-%m-%d %H:%M}\n")

    # ── 总体 ──
    out("## 一、总体盈亏结构（剔除迷你仓修复等噪声）")
    out("| 指标 | 值 |")
    out("|------|----|")
    out(f"| 有效平仓样本 | {s['n']} 笔（另有 {s['noise_count']} 笔噪声平仓已剔除） |")
    out(f"| 盈利单 / 亏损单 | {s['wins']} / {s['losses']} |")
    out(f"| 胜率 | {s['win_rate']:.1f}%")
    out(f"| 总盈利 / 总亏损 | +{s['gross_win']:.2f} / -{s['gross_loss']:.2f} USDT |")
    plr_txt = f"{s['plr']:.2f}" if s["plr"] != float("inf") else "∞"
    out(f"| **盈亏比** | **{plr_txt}** |")
    out(f"| 平均期望 / 笔 | {s['expected']:+.3f} USDT |")

    # ── 按原因 ──
    out("\n## 二、按平仓原因分组")
    out("| 原因 | 次数 | 占比 | 平均PnL% | 总盈亏(USDT) | 组内胜率 |")
    out("|------|------|------|----------|--------------|----------|")
    for r in s["reason_rows"]:
        out(f"| {r['name']} | {r['count']} | {r['pct']:.0f}% | {r['avg_pct']:+.2f}% | {r['sum']:+.2f} | {r['win_rate']:.0f}% |")

    # ── SL 拆分诊断 ──
    out("\n## 三、SL 拆分诊断（关键）")
    out("**移动止盈把 SL 上移后，触发平仓时 reason 仍记为 SL。**")
    out("| 类型 | 次数 | 总盈亏 | 说明 |")
    out("|------|------|--------|------|")
    out(f"| 初始止损（真亏损） | {s['sl_loss']['count']} | {s['sl_loss']['sum']:+.2f} USDT | 价格直接触底 |")
    out(f"| 保护性止损（盈利落袋） | {s['sl_protect']['count']} | {s['sl_protect']['sum']:+.2f} USDT | 浮盈回吐后被移动止盈收割 |")
    if s["sl_protect"]["count"]:
        out(f"\n**利润回吐估算**：{s['sl_protect']['count']} 笔保护性止损平均 PnL {s['sl_protect']['avg_pct']:+.2f}%，"
            f"而 TP1 设计目标约 +8%。若移动止盈更宽松，这些单本可贡献更多利润。")

    # ── 结论 ──
    out("\n## 四、结论")
    out(s["conclusion"])
    return out.text()


def main():
    days = None
    if len(sys.argv) > 2 and sys.argv[1] == "--days":
        days = int(sys.argv[2])
    sl = "--sl" in sys.argv
    sys.stdout.write(generate_report(days=days, sl=sl))


if __name__ == "__main__":
    main()
