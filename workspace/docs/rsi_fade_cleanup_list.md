# rsi_fade 清理清单

快照提交：`073712abb93ae25635e56c485853d0f01d1ffed3`  
标签：`snapshot/pre-rsi-fade-cleanup-2026-11-13`

这份清单只标文件，不删代码。当前实盘 `entry_strategy=rsi_fade`。旧评分开仓在主循环里是 `elif`，这条配置下走不进去。

## 必须保留

这些是正在跑的 rsi_fade，以及开平仓、记账、限频、看门狗、持仓存储共用的部分。

| 文件 | 原因 |
|---|---|
| `workspace/scripts/rsi_fade.py` | 试跑规则写在这里：1h Wilder RSI(14)，≥70 空、≤30 多，回到 50 或满 24 小时平。阈值和名义 200U、最多 4 仓写死，和配置对不上就拒绝下单。 |
| `workspace/scripts/test_rsi_fade.py` | 上面这套规则的测试。 |
| `workspace/scripts/crypto_signal_monitor.py` 里的 `_rsi_fade_trial_pass` 和主循环 | 实盘进出场从这里下单。文件里还有旧评分分支，见下文「以后可删」，整文件不能删。 |
| `workspace/scripts/execution_layer.py` 的 `close_position`、成交确认、`_apply_close_book` | 平仓、按成交量记账、-2022 只重试 reduceOnly。rsi_fade 的平仓走这里。 |
| `workspace/scripts/fill_accounting.py`、`test_fill_accounting.py` | 只按确认成交记账，不用标记价当已实现盈亏。 |
| `workspace/scripts/binance_auto_trade.py` | 签名下单、查仓、数量精度。开仓和平仓都经过它。 |
| `workspace/scripts/rest_backoff.py`、`test_rest_backoff.py` | 全局限频闸。-1003 / HTTP 418 时整机暂停。行情、K 线、下单、平仓都走它。 |
| `workspace/scripts/position_store.py` | 读写 `crypto_positions.json` 和 `crypto_state.json`。 |
| `workspace/scripts/rsi_flat_guard.py`、`test_rsi_flat_guard.py` | 心跳超时后才对 rsi_fade 仓发一笔 reduceOnly。正常出场不走这里。 |
| `workspace/scripts/crypto_monitor_watchdog.py` | 监控进程掉了由它拉起；心跳超时才调用上面的兜底平仓。 |
| `workspace/scripts/data_layer.py` | 现价和 1h K 线。RSI 用已收盘的 1h K 线。 |
| `workspace/scripts/risk_layer.py` | 熔断。rsi_fade 熔断时只平不开。 |
| `workspace/scripts/config.py`、`config.json` | 实盘配置。这次不改。`entry_strategy`、名义、仓数、币池都在这里被读。 |
| `workspace/scripts/notify_layer.py`、`feishu_helper.py`、`openclaw_logging.py` | 汇报和日志。 |
| `workspace/scripts/trade_features.py` | 平仓成交确认后记一笔特征。删了会影响平仓收尾，不是旧策略专属。 |

## 以后可删

这些不再决定现在的开平仓。删之前要先从「必须保留」的文件里拆走引用，不能整文件一刀切。

| 文件或段落 | 原因 |
|---|---|
| `crypto_signal_monitor.py` 里 `entry_strategy == rsi_fade` 后面的 `elif` 开仓块 | 旧评分开仓。配置是 rsi_fade 时不会走进去。里面还有 AI 加分和 `record_open`。 |
| `strategy_layer.py` 的 `calc_score`、`ai_score_adjust`、旧止盈止损价 | 只给旧评分用。同文件的 `calc_atr_sl_pct` 仍被执行层的 ATR 止损 import，拆之前不要删整个文件。 |
| `ai_layer.py` 里用于改开仓分数的部分 | 旧开仓才会把 AI 分数加进评分。整点扫描是否还要留，见「需要确认」。 |
| `execution_layer.py` 里只服务旧仓的止盈、止损、移动止盈、ATR 动态止损、48 小时超时 | rsi_fade 仓在这些函数开头就被跳过。函数还在，是因为主循环仍会调用。确认没有旧仓之后可以删调用和函数。 |
| `hist_backtest.py` 里除 `rsi_fade` 以外的入场（趋势突破、回踩、固定 R、资金费、横截面等） | 研究脚本，不参与实盘下单。文件里同时有 rsi_fade 回测，整文件不能删。 |
| `backtester.py`、`exit_replay.py`、`param_scan.py` | 旧链路回测、出场重放、扫参。实盘不调用。 |
| `analyze_features.py`、`export_features.py` | 只服务旧特征研究。 |
| `test_xs_mean_revert.py`、`test_xs_rel_strength.py`、`test_utc_orb.py`、`test_vol_compress_break.py`、`test_dual_ma_cross.py`、`test_funding_extreme_fade.py`、`test_pair_spread_revert.py`、`test_donchian_channel.py` | 对应候选策略的测试，不是正在跑的 rsi_fade。 |
| `phase3_regression.py` | 旧阶段回归，不在实盘循环里。 |

## 需要确认

主循环现在仍会调用，但对 `strategy=rsi_fade` 或 `regime=rsi` 的仓会跳过，或者只记账、不开仓。删之前要确认没有人还靠这些输出。

| 位置 | 现在的行为 |
|---|---|
| `execution_layer.check_exits_fast` | 每轮都调用。rsi_fade 仓直接 `continue`，不按止盈止损价平仓。 |
| `execution_layer.check_timeout_exits` | 配置开了超时才会跑。rsi_fade 仓跳过，不会按 48 小时旧规则平。 |
| `execution_layer.update_trailing_stop` | 每轮都调用。rsi_fade 仓跳过，不改移动止盈。 |
| `execution_layer.update_atr_dynamic_stops` | 指标刷新后调用。rsi_fade 仓跳过，不改 ATR 止损，也不往交易所挂止损。 |
| `execution_layer.ensure_protective_algos` | 约每 5 分钟查一次条件单。rsi_fade 的止盈止损价是 0，补挂条件不成立，不会给它挂止盈止损。 |
| `execution_layer.submit_initial_algo_orders` | 持仓同步后会对每笔仓调用。没有止盈止损价就直接返回。 |
| `crypto_signal_monitor._build_hourly_score_map` | 整点汇报仍算旧评分，只写进汇报，不开仓。 |
| `crypto_signal_monitor._run_ai_scan` | 整点仍跑 AI 扫描并入库。注释写明只记录观点，不做交易决策。 |
| `data_layer` 的恐惧贪婪和资金费率 | 主循环仍按间隔去拉。rsi_fade 开仓不用这两项。 |
