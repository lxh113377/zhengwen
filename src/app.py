"""证闻 · 服务端（纯 Python 标准库，零第三方依赖）

为什么用标准库而不是 Flask：
  交付红线要求「网页应用须确保链接稳定可访问」且演示需可离线。
  零依赖意味着任何免费托管、任何干净解压环境都能直接 `python app.py` 跑起来，
  不会出现「缺包 → 502」这类演示事故。

接口
  GET  /                    3D 叙事页
  GET  /web/<path>          静态资源（css / js / vendor）
  GET  /api/health          健康检查
  GET  /api/graph           离线语料图谱（事件 → 来源 → 证据），供 3D 场景渲染
  GET  /api/live/sources    实时源登记表（可用源 / 实测不可用项 / 许可 / 抓取方式）
  GET  /api/evidence/<id>   单条证据详情（离线语料）
  POST /api/ask             { "query": "...", "session": "..." }
                            → 双轨检索：离线语料 + 实时权威源，返回合并证据、
                              逐源状态、实时证据网络（供 3D 场景重建）
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from live import (
    MAX_RELATED,
    MIN_LIVE_BIGRAM_HITS,
    RELATED_MIN_HITS,
    build_live_graph,
    merge_results,
    registry as live_registry,
    search_live,
)
from llm import LLM_PROVIDER_NAME, answer, model_available
from rag import MIN_ORIGINAL_HITS, REFUSE_THRESHOLD, Corpus

# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8848"))

# 只读演示模式（READONLY=1 或 --readonly）：
#   面向复赛「线上评选」的评委入口 —— 零注册、零登录、打开即用。
#   只读的落点不是「假装」而是真实约束：
#     ① 任何非 GET/HEAD 请求除 /api/ask 外一律拒绝（/api/ask 只读语料，不产生持久化副作用）；
#     ② 只读模式下**不写会话历史**（SESSIONS 完全不动），因此对服务状态零副作用；
#     ③ 前端顶部显示「演示环境 · 只读」横幅，不让评委误以为是生产系统。
READ_ONLY = os.environ.get("READONLY", "").strip().lower() in ("1", "true", "yes", "on")

CORPUS = Corpus()

# 会话历史（内存态，演示够用；生产应换为外部存储）
SESSIONS: dict[str, list[dict[str, str]]] = {}
MAX_HISTORY = 8

ALLOWED_STATIC_EXT = {
    ".html", ".css", ".js", ".json", ".svg", ".png", ".jpg", ".jpeg",
    ".webp", ".woff2", ".woff", ".ttf", ".ico", ".map", ".txt",
}


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=None).encode("utf-8")


def _safe_join(root: str, rel: str) -> str | None:
    """路径穿越防护：解析后的绝对路径必须仍在 root 之内。"""
    rel = rel.replace("\\", "/").lstrip("/")
    target = os.path.normpath(os.path.join(root, rel))
    root_norm = os.path.normpath(root)
    if target != root_norm and not target.startswith(root_norm + os.sep):
        return None
    return target


# ---------------------------------------------------------------------------
# 业务处理
# ---------------------------------------------------------------------------


def handle_ask(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    query = (payload.get("query") or "").strip()
    session_id = str(payload.get("session") or "anon")[:64]

    if len(query) > 300:
        return 400, {
            "ok": False,
            "error": "问题过长（上限 300 字）",
            "hint": "请把问题缩小到一个具体事实点。",
        }

    t0 = time.perf_counter()
    # 双轨并行（2026-09-19）：离线语料 + 实时权威源各自检索，再合并判定。
    # 拒答语义随之升级为「两条路都没有可核实依据才拒答」，不再是「语料没收录就不答」。
    corpus_result = CORPUS.search(query)
    live_result = search_live(query)
    result = merge_results(query, corpus_result, live_result)
    generated = answer(query, result)
    total_ms = round((time.perf_counter() - t0) * 1000, 2)

    # 会话历史（仅保留最近 N 轮，避免无界增长）
    # 只读模式：完全不写入会话状态 —— 这是「无写入副作用」的可验证落点。
    if not READ_ONLY:
        history = SESSIONS.setdefault(session_id, [])
        history.append({"role": "user", "content": query})
        history.append({"role": "assistant", "content": generated["answer"][:500]})
        if len(history) > MAX_HISTORY * 2:
            del history[: len(history) - MAX_HISTORY * 2]

    return 200, {
        "ok": True,
        "query": query,
        "answer": generated["answer"],
        "mode": generated["mode"],
        "provider": generated.get("provider"),
        "model": generated.get("model"),
        "refused": generated["refused"],
        "refuse_reason": result.refuse_reason,
        "confidence": result.confidence,
        "threshold": REFUSE_THRESHOLD,
        "citations": generated["citations"],
        "dropped_citations": generated.get("dropped_citations", []),
        "degraded_reason": generated.get("degraded_reason"),
        "divergences": result.divergences,
        # 实时证据没有 BM25 分数（口径不同，不硬凑）→ 传 None，界面据 ev.live 显示「实时」而非分数
        "evidence": [
            ev.public(
                None if ev.live else (result.scores[i] if i < len(result.scores) else None),
                result.matched_terms.get(ev.id),
            )
            for i, ev in enumerate(result.evidence)
        ],
        "trace": [step.public() for step in result.trace],
        # 相关线索（分层输出第二层）：弱命中、verified=false、**不参与拒答判定**。
        # 即使本问被拒答也照常返回 —— 拒答不代表没查到东西，只代表没查到「可核实的依据」。
        "related": result.related,
        "timing": {"server_total_ms": total_ms, "generate_ms": generated.get("elapsed_ms")},
        "corpus": CORPUS.meta.get("name"),
        # 双轨拆解：离线语料命中数 / 置信度，与实时核证的逐源状态分开报，便于评审核验
        "corpus_result": {
            "hits": len(corpus_result.evidence),
            "confidence": corpus_result.confidence,
            "refused": corpus_result.refused,
            "refuse_reason": corpus_result.refuse_reason,
        },
        "live": result.live,
        # 3D 场景用：本次实时检索的证据网络（与 /api/graph 同构，前端无需分叉）
        "live_graph": build_live_graph(query, live_result),
    }


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "ZhengwenDemo/0.1"
    protocol_version = "HTTP/1.1"

    # 静音默认日志（改为结构化单行）
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write("[证闻] %s - %s\n" % (self.address_string(), fmt % args))

    # -- 工具 ---------------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: Any) -> None:
        self._send(code, _json_bytes(payload), "application/json; charset=utf-8")

    def _serve_file(self, path: str) -> None:
        if not os.path.isfile(path):
            self._json(404, {"ok": False, "error": "资源不存在"})
            return
        ext = os.path.splitext(path)[1].lower()
        if ext not in ALLOWED_STATIC_EXT:
            self._json(403, {"ok": False, "error": "该类型资源不允许访问"})
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(path, "rb") as fh:
            self._send(200, fh.read(), ctype)

    # -- 路由 ---------------------------------------------------------------

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = unquote(parsed.path)

        # / 与 /demo 都指向同一页面：/demo 是给评委的「免登录只读演示入口」别名，
        # 便于在材料里单独给出一个语义明确的链接。
        if route in ("/", "/index.html", "/demo", "/demo/"):
            self._serve_file(os.path.join(WEB_DIR, "index.html"))
            return

        if route == "/api/health":
            self._json(200, {
                "ok": True,
                "service": "证闻 · 对话式新闻智能体",
                "model_available": model_available(),
                "provider": LLM_PROVIDER_NAME if model_available() else None,
                "corpus": CORPUS.meta.get("name"),
                "corpus_kind": CORPUS.meta.get("kind"),
                "license": CORPUS.meta.get("license"),
                "badge": CORPUS.meta.get("badge"),
                # 逐源许可：前端据此显示「合成演示 / 公开来源」与各自署名要求
                "sources": [
                    {
                        "id": s.get("id"),
                        "name": s.get("name"),
                        "kind": s.get("kind"),
                        "license": s.get("license"),
                        "terms_url": s.get("terms_url"),
                        "attribution_required": s.get("attribution_required"),
                        "retrieved": s.get("retrieved"),
                    }
                    for s in CORPUS.sources
                ],
                "stats": CORPUS.graph()["stats"],
                "refuse_threshold": REFUSE_THRESHOLD,
                "min_original_hits": MIN_ORIGINAL_HITS,
                "readonly": READ_ONLY,
                "entry": "免登录只读演示入口" if READ_ONLY else "完整演示",
                # 只读性可验证指标：只读模式下无论问多少次，这里都应为 0
                "sessions_tracked": len(SESSIONS),
                # 实时核证层状态（可用源数 / 通用检索插槽是否配置），供评委核验「查了哪些源」
                "live": {
                    "enabled": live_registry()["enabled"],
                    "available_sources": len(live_registry()["available"]),
                    "unavailable_sources": len(live_registry()["unavailable"]),
                    "web_search_slot_configured": live_registry()["web_search_slot"]["configured"],
                    "per_source_timeout_s": live_registry()["per_source_timeout_s"],
                    "total_budget_s": live_registry()["total_budget_s"],
                    # 分层输出判据必须公开：strict 才作依据，related 仅作线索
                    "strict_min_bigram_hits": MIN_LIVE_BIGRAM_HITS,
                    "related_min_bigram_hits": RELATED_MIN_HITS,
                    "max_related": MAX_RELATED,
                },
            })
            return

        if route == "/api/live/sources":
            # 实时源登记表（含每条源的许可、抓取方式、实测结论与不可用原因）
            self._json(200, {"ok": True, **live_registry()})
            return

        if route == "/api/graph":
            self._json(200, {"ok": True, **CORPUS.graph()})
            return

        if route.startswith("/api/evidence/"):
            ev_id = route[len("/api/evidence/"):]
            ev = CORPUS.get_evidence(ev_id)
            if not ev:
                self._json(404, {"ok": False, "error": "证据编号不存在"})
            else:
                self._json(200, {"ok": True, "evidence": ev})
            return

        if route.startswith("/web/"):
            target = _safe_join(WEB_DIR, route[len("/web/"):])
            if target is None:
                self._json(403, {"ok": False, "error": "非法路径"})
                return
            self._serve_file(target)
            return

        self._json(404, {"ok": False, "error": "接口不存在"})

    def _reject_write(self) -> None:
        """只读模式下对写方法统一拒绝（可被评委用 curl / 浏览器实测验证）。"""
        self._json(403, {
            "ok": False,
            "error": "只读演示环境：本服务不接受写操作（仅开放 GET 查询与 POST /api/ask 只读问答）",
        })

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_write()

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_write()

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject_write()

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route != "/api/ask":
            if READ_ONLY:
                # 只读演示环境：除「只读问答」外的一切写操作一律拒绝（可被评委实测验证）
                self._json(403, {
                    "ok": False,
                    "error": "只读演示环境：仅开放 GET 查询与 POST /api/ask（只读问答），不接受任何写操作",
                })
            else:
                self._json(404, {"ok": False, "error": "接口不存在"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 64 * 1024:
            self._json(400, {"ok": False, "error": "请求体为空或过大"})
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload 必须是对象")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
            return

        if not (payload.get("query") or "").strip():
            self._json(400, {
                "ok": False,
                "error": "请输入问题",
                "hint": "例如：5号线开通后客流多少？",
            })
            return

        try:
            code, body = handle_ask(payload)
        except Exception:  # noqa: BLE001 — 兜底：绝不把堆栈返回给前端
            sys.stderr.write("[证闻] 未捕获异常：\n" + traceback.format_exc())
            code, body = 500, {
                "ok": False,
                "error": "服务内部异常，已降级处理",
                "hint": "请稍后重试；若持续出现，请检查服务端日志。",
            }
        self._json(code, body)


def main() -> None:
    global READ_ONLY, HOST, PORT

    # 命令行参数（与 HOST / PORT / READONLY 三个环境变量等价，便于云平台两种接法）：
    #   python app.py --readonly --host 0.0.0.0 --port 8848
    argv = sys.argv[1:]
    if "--readonly" in argv:
        READ_ONLY = True
    for i, token in enumerate(argv):
        if token == "--host" and i + 1 < len(argv):
            HOST = argv[i + 1]
        if token == "--port" and i + 1 < len(argv):
            PORT = int(argv[i + 1])

    stats = CORPUS.graph()["stats"]
    print("=" * 66)
    print("  证闻 · 对话式新闻智能体 —— 演示服务")
    print("=" * 66)
    print(f"  语料        : {CORPUS.meta.get('name')}（{CORPUS.meta.get('kind')}）")
    for s in CORPUS.sources:
        print(f"                · {s.get('id')} {s.get('name')}｜{s.get('license')}")
    print(f"  语料规模    : {stats['topics']} 主题 / {stats['sources']} 来源 / {stats['evidence']} 条证据 / {stats['divergences']} 组分歧")
    print(f"  模型路径    : {'在线模型（' + LLM_PROVIDER_NAME + '）' if model_available() else '未配置凭据 → 规则版'}")
    reg = live_registry()
    print(f"  实时核证    : {'启用' if reg['enabled'] else '关闭'}（可用源 {len(reg['available'])} 个 /"
          f" 实测不可用 {len(reg['unavailable'])} 项；"
          f"通用检索插槽 {'已配置' if reg['web_search_slot']['configured'] else '未配置'}）")
    print(f"  依据闸门    : 置信度阈值 {REFUSE_THRESHOLD} + 原始词命中 ≥{MIN_ORIGINAL_HITS}"
          f"（双轨并行：两条路都没有可核实依据才拒答）")
    print(f"  分层输出    : 可核实依据（bigram ≥{MIN_LIVE_BIGRAM_HITS}，可作结论）"
          f" ｜ 相关线索（≥{RELATED_MIN_HITS}，最多 {MAX_RELATED} 条，仅展示、不作依据）")
    print(f"  运行模式    : {'只读演示（零注册 / 零写入）' if READ_ONLY else '完整演示'}")
    print(f"  监听        : http://{HOST}:{PORT}/" + ("   （只读入口：/demo）" if READ_ONLY else ""))
    print("=" * 66)
    print("  按 Ctrl+C 停止")
    print()

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[证闻] 已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
