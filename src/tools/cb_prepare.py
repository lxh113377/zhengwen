"""证闻 · CloudBase 云函数打包（可复跑，生成而非手工复制）。

为什么要有这一步（而不是在函数目录里手放一份代码副本）：
  本项目吃过「权威源 → 交付副本」不同步的亏（plan.html 长期停在旧版没人发现）。
  所以这里**从 src/ 生成**函数目录：每次部署前重跑，内容必然与 src/ 一致；
  生成物在 `src/build/`（不入库、不进提交包）。

CloudBase 函数的关键约束（官方 CLI 文档 + 2026-09-19 实测）：
  - 体验版环境走「HTTP 访问服务（云接入）+ SCF 路由」时，上游函数必须是 **Event 型**
    （路由上游类型 SCF）；HTTP 型函数（scf_bootstrap + 9000 端口）实测返回
    `FUNCTIONS_PARAM_INVALID: FunctionType parameter is invalid`，改 WEB_SCF 路由后
    仍返回 HTTP 443（函数 Web 服务未起来，且体验版默认未开日志服务无法查日志）。
  - 因此本项目改用 **Event 型函数 + HTTP 事件适配层 index.py**（经典路径，兼容性最好）：
    运行时 `Python3.9`（官方文档：Python3.10 仅 HTTP 型函数可用），
    handler 为 `index.main_handler`。
  - 函数类型创建后不可改（Event <-> HTTP），运行时同样锁定 → 首次部署前必须定对。
  - 本项目零第三方依赖 → installDependency=false，无需装包。
  - 兼容性：本项目全部模块带 `from __future__ import annotations`、无 3.10+ 运行时语法
    （已用扫描核验）→ 可在 Python3.9 运行时工作。

用法：
  python src/tools/cb_prepare.py            # 生成 src/build/zhengwen-api/
  python src/tools/cb_prepare.py --check    # 只校验生成物与 src/ 是否一致
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # src/
BUILD = os.path.join(HERE, "build", "zhengwen-api")

# 与 pa_deploy.py 同一份「运行期需要哪些文件」的口径（不传 tools/ report/ __pycache__）
COPY_ROOT = ("app.py", "live.py", "llm.py", "rag.py", "wsgi.py", "README.md")
COPY_DIRS = ("corpus", "web")
EXCLUDE_PARTS = ("__pycache__", ".pyc", ".cmd")

# Event 型函数的 HTTP 事件适配层：把「HTTP 访问服务（云接入）」的事件
# 翻译成内部 `_run_request()`（与本地 stdlib 服务同一套路由实现），再翻译回 HTTP 响应。
# 关键纪律：路由/只读/403/404 只有一份实现 —— 这里不做任何业务判断。
INDEX_PY = '''"""证闻 · CloudBase Event 函数 HTTP 适配层（由 src/tools/cb_prepare.py 生成，勿手改）。"""
import base64
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
# 对外公开入口一律只读：零注册、零写入（wsgi.py 也会兜一次 setdefault）
os.environ.setdefault("READONLY", "1")

from wsgi import _run_request  # noqa: E402  （必须在设置环境变量之后导入）


def _query_string(params) -> str:
    """queryStringParameters 兼容 dict / 原始串两种形态（事件结构以实测为准）。"""
    if not params:
        return ""
    if isinstance(params, str):
        return params if params.startswith("?") else "?" + params
    try:
        parts = []
        for k, v in params.items():
            if isinstance(v, list):
                parts.extend("{}={}".format(k, x) for x in v)
            else:
                parts.append("{}={}".format(k, v))
        return "?" + "&".join(parts) if parts else ""
    except Exception:
        return ""


def main_handler(event, context):
    ev = event if isinstance(event, dict) else {}
    method = str(ev.get("httpMethod") or ev.get("method") or "GET").upper()
    path = str(ev.get("path") or ev.get("rawPath") or "/")
    raw = path + _query_string(ev.get("queryStringParameters") or ev.get("query"))

    body = ev.get("body") or ""
    if ev.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body)
        except Exception:
            body = b""
    if isinstance(body, str):
        body = body.encode("utf-8")

    try:
        code, ctype, payload = _run_request(method, raw or "/", body,
                                            str(ev.get("headers", {}).get("Content-Type")
                                                or ev.get("headers", {}).get("content-type")
                                                or ""))
    except Exception as exc:  # noqa: BLE001 —— 兜底：绝不把堆栈抛给访问者
        code, ctype = 500, "application/json; charset=utf-8"
        payload = ('{"ok": false, "error": "服务内部异常，已降级处理"}').encode("utf-8")
        sys.stderr.write("[zhengwen] handler 异常: {}: {}\\n".format(type(exc).__name__, exc))

    if method == "HEAD":
        payload = b""
    headers = {
        "Content-Type": ctype,
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    return {
        "statusCode": code,
        "headers": headers,
        "isBase64Encoded": False,
        "body": payload.decode("utf-8", "ignore"),
    }
'''


def collect() -> list:
    out = []
    for name in COPY_ROOT:
        p = os.path.join(HERE, name)
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                out.append((name, fh.read()))
    for folder in COPY_DIRS:
        root = os.path.join(HERE, folder)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_PARTS]
            for fn in sorted(filenames):
                if any(x in fn for x in EXCLUDE_PARTS):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, HERE).replace("\\", "/")
                with open(full, "rb") as fh:
                    out.append((rel, fh.read()))
    out.append(("index.py", INDEX_PY.encode("utf-8")))
    return out


def _short(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验生成物与 src/ 是否一致，不写盘")
    args = ap.parse_args()

    plan = collect()
    total = sum(len(b) for _r, b in plan)
    print("待打包：{} 个文件 / {} 字节 → {}".format(len(plan), total, os.path.relpath(BUILD, HERE)))

    if args.check:
        bad = []
        for rel, blob in plan:
            target = os.path.join(BUILD, rel.replace("/", os.sep))
            if not os.path.isfile(target):
                bad.append((rel, "MISSING"))
                continue
            with open(target, "rb") as fh:
                if _short(fh.read()) != _short(blob):
                    bad.append((rel, "DIFF"))
        if bad:
            print("生成物与 src/ 不一致（{} 项）→ 请重跑 cb_prepare.py".format(len(bad)))
            for rel, kind in bad[:10]:
                print("   {} {}".format(kind, rel))
            return 1
        print("OK 生成物与 src/ 一致（{} 文件）".format(len(plan)))
        return 0

    if os.path.isdir(BUILD):
        shutil.rmtree(BUILD)
    for rel, blob in plan:
        target = os.path.join(BUILD, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        # 关键：生成件必须 LF 行尾（CRLF 在 Linux 运行时会产生不可见事故）
        if rel == "index.py":
            with open(target, "w", newline="\n", encoding="utf-8") as fh:
                fh.write(blob.decode("utf-8"))
        else:
            with open(target, "wb") as fh:
                fh.write(blob)
    print("OK 已生成（含 index.py 事件适配层，LF 行尾）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
