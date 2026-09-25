#!/usr/bin/env python3
"""
trade_db.py — 归因存储层（量化升级 Phase 2.6）

职责：SQLite 主存（WAL）的连接、建表与写入助手。取代此前的
trade_features.jsonl / ai_scan.jsonl 双文件方案。

为什么换（不是体积，是查询形态）：
  - 无 schema：score_raw / ai_score_adjust 是 08-25 才补的，此前记录静默缺字段
  - 无约束：已出现 18 个 trade_id 重复 close，统计口径被迫二选一
  - 配对靠全表扫描 + 内存缓存（_CONSUMED 从不失效）
  - 新增 vetoes 需要与前瞻价格窗口 join

单点回滚：写入出口收敛在本模块 + trade_features.py，恢复这两个文件即可回退。

表结构：
  trades          开仓事实（含 5 维特征 + AI 观点快照）
  trade_closes    平仓事实（一 trade 可多行=残余仓清理，主平仓唯一）
  ai_scans        整点 AI 观点 + 当时价格（每币一行）
  vetoes          被拦截信号（AI 观望等）——此前 100% 无记录
  veto_outcomes   否决后的前瞻价格（异步回填，支撑反事实评估）
  v_trade_attribution  归因视图（一笔交易一行主平仓结果）
"""
import logging
import os
import sqlite3
from datetime import datetime

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_data.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id        TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('LONG','SHORT')),
    entry_price     REAL,
    score           INTEGER,           -- 含 AI 一致性调整
    score_raw       INTEGER,           -- 数学原始分（开仓门实际用这个）
    ai_score_adjust INTEGER DEFAULT 0, -- +5 / -3 / 0
    regime          TEXT,
    f_breakout      INTEGER,
    f_volume_ratio  REAL,
    f_atr_pct       REAL,
    f_btc_bull      INTEGER,
    f_sentiment     INTEGER,
    ai_direction    TEXT,
    ai_confidence   REAL,
    ai_reason       TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_ts ON trades(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_trades_regime    ON trades(regime);

CREATE TABLE IF NOT EXISTS trade_closes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id   TEXT NOT NULL,
    ts         TEXT NOT NULL,
    pnl_usdt   REAL,
    pnl_pct    REAL,
    reason     TEXT,
    is_primary INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_closes_trade ON trade_closes(trade_id);
-- 每笔交易只允许一条主平仓，从结构上堵死历史 18 例重复 close 导致的统计失真
CREATE UNIQUE INDEX IF NOT EXISTS uq_primary_close ON trade_closes(trade_id) WHERE is_primary = 1;

CREATE TABLE IF NOT EXISTS ai_scans (
    ts         TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    fg         INTEGER,
    price      REAL,               -- 早期记录缺价格 → 允许 NULL
    direction  TEXT,
    confidence REAL,
    reason     TEXT,
    PRIMARY KEY (ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_scans_symbol_ts ON ai_scans(symbol, ts);

CREATE TABLE IF NOT EXISTS vetoes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,   -- 实际否决时刻
    bucket        TEXT NOT NULL,   -- 小时桶 'YYYY-MM-DDTHH'，去重键
    symbol        TEXT NOT NULL,
    direction     TEXT,            -- 数学信号方向
    score_raw     INTEGER,
    regime        TEXT,
    ai_direction  TEXT,
    ai_confidence REAL,
    veto_reason   TEXT NOT NULL,   -- ai_range / ai_conflict
    price         REAL NOT NULL,   -- 否决时刻价格（反事实基准）
    UNIQUE(bucket, symbol, veto_reason)
);
CREATE INDEX IF NOT EXISTS idx_veto_symbol_ts ON vetoes(symbol, ts);

CREATE TABLE IF NOT EXISTS veto_outcomes (
    veto_id   INTEGER PRIMARY KEY,
    price_1h  REAL,
    price_4h  REAL,
    price_24h REAL,
    filled_at TEXT
);

CREATE VIEW IF NOT EXISTS v_trade_attribution AS
SELECT t.*, c.ts AS close_ts, c.pnl_usdt, c.pnl_pct, c.reason AS close_reason,
       (c.id IS NULL) AS is_unpaired
FROM trades t
LEFT JOIN trade_closes c ON c.trade_id = t.trade_id AND c.is_primary = 1;
"""


def connect(readonly: bool = False) -> sqlite3.Connection:
    """打开连接。readonly=True 供分析脚本使用，避免误写生产库。"""
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    else:
        conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表建视图（幂等）。monitor 启动时调用。"""
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────
# 写入助手（monitor 主循环调用，单条短事务）
# ─────────────────────────────────────────────
def insert_trade(trade_id: str, ts: str, symbol: str, direction: str,
                 entry_price: float, score: int, score_raw: int,
                 ai_score_adjust: int, regime: str, features: dict,
                 ai_result: dict) -> None:
    """写入开仓事实（trades 表）。"""
    feats = features or {}
    ai = ai_result or {}
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO trades
               (trade_id, ts, symbol, direction, entry_price, score, score_raw,
                ai_score_adjust, regime, f_breakout, f_volume_ratio, f_atr_pct,
                f_btc_bull, f_sentiment, ai_direction, ai_confidence, ai_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, ts, symbol, direction, entry_price, score, score_raw,
             ai_score_adjust, regime,
             feats.get("breakout"), feats.get("volume_ratio"), feats.get("atr_pct"),
             feats.get("btc_bull"), feats.get("sentiment"),
             ai.get("direction"), ai.get("confidence"), ai.get("reason")),
        )


def has_primary_close(trade_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "SELECT 1 FROM trade_closes WHERE trade_id=? AND is_primary=1 LIMIT 1", (trade_id,))
        return cur.fetchone() is not None


def insert_close(trade_id: str, ts: str, pnl_usdt: float, pnl_pct: float,
                 reason: str, is_primary: bool = True) -> None:
    """写入平仓事实。同一 trade_id 的第二条起自动降级为残余仓清理记录。"""
    with connect() as conn:
        conn.execute(
            """INSERT INTO trade_closes (trade_id, ts, pnl_usdt, pnl_pct, reason, is_primary)
               VALUES (?,?,?,?,?,?)""",
            (trade_id, ts, pnl_usdt, pnl_pct, reason, 1 if is_primary else 0),
        )


def resolve_open_by_symbol(symbol: str, direction: str | None = None) -> str | None:
    """symbol 回退：该 symbol 最近一笔无主平仓的 open trade_id。

    取代此前的全文件扫描 + 内存 _CONSUMED 缓存（该缓存从不失效，文件轮转即脏）。
    仅当同 symbol 同时最多 1 仓时成立（策略 max_positions + 不重复 symbol 约束）。

    direction：可选 LONG/SHORT。重建丢 meta 后同币可能残留多笔未配对开仓
    （Algo 漏记造成），加方向可避免把空单挂到多单的 trade_id 上。
    """
    meta = resolve_open_meta(symbol, direction=direction)
    return meta["trade_id"] if meta else None


def resolve_open_meta(
    symbol: str,
    direction: str | None = None,
    entry_price: float | None = None,
    require_entry_match: bool = None,
) -> dict | None:
    """回填重建元数据：返回未平仓 open 的 trade_id / ts / entry_price / direction。

    优先同向。入场价规则（2026-09-25）：
      - 给了 entry_price 时：**必须**偏差 <2% 才匹配，否则返回 None
        （禁止把 8 月孤儿挂到当前仓 → 误触发 48h 超时）
      - 未给 entry_price 时（平仓 symbol 兜底）：取最近一笔未配对 open
    require_entry_match 默认 = (entry_price is not None)。
    """
    if not symbol:
        return None
    if require_entry_match is None:
        require_entry_match = entry_price is not None and entry_price > 0
    with connect() as conn:
        sql = """SELECT t.trade_id, t.ts, t.entry_price, t.direction, t.score, t.regime
                 FROM trades t
                 LEFT JOIN trade_closes c ON c.trade_id = t.trade_id AND c.is_primary = 1
                 WHERE t.symbol = ? AND c.id IS NULL"""
        params: list = [symbol]
        if direction in ("LONG", "SHORT"):
            sql += " AND t.direction = ?"
            params.append(direction)
        sql += " ORDER BY t.ts DESC"
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        return None
    if require_entry_match:
        for r in rows:
            ep = r["entry_price"]
            if ep and abs(ep - entry_price) / max(abs(entry_price), 1e-12) < 0.02:
                return dict(r)
        return None  # 有价无近邻 → 宁可当新仓，不挂僵尸 id
    return dict(rows[0])


def insert_scan_rows(ts: str, fg: int, price_map: dict, results: dict) -> int:
    """写入整点 AI 扫描（每币一行）。返回写入行数。"""
    rows = []
    for sym, r in (results or {}).items():
        if not isinstance(r, dict):
            continue
        rows.append((ts, sym, fg, price_map.get(sym),
                     r.get("direction"), r.get("confidence"), r.get("reason")))
    if not rows:
        return 0
    with connect() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO ai_scans
               (ts, symbol, fg, price, direction, confidence, reason)
               VALUES (?,?,?,?,?,?,?)""",
            rows,
        )
    return len(rows)


def insert_veto(ts: str, symbol: str, direction: str, score_raw: int, regime: str,
                ai_direction: str, ai_confidence: float, veto_reason: str,
                price: float) -> bool:
    """写入被拦截信号。同小时内同币同原因只记一次（UNIQUE 约束去重）。

    返回 True 表示实际写入，False 表示被去重（主循环每 15s 重算一次信号，
    不去重会产生大量重复行）。
    """
    bucket = ts[:13]  # 'YYYY-MM-DDTHH'
    with connect() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO vetoes
               (ts, bucket, symbol, direction, score_raw, regime,
                ai_direction, ai_confidence, veto_reason, price)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (ts, bucket, symbol, direction, score_raw, regime,
             ai_direction, ai_confidence, veto_reason, price),
        )
        return cur.rowcount > 0


def backfill_veto_outcomes(tolerance_hours: float = 2.0) -> int:
    """回填否决后的前瞻价格（1h/4h/24h），数据源为 ai_scans 的整点价格。

    对每个已过对应时长但尚未回填的 veto，取 ts+N 小时后**最早**的一条 ai_scans
    价格（即最接近 N 小时目标点）。实际取值窗口为 [N, N+tolerance] 小时，
    超出 tolerance 仍无数据则视为缺失（不回填），避免"1h"名不副实。
    返回更新的 veto 数。
    """
    horizons = [(1, "price_1h"), (4, "price_4h"), (24, "price_24h")]
    updated = 0
    with connect() as conn:
        for hours, col in horizons:
            # 注意：s.ts 是 ISO 格式（含 'T' 分隔符），datetime() 输出为空格分隔，
            # 两者直接做字符串比较会失真（'T' > ' ' 使上界恒为假）→ 两侧都套 datetime()
            cur = conn.execute(
                f"""SELECT v.id,
                           (SELECT s.price FROM ai_scans s
                             WHERE s.symbol = v.symbol
                               AND s.price IS NOT NULL
                               AND datetime(s.ts) >= datetime(v.ts, '+{hours} hours')
                               AND datetime(s.ts) <= datetime(v.ts, '+{hours + tolerance_hours} hours')
                             ORDER BY s.ts LIMIT 1) AS px
                    FROM vetoes v
                    LEFT JOIN veto_outcomes o ON o.veto_id = v.id
                    WHERE o.{col} IS NULL
                      AND datetime('now', 'localtime') >= datetime(v.ts, '+{hours} hours')"""
            )
            rows = [(r["px"], r["id"]) for r in cur.fetchall() if r["px"] is not None]
            now = datetime.now().isoformat()
            for px, vid in rows:
                conn.execute(
                    f"""INSERT INTO veto_outcomes (veto_id, {col}, filled_at) VALUES (?,?,?)
                        ON CONFLICT(veto_id) DO UPDATE SET
                            {col} = excluded.{col}, filled_at = excluded.filled_at""",
                    (vid, px, now),
                )
                updated += 1
    return updated