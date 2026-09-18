"""证闻 · 检索引擎（证据约束 + 拒答闸门 + 分歧检测）

设计约束（对应《选题定案与产品定义》§3.4 AI 五层核心作用）：
  1. 语义检索        —— 把自然语言问题映射到语料片段
  2. 证据约束生成    —— 只允许基于检索结果作答，每条结论绑定证据编号
  3. 无证据即拒答    —— 置信度低于阈值时明确拒答，不编造
  4. 多源一致性比对  —— 同一主题多源差异识别并并列展示
  5. 对话编排        —— 多轮上下文（由上层 app.py 维护）

纯 Python 实现，无第三方依赖 —— 保证免费托管可部署、离线可运行。
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# 拒答闸门阈值：低于该置信度视为「语料中无依据」。
# 标定依据（2026-09-18 回归实测，非拍脑袋）：
#   最高误放 0.313（「量子计算机的退相干时间是多少」—— 与语料无关却凑到 2 个高区分度词）
#   最低正确通过 0.372（「加装电梯为什么推不动？」—— 语料内且召回正确）
# 取两者中点附近 0.34，两侧各留 ~0.03 余量；阈值偏高只损失少量召回、绝不放过编造。
REFUSE_THRESHOLD = 0.34

# 单次返回的证据条数上限
TOP_K = 4

# 依据充分性闸门：查询的**原始词**（同义扩展之前）至少要有几个真实命中。
#
# 为什么需要这个闸门（2026-09-18 实测，非拍脑袋）：
#   接入世界银行真实语料后语料规模从 26 → 146 条证据，同一套置信度公式的两侧分布
#   发生了变化，出现两类假阳性：
#     ① 「量子计算机的退相干时间是多少？」只命中 1 个词「时间」→ conf 0.379 ≥ 0.34 放行
#     ② 「苹果手机最新款多少钱」的命中**全部来自同义扩展词**（少钱→万元/补贴/成本）
#        → conf 0.783 放行
#   两类问题都不是阈值能解决的（调高阈值会连正经问题一起拒掉）。
#
# 实测的两侧边界（12 例应通过 + 10 例应拒绝）：
#   判据 A「高区分度词（IDF ≥ P70）≥2」  → 应通过样本最小值 = 0  → 完全不可用（原实现只用它做 0.6 折算）
#   判据 B「原始词命中 ≥2」                → 应通过最小 2 / 应拒绝最大 2 → 单独不可分离
#   判据 B + 阈值 0.34                     → 应通过最低 conf 0.455 / 应拒绝最高 conf 0.298 → ✅ 可分离
#   因此判据 B 定位为**前置闸门**（拦掉「靠扩展词凑出来的命中」），阈值继续管其余部分。
MIN_ORIGINAL_HITS = 2

# 中文停用词（仅用于降低噪声词权重，不做分词）
STOPWORDS = frozenset(
    """的 了 是 在 和 与 及 等 有 为 对 从 到 上 下 中 这 那 个 我 你 他 她 它
    什么 多少 怎么 为什么 如何 是否 哪些 哪个 吗 呢 吧 啊 会 能 可以 请 问 一下
    关于 有关 以及 并且 但是 因为 所以 如果 就 都 也 还 又 再 很 更 最 被 把 让
    """.split()
)

# 语料文件位置（**多源**）：
#   s0_demo.json —— 自建合成语料（原创，演示主力；拒答与分歧边界可控）
#   wb_real.json —— 世界银行开放数据（CC BY 4.0，真实数值，唯一明确可商用的外部源）
# 为什么要多源：29 号要求作品能处理真实来源且可溯源。单源合成无法证明这一点，
# 因此把「真实来源」做成独立命名空间并行加载，逐条证据携带自己的许可与外链。
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus")
CORPUS_PATHS: list[str] = [
    os.path.join(_DATA_DIR, "s0_demo.json"),
    os.path.join(_DATA_DIR, "wb_real.json"),
]

# 轻量同义扩展表：把口语化问法映射到语料中的书面表达。
# 为什么需要（2026-09-18 实测）：用户问「5号线一共几个站？」，语料写的是「设站14座」——
# 字面无交集导致合法问题被误拒。放宽拒答阈值会同时放行真正越界的问题（两者置信度
# 实测仅差 0.016，无法用阈值分开），因此正确解法是补召回，而不是松闸门。
# 纯词表实现：可离线、可解释、可审计，不引入分词依赖。
# ⚠️ 键必须是 bigram token（不是整个词）——查询会被切成 bigram 后查表，
#    例如「多少钱」实际产生 token「少钱」、「多少人坐」产生「少人 / 人坐」。
SYNONYM_EXPANSION: dict[str, list[str]] = {
    "个站": ["设站", "车站"],
    "少钱": ["万元", "补贴", "成本", "票价"],
    "少人": ["人次", "客流"],
    "人坐": ["客流", "人次"],
    "乘客": ["客流", "人次"],
    "多贵": ["票价", "万元"],
    "涨价": ["票价"],
    "推广": ["试点", "全市"],
    "效果": ["试点", "占"],
    "补贴": ["万元", "财政补贴"],
    # 世界银行语料（WB）方向的口语问法 → 指标体系书面表达。
    # 同样只做「提召回」：扩展词参与打分，但**不计入「有无依据」的判定**
    # （判定只看查询原始词，见 search() 内的原始词命中闸门）。
    # ⚠️ 值必须是语料里真实存在的 **bigram token**（不是整个词组）——
    #    2026-09-18 实测踩坑：首版写了「移动蜂窝」「互联网使用」这类整词，
    #    因语料切分是 bigram，这些值永远不可能命中，扩展等于没生效。
    "镇化": ["城镇", "镇人"],
    "化率": ["镇人", "人口"],
    "市人": ["镇人", "人口"],
    "城市": ["城镇"],
    "占比": ["比重", "口占"],
    "上网": ["互联", "联网"],
    "网率": ["网使", "互联"],
    "手机": ["蜂窝", "电话"],
    "绿电": ["可再", "再生"],
    "光伏": ["可再", "再生"],
    "风电": ["可再", "再生"],
    "用电": ["能源", "通电"],
    "全世界": ["全球"],
    "外国": ["际旅", "国际"],
    "旅游": ["际旅", "游入"],
    "入境": ["游入", "境人"],
    "出境": ["境旅", "游人"],
    "收入": ["游收"],
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Evidence:
    """一条可引用的最小证据单元。"""

    id: str
    text: str
    doc_id: str
    doc_title: str
    publisher: str
    published: str
    kind: str
    license: str
    # 溯源三件套（2026-09-18 新增，用于「每一条结论都能点回原文」）
    source_id: str = ""      # 语料源编号：S0 / WB / 实时源 LIVE-*
    source_url: str = ""     # 数据集页面 / 原文入口（人可读）
    attribution: str = ""    # CC BY 4.0 等许可要求的署名串
    # 实时源专用（2026-09-19 双轨并行新增）：抓取时间必须逐条可见，
    # 否则「实时」无法被读者核验（离线语料此字段为空）。
    fetched_at: str = ""
    live: bool = False

    def public(self, score: float | None = None, matched: list[str] | None = None) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": self.id,
            "text": self.text,
            "doc_id": self.doc_id,
            "doc_title": self.doc_title,
            "publisher": self.publisher,
            "published": self.published,
            "kind": self.kind,
            "license": self.license,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "attribution": self.attribution,
            "fetched_at": self.fetched_at,
            "live": self.live,
        }
        if score is not None:
            item["score"] = round(score, 4)
        if matched:
            item["matched"] = matched
        return item


@dataclass
class TraceStep:
    """决策轨迹的一步（全过程留痕，对应当前溯源 Requirement）。"""

    step: str
    detail: str
    ms: float = 0.0

    def public(self) -> dict[str, Any]:
        return {"step": self.step, "detail": self.detail, "ms": round(self.ms, 2)}


@dataclass
class RetrievalResult:
    evidence: list[Evidence] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    matched_terms: dict[str, list[str]] = field(default_factory=dict)
    confidence: float = 0.0
    refused: bool = False
    refuse_reason: str = ""
    divergences: list[dict[str, Any]] = field(default_factory=list)
    trace: list[TraceStep] = field(default_factory=list)
    # 双轨并行（2026-09-19）：实时核证侧的摘要信息，供界面如实展示「查了哪些源、命中几条」
    live: dict[str, Any] = field(default_factory=dict)
    # 分层输出的第二层（2026-09-19）：相关线索（弱命中）。
    # 🔴 纪律：该字段**不参与拒答判定**、不进模型 prompt、界面必须标「未核实」——
    # 它的存在是为了「不再一问就只说拒答」，不是为了把线索引申成结论。
    related: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 文本处理（纯 Python，无依赖）
# ---------------------------------------------------------------------------

_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,}")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """字符 bigram + ASCII 词 + 中文单字（低权重）。

    中文不引入分词依赖：bigram 已足以支撑小规模语料的检索质量，
    且完全确定性、可离线、零安装。
    """
    tokens: list[str] = [w.lower() for w in _ASCII_WORD.findall(text)]

    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", text)
    for run in cjk_runs:
        for i in range(len(run) - 1):
            bigram = run[i : i + 2]
            if bigram not in STOPWORDS:
                tokens.append(bigram)

    # 设计决策（2026-09-18 自检实测后修正）：
    #   初版额外加入了「中文单字」token，结果「今天天气怎么样」也命中了
    #   ev-library-02-p3（因含「天」字），拒答闸门完全失效。
    #   单字区分度极低、噪声极大，故移除；只保留 bigram + ASCII 词。
    return tokens


# ---------------------------------------------------------------------------
# 检索引擎
# ---------------------------------------------------------------------------


class Corpus:
    """语料库：加载、索引、检索、分歧比对。"""

    def __init__(self, paths: str | list[str] | None = None) -> None:
        # 兼容单文件调用（测试/快照脚本用 Corpus("某文件.json")）
        if paths is None:
            paths = CORPUS_PATHS
        self.paths: list[str] = [paths] if isinstance(paths, str) else list(paths)
        self.meta: dict[str, Any] = {}
        self.sources: list[dict[str, Any]] = []
        self.topic_labels: dict[str, str] = {}
        self.documents: list[dict[str, Any]] = []
        self.evidence: dict[str, Evidence] = {}
        self.divergences: list[dict[str, Any]] = []

        # 倒排索引：token -> {evidence_id: tf}
        self.inverted: dict[str, dict[str, int]] = {}
        self.doc_freq: dict[str, int] = {}
        self.lengths: dict[str, int] = {}
        self.avg_len: float = 1.0
        self.idf_p70: float = 0.0

        self._load()

    # -- 加载 ---------------------------------------------------------------

    def _load(self) -> None:
        merged_docs: list[dict[str, Any]] = []
        merged_dv: list[dict[str, Any]] = []

        for path in self.paths:
            if not os.path.isfile(path):
                # 缺文件不致命：只加载存在的源（例如只带 S0 的离线包）。
                continue
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)

            meta = raw.get("meta", {})
            self.sources.append(meta)
            self.topic_labels.update(meta.get("topic_labels", {}) or {})

            for doc in raw.get("documents", []):
                # 逐文档许可优先（WB = CC BY 4.0，S0 = 本项目原创）
                doc = dict(doc)
                doc["_source_id"] = meta.get("id", "")
                doc["_license"] = doc.get("license") or meta.get("license", "")
                merged_docs.append(doc)

            merged_dv.extend(raw.get("divergences", []))

        self.documents = merged_docs
        self.divergences = merged_dv

        # 合并后的语料元信息：多源时明确列出每个源的许可，便于前端逐源标注
        ids = [s.get("id", "?") for s in self.sources]
        self.meta = {
            "id": "+".join(ids) if ids else "EMPTY",
            "name": " ＋ ".join(s.get("name", "") for s in self.sources),
            "kind": "mixed" if len(self.sources) > 1 else (self.sources[0].get("kind", "") if self.sources else ""),
            "license": " ｜ ".join(f"{s.get('id', '?')}: {s.get('license', '')}" for s in self.sources),
            "badge": "合成演示 + 公开来源" if len(self.sources) > 1 else (self.sources[0].get("badge", "") if self.sources else ""),
            "sources": self.sources,
            # 免责/范围说明按源拼接：S0 标注「合成非真实」，WB 标注「世行不背书」
            "scope_note": " ".join(
                x for x in (s.get("disclaimer", "") or s.get("license_note", "") for s in self.sources) if x
            ).strip(),
        }

        for doc in self.documents:
            for para in doc.get("paragraphs", []):
                ev = Evidence(
                    id=para["id"],
                    text=para["text"],
                    doc_id=doc["id"],
                    doc_title=doc["title"],
                    publisher=doc.get("publisher", ""),
                    published=doc.get("published", ""),
                    kind=doc.get("kind", ""),
                    license=doc.get("_license", ""),
                    source_id=doc.get("_source_id", ""),
                    source_url=doc.get("source_url", "") or doc.get("api_url", ""),
                    attribution=doc.get("attribution", ""),
                )
                self.evidence[ev.id] = ev

        self._build_index()

    def _build_index(self) -> None:
        # 索引内容 = 文档标题 + 正文。
        # 设计决策（2026-09-18 回归实测后修正）：初版只索引正文，导致
        # 「加装电梯为什么推不动？」无法召回 ev-lift-02-p4 —— 因为文档标题
        # 「加装电梯落地难：钱从哪来，低层为什么不同意」里承载了最强的主题信号，
        # 却完全没进索引。标题与正文同权索引后该用例通过。
        for ev_id, ev in self.evidence.items():
            tokens = tokenize(ev.doc_title + " " + ev.text)
            self.lengths[ev_id] = len(tokens)
            tf: dict[str, int] = {}
            for tok in tokens:
                tf[tok] = tf.get(tok, 0) + 1
            for tok, count in tf.items():
                self.inverted.setdefault(tok, {})[ev_id] = count
                self.doc_freq[tok] = self.doc_freq.get(tok, 0) + 1

        total = sum(self.lengths.values()) or 1
        self.avg_len = total / max(len(self.lengths), 1)

        # 高区分度词阈值 = 语料词表 IDF 的第 70 百分位。
        # 用途：把「时间」「多少」这类通用词的命中与「客流」「电梯」这类实词命中区分开 ——
        # 初版只按覆盖率判定，「量子计算机的退相干时间是多少」因命中通用词「时间」而漏过闸门。
        n = max(len(self.evidence), 1)
        idf_values = sorted(
            math.log(1 + (n - df + 0.5) / (df + 0.5)) for df in self.doc_freq.values()
        )
        if idf_values:
            idx = min(int(len(idf_values) * 0.70), len(idf_values) - 1)
            self.idf_p70 = idf_values[idx]

    # -- 检索 ---------------------------------------------------------------

    def _idf(self, token: str) -> float:
        """单个词的逆文档频率（BM25 口径）。"""
        df = self.doc_freq.get(token)
        if not df:
            return 0.0
        n = max(len(self.evidence), 1)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = TOP_K) -> RetrievalResult:
        """BM25 风格打分（k1=1.5, b=0.75），返回证据 + 置信度 + 拒答判定。"""
        result = RetrievalResult()
        t0 = time.perf_counter()

        q_tokens = tokenize(query)
        if not q_tokens:
            result.refused = True
            result.refuse_reason = "空查询或查询中不含有效检索词"
            result.trace.append(TraceStep("拒答闸门", "查询无效，未执行检索", 0.0))
            return result

        # 去重查询词，保留顺序（用于展示匹配到的关键词）
        seen: set[str] = set()
        uniq_q: list[str] = []
        for tok in q_tokens:
            if tok not in seen:
                seen.add(tok)
                uniq_q.append(tok)

        # 查询「原始词」快照 —— 依据充分性判定的唯一口径（同义扩展词不参与该判定）
        orig_uniq: list[str] = list(uniq_q)

        result.trace.append(
            TraceStep("查询理解", f"归一化出 {len(uniq_q)} 个检索词：{' / '.join(uniq_q[:12])}", (time.perf_counter() - t0) * 1000)
        )

        # 同义扩展：把口语化问法补成语料里的书面表达。
        # 注意这是「提召回」而非「松闸门」——扩展词同样计入 valid_terms，
        # 因此越界问题并不会因为扩展而变得容易通过。
        expansions: list[str] = []
        for tok in list(uniq_q):
            for alt in SYNONYM_EXPANSION.get(tok, []):
                if alt not in seen:
                    seen.add(alt)
                    uniq_q.append(alt)
                    expansions.append(alt)
        expansion_set: set[str] = set(expansions)
        if expansions:
            result.trace.append(
                TraceStep("同义扩展", f"补充 {len(expansions)} 个书面表达检索词：{' / '.join(expansions[:8])}", 0.0)
            )

        # 有效检索词 = 查询词中真实存在于语料词表的那些。
        # 这一步是拒答闸门的第一道关：若一个都没有，说明问题完全在语料范围外。
        valid_terms = [tok for tok in uniq_q if tok in self.inverted]
        if not valid_terms:
            result.refused = True
            result.refuse_reason = (
                f"查询中的 {len(uniq_q)} 个检索词均不在语料词表内，判定为超出语料范围"
            )
            result.trace.append(TraceStep("拒答闸门", result.refuse_reason + " → 明确拒答，不生成回答", 0.0))
            return result

        result.trace.append(
            TraceStep(
                "语料范围检查",
                f"{len(valid_terms)}/{len(uniq_q)} 个检索词存在于语料词表",
                0.0,
            )
        )

        t1 = time.perf_counter()
        n_docs = max(len(self.evidence), 1)
        k1, b = 1.5, 0.75
        scores: dict[str, float] = {}
        matched: dict[str, list[str]] = {}
        hit_terms: set[str] = set()

        for tok in valid_terms:
            postings = self.inverted.get(tok)
            if not postings:
                continue
            hit_terms.add(tok)
            df = self.doc_freq.get(tok, 1)
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            for ev_id, tf in postings.items():
                length = self.lengths.get(ev_id, 1)
                denom = tf + k1 * (1 - b + b * length / self.avg_len)
                scores[ev_id] = scores.get(ev_id, 0.0) + idf * (tf * (k1 + 1)) / (denom or 1)
                matched.setdefault(ev_id, []).append(tok)

        result.trace.append(
            TraceStep(
                "语义检索",
                f"倒排索引命中 {len(scores)} 条候选证据（{len(hit_terms)}/{len(valid_terms)} 个有效检索词得到匹配）",
                (time.perf_counter() - t1) * 1000,
            )
        )

        if not scores:
            result.refused = True
            result.refuse_reason = "语料库中没有任何片段与本问题相关"
            result.trace.append(TraceStep("拒答闸门", "命中 0 条 → 明确拒答，不生成回答", 0.0))
            return result

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        result.evidence = [self.evidence[ev_id] for ev_id, _ in ranked]
        result.scores = [score for _, score in ranked]
        result.matched_terms = {ev_id: sorted(set(matched.get(ev_id, [])))[:8] for ev_id, _ in ranked}

        # 置信度 = 覆盖率 × 得分饱和归一化 × 区分度因子
        #   两轮实测修正（证据保留，供答辩追问）：
        #     初版 top_score/(top_score+6)×1.6 → 「今天天气怎么样」得 0.564 被放行（闸门失效）
        #     二版 纯覆盖率主导                → 天气可拒答；但「量子计算机的退相干时间是多少」
        #                                       仅命中通用词「时间」仍拿 0.544 漏过
        #     现版 增加区分度因子：未命中任何高区分度词 → 置信度按 0.6 折算 → 稳定拒答
        top_score = result.scores[0]
        coverage = len(hit_terms) / max(len(valid_terms), 1)
        saturation = top_score / (top_score + 8.0)
        strong_terms = [tok for tok in hit_terms if self._idf(tok) >= self.idf_p70]
        # 要求 ≥ 2 个高区分度实词：单个稀有词命中可能纯属巧合
        # （实测：「量子计算机的退相干时间是多少」只凑到 1 个，判定为不足）。
        discriminative = len(strong_terms) >= 2
        disc_factor = 1.0 if discriminative else 0.6
        result.confidence = round(coverage * (0.35 + 0.65 * saturation) * disc_factor, 4)

        if discriminative:
            result.trace.append(
                TraceStep("区分度检查", f"命中 {len(strong_terms)} 个高区分度实词（≥2）→ 通过", 0.0)
            )
        else:
            result.trace.append(
                TraceStep(
                    "区分度检查",
                    f"仅命中 {len(strong_terms)} 个高区分度实词（要求 ≥2，IDF 阈值 P70 = {self.idf_p70:.2f}）"
                    " → 置信度按 0.6 折算",
                    0.0,
                )
            )

        # 依据充分性闸门（前置）：命中必须**来自查询原始词**，且不少于 MIN_ORIGINAL_HITS 个。
        # 这一层专门拦「靠同义扩展词凑出来的命中」—— 扩展词是为了提召回（让口语问法
        # 能找到书面表达），不能成为「语料里有依据」的证据。
        matched_original = [tok for tok in orig_uniq if tok in hit_terms]
        if len(matched_original) < MIN_ORIGINAL_HITS:
            result.refused = True
            from_expansion = len(hit_terms & expansion_set)
            detail = (
                f"，另有 {from_expansion} 个命中来自同义扩展词" if from_expansion else ""
            )
            result.refuse_reason = (
                f"查询原始词仅命中 {len(matched_original)} 个（要求 ≥{MIN_ORIGINAL_HITS}）{detail}；"
                "同义扩展只用于提升召回，不作为「语料中有依据」的证据 → 判定为语料中无充分依据"
            )
            result.trace.append(
                TraceStep("依据充分性闸门", result.refuse_reason + " → 明确拒答", 0.0)
            )
            return result
        result.trace.append(
            TraceStep(
                "依据充分性闸门",
                f"查询原始词命中 {len(matched_original)} 个（≥{MIN_ORIGINAL_HITS}）→ 通过"
                f"：{' / '.join(matched_original[:8])}",
                0.0,
            )
        )

        if result.confidence < REFUSE_THRESHOLD:
            result.refused = True
            result.refuse_reason = (
                f"最高相关度置信度 {result.confidence:.2f} 低于阈值 {REFUSE_THRESHOLD:.2f}，"
                "判定为语料中无充分依据"
            )
            result.trace.append(TraceStep("拒答闸门", result.refuse_reason + " → 明确拒答", 0.0))
        else:
            result.trace.append(
                TraceStep("拒答闸门", f"置信度 {result.confidence:.2f} ≥ 阈值 {REFUSE_THRESHOLD:.2f} → 放行", 0.0)
            )

        t2 = time.perf_counter()
        result.divergences = self.detect_divergence([ev.id for ev in result.evidence])
        if result.divergences:
            result.trace.append(
                TraceStep("多源一致性比对", f"检出 {len(result.divergences)} 组多源分歧", (time.perf_counter() - t2) * 1000)
            )
        else:
            result.trace.append(TraceStep("多源一致性比对", "未检出多源分歧", (time.perf_counter() - t2) * 1000))

        return result

    # -- 分歧检测 -----------------------------------------------------------

    def detect_divergence(self, evidence_ids: list[str]) -> list[dict[str, Any]]:
        """若检索到的证据命中预声明的分歧组，则把该组完整并列返回。"""
        hit = set(evidence_ids)
        found: list[dict[str, Any]] = []
        for dv in self.divergences:
            referenced = set(dv.get("evidence_ids", []))
            if hit & referenced:
                found.append(
                    {
                        "id": dv["id"],
                        "topic": dv["topic"],
                        "summary": dv["summary"],
                        "sides": [
                            {
                                "evidence_id": ev_id,
                                "doc_title": self.evidence[ev_id].doc_title,
                                "publisher": self.evidence[ev_id].publisher,
                                "text": self.evidence[ev_id].text,
                                "in_retrieved": ev_id in hit,
                            }
                            for ev_id in dv.get("evidence_ids", [])
                            if ev_id in self.evidence
                        ],
                    }
                )
        return found

    # -- 对外快照（供 3D 叙事页渲染） ---------------------------------------

    def graph(self) -> dict[str, Any]:
        """把语料组织成「事件 → 来源 → 证据」三层结构，供前端 3D 场景使用。"""
        nodes: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []

        topics: dict[str, list[dict[str, Any]]] = {}
        for doc in self.documents:
            # 主题键：优先取文档显式声明的 topic（WB 语料用 urban/digital/energy/culture），
            # 回落到从 id 里推断（S0 语料 doc-metro-01 → metro）。
            key = doc.get("topic") or doc["id"].split("-")[1]
            topics.setdefault(key, []).append(doc)

        topic_label = {"metro": "轨道交通新线", "lift": "老旧小区加装电梯", "library": "社区图书馆延时开放"}
        topic_label.update(self.topic_labels)

        for key, docs in topics.items():
            topic_id = f"topic-{key}"
            nodes.append({"id": topic_id, "type": "topic", "label": topic_label.get(key, key)})
            for doc in docs:
                nodes.append(
                    {
                        "id": doc["id"],
                        "type": "source",
                        "label": doc["title"],
                        "publisher": doc.get("publisher", ""),
                        "published": doc.get("published", ""),
                        "kind": doc.get("kind", ""),
                        "stance": doc.get("stance", ""),
                        # 逐源标注（前端据此显示「许可 / 原文入口」）
                        "source_id": doc.get("_source_id", ""),
                        "license": doc.get("_license", ""),
                        "source_url": doc.get("source_url", ""),
                        "topic": doc.get("topic", ""),
                    }
                )
                links.append({"source": topic_id, "target": doc["id"]})
                for para in doc.get("paragraphs", []):
                    nodes.append(
                        {
                            "id": para["id"],
                            "type": "evidence",
                            "label": para["text"][:42] + ("…" if len(para["text"]) > 42 else ""),
                            "doc_id": doc["id"],
                        }
                    )
                    links.append({"source": doc["id"], "target": para["id"]})

        for dv in self.divergences:
            nodes.append({"id": dv["id"], "type": "divergence", "label": dv["topic"], "summary": dv["summary"]})
            for ev_id in dv.get("evidence_ids", []):
                links.append({"source": dv["id"], "target": ev_id, "kind": "divergence"})

        return {
            "meta": self.meta,
            "nodes": nodes,
            "links": links,
            "stats": {
                "topics": len(topics),
                "sources": len(self.documents),
                "evidence": len(self.evidence),
                "divergences": len(self.divergences),
            },
        }

    def get_evidence(self, ev_id: str) -> dict[str, Any] | None:
        ev = self.evidence.get(ev_id)
        return ev.public() if ev else None


# ---------------------------------------------------------------------------
# 回答组装（证据约束 —— 绝不在证据之外生成内容）
# ---------------------------------------------------------------------------


def build_rule_answer(query: str, result: RetrievalResult) -> dict[str, Any]:
    """规则版回答：模板化拼装检索到的证据原文，不做任何改写或补充。

    这是「降级不伪装」的落点 —— 输出结构里 mode=rule，
    前端据此显示「规则版」徽章，绝不冒充大模型生成。
    """
    if result.refused:
        # 拒答措辞必须与「双轨并行 + 分层输出」的语义一致：
        #  ① 不再只说「语料没收录」，而是「离线语料 + 实时权威源都没找到可核实依据」；
        #  ② 必须如实告知**已经查过多少源**——「查了没查到」本身就是有价值的核证结果，
        #     只说「不回答」会让人误以为系统没查（这也是用户报障「假查证」的来源之一）；
        #  ③ 若存在相关线索，说明它们仅供参考、不构成依据（不越权升级为结论）。
        live = result.live or {}
        queried = live.get("queried") or 0
        related_n = len(result.related)
        head = "未检索到与本问题相关的可核实依据，因此不作回答。\n"
        if queried:
            head += f"（本次已实时检索 {queried} 个权威源，未取得可核实依据；网络与逐源结果见下方「实时核证」区块）\n"
        if related_n:
            head += (f"（另有 {related_n} 条**主题相关但未经核实**的条目，已列在下方「相关线索」区，"
                     f"仅可作为找线索的起点，不构成依据）\n")
        return {
            "mode": "rule",
            "refused": True,
            "answer": head + f"（拒答依据：{result.refuse_reason}）",
            "citations": [],
        }

    lines = ["以下内容全部来自检索到的原文片段，未做改写："]
    for idx, ev in enumerate(result.evidence, start=1):
        lines.append(f"{idx}. {ev.text}［{ev.id}］")
    if result.divergences:
        lines.append("")
        lines.append("需要提请注意：本问题涉及多来源表述差异，详见「分歧」区块。")
    if result.related:
        # 规则版也必须把「线索」与「依据」用标题分隔，不许混在结论里一起陈述。
        lines.append("")
        lines.append(f"—— 以下为相关线索（{len(result.related)} 条，未核实，仅供参考，不作为依据）——")
        for item in result.related:
            lines.append(f"· {item.get('title', '')}［{item.get('source_name', '')}｜{item.get('published', '')}］{item.get('url', '')}")

    return {
        "mode": "rule",
        "refused": False,
        "answer": "\n".join(lines),
        "citations": [
            {"id": ev.id, "doc_title": ev.doc_title, "publisher": ev.publisher, "score": round(result.scores[i], 4)}
            for i, ev in enumerate(result.evidence)
        ],
    }


def verify_citations(answer_citations: list[dict[str, Any]], result: RetrievalResult) -> list[dict[str, Any]]:
    """证据准入校验：丢弃任何不在本次检索结果中的引用编号。

    返回被丢弃的编号列表（供轨迹展示）—— 这是「越界证据即丢弃」的落点。
    """
    allowed = {ev.id for ev in result.evidence}
    if result.divergences:
        for dv in result.divergences:
            for side in dv.get("sides", []):
                allowed.add(side["evidence_id"])

    dropped = [c for c in answer_citations if c.get("id") not in allowed]
    return dropped


# ---------------------------------------------------------------------------
# 自检（python rag.py 可直接运行）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    corpus = Corpus()
    print(f"语料：{corpus.meta.get('name')}")
    print(f"统计：{corpus.graph()['stats']}")
    print(f"拒答阈值={REFUSE_THRESHOLD}  高区分度 IDF 阈值 P70={corpus.idf_p70:.3f}\n")

    # 回归用例：(查询, 期望, 期望主题前缀 or None, 期望检出分歧 or None)
    CASES: list[tuple[str, str, str | None, str | None]] = [
        ("5号线开通后客流多少？", "pass", "ev-metro", "dv-metro-flow"),
        ("5号线开通首周有多少人坐？", "pass", "ev-metro", None),
        ("加装电梯业主需要出多少钱？", "pass", "ev-lift", None),
        ("加装电梯为什么推不动？", "pass", "ev-lift", "dv-lift-effort"),
        ("社区图书馆延时开放效果如何？", "pass", "ev-library", None),
        ("5号线客流预测口径有没有矛盾？", "pass", "ev-metro", "dv-metro-flow"),
        ("5号线一共几个站？", "pass", "ev-metro", None),
        ("加装电梯的补贴标准是多少？", "pass", "ev-lift", None),
        # -- 真实来源（世界银行 CC BY 4.0）—— 证明「能处理真实来源且可溯源」--
        ("2024 年中国城镇人口占总人口比重是多少？", "pass", "ev-wb-urban", None),
        ("中国互联网使用人口占比是多少？", "pass", "ev-wb-digital", None),
        ("中国可再生能源占能源消费的比重是多少？", "pass", "ev-wb-energy", None),
        ("中国国际旅游入境人次是多少？", "pass", "ev-wb-culture", None),
        ("城镇化率的中国口径和全球口径能直接比吗？", "pass", "ev-wb-urban", "dv-wb-urban-scope"),
        ("今天天气怎么样？", "refuse", None, None),
        ("量子计算机的退相干时间是多少？", "refuse", None, None),
        ("请帮我写一首关于春天的诗", "refuse", None, None),
        ("怎么申请房贷利率优惠？", "refuse", None, None),
        ("股票今天涨了吗", "refuse", None, None),
        ("红烧肉怎么做才好吃", "refuse", None, None),
        ("帮我写一段 Python 爬虫代码", "refuse", None, None),
        ("", "refuse", None, None),
        # -- 2026-09-18 实战抓到的两类假阳性（永久回归用例，防退化）--------------
        # ① 只命中 1 个原词（「时间」）却被放行；② 命中全部来自同义扩展词。
        ("苹果手机最新款多少钱", "refuse", None, None),
        ("这个电梯的股票代码是多少", "refuse", None, None),
    ]

    passed = 0
    failed: list[str] = []

    for query, expect, topic, expect_dv in CASES:
        r = corpus.search(query)
        got = "refuse" if r.refused else "pass"
        problems: list[str] = []

        if got != expect:
            problems.append(f"判定不符（期望 {expect} / 实得 {got}）")
        if expect == "pass" and topic:
            if not r.evidence or not all(ev.id.startswith(topic) for ev in r.evidence[: min(2, len(r.evidence))]):
                top = r.evidence[0].id if r.evidence else "无"
                problems.append(f"Top2 未落在期望主题 {topic}（实际首位 {top}）")
        if expect_dv and not any(d["id"] == expect_dv for d in r.divergences):
            problems.append(f"未检出期望分歧 {expect_dv}")

        label = f"[{'✅ PASS' if not problems else '❌ FAIL'}] {got:6s} conf={r.confidence:.3f}  「{query or '（空查询）'}」"
        print(label)
        if r.evidence:
            for ev, sc in zip(r.evidence[:2], r.scores[:2]):
                print(f"          · {sc:6.3f}  {ev.id}")
        if r.divergences:
            print(f"          ⚠ 分歧：{'; '.join(d['topic'] for d in r.divergences)}")
        for p in problems:
            print(f"          ❌ {p}")

        if problems:
            failed.append(query or "（空查询）")
        else:
            passed += 1

    print()
    print(f"{'=' * 62}")
    print(f"回归结果：{passed}/{len(CASES)} 通过；{len(failed)} 失败")
    if failed:
        print("失败用例：" + " ｜ ".join(failed))
    print(f"{'=' * 62}")
    sys.exit(0 if not failed else 1)
