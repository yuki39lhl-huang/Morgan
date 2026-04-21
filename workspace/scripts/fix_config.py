#!/usr/bin/env python3
"""
2026-03-28 老公指示：修改 CONFIG 配置
- position_size_pct: 0.25 → 0.10（10%）
"""

with open("/root/.openclaw/workspace/scripts/crypto_signal_monitor.py", "r", encoding="utf-8") as f:
    content = f.read()

# 替换 position_size_pct
old_line = '"position_size_pct": 0.25,     # 单仓 25%'
new_line = '"position_size_pct": 0.10,     # 单仓 10% - 2026-03-28 老公指示'

if old_line in content:
    content = content.replace(old_line, new_line)
    
    with open("/root/.openclaw/workspace/scripts/crypto_signal_monitor.py", "w", encoding="utf-8") as f:
        f.write(content)
    
    print("✅ CONFIG.position_size_pct 已修改为 0.10 (10%)")
else:
    print("❌ 未找到 position_size_pct 配置行")
