"""证闻 · 世界银行开放数据（CC BY 4.0）语料抓取与生成

为什么要有这个脚本（可复现性要求）：
  参赛材料第 4 页要求「使用的核心技术、知识产权为参赛团队所有或经技术持有者书面授权」。
  世行数据许可为 CC BY 4.0 —— 允许复制 / 改编 / 纳入产品 / 商业使用，义务是署名 +
  不暗示世行背书 + 不使用世行名称与标识。

  为了让「数据从哪来」可被评委独立复核，本脚本把抓取过程完全固化：
    1. 原始 API 响应整包落盘 → corpus/_raw/worldbank-<日期>/   （审计留痕，可逐字节比对）
    2. 由原始响应派生语料 → corpus/wb_real.json               （派生件，可重建）
  任何时候删掉派生件重跑本脚本，都能得到同一份语料（除 API 侧数据修订外）。

用法：
    python tools/fetch_worldbank.py            # 抓取并生成语料
    python tools/fetch_worldbank.py --dry-run  # 只抓取打印，不写文件

零第三方依赖（urllib），与 rag.py / app.py 的部署约束一致。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS_DIR = os.path.join(BASE_DIR, "corpus")
RAW_DIR = os.path.join(CORPUS_DIR, "_raw")
OUT_PATH = os.path.join(CORPUS_DIR, "wb_real.json")

RETRIEVED = "2026-09-18"
API_BASE = "https://api.worldbank.org/v2"
USER_AGENT = "Zhengwen-Demo/0.1 (iCAN competition prototype; contact via submission channel)"

# ---------------------------------------------------------------------------
# 抓取范围：四个主题方向（用户 2026-09-18 决策「四个方向全接」）
# 每个指标 = 一个「文档」，每年一条证据。
# ---------------------------------------------------------------------------

TOPIC_LABELS = {
    "urban": "城市与交通（世界银行 WDI）",
    "digital": "数字经济（世界银行 WDI）",
    "energy": "能源与气候（世界银行 WDI）",
    "culture": "文旅与文化（世界银行 WDI）",
}

INDICATORS: list[dict] = [
    # -- 城市与交通 ---------------------------------------------------------
    {
        "topic": "urban",
        "code": "SP.URB.TOTL.IN.ZS",
        "name_zh": "城镇人口占总人口比重",
        "unit_zh": "%",
        "countries": ["CHN", "WLD"],
        "value_kind": "percent",
    },
    {
        "topic": "urban",
        "code": "SP.URB.GROW",
        "name_zh": "城镇人口年增长率",
        "unit_zh": "%",
        "countries": ["CHN", "WLD"],
        "value_kind": "percent",
    },
    {
        "topic": "urban",
        "code": "SP.URB.TOTL",
        "name_zh": "城镇人口总数",
        "unit_zh": "人",
        "countries": ["CHN", "WLD"],
        "value_kind": "count_ppl",
    },
    # -- 数字经济 -----------------------------------------------------------
    {
        "topic": "digital",
        "code": "IT.NET.USER.ZS",
        "name_zh": "互联网使用人口占比",
        "unit_zh": "%",
        "countries": ["CHN", "WLD"],
        "value_kind": "percent",
    },
    {
        "topic": "digital",
        "code": "IT.CEL.SETS.P2",
        "name_zh": "每百人移动蜂窝电话订阅数",
        "unit_zh": "户/百人",
        "countries": ["CHN", "WLD"],
        "value_kind": "per100",
    },
    {
        "topic": "digital",
        "code": "IT.NET.BBND.P2",
        "name_zh": "每百人固定宽带订阅数",
        "unit_zh": "户/百人",
        "countries": ["CHN", "WLD"],
        "value_kind": "per100",
    },
    # -- 能源与气候 ---------------------------------------------------------
    {
        "topic": "energy",
        "code": "EG.FEC.RNEW.ZS",
        "name_zh": "可再生能源占最终能源消费比重",
        "unit_zh": "%",
        "countries": ["CHN", "WLD"],
        "value_kind": "percent",
    },
    {
        "topic": "energy",
        "code": "EG.ELC.ACCS.ZS",
        "name_zh": "通电人口占比",
        "unit_zh": "%",
        "countries": ["CHN", "WLD"],
        "value_kind": "percent",
    },
    {
        "topic": "energy",
        "code": "EG.USE.PCAP.KG.OE",
        "name_zh": "人均能源使用量",
        "unit_zh": "千克油当量",
        "countries": ["CHN", "WLD"],
        "value_kind": "plain",
    },
    # -- 文旅与文化 ---------------------------------------------------------
    {
        "topic": "culture",
        "code": "ST.INT.ARVL",
        "name_zh": "国际旅游入境人次",
        "unit_zh": "人次",
        "countries": ["CHN", "WLD"],
        "value_kind": "count_big",
    },
    {
        "topic": "culture",
        "code": "ST.INT.RCPT.CD",
        "name_zh": "国际旅游收入",
        "unit_zh": "现价美元",
        "countries": ["CHN", "WLD"],
        "value_kind": "usd_big",
    },
    {
        "topic": "culture",
        "code": "ST.INT.DPRT",
        "name_zh": "本国居民出境旅游人次",
        "unit_zh": "人次",
        "countries": ["CHN", "WLD"],
        "value_kind": "count_big",
    },
]

COUNTRY_ZH = {"CHN": "中国", "WLD": "全球（世界银行口径）"}

# --from-raw：离线重建开关（在 main 中按命令行参数设置）
USE_RAW = False

# 每个指标最多保留的年数（证据条数控制：语料要能演示，也要控制体积）
YEARS_KEEP = 5


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------


def fetch_indicator(code: str, country: str, retries: int = 3, timeout: int = 30) -> list[dict]:
    """抓取单个指标 × 单个国家/地区的全部年份数据（API 分页全取）。

    --from-raw 模式：直接用 RAW_DIR 中已落盘的原始快照重建（不联网），
    用于离线复现 / 断网演示 / 复核历史抓取结果。
    """
    safe = f"{code}__{country}"
    if USE_RAW:
        cached = os.path.join(RAW_DIR, f"{safe}.json")
        if not os.path.isfile(cached):
            print(f"  ! --from-raw 但缺少快照：{safe}.json", file=sys.stderr)
            return []
        with open(cached, "r", encoding="utf-8") as fh:
            rows = (json.load(fh)[1] or [])
        return [r for r in rows if r.get("value") is not None]

    url = (
        f"{API_BASE}/country/{country}/indicator/{code}"
        f"?format=json&per_page=200&date=2000:2025"
    )
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            payload = json.loads(raw)
            if not isinstance(payload, list) or len(payload) < 2:
                raise ValueError(f"返回结构异常：{str(payload)[:200]}")
            rows = payload[1] or []
            # 保留原始响应（审计留痕）
            os.makedirs(RAW_DIR, exist_ok=True)
            with open(os.path.join(RAW_DIR, f"{safe}.json"), "w", encoding="utf-8") as fh:
                fh.write(raw)
            return [r for r in rows if r.get("value") is not None]
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as err:
            last_err = err
            print(f"  ! 第 {attempt}/{retries} 次失败：{code} / {country} —— {err}", file=sys.stderr)
            time.sleep(2 * attempt)
    raise RuntimeError(f"抓取失败（已重试 {retries} 次）：{code} / {country}") from last_err


# ---------------------------------------------------------------------------
# 数值格式化（证据文本要能被普通读者读懂，同时保留原始量级）
# ---------------------------------------------------------------------------


def fmt_value(value: float, kind: str) -> str:
    if kind == "percent":
        return f"{value:.1f}%"
    if kind == "per100":
        return f"{value:.1f}"
    if kind == "count_km":
        if value >= 10000:
            return f"{value / 10000:.1f} 万公里"
        return f"{value:.0f} 公里"
    if kind == "count_ppl":
        if value >= 100000000:
            return f"{value / 100000000:.2f} 亿人"
        if value >= 10000:
            return f"{value / 10000:.1f} 万人"
        return f"{value:.0f} 人"
    if kind == "count_big":
        if value >= 100000000:
            return f"{value / 100000000:.2f} 亿人次"
        if value >= 10000:
            return f"{value / 10000:.1f} 万人次"
        return f"{value:.0f} 人次"
    if kind == "usd_big":
        if value >= 1000000000000:
            return f"{value / 1000000000000:.2f} 万亿美元"
        if value >= 100000000:
            return f"{value / 100000000:.1f} 亿美元"
        return f"{value:.0f} 美元"
    return f"{value:,.0f}"


def build_documents() -> tuple[list[dict], list[str]]:
    documents: list[dict] = []
    warnings: list[str] = []

    for spec in INDICATORS:
        topic = spec["topic"]
        for country in spec["countries"]:
            rows = fetch_indicator(spec["code"], country)
            if not rows:
                warnings.append(f"{spec['code']} / {country} 无可用数据点 → 跳过")
                continue

            rows.sort(key=lambda r: r["date"], reverse=True)
            rows = rows[:YEARS_KEEP]
            rows.sort(key=lambda r: r["date"])  # 证据按时间正序，便于阅读

            doc_id = f"doc-wb-{topic}-{spec['code'].lower().replace('.', '-')}-{country.lower()}"
            dataset_url = f"https://data.worldbank.org/indicator/{spec['code']}?locations={country}"
            api_url = (
                f"{API_BASE}/country/{country}/indicator/{spec['code']}?format=json&per_page=200"
            )
            country_zh = COUNTRY_ZH.get(country, country)

            paragraphs = []
            for idx, row in enumerate(rows, start=1):
                year = row["date"]
                text = (
                    f"{year} 年，{country_zh}的{spec['name_zh']}为 "
                    f"{fmt_value(float(row['value']), spec['value_kind'])}"
                    f"（世界银行 WDI 指标 {spec['code']}，{spec['unit_zh']}，"
                    f"数据来源：世界银行开放数据，许可 CC BY 4.0）。"
                )
                paragraphs.append({"id": f"ev-wb-{topic}-{country.lower()}-{spec['code'].lower().replace('.', '-')}-{year}", "text": text})

            documents.append(
                {
                    "id": doc_id,
                    "topic": topic,
                    "title": f"世界银行数据：{country_zh} · {spec['name_zh']}（{spec['code']}）",
                    "publisher": "世界银行开放数据（World Bank Open Data）",
                    "published": rows[-1]["date"],
                    "kind": "开放数据指标",
                    "license": "CC BY 4.0",
                    "source_url": dataset_url,
                    "api_url": api_url,
                    "attribution": f"The World Bank: World Development Indicators: {api_url}",
                    "stance": "量化事实（可逐点回溯至世行 API）",
                    "paragraphs": paragraphs,
                }
            )
            print(f"  ✓ {spec['code']:>20s} / {country}  ← {len(paragraphs)} 条证据")

    # -- 口径说明文档（本项目撰写，依据世行 WDI 指标元数据）----------------------
    # 为什么单独成文档：真实数据里确实存在「跨国口径不可直接比较」这一方法论限制，
    # 产品承诺「多源不一致就都说」。把这条限制写成可检索的证据，用户问到口径/比较时
    # 系统才能把它作为分歧并列展示，而不是默默给出一个看起来确定的结论。
    documents.append(
        {
            "id": "doc-wb-method-urban-scope",
            "topic": "urban",
            "title": "口径说明：城镇人口占比的跨国比较限制（依据世行 WDI 指标元数据）",
            "publisher": "证闻项目组（依据世界银行 WDI 指标元数据撰写）",
            "published": RETRIEVED,
            "kind": "口径说明",
            "license": "本项目撰写（引用世界银行 WDI 方法论，非世行原文）",
            "source_url": "https://data.worldbank.org/indicator/SP.URB.TOTL.IN.ZS",
            "attribution": (
                "依据 The World Bank: World Development Indicators: "
                "SP.URB.TOTL.IN.ZS 指标元数据撰写；世行未参与本项目"
            ),
            "stance": "口径提示（提醒不可直接合并比较）",
            "paragraphs": [
                {
                    "id": "ev-wb-urban-method-p1",
                    "text": (
                        "世界银行 WDI 对「城镇人口占总人口比重」有方法论提示：各国对「城镇」的"
                        "定义不同（行政区划、人口密度、建成区标准各异），该指标在不同国家或地区"
                        "之间直接比较会引入口径差异。"
                    ),
                },
                {
                    "id": "ev-wb-urban-method-p2",
                    "text": (
                        "证闻项目组据此标注：中国的城镇人口占比与全球合计值可以并列展示，"
                        "但不应被当作「谁更城市化」的单一结论；本系统对这类跨口径比较只做并列，不做合并。"
                    ),
                },
            ],
        }
    )

    return documents, warnings


def build_divergences(documents: list[dict]) -> list[dict]:
    """真实可解释的「口径差异」组。

    为什么是这一组（诚实性要求）：
      世行 WDI 的「城镇人口占比」在方法论上明确提示：各国对「城镇」的定义不同，
      跨国家直接比较该指标会引入口径差异。因此我们把「中国」与「全球」两个数值
      并列展示，并把差异原因写清楚 —— 这正是产品「多源不一致就都说，不替你合并成
      一个结论」的真实案例，而不是编造的冲突。
    """
    ev_ids: list[str] = []
    doc_ids: list[str] = []
    for doc in documents:
        if doc["topic"] == "urban" and (
            "sp-urb-totl-in-zs" in doc["id"] or doc["kind"] == "口径说明"
        ):
            doc_ids.append(doc["id"])
            for para in doc["paragraphs"]:
                ev_ids.append(para["id"])
    if len(ev_ids) < 2:
        return []
    return [
        {
            "id": "dv-wb-urban-scope",
            "topic": "城镇人口占比：中国口径与全球口径不可直接比较",
            "doc_ids": doc_ids,
            "summary": (
                "世界银行 WDI 对「城镇人口占比」有明确口径提示：各国对城镇的定义不同"
                "（行政区划、人口密度、建成区标准各异），因此中国的数值与全球合计值"
                "并列展示，但不应被当作「谁更城市化」的单一结论。系统并列两个口径"
                "并标注差异成因，由使用者自行判断。"
            ),
            "evidence_ids": ev_ids,
        }
    ]


def build_corpus() -> dict:
    print("抓取世界银行开放数据（CC BY 4.0）…")
    documents, warnings = build_documents()
    divergences = build_divergences(documents)
    for w in warnings:
        print(f"  ⚠ {w}", file=sys.stderr)

    return {
        "meta": {
            "id": "WB",
            "name": "世界银行开放数据（WDI）真实指标",
            "kind": "open-data",
            "license": "CC BY 4.0",
            "license_note": (
                "Creative Commons Attribution 4.0 International。允许复制 / 分发 / 改编 /"
                "纳入产品 / 商业使用，须署名、不得暗示世行背书、不得使用世行名称与标识。"
            ),
            "terms_url": "https://data.worldbank.org/summary-terms-of-use",
            "attribution_required": "The World Bank: World Development Indicators: <API URL>",
            "badge": "公开来源",
            "retrieved": RETRIEVED,
            "generated_by": "src/tools/fetch_worldbank.py",
            "raw_snapshot_dir": "src/corpus/_raw/",
            "disclaimer": (
                "本语料为世界银行开放数据的真实数值，按 CC BY 4.0 使用；世界银行未参与、"
                "未背书本项目，本项目不得使用世行名称与标识。数值以 API 原始返回为准，"
                "原始响应快照见 raw_snapshot_dir。"
            ),
            "topic_labels": TOPIC_LABELS,
        },
        "documents": documents,
        "divergences": divergences,
        "scope_note": (
            "本语料覆盖世界银行 WDI 四类指标：城市与交通、数字经济、能源与气候、文旅与文化。"
            "此范围之外的问题应触发拒答闸门。"
        ),
    }


def main() -> None:
    global USE_RAW
    parser = argparse.ArgumentParser(description="世界银行开放数据语料抓取（CC BY 4.0）")
    parser.add_argument("--dry-run", action="store_true", help="只抓取打印，不写语料文件")
    parser.add_argument(
        "--from-raw",
        action="store_true",
        help="用 corpus/_raw/ 中已落盘的原始快照重建语料（不联网，可离线复现）",
    )
    args = parser.parse_args()
    USE_RAW = bool(args.from_raw)

    corpus = build_corpus()
    stats = {
        "documents": len(corpus["documents"]),
        "evidence": sum(len(d["paragraphs"]) for d in corpus["documents"]),
        "divergences": len(corpus["divergences"]),
    }

    if args.dry_run:
        print(f"\n[dry-run] {stats}")
        return

    os.makedirs(CORPUS_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(corpus, fh, ensure_ascii=False, indent=1)
    print(f"\n已生成：{OUT_PATH}")
    print(f"文档 {stats['documents']} / 证据 {stats['evidence']} / 分歧 {stats['divergences']}")
    print(f"原始快照：{RAW_DIR}")


if __name__ == "__main__":
    main()
