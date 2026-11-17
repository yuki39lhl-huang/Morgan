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
        self.assertIn("暂无平仓样本，继续观察。", text)
        self.assertNotIn("止损拆分", text)
        self.assertNotIn("AI 观点命中", text)

    def test_summary_leads_and_empty_diagnostics_stay_off(self):
        text = str(wr.build_elements(_sample(), 7, None))
        self.assertIn("已实现", text)
        self.assertIn("+8.00", text)
        self.assertIn("75.0%", text)
        self.assertLess(text.find("样本"), text.find("样本还少"))
        self.assertNotIn("止损拆分", text)
        self.assertNotIn("AI 观点命中", text)


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
