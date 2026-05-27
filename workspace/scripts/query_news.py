#!/usr/bin/env python3
"""
query_news.py - 查询最新加密货币新闻

数据源：
    crypto_news.json    (由 news_fetcher_daemon.py 每小时刷新)

用法：
    python3 query_news.py                # 最新 5 条新闻 → stdout
    python3 query_news.py 10             # 最新 10 条 → stdout
    python3 query_news.py --push         # 最新 5 条推送卡片到飞书群
    python3 query_news.py 8 --push       # 最新 8 条推送卡片
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

SCRIPT_DIR = Path(__file__).parent
NEWS_FILE = SCRIPT_DIR / "crypto_news.json"
STALE_HOURS = 3  # 超过 3 小时未刷新视为过期


def load_news():
    if not NEWS_FILE.exists():
        return None
    try:
        return json.loads(NEWS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[ERROR] 读取 crypto_news.json 失败: {e}")
        return None


def fmt_age(timestamp: str) -> tuple[float, str]:
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        now = datetime.now(tz=ts.tzinfo) if ts.tzinfo else datetime.now()
        age_sec = (now - ts).total_seconds()
        if age_sec < 60:
            return age_sec, f"{int(age_sec)}秒前"
        if age_sec < 3600:
            return age_sec, f"{int(age_sec/60)}分钟前"
        if age_sec < 86400:
            return age_sec, f"{int(age_sec/3600)}小时前"
        return age_sec, f"{int(age_sec/86400)}天前"
    except Exception:
        return 99999, "?"


def main():
    args = sys.argv[1:]
    push_card_mode = "--push" in args
    if push_card_mode:
        args.remove("--push")
    limit = int(args[0]) if args and args[0].isdigit() else 5

    data = load_news()
    if not data:
        msg = "📭 暂无新闻数据 —— news_fetcher_daemon 可能未启动"
        if push_card_mode:
            from feishu_helper import push_card, md
            push_card("📰 加密货币新闻", [md(msg)], template="grey")
            print("[PUSH] OK")
        else:
            print(msg)
            print("修复: nohup python3 ~/.openclaw/workspace/scripts/news_fetcher_daemon.py &")
        return

    news_list = data.get("news", [])
    timestamp = data.get("timestamp", "")
    age_sec, age_str = fmt_age(timestamp)

    is_stale = age_sec > STALE_HOURS * 3600

    if not news_list:
        msg = "📭 新闻列表为空"
        if push_card_mode:
            from feishu_helper import push_card, md
            push_card("📰 加密货币新闻", [md(msg)], template="grey")
            print("[PUSH] OK")
        else:
            print(msg)
        return

    selected = news_list[:limit]

    if push_card_mode:
        from feishu_helper import push_card, md, hr, note
        elements = []
        for i, n in enumerate(selected, 1):
            emoji = n.get("emoji", "⚪")
            title = n.get("title", "")
            url = n.get("url", "")
            time_str = n.get("time", "")
            elements.append(md(f"**{i}. {emoji}** [{title}]({url})\n*{time_str}*"))
        elements.append(hr())
        prefix = "⚠️ 新闻已过期 · " if is_stale else "📡 "
        elements.append(note(f"{prefix}最后刷新 {age_str} · 共 {len(news_list)} 条"))
        template = "orange" if is_stale else "blue"
        ok = push_card(f"📰 最新加密货币新闻 (Top {len(selected)})", elements, template=template)
        print("[PUSH] OK" if ok else "[PUSH] FAILED")
        return

    print(f"[OK] 最新 {len(selected)} 条新闻 (最后刷新 {age_str}{' ⚠️过期' if is_stale else ''})")
    for i, n in enumerate(selected, 1):
        print(f"  {i}. {n.get('emoji', '⚪')} {n.get('title', '')}")
        print(f"     [{n.get('time', '')}] {n.get('url', '')}")


if __name__ == "__main__":
    main()
