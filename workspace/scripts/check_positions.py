#!/usr/bin/env python3
import json
from binance_auto_trade import get_all_positions
import requests

PROXIES = {'https': 'http://127.0.0.1:7890', 'http': 'http://127.0.0.1:7890'}

positions = get_all_positions()
print('## 📊 当前持仓盈亏汇报\n')

total_pnl = 0
for pos in positions:
    symbol = pos['symbol']
    ptype = pos['type']
    entry = pos['entry_price']
    amount = pos['amount']
    unrealized_pnl = pos['unrealized_pnl']
    total_pnl += unrealized_pnl
    
    # 获取当前价格
    try:
        # 🐛 Bug 修复：禁用 SSL 验证，防止代理 SSL 错误
        resp = requests.get(f'https://api.binance.com/api/v3/ticker/price?symbol={symbol}USDT', 
                          proxies=PROXIES, timeout=5, verify=False)
        data = resp.json()
        current_price = float(data['price'])
    except:
        current_price = entry * (1 + unrealized_pnl / (abs(amount) * entry))
    
    # 计算盈亏百分比
    notional = abs(amount) * entry
    pnl_pct = (unrealized_pnl / notional) * 100 if notional > 0 else 0
    
    direction = '🟢' if ptype == 'LONG' else '🔴'
    pnl_sign = '+' if unrealized_pnl >= 0 else ''
    pnl_color = '✅' if unrealized_pnl >= 0 else '❌'
    
    print(f'{direction} **{symbol} {ptype}**')
    print(f'   开仓价：${entry:.6f}')
    print(f'   当前价：${current_price:.6f}')
    print(f'   持仓价值：${notional:.2f} USDT')
    print(f'   盈亏：{pnl_color} ${pnl_sign}{unrealized_pnl:.4f} USDT ({pnl_sign}{pnl_pct:.2f}%)')
    print()

print(f'**总盈亏：${total_pnl:+.4f} USDT**')
