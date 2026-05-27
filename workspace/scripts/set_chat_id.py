#!/usr/bin/env python3
"""
set_chat_id.py - 切换飞书卡片推送目标群

用法：
    python3 set_chat_id.py oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
    python3 set_chat_id.py --by-name "操控市场的大手"   # 按群名模糊匹配
    python3 set_chat_id.py --show                       # 只显示当前 chat_id

会同时更新：
    - feishu_chat_id.txt           （所有 query_*.py 工具用的目标群）
    - crypto_signal_monitor.py     （monitor 自动播报的目标群，需重启 kj 生效）
"""
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

CHAT_ID_FILE = SCRIPT_DIR / "feishu_chat_id.txt"
MONITOR_PY = SCRIPT_DIR / "crypto_signal_monitor.py"


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


def _update_monitor_py(new_chat_id: str) -> bool:
    """同步更新 monitor 里 hardcoded 的 feishu_chat_id"""
    if not MONITOR_PY.exists():
        return False
    content = MONITOR_PY.read_text(encoding="utf-8")
    new_content = re.sub(
        r'("feishu_chat_id"\s*:\s*")[^"]+(")',
        rf'\g<1>{new_chat_id}\g<2>',
        content,
        count=1,
    )
    if new_content == content:
        return False
    MONITOR_PY.write_text(new_content, encoding="utf-8")
    return True


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
    
    if _update_monitor_py(new_id):
        print(f"✅ 已同步更新 crypto_signal_monitor.py 中的 feishu_chat_id")
        print(f"⚠️ 需要执行 kj 重启 monitor，自动播报才会切换到新群")
    else:
        print(f"⚠️ crypto_signal_monitor.py 中未找到 feishu_chat_id 字段（手动检查）")
    
    print()
    print("立刻验证：")
    print("  python3 query_price.py --push   # 应推送到新群")


if __name__ == "__main__":
    main()
