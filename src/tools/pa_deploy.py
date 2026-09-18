"""证闻 · PythonAnywhere 长期部署（官方 API，可复跑）。

为什么用 PythonAnywhere：用户红线——**永久禁止临时部署地址**（云沙箱等），对外入口必须是长期通道。

为什么是这个工具形态（而不是一堆 PowerShell 拼命令）：
  · PowerShell 在本机反复踩坑（`$Host` 保留变量致整段静默空转、引号破坏整条命令）；
  · 部署要发几十次带 multipart 的请求，脚本化才能**可复跑、可审计、失败可定位**；
  · 🔴 **凭据零落盘**：token 只从环境变量 `PA_TOKEN` 读取，脚本内不写、不回显、不记日志。

官方 API 的关键约束（2026-09-19 实测 + 官方文档）：
  · 认证：请求头 `Authorization: Token <token>`；美区 www / 欧区 eu（本账号在 **www**，eu 返回 401）
  · 限速 **40 请求/分钟** → 上传按 1.6s 间隔推进，避免被限流
  · 上传：`POST /api/v0/user/<u>/files/path/home/<u>/...`，**body 必须是 multipart、字段名固定 `content`**
  · 🔴 **`/var/www` 下的 WSGI 配置文件无 API 可写**（实测 GET 404）→ 那一步只能人在网页端粘贴，
    脚本会把该贴什么原样打印出来（不猜、不假装自动化）
  · 改完配置/文件需要 `POST .../webapps/<domain>/reload/`

用法（token 只在当前进程环境里，不写文件）：
  $env:PA_TOKEN = "<你的 token>"
  python src/tools/pa_deploy.py --user lxh123 --dry-run
  python src/tools/pa_deploy.py --user lxh123
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

HOST = "https://www.pythonanywhere.com"
BASE_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # src/
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 上传白名单：只传运行期真正需要的（不传 tools/ report/ __pycache__ *.cmd）
INCLUDE_ROOT = ("app.py", "live.py", "llm.py", "rag.py", "wsgi.py", "README.md")
INCLUDE_DIRS = ("corpus", "web")
EXCLUDE_PARTS = ("__pycache__", ".pyc", ".cmd")

UPLOAD_GAP_S = 1.6   # 40 请求/分钟限速 → 留出余量


def env_token() -> str:
    token = os.environ.get("PA_TOKEN", "").strip()
    if not token:
        print("缺少环境变量 PA_TOKEN（不要把 token 写进文件）。", file=sys.stderr)
        raise SystemExit(2)
    return token


def api(token: str, path: str, *, method: str = "GET", data: bytes | None = None,
        ctype: str = "application/json", timeout: float = 60.0) -> tuple[int, Any]:
    """返回 (状态码, 解析后的 JSON 或 None)。4xx/5xx 不抛异常，由调用方判定。"""
    req = urllib.request.Request(HOST + path, data=data, method=method)
    req.add_header("Authorization", "Token " + token)
    req.add_header("User-Agent", UA)
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return resp.status, raw.decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "ignore")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, "{}: {}".format(type(exc).__name__, exc)


def upload(token: str, remote_path: str, blob: bytes) -> tuple[int, Any]:
    """按官方要求用 multipart（字段名固定 content），文件名会被忽略。"""
    boundary = "----zhengwen" + uuid.uuid4().hex
    name = os.path.basename(remote_path) or "file"
    head = ('--{b}\r\nContent-Disposition: form-data; name="content"; filename="{n}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n').format(b=boundary, n=name)
    tail = "\r\n--{}--\r\n".format(boundary)
    body = head.encode("utf-8") + blob + tail.encode("utf-8")
    return api(token, "/api/v0/user/{}/files/path{}".format(USER, remote_path),
               method="POST", data=body,
               ctype="multipart/form-data; boundary=" + boundary)


USER = "lxh123"   # main() 会按 --user 覆盖


def collect_local() -> list[tuple[str, str, bytes]]:
    """返回 [(本地绝对路径, 远端相对路径, 内容)]。"""
    out: list[tuple[str, str, bytes]] = []
    for name in INCLUDE_ROOT:
        p = os.path.join(BASE_SRC, name)
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                out.append((p, name, fh.read()))
    for folder in INCLUDE_DIRS:
        root = os.path.join(BASE_SRC, folder)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_PARTS]
            for fn in sorted(filenames):
                if any(x in fn for x in EXCLUDE_PARTS):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, BASE_SRC).replace("\\", "/")
                with open(full, "rb") as fh:
                    out.append((full, rel, fh.read()))
    return out


def _public(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None,
            timeout: float = 60.0) -> tuple[int, str]:
    """不带凭据的公开访问（测站点本身，不是测 API）。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "ignore")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, "{}: {}".format(type(exc).__name__, exc)


WSGI_TEMPLATE = """import sys

path = '{remote_src}'
if path not in sys.path:
    sys.path.insert(0, path)

# 入口在 src/wsgi.py：它复用 app.py 的 Handler（路由/只读/403/404 只有一份实现），
# 并在导入 app 之前把 READONLY 默认置 1（对外公开入口一律只读）。
from wsgi import application
"""


def write_wsgi(token: str, domain: str, remote_src: str) -> tuple[int, Any]:
    """写 web app 的 WSGI 配置文件（/var/www/<domain 点换下划线>_wsgi.py）。

    🔴 2026-09-19 实测结论：这个位置**官方文档没有对应端点**，但 Files API 能读能写 ——
    所以不必再让人工去网页端粘贴（那是文档级结论，不是能力级结论）。
    写入后必须 reload 才生效，故本函数之后一律跟着 reload。
    """
    path = "/var/www/{}_wsgi.py".format(domain.replace(".", "_"))
    blob = WSGI_TEMPLATE.format(remote_src=remote_src).encode("utf-8")
    return upload(token, path, blob)


def smoke(domain: str, query: str) -> bool:
    """部署后冒烟：健康检查 + 一次真实提问，并**逐源打印**结果。

    为什么必须逐源打印：免费档（Beginner）的出站白名单会拦掉中国权威媒体域名 ——
    那会让「实时核查」整体失效。这个自检就是**用实测判定档位限制**，
    而不是靠读定价页推断（部署完才发现查不了，代价更大）。
    """
    print("\n" + "-" * 68)
    print("部署后冒烟自检")
    code, body = _public("https://{}/api/health".format(domain), timeout=60)
    print("[1] GET /api/health → {}".format(code))
    if code != 200:
        print("    正文片段：{}".format(body[:300]))
        print("    → 站点未就绪：多半是 WSGI 配置未生效（看 Web 标签页的 Error log）")
        return False
    h = json.loads(body)
    live_h = h.get("live") or {}
    print("    readonly={} ｜ model_available={} ｜ 实时可用源={} ｜ 实测不可用={} ｜ 通用检索插槽={}".format(
        h.get("readonly"), h.get("model_available"),
        live_h.get("available_sources"), live_h.get("unavailable_sources"),
        live_h.get("web_search_slot_configured")))

    code, body = _public("https://{}/api/ask".format(domain), method="POST",
                         payload={"query": query}, timeout=180)
    print("[2] POST /api/ask 「{}」→ {}".format(query, code))
    if code != 200:
        print("    正文片段：{}".format(body[:300]))
        return False
    d = json.loads(body)
    live = d.get("live") or {}
    print("    refused={} ｜ mode={} ｜ 实时可核实={} ｜ 线索={} ｜ 离线置信度={}".format(
        d.get("refused"), d.get("mode"), live.get("hits"), live.get("related_hits"),
        d.get("confidence")))
    failed = [s for s in (live.get("sources") or []) if not s["ok"]]
    for s in (live.get("sources") or []):
        print("      · {:<34} hits={:<3} {}{}".format(
            str(s.get("name"))[:34], s.get("hits"),
            "OK" if s["ok"] else "FAIL", "" if s["ok"] else " " + str(s.get("error"))[:60]))
    if failed and live.get("offline"):
        print("    ⚠️ 全部实时源未返回 → 高度怀疑平台**出站白名单**限制（免费档）；")
        print("       处置：按实标注，或升级付费档（Unrestricted Internet access）后再测。")
    elif failed:
        print("    ⚠️ 有 {} 个源未返回（其余源正常）→ 属源侧/网络抖动，不影响整体可用性".format(len(failed)))
    return True


def main() -> int:
    global USER
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="lxh123")
    ap.add_argument("--remote-root", default="")
    ap.add_argument("--domain", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-upload", action="store_true")
    ap.add_argument("--skip-reload", action="store_true")
    ap.add_argument("--skip-wsgi", action="store_true")
    args = ap.parse_args()

    USER = args.user
    remote_root = args.remote_root or "/home/{}/zhengwen".format(USER)
    domain = args.domain or "{}.pythonanywhere.com".format(USER)
    remote_src = remote_root + "/src"

    files = collect_local()
    total = sum(len(b) for _p, _r, b in files)
    print("待上传：{} 个文件 / {} 字节 → {}/src".format(len(files), total, remote_root))
    if args.dry_run:
        for _p, rel, blob in files:
            print("  {:<42} {:>8}B".format(rel, len(blob)))
        print("\n（dry-run：未发起任何请求）")
        return 0

    token = env_token()

    if not args.skip_upload:
        ok = fail = 0
        for i, (_p, rel, blob) in enumerate(files, 1):
            code, resp = upload(token, "{}/src/{}".format(remote_root, rel), blob)
            good = code in (200, 201)
            ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
            print("[{:>3}/{}] {} {:<42} {}".format(
                i, len(files), "✅" if good else "❌", rel,
                "" if good else "→ {} {}".format(code, str(resp)[:110])))
            if not good and code in (429, 0):
                time.sleep(3)   # 限速/网络抖动：放慢继续，不整批中止
            time.sleep(UPLOAD_GAP_S)
        print("上传完成：成功 {} / 失败 {}".format(ok, fail))
        if fail:
            print("⚠️ 有失败项，部署不完整 —— 请修复后重跑（脚本幂等，可重复执行）。")

    # 1) 源码目录（WSGI 之外唯一需要配置的项）
    code, resp = api(token, "/api/v0/user/{}/webapps/{}/".format(USER, domain),
                     method="PATCH", data=json.dumps({"source_directory": remote_src}).encode())
    print("设置 source_directory → {} {}".format(code, str(resp)[:120]))

    # 2) 静态映射：/web/ 交给平台直接发（不占 web worker；CSS/JS/字体都是静态文件）
    code, resp = api(token, "/api/v0/user/{}/webapps/{}/static_files/".format(USER, domain),
                     method="POST",
                     data=json.dumps({"url": "/web/", "path": remote_src + "/web/"}).encode())
    print("设置静态映射 /web/ → {} {}".format(code, str(resp)[:160]))

    if not args.skip_reload:
        # 3) WSGI 配置文件：官方文档没有端点，但 Files API 实测能写（见 write_wsgi 说明）
        if not args.skip_wsgi:
            code, resp = write_wsgi(token, domain, remote_src)
            ok = code in (200, 201)
            print("写 WSGI 配置文件 → {} {}".format(code, "" if ok else str(resp)[:160]))
            if not ok:
                print("⚠️ 写入失败 → 只能人工粘贴（Web 标签页 → WSGI configuration file）：")
                print(WSGI_TEMPLATE.format(remote_src=remote_src))

        code, resp = api(token, "/api/v0/user/{}/webapps/{}/reload/".format(USER, domain),
                         method="POST", data=b"")
        print("reload → {} {}".format(code, str(resp)[:120]))

    print("\n" + "=" * 68)
    print("落地地址：https://{}/（长期有效；非临时沙箱）".format(domain))
    print("⚠️ 免费档 Web App 有到期日，**须在网页端点一次续期**，否则会被停用。")
    print("=" * 68)

    smoke(domain, "英伟达最新财报")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
