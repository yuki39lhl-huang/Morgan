#!/usr/bin/env python3
"""
2026-03-28 老公指示：取消双 TP，一次全平
"""

with open("/root/.openclaw/workspace/scripts/crypto_signal_monitor.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

# 找到并修改 TP1 逻辑部分 (大约 1689-1710 行)
new_lines = []
skip_until_else = False

for i, line in enumerate(lines):
    # 找到 if reason == "TP1": 行
    if 'if reason == "TP1":' in line and not skip_until_else:
        # 写入新的 TP1 逻辑
        indent = '                '
        new_lines.append(line)  # if reason == "TP1":
        new_lines.append(indent + '    # 2026-03-28 老公指示：取消双 TP，一次全平\n')
        new_lines.append(indent + '    pos["tp1_hit"] = True\n')
        new_lines.append(indent + '    pos["size_remaining"] = 0.0  # 全平\n')
        new_lines.append(indent + '    log.info(f"TP1 触发 {pos[\'symbol\']}，一次全平（取消双 TP）")\n')
        skip_until_else = True
    elif skip_until_else and line.strip().startswith('else:'):
        new_lines.append(line)
        skip_until_else = False
    elif not skip_until_else:
        new_lines.append(line)
    # 跳过 TP1 逻辑块内的行

with open("/root/.openclaw/workspace/scripts/crypto_signal_monitor.py", "w", encoding="utf-8") as f:
    f.writelines(new_lines)

print("✅ TP1 逻辑已修改为一次全平")
