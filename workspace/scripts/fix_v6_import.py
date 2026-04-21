#!/usr/bin/env python3
"""
修复 v6.0 导入问题
"""

# 读取文件
with open('/root/.openclaw/workspace/scripts/crypto_signal_monitor.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 修复导入 - 移除 close_position，添加日志定义在前
old_import = '''import asyncio
import json
import time
import logging
import os
import sys
import requests
import websockets
import numpy as np
from datetime import datetime, timedelta
from collections import deque
from typing import Optional

# 添加脚本目录到路径，以便导入 binance_auto_trade
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 自动交易模块导入
try:
    from binance_auto_trade import place_order, get_all_positions, close_position as api_close_position
    AUTO_TRADE_ENABLED = True
    log.info("✅ 自动交易模块已加载")
except Exception as e:
    AUTO_TRADE_ENABLED = False
    log.warning(f"⚠️ 自动交易模块未加载：{e}")'''

new_import = '''import asyncio
import json
import time
import logging
import os
import sys
import requests
import websockets
import numpy as np
from datetime import datetime, timedelta
from collections import deque
from typing import Optional

# ─────────────────────────────────────────────
# 日志（定义在导入前，避免 NameError）
# ─────────────────────────────────────────────
LOG_FILE = "crypto_monitor.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# 添加脚本目录到路径，以便导入 binance_auto_trade
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 自动交易模块导入
try:
    from binance_auto_trade import place_order, get_all_positions
    AUTO_TRADE_ENABLED = True
    log.info("✅ 自动交易模块已加载")
except Exception as e:
    AUTO_TRADE_ENABLED = False
    log.warning(f"⚠️ 自动交易模块未加载：{e}")'''

content = content.replace(old_import, new_import)

# 2. 修复 open_position - 用 place_order 代替
old_open = '''    # 实盘交易调用
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
            order_result = {"error": str(e)}'''

new_open = '''    # 实盘交易调用
    order_result = None
    if AUTO_TRADE_ENABLED:
        try:
            # 调用 binance_auto_trade 模块下单
            side = "BUY" if direction == "LONG" else "SELL"
            order_result = place_order(
                symbol=f"{symbol}USDT",
                side=side,
                quantity=round(qty, 6),
                leverage=3,
                tp_price=tp_sl.get("tp1_price"),
                sl_price=tp_sl.get("sl_price"),
                price=entry_price,
                reduce_only=False,
            )
            log.info(f"📝 下单结果：{order_result}")
        except Exception as e:
            log.error(f"❌ 下单失败：{e}")
            order_result = {"error": str(e)}'''

content = content.replace(old_open, new_open)

# 3. 修复 close_position - 用 place_order(reduce_only=True) 代替
old_close = '''    # 实盘平仓调用
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
            close_result = {"error": str(e)}'''

new_close = '''    # 实盘平仓调用（使用 place_order with reduce_only=True）
    close_result = None
    if AUTO_TRADE_ENABLED and size_ratio > 0:
        try:
            side = "SELL" if typ == "LONG" else "BUY"  # 平仓方向与持仓相反
            close_result = place_order(
                symbol=f"{symbol}USDT",
                side=side,
                quantity=round(qty, 6),
                leverage=3,
                reduce_only=True,  # 只减仓（平仓）
            )
            log.info(f"📝 平仓结果：{close_result}")
        except Exception as e:
            log.error(f"❌ 平仓失败：{e}")
            close_result = {"error": str(e)}'''

content = content.replace(old_close, new_close)

# 4. 删除后面重复的日志定义（避免重复）
# 查找并删除重复的 logging.basicConfig
import re
# 找到第二个 logging.basicConfig 并删除
pattern = r'\n# ─────────────────────────────────────────────\n# 日志\n# ─────────────────────────────────────────────\nlogging\.basicConfig\([^)]+\)\nlog = logging\.getLogger\(__name__\)\n'
content = re.sub(pattern, '\n', content)

# 写回
with open('/root/.openclaw/workspace/scripts/crypto_signal_monitor.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("✅ 修复完成！")
