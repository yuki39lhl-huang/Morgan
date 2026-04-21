#!/usr/bin/env python3
"""
2026-03-28 老公指示：修改 calc_tp_sl 为固定百分比止盈止损
- 止盈：+4%
- 止损：-2%
"""

import re

with open("/root/.openclaw/workspace/scripts/crypto_signal_monitor.py", "r", encoding="utf-8") as f:
    content = f.read()

# 新的 calc_tp_sl 函数
new_func = '''def calc_tp_sl(
    entry: float,
    direction: str,
    ind: dict,
    price_data: dict,
) -> dict:
    """
    返回 {tp1, tp2, sl, tp1_hit, size_remaining}
    
    2026-03-28 老公指示：改用固定百分比止盈止损
    - 止盈：+4%
    - 止损：-2%
    - 暂时替代 ATR 计算
    """
    # 2026-03-28 老公指示：固定百分比止盈止损
    TAKE_PROFIT_PCT = 0.04  # +4%
    STOP_LOSS_PCT   = 0.02  # -2%
    
    if direction == "LONG":
        tp1 = entry * (1 + TAKE_PROFIT_PCT)  # 做多 +4%
        tp2 = entry * (1 + TAKE_PROFIT_PCT * 1.5)  # TP2 = +6%
        sl  = entry * (1 - STOP_LOSS_PCT)    # 做多 -2%
    else:
        tp1 = entry * (1 - TAKE_PROFIT_PCT)  # 做空 -4%
        tp2 = entry * (1 - TAKE_PROFIT_PCT * 1.5)  # TP2 = -6%
        sl  = entry * (1 + STOP_LOSS_PCT)    # 做空 +2%

    return {
        "tp1_price":      round(tp1, 4),
        "tp2_price":      round(tp2, 4),
        "sl_price":       round(sl, 4),
        "tp1_hit":        False,
        "size_remaining": 1.0,
        "peak_pnl":       0.0,   # 移动止盈用
    }

'''

# 找到旧的 calc_tp_sl 函数并替换
# 匹配从 def calc_tp_sl 到下一个空行 + # 开头的行
pattern = r'def calc_tp_sl\([^)]*\) -> dict:.*?return \{[^}]*"peak_pnl":[^}]*\}'
match = re.search(pattern, content, re.DOTALL)

if match:
    old_func = match.group(0)
    content = content.replace(old_func, new_func.strip())
    
    with open("/root/.openclaw/workspace/scripts/crypto_signal_monitor.py", "w", encoding="utf-8") as f:
        f.write(content)
    
    print("✅ calc_tp_sl 函数已修改为固定百分比止盈止损")
else:
    print("❌ 未找到 calc_tp_sl 函数")
