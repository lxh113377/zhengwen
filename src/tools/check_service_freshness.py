"""证闻 · 服务新鲜度门禁（取「服务行为」证据前的就绪判据）

为什么需要（2026-09-19 真实踩坑，非推测）
  修好「查询变体」后第一次导出证据，结果仍是**旧代码**的表现（「加装电梯需要什么手续？」实时命中 0）。
  原因不是残留进程（那属于另一条既有教训），而是：**Python 模块在进程启动时 import 固化**，
  本次修复的进程虽然「是新起的」，却**早于最后一次源码修改** → 服务跑的是改前的代码。
  重启同一实例后，同一导出立刻变成命中 6。⇒ 取服务行为证据前，必须先验证「服务比源码新」。

判据（只对 Python 模块成立）
  · 进程 CreationDate **晚于** `src/**/*.py` 的最新 LastWriteTime → ✅ 证据有效
  · 更早 → ❌ 证据无效，须重启服务后再取
  · 静态资源（`.js/.css/.html`）每次请求读盘，**不受此约束**（改了立刻生效，无需重启）

用法：
  python src/tools/check_service_freshness.py            # 只判定（退出码 0=有效 / 1=须重启 / 2=无服务）
  python src/tools/check_service_freshness.py --wait 15  # 轮询等待服务起新（最多 N 秒）
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import os
import subprocess
import sys
import time

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # src/
# 决定服务行为的源码扩展名：Python 由 import 固化，其余按请求读盘
FROZEN_AT_START = (".py",)
MARKER = "app.py"          # 用于在进程命令行里识别本项目服务


def _process_start() -> tuple[dt.datetime | None, int | None]:
    """返回本项目服务进程的最早启动时间与 PID（Windows 下用 CIM 查询命令行）。"""
    # 用字符串拼接而非 .format()：PowerShell 脚本里的 { } 会被 str.format 当成占位符
    ps = (
        "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*" + MARKER + "*' } | "
        "Sort-Object CreationDate | Select-Object -First 1; "
        "if ($p) { \"{0}|{1}\" -f $p.CreationDate.ToString('o'), $p.ProcessId }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, None
    if not out or "|" not in out:
        return None, None
    stamp, _, pid = out.partition("|")
    try:
        return dt.datetime.fromisoformat(stamp.strip()), int(pid)
    except ValueError:
        return None, None


def _entry_path() -> str:
    return os.path.join(SRC_DIR, MARKER)


def _import_closure(entry: str) -> set[str]:
    """从入口模块出发，递归收集**本项目内被 import 的**模块文件（仅用标准库 ast）。

    为什么必须收窄而不是横扫 `src/**/*.py`：
      实测 —— 新建一个与运行无关的工具脚本（`tools/check_service_freshness.py`）后，
      横扫式判据立刻报「服务早于源码、证据无效」，而该文件**根本不参与服务运行**。
      **误报多了这条规则就会被无视**，所以判据必须锚定「真正决定服务行为的文件集合」。
    """
    files: set[str] = set()
    stack = [entry]
    while stack:
        path = stack.pop()
        if path in files or not os.path.isfile(path):
            continue
        files.add(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                for cand in (os.path.join(SRC_DIR, top + ".py"),
                             os.path.join(SRC_DIR, top, "__init__.py")):
                    if os.path.isfile(cand) and cand not in files:
                        stack.append(cand)
    return files


def _newest_source(scan_all: bool = False) -> tuple[dt.datetime | None, str]:
    """返回「决定服务行为」的源码中最新的一份。scan_all=True 用保守近似（会含无关脚本）。"""
    if scan_all:
        targets: list[str] = []
        for dirpath, dirnames, filenames in os.walk(SRC_DIR):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            targets += [os.path.join(dirpath, n) for n in filenames
                        if n.endswith(FROZEN_AT_START)]
    else:
        targets = sorted(_import_closure(_entry_path()))

    newest_t, newest_p = None, ""
    for full in targets:
        if not os.path.isfile(full):
            continue
        mtime = dt.datetime.fromtimestamp(os.path.getmtime(full)).astimezone()
        if newest_t is None or mtime > newest_t:
            newest_t, newest_p = mtime, os.path.relpath(full, SRC_DIR)
    return newest_t, newest_p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=float, default=0.0, help="轮询等待服务起新（秒）")
    ap.add_argument("--all", action="store_true",
                    help="保守近似：扫描 src/**/*.py（含与运行无关的脚本，会有误报）")
    args = ap.parse_args()

    scope = "src/**/*.py（保守近似，含无关脚本）" if args.all else "入口模块的 import 闭包"
    print("判据范围 : {}".format(scope))

    deadline = time.time() + max(0.0, args.wait)
    while True:
        started, pid = _process_start()
        if started is None:
            print("❌ 未发现运行中的服务进程（命令行含 '{}'）→ 先启动服务再取证据".format(MARKER))
            return 2
        newest, path = _newest_source(scan_all=args.all)
        if newest is None:
            print("❌ 未找到任何 Python 源码 → 路径异常")
            return 2
        ok = started > newest
        print("服务启动 : {}（PID {}）".format(started.strftime("%Y-%m-%d %H:%M:%S"), pid))
        print("最新源码 : {}（{}）".format(newest.strftime("%Y-%m-%d %H:%M:%S"), path))
        if ok:
            print("✅ 服务晚于源码 —— 本次取到的「服务行为」证据有效")
            return 0
        print("❌ 证据无效：服务早于源码 → 服务仍运行改前代码（须重启后重新取证）")
        if time.time() >= deadline:
            return 1
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
