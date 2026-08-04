#!/usr/bin/env python3
"""
notify_layer.py — 推送层

职责：交易信号告警（push_signal_alert）、整点汇报（hourly_report / push_hourly_report）、
告警落盘（write_alert）、今日盈亏校准（sync/restore_daily_pnl）、账户余额快照。
依赖方向：依赖 strategy_layer / data_layer / position_store / feishu_helper；
cooldown_manager 由 main 通过 set_cooldown_manager() 注入。
"""
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from config import get_config
from feishu_helper import get_token as get_feishu_token, push_card as push_feishu_card
from openclaw_logging import daily_alert_path

from strategy_layer import calc_score
from data_layer import IndicatorEngine, detect_regime
from position_store import normalize_position, position_amount

CONFIG = get_config()
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
    global cooldown_manager
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

    # 颜色和 emoji
    if sig_type == 'LONG':
        emoji = '🟢'
        template = 'green'
        type_text = '开多'
    elif sig_type == 'SHORT':
        emoji = '🔴'
        template = 'red'
        type_text = '开空'
    else:
        emoji = '⚠️'
        template = 'blue'
        type_text = action

    is_merged = bool(merged_close_count) and action in ['开多', '开空']

    content = f"**{emoji} 币种：** {symbol}\n"
    content += f"**📊 方向：** {type_text}\n"

    if action in ['开多', '开空']:
        content += f"**💰 开仓价：** ${price:,.4f}\n"
    elif action in ['止盈', '止损', '平仓']:
        if trigger_price and trigger_price != price:
            content += f"**🎯 触发价：** ${trigger_price:,.4f}\n"
            content += f"**💰 成交价：** ${price:,.4f}（市价滑点）\n"
        else:
            content += f"**💰 成交价：** ${price:,.4f}\n"
    else:
        content += f"**💰 价格：** ${price:,.4f}\n"

    content += f"**📈 评分：** {score}/100\n"
    content += f"**🎯 状态：** {regime}\n"

    if action in ['开多', '开空'] and tp_price and sl_price:
        content += f"**🎯 止盈：** ${tp_price:,.4f}\n"
        content += f"**🛑 止损：** ${sl_price:,.4f}\n"

    if action in ['止盈', '止损', '平仓']:
        content += f"**💰 盈亏：** {pnl:+.4f} USDT\n"
        if merged_count and merged_count > 1:
            content += f"**📦 合并：** {merged_count} 次平仓\n"

    if merged_pnl != 0 and is_merged:
        content += f"\n---\n**📦 缓冲区合并播报（5分钟内同币种汇总）**\n"
        content += f"**💰 期间已平仓盈亏：** {merged_pnl:+.4f} USDT\n"
        content += f"**🔢 期间平仓次数：** {merged_close_count} 次\n"
    elif merged_pnl != 0:
        content += f"**💰 累计盈亏：** {merged_pnl:+.4f} USDT\n"
        if merged_close_count:
            content += f"**📦 包含：** {merged_close_count} 次平仓\n"

    if order_id:
        content += f"**🆔 订单：** `{order_id}`\n"

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": content
            }
        },
        {
            "tag": "note",
            "elements": [{
                "tag": "plain_text",
                "content": f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC+8) · ⚠️ 仅供参考，注意风险"
            }]
        }
    ]

    if is_merged:
        title = f"📊 {symbol} 交易汇总（最新：{action}）"
    else:
        title = f"🚨 {action} - {symbol}"
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
        eng = IndicatorEngine()
        r = eng.session.get(
            f"{CONFIG['binance_futures']}/ticker/24hr",
            timeout=15,
        )
        if r.status_code != 200:
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


def _price_data_for_score(symbol: str, pd: dict, ind: dict) -> dict:
    """整点汇报评分：价格/成交量与指标引擎同源"""
    price = float(pd.get("price") or ind.get("last_close") or 0)
    return {
        "price": price,
        "volume": float(ind.get("vol_current") or pd.get("volume") or 0),
        "change_24h": float(pd.get("change_24h", 0) or 0),
    }


def _regime_label(regime: str) -> str:
    return {"trending": "趋势", "ranging": "震荡", "volatile": "高波动"}.get(regime, regime)


def _format_hourly_score_line(symbol: str, pd: dict, ind: dict, regime: str, fg: int, fr: float, btc_ind: dict) -> str:
    """整点汇报单行：评分 + RSI + 市场状态（与飞书问答表格一致）"""
    score_pd = _price_data_for_score(symbol, pd, ind)
    score, direction = calc_score(symbol, score_pd, ind, regime, fg, fr, btc_ind)
    rsi = ind.get("rsi", 0)
    return f"{score}分 RSI{rsi:.0f} {_regime_label(regime)} → {direction}"


def prepare_hourly_report_data(
    prices: dict,
    indicators: dict,
    indicator_engine: Optional["IndicatorEngine"] = None,
) -> tuple[dict, dict]:
    """
    整点汇报前强制刷新：价格涨跌幅 + 全币种指标。
    避免整点时刻 indicators 未更新、change_24h 为 0、评分为 0。
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

    engine = indicator_engine or IndicatorEngine()
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
    indicator_engine: Optional["IndicatorEngine"] = None,
):
    """推送整点汇报（显示所有币种的价格和评分）"""
    prices, indicators = prepare_hourly_report_data(prices, indicators, indicator_engine)
    btc_ind = indicators.get("BTC", {})

    # 恐惧贪婪描述
    if fg < 25:
        fg_text = "极度恐惧"
        fg_color = "🔴"
    elif fg < 40:
        fg_text = "恐惧"
        fg_color = "🟠"
    elif fg < 60:
        fg_text = "中性"
        fg_color = "🟡"
    elif fg < 75:
        fg_text = "贪婪"
        fg_color = "🟢"
    else:
        fg_text = "极度贪婪"
        fg_color = "🔵"

    # 优先用 Binance API 浮动盈亏（与 query_positions / 飞书问答一致）
    api_by_symbol = {}
    try:
        from binance_auto_trade import get_all_positions
        for ap in get_all_positions() or []:
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
        open_score = pos.get("score", "—")
        total_floating += unrealized_pnl
        pos_lines.append(
            f"**{symbol}** {pos_type} | 开仓：${entry_price:,.2f} | "
            f"现价：${display_price:,.2f} | 开仓评分：{open_score} | "
            f"盈亏：${unrealized_pnl:+.2f} ({pnl_pct:+.2f}%)"
        )

    # 构建所有币种的价格和评分列表
    all_coins_lines = []

    for symbol in CONFIG["symbols"]:
        if prices.get(symbol):
            price = prices[symbol].get('price', 0)
            change_24h = prices[symbol].get('change_24h', 0)

            ind = indicators.get(symbol, {})
            regime = detect_regime(ind) if ind else "ranging"
            fr = 0
            score_txt = _format_hourly_score_line(
                symbol, prices[symbol], ind, regime, fg, fr, btc_ind
            ) if ind else "—分（指标未就绪）"

            holding = "📌" if any(p['symbol'] == symbol for p in positions) else "  "
            all_coins_lines.append(
                f"{holding} **{symbol}** ${price:,.2f} ({change_24h:+.2f}%) | {score_txt}"
            )

    # 构建卡片元素（使用 div 标签）
    content_lines = [
        f"**📊 今日已实现：** {daily_pnl:+.2f} USDT",
        f"**💧 浮动盈亏：** {total_floating:+.2f} USDT",
        f"**😨 恐惧贪婪：** {fg_color} {fg_text} ({fg})",
        f"**📦 持仓数：** {len(positions)}/{CONFIG['max_positions']}"
    ]

    content_lines.append("")
    content_lines.append("**📊 全部币种:**")
    content_lines.extend(all_coins_lines)

    if pos_lines:
        content_lines.append("")
        content_lines.append("**📌 持仓详情:**")
        content_lines.extend(pos_lines)

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(content_lines)
            }
        },
        {
            "tag": "note",
            "elements": [{
                "tag": "plain_text",
                "content": f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)"
            }]
        }
    ]

    title = "📊 整点汇报"
    return push_feishu_card(title, elements, "blue")


# ═══════════════════════════════════════════════════════════════
# 十五、整点汇报
# ═══════════════════════════════════════════════════════════════
def hourly_report(
    positions: list,
    prices: dict,
    indicators: dict,
    circuit,
    fg: int,
    indicator_engine: Optional["IndicatorEngine"] = None,
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
            from binance_auto_trade import get_all_positions
            api_positions = get_all_positions()
            if api_positions:
                positions = []
                for p in api_positions:
                    amt = float(p.get('amount', 0))
                    entry = float(p.get('entry_price', 0))
                    # 🔧 2026-06-06 修复：过滤粉尘仓位
                    if amt != 0 and abs(amt) * entry >= 5.0:
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
                            'tp_pct': 0.04,  # 汇报用默认值
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

    # 📊 添加各币种评分详情日志（2026-03-27 老公指示）
    score_lines = [f"\n📊 评分详情 ({now}):"]
    btc_ind = indicators.get("BTC", {})  # 2026-03-28 老公指示：用 BTC 代替 ETH 作为大盘参考
    for sym in CONFIG["symbols"]:
        pd = prices.get(sym, {})
        ind = indicators.get(sym, {})
        if pd.get('price', 0) and ind:
            regime = detect_regime(ind)
            score, direction = calc_score(sym, pd, ind, regime, fg, 0, btc_ind)
            rsi = ind.get('rsi', 0)
            # MA60 仅作信息展示：评分模型（突破/成交量/ATR/BTC方向/情绪）不使用单币 MA60 过滤
            ma60_val = ind.get('ema60_15m')
            ma60_ok = "✓" if (ma60_val and pd.get('price', 0) >= ma60_val) else "✗"
            score_lines.append(
                f"  {sym}: {score}分 (RSI:{rsi:.0f}, 24h:{pd.get('change_24h',0):+.1f}%, MA60:{ma60_ok}) → 信号：{direction}"
            )
    score_log = "\n".join(score_lines)
    log.info(score_log)

    # 推送飞书（带错误检查和重试）
    try:
        success = push_hourly_report(
            positions, prices, indicators, circuit.daily_pnl, fg, indicator_engine
        )
        if not success:
            log.warning("⚠️ 小时汇报推送失败，已跳过")
    except Exception as e:
        log.error(f"❌ 小时汇报推送异常：{e}")
