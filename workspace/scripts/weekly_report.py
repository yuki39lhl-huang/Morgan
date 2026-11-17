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
    realized = float(s["gross_win"]) - float(s["gross_loss"])
    e = [
        feishu_helper.kv_block([
            ("样本", f"**{s['n']} 笔**"),
            ("已实现", f"**{realized:+.2f} U**"),
            ("胜率", f"**{s['win_rate']:.1f}%**"),
            ("盈亏比", f"**{plr_txt}**"),
        ]),
        feishu_helper.md(
            f"盈利 {s['wins']} 笔，亏损 {s['losses']} 笔，平均每笔 {s['expected']:+.3f} U"
            f"。另有 {s['noise_count']} 笔噪声已剔除。"
        ),
    ]

    rows_ = s.get("reason_rows") or []
    if rows_:
        e.append(feishu_helper.hr())
        e.append(feishu_helper.md("**按平仓原因**"))
        for i in range(0, len(rows_), 2):
            pair = []
            for r in rows_[i:i + 2]:
                sign = "+" if r["sum"] >= 0 else ""
                pair.append(feishu_helper.field(
                    f"{r['name']} · {r['count']}笔 · 胜率{r['win_rate']:.0f}%",
                    f"{sign}{r['sum']:.2f}U · 平均{r['avg_pct']:+.2f}%",
                ))
            e.append(feishu_helper.row(*pair))

    if s["sl_loss"]["count"] or s["sl_protect"]["count"]:
        e.append(feishu_helper.hr())
        e.append(feishu_helper.md("**止损拆分**"))
        e.append(feishu_helper.row(
            feishu_helper.field("初始止损", f"{s['sl_loss']['count']}笔 · {s['sl_loss']['sum']:+.2f}U"),
            feishu_helper.field("保护性止损", f"{s['sl_protect']['count']}笔 · {s['sl_protect']['sum']:+.2f}U"),
        ))
        if s["sl_protect"]["count"]:
            e.append(feishu_helper.note(
                f"保护单平均 +{s['sl_protect']['avg_pct']:.2f}% 落袋"
            ))

    if ai and ai.get("n"):
        e.append(feishu_helper.hr())
        e.append(feishu_helper.md("**AI 观点命中**"))
        g = {x["name"]: x for x in ai["groups"]}
        e.append(feishu_helper.row(
            _hit_field("全部", g.get("全部")),
            _hit_field("LONG", g.get("LONG")),
        ))
        e.append(feishu_helper.row(
            _hit_field("SHORT", g.get("SHORT")),
            _hit_field("震荡", g.get("震荡")),
        ))
        if ai["n"] < 20:
            e.append(feishu_helper.note(f"命中样本 {ai['n']} 个"))

    e.append(feishu_helper.hr())
    e.append(feishu_helper.md(s["conclusion"]))
    e.append(feishu_helper.note(f"{datetime.now():%Y-%m-%d %H:%M} · 近 {days} 天 (UTC+8)"))
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
