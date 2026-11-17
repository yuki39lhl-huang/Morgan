#!/usr/bin/env python3
"""整点卡片：rsi_fade 只用已收盘 1h RSI，不拿 15 分钟 RSI 或旧评分顶上。"""
import unittest
from unittest.mock import patch

import notify_layer as nl

HOUR = 3_600_000
NOW = 1_700_000_000_000


def bars(prices, now_ms=NOW):
    rows = []
    start = now_ms - len(prices) * HOUR
    for i, px in enumerate(prices):
        open_ts = start + i * HOUR
        rows.append([open_ts, px, px, px, px, 0, open_ts + HOUR - 1, 0, 0, 0, 0, 0])
    return rows


class _Stream:
    def __init__(self, book):
        self.book = book

    def closed_klines(self, symbol):
        return self.book.get(symbol, [])


class TestHourlyCard(unittest.TestCase):
    def test_short_cache_is_not_filled_with_15m_rsi(self):
        self.assertIsNone(nl.closed_hour_rsi(bars([100] * 10), NOW))
        text = nl.format_rsi_fade_status(None, no_open=True)
        self.assertIn("缓存不足", text)
        self.assertIn("不开仓", text)
        self.assertNotIn("分", text)
        self.assertNotIn("NONE", text)

    def test_closed_rsi_ignores_the_forming_bar(self):
        closed = bars([100 - i for i in range(40)])
        forming = [NOW, 1.0, 1.0, 1.0, 1.0, 0, NOW + HOUR, 0, 0, 0, 0, 0]
        self.assertEqual(
            nl.closed_hour_rsi(closed, NOW),
            nl.closed_hour_rsi(closed + [forming], NOW),
        )
        self.assertLessEqual(nl.closed_hour_rsi(closed, NOW), 30)

    def test_card_uses_1h_cache_not_indicator_rsi(self):
        down = bars([100 - i for i in range(40)])
        up = bars([50 + i for i in range(40)])
        stream = _Stream({"SOL": down, "ETH": up, "BTC": bars([1, 2, 3])})
        indicators = {
            "SOL": {"rsi": 99},
            "ETH": {"rsi": 1},
            "BTC": {"rsi": 55},
        }
        prices = {
            "SOL": {"price": 71.41, "change_24h": 0.9},
            "ETH": {"price": 3200.5, "change_24h": -1.2},
            "BTC": {"price": 64000, "change_24h": 0.1},
        }
        lines = nl.build_hourly_coin_lines(
            ["SOL", "ETH", "BTC"],
            prices,
            indicators,
            {"SOL": {"score": 80, "direction": "NONE", "regime": "ranging"}},
            [{"symbol": "SOL"}],
            rsi_fade=True,
            price_stream=stream,
            now_ms=NOW,
            no_open_symbols={"BTC", "ETH"},
        )
        text = "\n".join(lines)
        self.assertIn("RSI ", text)
        self.assertIn("触及 ≤30", text)
        self.assertIn("触及 ≥70", text)
        self.assertIn("缓存不足", text)
        self.assertIn("不开仓", text)
        self.assertIn("持仓", text)
        self.assertNotIn("1h RSI", text)
        self.assertNotIn("99", text)
        self.assertNotIn("分", text)
        self.assertNotIn("NONE", text)
        self.assertNotIn("信号", text)
        self.assertNotIn("趋势", text)
        self.assertNotIn("震荡", text)

    def test_rsi_fade_hour_does_not_calc_15m(self):
        self.assertFalse(nl.should_refresh_15m_indicators("rsi_fade"))
        self.assertTrue(nl.should_refresh_15m_indicators("score"))

        class Engine:
            def calc(self, symbol):
                raise AssertionError(symbol)

        with patch.object(nl, "_fetch_24h_tickers", return_value={}):
            prices, _ = nl.prepare_hourly_report_data(
                {"SOL": {"price": 71.4, "change_24h": 0.5}},
                {"SOL": {"rsi": 15}},
                Engine(),
                refresh_indicators=True,
            )
        self.assertEqual(prices["SOL"]["price"], 71.4)
        self.assertTrue(nl.entry_is_rsi_fade())
