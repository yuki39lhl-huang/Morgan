#!/usr/bin/env python3
"""
2026-03-28 老公指示：在主循环中添加爆仓保护检查
在 while True 循环开始后立即检查保证金率
"""

with open("/root/.openclaw/workspace/scripts/crypto_signal_monitor.py", "r", encoding="utf-8") as f:
    content = f.read()

# 在 while True: 后添加爆仓保护检查
old_code = '''while True:
        loop_start = time.time()
        scheduler.tick_up()'''

new_code = '''while True:
        loop_start = time.time()
        scheduler.tick_up()
        
        # 2026-03-28 老公指示：爆仓保护检查（保证金率<5% 强制平仓）
        try:
            from binance_auto_trade import check_margin_ratio_protection
            check_margin_ratio_protection()
        except Exception as e:
            log.warning(f"⚠️ 爆仓保护检查失败：{e}")'''

if old_code in content:
    content = content.replace(old_code, new_code)
    
    with open("/root/.openclaw/workspace/scripts/crypto_signal_monitor.py", "w", encoding="utf-8") as f:
        f.write(content)
    
    print("✅ 爆仓保护检查已添加到主循环")
else:
    print("❌ 未找到 while True 循环位置")
