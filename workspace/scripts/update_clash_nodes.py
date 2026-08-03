#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clash 订阅节点转换器：统一管理机场节点，消除手动复制粘贴。

流程：
  1. 从 secrets.json 读取 clash_subscription 订阅链接（唯一配置源）
  2. 拉取订阅（直连优先，失败走本地 7890 代理）
  3. 支持 base64 编码 / 纯 URI / YAML 三种订阅格式
  4. 解析 vless / hysteria2 节点，去重（按 type+server+port），剔除垃圾命名
  5. 生成 mihomo 可用的 proxies YAML（sub-nodes.yaml）
  6. 供 clash-config.yaml 的 proxy-providers (type: file) 引用

用法：
  python3 update_clash_nodes.py [--output 路径]   # 默认输出到脚本同目录 sub-nodes.yaml
失败时不会覆盖已有节点文件（保留旧节点兜底）。
"""
import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(SCRIPT_DIR, "secrets.json")
DEFAULT_OUTPUT = os.path.join(SCRIPT_DIR, "sub-nodes.yaml")

# 垃圾命名（订阅里的提示/状态节点，能跳过就跳过）
JUNK_PATTERNS = ("剩余流量", "套餐到期", "建议每日", "有异常", "请重启")
# 名字含此关键字的节点优先保留（KLC·国家 是真实节点命名）
REAL_KEYWORD = "KLC"


def load_subscription_url():
    with open(SECRETS, encoding="utf-8") as f:
        cfg = json.load(f)
    url = str(cfg.get("clash_subscription", "")).strip()
    if not url:
        raise SystemExit("[更新节点] secrets.json 缺少 clash_subscription，无法更新订阅节点")
    return url


def fetch(url):
    """直连优先，失败走本地代理。"""
    errs = []
    for proxy in (None, "http://127.0.0.1:7890"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            if proxy:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            else:
                opener = urllib.request.build_opener()
            with opener.open(req, timeout=20) as resp:
                data = resp.read()
            if data and len(data) > 100:
                return data
            errs.append("内容过短(%dB)" % (len(data) if data else 0))
        except Exception as e:  # noqa: BLE001
            errs.append(str(e))
    raise SystemExit("[更新节点] 拉取订阅失败: " + "; ".join(errs))


def decode_sub(data):
    """识别 base64 编码的订阅并解码，返回 URI 文本。"""
    text = data.decode("utf-8", "replace").strip()
    stripped = text.replace("\n", "").replace("\r", "")
    for candidate in (text, stripped):
        try:
            raw = base64.b64decode(candidate, validate=False)
            s = raw.decode("utf-8", "replace")
            if "://" in s and any(k in s for k in ("vless", "hysteria2", "vmess", "trojan", "ss://", "hy2")):
                return s
        except Exception:  # noqa: BLE001
            continue
    return text


def parse_vless(uri):
    m = re.match(r"^vless://([^@]+)@([^:]+):(\d+)(?:\?(.*?))?(?:#(.*))?$", uri, re.S)
    if not m:
        return None
    user, host, port, query, frag = m.group(1), m.group(2), m.group(3), m.group(4) or "", m.group(5) or ""
    q = urllib.parse.parse_qs(query)
    name = urllib.parse.unquote(frag).strip() or f"{host}:{port}"
    node = {
        "name": name,
        "type": "vless",
        "server": host,
        "port": int(port),
        "uuid": user,
        "network": q.get("type", ["tcp"])[0],
        "tls": True,
        "udp": True,
        "servername": q.get("servername", [q.get("sni", [host])[0]])[0],
        "client-fingerprint": q.get("fp", ["chrome"])[0],
    }
    # 仅在显式声明 tls=false 时关闭 TLS（默认 vless 即走 TLS）
    if "tls" in q and q["tls"][0].lower() in ("false", "0", "none"):
        node["tls"] = False
    security = q.get("security", [""])[0]
    if security == "reality":
        node["flow"] = q.get("flow", [""])[0]
        node["reality-opts"] = {
            "public-key": q.get("pbk", [""])[0],
            "short-id": q.get("sid", [""])[0],
        }
    return node


def parse_hysteria2(uri):
    body = uri[len("hysteria2://"):] if uri.startswith("hysteria2://") else (uri[len("hy2://"):] if uri.startswith("hy2://") else None)
    if body is None:
        return None
    m = re.match(r"^([^@]+)@([^:]+):(\d+)(?:\?(.*?))?(?:#(.*))?$", body, re.S)
    if not m:
        return None
    user, host, port, query, frag = m.group(1), m.group(2), m.group(3), m.group(4) or "", m.group(5) or ""
    q = urllib.parse.parse_qs(query)
    name = urllib.parse.unquote(frag).strip() or f"{host}:{port}"
    node = {
        "name": name,
        "type": "hysteria2",
        "server": host,
        "port": int(port),
        "password": user,
        "sni": q.get("sni", [host])[0],
        "skip-cert-verify": True,  # 订阅 insecure=1，mihomo 对应字段为 skip-cert-verify
    }
    if q.get("alpn"):
        node["alpn"] = [a for a in q["alpn"][0].split(",") if a]
    if q.get("obfs"):
        node["obfs"] = q["obfs"][0]
    if q.get("obfs-password"):
        node["obfs-password"] = q["obfs-password"][0]
    if q.get("mport"):
        try:
            node["port"] = int(q["mport"][0])
        except ValueError:
            pass
    return node


def parse_uris(text):
    nodes = []
    seen = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        node = None
        if line.startswith("vless://"):
            node = parse_vless(line)
        elif line.startswith("hysteria2://") or line.startswith("hy2://"):
            node = parse_hysteria2(line)
        if not node:
            continue
        key = (node["type"], node["server"], node["port"])
        if key in seen:
            continue  # 同协议同地址去重（如 mg3 的三条状态提示）
        seen.add(key)
        nodes.append(node)
    # 同一地址如有重复名：优先保留真实节点命名
    by_key = {}
    for n in nodes:
        by_key.setdefault((n["type"], n["server"], n["port"]), []).append(n)
    best = []
    for key, group in by_key.items():
        real = [n for n in group if REAL_KEYWORD in n["name"]]
        nonjunk = [n for n in group if not any(p in n["name"] for p in JUNK_PATTERNS)]
        pick = (real or nonjunk or group)[0]
        # 垃圾命名节点（如"剩余流量"）重命名为 国家前缀+主机名，便于识别
        if any(p in pick["name"] for p in JUNK_PATTERNS):
            host = pick["server"].split(".")[0].upper()
            pick["name"] = "KLC·%s|%s" % (host, pick["port"])
        best.append(pick)
    best.sort(key=lambda n: n["name"])
    return best


def emit_yaml(nodes, output):
    lines = [
        "# 由 update_clash_nodes.py 从订阅自动生成，请勿手改",
        "# 生成时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "# 节点数: %d" % len(nodes),
        "proxies:",
    ]
    for n in nodes:
        lines.append("  - name: %s" % json.dumps(n["name"], ensure_ascii=False))
        for k in ("type", "server", "uuid", "password"):
            if k in n:
                lines.append("    %s: %s" % (k, json.dumps(n[k], ensure_ascii=False)))
        if "port" in n:
            lines.append("    port: %d" % n["port"])
        for k in ("network", "servername", "client-fingerprint", "flow", "sni", "obfs", "obfs-password"):
            if k in n:
                lines.append("    %s: %s" % (k, json.dumps(n[k], ensure_ascii=False)))
        for k in ("tls", "udp", "skip-cert-verify"):
            if n.get(k):
                lines.append("    %s: true" % k)
        if n.get("alpn"):
            lines.append("    alpn:")
            for a in n["alpn"]:
                lines.append("      - %s" % json.dumps(a, ensure_ascii=False))
        if n.get("reality-opts"):
            lines.append("    reality-opts:")
            for rk in ("public-key", "short-id"):
                if rk in n["reality-opts"]:
                    lines.append("      %s: %s" % (rk, json.dumps(n["reality-opts"][rk], ensure_ascii=False)))
    with open(output, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    output = DEFAULT_OUTPUT
    args = [a for a in sys.argv[1:]]
    if "--output" in args:
        idx = args.index("--output")
        if idx + 1 < len(args):
            output = args[idx + 1]

    url = load_subscription_url()
    print("[更新节点] 订阅源: %s" % url)
    data = fetch(url)
    text = decode_sub(data)
    nodes = parse_uris(text)
    if not nodes:
        print("[更新节点] 解析到 0 个节点，保留旧文件不覆盖", file=sys.stderr)
        sys.exit(1)
    if len(nodes) < 3:
        print("[更新节点] 节点数过少(%d)，可能订阅格式异常，保留旧文件不覆盖" % len(nodes), file=sys.stderr)
        sys.exit(1)
    emit_yaml(nodes, output)
    print("[更新节点] 已生成 %d 个节点 -> %s" % (len(nodes), output))
    for n in nodes[:8]:
        print("   - %s  (%s:%d)" % (n["name"], n["server"], n["port"]))
    if len(nodes) > 8:
        print("   ... 共 %d 个" % len(nodes))


if __name__ == "__main__":
    main()
