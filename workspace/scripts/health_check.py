#!/usr/bin/env python3
"""
health_check.py - 系统全链路健康检查

检查项：
    🟢 进程层  - Clash / Gateway / Monitor / Watchdog / NewsDaemon
    🟢 数据层  - crypto_state.json 实时刷新（< 60秒）
    🟢 API 层  - Binance Testnet 签名验证 / Feishu Token / DeepSeek
    🟢 业务层  - 信号评分系统是否在跑（看 monitor.log 最近活动）

用法：
    python3 health_check.py            # stdout 报告
    python3 health_check.py --push     # 推送卡片到飞书群
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from openclaw_logging import trading_log_path

STATE_FILE = SCRIPT_DIR / "crypto_state.json"
NEWS_FILE = SCRIPT_DIR / "crypto_news.json"

MONITOR_LOG = trading_log_path()
GATEWAY_LOG_DIR = Path("/tmp/openclaw")

OK = "🟢"
WARN = "🟡"
ERR = "🔴"


def proc_running(pattern: str) -> tuple[bool, str, int]:
    try:
        out = subprocess.check_output(
            ["pgrep", "-af", pattern], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if out:
            lines = [ln for ln in out.splitlines() if ln.strip()]
            pids = [ln.split()[0] for ln in lines]
            count = len(pids)
            if count == 1:
                return True, f"PID {pids[0]}", count
            return False, f"重复运行 {count} 个 (PID: {', '.join(pids[:4])})", count
    except subprocess.CalledProcessError:
        pass
    return False, "未运行", 0


def check_processes():
    items = [
        ("Clash 代理", "mihomo"),
        ("OpenClaw Gateway", "openclaw-gateway"),
        ("信号监控主进程", "crypto_signal_monitor.py"),
        ("看门狗", "crypto_monitor_watchdog.py"),
        ("新闻守护", "news_fetcher_daemon.py"),
    ]
    results = []
    for label, pat in items:
        ok, info, count = proc_running(pat)
        results.append((label, ok, info))
        if count > 1:
            # 额外附加一条提示，方便飞书卡片中肉眼快速识别
            results.append((f"{label}（重复风险）", False, "建议执行 kj restart 收敛为单实例"))
    return results


def check_state_freshness():
    if not STATE_FILE.exists():
        return False, "crypto_state.json 不存在"
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data.get("last_update", ""))
        age = (datetime.now() - ts).total_seconds()
        prices = data.get("prices", {})
        n = len(prices)
        if age < 60:
            return True, f"{int(age)}秒前更新, {n} 个币种"
        return False, f"⚠️过期 {int(age)}秒"
    except Exception as e:
        return False, f"解析失败: {e}"


def check_news_freshness():
    if not NEWS_FILE.exists():
        return False, "crypto_news.json 不存在"
    try:
        data = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
        ts_str = data.get("timestamp", "").replace("Z", "+00:00")
        ts = datetime.fromisoformat(ts_str)
        now = datetime.now(tz=ts.tzinfo) if ts.tzinfo else datetime.now()
        age = (now - ts).total_seconds()
        cnt = len(data.get("news", []))
        if age < 4 * 3600:
            return True, f"{int(age/60)}分钟前刷新, {cnt} 条"
        return False, f"⚠️过期 {int(age/3600)}小时"
    except Exception as e:
        return False, f"解析失败: {e}"


def check_binance_api():
    try:
        import binance_auto_trade as bat
        # 余额为 0 并不代表失败（可能满仓）。只要签名请求返回结构正确就算通过。
        result = bat.request("GET", "/fapi/v2/balance")
        if isinstance(result, list):
            usdt = 0.0
            for item in result:
                if isinstance(item, dict) and item.get("asset") == "USDT":
                    try:
                        usdt = float(item.get("availableBalance", 0) or 0)
                    except Exception:
                        usdt = 0.0
                    break
            return True, f"签名 OK, 可用 {usdt:.2f} USDT"
        if isinstance(result, dict) and result.get("error"):
            return False, f"签名请求失败: {result.get('error')}"
        return False, f"签名返回异常: {result}"
    except Exception as e:
        return False, f"调用失败: {e}"


def check_feishu_token():
    try:
        from feishu_helper import get_token
        token = get_token()
        if token:
            return True, f"token OK ({token[:20]}...)"
        return False, "token 获取失败"
    except Exception as e:
        return False, f"异常: {e}"


def check_llm_api():
    try:
        import requests
        api_key = "sk-e91eabcb8c7d4a15a867fac0a3fb1c07"
        url = "https://api.deepseek.com/chat/completions"
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5,
                "thinking": {"type": "disabled"},
            },
            timeout=10,
            proxies=None,
        )
        if r.status_code == 200:
            return True, "deepseek-v4-pro OK"
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"异常: {e}"


def check_signal_activity():
    """看 monitor.log 最近 10 分钟的策略心跳"""
    if not MONITOR_LOG.exists():
        return False, f"日志文件不存在: {MONITOR_LOG}"
    try:
        size = MONITOR_LOG.stat().st_size
        if size == 0:
            return False, f"日志文件为空（{MONITOR_LOG.name}）"
        # 读末尾 512KB，覆盖更长时间窗口
        with open(MONITOR_LOG, "rb") as f:
            f.seek(max(0, size - 524288))
            tail = f.read().decode("utf-8", errors="ignore")
        lines = tail.splitlines()[-3000:]
        now = time.time()
        # 真实日志关键字（来自 crypto_signal_monitor.py 实际 log 语句）
        # 1) 交易动作（强信号）
        # 2) 心跳类（频繁）
        action_kw = re.compile(r"开多|开空|平仓|止盈|止损|下单结果|平仓结果|订单确认")
        heartbeat_kw = re.compile(
            r"K线|获取|持仓|价格|WebSocket|RSI|ATR|score|评分|信号|"
            r"整点汇报|资金费率|恐惧贪婪|AI预测|AI跳过|冷却|"
            r"扫描|计算|更新|从 Binance|API"
        )
        ts_pattern = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
        last_ts = ""
        recent_heartbeat = 0
        recent_actions = 0
        for line in lines:
            m = ts_pattern.search(line)
            if not m:
                continue
            try:
                lts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                age = now - lts.timestamp()
                last_ts = m.group(1)
                if age <= 600:  # 10 分钟
                    if action_kw.search(line):
                        recent_actions += 1
                    elif heartbeat_kw.search(line):
                        recent_heartbeat += 1
            except Exception:
                continue
        if recent_actions > 0:
            return True, f"10分钟内 {recent_actions} 条交易动作，{recent_heartbeat} 条心跳"
        if recent_heartbeat > 0:
            return True, f"10分钟内 {recent_heartbeat} 条心跳，无交易动作（市场静默期）"
        return False, f"10分钟无策略日志（最后日志: {last_ts or '空'}，路径: {MONITOR_LOG.name}）"
    except Exception as e:
        return False, f"读取失败: {e}"


def check_websocket_alive():
    """看 monitor.log 中 WebSocket 连接是否最近成功过"""
    if not MONITOR_LOG.exists():
        return False, "monitor.log 不存在"
    try:
        size = MONITOR_LOG.stat().st_size
        with open(MONITOR_LOG, "rb") as f:
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", errors="ignore")
        if "WebSocket 价格流已连接" in tail or "WebSocket" in tail and "连接" in tail:
            return True, "WebSocket 已连接"
        if "fallback" in tail.lower():
            return False, "⚠️ 已降级为 REST 兜底"
        return True, "未见错误（推断正常）"
    except Exception:
        return False, "读取失败"


def main():
    push = "--push" in sys.argv

    procs = check_processes()
    state_ok, state_msg = check_state_freshness()
    news_ok, news_msg = check_news_freshness()
    api_ok, api_msg = check_binance_api()
    feishu_ok, feishu_msg = check_feishu_token()
    llm_ok, llm_msg = check_llm_api()
    signal_ok, signal_msg = check_signal_activity()
    ws_ok, ws_msg = check_websocket_alive()

    all_checks = []
    all_checks.append(("**进程层**", "", ""))
    for name, ok, info in procs:
        all_checks.append((name, ok, info))
    all_checks.append(("**数据层**", "", ""))
    all_checks.append(("crypto_state 实时刷新", state_ok, state_msg))
    all_checks.append(("crypto_news 周期刷新", news_ok, news_msg))
    all_checks.append(("WebSocket 价格流", ws_ok, ws_msg))
    all_checks.append(("**API 层**", "", ""))
    all_checks.append(("Binance Testnet 签名", api_ok, api_msg))
    all_checks.append(("飞书 Token", feishu_ok, feishu_msg))
    all_checks.append(("DeepSeek 大模型", llm_ok, llm_msg))
    all_checks.append(("**业务层**", "", ""))
    all_checks.append(("信号评分系统", signal_ok, signal_msg))

    fail = sum(
        1
        for n, ok, _ in all_checks
        if not n.startswith("**") and ok is False
    )
    total = sum(1 for n, _, _ in all_checks if not n.startswith("**"))
    summary = (
        f"✅ 全系统正常 ({total}/{total})"
        if fail == 0
        else f"⚠️ {fail} 项异常 / 共 {total} 项"
    )

    if push:
        from feishu_helper import push_card, md, hr, note

        elements = []
        groups = []  # [(title, [lines])]
        for name, ok_val, info in all_checks:
            if name.startswith("**"):
                groups.append((name.replace("**", ""), []))
            else:
                if not groups:
                    groups.append(("检查项", []))
                emoji = OK if ok_val is True else (ERR if ok_val is False else WARN)
                color = "green" if ok_val is True else ("red" if ok_val is False else "orange")
                groups[-1][1].append(
                    f"{emoji} <font color='grey'>{name}</font>　"
                    f"<font color='{color}'>{info}</font>"
                )
        for title, lines in groups:
            if lines:
                elements.append(md(f"### {title}\n" + "\n".join(lines)))
        
        elements.append(hr())
        # 总览
        if fail == 0:
            elements.append(md(
                f"### 🎉 <font color='green'>**全系统正常**</font>　"
                f"<font color='grey'>({total}/{total})</font>"
            ))
        else:
            elements.append(md(
                f"### ⚠️ <font color='red'>**{fail} 项异常**</font>　"
                f"<font color='grey'>共 {total} 项</font>"
            ))
        elements.append(note(
            f"🩺 健康检查 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        ))
        template = "green" if fail == 0 else ("orange" if fail <= 2 else "red")
        ok = push_card("🩺 系统健康报告", elements, template=template)
        print("[PUSH] OK" if ok else "[PUSH] FAILED")
        return

    print(f"🩺 系统健康检查 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    for name, ok, info in all_checks:
        if name.startswith("**"):
            print(f"\n── {name.replace('**', '')} ──")
        else:
            emoji = OK if ok is True else (ERR if ok is False else WARN)
            print(f"  {emoji} {name:<25} {info}")
    print(f"\n{summary}\n")


if __name__ == "__main__":
    main()
