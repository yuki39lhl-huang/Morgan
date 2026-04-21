#!/usr/bin/env python3
"""
修复 v6.0 WebSocket 代理问题 - 版本 4
修复 aiohttp.ws_connect 参数问题
"""

with open('/root/.openclaw/workspace/scripts/crypto_signal_monitor.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 修复 ws_connect 参数
old_ws = '''                    async with session.ws_connect(
                        self.url,
                        proxy=self.proxy,
                        ping_interval=20,
                        heartbeat=30,
                    ) as ws:'''

new_ws = '''                    async with session.ws_connect(
                        self.url,
                        proxy=self.proxy,
                        heartbeat=20,  # aiohttp 使用 heartbeat 代替 ping_interval
                    ) as ws:'''

content = content.replace(old_ws, new_ws)

with open('/root/.openclaw/workspace/scripts/crypto_signal_monitor.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("✅ aiohttp.ws_connect 参数修复完成！")
