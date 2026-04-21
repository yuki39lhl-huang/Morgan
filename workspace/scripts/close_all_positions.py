#!/usr/bin/env python3
"""
批量平仓脚本 - 关闭所有 Binance 测试网持仓
"""

import hashlib
import hmac
import time
import requests
import json
from datetime import datetime, timezone

# 加载配置
with open("/root/.openclaw/workspace/scripts/auto_trade_config.json") as f:
    CONFIG = json.load(f)

API_KEY = CONFIG["binance_api"]["api_key"]
SECRET_KEY = CONFIG["binance_api"]["secret_key"]
BASE_URL = "https://testnet.binancefuture.com"

def get_signature(query_string):
    """生成 HMAC SHA256 签名"""
    return hmac.new(SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()

def request_api(method, path, params=None):
    """发送签名请求"""
    timestamp = int(time.time() * 1000)
    
    if params is None:
        params = {}
    params['timestamp'] = timestamp
    
    query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
    signature = get_signature(query_string)
    
    headers = {
        'X-MBX-APIKEY': API_KEY,
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    
    url = f"{BASE_URL}{path}?{query_string}&signature={signature}"
    
    try:
        resp = requests.request(method, url, headers=headers, timeout=10)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}

def get_all_positions():
    """获取所有持仓"""
    result = request_api("GET", "/fapi/v2/positionRisk")
    if isinstance(result, list):
        positions = []
        for pos in result:
            amount = float(pos.get("positionAmt", 0))
            if amount != 0:  # 只保留有持仓的
                positions.append({
                    "symbol": pos.get("symbol"),
                    "amount": amount,
                    "entry_price": float(pos.get("entryPrice", 0)),
                    "unrealized_pnl": float(pos.get("unrealizedProfit", 0))
                })
        return positions
    return []

def close_position(symbol, amount):
    """
    平仓
    amount > 0: 做多，需要卖出平仓
    amount < 0: 做空，需要买入平仓
    """
    side = "SELL" if amount > 0 else "BUY"
    quantity = abs(amount)
    
    # 市价平仓
    result = request_api("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": quantity,
        "reduceOnly": "true"  # 只减仓，不反向开仓
    })
    
    return result

def main():
    print("=" * 60)
    print("🔄 批量平仓脚本 - Binance 测试网")
    print("=" * 60)
    
    # 获取持仓
    print("\n📊 获取当前持仓...")
    positions = get_all_positions()
    
    if not positions:
        print("✅ 当前无持仓，无需平仓")
        return
    
    print(f"📦 发现 {len(positions)} 个持仓：\n")
    
    total_pnl = 0
    
    for pos in positions:
        symbol = pos['symbol']
        amount = pos['amount']
        entry_price = pos['entry_price']
        pnl = pos['unrealized_pnl']
        total_pnl += pnl
        
        direction = "LONG" if amount > 0 else "SHORT"
        print(f"  {symbol} {direction}: {amount:.4f} @ ${entry_price:.2f} | 未实现盈亏：${pnl:.2f}")
    
    print(f"\n💰 总未实现盈亏：${total_pnl:.2f}")
    print("\n" + "=" * 60)
    
    # 自动平仓（无需确认）
    print("\n⚠️  自动执行平仓...")
    
    # 执行平仓
    print("\n🔄 开始平仓...\n")
    
    for pos in positions:
        symbol = pos['symbol']
        amount = pos['amount']
        
        result = close_position(symbol, amount)
        
        if 'code' in result and result['code'] == 0:
            print(f"✅ {symbol} 平仓成功")
        elif 'orderId' in result:
            print(f"✅ {symbol} 平仓成功 (订单 ID: {result['orderId']})")
        else:
            print(f"❌ {symbol} 平仓失败：{result}")
    
    print("\n" + "=" * 60)
    print("🎉 平仓完成！")
    print("=" * 60)
    
    # 验证
    print("\n📊 验证剩余持仓...")
    time.sleep(2)
    remaining = get_all_positions()
    if remaining:
        print(f"⚠️  还有 {len(remaining)} 个仓位未平：")
        for pos in remaining:
            print(f"  {pos['symbol']}: {pos['amount']}")
    else:
        print("✅ 所有仓位已清空！")

if __name__ == "__main__":
    main()
