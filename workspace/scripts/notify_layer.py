#!/usr/bin/env python3
"""
notify_layer.py — 推送层

职责：交易信号告警（push_signal_alert）、整点汇报（hourly_report / push_hourly_report）、
告警落盘（write_alert）、今日盈亏校准（sync/restore_daily_pnl）、账户余额快照。
依赖方向：仅依赖基础设施（config / feishu_helper / openclaw_logging / binance_auto_trade）
与状态层（position_store）；评分/指标等业务计算由 main 预计算后传入，禁止跨层依赖；
cooldown_manager 由 main 通过 set_cooldown_manager() 注入。
"""
import json
import logging
import math
import os
import time
import requests
from datetime import datetime
from typing import Optional

from config import CONFIG, get_config, get_exit_config, get_risk_config, max_positions_for_symbol
from feishu_helper import (
    push_card as push_feishu_card,
    md,
    hr,
    note,
    column,
    row,
    img,
    kv_block,
)
from openclaw_logging import daily_alert_path

from position_store import normalize_position, position_amount

log = logging.getLogger(__name__)
HOURLY_TICKER_MAX_AGE_SEC = 60

# 信号推送冷却管理器引用（由 main 装配时注入）
cooldown_manager = None


def set_cooldown_manager(cm) -> None:
    """注入信号冷却管理器实例（risk_layer.CooldownManager）供推送缓冲使用。"""
    global cooldown_manager
    cooldown_manager = cm


def get_last_balance_snapshot() -> float:
    """读取当前账户余额快照（避免 from-import 复制失效）。"""
    return _last_balance_snapshot


# 账户余额快照（整点获取，供状态文件/口径统计复用，避免高频查 API）
_last_balance_snapshot = 0.0


def write_alert(msg: str):
    try:
        with open(daily_alert_path(), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def sync_daily_pnl_from_exchange(circuit) -> float:
    """以交易所当日成交汇总为准，刷新今日已实现盈亏（用于启动/整点汇报）。"""
    today = datetime.now().date()
    circuit.reset_date = today
    try:
        from binance_auto_trade import fetch_today_realized_pnl

        api_pnl = fetch_today_realized_pnl(CONFIG["symbols"])
        if api_pnl is None:
            return circuit.daily_pnl
        circuit.daily_pnl = api_pnl
        return api_pnl
    except Exception as e:
        log.warning(f"⚠️ 同步今日已实现盈亏失败：{e}")
        return circuit.daily_pnl


def restore_daily_pnl(circuit) -> float:
    """启动时恢复今日已实现：优先交易所汇总，失败则用同日状态文件。"""
    today = datetime.now().date()
    circuit.reset_date = today
    state_pnl = 0.0
    try:
        if os.path.exists(CONFIG["state_file"]):
            with open(CONFIG["state_file"]) as f:
                state = json.load(f)
            if state.get("daily_pnl_date") == today.isoformat():
                state_pnl = float(state.get("daily_pnl", 0.0))
    except Exception as e:
        log.warning(f"⚠️ 读取状态 daily_pnl 失败：{e}")

    try:
        from binance_auto_trade import fetch_today_realized_pnl

        api_pnl = fetch_today_realized_pnl(CONFIG["symbols"])
        if api_pnl is not None:
            circuit.daily_pnl = api_pnl
            log.info(f"📋 今日已实现盈亏：{api_pnl:+.2f}U（来源：交易所 userTrades）")
            return api_pnl
    except Exception as e:
        log.warning(f"⚠️ 交易所今日盈亏同步失败：{e}")

    circuit.daily_pnl = state_pnl
    log.info(f"📋 今日已实现盈亏：{state_pnl:+.2f}U（来源：状态文件）")
    return state_pnl


# ═══════════════════════════════════════════════════════════════
# 十三、飞书推送（统一走 feishu_helper，本模块仅保留别名）
# ═══════════════════════════════════════════════════════════════
def push_signal_alert(signal: dict, immediate: bool = False):
    """推送交易信号告警（开仓/平仓）- 支持缓冲合并"""
    symbol = signal['symbol']

    # 添加到缓冲区（除非立即推送）
    try:
        if not immediate and cooldown_manager:
            cooldown_manager.add_push_signal(symbol, signal)
            return
    except NameError:
        pass  # cooldown_manager 未初始化，直接推送

    # 立即推送
    _send_push_signal(signal)


def _send_push_signal(signal: dict):
    """实际发送推送信号"""
    symbol = signal['symbol']
    sig_type = signal.get('type', 'UNKNOWN')
    action = signal.get('action', '信号')
    price = signal.get('price', 0)
    score = signal.get('score', 0)
    regime = signal.get('regime', 'unknown')
    tp_price = signal.get('tp_price', 0)
    sl_price = signal.get('sl_price', 0)
    pnl = signal.get('pnl', 0)
    trigger_price = signal.get('trigger_price', 0)
    order_id = signal.get('order_id', '')
    merged_pnl = signal.get('merged_pnl', 0)
    merged_count = signal.get('merged_count', 0)
    merged_close_count = signal.get('merged_close_count', 0)

    if sig_type == 'LONG':
        template = 'green'
        type_text = '开多'
    elif sig_type == 'SHORT':
        template = 'red'
        type_text = '开空'
    else:
        template = 'blue'
        type_text = action

    is_merged = bool(merged_close_count) and action in ['开多', '开空']
    kv_items = [("币种", f"**{symbol}**"), ("方向", f"**{type_text}**")]

    if action in ['开多', '开空']:
        kv_items.append(("开仓价", f"**{_format_px(float(price or 0))}**"))
    elif action in ['止盈', '止损', '平仓']:
        if trigger_price and trigger_price != price:
            kv_items.append(("触发价", f"**{_format_px(float(trigger_price))}**"))
            kv_items.append(("成交价", f"**{_format_px(float(price or 0))}**"))
        else:
            kv_items.append(("成交价", f"**{_format_px(float(price or 0))}**"))
    else:
        kv_items.append(("价格", f"**{_format_px(float(price or 0))}**"))

    # 旧评分只在真有分数时附在后面，不拿 0 分占主信息
    if score:
        kv_items.append(("评分", f"{score}/100"))
        if regime and regime not in ("unknown", "rsi"):
            kv_items.append(("状态", f"{regime}"))

    if action in ['开多', '开空'] and tp_price and sl_price:
        kv_items.append(("止盈", f"**{_format_px(float(tp_price))}**"))
        kv_items.append(("止损", f"**{_format_px(float(sl_price))}**"))

    if action in ['止盈', '止损', '平仓']:
        kv_items.append(("盈亏", f"**{float(pnl):+.2f} USDT**"))
        if merged_count and merged_count > 1:
            kv_items.append(("合并", f"{merged_count} 次"))

    elements = [kv_block(kv_items)]

    if merged_pnl != 0 and is_merged:
        elements.append(hr())
        elements.append(md("这段时间里同一币种的平仓"))
        elements.append(kv_block([
            ("平仓盈亏", f"**{float(merged_pnl):+.2f} USDT**"),
            ("次数", f"{merged_close_count} 次"),
        ]))
    elif merged_pnl != 0:
        elements.append(hr())
        elements.append(md(f"累计盈亏 {float(merged_pnl):+.2f} USDT"))
        if merged_close_count:
            elements.append(md(f"含 {merged_close_count} 次平仓"))

    if order_id:
        elements.append(note(f"订单 {order_id}"))

    elements.append(note(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)"))

    if is_merged:
        title = f"{symbol} 汇总"
    else:
        title = f"{action} {symbol}"
    push_feishu_card(title, elements, template)


def _normalize_price_data(prices: dict) -> dict:
    """整点汇报用：保证每个币种价格是完整 dict（含 change_24h / volume）"""
    out = {}
    for sym in CONFIG["symbols"]:
        pd = prices.get(sym)
        if isinstance(pd, (int, float)):
            pd = {"price": float(pd)}
        elif isinstance(pd, dict):
            pd = dict(pd)
        else:
            pd = {}
        if pd.get("price", 0) <= 0:
            continue
        if pd.get("change_24h") is None and pd.get("open_24h", 0) > 0:
            o = float(pd["open_24h"])
            p = float(pd["price"])
            pd["change_24h"] = (p - o) / o * 100
        if pd.get("change_24h") is None:
            pd["change_24h"] = 0.0
        pd.setdefault("volume", 0.0)
        out[sym] = pd
    return out


def _fetch_24h_tickers() -> dict[str, dict]:
    """REST 批量拉 24h ticker（价格、涨跌幅、成交量兜底）"""
    out = {}
    try:
        from rest_backoff import fapi_public_get, response_limited

        session = requests.Session()
        session.proxies = CONFIG["proxies"]
        session.verify = False
        r = fapi_public_get(
            session,
            f"{CONFIG['binance_futures']}/ticker/24hr",
            "/fapi/v1/ticker/24hr",
            timeout=15,
        )
        if response_limited(r) or r.status_code != 200:
            return out
        for t in r.json():
            sym = t.get("symbol", "").replace("USDT", "")
            if sym in CONFIG["symbols"]:
                out[sym] = {
                    "price": float(t.get("lastPrice", 0) or 0),
                    "change_24h": float(t.get("priceChangePercent", 0) or 0),
                    "volume": float(t.get("volume", 0) or 0),
                }
    except Exception as e:
        log.warning(f"⚠️ 整点汇报拉 24h ticker 失败：{e}")
    return out


def _regime_label(regime: str) -> str:
    return {"trending": "趋势", "ranging": "震荡", "volatile": "高波动"}.get(regime, regime)


def _format_hourly_score_line(symbol: str, pd: dict, ind: dict, score_info: Optional[dict]) -> str:
    """旧评分路径的整点单行。rsi_fade 不走这里。"""
    if not score_info:
        return "—分（指标未就绪）"
    score = score_info.get("score", 0)
    direction = score_info.get("direction", "NONE")
    regime = score_info.get("regime", "ranging")
    rsi = ind.get("rsi", 0)
    return f"{score}分 RSI{rsi:.0f} {_regime_label(regime)} → {direction}"


def entry_is_rsi_fade(cfg: Optional[dict] = None) -> bool:
    """当前是否 rsi_fade。只决定整点展示，不改开平仓。"""
    if cfg is None:
        cfg = get_config()
    return str((cfg or {}).get("entry_strategy") or "") == "rsi_fade"


def should_refresh_15m_indicators(entry_strategy: str) -> bool:
    """rsi_fade 整点不再为旧评分去算 15 分钟指标。"""
    return str(entry_strategy or "") != "rsi_fade"


def closed_hour_rsi(rows, now_ms: int):
    """已收盘 1 小时 RSI(14)。和试跑同一份缓存、同一套 Wilder。不够就返回 None。"""
    import numpy as np
    from rsi_fade import RSI_N, closed_pairs, wilder_rsi

    pairs = closed_pairs(rows or [], int(now_ms))
    if len(pairs) < RSI_N + 2:
        return None
    opens = np.asarray([t for t, _ in pairs], dtype=np.int64)
    closes = np.asarray([p for _, p in pairs], dtype=np.float64)
    rsi = wilder_rsi(closes, opens)
    cur = float(rsi[-1])
    if not math.isfinite(cur):
        return None
    return cur


def format_rsi_fade_status(rsi, no_open: bool) -> str:
    """整点短状态：已收盘 1h RSI。不写旧评分。"""
    from rsi_fade import RSI_HIGH, RSI_LOW

    if rsi is None:
        text = "RSI 缓存不足"
    elif rsi >= RSI_HIGH:
        text = f"RSI {rsi:.1f} · 触及 ≥{RSI_HIGH:.0f}"
    elif rsi <= RSI_LOW:
        text = f"RSI {rsi:.1f} · 触及 ≤{RSI_LOW:.0f}"
    else:
        text = f"RSI {rsi:.1f} · 未触及"
    if no_open:
        text += " · 不开仓"
    return text


def _format_px(price: float) -> str:
    if not price:
        return "—"
    if price >= 100:
        return f"${price:,.2f}"
    return f"${price:,.4f}"


HOURLY_CARD_TITLE = "市场播报"
HOURLY_CARD_TEMPLATE = "wathet"
_SPARK_W = 156
_SPARK_H = 44
# 和飞书深色瓷片接近的不透明底。透明 PNG 会被画成白块。
_CARD_BG = (49, 52, 58)


def format_change_md(change) -> str:
    """24 小时涨跌。正数绿色，负数红色，0 和缺失不着色。"""
    if change is None:
        return "—"
    try:
        value = float(change)
    except (TypeError, ValueError):
        return "—"
    if value != value:
        return "—"
    text = f"{value:+.2f}%"
    if value > 0:
        return f"<font color='green'>{text}</font>"
    if value < 0:
        return f"<font color='red'>{text}</font>"
    return text


def _closed_closes(rows, now_ms: int) -> list:
    """已收盘 1h 收盘价。和 RSI 同一套过滤，不把正在走的那根画进去。"""
    from rsi_fade import closed_pairs

    return [px for _, px in closed_pairs(rows or [], int(now_ms))]


def build_hourly_coin_cells(
    symbols,
    prices: dict,
    indicators: dict,
    score_map: Optional[dict],
    positions: list,
    *,
    rsi_fade: bool,
    price_stream=None,
    now_ms: int = 0,
    no_open_symbols=None,
) -> list:
    """整点币种结构。rsi_fade 的 RSI 只来自已收盘 1h 缓存。"""
    from rsi_fade import RSI_HIGH, RSI_LOW

    held = {p.get("symbol") for p in positions}
    closed = set(no_open_symbols or [])
    cells = []
    for symbol in symbols:
        pd = prices.get(symbol) or {}
        price = float(pd.get("price") or 0)
        change = pd.get("change_24h")
        try:
            change_value = None if change is None else float(change)
            if change_value != change_value:
                change_value = None
        except (TypeError, ValueError):
            change_value = None
        closes = []
        rsi = None
        if rsi_fade:
            rows = price_stream.closed_klines(symbol) if price_stream is not None else []
            rsi = closed_hour_rsi(rows, now_ms)
            closes = _closed_closes(rows, now_ms)
            status = format_rsi_fade_status(rsi, symbol in closed)
        else:
            if not price:
                continue
            ind = indicators.get(symbol, {})
            score_info = (score_map or {}).get(symbol)
            status = (
                _format_hourly_score_line(symbol, pd, ind, score_info)
                if ind else "—分（指标未就绪）"
            )
        touched = rsi is not None and (float(rsi) >= RSI_HIGH or float(rsi) <= RSI_LOW)
        if rsi is not None and float(rsi) >= RSI_HIGH:
            side = "short"
        elif rsi is not None and float(rsi) <= RSI_LOW:
            side = "long"
        else:
            side = ""
        cells.append({
            "symbol": symbol,
            "price_txt": _format_px(price),
            "change": change_value,
            "change_md": format_change_md(change),
            "rsi_text": status,
            "rsi": None if rsi is None else float(rsi),
            "layout": "rsi" if rsi_fade else "score",
            "side": side,
            "no_open": symbol in closed,
            "holding": symbol in held,
            "touched": bool(touched),
            "cache_ok": rsi is not None,
            "closes": closes,
        })
    return cells


def build_hourly_coin_lines(
    symbols,
    prices: dict,
    indicators: dict,
    score_map: Optional[dict],
    positions: list,
    *,
    rsi_fade: bool,
    price_stream=None,
    now_ms: int = 0,
    no_open_symbols=None,
) -> list:
    """整点币种行。rsi_fade 只用已收盘 1h 缓存，不用 indicators 里的 15 分钟 RSI。"""
    lines = []
    for cell in build_hourly_coin_cells(
        symbols,
        prices,
        indicators,
        score_map,
        positions,
        rsi_fade=rsi_fade,
        price_stream=price_stream,
        now_ms=now_ms,
        no_open_symbols=no_open_symbols,
    ):
        holding = "持仓 " if cell["holding"] else ""
        lines.append(
            f"**{cell['symbol']}** {holding}{cell['price_txt']}  {cell['change_md']}\n"
            f"{cell['rsi_text']}"
        )
    return lines


def hourly_signal_label(cells) -> Optional[str]:
    """全部未触及且缓存都可用才是无信号；任一币触及阈值就是有信号。"""
    if not cells:
        return None
    if any(cell.get("touched") for cell in cells):
        return "有信号"
    if all(cell.get("cache_ok") for cell in cells):
        return "无信号"
    return None


def _tone_num(value: float, text: str) -> str:
    if value > 0:
        return f"<font color='green'>**{text}**</font>"
    if value < 0:
        return f"<font color='red'>**{text}**</font>"
    return f"**{text}**"


def _summary_row(daily_pnl: float, floating: float, position_count: int, max_positions: int) -> dict:
    def item(label: str, value: float, text: str) -> dict:
        return column([md(f"{_tone_num(value, text)}\n<font color='grey'>{label}</font>")], 1)

    return row(
        item("已实现", float(daily_pnl), f"{float(daily_pnl):+.2f}"),
        item("浮动", float(floating), f"{float(floating):+.2f}"),
        item("持仓", 0, f"{int(position_count)}/{int(max_positions)}"),
        background_style="default",
        flex_mode="trisect",
    )


def _cell_side(cell: dict) -> str:
    side = cell.get("side") or ""
    if side:
        return side
    text = cell.get("rsi_text") or ""
    if "≤30" in text:
        return "long"
    if "≥70" in text:
        return "short"
    return ""


def _name_md(cell: dict) -> str:
    parts = [f"**{cell['symbol']}**"]
    side = _cell_side(cell)
    if side == "long":
        parts.append("<font color='green'>做多</font>")
    elif side == "short":
        parts.append("<font color='red'>做空</font>")
    if cell.get("holding"):
        parts.append("<font color='grey'>持仓</font>")
    return " ".join(parts)


def _rsi_md(cell: dict) -> str:
    extra = " · 不开仓" if cell.get("no_open") else ""
    rsi = cell.get("rsi")
    if cell.get("layout", "rsi") != "rsi" or "rsi" not in cell:
        return cell.get("rsi_text") or "—"
    if rsi is None:
        return f"<font color='grey'>缓存不足{extra}</font>"
    text = f"{float(rsi):.1f}{extra}"
    side = _cell_side(cell)
    if side == "long":
        return f"<font color='green'>{text}</font>"
    if side == "short":
        return f"<font color='red'>{text}</font>"
    return f"<font color='grey'>{text}</font>"


def _coin_row(cell: dict, images: Optional[dict]) -> dict:
    """文字兜底。飞书分栏撑不开 HTML 那种密度，正常推送走整张图。"""
    _ = images
    return row(
        column([md(_name_md(cell)), md(_rsi_md(cell))], 1),
        column([md(f"**{cell['price_txt']}**\n{cell['change_md']}")], 1),
        background_style="default",
        flex_mode="none",
    )


def _broadcast_status(now: datetime, label: Optional[str]) -> dict:
    from zoneinfo import ZoneInfo

    shanghai = ZoneInfo("Asia/Shanghai")
    if now.tzinfo is None:
        now = now.replace(tzinfo=shanghai)
    else:
        now = now.astimezone(shanghai)
    signal = md(f"**{label}**") if label else md(" ")
    return row(
        column([signal], 1),
        column([md(f"<font color='grey'>{now:%H:%M}</font>")], 1),
        background_style="default",
        flex_mode="none",
    )


def _entry_legend() -> dict:
    return row(
        column([md("<font color='green'>≤30 开多</font>")], 1),
        column([md("<font color='red'>≥70 开空</font>")], 1),
        background_style="default",
        flex_mode="bisect",
    )


def build_hourly_broadcast_elements(
    *,
    daily_pnl: float,
    floating: float,
    position_count: int,
    max_positions: int,
    cells: list,
    position_lines: list,
    images: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> list:
    """市场播报卡片元素。没有图片时只少图标和走势，数字仍在。"""
    from zoneinfo import ZoneInfo

    if now is None:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
    elements = [
        _summary_row(daily_pnl, floating, position_count, max_positions),
        _broadcast_status(now, hourly_signal_label(cells)),
    ]
    if not cells or any(cell.get("layout", "rsi") == "rsi" for cell in cells):
        elements.append(_entry_legend())
    elements.extend(_coin_row(cell, images) for cell in cells)
    if position_lines:
        elements.append(hr())
        elements.append(md("<font color='grey'>持仓</font>"))
        elements.extend(md(text) for text in position_lines)
    return elements


def _icon_file(symbol: str):
    from pathlib import Path

    path = Path(__file__).resolve().parent / "assets" / "coins" / f"{symbol.upper()}.png"
    return path if path.is_file() else None


def render_sparkline_png(closes) -> Optional[bytes]:
    """用已收盘收盘价画一张小走势。点数不够或没有 PIL 就返回 None。"""
    vals = [float(v) for v in (closes or []) if v is not None][-24:]
    if len(vals) < 2:
        return None
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    import io

    image = Image.new("RGB", (_SPARK_W, _SPARK_H), _CARD_BG)
    draw = ImageDraw.Draw(image)
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1.0
    pad = 3
    points = []
    last = len(vals) - 1
    for i, value in enumerate(vals):
        x = pad + (_SPARK_W - 2 * pad) * (i / last)
        y = pad + (_SPARK_H - 2 * pad) * (1 - (value - lo) / span)
        points.append((x, y))
    rising = vals[-1] >= vals[0]
    line = (54, 211, 153) if rising else (248, 113, 113)
    wash = (32, 92, 72) if rising else (92, 48, 52)
    draw.polygon([(points[0][0], _SPARK_H - 1), *points, (points[-1][0], _SPARK_H - 1)], fill=wash)
    draw.line(points, fill=line, width=2)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


_METER_W = 220
_METER_H = 28


def render_rsi_meter_png(rsi, side: str = "") -> Optional[bytes]:
    """1 小时 RSI 尺子。左 30 开多，右 70 开空。没有 RSI 就不画。"""
    if rsi is None:
        return None
    try:
        value = float(rsi)
    except (TypeError, ValueError):
        return None
    if value != value:
        return None
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    import io

    image = Image.new("RGB", (_METER_W, _METER_H), _CARD_BG)
    draw = ImageDraw.Draw(image)
    y = _METER_H // 2
    x0, x1 = 6, _METER_W - 6

    def x_at(point: float) -> float:
        return x0 + (x1 - x0) * (max(0.0, min(100.0, point)) / 100.0)

    def mix(color, strong: bool):
        if strong:
            return color
        return tuple(int(c * 0.42 + b * 0.58) for c, b in zip(color, _CARD_BG))

    green = (62, 207, 142)
    red = (255, 107, 120)
    draw.line([(x_at(0), y), (x_at(30), y)], fill=mix(green, side == "long"), width=4)
    draw.line([(x_at(30), y), (x_at(70), y)], fill=(70, 74, 82), width=4)
    draw.line([(x_at(70), y), (x_at(100), y)], fill=mix(red, side == "short"), width=4)
    if side == "long":
        dot = green
    elif side == "short":
        dot = red
    else:
        dot = (213, 216, 224)
    cx = x_at(value)
    radius = 5
    draw.ellipse((cx - radius - 2, y - radius - 2, cx + radius + 2, y + radius + 2), fill=_CARD_BG)
    draw.ellipse((cx - radius, y - radius, cx + radius, y + radius), fill=dot)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def resolve_hourly_images(cells, upload=None, cache_path=None) -> dict:
    """上传币种图标、RSI 尺子和走势。任何一步失败都只让该图为空。"""
    import hashlib
    from pathlib import Path

    from feishu_helper import cached_message_image

    if cache_path is None:
        cache_path = Path(__file__).resolve().parent / ".feishu_img_cache.json"
    out = {}
    for cell in cells or []:
        symbol = str(cell.get("symbol") or "")
        slot = {"icon": None, "spark": None, "meter": None}
        try:
            icon = _icon_file(symbol)
            if icon is not None:
                data = icon.read_bytes()
                digest = hashlib.sha256(data).hexdigest()[:12]
                slot["icon"] = cached_message_image(
                    cache_path, f"icon:{symbol}:{digest}", data, upload=upload,
                )
        except Exception as e:
            log.warning("整点图标跳过 %s：%s", symbol, type(e).__name__)
        try:
            png = render_sparkline_png(cell.get("closes") or [])
            if png:
                digest = hashlib.sha256(png).hexdigest()[:16]
                slot["spark"] = cached_message_image(
                    cache_path, f"spark:{digest}", png, upload=upload,
                )
        except Exception as e:
            log.warning("整点走势跳过 %s：%s", symbol, type(e).__name__)
        try:
            png = render_rsi_meter_png(cell.get("rsi"), cell.get("side") or "")
            if png:
                digest = hashlib.sha256(png).hexdigest()[:16]
                slot["meter"] = cached_message_image(
                    cache_path, f"meter:{digest}", png, upload=upload,
                )
        except Exception as e:
            log.warning("整点尺子跳过 %s：%s", symbol, type(e).__name__)
        if symbol:
            out[symbol] = slot
    return out


def prepare_hourly_report_data(
    prices: dict,
    indicators: dict,
    indicator_engine=None,
    refresh_indicators: bool = True,
) -> tuple[dict, dict]:
    """复用新鲜行情，缺失或过期时用 REST 补齐，按需刷新旧评分指标。"""
    now = time.time()
    fresh = {
        sym for sym, ticker in prices.items()
        if isinstance(ticker, dict)
        and ticker.get("price", 0) > 0
        and ticker.get("change_24h") is not None
        and isinstance(ticker.get("ts"), (int, float))
        and 0 <= now - ticker["ts"] <= HOURLY_TICKER_MAX_AGE_SEC
    }
    missing = set(CONFIG["symbols"]) - fresh
    prices = _normalize_price_data(prices)
    if missing:
        for sym, ticker in _fetch_24h_tickers().items():
            if sym not in missing:
                continue
            current = prices.setdefault(sym, {})
            if ticker.get("price", 0) > 0:
                current["price"] = ticker["price"]
            current["change_24h"] = ticker.get("change_24h", current.get("change_24h", 0))
            current["volume"] = ticker.get("volume", current.get("volume", 0))

    # rsi_fade 的整点不需要 15 分钟指标，这里也不补算
    if entry_is_rsi_fade() or not refresh_indicators:
        return prices, indicators
    if indicator_engine is None:
        log.warning("⚠️ 整点汇报指标刷新跳过：未注入 indicator_engine")
        return prices, indicators

    engine = indicator_engine
    refreshed = {}
    for sym in CONFIG["symbols"]:
        ind = engine.calc(sym)
        if ind:
            refreshed[sym] = ind
            if sym in prices and ind.get("last_close"):
                prices[sym]["price"] = float(ind["last_close"])
                prices[sym]["volume"] = float(ind.get("vol_current") or prices[sym].get("volume") or 0)
    if refreshed:
        indicators = refreshed
        log.info(f"📋 整点汇报已刷新指标：{len(refreshed)}/{len(CONFIG['symbols'])} 个币种")
    else:
        log.warning("⚠️ 整点汇报指标刷新失败，评分可能为 0")

    return prices, indicators


_COIN_DOT = {
    "BTC": (240, 162, 2), "ETH": (154, 166, 255), "SOL": (196, 181, 253),
    "BNB": (240, 193, 77), "DOT": (240, 138, 180), "LINK": (126, 182, 255),
    "XRP": (197, 204, 214),
}
_CARD_INK = (22, 24, 30)
_CARD_TEXT = (243, 244, 246)
_CARD_MUTED = (139, 145, 156)
_CARD_LINE = (42, 45, 52)
_CARD_UP = (62, 207, 142)
_CARD_DOWN = (255, 107, 120)


def _cjk(size: int, weight: str = "regular", mono: bool = False):
    from PIL import ImageFont

    # TTC 里简体是第 3 套。等宽用来对齐价格，避免数字宽窄不一。
    if mono:
        return ImageFont.truetype(
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", size, index=7,
        )
    name = "NotoSansCJK-Medium.ttc" if weight == "medium" else "NotoSansCJK-Regular.ttc"
    return ImageFont.truetype(f"/usr/share/fonts/opentype/noto/{name}", size, index=2)


def _text_width(font, text: str) -> int:
    box = font.getbbox(text)
    return box[2] - box[0]


def _blend(color, opacity: float, bg=_CARD_INK):
    return tuple(int(c * opacity + b * (1 - opacity)) for c, b in zip(color, bg))


def _draw_tracked(draw, xy, text, font, fill, tracking: float = 0):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += _text_width(font, ch) + tracking
    return x


def _draw_meter(base, x, y, w, h, rsi, side: str):
    """和网页一样：3 像素圆轨，左 30% 绿、右 30% 红，白点带一圈底色。"""
    from PIL import Image, ImageDraw

    track_h = max(2, round(h * 3 / 14))
    top = y + (h - track_h) / 2
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    radius = track_h / 2
    green = (*_CARD_UP, int(255 * (0.85 if side == "long" else 0.28)))
    red = (*_CARD_DOWN, int(255 * (0.85 if side == "short" else 0.28)))
    grey = (255, 255, 255, int(255 * 0.10))
    draw.rounded_rectangle((x, top, x + w, top + track_h), radius=radius, fill=grey)
    draw.rounded_rectangle((x, top, x + w * 0.30, top + track_h), radius=radius, fill=green)
    # 中段盖住左段的右圆角，右段再盖住中段。
    draw.rectangle((x + w * 0.30 - radius, top, x + w * 0.70, top + track_h), fill=grey)
    draw.rounded_rectangle((x + w * 0.70, top, x + w, top + track_h), radius=radius, fill=red)
    draw.rectangle((x + w * 0.70, top, x + w * 0.70 + radius, top + track_h), fill=grey)
    base.alpha_composite(overlay)
    dot = _CARD_UP if side == "long" else _CARD_DOWN if side == "short" else (213, 216, 224)
    cx = x + w * (max(0.0, min(100.0, float(rsi))) / 100.0)
    r = max(3, round(h * 4 / 14))
    ring = max(2, round(h * 2 / 14))
    ink = ImageDraw.Draw(base)
    ink.ellipse((cx - r - ring, top + track_h / 2 - r - ring, cx + r + ring, top + track_h / 2 + r + ring), fill=_CARD_INK + (255,))
    ink.ellipse((cx - r, top + track_h / 2 - r, cx + r, top + track_h / 2 + r), fill=dot + (255,))


def _draw_spark(base, x, y, w, h, closes, up=None):
    """网页里那条细线。颜色跟着旁边的 24 小时涨跌幅，不跟这 24 根的首尾比。"""
    vals = [float(v) for v in (closes or []) if v is not None][-24:]
    if len(vals) < 2:
        return
    from PIL import Image, ImageDraw

    lo, hi = min(vals), max(vals)
    span = hi - lo or 1.0
    pad = max(1, h * 1.5 / 18)
    pts = []
    last = len(vals) - 1
    for i, value in enumerate(vals):
        px = x + pad + (w - 2 * pad) * (i / last)
        py = y + pad + (h - 2 * pad) * (1 - (value - lo) / span)
        pts.append((px, py))
    if up is None:
        up = vals[-1] >= vals[0]
    color = _CARD_UP if up else _CARD_DOWN
    opacity = 0.16 if up else 0.14
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.polygon([(pts[0][0], y + h), *pts, (pts[-1][0], y + h)], fill=(*color, int(255 * opacity)))
    draw.line(pts, fill=color + (255,), width=max(2, round(h * 1.25 / 18)), joint="curve")
    base.alpha_composite(overlay)


def render_market_card_png(
    *,
    daily_pnl: float,
    floating: float,
    position_count: int,
    max_positions: int,
    cells: list,
    positions: Optional[list] = None,
    now: Optional[datetime] = None,
    signal: Optional[str] = None,
) -> Optional[bytes]:
    """按预览 HTML 的密度画整张市场播报。飞书分栏撑不出这个版式。"""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    import io
    from zoneinfo import ZoneInfo

    scale = 4

    def u(n):
        return n * scale

    card_w = u(360)
    margin = u(12)
    width = card_w + margin * 2
    pad = u(16)
    gap = u(8)
    meter_w = u(118)
    spark_w = u(64)
    origin = margin
    meter_x = origin + pad + u(130) + gap
    spark_x = meter_x + meter_w + gap
    right = origin + card_w - pad
    font_title = _cjk(u(15), "medium")
    font_m = _cjk(u(13), "medium")
    font_s = _cjk(u(12))
    font_n = _cjk(u(20), "medium")
    font_l = _cjk(u(11))
    font_k = _cjk(u(10))
    font_px = _cjk(u(14), "medium")
    font_tag = _cjk(u(10), "medium")
    if now is None:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
    elif now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    else:
        now = now.astimezone(ZoneInfo("Asia/Shanghai"))

    pos_list = list(positions or [])
    image = Image.new("RGBA", (width, u(2200)), (12, 14, 18, 255))
    plate = Image.new("RGBA", (card_w, u(2200)), _CARD_INK + (255,))
    image.paste(plate, (margin, margin))
    draw = ImageDraw.Draw(image)

    def tone(value: float):
        if value > 0:
            return _CARD_UP
        if value < 0:
            return _CARD_DOWN
        return _CARD_TEXT

    y = margin + pad
    _draw_tracked(draw, (origin + pad, y), "市场播报", font_title, _CARD_TEXT, tracking=u(1.2))
    clock = f"{now:%H:%M}"
    draw.text((right - _text_width(font_s, clock), y + u(2)), clock, font=font_s, fill=_CARD_MUTED)
    y += u(18 + 16)
    draw.line([(origin + pad, y), (right, y)], fill=_CARD_LINE + (255,), width=max(1, scale // 2))
    y += u(14)
    stats = [
        (f"{float(daily_pnl):+.2f}", "已实现", tone(float(daily_pnl))),
        (f"{float(floating):+.2f}", "浮动", tone(float(floating))),
        (f"{int(position_count)}/{int(max_positions)}", "持仓", _CARD_TEXT),
    ]
    col = (card_w - pad * 2) / 3
    for i, (value, label, color) in enumerate(stats):
        x = origin + pad + col * i
        if i:
            draw.line([(x, y), (x, y + u(34))], fill=_CARD_LINE + (255,), width=max(1, scale // 2))
            x += u(12)
        draw.text((x, y), value, font=font_n, fill=color + (255,))
        draw.text((x, y + u(24)), label, font=font_l, fill=_CARD_MUTED + (255,))
    y += u(20 + 4 + 11 + 14)
    if signal:
        hot = signal == "有信号"
        pill_ink = (230, 194, 122) if hot else _CARD_MUTED
        pill_fill = _blend((230, 194, 122), 0.12) if hot else _blend((255, 255, 255), 0.04)
        dot = (230, 194, 122) if hot else (109, 115, 128)
        tw = _text_width(font_s, signal)
        ph = u(18)
        draw.rounded_rectangle(
            (origin + pad, y, origin + pad + tw + u(22), y + ph),
            radius=ph // 2,
            fill=pill_fill + (255,),
        )
        draw.ellipse((origin + pad + u(7), y + u(6), origin + pad + u(13), y + u(12)), fill=dot + (255,))
        draw.text((origin + pad + u(16), y + u(2)), signal, font=font_s, fill=pill_ink + (255,))
        y += ph
    y += u(12)
    draw.text((meter_x, y), "≤30 开多", font=font_k, fill=_CARD_UP + (255,))
    right_label = "≥70 开空"
    draw.text((meter_x + meter_w - _text_width(font_k, right_label), y), right_label, font=font_k, fill=_CARD_DOWN + (255,))
    y += u(14)

    left = origin + pad
    for cell in cells or []:
        y += u(11)
        name = cell["symbol"]
        side = _cell_side(cell)
        draw.ellipse((left, y + u(4), left + u(6), y + u(10)), fill=_COIN_DOT.get(name, _CARD_MUTED) + (255,))
        draw.text((left + u(13), y), name, font=font_m, fill=_CARD_TEXT + (255,))
        nx = left + u(13) + _text_width(font_m, name) + u(6)
        if side and nx < meter_x - u(4):
            tag = "做多" if side == "long" else "做空"
            color = _CARD_UP if side == "long" else _CARD_DOWN
            tw = _text_width(font_tag, tag)
            draw.rounded_rectangle(
                (nx, y + u(1), nx + tw + u(12), y + u(15)),
                radius=u(8),
                fill=_blend(color, 0.14) + (255,),
            )
            draw.text((nx + u(6), y + u(2)), tag, font=font_tag, fill=color + (255,))
            nx += tw + u(16)
        if cell.get("holding") and nx < meter_x - u(2):
            draw.text((nx, y + u(2)), "持仓", font=font_k, fill=_CARD_MUTED + (255,))
        price = cell.get("price_txt") or "—"
        change = cell.get("change")
        change_txt = "—" if change is None else f"{float(change):+.2f}%"
        change_color = (197, 200, 208) if change is None or float(change) == 0 else tone(float(change))
        draw.text((right - _text_width(font_s, change_txt), y + u(1)), change_txt, font=font_s, fill=change_color + (255,))
        draw.text((meter_x + meter_w - _text_width(font_px, price), y - u(1)), price, font=font_px, fill=_CARD_TEXT + (255,))
        rsi = cell.get("rsi")
        if cell.get("layout", "rsi") != "rsi" or "rsi" not in cell:
            rsi_txt = cell.get("rsi_text") or "—"
            rsi_color = _CARD_MUTED
        elif rsi is None:
            rsi_txt = "缓存不足"
            rsi_color = _CARD_MUTED
        else:
            rsi_txt = f"{float(rsi):.1f}"
            rsi_color = _CARD_UP if side == "long" else _CARD_DOWN if side == "short" else _CARD_MUTED
        if cell.get("no_open"):
            rsi_txt += " · 不开仓"
        sub_y = y + u(16 + 5)
        draw.text((left, sub_y), rsi_txt, font=font_s, fill=rsi_color + (255,))
        if rsi is not None and cell.get("layout", "rsi") == "rsi":
            _draw_meter(image, meter_x, sub_y, meter_w, u(14), rsi, side)
        spark_up = None
        if change is not None and float(change) != 0 and float(change) == float(change):
            spark_up = float(change) > 0
        _draw_spark(image, spark_x, sub_y, spark_w, u(18), cell.get("closes") or [], up=spark_up)
        y = sub_y + u(18 + 11)
        draw.line([(left, y), (right, y)], fill=_CARD_LINE + (255,), width=max(1, scale // 2))

    if pos_list:
        y += u(12)
        draw.text((left, y), "持仓", font=font_l, fill=_CARD_MUTED + (255,))
        y += u(8 + 14)
        for pos in pos_list:
            draw.line([(left, y), (right, y)], fill=_CARD_LINE + (255,), width=max(1, scale // 2))
            y += u(8)
            side_name = pos.get("side") or ""
            side_color = _CARD_UP if side_name == "多" else _CARD_DOWN
            draw.text((left, y), pos.get("symbol") or "", font=font_m, fill=_CARD_TEXT + (255,))
            draw.text((left + u(46), y + u(1)), side_name, font=font_s, fill=side_color + (255,))
            pnl = float(pos.get("pnl") or 0)
            pnl_txt = f"{pnl:+.2f}"
            unit = "USDT"
            unit_w = _text_width(font_k, unit)
            draw.text((right - unit_w, y + u(3)), unit, font=font_k, fill=_CARD_MUTED + (255,))
            draw.text((right - unit_w - u(4) - _text_width(font_px, pnl_txt), y), pnl_txt, font=font_px, fill=tone(pnl) + (255,))
            sub = f"开仓 {pos.get('entry_txt') or '—'} · 现价 {pos.get('mark_txt') or '—'}"
            pct = float(pos.get("pct") or 0)
            pct_txt = f"{pct:+.2f}%"
            draw.text((left, y + u(18)), sub, font=font_s, fill=_CARD_MUTED + (255,))
            draw.text((right - _text_width(font_k, pct_txt), y + u(20)), pct_txt, font=font_k, fill=tone(pct) + (255,))
            y += u(18 + 16)

    used = min(image.height, int(y + u(12) + margin))
    image = image.crop((0, 0, width, used))
    # 圆角按网页的 18 像素裁，外圈用页面底色，避免飞书把透明角涂白。
    mask = Image.new("L", (card_w, image.height - margin * 2), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, card_w - 1, mask.height - 1), radius=u(18), fill=255)
    card = image.crop((margin, margin, margin + card_w, image.height - margin))
    flat = Image.new("RGB", image.size, (12, 14, 18))
    flat.paste(card.convert("RGB"), (margin, margin), mask)
    buf = io.BytesIO()
    flat.save(buf, format="PNG")
    return buf.getvalue()


def push_hourly_report(
    positions: list,
    prices: dict,
    indicators: dict,
    daily_pnl: float,
    fg: int,
    indicator_engine=None,
    score_map: Optional[dict] = None,
    refresh_indicators: bool = True,
    price_stream=None,
):
    """推送整点汇报。rsi_fade 展示已收盘 1h RSI；旧评分路径才用 score_map。"""
    prices, indicators = prepare_hourly_report_data(
        prices, indicators, indicator_engine, refresh_indicators=refresh_indicators,
    )

    # 恐惧贪婪不再放上整点卡片。fg 仍由调用方传入，这里不参与排版。
    _ = fg

    # 优先用 Binance API 浮动盈亏（与 query_positions / 飞书问答一致）
    api_by_symbol = {}
    try:
        from binance_auto_trade import get_all_positions, as_position_list
        for ap in as_position_list(get_all_positions()):
            sym = ap.get("symbol")
            if sym:
                api_by_symbol[sym] = ap
    except Exception as e:
        log.warning(f"⚠️ 整点汇报拉 API 持仓失败，用本地计算：{e}")

    # 构建持仓列表（包含价格、评分、盈亏）
    pos_lines = []
    pos_views = []
    total_floating = 0.0
    log.info(f"📋 整点汇报：持仓数={len(positions)}, prices 缓存={len(prices)}")
    for pos in positions:
        normalize_position(pos)
        symbol = pos['symbol']
        pos_type = pos['type']
        entry_price = float(pos.get('entry_price', 0) or 0)
        amount = position_amount(pos)  # 本地缓存数量（可能已被反转重建修正）
        api_pos = api_by_symbol.get(symbol, {})

        if api_pos:
            api_amount = abs(float(api_pos.get("amount", 0) or 0))
            # 检测交易所持仓数量与本地缓存的偏差（可能是历史 bug 残留仓位）
            if api_amount > 0 and amount != api_amount:
                deviation = abs(amount - api_amount) / max(amount, api_amount)
                if deviation > 0.05:  # 偏差 >5% 才告警
                    log.warning(
                        f"⚠️ {symbol} 交易所数量({api_amount:.1f})与本地缓存({amount:.1f})不一致，"
                        f"偏差 {deviation:.1%}，可能是历史 bug 残留仓位"
                    )
            if api_pos.get("unrealized_pnl") is not None and api_amount > 0:
                unrealized_pnl = float(api_pos["unrealized_pnl"])
                current_price = float(
                    api_pos.get("current_price") or api_pos.get("mark_price") or 0
                ) or prices.get(symbol, {}).get('price', entry_price)
            else:
                current_price = prices.get(symbol, {}).get('price', entry_price)
                # 用 API 真实数量算盈亏（不回退到本地缓存）
                calc_qty = api_amount if api_amount > 0 else amount
                unrealized_pnl = (
                    (current_price - entry_price) * calc_qty
                    if pos_type == 'LONG'
                    else (entry_price - current_price) * calc_qty
                )
        else:
            current_price = prices.get(symbol, {}).get('price', entry_price)
            unrealized_pnl = (
                (current_price - entry_price) * amount
                if pos_type == 'LONG'
                else (entry_price - current_price) * amount
            )

        log.info(
            f"  📍 {symbol} {pos_type}: entry={entry_price}, amount={amount}, "
            f"current={current_price}, pnl={unrealized_pnl:+.2f}"
        )

        notional = entry_price * amount
        pnl_pct = (unrealized_pnl / notional * 100) if notional > 0 else 0

        display_price = prices.get(symbol, {}).get("price") or current_price
        total_floating += unrealized_pnl
        side = "多" if pos_type == "LONG" else "空"
        pnl_txt = f"{unrealized_pnl:+.2f} USDT（{pnl_pct:+.2f}%）"
        if unrealized_pnl > 0:
            pnl_txt = f"<font color='green'>{pnl_txt}</font>"
        elif unrealized_pnl < 0:
            pnl_txt = f"<font color='red'>{pnl_txt}</font>"
        pos_lines.append(
            f"**{symbol}** {side}\n"
            f"开仓 {_format_px(entry_price)} · 现价 {_format_px(display_price)}\n"
            f"盈亏 {pnl_txt}"
        )
        pos_views.append({
            "symbol": symbol,
            "side": side,
            "entry_txt": _format_px(entry_price),
            "mark_txt": _format_px(display_price),
            "pnl": unrealized_pnl,
            "pct": pnl_pct,
        })

    rsi_mode = entry_is_rsi_fade()
    no_open = [
        sym for sym in CONFIG["symbols"]
        if max_positions_for_symbol(sym) <= 0
    ]
    now_ms = int(time.time() * 1000)
    cells = build_hourly_coin_cells(
        CONFIG["symbols"], prices, indicators, score_map, positions,
        rsi_fade=rsi_mode,
        price_stream=price_stream,
        now_ms=now_ms,
        no_open_symbols=no_open,
    )
    elements = _market_card_elements(
        daily_pnl=daily_pnl,
        floating=total_floating,
        position_count=len(positions),
        max_positions=int(CONFIG["max_positions"]),
        cells=cells,
        position_lines=pos_lines,
        position_views=pos_views,
    )
    return push_feishu_card(HOURLY_CARD_TITLE, elements, HOURLY_CARD_TEMPLATE, header=False)


def _market_card_elements(
    *,
    daily_pnl: float,
    floating: float,
    position_count: int,
    max_positions: int,
    cells: list,
    position_lines: list,
    position_views: Optional[list] = None,
    now: Optional[datetime] = None,
    upload=None,
) -> list:
    """整张图能传上去就用图。失败再退回文字行，避免飞书把小图撑成大色块。"""
    from zoneinfo import ZoneInfo

    if now is None:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
    signal = hourly_signal_label(cells)
    try:
        png = render_market_card_png(
            daily_pnl=daily_pnl,
            floating=floating,
            position_count=position_count,
            max_positions=max_positions,
            cells=cells,
            positions=position_views or [],
            now=now,
            signal=signal,
        )
    except Exception as exc:
        log.warning("市场播报画不出来：%s", type(exc).__name__)
        png = None
    key = None
    if png:
        try:
            if upload is None:
                from feishu_helper import upload_message_image
                upload = upload_message_image
            key = upload(png)
        except Exception as exc:
            log.warning("市场播报图片上传失败：%s", type(exc).__name__)
            key = None
    if key:
        return [{
            "tag": "img",
            "img_key": key,
            "alt": {"tag": "plain_text", "content": "市场播报"},
            "mode": "fit_horizontal",
            "preview": True,
        }]
    return build_hourly_broadcast_elements(
        daily_pnl=daily_pnl,
        floating=floating,
        position_count=position_count,
        max_positions=max_positions,
        cells=cells,
        position_lines=position_lines,
        images={},
        now=now,
    )


# ═══════════════════════════════════════════════════════════════
# 十五、整点汇报
# ═══════════════════════════════════════════════════════════════
def hourly_report(
    positions: list,
    prices: dict,
    indicators: dict,
    circuit,
    fg: int,
    indicator_engine=None,
    score_map: Optional[dict] = None,
    refresh_indicators: bool = True,
    price_stream=None,
):
    global _last_balance_snapshot
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    sync_daily_pnl_from_exchange(circuit)

    # 📊 账户余额快照（统一 PnL 口径：每日余额差 ≈ 当日真实盈亏）
    try:
        from binance_auto_trade import get_account_balance
        balance = get_account_balance()
        if balance and balance > 0:
            _last_balance_snapshot = balance
            log.info(f"💰 账户余额快照：{balance:.2f} USDT")
            write_alert(f"💰 账户余额快照：{balance:.2f} USDT")
    except Exception as _be:
        log.debug(f"账户余额快照获取失败：{_be}")

    # 如果数据为空，从 API 重新获取
    if not positions:
        log.warning("⚠️ 小时汇报时 positions 为空，从 API 重新获取...")
        try:
            from binance_auto_trade import get_all_positions, as_position_list
            api_positions = as_position_list(get_all_positions())
            if api_positions:
                positions = []
                for p in api_positions:
                    amt = float(p.get('amount', 0))
                    entry = float(p.get('entry_price', 0))
                    # 🔧 2026-06-06 修复：过滤粉尘仓位
                    if amt != 0 and abs(amt) * entry >= get_risk_config().dust_notional_usdt:
                        symbol = p['symbol']
                        # 🐛 Bug 修复：从 API 同步时计算合理的止盈止损（基于 entry_price ±3%）
                        is_long = amt > 0
                        tp1 = round(entry * 1.03, 4) if is_long else round(entry * 0.97, 4)
                        tp2 = round(entry * 1.06, 4) if is_long else round(entry * 0.94, 4)
                        sl = round(entry * 0.97, 4) if is_long else round(entry * 1.03, 4)

                        pos = {
                            'symbol': symbol,
                            'type': 'SHORT' if amt < 0 else 'LONG',
                            'entry_price': entry,
                            'entry_time': datetime.now().isoformat(),
                            'qty': abs(amt),
                            'amount': abs(amt),
                            'score': 55,
                            'regime': 'trending',
                            'ai_result': None,
                            'order_result': {'success': True, 'message': '从 API 同步'},
                            'high_24h': 0.0,
                            'low_24h': 0.0,
                            'tp1_price': tp1,
                            'tp2_price': tp2,  # 保留兼容（汇报用）
                            'sl_price': sl,
                            'tp1_hit': False,  # 保留兼容
                            'size_remaining': 1.0,  # 保留兼容
                            'peak_pnl': 0.0,
                            'tp_pct': get_exit_config().base_tp_pct,  # 汇报用默认值
                        }
                        positions.append(pos)
                log.info(f"✅ 小时汇报从 API 恢复 {len(positions)} 个持仓")
        except Exception as e:
            log.error(f"❌ 小时汇报获取持仓失败：{e}")

    lines = [
        f"\n{'='*50}",
        f"📊 整点汇报 {now}",
        f"恐惧贪婪指数: {fg} ({'极度恐惧' if fg<25 else '恐惧' if fg<40 else '中性' if fg<60 else '贪婪' if fg<75 else '极度贪婪'})",
        f"今日PnL: {circuit.daily_pnl:+.2f} USDT ({circuit.daily_pnl/CONFIG['total_capital']*100:+.1f}%)",
        f"持仓数: {len(positions)}/{CONFIG['max_positions']}",
    ]
    for pos in positions:
        normalize_position(pos)
        sym = pos["symbol"]
        p   = prices.get(sym, {}).get("price", 0)
        entry = pos["entry_price"]
        typ = pos["type"]
        amt = position_amount(pos)
        if amt > 0 and p > 0:
            pnl_usdt = (p - entry) * amt if typ == "LONG" else (entry - p) * amt
            pnl_pct = pnl_usdt / (entry * amt) * 100 if entry > 0 else 0
        else:
            pnl_pct = (p - entry) / entry if typ == "LONG" else (entry - p) / entry
            pnl_pct *= 100
            pnl_usdt = 0
        lines.append(
            f"  {'🟢' if typ=='LONG' else '🔴'} {sym} {typ} @{entry:.4f} "
            f"现价:{p:.4f} PnL:{pnl_usdt:+.2f}U ({pnl_pct:+.2f}%)"
        )
    for sym in CONFIG["symbols"]:
        pd = prices.get(sym, {})
        if pd.get('price', 0):  # 只显示有价格的币种
            lines.append(
                f"  {sym}: {pd.get('price',0):.4f} ({pd.get('change_24h',0):+.2f}%)"
            )
    lines.append("=" * 50)
    report = "\n".join(lines)
    log.info(report)
    write_alert(report)

    # rsi_fade 只记已收盘 1h RSI。旧评分路径仍记评分详情。
    if entry_is_rsi_fade():
        rsi_lines = build_hourly_coin_lines(
            CONFIG["symbols"], prices, indicators, score_map, positions,
            rsi_fade=True,
            price_stream=price_stream,
            now_ms=int(time.time() * 1000),
            no_open_symbols=[
                sym for sym in CONFIG["symbols"] if max_positions_for_symbol(sym) <= 0
            ],
        )
        log.info("📊 整点 1h RSI (%s):\n%s", now, "\n".join(rsi_lines))
    else:
        score_lines = [f"\n📊 评分详情 ({now}):"]
        for sym in CONFIG["symbols"]:
            pd = prices.get(sym, {})
            ind = indicators.get(sym, {})
            if pd.get('price', 0) and ind:
                score_info = (score_map or {}).get(sym) or {}
                score = score_info.get("score", 0)
                direction = score_info.get("direction", "NONE")
                rsi = ind.get('rsi', 0)
                ma60_val = ind.get('ema60_15m')
                ma60_ok = "✓" if (ma60_val and pd.get('price', 0) >= ma60_val) else "✗"
                score_lines.append(
                    f"  {sym}: {score}分 (RSI:{rsi:.0f}, 24h:{pd.get('change_24h',0):+.1f}%, MA60:{ma60_ok}) → 信号：{direction}"
                )
        log.info("\n".join(score_lines))

    # 推送飞书（带错误检查和重试）
    try:
        success = push_hourly_report(
            positions, prices, indicators, circuit.daily_pnl, fg, indicator_engine, score_map,
            refresh_indicators=refresh_indicators,
            price_stream=price_stream,
        )
        if not success:
            log.warning("⚠️ 小时汇报推送失败，已跳过")
    except Exception as e:
        log.error(f"❌ 小时汇报推送异常：{e}")
