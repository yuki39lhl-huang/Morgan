#!/usr/bin/env python3
"""
execution_layer.py — 执行层

职责：开仓/平仓（open_position / close_position）、止盈止损检查、移动止损、
ATR 动态止损、Algo 条件单同步（提交/取消/更新）。
依赖方向：依赖 binance_auto_trade / feishu_helper / notify_layer / config；
cooldown_manager 由 main 通过 set_cooldown_manager() 注入。
"""
import logging
import time
from datetime import datetime
from typing import Optional

from config import get_config
from feishu_helper import push_card as push_feishu_card

from notify_layer import push_signal_alert, write_alert
from trade_features import record_close

CONFIG = get_config()
log = logging.getLogger(__name__)

# 自动交易模块导入
try:
    from binance_auto_trade import (
        place_order,
        get_all_positions,
        cancel_algo_orders,
        place_algo_conditional_order,
        request,
        format_quantity,
    )
    AUTO_TRADE_ENABLED = True
    log.info("✅ 自动交易模块已加载")
except Exception as e:
    AUTO_TRADE_ENABLED = False
    log.warning(f"⚠️ 自动交易模块未加载：{e}")

# 平仓后清除冷却：由 main 装配时注入 CooldownManager 实例
cooldown_manager = None


def set_cooldown_manager(cm) -> None:
    """注入信号冷却管理器实例（risk_layer.CooldownManager）供平仓后清冷却使用。"""
    global cooldown_manager
    cooldown_manager = cm


# 失败日志限频：同一事件在窗口内只记一次，防止确定性错误（-2022/-4164）刷屏
_fail_log_ts = {}
FAIL_LOG_COOLDOWN = 60  # 秒


def log_limited(key: str, msg: str, level: str = "warning"):
    """同 key 在 FAIL_LOG_COOLDOWN 秒内只输出一次日志。"""
    now = time.time()
    if now - _fail_log_ts.get(key, 0) < FAIL_LOG_COOLDOWN:
        return
    _fail_log_ts[key] = now
    getattr(log, level)(msg)


# ═══════════════════════════════════════════════════════════════
# 六、止盈止损检查（每15秒最轻量）
# ═══════════════════════════════════════════════════════════════
def check_exits_fast(positions: list, prices: dict) -> list[tuple]:
    """
    返回 [(position, reason, close_size_ratio)]
    纯价格比较，无任何计算，<5ms

    2026-03-28 老公指示：TP1 全平，无 TP2
    """
    exits = []
    for pos in positions:
        sym = pos["symbol"]
        p   = prices.get(sym, {}).get("price")
        if not p:
            continue

        tp1 = pos["tp1_price"]
        sl  = pos["sl_price"]

        # 跳过止盈止损为 0 的无效持仓
        if tp1 == 0.0 or sl == 0.0:
            log.warning(f"⚠️ {sym} 止盈止损无效 (TP1={tp1}, SL={sl})，跳过检查")
            continue

        typ = pos["type"]

        if typ == "LONG":
            if p >= tp1:
                exits.append((pos, "TP1", 1.0))  # 全平
            elif p <= sl:
                exits.append((pos, "SL", 1.0))   # 全平
        else:  # SHORT
            if p <= tp1:
                exits.append((pos, "TP1", 1.0))  # 全平
            elif p >= sl:
                exits.append((pos, "SL", 1.0))   # 全平

    return exits


def check_timeout_exits(positions: list, timeout_hours: float) -> list[tuple]:
    """持仓超时退出：持仓超过 timeout_hours 小时未触发 TP/SL → 市价平仓。

    动机（2026-08-10）：突破信号有半衰期。历史统计显示持仓超 48h 的单子
    最终 90% 以 SL 收场（最长挂 11.5 天等死），主动超时离场可：
      - 释放被占用的仓位（流动性差时也能换手，加快特征样本积累）
      - 避免突破失败的单子挂到更大的止损
    参数外置 config.json → timeout_exit（enabled / hours），单点可回滚。
    """
    if timeout_hours <= 0:
        return []
    now = datetime.now()
    exits = []
    for pos in positions:
        et = pos.get("entry_time", "")
        try:
            entry = datetime.fromisoformat(et)
        except (ValueError, TypeError):
            continue  # 无有效开仓时间（旧数据）不参与超时判断
        if (now - entry).total_seconds() / 3600 >= timeout_hours:
            exits.append((pos, "超时退出", 1.0))
    if exits:
        log.info(f"⏰ 超时退出检查：{len(exits)} 个持仓超过 {timeout_hours}h，市价平仓释放仓位")
    return exits


def update_trailing_stop(positions: list, prices: dict):
    """
    移动止盈 - 2026-06-06 老公指示：改为相对止盈比例（ATR 动态止盈联动）
    三档基于实际止盈比例动态计算：
    - 50%止盈进度 → 回撤=实际止盈×20%
    - 75%止盈进度 → 回撤=实际止盈×35%
    - 100%止盈 → 全平

    2026-03-31 老公指示：每次更新 sl_price 后同步到 Binance（取消旧单 + 提交新单）
    """
    for pos in positions:
        sym = pos["symbol"]
        p   = prices.get(sym, {}).get("price")
        if not p:
            continue

        entry = pos["entry_price"]
        typ   = pos["type"]
        tp1   = pos.get("tp1_price", 0)

        # 反推止盈比例（兼容旧数据无 tp_pct）
        tp_pct = pos.get("tp_pct", 0)
        if tp_pct == 0 and entry > 0 and tp1 > 0:
            tp_pct = abs(tp1 - entry) / entry

        if tp_pct == 0:
            continue

        pnl_pct = (p - entry) / entry if typ == "LONG" else (entry - p) / entry

        # 更新峰值盈亏 和 峰值价格
        if pnl_pct > pos.get("peak_pnl", 0):
            pos["peak_pnl"] = pnl_pct
            pos["peak_price"] = p  # ✅ 记录峰值价格

        # 找到当前利润对应的档位（取最高档）
        active_tier = None
        peak_pnl = pos.get("peak_pnl", 0)
        for tier in CONFIG["trailing_tiers"]:
            if peak_pnl >= tp_pct * tier["pnl_ratio"]:
                active_tier = tier
            else:
                break

        if not active_tier:
            continue

        # 第3档 - 全平
        if active_tier.get("gap", 0) >= 0.9:
            old_sl = pos["sl_price"]
            pos["sl_price"] = p * 0.999 if typ == "LONG" else p * 1.001
            sync_stop_loss_to_binance(sym, typ, pos["sl_price"], old_sl, pos.get("tp1_price", 0), qty=abs(pos.get("amount", 0)))
            continue

        # 前两档 - 按比例回撤
        gap = tp_pct * active_tier["gap_ratio"]
        peak_p = pos.get("peak_price", entry)

        if typ == "LONG":
            new_sl = peak_p * (1 - gap)
            if new_sl > pos["sl_price"]:
                old_sl = pos["sl_price"]
                pos["sl_price"] = round(new_sl, 4)
                sync_stop_loss_to_binance(sym, typ, pos["sl_price"], old_sl, pos.get("tp1_price", 0), qty=abs(pos.get("amount", 0)))
        else:
            new_sl = peak_p * (1 + gap)
            if new_sl < pos["sl_price"]:
                old_sl = pos["sl_price"]
                pos["sl_price"] = round(new_sl, 4)
                sync_stop_loss_to_binance(sym, typ, pos["sl_price"], old_sl, pos.get("tp1_price", 0), qty=abs(pos.get("amount", 0)))


# ATR 动态止损冷却跟踪
_atr_sl_last_update: dict[str, float] = {}  # symbol → 上次更新时间戳
_ATR_SL_COOLDOWN = 300  # 5 分钟冷却，防止频繁取消/重建 Algo 单


def update_atr_dynamic_stops(positions: list, indicators: dict) -> bool:
    """
    ATR 动态止损 — 持仓期间实时跟新。
    用当前 ATR 重算止损距离，只在未进入移动止盈档位且冷却期外时生效。

    2026-06-16 老公指示：ATR 倍率（4%-15%）应该在持仓期也动态跑，
    而不只是开仓时算一次。行情波动大了自动放宽，小了自动收紧。
    同次修改：加 5 分钟冷却 + 仅层级变化时更新，防止过度调用 Binance。
    """
    global _atr_sl_last_update
    changed = False
    now_ts = time.time()

    for pos in positions:
        sym = pos["symbol"]
        ind = indicators.get(sym)
        if not ind:
            continue

        atr_pct = ind.get("atr_pct", 0)
        if atr_pct <= 0:
            continue

        entry = pos["entry_price"]
        typ = pos["type"]
        tp_pct = pos.get("tp_pct", 0.04)

        # 已进入移动止盈档位 → 三档跟止损接管，ATR 不干预
        peak_pnl = pos.get("peak_pnl", 0)
        first_tier_threshold = tp_pct * CONFIG["trailing_tiers"][0]["pnl_ratio"]
        if peak_pnl >= first_tier_threshold:
            continue

        # 冷却期内跳过
        last_upd = _atr_sl_last_update.get(sym, 0)
        if now_ts - last_upd < _ATR_SL_COOLDOWN:
            continue

        # ATR 分层倍率（与 calc_tp_sl 一致）
        atr_mult = 1.0
        atr_tier = 0  # 0=<2%, 1=<4%, 2=≥4%
        tiers = CONFIG["atr_sl_tiers"]
        for i, (threshold, mult) in enumerate(tiers):
            if atr_pct < threshold:
                atr_mult = mult
                atr_tier = i
                break

        sl_pct = max(0.02, min(atr_pct * atr_mult, 0.15))

        if typ == "LONG":
            atr_sl = round(entry * (1 - sl_pct), 4)
        else:  # SHORT
            atr_sl = round(entry * (1 + sl_pct), 4)

        # 只有止损价格变化超过 0.1% 才动手（过滤噪音，也说明层级真变了）
        sl_change = abs(atr_sl - pos["sl_price"]) / pos["sl_price"] if pos["sl_price"] else 0
        if sl_change < 0.001:
            continue

        old_sl = pos["sl_price"]
        pos["sl_price"] = atr_sl
        sync_stop_loss_to_binance(sym, typ, atr_sl, old_sl, pos.get("tp1_price", 0),
                                  qty=abs(pos.get("amount", 0)))
        _atr_sl_last_update[sym] = now_ts
        log.info(f"📐 {sym} ATR动态止损{tiers[atr_tier][0]*100:.0f}%档(atr={atr_pct*100:.2f}%): "
                 f"{old_sl:.4f}→{atr_sl:.4f} (变化{sl_change*100:.1f}%)")
        changed = True

    return changed


def _cancel_algo_by_type(symbol_usdt: str, order_type: str):
    """取消某币种指定类型的 Algo 单（不影响另一边）"""
    try:
        result = request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol_usdt})
        if isinstance(result, list):
            for o in result:
                if o.get('orderType') == order_type and o.get('algoStatus') == 'NEW':
                    algo_id = o.get('algoId')
                    if algo_id:
                        request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})
    except Exception as e:
        log.warning(f"⚠️ 按类型取消 Algo 单失败 ({order_type})：{e}")


def sync_stop_loss_to_binance(symbol: str, typ: str, new_sl: float, old_sl: float, tp_price: float = 0, qty: float = 0):
    """
    2026-06-03 修复：精准替换止损/止盈，不碰另一边
    原逻辑 cancel_all → 止损失败 → 止盈也被删 → 永远缺一边
    2026-06-04 修复：传入精确 qty 代替 closePosition=true（测试网算错数量）
    """
    try:
        sym_usdt = f"{symbol}USDT"
        sl_side = "SELL" if typ == "LONG" else "BUY"

        # 只取消止损单，不动止盈
        _cancel_algo_by_type(sym_usdt, "STOP_MARKET")
        sl_result = place_algo_conditional_order(
            sym_usdt, sl_side, "STOP_MARKET", new_sl, quantity=qty
        )

        if isinstance(sl_result, dict) and sl_result.get("algoId"):
            log.info(f"📌 {symbol} 止损 Algo 单已更新：{old_sl:.4f} → {new_sl:.4f}")
        else:
            log.warning(f"⚠️ {symbol} 更新 Binance 止损单失败：{sl_result}")

        # 同时更新止盈（如果有变更，只取消止盈不动止损）
        if tp_price > 0:
            tp_side = "SELL" if typ == "LONG" else "BUY"
            _cancel_algo_by_type(sym_usdt, "TAKE_PROFIT_MARKET")
            tp_result = place_algo_conditional_order(
                sym_usdt, tp_side, "TAKE_PROFIT_MARKET", tp_price, quantity=qty
            )
            if isinstance(tp_result, dict) and tp_result.get("algoId"):
                log.info(f"📌 {symbol} 止盈 Algo 单已恢复：{tp_price}")
            else:
                log.warning(f"⚠️ {symbol} 恢复 Binance 止盈单失败：{tp_result}")
    except Exception as e:
        log.warning(f"⚠️ {symbol} 同步止损/止盈单异常：{e}")


def submit_initial_algo_orders(pos: dict):
    """
    为从 API 同步/方向反转后的持仓提交初始止盈止损 Algo 单。

    2026-06-05 修复：方向反转 / 初始同步场景先全量清理该币种所有 Algo 单，
    再重新提交正确的 TP+SL。防止旧方向的条件单残留（如 LONG 的 SELL TP
    在反转成 SHORT 后没删干净）。
    """
    if not AUTO_TRADE_ENABLED:
        return
    sym = pos["symbol"]
    try:
        typ = pos["type"]
        sl_price = pos.get("sl_price")
        tp_price = pos.get("tp1_price")
        if not sl_price or not tp_price:
            return

        qty = abs(pos.get("amount", 0))
        sym_usdt = f"{sym}USDT"

        # 🔧 2026-06-05 修复：全量清理，不留任何旧单
        cancel_algo_orders(sym_usdt)

        # 提交止损
        sl_side = "SELL" if typ == "LONG" else "BUY"
        sl_result = place_algo_conditional_order(sym_usdt, sl_side, "STOP_MARKET", sl_price, quantity=qty)
        if isinstance(sl_result, dict) and sl_result.get("algoId"):
            log.info(f"📌 {sym} 止损 Algo 单已提交：{sl_price} algoId={sl_result.get('algoId')}")
        else:
            log.warning(f"⚠️ {sym} 止损 Algo 单提交失败：{sl_result}")

        # 提交止盈
        tp_side = "SELL" if typ == "LONG" else "BUY"
        tp_result = place_algo_conditional_order(sym_usdt, tp_side, "TAKE_PROFIT_MARKET", tp_price, quantity=qty)
        if isinstance(tp_result, dict) and tp_result.get("algoId"):
            log.info(f"📌 {sym} 止盈 Algo 单已提交：{tp_price} algoId={tp_result.get('algoId')}")
        else:
            log.warning(f"⚠️ {sym} 止盈 Algo 单提交失败：{tp_result}")
    except Exception as e:
        log.warning(f"⚠️ {sym} 提交初始 Algo 单异常：{e}")


def open_position(
    symbol: str,
    direction: str,
    entry_price: float,
    score: int,
    tp_sl: dict,
    regime: str,
    ai_result: Optional[dict] = None,
    trade_id: str = None,
) -> dict:
    # 2026-04-27 老公指示：下单前最后一道闸门 —— Binance API 实时确认无同币种持仓
    # 防止多进程/race condition 重复开仓（之前 12 秒内开 2 次 XRP / SSL EOF 后又开一次 BNB 都是这个 bug）
    try:
        _api_pos = get_all_positions() or []
        if any(p.get("symbol") == symbol
               and float(p.get("amount", 0)) != 0
               and abs(float(p.get("amount", 0))) * float(p.get("entry_price", 0)) >= 5.0
               for p in _api_pos):
            log.warning(f"🚫 {symbol} 下单闸门：API 已有持仓，拒绝重复下单")
            return {
                "symbol": symbol, "type": direction, "entry_price": entry_price,
                "qty": 0, "amount": 0, "score": score, "regime": regime,
                "ai_result": ai_result, "trade_id": trade_id,
                "order_result": {"success": False, "message": "API 已有持仓，拒绝重复下单"},
                "entry_time": datetime.now().isoformat(),
                "high_24h": 0.0, "low_24h": 0.0, "peak_pnl": 0.0,
                **tp_sl,
            }
    except Exception as _e:
        log.warning(f"⚠️ 下单闸门查持仓异常：{_e}（继续走流程）")

    # 🔒 防御：价格无效（0 或异常大）时拒绝下单
    if not entry_price or entry_price <= 0 or entry_price > 1_000_000:
        log.error(f"🚫 {symbol} 价格异常 entry_price={entry_price}，拒绝开仓")
        return {
            "symbol": symbol, "type": direction, "entry_price": entry_price,
            "qty": 0, "amount": 0, "score": score, "regime": regime,
            "ai_result": ai_result, "trade_id": trade_id,
            "order_result": {"success": False, "message": f"价格异常 entry_price={entry_price}"},
            "entry_time": datetime.now().isoformat(),
            "high_24h": 0.0, "low_24h": 0.0, "peak_pnl": 0.0,
            **tp_sl,
        }

    size_pct = CONFIG["position_size_pct"]
    if regime == "volatile":
        size_pct *= 0.6

    # 计算数量并格式化到正确精度
    raw_qty = (CONFIG["total_capital"] * size_pct * 10) / entry_price  # 2026-03-28 老公指示：加 10x 杠杆
    qty = format_quantity(symbol, raw_qty, entry_price)

    # 实盘交易调用
    order_result = None
    if AUTO_TRADE_ENABLED:
        try:
            # 调用 binance_auto_trade 模块下单
            side = "BUY" if direction == "LONG" else "SELL"
            order_result = place_order(
                symbol=f"{symbol}USDT",
                side=side,
                quantity=round(qty, 6),
                leverage=10,  # 2026-03-27 老公指示：3x→10x 测试两天
                tp_price=tp_sl.get("tp1_price"),
                sl_price=tp_sl.get("sl_price"),
                price=entry_price,
                reduce_only=False,
            )
            log.info(f"📝 下单结果：{order_result}")
        except Exception as e:
            log.error(f"❌ 下单失败：{e}")
            order_result = {"error": str(e)}

    pos = {
        "symbol":       symbol,
        "type":         direction,
        "entry_price":  entry_price,
        "entry_time":   datetime.now().isoformat(),
        "qty":          round(qty, 6),
        "amount":       round(qty, 6),  # 与 get_all_positions 返回格式一致，用于盈亏计算
        "score":        score,
        "regime":       regime,
        "ai_result":    ai_result,
        "trade_id":     trade_id,
        "order_result": order_result,
        "high_24h":     0.0,
        "low_24h":      0.0,
        "peak_pnl":     0.0,
        **tp_sl,
    }

    # 检查订单是否真正成功（默认 False，强制要求 place_order 显式返回 success=True）
    order_success = False
    if isinstance(order_result, dict):
        order_success = bool(order_result.get('success', False))

    msg = (
        f"🟢 开多 {symbol}" if direction == "LONG" else f"🔴 开空 {symbol}"
    ) + f" @{entry_price} | 评分:{score} | 状态:{regime}"
    if ai_result:
        msg += f" | AI:{ai_result.get('direction')}({ai_result.get('confidence')}%)"
    if not order_success:
        err_msg = order_result.get('message', '未知') if isinstance(order_result, dict) else str(order_result)
        msg += f" | ❌ 下单失败：{err_msg}"
        # 立即推送下单失败告警到飞书（不走缓冲，老公需要立刻知道）
        try:
            push_feishu_card(
                f"🚨 {symbol} 下单失败 - 信号未执行",
                [
                    {"tag": "div", "text": {"tag": "lark_md", "content":
                        f"**币种：** {symbol}\n"
                        f"**方向：** {'开多' if direction == 'LONG' else '开空'}\n"
                        f"**信号价：** ${entry_price:,.4f}\n"
                        f"**评分：** {score}/100\n"
                        f"**失败原因：** `{err_msg}`\n\n"
                        f"⚠️ **币安实际未下单**，请检查 API key / 余额 / 网络"
                    }},
                    {"tag": "note", "elements": [{"tag": "plain_text",
                        "content": f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)"}]},
                ],
                "red"
            )
        except Exception:
            pass
    else:
        msg += f" | ✅ 下单成功"
    log.info(msg)
    write_alert(msg)

    # 只有订单成功才推送飞书（走缓冲区合并，避免刷屏）
    if order_success:
        # 提取订单 ID（让用户能在币安直接查证）
        order_id = ''
        if order_result and isinstance(order_result, dict):
            order_obj = order_result.get('order', {})
            if isinstance(order_obj, dict):
                order_id = str(order_obj.get('orderId', ''))

        push_signal_alert({
            'symbol': symbol,
            'type': direction,
            'action': '开多' if direction == 'LONG' else '开空',
            'price': entry_price,
            'score': score,
            'regime': regime,
            'tp_price': pos.get('tp1_price', 0),   # 修复：字段名应为 tp1_price
            'sl_price': pos.get('sl_price', 0),
            'order_id': order_id,
        }, immediate=False)

    return pos


def close_position(pos: dict, reason: str, size_ratio: float, current_price: float):
    entry = pos["entry_price"]
    typ   = pos["type"]
    symbol = pos["symbol"]
    sym_usdt = f"{symbol}USDT"

    # 🐛 修复：从 API 取真实持仓数量，不用本地缓存
    # 反转重建会把本地缓存改成风控标准量，但交易所实际量可能不同
    # 用本地缓存的量会导致 reduceOnly 被拒（-2022），产生降级连锁反应
    actual_qty = pos.get("qty", 0)
    try:
        for ap in (get_all_positions() or []):
            if ap.get("symbol") == symbol:
                api_amt = abs(float(ap.get("amount", 0) or 0))
                if api_amt > 0:
                    actual_qty = api_amt
                    break
    except Exception:
        pass  # 降级使用本地缓存

    qty_raw = actual_qty * size_ratio
    qty = format_quantity(symbol, qty_raw, entry)

    # 实盘平仓调用（使用 place_order with reduce_only=True）
    # 2026-06-03 修复：币安测试网 reduceOnly 全线返回 -2022，降级为不带 reduceOnly 的平仓
    close_result = None
    margin_replenished = False  # 2026-06-03：-4164 保证金不足时只补一次
    position_padded = False     # 2026-06-03：名义价值 < $20 时只补一次
    if AUTO_TRADE_ENABLED and size_ratio > 0:
        for attempt in range(5):
            try:
                side = "SELL" if typ == "LONG" else "BUY"

                # 🔧 2026-06-03：第一次用 reduceOnly；如果被拒（-2022）则降级不带 reduceOnly
                close_result = place_order(
                    symbol=sym_usdt,
                    side=side,
                    quantity=qty,
                    leverage=10,
                    reduce_only=(attempt == 0),
                    price=current_price,
                )
                log.info(f"📝 平仓结果：{close_result}")

                if close_result.get('success', True):
                    break

                order_code = close_result.get('order', {}).get('code', 0)

                # 降级1：reduceOnly 被拒 → 不带 reduceOnly
                if attempt == 0 and order_code == -2022:
                    log.warning(f"⚠️ {symbol} reduceOnly 被币安拒（-2022），降级为普通平仓")
                    time.sleep(1)
                    close_result = place_order(
                        symbol=sym_usdt, side=side, quantity=qty,
                        leverage=10, reduce_only=False, price=current_price,
                    )
                    log.info(f"📝 平仓结果（降级）：{close_result}")
                    if close_result.get('success', True):
                        break
                    order_code = close_result.get('order', {}).get('code', 0)

                # 降级2：-4164 补充保证金
                if order_code == -4164 and not margin_replenished:
                    log.warning(f"⚠️ {symbol} 保证金/名义价值不足（-4164），补充 5 USDT")
                    try:
                        request("POST", "/fapi/v1/positionMargin", {
                            "symbol": sym_usdt, "amount": 5.0, "type": 1
                        })
                        margin_replenished = True
                        time.sleep(1)
                        close_result = place_order(
                            symbol=sym_usdt, side=side, quantity=qty,
                            leverage=10, reduce_only=False, price=current_price,
                        )
                        log.info(f"📝 平仓结果（补保证金后）：{close_result}")
                        if close_result.get('success', True):
                            break
                        order_code = close_result.get('order', {}).get('code', 0)
                    except Exception as me:
                        log.error(f"❌ 补充保证金失败：{me}")

                # 降级3：名义价值 < $20 → 买入≥$20 名义价值的量撑大后全平
                if order_code == -4164 and not position_padded and current_price > 0:
                    notional = qty * current_price
                    if notional < 20:
                        pad_qty = 25.0 / current_price  # ≥$20 +25% buffer：防 stepSize 取整缩水后仍 <$20（2026-09-02 ETH 0.008 卡仓教训）
                        pad_qty = format_quantity(symbol, pad_qty, current_price)
                        if pad_qty > 0:
                            log.warning(f"⚠️ {symbol} 名义 {notional:.1f} < $20，买入 {pad_qty} 撑大后全平")
                            try:
                                pad_side = "BUY" if typ == "LONG" else "SELL"
                                r = place_order(symbol=sym_usdt, side=pad_side, quantity=pad_qty,
                                                leverage=10, reduce_only=False, price=current_price)
                                if r.get('success', True):
                                    position_padded = True
                                    time.sleep(1)
                                    # 平全部（原持仓 + 补仓量）
                                    r_pos = request("GET", "/fapi/v2/positionRisk", {"symbol": sym_usdt})
                                    amt = abs(float(r_pos[0]['positionAmt'])) if isinstance(r_pos, list) and r_pos else qty
                                    total_qty = format_quantity(symbol, amt, current_price) if amt > qty else qty
                                    close_result = place_order(
                                        symbol=sym_usdt, side=side, quantity=total_qty,
                                        leverage=10, reduce_only=False, price=current_price,
                                    )
                                    log.info(f"📝 平仓结果（撑大后）：{close_result}")
                                    if close_result.get('success', True):
                                        break
                            except Exception as pe:
                                log.error(f"❌ 加仓撑大失败：{pe}")

                log_limited(f"close_fail_{symbol}", f"⚠️ {symbol} 平仓第{attempt+1}次失败，{'重试' if attempt < 4 else '放弃'}")
                if attempt < 4:
                    time.sleep(2)
            except Exception as e:
                log.error(f"❌ 平仓异常第{attempt+1}次：{e}")
                close_result = {"error": str(e), "success": False}
                if attempt < 4:
                    time.sleep(2)

    # 计算盈亏 - 使用实际持仓数量，不再用标准仓位
    pnl_pct = (current_price - entry) / entry if typ == "LONG" else (entry - current_price) / entry
    qty_raw = pos.get("qty", 0) * size_ratio
    pnl_usdt = qty_raw * entry * pnl_pct  # 实际数量 × 入场价 × 涨跌幅
    # 2026-03-25 老公指示：用实际成交价计算真实盈亏（API 返回的 realizedPnl 优先）
    if close_result and close_result.get('success') and close_result.get('order'):
        realized = close_result.get('order', {}).get('realizedPnl')
        if realized is not None:
            pnl_usdt = float(realized)


    # 检查平仓是否真正成功
    close_success = True
    if close_result:
        close_success = close_result.get('success', True)

    emoji = "✅" if pnl_usdt >= 0 else "❌"
    msg = (
        f"{emoji} 平仓 {symbol} {reason} | "
        f"入场:{entry:.4f} 出场:{current_price:.4f} | "
        f"PnL:{pnl_pct*100:.2f}% ({pnl_usdt:+.2f}USDT) | "
        f"平{size_ratio*100:.0f}%仓"
    )
    if not close_success:
        msg += f" | ❌ 平仓失败"
    else:
        msg += f" | ✅ 平仓成功"
    log.info(msg)
    write_alert(msg)

    # 只有平仓成功才推送飞书（走缓冲区合并，避免刷屏）
    if close_success:
        # Phase 1：平仓成功回填盈亏到归因库（trade_data.db / trade_closes 表）
        try:
            record_close(pos.get("trade_id"), pnl_usdt, pnl_pct, reason, symbol)
        except Exception as e:
            log.warning(f"⚠️ 特征回填失败: {e}")

        # ✅ 平仓后清除该币种冷却，让下次开仓不被卡 5 分钟
        try:
            global cooldown_manager
            if cooldown_manager and size_ratio >= 1.0:
                cooldown_manager.reset_after_close(symbol)
                log.info(f"🔄 {symbol} 平仓后已清除冷却记录")
        except Exception as e:
            log.warning(f"清除冷却异常: {e}")

        # 确保 score 和 regime 有值（从持仓中读取，如果没有则用 0/unknown）
        score = pos.get('score')
        if score is None or score == '':
            score = 0
        regime = pos.get('regime')
        if regime is None or regime == '':
            regime = 'unknown'

        # 提取触发价（止盈用 tp1_price，止损用 sl_price）
        if 'TP' in reason:
            trigger_px = pos.get('tp1_price', 0)
        else:
            trigger_px = pos.get('sl_price', 0)

        # 提取真实成交价（如果 close_result 里有）
        actual_px = current_price
        if close_result and close_result.get('order'):
            try:
                avg = close_result['order'].get('avgPrice')
                if avg and float(avg) > 0:
                    actual_px = float(avg)
            except Exception:
                pass

        # 提取订单 ID
        order_id = ''
        if close_result and close_result.get('order'):
            order_id = str(close_result['order'].get('orderId', ''))

        push_signal_alert({
            'symbol': symbol,
            'type': typ,
            'action': '止盈' if 'TP' in reason else '止损',
            'price': actual_px,
            'trigger_price': trigger_px,
            'score': score,
            'regime': regime,
            'pnl': pnl_usdt,
            'order_id': order_id,
        }, immediate=False)  # 改为缓冲模式

    return pnl_usdt


# ═══════════════════════════════════════════════════════════════
# 执行层收敛：迷你仓修复 + 孤儿 Algo 单清理
# （原散落在 crypto_signal_monitor.py 主循环，收敛到此层，
#  不跨层依赖 strategy_layer —— tp_sl 由 main 用 calc_tp_sl 算好传入）
# ═══════════════════════════════════════════════════════════════
def repair_mini_position(
    symbol: str,
    amt: float,
    entry: float,
    correct_amount: float,
    tp_sl: dict,
    cooldown_map: dict,
    cooldown_seconds: float,
):
    """
    迷你仓位修复：API 持仓量严重偏小（<80% 风控标准）时，先关迷你仓再用标准量重开。

    返回重建后的持仓片段；冷却中 / 关闭未生效 / 重开被拒时返回 None。
    tp_sl 由调用方（main）用 calc_tp_sl 计算后传入，本层不跨层依赖 strategy_layer。
    """
    last_fix = cooldown_map.get(symbol, 0)
    if time.time() - last_fix < cooldown_seconds:
        log.info(f"⏳ {symbol} 迷你仓位修复冷却中（距上次 {int(time.time()-last_fix)}s），跳过本轮")
        return None
    cooldown_map[symbol] = time.time()
    api_amount = abs(amt)
    log.warning(f"⚠️ {symbol} 迷你仓位检测：API={api_amount:.4f} < 标准80%，尝试修复")
    try:
        # 构造临时持仓对象，复用 close_position 的完整降级逻辑
        tmp_pos = {
            'symbol': symbol, 'type': 'SHORT' if amt < 0 else 'LONG',
            'entry_price': entry, 'amount': api_amount, 'qty': api_amount,
            'tp1_price': 0, 'sl_price': 0, 'tp1_hit': False,
            'size_remaining': 1.0, 'tp_pct': 0.04, 'peak_pnl': 0.0,
        }
        close_position(tmp_pos, "迷你仓修复", 1.0, entry)
        time.sleep(2)
        # 检查是否真的平掉了
        api_check = get_all_positions()
        still_there = any(
            float(ap.get('amount', 0)) != 0
            and abs(float(ap.get('amount', 0))) * float(ap.get('entry_price', 0)) >= 5.0
            and ap.get('symbol', '') == symbol
            for ap in (api_check or [])
        )
        if still_there:
            log.error(f"❌ {symbol} 迷你仓关闭未生效（仓位仍在API），跳过重建")
            return None

        # 2. 用标准量重新开仓
        side_open = "BUY" if amt > 0 else "SELL"
        sym_usdt = f"{symbol}USDT"
        open_result = place_order(
            symbol=sym_usdt, side=side_open,
            quantity=correct_amount, leverage=10,
            reduce_only=False, price=entry,
        )
        if open_result.get('success', True):
            log.info(f"✅ {symbol} 修复成功：{api_amount:.4f}→{correct_amount:.4f}")
            return {
                'symbol': symbol,
                'type': 'SHORT' if amt < 0 else 'LONG',
                'entry_price': entry,
                'amount': correct_amount,
                'tp1_price': tp_sl['tp1_price'],
                'tp2_price': 0.0,
                'sl_price': tp_sl['sl_price'],
                'tp1_hit': False,
                'size_remaining': 1.0,
                'tp_pct': tp_sl.get('tp_pct', 0.04),
                'peak_pnl': 0.0,
            }
        log.error(f"❌ {symbol} 修复失败（重开被拒）：{open_result}")
        return None
    except Exception as repair_e:
        log.error(f"❌ {symbol} 修复异常：{repair_e}")
        return None


def cleanup_orphan_algo_orders(position_symbols: set) -> int:
    """启动时清理不属于任何持仓的孤儿 Algo 条件单，返回清理数量。"""
    if not AUTO_TRADE_ENABLED:
        return 0
    removed = 0
    try:
        all_algos = request("GET", "/fapi/v1/openAlgoOrders", {})
        if isinstance(all_algos, list):
            for a in all_algos:
                sym_raw = a.get('symbol', '')
                sym = sym_raw.replace('USDT', '') if sym_raw else ''
                if sym and sym not in position_symbols:
                    algo_id = a.get('algoId')
                    if algo_id:
                        try:
                            request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})
                            removed += 1
                            log.info(f"🗑️ 启动清理孤儿 Algo 单：{sym} algoId={algo_id}")
                        except Exception:
                            pass
        if removed > 0:
            log.info(f"🗑️ 启动清理完成：{removed} 个孤儿 Algo 单")
    except Exception as e:
        log.warning(f"⚠️ 启动清理 Algo 单异常：{e}")
    return removed
