#!/usr/bin/env python3
"""
飞书卡片推送公用模块 - 供所有 query_*.py / monitor / reminder 等脚本调用

凭证与推送目标统一从 config.py（config.json + secrets.json）读取，
任何脚本都不再自带飞书凭证 / 自实现发送逻辑。
"""
import json
import time
import requests

import config

SCRIPT_DIR = config.SCRIPT_DIR

FEISHU_API_BASE = "https://open.feishu.cn"
FEISHU_TOKEN_CACHE = SCRIPT_DIR / ".feishu_token_cache.json"
CHAT_ID_FILE = SCRIPT_DIR / "feishu_chat_id.txt"


def _load_chat_id() -> str:
    """
    读取群推送目标 chat_id。
    优先级：secrets.json > feishu_chat_id.txt（历史兼容），
    找到后同步回写到 txt 供其它工具使用。
    """
    cfg = config.get_config()
    chat_id = cfg.get("feishu_chat_id", "")
    if chat_id:
        try:
            if not CHAT_ID_FILE.exists() or CHAT_ID_FILE.read_text(encoding="utf-8").strip() != chat_id:
                CHAT_ID_FILE.write_text(chat_id, encoding="utf-8")
        except Exception:
            pass
        return chat_id
    if CHAT_ID_FILE.exists():
        return CHAT_ID_FILE.read_text(encoding="utf-8").strip()
    return ""


def get_token(force_refresh: bool = False):
    """获取飞书 tenant_access_token（带本地缓存）。"""
    creds = config.feishu()
    if not creds["app_id"] or not creds["app_secret"]:
        print("[helper] 缺少飞书 app_id/app_secret（检查 secrets.json）")
        return None

    now = time.time()
    if not force_refresh and FEISHU_TOKEN_CACHE.exists():
        try:
            cache = json.loads(FEISHU_TOKEN_CACHE.read_text(encoding="utf-8"))
            if now - cache.get("time", 0) < 7000:
                return cache.get("token")
        except Exception:
            pass

    url = f"{FEISHU_API_BASE}/open-apis/auth/v3/tenant_access_token/internal"
    payload = {"app_id": creds["app_id"], "app_secret": creds["app_secret"]}
    try:
        # 飞书 API 不能走代理，必须直连
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


def _post_message(url: str, headers: dict, payload: dict) -> dict:
    """发送消息，失败时用新 token 重试一次。"""
    r = requests.post(
        url,
        headers=headers,
        data=json.dumps(payload, ensure_ascii=False),
        timeout=10,
        proxies=None,
    )
    data = r.json()
    if data.get("code") == 99991663:
        new_token = get_token(force_refresh=True)
        if new_token:
            headers["Authorization"] = f"Bearer {new_token}"
            r = requests.post(
                url,
                headers=headers,
                data=json.dumps(payload, ensure_ascii=False),
                timeout=10,
                proxies=None,
            )
            data = r.json()
    return data


def _send_raw(receive_id: str, receive_id_type: str, msg_type: str, content: str) -> bool:
    token = get_token()
    if not token:
        print("[helper] 飞书 token 获取失败")
        return False
    url = f"{FEISHU_API_BASE}/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "receive_id": receive_id,
        "msg_type": msg_type,
        "content": content,
    }
    try:
        data = _post_message(url, headers, payload)
        if data.get("code") == 0:
            print(f"[PUSH] OK -> {receive_id_type}={receive_id}")
            return True
        print(f"[PUSH] FAILED {receive_id_type}={receive_id} reason={data}")
    except Exception as e:
        print(f"[PUSH] FAILED {receive_id_type}={receive_id} exception={e}")
    return False


def push_card(title: str, elements: list, template: str = "blue") -> bool:
    """推送飞书卡片消息到目标群。"""
    chat_id = _load_chat_id()
    if not chat_id:
        print("[helper] 找不到 feishu_chat_id，无法发送")
        return False

    card_content = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }
    return _send_raw(
        chat_id,
        "chat_id",
        "interactive",
        json.dumps(card_content, ensure_ascii=False),
    )


def send_text_message(text: str, receive_id: str, receive_id_type: str = "open_id") -> bool:
    """发送纯文本消息（open_id 私聊 / chat_id 群聊均可）。"""
    return _send_raw(
        receive_id,
        receive_id_type,
        "text",
        json.dumps({"text": text}, ensure_ascii=False),
    )


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
