#!/usr/bin/env python3
"""
修复 v6.0 WebSocket 代理问题 - 版本 2
直接替换 PriceStream 类的 start 方法
"""

with open('/root/.openclaw/workspace/scripts/crypto_signal_monitor.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# 找到并替换 PriceStream.start 方法
new_lines = []
in_start_method = False
skip_until_next_def = False

for i, line in enumerate(lines):
    # 检测是否进入 async def start(self):
    if 'async def start(self):' in line and 'PriceStream' in ''.join(lines[max(0,i-20):i]):
        in_start_method = True
        skip_until_next_def = True
        # 替换整个 start 方法
        new_lines.append('    async def start(self):\n')
        new_lines.append('        self._running = True\n')
        new_lines.append('        while self._running:\n')
        new_lines.append('            try:\n')
        new_lines.append('                # 使用 aiohttp 支持代理\n')
        new_lines.append('                connector = aiohttp.TCPConnector(ssl=False) if self.proxy else None\n')
        new_lines.append('                async with aiohttp.ClientSession(connector=connector) as session:\n')
        new_lines.append('                    async with session.ws_connect(\n')
        new_lines.append('                        self.url,\n')
        new_lines.append('                        proxy=self.proxy,\n')
        new_lines.append('                        ping_interval=20,\n')
        new_lines.append('                        heartbeat=30,\n')
        new_lines.append('                    ) as ws:\n')
        new_lines.append('                        log.info("WebSocket 价格流已连接 (aiohttp + 代理)")\n')
        new_lines.append('                        async for msg in ws:\n')
        new_lines.append('                            if msg.type == aiohttp.WSMsgType.TEXT:\n')
        new_lines.append('                                data = json.loads(msg.data)\n')
        new_lines.append('                                t = data.get("data", {})\n')
        new_lines.append('                                sym = t.get("s", "").replace("USDT", "")\n')
        new_lines.append('                                if sym in self.symbols:\n')
        new_lines.append('                                    self.prices[sym] = {\n')
        new_lines.append('                                        "price":      float(t["c"]),\n')
        new_lines.append('                                        "high_24h":   float(t["h"]),\n')
        new_lines.append('                                        "low_24h":    float(t["l"]),\n')
        new_lines.append('                                        "volume":     float(t["v"]),\n')
        new_lines.append('                                        "change_24h": float(t["P"]),\n')
        new_lines.append('                                        "ts":         time.time(),\n')
        new_lines.append('                                    }\n')
        new_lines.append('                            elif msg.type == aiohttp.WSMsgType.ERROR:\n')
        new_lines.append('                                log.warning(f"WebSocket 错误：{ws.exception()}")\n')
        new_lines.append('                                break\n')
        new_lines.append('            except Exception as e:\n')
        new_lines.append('                log.warning(f"WebSocket 断线，5 秒后重连：{e}")\n')
        new_lines.append('                await asyncio.sleep(5)\n')
        continue
    
    # 跳过旧方法的行
    if skip_until_next_def:
        if line.strip() and not line.startswith(' ') and not line.startswith('\t'):
            # 遇到非缩进的新行，说明方法结束
            skip_until_next_def = False
            new_lines.append(line)
        elif line.startswith('    def ') or line.startswith('    async def '):
            skip_until_next_def = False
            new_lines.append(line)
        # 否则跳过
        continue
    
    new_lines.append(line)

# 写回
with open('/root/.openclaw/workspace/scripts/crypto_signal_monitor.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print("✅ WebSocket 代理修复完成 (v2)！")
