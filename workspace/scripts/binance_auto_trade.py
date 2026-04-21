#!/usr/bin/env python3
"""
Binance 自动交易模块 - 测试版
⚠️ 测试模式：使用 Binance Testnet
"""

import hashlib
import hmac
import time
import requests
import json
import logging
from datetime import datetime, timezone

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger(__name__)

# 配置
TESTNET = True  # 测试网模式
BASE_URL = "https://testnet.binancefuture.com" if TESTNET else "https://fapi.binance.com"

# 从配置文件加载
with open("/root/.openclaw/workspace/scripts/auto_trade_config.json") as f:
    CONFIG = json.load(f)

API_KEY = CONFIG["binance_api"]["api_key"]
SECRET_KEY = CONFIG["binance_api"]["secret_key"]

# 日志文件
LOG_FILE = "/root/.openclaw/workspace/scripts/crypto_monitor.log"

# 代理配置（Hysteria2 本地代理）
PROXIES = {
    'https': 'http://127.0.0.1:7890',
    'http': 'http://127.0.0.1:7890'
}

# 风控参数
RISK = CONFIG["risk_control"]
MAX_POSITION_SIZE = RISK["total_capital_usdt"] * (RISK["position_size_pct"] / 100)
DAILY_LOSS_LIMIT = RISK["total_capital_usdt"] * (RISK["daily_loss_limit_pct"] / 100)
MAX_LEVERAGE = RISK["max_leverage"]
MAX_POSITIONS = RISK["max_positions"]

def get_signature(query_string):
    """生成 HMAC SHA256 签名"""
    return hmac.new(SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()

def request(method, path, params=None):
    """发送签名请求"""
    timestamp = int(time.time() * 1000)
    
    if params is None:
        params = {}
    params['timestamp'] = timestamp
    params['recvWindow'] = 30000  # 2026-03-20 老公指示：10 秒→30 秒缓冲
    
    query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
    signature = get_signature(query_string)
    
    headers = {
        'X-MBX-APIKEY': API_KEY,
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    
    url = f"{BASE_URL}{path}?{query_string}&signature={signature}"
    
    try:
        # 🐛 Bug 修复：禁用 SSL 验证，防止代理 SSL 错误
        resp = requests.request(method, url, headers=headers, timeout=10, proxies=PROXIES, verify=False)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}

def get_account_balance():
    """获取账户余额"""
    result = request("GET", "/fapi/v2/balance")
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and item.get("asset") == "USDT":
                return float(item.get("availableBalance", 0))
    return 0

def get_position(symbol):
    """获取当前持仓"""
    result = request("GET", "/fapi/v2/positionRisk")
    if "error" not in result:
        for pos in result:
            if pos.get("symbol") == symbol:
                return {
                    "amount": float(pos.get("positionAmt", 0)),
                    "entry_price": float(pos.get("entryPrice", 0)),
                    "unrealized_pnl": float(pos.get("unrealizedProfit", 0))
                }
    return None

def get_all_positions():
    """获取所有实际持仓（从 Binance API）"""
    result = request("GET", "/fapi/v2/positionRisk")
    positions = []
    
    # 检查返回结果
    if not result:
        log.warning("⚠️ Binance API 返回空结果，可能网络故障")
        return []
    
    if isinstance(result, dict) and "error" in result:
        log.error(f"❌ Binance API 错误：{result}")
        return []
    
    if isinstance(result, list) and "error" not in str(result):
        for pos in result:
            amount = float(pos.get("positionAmt", 0))
            if amount != 0:  # 只返回有持仓的币种
                symbol = pos.get("symbol", "").replace("USDT", "")
                mark_price = float(pos.get("markPrice", 0))
                positions.append({
                    "symbol": symbol,
                    "type": "LONG" if amount > 0 else "SHORT",
                    "entry_price": float(pos.get("entryPrice", 0)),
                    "amount": amount,
                    "unrealized_pnl": float(pos.get("unRealizedProfit", 0)),  # API 原始盈亏
                    "current_price": mark_price,  # 用 markPrice（和币安 APP 一致）
                    "high_24h": mark_price,       # 测试网无 24h 数据，用 markPrice 兜底
                    "low_24h": mark_price
                })
        log.info(f"✅ 从 Binance API 获取 {len(positions)} 个持仓")
    else:
        log.warning(f"⚠️ Binance API 返回异常：{result}")
    
    return positions

def get_quantity_precision(symbol, price):
    """
    根据 Binance API 的 stepSize 动态获取数量精度
    来源：/fapi/v1/exchangeInfo LOT_SIZE.stepSize
    """
    # 币种精度映射（仅保留当前交易的币种）
    # 更新：2026-03-28 老公指示：添加 BTC
    precision_map = {
        'BTC': 3,      # stepSize: 0.001
        'ETH': 3,      # stepSize: 0.001
        'SOL': 2,      # stepSize: 0.01
        'BNB': 2,      # stepSize: 0.01 ✅ 2026-03-28 修复：3→2 位
        'DOT': 1,      # stepSize: 0.1
        'LINK': 2,     # stepSize: 0.01
        'XRP': 1,      # stepSize: 0.1 ✅ 2026-03-31 同步：加回 XRP
    }
    
    return precision_map.get(symbol.upper(), 8)  # 默认 8 位

def format_quantity(symbol, quantity, price):
    """格式化数量到正确的精度（Binance 要求 8 位小数）"""
    precision = get_quantity_precision(symbol, price)
    
    # 简单 round 到正确精度
    # 修复：XRP 精度 1 位，8.5/2=4.25 → 4.2（向下兼容）
    return round(quantity, precision)

def format_price(price):
    """格式化价格到正确的精度"""
    if price >= 1000:
        return round(price, 2)
    elif price >= 100:
        return round(price, 2)
    elif price >= 10:
        return round(price, 2)
    elif price >= 1:
        return round(price, 2)
    elif price >= 0.1:
        return round(price, 4)
    elif price >= 0.01:
        return round(price, 5)
    else:
        return round(price, 6)

def set_isolated_margin(symbol):
    """
    设置逐仓模式 (ISOLATED)
    2026-03-28 老公指示：全仓→逐仓，精准控制风险
    """
    timestamp = int(time.time() * 1000)
    params = {
        "symbol": symbol,
        "marginType": "ISOLATED",
        "timestamp": timestamp,
        "recvWindow": 30000
    }
    query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
    signature = get_signature(query_string)
    
    headers = {
        'X-MBX-APIKEY': API_KEY,
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    
    url = f"{BASE_URL}/fapi/v1/marginType?{query_string}&signature={signature}"
    
    try:
        # 🐛 Bug 修复：禁用 SSL 验证，防止代理 SSL 错误
        resp = requests.post(url, headers=headers, timeout=10, proxies=PROXIES, verify=False)
        result = resp.json()
        if "code" not in result or result.get("code") == 200 or result.get("code") == 0:  # Binance 返回 200 表示成功
            log.info(f"✅ {symbol} 已设置为逐仓模式 (ISOLATED)")
            return True
        else:
            log.warning(f"⚠️ {symbol} 设置逐仓模式失败：{result}")
            return False
    except Exception as e:
        log.error(f"❌ {symbol} 设置逐仓模式异常：{e}")
        return False

def get_margin_ratio():
    """
    获取账户保证金率
    2026-03-28 老公指示：保证金率<5% 立即强制平仓
    """
    result = request("GET", "/fapi/v2/account")
    if "error" not in result and "m" in result:
        # m = 保证金率 (如 0.05 = 5%)
        return float(result.get("m", 1.0))
    return 1.0

def close_all_positions():
    """
    平仓所有持仓（爆仓保护用）
    """
    positions = get_all_positions()
    for pos in positions:
        try:
            symbol = pos['symbol']
            amt = float(pos.get('amount', 0))
            if amt != 0:
                side = 'SELL' if amt > 0 else 'BUY'
                qty = abs(amt)
                place_order(symbol, side, qty, reduce_only=True)
                log.info(f"✅ 爆仓保护平仓 {symbol} {side} {qty}")
        except Exception as e:
            log.error(f"❌ 爆仓保护平仓失败 {symbol}: {e}")

def check_margin_ratio_protection():
    """
    爆仓保护检查
    保证金率低于 5% 立即平仓所有持仓
    """
    margin_ratio = get_margin_ratio()
    threshold = RISK.get("margin_ratio_warning", 0.05)
    
    if margin_ratio < threshold:
        log.warning(f"🚨 爆仓保护触发！保证金率={margin_ratio:.4f} < {threshold:.4f}")
        close_all_positions()
        log.critical("⚠️ 保证金率过低，已强制平仓所有持仓")
        return True
    return False

def place_order(symbol, side, quantity, leverage=10, tp_price=None, sl_price=None, price=0, reduce_only=False):
    """
    下单
    
    Args:
        symbol: 币种符号 (如 BTCUSDT)
        side: BUY/SELL
        quantity: 数量
        leverage: 杠杆倍数 (默认 10x)
        tp_price: 止盈价格 (可选)
        sl_price: 止损价格 (可选)
        price: 当前价格（用于计算精度）
        reduce_only: 是否只减仓（平仓时用，不受最低金额限制）
    """
    # 2026-03-28 老公指示：开仓前先设置逐仓模式
    if not reduce_only:
        set_isolated_margin(symbol)
    
    # 格式化数量和价格（根据 Binance 精度要求）
    formatted_quantity = format_quantity(symbol.replace("USDT", ""), quantity, price) if price > 0 else quantity
    formatted_tp = format_price(tp_price) if tp_price else None
    formatted_sl = format_price(sl_price) if sl_price else None
    
    log_order_info(symbol, side, formatted_quantity, price, formatted_tp, formatted_sl, reduce_only)
    
    # 1. 设置杠杆（reduce_only 时跳过）
    if not reduce_only:
        leverage_result = request("POST", "/fapi/v1/leverage", {
            "symbol": symbol,
            "leverage": leverage
        })
    else:
        leverage_result = {"skipped": True}
    
    # 2. 下单
    order_params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": formatted_quantity
    }
    if reduce_only:
        order_params["reduceOnly"] = "true"
    
    order_result = request("POST", "/fapi/v1/order", order_params)
    
    # 3. 设置止盈止损（如果有）- 2026-03-31 老公指示：挂 Binance 止损单
    if formatted_sl:
        # 止损单（开仓时同时提交，代理挂了也会自动执行）
        sl_side = "SELL" if side == "BUY" else "BUY"
        sl_result = request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": sl_side,
            "type": "STOP_MARKET",
            "stopPrice": formatted_sl,
            "closePosition": "true",  # 全平
            "workingType": "MARK_PRICE",  # 标记价格触发
            "timeInForce": "GTE_GTC"  # 触发后有效
        })
        log.info(f"📌 {symbol} 止损单已提交 Binance：{formatted_sl} (closePosition=true)")
    
    # 止盈单（可选）
    if formatted_tp:
        tp_side = "SELL" if side == "BUY" else "BUY"
        tp_result = request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": tp_side,
            "type": "STOP_MARKET",
            "stopPrice": formatted_tp,
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "timeInForce": "GTE_GTC"
        })
        log.info(f"📌 {symbol} 止盈单已提交 Binance：{formatted_tp}")
    
    # 检查订单是否成功
    order_success = True
    order_msg = ""
    
    if "error" in str(order_result) or "code" in str(order_result):
        order_success = False
        order_msg = f"订单失败：{order_result}"
    
    return {
        "leverage": leverage_result,
        "order": order_result,
        "quantity_used": formatted_quantity,
        "tp_price": formatted_tp,
        "sl_price": formatted_sl,
        "success": order_success,
        "message": order_msg
    }

def log_order_info(symbol, side, quantity, price, tp_price, sl_price, reduce_only=False):
    """记录订单信息到日志"""
    try:
        if reduce_only:
            action = "平仓"
        else:
            action = "开多" if side == "BUY" else "开空"
        log_msg = f"📝 下单详情：{symbol} {action} 数量={quantity:.6f} @ ${price:.6f}"
        if tp_price:
            log_msg += f", 止盈=${tp_price:.6f}"
        if sl_price:
            log_msg += f", 止损=${sl_price:.6f}"
        
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_msg + "\n")
        print(log_msg)
    except:
        pass

def cancel_all_orders(symbol):
    """取消所有挂单"""
    return request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

def test_connection():
    """测试 API 连接"""
    result = request("GET", "/fapi/v1/time")
    if "serverTime" in result:
        return True, "连接成功"
    return False, result.get("error", "未知错误")

if __name__ == "__main__":
    print("=" * 50)
    print("🧪 Binance 自动交易模块 - 测试模式")
    print("=" * 50)
    
    # 测试连接
    print("\n📡 测试 API 连接...")
    success, msg = test_connection()
    if success:
        print(f"✅ {msg}")
    else:
        print(f"❌ {msg}")
    
    # 获取余额
    print("\n💰 获取账户余额...")
    balance = get_account_balance()
    print(f"   USDT 可用余额：{balance:.2f}")
    
    # 获取持仓
    print("\n📊 当前持仓:")
    result = request("GET", "/fapi/v2/positionRisk")
    if isinstance(result, list):
        for pos in result:
            if isinstance(pos, dict):
                amt = float(pos.get("positionAmt", 0))
                if amt != 0:
                    print(f"   {pos['symbol']}: {amt} @ ${pos.get('entryPrice', 0)}")
        print("   (无持仓)")
    else:
        print(f"   获取失败：{result}")
    
    print("\n" + "=" * 50)
    print("✅ 模块测试完成")
    print("=" * 50)
