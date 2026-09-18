"""证闻 · 滚动 → 3D 联动回归套件（可复跑）

为什么需要它（2026-09-19 真实缺陷，普通冒烟测不出来）
  用户报障：「滚动条拖到前一半时 3D 背景会跟着动，后半段就不动了」。
  根因：`#story` 的高度是 **/api/graph 返回后**才定型的（renderDivergences 往 #s3 注入分歧卡，
  整页由 5×vh 涨到 6616px）；而 ScrollTrigger 只在 load / resize / visibilitychange 重算 start/end，
  **DOM 内容注入不会触发重算**。于是：
    · 正常网络（load 晚于注入）→ end=5714（正确）；
    · fonts.googleapis.com 挂起（境内典型，load 永不触发）或接口晚于 load → end 停在 3600，
      滚动条过 63% 后 progress 恒为 1 → 3D「后半段不动」。
  ⇒ 这个缺陷**只在特定网络时序下出现**，用「打开页面看一眼」的方式永远测不出来，
    必须做成带**干扰注入**的回归套件。

测什么（判据）
  对三种时序场景分别沿滚动条采样 `scene.progress` 与**3D 节点的屏幕投影位移均值**：
    A 正常网络          —— 基准
    B 字体域名挂起       —— load 永不触发（用 page.route 挂起，非 abort；abort 是快速失败，量级不同）
    C 接口晚于 load      —— 人为延迟 /api/graph 5s，使内容注入发生在 load 之后
  判据：三场景都必须「progress 全程 0→1 单调」且「后 40% 位移均值 > 5px」。
        任一场景不满足 = 3D 未全程跟随滚动条 → FAIL。

用法：
  python src/app.py                                  # 另开终端
  python src/tools/verify_scroll_coupling.py         # 退出码 0=通过 / 1=失败 / 2=环境不满足
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    print("需要 playwright：pip install playwright", file=sys.stderr)
    raise SystemExit(2)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # src/
PROJECT_DIR = os.path.dirname(BASE_DIR)                                          # 项目根
OUT_DIR = os.path.join(PROJECT_DIR, "交付物", "提交包", "工程佐证材料",
                       "03-工程验证证据", "raw")

GL_ARGS = ["--enable-unsafe-swiftshader", "--disable-dev-shm-usage"]
STEPS = 20            # 滚动采样步数
SETTLE_MS = 420       # 每步等待（进度是 lerp 收敛的，需给足时间）
MIN_TAIL_MOVE_PX = 5  # 后 40% 的平均屏幕位移下限

# 把 scene 实例挂到 window（app.js 内部持有，不挂出来只能靠投影反推，无法直接断言 progress）
HOOK = r"""
(() => {
  Object.defineProperty(window, 'ZhengwenScene', {
    configurable: true,
    get() { return undefined; },
    set(v) {
      try {
        const orig = v.boot;
        v.boot = function (g) { const s = orig.call(v, g); window.__scene = s; return s; };
      } catch (e) {}
      Object.defineProperty(window, 'ZhengwenScene', { value: v, writable: true, configurable: true });
    }
  });
})();
"""

PROBE = r"""
() => {
  const sc = window.__scene;
  const w = window.innerWidth, h = window.innerHeight;
  const pts = [];
  if (sc) {
    sc.group.updateMatrixWorld(true);
    sc.group.children.forEach(function (child) {
      if (!child.isPoints) return;
      const arr = child.geometry.attributes.position.array;
      for (let i = 0; i < arr.length; i += 3) {
        const v = new THREE.Vector3(arr[i], arr[i + 1], arr[i + 2]);
        v.applyMatrix4(child.matrixWorld).project(sc.camera);
        pts.push([(v.x * 0.5 + 0.5) * w, (-v.y * 0.5 + 0.5) * h]);
      }
    });
  }
  return {
    scrollY: Math.round(window.scrollY),
    maxScroll: document.documentElement.scrollHeight - window.innerHeight,
    progress: sc ? Math.round(sc.progress * 1000) / 1000 : null,
    sceneOk: !!(window.__sceneStatus || {}).ok,
    pts: pts,
  };
}
"""


def run_case(pw, *, hang_fonts: bool, graph_delay_ms: int, base: str, label: str,
             early_scroll: bool = False) -> dict:
    browser = pw.chromium.launch(channel="msedge", headless=True, args=GL_ARGS)
    out: dict = {"scenario": label, "hang_fonts": hang_fonts, "graph_delay_ms": graph_delay_ms,
                 "early_scroll": early_scroll}
    try:
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.add_init_script(HOOK)
        if hang_fonts:
            # 挂起法：注册处理器但**不调用** continue/abort/fulfill ⇒ 请求永久 pending。
            # 这才是被墙/丢包网络下 render-blocking 资源的真实形态（abort 是快速失败，后果量级不同）。
            for pat in ("**/fonts.googleapis.com/**", "**/fonts.gstatic.com/**"):
                page.route(pat, lambda route: None)
        if graph_delay_ms:
            def _slow(route):
                time.sleep(graph_delay_ms / 1000.0)
                route.continue_()
            page.route("**/api/graph", _slow)
        page.goto(base, wait_until="commit", timeout=60000)
        if early_scroll:
            # D 场景（2026-09-19 新增，用户真实操作形态）：
            # 用户**在内容注入之前就开始滚**——先滚到当时的最大滚动位置附近，
            # 等接口返回、页面变高（#s3 分歧卡注入），再进入统一采样。
            # 这是「后半段 3D 不动且**概率性**出现」最贴近的复现路径：
            # 旧实现只信 #story 几何，页面变高那一刻进度就会被算错并提前停在 1。
            page.wait_for_timeout(1200)
            before = page.evaluate(
                "() => (document.scrollingElement || document.documentElement).scrollHeight - window.innerHeight")
            out["max_scroll_before_inject"] = before
            page.evaluate("(y) => window.scrollTo(0, y)", int(before * 0.55))
            page.wait_for_timeout(graph_delay_ms + 2500)
        else:
            page.wait_for_timeout(8000)

        first = page.evaluate(PROBE)
        out["scene_ok"] = first["sceneOk"]
        maxs = first["maxScroll"]
        out["max_scroll"] = maxs
        out["page_grew_px"] = maxs - out.get("max_scroll_before_inject", maxs)
        progresses, moves = [], []
        prev = None
        for i in range(STEPS + 1):
            page.evaluate("(y) => window.scrollTo(0, y)", round(maxs * i / STEPS))
            page.wait_for_timeout(SETTLE_MS)
            s = page.evaluate(PROBE)
            progresses.append(s["progress"])
            if prev is not None and s["pts"] and len(prev["pts"]) == len(s["pts"]):
                d = sorted(math.dist(a, b) for a, b in zip(prev["pts"], s["pts"]))
                moves.append(round(sum(d) / len(d), 2))
            prev = s
        out["progress_series"] = progresses
        out["move_series_px"] = moves
        tail = moves[int(len(moves) * 0.6):] if moves else []
        out["tail_move_avg_px"] = round(sum(tail) / len(tail), 2) if tail else 0.0
        out["monotonic_0_to_1"] = bool(
            progresses and progresses[0] is not None
            and abs(progresses[0]) < 0.05 and progresses[-1] is not None
            and progresses[-1] > 0.95)
        out["pass"] = bool(out["monotonic_0_to_1"] and out["tail_move_avg_px"] >= MIN_TAIL_MOVE_PX)
        ctx.close()
    except Exception as exc:  # noqa: BLE001
        out["error"] = "{}: {}".format(type(exc).__name__, exc)
        out["pass"] = False
    finally:
        browser.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("TARGET_URL", "http://127.0.0.1:8848"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    base = args.base.rstrip("/") + "/"

    import urllib.request
    # ⚠️ 2026-09-19 实测：云沙箱代理对默认 UA「Python-urllib/3.x」直接回 HTTP 400 ——
    # 前置检查自身被挡会被误读成「服务未就绪」（工具缺陷，不是目标服务缺陷）。
    # 与 live.py 同一纪律：出网请求必须带浏览器 UA。
    try:
        req = urllib.request.Request(
            base.rstrip("/") + "/api/health",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=15) as r:
            json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print("服务未就绪，先运行 `python src/app.py`。原因：{}".format(exc), file=sys.stderr)
        return 2

    cases = [
        dict(label="A-正常网络", hang_fonts=False, graph_delay_ms=0, early_scroll=False),
        dict(label="B-字体域名挂起(load 不触发)", hang_fonts=True, graph_delay_ms=0, early_scroll=False),
        dict(label="C-接口晚于 load(内容注入晚于刷新)", hang_fonts=False, graph_delay_ms=5000,
             early_scroll=False),
        dict(label="D-先滚动后注入(用户真实操作)", hang_fonts=False, graph_delay_ms=5000,
             early_scroll=True),
    ]

    results = []
    with sync_playwright() as pw:
        for c in cases:
            r = run_case(pw, hang_fonts=c["hang_fonts"], graph_delay_ms=c["graph_delay_ms"],
                         base=base, label=c["label"], early_scroll=c["early_scroll"])
            results.append(r)
            print("[{}] {} ｜ 末值progress={} ｜ 后40%位移均值={}px ｜ 场景就绪={}{}".format(
                "✅" if r.get("pass") else "❌", c["label"],
                (r.get("progress_series") or [None])[-1], r.get("tail_move_avg_px"),
                r.get("scene_ok"), " ｜ " + r["error"] if r.get("error") else ""))

    failed = [r["scenario"] for r in results if not r.get("pass")]
    payload = {
        "meta": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "base_url": base,
            "browser": "Microsoft Edge (playwright channel=msedge)",
            "viewport": {"width": 1440, "height": 900},
            "steps": STEPS,
            "settle_ms": SETTLE_MS,
            "criteria": {
                "monotonic_0_to_1": "第 1 步 progress < 0.05 且末步 > 0.95",
                "tail_move_avg_px": ">= {} px（后 40% 滚动区间内 3D 节点屏幕位移均值）".format(MIN_TAIL_MOVE_PX),
            },
            "pass": not failed,
            "failed": failed,
        },
        "cases": results,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "scroll-coupling-{}.json".format(datetime.now().strftime("%Y%m%d-%H%M%S")))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    if args.json:
        print(json.dumps(payload["meta"], ensure_ascii=False))
    print()
    print("结论：{}".format("全部通过（3/3 场景 3D 全程跟随滚动条）" if not failed
                          else "失败场景：{}".format(" ｜ ".join(failed))))
    print("原始数据已落盘：{}".format(path))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
