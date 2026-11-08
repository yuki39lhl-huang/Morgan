"""平仓记账：只认交易所成交量和 realizedPnl。

请求数量、回执里的 origQty、下单瞬间的标记价都不能当成成交。
"""


def order_executed_qty(order: dict):
    """回执里已经写明的成交量。

    executedQty 为 0 且状态还是 NEW 时返回 None（未确认），不是 0 成交。
    绝不返回调用方的请求数量。
    """
    if not isinstance(order, dict) or not order.get("orderId"):
        return None
    status = str(order.get("status") or "").upper()
    raw = order.get("executedQty")
    if raw in (None, ""):
        raw = order.get("cumQty")
    try:
        executed = float(raw or 0)
    except (TypeError, ValueError):
        return None
    if executed > 0:
        return executed
    if status in ("CANCELED", "EXPIRED", "REJECTED"):
        return 0.0
    return None


def trades_for_order(trades, order_id) -> list:
    oid = str(order_id)
    return [t for t in trades or [] if isinstance(t, dict) and str(t.get("orderId")) == oid]


def fill_from_trades(trades) -> dict | None:
    """同一订单的 userTrades 汇总。没有成交行就返回 None。realized 可以为 0。"""
    rows = [t for t in trades or [] if isinstance(t, dict)]
    if not rows:
        return None
    qty = 0.0
    notion = 0.0
    realized = 0.0
    for t in rows:
        q = abs(float(t.get("qty") or 0))
        px = float(t.get("price") or 0)
        qty += q
        notion += q * px
        realized += float(t.get("realizedPnl") or 0)
    if qty <= 0:
        return None
    return {
        "filled_qty": qty,
        "avg_price": notion / qty,
        "realized_pnl": realized,
    }


def combine_fills(fills: list) -> dict | None:
    rows = [f for f in fills or [] if f and float(f.get("filled_qty") or 0) > 0]
    if not rows:
        return None
    qty = sum(float(f["filled_qty"]) for f in rows)
    realized = sum(float(f["realized_pnl"]) for f in rows)
    notion = sum(float(f["filled_qty"]) * float(f.get("avg_price") or 0) for f in rows)
    return {
        "filled_qty": qty,
        "avg_price": (notion / qty) if qty else 0.0,
        "realized_pnl": realized,
    }


def remaining_after_fill(position_qty: float, filled_qty: float) -> tuple[float, bool]:
    """返回 (剩余数量, 是否已经平完)。剩余按持仓量扣成交量，不按请求量。"""
    start = abs(float(position_qty))
    filled = abs(float(filled_qty))
    remaining = start - filled
    dust = max(1e-8, start * 1e-4)
    if remaining <= dust:
        return 0.0, True
    return remaining, False


def book_realized(trades) -> float:
    """账本已实现 = 成交回报里的 realizedPnl 之和。"""
    total = 0.0
    for t in trades or []:
        if isinstance(t, dict):
            total += float(t.get("realizedPnl") or 0)
    return total


def income_realized(rows) -> float:
    total = 0.0
    for r in rows or []:
        if isinstance(r, dict):
            total += float(r.get("income") or 0)
    return total
