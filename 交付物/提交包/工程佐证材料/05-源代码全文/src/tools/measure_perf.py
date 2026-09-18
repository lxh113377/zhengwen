"""证闻 · 首屏加载时间与滚动帧率实测工具（v2）

为什么需要本工具：
  材料数字对照表「二·8 首屏加载时间 / 滚动帧率」此前为「未测」——
  未实测的数字一律不得写入参赛材料，因此必须先有可复跑的实测。

v2 修订（2026-09-18，本轮实测驱动，非拍脑袋）：
  v1 首轮实跑发现空白页 rAF 基线为 **294 fps**，远超 60Hz —— 说明 headless 下
  requestAnimationFrame **未被 vsync 限制**，此时把「fps」当显示帧率是错的。
  按「判据标定前先测两侧边界」的纪律：**改机制，不调参**。v2 因此拆成两路：
    · 加载时间矩阵（headless）：与帧率无关，headless 完全适用；
    · 帧率测量（双模式）：
        - headless-unthrottled：无 vsync → 结果解释为「每帧开销上限」
          （300fps ⇒ 单帧 ≤3.33ms），是 60Hz 预算（16.7ms）的下界比较基准；
        - headed-vsync：真实窗口 + 真实 GPU/合成器 → rAF 受显示器刷新约束，
          这才是可写入材料的「滚动帧率」。
  两路各自先跑**空白页基线**作对照（同模式下比较才成立）。

另新增：**低帧率自适应降级的触发验证**。
  `src/web/js/scene.js` L261-268：连续 24 帧 <26fps 时把 renderer 像素比降到 1。
  该分支此前从未被验证过。本工具用 `Emulation.setCPUThrottlingRate` 施压触发，
  并以 canvas 物理宽 / CSS 宽反推 pixelRatio 观测其是否真的下降
  （DPR 必须 ≥2 才可观测，故该场景固定使用 device_scale_factor=2）。

测什么（对应官方评分「用户体验 10 分」与材料登记项）：
  1. 首屏加载：TTFB / DOMContentLoaded / load / FCP / LCP / 首屏可读 /
     3D 场景就绪时间；资源构成与外域请求（定位首屏阻塞点）。
  2. 滚动帧率：沿 5 段叙事全程滚动的帧间隔分布（均值 / p50 / p5 / 最低）
     与低于 30fps、低于 26fps 的帧占比。
  3. 3D 降级触发点：施压下 pixelRatio 是否由 2 降为 1。

诚实性纪律：
  - headless 的 WebGL 走软件光栅（SwiftShader），帧开销高于真机；
    headed 才是真实 GPU。两种模式的结果必须分别标注，不得混用。
  - 场景若未真正启动 3D（`__sceneStatus.ok=false`），该场景不产帧率。

用法：
  python src/tools/measure_perf.py                # 全部矩阵（含 headed，会短暂弹出浏览器窗口）
  NO_HEADED=1 python src/tools/measure_perf.py    # 跳过 headed（无桌面会话时）
  TARGET_URL=http://127.0.0.1:8848/ python src/tools/measure_perf.py
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    print("需要 playwright：pip install playwright", file=sys.stderr)
    raise SystemExit(3)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # src/
PROJECT_DIR = os.path.dirname(BASE_DIR)                                          # 项目根
TARGET_URL = os.environ.get("TARGET_URL", "http://127.0.0.1:8848/")
NO_HEADED = os.environ.get("NO_HEADED", "").strip() in ("1", "true", "yes")
# 场景过滤（补测用）：PERF_ONLY=HANG-ROUTE,FPS 只跑名字含这些子串的场景，避免重跑全矩阵
PERF_ONLY = [s.strip() for s in os.environ.get("PERF_ONLY", "").split(",") if s.strip()]
OUT_DIR = os.path.join(
    PROJECT_DIR, "交付物", "提交包", "工程佐证材料", "03-工程验证证据", "raw"
)

GL_ARGS = ["--enable-unsafe-swiftshader", "--disable-dev-shm-usage"]

# 采样注入：必须早于页面任何脚本执行，才能拿到准确 t0 与绘制时间点
INIT_PROBE = r"""
(() => {
  const P = { t0: performance.now(), fcp: null, lcp: null, bootDone: null, sceneReady: null };
  window.__perf = P;
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) if (e.name === 'first-contentful-paint') P.fcp = e.startTime;
    }).observe({ type: 'paint', buffered: true });
  } catch (e) { P.fcpErr = String(e); }
  try {
    new PerformanceObserver((l) => {
      const es = l.getEntries();
      if (es.length) P.lcp = es[es.length - 1].startTime;
    }).observe({ type: 'largest-contentful-paint', buffered: true });
  } catch (e) { P.lcpErr = String(e); }
  const tick = () => {
    if (document.body && P.bootDone === null && !document.body.classList.contains('is-loading')) {
      P.bootDone = performance.now();
    }
    if (P.sceneReady === null && window.__sceneStatus) P.sceneReady = performance.now();
    if (P.bootDone === null || P.sceneReady === null) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);

  const F = { frames: [], running: false };
  window.__fps = F;
  F.begin = function () {
    F.frames = []; F.running = true;
    const loop = (t) => { if (!F.running) return; F.frames.push(t); requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  };
  F.end = function () { F.running = false; return F.frames.slice(); };
})();
"""

SCROLL_THROUGH = r"""
(dur) => new Promise((res) => {
  const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
  const t0 = performance.now();
  const step = () => {
    const p = Math.min(1, (performance.now() - t0) / dur);
    window.scrollTo(0, Math.round(max * p));
    if (p < 1) requestAnimationFrame(step); else res(max);
  };
  requestAnimationFrame(step);
})
"""

SNAPSHOT = r"""
() => {
  const c = document.getElementById('scene');
  const st = window.__sceneStatus || {};
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const res = performance.getEntriesByType('resource') || [];
  const round = (v) => (typeof v === 'number' && isFinite(v) ? Math.round(v * 10) / 10 : null);
  return {
    perf: window.__perf || null,
    scene: { ok: !!st.ok, reason: st.reason || null, nodes: st.nodes != null ? st.nodes : null },
    canvas: c
      ? {
          width: c.width, height: c.height,
          clientWidth: c.clientWidth, clientHeight: c.clientHeight,
          display: getComputedStyle(c).display,
          pixelRatio: c.clientWidth ? Math.round((c.width / c.clientWidth) * 100) / 100 : null,
        }
      : null,
    nav: {
      ttfb: round(nav.responseStart),
      domContentLoaded: round(nav.domContentLoadedEventEnd),
      load: round(nav.loadEventEnd),
      domInteractive: round(nav.domInteractive),
    },
    story: {
      scrollHeight: document.documentElement.scrollHeight,
      panels: document.querySelectorAll('section.panel').length,
      firstHeading: ((document.querySelector('#s1 h1') || {}).textContent || '').trim() || null,
      loadingClass: document.body.classList.contains('is-loading'),
      statSources: (document.querySelector('[data-stat="sources"]') || {}).textContent || null,
    },
    resources: res.map((r) => ({
      name: r.name, type: r.initiatorType, dur: round(r.duration),
      size: r.transferSize || 0,
      thirdParty: r.name.indexOf('127.0.0.1') === -1 && r.name.indexOf('localhost') === -1,
    })),
  };
}
"""


def _stats(frames: list[float]) -> dict:
    if len(frames) < 3:
        return {"frames": len(frames), "avg_fps": None, "valid": False, "reason": "帧数不足"}
    deltas = [frames[i + 1] - frames[i] for i in range(len(frames) - 1)]
    inst = [1000.0 / d for d in deltas if d > 0]
    span = frames[-1] - frames[0]
    rnd = lambda v: round(v, 1)  # noqa: E731
    return {
        "frames": len(frames),
        "span_ms": rnd(span),
        "avg_fps": rnd((len(frames) - 1) / span * 1000) if span > 0 else None,
        "p50_fps": rnd(statistics.median(inst)),
        "p5_fps": rnd(sorted(inst)[max(0, int(len(inst) * 0.05) - 1)]),
        "min_fps": rnd(min(inst)),
        "avg_frame_ms": rnd(span / max(1, len(frames) - 1)),
        "max_frame_ms": rnd(max(deltas)),
        "below_30_ratio": round(sum(1 for v in inst if v < 30) / len(inst), 4),
        "below_26_ratio": round(sum(1 for v in inst if v < 26) / len(inst), 4),
        "valid": True,
    }


def _interpret(baseline: float | None, measured: float | None) -> str:
    """按「同模式空白页基线」解释帧率含义，避免把无 vsync 的数字当显示帧率。"""
    if not baseline or not measured:
        return "基线或测量缺失"
    if baseline > 150:
        return ("unthrottled（无 vsync 约束，非显示帧率）：测得 {:.0f} fps ⇒ 单帧开销 ≤{:.2f} ms，"
                "可作为 60Hz 16.7ms 预算下的余量证据".format(measured, 1000.0 / measured))
    if baseline >= measured * 0.75:
        return ("vsync 受限（真实显示帧率）：基线 {:.0f} fps，滚动 {:.0f} fps ⇒ 3D 场景未造成掉帧"
                .format(baseline, measured))
    return ("vsync 受限但滚动期下降：基线 {:.0f} fps，滚动 {:.0f} fps（保留率 {:.0%}）"
            .format(baseline, measured, measured / baseline))


# 模拟「境内直连」：把 Google Fonts 域名解析到不可路由地址 → TCP SYN 挂起（非快速失败）。
# 为什么必须单列：`route.abort()` 是**快速失败**，而真实被墙网络是**挂起**；
# render-blocking 样式表在挂起时会一直阻塞首屏渲染，直到 TCP 超时 —— 两者后果量级不同。
HANG_ARGS = [
    "--host-resolver-rules=MAP fonts.googleapis.com 10.255.255.1, "
    "MAP fonts.gstatic.com 10.255.255.1",
]


def _launch(pw, headed: bool, extra_args: list | None = None):
    args = list(GL_ARGS) + list(extra_args or [])
    return pw.chromium.launch(channel="msedge", headless=not headed, args=args)


# ─────────────────────────── 路一：加载时间矩阵（headless） ───────────────────────────

def run_load(pw, name, viewport, dpr, block_fonts=False, reduced_motion=False,
             warm=False, hang_fonts=False, route_hang=False, timeout_ms=45000) -> dict:
    result = {"scenario": name, "viewport": viewport, "dpr": dpr,
              "block_fonts": block_fonts,
              "hang_mode": "resolver" if hang_fonts else ("route-pending" if route_hang else "none"),
              "warm": warm}
    browser = None
    t0 = time.time()
    try:
        browser = _launch(pw, headed=False, extra_args=HANG_ARGS if hang_fonts else None)
        ctx = browser.new_context(
            viewport=viewport, device_scale_factor=dpr,
            reduced_motion="reduce" if reduced_motion else "no-preference")
        page = ctx.new_page()
        failed, cerr = [], []
        page.on("requestfailed", lambda r: failed.append({"url": r.url, "err": (r.failure or "")}))
        page.on("console", lambda m: cerr.append(m.text) if m.type == "error" else None)
        if block_fonts:
            for pat in ("**/fonts.googleapis.com/**", "**/fonts.gstatic.com/**"):
                page.route(pat, lambda route: route.abort())
        if route_hang:
            # 挂起法：注册处理器但**不**继续/中止/响应 ⇒ 请求永久 pending。
            # 这才是 render-blocking 样式表在「被墙/丢包」网络下的真实形态（非快速失败）。
            for pat in ("**/fonts.googleapis.com/**", "**/fonts.gstatic.com/**"):
                page.route(pat, lambda route: None)
        page.add_init_script(INIT_PROBE)
        if hang_fonts or route_hang:
            # 挂起场景：不能等 load（会被阻塞的样式表拖住），改为 commit 后逐秒采样时间线
            page.goto(TARGET_URL, wait_until="commit", timeout=timeout_ms)
            t_start = time.time()
            timeline = []
            while time.time() - t_start < 40:
                try:
                    probe = page.evaluate(
                        "() => ({fcp: (window.__perf||{}).fcp, boot: (window.__perf||{}).bootDone,"
                        " loading: document.body ? document.body.classList.contains('is-loading') : null,"
                        " panels: document.querySelectorAll('section.panel').length,"
                        " h1_in_dom: !!((document.querySelector('#s1 h1')||{}).textContent||'').trim()})")
                except Exception:  # noqa: BLE001
                    probe = None
                row = {"t_wall_ms": int((time.time() - t_start) * 1000)}
                row.update(probe or {"probe_failed": True})
                timeline.append(row)
                if probe and probe.get("boot") is not None:
                    break
                page.wait_for_timeout(500)
            result["hang_timeline"] = timeline
            try:
                page.wait_for_load_state("load", timeout=timeout_ms)
            except Exception as exc:  # noqa: BLE001
                result["load_state_error"] = "{}: {}".format(type(exc).__name__, exc)
            page.wait_for_timeout(1500)
        else:
            page.goto(TARGET_URL, wait_until="load", timeout=timeout_ms)
            page.wait_for_timeout(2000)
        if warm:
            page.reload(wait_until="load", timeout=timeout_ms)
            page.wait_for_timeout(2000)
        snap = page.evaluate(SNAPSHOT)
        result.update({
            "perf": snap["perf"], "nav": snap["nav"], "scene": snap["scene"],
            "canvas": snap["canvas"], "story": snap["story"],
            "resources": snap["resources"],
            "request_failed": failed, "console_errors": cerr[:10],
        })
        ctx.close()
    except Exception as exc:  # noqa: BLE001
        result["error"] = "{}: {}".format(type(exc).__name__, exc)
    finally:
        if browser:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    result["elapsed_s"] = round(time.time() - t0, 2)
    return result


# ─────────────────────────── 路二：帧率（双模式）+ 降级触发 ───────────────────────────

def _control(pw, headed: bool) -> dict:
    """同模式空白页 rAF 基线：判定该模式的 rAF 是否受 vsync 约束。"""
    browser = _launch(pw, headed)
    try:
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        page.add_init_script(INIT_PROBE)
        page.goto("about:blank")
        page.evaluate("() => window.__fps.begin()")
        page.wait_for_timeout(4000)
        return _stats(page.evaluate("() => window.__fps.end()"))
    finally:
        browser.close()


def run_fps(pw, headed: bool, dpr: float = 1, throttle_rate: float | None = None) -> dict:
    """一轮帧率测量：空白基线 → 3D 滚动 →（可选）CPU 施压下的降级触发。"""
    mode = "headed-vsync" if headed else "headless-unthrottled"
    out: dict = {"mode": mode, "dpr": dpr, "throttle_rate": throttle_rate}
    browser = None
    t0 = time.time()
    try:
        out["control"] = _control(pw, headed)
        out["control_interpretation"] = (
            "空白页基线 {:.0f} fps ⇒ {}".format(
                out["control"]["avg_fps"] or 0,
                "rAF 受 vsync 约束，可作显示帧率参照" if (out["control"]["avg_fps"] or 0) <= 150
                else "rAF 未受 vsync 约束（无头模式），帧率不可当显示帧率"))
        browser = _launch(pw, headed)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  device_scale_factor=dpr)
        page = ctx.new_page()
        page.add_init_script(INIT_PROBE)
        page.goto(TARGET_URL, wait_until="load", timeout=45000)
        page.wait_for_timeout(2000)

        before = page.evaluate(SNAPSHOT)
        out["scene"] = before["scene"]
        out["canvas_before"] = before["canvas"]
        if not before["scene"]["ok"]:
            out["reason"] = "3D 未启动（{}），本场景不产帧率".format(before["scene"]["reason"])
            ctx.close()
            return out

        cdp = ctx.new_cdp_session(page)
        if throttle_rate:
            try:
                cdp.send("Emulation.setCPUThrottlingRate", {"rate": throttle_rate})
                out["cpu_throttle"] = "applied x{}".format(throttle_rate)
            except Exception as exc:  # noqa: BLE001
                out["cpu_throttle"] = "unsupported: {}".format(exc)
                throttle_rate = None

        # 常规滚动
        if not throttle_rate:
            page.evaluate("() => window.__fps.begin()")
            page.evaluate(SCROLL_THROUGH, 7000)
            frames = page.evaluate("() => window.__fps.end()")
            out["scroll"] = _stats(frames)
            out["scroll"]["interpretation"] = _interpret(
                out["control"].get("avg_fps"), out["scroll"].get("avg_fps"))
        else:
            # 施压滚动：目的不是测帧率，而是逼出「连续 <26fps」以验证自适应降级
            page.evaluate("() => window.__fps.begin()")
            page.evaluate(SCROLL_THROUGH, 8000)
            frames = page.evaluate("() => window.__fps.end()")
            out["stressed_scroll"] = _stats(frames)

        after = page.evaluate(SNAPSHOT)
        out["canvas_after"] = after["canvas"]
        cb, ca = before["canvas"] or {}, after["canvas"] or {}
        out["pixel_ratio_change"] = {"before": cb.get("pixelRatio"), "after": ca.get("pixelRatio")}
        out["adaptive_downscale_triggered"] = bool(
            cb.get("pixelRatio") and ca.get("pixelRatio") and ca["pixelRatio"] < cb["pixelRatio"])
        ctx.close()
    except Exception as exc:  # noqa: BLE001
        out["error"] = "{}: {}".format(type(exc).__name__, exc)
    finally:
        if browser:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    out["elapsed_s"] = round(time.time() - t0, 2)
    return out


DEGRADE_INJECT = r"""
() => {
  const c = document.getElementById('scene');
  const ratio = () => (c && c.clientWidth) ? Math.round((c.width / c.clientWidth) * 100) / 100 : null;
  const info = { before: ratio(), patch: null, three: !!(window.THREE && window.THREE.Clock) };
  if (window.THREE && THREE.Clock && THREE.Clock.prototype.getDelta) {
    THREE.Clock.prototype.getDelta = function () { return 1.0; };
    info.patch = 'THREE.Clock.prototype.getDelta -> 1.0（经 L256 clamp 后 dt=0.05s ⇒ 代码内计得 20fps < 26fps 阈值）';
  }
  return info;
}
"""

DEGRADE_PROBE = r"""
() => {
  const c = document.getElementById('scene');
  const st = window.__sceneStatus || {};
  return {
    ratio: (c && c.clientWidth) ? Math.round((c.width / c.clientWidth) * 100) / 100 : null,
    canvasWidth: c ? c.width : null,
    clientWidth: c ? c.clientWidth : null,
    scene: { ok: !!st.ok, reason: st.reason || null },
  };
}
"""


def run_degrade(pw) -> dict:
    """确定性验证低帧率自适应降级分支（`src/web/js/scene.js` L259-268）。

    为什么不靠「真实卡顿」：本机 rAF 调度下限约 3.3ms（空白基线 295fps），
    连 CPU 限速 x100 都压不到 26fps 以下（见 fps_runs 的限速扫描）——
    **真实低帧率在本环境不可构造**。因此改用注入法：把 `Clock.getDelta` 固定为 1.0s，
    经 L256 的 clamp 后 dt=0.05s ⇒ 代码内计得 fps=20 < 26，连续 24 帧后应触发
    `setPixelRatio(1)`。这验证「分支逻辑 + 接线」（_loop 真的消费 dt、真的调了 setPixelRatio），
    属**机制验证**，不等同「真实低端设备实测」，报告须如实标注。
    """
    out: dict = {"mode": "degrade-injection", "dpr": 2}
    browser = None
    t0 = time.time()
    try:
        browser = _launch(pw, headed=False)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page = ctx.new_page()
        page.add_init_script(INIT_PROBE)
        page.goto(TARGET_URL, wait_until="load", timeout=45000)
        page.wait_for_timeout(2000)
        before = page.evaluate(DEGRADE_PROBE)
        out["before"] = before
        if not before["scene"]["ok"]:
            out["reason"] = "3D 未启动，无法验证降级分支"
            ctx.close()
            return out
        out["injection"] = page.evaluate(DEGRADE_INJECT)
        page.wait_for_timeout(2000)   # 300fps 下 24 帧仅约 80ms，2s 足以跨过阈值
        out["after"] = page.evaluate(DEGRADE_PROBE)
        out["triggered"] = bool(before.get("ratio") and out["after"].get("ratio")
                                and out["after"]["ratio"] < before["ratio"])
        ctx.close()
    except Exception as exc:  # noqa: BLE001
        out["error"] = "{}: {}".format(type(exc).__name__, exc)
    finally:
        if browser:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    out["elapsed_s"] = round(time.time() - t0, 2)
    return out


def main() -> int:
    import urllib.request
    try:
        with urllib.request.urlopen(TARGET_URL.rstrip("/") + "/api/health", timeout=10) as r:
            health = json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print("服务未就绪，先运行 `python src/app.py`。原因：{}".format(exc), file=sys.stderr)
        return 2

    desktop = {"width": 1440, "height": 900}
    mobile = {"width": 375, "height": 812}

    out = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "target_url": TARGET_URL,
            "browser": "Microsoft Edge (playwright channel=msedge)",
            "gl_note": "headless 走 SwiftShader 软件光栅；headed 使用真实 GPU 与合成器",
            "health": {k: health.get(k) for k in
                       ("ok", "model_available", "readonly", "sessions_tracked",
                        "refuse_threshold", "min_original_hits")},
            "health_stats": health.get("stats"),
        },
        "load_matrix": [],
        "fps_runs": [],
    }

    with sync_playwright() as pw:
        print("── 路一：加载时间矩阵（headless） ──", flush=True)
        for s in [
            dict(name="A-desktop-1440-cold-fonts-on", vp=desktop, dpr=1),
            dict(name="B-mobile-375-cold-fonts-on", vp=mobile, dpr=2),
            dict(name="C-desktop-1440-cold-fonts-blocked", vp=desktop, dpr=1, block_fonts=True),
            dict(name="D-mobile-375-cold-fonts-blocked", vp=mobile, dpr=2, block_fonts=True),
            dict(name="C2-desktop-1440-cold-fonts-HANG-RESOLVER", vp=desktop, dpr=1,
                 hang_fonts=True, timeout_ms=70000),
            dict(name="D2-mobile-375-cold-fonts-HANG-RESOLVER", vp=mobile, dpr=2,
                 hang_fonts=True, timeout_ms=70000),
            dict(name="C3-desktop-1440-cold-fonts-HANG-ROUTE", vp=desktop, dpr=1,
                 route_hang=True, timeout_ms=30000),
            dict(name="D3-mobile-375-cold-fonts-HANG-ROUTE", vp=mobile, dpr=2,
                 route_hang=True, timeout_ms=30000),
            dict(name="E-desktop-1440-warm-fonts-on", vp=desktop, dpr=1, warm=True),
            dict(name="F-desktop-1440-reduced-motion", vp=desktop, dpr=1, reduced_motion=True),
        ]:
            if PERF_ONLY and not any(k in s["name"] for k in PERF_ONLY):
                continue
            print("→ {}".format(s["name"]), flush=True)
            kw = {k: v for k, v in s.items() if k not in ("name", "vp", "dpr")}
            out["load_matrix"].append(run_load(pw, s["name"], s["vp"], s["dpr"], **kw))

        run_fps_too = (not PERF_ONLY) or any("FPS" in k.upper() for k in PERF_ONLY)
        if run_fps_too:
            print("── 路二：帧率（headless 无节流） ──", flush=True)
            out["fps_runs"].append(run_fps(pw, headed=False, dpr=1))

            print("── 路二附：CPU 限速扫描（验证真实低帧率能否构造） ──", flush=True)
            for rate in (8, 20, 100):
                print("→ throttle x{}".format(rate), flush=True)
                out["fps_runs"].append(run_fps(pw, headed=False, dpr=2, throttle_rate=rate))

            print("── 路二附：降级分支确定性注入验证（DPR2） ──", flush=True)
            out["degrade_injection"] = run_degrade(pw)
        else:
            print("（已按 PERF_ONLY 跳过帧率与降级测量）", flush=True)

        if run_fps_too and not NO_HEADED:
            print("── 路二：帧率（headed 真实 vsync）── 会短暂弹出浏览器窗口 ──", flush=True)
            out["fps_runs"].append(run_fps(pw, headed=True, dpr=1))
        elif not run_fps_too:
            pass
        else:
            out["fps_runs"].append({"mode": "headed-vsync", "skipped": "NO_HEADED=1"})

    # 事后判定：headless 是否无 vsync（决定帧率字段的解释口径）
    headless_control = next(
        (r.get("control", {}).get("avg_fps") for r in out["fps_runs"]
         if r.get("mode") == "headless-unthrottled"), None)
    out["meta"]["headless_control_fps"] = headless_control
    out["meta"]["headless_unthrottled"] = bool(headless_control and headless_control > 150)
    if not headless_control:
        out["meta"]["fps_usage_rule"] = "本次为补测（未跑帧率），无帧率口径结论"
    else:
        out["meta"]["fps_usage_rule"] = (
            "headless 帧率仅作「每帧开销上限」证据；写入材料的滚动帧率必须取 headed-vsync 口径"
            if out["meta"]["headless_unthrottled"] else "headless 帧率可直接作为显示帧率参照")

    os.makedirs(OUT_DIR, exist_ok=True)
    tag = "-part" if PERF_ONLY else ""
    path = os.path.join(OUT_DIR, "perf-{}{}.json".format(datetime.now().strftime("%Y%m%d-%H%M%S"), tag))
    out["meta"]["perf_only"] = PERF_ONLY or None
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("\n================ 加载时间矩阵 ================")
    for s in out["load_matrix"]:
        if s.get("error"):
            print("[{}] ERROR {}".format(s["scenario"], s["error"]))
            continue
        p, n = s["perf"], s["nav"]
        r1 = lambda v: round(v, 1) if isinstance(v, (int, float)) else None  # noqa: E731
        print("[{}] TTFB={} DCL={} load={} FCP={} LCP={} 首屏可读={} 3D就绪={} | 资源{} 第三方{} 失败{} | 3D ok={} | 滚动高度={}".format(
            s["scenario"], n.get("ttfb"), n.get("domContentLoaded"), n.get("load"),
            r1(p.get("fcp")), r1(p.get("lcp")), r1(p.get("bootDone")), r1(p.get("sceneReady")),
            len(s["resources"]), sum(1 for x in s["resources"] if x["thirdParty"]),
            len(s["request_failed"]), s["scene"]["ok"], s["story"]["scrollHeight"]))
        if s.get("hang_timeline"):
            tl = s["hang_timeline"]
            first_dom = next((r["t_wall_ms"] for r in tl if r.get("h1_in_dom")), None)
            first_paint = next((r["t_wall_ms"] for r in tl if r.get("fcp") is not None), None)
            first_boot = next((r["t_wall_ms"] for r in tl if r.get("boot") is not None), None)
            tail = tl[-1] if tl else {}
            print("    挂起时间线（共观测 {}ms）：标题进 DOM={}ms ｜ **首次绘制={}ms** ｜ 首屏可读={}ms"
                  " ｜ 观测末态 panels={} loading={} ｜ load 报错={}".format(
                      tail.get("t_wall_ms"), first_dom, first_paint, first_boot,
                      tail.get("panels"), tail.get("loading"),
                      s.get("load_state_error", "无")))

    print("\n================ 帧率 ================")
    for r in out["fps_runs"]:
        if r.get("skipped"):
            print("[{}] 跳过（{}）".format(r["mode"], r["skipped"]))
            continue
        if r.get("error"):
            print("[{}] ERROR {}".format(r["mode"], r["error"]))
            continue
        ctl = r.get("control", {}).get("avg_fps")
        print("[{}] 空白基线={} fps ｜ {}".format(r["mode"], ctl, r.get("control_interpretation")))
        if r.get("scroll"):
            f = r["scroll"]
            print("    滚动 avg={} p50={} p5={} min={} 单帧均值={}ms ｜ <30fps={:.1%} <26fps={:.1%}".format(
                f["avg_fps"], f["p50_fps"], f["p5_fps"], f["min_fps"], f["avg_frame_ms"],
                f["below_30_ratio"], f["below_26_ratio"]))
            print("    → {}".format(f.get("interpretation")))
        if r.get("stressed_scroll"):
            f = r["stressed_scroll"]
            print("    CPU 限速 x{} 下：avg={} fps ｜ <30fps={:.1%} <26fps={:.1%} ｜ 像素比 {} → {} ｜ 自适应降级触发={}".format(
                r.get("throttle_rate"), f["avg_fps"], f["below_30_ratio"], f["below_26_ratio"],
                r["pixel_ratio_change"]["before"], r["pixel_ratio_change"]["after"],
                r.get("adaptive_downscale_triggered")))
        if r.get("reason"):
            print("    {}".format(r["reason"]))

    print("\nheadless 无 vsync 判定：{} ｜ 口径：{}".format(
        out["meta"]["headless_unthrottled"], out["meta"]["fps_usage_rule"]))

    d = out.get("degrade_injection") or {}
    if not d:
        pass
    elif d.get("error"):
        print("\n[降级注入验证] ERROR {}".format(d["error"]))
    else:
        print("\n[降级注入验证] 注入={} ｜ 像素比 {} → {} ｜ 触发={}".format(
            d.get("injection", {}).get("patch"),
            (d.get("before") or {}).get("ratio"), (d.get("after") or {}).get("ratio"),
            d.get("triggered")))
        if d.get("before"):
            print("    canvas 物理宽 {} → {}（CSS 宽 {}）".format(
                d["before"].get("canvasWidth"), (d.get("after") or {}).get("canvasWidth"),
                d["before"].get("clientWidth")))
    print("原始数据已落盘：{}".format(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
