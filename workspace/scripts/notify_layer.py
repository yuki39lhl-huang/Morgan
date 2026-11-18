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
        elif not isinstance(pd, dict):
            pd = {}
        if pd.get("price", 0) <= 0:
            continue
        if not pd.get("change_24h") and pd.get("open_24h", 0) > 0:
            o = float(pd["open_24h"])
            p = float(pd["price"])
            pd["change_24h"] = (p - o) / o * 100
        pd.setdefault("change_24h", 0.0)
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
        cells.append({
            "symbol": symbol,
            "price_txt": _format_px(price),
            "change_md": format_change_md(change),
            "rsi_text": status,
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


def _summary_row(daily_pnl: float, floating: float, position_count: int, max_positions: int) -> dict:
    def item(label: str, value: str) -> dict:
        return column([md(f"<font color='grey'>{label}</font>\n**{value}**")], 1)

    return row(
        item("已实现", f"{float(daily_pnl):+.2f}"),
        item("浮动", f"{float(floating):+.2f}"),
        item("持仓", f"{int(position_count)}/{int(max_positions)}"),
        background_style="grey",
        flex_mode="trisect",
    )


def _tile_rows(columns: list) -> list:
    rows = []
    for i in range(0, len(columns), 2):
        group = columns[i:i + 2]
        mode = "bisect" if len(group) == 2 else "stretch"
        rows.append(row(*group, background_style="grey", flex_mode=mode))
    return rows


def _coin_column(cell: dict, images: Optional[dict]) -> dict:
    symbol = cell["symbol"]
    slot = (images or {}).get(symbol) or {}
    icon = slot.get("icon")
    spark = slot.get("spark")
    name = f"**{symbol}**"
    if cell.get("holding"):
        name += " 持仓"
    # 价格和涨跌同一行，避免瓷片被撑得又高又空
    quote = f"**{cell['price_txt']}**  {cell['change_md']}\n{cell['rsi_text']}"
    elements = []
    if icon:
        elements.append(row(
            column([img(icon, symbol, custom_width=28)], 1),
            column([md(name)], 5),
            background_style="default",
            flex_mode="none",
        ))
    else:
        elements.append(md(name))
    if spark:
        elements.append(row(
            column([md(quote)], 3),
            column([img(spark, f"{symbol} 走势", custom_width=108)], 2),
            background_style="default",
            flex_mode="none",
        ))
    else:
        elements.append(md(quote))
    return column(elements, 1)


def _broadcast_footer(now: datetime, label: Optional[str]) -> dict:
    from zoneinfo import ZoneInfo

    shanghai = ZoneInfo("Asia/Shanghai")
    if now.tzinfo is None:
        now = now.replace(tzinfo=shanghai)
    else:
        now = now.astimezone(shanghai)
    columns = [column([md(f"<font color='grey'>数据更新时间：{now:%H:%M}</font>")], 3)]
    if label:
        columns.append(column([md(f"**{label}**")], 1))
    return row(*columns, background_style="default", flex_mode="none")


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
        hr(),
    ]
    elements.extend(_tile_rows([_coin_column(cell, images) for cell in cells]))
    if position_lines:
        elements.append(hr())
        elements.append(md("**持仓**"))
        elements.extend(_tile_rows([column([md(text)], 1) for text in position_lines]))
    elements.append(_broadcast_footer(now, hourly_signal_label(cells)))
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


def resolve_hourly_images(cells, upload=None, cache_path=None) -> dict:
    """上传币种图标和走势。任何一步失败都只让该图为空。"""
    import hashlib
    from pathlib import Path

    from feishu_helper import cached_message_image

    if cache_path is None:
        cache_path = Path(__file__).resolve().parent / ".feishu_img_cache.json"
    out = {}
    for cell in cells or []:
        symbol = str(cell.get("symbol") or "")
        slot = {"icon": None, "spark": None}
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
        if symbol:
            out[symbol] = slot
    return out


def prepare_hourly_report_data(
    prices: dict,
    indicators: dict,
    indicator_engine=None,
    refresh_indicators: bool = True,
) -> tuple[dict, dict]:
    """
    整点汇报前强制刷新：价格涨跌幅 + 全币种指标。
    避免整点时刻 indicators 未更新、change_24h 为 0、评分为 0。
    indicator_engine 由 main 装配层注入（本层不实例化数据层对象）。
    """
    prices = _normalize_price_data(prices)
    ticker_map = _fetch_24h_tickers()
    for sym, tk in ticker_map.items():
        if sym not in prices:
            prices[sym] = {}
        if tk.get("price", 0) > 0:
            prices[sym]["price"] = tk["price"]
        prices[sym]["change_24h"] = tk.get("change_24h", prices[sym].get("change_24h", 0))
        if not prices[sym].get("volume"):
            prices[sym]["volume"] = tk.get("volume", 0)

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
    images = {}
    try:
        images = resolve_hourly_images(cells)
    except Exception as e:
        log.warning("整点图片跳过：%s", type(e).__name__)

    elements = build_hourly_broadcast_elements(
        daily_pnl=daily_pnl,
        floating=total_floating,
        position_count=len(positions),
        max_positions=int(CONFIG["max_positions"]),
        cells=cells,
        position_lines=pos_lines,
        images=images,
    )
    return push_feishu_card(HOURLY_CARD_TITLE, elements, HOURLY_CARD_TEMPLATE)


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
