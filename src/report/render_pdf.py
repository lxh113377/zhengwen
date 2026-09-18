"""证闻 · 《应用方案》PDF 渲染（HTML → PDF）

为什么用 HTML 打印而不是直接写 PDF：
  中文排版（换行、字距、表格）用 HTML/CSS 控制最稳，且源文件可版本化、可 diff。
  Chromium 打印能保证中文不丢字、页码与页眉页脚可控。

硬约束：
  官方要求 PDF ≤ 20 页 —— 本脚本渲染后**自动校验页数**，超过 20 页直接报错退出（不产出发包）。

用法：
    python report/render_pdf.py
    python report/render_pdf.py --out ../../交付物/提交包/证闻-应用方案-2026-09-30.pdf
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import zlib

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # src/
REPORT_DIR = os.path.join(BASE_DIR, "report")
HTML_PATH = os.path.join(REPORT_DIR, "plan.html")
ASSETS_DIR = os.path.join(REPORT_DIR, "assets")
SHOTS_DIR = os.path.abspath(
    os.path.join(BASE_DIR, "..", "交付物", "提交包", "工程佐证材料", "06-运行实拍截图")
)
DEFAULT_OUT = os.path.abspath(
    os.path.join(BASE_DIR, "..", "交付物", "提交包", "证闻-应用方案-2026-09-30.pdf")
)

MAX_PAGES = 20

# 方案中引用的截图（正文 7.2 节）
FIGURES = [
    "viewport-1440-s1.png",
    "viewport-768-s1.png",
    "viewport-375-s3.png",
    "reduced-motion-1440.png",
]


def sync_assets() -> None:
    """把实拍截图复制到 report/assets/（相对引用，避免把本机绝对路径带进产物）。"""
    os.makedirs(ASSETS_DIR, exist_ok=True)
    for name in FIGURES:
        src = os.path.join(SHOTS_DIR, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(ASSETS_DIR, name))
        else:
            print(f"  ! 缺少截图：{name}（方案中将出现空白图位）", file=sys.stderr)


def count_pages(pdf_path: str) -> int:
    """统计 PDF 页数：优先解压对象流里的 /Type /Page（Chromium 输出通常为压缩流）。"""
    with open(pdf_path, "rb") as fh:
        raw = fh.read()

    # ① 未压缩对象
    pages = len(re.findall(rb"/Type\s*/Page[^s]", raw))
    if pages:
        return pages

    # ② 压缩对象流：逐个 stream 试解压
    total = 0
    for chunk in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
        for data in (chunk, chunk.rstrip(b"\r\n")):
            try:
                text = zlib.decompress(data)
            except zlib.error:
                continue
            total += len(re.findall(rb"/Type\s*/Page[^s]", text))
            break
    if total:
        return total

    # ③ 兜底：/Count N（页树节点）
    m = re.findall(rb"/Count\s+(\d+)", raw)
    return max((int(x) for x in m), default=0)


def _launch(p):
    """按「内置 chromium → 系统 Edge → 系统 Chrome」顺序尝试，避免强制下载浏览器。

    实测（2026-09-18）：本机 playwright 未下载内置 chromium，但系统装有 Edge，
    用 channel="msedge" 可直接复用，无需 130MB 下载。
    """
    attempts = [
        ("内置 chromium", {}),
        ("系统 Microsoft Edge", {"channel": "msedge"}),
        ("系统 Google Chrome", {"channel": "chrome"}),
    ]
    last_err: Exception | None = None
    for label, kwargs in attempts:
        try:
            browser = p.chromium.launch(**kwargs)
            print(f"  浏览器：{label}")
            return browser
        except Exception as err:  # noqa: BLE001 — 逐个降级，最后抛出
            last_err = err
            first = str(err).splitlines()[0][:90] if str(err) else err.__class__.__name__
            print(f"  ! {label} 不可用：{first}", file=sys.stderr)
    raise RuntimeError("无可用的浏览器内核（请安装 Edge/Chrome，或执行 playwright install chromium）") from last_err


def render(out_path: str) -> None:
    from playwright.sync_api import sync_playwright

    sync_assets()
    url = "file:///" + HTML_PATH.replace(os.sep, "/")

    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page()
        page.goto(url, wait_until="load")
        # 等图片解码完成，避免 PDF 里出现空白图
        page.wait_for_timeout(1200)
        page.pdf(
            path=out_path,
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template='<div style="font-size:7pt;color:#8a8a92;width:100%;padding:0 14mm;">证闻 · 对话式新闻智能体 · 应用方案</div>',
            footer_template=(
                '<div style="font-size:7pt;color:#8a8a92;width:100%;padding:0 14mm;'
                'display:flex;justify-content:space-between;">'
                "<span>2026 年 iCAN 大学生创新创业大赛 · AI 应用创新挑战赛 · 软件赛道 · 高校组</span>"
                '<span>第 <span class="pageNumber"></span> / <span class="totalPages"></span> 页</span></div>'
            ),
            margin={"top": "16mm", "bottom": "16mm", "left": "14mm", "right": "14mm"},
        )
        browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="渲染《应用方案》PDF 并校验页数")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    print(f"渲染：{HTML_PATH}")
    render(out)

    size_kb = os.path.getsize(out) / 1024
    pages = count_pages(out)
    print(f"已生成：{out}")
    print(f"体积：{size_kb:.1f} KB ｜ 页数：{pages} ｜ 上限：{MAX_PAGES}")

    if pages == 0:
        print("  ! 页数解析失败（无法确认是否满足 ≤20 页），请人工核对", file=sys.stderr)
        sys.exit(2)
    if pages > MAX_PAGES:
        print(f"  ✗ 超出官方上限（{pages} > {MAX_PAGES}）—— 必须压缩内容后重新渲染", file=sys.stderr)
        sys.exit(1)
    print("  ✓ 页数符合官方要求（≤20 页）")


if __name__ == "__main__":
    main()
