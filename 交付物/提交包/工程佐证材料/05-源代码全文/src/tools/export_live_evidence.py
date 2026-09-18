"""证闻 · 实时核证层证据导出（可复跑）

为什么需要：参赛材料里的「实时源」与「命中/拒答」数字必须有可核验的原始数据，
不能只有结论。本工具在服务运行中抓取一份**可复核快照**：

  1) 实时源登记表（/api/live/sources）—— 可用源 / 实测不可用项及其原因
  2) 运行状态（/api/health）—— 实时源开关、超时预算、模型可用性
  3) 一组问答的完整响应 —— 含逐源命中数、耗时、实时证据、拒答依据

用法：
  python src/app.py                 # 另开一个终端，保持运行
  python src/tools/export_live_evidence.py
  python src/tools/export_live_evidence.py --base http://127.0.0.1:8848

输出：交付物/提交包/工程佐证材料/03-工程验证证据/raw/live-<时间戳>.json
纪律：只读（全部为 GET / POST /api/ask 只读问答），不写入被观测服务。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # src/
PROJECT_DIR = os.path.dirname(BASE_DIR)                                          # 项目根
OUT_DIR = os.path.join(PROJECT_DIR, "交付物", "提交包", "工程佐证材料",
                       "03-工程验证证据", "raw")

# 用例刻意成对：**应命中** 与 **应无依据**（对照组）——
# 只报「有命中的例子」不能证明闸门有效（R238 两层纪律）。
#
# ⚠️ 历史留痕（2026-09-19，保留原值 + 校正注，不追改）：
#   原「对照·应无依据」用例为「今天天气怎么样？」—— 在**改造前**（7 个源、无新闻检索）
#   该问实测实时 0 条、离线 0 条，判为应拒答是正确的；改造后央视网检索可用，
#   该问能取回**真实天气新闻**（最新发布日 2026-09-14），此时再要求拒答与用户
#   「任何问题都要实时查证权威媒体」的产品意图相冲突 → **变更的是用例，不是闸门**。
#   负样本改用与新闻语料无词面交集的查询，继续守住「闸门未被放宽」。
CASES = [
    ("应命中·政策法规", "加装电梯需要什么手续？"),
    ("应命中·政策法规(口语长句)", "加装电梯业主需要出多少钱？"),
    ("应命中·离线语料命中但实时可能未命中", "5号线开通后客流多少？"),
    ("应命中·企业财经(改造前 0 命中)", "英伟达最新财报"),
    ("应命中·国际文化(改造前 0 命中)", "2026年诺贝尔文学奖得主"),
    ("行为变更留痕·天气类(改造前应拒答)", "今天天气怎么样？"),
    ("对照·应无依据(非新闻请求)", "帮我给这只猫起个名字吧"),
    ("对照·应无依据(代码请求)", "请帮我写一段 Python 爬虫代码"),
    # ⚠️ 边界留痕（2026-09-19 实测，不掩盖）：原「对照·应无依据(越界主题)」为「红烧肉怎么做才好吃」，
    #   改造前拒答；扩源后央视网确实存在该主题的**真实条目**，故现在会取回（可核实 6 条 / 线索 2 条）。
    #   这不是编造 —— 条目真实、可点回原文；但**说明本层不区分「新闻/非新闻」意图**，
    #   只按词面相关性与来源权威性取回。该边界已在《应用方案》§7.5 与《材料数字对照表》如实披露。
    ("边界留痕·生活服务类(改造前拒答/现取回条目)", "红烧肉怎么做才好吃"),
]


def _post(base: str, query: str) -> dict:
    body = json.dumps({"query": query, "session": "evidence-export"}).encode("utf-8")
    req = urllib.request.Request(base + "/api/ask", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _trim_ask(d: dict) -> dict:
    """保留可核验字段，去掉冗长正文（原始响应仍可由命令复跑重现）。"""
    return {
        "query": d.get("query"),
        "refused": d.get("refused"),
        "mode": d.get("mode"),
        "confidence": d.get("confidence"),
        "threshold": d.get("threshold"),
        "corpus_result": d.get("corpus_result"),
        "live": d.get("live"),
        "refuse_reason": d.get("refuse_reason"),
        "citations": d.get("citations"),
        "evidence": [
            {
                "id": e.get("id"), "live": e.get("live"),
                "publisher": e.get("publisher"), "published": e.get("published"),
                "fetched_at": e.get("fetched_at"), "source_url": e.get("source_url"),
                "score": e.get("score"), "matched": e.get("matched"),
                "text_head": (e.get("text") or "")[:120],
            }
            for e in (d.get("evidence") or [])
        ],
        # 分层输出的第二层（未核实线索）：保留标题/链接/来源与 verified 标记，
        # 让「线索未进入结论」这一结论本身可被核验（而不是只看文字声明）。
        "related_count": len(d.get("related") or []),
        "related": [
            {
                "id": r.get("id"), "title": r.get("title"),
                "source_name": r.get("source_name"), "published": r.get("published"),
                "fetched_at": r.get("fetched_at"), "url": r.get("url"),
                "verified": r.get("verified"), "related_score": r.get("related_score"),
                "live": r.get("live"),
            }
            for r in (d.get("related") or [])
        ],
        "evidence_ids": [e.get("id") for e in (d.get("evidence") or [])],
        "related_ids": [r.get("id") for r in (d.get("related") or [])],
        "live_graph_stats": (d.get("live_graph") or {}).get("stats"),
        "timing": d.get("timing"),
        "trace": d.get("trace"),
        "answer_head": (d.get("answer") or "")[:300],
    }


def _write_registry_md(reg: dict, base: str) -> tuple[str, int]:
    """把实时源登记表导出为 Markdown（交付包用）。

    为什么必须落成文件：材料（方案 §7.5 / §10.1a、数字对照表）多处引用《实时源登记表》，
    但磁盘上此前**并不存在该文件** —— 属「文档引用 ≠ 磁盘存在」的隐性断链。
    本函数与 `/api/live/sources` 同源，保证「材料里写的」与「系统实际接的」不会两套口径。
    """
    lines = [
        "# 实时源登记表",
        "",
        "> 与 `GET /api/live/sources` **同源**（由 `src/tools/export_live_evidence.py` 导出，勿手工编辑）。",
        "> 导出时间：{}　｜　导出目标：{}".format(
            datetime.now().astimezone().isoformat(timespec="seconds"), base),
        "",
        "> 判据说明：**可核实依据** = 查询原始词命中 ≥2（可进结论、参与拒答判定）；"
        "**相关线索** = ≥1（最多 5 条，仅展示、标「未核实」，不进结论、不进模型输入）。",
        "",
        "## 一、可用源（{} 个）".format(len(reg["available"])),
        "",
        "| # | 编号 | 来源 | 类型 | 许可以及引用粒度 | 抓取方式与实测结论 |",
        "|---|---|---|---|---|---|",
    ]
    for i, s in enumerate(reg["available"], start=1):
        lines.append("| {} | `{}` | {} | {} | {} | {} |".format(
            i, s["id"], s["name"], s["kind"],
            (s.get("license") or "").replace("|", "/"),
            (s.get("note") or "").replace("|", "/")))
    lines += [
        "",
        "## 二、实测不可用项（{} 项，保留登记以便追溯）".format(len(reg["unavailable"])),
        "",
        "> 「不可用」同样是实测结论，必须留痕 —— 只报好消息等于把「查了没查到」藏起来。",
        "",
        "| # | 编号 | 来源 | 实测形态 |",
        "|---|---|---|---|",
    ]
    for i, s in enumerate(reg["unavailable"], start=1):
        lines.append("| {} | `{}` | {} | {} |".format(
            i, s["id"], s["name"], (s.get("note") or "").replace("|", "/")))
    slot = reg["web_search_slot"]
    lines += [
        "",
        "## 三、通用检索插槽",
        "",
        "- 状态：**{}**（provider: {}）".format("已配置" if slot["configured"] else "未配置 → 不启用", slot["provider"]),
        "- 说明：代码已就绪并支持按权威域过滤；未配置凭据时**如实标注未启用**，不静默假装已覆盖。",
        "",
        "## 四、检索时序",
        "",
        "- 全部可用源**并发**发起；单源超时 **{} s**，整轮预算 **{} s**（到点用已到达结果收口）。".format(
            reg["per_source_timeout_s"], reg["total_budget_s"]),
        "- 未在预算内返回的源显式登记为「预算内未返回」，不假装查询过。",
        "",
        "> ⚠️ 诚实边界：覆盖面**虽较此前显著扩大但仍非全网**。中央媒体中仅**央视网**站内检索可用；"
        "人民网 / 新华网 / 光明网 / 百度 / 头条等均不可得（见第二节）。**不得写成「全网实时检索」**。",
        "",
    ]
    # 落点是提交包根目录（与《语料来源台账》同级）—— 便于评委在包内一眼找到，不埋在证据子目录里
    path = os.path.normpath(os.path.join(OUT_DIR, "..", "..", "..", "实时源登记表.md"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path, len(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("TARGET_URL", "http://127.0.0.1:8848"))
    args = ap.parse_args()
    base = args.base.rstrip("/")

    try:
        health = _get(base, "/api/health")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print("服务未就绪，先运行 `python src/app.py`。原因：{}".format(exc), file=sys.stderr)
        return 2

    out = {
        "meta": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "base_url": base,
            "tool": "src/tools/export_live_evidence.py",
            "note": "只读导出：全部为 GET 与 POST /api/ask（只读问答），不修改被观测服务。",
        },
        "health": {
            k: health.get(k) for k in
            ("ok", "readonly", "model_available", "provider", "refuse_threshold",
             "min_original_hits", "sessions_tracked", "stats", "live")
        },
        "live_sources": _get(base, "/api/live/sources"),
        "cases": [],
    }

    for label, q in CASES:
        try:
            out["cases"].append({"label": label, **_trim_ask(_post(base, q))})
            print("[OK ] {}「{}」".format(label, q))
        except Exception as exc:  # noqa: BLE001
            out["cases"].append({"label": label, "query": q, "error": repr(exc)[:200]})
            print("[FAIL] {}「{}」{}".format(label, q, exc), file=sys.stderr)

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "live-{}.json".format(datetime.now().strftime("%Y%m%d-%H%M%S")))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)

    reg = out["live_sources"]
    print()
    print("实时源：可用 {} 项 / 实测不可用 {} 项 / 通用检索插槽已配置={}".format(
        len(reg["available"]), len(reg["unavailable"]), reg["web_search_slot"]["configured"]))
    md_path, _n = _write_registry_md(reg, base)
    print("实时源登记表（材料引用，与接口同源）已落盘：{}".format(md_path))
    for c in out["cases"]:
        live = c.get("live") or {}
        print("  {:<38} refused={:<5} 可核实={:<3} 线索={:<3} 离线命中={}".format(
            c["label"], str(c.get("refused")), live.get("hits"),
            c.get("related_count"),
            (c.get("corpus_result") or {}).get("hits")))
    print()
    print("原始数据已落盘：{}".format(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
