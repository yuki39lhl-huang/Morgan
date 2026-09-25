#!/usr/bin/env python3
"""
query_positions.py - 查询当前实时持仓 + 浮动盈亏

数据源（按优先级）：
    1. binance_auto_trade.get_all_positions()  (复用主交易模块的 API 调用，签名 100% 一致)
    2. crypto_state.json                       (今日已实现盈亏)
    3. crypto_positions.json                   (本地账本，仅用于附加 SL/TP 信息)

用法：
    python3 query_positions.py            # stdout
    python3 query_positions.py --push     # 推送卡片到飞书群
"""
import json
import sys
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).parent
POSITIONS_FILE = SCRIPT_DIR / "crypto_positions.json"
STATE_FILE = SCRIPT_DIR / "crypto_state.json"

sys.path.insert(0, str(SCRIPT_DIR))

EMOJI = {
    "BTC": "👑", "ETH": "💎", "SOL": "⚡",
    "BNB": "🔶", "DOT": "🌐", "LINK": "🔗", "XRP": "🪙",
}


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def fetch_real_positions() -> list[dict]:
    """复用 binance_auto_trade.get_all_positions() —— 签名/配置/代理与主交易模块完全一致"""
    try:
        import binance_auto_trade as bat
    except Exception as e:
        print(f"[ERROR] 无法导入 binance_auto_trade: {e}", file=sys.stderr)
        return []
    try:
        return bat.as_position_list(bat.get_all_positions())
    except Exception as e:
        print(f"[ERROR] 调用 get_all_positions 失败: {e}", file=sys.stderr)
        return []


def calc_pnl_pct(pos: dict) -> float:
    entry = pos.get("entry_price", 0)
    current = pos.get("current_price", 0) or pos.get("mark_price", 0)
    if entry <= 0 or current <= 0:
        return 0.0
    if pos.get("type") == "LONG":
        return (current - entry) / entry * 100
    return (entry - current) / entry * 100


def find_local_meta(positions_local: list, symbol: str) -> dict:
    for p in positions_local:
        if p.get("symbol") == symbol:
            return p
    return {}


def fetch_open_algo_orders() -> list[dict]:
    """
    从 Binance API 拉取所有活跃的 Algo 委托（止盈/止损单）。
    复用 binance_auto_trade.request() 保证签名一致。
    """
    try:
        import binance_auto_trade as bat
    except Exception as e:
        print(f"[ERROR] 无法导入 binance_auto_trade: {e}", file=sys.stderr)
        return []
    try:
        raw = bat.request("GET", "/fapi/v1/openAlgoOrders")
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict) and raw.get("error"):
            print(f"[WARN] openAlgoOrders 查询失败: {raw.get('error')}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"[ERROR] fetch_open_algo_orders: {e}", file=sys.stderr)
        return []


def calc_tp_sl_info(pos: dict, algo_orders: list[dict]) -> dict:
    """
    为某个持仓匹配止盈/止损 Algo 委托，计算盈亏 U 数和百分比。
    返回 {"tp": {price, pnl, pct}, "sl": {price, pnl, pct}}
    """
    sym = pos.get("symbol", "")
    entry = pos.get("entry_price", 0)
    amount = abs(float(pos.get("amount", 0) or 0))
    is_long = pos.get("type") == "LONG"
    current = pos.get("current_price", 0) or pos.get("mark_price", 0)

    tp_data, sl_data = None, None

    for o in algo_orders:
        if o.get("symbol") != sym:
            continue
        otype = o.get("orderType", "")
        trigger = float(o.get("triggerPrice", 0) or 0)
        if trigger <= 0:
            continue

        if "TAKE_PROFIT" in otype:
            if is_long:
                pnl = (trigger - entry) * amount
            else:
                pnl = (entry - trigger) * amount
            tp_data = {
                "price": trigger,
                "pnl": pnl,
                "pct": (pnl / (entry * amount * 0.1)) * 100 if entry and amount else 0,
            }
        elif "STOP" in otype:
            if is_long:
                pnl = (trigger - entry) * amount
            else:
                pnl = (entry - trigger) * amount
            sl_data = {
                "price": trigger,
                "pnl": pnl,
                "pct": (pnl / (entry * amount * 0.1)) * 100 if entry and amount else 0,
            }

    return {"tp": tp_data, "sl": sl_data}


def main():
    args = sys.argv[1:]
    push_card_mode = "--push" in args

    state = load_json(STATE_FILE, {})
    daily_pnl = state.get("daily_pnl", 0.0)
    positions_local = load_json(POSITIONS_FILE, [])

    positions = fetch_real_positions()
    stale_mode = False
    if not positions and positions_local:
        # Binance API 短时抖动时，用本地账本兜底，避免“查不到持仓详情”
        stale_mode = True
        positions = positions_local

    if not positions:
        msg = "📭 当前无持仓（已查询 Binance Testnet API）"
        if push_card_mode:
            from feishu_helper import push_card, md, note
            push_card(
                "👑 当前持仓",
                [
                    md(f"### 📭 暂无持仓\n\n_系统正在持续监控信号，符合条件会自动开仓_"),
                    md(
                        f"<font color='grey'>**今日已实现：**</font> "
                        f"{('🟢' if daily_pnl >= 0 else '🔴')} `{daily_pnl:+.2f}` USDT"
                    ),
                    note(
                        f"📡 Binance Testnet · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    ),
                ],
                template="grey",
            )
            print("[PUSH] OK")
        else:
            print(msg)
            print(f"今日已实现盈亏: {daily_pnl:+.2f} USDT")
        return

    algo_orders = fetch_open_algo_orders() if not stale_mode else []

    rows_stdout = []
    pos_blocks = []
    total_floating = 0.0

    for pos in positions:
        sym = pos.get("symbol", "?")
        typ = pos.get("type", "?")
        entry = pos.get("entry_price", 0)
        current = pos.get("current_price", 0) or pos.get("mark_price", 0)
        pnl_usdt = pos.get("unrealized_pnl", 0)
        pnl_pct = calc_pnl_pct(pos)
        amount = abs(float(pos.get("amount", 0) or 0))
        total_floating += pnl_usdt
        emoji = EMOJI.get(sym, "🟣")
        sign = "🟢" if pnl_usdt >= 0 else "🔴"
        dir_emoji = "📈" if typ == "LONG" else "📉"
        dir_color = "green" if typ == "LONG" else "red"

        # 优先用 Algo 委托数据，本地账本做兜底
        algo_info = calc_tp_sl_info(pos, algo_orders) if algo_orders else {}
        tp_data = algo_info.get("tp")
        sl_data = algo_info.get("sl")

        # 兜底：Algo 没数据时用本地缓存
        if not tp_data:
            local = find_local_meta(positions_local, sym)
            tp1_p = local.get("tp1_price")
            if tp1_p:
                if typ == "LONG":
                    tp_pnl = (tp1_p - entry) * amount
                else:
                    tp_pnl = (entry - tp1_p) * amount
                tp_data = {"price": tp1_p, "pnl": tp_pnl, "pct": 0}
        if not sl_data:
            local = find_local_meta(positions_local, sym)
            sl_p = local.get("sl_price")
            if sl_p:
                if typ == "LONG":
                    sl_pnl = (sl_p - entry) * amount
                else:
                    sl_pnl = (entry - sl_p) * amount
                sl_data = {"price": sl_p, "pnl": sl_pnl, "pct": 0}

        # stdout 行：止盈止损 + 盈亏 U 数
        tp_str = ""
        if tp_data:
            tp_str = f" 🎯止盈:{tp_data['price']:.4g}({tp_data['pnl']:+.2f}U)"
        sl_str = ""
        if sl_data:
            sl_str = f" 🛑止损:{sl_data['price']:.4g}({sl_data['pnl']:+.2f}U)"

        rows_stdout.append(
            f"  {emoji} {sym} {typ:5s}  入${entry:<10.4g} 现${current:<10.4g} "
            f"{sign} {pnl_usdt:+.2f}USDT ({pnl_pct:+.2f}%)"
            f"{tp_str}{sl_str}"
        )

        # 飞书卡片块（每个持仓一个独立美观块）
        title_line = (
            f"### {emoji} **{sym}**  "
            f"<font color='{dir_color}'>{dir_emoji} {typ}</font>  "
            f"·  数量 `{amount:g}`"
        )
        price_line = (
            f"<font color='grey'>开仓</font> `${entry:.4g}` "
            f"&nbsp;&nbsp;➜&nbsp;&nbsp; "
            f"<font color='grey'>现价</font> `${current:.4g}`"
        )
        pnl_color = "green" if pnl_usdt >= 0 else "red"
        pnl_line = (
            f"{sign} <font color='{pnl_color}'>**{pnl_usdt:+.4f} USDT**</font> "
            f"({pnl_pct:+.2f}%)"
        )

        block_md = title_line + "\n" + price_line + "\n" + pnl_line

        if tp_data or sl_data:
            if tp_data:
                tp_line = (
                    f"🎯 <font color='grey'>止盈</font> "
                    f"`${tp_data['price']:.4g}` "
                    f"<font color='green'>{tp_data['pnl']:+.2f} U</font>"
                )
            else:
                tp_line = "🎯 <font color='grey'>止盈</font> —"

            if sl_data:
                sl_line = (
                    f"🛑 <font color='grey'>止损</font> "
                    f"`${sl_data['price']:.4g}` "
                    f"<font color='red'>{sl_data['pnl']:+.2f} U</font>"
                )
            else:
                sl_line = "🛑 <font color='grey'>止损</font> —"

            block_md += f"\n{tp_line} &nbsp;·&nbsp; {sl_line}"

        pos_blocks.append(block_md)

    net = total_floating + daily_pnl
    summary_emoji = "🎉" if net > 0 else ("⚠️" if net < 0 else "⚖️")

    if push_card_mode:
        from feishu_helper import push_card, md, hr, note, kv_block

        elements = []
        for i, block in enumerate(pos_blocks):
            elements.append(md(block))
            if i < len(pos_blocks) - 1:
                elements.append(hr())

        elements.append(hr())

        # 用 fields 网格做汇总（左右两栏对齐）
        floating_color = "green" if total_floating >= 0 else "red"
        daily_color = "green" if daily_pnl >= 0 else "red"
        net_color = "green" if net >= 0 else "red"

        elements.append(kv_block([
            ("💧 浮动盈亏",
             f"<font color='{floating_color}'>**{total_floating:+.2f}** USDT</font>"),
            ("📅 今日已实现",
             f"<font color='{daily_color}'>**{daily_pnl:+.2f}** USDT</font>"),
        ]))
        elements.append(md(
            f"<font color='grey'>━━━━━━━━━━━━━━</font>\n"
            f"### {summary_emoji} 净盈亏 "
            f"<font color='{net_color}'>**{net:+.2f}** USDT</font>"
        ))
        elements.append(note(
            f"📡 Binance Testnet · 持仓 {len(positions)} 个 · "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        ))
        if stale_mode:
            elements.append(note("⚠️ API 暂时波动，本次为本地缓存快照（非实时）"))

        template = "green" if net >= 0 else ("red" if net < -2 else "orange")
        ok = push_card("👑 当前持仓", elements, template=template)
        print("[PUSH] OK" if ok else "[PUSH] FAILED")
        return

    if stale_mode:
        print(f"[WARN] 当前持仓 {len(positions)} 个 (本地缓存快照，API 暂时不可用)")
    else:
        print(f"[OK] 当前持仓 {len(positions)} 个 (Binance Testnet 实时)")
    for line in rows_stdout:
        print(line)
    print(f"\n  浮动盈亏: {total_floating:+.2f} USDT")
    print(f"  今日已实现: {daily_pnl:+.2f} USDT")
    print(f"  {summary_emoji} 净盈亏: {net:+.2f} USDT")


if __name__ == "__main__":
    main()
