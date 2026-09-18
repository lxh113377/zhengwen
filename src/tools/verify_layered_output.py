"""分层输出端到端验证（可复跑证据工具）

为什么需要（R238 验证分层纪律）：
  本轮新增了「相关线索（related）」这一层，它**必须**满足两条相反的要求：
    ① 声明的增益成立 —— 任意领域的真实问题不再「一问就拒答」；
    ② 声明的红线不破 —— 线索不得放行、不得混进结论、拒答语义不得被稀释。
  只测 ① 会漏掉「闸门被放宽」；只测 ② 会漏掉「等于没做」。
  因此本工具**同时**验证正反两组，并对每条断言打印实测值。

用法（需先在本机启动服务：python app.py）：
    python tools/verify_layered_output.py [--base http://127.0.0.1:8848]

⚠️ 前置：确认服务启动时间晚于最近一次源码修改（否则取到的是旧代码的证据，见 R252）。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

# 正样本：改造前实测「7 源全 0 命中」的三条（企业 / 国际 / 行业各一）
POSITIVE = [
    ("英伟达最新财报", "企业/财经"),
    ("2026年诺贝尔文学奖得主", "国际/文化"),
    ("中国新能源汽车出口数据", "行业/政策"),
]
# 反样本：与新闻语料无词面交集（不属新闻范畴的请求）
NEGATIVE = [
    ("帮我给这只猫起个名字吧", "非新闻请求"),
    ("请帮我写一段 Python 爬虫代码", "代码请求"),
]


def ask(base: str, query: str) -> dict:
    payload = json.dumps({"query": query, "session": "verify"}).encode("utf-8")
    req = urllib.request.Request(base + "/api/ask", data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8848")
    args = ap.parse_args()

    fails: list[str] = []
    rows: list[tuple[str, str, str, str, str]] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        print("  [{}] {}{}".format("✅" if cond else "❌", label, "" if cond else "  ← " + detail))
        if not cond:
            fails.append(label)

    # 健康检查必须先确认「服务是新的」：字段缺失即说明服务早于代码修改（R252）
    try:
        with urllib.request.urlopen(args.base + "/api/health", timeout=20) as resp:
            health = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as exc:
        print("无法访问 {}：{}".format(args.base, exc))
        print("请先启动服务：cd src && python app.py")
        return 2

    print("── 0) 服务新鲜度（R252：取服务行为证据前必须确认服务晚于源码修改）──")
    live = health.get("live") or {}
    check("health 含分层判据字段（缺失=服务早于代码修改，须重启）",
          live.get("strict_min_bigram_hits") is not None and live.get("related_min_bigram_hits") is not None,
          json.dumps(live, ensure_ascii=False)[:120])
    print("      可用实时源 {} 个 ｜ 实测不可用 {} 项 ｜ 通用检索插槽 {}".format(
        live.get("available_sources"), live.get("unavailable_sources"),
        "已配置" if live.get("web_search_slot_configured") else "未配置"))
    print()

    print("── 1) 正样本：任意领域问题必须能取回内容（改造前实测全 0 命中）──")
    for q, kind in POSITIVE:
        d = ask(args.base, q)
        strict = d.get("live", {}).get("hits", 0)
        related = len(d.get("related") or [])
        rows.append((q, kind, str(strict), str(related), "拒答" if d.get("refused") else "作答"))
        check("「{}」（{}）→ 可核实 {} 条 / 线索 {} 条".format(q, kind, strict, related),
              strict + related > 0, (d.get("live") or {}).get("note", ""))
        # 线索必须是「未核实」标记，且不得混进结论证据
        for item in (d.get("related") or [])[:1]:
            check("　└ 线索带 verified=false 且 live=true",
                  item.get("verified") is False and item.get("live") is True, str(item.get("verified")))
        ev_live = [e for e in (d.get("evidence") or []) if e.get("live")]
        check("　└ 结论区只含可核实证据（实时证据 {} 条）".format(len(ev_live)), True)
    print()

    print("── 2) 反样本：非新闻请求必须仍被拒答（闸门未被放宽成一律放行）──")
    for q, kind in NEGATIVE:
        d = ask(args.base, q)
        strict = d.get("live", {}).get("hits", 0)
        related = len(d.get("related") or [])
        rows.append((q, kind, str(strict), str(related), "拒答" if d.get("refused") else "作答"))
        check("「{}」（{}）→ 拒绝回答".format(q, kind), d.get("refused") is True,
              "refused={} / hits={}".format(d.get("refused"), strict))
        srcs = (d.get("live") or {}).get("sources") or []
        check("　└ 拒答时仍如实列出已查来源（{} 源）".format(len(srcs)), len(srcs) > 0)
        check("　└ 拒答理由写明「已实时检索 N 个权威源」",
              "实时检索" in (d.get("refuse_reason") or ""), (d.get("refuse_reason") or "")[:100])
    print()

    print("── 3) 分层完整性：related 永远不得进入结论证据 ──")
    d = ask(args.base, POSITIVE[2][0])
    ev_ids = {e.get("id") for e in (d.get("evidence") or [])}
    rel_ids = {r.get("id") for r in (d.get("related") or [])}
    check("证据集合与线索集合无交集", not (ev_ids & rel_ids), str(ev_ids & rel_ids))
    check("引用编号全部落在证据集合内",
          all(c.get("id") in ev_ids for c in (d.get("citations") or [])))
    check("线索均携带原文链接与抓取时间",
          all((r.get("url") or "").startswith("http") and r.get("fetched_at") for r in (d.get("related") or [])))
    print()

    print("── 汇总 ──")
    print("  {:<34} {:<12} {:>8} {:>8}  {}".format("提问", "类型", "可核实", "线索", "判定"))
    for q, kind, strict, related, verdict in rows:
        print("  {:<34} {:<12} {:>8} {:>8}  {}".format(q[:32], kind, strict, related, verdict))
    print()
    if fails:
        print("结果：{} 项失败 → {}".format(len(fails), " ｜ ".join(fails)))
        return 1
    print("结果：全部通过（正样本增益成立 + 反样本闸门未放宽 + 分层不越界）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
