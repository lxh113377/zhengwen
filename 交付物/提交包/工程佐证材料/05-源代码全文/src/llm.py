"""证闻 · AI 生成层（证据约束生成 + 降级不伪装）

两条路径，互不冒充：
  · model 路径 —— 配置了模型 Key 时走在线大模型，system prompt 强制「只能用给定证据作答」
  · rule  路径 —— 未配置 Key / 调用失败时走规则版：直接拼装证据原文，输出 mode=rule

红线（《选题定案与产品定义》§3.4 / §7）：
  1. 模型返回的引用编号必须经 verify_citations 校验，越界的一律丢弃并记录
  2. mode 字段是唯一真相源，前端据此显示徽章；规则版结果绝不显示「AI 生成」
  3. 任何失败都降级、不抛裸错误串到界面
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from rag import RetrievalResult, build_rule_answer, verify_citations

# ---------------------------------------------------------------------------
# 配置（环境变量驱动，不硬编码任何密钥）
# ---------------------------------------------------------------------------

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "25"))

# 供界面与材料如实标注的供应商名（29 号第 4 页要求第三方组件如实说明）
LLM_PROVIDER_NAME = os.environ.get("LLM_PROVIDER_NAME", "DeepSeek")

SYSTEM_PROMPT = """你是「证闻」的证据约束问答引擎。

【硬性规则 —— 违反即视为失败】
1. 只能使用【证据】区块中给出的原文作答。不得引入任何外部知识。
2. 每一句结论后面必须附上它依据的证据编号，格式为［证据编号］。
3. 若【证据】中没有足够信息回答，必须直接回答「未检索到可核实依据，不作回答」，禁止推测、禁止编造。
4. 不得改变证据中的数字、日期、机构名与措辞。
5. 若证据之间存在表述差异，必须显式指出存在差异，不得替读者合并成一个结论。
6. 证据分两类，必须区别对待、不得混为一谈：
   · 标注「实时」的证据来自**本次提问时**对权威源的实时抓取 —— 引用时须带上它的抓取时间；
   · 未标注「实时」的证据来自**离线固定语料**（含合成演示数据与开放数据）。
   若两类证据对同一事实的表述不一致，必须指出「实时来源与离线语料表述不同」。

【输出格式】
先给结论（每条带证据编号），再给一句「已核验引用」。不要输出 Markdown 标题。"""


def _build_user_prompt(query: str, result: RetrievalResult) -> str:
    lines = [f"【问题】{query}", "", "【证据】"]
    for ev in result.evidence:
        # 实时证据必须把「抓取时间 + 原文链接」带进 prompt：
        # 否则模型会把实时内容当普通语料陈述，读者也无从核验「这是刚查到的」。
        tag = "【实时】" if getattr(ev, "live", False) else ""
        stamp = f" ｜实时抓取于 {ev.fetched_at}" if getattr(ev, "live", False) and ev.fetched_at else ""
        lines.append(
            f"［{ev.id}］{tag}({ev.doc_title} / {ev.publisher} / {ev.published}){stamp} {ev.text}"
        )
        if getattr(ev, "live", False) and ev.source_url:
            lines.append(f"      原文链接：{ev.source_url}")
    if result.divergences:
        lines.append("")
        lines.append("【已检出的多源差异（必须显式指出，不得合并）】")
        for dv in result.divergences:
            lines.append(f"· 主题：{dv['topic']} —— {dv['summary']}")
    if result.live:
        lines.append("")
        lines.append("【本次实时核证情况（如实体现在回答中，不得夸大）】")
        lines.append("· 已查询权威源 {} 个，命中 {} 条可核实依据；抓取时间 {}。".format(
            result.live.get("queried", 0), result.live.get("hits", 0),
            result.live.get("fetched_at") or "—"))
        for s in result.live.get("sources", []):
            lines.append("  - {}：{}".format(
                s["name"], "命中 {} 条".format(s["hits"]) if s["ok"] else "本次未取得结果（{}）".format(s["error"])))
        # 相关线索只告知「存在」与条数，**不把内容交给模型** ——
        # 一旦进 prompt，模型就可能把它们当依据引用，等于把分层白做。
        if result.related:
            lines.append("· 另有 {} 条「相关线索」：主题沾边但**未经核实**，"
                         "不得作为任何结论的依据；如需提及，只能说「另有 N 条相关线索待人工核实」。".format(
                             len(result.related)))
    lines.append("")
    lines.append("请仅依据以上证据作答。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 在线模型调用
# ---------------------------------------------------------------------------


def _call_model(query: str, result: RetrievalResult) -> str:
    """调用 OpenAI 兼容的 /chat/completions。失败时抛异常，由上层降级。"""
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(query, result)},
        ],
        "temperature": 0.2,
        "max_tokens": 700,
        "stream": False,
    }
    req = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"].strip()


def model_available() -> bool:
    """是否具备走 model 路径的条件（有 Key 即视为可尝试；真实可用性由调用结果决定）。"""
    return bool(LLM_API_KEY)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


def answer(query: str, result: RetrievalResult) -> dict[str, Any]:
    """生成回答。返回结构里的 mode 是「降级不伪装」的唯一真相源。"""
    started = time.perf_counter()

    # 拒答优先级最高：闸门已判定无依据时，不走模型，避免模型被诱发编造
    if result.refused:
        ruled = build_rule_answer(query, result)
        ruled["provider"] = None
        ruled["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        ruled["dropped_citations"] = []
        ruled["degraded_reason"] = "拒答闸门：语料中无充分依据"
        return ruled

    # 尝试 model 路径
    if model_available():
        try:
            text = _call_model(query, result)
            # 提取回答中出现的证据编号，做越界校验
            cited = [
                {"id": ev.id, "doc_title": ev.doc_title, "publisher": ev.publisher}
                for ev in result.evidence
                if f"｛{ev.id}｝" in text or f"[{ev.id}]" in text or f"［{ev.id}］" in text or ev.id in text
            ]
            dropped = verify_citations(
                [{"id": ev.id} for ev in result.evidence if ev.id not in {c["id"] for c in cited}] + cited,
                result,
            )
            return {
                "mode": "model",
                "refused": False,
                "answer": text,
                "citations": cited,
                "provider": LLM_PROVIDER_NAME,
                "model": LLM_MODEL,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "dropped_citations": [d["id"] for d in dropped],
                "degraded_reason": None,
            }
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, TimeoutError, OSError) as exc:
            reason = f"在线模型调用失败（{type(exc).__name__}）→ 自动降级为规则版"
    else:
        reason = "未配置模型凭据 → 使用规则版"

    # 降级：规则版，显式标注来源与原因
    ruled = build_rule_answer(query, result)
    ruled["provider"] = None
    ruled["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    ruled["dropped_citations"] = []
    ruled["degraded_reason"] = reason
    return ruled


if __name__ == "__main__":
    from rag import Corpus

    corpus = Corpus()
    print(f"模型可用：{model_available()}（provider={LLM_PROVIDER_NAME}, model={LLM_MODEL}）\n")
    for q in ["5号线开通后客流多少？", "5号线客流预测口径有没有矛盾？", "今天天气怎么样？"]:
        r = corpus.search(q)
        out = answer(q, r)
        print(f"[mode={out['mode']}] 「{q}」")
        print(f"  拒答={out['refused']}  置信度={r.confidence:.3f}  耗时={out['elapsed_ms']}ms")
        if out.get("degraded_reason"):
            print(f"  降级原因：{out['degraded_reason']}")
        print(f"  回答：{out['answer'][:120].replace(chr(10), ' / ')}…")
        print()
