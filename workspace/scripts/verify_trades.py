#!/usr/bin/env python3
"""
verify_trades.py - 验证「飞书播报」与「币安实际成交」是否一致

用法：
    python3 verify_trades.py              # 列出最近 30 分钟的实际成交
    python3 verify_trades.py 60           # 最近 60 分钟
    python3 verify_trades.py 60 --push    # 推送到飞书

用途：
    解决疑虑「飞书播报了开仓但测试盘里没看到」—— 直接拉 Binance 用户成交记录，
    跟 monitor 日志里的开仓/平仓动作对照，确认是否真的下单了。
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from openclaw_logging import trading_log_path

MONITOR_LOG = trading_log_path()

SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "DOT", "LINK", "XRP"]


def fetch_user_trades(symbol: str, since_ms: int) -> list[dict]:
    """调用 Binance Testnet /fapi/v1/userTrades"""
    try:
        import binance_auto_trade as bat
    except Exception as e:
        print(f"[ERR] 无法导入 binance_auto_trade: {e}", file=sys.stderr)
        return []
    params = {"symbol": f"{symbol}USDT", "startTime": since_ms, "limit": 200}
    try:
        # 复用 binance_auto_trade.request 的签名 + 代理逻辑
        result = bat.request("GET", "/fapi/v1/userTrades", params=params)
        if isinstance(result, list):
            return result
        return []
    except Exception as e:
        print(f"[ERR] {symbol} userTrades 失败: {e}", file=sys.stderr)
        return []


def parse_monitor_actions(minutes: int) -> list[dict]:
    """从 monitor.log 末尾解析最近 N 分钟的开仓/平仓动作"""
    if not MONITOR_LOG.exists():
        return []
    actions = []
    try:
        size = MONITOR_LOG.stat().st_size
        with open(MONITOR_LOG, "rb") as f:
            f.seek(max(0, size - 524288))  # 读末尾 512KB
            tail = f.read().decode("utf-8", errors="ignore")
        lines = tail.splitlines()
        cutoff = time.time() - minutes * 60
        ts_pattern = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
        # 真实日志格式：🟢 开多 BTC @78114.30 | 评分:65 | 状态:trending | ✅ 下单成功
        action_pattern = re.compile(
            r"(🟢 开多|🔴 开空|✅ 平仓|❌ 平仓)\s+(\w+).*?@?([0-9]+\.?[0-9]*)"
        )
        for line in lines:
            m = ts_pattern.search(line)
            if not m:
                continue
            try:
                lts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                if lts.timestamp() < cutoff:
                    continue
            except Exception:
                continue
            am = action_pattern.search(line)
            if not am:
                continue
            ok = "✅" in line or "下单成功" in line
            actions.append({
                "ts": m.group(1),
                "action": am.group(1),
                "symbol": am.group(2),
                "price": float(am.group(3) or 0),
                "ok": ok,
                "raw": line[:200],
            })
    except Exception as e:
        print(f"[ERR] 解析 monitor.log 失败: {e}", file=sys.stderr)
    return actions


def fmt_trade(t: dict) -> str:
    side = "🟢 BUY" if t.get("side") == "BUY" else "🔴 SELL"
    qty = float(t.get("qty", 0))
    price = float(t.get("price", 0))
    pnl = float(t.get("realizedPnl", 0))
    sym = t.get("symbol", "")
    ts = datetime.fromtimestamp(t.get("time", 0) / 1000).strftime("%H:%M:%S")
    pnl_str = f"{pnl:+.2f}" if pnl != 0 else "—"
    return f"{ts} {sym:<10} {side} qty={qty:<10g} @ {price:<10g} pnl={pnl_str}"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    push = "--push" in sys.argv
    minutes = int(args[0]) if args else 30
    
    since_ms = int((time.time() - minutes * 60) * 1000)
    
    print(f"\n📋 验证最近 {minutes} 分钟交易记录 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    # === 1. Binance 实际成交 ===
    print("─" * 60)
    print(f"🏦 Binance Testnet 实际成交（按时间排序）")
    print("─" * 60)
    all_trades = []
    for sym in SYMBOLS:
        trades = fetch_user_trades(sym, since_ms)
        all_trades.extend(trades)
    all_trades.sort(key=lambda x: x.get("time", 0))
    
    if not all_trades:
        print("  📭 无实际成交")
    else:
        for t in all_trades:
            print(f"  {fmt_trade(t)}")
    print(f"\n  共 {len(all_trades)} 笔实际成交\n")
    
    # === 2. monitor.log 中的开仓/平仓动作 ===
    print("─" * 60)
    print(f"📝 monitor.log 中的策略动作")
    print("─" * 60)
    actions = parse_monitor_actions(minutes)
    if not actions:
        print("  📭 无策略动作")
    else:
        for a in actions:
            ok_str = "✅" if a["ok"] else "❌"
            print(f"  {a['ts'].split(' ')[1]} {a['action']:<8} {a['symbol']:<6} @ {a['price']:<10g} {ok_str}")
    print(f"\n  共 {len(actions)} 个动作\n")
    
    # === 3. 一致性检查 ===
    print("─" * 60)
    print(f"🔍 一致性分析")
    print("─" * 60)
    
    mon_count = sum(1 for a in actions if a["ok"])
    bin_count = len(all_trades)
    
    if mon_count == 0 and bin_count == 0:
        verdict = "✅ 一致：本周期无任何交易"
    elif mon_count == bin_count:
        verdict = f"✅ 一致：日志 {mon_count} 个动作 ↔ 币安 {bin_count} 笔成交"
    elif bin_count > mon_count:
        verdict = f"⚠️ 币安成交({bin_count}) > 日志动作({mon_count})：可能有手动交易或 SL/TP 自动触发"
    else:
        verdict = f"🔴 日志说成功({mon_count}) > 币安实际({bin_count})：怀疑 monitor 误判下单成功！"
    print(f"  {verdict}\n")
    
    # === 4. 推送飞书卡片 ===
    if push:
        from feishu_helper import push_card, md, hr, note
        rows = ["**时间**|**币种**|**方向**|**数量**|**价格**|**盈亏**", ":---|:---|:---:|---:|---:|---:"]
        for t in all_trades[-15:]:
            ts = datetime.fromtimestamp(t.get("time", 0) / 1000).strftime("%H:%M:%S")
            sym = t.get("symbol", "").replace("USDT", "")
            side = "🟢 买" if t.get("side") == "BUY" else "🔴 卖"
            qty = f"{float(t.get('qty', 0)):g}"
            price = f"{float(t.get('price', 0)):g}"
            pnl = float(t.get("realizedPnl", 0))
            pnl_str = f"`{pnl:+.2f}`" if pnl else "—"
            rows.append(f"`{ts}`|**{sym}**|{side}|{qty}|{price}|{pnl_str}")
        elements = [
            md(f"**📊 最近 {minutes} 分钟实际成交**\n\n" + "\n".join(rows) if all_trades
               else f"**📭 最近 {minutes} 分钟无实际成交**"),
            hr(),
            md(f"**🔍 一致性：** {verdict}"),
            md(f"**📝 monitor 日志动作：** {len(actions)} 个 / **🏦 币安实际成交：** {len(all_trades)} 笔"),
            note(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC+8) · 数据源：Binance Testnet API"),
        ]
        ok = push_card(f"🔍 交易验证 · 最近{minutes}分钟", elements, template="blue")
        print("[PUSH] OK" if ok else "[PUSH] FAILED")


if __name__ == "__main__":
    main()
