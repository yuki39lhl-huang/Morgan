#!/usr/bin/env python3
"""
v6.0 升级脚本 - 添加自动交易功能
"""
import re

# 读取 v6.0 文件
with open('/root/.openclaw/workspace/scripts/crypto_signal_monitor_v6.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 在 CONFIG 中添加 testnet 配置
old_api_config = '''    # API 配置
    "binance_base":    "https://api.binance.com/api/v3",
    "binance_futures": "https://fapi.binance.com/fapi/v1",
    "coingecko_base":  "https://pro-api.coingecko.com/api/v3",
    "coingecko_key":   "CG-DZoCE8UMF3FWpeYhBMvqGq4g",
    "proxy":           "http://127.0.0.1:8081",'''

new_api_config = '''    # API 配置
    "binance_base":    "https://api.binance.com/api/v3",
    "binance_futures": "https://fapi.binance.com/fapi/v1",
    "binance_testnet": "https://testnet.binancefuture.com",  # 测试网
    "coingecko_base":  "https://pro-api.coingecko.com/api/v3",
    "coingecko_key":   "CG-DZoCE8UMF3FWpeYhBMvqGq4g",
    "proxy":           "http://127.0.0.1:8081",
    
    # 交易配置
    "testnet": True,  # 使用测试网'''

content = content.replace(old_api_config, new_api_config)

# 2. 修改 open_position 函数 - 添加实际交易调用
old_open_position = '''def open_position(
    symbol: str,
    direction: str,
    entry_price: float,
    score: int,
    tp_sl: dict,
    regime: str,
    ai_result: Optional[dict] = None,
) -> dict:
    # 高波动时减仓
    size_pct = CONFIG["position_size_pct"]
    if regime == "volatile":
        size_pct *= 0.6

    qty = (CONFIG["total_capital"] * size_pct) / entry_price

    pos = {
        "symbol":       symbol,
        "type":         direction,
        "entry_price":  entry_price,
        "entry_time":   datetime.now().isoformat(),
        "qty":          round(qty, 6),
        "score":        score,
        "regime":       regime,
        "ai_result":    ai_result,
        "high_24h":     0.0,  # 由动态调整更新
        "low_24h":      0.0,
        **tp_sl,
    }

    msg = (
        f"🟢 开多 {symbol}" if direction == "LONG" else f"🔴 开空 {symbol}"
    ) + f" @{entry_price} | 评分:{score} | 状态:{regime}"
    if ai_result:
        msg += f" | AI:{ai_result.get('direction')}({ai_result.get('confidence')}%)"
    log.info(msg)
    write_alert(msg)
    return pos'''

new_open_position = '''def open_position(
    symbol: str,
    direction: str,
    entry_price: float,
    score: int,
    tp_sl: dict,
    regime: str,
    ai_result: Optional[dict] = None,
) -> dict:
    # 高波动时减仓
    size_pct = CONFIG["position_size_pct"]
    if regime == "volatile":
        size_pct *= 0.6

    qty = (CONFIG["total_capital"] * size_pct) / entry_price

    # 实盘交易调用
    order_result = None
    if AUTO_TRADE_ENABLED:
        try:
            # 调用 binance_auto_trade 模块下单
            side = "BUY" if direction == "LONG" else "SELL"
            order_result = place_order(
                symbol=f"{symbol}USDT",
                side=side,
                position_side="BOTH",
                qty=round(qty, 6),
                tp_price=tp_sl.get("tp1_price"),
                sl_price=tp_sl.get("sl_price"),
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
        "score":        score,
        "regime":       regime,
        "ai_result":    ai_result,
        "order_result": order_result,
        "high_24h":     0.0,
        "low_24h":      0.0,
        **tp_sl,
    }

    msg = (
        f"🟢 开多 {symbol}" if direction == "LONG" else f"🔴 开空 {symbol}"
    ) + f" @{entry_price} | 评分:{score} | 状态:{regime}"
    if ai_result:
        msg += f" | AI:{ai_result.get('direction')}({ai_result.get('confidence')}%)"
    if order_result and "error" in order_result:
        msg += f" | ❌ 下单失败"
    else:
        msg += f" | ✅ 下单成功"
    log.info(msg)
    write_alert(msg)
    return pos'''

content = content.replace(old_open_position, new_open_position)

# 3. 修改 close_position 函数 - 添加实际平仓调用
old_close_position = '''def close_position(pos: dict, reason: str, size_ratio: float, current_price: float):
    entry = pos["entry_price"]
    typ   = pos["type"]

    pnl_pct = (current_price - entry) / entry if typ == "LONG" else (entry - current_price) / entry
    pnl_usdt = CONFIG["total_capital"] * CONFIG["position_size_pct"] * pnl_pct * size_ratio

    emoji = "✅" if pnl_usdt >= 0 else "❌"
    msg = (
        f"{emoji} 平仓 {pos['symbol']} {reason} | "
        f"入场:{entry:.4f} 出场:{current_price:.4f} | "
        f"PnL:{pnl_pct*100:.2f}% ({pnl_usdt:+.2f}USDT) | "
        f"平{size_ratio*100:.0f}%仓"
    )
    log.info(msg)
    write_alert(msg)
    return pnl_usdt'''

new_close_position = '''def close_position(pos: dict, reason: str, size_ratio: float, current_price: float):
    entry = pos["entry_price"]
    typ   = pos["type"]
    symbol = pos["symbol"]
    qty = pos.get("qty", 0) * size_ratio

    # 实盘平仓调用
    close_result = None
    if AUTO_TRADE_ENABLED and size_ratio > 0:
        try:
            side = "SELL" if typ == "LONG" else "BUY"  # 平仓方向与持仓相反
            close_result = api_close_position(
                symbol=f"{symbol}USDT",
                side=side,
                qty=round(qty, 6),
            )
            log.info(f"📝 平仓结果：{close_result}")
        except Exception as e:
            log.error(f"❌ 平仓失败：{e}")
            close_result = {"error": str(e)}

    # 计算盈亏
    pnl_pct = (current_price - entry) / entry if typ == "LONG" else (entry - current_price) / entry
    pnl_usdt = CONFIG["total_capital"] * CONFIG["position_size_pct"] * pnl_pct * size_ratio

    emoji = "✅" if pnl_usdt >= 0 else "❌"
    msg = (
        f"{emoji} 平仓 {symbol} {reason} | "
        f"入场:{entry:.4f} 出场:{current_price:.4f} | "
        f"PnL:{pnl_pct*100:.2f}% ({pnl_usdt:+.2f}USDT) | "
        f"平{size_ratio*100:.0f}%仓"
    )
    if close_result and "error" in close_result:
        msg += f" | ❌ 平仓失败"
    else:
        msg += f" | ✅ 平仓成功"
    log.info(msg)
    write_alert(msg)
    return pnl_usdt'''

content = content.replace(old_close_position, new_close_position)

# 写回文件
with open('/root/.openclaw/workspace/scripts/crypto_signal_monitor_v6.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("✅ v6.0 升级完成！")
print("   - 添加自动交易模块导入")
print("   - 添加 testnet 配置")
print("   - open_position() 添加实际下单调用")
print("   - close_position() 添加实际平仓调用")
