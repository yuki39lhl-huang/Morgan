"""Phase 3 离线回归：按时间前 60% 拟合、后 40% 考试，看开仓特征能否区分盈亏。只出报告，不改实盘权重。"""
import os, sqlite3, math
import numpy as np

c = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_data.db"))
rows = c.execute("""
select t.ts, t.symbol, t.direction, t.score, t.score_raw, t.ai_score_adjust, t.regime,
       t.f_breakout, t.f_volume_ratio, t.f_atr_pct, t.f_btc_bull, t.f_sentiment,
       t.ai_direction, t.ai_confidence, s.pnl, s.pct
from trades t join (select trade_id, sum(pnl_usdt) pnl, sum(case when is_primary=1 then pnl_pct end) pct
                    from trade_closes group by trade_id) s on s.trade_id=t.trade_id
order by t.ts""").fetchall()
N = len(rows)
cut = int(N * 0.6)
print(f"样本 {N} 笔；拟合段 {cut} 笔（{rows[0][0][:10]} ~ {rows[cut-1][0][:10]}），考试段 {N-cut} 笔（{rows[cut][0][:10]} ~ {rows[-1][0][:10]}）")


def feats(r):
    ts, sym, d, score, raw, adj, reg, brk, vol, atr, btc, sent, aid, aconf, pnl, pct = r
    long_ = d == "LONG"
    ai_dir = {"做多": 1, "做空": -1}.get(aid, 0)
    agree = 1 if ai_dir == (1 if long_ else -1) else (-1 if ai_dir else 0)
    btc_al = (1 if btc else -1) * (1 if long_ else -1)
    return {
        "做多": 1.0 if long_ else 0.0,
        "趋势市": 1.0 if reg == "trending" else 0.0,
        "原始分": float(raw or 0),
        "突破分": float(brk or 0),
        "量比(log)": math.log1p(max(vol or 0, 0)),
        "ATR%": float(atr or 0) * 100,
        "BTC同向": float(btc_al),
        "情绪分": float(sent or 0),
        "AI同向": float(agree),
        "AI置信度": float(aconf or 0),
        "AI调整分": float(adj or 0),
        "小时": float(int(ts[11:13])),
    }


F = [feats(r) for r in rows]
names = list(F[0])
X = np.array([[f[k] for k in names] for f in F])
y = np.array([1.0 if r[14] > 0 else 0.0 for r in rows])
pnl = np.array([r[14] for r in rows])
syms = [r[1] for r in rows]


def auc(score, lab):
    pos, neg = score[lab == 1], score[lab == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    s = 0.0
    for p in pos:
        s += (neg < p).sum() + 0.5 * (neg == p).sum()
    return s / (len(pos) * len(neg))


tr, te = slice(0, cut), slice(cut, N)
print(f"胜率：拟合段 {y[tr].mean()*100:.1f}%  考试段 {y[te].mean()*100:.1f}%；合计盈亏：拟合段 {pnl[tr].sum():+.1f}U  考试段 {pnl[te].sum():+.1f}U\n")

print("== 单特征：按拟合段三分位分档，看两段的胜率/均盈亏是否同方向 ==")
print(f"{'特征':<10}{'AUC拟合':>8}{'AUC考试':>8}   低档(拟合|考试)              高档(拟合|考试)")
for j, k in enumerate(names):
    x = X[:, j]
    a1, a2 = auc(x[tr], y[tr]), auc(x[te], y[te])
    if len(set(x[tr])) <= 3:
        th_lo, th_hi = x.min(), x.max() - 1e-9
    else:
        th_lo, th_hi = np.quantile(x[tr], [1 / 3, 2 / 3])

    def seg(mask_fn, s):
        xs, ys, ps = x[s], y[s], pnl[s]
        m = mask_fn(xs)
        return f"{m.sum():>3}笔 {ys[m].mean()*100 if m.sum() else 0:>4.0f}% {ps[m].mean() if m.sum() else 0:+.2f}U"
    lo = lambda v: v <= th_lo
    hi = lambda v: v > th_hi
    stable = "✓" if (a1 - 0.5) * (a2 - 0.5) > 0 and abs(a2 - 0.5) >= 0.05 else ""
    print(f"{k:<10}{a1:>8.2f}{a2:>8.2f}{stable:>2} {seg(lo, tr)} | {seg(lo, te)}    {seg(hi, tr)} | {seg(hi, te)}")

print("\n== 逻辑回归（L2，标准化，只用拟合段训练） ==")
mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
Z = (X - mu) / sd
Zb = np.hstack([np.ones((N, 1)), Z])
w = np.zeros(Zb.shape[1])
lam = 1.0
for _ in range(5000):
    p = 1 / (1 + np.exp(-Zb[tr] @ w))
    g = Zb[tr].T @ (p - y[tr]) / cut + lam * np.r_[0, w[1:]] / cut
    w -= 0.1 * g
pt = 1 / (1 + np.exp(-Zb @ w))
print(f"AUC 拟合段 {auc(pt[tr], y[tr]):.2f}  考试段 {auc(pt[te], y[te]):.2f}   （0.5 = 抛硬币）")
print(f"对照：现行评分 score 的考试段 AUC {auc(np.array([r[3] for r in rows], float)[te], y[te]):.2f}")
for k, wi in sorted(zip(names, w[1:]), key=lambda t: -abs(t[1])):
    print(f"  {k:<10}{wi:+.2f}")
q = np.quantile(pt[te], 0.5)
for lab, m in [("模型看好的一半", pt[te] > q), ("模型不看好的一半", pt[te] <= q)]:
    print(f"考试段 {lab}: {m.sum()}笔 胜率 {y[te][m].mean()*100:.0f}% 合计 {pnl[te][m].sum():+.1f}U")

print("\n== 分币种（拟合段 | 考试段，合计U） ==")
for s in sorted(set(syms)):
    m = np.array([x == s for x in syms])
    mt, me = m.copy(), m.copy()
    mt[cut:] = False
    me[:cut] = False
    print(f"  {s:<5}{mt.sum():>3}笔 {pnl[mt].sum():+6.1f}U | {me.sum():>3}笔 {pnl[me].sum():+6.1f}U")
for d in ("LONG", "SHORT"):
    m = np.array([r[2] == d for r in rows])
    mt, me = m.copy(), m.copy()
    mt[cut:] = False
    me[:cut] = False
    print(f"  {d:<5}{mt.sum():>3}笔 {pnl[mt].sum():+6.1f}U | {me.sum():>3}笔 {pnl[me].sum():+6.1f}U")
