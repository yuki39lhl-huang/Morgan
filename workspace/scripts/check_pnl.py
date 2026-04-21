#!/usr/bin/env python3
"""
检查今日盈亏（可靠版本）
- 从日志解析 circuit_breaker 的实时累加值
- 使用字符串分割避免正则问题
"""

import re
from pathlib import Path

LOG_FILE = Path("/root/.openclaw/workspace/scripts/crypto_monitor.log")

def parse_daily_pnl_from_log():
    """从日志解析 circuit_breaker 的每日盈亏（最可靠）"""
    try:
        with open(LOG_FILE, 'rb') as f:
            content = f.read()
        
        text = content.decode('utf-8')
        lines = text.split('\n')
        
        # 找最后一行包含"今日"和"PnL"的行
        last_pnl = None
        for line in lines:
            if '今日' in line and 'PnL' in line:
                # 提取数字
                match = re.search(r'([+-]?\d+\.\d+)\s*USDT', line)
                if match:
                    last_pnl = float(match.group(1))
        
        return last_pnl
            
    except Exception as e:
        print(f"❌ 解析失败：{e}")
        return None

if __name__ == "__main__":
    print("=== 今日盈亏统计（可靠版）===\n")
    
    pnl = parse_daily_pnl_from_log()
    if pnl is not None:
        print(f"📊 今日盈亏：{pnl:+.2f} USDT")
        print(f"💡 数据来源：circuit_breaker 实时累加（整点汇报）")
        print(f"💰 估算余额：{5000 + pnl:.2f} USDT (初始 5000U)")
        print(f"📉 总亏损：{-pnl:.2f} USDT ({-pnl/5000*100:.2f}%)")
    else:
        print("❌ 无法获取盈亏数据")
        print("💡 建议：查看最新的整点汇报消息")
