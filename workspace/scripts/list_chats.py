#!/usr/bin/env python3
"""
list_chats.py - 列出飞书机器人加入的所有群（用于切换推送目标）

用法：
    python3 list_chats.py            # 列出所有群 + 当前推送目标
    python3 list_chats.py --json     # JSON 格式输出
"""
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))


def main():
    json_mode = "--json" in sys.argv
    
    from feishu_helper import get_token, _load_chat_id
    import requests
    
    token = get_token()
    if not token:
        print("[ERROR] 飞书 token 获取失败", file=sys.stderr)
        sys.exit(1)
    
    current_chat_id = _load_chat_id()
    
    chats = []
    page_token = None
    while True:
        url = "https://open.feishu.cn/open-apis/im/v1/chats?page_size=100"
        if page_token:
            url += f"&page_token={page_token}"
        try:
            r = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
                proxies=None,
            )
            data = r.json()
            if data.get("code") != 0:
                print(f"[ERROR] API 调用失败: {data}", file=sys.stderr)
                sys.exit(2)
            d = data.get("data", {})
            chats.extend(d.get("items", []))
            page_token = d.get("page_token")
            if not d.get("has_more") or not page_token:
                break
        except Exception as e:
            print(f"[ERROR] 请求异常: {e}", file=sys.stderr)
            sys.exit(3)
    
    if json_mode:
        print(json.dumps(chats, ensure_ascii=False, indent=2))
        return
    
    print(f"\n📋 机器人已加入 {len(chats)} 个群\n")
    print(f"{'当前':<4} {'序号':<4} {'群名':<30} {'chat_id'}")
    print("─" * 100)
    for i, chat in enumerate(chats, 1):
        cid = chat.get("chat_id", "")
        name = chat.get("name", "(未命名)")[:28]
        is_current = "👉" if cid == current_chat_id else "  "
        print(f"{is_current}   #{i:<3} {name:<30} {cid}")
    print()
    print("切换推送目标群：")
    print(f"  python3 set_chat_id.py <chat_id>")
    print(f"  例：python3 set_chat_id.py {chats[0]['chat_id'] if chats else 'oc_xxx'}")
    print()


if __name__ == "__main__":
    main()
