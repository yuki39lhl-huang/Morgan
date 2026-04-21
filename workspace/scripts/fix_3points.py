#!/usr/bin/env python3
"""
2026-03-28 老公指示：三处修改
1. 仓位计算加杠杆
2. 移动止盈参数
3. TP1 改全平
"""

with open("/root/.openclaw/workspace/scripts/crypto_signal_monitor.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. 修改仓位计算 - 加杠杆
old_qty = 'raw_qty = (CONFIG["total_capital"] * size_pct) / entry_price'
new_qty = 'raw_qty = (CONFIG["total_capital"] * size_pct * 10) / entry_price  # 2026-03-28 老公指示：加 10x 杠杆'
if old_qty in content:
    content = content.replace(old_qty, new_qty)
    print("✅ 1. 仓位计算已加杠杆 (×10)")
else:
    print("❌ 1. 未找到仓位计算行")

# 2. 修改移动止盈参数
content = content.replace(
    '"trailing_trigger_pct": 0.06,  # 盈利 4.5% 启动',
    '"trailing_trigger_pct": 0.02,  # 盈利 2% 启动 - 2026-03-28 老公指示'
)
content = content.replace(
    '"trailing_gap_pct":     0.02,  # 追踪距离 1.5%',
    '"trailing_gap_pct":     0.01,  # 回撤 1% 出场 - 2026-03-28 老公指示'
)
print("✅ 2. 移动止盈参数已修改 (trigger=2%, gap=1%)")

# 3. 修改 TP1 全平 - exits.append
content = content.replace('exits.append((pos, "TP1", 0.5))', 'exits.append((pos, "TP1", 1.0))  # 2026-03-28 老公指示：全平')
print("✅ 3. TP1 exits.append 已改为 1.0 (全平)")

# 4. 修改 TP1 处理逻辑 - 直接 remove
old_tp1 = '''                if reason == "TP1":
                    # 2026-03-28 老公指示：取消双 TP，一次全平
                    pos["tp1_hit"] = True
                    pos["size_remaining"] = 0.0  # 全平
                    log.info(f"TP1 触发 {pos['symbol']}，一次全平（取消双 TP）")
                    positions.remove(pos)  # 直接移除持仓'''

new_tp1 = '''                if reason == "TP1":
                    # 2026-03-28 老公指示：TP1 全平，直接移除
                    positions.remove(pos)
                    log.info(f"TP1 触发 {pos['symbol']}，全平移除")'''

if old_tp1 in content:
    content = content.replace(old_tp1, new_tp1)
    print("✅ 4. TP1 处理逻辑已改为直接 remove")
else:
    print("⚠️ 4. 未找到旧 TP1 逻辑，可能格式不同")

with open("/root/.openclaw/workspace/scripts/crypto_signal_monitor.py", "w", encoding="utf-8") as f:
    f.write(content)

print("\n✅ 所有修改完成")
