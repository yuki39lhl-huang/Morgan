#!/usr/bin/env python3
"""
ai_layer.py — AI 层

职责：DeepSeek 趋势预测（AIPredictor）、AI 方向输出归一化。
依赖方向：仅依赖 config / requests，禁止反向依赖。
"""
import json
import logging
import re

import requests
from typing import Optional

from config import CONFIG, get_config

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 十、AI 预测（DeepSeek，无调用次数/间隔限制）
# ═══════════════════════════════════════════════════════════════
class AIPredictor:

    def predict(self, symbol: str, direction: str, ind: dict, price_data: dict, fg: int = 50) -> Optional[dict]:
        prompt = f"""你是加密货币分析师，基于以下数据预测{symbol}未来 1 小时趋势：

【实时数据】
当前价格：{price_data.get('price')}
24h 涨跌：{price_data.get('change_24h')}%
恐惧贪婪指数：{fg}

【技术指标】
RSI：{ind.get('rsi', 0):.1f}
ATR%：{ind.get('atr_pct', 0):.3f}
价格 vs 支撑位：{((price_data.get('price',0)-ind.get('support',1))/ind.get('support',1)*100):.2f}%
价格 vs 阻力位：{((price_data.get('price',0)-ind.get('resistance',1))/ind.get('resistance',1)*100):.2f}%

只返回 JSON，不要任何解释：
{{"direction":"做多/做空/震荡","confidence":0,"reason":"一句话"}}"""

        try:
            headers = {
                "Authorization": f"Bearer {CONFIG['deepseek_api_key']}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": CONFIG["deepseek_model"],
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "thinking": {"type": "disabled"},
            }
            r = requests.post(
                CONFIG["deepseek_url"],
                headers=headers,
                json=payload,
                timeout=20,
                proxies={}  # DeepSeek 国内直连，不需要代理
            )
            content = r.json()["choices"][0]["message"]["content"].strip()
            # 提取 JSON
            match = re.search(r'\{.*?\}', content, re.DOTALL)
            if match:
                result = json.loads(match.group())
                log.info(f"🤖 AI 预测 {symbol}: {result}")
                return result
        except Exception as e:
            log.warning(f"⚠️ AI 预测失败 {symbol}: {e}")
        return None

    def scan_all(self, symbols: list, prices: dict, indicators: dict, fg: int = 50) -> dict:
        """
        整点全量 AI 观点扫描（方案B）：对每个有价格+指标数据的币做一次趋势预测。
        仅收集观点供后续评估 AI 与数学信号一致性，不做任何交易决策。
        返回 {symbol: {"direction","confidence","reason"}}，失败/无数据的币不包含。
        """
        results = {}
        for sym in symbols:
            ind = indicators.get(sym)
            if not ind:
                continue
            price = prices.get(sym)
            if not price:
                continue
            price_data = {"price": price, "change_24h": ind.get("change_24h", 0)}
            result = self.predict(sym, "", ind, price_data, fg)
            if result:
                results[sym] = {
                    "direction": _normalize_ai_direction(result.get("direction", "")),
                    "confidence": _normalize_ai_confidence(result.get("confidence", 0)),
                    "reason": str(result.get("reason", ""))[:80],
                }
        return results


def _normalize_ai_direction(s) -> str:
    """归一化 AI 输出方向，容忍大模型措辞漂移（看多/做多/bull 等 → LONG）。"""
    if not s:
        return ""
    s = str(s).strip().lower()
    if any(k in s for k in ("做多", "看多", "买", "bull", "long", "up")):
        return "LONG"
    if any(k in s for k in ("做空", "看空", "卖", "bear", "short", "down")):
        return "SHORT"
    if any(k in s for k in ("震荡", "横盘", "观望", "neutral", "side", "flat")):
        return "震荡"
    return ""


def _normalize_ai_confidence(value) -> float:
    """归一化 AI 置信度为 0~1 制，容忍模型输出量纲漂移（0.7 或 70 都视为 0.7）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v > 1.0:
        v = v / 100.0
    return max(0.0, min(1.0, v))
