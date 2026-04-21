#!/usr/bin/env python3
"""
清理 TP1 逻辑残留代码
"""

with open("/root/.openclaw/workspace/scripts/crypto_signal_monitor.py", "r", encoding="utf-8") as f:
    content = f.read()

# 删除残留的 TP2 动态下移逻辑
old_text = '''                if reason == "TP1":
                    # 2026-03-28 老公指示：取消双 TP，一次全平
                    pos["tp1_hit"] = True
                    pos["size_remaining"] = 0.0  # 全平
                    log.info(f"TP1 触发 {pos['symbol']}，一次全平（取消双 TP）")
                            else:  # SHORT
                                pos["tp2_price"] = p - atr * 2.0
                            log.info(f"TP1 触发 {sym}，TP2 动态下移至 ${pos['tp2_price']:.4f} (ATR×2.0)")
                    
                    # 更新日志
                    log.info(f"TP1 触发 {pos['symbol']}，止损移至成本，保留 50% 仓位，TP2 动态调整")
                else:'''

new_text = '''                if reason == "TP1":
                    # 2026-03-28 老公指示：取消双 TP，一次全平
                    pos["tp1_hit"] = True
                    pos["size_remaining"] = 0.0  # 全平
                    log.info(f"TP1 触发 {pos['symbol']}，一次全平（取消双 TP）")
                    positions.remove(pos)  # 直接移除持仓
                elif reason == "TP2":'''

if old_text in content:
    content = content.replace(old_text, new_text)
    print("✅ 残留代码已清理")
else:
    print("⚠️ 未找到残留代码，尝试其他方式")
    # 直接删除不需要的行
    lines = content.split('\n')
    new_lines = []
    skip_patterns = ['else:  # SHORT', 'pos["tp2_price"] = p', 'TP2 动态下移', '止损移至成本，保留 50%']
    for line in lines:
        if any(p in line for p in skip_patterns):
            continue
        new_lines.append(line)
    content = '\n'.join(new_lines)

with open("/root/.openclaw/workspace/scripts/crypto_signal_monitor.py", "w", encoding="utf-8") as f:
    f.write(content)

print("✅ 清理完成")
