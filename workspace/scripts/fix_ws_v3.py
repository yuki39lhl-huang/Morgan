#!/usr/bin/env python3
"""
修复 v6.0 WebSocket 代理问题 - 版本 3
更新 PriceStream.__init__ 添加 proxy 参数
"""

with open('/root/.openclaw/workspace/scripts/crypto_signal_monitor.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 替换 __init__ 方法
old_init = '''    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        streams = "/".join([f"{s.lower()}usdt@ticker" for s in symbols])
        self.url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        self.prices: dict = {}
        self._running = False'''

new_init = '''    def __init__(self, symbols: list[str], proxy: str = None):
        self.symbols = symbols
        streams = "/".join([f"{s.lower()}usdt@ticker" for s in symbols])
        self.url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        self.proxy = proxy
        self.prices: dict = {}
        self._running = False'''

content = content.replace(old_init, new_init)

# 也更新文档字符串
old_doc = '''    """
    Binance WebSocket 合并流，所有币种一个连接。
    每次收到推送立刻更新缓存，主循环直接读缓存，0 REST 请求。
    """'''

new_doc = '''    """
    Binance WebSocket 合并流，所有币种一个连接。
    使用 aiohttp 支持 HTTP 代理。
    每次收到推送立刻更新缓存，主循环直接读缓存，0 REST 请求。
    """'''

content = content.replace(old_doc, new_doc)

with open('/root/.openclaw/workspace/scripts/crypto_signal_monitor.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("✅ PriceStream.__init__ 修复完成！")
