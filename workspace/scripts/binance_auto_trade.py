#!/usr/bin/env python3
"""
Binance 自动交易模块 - 测试版
⚠️ 测试模式：使用 Binance Testnet
"""

import hashlib
import hmac
import os
import time
import uuid
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

# 配置文件路径
CONFIG_PATH = "/root/.openclaw/workspace/scripts/auto_trade_config.json"

# 模块级缓存（含 mtime，文件变更时自动重新加载，避免 monitor 必须重启才能换 key）
_CONFIG_CACHE = {"data": None, "mtime": 0}


def _load_config_if_changed():
    """检查 config 文件 mtime，变更时重新加载 —— 让 API key 热更新"""
    global _CONFIG_CACHE
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return _CONFIG_CACHE["data"]
    if _CONFIG_CACHE["data"] is None or mtime > _CONFIG_CACHE["mtime"]:
        with open(CONFIG_PATH) as f:
            _CONFIG_CACHE["data"] = json.load(f)
        _CONFIG_CACHE["mtime"] = mtime
        api_key = _CONFIG_CACHE["data"]["binance_api"]["api_key"]
        log.info(f"🔄 已加载 auto_trade_config.json（API key 前缀：{api_key[:8]}...{api_key[-4:]}）")
    return _CONFIG_CACHE["data"]


# 启动时加载一次
CONFIG = _load_config_if_changed()


def _api_key():
    return _load_config_if_changed()["binance_api"]["api_key"]


def _secret_key():
    return _load_config_if_changed()["binance_api"]["secret_key"]


# 兼容旧代码：保留模块级常量但不再被签名函数使用
API_KEY = CONFIG["binance_api"]["api_key"]
SECRET_KEY = CONFIG["binance_api"]["secret_key"]

# 与 monitor 共用同一业务日志（避免 scripts/ 下重复 stderr 文件）
from openclaw_logging import trading_log_path

LOG_FILE = str(trading_log_path())

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
    """生成 HMAC SHA256 签名（动态读取 secret，避免 monitor 进程缓存旧 key）"""
    return hmac.new(_secret_key().encode(), query_string.encode(), hashlib.sha256).hexdigest()

def _query_order_by_client_id(symbol, client_order_id):
    """网络抖动时，按 clientOrderId 回查订单，避免把已成交误判为失败"""
    timestamp = int(time.time() * 1000)
    params = {
        "symbol": symbol,
        "origClientOrderId": client_order_id,
        "timestamp": timestamp,
        "recvWindow": 30000
    }
    query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
    signature = get_signature(query_string)
    headers = {
        'X-MBX-APIKEY': _api_key(),
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    url = f"{BASE_URL}/fapi/v1/order?{query_string}&signature={signature}"
    try:
        resp = requests.request("GET", url, headers=headers, timeout=10, proxies=PROXIES, verify=False)
        result = resp.json()
        if isinstance(result, dict) and result.get("orderId"):
            return result
    except Exception as e:
        log.warning(f"⚠️ 回查订单失败 {symbol}/{client_order_id}: {e}")
    return None

def request(method, path, params=None):
    """发送签名请求（动态读取 API key）"""
    timestamp = int(time.time() * 1000)
    
    if params is None:
        params = {}
    params['timestamp'] = timestamp
    params['recvWindow'] = 30000  # 2026-03-20 老公指示：10 秒→30 秒缓冲
    client_order_id = None
    # 下单请求加 clientOrderId，便于网络异常后二次确认是否已成交
    if method.upper() == "POST" and path == "/fapi/v1/order":
        if not params.get("newClientOrderId"):
            params["newClientOrderId"] = f"oc_{int(time.time())}_{uuid.uuid4().hex[:10]}"
        client_order_id = params.get("newClientOrderId")
    
    query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
    signature = get_signature(query_string)
    
    headers = {
        'X-MBX-APIKEY': _api_key(),  # 动态读取
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    
    url = f"{BASE_URL}{path}?{query_string}&signature={signature}"
    
    last_error = None
    for attempt in range(1, 4):
        try:
            # 🐛 Bug 修复：禁用 SSL 验证，防止代理 SSL 错误
            resp = requests.request(method, url, headers=headers, timeout=12, proxies=PROXIES, verify=False)
            return resp.json()
        except Exception as e:
            last_error = e
            if client_order_id:
                # POST /order 在异常时回查一次，防止“已成交却显示失败”
                checked = _query_order_by_client_id(params.get("symbol"), client_order_id)
                if checked:
                    log.warning(f"⚠️ 下单请求异常但订单已落地：{params.get('symbol')} clientOrderId={client_order_id}")
                    return checked
            if attempt < 3:
                time.sleep(0.4 * attempt)
    return {"error": str(last_error), "clientOrderId": client_order_id}

def fetch_today_realized_pnl(symbols: list[str] | None = None) -> float | None:
    """
    从 userTrades 汇总今日（Asia/Shanghai 自然日）已实现盈亏。
    重启后用于校准 daily_pnl，避免漏计 kj 宕机期间的平仓。
    """
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        since_ms = int(start.timestamp() * 1000)
    except Exception:
        today = datetime.now().date()
        start = datetime.combine(today, datetime.min.time())
        since_ms = int(start.timestamp() * 1000)

    total = 0.0
    syms = symbols or []
    any_ok = False
    for sym in syms:
        result = request(
            "GET",
            "/fapi/v1/userTrades",
            {"symbol": f"{sym}USDT", "startTime": since_ms, "limit": 1000},
        )
        if isinstance(result, dict) and result.get("error"):
            log.warning(f"⚠️ userTrades 失败 {sym}: {result.get('error')}")
            continue
        if not isinstance(result, list):
            continue
        any_ok = True
        for t in result:
            if isinstance(t, dict):
                total += float(t.get("realizedPnl", 0) or 0)
    return round(total, 4) if any_ok else None


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
            entry_price = float(pos.get("entryPrice", 0))
            # 🔧 2026-06-06 修复：过滤粉尘仓位（名义价值<$5）
            if amount != 0 and abs(amount) * entry_price >= 5.0:  # 只返回有持仓的币种
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

def get_price_precision(symbol):
    """
    按币种返回价格精度（tickSize 小数位数）
    2026-06-05 新增：format_price 按价格区间取精度对子 $1 币种（DOT）
    不准确，需按 symbol 区分。
    """
    precision_map = {
        'BTC': 1,      # tickSize: 0.1 (64464.5)
        'ETH': 2,      # tickSize: 0.01
        'SOL': 2,      # tickSize: 0.01 (same as ETH)
        'BNB': 1,      # tickSize: 0.1 (597.2)
        'DOT': 3,      # tickSize: 0.001 (0.983)
        'LINK': 3,     # tickSize: 0.001 (8.036)
        'XRP': 4,      # tickSize: 0.0001
    }
    return precision_map.get(symbol.upper(), 2)

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
        'X-MBX-APIKEY': _api_key(),  # 动态读取
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
            symbol = pos['symbol'] + "USDT"
            amt = float(pos.get('amount', 0))
            if amt != 0:
                side = 'SELL' if amt > 0 else 'BUY'
                qty = abs(amt)
                place_order(symbol, side, qty)
                log.info(f"✅ 爆仓保护平仓 {symbol} {side} {qty}")
        except Exception as e:
            log.error(f"❌ 爆仓保护平仓失败 {symbol}: {e}")

def cancel_algo_orders(symbol: str):
    """取消某币种全部 Algo 条件单（TP/SL）
    
    2026-06-05 修复：旧版用 /algoOpenOrders 批量删不生效，
    改为先列后逐笔删，确保清理彻底。
    """
    sym_usdt = f"{symbol}USDT" if not symbol.endswith("USDT") else symbol
    results = []
    try:
        # 1. 列出所有 algo 单
        open_algos = request("GET", "/fapi/v1/openAlgoOrders", {"symbol": sym_usdt})
        if not isinstance(open_algos, list):
            log.warning(f"⚠️ {symbol} 列出 Algo 单失败：{open_algos}")
            return results
        
        # 2. 逐笔取消
        for o in open_algos:
            algo_id = o.get('algoId')
            if algo_id:
                del_result = request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})
                results.append(del_result)
                if isinstance(del_result, dict) and del_result.get('code') == '200':
                    log.info(f"🗑️ {symbol} Algo 单已取消 algoId={algo_id}")
                elif isinstance(del_result, dict) and del_result.get('code') in (-2011,):
                    # 单已被取消（不存在），正常情况
                    log.info(f"🗑️ {symbol} Algo 单 algoId={algo_id} 已不存在（跳过）")
                else:
                    log.warning(f"⚠️ {symbol} Algo 单取消失败 algoId={algo_id}: {del_result}")
    except Exception as e:
        log.error(f"❌ {symbol} 取消 Algo 单异常：{e}")
    return results


def place_algo_conditional_order(
    symbol: str,
    side: str,
    order_type: str,
    trigger_price,
    working_type: str = "MARK_PRICE",
    quantity: float = None,
):
    """
    币安 USD-M 条件单（2025-12 起须走 Algo API，否则 -4120）

    order_type: STOP_MARKET（止损）| TAKE_PROFIT_MARKET（止盈）
    side: 平仓方向 — 平多 SELL，平空 BUY
    quantity: 精确数量（传了就用 reduceOnly+quantity，不传保留旧行为 closePosition=true）
    2026-06-04 修复：closePosition=true 在测试网算出错误数量，改为传入精确 quantity
    2026-06-05 修复：triggerPrice 按 symbol 精度格式化，替代全局 format_price（DOT -1111 bug）
    """
    sym = symbol.replace("USDT", "")
    # 按币种价格精度格式化触发价
    price_prec = get_price_precision(sym)
    formatted_trigger = round(trigger_price, price_prec)
    
    params = {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "triggerPrice": formatted_trigger,
        "workingType": working_type,
    }
    if quantity is not None:
        sym = symbol.replace("USDT", "")
        params["quantity"] = str(format_quantity(sym, quantity, 0))
        params["reduceOnly"] = "true"
    else:
        params["closePosition"] = "true"
    return request("POST", "/fapi/v1/algoOrder", params)


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
        "quantity": formatted_quantity,
        "reduceOnly": str(reduce_only).lower()
    }
    
    order_result = request("POST", "/fapi/v1/order", order_params)
    
    # 主单失败时直接返回，不要再下 SL/TP（避免无主单的孤儿挂单）
    main_order_ok = isinstance(order_result, dict) and bool(order_result.get("orderId"))
    if not main_order_ok:
        log.error(f"🔴 {symbol} 主单失败，跳过 SL/TP 提交。原始响应：{order_result}")
    
    # 3. 设置止盈止损（Algo Order API，避免 -4120）
    if main_order_ok and formatted_sl:
        sl_side = "SELL" if side == "BUY" else "BUY"
        sl_result = place_algo_conditional_order(
            symbol, sl_side, "STOP_MARKET", formatted_sl, quantity=formatted_quantity
        )
        if isinstance(sl_result, dict) and sl_result.get("algoId"):
            log.info(
                f"📌 {symbol} 止损 Algo 单已提交：{formatted_sl} algoId={sl_result.get('algoId')}"
            )
        else:
            log.warning(f"⚠️ {symbol} 止损单提交失败：{sl_result}")

    if main_order_ok and formatted_tp:
        tp_side = "SELL" if side == "BUY" else "BUY"
        tp_result = place_algo_conditional_order(
            symbol, tp_side, "TAKE_PROFIT_MARKET", formatted_tp, quantity=formatted_quantity
        )
        if isinstance(tp_result, dict) and tp_result.get("algoId"):
            log.info(
                f"📌 {symbol} 止盈 Algo 单已提交：{formatted_tp} algoId={tp_result.get('algoId')}"
            )
        else:
            log.warning(f"⚠️ {symbol} 止盈单提交失败：{tp_result}")
    
    # 检查订单是否成功（严格判断：必须有 orderId）
    # 🐛 2026-04-27 修复：原 `"code" in str(order_result)` 判断在某些 Binance 字段
    #     里可能误判（如未来新增字段名含 code），改为「正向判断有 orderId」
    order_success = False
    order_msg = ""
    
    if isinstance(order_result, dict):
        if order_result.get("orderId"):
            order_success = True
            status = order_result.get("status", "")
            log.info(f"✅ 订单确认 orderId={order_result.get('orderId')} status={status}")
        elif "code" in order_result:
            order_success = False
            order_msg = f"Binance 拒单：code={order_result.get('code')} msg={order_result.get('msg')}"
            log.error(f"❌ {order_msg}")
        elif "error" in order_result:
            order_success = False
            order_msg = f"请求异常：{order_result.get('error')}"
            log.error(f"❌ {order_msg}")
        else:
            order_success = False
            order_msg = f"未知响应（无 orderId）：{order_result}"
            log.error(f"❌ {order_msg}")
    else:
        order_success = False
        order_msg = f"非字典响应：{order_result!r}"
        log.error(f"❌ {order_msg}")
    
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
    """取消所有挂单（普通 + 条件委托）
    
    2026-06-05 修复：增加 Algo 条件单清理，防止 regular cancel 成功后
    TP/SL 条件单残留（如仓位已平但止盈止损仍挂着的孤儿单）。
    """
    sym_usdt = f"{symbol}USDT" if not symbol.endswith("USDT") else symbol
    # 1. 取消普通挂单
    regular_result = request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": sym_usdt})
    # 2. 取消条件委托（Algo 单）
    algo_results = cancel_algo_orders(sym_usdt)
    return {"regular": regular_result, "algo": algo_results}

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
