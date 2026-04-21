#!/usr/bin/env python3
# SOL 开仓安全执行器（SOUL.md 铁律校验版）

import sys
sys.path.insert(0, '/root/.openclaw/workspace/scripts')

from binance_auto_trade import place_order

# 参数硬编码（防注入），按函数签名顺序
result = place_order(
    'SOLUSDT',
    'BUY',
    0.109,
    leverage=10,
    tp_price=[187.10, 189.00, 191.00],
    sl_price=180.20
)

print(result)
