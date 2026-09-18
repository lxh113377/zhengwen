"""证闻 · 交付包源码副本同步（含三类双向比对）

为什么需要
  交付包 `工程佐证材料/05-源代码全文/src/` 是 `src/` 的逐文件副本。
  项目里已发生过两次「改了 src 却漏同步副本」：目录空了没人发现（隐性断链），
  以及副本长期停在过时陈述（R242 陈述一致性）。人工比对必然漏，故做成工具。

判据（三类必须全为 0 才算同步完成 —— 只测「存在性」不算验证）
  MISSING：src 有、副本没有
  DIFF   ：两边都有但内容不同
  EXTRA  ：副本有、src 没有（含已删除文件与新产生的构建产物）

排除：`__pycache__/` 与 `_tmp_*`（构建/临时产物，本就不该进交付包）

用法：
  python src/tools/sync_source_copy.py            # 只比对，不改动（默认）
  python src/tools/sync_source_copy.py --apply    # 同步后再比对
退出码：0 = 已一致 ｜ 1 = 存在差异（apply 后仍不一致）｜ 2 = 路径异常
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))            # src/
PROJECT_DIR = os.path.dirname(SRC_DIR)                                            # 项目根
DST_DIR = os.path.join(PROJECT_DIR, "交付物", "提交包", "工程佐证材料",
                       "05-源代码全文", "src")

EXCLUDE_DIRS = {"__pycache__"}
EXCLUDE_PREFIX = ("_tmp_",)


def _keep(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if any(p in EXCLUDE_DIRS for p in parts):
        return False
    return not os.path.basename(rel).startswith(EXCLUDE_PREFIX)


def _walk(root: str) -> dict[str, str]:
    """返回 {相对路径: 绝对路径}（已按排除规则过滤）。"""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if _keep(rel):
                out[rel] = full
    return out


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def compare() -> tuple[list[str], list[str], list[str]]:
    src = _walk(SRC_DIR)
    dst = _walk(DST_DIR)
    missing = sorted(set(src) - set(dst))
    extra = sorted(set(dst) - set(src))
    diff = sorted(k for k in (set(src) & set(dst)) if _sha256(src[k]) != _sha256(dst[k]))
    return missing, diff, extra


def apply_sync(missing: list[str], diff: list[str], extra: list[str]) -> None:
    src = _walk(SRC_DIR)
    for rel in missing + diff:
        target = os.path.join(DST_DIR, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(src[rel], target)
    for rel in extra:
        os.remove(os.path.join(DST_DIR, rel))
    # 清理副本内的空目录（空目录会让评委判为材料不全）
    for dirpath, dirnames, filenames in os.walk(DST_DIR, topdown=False):
        if dirpath != DST_DIR and not os.listdir(dirpath):
            os.rmdir(dirpath)


def report(missing: list[str], diff: list[str], extra: list[str]) -> bool:
    ok = not (missing or diff or extra)
    print("MISSING={} DIFF={} EXTRA={}".format(len(missing), len(diff), len(extra)))
    for label, items in (("MISSING", missing), ("DIFF", diff), ("EXTRA", extra)):
        for rel in items:
            print("  {:<8} {}".format(label, rel))
    print("结论：{}".format("三类差异均为 0 —— 副本与 src 逐字节一致" if ok else "仍有差异，未同步完成"))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="执行同步（默认只比对）")
    args = ap.parse_args()

    for p in (SRC_DIR, DST_DIR):
        if not os.path.isdir(p):
            print("路径异常：{} 不存在".format(p), file=sys.stderr)
            return 2

    missing, diff, extra = compare()
    if args.apply and (missing or diff or extra):
        print("同步前：MISSING={} DIFF={} EXTRA={}".format(len(missing), len(diff), len(extra)))
        apply_sync(missing, diff, extra)
        missing, diff, extra = compare()
        print()

    return 0 if report(missing, diff, extra) else 1


if __name__ == "__main__":
    raise SystemExit(main())
