#!/usr/bin/env python3
"""
加密货币新闻抓取
📰 抓取 CoinDesk / Cointelegraph / Decrypt（RSS）+ 币安官方公告
🧠 分析情绪（利好/利空）
📊 结合技术面给出综合建议
"""

import requests
import json
import os
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

RSS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
]


def fetch_rss(name, url, limit=15):
    """抓取 RSS 新闻源，时间换成北京时间。"""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15, proxies=PROXY)
        response.raise_for_status()
        news_items = []
        for item in ET.fromstring(response.content).findall('.//item')[:limit]:
            title = (item.findtext('title') or '').strip()
            pub = item.findtext('pubDate')
            if not title or not pub:
                continue
            try:
                dt = parsedate_to_datetime(pub).astimezone(timezone(timedelta(hours=8)))
            except (TypeError, ValueError):
                continue
            news_items.append({
                'title': title,
                'url': (item.findtext('link') or '').strip(),
                'time': dt.strftime('%Y-%m-%d %H:%M'),
                'source': name,
                'content': ''
            })
        log(f"✅ {name} 抓取成功：{len(news_items)} 条")
        return news_items
    except Exception as e:
        log(f"❌ {name} 抓取失败：{e}")
        return []


def fetch_binance_announcements():
    """抓取币安官方公告：上币、币安新闻、下架（活动类跳过）。
    catalog/list 接口不返回发布时间，改用 article/list，带 releaseDate。"""
    wanted = {48, 49, 161}
    try:
        url = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'
        }
        response = requests.get(url, headers=headers, params={'type': 1, 'pageNo': 1, 'pageSize': 10},
                                timeout=15, proxies=PROXY)
        response.raise_for_status()
        data = response.json()
        if data.get('code') != '000000' or not data.get('data'):
            log(f"⚠️ 币安公告 API 返回异常：{data.get('code')}")
            return []
        all_news = []
        for cat in data['data'].get('catalogs', []):
            if cat.get('catalogId') not in wanted:
                continue
            for article in cat.get('articles', []):
                ms = article.get('releaseDate')
                if not ms:
                    continue
                dt = datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(timezone(timedelta(hours=8)))
                all_news.append({
                    'title': article.get('title', ''),
                    'url': f"https://www.binance.com/zh-CN/support/announcement/{article.get('code', '')}",
                    'time': dt.strftime('%Y-%m-%d %H:%M'),
                    'source': '币安',
                    'content': ''
                })
        log(f"✅ 币安公告抓取成功：{len(all_news)} 条")
        return all_news
    except Exception as e:
        log(f"❌ 币安公告抓取失败：{e}")
        return []

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
            'source': news.get('source', '未知')  # ✅ 保留来源
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
    log("🚀 新闻抓取启动")
    
    # 抓取新闻
    all_news = []
    
    for name, url in RSS_FEEDS:
        all_news.extend(fetch_rss(name, url))
    
    # 币安公告（官方消息）
    binance_news = fetch_binance_announcements()
    all_news.extend(binance_news)
    
    # 去重（按标题）
    seen_titles = set()
    unique_news = []
    for news in all_news:
        title_key = news['title'][:50]  # 取前 50 字符作为 key
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique_news.append(news)
    
    all_news = unique_news
    log(f"📊 合并后新闻总数：{len(all_news)} 条")
    
    if not all_news:
        log("⚠️ 本轮没抓到新闻")

    # 生成分析报告
    analysis = generate_analysis(all_news)
    
    log("✅ 新闻分析完成")
    log(f"📄 报告已保存：{ANALYSIS_FILE}")
    
    # 输出报告内容
    print("\n" + "="*60)
    print(analysis)
    print("="*60 + "\n")

if __name__ == "__main__":
    main()
