#!/usr/bin/env python3
"""
修复 v6.0 BNB 精度问题
添加 format_quantity 函数到 open_position 中
"""

with open('/root/.openclaw/workspace/scripts/crypto_signal_monitor.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 在 CONFIG 后面添加精度映射
old_config_end = '''    # 扫描
    "scan_interval": 15,
}'''

new_config_end = '''    # 扫描
    "scan_interval": 15,
    
    # 币种精度映射（Binance stepSize）
    "quantity_precision": {
        'COS': 0, 'ETH': 3, 'DOGE': 0, 'C': 0, 'ZEC': 3,
        'SOL': 2, 'PIXEL': 0, 'TAO': 3, 'XRP': 1, 'DEGO': 1,
        'BNB': 3,  # BNB stepSize: 0.001
    },
}'''

content = content.replace(old_config_end, new_config_end)

# 2. 添加 format_quantity 函数
old_open_func = '''def open_position(
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

    qty = (CONFIG["total_capital"] * size_pct) / entry_price'''

new_open_func = '''def get_quantity_precision(symbol: str, price: float) -> int:
    """获取币种数量精度"""
    return CONFIG["quantity_precision"].get(symbol.upper(), 8)

def format_quantity(symbol: str, quantity: float, price: float) -> float:
    """格式化数量到正确的精度"""
    precision = get_quantity_precision(symbol, price)
    return round(quantity, precision)

def open_position(
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

    # 计算数量并格式化到正确精度
    raw_qty = (CONFIG["total_capital"] * size_pct) / entry_price
    qty = format_quantity(symbol, raw_qty, entry_price)'''

content = content.replace(old_open_func, new_open_func)

with open('/root/.openclaw/workspace/scripts/crypto_signal_monitor.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("✅ BNB 精度修复完成！")
print("   - 添加 quantity_precision 配置")
print("   - 添加 get_quantity_precision() 函数")
print("   - 添加 format_quantity() 函数")
print("   - open_position() 使用格式化后的数量")
