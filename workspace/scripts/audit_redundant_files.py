#!/usr/bin/env python3
"""
audit_redundant_files.py - 冗余/重复文件审计（只读）

用途：
1) 扫描常见备份目录（.trash、王姐修复备份、sessions reset）中的重复副本
2) 给出“可清理建议”，但不执行删除（安全）

用法：
    python3 audit_redundant_files.py
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


ROOT = Path("/root")
WORKSPACE = Path("/root/.openclaw/workspace")
SESSION_DIR = Path("/root/.openclaw/agents/main/sessions")

# 只审计这些目录，避免全盘大扫描
AUDIT_DIRS = [
    WORKSPACE,
    Path("/root/.trash-20260430"),
    Path("/root/王姐修复-2026-04-30-备份"),
    SESSION_DIR,
]


def sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def collect_files() -> list[Path]:
    files: list[Path] = []
    for base in AUDIT_DIRS:
        if not base.exists():
            continue
        for dp, dns, fns in os.walk(base):
            # 跳过缓存目录
            dns[:] = [d for d in dns if d not in {"__pycache__", ".git", "node_modules"}]
            for fn in fns:
                p = Path(dp) / fn
                try:
                    if p.is_file() and p.stat().st_size <= 20 * 1024 * 1024:
                        files.append(p)
                except Exception:
                    continue
    return files


def main():
    files = collect_files()
    by_hash: dict[str, list[Path]] = {}
    for p in files:
        h = sha256(p)
        if h:
            by_hash.setdefault(h, []).append(p)

    dup_groups = [g for g in by_hash.values() if len(g) > 1]
    dup_groups.sort(key=len, reverse=True)

    print("📦 冗余文件审计报告")
    print(f"扫描文件数: {len(files)}")
    print(f"重复内容组: {len(dup_groups)}\n")

    if not dup_groups:
        print("✅ 未发现内容重复文件")
        return

    shown = 0
    removable_candidates: set[Path] = set()
    for group in dup_groups:
        # 优先保留 workspace / sessions 目录，备份目录算候选可清理
        keep = None
        for p in group:
            if str(p).startswith(str(WORKSPACE)) or str(p).startswith(str(SESSION_DIR)):
                keep = p
                break
        if keep is None:
            keep = group[0]

        candidates = [p for p in group if p != keep]
        for c in candidates:
            if "/.trash-" in str(c) or "王姐修复-2026-04-30-备份" in str(c):
                removable_candidates.add(c)

        if shown < 20:
            print("—" * 60)
            print(f"保留: {keep}")
            for c in candidates:
                tag = "可清理候选" if c in removable_candidates else "重复但建议保留"
                print(f"  - {c}  [{tag}]")
            shown += 1

    print("\n" + "=" * 60)
    print(f"🧹 可清理候选数量: {len(removable_candidates)}")
    if removable_candidates:
        print("建议：确认后再统一清理这些备份副本（非运行文件）。")


if __name__ == "__main__":
    main()

