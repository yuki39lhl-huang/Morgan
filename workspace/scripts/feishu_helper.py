#!/usr/bin/env python3
"""
飞书卡片推送公用模块 - 供所有 query_*.py 工具调用
直接复用 crypto_signal_monitor.py 中的飞书配置
"""
import json
import os
import time
import requests
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

FEISHU_APP_ID = "cli_a92eff25e5789cbd"
FEISHU_APP_SECRET = "xlzBVRbYq4ng72a60TbHMhMe8Akc3ZqD"
FEISHU_TOKEN_CACHE = SCRIPT_DIR / ".feishu_token_cache.json"

CHAT_ID_FILE = SCRIPT_DIR / "feishu_chat_id.txt"


def _load_chat_id() -> str:
    """从 crypto_signal_monitor.py 的 CONFIG 中读 chat_id（首次需要从主脚本配置中获取）"""
    if CHAT_ID_FILE.exists():
        return CHAT_ID_FILE.read_text(encoding="utf-8").strip()
    try:
        with open(SCRIPT_DIR / "crypto_signal_monitor.py", "r", encoding="utf-8") as f:
            for line in f:
                if '"feishu_chat_id"' in line and ":" in line:
                    val = line.split(":", 1)[1].strip().rstrip(",").strip().strip('"')
                    if val:
                        CHAT_ID_FILE.write_text(val, encoding="utf-8")
                        return val
    except Exception:
        pass
    return ""


def get_token(force_refresh: bool = False):
    now = time.time()
    if not force_refresh and FEISHU_TOKEN_CACHE.exists():
        try:
            cache = json.loads(FEISHU_TOKEN_CACHE.read_text(encoding="utf-8"))
            if now - cache.get("time", 0) < 7000:
                return cache.get("token")
        except Exception:
            pass

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}
    try:
        r = requests.post(url, json=payload, timeout=10, proxies=None)
        data = r.json()
        if data.get("code") == 0:
            token = data.get("tenant_access_token")
            FEISHU_TOKEN_CACHE.write_text(
                json.dumps({"token": token, "time": now}), encoding="utf-8"
            )
            return token
    except Exception as e:
        print(f"[helper] 获取飞书 token 失败: {e}")
    return None


def push_card(title: str, elements: list, template: str = "blue") -> bool:
    """推送飞书卡片消息到目标群"""
    chat_id = _load_chat_id()
    if not chat_id:
        print("[helper] 找不到 feishu_chat_id，无法发送")
        return False

    token = get_token()
    if not token:
        print("[helper] 飞书 token 获取失败")
        return False

    card_content = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card_content, ensure_ascii=False),
    }
    try:
        r = requests.post(
            url,
            headers=headers,
            data=json.dumps(payload, ensure_ascii=False),
            timeout=10,
            proxies=None,
        )
        data = r.json()
        if data.get("code") == 0:
            print(f"[PUSH] OK -> chat_id={chat_id}")
            return True
        if data.get("code") == 99991663:
            token = get_token(force_refresh=True)
            if token:
                headers["Authorization"] = f"Bearer {token}"
                r = requests.post(
                    url,
                    headers=headers,
                    data=json.dumps(payload, ensure_ascii=False),
                    timeout=10,
                    proxies=None,
                )
                if r.json().get("code") == 0:
                    print(f"[PUSH] OK -> chat_id={chat_id}")
                    return True
        print(f"[PUSH] FAILED chat_id={chat_id} reason={data}")
    except Exception as e:
        print(f"[PUSH] FAILED chat_id={chat_id} exception={e}")
    return False


def md(text: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def hr() -> dict:
    return {"tag": "hr"}


def note(text: str) -> dict:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": text}]}


def col(text: str, weight: int = 1) -> dict:
    """单列容器（用于 row 内）"""
    return {
        "tag": "column",
        "width": "weighted",
        "weight": weight,
        "vertical_align": "top",
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text}}],
    }


def row(*columns: dict) -> dict:
    """分栏行（飞书原生 column_set，比 markdown 表格美观）"""
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "default",
        "columns": list(columns),
    }


def field(label: str, value: str, weight: int = 1) -> dict:
    """字段列（标签灰色、值高亮）—— 用于 row(...) 内"""
    return col(f"<font color='grey'>{label}</font>\n**{value}**", weight)


def kv_block(items: list, columns: int = 2) -> dict:
    """
    生成 key-value 网格区块（飞书原生 div 的 fields 字段）
    
    items: [(label, value), ...]
    columns: 每行几列（飞书最多 2）
    """
    return {
        "tag": "div",
        "fields": [
            {
                "is_short": True,
                "text": {"tag": "lark_md", "content": f"**{label}**\n{value}"},
            }
            for label, value in items
        ],
    }
