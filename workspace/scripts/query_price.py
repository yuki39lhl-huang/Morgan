#!/usr/bin/env python3
"""
query_price.py - 查询实时价格（直接读 crypto_signal_monitor 的状态文件）

用法：
    python3 query_price.py                # 查询所有 7 个币种 → stdout
    python3 query_price.py BTC            # 查询单个 → stdout
    python3 query_price.py BTC ETH        # 查询多个 → stdout
    python3 query_price.py --push         # 所有币种推送卡片到飞书群
    python3 query_price.py BTC --push     # 单币种推送卡片到飞书群
"""
import json
import sys
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).parent
STATE_FILE = SCRIPT_DIR / "crypto_state.json"
STALE_SECONDS = 60

EMOJI = {
    "BTC": "👑", "ETH": "💎", "SOL": "⚡",
    "BNB": "🔶", "DOT": "🌐", "LINK": "🔗", "XRP": "🪙",
}


def load_state():
    if not STATE_FILE.exists():
        print("[ERROR] crypto_state.json 不存在 —— 监控未启动")
        sys.exit(2)
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[ERROR] 读取失败: {e}")
        sys.exit(3)


def calc_age(state):
    try:
        ts = datetime.fromisoformat(state.get("last_update", ""))
        return (datetime.now() - ts).total_seconds()
    except Exception:
        return 99999


def main():
    args = sys.argv[1:]
    push_card_mode = "--push" in args
    if push_card_mode:
        args.remove("--push")

    state = load_state()
    age = calc_age(state)
    prices = state.get("prices", {})

    if age > STALE_SECONDS:
        print(f"[STALE] 数据已过期 {int(age)} 秒（最后更新: {state.get('last_update')}）")
        print("[ACTION] 执行 `kj` 重启监控系统")
        sys.exit(1)

    requested = [s.upper() for s in args] or list(prices.keys())

    if push_card_mode:
        from feishu_helper import push_card, md, hr, note, kv_block
        # 找到的币种和缺失的币种
        found = [(sym, prices.get(sym)) for sym in requested if prices.get(sym) is not None]
        missing = [sym for sym in requested if prices.get(sym) is None]
        
        elements = []
        if not found:
            elements.append(md("⚠️ **未找到任何匹配币种**"))
        else:
            # 用 kv_block 两列网格布局，最美观
            items = []
            for sym, px in found:
                emo = EMOJI.get(sym, "🟣")
                items.append((f"{emo} {sym}", f"`${px:,g}`"))
            # kv_block 一次最多放偶数个比较好看，不够补"—"
            if len(items) % 2 == 1:
                items.append(("", ""))
            elements.append(kv_block(items))
        
        if missing:
            elements.append(hr())
            elements.append(md(
                f"<font color='grey'>未在监控列表：{', '.join(missing)}</font>"
            ))
        
        elements.append(note(
            f"📡 Binance WSS · 更新于 {int(age)} 秒前 · "
            f"{datetime.now().strftime('%H:%M:%S')}"
        ))
        ok = push_card("👑 实时行情", elements, template="blue")
        print("[PUSH] OK" if ok else "[PUSH] FAILED")
        return

    print(f"[OK] 实时价格 (更新于 {int(age)} 秒前)")
    for sym in requested:
        px = prices.get(sym)
        if px is None:
            print(f"  {sym}: 不在监控列表")
        else:
            print(f"  {EMOJI.get(sym, ' ')} {sym}: {px} USDT")


if __name__ == "__main__":
    main()
