#!/usr/bin/env python3
"""
加密货币新闻抓取（双源方案）
📰 抓取 CoinDesk（市场新闻）+ 币安官方公告
🧠 分析情绪（利好/利空）
📊 结合技术面给出综合建议
"""

import requests
import json
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta

# 代理配置
PROXY = {'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}

NEWS_FILE = Path("/root/.openclaw/workspace/scripts/crypto_news.json")
ANALYSIS_FILE = Path("/root/.openclaw/workspace/scripts/crypto_news_analysis.md")

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

def fetch_coindesk():
    """抓取 CoinDesk 新闻（市场新闻/分析）"""
    try:
        import xml.etree.ElementTree as ET
        
        url = "https://www.coindesk.com/arc/outboundfeeds/rss/"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=15, proxies=PROXY)
        response.raise_for_status()
        
        try:
            root = ET.fromstring(response.content)
            news_items = []
            
            for item in root.findall('.//item')[:10]:
                title = item.find('title').text if item.find('title') is not None else ''
                link = item.find('link').text if item.find('link') is not None else ''
                pub_date = item.find('pubDate').text if item.find('pubDate') is not None else ''
                
                try:
                    dt = datetime.strptime(pub_date, '%a, %d %b %Y %H:%M:%S %z')
                    dt_beijing = dt.astimezone(timezone(timedelta(hours=8)))
                    time_str = dt_beijing.strftime('%Y-%m-%d %H:%M')
                except:
                    time_str = pub_date
                
                news_items.append({
                    'title': title,
                    'url': link,
                    'time': time_str,
                    'source': 'CoinDesk',
                    'content': ''
                })
            
            log(f"✅ CoinDesk 抓取成功：{len(news_items)} 条")
            return news_items
        except ET.ParseError as e:
            log(f"❌ XML 解析失败：{e}")
            return []
            
    except Exception as e:
        log(f"❌ CoinDesk 抓取失败：{e}")
        return []


def fetch_coingecko_news():
    """抓取 CoinGecko 新闻（双源备份）"""
    try:
        url = "https://api.coingecko.com/api/v3/news"
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'x-cg-demo-api-key': 'CG-DZoCE8UMF3FWpeYhBMvqGq4g'
        }
        response = requests.get(url, headers=headers, timeout=15, proxies=PROXY)
        response.raise_for_status()
        
        data = response.json()
        if not isinstance(data, dict) or 'data' not in data:
            log(f"⚠️ CoinGecko 新闻 API 格式异常")
            return []
        
        articles = data.get('data', [])[:10]
        news_items = []
        
        for article in articles:
            title = article.get('title', '')
            url_link = article.get('url', '')
            
            try:
                dt = datetime.fromtimestamp(article.get('updated_at', 0), timezone.utc)
                dt_beijing = dt.astimezone(timezone(timedelta(hours=8)))
                time_str = dt_beijing.strftime('%Y-%m-%d %H:%M')
            except:
                time_str = datetime.now().strftime('%Y-%m-%d %H:%M')
            
            news_items.append({
                'title': title,
                'url': url_link,
                'time': time_str,
                'source': 'CoinGecko',
                'content': article.get('description', '')
            })
        
        log(f"✅ CoinGecko 抓取成功：{len(news_items)} 条")
        return news_items
        
    except Exception as e:
        log(f"❌ CoinGecko 抓取失败：{e}")
        return []


def fetch_binance_announcements():
    """抓取币安官方公告（上币/下架/维护等）"""
    try:
        # 币安公告 API（正确参数：pageNot 不是 page）
        url = "https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query"
        params = {
            'catalogId': 48,  # 全部公告
            'pageNo': 1,
            'pageSize': 10
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=15, proxies=PROXY)
        response.raise_for_status()
        
        data = response.json()
        if data.get('code') == '000000' and data.get('data'):
            articles = data['data'].get('articles', [])
            all_news = []
            
            for article in articles[:10]:
                title = article.get('title', '')
                article_id = article.get('id')
                publish_date = article.get('publishDate')
                
                # 转换时间戳为北京时间
                time_str = datetime.now().strftime('%Y-%m-%d %H:%M')
                if publish_date:
                    try:
                        dt = datetime.fromtimestamp(publish_date / 1000, timezone.utc)
                        dt_beijing = dt.astimezone(timezone(timedelta(hours=8)))
                        time_str = dt_beijing.strftime('%Y-%m-%d %H:%M')
                    except:
                        pass
                
                link = f"https://www.binance.com/zh-CN/support/announcement/{article_id}"
                
                all_news.append({
                    'title': title,
                    'url': link,
                    'time': time_str,
                    'source': '币安',
                    'content': ''
                })
            
            log(f"✅ 币安公告抓取成功：{len(all_news)} 条")
            return all_news
        else:
            log(f"⚠️ 币安公告 API 返回异常：{data.get('code')}")
            return []
        
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
    
    # CoinDesk（市场新闻）
    coindesk_news = fetch_coindesk()
    all_news.extend(coindesk_news)
    
    # CoinGecko（双源备份）
    coingecko_news = fetch_coingecko_news()
    all_news.extend(coingecko_news)
    
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
    
    # 如果没有新闻，用示例数据测试（带当前时间）
    if not all_news:
        log("⚠️ 无新闻数据，使用示例数据")
        now = get_beijing_time().strftime('%Y-%m-%d %H:%M')
        all_news = [
            {'title': 'Bitcoin Surges Past $70,000 as Institutional Adoption Grows', 'url': 'https://example.com/1', 'time': now, 'source': 'CoinDesk'},
            {'title': 'Binance Lists New Token XYZ', 'url': 'https://example.com/2', 'time': now, 'source': '币安'},
            {'title': 'Ethereum Network Upgrade Successfully Completed', 'url': 'https://example.com/3', 'time': now, 'source': 'CoinDesk'},
        ]
    
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
