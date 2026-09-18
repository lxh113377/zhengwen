"""证闻 · WSGI 适配层（把纯标准库服务搬到「只能跑 WSGI」的长期托管）。

为什么需要：
  PythonAnywhere 这类长期托管只给 **WSGI 入口**，不允许自定义 `http.server` 监听端口
  （沙箱那套「起进程占端口」的方式在这里不可用，而且临时沙箱已按用户红线永久禁用）。
  本项目刻意零第三方依赖（见 app.py 顶部说明），因此**不引入 Flask** ——
  而是把现有 `Handler` 原样复用：

      内存里造一份「假 socket + 原始 HTTP 请求字节」→ 交给 Handler.handle()
      → 从内存 wfile 取回响应字节 → 按 WSGI 协议交回

  收益：路由、只读语义、404/403、错误兜底**只有一份实现** ——
  不会出现「本地与线上行为不同」的双份逻辑漂移（这正是本项目反复踩过的坑）。

用法（PythonAnywhere）：Web 应用的 WSGI 配置文件里写
    import sys; sys.path.insert(0, '/home/<用户名>/zhengwen/src')
    from wsgi import application

本地自检（不发任何外部网络请求）：
    python src/wsgi.py
"""

from __future__ import annotations

import io
import json
import os
import sys
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# 环境默认值：必须在导入 app 之前设置 —— app.py 在**导入时**读取这些环境变量
# ---------------------------------------------------------------------------
# 对外公开入口一律只读：零注册、零写入、写方法一律 403（可被评委实测验证）。
# 允许用环境变量显式覆盖（本地调试时设 READONLY=0 即可）。
os.environ.setdefault("READONLY", "1")
# 长期托管默认必须监听全部网卡（WSGI 下不真正监听，仅保持语义一致）
os.environ.setdefault("HOST", "0.0.0.0")

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import app as _app  # noqa: E402  （必须在设置环境变量之后导入）


class _MemoryConn:
    """假连接：让 BaseHTTPRequestHandler 以为自己在一根 socket 上读写。

    两个必须遵守的 CPython 细节（都是本轮自检实测抓出来的，不是推测）：
      ① `makefile('rb')` 必须**稳定返回同一个 rfile 对象**（响应体只存放一次）；
      ② Python 3.6+ 的 `StreamRequestHandler` 在 `wbufsize == 0` 时**不使用 makefile**，
         而是把 `self.wfile` 设成 `_SocketWriter(self.connection)` ——
         它的 `write()` 直接调 `self.connection.sendall(b)`。
         因此假连接**必须实现 `sendall`，否则一写响应头就 AttributeError**
         （原实现只实现了 makefile，自检第 1 例即崩）。
    """

    def __init__(self, request_bytes: bytes) -> None:
        self.rfile = io.BytesIO(request_bytes)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str = "rb", *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.wfile if "w" in mode else self.rfile

    # -- 让 _SocketWriter 能工作 --------------------------------------------
    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)

    def send(self, data: bytes) -> int:
        return self.wfile.write(data)

    def setblocking(self, flag: bool) -> None:  # pragma: no cover - 仅为接口兼容
        return None

    def setsockopt(self, *args: Any) -> None:  # pragma: no cover - 仅为接口兼容
        return None

    def close(self) -> None:
        return None

    def getpeername(self) -> tuple[str, int]:
        return ("127.0.0.1", 0)


class _DummyServer:
    """BaseHTTPRequestHandler 需要的最小 server 接口。"""

    server_name = "zhengwen-wsgi"
    server_port = 443


def _run_request(method: str, raw_path: str, body: bytes, content_type: str = "") -> tuple[int, str, bytes]:
    """把一次请求喂给现有 Handler，返回 (状态码, Content-Type, 响应体)。"""
    head = "{} {} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n".format(method, raw_path or "/")
    if body:
        head += "Content-Type: {}\r\nContent-Length: {}\r\n".format(content_type or "application/json", len(body))
    request_bytes = head.encode("utf-8") + b"\r\n" + body

    conn = _MemoryConn(request_bytes)
    handler_cls = _app.Handler
    # 静音：WSGI 环境下 access log 由平台记录，不重复打到 stderr
    original_log = handler_cls.log_message
    handler_cls.log_message = lambda self, fmt, *a: None  # type: ignore[assignment]
    try:
        handler_cls(conn, ("127.0.0.1", 0), _DummyServer())
    finally:
        handler_cls.log_message = original_log  # type: ignore[assignment]

    raw = conn.wfile.getvalue()
    head_bytes, _, payload = raw.partition(b"\r\n\r\n")
    status_line = head_bytes.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
    parts = status_line.split(" ", 2)
    code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 502
    ctype = "application/json; charset=utf-8"
    for line in head_bytes.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-type:"):
            ctype = line.split(b":", 1)[1].strip().decode("latin-1", "ignore")
            break
    return code, ctype, payload


def application(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
    """WSGI 入口。"""
    method = (environ.get("REQUEST_METHOD") or "GET").upper()
    path = environ.get("PATH_INFO") or "/"
    query = environ.get("QUERY_STRING") or ""
    raw_path = path + (("?" + query) if query else "")

    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    body = b""
    if length > 0:
        try:
            body = environ["wsgi.input"].read(length)
        except Exception:  # noqa: BLE001 — 读体失败按空体处理，由路由层给 400
            body = b""

    try:
        code, ctype, payload = _run_request(method, raw_path, body,
                                            environ.get("CONTENT_TYPE") or "")
    except Exception as exc:  # noqa: BLE001 — 兜底：绝不把堆栈抛给访问者
        sys.stderr.write("[证闻] WSGI 未捕获异常：{}: {}\n".format(type(exc).__name__, exc))
        code, ctype = 500, "application/json; charset=utf-8"
        payload = ('{"ok": false, "error": "服务内部异常，已降级处理"}').encode("utf-8")

    # HEAD 不得返回正文（否则部分客户端会挂住）
    if method == "HEAD":
        payload = b""

    reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
              405: "Method Not Allowed", 500: "Internal Server Error"}.get(code, "OK")
    start_response("{} {}".format(code, reason), [
        ("Content-Type", ctype),
        ("Content-Length", str(len(payload))),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
    ])
    return [payload]


# ---------------------------------------------------------------------------
# 本地自检：验证「WSGI 适配层」与「stdlib 服务」行为一致（不发外部网络请求）
# ---------------------------------------------------------------------------

def _selftest() -> int:
    cases: list[tuple[str, str, bytes, str, int]] = [
        # (方法, 路径, 请求体, content-type, 期望状态码)
        ("GET", "/api/health", b"", "", 200),
        ("GET", "/api/graph", b"", "", 200),
        ("GET", "/api/live/sources", b"", "", 200),
        ("GET", "/no-such-route", b"", "", 404),
        ("PUT", "/api/ask", b"", "", 403),
        ("DELETE", "/api/ask", b"", "", 403),
        ("POST", "/api/ask", b"", "application/json", 400),
        ("POST", "/api/ask", b"not-json", "application/json", 400),
        ("POST", "/api/ask", b'{"query": ""}', "application/json", 400),
    ]
    failed = []
    for method, path, body, ctype, want in cases:
        code, _c, payload = _run_request(method, path, body, ctype)
        ok = code == want
        print("{} {:<6} {:<20} → {}（期望 {}）".format("✅" if ok else "❌", method, path, code, want))
        if not ok:
            failed.append("{} {}".format(method, path))

    # 只读语义的机器可读断言：健康检查必须自证「只读 + 零会话写入」
    code, _c, payload = _run_request("GET", "/api/health", b"")
    health = json.loads(payload.decode("utf-8"))
    checks = {
        "readonly=true": health.get("readonly") is True,
        "sessions_tracked=0": health.get("sessions_tracked") == 0,
        "has_live_block": isinstance(health.get("live"), dict),
        "live_available>0": (health.get("live") or {}).get("available_sources", 0) > 0,
    }
    for name, ok in checks.items():
        print("{} 断言 {}".format("✅" if ok else "❌", name))
        if not ok:
            failed.append("assert:" + name)

    print()
    print("结论：{}".format("全部通过（{} 项）".format(len(cases) + len(checks)) if not failed
                          else "失败：{}".format(" ｜ ".join(failed))))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
