#!/usr/bin/env python3
"""
set_chat_id.py - 切换飞书卡片推送目标群

用法：
    python3 set_chat_id.py oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
    python3 set_chat_id.py --by-name "操控市场的大手"   # 按群名模糊匹配
    python3 set_chat_id.py --show                       # 只显示当前 chat_id

会同时更新：
    - secrets.json 的 feishu_chat_id    （统一配置入口，monitor / 工具共用）
    - feishu_chat_id.txt                （历史兼容，供旧工具读取）
"""
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

CHAT_ID_FILE = SCRIPT_DIR / "feishu_chat_id.txt"
SECRETS_FILE = SCRIPT_DIR / "secrets.json"


def _validate(cid: str) -> bool:
    return bool(re.fullmatch(r"oc_[A-Za-z0-9]{20,}", cid))


def _find_by_name(name_keyword: str) -> str:
    """通过群名模糊匹配找 chat_id"""
    from feishu_helper import get_token
    import requests
    token = get_token()
    if not token:
        print("[ERROR] 飞书 token 获取失败", file=sys.stderr)
        sys.exit(1)
    matched = []
    page_token = None
    while True:
        url = "https://open.feishu.cn/open-apis/im/v1/chats?page_size=100"
        if page_token:
            url += f"&page_token={page_token}"
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
            proxies=None,
        )
        data = r.json()
        if data.get("code") != 0:
            print(f"[ERROR] API 失败: {data}", file=sys.stderr)
            sys.exit(2)
        d = data.get("data", {})
        for chat in d.get("items", []):
            n = chat.get("name", "")
            if name_keyword in n:
                matched.append((n, chat.get("chat_id")))
        page_token = d.get("page_token")
        if not d.get("has_more") or not page_token:
            break
    if not matched:
        print(f"[ERROR] 没有找到名字含「{name_keyword}」的群", file=sys.stderr)
        sys.exit(3)
    if len(matched) > 1:
        print(f"⚠️ 找到多个匹配，请用具体 chat_id 指定：")
        for n, cid in matched:
            print(f"  {n:<30} {cid}")
        sys.exit(4)
    n, cid = matched[0]
    print(f"✅ 匹配到群：{n} → {cid}")
    return cid


def _update_secrets_json(new_chat_id: str) -> bool:
    """把新 chat_id 写入 secrets.json（统一配置入口）"""
    if not SECRETS_FILE.exists():
        return False
    try:
        data = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        data["feishu_chat_id"] = new_chat_id
        SECRETS_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return True
    except Exception:
        return False


def main():
    args = sys.argv[1:]
    
    current = CHAT_ID_FILE.read_text(encoding="utf-8").strip() if CHAT_ID_FILE.exists() else "(未设置)"
    
    if not args or "--show" in args:
        print(f"📌 当前推送目标 chat_id: {current}")
        print(f"   存储位置: {CHAT_ID_FILE}")
        print()
        print("用法：")
        print("  python3 set_chat_id.py oc_xxxxxxxxxxxxxxxxxxxx")
        print("  python3 set_chat_id.py --by-name 操控市场的大手")
        print("  python3 list_chats.py           （列出所有群）")
        return
    
    if args[0] == "--by-name":
        if len(args) < 2:
            print("[ERROR] --by-name 需要群名关键字", file=sys.stderr)
            sys.exit(5)
        new_id = _find_by_name(" ".join(args[1:]))
    else:
        new_id = args[0].strip()
        if not _validate(new_id):
            print(f"[ERROR] chat_id 格式不对：{new_id}（应该以 oc_ 开头）", file=sys.stderr)
            sys.exit(6)
    
    if new_id == current:
        print(f"⚠️ 新 chat_id 与当前相同，无需更改：{new_id}")
        return
    
    CHAT_ID_FILE.write_text(new_id, encoding="utf-8")
    print(f"✅ 已更新 feishu_chat_id.txt")
    print(f"   旧: {current}")
    print(f"   新: {new_id}")
    
    if _update_secrets_json(new_id):
        print(f"✅ 已同步更新 secrets.json 中的 feishu_chat_id")
        print(f"⚠️ 长驻进程下次 get_config() 自动生效；monitor 需 kj 重启")
    else:
        print(f"⚠️ 更新 secrets.json 失败（手动检查）")
    
    print()
    print("立刻验证：")
    print("  python3 query_price.py --push   # 应推送到新群")


if __name__ == "__main__":
    main()
