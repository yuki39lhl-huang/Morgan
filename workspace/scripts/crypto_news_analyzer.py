#!/usr/bin/env python3
"""
加密货币新闻/热点分析模块
📰 分析社区情绪、新闻、热点
📊 结合技术面给出综合建议
"""

import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

NEWS_FILE = Path("/root/.openclaw/workspace/scripts/crypto_news.json")
ANALYSIS_FILE = Path("/root/.openclaw/workspace/scripts/crypto_analysis.md")

def get_beijing_time():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))

# 社区情绪指标（需要手动更新或 API）
SENTIMENT_KEYWORDS = {
    'bullish': ['暴涨', '突破', '利好', '上涨', '买入', 'bull', 'moon', 'rally'],
    'bearish': ['暴跌', '崩盘', '利空', '下跌', '卖出', 'bear', 'crash', 'dump'],
    'neutral': ['震荡', '盘整', '观望', 'sideways', 'consolidation']
}

def analyze_news(news_items):
    """分析新闻情绪"""
    sentiment = {'bullish': 0, 'bearish': 0, 'neutral': 0}
    
    for news in news_items:
        title = news.get('title', '').lower()
        content = news.get('content', '').lower()
        text = title + ' ' + content
        
        for keyword in SENTIMENT_KEYWORDS['bullish']:
            if keyword in text:
                sentiment['bullish'] += 1
                break
        for keyword in SENTIMENT_KEYWORDS['bearish']:
            if keyword in text:
                sentiment['bearish'] += 1
                break
        for keyword in SENTIMENT_KEYWORDS['neutral']:
            if keyword in text:
                sentiment['neutral'] += 1
                break
    
    return sentiment

def generate_analysis(market_data, sentiment, news_items):
    """生成综合分析报告"""
    time_str = get_beijing_time().strftime("%Y-%m-%d %H:%M:%S")
    
    lines = [
        f"# 📊 加密货币综合分析报告",
        f"",
        f"**生成时间：** {time_str}",
        f"",
        f"---",
        f"",
        f"## 📰 市场情绪",
        f"",
        f"| 情绪 | 数量 |",
        f"| :--- | :--- |",
        f"| 🟢 利好 | {sentiment.get('bullish', 0)} |",
        f"| 🔴 利空 | {sentiment.get('bearish', 0)} |",
        f"| ⚪ 中性 | {sentiment.get('neutral', 0)} |",
        f"",
    ]
    
    # 综合建议
    if sentiment.get('bullish', 0) > sentiment.get('bearish', 0) * 1.5:
        overall = "🟢 偏多"
    elif sentiment.get('bearish', 0) > sentiment.get('bullish', 0) * 1.5:
        overall = "🔴 偏空"
    else:
        overall = "⚪ 震荡"
    
    lines.extend([
        f"**整体情绪：** {overall}",
        f"",
        f"---",
        f"",
        f"## 📈 技术面分析",
        f"",
    ])
    
    # 分析每个币种
    for coin in market_data:
        symbol = coin['symbol']
        rsi = coin.get('rsi')
        change = coin.get('change_24h', 0)
        
        # 技术面判断
        signals = []
        if rsi:
            if rsi < 30:
                signals.append("超卖")
            elif rsi > 70:
                signals.append("超买")
            elif rsi < 40:
                signals.append("偏弱")
            elif rsi > 60:
                signals.append("偏强")
        
        if change > 5:
            signals.append("大涨")
        elif change < -5:
            signals.append("大跌")
        elif change > 2:
            signals.append("上涨")
        elif change < -2:
            signals.append("下跌")
        
        signal_str = ", ".join(signals) if signals else "中性"
        
        lines.append(f"**{symbol}**: {signal_str}")
    
    lines.extend([
        f"",
        f"---",
        f"",
        f"## 💡 操作建议",
        f"",
        f"⚠️ 仅供参考，请自行判断风险！",
        f"",
    ])
    
    return "\n".join(lines)

if __name__ == "__main__":
    print("新闻分析模块已加载")
