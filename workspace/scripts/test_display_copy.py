#!/usr/bin/env python3
"""展示文案：周报层次、提醒现写与失败回退。不改统计口径。"""
import unittest
from unittest.mock import patch

import reminder_scheduler as rs
import weekly_report as wr


def _sample():
    return {
        "n": 4,
        "noise_count": 1,
        "wins": 3,
        "losses": 1,
        "win_rate": 75.0,
        "gross_win": 12.0,
        "gross_loss": 4.0,
        "plr": 3.0,
        "expected": 2.0,
        "reason_rows": [],
        "sl_loss": {"count": 0, "sum": 0.0},
        "sl_protect": {"count": 0, "sum": 0.0, "avg_pct": 0.0},
        "conclusion": "样本还少。",
    }


class TestWeeklyLayout(unittest.TestCase):
    def test_empty_sample_stays_one_line(self):
        elements = wr.build_elements({"n": 0}, 7, {"n": 0, "groups": []})
        text = str(elements)
        self.assertIn("暂无平仓", text)
        self.assertIn("这 7 天没有记到平仓。", text)
        self.assertNotIn("止损拆分", text)
        self.assertNotIn("AI 观点命中", text)

    def test_summary_leads_and_empty_diagnostics_stay_off(self):
        text = str(wr.build_elements(_sample(), 7, None))
        self.assertIn("已实现", text)
        self.assertIn("+8.00", text)
        self.assertIn("75.0%", text)
        self.assertIn("盈 3", text)
        self.assertIn("达到 2", text)
        self.assertNotIn("样本还少", text)
        self.assertNotIn("止损拆分", text)
        self.assertNotIn("AI 观点命中", text)

    def test_reason_names_and_shortfall(self):
        sample = _sample()
        sample["plr"] = 0.75
        sample["gross_win"] = 48.57
        sample["gross_loss"] = 64.58
        sample["reason_rows"] = [
            {"name": "SL止损", "count": 15, "pct": 44, "avg_pct": -2.06, "sum": -61.74, "win_rate": 0},
            {"name": "其他(rsi_mid)", "count": 7, "pct": 20, "avg_pct": 1.05, "sum": 14.73, "win_rate": 100},
        ]
        text = str(wr.build_elements(
            sample, 30, {"n": 100, "groups": [{"name": "全部", "total": 10, "rate": 0.5}]},
        ))
        self.assertIn("初始止损", text)
        self.assertIn("回到 50", text)
        self.assertIn("-61.74", text)
        self.assertIn("低于 2", text)
        self.assertNotIn("其他(rsi_mid)", text)
        self.assertNotIn("AI", text)


class TestReminderCopy(unittest.TestCase):
    def test_generated_line_is_used(self):
        with patch.object(rs, "generate_reminder", return_value="该吃早餐了"):
            self.assertEqual(rs.compose_reminder("breakfast"), "该吃早餐了")

    def test_failure_falls_back_to_fixed_lines(self):
        with patch.object(rs, "generate_reminder", return_value=None):
            text = rs.compose_reminder("breakfast")
        self.assertIn(text, rs.MESSAGES["breakfast"])

    def test_wake_still_attaches_weather(self):
        with patch.object(rs, "generate_reminder", return_value="起床了"):
            text = rs.compose_reminder("wake", weather="晴 22°C")
        self.assertTrue(text.startswith("起床了"))
        self.assertIn("龙川天气：晴 22°C", text)
