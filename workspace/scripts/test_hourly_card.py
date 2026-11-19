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


def _cell(symbol, change, rsi_text, touched=False, cache_ok=True, holding=False):
    return {
        "symbol": symbol,
        "price_txt": "$1.0000",
        "change_md": nl.format_change_md(change),
        "rsi_text": rsi_text,
        "holding": holding,
        "touched": touched,
        "cache_ok": cache_ok,
        "closes": [1.0, 1.1, 1.2],
    }


def _coin_rows(elements):
    import json
    rows = []
    for element in elements:
        if element.get("tag") != "column_set":
            continue
        if len(element.get("columns") or []) != 2:
            continue
        blob = json.dumps(element, ensure_ascii=False)
        if "开多" in blob or "开空" in blob:
            continue
        if "$" not in blob and "—" not in blob and "缓存" not in blob:
            continue
        rows.append(element)
    return rows


class TestHourlyBroadcast(unittest.TestCase):
    def test_change_color(self):
        self.assertIn("green", nl.format_change_md(3.32))
        self.assertIn("+3.32%", nl.format_change_md(3.32))
        self.assertIn("red", nl.format_change_md(-1.25))
        self.assertIn("-1.25%", nl.format_change_md(-1.25))
        zero = nl.format_change_md(0)
        missing = nl.format_change_md(None)
        self.assertNotIn("green", zero)
        self.assertNotIn("red", zero)
        self.assertEqual(zero, "+0.00%")
        self.assertEqual(missing, "—")
        self.assertNotIn("font", missing)

    def test_three_columns_and_no_fear(self):
        import json
        cells = [
            _cell("BTC", -0.26, "RSI 52.9 · 未触及"),
            _cell("ETH", 1.2, "RSI 51.5 · 未触及"),
        ]
        elements = nl.build_hourly_broadcast_elements(
            daily_pnl=0,
            floating=1.5,
            position_count=0,
            max_positions=4,
            cells=cells,
            position_lines=[],
            images={},
            now=__import__("datetime").datetime(2026, 11, 18, 14, 0),
        )
        summary = elements[0]
        self.assertEqual(summary["tag"], "column_set")
        self.assertEqual(len(summary["columns"]), 3)
        self.assertNotIn("fields", summary)
        blob = json.dumps(elements, ensure_ascii=False)
        self.assertIn("已实现", blob)
        self.assertIn("浮动", blob)
        self.assertIn("持仓", blob)
        self.assertIn("0/4", blob)
        self.assertNotIn("恐惧", blob)
        self.assertNotIn("贪婪", blob)
        self.assertNotIn("实时数据", blob)
        self.assertIn("14:00", blob)
        self.assertIn("无信号", blob)
        self.assertIn("≤30 开多", blob)
        self.assertIn("≥70 开空", blob)
        self.assertEqual(nl.HOURLY_CARD_TITLE, "市场播报")
        self.assertEqual(nl.HOURLY_CARD_TEMPLATE, "wathet")

    def test_odd_coin_takes_the_full_row(self):
        import json
        names = ["BTC", "ETH", "SOL", "BNB", "DOT", "LINK", "XRP"]
        cells = [_cell(name, 0.1, "RSI 50.0 · 未触及") for name in names]
        elements = nl.build_hourly_broadcast_elements(
            daily_pnl=0,
            floating=0,
            position_count=1,
            max_positions=4,
            cells=cells,
            position_lines=["**SOL** 多\n开仓 $70 · 现价 $71\n盈亏 +1.00 USDT（+1.00%）"],
            images={},
            now=__import__("datetime").datetime(2026, 11, 18, 14, 0),
        )
        widths = [len(row["columns"]) for row in _coin_rows(elements)]
        self.assertEqual(widths, [2, 2, 2, 2, 2, 2, 2])
        last = json.dumps(_coin_rows(elements)[-1], ensure_ascii=False)
        self.assertIn("XRP", last)
        self.assertNotIn("恐惧", json.dumps(elements, ensure_ascii=False))
        blob = json.dumps(elements, ensure_ascii=False)
        self.assertIn("开仓", blob)
        self.assertIn("+1.00 USDT", blob)
        self.assertIn("持仓", blob)

    def test_signal_labels(self):
        import json
        quiet = [_cell("BTC", 0, "RSI 50.0 · 未触及")]
        hot = [_cell("ETH", -1, "RSI 72.0 · 触及 ≥70", touched=True)]
        short = [_cell("SOL", 0, "RSI 缓存不足", cache_ok=False), quiet[0]]
        now = __import__("datetime").datetime(2026, 11, 18, 14, 0)

        def footer(cells):
            elements = nl.build_hourly_broadcast_elements(
                daily_pnl=0, floating=0, position_count=0, max_positions=4,
                cells=cells, position_lines=[], now=now,
            )
            return json.dumps(elements, ensure_ascii=False)

        self.assertEqual(nl.hourly_signal_label(quiet), "无信号")
        self.assertIn("无信号", footer(quiet))
        self.assertNotIn("有信号", footer(quiet))
        self.assertEqual(nl.hourly_signal_label(hot), "有信号")
        self.assertIn("有信号", footer(hot))
        self.assertNotIn("无信号", footer(hot))
        self.assertIsNone(nl.hourly_signal_label(short))
        self.assertNotIn("无信号", footer(short))
        self.assertNotIn("有信号", footer(short))

    def test_real_rsi_sets_touch_and_keeps_numbers_without_images(self):
        import json
        down = bars([100 - i for i in range(40)])
        px = 100.0
        mid_px = []
        for i in range(48):
            px += 0.4 if i % 2 == 0 else -0.35
            mid_px.append(round(px, 4))
        stream = _Stream({"SOL": down, "BTC": bars(mid_px)})
        prices = {
            "SOL": {"price": 71.41, "change_24h": 0.9},
            "BTC": {"price": 64000, "change_24h": 0},
        }
        cells = nl.build_hourly_coin_cells(
            ["SOL", "BTC"], prices, {}, None, [{"symbol": "SOL"}],
            rsi_fade=True, price_stream=stream, now_ms=NOW,
        )
        sol = cells[0]
        btc = cells[1]
        self.assertTrue(sol["touched"])
        self.assertTrue(sol["holding"])
        self.assertIn("green", sol["change_md"])
        self.assertFalse(btc["touched"])
        self.assertTrue(btc["cache_ok"])
        self.assertNotIn("font", btc["change_md"])
        self.assertEqual(nl.hourly_signal_label(cells), "有信号")
        elements = nl.build_hourly_broadcast_elements(
            daily_pnl=0, floating=0, position_count=1, max_positions=4,
            cells=cells, position_lines=[], images={},
            now=__import__("datetime").datetime(2026, 11, 18, 14, 0),
        )
        blob = json.dumps(elements, ensure_ascii=False)
        self.assertIn("做多", blob)
        self.assertIn("持仓", blob)
        self.assertIn("$71.4100", blob)
        self.assertIn("$64,000.00", blob)
        self.assertNotIn('"tag": "img"', blob)
        self.assertIn("有信号", blob)

    def test_image_failure_does_not_drop_the_card(self):
        import json
        from pathlib import Path
        cells = nl.build_hourly_coin_cells(
            ["BTC"],
            {"BTC": {"price": 64640.7, "change_24h": -0.26}},
            {},
            None,
            [],
            rsi_fade=True,
            price_stream=_Stream({"BTC": bars([100 + i * 0.1 for i in range(40)])}),
            now_ms=NOW,
        )
        def boom(_payload):
            raise RuntimeError("upload failed")
        images = nl.resolve_hourly_images(
            cells, upload=boom, cache_path=Path("/tmp/hourly-broadcast-no-cache.json"),
        )
        self.assertIsNone(images["BTC"]["icon"])
        self.assertIsNone(images["BTC"]["spark"])
        self.assertIsNone(images["BTC"]["meter"])
        elements = nl.build_hourly_broadcast_elements(
            daily_pnl=0, floating=0, position_count=0, max_positions=4,
            cells=cells, position_lines=[], images=images,
            now=__import__("datetime").datetime(2026, 11, 18, 14, 0),
        )
        blob = json.dumps(elements, ensure_ascii=False)
        self.assertIn("$64,640.70", blob)
        self.assertIn(f"{cells[0]['rsi']:.1f}", blob)
        self.assertNotIn('"tag": "img"', blob)
        png = nl.render_sparkline_png(cells[0]["closes"])
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertIsNone(nl.render_sparkline_png([1.0]))
        images = nl.resolve_hourly_images(
            cells, upload=lambda _payload: "img_ok", cache_path=Path("/tmp/hourly-broadcast-ok.json"),
        )
        self.assertEqual(images["BTC"]["icon"], "img_ok")
        self.assertEqual(images["BTC"]["spark"], "img_ok")
        self.assertEqual(images["BTC"]["meter"], "img_ok")

    def test_sparkline_is_opaque(self):
        import io
        from PIL import Image
        png = nl.render_sparkline_png([1, 1.2, 1.1, 1.4, 1.3])
        image = Image.open(io.BytesIO(png))
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.getpixel((0, 0)), nl._CARD_BG)

    def test_push_sends_broadcast_card(self):
        import json
        import sys
        import types
        fake = types.ModuleType("binance_auto_trade")
        fake.get_all_positions = lambda: []
        fake.as_position_list = lambda rows: rows or []
        sys.modules["binance_auto_trade"] = fake
        with patch.object(nl, "_fetch_24h_tickers", return_value={}), \
             patch("feishu_helper.upload_message_image", return_value=None), \
             patch.object(nl, "push_feishu_card", return_value=True) as push:
            ok = nl.push_hourly_report(
                [],
                {"BTC": {"price": 64000, "change_24h": -0.26}},
                {},
                0.0,
                8,
                refresh_indicators=False,
                price_stream=_Stream({}),
            )
        self.assertTrue(ok)
        title, elements, template = push.call_args[0]
        self.assertEqual(title, "市场播报")
        self.assertEqual(template, "wathet")
        self.assertNotEqual(title, "整点")
        blob = json.dumps(elements, ensure_ascii=False)
        self.assertNotIn("恐惧", blob)
        self.assertNotIn("贪婪", blob)
        self.assertEqual(len(elements[0]["columns"]), 3)
        coin_rows = _coin_rows(elements)
        self.assertEqual([len(row["columns"]) for row in coin_rows], [2, 2, 2, 2, 2, 2, 2])
        self.assertIn("XRP", json.dumps(coin_rows[-1], ensure_ascii=False))

    def test_market_picture_is_one_image(self):
        import io
        import json
        from PIL import Image

        cell = {
            "symbol": "ETH",
            "price_txt": "$1,864.20",
            "change": 3.2,
            "rsi": 72.4,
            "layout": "rsi",
            "side": "short",
            "holding": False,
            "closes": [1 + i * 0.1 for i in range(24)],
            "change_md": "<font color='green'>+3.20%</font>",
        }
        png = nl.render_market_card_png(
            daily_pnl=16.27,
            floating=-2.01,
            position_count=1,
            max_positions=4,
            cells=[cell],
            positions=[{
                "symbol": "SOL", "side": "多", "entry_txt": "$74.5000",
                "mark_txt": "$71.2000", "pnl": -8.86, "pct": -4.43,
            }],
            now=__import__("datetime").datetime(2026, 11, 18, 21, 0),
            signal="有信号",
        )
        image = Image.open(io.BytesIO(png))
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.size[0], 720)
        self.assertGreater(image.size[1], 200)
        self.assertLess(image.size[1], 1600)
        self.assertIsNone(image.getcolors(8))

        elements = nl._market_card_elements(
            daily_pnl=0, floating=0, position_count=0, max_positions=4,
            cells=[cell], position_lines=[], position_views=[],
            now=__import__("datetime").datetime(2026, 11, 18, 21, 0),
            upload=lambda _png: "card_key",
        )
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["tag"], "img")
        self.assertEqual(elements[0]["img_key"], "card_key")
        self.assertNotIn("compact_width", elements[0])
        self.assertEqual(elements[0]["mode"], "fit_horizontal")

        fallback = nl._market_card_elements(
            daily_pnl=0, floating=0, position_count=0, max_positions=4,
            cells=[cell], position_lines=["**ETH** 空"], position_views=[],
            now=__import__("datetime").datetime(2026, 11, 18, 21, 0),
            upload=lambda _png: None,
        )
        blob = json.dumps(fallback, ensure_ascii=False)
        self.assertIn("ETH", blob)
        self.assertNotIn('"tag": "img"', blob)
