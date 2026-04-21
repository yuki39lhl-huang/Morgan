#!/usr/bin/env python3
"""
2026-03-28 老公完整调整方案：
1. 取消双 TP，改一次全平
2. 移动止盈参数调整
3. 确认配置正确
"""

# 1. 修改移动止盈参数
with open("/root/.openclaw/workspace/scripts/crypto_signal_monitor.py", "r", encoding="utf-8") as f:
    content = f.read()

# 移动止盈参数
content = content.replace(
    '"trailing_trigger_pct": 0.06,  # 盈利 4.5% 启动',
    '"trailing_trigger_pct": 0.02,  # 盈利 2% 启动 - 2026-03-28 老公指示'
)
content = content.replace(
    '"trailing_gap_pct":     0.02,  # 追踪距离 1.5%',
    '"trailing_gap_pct":     0.01,  # 回撤 1% 出场 - 2026-03-28 老公指示'
)

# 2. 修改 TP1 触发逻辑 - 不再移止损到成本，不再保留 50%
old_tp1_logic = '''if reason == "TP1":
                    # 平 50%，止损移至成本
                    pos["tp1_hit"]        = True
                    pos["size_remaining"] = 0.5
                    pos["sl_price"]       = pos["entry_price"]
                    log.info(f"TP1 触发 {pos['symbol']}，止损移至成本，保留 50% 仓位")

                    # 2026-03-28 老公指示：TP1 触发后 TP2 动态下移（防止剩余仓位卡住）
                    # 原 TP2 不变，现在下移到 当前价+ATR×2.0（让 TP2 更容易触发）
                    sym = pos["symbol"]
                    if sym in prices and sym in indicators:
                        atr = indicators[sym].get("atr", 0)
                        if atr and atr > 0:
                            if pos["type"] == "LONG":
                                pos["tp2_price"] = p + atr * 2.0
                            else:  # SHORT
                                pos["tp2_price"] = p - atr * 2.0'''

new_tp1_logic = '''if reason == "TP1":
                    # 2026-03-28 老公指示：取消双 TP，一次全平
                    # 不再保留 50%，直接全平
                    pos["tp1_hit"]        = True
                    pos["size_remaining"] = 0.0  # 全平
                    log.info(f"TP1 触发 {pos['symbol']}，一次全平（取消双 TP）")'''

if old_tp1_logic in content:
    content = content.replace(old_tp1_logic, new_tp1_logic)
    print("✅ TP1 逻辑已修改为一次全平")
else:
    print("⚠️ 未找到旧 TP1 逻辑，可能已修改")

# 3. 修改 TP2 触发逻辑 - 全平
old_tp2_logic = '''elif reason == "TP2":
                    # 平剩余 50%
                    pos["size_remaining"] = 0.0
                    log.info(f"TP2 触发 {pos['symbol']}，平仓剩余 50%")'''

new_tp2_logic = '''elif reason == "TP2":
                    # 2026-03-28 老公指示：取消双 TP，TP2 不再使用
                    # 但保留代码以防万一
                    pos["size_remaining"] = 0.0
                    log.info(f"TP2 触发 {pos['symbol']}，全平")'''

if old_tp2_logic in content:
    content = content.replace(old_tp2_logic, new_tp2_logic)
    print("✅ TP2 逻辑已更新")
else:
    print("⚠️ 未找到 TP2 逻辑")

with open("/root/.openclaw/workspace/scripts/crypto_signal_monitor.py", "w", encoding="utf-8") as f:
    f.write(content)

print("✅ 所有修改完成")
