#!/usr/bin/env python3
"""
render_gateway_config.py — 网关配置渲染（密钥运行时注入）

背景（2026-09-22）：openclaw.json / config.yaml 曾把 DeepSeek key、飞书 AppSecret、
网关 token 以明文提交进 git。OpenClaw 本身不支持 ${VAR} 展开（已核实：包内无
expandEnv/interpolate 逻辑），因此改为「模板 + 本地密钥 → 启动时渲染」：

    openclaw.template.json（进仓库，仅占位符）
        +  scripts/secrets.json（不进仓库，唯一密钥源）
        →  /root/.openclaw/openclaw.json（不进仓库，运行文件）

由 kj.sh 在启动 Gateway 前调用；渲染失败即退出非零，kj.sh 据此中止启动，
避免用错误的凭证把网关拉起来又静默失败。

设计约束：
    - 占位符未在 secrets.json 中找到 → 报错退出，绝不写空值
    - 原子写入（临时文件 + os.replace），避免网关读到半截配置
    - 只提交模板，真值永不落仓库（.gitignore 已排除 openclaw.json）
用法：
    python3 render_gateway_config.py            # 渲染
    python3 render_gateway_config.py --check    # 只校验密钥齐备（不写文件）
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

SCRIPT_DIR = config.SCRIPT_DIR
OPENCLAW_ROOT = config.OPENCLAW_ROOT
TEMPLATE_FILE = OPENCLAW_ROOT / "openclaw.template.json"
TARGET_FILE = OPENCLAW_ROOT / "openclaw.json"
# agent 级 provider 副本（同样含 apiKey，随密钥轮换保持同步）
AGENT_MODELS_FILE = OPENCLAW_ROOT / "agents" / "main" / "agent" / "models.json"

# 占位符 → secrets.json 字段
PLACEHOLDERS = {
    "DEEPSEEK_API_KEY": "deepseek_api_key",
    "FEISHU_APP_SECRET": "feishu_app_secret",
    "GATEWAY_AUTH_TOKEN": "gateway_auth_token",
}


def _mask(v: str) -> str:
    """脱敏显示：只留头 8 尾 4，避免密钥进日志。"""
    if not v:
        return "(空)"
    return f"{v[:8]}...{v[-4:]}" if len(v) > 16 else "(已设置)"


def load_secrets() -> dict:
    """读取 secrets.json 并校验占位符齐备。"""
    cfg = config.get_config()  # config.json + secrets.json 合并（下划线注释项已剔除）
    missing = [ph for ph, key in PLACEHOLDERS.items() if not cfg.get(key)]
    if missing:
        raise SystemExit(
            "❌ secrets.json 缺少以下字段（渲染中止，避免写入空密钥）：\n   "
            + "\n   ".join(f"{ph} → {PLACEHOLDERS[ph]}" for ph in missing)
        )
    return cfg


def render(check_only: bool = False) -> int:
    if not TEMPLATE_FILE.exists():
        print(f"❌ 模板不存在：{TEMPLATE_FILE}")
        return 1

    secrets = load_secrets()
    text = TEMPLATE_FILE.read_text(encoding="utf-8")

    # 逐个占位符替换，任何残留都视为错误（防止漏配）
    for ph, key in PLACEHOLDERS.items():
        text = text.replace("${" + ph + "}", secrets[key])
    leftover = re.findall(r"\$\{[A-Z_]+\}", text)
    if leftover:
        print(f"❌ 模板存在未定义占位符：{set(leftover)}")
        return 1

    # 统一以单个换行结尾（模板可能无尾换行，避免每次渲染产生无意义 diff）
    text = text.rstrip("\n") + "\n"

    # 渲染结果必须是合法 JSON（网关启动前拦掉语法错误）
    try:
        json.loads(text)
    except json.JSONDecodeError as e:
        print(f"❌ 渲染结果不是合法 JSON：{e}")
        return 1

    print("🔑 密钥注入来源：secrets.json")
    for ph, key in PLACEHOLDERS.items():
        print(f"   {ph:<20} {_mask(secrets[key])}")

    if check_only:
        print("✅ 校验通过（--check 未写文件）")
        return 0

    # 原子写入：先写临时文件再替换，避免网关读到写了一半的配置
    tmp = TARGET_FILE.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, TARGET_FILE)
    print(f"✅ 已渲染 {TARGET_FILE}")

    # agent 级 provider 副本同步（仅替换 apiKey，其余字段原样保留）
    if AGENT_MODELS_FILE.exists():
        try:
            data = json.loads(AGENT_MODELS_FILE.read_text(encoding="utf-8"))
            prov = (data.get("providers") or {}).get("deepseek")
            if isinstance(prov, dict) and prov.get("apiKey") != secrets["deepseek_api_key"]:
                prov["apiKey"] = secrets["deepseek_api_key"]
                AGENT_MODELS_FILE.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(f"✅ 已同步 {AGENT_MODELS_FILE}（apiKey）")
            else:
                print(f"✓  {AGENT_MODELS_FILE} 无需同步")
        except Exception as e:
            print(f"⚠️ agent models.json 同步失败（不影响主配置）：{e}")

    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="网关配置渲染（密钥运行时注入）")
    ap.add_argument("--check", action="store_true", help="只校验密钥齐备，不写文件")
    args = ap.parse_args()
    raise SystemExit(render(check_only=args.check))