#!/usr/bin/env python3
"""
crypto_signal_monitor.py
加密货币自动交易策略 v6.0 — 分层解耦版

仅保留：日志配置、全局参数、ScanScheduler 调度、main() 主循环与依赖装配。
业务逻辑已拆分到独立分层模块：
  data_layer     实时价格流 / 指标 / 市场状态 / 情绪
  strategy_layer 评分 / 止盈止损计算
  risk_layer     熔断 / 冷却 / 新闻过滤
  ai_layer       DeepSeek 预测
  execution_layer 开平仓 / 移动止损 / Algo 单
  notify_layer   信号推送 / 整点汇报 / 告警落盘
  position_store 持仓与状态持久化
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime

# 添加脚本目录到路径（须在 openclaw_logging / 分层模块导入之前）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ─────────────────────────────────────────────
# 日志（归档目录 /root/.openclaw/logs/trading/）
# ─────────────────────────────────────────────
from openclaw_logging import configure_root_logging, trading_log_path

LOG_FILE = str(trading_log_path())
configure_root_logging("trading")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 配置区（统一外置：config.json + secrets.json）
# ─────────────────────────────────────────────
from config import get_config

CONFIG = get_config()

# ─────────────────────────────────────────────
# 分层模块导入（依赖方向：main → 各 layer → 基础设施）
# ─────────────────────────────────────────────
from binance_auto_trade import (
    get_all_positions, cancel_algo_orders, format_quantity, place_order,
    check_margin_ratio_protection, _api_key,
)

from position_store import load_positions, save_positions, save_state
from data_layer import PriceStream, IndicatorEngine, SentimentEngine, detect_regime
from strategy_layer import calc_score, calc_tp_sl, calc_features, ai_score_adjust, set_sentiment_engine
from ai_layer import AIPredictor, _normalize_ai_direction, _normalize_ai_confidence
from risk_layer import CircuitBreaker, CooldownManager, NewsFilter
from execution_layer import (
    AUTO_TRADE_ENABLED,
    check_exits_fast,
    check_timeout_exits,
    update_trailing_stop,
    update_atr_dynamic_stops,
    submit_initial_algo_orders,
    open_position,
    close_position,
    repair_mini_position,
    cleanup_orphan_algo_orders,
    set_cooldown_manager as execution_set_cooldown_manager,
)
from notify_layer import (
    hourly_report,
    _send_push_signal,
    write_alert,
    restore_daily_pnl,
    get_last_balance_snapshot,
    set_cooldown_manager as notify_set_cooldown_manager,
)
from trade_features import record_open, new_trade_id

# AI 浮亏触发冷却（放在主循环外面）
ai_loss_cooldown = {}  # symbol → 上次触发时间

# 迷你仓位修复冷却：避免每轮主循环反复尝试同一迷你仓位（-2022/-4164 拒单刷屏）
mini_fix_cooldown = {}  # symbol → 上次修复尝试时间
MINI_FIX_COOLDOWN_SECONDS = 600  # 10 分钟内同一 symbol 只尝试一次修复

# 整点 AI 全量扫描防重入（同步 predict 走线程池，上一轮未完成时跳过本轮）
_ai_scan_in_progress = False


async def _run_ai_scan(predictor, symbols: list, prices: dict, indicators: dict, fg: int):
    """整点全量 AI 观点扫描（方案B）：只记录 AI 观点，不做任何交易决策。"""
    global _ai_scan_in_progress
    if _ai_scan_in_progress:
        log.info("⚠️ AI扫描防重入：上一轮未完成，跳过本轮")
        return
    _ai_scan_in_progress = True
    try:
        log.info(f"🔍 AI扫描启动：{len(symbols)}币")
        # 主循环 prices 为 {sym: {price,...}}，提取价格数值供扫描
        price_map = {s: (v.get("price") if isinstance(v, dict) else v) for s, v in prices.items()}
        def _do():
            return predictor.scan_all(symbols, price_map, indicators, fg)
        results = await asyncio.to_thread(_do)
        log.info(f"🔍 scan_all 返回 {len(results or {})} 个结果")
        if not results:
            log.warning("⚠️ AI扫描无结果（predict 全部失败或无数据）")
            return
        parts = [f"{s}={r['direction']}({r['confidence']:.0%})" for s, r in results.items()]
        msg = "🔍 整点AI全量扫描: " + "; ".join(parts)
        log.info(msg)
        write_alert(msg)
        # 落盘 jsonl 便于后续评估 AI 观点与数学信号一致性
        try:
            scan_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_scan.jsonl")
            with open(scan_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": datetime.now().isoformat(), "fg": fg,
                    "prices": price_map,  # Phase 1：记录整点价格，供 AI 命中率对比
                    "scan": results,
                }, ensure_ascii=False) + "\n")
        except Exception as _e:
            log.warning(f"⚠️ AI扫描落盘失败: {_e}")
    except Exception as e:
        log.warning(f"⚠️ 整点AI扫描失败: {e}")
    finally:
        _ai_scan_in_progress = False


# ═══════════════════════════════════════════════════════════════
# 十四、分层调度器
# ═══════════════════════════════════════════════════════════════
def _build_hourly_score_map(symbols: list, prices: dict, indicators: dict, fg: int) -> dict:
    """整点汇报用：为所有监控币预计算评分/方向/市场状态。

    main 装配层是唯一允许调用 strategy/data 业务函数的地方；
    notify_layer 仅接收结果做渲染，避免推送层跨层算分。
    """
    btc_ind = indicators.get("BTC", {})  # 2026-03-28 老公指示：用 BTC 作为大盘参考
    out = {}
    for sym in symbols:
        pd = prices.get(sym, {})
        ind = indicators.get(sym, {})
        if not pd.get("price") or not ind:
            continue
        regime = detect_regime(ind)
        score, direction = calc_score(sym, pd, ind, regime, fg, 0, btc_ind)
        out[sym] = {"score": score, "direction": direction, "regime": regime}
    return out


class ScanScheduler:
    """控制各任务执行频率，避免每15秒都跑重计算"""

    def __init__(self):
        self.tick = 0   # 每15秒+1

    def tick_up(self):
        self.tick += 1

    def should(self, task: str) -> bool:
        # task → 每隔多少个tick执行一次
        intervals = {
            "indicators":   4,    # 60秒
            "sentiment":    20,   # 300秒
            "funding":      20,   # 300秒
            "hourly_report":240,  # 3600秒
        }
        n = intervals.get(task, 1)
        return self.tick % n == 0


# ═══════════════════════════════════════════════════════════════
# 十六、主循环
# ═══════════════════════════════════════════════════════════════
async def main():
    log.info("🚀 crypto_signal_monitor v6.0 启动")

    # 启动横幅：可视化确认 API key（防止使用旧 key 而不自知）
    if AUTO_TRADE_ENABLED:
        try:
            ak = _api_key()
            log.info(f"🔑 当前 Binance API key: {ak[:8]}...{ak[-4:]} (动态读取，文件改动会自动重载)")
        except Exception as e:
            log.warning(f"⚠️ 无法读取 API key 信息：{e}")

    # 初始化各模块
    price_stream = PriceStream(CONFIG["symbols"], proxy=CONFIG["proxy"])
    indicator_engine = IndicatorEngine()
    sentiment_engine = SentimentEngine()
    set_sentiment_engine(sentiment_engine)
    ai_predictor     = AIPredictor()
    circuit_breaker  = CircuitBreaker()
    cooldown_manager = CooldownManager()
    execution_set_cooldown_manager(cooldown_manager)
    notify_set_cooldown_manager(cooldown_manager)
    news_filter      = NewsFilter()
    scheduler        = ScanScheduler()

    # 缓存
    indicators:  dict = {}
    fg_cache:    int  = 50
    fr_cache:    dict = {}

    # 启动WebSocket（后台）
    asyncio.create_task(price_stream.start())

    # 等待WebSocket就绪
    log.info("等待WebSocket价格流就绪...")
    for _ in range(30):
        if price_stream.is_ready():
            break
        await asyncio.sleep(1)
    log.info(f"价格流就绪: {list(price_stream.prices.keys())}")

    # 加载持仓（从 Binance API 同步真实持仓）
    log.info("🔄 从 Binance API 同步持仓...")
    try:
        api_positions = get_all_positions()
        # 如果 API 返回空，尝试从本地文件加载
        if not api_positions:
            log.warning("⚠️ Binance API 返回空持仓，尝试从本地文件加载...")
            positions = load_positions()
            if positions:
                log.info(f"✅ 从本地文件恢复 {len(positions)} 个持仓")
            else:
                positions = []
                log.warning("⚠️ 本地文件也无持仓，初始化为空")
        else:
            # 2026-03-28 老公指示：先加载本地缓存，保留止盈止损
            old_positions = load_positions()
            old_map = {p.get("symbol", ""): p for p in old_positions}
            log.info(f'📊 加载本地缓存 {len(old_positions)} 个持仓用于保留止盈止损')

            # 过滤有实际持仓的币种
            positions = []
            for p in api_positions:
                amt = float(p.get('amount', 0))
                entry = float(p.get('entry_price', 0))
                # 🔧 2026-06-06 修复：过滤粉尘仓位（名义价值<$5 不纳入）
                if amt != 0 and abs(amt) * entry >= 5.0:
                    # 转换为本地格式
                    symbol = p['symbol']
                    entry = float(p.get('entry_price', 0))
                    direction = 'SHORT' if amt < 0 else 'LONG'

                    # 查找旧缓存
                    old = old_map.get(symbol, {})
                    old_direction = old.get("type", "")

                    # 用默认参数计算新的 SL/TP（作为兜底）
                    new_tp_sl = calc_tp_sl(entry, direction, {}, {})

                    # 🔧 2026-06-16 修复：启动时保留三档移动止盈收窄过的 SL
                    # 如果旧缓存里同方向且 SL 更紧（对持仓更有利），优先保留旧的
                    old_sl = old.get("sl_price", 0)
                    new_sl = new_tp_sl["sl_price"]
                    old_tp1 = old.get("tp1_price", 0)
                    new_tp1 = new_tp_sl["tp1_price"]

                    sl_price = new_sl  # 默认用新值
                    tp1_price = new_tp1
                    peak_pnl = 0.0
                    peak_price = 0.0
                    tp_pct = new_tp_sl.get("tp_pct", 0.04)

                    if old_direction == direction:
                        has_peak = old.get("peak_pnl", 0) > 0

                        if old_sl > 0:
                            # 判断谁的 SL 更紧
                            if direction == "SHORT":
                                tighter = old_sl < new_sl
                            else:
                                tighter = old_sl > new_sl

                            if tighter:
                                sl_price = old_sl
                                log.info(f"🔒 {symbol} 保留旧 SL {old_sl:.4f}（比新 SL {new_sl:.4f} 更紧）")
                        elif has_peak:
                            # SL 丢了但有峰值数据 → 用 max(calc默认, entry±动态峰值)
                            # 从旧 peak_price 推算 SL（三档移动止盈反推）
                            old_peak_price = old.get("peak_price", 0)
                            if old_peak_price > 0:
                                # 反推：peak_price 附近的 SL（给 0.5% 缓冲）
                                if direction == "SHORT":
                                    estimated_sl = old_peak_price * 1.005
                                    sl_price = min(new_sl, estimated_sl)
                                else:
                                    estimated_sl = old_peak_price * 0.995
                                    sl_price = max(new_sl, estimated_sl)
                                sl_price = round(sl_price, 4)
                                log.info(f"🔧 {symbol} SL丢失恢复：peak_price={old_peak_price} → 估算SL={sl_price:.4f}")

                        # 恢复峰值数据（三档移动止盈依赖）
                        peak_pnl = old.get("peak_pnl", 0)
                        peak_price = old.get("peak_price", 0)
                        tp_pct = old.get("tp_pct", tp_pct)
                        tp1_price = old_tp1 if old_tp1 > 0 else new_tp1

                        if peak_pnl > 0:
                            log.info(f"📈 {symbol} 恢复 peak_pnl={peak_pnl:.2%} peak_price={peak_price}")

                    pos = {
                        'symbol': symbol,
                        'type': direction,
                        'entry_price': entry,
                        'entry_time': old.get("entry_time", datetime.now().isoformat()),
                        'qty': abs(amt),
                        'amount': abs(amt),
                        'score': old.get("score", 55),
                        'regime': old.get("regime", "trending"),
                        'ai_result': None,
                        'order_result': {'success': True, 'message': '从 API 同步'},
                        'high_24h': old.get("high_24h", 0.0),
                        'low_24h': old.get("low_24h", 0.0),
                        'tp1_price': tp1_price,
                        'sl_price': sl_price,
                        'peak_pnl': peak_pnl,
                        'peak_price': peak_price,
                        'tp_pct': tp_pct,
                    }
                    positions.append(pos)
                    status = "恢复数据" if peak_pnl > 0 else "新同步"
                    log.info(f"✅ 同步持仓({status})：{symbol} {pos['type']} @ ${pos['entry_price']:.2f} "
                             f"SL={sl_price:.4f} TP={tp1_price:.4f}")
        save_positions(positions)
        log.info(f"✅ 持仓同步完成：{len(positions)}个")

        # 🔧 2026-06-06 修复：启动时清理不属于任何持仓的孤儿 Algo 条件单（收敛至 execution_layer）
        if AUTO_TRADE_ENABLED:
            position_symbols = {p.get('symbol', '') for p in positions}
            cleanup_orphan_algo_orders(position_symbols)

        # 🐛 仓位反转修复：启动时为从 API 同步的每个持仓提交止盈止损 Algo 单
        for pos in positions:
            submit_initial_algo_orders(pos)
    except Exception as e:
        log.error(f"❌ 持仓同步失败：{e}")
        positions = load_positions()  # 回退到本地文件

    # 从状态文件恢复上次推送时间、峰值盈亏、熔断暂停状态；今日已实现由 restore_daily_pnl 校准
    last_report_hour = -1
    last_report_time = 0
    peak_pnl_map = {}
    try:
        if os.path.exists(CONFIG["state_file"]):
            with open(CONFIG["state_file"]) as f:
                state = json.load(f)
                if state.get("last_report_hour") is not None:
                    last_report_hour = state["last_report_hour"]
                if state.get("last_report_time"):
                    last_report_time = state["last_report_time"]
                if state.get("peak_pnl_map"):
                    peak_pnl_map = state.get("peak_pnl_map", {})
                # 恢复熔断暂停：进程重启后不丢失保护（旧逻辑会在启动时无条件清空）
                pu = state.get("paused_until")
                if pu:
                    try:
                        paused_dt = datetime.fromisoformat(str(pu))
                        if paused_dt > datetime.now():
                            circuit_breaker.paused_until = paused_dt
                    except ValueError:
                        pass
                circuit_breaker.consecutive_losses = int(state.get("consecutive_losses", 0))
    except Exception as e:
        log.warning(f"⚠️ 加载状态文件失败：{e}")

    realized_pnl = restore_daily_pnl(circuit_breaker)
    # 熔断暂停状态已从状态文件恢复，不再在启动时清零（避免重启丢失熔断保护）
    if circuit_breaker.paused_until is None:
        circuit_breaker.consecutive_losses = 0
    log.info(
        f"📋 恢复状态：last_report_hour={last_report_hour}, "
        f"daily_pnl={realized_pnl:+.2f}U, peak_pnl={peak_pnl_map}"
    )

    # 应用恢复的 peak_pnl 到持仓（移动止盈关键数据）
    if peak_pnl_map and positions:
        for pos in positions:
            sym = pos["symbol"]
            if sym in peak_pnl_map:
                pos["peak_pnl"] = peak_pnl_map[sym]
        save_positions(positions)
        log.info(f"✅ 恢复 peak_pnl 到 {len(positions)} 个持仓")

    while True:
        loop_start = time.time()
        scheduler.tick_up()

        # 2026-03-28 老公指示：爆仓保护检查（保证金率<5% 强制平仓）
        try:
            check_margin_ratio_protection()
        except Exception as e:
            log.warning(f"⚠️ 爆仓保护检查失败：{e}")

        # 每 20 个 tick（5 分钟）从 API 同步持仓
        # 🐛 Bug 修复：API 是真相，本地是缓存。API 返回 0 说明真的没有持仓
        if scheduler.tick % 20 == 0:
            try:
                api_positions = get_all_positions()
                # 🐛 Bug 修复：API 返回 dict 表示错误，保留缓存不处理
                if isinstance(api_positions, dict) and 'error' in api_positions:
                    log.warning(f"⚠️ API 查询失败，保留本地缓存：{api_positions['error'][:50]}")
                    api_count = -1  # 标记失败
                else:
                    # 🔧 2026-06-06 修复：过滤粉尘仓位（名义价值<$5），防止误重建
                    api_count = sum(1 for p in api_positions
                        if float(p.get('amount', 0)) != 0
                        and abs(float(p.get('amount', 0))) * float(p.get('entry_price', 0)) >= 5.0)

                # 判断是否需要重建持仓：计数变化 OR 方向变化
                need_rebuild = False

                if api_count == 0:
                    if len(positions) > 0:
                        log.info(f"🔄 持仓同步：API 持仓=0，清空本地缓存（原{len(positions)}个）")
                        # 🔧 2026-06-06 修复：清空持仓时同步取消 Binance 上所有残留 Algo 条件单
                        if AUTO_TRADE_ENABLED:
                            for old_pos in positions:
                                try:
                                    cancel_algo_orders(f"{old_pos['symbol']}USDT")
                                except Exception:
                                    pass
                        positions = []
                        save_positions(positions)
                        log.info("✅ 持仓同步完成：0 个（API 为空）")
                elif api_count > 0 and api_count != len(positions):
                    need_rebuild = True
                    log.info(f"🔄 持仓同步：本地{len(positions)}个 → API{api_count}个")
                elif api_count > 0 and api_count == len(positions) and api_count > 0:
                    # 🐛 2026-06-15 修复：计数相同但币种集合不同 → 僵尸缓存（外部平仓后新单替补，缓存未清理）
                    api_symbols = set()
                    for p in api_positions:
                        amt = float(p.get('amount', 0))
                        if amt != 0 and abs(amt) * float(p.get('entry_price', 0)) >= 5.0:
                            api_symbols.add(p['symbol'])
                    local_symbols = {lp['symbol'] for lp in positions}
                    if api_symbols != local_symbols:
                        need_rebuild = True
                        stale = local_symbols - api_symbols
                        new_syms = api_symbols - local_symbols
                        log.info(f"🔄 持仓同步：缓存币种变化 旧={sorted(local_symbols)} → 新={sorted(api_symbols)}"
                                 + (f" (僵尸:{sorted(stale)})" if stale else "")
                                 + (f" (新增:{sorted(new_syms)})" if new_syms else ""))
                    else:
                        # 🐛 仓位反转检测：计数相同、币种相同但方向不同时也要重建
                        for p in api_positions:
                            amt = float(p.get('amount', 0))
                            entry = float(p.get('entry_price', 0))
                            if amt != 0 and abs(amt) * entry >= 5.0:  # 🔧 过滤粉尘
                                sym = p['symbol']
                                api_dir = 'SHORT' if amt < 0 else 'LONG'
                                for lp in positions:
                                    if lp['symbol'] == sym and lp['type'] != api_dir:
                                        need_rebuild = True
                                        log.info(f"🔄 持仓同步：{sym} 方向变化 {lp['type']}→{api_dir}，强制重建")
                                        break
                                if need_rebuild:
                                    break

                if need_rebuild:
                    new_positions = []
                    for p in api_positions:
                        amt = float(p.get('amount', 0))
                        entry = float(p.get('entry_price', 0))
                        # 🔧 2026-06-06 修复：过滤粉尘仓位（名义价值<$5 不重建）
                        if amt != 0 and abs(amt) * entry >= 5.0:
                            symbol = p['symbol']
                            entry = float(p.get('entry_price', 0))
                            # 🐛 修复：反转重建持仓时按风控规则重算数量，不用 API 的 abs(amt)
                            size_pct = CONFIG["position_size_pct"]
                            expected_qty = (CONFIG["total_capital"] * size_pct * 10) / entry
                            correct_amount = format_quantity(symbol, expected_qty, entry)
                            api_amount = abs(amt)
                            size_ratio = api_amount / correct_amount if correct_amount > 0 else 1.0
                            log.info(f"🔧 反转重建 {symbol}：API原始={api_amount:.4f} → 风控标准={correct_amount:.4f} (比例={size_ratio:.1%})")

                            # 🔧 2026-06-10 修复：API 持仓量严重偏小（<80%风控标准）→ 迷你仓位，关闭后用标准量重开
                            # 修复逻辑收敛至 execution_layer.repair_mini_position（tp_sl 由 main 预计算传入）
                            if size_ratio < 0.8 and AUTO_TRADE_ENABLED:
                                tp_sl = calc_tp_sl(entry, 'LONG' if amt > 0 else 'SHORT', {}, {})
                                repaired = repair_mini_position(
                                    symbol, amt, entry, correct_amount, tp_sl,
                                    mini_fix_cooldown, MINI_FIX_COOLDOWN_SECONDS,
                                )
                                if repaired:
                                    new_positions.append(repaired)
                            else:
                                # 正常重建（数量匹配）
                                tp_sl = calc_tp_sl(entry, 'LONG' if amt > 0 else 'SHORT', {}, {})
                                tp1 = tp_sl['tp1_price']
                                sl = tp_sl['sl_price']
                                new_positions.append({
                                    'symbol': symbol,
                                    'type': 'SHORT' if amt < 0 else 'LONG',
                                    'entry_price': entry,
                                    'amount': correct_amount,
                                    'tp1_price': tp1,
                                    'tp2_price': 0.0,
                                    'sl_price': sl,
                                    'tp1_hit': False,
                                    'size_remaining': 1.0,
                                    'tp_pct': tp_sl.get('tp_pct', 0.04),
                                    'peak_pnl': 0.0,
                                })
                    # 🔧 2026-06-06 修复：清理已消失币种的孤儿 Algo 条件单
                    old_symbols = {p['symbol'] for p in positions}  # 修复前缓存
                    new_symbols = {p['symbol'] for p in new_positions}
                    orphan_symbols = old_symbols - new_symbols
                    if orphan_symbols and AUTO_TRADE_ENABLED:
                        for sym in orphan_symbols:
                            try:
                                cancel_algo_orders(f"{sym}USDT")
                                log.info(f"🗑️ 清理孤儿 Algo 单：{sym}")
                            except Exception:
                                pass
                    positions = new_positions
                    save_positions(positions)
                    log.info(f"✅ 持仓同步完成：{len(positions)}个")
                    # 🐛 仓位反转修复：重建后提交止盈止损 Algo 单
                    for pos in positions:
                        submit_initial_algo_orders(pos)
            except Exception as e:
                log.error(f"⚠️ 同步失败：{e}")

        try:
            # ──────────────────────────────────
            # Step 1: 获取实时价格（0 REST请求）
            # ──────────────────────────────────
            prices = {sym: price_stream.get(sym) for sym in CONFIG["symbols"]}

            # ──────────────────────────────────
            # Step 2: 止盈止损检查（每15秒，最轻量）
            # ──────────────────────────────────
            exits = check_exits_fast(positions, prices)
            for pos, reason, size in exits:
                p = prices[pos["symbol"]]["price"]
                pnl = close_position(pos, reason, size, p)
                circuit_breaker.record_trade(pnl)

                # 🐛 2026-06-03 修复：平仓后验证 API 才删除本地持仓，防止平仓失败时被 API 同步救回循环
                if reason == "TP1":
                    sym_closed = pos['symbol']
                    positions.remove(pos)
                    save_positions(positions)
                    log.info(f"TP1 触发 {sym_closed}，全平移除")
                    # 🔧 2026-06-05 修复：平仓后清理该币种所有 Algo 条件单，防止孤儿单
                    if AUTO_TRADE_ENABLED:
                        cancel_algo_orders(f"{sym_closed}USDT")
                elif reason == "SL":
                    # 先同步 API 确认仓位真的没了再删
                    sym_closed = pos['symbol']
                    positions.remove(pos)
                    save_positions(positions)
                    log.info(f"SL 触发 {sym_closed}，止损移除")
                    try:
                        api_positions = get_all_positions()
                        api_still_there = any(
                            float(p.get('amount', 0)) != 0
                            and abs(float(p.get('amount', 0))) * float(p.get('entry_price', 0)) >= 5.0
                            and p.get('symbol', '') == sym_closed
                            for p in (api_positions or [])
                        )
                        if api_still_there:
                            log.error(f"⛔ SL 平仓 {sym_closed} 未成交！Binance 仓位仍在，恢复本地缓存")
                            # 从 API 重建该币种持仓
                            for ap in api_positions:
                                amt = float(ap.get('amount', 0))
                                entry = float(ap.get('entry_price', 0))
                                if amt != 0 and abs(amt) * entry >= 5.0 and ap.get('symbol', '') == sym_closed:
                                    entry = float(ap.get('entry_price', 0))
                                    typ = 'SHORT' if amt < 0 else 'LONG'
                                    old = pos
                                    positions.append({
                                        'symbol': sym_closed,
                                        'type': typ,
                                        'entry_price': entry,
                                        'amount': abs(amt),
                                        'tp1_price': old.get('tp1_price', 0),
                                        'sl_price': old.get('sl_price', 0),
                                        'tp1_hit': old.get('tp1_hit', False),
                                        'size_remaining': old.get('size_remaining', 1.0),
                                        'tp_pct': old.get('tp_pct', 0.04),
                                        'peak_pnl': old.get('peak_pnl', 0.0),
                                    })
                                    save_positions(positions)
                                    log.warning(f"🔄 已恢复本地持仓 {sym_closed}（Binance 上仓位未被平掉）")
                                    break
                        else:
                            api_count = sum(1 for p in api_positions
                                if float(p.get('amount', 0)) != 0
                                and abs(float(p.get('amount', 0))) * float(p.get('entry_price', 0)) >= 5.0)
                            if api_count == 0:
                                positions = []
                                save_positions(positions)
                                log.info("📊 SL 平仓后同步：API 持仓为 0，清空本地缓存")
                            else:
                                log.info(f"📊 SL 平仓后同步：API 还有{api_count}个持仓，保留本地缓存")
                    except Exception as e:
                        log.error(f"⚠️ SL 平仓后同步失败：{e}")
                    # 🔧 2026-06-05 修复：止损平仓后清理该币种所有 Algo 条件单
                    if AUTO_TRADE_ENABLED:
                        try:
                            cancel_algo_orders(f"{sym_closed}USDT")
                        except Exception:
                            pass


            # ──────────────────────────────────
            # Step 2.5: 持仓超时退出（2026-08-10 防止突破信号挂死等 SL）
            # 参数外置 config.json → timeout_exit；只在 TP/SL 检查后运行，
            # 已被触发的仓位不参与超时判断。
            # ──────────────────────────────────
            to_cfg = CONFIG.get("timeout_exit", {})
            if to_cfg.get("enabled", False):
                to_exits = check_timeout_exits(positions, float(to_cfg.get("hours", 48)))
                for pos, reason, size in to_exits:
                    p = prices.get(pos["symbol"], {}).get("price")
                    if not p:
                        continue
                    pnl = close_position(pos, reason, size, p)
                    circuit_breaker.record_trade(pnl)
                    sym_closed = pos["symbol"]
                    positions.remove(pos)
                    save_positions(positions)
                    if AUTO_TRADE_ENABLED:
                        try:
                            cancel_algo_orders(f"{sym_closed}USDT")
                        except Exception:
                            pass


            # 移动止盈更新
            update_trailing_stop(positions, prices)
            save_positions(positions)


            # ──────────────────────────────────
            # Step 3: 指标计算（60秒一次）
            # ──────────────────────────────────
            if scheduler.should("indicators"):
                for sym in CONFIG["symbols"]:
                    ind = indicator_engine.calc(sym)
                    if ind:
                        indicators[sym] = ind
                # ATR 动态止损同步（指标更新后跑一次）
                if update_atr_dynamic_stops(positions, indicators):
                    save_positions(positions)

            # ──────────────────────────────────
            # Step 4: 情绪数据（300秒一次）
            # ──────────────────────────────────
            if scheduler.should("sentiment"):
                fg_cache = sentiment_engine.fear_greed()
            if scheduler.should("funding"):
                fr_cache = sentiment_engine.funding_rates(CONFIG["symbols"])

            # ──────────────────────────────────
            # Step 5: 开仓评分（60秒一次）
            # ──────────────────────────────────
            if scheduler.should("indicators") and indicators:
                # 熔断检查
                allowed, reason = circuit_breaker.is_trading_allowed()
                if not allowed:
                    log.info(f"交易暂停: {reason}")
                else:
                    btc_ind = indicators.get("BTC", {})  # 2026-03-28 老公指示：用 BTC 作为大盘参考

                    # 🐛 Bug 修复：API 是真相，本地是缓存
                    # 优先相信 API，只有当 API 失败时才用本地缓存
                    current_pos = get_all_positions()
                    api_count = sum(1 for p in current_pos
                        if float(p.get("amount", 0)) != 0
                        and abs(float(p.get("amount", 0))) * float(p.get("entry_price", 0)) >= 5.0)
                    local_count = len(positions)

                    # API 返回 0 时，说明真的没有持仓（刚平仓）
                    # API 有数据时，相信 API
                    # API 失败/异常时，才用本地缓存
                    if api_count > 0:
                        actual_count = api_count  # 相信 API
                    else:
                        actual_count = local_count  # API 为空，用本地（可能是刚启动）

                    log.info(f"📊 开仓前检查：API 持仓={api_count}, 本地缓存={local_count}, 采用={actual_count}/{CONFIG['max_positions']}")

                    # 开仓计数器（本循环内累加）
                    opened_this_loop = 0

                    for sym in CONFIG["symbols"]:
                        # 检查是否已满仓（包含本循环已开的仓位）
                        if actual_count + opened_this_loop >= CONFIG["max_positions"]:
                            log.info(f"⚠️ {sym} 已达最大持仓数，跳过开仓")
                            break
                        if not indicators.get(sym) or not prices.get(sym):
                            continue
                        if not news_filter.check_suspend(sym):
                            continue

                        ind    = indicators[sym]
                        pd     = prices[sym]
                        regime = detect_regime(ind)
                        fr     = fr_cache.get(sym, 0)

                        score, direction = calc_score(
                            sym, pd, ind, regime, fg_cache, fr, btc_ind
                        )

                        if direction == "NONE":
                            continue
                        if not cooldown_manager.can_open(sym, direction):
                            continue

                        # 同一币种不能重复开仓 - 2026-04-27 老公指示：API 真相优先
                        # 本地 positions 缓存可能滞后（多进程/race condition），必须从 Binance API 实时查
                        try:
                            api_positions_check = get_all_positions() or []
                        except Exception as _e:
                            log.warning(f"⚠️ 检查持仓时 API 异常：{_e}，回退本地缓存")
                            api_positions_check = positions
                        api_has_symbol = any(
                            p.get("symbol") == sym
                            and float(p.get("amount", 0)) != 0
                            and abs(float(p.get("amount", 0))) * float(p.get("entry_price", 0)) >= 5.0
                            for p in api_positions_check
                        )
                        local_has_symbol = any(p["symbol"] == sym for p in positions)
                        if api_has_symbol or local_has_symbol:
                            log.info(f"⚠️ {sym} 已有持仓（api={api_has_symbol}, local={local_has_symbol}），跳过重复开仓")
                            continue

                        # 2026-03-28 老公指示：移除同方向限制，只保留总持仓数限制
                        # 原因：同方向 0-1U 浮动的仓位占位置，导致其他信号进不来

                        # ═══════════════════════════════════════
                        # AI 触发条件判断（2026-03-28 老公指示）
                        # ═══════════════════════════════════════
                        # 满仓时只保留新闻和浮亏判断，不做开仓确认
                        is_full = len(positions) >= CONFIG["max_positions"]

                        should_trigger_ai = False

                        # 条件 1：有开仓信号且未满仓
                        if not is_full and score >= CONFIG["score_threshold"][regime]:
                            should_trigger_ai = True
                            log.info(f"🤖 {sym} 触发 AI：开仓信号确认 ({score}分)")

                        # 条件 2：重大新闻（满仓也触发）
                        if news_filter.has_major_news(sym):
                            should_trigger_ai = True
                            log.info(f"🤖 {sym} 触发 AI：重大新闻")

                        # 条件 3：持仓浮亏超 1%，2 小时冷却
                        for pos in positions:
                            if pos["symbol"] == sym:
                                p = prices.get(sym, {}).get("price", 0)
                                entry = pos["entry_price"]
                                pnl_pct = (p-entry)/entry if pos["type"]=="LONG" else (entry-p)/entry
                                last_trigger = ai_loss_cooldown.get(sym, 0)
                                if pnl_pct < -0.02 and time.time() - last_trigger > 7200:
                                    should_trigger_ai = True
                                    ai_loss_cooldown[sym] = time.time()
                                    log.info(f"🤖 {sym} 触发 AI：浮亏{pnl_pct*100:.1f}%")

                        # 只有满足条件才调用 AI
                        if should_trigger_ai:
                            ai_result = ai_predictor.predict(sym, direction, ind, pd, fg_cache)
                        else:
                            ai_result = None  # 纯数学决策
                            log.info(f"🤖 {sym} 跳过 AI（评分{score}分，纯数学决策）")

                        # AI与数学信号冲突时降级观望
                        if ai_result:
                            ai_dir = _normalize_ai_direction(ai_result.get("direction", ""))
                            ai_conf = _normalize_ai_confidence(ai_result.get("confidence", 0))
                            conflict = (
                                (direction == "LONG"  and ai_dir in ("SHORT", "震荡")) or
                                (direction == "SHORT" and ai_dir in ("LONG", "震荡"))
                            )
                            # 🐛 修复：AI 说震荡 → 无条件观望；AI 反向 + 高信心 → 观望
                            if ai_dir == "震荡":
                                log.info(f"⏸️ {sym} AI判断震荡市，观望（数学={direction}，AI=震荡）")
                                continue
                            if conflict and ai_conf >= 0.7:
                                log.info(f"⏸️ {sym} AI与数学信号冲突({ai_conf:.0%})，观望")
                                continue

                        # 开仓前再次检查持仓数（双重保险）
                        # 🐛 Bug 修复：API 是真相，本地是缓存
                        current_pos = get_all_positions()
                        api_count = sum(1 for p in current_pos
                            if float(p.get("amount", 0)) != 0
                            and abs(float(p.get("amount", 0))) * float(p.get("entry_price", 0)) >= 5.0)
                        local_count = len(positions)

                        # API 有数据时相信 API，否则用本地
                        if api_count > 0:
                            actual_count = api_count + opened_this_loop
                        else:
                            actual_count = local_count + opened_this_loop

                        if actual_count >= CONFIG["max_positions"]:
                            log.info(f"⚠️ {sym} 已达最大持仓数 {actual_count}/{CONFIG['max_positions']}，跳过开仓")
                            continue

                        entry = pd["price"]
                        tp_sl = calc_tp_sl(entry, direction, ind, pd)
                        # Phase 2：AI 观点一致性分（第 6 维，同向加分/反向低置信减分，config 外置 ai_score）
                        ai_score_adj = ai_score_adjust(direction, ai_result)
                        score_raw = score
                        score += ai_score_adj
                        # Phase 1：开仓前生成 trade_id 并提取 5 维特征（供特征归因周报使用）
                        trade_id = new_trade_id(sym)
                        features = calc_features(sym, pd, ind, fg_cache, fr, btc_ind, direction)
                        pos = open_position(sym, direction, entry, score, tp_sl, regime, ai_result, trade_id)
                        # 只有订单成功才记录持仓和冷却
                        if pos.get("order_result", {}).get("success", True):
                            record_open(trade_id, sym, direction, score, regime, features, ai_result, entry, score_raw, ai_score_adj)
                            positions.append(pos)
                            cooldown_manager.record(sym, direction)
                            save_positions(positions)
                            opened_this_loop += 1  # 累加本循环开仓数
                            log.info(f"✅ {sym} 开仓成功，当前持仓：{len(positions)}/{CONFIG['max_positions']}")
                        else:
                            # ✅ 2026-03-24 修复：失败也要记录冷却，防止循环重试刷屏
                            cooldown_manager.record(sym, direction)
                            log.error(f"❌ {sym} 开仓失败，已记录冷却 300 秒")

            # 刷新推送缓冲区（合并同一币种的信号）
            merged_signals = cooldown_manager.flush_push_buffer()
            for signal in merged_signals:
                _send_push_signal(signal)

            # ──────────────────────────────────
            # Step 6: 整点汇报（每小时一次，重启后自动补发）
            # ──────────────────────────────────
            current_hour = datetime.now().hour
            current_time = time.time()

            # 整点汇报：每小时第一次检查时触发 + 距离上次推送至少 30 分钟
            if current_hour != last_report_hour and (current_time - last_report_time) > 1800:
                # 进程刚启动的首个整点：指标可能尚未完成首轮计算（tick<4），先补齐再汇报/扫描
                if not indicators or len(indicators) < len(CONFIG["symbols"]):
                    for _sym in CONFIG["symbols"]:
                        _ind = indicator_engine.calc(_sym)
                        if _ind:
                            indicators[_sym] = _ind
                # 整点汇报：每小时第一次检查时触发（不依赖 tick）
                # 评分/市场状态由 main 预计算，notify 仅渲染（分层依赖方向）
                score_map = _build_hourly_score_map(
                    CONFIG["symbols"], prices, indicators, fg_cache
                )
                hourly_report(
                    positions, prices, indicators, circuit_breaker, fg_cache,
                    indicator_engine, score_map
                )
                last_report_hour = current_hour
                last_report_time = current_time  # 记录推送时间

                # 方案B：整点全量 AI 观点扫描（异步，不阻塞主循环，仅记录不交易）
                asyncio.create_task(_run_ai_scan(
                    ai_predictor, CONFIG["symbols"], prices, indicators, fg_cache
                ))

            # 保存状态（包含 peak_pnl 用于移动止盈）
            save_state({
                "last_update": datetime.now().isoformat(),
                "prices":      {k: v.get("price") for k, v in prices.items()},
                "positions":   len(positions),
                "daily_pnl":   circuit_breaker.daily_pnl,
                "daily_pnl_date": datetime.now().date().isoformat(),
                "fear_greed":  fg_cache,
                "tick":        scheduler.tick,
                "last_report_hour": last_report_hour,
                "last_report_time": last_report_time,
                "peak_pnl_map":  {p["symbol"]: p.get("peak_pnl", 0.0) for p in positions},  # 保存峰值盈亏
                "paused_until":  circuit_breaker.paused_until.isoformat() if circuit_breaker.paused_until else None,  # 熔断暂停持久化
                "consecutive_losses": circuit_breaker.consecutive_losses,
                "account_balance": get_last_balance_snapshot(),  # 余额快照（整点更新）
            })

        except Exception as e:
            log.error(f"主循环异常: {e}", exc_info=True)

        # 精确15秒间隔
        elapsed = time.time() - loop_start
        await asyncio.sleep(max(0, CONFIG["scan_interval"] - elapsed))


if __name__ == "__main__":

    asyncio.run(main())
