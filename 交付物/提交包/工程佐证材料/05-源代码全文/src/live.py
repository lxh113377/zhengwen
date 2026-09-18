"""证闻 · 实时核证层（在线权威源检索 + 证据条目化 + 降级不伪装）

为什么有这一层
  原来的检索只在**固定语料**内进行，无法回答语料之外的问题 —— 那是「假查证」。
  本模块让「证闻」在收到提问时**实时**向公开权威源发起检索，命中的才给结论并回链原文；
  未命中就明确说明「未检索到可核实依据」，绝不编造。

源从哪来（不是拍脑袋，是 2026-09-19 十一轮实测的结论，逐条见 _note）
  ✅ 中国政府网·国务院文件库 / 部门文件库 —— 真关键词检索（JSON API）
  ✅ 央视网新闻列表 —— 服务端渲染的最新条目池（关键词过滤）
  ✅ 联合国新闻（中文）—— RSS 最新条目池
  ✅ 世界卫生组织 WHO —— 官方 JSON 新闻接口
  ✅ 原文正文抽取 —— 对命中的 URL 抽正文，生成真实摘录（点回原文的落点）
  ⚠️ 通用检索插槽 —— 需配置 WEB_SEARCH_API_URL / WEB_SEARCH_API_KEY（Tavily / 博查 / Serper 兼容）
  ❌ 实测不可用的源保留登记（_note 写明失败形态），供《实时源登记表》与答辩追问

合规纪律（与项目红线一致）
  1. 每条证据**只保留标题 / 文号 / 日期 / 短摘录（≤280 字）+ 原文链接**，不做全文转载；
  2. 逐条携带 source / license / fetched_at，界面必须显示「抓取时间」；
  3. 任何失败都不抛裸错误串到界面，只降级并如实标注原因。

纯标准库实现（urllib / concurrent.futures / xml / json），保证免费托管与干净环境直接可跑。
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from rag import STOPWORDS, Evidence, RetrievalResult, TraceStep, tokenize

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 单源超时（秒）：单源挂起不得拖垮整轮问答
PER_SOURCE_TIMEOUT = float(os.environ.get("LIVE_PER_SOURCE_TIMEOUT", "8"))
# 整轮实时检索总预算（秒）：到点即用已到达的结果收口，未返回的源标注「超时未返回」
TOTAL_BUDGET = float(os.environ.get("LIVE_TOTAL_BUDGET", "13"))
# 单条摘录上限（合规：只做短摘录，不转载全文）
SNIPPET_LIMIT = 280
# 实时证据最多保留条数
MAX_LIVE_EVIDENCE = 6
# 相关性闸门：查询原始 bigram 至少命中几个才承认「这条与本问题相关」
# （与固定语料侧 MIN_ORIGINAL_HITS 同一设计哲学：命中必须来自查询原始词）
MIN_LIVE_BIGRAM_HITS = 2

# ---------------------------------------------------------------------------
# 分层输出（2026-09-19 上线）
# ---------------------------------------------------------------------------
# 起因（用户报障）：预览版「拒答率太高」。诊断结论是**召回不足**，不是闸门过严 ——
# 但直接放宽闸门会把弱相关条目当成依据，等于把作品做回「假查证」。
# 因此改为**两层**，能力与诚实同时保住：
#   strict（可核实依据）—— 判据丝毫未放松：原始 bigram 命中 ≥ MIN_LIVE_BIGRAM_HITS
#                          只有 strict 能进结论、进模型 prompt、参与拒答判定
#   related（相关线索）—— 命中 ≥ RELATED_MIN_HITS（更弱），**仅作线索展示**
#                          界面必须标「未核实」、不得作为依据、不得写成结论
# 关键纪律：related 永远不参与 refused 判定 —— 否则「有条目沾边就放行」= 闸门失效。
RELATED_MIN_HITS = 1
MAX_RELATED = 5
# 滚动新闻列表页翻页数（1 页约 130—150 条；用于扩大「最新条目池」）
ROLL_PAGES = int(os.environ.get("LIVE_ROLL_PAGES", "2"))
# 是否对命中的原文 URL 抽取正文（更真实的取证；受总预算约束）
FETCH_ARTICLE = os.environ.get("LIVE_FETCH_ARTICLE", "1").strip().lower() not in ("0", "false", "no", "off")
ARTICLE_TIMEOUT = float(os.environ.get("LIVE_ARTICLE_TIMEOUT", "5"))
ARTICLE_FETCH_LIMIT = int(os.environ.get("LIVE_ARTICLE_LIMIT", "3"))

# 通用检索插槽（可选；未配置则明确标注未启用，不静默跳过）
WEB_SEARCH_URL = os.environ.get("WEB_SEARCH_API_URL", "").strip()
WEB_SEARCH_KEY = os.environ.get("WEB_SEARCH_API_KEY", "").strip()
WEB_SEARCH_PROVIDER = os.environ.get("WEB_SEARCH_PROVIDER", "通用检索").strip()

ENABLED = os.environ.get("LIVE", "1").strip().lower() not in ("0", "false", "no", "off")

# 口语问法里区分度极低的 bigram（不参与相关性判定，避免「多少/怎么」凑命中）
WEAK_BIGRAMS = frozenset(
    """多少 怎么 为什 什么 一下 介绍 请问 是否 哪些 哪个 如何 最新 消息 情况 相关 内容
    告诉 我一下 知道 目前 现在 最近 关于 方面""".split()
)

# ---------------------------------------------------------------------------
# 聚合检索通道 + 权威域白名单（2026-09-19 新增，用户拍板「扩源两通道都接」后落地）
# ---------------------------------------------------------------------------
# 为什么需要它（用户报障：拒答率太高、像固定语料的假查证）：
#   逐个实测 36 个检索入口后，**真关键词检索**只有极少数可用 —— 中国政府网两个文件库、
#   央视网站内检索，以及 2 个免 key 的通用聚合检索。聚合页会混入财经号、自媒体、
#   百科与视频聚合，**因此必须用白名单把结果收窄到权威域**：既不放弃召回面，
#   也不把来源不明的条目当依据（与「宁少不假」同一纪律，见 UNAVAILABLE 里头条的处置）。
AUTH_DOMAINS: tuple[str, ...] = (
    # 政府
    "gov.cn",
    # 中央重点新闻网站 / 国家媒体
    "people.com.cn", "xinhuanet.com", "news.cn", "cctv.com", "cnr.cn",
    "chinanews.com.cn", "gmw.cn", "chinadaily.com.cn", "ce.cn", "youth.cn",
    "qstheory.cn", "china.com.cn", "81.cn", "huanqiu.com",
    # 持牌主流机构媒体（财经/都市）
    "thepaper.cn", "yicai.com", "stcn.com", "cs.com.cn", "nbd.com.cn",
    "jiemian.com", "legaldaily.com.cn",
)

# 域 → 中文媒体名。用于把「聚合页给的来源字符串」换成**由域名推出的可核验来源名**
# （聚合页的 cite 文本可能被截断或写错，域名才是硬证据）。
AUTH_DOMAIN_NAMES: dict[str, str] = {
    "gov.cn": "中国政府网",
    "people.com.cn": "人民网",
    "xinhuanet.com": "新华网",
    "news.cn": "新华网",
    "cctv.com": "央视网",
    "cnr.cn": "央广网",
    "chinanews.com.cn": "中国新闻网",
    "gmw.cn": "光明网",
    "chinadaily.com.cn": "中国日报",
    "ce.cn": "中国经济网",
    "youth.cn": "中国青年网",
    "qstheory.cn": "求是网",
    "china.com.cn": "中国网",
    "81.cn": "中国军网",
    "huanqiu.com": "环球网",
    "thepaper.cn": "澎湃新闻",
    "yicai.com": "第一财经",
    "stcn.com": "证券时报",
    "cs.com.cn": "中国证券报",
    "nbd.com.cn": "每日经济新闻",
    "jiemian.com": "界面新闻",
    "legaldaily.com.cn": "法治日报",
}

# 聚合检索入口（免 key）。实测结论见对应 SourceSpec.note。
AGG_ENGINES: dict[str, tuple[str, str]] = {
    # engine → (检索 URL 模板, Referer)
    "so360": ("https://news.so.com/ns?q={q}", "https://news.so.com/"),
    "baidu": ("https://www.baidu.com/s?word={q}&tn=news&rn=20", "https://www.baidu.com/"),
}

# 360 结果块：<li ... data-url="原文直链" ...>…</li>，块内含标题/摘要/来源/时间
_AGG_360_ITEM = re.compile(r'<li[^>]*data-url="(https?://[^"]+)"[^>]*>(.*?)</li>', re.S | re.I)
_AGG_TITLE_ATTR = re.compile(r'\btitle="([^"]{6,200})"')
_AGG_SITE = re.compile(r'<cite[^>]*class="[^"]*sitename[^"]*"[^>]*>(.*?)</cite>', re.S | re.I)
_AGG_SUMMARY = re.compile(r'<p[^>]*class="[^"]*summary[^"]*"[^>]*>(.*?)</p>', re.S | re.I)
_AGG_TIME = re.compile(r'<span[^>]*class="[^"]*\btime\b[^"]*"[^>]*>(.*?)</span>', re.S | re.I)
# 聚合页常见的相对时间写法（「12小时前」「3天前」）与绝对日期
_AGG_REL = re.compile(r"(\d+\s*(?:分钟|小时|天)前|今天|昨天)")


def _http_host(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def auth_domain(url: str) -> str | None:
    """URL 是否落在权威域白名单内；返回命中的白名单条目（便于归因），否则 None。

    匹配规则：等于该项，或为其子域（`m.gmw.cn` → `gmw.cn`）。
    """
    host = _http_host(url)
    if not host:
        return None
    for dom in AUTH_DOMAINS:
        if host == dom or host.endswith("." + dom):
            return dom
    return None


def _agg_items(engine: str, page: str) -> list[tuple[str, str, str, str, str]]:
    """从聚合结果页抽出 (原文URL, 标题, 摘要, 来源名, 时间)。纯函数，便于离线单测。"""
    items: list[tuple[str, str, str, str, str]] = []
    if engine == "so360":
        for href, block in _AGG_360_ITEM.findall(page):
            tm = _AGG_TITLE_ATTR.search(block)
            title = strip_html(tm.group(1)) if tm else ""
            if not title:
                continue
            sm = _AGG_SUMMARY.search(block)
            gm = _AGG_SITE.search(block)
            wm = _AGG_TIME.search(block)
            items.append((
                href,
                _CJK_SPACE.sub("", title),
                strip_html(sm.group(1)) if sm else "",
                strip_html(gm.group(1)) if gm else "",
                strip_html(wm.group(1)) if wm else "",
            ))
        return items
    # 兜底形态（含百度等未知结构）：只认「href 本身即原文直链」的锚点 ——
    # 白名单过滤必须知道目标域；跳转壳（如 /link?url=）无法在抓取阶段核验，宁可丢弃。
    for href, inner in _ANCHOR.findall(page):
        if not href.startswith("http"):
            continue
        title = _CJK_SPACE.sub("", strip_html(inner))
        if len(title) < 8:
            continue
        when = _AGG_REL.search(title)
        items.append((href, title, "", "", when.group(0) if when else ""))
    return items


# ---------------------------------------------------------------------------
# 源登记表（实测结论固化于此 —— 界面与《实时源登记表》共用同一来源）
# ---------------------------------------------------------------------------
#
# _note 写的是**实测形态**，不是猜测；available=False 的源不会发起请求，
# 但仍会出现在 /api/live/sources 与登记表里 —— 让「接了哪些、为什么没接哪些」可被追问。

@dataclass
class SourceSpec:
    id: str
    name: str
    publisher: str
    kind: str
    license: str
    terms_url: str
    adapter: str
    available: bool = True
    note: str = ""
    params: dict[str, Any] = field(default_factory=dict)


SOURCES: list[SourceSpec] = [
    SourceSpec(
        id="LIVE-GOV-GW",
        name="中国政府网 · 国务院文件库",
        publisher="中国政府网",
        kind="policy",
        license="政府网站内容：可引用，须注明来源与网址；本项目仅做短摘录与链接",
        terms_url="https://www.gov.cn/",
        adapter="gov_policy",
        note="实测：search-gov/data JSON 接口可用（t=zhengcelibrary_gw，关键词「加装电梯」命中 189 条，返回 50 条中 15 条正文含实词）",
        params={"t": "zhengcelibrary_gw"},
    ),
    SourceSpec(
        id="LIVE-GOV-BM",
        name="中国政府网 · 部门文件库",
        publisher="中国政府网（各部委）",
        kind="policy",
        license="政府网站内容：可引用，须注明来源与网址；本项目仅做短摘录与链接",
        terms_url="https://www.gov.cn/",
        adapter="gov_policy",
        note="实测：同上接口 t=zhengcelibrary_bm，命中 214 条（同一问题）",
        params={"t": "zhengcelibrary_bm"},
    ),
    SourceSpec(
        id="LIVE-CCTV-SEARCH",
        name="央视网 · 站内检索",
        publisher="央视网",
        kind="news",
        license="央视网内容版权归央视所有：本项目仅引用标题与原文链接，不转载正文",
        terms_url="https://search.cctv.com/",
        adapter="cctv_search",
        note="2026-09-19 第二轮实测更正：search.cctv.com/search.php?qtext= 为**服务端渲染**，"
             "可稳定抽出结果（10 条/页、支持翻页、带日期），覆盖 news.cctv.com 与 energy.cctv.com 等子域；"
             "结果链接形如 link_p.php?targetpage=<urlencoded 原文地址>，须解码后再回链原文。"
             "⚠️ 此前登记的「JS 渲染、无可用 XHR」系误判，已更正。"
             "实证：「英伟达」7 条、「新能源汽车出口」6 条（含 2026-09-11 条目）",
        params={"pages": 2, "max_variants": 2},
    ),
    SourceSpec(
        id="LIVE-CCTV",
        name="央视网 · 新闻频道最新条目",
        publisher="央视网",
        kind="news",
        license="央视网内容版权归央视所有：本项目仅引用标题与原文链接，不转载正文",
        terms_url="https://news.cctv.com/",
        adapter="list_pool",
        note="实测：news.cctv.com 列表页服务端渲染，可抽到当日条目（09-18，6 条）；"
             "站内检索见 LIVE-CCTV-SEARCH（可用，承担关键词检索职责）",
        params={"url": "https://news.cctv.com/"},
    ),
    SourceSpec(
        id="LIVE-CHINANEWS-ROLL",
        name="中国新闻网 · 滚动新闻",
        publisher="中国新闻网",
        kind="news",
        license="中国新闻网内容版权归中新社所有：本项目仅引用标题与原文链接，不转载正文",
        terms_url="https://www.chinanews.com.cn/",
        adapter="roll_pool",
        note="实测：scroll-news/news{n}.html 服务端渲染，每页 145 条**带日期**条目（最新当日），"
             "支持翻页 → 池规模由「首页几十条」提升到数百条",
        params={"url": "https://www.chinanews.com.cn/scroll-news/news{n}.html", "pages": ROLL_PAGES},
    ),
    SourceSpec(
        id="LIVE-CHINADAILY",
        name="中国日报中文网 · 滚动条目",
        publisher="中国日报",
        kind="news",
        license="中国日报内容版权归中国日报所有：本项目仅引用标题与原文链接，不转载正文",
        terms_url="https://cn.chinadaily.com.cn/",
        adapter="roll_pool",
        note="实测：首页可抽 136—145 条带日期条目（含当日）",
        params={"url": "https://cn.chinadaily.com.cn/", "pages": 1},
    ),
    SourceSpec(
        id="LIVE-YOUTH",
        name="中国青年网 · 滚动条目",
        publisher="中国青年网",
        kind="news",
        license="中国青年网内容版权归中青网所有：本项目仅引用标题与原文链接，不转载正文",
        terms_url="https://www.youth.cn/",
        adapter="roll_pool",
        note="实测：首页可抽 129—143 条带日期条目（含当日）",
        params={"url": "https://www.youth.cn/", "pages": 1},
    ),
    SourceSpec(
        id="LIVE-CE",
        name="中国经济网 · 滚动条目",
        publisher="中国经济网",
        kind="news",
        license="中国经济网内容版权归经济日报社所有：本项目仅引用标题与原文链接，不转载正文",
        terms_url="http://www.ce.cn/",
        adapter="roll_pool",
        note="实测：首页可抽 142—155 条带日期条目（含当日）",
        params={"url": "http://www.ce.cn/", "pages": 1},
    ),
    SourceSpec(
        id="LIVE-PEOPLE-HOME",
        name="人民网 · 首页最新条目",
        publisher="人民网",
        kind="news",
        license="人民网内容版权归人民网所有：本项目仅引用标题与原文链接，不转载正文",
        terms_url="http://www.people.com.cn/",
        adapter="list_pool",
        note="实测：人民网首页服务端渲染，可抽 82 条带日期的文章链接（最新 2026-09-14）；"
             "但其 RSS 停更（最新 2025-06-05）、站内检索被反爬拦截 → 只走「最新条目池 + 关键词过滤」",
        params={"url": "http://www.people.com.cn/"},
    ),
    SourceSpec(
        id="LIVE-XINHUA-HOME",
        name="新华网 · 首页最新条目",
        publisher="新华网",
        kind="news",
        license="新华网内容版权归新华社所有：本项目仅引用标题与原文链接，不转载正文",
        terms_url="https://www.news.cn/",
        adapter="list_pool",
        note="实测：新华网首页可抽 14 条当日文章链接（2026-09-18）；"
             "站内检索 so.news.cn 全组合 405、RSS 无 pubDate → 只走「最新条目池 + 关键词过滤」",
        params={"url": "https://www.news.cn/"},
    ),
    SourceSpec(
        id="LIVE-UN",
        name="联合国新闻（中文）",
        publisher="联合国新闻",
        kind="news",
        license="联合国新闻：可引用，须注明来源；本项目仅引用标题与链接",
        terms_url="https://news.un.org/zh/",
        adapter="rss_pool",
        available=False,
        note="2026-09-18 实测：中文 RSS 可用且新鲜（最新条目 2026-09-18）→ 曾列为可用源；"
             "⚠️ 2026-09-19 在 CloudBase 线上环境复测**返回 HTTP 404**（同一 URL，疑为对方改版或 CDN 变更）"
             "→ 按「不把失效源算作可用」的纪律**暂置 available=False**，保留登记待复核；"
             "复核通过后可一行改回（本条按 R241 保留原实测结论，不抹除）",
        params={"url": "https://news.un.org/feed/subscribe/zh/news/all/rss.xml"},
    ),
    SourceSpec(
        id="LIVE-WHO",
        name="世界卫生组织 WHO · 新闻",
        publisher="世界卫生组织",
        kind="org",
        license="WHO 内容：允许非商业引用，须注明来源",
        terms_url="https://www.who.int/news",
        adapter="who_news",
        note="实测：https://www.who.int/api/news/newsitems?top=N JSON 可用（返回 50 条带日期）",
    ),
    SourceSpec(
        id="LIVE-AGG-SO360",
        name="360资讯 · 聚合检索（权威域白名单）",
        publisher="聚合检索通道",
        kind="news",
        license="仅引用标题与原文链接，正文版权归原发布媒体；聚合页本身不作转载",
        terms_url="https://news.so.com/",
        adapter="agg_search",
        note="2026-09-19 实测：news.so.com/ns?q= **服务端渲染**，单页约 35 个结果锚点、"
             "**直链原文**（finance.sina / thepaper / jiemian / m.gmw 等），结果块内含 "
             "<cite class=sitename> 来源与 <span class=time> 时间；同页「热搜」侧栏是 so.com 域 → "
             "由白名单自动剔除。作用：把「任意问题」的检索面从各源首页池扩到全网，"
             "再由白名单收窄到权威域（用户拍板「两通道都接」后落地）",
        params={"engine": "so360", "max_variants": 2},
    ),
    SourceSpec(
        id="LIVE-AGG-BAIDU",
        name="百度资讯 · 聚合检索（权威域白名单）",
        publisher="聚合检索通道",
        kind="news",
        license="仅引用标题与原文链接，正文版权归原发布媒体；聚合页本身不作转载",
        terms_url="https://www.baidu.com/",
        adapter="agg_search",
        available=False,
        note="⚠️ 2026-09-19 实测判定为**不启用**（宁少不假）：同一 IP 前两次请求成功"
             "（598KB/56 锚点、570KB/47 锚点，标题命中 22/10），**第 3 次起持续返回 1.4KB 反爬页**"
             "（等待 75s 复测仍被拦）；且两次成功抓取只拿到统计，"
             "**未取得其成功页的链接结构样本** → 无法确认结果链接是原文直链还是 /link?url= 跳转壳，"
             "而白名单过滤必须先知道目标域。适配器已就绪（engine=baidu），参数一行即可启用",
        params={"engine": "baidu", "max_variants": 1},
    ),
    SourceSpec(
        id="LIVE-WEB",
        name="通用检索插槽（可选）",
        publisher=WEB_SEARCH_PROVIDER,
        kind="search",
        license="由所选检索服务商条款决定",
        terms_url="",
        adapter="web_search",
        available=bool(WEB_SEARCH_URL and WEB_SEARCH_KEY),
        note=("已配置：并入全网检索结果（按白名单域过滤）" if (WEB_SEARCH_URL and WEB_SEARCH_KEY)
              else "未配置 WEB_SEARCH_API_URL / WEB_SEARCH_API_KEY → 本次不启用（这是白名单直连之外的补充通道）"),
    ),
]

# 实测不可用、保留登记以便追溯（不发起请求）
UNAVAILABLE: list[dict[str, str]] = [
    {"id": "LIVE-XINHUA-SEARCH", "name": "新华网 · 站内检索",
     "note": "实测 so.news.cn/getNews 在 GET/POST、带 Referer/X-Requested-With 各组合下均返回 HTTP 405 → "
             "降级为由「新华网首页最新条目池」承担（见 LIVE-XINHUA-HOME）"},
    {"id": "LIVE-XINHUA-RSS", "name": "新华网 · 时政 RSS",
     "note": "实测可下载 300 条，但 XML 无 pubDate 字段 → 无法核验时效，故不采用（不拿旧闻冒充实时）"},
    {"id": "LIVE-PEOPLE-RSS", "name": "人民网 · 时政 RSS",
     "note": "实测 100 条且日期齐全，但最新条目停在 2025-06-05（停更一年多）→ 不作为实时源；"
             "改由「人民网首页最新条目池」承担（见 LIVE-PEOPLE-HOME）"},
    {"id": "LIVE-PEOPLE-SEARCH", "name": "人民网 · 站内检索",
     "note": "实测 search.people.cn 返回 3.4KB 空壳页（被反爬拦截）、politics.people.com.cn 频道页 403 → "
             "无法做全站关键词检索，只走首页最新条目池"},
    {"id": "LIVE-CCTV-SEARCH-OLD", "name": "央视网 · 站内检索（原登记已更正）",
     "note": "⚠️ 2026-09-19 更正：原登记为「仅返回检索表单壳、结果由 JS 异步加载」系**误判** —— "
             "第二轮实测确认 search.cctv.com 结果为**服务端渲染**且可稳定抽取，已升级为可用源 "
             "LIVE-CCTV-SEARCH（保留本条以留痕「曾误判、后更正」，不删改历史结论）"},
    {"id": "LIVE-BAIDU-NEWS", "name": "百度 · 新闻搜索（免 key）",
     "note": "实测 2026-09-19：https://www.baidu.com/s?tn=news&word= 返回 532KB，但页面内 **0 个结果 <a> 标签**，"
             "为纯 JS 壳（仅含 window.T.perf 等框架脚本）→ 无法抽取结果。"
             "⚠️ 同日二次实测**部分更正**：把参数写成 `word=` 在前 + `tn=news&rn=20` 时确实能取到结果"
             "（598KB/56 锚点、570KB/47 锚点）→ 原判定受参数组合误导，**并非恒不可用**；"
             "但同一 IP 第 3 次请求起持续被反爬拦截，故仍不纳入可用源（详见 LIVE-AGG-BAIDU 的登记）。"
             "按 R241 纪律保留原结论并附更正注，不直接改写"},
    {"id": "LIVE-TOUTIAO", "name": "头条搜索（免 key）",
     "note": "实测 2026-09-19：so.toutiao.com 确实返回结果（内嵌 JSON 含「中汽协：汽车出口连续三个月破百万辆」等），"
             "**但**为聚合检索、混入自媒体且多数条目的来源字段不可辨 → 不满足「权威来源可核验」要求，"
             "故**不纳入白名单**（宁少不假：拿来源不明的条目当依据等于把作品做成假查证）"},
    {"id": "LIVE-GMW-SEARCH", "name": "光明网 · 站内检索",
     "note": "实测 so.gmw.cn DNS 解析失败（getaddrinfo failed）→ 本机网络不可达，不可用"},
    {"id": "LIVE-GOV-OTHER-T", "name": "中国政府网 · 其他检索频道",
     "note": "实测 t=zhengce / govall / site / zcjd / zhengcejd 均返回 97—135KB 但 listVO 为空（0 条）"
             "→ 只有 t=zhengcelibrary_gw / _bm 两个文件库可用于关键词检索"},
    {"id": "LIVE-SINA-ROLL", "name": "新浪 · 滚动新闻 JSON API",
     "note": "实测 feed.mix.sina.com.cn/api/roll/get 返回 103KB，但 result.data 条数为 0"
             "（lid=2509/2510/2511/2669/1 均试）→ 接口参数已变更，不可用"},
    {"id": "LIVE-TENCENT-HUANQIU", "name": "腾讯新闻 / 环球网 · 首页",
     "note": "实测返回 18KB / 987B 空壳（无结果锚点）→ 客户端渲染，不可用"},
    {"id": "LIVE-THEPAPER", "name": "澎湃新闻 · 站内检索",
     "note": "实测 api.thepaper.cn 返回 code 99998「系统繁忙」；searchResult 为 Next.js 客户端渲染，_next/data 路由 404"},
    {"id": "LIVE-STATS", "name": "国家统计局 · 站内检索",
     "note": "实测底层为 p.so-gov.cn 政府检索 SaaS，页面仅表单壳；data.stats.gov.cn easyquery 返回 403"},
    {"id": "LIVE-BING-KEYLESS", "name": "Bing · 免 key 通用检索",
     "note": "实测三次查询（含 site: 限定）返回的 10 条结果完全相同 → site: 与关键词均被忽略，不可靠"},
    {"id": "LIVE-SOGOU", "name": "搜狗 · 免 key 通用检索", "note": "实测返回验证码页（5.6KB）"},
    {"id": "LIVE-WIKI-BBC-REUTERS", "name": "维基百科 / BBC / Reuters",
     "note": "实测 TCP 连接超时（本机网络不可达）→ 白名单未纳入"},
]


def registry() -> dict[str, Any]:
    """源登记表（界面与交付文档共用，避免两处口径不一致）。"""
    return {
        "enabled": ENABLED,
        "per_source_timeout_s": PER_SOURCE_TIMEOUT,
        "total_budget_s": TOTAL_BUDGET,
        "web_search_slot": {
            "configured": bool(WEB_SEARCH_URL and WEB_SEARCH_KEY),
            "provider": WEB_SEARCH_PROVIDER,
        },
        "available": [
            {
                "id": s.id, "name": s.name, "publisher": s.publisher, "kind": s.kind,
                "license": s.license, "terms_url": s.terms_url, "note": s.note,
            }
            for s in SOURCES if s.available
        ],
        # 不可用清单 = 独立登记的历史实测项 ∪ 登记表里被显式关闭的源。
        # 🔴 后者必须并入：否则「登记过但已关闭」的源（如百度聚合、联合国 RSS）
        # 会**同时不出现在可用与不可用两个清单里** —— 材料上等于凭空消失，
        # 而「为什么没接这条源」恰恰是最该被追问的部分（2026-09-19 实测发现该缺口）。
        "unavailable": UNAVAILABLE + [
            {"id": s.id, "name": s.name, "note": s.note}
            for s in SOURCES if not s.available
        ],
    }


# ---------------------------------------------------------------------------
# HTTP 与文本工具
# ---------------------------------------------------------------------------


@dataclass
class LiveEvidence:
    """一条实时证据：只保留「可核实」的最小信息 + 回链原文所需字段。"""

    id: str
    title: str
    text: str
    url: str
    publisher: str
    published: str
    kind: str
    license: str
    source_id: str
    source_name: str
    fetched_at: str
    # 相关性得分 = 查询原始 bigram 在该条命中数（分层输出的判定依据）：
    #   ≥ MIN_LIVE_BIGRAM_HITS → 可核实依据（strict），可作结论
    #   == RELATED_MIN_HITS    → 相关线索（related），**仅展示、不核实**
    score: int = 0

    @property
    def verified(self) -> bool:
        return self.score >= MIN_LIVE_BIGRAM_HITS

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "text": self.text, "url": self.url,
            "publisher": self.publisher, "published": self.published, "kind": self.kind,
            "license": self.license, "source_id": self.source_id, "source_name": self.source_name,
            "fetched_at": self.fetched_at, "live": True,
            # verified 是「能不能当依据」的唯一真相源：related 层一律 False，
            # 前端与材料都据此区分「已核实结论」与「仅相关线索」。
            "verified": self.verified, "related_score": self.score,
        }

    def as_evidence(self) -> Evidence:
        """转成固定语料侧同款 Evidence —— 让 llm.py / verify_citations / 前端渲染无需分叉。"""
        return Evidence(
            id=self.id,
            text=self.text,
            doc_id=self.source_id,
            doc_title=self.title,
            publisher=self.publisher,
            published=self.published,
            kind=self.kind,
            license=self.license,
            source_id=self.source_id,
            source_url=self.url,
            attribution=self.source_name,
            fetched_at=self.fetched_at,
            live=True,
        )


@dataclass
class LiveResult:
    query: str = ""
    evidences: list[LiveEvidence] = field(default_factory=list)   # strict：可核实依据
    related: list[LiveEvidence] = field(default_factory=list)     # related：仅线索，不可作依据
    sources: list[dict[str, Any]] = field(default_factory=list)   # 每源 ok/hits/ms/error
    trace: list[TraceStep] = field(default_factory=list)
    offline: bool = False          # 全部源失败 / 未启用 → 界面须明示「本次未能完成联网核实」
    note: str = ""

    @property
    def hits(self) -> int:
        return len(self.evidences)

    @property
    def related_hits(self) -> int:
        return len(self.related)


def _now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


# 提问里出现这些词 = 用户在问「当下」，此时命中旧闻必须显式提示（不拿旧闻冒充实时）
TIME_SENSITIVE_WORDS = ("今天", "今日", "昨天", "明日", "明天", "本周", "上周", "最新", "近日", "近期", "现在")
_DATE_FORMS = (
    re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})"),
    re.compile(r"(20\d{2})\.(\d{1,2})\.(\d{1,2})"),
    re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日"),
    re.compile(r"(20\d{2})(\d{2})(\d{2})"),
)


def normalize_date(text: str) -> str:
    """把各种日期写法归一成 YYYY-MM-DD；无法识别返回空串（不猜测、不编造日期）。"""
    for rx in _DATE_FORMS:
        m = rx.search(text or "")
        if m:
            y, mo, d = (int(x) for x in m.groups())
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return "{:04d}-{:02d}-{:02d}".format(y, mo, d)
    return ""


def _http_get(url: str, timeout: float, headers: dict[str, str] | None = None) -> bytes:
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "*/*")
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
_WS = re.compile(r"\s+")


def strip_html(text: str) -> str:
    text = _SCRIPT.sub(" ", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    return _WS.sub(" ", text).strip()


def clip(text: str, limit: int = SNIPPET_LIMIT) -> str:
    text = _WS.sub(" ", (text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def query_bigrams(query: str) -> list[str]:
    """查询的「原始词」—— 与固定语料侧同口径：中文 bigram + ASCII 词，扣除停用词。"""
    out: list[str] = []
    for tok in tokenize(query):
        if len(tok) < 2 or tok in STOPWORDS or tok in WEAK_BIGRAMS:
            continue
        if tok not in out:
            out.append(tok)
    return out


# 口语问句里的填充词：它们不承载检索语义，却是让「宽松匹配」型站内检索失准的主因
# （实测：中国政府网对「加装电梯需要什么手续？」只返回 2 条且全不相关，
#   去掉「需要 / 什么」后拿到的长片段「加装电梯」返回 189 条）。
QUESTION_FILLER = (
    "什么", "怎么", "怎样", "如何", "多少", "为什么", "为何", "是否", "哪些", "哪个",
    "请问", "介绍一下", "介绍", "有没有", "需要", "告诉", "知道", "关于", "相关",
    "的", "了", "吗", "呢", "吧", "啊", "是", "我", "你", "想", "请",
)
_QUERY_PUNCT = re.compile(r"[？?！!。，,、；;：:\s～~「」【】（）()\"']+")


def query_variants(query: str) -> list[str]:
    """为「宽松匹配」型站内检索生成查询变体（**只扩召回，不放松判据**）。

    背景（2026-09-19 标定，先测两侧边界再定机制，不是调参）：
      中国政府网对「加装电梯需要什么手续？」只返回 2 条（且全不相关），
      对「加装电梯」返回 189 条、对「加装电梯手续」返回 127 条。
      → 说明该接口按整串做宽松匹配，口语填充词会把真正相关的条目挤出候选池。
    变体策略（可解释、无分词依赖）：
      v1 原问句（去标点）
      v2 去掉口语填充词后的串
      v3 **被填充词切开后最长的那一段**（本例即「加装电梯」）
    注意：变体只用于**取回候选**；「有无依据」的判定始终用**原始查询**的 bigram
    （与固定语料侧同口径）—— 扩召回不等于松闸门。
    """
    base = _QUERY_PUNCT.sub("", query or "")
    variants: list[str] = []
    if base:
        variants.append(base)

    stripped = base
    for w in QUESTION_FILLER:
        stripped = stripped.replace(w, "")
    stripped = stripped.strip()
    if len(stripped) >= 2 and stripped not in variants:
        variants.append(stripped)

    # 最长连续段：把填充词当分隔符切开，取最长的一片
    pieces = [base]
    for w in QUESTION_FILLER:
        nxt: list[str] = []
        for p in pieces:
            nxt.extend(p.split(w))
        pieces = nxt
    pieces = [p.strip() for p in pieces if len(p.strip()) >= 2]
    if pieces:
        longest = max(pieces, key=len)
        if longest not in variants:
            variants.append(longest)
    return variants


def relevance(query_grams: list[str], *fields: str) -> tuple[int, list[str]]:
    """相关性 = 查询原始 bigram 在文本中的命中数（与「无依据即拒答」同一口径）。"""
    hay = " ".join(f or "" for f in fields)
    hit = [g for g in query_grams if g in hay]
    return len(hit), hit


def fetch_article_text(url: str, timeout: float = ARTICLE_TIMEOUT) -> tuple[str, str]:
    """抽取原文正文前段（用于生成真实摘录）。返回 (正文, 标题)。失败返回 ("", "")。"""
    try:
        if not url.lower().startswith(("http://", "https://")):
            return "", ""
        raw = _http_get(url, timeout)
        page = _decode(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return "", ""
    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    if m:
        title = strip_html(m.group(1))
    body = strip_html(page)
    return body, title


# ---------------------------------------------------------------------------
# 各源适配器：统一签名 (query, grams, spec) -> list[LiveEvidence]
# ---------------------------------------------------------------------------


def _mk(spec: SourceSpec, idx: int, title: str, text: str, url: str,
        published: str, fetched: str, score: int = 0, publisher: str = "") -> LiveEvidence:
    """组装一条实时候选。

    score = 查询原始 bigram 的命中数，是分层输出的唯一依据：
    适配器只负责「候选捞全」（阈值放到 RELATED_MIN_HITS），
    分层（strict / related）统一由 search_live 判定，避免各源各写一套口径。

    publisher：默认取源登记表的发布方；聚合检索通道必须**逐条**传入
    「由原文域名推出的来源名」（同一条聚合结果来自不同媒体，共用一个源名会失真）。
    """
    return LiveEvidence(
        id="live-{}-{:02d}".format(spec.id.replace("LIVE-", "").lower(), idx),
        title=clip(title, 120),
        text=clip(text),
        url=url,
        publisher=publisher or spec.publisher,
        published=published or "日期未标注",
        kind=spec.kind,
        license=spec.license,
        source_id=spec.id,
        source_name=spec.name,
        fetched_at=fetched,
        score=score,
    )


def _gov_fetch(spec: SourceSpec, q: str) -> list[dict[str, Any]]:
    """单次调用中国政府网检索接口，返回原始条目列表（含 code=1001 → 空集语义）。"""
    url = ("https://sousuo.www.gov.cn/search-gov/data?t={}&q={}&sort=score&sortType=1"
           "&searchfield=title:content&p=1&n=50").format(spec.params["t"], urllib.parse.quote(q))
    raw = _http_get(url, PER_SOURCE_TIMEOUT, {"Referer": "https://www.gov.cn/"})
    data = json.loads(_decode(raw))
    code = data.get("code")
    # 实测：接口对「过短 / 纯口语」的查询返回 code=1001 且不带 listVO ——
    # 这是「该查询未产生结果」，不是故障，因此返回空集而不是抛错（否则会被误报成源失效）。
    if code == 1001:
        return []
    if code != 200:
        raise ValueError("接口返回 code={}".format(code))
    return [x for x in (((data.get("searchVO") or {}).get("listVO")) or []) if isinstance(x, dict)]


def _gov_policy(query: str, grams: list[str], spec: SourceSpec) -> list[LiveEvidence]:
    """中国政府网政策文件库（真关键词检索）。

    为什么发多个查询变体：该接口是宽松匹配，口语问句会把相关条目挤出候选池
    （实测「加装电梯需要什么手续？」→ 2 条且无关；「加装电梯」→ 189 条）。
    变体只是把候选池找回来，**是否算「有依据」仍由原始查询的 bigram 判定**。
    """
    fetched = _now()
    out: list[LiveEvidence] = []
    seen: set[str] = set()
    for variant in query_variants(query):
        for it in _gov_fetch(spec, variant):
            title = strip_html(str(it.get("title") or ""))
            summary = strip_html(str(it.get("summary") or ""))
            score, _ = relevance(grams, title, summary)
            if score < RELATED_MIN_HITS:
                continue
            url = str(it.get("url") or "")
            key = url or title
            if key in seen:
                continue
            seen.add(key)
            # 文号与发文机关是政策文件最可核实的锚点，必须带进证据
            meta = " ｜ ".join(x for x in [
                str(it.get("pcode") or "").strip(),
                str(it.get("puborg") or "").strip(),
                str(it.get("childtype") or "").replace("\\", " / ").strip(),
            ] if x)
            text = (meta + " ｜ " if meta else "") + summary
            out.append(_mk(spec, len(out) + 1, title, text, url,
                           str(it.get("pubtimeStr") or ""), fetched, score))
        # 已有足够**可核实**命中就收手：少发一次请求 = 少一分超时与被限流的风险。
        # 注意这里数的是 strict 条数 —— 若把 related 也算进去会提前收手，反而丢掉真依据。
        if len([e for e in out if e.score >= MIN_LIVE_BIGRAM_HITS]) >= 3:
            break
    # 站内检索按 score 排序但会混入弱相关条目 → 按我方命中数重排（可解释、可审计）
    out.sort(key=lambda e: relevance(grams, e.title, e.text)[0], reverse=True)
    return out


_ANCHOR = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_NEWS_PATH = re.compile(r"/20\d{2}[-/]?\d{2}[-/]?\d{2}/", re.I)
# 滚动页用的宽松日期形态（年+月即可）——见 _roll_pool 文档字符串的实测理由
_ROLL_DATE = re.compile(r"20\d{2}[-/]?\d{2}", re.I)
_POOL_SIZE: dict[str, int] = {}   # 上一轮各池实际抽到的条目数（供 trace 如实报出「在多少条里找的」）
_CJK_SPACE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")
_CCTV_DATE = re.compile(r"20\d{2}-\d{2}-\d{2}")
# 央视网原文地址里嵌了发布日期（/2019/04/29/ 或 /2026/09/14/）——
# 页面上日期可能不在标题邻近范围内（实测有 4/6 条抽不到 → 界面显示「日期未标注」，
# 而「实时核证」最要紧的恰恰是时效）。故用 URL 兜底，保证每条都有日期可核验。
_URL_DATE = re.compile(r"/(20\d{2})/(\d{2})/(\d{2})/")


def _abs_url(base: str, href: str) -> str:
    """相对链接 → 绝对链接。

    2026-09-19 修正：原实现只处理 `//` 与 `/` 开头，**普通相对路径（如 `a.html`）
    会被原样返回**，随后又因 `startswith("http")` 判假被静默丢弃 ——
    等于「页面上真实存在的条目被无声跳过」。改用 urljoin 统一解析（幂等：已是绝对链接则原样返回）。
    """
    if href.startswith("//"):
        return "https:" + href
    if href.startswith(("http://", "https://")):
        return href
    return urllib.parse.urljoin(base, href)


def _pool_candidates(spec: SourceSpec, grams: list[str], fetched: str,
                     pages: list[tuple[str, str]], date_rx: re.Pattern[str]) -> tuple[list[LiveEvidence], int]:
    """从若干「服务端渲染的列表页」里抽候选条目（strict 与 related 一起捞）。

    诚实边界：检索范围是**这些列表页上的条目**，不是全站历史 ——
    因此池大小必须如实报出，让「在多少条里找的」可被追问与追责。
    """
    out: list[LiveEvidence] = []
    seen: set[str] = set()
    pool = 0
    for base, page in pages:
        for href, inner in _ANCHOR.findall(page):
            title = strip_html(inner)
            href = _abs_url(base, href)
            if not href.startswith("http") or href in seen:
                continue
            if len(title) < 10 or not date_rx.search(href):
                continue
            seen.add(href)
            pool += 1
            score, _ = relevance(grams, title)
            if score < RELATED_MIN_HITS:
                continue
            out.append(_mk(spec, len(out) + 1, title, title, href, "", fetched, score))
    return out, pool


def _list_page_pool(query: str, grams: list[str], spec: SourceSpec) -> list[LiveEvidence]:
    """单页列表（各媒体首页）→ 抽最新条目 → 关键词过滤。

    与 _roll_pool 的区别：首页只用「带日期的文章路径」做收窄（噪声更低），
    滚动页则允许更宽松的日期形态（见 LIVE_ROLL_DATE 注释）。
    """
    base = spec.params["url"]
    page = _decode(_http_get(base, PER_SOURCE_TIMEOUT))
    out, pool = _pool_candidates(spec, grams, _now(), [(base, page)], _NEWS_PATH)
    _POOL_SIZE[spec.id] = pool
    return out


def _roll_pool(query: str, grams: list[str], spec: SourceSpec) -> list[LiveEvidence]:
    """滚动新闻列表页池（可翻页）→ 关键词过滤。

    为什么要它：各媒体**站内检索**大多不可用（实测详见 UNAVAILABLE），
    首页池又只有几十条 → 对任意问题几乎必然 0 命中，这是「拒答率高」的根因之一。
    滚动页每页 130—150 条且支持翻页，把可检索面从「几十条」提到「数百条」。

    日期形态放宽理由：youth.cn 的链接形如 /jsxw/202609/t20260918_xxx.htm，
    用严格的「/YYYY/MM/DD/」会整站抽不到（实测 0 条）→ 改用 LIVE_ROLL_DATE（年+月即收）。
    放宽只影响**候选池大小**（如实报出），不影响相关性判定口径。
    """
    tpl = spec.params["url"]
    pages_n = int(spec.params.get("pages", 1))
    bases_pages: list[tuple[str, str]] = []
    for i in range(1, pages_n + 1):
        url = tpl.format(n=i) if "{n}" in tpl else tpl
        try:
            bases_pages.append((url, _decode(_http_get(url, PER_SOURCE_TIMEOUT))))
        except Exception:  # noqa: BLE001 — 单页失败不拖垮该源：有其他页可用即继续
            continue
    if not bases_pages:
        raise OSError("滚动页全部请求失败")
    out, pool = _pool_candidates(spec, grams, _now(), bases_pages, _ROLL_DATE)
    _POOL_SIZE[spec.id] = pool
    return out


_ITEM = re.compile(r"<item[\s>].*?</item>", re.S | re.I)
_CDATA = re.compile(r"(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?$", re.S)


def _rss_pool(query: str, grams: list[str], spec: SourceSpec) -> list[LiveEvidence]:
    """RSS 最新条目池 → 关键词过滤（覆盖「最新动态」面）。"""
    xml = _decode(_http_get(spec.params["url"], PER_SOURCE_TIMEOUT))
    fetched = _now()
    out: list[LiveEvidence] = []
    for block in _ITEM.findall(xml):
        def tag(name: str) -> str:
            m = re.search(r"<{}>(.*?)</{}>".format(name, name), block, re.S | re.I)
            return _CDATA.match(m.group(1).strip()).group(1).strip() if m else ""

        title, link, pub = tag("title"), tag("link"), tag("pubDate")
        desc = tag("description")
        title = strip_html(title)
        if not title:
            continue
        score, _ = relevance(grams, title, strip_html(desc))
        if score < RELATED_MIN_HITS:
            continue
        out.append(_mk(spec, len(out) + 1, title, title, link, pub[:22], fetched, score))
    return out


def _who_news(query: str, grams: list[str], spec: SourceSpec) -> list[LiveEvidence]:
    """WHO 官方新闻 JSON 接口 → 标题过滤。"""
    raw = _http_get("https://www.who.int/api/news/newsitems?top=50", PER_SOURCE_TIMEOUT)
    data = json.loads(_decode(raw))
    items = data.get("value") if isinstance(data, dict) else data
    fetched = _now()
    out: list[LiveEvidence] = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        title = strip_html(str(it.get("Title") or ""))
        if not title:
            continue
        # WHO 为英文源：查询为中文时 bigram 必然不命中 → 该源自然返回 0 条（不硬凑）
        score, _ = relevance(grams, title, str(it.get("Summary") or ""))
        if score < RELATED_MIN_HITS:
            continue
        url = str(it.get("ItemDefaultUrl") or it.get("Url") or "")
        if url and not url.startswith("http"):
            url = "https://www.who.int/news/item/" + url.lstrip("/")
        out.append(_mk(spec, len(out) + 1, title, title, url,
                       str(it.get("PublicationDateAndTime") or "")[:10], fetched, score))
    return out


def _web_search(query: str, grams: list[str], spec: SourceSpec) -> list[LiveEvidence]:
    """通用检索插槽（可选）。未配置 → 返回空并如实标注，不静默假装。"""
    if not (WEB_SEARCH_URL and WEB_SEARCH_KEY):
        return []
    payload = json.dumps({"query": query, "max_results": 8}).encode("utf-8")
    req = urllib.request.Request(WEB_SEARCH_URL, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + WEB_SEARCH_KEY)
    req.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(req, timeout=PER_SOURCE_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    items = data.get("results") or data.get("value") or data.get("data") or []
    fetched = _now()
    out: list[LiveEvidence] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = strip_html(str(it.get("title") or ""))
        link = str(it.get("url") or it.get("link") or "")
        snippet = strip_html(str(it.get("content") or it.get("snippet") or ""))
        score, _ = relevance(grams, title, snippet)
        if score < RELATED_MIN_HITS:
            continue
        out.append(_mk(spec, len(out) + 1, title, snippet or title, link, "", fetched, score))
    return out


def cctv_target_url(href: str, base: str = "https://search.cctv.com/search.php") -> str:
    """把央视网检索的跳转壳还原成原文地址（纯函数，便于离线单测）。

    实测形态：href = "link_p.php?targetpage=https%3A%2F%2Fnews.cctv.com%2F2026%2F03%2F19%2FARTIxxx.shtml"
    若不还原，读者点开得到的是搜索页而非原文 —— 「点回原文」这条承诺就断了。
    """
    m = re.search(r"targetpage=([^&\"]+)", href or "")
    if m:
        target = urllib.parse.unquote(m.group(1))
        if target.startswith("http"):
            return target
    return _abs_url(base, href or "")


def _cctv_search(query: str, grams: list[str], spec: SourceSpec) -> list[LiveEvidence]:
    """央视网·站内检索（**服务端渲染**的真关键词检索）。

    为什么重要：此前「中央媒体站内检索不可得」是**误判**（2026-09-19 实测更正），
    央视网检索可用意味着非政策类问题（企业、行业、国际）首次有了权威可回链的来源。

    两个必须处理的细节（实测得出，不是猜测）：
      ① 结果链接是跳转壳 `link_p.php?targetpage=<urlencoded 原文地址>`
         → 必须解码出真实地址再回链，否则读者点开是搜索页而非原文；
      ② 标题被检索词高亮 `<em>` 切开 → strip_html 后会在中文之间留下空格，
         需去掉「中日韩字符之间的空白」再入库（否则同一标题在界面上是断开的）。
    """
    fetched = _now()
    out: list[LiveEvidence] = []
    seen: set[str] = set()
    max_variants = int(spec.params.get("max_variants", 2))
    pages = int(spec.params.get("pages", 1))
    search_base = "https://search.cctv.com/search.php"
    for variant in query_variants(query)[:max_variants]:
        for page_no in range(1, pages + 1):
            url = "{}?qtext={}&type=web&page={}".format(
                search_base, urllib.parse.quote(variant), page_no)
            page = _decode(_http_get(url, PER_SOURCE_TIMEOUT, {"Referer": "https://search.cctv.com/"}))
            for m in re.finditer(r'<a\s[^>]*href="([^"]*targetpage=[^"]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
                href, inner = m.group(1), m.group(2)
                title = _CJK_SPACE.sub("", strip_html(inner))
                if len(title) < 8:
                    continue
                link = cctv_target_url(href, search_base)
                if not link.startswith("http") or link in seen:
                    continue
                seen.add(link)
                # 日期优先取结果块内的时间串；抽不到时用原文 URL 里的日期兜底
                # （实时核证最要紧的是时效，宁可显示 URL 推断的日期也不要「日期未标注」）
                dm = _CCTV_DATE.search(page[m.end(): m.end() + 800])
                um = _URL_DATE.search(link)
                published = dm.group(0) if dm else ("{}-{}-{}".format(*um.groups()) if um else "")
                score, _ = relevance(grams, title)
                if score < RELATED_MIN_HITS:
                    continue
                out.append(_mk(spec, len(out) + 1, title, title, link, published, fetched, score))
        # 已有足够可核实命中即换下一条变体也省了：少发请求 = 少一分被限流风险
        if len([e for e in out if e.score >= MIN_LIVE_BIGRAM_HITS]) >= 4:
            break
    out.sort(key=lambda e: e.score, reverse=True)
    return out


def _agg_search(query: str, grams: list[str], spec: SourceSpec) -> list[LiveEvidence]:
    """通用聚合检索（360 资讯 / 百度资讯）→ **只保留权威域白名单内的结果**。

    设计要点（逐条由实测驱动）：
      ① 白名单在**抓取阶段**完成过滤：域不在名单内的一律丢弃（自媒体 / 财经号 / 百科 /
         视频聚合）—— 「权威」这件事由代码保证，而不是靠事后说明；
      ② 结果链接必须是**绝对原文直链**，否则丢弃（跳转壳 /link?url= 无法在抓取阶段核验目标域，
         拿它当证据等于把「来源可核验」这条承诺交出去）；
      ③ 时间优先取结果块里的时间串，缺失时用 URL 内嵌日期兜底（实时核证最要紧的是时效，
         宁可显式标注也不留空）；
      ④ 单源失败绝不外抛：由 search_live 统一登记为「本次未取得结果」，不拖垮整轮问答。
    """
    engine = str(spec.params.get("engine") or "so360")
    tpl, referer = AGG_ENGINES[engine]
    fetched = _now()
    out: list[LiveEvidence] = []
    seen: set[str] = set()
    for variant in query_variants(query)[: int(spec.params.get("max_variants", 2))]:
        url = tpl.format(q=urllib.parse.quote(variant))
        page = _decode(_http_get(url, PER_SOURCE_TIMEOUT, {"Referer": referer}))
        for link, title, summary, site, when in _agg_items(engine, page):
            dom = auth_domain(link)
            if not dom:
                continue                       # 非权威域 → 不进入证据池
            if link in seen:
                continue
            seen.add(link)
            text = summary or title
            score, _hit = relevance(grams, title, text)
            if score < RELATED_MIN_HITS:
                continue
            um = _URL_DATE.search(link)
            published = when or ("{}-{}-{}".format(*um.groups()) if um else "")
            out.append(_mk(spec, len(out) + 1, title, text, link, published, fetched, score,
                           publisher=AUTH_DOMAIN_NAMES.get(dom, site or dom)))
        # 已有足够**可核实**命中就收手：少发一次请求 = 少一分被聚合站限流的风险
        if len([e for e in out if e.score >= MIN_LIVE_BIGRAM_HITS]) >= 4:
            break
    out.sort(key=lambda e: e.score, reverse=True)
    return out


ADAPTERS: dict[str, Callable[[str, list[str], SourceSpec], list[LiveEvidence]]] = {
    "gov_policy": _gov_policy,
    "cctv_search": _cctv_search,
    "agg_search": _agg_search,
    "list_pool": _list_page_pool,
    "roll_pool": _roll_pool,
    "rss_pool": _rss_pool,
    "who_news": _who_news,
    "web_search": _web_search,
}


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def search_live(query: str, top_k: int = MAX_LIVE_EVIDENCE) -> LiveResult:
    """并发向全部可用权威源发起实时检索，返回去重、排序后的证据与逐源状态。"""
    result = LiveResult(query=query)
    if not ENABLED:
        result.offline = True
        result.note = "实时检索已被 LIVE=0 关闭 → 本次仅使用离线语料"
        result.trace.append(TraceStep("实时核证", result.note, 0.0))
        return result

    grams = query_bigrams(query)
    if not grams:
        result.offline = True
        result.note = "查询中没有可用于检索的实词（过短或全为疑问词）→ 未发起联网检索"
        result.trace.append(TraceStep("实时核证", result.note, 0.0))
        return result

    active = [s for s in SOURCES if s.available]
    if not active:
        result.offline = True
        result.note = "没有可用的实时源（白名单全部未启用 / 通用检索插槽未配置）"
        result.trace.append(TraceStep("实时核证", result.note, 0.0))
        return result

    t0 = time.perf_counter()
    collected: list[LiveEvidence] = []   # strict：可核实依据（能进结论）
    loose: list[LiveEvidence] = []       # related：相关线索（仅展示，不作依据）
    _POOL_SIZE.clear()   # 池大小为「本次检索」的观测值，跨请求必须重置
    deadline = t0 + TOTAL_BUDGET

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(active)) as pool:
        futures = {
            pool.submit(ADAPTERS[s.adapter], query, grams, s): s
            for s in active if s.adapter in ADAPTERS
        }
        for fut in concurrent.futures.as_completed(futures, timeout=max(1.0, deadline - time.perf_counter())):
            spec = futures[fut]
            ms = round((time.perf_counter() - t0) * 1000, 1)
            try:
                evs = fut.result()
                # 分层在此统一完成（适配器只负责把候选捞全）：
                # 逐源分别报出「可核实 N 条 / 仅相关 M 条」，避免把线索算成命中。
                strict = [e for e in evs if e.verified]
                rel = [e for e in evs if not e.verified]
                collected.extend(strict)
                loose.extend(rel)
                result.sources.append({
                    "id": spec.id, "name": spec.name, "ok": True,
                    "hits": len(strict), "related": len(rel), "ms": ms, "error": None,
                })
            except concurrent.futures.TimeoutError:
                result.sources.append({"id": spec.id, "name": spec.name, "ok": False,
                                       "hits": 0, "related": 0, "ms": ms, "error": "超时"})
            except Exception as exc:  # noqa: BLE001 — 单源失败绝不外抛
                result.sources.append({"id": spec.id, "name": spec.name, "ok": False, "hits": 0,
                                       "related": 0, "ms": ms,
                                       "error": "{}: {}".format(type(exc).__name__, str(exc)[:80])})

    # 到点收口：未返回的源显式登记为「预算内未返回」，不假装查询过
    for spec in active:
        if not any(s["id"] == spec.id for s in result.sources):
            result.sources.append({"id": spec.id, "name": spec.name, "ok": False, "hits": 0,
                                   "related": 0,
                                   "ms": round((time.perf_counter() - t0) * 1000, 1),
                                   "error": "总预算 {:.0f}s 内未返回".format(TOTAL_BUDGET)})

    def _key(ev: LiveEvidence) -> str:
        return ev.url or (ev.source_id + "|" + ev.title)

    # 去重（同一 URL 只留一条）并排序；排序用适配器算出的 score（与判定同源，可审计）
    uniq: dict[str, LiveEvidence] = {}
    for ev in collected:
        uniq.setdefault(_key(ev), ev)
    result.evidences = sorted(uniq.values(), key=lambda e: e.score, reverse=True)[:top_k]

    # 相关线索：去重后剔除「已作为可核实依据出现」的同一条，避免同一内容既当依据又当线索
    strict_keys = {_key(e) for e in result.evidences}
    rel_uniq: dict[str, LiveEvidence] = {}
    for ev in loose:
        if _key(ev) not in strict_keys:
            rel_uniq.setdefault(_key(ev), ev)
    result.related = sorted(rel_uniq.values(), key=lambda e: e.score, reverse=True)[:MAX_RELATED]

    # 正文抽取：给「只有标题」的证据补上原文首段（更接近「查出内容」而非「只给个标题」）
    if FETCH_ARTICLE and result.evidences:
        thin = [e for e in result.evidences if e.text.strip() == e.title.strip()][:ARTICLE_FETCH_LIMIT]
        if thin and time.perf_counter() < deadline:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(thin)) as pool:
                fetched = list(pool.map(lambda e: (e, fetch_article_text(e.url)), thin))
            for ev, (body, _title) in fetched:
                if body:
                    # 从正文里截取包含查询词的那一段，避免摘录变成页面导航垃圾
                    pos = -1
                    for g in grams:
                        pos = body.find(g)
                        if pos >= 0:
                            break
                    snippet = body[max(0, pos - 60): pos + SNIPPET_LIMIT] if pos >= 0 else body[:SNIPPET_LIMIT]
                    ev.text = clip(snippet)
                    ev.kind = ev.kind + "+原文摘录"

    ok_sources = [s for s in result.sources if s["ok"]]
    strict_sources = [s for s in result.sources if s["hits"]]
    rel_sources = [s for s in result.sources if s.get("related")]
    # 三种结论必须措辞不同：没查到 / 只有线索 / 有依据 —— 「只有线索」不能说成「命中」
    if not ok_sources:
        result.offline = True
        result.note = "全部实时源均未返回（网络异常或接口变更）→ 本次未能完成联网核实"
    elif not strict_sources and not rel_sources:
        result.note = "已实时检索 {} 个权威源，未找到与本问题相关的公开条目".format(len(ok_sources))
    elif not strict_sources:
        result.note = "已实时检索 {} 个权威源，未找到可核实依据；另有 {} 条相关线索（未核实，仅供参考）".format(
            len(ok_sources), result.related_hits)
    else:
        result.note = "已实时检索 {} 个权威源，命中 {} 条可核实依据{}".format(
            len(ok_sources), len(result.evidences),
            "；另有 {} 条相关线索（未核实）".format(result.related_hits) if result.related_hits else "")

    # 时效提示（2026-09-19）：提问含「今天/最新/近日」这类时效词时，
    # 若命中条目全是旧闻，**必须显式说明** —— 与「不拿旧闻冒充实时」是同一条纪律。
    # 起因实测：查「今天天气怎么样」时命中的是 2019 年的天气预报，界面若不提示，
    # 读者会把它当当日天气 —— 这正是「假查证」的典型形态。
    if result.evidences and any(w in query for w in TIME_SENSITIVE_WORDS):
        dates = sorted(d for d in (normalize_date(e.published) for e in result.evidences) if d)
        if dates:
            newest = dates[-1]
            stale_after = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            if newest < stale_after:
                result.note += "；⚠️ 命中条目最新发布日为 {}，均早于 30 天前（提问含时效词，请注意属旧闻）".format(newest)
            else:
                result.note += "；命中条目最新发布日 {}".format(newest)

    def _label(s: dict[str, Any]) -> str:
        short = s["name"].split(" ·")[0]
        if not s["ok"]:
            return "{}×".format(short)
        pool = _POOL_SIZE.get(s["id"])
        # 池源如实报出「在多少条最新条目里找的」，避免让人以为做了全站检索
        extra = "＋{}线索".format(s["related"]) if s.get("related") else ""
        return "{}{}:{}{}{}".format(short, "（最新池{}条）".format(pool) if pool else "",
                                  s["hits"], extra, "" if (s["hits"] or extra) else "(未命中)")

    result.trace.append(TraceStep(
        "实时核证",
        "{}（{}）".format(result.note, " / ".join(_label(s) for s in result.sources)),
        round((time.perf_counter() - t0) * 1000, 1),
    ))
    return result


# ---------------------------------------------------------------------------
# 双轨合并（离线语料 + 实时权威源）
# ---------------------------------------------------------------------------

# 合并后送入模型/界面的证据上限（实时优先）
MAX_MERGED_EVIDENCE = int(os.environ.get("LIVE_MAX_MERGED", "8"))


def merge_results(query: str, corpus_result: RetrievalResult, live: LiveResult) -> RetrievalResult:
    """把「离线语料检索」与「实时核证」两条路的结果合并成一份判定。

    拒答语义（2026-09-19 用户拍板 B 方案）：
      **只有两条路都没有依据时才拒答**；任一路命中即作答，且界面必须分别标注来源与抓取时间。

    为什么 confidence 仍只取语料侧的 BM25 置信度：
      实时源没有可与 BM25 对齐的打分口径，硬凑一个统一分数等于制造不可核验的数字。
      因此实时侧只报**可核实的事实**（查了几个源、命中几条、抓取时间），不编造置信度。
    """
    merged = RetrievalResult()
    live_evs = [e.as_evidence() for e in live.evidences]
    corpus_evs = list(corpus_result.evidence)

    merged.evidence = (live_evs + corpus_evs)[:MAX_MERGED_EVIDENCE]
    # scores 仅对语料侧有意义：实时证据用 -1 占位，序列化时按 ev.live 转成 None
    merged.scores = [-1.0] * len(live_evs) + list(corpus_result.scores)
    merged.scores = merged.scores[: len(merged.evidence)]

    grams = query_bigrams(query)
    matched: dict[str, list[str]] = {}
    for ev in live_evs:
        hit = [g for g in grams if g in (ev.doc_title + ev.text)]
        matched[ev.id] = hit[:8]
    for ev_id, terms in corpus_result.matched_terms.items():
        matched[ev_id] = terms
    merged.matched_terms = matched

    merged.confidence = corpus_result.confidence
    merged.divergences = corpus_result.divergences
    # 🔴 关键：related（相关线索）**不参与拒答判定** ——
    # 否则「有条目沾边就放行」，等于把「无依据即拒答」这个核心卖点拆掉。
    merged.refused = bool(corpus_result.refused and live.hits == 0)

    # 相关线索原样透传（verified=False，界面必须标「未核实」；不转成 Evidence、不进模型 prompt）
    merged.related = [e.public() for e in live.related]

    if merged.refused:
        merged.refuse_reason = (
            "离线语料：{}；实时核证：{}".format(corpus_result.refuse_reason, live.note)
        )
    else:
        merged.refuse_reason = ""

    merged.live = {
        "enabled": ENABLED,
        "offline": live.offline,
        "note": live.note,
        "hits": live.hits,
        "strict_hits": live.hits,
        "related_hits": live.related_hits,
        "queried": len([s for s in live.sources if s["ok"]]),
        "sources": live.sources,
        "fetched_at": (live.evidences[0].fetched_at if live.evidences
                       else (live.related[0].fetched_at if live.related else "")),
    }

    merged.trace = list(corpus_result.trace)
    merged.trace.extend(live.trace)
    merged.trace.append(TraceStep(
        "双轨合并判定",
        "离线语料：{}（置信度 {:.2f}）｜ 实时核证：可核实 {} 条 / 线索 {} 条 → {}".format(
            "命中 {} 条".format(len(corpus_evs)) if corpus_evs else "无依据",
            corpus_result.confidence, live.hits, live.related_hits,
            "拒答（两条路均无可核实依据；线索不参与判定）" if merged.refused
            else "放行（证据 {} 条，其中实时 {} 条）".format(len(merged.evidence), len(live_evs)),
        ),
        0.0,
    ))
    return merged


def build_live_graph(query: str, live: LiveResult) -> dict[str, Any]:
    """把「本次实时检索」组织成 3D 场景可用的三层图（主题 → 来源 → 证据）。

    结构与 Corpus.graph() 完全一致 —— 前端 scene.js 无需为实时数据分叉。
    离线时返回空图（前端据此显示「本次未取得实时证据」而非空白无解释）。
    """
    nodes: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []

    topic_id = "live-topic"
    nodes.append({"id": topic_id, "type": "topic", "label": clip(query, 28)})

    by_source: dict[str, list[LiveEvidence]] = {}
    for ev in live.evidences:
        by_source.setdefault(ev.source_id, []).append(ev)

    for sid, evs in by_source.items():
        sid_node = "live-src-" + sid.replace("LIVE-", "").lower()
        nodes.append({
            "id": sid_node, "type": "source", "label": evs[0].source_name,
            "publisher": evs[0].publisher, "published": evs[0].fetched_at,
            "kind": evs[0].kind, "stance": "live",
            "source_id": sid, "license": evs[0].license, "source_url": "",
            "topic": "live", "live": True,
        })
        links.append({"source": topic_id, "target": sid_node})
        for ev in evs:
            nodes.append({
                "id": ev.id, "type": "evidence", "label": clip(ev.title, 42),
                "doc_id": sid, "live": True, "url": ev.url, "fetched_at": ev.fetched_at,
            })
            links.append({"source": sid_node, "target": ev.id})

    return {
        "meta": {
            "id": "LIVE", "name": "实时权威源检索结果", "kind": "live",
            "license": "逐条见证据详情（各源条款不同，均只做短摘录与回链）",
            "badge": "实时核证", "scope_note": live.note,
            "sources": [], "query": query,
        },
        "nodes": nodes,
        "links": links,
        "stats": {
            "topics": 1,
            "sources": len(by_source),
            "evidence": len(live.evidences),
            "divergences": 0,
        },
    }


def selftest() -> int:
    """判据自检（R238 两层纪律）。

    层 a —— **纯函数隔离桩**：不联网，验证合并判定的边界（这是本轮新增的判据，必须单独测）；
    层 b —— **集成对照**：真实源上跑一组「应有命中 / 应无命中」的对照，
             证明相关性闸门没有被放宽成「一律放行」。

    对照组的必要性：只测「有命中时能放行」不能证明闸门有效 ——
    还必须证明「无依据时确实拒答」。
    """
    a_fail: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        print("  [{}] {}{}".format("✅" if cond else "❌", label, "" if cond else "  ← " + detail))
        if not cond:
            a_fail.append(label)

    print("── 层 a：纯函数隔离桩（不联网） ──")

    # 1) 两条路都无依据 → 必须拒答
    empty_corpus = RetrievalResult()
    empty_corpus.refused = True
    empty_corpus.refuse_reason = "语料无依据"
    empty_corpus.confidence = 0.0
    live_none = LiveResult(query="x", note="0 命中")
    live_none.sources = [{"id": "LIVE-T", "name": "T", "ok": True, "hits": 0, "ms": 1, "error": None}]
    r = merge_results("加装电梯", empty_corpus, live_none)
    check("双路皆空 → 拒答", r.refused is True, "refused={}".format(r.refused))
    check("双路皆空 → 证据为空", len(r.evidence) == 0)

    # 2) 语料拒答但实时命中 → 必须放行（本轮核心新判据）
    ev = LiveEvidence(id="live-t-01", title="加装电梯政策", text="加装电梯政策摘录",
                      url="https://example.invalid/1", publisher="测试源", published="2026-01-01",
                      kind="policy", license="测试", source_id="LIVE-T", source_name="测试源",
                      fetched_at="2026-09-19 10:00:00")
    live_hit = LiveResult(query="加装电梯", evidences=[ev], note="命中 1 条")
    live_hit.sources = [{"id": "LIVE-T", "name": "T", "ok": True, "hits": 1, "ms": 1, "error": None}]
    r2 = merge_results("加装电梯", empty_corpus, live_hit)
    check("语料拒答 + 实时命中 → 放行", r2.refused is False, "refused={}".format(r2.refused))
    check("实时证据带「实时抓取」标记", bool(r2.evidence and r2.evidence[0].live))
    check("实时证据携带抓取时间", bool(r2.evidence and r2.evidence[0].fetched_at))

    # 3) 语料命中 + 实时 0 命中 → 放行，且不冒充实时
    corpus_hit = RetrievalResult()
    corpus_hit.evidence = [Evidence(id="ev-x-01", text="离线证据", doc_id="d", doc_title="t",
                                    publisher="p", published="2026-01-01", kind="news", license="L")]
    corpus_hit.scores = [0.9]
    corpus_hit.confidence = 0.9
    r3 = merge_results("加装电梯", corpus_hit, live_none)
    check("语料命中 + 实时 0 → 放行", r3.refused is False)
    check("合并后实时块如实报 0 命中", r3.live.get("hits") == 0)

    # 4) 双路都有 → 实时证据排在前（实时优先）
    r4 = merge_results("加装电梯", corpus_hit, live_hit)
    check("双路齐备 → 实时证据排首位", bool(r4.evidence) and r4.evidence[0].live is True)
    check("双路齐备 → 证据数 = 1 实时 + 1 离线", len(r4.evidence) == 2, "len={}".format(len(r4.evidence)))

    # ---- 4b) 分层输出（2026-09-19 新增判据，必须单独隔离桩验证）----------------
    # 这是本轮最关键的机制：related 只展示、绝不放行 —— 必须正反两面都测。
    rel_ev = LiveEvidence(id="live-r-01", title="英伟达相关报道", text="英伟达相关报道标题",
                          url="https://example.invalid/rel", publisher="测试源", published="2026-01-01",
                          kind="news", license="测试", source_id="LIVE-R", source_name="测试源",
                          fetched_at="2026-09-19 10:00:00", score=1)
    live_only_related = LiveResult(query="英伟达最新财报", related=[rel_ev], note="仅线索")
    live_only_related.sources = [{"id": "LIVE-R", "name": "R", "ok": True, "hits": 0, "related": 1,
                                  "ms": 1, "error": None}]
    r5 = merge_results("英伟达最新财报", empty_corpus, live_only_related)
    check("【关键】只有相关线索 + 语料无依据 → 仍必须拒答（线索不得放行）",
          r5.refused is True, "refused={}".format(r5.refused))
    check("拒绝时仍透传相关线索（不隐瞒查到了什么）", len(r5.related) == 1, "len={}".format(len(r5.related)))
    check("相关线索标记 verified=False", r5.related and r5.related[0].get("verified") is False,
          str(r5.related[:1]))
    check("相关线索标签 live=True（仍属实时抓取）",
          bool(r5.related) and r5.related[0].get("live") is True)
    check("live.related_hits 如实报数", r5.live.get("related_hits") == 1, str(r5.live.get("related_hits")))

    r6 = merge_results("英伟达最新财报", corpus_hit, live_only_related)
    check("语料有依据 + 仅线索 → 放行，且线索不混入结论证据",
          r6.refused is False and len(r6.evidence) == 1, "ev={}".format(len(r6.evidence)))
    check("放行时线索仍单列在 related", len(r6.related) == 1)

    check("LiveEvidence.verified 由 score 决定（2 通过 / 1 不通过）",
          LiveEvidence(id="a", title="t", text="t", url="u", publisher="p", published="d",
                       kind="k", license="l", source_id="s", source_name="n",
                       fetched_at="f", score=2).verified is True
          and rel_ev.verified is False)

    # 央视网跳转壳还原：不还原就会「点回搜索页」而非原文（纯函数，离线可测）
    _shell = "link_p.php?targetpage=https%3A%2F%2Fnews.cctv.com%2F2026%2F03%2F19%2FARTIHZSkVkx.shtml"
    check("央视网跳转壳 → 还原出真实原文地址",
          cctv_target_url(_shell) == "https://news.cctv.com/2026/03/19/ARTIHZSkVkx.shtml",
          cctv_target_url(_shell))
    check("非跳转壳 → 回落到原 href（不误改）",
          cctv_target_url("a.shtml", "https://search.cctv.com/search.php") == "https://search.cctv.com/a.shtml")
    check("标题高亮残留空格被清除（CJK 之间不留断字）",
          _CJK_SPACE.sub("", "商务部回应 英伟达 对华芯片销售情况") == "商务部回应英伟达对华芯片销售情况")
    # 日期归一：实时核证最要紧的是时效，日期写错等于把旧闻说成新事
    check("日期归一：2026-09-14 / 2026.9.4 / 2026年9月14日 / 20260914 全部识别",
          normalize_date("2026-09-14") == "2026-09-14"
          and normalize_date("2026.9.4") == "2026-09-04"
          and normalize_date("2026年9月14日") == "2026-09-14"
          and normalize_date("20260914") == "2026-09-14")
    check("日期归一：识别不出时返回空串（不猜日期）", normalize_date("日期未标注") == "")

    # 5) 查询实词提取必须扣掉疑问词（否则「多少/怎么」会凑出假命中）
    grams = query_bigrams("今天天气怎么样？")
    check("疑问词不进入检索实词", "怎么" not in grams and "多少" not in grams, str(grams))

    # 5b) 查询变体：长口语问句必须能退化成「核心片段」去扩召回
    vs = query_variants("加装电梯需要什么手续？")
    check("变体含原问句", vs and vs[0].startswith("加装电梯"), str(vs))
    check("变体含核心片段「加装电梯」", "加装电梯" in vs, str(vs))
    check("变体含去填充词串", "加装电梯手续" in vs, str(vs))
    check("变体不含标点", all("？" not in v for v in vs), str(vs))
    check("空查询不产生变体", query_variants("") == [], str(query_variants("")))

    # 6) 相关性口径：命中数来自查询原始 bigram
    n_hit, _ = relevance(["加装", "电梯"], "关于加装电梯的通知", "")
    n_miss, _ = relevance(["加装", "电梯"], "关于碳达峰的通知", "")
    check("相关性：命中 2", n_hit == 2, "got {}".format(n_hit))
    check("相关性：无关文本命中 0", n_miss == 0, "got {}".format(n_miss))

    # 7) 实时图：空结果只有主题节点，有结果结构正确
    g0 = build_live_graph("x", live_none)
    check("空实时图 → 仅 1 个主题节点", len(g0["nodes"]) == 1 and g0["stats"]["evidence"] == 0)
    g1 = build_live_graph("加装电梯", live_hit)
    check("有结果 → 主题/来源/证据三层齐备",
          len(g1["nodes"]) == 3 and len(g1["links"]) == 2 and g1["stats"]["evidence"] == 1,
          str(g1["stats"]))

    # 8) 空查询不得发起联网检索（避免无意义请求打满超时预算）
    r8 = search_live("？？？")
    check("无实词的查询 → 不发起联网", r8.offline is True and "未发起" in r8.note, r8.note)

    print()
    print("── 层 b：集成对照（真实源） ──")
    hit_q = "加装电梯需要什么手续？"
    # ⚠️ 负样本校正注（2026-09-19，保留原结论 + 记录变更原因，不抹掉演进痕迹）：
    #   原负样本为「今天天气怎么样？」——它在**改造前**（只有政策库 + 首页池）实测 7 源全 0 命中，
    #   故当时判为「应拒答」是正确的。改造后央视网检索可用，该问实际能取回**真实的天气新闻**
    #   （如「今天华南部分地区雨势仍强…」2026-09-14），此时再要求拒答就与用户「任何问题都要
    #   实时查证权威媒体」的产品意图冲突 —— 故**变更的是负样本，不是闸门**。
    #   为继续守住「闸门没被放宽成一律放行」，负样本换成与新闻语料**无词面交集**的查询。
    miss_q = "帮我给这只猫起个名字吧"
    rh, rm = search_live(hit_q), search_live(miss_q)
    check("对照·应命中 → 命中 > 0（{} 条）".format(rh.hits), rh.hits > 0, rh.note)
    check("对照·应无命中 → 命中 == 0", rm.hits == 0, "hits={}".format(rm.hits))
    check("对照·应无命中 → 不得被静默放行（并入语料侧判定）",
          merge_results(miss_q, empty_corpus, rm).refused is True)

    # 边界留痕：把「天气类问题」这一行为变更**显式测出来**，而不是让它悄悄变掉。
    # 用户报障「拒答率太高」的诉求正是「任何问题都去查权威媒体」——
    # 因此这里期望「能取回实时条目」，并对时效作说明（不假装是当日实况）。
    rw = search_live("今天天气怎么样？")
    check("行为变更留痕·天气类问题 → 已能取回实时条目（{} 可核实 / {} 线索）".format(
        rw.hits, rw.related_hits), rw.hits + rw.related_hits > 0, rw.note)
    check("行为变更留痕·时效词提示已写入结论", ("最新发布日" in rw.note) or ("旧闻" in rw.note), rw.note)

    # 4c) 「任意问题」召回对照（2026-09-19 用户报障的直接判据）：
    #     这三条在改造前实测 7 源全 0 命中（企业 / 国际 / 行业各一），
    #     改造后必须至少能取回内容 —— 证明「不只有政策问题查得到」。
    #     判据取「strict 或 related 有其一 > 0」：不夸大成「都能给出可核实依据」，
    #     但也不允许再出现「查了 7 个源一条都没有」。
    print()
    print("── 层 c：任意问题召回对照（改造前实测全 0 命中的三条） ──")
    arbitrary = ["英伟达最新财报", "2026年诺贝尔文学奖得主", "中国新能源汽车出口数据"]
    for q in arbitrary:
        rr = search_live(q)
        total = rr.hits + rr.related_hits
        check("「{}」→ 可核实 {} 条 / 线索 {} 条（合计 {}）".format(q, rr.hits, rr.related_hits, total),
              total > 0, rr.note)
        for ev in rr.evidences[:1]:
            print("      · 依据 [{}] {} ｜ {}".format(ev.score, ev.title[:46], ev.url[:60]))
        for ev in rr.related[:1]:
            print("      · 线索 [{}] {} ｜ {}".format(ev.score, ev.title[:46], ev.url[:60]))

    print()
    print("── 层 d：聚合检索通道（白名单过滤是本轮新增判据，必须隔离桩 + 集成两层测） ──")
    # d-1 白名单匹配（纯函数）：子域必须命中；自媒体 / 百科 / 聚合站自身必须被拒
    check("白名单：子域命中（m.gmw.cn → gmw.cn）", auth_domain("https://m.gmw.cn/a.htm") == "gmw.cn")
    check("白名单：gov.cn 子域命中", auth_domain("https://sousuo.www.gov.cn/x") == "gov.cn")
    check("白名单：百家号被拒", auth_domain("https://baijiahao.baidu.com/s?id=1") is None)
    check("白名单：聚合站自身被拒（so.com）", auth_domain("https://www.so.com/s?q=x") is None)
    check("白名单：商业财经门户被拒（finance.sina）", auth_domain("https://finance.sina.com.cn/x") is None)

    # d-2 结果解析（纯函数，fixture 取自 2026-09-19 实抓到的 360 结果块结构）
    fixture = (
        '<li class="res-list" data-from="news" data-url="https://m.gmw.cn/2026-09/17/content_1.htm">'
        '<a hidefocus="true" href="https://m.gmw.cn/2026-09/17/content_1.htm" title="英伟达CEO自嘲身高">'
        '<h3 class="g-title"><div class="g-txt-inner">英伟达CEO自嘲身高</div></h3>'
        '<div class="g-figure-caption"><p class="g-ellipsis3 summary">据外媒报道，英伟达CEO称…</p>'
        '<p class="g-linkinfo"><cite class="sitename">光明网</cite>'
        '<span class="g-linkinfo-txt g-c-gray time">1天前</span></p></div></a></li>'
        '<li class="res-list" data-url="https://baijiahao.baidu.com/s?id=2">'
        '<a href="https://baijiahao.baidu.com/s?id=2" title="自媒体标题党内容">'
        '<h3 class="g-title">自媒体标题党内容</h3></a></li>'
    )
    items = _agg_items("so360", fixture)
    check("解析：抽出 2 条候选", len(items) == 2, str(len(items)))
    check("解析：标题/来源/时间齐全",
          bool(items) and items[0][1] == "英伟达CEO自嘲身高"
          and items[0][3] == "光明网" and items[0][4] == "1天前",
          str(items[0] if items else None))
    # 必测负例：白名单必须真的剔掉非权威域 —— 否则等于「没过滤」，只是看起来过滤了
    check("解析+白名单：自媒体被剔除（2 条只剩 1 条）",
          len([x for x in items if auth_domain(x[0])]) == 1,
          str([auth_domain(x[0]) for x in items]))

    # d-3 集成（真实网络）：企业类问题必须能取回权威域依据
    ra = search_live("英伟达最新财报")
    check("集成：聚合通道已参与本轮检索",
          any(s["id"].startswith("LIVE-AGG") for s in ra.sources), str([s["id"] for s in ra.sources]))
    check("集成：任意问题取回可核实依据 > 0（当前 {} 条）".format(ra.hits), ra.hits > 0, ra.note)
    agg_ev = [e for e in ra.evidences if e.source_id.startswith("LIVE-AGG")]
    check("集成：聚合证据逐条落在权威域白名单内",
          all(auth_domain(e.url) for e in agg_ev), str([e.url for e in agg_ev][:3]))
    for e in ra.evidences[:3]:
        print("      · [{}] {} ｜ {} ｜ {}".format(e.score, e.publisher, e.title[:38], e.url[:56]))

    print()
    if a_fail:
        print("结果：{} 项失败 → {}".format(len(a_fail), " ｜ ".join(a_fail)))
        return 1
    print("结果：全部通过（层 a 隔离桩 + 层 b 集成对照 + 层 c 任意问题召回 + 层 d 聚合通道白名单）")
    return 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(selftest())

    print("实时源登记表")
    print("=" * 78)
    reg = registry()
    for s in reg["available"]:
        print("  ✅ {:<22} {}".format(s["id"], s["name"]))
    for s in reg["unavailable"]:
        print("  ❌ {:<22} {}".format(s["id"], s["name"]))
        print("      {}".format(s["note"]))
    print()

    queries = sys.argv[1:] or ["加装电梯业主需要出多少钱？", "今天天气怎么样？"]
    for q in queries:
        r = search_live(q)
        print("提问：{}".format(q))
        print("  结论：{}".format(r.note))
        for s in r.sources:
            print("    · {:<34} hits={:<3} {}{}".format(
                s["name"][:34], s["hits"], "OK" if s["ok"] else "FAIL",
                "" if s["ok"] else " " + str(s["error"])))
        for ev in r.evidences:
            print("    → [{}] {} | {} | {}".format(ev.id, ev.title[:52], ev.published, ev.url[:68]))
            print("      {}".format(ev.text[:150]))
        print()
