#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CNIPA 商标公告检索 —— 正式取数。

数据源：国家政务服务平台「商标公告查询服务」，页面明示
        「本服务由国家知识产权局提供」（https://app.gjzwfw.gov.cn/jmopen/webapp/html5/sbggcxfw/index.html）

产出：原始 XHR 响应 + 渲染后表格文本 → JSON，供材料证据链引用。

⚠️ 口径限制（必须连同结论一起引用，不得单独引用数字）：
  本服务检索的是**商标公告**（初步审定/注册/转让/无效等各类公告），
  **不等价于**商标网上检索系统（wcjs.sbj.cnipa.gov.cn）的「商标综合查询」。
  差异：① 已受理但尚未公告的申请不在本结果内；② 页面明示「数据并非实时更新，仅供参考，不具有法律效力」。
  因此结论只能表述为「未检索到公告记录」，**不得**表述为「无冲突」。

用法：
  python tmsearch_gov.py --name 证闻
  python tmsearch_gov.py --name 证闻 --name 闻证 --out 工程佐证材料/tmsearch.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

PAGE = "https://app.gjzwfw.gov.cn/jmopen/webapp/html5/sbggcxfw/index.html"
DEFAULT_OUT = (
    Path(__file__).resolve().parents[2]
    / "交付物"
    / "提交包"
    / "工程佐证材料"
    / "tmsearch_cnipa.json"
)


def goto_with_retry(page, url: str, tries: int = 4) -> None:
    """该站首字节实测可达 10s+ 且偶发 ERR_EMPTY_RESPONSE（2026-09-19），故重试若干次。"""
    last: Exception | None = None
    for i in range(1, tries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_selector("#search_text", timeout=30000)
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"[WARN] goto attempt {i}/{tries} failed: {type(exc).__name__}")
            page.wait_for_timeout(3000)
    raise RuntimeError(f"无法加载 {url}：{last!r}")


def wait_mask_gone(page) -> bool:
    """等待加载遮罩 #huise 消失。

    2026-09-19 实测：每次查询会弹出 `<div id="huise" class="model">` 遮罩，
    若不等待其消失就下一次点击，会被拦截（TimeoutError）。
    """
    try:
        page.wait_for_function(
            "() => {const e=document.getElementById('huise');"
            "if(!e) return true;"
            "const s=getComputedStyle(e);"
            "return s.display==='none'||s.visibility==='hidden'||s.opacity==='0';}",
            timeout=30000,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def search_once(page, captured: list[dict], keyword: str) -> dict:
    captured.clear()
    page.fill("#search_text", keyword)
    # 用 DOM 级 click 绕过遮罩的 pointer-events 拦截（遮罩存在时展示 click 会超时）
    page.eval_on_selector("button:has-text('查询')", "el => el.click()")
    page.wait_for_timeout(6000)
    wait_mask_gone(page)

    rows = page.evaluate(
        "() => {const t=document.querySelector('table');"
        "if(!t) return null;"
        "return Array.from(t.querySelectorAll('tr')).map(tr=>"
        "Array.from(tr.children).map(c=>(c.innerText||'').trim()));}"
    )
    # 判断「查无记录」的常见提示，避免把 0 条误当成未执行
    body_text = page.inner_text("body")
    no_hit_markers = [m for m in ("暂无数据", "无数据", "未查询到", "没有查询到", "共 0 条")
                      if m in body_text]

    return {
        "keyword": keyword,
        "queried_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "requests": [dict(c) for c in captured],
        "table": rows,
        "row_count": (len(rows) - 1) if rows else None,
        "no_hit_markers": no_hit_markers,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", action="append", required=True, help="待查商标名（可重复）")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    captured: list[dict] = []
    results: list[dict] = []

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge")
        except Exception:
            browser = p.chromium.launch()
        page = browser.new_page()

        def on_requestfinished(req):
            """只录真正的 XHR/fetch —— 静态资源不入（否则噪声淹没判据）。"""
            try:
                if req.resource_type not in ("xhr", "fetch"):
                    return
                captured.append(
                    {
                        "url": req.url,
                        "method": req.method,
                        "post_data": req.post_data,
                        "resource_type": req.resource_type,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                captured.append({"url": getattr(req, "url", "?"), "error": repr(exc)})

        def on_response(resp):
            """XHR 命中后再补 status + body，與 requests 按 url 合并呈现。"""
            try:
                req = resp.request
                if req.resource_type not in ("xhr", "fetch"):
                    return
                entry = {
                    "url": resp.url,
                    "status": resp.status,
                    "method": req.method,
                    "post_data": req.post_data,
                    "resource_type": req.resource_type,
                    "body_head": "",
                }
                try:
                    entry["body_head"] = resp.text()[:8000]
                except Exception:  # noqa: BLE001
                    entry["body_head"] = "<unreadable>"
                captured.append(entry)
            except Exception as exc:  # noqa: BLE001
                captured.append({"url": resp.url, "error": repr(exc)})

        page.on("requestfinished", on_requestfinished)
        page.on("response", on_response)
        goto_with_retry(page, PAGE)
        page.wait_for_timeout(3000)

        for kw in args.name:
            r = search_once(page, captured, kw)
            results.append(r)
            n = r["row_count"]
            print(f"[OK] '{kw}' rows={n} no_hit_markers={r['no_hit_markers']} "
                  f"xhr={len(r['requests'])}")
        browser.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "source": {
                    "name": "国家政务服务平台 · 商标公告查询服务（本服务由国家知识产权局提供）",
                    "url": PAGE,
                    "scope_limit": "仅覆盖商标公告；不含已受理未公告申请；数据非实时，不具法律效力",
                },
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[OK] raw written -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
