# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repository Is

This is an OpenClaw agent configuration repository for a cryptocurrency monitoring and automated trading system. It runs on Binance Futures Testnet and uses DeepSeek `deepseek-v4-pro` (thinking disabled) for AI signal mixing. The AI persona is Wang Jie (王姐), a Morgan-from-FGO style companion for user yuki (老公).

## Session Startup Protocol

When starting a new session, always read in order:
1. `workspace/SOUL.md` — AI persona and trading strategy rules
2. `workspace/USER.md` — User profile (yuki, Asia/Shanghai, Java dev student)
3. `workspace/memory/YYYY-MM-DD.md` for today's date — session continuity

## Starting the System

```bash
kj          # One-command startup (kj.sh v3.19): starts Clash proxy → OpenClaw gateway → news fetcher → auto trader → signal monitor
```

Manual component startup (if needed):
```bash
python3 workspace/scripts/crypto_signal_monitor.py   # Main trading engine
python3 workspace/scripts/binance_auto_trade.py      # Order execution
python3 workspace/scripts/crypto_news_fetcher.py     # News aggregation
openclaw gateway                                      # WebSocket/REST gateway on :18789
```

Check logs:
```bash
tail -f workspace/scripts/crypto_monitor.log   # Main signal + trade log
tail -f workspace/scripts/binance_trade.log    # Order execution log
tail -f workspace/scripts/crypto_news.log      # News fetcher log
tail -f workspace/scripts/clash.log            # Proxy log
```

## Architecture

```
kj.sh → Clash Proxy (127.0.0.1:7890) → OpenClaw Gateway (:18789)
                                               ↓
         crypto_signal_monitor.py ←→ binance_auto_trade.py
                  ↓                            ↓
         Binance WebSocket              Binance Futures Testnet
         (15s price checks)            (HMAC-signed REST API)
                  ↓
         crypto_news_fetcher.py → Feishu notifications
```

**Why Clash proxy is required:** Binance is geo-blocked in China. All Binance API calls must route through Clash (Mihomo) on port 7890 or requests will fail.

## Key Configuration Files

| File | Purpose |
|------|---------|
| `openclaw.json` | LLM provider (deepseek-v4-pro), gateway port 18789, Feishu integration |
| `config.yaml` | Gateway bind address, Feishu credentials, Clash config path |
| `workspace/scripts/auto_trade_config.json` | Trading parameters: 100 USDT capital, 20 USDT/position, 10x leverage, 4% TP, 2% SL |
| `workspace/scripts/crypto_positions.json` | Live position state |
| `workspace/scripts/crypto_state.json` | System state (scores, thresholds) |
| `feishu/dedup/default.json` | Deduplication cache for notifications |

## Trading Strategy (v6.1)

Tracked coins: BTC, ETH, SOL, BNB, DOT, LINK, XRP

Signal scoring (0–100):
- RSI: 35 points
- 24h price change: 25 points
- MA60 crossover: 20 points
- Volume: 20 points

Thresholds by market state:
- Trending: ≥70 score, 25% position size
- Ranging: ≥30 score (temp test setting)
- Volatile: ≥90 score, 15% position size

Risk controls: -2% stop-loss per position, three-tier trailing stop (0.8%/1%/full), -10% daily circuit breaker (4h pause), max 4 concurrent positions.

## Critical Bug Patterns (Blood-Learned)

These bugs have caused real losses and must not be reintroduced:

1. **Never read price from cache** — always fetch live from Binance API. Cache is backup only.
2. **Always add `continue`/`return` after condition checks** in signal loops — missing flow control caused duplicate orders.
3. **Per-symbol precision is mandatory** — each coin has different `stepSize`: BTC(3), ETH(3), SOL(2), BNB(2), DOT(1), LINK(2), XRP(1). Generic rounding causes order rejection.
4. **Restart after code changes** — the running process must be killed and restarted; hot-reload does not exist here.
5. **`recvWindow=60000`** must be set on all Binance API calls to handle clock drift.

## Memory System

Daily memory files live in `workspace/memory/YYYY-MM-DD.md`. Main agent writes to these; sub-agents must not. `workspace/memory/MEMORY.md` is the index. Do not store code patterns here — only non-obvious context and decisions.

## Agents Directory

`agents/main/` holds session state only (auth profiles, active session). No custom agent definitions exist; the main agent persona is fully defined in `workspace/SOUL.md`.
