#!/usr/bin/env python3
"""
manual_trade.py - 手动交易工具（测试 + 应急）

⚠️ 全部走 Binance Testnet，不会动你的实盘账户

用法：
    python3 manual_trade.py status
        显示账户余额 + 当前所有持仓

    python3 manual_trade.py open <币种> <方向> <USDT金额> [杠杆]
        开仓。例: python3 manual_trade.py open BTC SHORT 10 10
        默认杠杆 10x，金额单位 USDT（保证金，实际仓位 = 金额 × 杠杆）
        必须加 --confirm 才会真的下单

    python3 manual_trade.py close <币种>
        市价平掉该币种所有持仓
        必须加 --confirm 才会真的下单

    python3 manual_trade.py close-all
        市价平掉所有持仓（紧急用）
        必须加 --confirm 才会真的下单

    python3 manual_trade.py cancel <币种>
        撤销该币种所有挂单（止盈/止损单等）

示例（带确认）：
    python3 manual_trade.py open BTC LONG 10 10 --confirm
    python3 manual_trade.py close BTC --confirm
"""
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))


def need_confirm(args: list) -> bool:
    return "--confirm" in args


def get_price(symbol: str) -> float:
    """从 crypto_state.json 拿当前价"""
    import json
    state_file = SCRIPT_DIR / "crypto_state.json"
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            return float(data.get("prices", {}).get(symbol, 0) or 0)
        except Exception:
            pass
    return 0


def cmd_status():
    import binance_auto_trade as bat
    balance = bat.get_account_balance()
    positions = bat.get_all_positions()
    print(f"💰 账户可用余额: {balance:.2f} USDT")
    print(f"📊 当前持仓: {len(positions)} 个\n")
    if not positions:
        print("  (无持仓)")
        return
    for p in positions:
        sym = p["symbol"]
        typ = p["type"]
        amt = p["amount"]
        entry = p["entry_price"]
        current = p.get("current_price", 0) or p.get("mark_price", 0)
        pnl = p.get("unrealized_pnl", 0)
        print(f"  {sym} {typ:5s} 数量={amt:<10.4g} 入场=${entry:<10.4g} 现价=${current:<10.4g} 浮盈=${pnl:+.2f}")


def cmd_open(args: list):
    if len(args) < 3:
        print("[ERROR] 用法: open <币种> <方向> <USDT金额> [杠杆]")
        sys.exit(1)
    symbol = args[0].upper()
    direction = args[1].upper()
    if direction not in ("LONG", "SHORT", "BUY", "SELL"):
        print(f"[ERROR] 方向必须是 LONG / SHORT，给的是: {direction}")
        sys.exit(1)
    side = "BUY" if direction in ("LONG", "BUY") else "SELL"
    try:
        margin_usdt = float(args[2])
    except Exception:
        print(f"[ERROR] 金额必须是数字: {args[2]}")
        sys.exit(1)
    leverage = int(args[3]) if len(args) > 3 and args[3].isdigit() else 10

    price = get_price(symbol)
    if price <= 0:
        print(f"[ERROR] 拿不到 {symbol} 当前价（crypto_state.json 不可用）")
        sys.exit(1)

    notional = margin_usdt * leverage  # 名义价值
    quantity = notional / price

    print("─────────── 开仓预览 ───────────")
    print(f"  币种: {symbol}USDT")
    print(f"  方向: {direction} ({side})")
    print(f"  保证金: {margin_usdt:.2f} USDT")
    print(f"  杠杆: {leverage}x")
    print(f"  名义价值: {notional:.2f} USDT")
    print(f"  当前价: ${price}")
    print(f"  预计数量: {quantity:.6g}")
    print("───────────────────────────────")

    if not need_confirm(args):
        print("⚠️ 这是预览（DRY RUN），未真实下单")
        print("✅ 确认无误请加 --confirm 重新执行")
        return

    import binance_auto_trade as bat
    from crypto_signal_monitor import calc_tp_sl
    tp_sl = calc_tp_sl(price, direction, {}, {})
    tp_price = tp_sl["tp1_price"]
    sl_price = tp_sl["sl_price"]
    print(f"  止损(SL): ${sl_price:.2f} ({'+-'[direction=='SHORT']}{abs((sl_price-price)/price)*100:.0f}%)")
    print(f"  止盈(TP): ${tp_price:.2f} ({'+-'[direction=='SHORT']}{abs((tp_price-price)/price)*100:.0f}%)")
    print("\n🚀 真实下单中...")
    result = bat.place_order(
        symbol=f"{symbol}USDT",
        side=side,
        quantity=quantity,
        leverage=leverage,
        price=price,
        tp_price=tp_price,
        sl_price=sl_price,
        reduce_only=False,
    )
    if result.get("success"):
        print(f"✅ 开仓成功: {result.get('order')}")
    else:
        print(f"❌ 开仓失败: {result.get('message')}")
        print(f"   原始返回: {result.get('order')}")


def cmd_close(args: list):
    if not args:
        print("[ERROR] 用法: close <币种>")
        sys.exit(1)
    symbol = args[0].upper()
    import binance_auto_trade as bat
    pos = bat.get_position(f"{symbol}USDT")
    if not pos or pos["amount"] == 0:
        print(f"📭 {symbol} 当前无持仓")
        return
    qty = abs(pos["amount"])
    side = "SELL" if pos["amount"] > 0 else "BUY"
    print("─────────── 平仓预览 ───────────")
    print(f"  币种: {symbol}USDT")
    print(f"  方向: {'平多 (SELL)' if side == 'SELL' else '平空 (BUY)'}")
    print(f"  数量: {qty}")
    print(f"  入场价: ${pos['entry_price']}")
    print(f"  浮动盈亏: {pos['unrealized_pnl']:+.2f} USDT")
    print("───────────────────────────────")

    if not need_confirm(args):
        print("⚠️ 这是预览（DRY RUN），未真实下单")
        print("✅ 确认无误请加 --confirm 重新执行")
        return

    print("\n🚀 真实平仓中...")
    price = get_price(symbol)
    result = bat.place_order(
        symbol=f"{symbol}USDT",
        side=side,
        quantity=qty,
        price=price,
        reduce_only=False,
    )
    if result.get("success"):
        print(f"✅ 平仓成功")
        # 撤掉残余的 SL/TP 挂单
        bat.cancel_all_orders(f"{symbol}USDT")
        print(f"✅ {symbol} 残余 SL/TP 挂单已清理")
    else:
        print(f"❌ 平仓失败: {result.get('message')}")


def cmd_close_all(args: list):
    import binance_auto_trade as bat
    positions = bat.get_all_positions()
    if not positions:
        print("📭 当前无持仓")
        return
    print(f"⚠️ 即将平掉 {len(positions)} 个持仓:")
    for p in positions:
        print(f"  - {p['symbol']} {p['type']} 数量={p['amount']} 浮盈={p['unrealized_pnl']:+.2f}")
    if not need_confirm(args):
        print("\n⚠️ 这是预览（DRY RUN），未真实下单")
        print("✅ 确认全部平仓请加 --confirm 重新执行")
        return
    print("\n🚀 全部平仓中...")
    bat.close_all_positions()
    print("✅ 已发送全部平仓请求，等几秒后用 status 验证")


def cmd_cancel(args: list):
    if not args:
        print("[ERROR] 用法: cancel <币种>")
        sys.exit(1)
    symbol = args[0].upper()
    import binance_auto_trade as bat
    bat.cancel_all_orders(f"{symbol}USDT")
    print(f"✅ {symbol} 所有挂单已撤销")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1]
    args = sys.argv[2:]
    if cmd == "status":
        cmd_status()
    elif cmd == "open":
        cmd_open(args)
    elif cmd == "close":
        cmd_close(args)
    elif cmd == "close-all":
        cmd_close_all(args)
    elif cmd == "cancel":
        cmd_cancel(args)
    else:
        print(f"[ERROR] 未知命令: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
