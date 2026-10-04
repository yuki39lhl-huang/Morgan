#!/usr/bin/env python3
"""
market_ext.py — 外部市场数据（只记录，不进评分）

  - OKX 永续：真实资金费率、持仓量、近 1 小时持仓量变化
  - CoinGecko /global：BTC 市值占比、全市场市值 24h 变化

主循环用的资金费率来自测试网，不是真实市场；这里的数据先落库，
攒够样本后再用离线回归决定要不要进评分。
本机直连 OKX / CoinGecko 不通（国内网络），统一走 config 里的代理。
"""
import logging
import time

import requests

import config

log = logging.getLogger(__name__)

OKX = "https://www.okx.com/api/v5"
COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"
TIMEOUT = 8


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.proxies = config.get_config().get("proxies") or {}
    return s


def _okx(s: requests.Session, path: str, params: dict):
    r = s.get(f"{OKX}{path}", params=params, timeout=TIMEOUT)
    data = r.json()
    if data.get("code") != "0":
        raise RuntimeError(f"{path} {data.get('code')} {data.get('msg')}")
    return data.get("data") or []


def _symbol(s: requests.Session, sym: str) -> dict:
    inst = f"{sym}-USDT-SWAP"
    out = {"funding": None, "oi_usd": None, "oi_chg_1h": None}
    try:
        out["funding"] = float(_okx(s, "/public/funding-rate", {"instId": inst})[0]["fundingRate"])
    except Exception as e:
        log.warning(f"⚠️ OKX 资金费率失败 {sym}: {e}")
    try:
        # [ts, oi(张), oi(币), oi(USD)]，新的在前
        hist = _okx(s, "/rubik/stat/contracts/open-interest-history",
                    {"instId": inst, "period": "1H", "limit": 2})
        if hist:
            now_usd = float(hist[0][3])
            out["oi_usd"] = now_usd
            if len(hist) > 1 and float(hist[1][3]) > 0:
                out["oi_chg_1h"] = now_usd / float(hist[1][3]) - 1
    except Exception as e:
        log.warning(f"⚠️ OKX 持仓量失败 {sym}: {e}")
    return out


def _global(s: requests.Session) -> dict:
    try:
        key = config.get_config().get("coingecko_api_key", "")
        r = s.get(COINGECKO_GLOBAL, headers={"x-cg-demo-api-key": key}, timeout=TIMEOUT)
        g = r.json()["data"]
        return {"btc_dom": float(g["market_cap_percentage"]["btc"]),
                "mcap_chg_24h": float(g["market_cap_change_percentage_24h_usd"])}
    except Exception as e:
        log.warning(f"⚠️ CoinGecko global 失败: {e}")
        return {}


def fetch_snapshot(symbols: list) -> dict:
    """返回 {sym: {funding, oi_usd, oi_chg_1h}, "_global": {btc_dom, mcap_chg_24h}}。"""
    s = _session()
    snap = {}
    for sym in symbols:
        snap[sym] = _symbol(s, sym)
        time.sleep(0.5)  # OKX rubik 接口限频约 5 次/2 秒
    snap["_global"] = _global(s)
    return snap


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_snapshot(["BTC", "DOT"]), indent=2))
