#!/usr/bin/env python3
"""
audit_strategy.py - 检查三大核心功能是否正常运行

检查：
    1️⃣ 分数计算    —— monitor 是否在持续算评分
    2️⃣ 自动开仓    —— monitor 是否触发并成功调用 binance 下单
    3️⃣ 移动止盈止损 —— monitor 是否在动态调整止损价并同步到 Binance

用法：
    python3 audit_strategy.py                    # 快照（最近 60 分钟）
    python3 audit_strategy.py 30                 # 最近 30 分钟
    python3 audit_strategy.py --push             # 推送美观的飞书卡片
    python3 audit_strategy.py --watch            # 实时跟踪关键日志
    python3 audit_strategy.py --raw              # 输出原始日志（调试用）
"""
import re
import sys
import time
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from openclaw_logging import trading_log_path

MONITOR_LOG = trading_log_path()


def _last_ts_in(path: Path):
    """读文件末尾，返回最后一条可解析的时间戳；空文件 / 无格式日志返回 None"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16384))
            tail = f.read().decode("utf-8", errors="ignore")
        last = None
        for line in tail.splitlines():
            ts = parse_ts(line)
            if ts:
                last = ts
        return last
    except Exception:
        return None


def _find_log_via_proc(verbose: bool = False) -> Path:
    """
    最准确的方法：直接从 monitor 进程的 /proc/$PID/fd/ 找它打开的 .log 文件
    这能 100% 命中 monitor 真正在写的日志，绕开候选路径猜测
    """
    try:
        out = subprocess.check_output(
            ["pgrep", "-af", "crypto_signal_monitor.py"],
            text=True, timeout=3,
        )
    except Exception:
        return None
    pids = []
    for line in out.strip().splitlines():
        parts = line.split(None, 1)
        if parts and parts[0].isdigit():
            pids.append(parts[0])
    if not pids:
        return None
    found = []
    for pid in pids:
        fd_dir = Path(f"/proc/{pid}/fd")
        if not fd_dir.exists():
            continue
        try:
            for fd in fd_dir.iterdir():
                try:
                    target = fd.resolve()
                except Exception:
                    continue
                name = str(target)
                if name.endswith(".log") and "crypto_monitor" in name and Path(name).exists():
                    found.append(Path(name))
        except Exception:
            continue
    if verbose and found:
        print(f"[debug] /proc 发现 monitor 打开的日志：{set(map(str, found))}")
    if not found:
        return None
    by_freshness = []
    for p in set(found):
        ts = _last_ts_in(p)
        by_freshness.append((p, ts))
    by_freshness.sort(key=lambda x: x[1] or datetime.min, reverse=True)
    return by_freshness[0][0]


def find_monitor_log(verbose: bool = False) -> Path:
    """
    选择当前真正在写日志的 monitor.log：
    1. 最优先：从 /proc/$PID/fd/ 直接找 monitor 进程打开的 .log
    2. 次优：候选列表中「文件末尾时间戳最新」的
    3. 兜底：mtime 最新
    """
    proc_log = _find_log_via_proc(verbose=verbose)
    if proc_log:
        if verbose:
            print(f"[debug] /proc 找到 → {proc_log}")
        return proc_log
    
    cands = [MONITOR_LOG] if MONITOR_LOG.exists() and MONITOR_LOG.stat().st_size > 0 else []
    if not cands:
        return None
    
    scored = []
    for p in cands:
        last_ts = _last_ts_in(p)
        scored.append((p, last_ts, p.stat().st_size, p.stat().st_mtime))
    
    if verbose:
        print("[debug] 候选日志：")
        for p, ts, sz, mt in scored:
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "(无时间戳)"
            print(f"  {p}  size={sz}  last_ts={ts_str}")
    
    with_ts = [(p, ts, sz, mt) for p, ts, sz, mt in scored if ts]
    if with_ts:
        with_ts.sort(key=lambda x: x[1], reverse=True)
        return with_ts[0][0]
    
    scored.sort(key=lambda x: x[3], reverse=True)
    return scored[0][0]


TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def parse_ts(line: str):
    m = TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


PATTERNS = {
    "score_compute": re.compile(r"📊 开仓前检查|🤖 .+ (跳过|触发) AI"),
    "score_signal": re.compile(r"🤖 (\w+) (?:跳过|触发) AI(?:.*?评分\s*(\d+))?(?:.*?\((\d+)\s*分\))?"),
    "open_success": re.compile(r"✅ (\w+) 开仓成功"),
    "open_failed": re.compile(r"🚨 (\w+) 下单失败|❌ (\w+) place_order 失败|⚠️ (\w+) 已达最大持仓数"),
    "trail_update": re.compile(r"📌 (\w+) 止损单已更新 Binance：([\d.]+)\s*→\s*([\d.]+)"),
    "trail_fail": re.compile(r"⚠️ (\w+) 更新 Binance 止损单失败|⚠️ (\w+) 同步止损单异常"),
    "close_tp": re.compile(r"✅ 平仓 (\w+) (TP\d+|止盈)"),
    "close_sl": re.compile(r"✅ 平仓 (\w+) (SL|止损)"),
    "ai_call": re.compile(r"🤖 (\w+) 触发 AI"),
    "circuit": re.compile(r"交易暂停|连亏\d+次"),
}


def collect_events(log_path: Path, since: datetime):
    """扫描日志，提取最近 since 之后的关键事件"""
    events = {k: [] for k in PATTERNS}
    if not log_path or not log_path.exists():
        return events, None
    last_ts = None
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 5_000_000))
            for line in f:
                ts = parse_ts(line)
                if ts:
                    last_ts = ts
                if not ts or ts < since:
                    continue
                for name, pat in PATTERNS.items():
                    if pat.search(line):
                        events[name].append((ts, line.rstrip()))
                        break
    except Exception as e:
        print(f"[ERROR] 读取日志失败: {e}", file=sys.stderr)
    return events, last_ts


def fmt_age(ts: datetime, now: datetime) -> str:
    delta = (now - ts).total_seconds()
    if delta < 60:
        return f"{int(delta)}秒前"
    if delta < 3600:
        return f"{int(delta/60)}分钟前"
    return f"{int(delta/3600)}小时前"


def render_text(events, last_ts, since_minutes, log_path) -> tuple:
    now = datetime.now()
    lines = []
    issues = []
    
    lines.append(f"📋 策略审计报告（最近 {since_minutes} 分钟）")
    lines.append(f"📁 日志：{log_path or '❌ 找不到'}")
    if last_ts:
        lines.append(f"⏰ 最后日志：{last_ts.strftime('%H:%M:%S')} ({fmt_age(last_ts, now)})")
    lines.append("─" * 60)
    
    score_events = events["score_compute"]
    score_signal_events = events["score_signal"]
    if score_events:
        last_t, _ = score_events[-1]
        lines.append(f"1️⃣ 分数计算：✅ 正常（{len(score_events)} 次扫描，最后 {fmt_age(last_t, now)}）")
        latest_scores = {}
        for ts, line in score_signal_events[-30:]:
            m = PATTERNS["score_signal"].search(line)
            if m:
                sym = m.group(1)
                score = m.group(2) or m.group(3) or "?"
                latest_scores[sym] = (ts, score)
        if latest_scores:
            sample = ", ".join(f"{s}={sc}分" for s, (_, sc) in list(latest_scores.items())[:7])
            lines.append(f"   最新评分：{sample}")
    else:
        lines.append(f"1️⃣ 分数计算：🔴 {since_minutes} 分钟内无评分日志")
        issues.append("分数计算停滞")
    
    open_ok = events["open_success"]
    open_fail = events["open_failed"]
    if open_ok:
        last_t, last_line = open_ok[-1]
        m = PATTERNS["open_success"].search(last_line)
        sym = m.group(1) if m else "?"
        lines.append(f"2️⃣ 自动开仓：✅ 成功 {len(open_ok)} 次（最近 {sym} {fmt_age(last_t, now)}）")
    elif open_fail:
        lines.append(f"2️⃣ 自动开仓：🟡 触发但失败 {len(open_fail)} 次")
        for ts, line in open_fail[-3:]:
            lines.append(f"   • {ts.strftime('%H:%M')} {line[20:120]}")
        issues.append(f"开仓失败 {len(open_fail)} 次")
    else:
        lines.append(f"2️⃣ 自动开仓：⚪ {since_minutes} 分钟内未触发（市场没信号 / 已满仓 / 冷却中）")
    
    trail_ok = events["trail_update"]
    trail_fail = events["trail_fail"]
    if trail_ok:
        last_t, last_line = trail_ok[-1]
        m = PATTERNS["trail_update"].search(last_line)
        if m:
            sym, old, new = m.group(1), m.group(2), m.group(3)
            lines.append(f"3️⃣ 移动止盈止损：✅ 更新 {len(trail_ok)} 次（{sym}: {old}→{new}, {fmt_age(last_t, now)}）")
        else:
            lines.append(f"3️⃣ 移动止盈止损：✅ 更新 {len(trail_ok)} 次")
    elif trail_fail:
        lines.append(f"3️⃣ 移动止盈止损：🔴 同步失败 {len(trail_fail)} 次")
        issues.append(f"止损同步失败 {len(trail_fail)} 次")
    else:
        lines.append(f"3️⃣ 移动止盈止损：⚪ {since_minutes} 分钟内未触发更新（持仓无利润推进）")
    
    extras = []
    if events["close_tp"]:
        extras.append(f"🎯 止盈平仓 {len(events['close_tp'])} 次")
    if events["close_sl"]:
        extras.append(f"🛑 止损平仓 {len(events['close_sl'])} 次")
    if events["ai_call"]:
        extras.append(f"🤖 AI 调用 {len(events['ai_call'])} 次")
    if events["circuit"]:
        extras.append(f"⚡ 熔断事件 {len(events['circuit'])} 次")
    if extras:
        lines.append("─" * 60)
        lines.append("附加：" + " | ".join(extras))
    
    lines.append("─" * 60)
    if issues:
        lines.append(f"⚠️ 发现问题：{', '.join(issues)}")
    elif not score_events:
        lines.append("⚠️ 整个监控可能没在运行，请检查 ps -ef | grep crypto_signal_monitor")
    else:
        lines.append("🎉 三大核心功能运转正常")
    
    return "\n".join(lines), issues, events


def push_card(events, last_ts, since_minutes, log_path, issues):
    from feishu_helper import push_card as _push, md, hr, kv_block, note
    now = datetime.now()
    
    score_events = events["score_compute"]
    open_ok = events["open_success"]
    open_fail = events["open_failed"]
    trail_ok = events["trail_update"]
    trail_fail = events["trail_fail"]
    
    def status(ok, fail, neutral_text):
        if fail and not ok:
            return f"🔴 失败 {len(fail)} 次"
        if ok:
            return f"🟢 正常 {len(ok)} 次"
        return f"⚪ {neutral_text}"
    
    elements = []
    elements.append(md(f"<font color='grey'>窗口：最近 {since_minutes} 分钟</font>"))
    elements.append(hr())
    
    score_status = "🟢 持续运行" if score_events else "🔴 停滞"
    score_detail = f"{len(score_events)} 次扫描" if score_events else f"{since_minutes}分钟无日志"
    elements.append(md(f"### 1️⃣ 分数计算\n{score_status} —— {score_detail}"))
    
    score_signal_events = events["score_signal"]
    latest_scores = {}
    for ts, line in score_signal_events[-30:]:
        m = PATTERNS["score_signal"].search(line)
        if m:
            sym = m.group(1)
            score = m.group(2) or m.group(3) or "?"
            latest_scores[sym] = score
    if latest_scores:
        elements.append(kv_block([(s, f"{sc} 分") for s, sc in list(latest_scores.items())[:8]]))
    elements.append(hr())
    
    open_status_str = status(open_ok, open_fail, f"{since_minutes}分钟无开仓信号")
    elements.append(md(f"### 2️⃣ 自动开仓\n{open_status_str}"))
    if open_ok:
        last_t, last_line = open_ok[-1]
        m = PATTERNS["open_success"].search(last_line)
        sym = m.group(1) if m else "?"
        elements.append(md(f"<font color='green'>最近成功</font>: **{sym}** ({fmt_age(last_t, now)})"))
    elif open_fail:
        for ts, line in open_fail[-3:]:
            elements.append(md(f"<font color='red'>•</font> {ts.strftime('%H:%M')} `{line[20:140]}`"))
    elements.append(hr())
    
    trail_status_str = status(trail_ok, trail_fail, f"{since_minutes}分钟无止损更新")
    elements.append(md(f"### 3️⃣ 移动止盈止损\n{trail_status_str}"))
    if trail_ok:
        rows = []
        for ts, line in trail_ok[-5:]:
            m = PATTERNS["trail_update"].search(line)
            if m:
                rows.append((f"{m.group(1)} ({ts.strftime('%H:%M')})", f"{m.group(2)} → {m.group(3)}"))
        if rows:
            elements.append(kv_block(rows))
    
    extras = []
    if events["close_tp"]: extras.append(("🎯 止盈平仓", f"{len(events['close_tp'])} 次"))
    if events["close_sl"]: extras.append(("🛑 止损平仓", f"{len(events['close_sl'])} 次"))
    if events["ai_call"]:  extras.append(("🤖 AI 调用", f"{len(events['ai_call'])} 次"))
    if events["circuit"]:  extras.append(("⚡ 熔断事件", f"{len(events['circuit'])} 次"))
    if extras:
        elements.append(hr())
        elements.append(md("### 📊 附加事件"))
        elements.append(kv_block(extras))
    
    elements.append(hr())
    if issues:
        elements.append(md(f"### ⚠️ <font color='red'>**发现问题**</font>\n" + "\n".join(f"• {x}" for x in issues)))
        template = "red"
    elif not score_events:
        elements.append(md("### 🔴 <font color='red'>**监控可能未运行**</font>\n请检查 `ps -ef | grep crypto_signal_monitor`"))
        template = "red"
    else:
        elements.append(md("### 🎉 <font color='green'>**三大核心功能运转正常**</font>"))
        template = "green"
    
    elements.append(note(f"快照时间 {now.strftime('%Y-%m-%d %H:%M:%S')}"))
    
    title = "🔍 策略运行审计"
    return _push(title, elements, template=template)


def watch_mode():
    """实时跟踪关键事件"""
    log_path = find_monitor_log()
    if not log_path:
        print("[ERROR] 找不到 monitor 日志")
        sys.exit(1)
    print(f"📍 跟踪：{log_path}")
    print("等待事件...（Ctrl+C 退出）\n")
    keywords = ["📊 开仓前检查", "✅", "📌", "🚨", "🤖", "🎯", "🛑", "⚠️", "❌", "🔄"]
    proc = subprocess.Popen(
        ["tail", "-F", "-n", "0", str(log_path)],
        stdout=subprocess.PIPE, text=True, bufsize=1,
    )
    try:
        for line in proc.stdout:
            if any(k in line for k in keywords):
                sys.stdout.write(line)
                sys.stdout.flush()
    except KeyboardInterrupt:
        proc.terminate()
        print("\n👋 已退出跟踪")


def main():
    args = sys.argv[1:]
    
    if "--watch" in args:
        watch_mode()
        return
    
    push = "--push" in args
    raw = "--raw" in args
    debug = "--debug" in args
    nums = [a for a in args if a.isdigit()]
    minutes = int(nums[0]) if nums else 60
    
    log_path = find_monitor_log(verbose=debug)
    since = datetime.now() - timedelta(minutes=minutes)
    events, last_ts = collect_events(log_path, since)
    
    if raw:
        for name, lst in events.items():
            if lst:
                print(f"\n=== {name} ({len(lst)}) ===")
                for ts, line in lst[-10:]:
                    print(line)
        return
    
    text, issues, _ = render_text(events, last_ts, minutes, log_path)
    print(text)
    
    has_stale = False
    if not events["score_compute"] or not last_ts:
        cands = [(MONITOR_LOG, _last_ts_in(MONITOR_LOG), MONITOR_LOG.stat().st_size if MONITOR_LOG.exists() else 0)]
        if MONITOR_LOG.exists() and len(cands) >= 1:
            print("\n📂 候选日志文件（用于排查日志路径）：")
            for p, ts, sz in cands:
                age = fmt_age(ts, datetime.now()) if ts else "(无时间戳)"
                marker = "👉" if p == log_path else "  "
                print(f"  {marker} {p}")
                print(f"     大小={sz}B  最后日志={age}")
                if ts is None or (datetime.now() - ts).total_seconds() > 600:
                    has_stale = True
            if has_stale:
                print("\n💡 发现 stale 旧日志文件，建议清理（确保 monitor 在写的日志是唯一的）：")
                stale = [p for p, ts, _ in cands
                         if (ts is None or (datetime.now() - ts).total_seconds() > 600) and p != log_path]
                for p in stale:
                    print(f"  trash {p}   # 或 rm {p}")
    
    if push:
        ok = push_card(events, last_ts, minutes, log_path, issues)
        if not ok:
            sys.exit(2)


if __name__ == "__main__":
    main()
