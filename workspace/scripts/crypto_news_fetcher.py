#!/usr/bin/env python3
"""
加密货币新闻抓取
📰 抓取媒体 RSS、美联储货币政策 RSS 和币安公告公开频道
🧠 分析情绪（利好/利空）
📊 结合技术面给出综合建议
"""

import requests
import json
import os
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from datetime import datetime, timezone, timedelta

import config
import trade_db

# 统一配置：代理 / 文件路径来自 config.json
APP_CONFIG = config.get_config()
PROXY = APP_CONFIG['proxies']
NEWS_FILE = Path(APP_CONFIG['news_file'])
ANALYSIS_FILE = Path(APP_CONFIG['analysis_file'])

# 情绪关键词
SENTIMENT_KEYWORDS = {
    'bullish': [
        '暴涨', '突破', '利好', '上涨', '买入', 'bull', 'moon', 'rally',
        'surge', 'jump', 'gain', 'rise', 'breakout', 'buy', 'upgrade'
    ],
    'bearish': [
        '暴跌', '崩盘', '利空', '下跌', '卖出', 'bear', 'crash', 'dump',
        'plunge', 'drop', 'fall', 'sell', 'hack', 'attack', 'ban'
    ],
    'neutral': [
        '震荡', '盘整', '观望', 'sideways', 'consolidation', 'regulation'
    ]
}

def get_beijing_time():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))

def log(msg):
    timestamp = get_beijing_time().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")

FED_SOURCE = "美联储货币政策"
RSS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    FED_SOURCE: "https://www.federalreserve.gov/feeds/press_monetary.xml",
}
BINANCE_SOURCE = "币安"
BINANCE_CHANNEL = "binance_announcements"
SOURCES = {**RSS_FEEDS, BINANCE_SOURCE: f"https://t.me/s/{BINANCE_CHANNEL}"}
CST = timezone(timedelta(hours=8))


def _item(source, title, url, published, observed_ms, content=""):
    return {
        "source": source, "title": title, "url": url,
        "time": published.astimezone(CST).strftime("%Y-%m-%d %H:%M"),
        "published_ms": int(published.timestamp() * 1000),
        "observed_ms": observed_ms, "content": content,
    }


def _parse_rss(source, payload, observed_ms):
    root = ET.fromstring(payload)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("missing RSS channel")
    result = []
    for item in channel.findall("item")[:15]:
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        raw_date = item.findtext("pubDate")
        if not title or not url or not raw_date:
            continue
        try:
            published = parsedate_to_datetime(raw_date)
        except (TypeError, ValueError):
            continue
        if published.tzinfo is not None:
            # Fed recurring releases can reuse the exact title across different meetings.
            if source == FED_SOURCE:
                title += f" ({published.astimezone(CST):%Y-%m-%d %H:%M})"
            result.append(_item(source, title, url, published, observed_ms))
    if not result and channel.findall("item"):
        raise ValueError("no readable RSS items")
    return result


class _TelegramPosts(HTMLParser):
    """Extract public channel messages without visiting their outbound links."""

    def __init__(self):
        super().__init__()
        self.items = []
        self.post = None
        self.text_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            post_id = attrs.get("data-post", "")
            if post_id.startswith(BINANCE_CHANNEL + "/"):
                suffix = post_id.rsplit("/", 1)[-1]
                if suffix.isdecimal():
                    self.post = {"id": suffix, "body": [], "published": ""}
                    self.items.append(self.post)
                    self.text_depth = 0
            if self.text_depth:
                self.text_depth += 1
            elif self.post and "tgme_widget_message_text" in attrs.get("class", "").split():
                self.text_depth = 1
        elif self.post and tag == "time" and attrs.get("datetime"):
            self.post["published"] = attrs["datetime"]
        elif self.text_depth and tag == "br":
            self.post["body"].append("\n")

    def handle_endtag(self, tag):
        if tag == "div" and self.text_depth:
            self.text_depth -= 1

    def handle_data(self, data):
        if self.text_depth:
            self.post["body"].append(data)


def _parse_binance(source, payload, observed_ms):
    parser = _TelegramPosts()
    parser.feed(payload.decode("utf-8", errors="replace"))
    result = []
    for post in parser.items:
        try:
            published = datetime.fromisoformat(post["published"])
        except ValueError:
            continue
        lines = [line.strip() for line in "".join(post["body"]).splitlines() if line.strip()]
        if lines and published.tzinfo is not None:
            permalink = f"https://t.me/{BINANCE_CHANNEL}/{post['id']}"
            title = lines[0][:300]
            result.append(_item(source, title, permalink, published, observed_ms, "\n".join(lines)[:600]))
    if not result:
        raise ValueError("no readable Binance channel posts")
    return result


def _fetch(source, url, validators):
    headers = {"User-Agent": "Mozilla/5.0"}
    for key, field in (("If-None-Match", "etag"), ("If-Modified-Since", "last_modified")):
        if validators.get(field):
            headers[key] = validators[field]
    try:
        response = requests.get(url, headers=headers, timeout=(4, 8), proxies=PROXY)
        now = time.time_ns() // 1_000_000
        if response.status_code == 304:
            return None, {"checked_ms": now, "status": "unchanged", **validators}
        response.raise_for_status()
        parser = _parse_binance if source == BINANCE_SOURCE else _parse_rss
        items = [item for item in parser(source, response.content, now) if item["published_ms"] <= now]
        published = max((item["published_ms"] for item in items), default=None)
        return items, {
            "checked_ms": now, "status": "ok", "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"), "item_count": len(items),
            "latest_published_ms": published,
        }
    except (requests.RequestException, ET.ParseError, ValueError) as exc:
        log(f"{source} 抓取失败 ({type(exc).__name__})")
        return None, {"checked_ms": time.time_ns() // 1_000_000,
                      "status": "error", "error": type(exc).__name__}


def analyze_sentiment(text):
    """分析文本情绪"""
    text_lower = text.lower()
    
    bullish_count = sum(1 for kw in SENTIMENT_KEYWORDS['bullish'] if kw.lower() in text_lower)
    bearish_count = sum(1 for kw in SENTIMENT_KEYWORDS['bearish'] if kw.lower() in text_lower)
    neutral_count = sum(1 for kw in SENTIMENT_KEYWORDS['neutral'] if kw.lower() in text_lower)
    
    if bullish_count > bearish_count * 1.5:
        return 'bullish', '🟢 利好'
    elif bearish_count > bullish_count * 1.5:
        return 'bearish', '🔴 利空'
    else:
        return 'neutral', '⚪ 中性'

def generate_analysis(news_items, market_data=None):
    """生成新闻分析报告"""
    time_str = get_beijing_time().strftime("%Y-%m-%d %H:%M:%S")
    
    # 统计情绪
    sentiment_counts = {'bullish': 0, 'bearish': 0, 'neutral': 0}
    analyzed_news = []
    
    for news in news_items:
        sentiment, emoji = analyze_sentiment(news.get('title', '') + ' ' + news.get('content', ''))
        sentiment_counts[sentiment] += 1
        analyzed_news.append({
            'title': news.get('title', ''),
            'url': news.get('url', ''),
            'sentiment': sentiment,
            'emoji': emoji,
            'time': news.get('time', ''),
            'source': news.get('source', '未知'),
            'observed_ms': news.get('observed_ms'),
            'published_ms': news.get('published_ms'),
            'content': news.get('content', ''),
        })

    try:
        trade_db.init_db()
        added = trade_db.insert_news(analyzed_news)
        log(f"💾 新闻入库：新增 {added} 条")
    except Exception as e:
        log(f"⚠️ 新闻入库失败：{e}")
    
    # 综合情绪判断
    if sentiment_counts['bullish'] > sentiment_counts['bearish'] * 1.5:
        overall = "🟢 偏多"
    elif sentiment_counts['bearish'] > sentiment_counts['bullish'] * 1.5:
        overall = "🔴 偏空"
    else:
        overall = "⚪ 震荡"
    
    # 生成报告
    lines = [
        f"# 📰 加密货币新闻分析",
        f"",
        f"**生成时间：** {time_str}",
        f"",
        f"---",
        f"",
        f"## 📊 市场情绪统计",
        f"",
        f"| 情绪 | 数量 |",
        f"| :---: | :---: |",
        f"| 🟢 利好 | {sentiment_counts['bullish']} |",
        f"| 🔴 利空 | {sentiment_counts['bearish']} |",
        f"| ⚪ 中性 | {sentiment_counts['neutral']} |",
        f"",
        f"**整体情绪：** {overall}",
        f"",
        f"---",
        f"",
        f"## 📰 重要新闻",
        f"",
    ]
    
    for news in analyzed_news[:5]:  # 只显示前 5 条
        source_tag = f"[{news.get('source', '未知')}]"
        lines.append(f"{news['emoji']} {source_tag} **{news['title']}**")
        lines.append(f"   [链接]({news['url']})")
        lines.append("")
    
    # 结合技术面（如果有市场数据）
    if market_data:
        lines.extend([
            f"---",
            f"",
            f"## 📈 技术面结合",
            f"",
        ])
        
        for coin in market_data[:5]:
            symbol = coin.get('symbol', 'UNKNOWN')
            rsi = coin.get('rsi')
            change = coin.get('change_24h', 0)
            
            # 综合判断
            tech_signal = "⚪ 中性"
            if rsi and rsi < 30:
                tech_signal = "🟢 超卖"
            elif rsi and rsi > 70:
                tech_signal = "🔴 超买"
            
            lines.append(f"**{symbol}**: {tech_signal} (RSI: {rsi if rsi else '--'}, 24h: {change:+.2f}%)")
        
        lines.append("")
    
    # 操作建议
    lines.extend([
        f"---",
        f"",
        f"## 💡 综合建议",
        f"",
        f"⚠️ 仅供参考，请自行判断风险！",
        f"",
    ])
    
    content = "\n".join(lines)
    
    # 保存到文件（原子写入）
    tmp_content = str(ANALYSIS_FILE) + '.tmp'
    with open(tmp_content, 'w') as f:
        f.write(content)
    os.replace(tmp_content, str(ANALYSIS_FILE))
    
    # 保存原始新闻数据（原子写入，防止文件写一半被截断）
    tmp_file = str(NEWS_FILE) + '.tmp'
    with open(tmp_file, 'w') as f:
        json.dump({
            'timestamp': get_beijing_time().isoformat(),
            'news': analyzed_news,
            'sentiment_counts': sentiment_counts,
            'overall': overall
        }, f, indent=2, ensure_ascii=False)
    os.replace(tmp_file, str(NEWS_FILE))
    
    return content

def main():
    trade_db.init_db()
    try:
        previous = json.loads(NEWS_FILE.read_text(encoding="utf-8"))["news"]
    except (OSError, ValueError, KeyError, TypeError):
        previous = []
    validators = trade_db.news_source_validators()
    previous_by_source = {
        source: [item for item in previous if item.get("source") == source] for source in SOURCES
    }
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        pending = {
            source: pool.submit(_fetch, source, url, validators.get(source, {}) if previous_by_source[source] else {})
            for source, url in SOURCES.items()
        }
        results = {source: task.result() for source, task in pending.items()}
    all_news = {}
    for source, (items, health) in results.items():
        log(f"{source}: {health['status']} ({len(items) if items is not None else '沿用上次快照'})")
        for item in previous_by_source[source] if items is None else items:
            all_news.setdefault((source, item["title"]), item)
    if any(health["status"] != "error" for _, health in results.values()):
        generate_analysis(sorted(all_news.values(), key=lambda item: item["time"], reverse=True))
    for source, (_, health) in results.items():
        trade_db.record_news_source(source, health)
    return 0 if any(health["status"] != "error" for _, health in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
