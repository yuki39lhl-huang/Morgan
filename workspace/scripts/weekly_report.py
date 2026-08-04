#!/usr/bin/env python3
"""
weekly_report.py — 量化周报自动推送（Phase 0.5 S3 验证工具）

装配层（薄壳）：analyze_closes 结构化数据 → 飞书卡片（布局对齐整点汇报）。

职责分离：
  - 分析计算在 analyze_closes.compute_summary（纯数据，无渲染/推送依赖）
  - 卡片渲染在本文件（结构化数据 → feishu_helper 原语）
  - 推送原语在 feishu_helper.py（统一飞书通道，密钥集中 secrets.json）
  - 参数全部外置 config.json

调度：weekly_scheduler.py 每周一 09:00 触发（常驻进程，替代不可用的 crontab）
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import analyze_closes
import analyze_features
import feishu_helper


def _hit_field(name: str, g: dict) -> dict:
    """AI 命中率分组 → 卡片字段（无样本显示 -）。"""
    if not g.get("total"):
        return feishu_helper.field(name, "-")
    return feishu_helper.field(name, f"{g['total']}样本 · {g['rate']*100:.1f}%")


def build_elements(s: dict, days: int, ai: dict = None) -> list:
    """把结构化统计渲染为飞书卡片元素（布局对齐整点汇报）。"""
    if not s.get("n"):
        return [
            feishu_helper.md("暂无平仓样本，继续观察。"),
            feishu_helper.note(f"⏰ {datetime.now():%Y-%m-%d %H:%M} · 统计窗口近 {days} 天 (UTC+8)"),
        ]

    plr_txt = f"{s['plr']:.2f}" if s["plr"] != float("inf") else "∞"
    e = []

    # 顶部摘要网格（2 列）
    e.append(feishu_helper.kv_block([
        ("有效平仓", f"{s['n']} 笔"),
        ("胜率", f"{s['win_rate']:.1f}%"),
        ("总盈利 / 总亏损", f"+{s['gross_win']:.2f} / -{s['gross_loss']:.2f} U"),
        ("平均期望 / 笔", f"{s['expected']:+.3f} U"),
    ]))
    e.append(feishu_helper.hr())

    # 盈亏比重点
    ok = "✅ 达标（≥2:1）" if s["plr"] >= 2.0 else "⚠️ 未达标（<2:1）"
    e.append(feishu_helper.md(f"**📈 盈亏比：{plr_txt}** {ok}"))
    e.append(feishu_helper.md(f"盈利 {s['wins']} 单 / 亏损 {s['losses']} 单"
                              f"（另有 {s['noise_count']} 笔噪声平仓已剔除）"))
    e.append(feishu_helper.hr())

    # 按平仓原因（两列分栏）
    e.append(feishu_helper.md("**📊 按平仓原因**"))
    rows_ = s["reason_rows"]
    for i in range(0, len(rows_), 2):
        pair = []
        for r in rows_[i:i + 2]:
            sign = "+" if r["sum"] >= 0 else ""
            pair.append(feishu_helper.field(
                f"{r['name']} · {r['count']}笔 · 胜率{r['win_rate']:.0f}%",
                f"{sign}{r['sum']:.2f}U · 平均{r['avg_pct']:+.2f}%",
            ))
        e.append(feishu_helper.row(*pair))
    e.append(feishu_helper.hr())

    # SL 拆分（关键诊断）
    e.append(feishu_helper.md("**🔍 SL 拆分**（移动止盈落袋 vs 初始止损）"))
    e.append(feishu_helper.row(
        feishu_helper.field("初始止损（真亏损）", f"{s['sl_loss']['count']}笔 · {s['sl_loss']['sum']:+.2f}U"),
        feishu_helper.field("保护性止损（盈利落袋）", f"{s['sl_protect']['count']}笔 · {s['sl_protect']['sum']:+.2f}U"),
    ))
    if s["sl_protect"]["count"]:
        e.append(feishu_helper.note(
            f"利润回吐：{s['sl_protect']['count']} 笔保护单平均 +{s['sl_protect']['avg_pct']:.2f}% 落袋"
            f"（TP1 目标 +8%），若回吐占比高可放松 trailing_tiers"
        ))
    e.append(feishu_helper.hr())

    # AI 观点命中率（Phase 2 前置：AI 是否有预测力）
    e.append(feishu_helper.md("**🤖 AI 观点命中率**（整点扫描 vs 1h 走势）"))
    if ai is None or not ai.get("n"):
        e.append(feishu_helper.note("暂无对比样本（需 ≥2 次整点扫描，ai_scan.jsonl 积累中）"))
    else:
        g = {x["name"]: x for x in ai["groups"]}
        e.append(feishu_helper.row(
            _hit_field("全部", g.get("全部")),
            _hit_field("LONG", g.get("LONG")),
        ))
        e.append(feishu_helper.row(
            _hit_field("SHORT", g.get("SHORT")),
            _hit_field("震荡", g.get("震荡")),
        ))
        e.append(feishu_helper.row(
            _hit_field("置信度 高", g.get("置信度 高(≥0.65)")),
            _hit_field("置信度 低", g.get("置信度 低(<0.60)")),
        ))
        if ai["n"] < 20:
            e.append(feishu_helper.note(f"样本仅 {ai['n']} 个，≥20 才有参考价值，≥100 结论可靠"))
    e.append(feishu_helper.hr())

    # 结论
    e.append(feishu_helper.md(f"**📋 结论**\n{s['conclusion']}"))
    e.append(feishu_helper.note(f"⏰ {datetime.now():%Y-%m-%d %H:%M} · 统计窗口近 {days} 天 (UTC+8)"))
    return e


def main() -> int:
    cfg = config.get_config()
    wr = cfg.get("weekly_report", {})
    if not wr.get("enabled", True):
        print("weekly_report 未启用（config.json weekly_report.enabled=false），跳过")
        return 0

    days = int(wr.get("lookback_days", 7))
    title = wr.get("title", "量化周报")
    summary = analyze_closes.generate_summary(days=days)
    ai = analyze_features.compute_ai_hit()
    ok = feishu_helper.push_card(title, build_elements(summary, days, ai))
    print(f"周报推送{'成功' if ok else '失败'}（窗口 {days} 天，样本 {summary.get('n', 0)} 笔，AI 命中样本 {ai['n']}）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
