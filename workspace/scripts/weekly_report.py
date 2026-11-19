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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import analyze_closes
import analyze_features
import feishu_helper


_REASON_NAMES = {
    "SL止损": "初始止损",
    "SL保护(移动止盈落袋)": "移动止盈",
    "其他(rsi_mid)": "回到 50",
    "其他(rsi_time)": "满 24 小时",
    "其他(rsi_gap)": "小时断开",
    "超时退出": "超时",
    "TP1 止盈": "止盈",
}


def _reason_name(name: str) -> str:
    return _REASON_NAMES.get(name, name)


def _signed(value: float, digits: int = 2) -> str:
    return f"{float(value):+.{digits}f}"


def _colored(value: float, text: str) -> str:
    if value > 0:
        return f"<font color='green'>**{text}**</font>"
    if value < 0:
        return f"<font color='red'>**{text}**</font>"
    return f"**{text}**"


def _plain_conclusion(s: dict) -> str:
    plr = s["plr"]
    if plr == float("inf"):
        return "暂无亏损单。"
    if plr >= 2:
        return f"盈亏比 {plr:.2f}，达到 2。"
    worst = None
    for row in s.get("reason_rows") or []:
        if row["sum"] < 0 and (worst is None or row["sum"] < worst["sum"]):
            worst = row
    if worst:
        return (
            f"盈亏比 {plr:.2f}，低于 2。最大亏损是{_reason_name(worst['name'])}，"
            f"合计 {_signed(worst['sum'])}。"
        )
    return f"盈亏比 {plr:.2f}，低于 2。"


def build_elements(s: dict, days: int, ai: dict = None) -> list:
    """周报卡片。ai 仍由调用方传入，这版不放全部历史的命中率。"""
    _ = ai
    if not s.get("n"):
        return [
            feishu_helper.md("**暂无平仓**"),
            feishu_helper.md(f"<font color='grey'>这 {days} 天没有记到平仓。</font>"),
        ]

    plr_txt = "∞" if s["plr"] == float("inf") else f"{s['plr']:.2f}"
    realized = float(s["gross_win"]) - float(s["gross_loss"])

    def stat(value_md: str, label: str) -> dict:
        return feishu_helper.column(
            [feishu_helper.md(f"{value_md}\n<font color='grey'>{label}</font>")],
            1,
        )

    elements = [
        feishu_helper.row(
            stat(_colored(realized, _signed(realized)), "已实现"),
            stat(f"**{s['win_rate']:.1f}%**", "胜率"),
            stat(f"**{plr_txt}**", "盈亏比"),
            background_style="default",
            flex_mode="trisect",
        ),
        feishu_helper.md(
            f"<font color='grey'>{s['n']} 笔 · 盈 {s['wins']} · 亏 {s['losses']}"
            f" · 平均 {_signed(s['expected'])} U</font>"
        ),
    ]
    if s.get("noise_count"):
        elements.append(feishu_helper.md(
            f"<font color='grey'>另有 {s['noise_count']} 笔噪声已剔除。</font>"
        ))

    rows_ = s.get("reason_rows") or []
    if rows_:
        elements.append(feishu_helper.hr())
        elements.append(feishu_helper.md("<font color='grey'>平仓原因</font>"))
        for r in rows_:
            elements.append(feishu_helper.md(
                f"**{_reason_name(r['name'])}**　　{_colored(r['sum'], _signed(r['sum']))}\n"
                f"<font color='grey'>{r['count']} 笔 · 胜率 {r['win_rate']:.0f}%"
                f"　　平均 {_signed(r['avg_pct'])}%</font>"
            ))

    elements.append(feishu_helper.hr())
    elements.append(feishu_helper.md(_plain_conclusion(s)))
    elements.append(feishu_helper.note(f"近 {days} 天"))
    return elements


def main() -> int:
    cfg = config.get_config()
    wr = cfg.get("weekly_report", {})
    if not wr.get("enabled", True):
        print("weekly_report 未启用（config.json weekly_report.enabled=false），跳过")
        return 0

    days = int(wr.get("lookback_days", 7))
    summary = analyze_closes.generate_summary(days=days)
    ai = analyze_features.compute_ai_hit()
    ok = feishu_helper.push_card("量化周报", build_elements(summary, days, ai), "wathet")
    print(f"周报推送{'成功' if ok else '失败'}（窗口 {days} 天，样本 {summary.get('n', 0)} 笔，AI 命中样本 {ai['n']}）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
