"""Queue and manage confirmed signals in the single Morgan testnet monitor."""

from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from hashlib import sha256
import logging
import re
import time
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
STRATEGY = "news_trend"
SCAN_STRATEGY = "market_scan"
SCAN_HOLD_SECONDS = 3600
HOLD_SECONDS = 900
SCHEMA = """
CREATE TABLE IF NOT EXISTS news_testnet_orders (
    event_key TEXT PRIMARY KEY, stage TEXT NOT NULL, symbol TEXT NOT NULL,
    direction TEXT NOT NULL, ready_ms INTEGER NOT NULL, deadline_ms INTEGER NOT NULL,
    client_id TEXT NOT NULL UNIQUE, notional REAL NOT NULL, reference_price REAL NOT NULL,
    status TEXT NOT NULL, submitted_ms INTEGER, order_id INTEGER, opened_ms INTEGER,
    entry_price REAL, quantity REAL, close_client_id TEXT, close_ms INTEGER, close_attempt INTEGER NOT NULL DEFAULT 0,
    strategy TEXT NOT NULL DEFAULT 'news_trend', limit_price TEXT, protect_attempt_ms INTEGER,
    protected INTEGER NOT NULL DEFAULT 0
);
"""


def now_ms():
    return time.time_ns() // 1_000_000


def is_managed(pos):
    return pos.get("regime") in {STRATEGY, SCAN_STRATEGY}


def ensure_schema(db):
    db.executescript(SCHEMA)
    with db:
        db.execute("BEGIN IMMEDIATE")
        fields = {field[1] for field in db.execute("PRAGMA table_info(news_testnet_orders)")}
        for key, sql in {
            "strategy": "TEXT NOT NULL DEFAULT 'news_trend'",
            "limit_price": "TEXT",
            "protect_attempt_ms": "INTEGER",
            "protected": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if key not in fields:
                db.execute(f"ALTER TABLE news_testnet_orders ADD COLUMN {key} {sql}")


def testnet_enabled(config, base_url, strategy=STRATEGY):
    flag = "market_scan_testnet_enabled" if strategy == SCAN_STRATEGY else "news_testnet_enabled"
    return config.get(flag) is True and testnet_environment(config, base_url)


def testnet_environment(config, base_url):
    if config.get("binance_testnet") is not True or config.get("entry_strategy") != "rsi_fade":
        return False
    for value in (base_url, config.get("binance_auto_base"), config.get("binance_futures")):
        endpoint = urlsplit(value or "")
        if endpoint.scheme != "https" or endpoint.hostname != "testnet.binancefuture.com":
            return False
    return True


def enqueue(db_module, event, entry, deadline, now, *, strategy=STRATEGY):
    if event["stage"] != "confirmed" or not now < deadline or not re.fullmatch(r"[A-Z0-9]+USDT", event["symbol"]):
        return False
    prefix = "ms_" if strategy == SCAN_STRATEGY else "mn_"
    client_id = prefix + sha256(event["event_key"].encode()).hexdigest()[:24]
    with closing(db_module.connect()) as db, db:
        created = db.execute(
            """INSERT INTO news_testnet_orders
               (event_key,stage,symbol,direction,ready_ms,deadline_ms,client_id,notional,reference_price,status,strategy)
               VALUES (?,?,?,?,?,?,?,?,?,'queued',?) ON CONFLICT(event_key) DO NOTHING""",
            (event["event_key"], event["stage"], event["symbol"], entry["action"].upper(), now, deadline,
             client_id, entry["notional_usdt"], entry["reference"], strategy),
        ).rowcount
        if created:
            write_status(db, dict(event, strategy=strategy), "testnet_queued")
    return bool(created)


def write_status(db, row, status):
    if row.get("strategy") == SCAN_STRATEGY:
        db.execute("UPDATE market_scan_signals SET status=? WHERE event_key=?", (status, row["event_key"]))
    else:
        db.execute("UPDATE news_entry_decisions SET status=? WHERE event_key=? AND stage=?",
                   (status, row["event_key"], row["stage"]))


def transition(db_module, row, state, display, submitted_ms=None):
    with closing(db_module.connect()) as db, db:
        changed = db.execute(
            """UPDATE news_testnet_orders SET status=?,submitted_ms=COALESCE(?,submitted_ms),
               limit_price=COALESCE(?,limit_price) WHERE event_key=? AND status=?""",
            (state, submitted_ms, row.get("limit_price"), row["event_key"], row["status"]),
        ).rowcount
        if changed:
            write_status(db, row, display)
    return bool(changed)


def decimal(value):
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError("nonfinite")
    return value


def position_rows(request):
    raw = request("GET", "/fapi/v3/positionRisk")
    if not isinstance(raw, list):
        return None
    try:
        for position in raw:
            if position.get("positionSide") != "BOTH":
                return None
            decimal(position["positionAmt"])
        return [position for position in raw if decimal(position["positionAmt"])]
    except (InvalidOperation, ValueError, KeyError, TypeError):
        return None


def market_qty(catalog, row, *, limit=False):
    match = next((p for p in catalog.get("symbols", []) if p.get("symbol") == row["symbol"]), None)
    if not match or match.get("status") != "TRADING" or match.get("quoteAsset") != "USDT":
        return None
    filters = {f["filterType"]: f for f in match.get("filters", [])}
    try:
        lot = filters["LOT_SIZE" if limit else "MARKET_LOT_SIZE"]
        step = decimal(lot["stepSize"])
        price = decimal(row.get("limit_price") if limit and row["direction"] == "LONG" else row["reference_price"])
        qty = (decimal(row["notional"]) / price / step).to_integral_value(rounding=ROUND_DOWN) * step
        minimum = decimal(filters.get("MIN_NOTIONAL", {}).get("notional", 0))
        if qty <= 0 or not decimal(lot["minQty"]) <= qty <= decimal(lot["maxQty"]) or qty * min(price, decimal(row.get("limit_price") or price)) < minimum:
            return None
        return format(qty, "f")
    except (InvalidOperation, ArithmeticError, ValueError, KeyError, TypeError):
        return None


def round_price(catalog, symbol, raw_price, rounding):
    contract = next((p for p in catalog.get("symbols", []) if p.get("symbol") == symbol), {})
    try:
        rule = next(f for f in contract.get("filters", []) if f["filterType"] == "PRICE_FILTER")
        tick = decimal(rule["tickSize"])
        rounded = (raw_price / tick).to_integral_value(rounding=rounding) * tick
        if rounded <= 0 or not decimal(rule["minPrice"]) <= rounded <= decimal(rule["maxPrice"]):
            return None
        return format(rounded, "f")
    except (StopIteration, InvalidOperation, ArithmeticError, ValueError, KeyError, TypeError):
        return None


def bounded_ioc_price(catalog, row):
    # Use at most 15 bps past the fresh executable quote.
    long = row["direction"] == "LONG"
    ref = decimal(row["reference_price"])
    limit = ref * (Decimal("1.0015") if long else Decimal("0.9985"))
    price = round_price(catalog, row["symbol"], limit, ROUND_DOWN if long else ROUND_UP)
    if price and ((decimal(price) >= ref) if long else (decimal(price) <= ref)):
        return price
    return None


def confirmed_fill(row, order):
    if not isinstance(order, dict) or order.get("status") not in {"FILLED", "EXPIRED", "CANCELED", "EXPIRED_IN_MATCH"}:
        return None
    expected = "BUY" if row["direction"] == "LONG" else "SELL"
    if (order.get("symbol"), order.get("clientOrderId"), order.get("side"), order.get("positionSide")) != (
        row["symbol"], row["client_id"], expected, "BOTH",
    ):
        return None
    try:
        qty, price = decimal(order["executedQty"]), decimal(order["avgPrice"])
        stamp, order_id = int(order["updateTime"]), int(order["orderId"])
        if qty > 0 and price > 0 and stamp > 0 and order_id > 0:
            return float(qty), float(price), stamp, order_id
    except (InvalidOperation, ValueError, TypeError, KeyError):
        pass
    return None


def position_for(row):
    strategy = row.get("strategy", STRATEGY)
    return {"symbol": row["symbol"][:-4], "type": row["direction"], "trade_id": row["client_id"],
            "strategy": strategy, "regime": strategy, "entry_price": row["entry_price"],
            "entry_time": datetime.fromtimestamp(row["opened_ms"] / 1000).isoformat(),
            "amount": row["quantity"], "qty": row["quantity"], "tp1_price": 0.0, "sl_price": 0.0,
            "tp_pct": 0.0, "peak_pnl": 0.0, "size_remaining": 1.0}


def save_fill(db_module, row, fill):
    qty, price, stamp, order_id = fill
    filled = dict(row, opened_ms=stamp, entry_price=price, quantity=qty)
    pos = position_for(filled)
    with closing(db_module.connect()) as db, db:
        db.execute(
            """INSERT INTO trades (trade_id,ts,symbol,direction,entry_price,regime)
               VALUES (?,?,?,?,?,?) ON CONFLICT(trade_id) DO NOTHING""",
            (pos["trade_id"], pos["entry_time"], pos["symbol"], pos["type"], price, pos["regime"]),
        )
        db.execute(
            """UPDATE news_testnet_orders SET status='open',order_id=?,opened_ms=?,entry_price=?,quantity=?
               WHERE event_key=? AND status='submitting'""", (order_id, stamp, price, qty, row["event_key"]),
        )
        write_status(db, row, "testnet_open")
    return pos


def accept_order(db_module, row, order):
    fill = confirmed_fill(row, order)
    if fill:
        return save_fill(db_module, row, fill)
    if isinstance(order, dict) and order.get("status") in {"CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}:
        if order.get("clientOrderId") == row["client_id"] and str(order.get("executedQty", "")) in {"0", "0.0", "0.00"}:
            transition(db_module, row, "rejected", "testnet_rejected")
    return None


def submit(db_module, row, positions, config, request, catalog):
    if now_ms() >= row["deadline_ms"]:
        transition(db_module, row, "expired", "testnet_expired")
        return None
    base = row["symbol"][:-4]
    limits = config.get("max_positions_per_symbol") or {}
    if limits.get(base, 1) <= 0 or any(p["symbol"] == base for p in positions):
        transition(db_module, row, "rejected", "testnet_position_conflict")
        return None
    actual = position_rows(request)
    if actual is None:
        return None
    if any(p["symbol"] == row["symbol"] for p in actual):
        transition(db_module, row, "rejected", "testnet_position_conflict")
        return None
    if len({p["symbol"] for p in actual} | {p["symbol"] + "USDT" for p in positions}) >= int(config["max_positions"]):
        transition(db_module, row, "rejected", "testnet_capacity")
        return None
    scan = row.get("strategy") == SCAN_STRATEGY
    if scan:
        limit_price = bounded_ioc_price(catalog, row)
        if not limit_price:
            transition(db_module, row, "rejected", "testnet_quantity")
            return None
        row = dict(row, limit_price=limit_price)
    quantity = market_qty(catalog, row, limit=scan)
    if not quantity:
        transition(db_module, row, "rejected", "testnet_quantity")
        return None
    pair = row["symbol"]
    margin = request("POST", "/fapi/v1/marginType", {"symbol": pair, "marginType": "ISOLATED"})
    if not isinstance(margin, dict) or margin.get("code") not in {200, -4046}:
        transition(db_module, row, "rejected", "testnet_setup_failed")
        return None
    leverage = int(config["leverage"])
    response = request("POST", "/fapi/v1/leverage", {"symbol": pair, "leverage": leverage})
    if not isinstance(response, dict) or response.get("leverage") != leverage:
        transition(db_module, row, "rejected", "testnet_setup_failed")
        return None
    stamp = now_ms()
    if stamp >= row["deadline_ms"]:
        transition(db_module, row, "expired", "testnet_expired")
        return None
    if not transition(db_module, row, "submitting", "testnet_order_pending", stamp):
        return None
    row = dict(row, status="submitting", submitted_ms=stamp)
    order = {"symbol": pair, "side": "BUY" if row["direction"] == "LONG" else "SELL",
             "type": "MARKET", "quantity": quantity, "positionSide": "BOTH",
             "newClientOrderId": row["client_id"], "newOrderRespType": "RESULT"}
    if scan:
        order.update(type="LIMIT", timeInForce="IOC", price=row["limit_price"])
    result = request("POST", "/fapi/v1/order", order, retry_order=False, deadline_ms=row["deadline_ms"])
    # An unclear response is reconciled by client ID. A submission is never repeated.
    return accept_order(db_module, row, result)


def settle_close(db_module, row, request, circuit, order):
    if not isinstance(order, dict) or order.get("status") not in {"FILLED", "EXPIRED", "CANCELED", "REJECTED", "EXPIRED_IN_MATCH"}:
        return
    side = "SELL" if row["direction"] == "LONG" else "BUY"
    if (order.get("symbol"), order.get("clientOrderId"), order.get("side"), order.get("positionSide")) != (
        row["symbol"], row["close_client_id"], side, "BOTH",
    ):
        return
    qty = decimal(order.get("executedQty", 0))
    if qty:
        fills = request("GET", "/fapi/v1/userTrades", {"symbol": row["symbol"], "orderId": order["orderId"]})
        if not isinstance(fills, list) or not fills or any(
            f["orderId"] != order["orderId"] or f["side"] != side or f["symbol"] != row["symbol"]
            or f["positionSide"] != "BOTH" for f in fills
        ) or sum(decimal(f["qty"]) for f in fills) != qty:
            return
        pnl = db_module.insert_exchange_close(row["client_id"], datetime.now().isoformat(),
                                             "异动退出" if row.get("strategy") == SCAN_STRATEGY else "news_15m",
                                             fills, row["entry_price"], row["direction"])
        if pnl is not None:
            circuit.record_trade(pnl)
    account = position_rows(request)
    if account is None:
        return
    position = next((p for p in account if p["symbol"] == row["symbol"]), None)
    if position is None and db_module.has_primary_close(row["client_id"]):
        transition(db_module, row, "closed", "testnet_closed")
    elif position:
        transition(db_module, row, "open", "testnet_open")


def settle_external_flat(db_module, row, request, circuit):
    side = "SELL" if row["direction"] == "LONG" else "BUY"
    trades = request("GET", "/fapi/v1/userTrades", {
        "symbol": row["symbol"], "startTime": row["opened_ms"], "limit": 100,
    })
    if not isinstance(trades, list) or not trades or len(trades) == 100:
        return
    fills = [f for f in trades if f["side"] == side and f["symbol"] == row["symbol"]
             and f["positionSide"] == "BOTH"]
    if any(f["orderId"] != row["order_id"] and f["side"] != side for f in trades):
        return
    if not fills or sum(decimal(f["qty"]) for f in fills) != decimal(row["quantity"]):
        return
    pnl = db_module.insert_exchange_close(row["client_id"], datetime.now().isoformat(),
                                         "异动保护或外部平仓" if row.get("strategy") == SCAN_STRATEGY else "news_external_close",
                                         fills, row["entry_price"], row["direction"])
    if pnl is not None:
        circuit.record_trade(pnl)
    if db_module.has_primary_close(row["client_id"]):
        transition(db_module, row, "closed", "testnet_closed")


def close_due(db_module, row, request, circuit):
    if row["status"] == "closing":
        order = request("GET", "/fapi/v1/order", {"symbol": row["symbol"], "origClientOrderId": row["close_client_id"]})
        if isinstance(order, dict) and order.get("code") == -2013 and now_ms() > row["close_ms"] + 60_000:
            account = position_rows(request)
            if account is not None:
                if any(p["symbol"] == row["symbol"] for p in account):
                    transition(db_module, row, "open", "testnet_open")
                else:
                    settle_external_flat(db_module, row, request, circuit)
        else:
            settle_close(db_module, row, request, circuit, order)
        return
    account = position_rows(request)
    if account is None:
        return
    live = next((p for p in account if p["symbol"] == row["symbol"]), None)
    if not live:
        with closing(db_module.connect()) as db, db:
            write_status(db, row, "testnet_close_unverified")
        settle_external_flat(db_module, row, request, circuit)
        return
    quantity = decimal(live["positionAmt"])
    if (quantity > 0) != (row["direction"] == "LONG") or abs(quantity) > decimal(row["quantity"]):
        with closing(db_module.connect()) as db, db:
            write_status(db, row, "testnet_position_conflict")
        return
    client_id = row["client_id"] + "_c" + str(row["close_attempt"] + 1)
    stamp = now_ms()
    with closing(db_module.connect()) as db, db:
        changed = db.execute(
            """UPDATE news_testnet_orders SET status='closing',close_client_id=?,close_ms=?,close_attempt=close_attempt+1
               WHERE event_key=? AND status='open'""", (client_id, stamp, row["event_key"]),
        ).rowcount
        if changed:
            write_status(db, row, "testnet_closing")
    if not changed:
        return
    row = dict(row, status="closing", close_client_id=client_id, close_ms=stamp)
    result = request("POST", "/fapi/v1/order", {
        "symbol": row["symbol"], "side": "SELL" if quantity > 0 else "BUY", "type": "MARKET",
        "quantity": format(abs(quantity), "f"), "reduceOnly": "true", "positionSide": "BOTH",
        "newClientOrderId": client_id, "newOrderRespType": "RESULT",
    }, retry_order=False)
    settle_close(db_module, row, request, circuit, result)


def protect_or_close(db_module, row, request, circuit, catalog, expired):
    # Query a deterministic client ID after any ambiguous protection request.
    algo_id = row["client_id"] + "_s"
    symbol = row["symbol"]
    age = now_ms() - row["opened_ms"]
    if row["protect_attempt_ms"] is None and not expired:
        long = row["direction"] == "LONG"
        entry = decimal(row["entry_price"])
        price = round_price(catalog or {}, symbol, entry * (Decimal("0.98") if long else Decimal("1.02")),
                            ROUND_UP if long else ROUND_DOWN)
        if price:
            stamp = now_ms()
            with closing(db_module.connect()) as db, db:
                db.execute("UPDATE news_testnet_orders SET protect_attempt_ms=? WHERE event_key=?",
                           (stamp, row["event_key"]))
            response = request("POST", "/fapi/v1/algoOrder", {
                "algoType": "CONDITIONAL", "symbol": symbol, "side": "SELL" if long else "BUY",
                "type": "STOP_MARKET", "triggerPrice": price, "workingType": "MARK_PRICE",
                "quantity": format(decimal(row["quantity"]), "f"), "reduceOnly": "true",
                "positionSide": "BOTH", "clientAlgoId": algo_id,
            }, retry_order=False)
            if confirmed_protection(response, algo_id, symbol):
                mark_protected(db_module, row)
                return False
            return True
    response = request("GET", "/fapi/v1/algoOrder", {"clientAlgoId": algo_id})
    if confirmed_protection(response, algo_id, symbol):
        if not expired:
            mark_protected(db_module, row)
            return False
        response = request("DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": algo_id})
        if not isinstance(response, dict) or str(response.get("code")) not in {"200", "-2011", "-2013"}:
            return True
    elif isinstance(response, dict) and response.get("clientAlgoId") == algo_id:
        if response.get("algoStatus") not in {"CANCELED", "EXPIRED", "FINISHED", "REJECTED"}:
            return True
    elif not isinstance(response, dict) or str(response.get("code")) not in {"-2011", "-2013"}:
        # A timeout must not be mistaken for an absent stop. A reduce-only close remains safe.
        if expired or age >= 45_000:
            close_due(db_module, row, request, circuit)
        return True
    if expired or age >= 45_000:
        close_due(db_module, row, request, circuit)
    return True


def confirmed_protection(response, algo_id, symbol):
    return (isinstance(response, dict) and response.get("clientAlgoId") == algo_id
            and response.get("symbol") == symbol and response.get("algoStatus") == "NEW")


def mark_protected(db_module, row):
    with closing(db_module.connect()) as db, db:
        db.execute("UPDATE news_testnet_orders SET protected=1 WHERE event_key=?", (row["event_key"],))
        write_status(db, row, "testnet_protected")


def service(db_module, positions, config, circuit, request, catalog, save_positions, *, enabled=None):
    enabled = {STRATEGY} if enabled is None else enabled
    with closing(db_module.connect(readonly=True)) as db:
        rows = [dict(row) for row in db.execute(
            """SELECT * FROM news_testnet_orders WHERE status IN ('queued','submitting','open','closing')
               ORDER BY ready_ms"""
        )]
    active = [row for row in rows if row["status"] != "queued"]
    pending_protection = False
    for row in active:
        scan = row.get("strategy") == SCAN_STRATEGY
        if row["status"] == "submitting":
            order = request("GET", "/fapi/v1/order", {"symbol": row["symbol"], "origClientOrderId": row["client_id"]})
            added = accept_order(db_module, row, order)
            if added:
                positions[:] = [p for p in positions if p["symbol"] != added["symbol"]] + [added]
                save_positions(positions)
                pending_protection |= scan
            elif isinstance(order, dict) and order.get("code") == -2013 and now_ms() > row["submitted_ms"] + 60_000:
                account = position_rows(request)
                if account is not None and not any(p["symbol"] == row["symbol"] for p in account):
                    transition(db_module, row, "rejected", "testnet_rejected")
        else:
            expired = now_ms() >= row["opened_ms"] + (SCAN_HOLD_SECONDS if scan else HOLD_SECONDS) * 1000
            try:
                if scan and row["status"] == "open":
                    has_local = any(pos.get("trade_id") == row["client_id"] for pos in positions)
                    if row["protected"] and not expired and has_local:
                        continue
                    if protect_or_close(db_module, row, request, circuit, catalog, expired or (row["protected"] and not has_local)):
                        pending_protection = True
                elif row["status"] == "closing" or expired:
                    close_due(db_module, row, request, circuit)
            except (KeyError, InvalidOperation, TypeError, ValueError):
                log.warning("Managed close/protection response invalid: %s", row["symbol"])
                pending_protection |= scan
            with closing(db_module.connect(readonly=True)) as db:
                state = db.execute("SELECT status FROM news_testnet_orders WHERE event_key=?", (row["event_key"],)).fetchone()
            if state[0] == "closed":
                positions[:] = [p for p in positions if p.get("trade_id") != row["client_id"]]
                save_positions(positions)
    for row in rows:
        if row["status"] != "queued":
            continue
        if now_ms() >= row["deadline_ms"]:
            transition(db_module, row, "expired", "testnet_expired")
        elif (row.get("strategy", STRATEGY) in enabled and not pending_protection
              and not any(item["status"] in {"submitting", "closing"} for item in active)
              and catalog and circuit.is_trading_allowed()[0]):
            added = submit(db_module, row, positions, config, request, catalog)
            if added:
                positions.append(added)
                save_positions(positions)
                pending_protection |= row.get("strategy") == SCAN_STRATEGY
            # A claimed order reserves the shared slot until Binance confirms its state.
            break
    with closing(db_module.connect(readonly=True)) as db:
        return pending_protection or db.execute(
            "SELECT 1 FROM news_testnet_orders WHERE status='submitting' LIMIT 1"
        ).fetchone() is not None
