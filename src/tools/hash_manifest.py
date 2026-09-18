#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成『交付物/提交包』的 SHA256 台账（验收清单第 12 项）。

产出：
  1) 交付物/提交包/提交包-SHA256台账.md        —— 人读表格
  2) 交付物/提交包/工程佐证材料/submit_manifest.json —— 机器读（可供复核脚本二次比对）

纪律：相对路径统一用 `/`；UTF-8 无 BOM；输出文件自身不在清单内。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "交付物" / "提交包"
MD_OUT = ROOT / "提交包-SHA256台账.md"
JSON_OUT = ROOT / "工程佐证材料" / "submit_manifest.json"
SELF_NAMES = {MD_OUT.name, JSON_OUT.name}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def main() -> int:
    if not ROOT.is_dir():
        print(f"[FATAL] 提交包目录不存在: {ROOT}", file=sys.stderr)
        return 2

    entries, errors = [], []
    for p in sorted(ROOT.rglob("*"), key=lambda x: str(x).lower()):
        if not p.is_file():
            continue
        if p.name in SELF_NAMES:
            continue
        try:
            entries.append(
                {"path": rel(p), "bytes": p.stat().st_size, "sha256": sha256_of(p)}
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"path": rel(p), "error": repr(exc)})

    total_bytes = sum(e["bytes"] for e in entries)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = {
        "generated_at": now,
        "root": str(ROOT),
        "count": len(entries),
        "total_bytes": total_bytes,
        "files": entries,
        "errors": errors,
    }

    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        "# 提交包 SHA256 台账",
        "",
        f"> 生成时间：**{now}**",
        f"> 根目录：`交付物/提交包/` ｜ 文件数：**{len(entries)}** ｜ 总字节：**{total_bytes:,}**",
        "> 用途：提交前自证文件未被篡改 / 提交后核验上传完整性（验收清单第 12 项）",
        "> 机器可读副本：`工程佐证材料/submit_manifest.json`",
        "> 本台账文件与 JSON 副本自身不计入清单。",
        "",
        "| # | 相对路径 | 字节 | SHA256（前 16 位） | 完整值 |",
        "|---|---|---:|---|---|",
    ]
    for i, e in enumerate(entries, 1):
        lines.append(
            f"| {i} | `{e['path']}` | {e['bytes']:,} | `{e['sha256'][:16]}` | `{e['sha256']}` |"
        )
    if errors:
        lines += ["", "## ⚠️ 读取失败", ""]
        for err in errors:
            lines.append(f"- `{err['path']}` — {err['error']}")
    MD_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"[OK] files={len(entries)} bytes={total_bytes:,} errors={len(errors)}")
    print(f"[OK] md   -> {MD_OUT}")
    print(f"[OK] json -> {JSON_OUT}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
