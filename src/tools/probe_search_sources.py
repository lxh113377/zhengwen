"""证闻 · 候选检索入口实测探针（只读，不改任何配置）。

为什么需要：用户要求「任何问题都实时查权威媒体」，而当前真正的关键词检索只有 3 个源
（中国政府网两个文件库 + 央视网站内检索）。扩源不能靠猜 —— 必须逐个实测：
返回多少字节、能否抽出结果锚点、抽到的是不是当前查询真的命中的条目。

判定纪律（防止把「页面壳」当成「可用检索」）：
  · 看点 1：HTTP 状态与字节数（过小 = 空壳 / 验证码页）
  · 看点 2：结果锚点数量（正则抽 href+标题，长度 ≥ 10）
  · 看点 3：**抽到的标题里是否含查询词或其片段**（决定性判据）——
            「有 200 个锚点但都不含关键词」= 导航菜单，不是检索结果。

用法：
  python src/tools/probe_search_sources.py                 # 默认查「英伟达」
  python src/tools/probe_search_sources.py --q 新能源汽车
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html as html_mod
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 8.0

_SCRIPT = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_ANCHOR = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)

# (id, 名称, URL 模板)  —— {q} 会被 urlencode 替换
CANDIDATES: list[tuple[str, str, str]] = [
    ("CHINANEWS-SOU", "中国新闻网 · 站内检索", "https://sou.chinanews.com.cn/search.do?q={q}"),
    ("CHINASO", "中国搜索（新华社旗下）", "https://www.chinaso.com/newssearch?q={q}"),
    ("CHINASO2", "中国搜索 · 新闻频道", "https://so.chinaso.com/news?q={q}"),
    ("CNR", "央广网 · 站内检索", "https://search.cnr.cn/search?q={q}"),
    ("CHINA-COM", "中国网 · 站内检索", "http://search.china.com.cn/s?q={q}"),
    ("HUANQIU", "环球网 · 站内检索", "https://search.huanqiu.com/?q={q}"),
    ("SINA", "新浪 · 新闻搜索", "https://search.sina.com.cn/?q={q}&c=news&range=all"),
    ("NETEASE", "网易 · 新闻搜索", "https://so.news.163.com/search?q={q}"),
    ("JIEMIAN", "界面新闻 · 站内检索", "https://www.jiemian.com/search.html?keyword={q}"),
    ("GUANCHA", "观察者网 · 站内检索", "https://www.guancha.cn/search?q={q}"),
    ("QSTHEORY", "求是网 · 站内检索", "http://www.qstheory.cn/so/search.html?q={q}"),
    ("CHINADAILY-CN", "中国日报中文网 · 站内检索", "https://cn.chinadaily.com.cn/search?q={q}"),
    ("SO360", "360 · 新闻搜索", "https://news.so.com/ns?q={q}"),
    ("BINGNEWS", "必应 · 新闻搜索", "https://cn.bing.com/news/search?q={q}"),
    ("IFENG", "凤凰网 · 站内检索", "https://search.ifeng.com/sofeng/search?q={q}"),
    ("YOUTH-SOU", "中国青年网 · 站内检索", "https://search.youth.cn/?q={q}"),
    ("CE-SOU", "中国经济网 · 站内检索", "http://search.ce.cn/search?q={q}"),
    ("81", "中国军网 · 站内检索", "http://www.81.cn/search/?q={q}"),
    ("PIYAO", "中国互联网联合辟谣平台", "https://www.piyao.org.cn/search?q={q}"),
    ("WX-CN", "光明网 · 站内检索（备选域名）", "https://www.gmw.cn/search?q={q}"),
    ("CNN-SOU2", "中国新闻网 · 检索（备选参数）", "https://sou.chinanews.com.cn/search.do?q={q}&ps=20"),
    ("PEOPLE-API", "人民网 · 检索接口", "http://search.people.cn/api-search/front/search?key={q}"),
    ("CHINADAILY2", "中国日报中文网 · 检索（备选）", "https://cn.chinadaily.com.cn/search_1.html?q={q}"),
    ("HUANQIU2", "环球网 · 检索（备选路径）", "https://www.huanqiu.com/search?q={q}"),
    ("CNR2", "央广网 · 检索（备选路径）", "http://www.cnr.cn/search/?q={q}"),
    ("CCTV-NEWS-SOU", "央视新闻 · 站内检索", "https://search.cctv.com/search.php?qtext={q}&type=web&page=1"),
    ("XINHUA-SO2", "新华网 · 检索（getNews）", "https://so.news.cn/getNews?keyword={q}&curPage=1&sortField=0&searchFields=1&lang=cn"),
    ("BAIDU-NEWS2", "百度 · 资讯搜索（word+tn=news）", "https://www.baidu.com/s?word={q}&tn=news&rn=20"),
    ("SOHU", "搜狐 · 新闻搜索", "https://search.sohu.com/?keyword={q}&type=news"),
    ("SOHU2", "搜狐 · 新闻搜索（备选）", "https://search.sohu.com/news?keyword={q}"),
    ("GUANCHA2", "观察者网 · 检索（备选参数）", "https://www.guancha.cn/search?q={q}&type=news"),
    ("IFENG2", "凤凰网 · 检索（备选参数）", "https://search.ifeng.com/sofeng/search?q={q}&c=1"),
    ("YICAI", "第一财经 · 站内检索", "https://www.yicai.com/search?keys={q}"),
    ("CLS", "财联社 · 站内检索", "https://www.cls.cn/searchPage?keyword={q}&type=telegram"),
    ("STCN", "证券时报 · 站内检索", "https://www.stcn.com/search/index.html?keyword={q}"),
    ("71", "中国新闻网 · 频道检索", "https://www.chinanews.com.cn/search/?q={q}"),
]

_HINT = re.compile(r"英伟达|新能源汽车|加装电梯")


def _strip(text: str) -> str:
    text = _SCRIPT.sub(" ", text)
    text = _TAG.sub(" ", text)
    text = html_mod.unescape(text)
    return _WS.sub(" ", text).strip()


def probe(item: tuple[str, str, str], query: str) -> dict:
    sid, name, tpl = item
    url = tpl.format(q=urllib.parse.quote(query))
    out: dict = {"id": sid, "name": name, "url": url}
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            out["status"] = resp.status
        page = raw.decode("utf-8", "ignore") if b"charset=gb" not in raw[:2048].lower() else raw.decode("gb18030", "ignore")
    except Exception as exc:  # noqa: BLE001
        out["error"] = "{}: {}".format(type(exc).__name__, str(exc)[:90])
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return out

    out["bytes"] = len(raw)
    anchors = []
    for href, inner in _ANCHOR.findall(page):
        title = _strip(inner)
        if len(title) < 10:
            continue
        anchors.append((href, title))
    out["anchors"] = len(anchors)
    hit = [t for _h, t in anchors if _HINT.search(t) or query in t]
    out["hits_in_titles"] = len(hit)
    out["sample_hits"] = hit[:3]
    out["sample_any"] = [t[:40] for _h, t in anchors[:3]]
    out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


def dump(sid: str, query: str, grep: str = "") -> int:
    """把某个源的结果页结构打出来（看清标题 / 原文链接 / 来源 / 时间各自在哪个标签里）。

    为什么必须看结构而不是只看命中数：要「点回原文」就必须拿到**原始发布方**的 URL，
    聚合检索页常有跳转壳（`/link?url=`）或站内详情页 —— 不看结构就会写出一个
    「点开是搜索页」的假回链（央视网那次的同族教训）。
    """
    item = next((c for c in CANDIDATES if c[0] == sid), None)
    if not item:
        print("未知 id：{}（可选：{}）".format(sid, "、".join(c[0] for c in CANDIDATES)))
        return 2
    url = item[2].format(q=urllib.parse.quote(query))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    page = raw.decode("utf-8", "ignore")
    print("URL：{}\n字节：{}\n".format(url, len(raw)))
    anchors = [(h, _strip(t)) for h, t in _ANCHOR.findall(page)]
    anchors = [(h, t) for h, t in anchors if len(t) >= 10]
    print("锚点共 {} 个；前 25 个：".format(len(anchors)))
    for h, t in anchors[:25]:
        print("  {:<70} {}".format(h[:70], t[:50]))
    pos = page.find(query) if not grep else -1
    if grep:
        pos = page.find(grep)
        print("定位串「{}」首次出现位置：{}".format(grep, pos))
    print("\n关键词首次出现位置：{}".format(page.find(query)))
    if pos >= 0:
        fragment = page[max(0, pos - 1500): pos + 2500]
        fragment = re.sub(r">\s+<", "><", fragment)
        print("\n--- 上下文片段（压缩空白后）---\n{}".format(fragment[:3500]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", default="英伟达")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dump", default="", help="打印指定源 id 的结果页结构")
    ap.add_argument("--grep", default="", help="dump 时按该串定位并打印上下文")
    args = ap.parse_args()

    if args.dump:
        return dump(args.dump, args.q, args.grep)

    print("探测查询：{}｜候选 {} 个｜单源超时 {:.0f}s\n".format(args.q, len(CANDIDATES), TIMEOUT))
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(CANDIDATES)) as pool:
        results = list(pool.map(lambda c: probe(c, args.q), CANDIDATES))

    ok = []
    for r in sorted(results, key=lambda x: (-x.get("hits_in_titles", 0), x["id"])):
        if r.get("error"):
            print("❌ {:<18} {:<26} {}".format(r["id"], r["name"], r["error"]))
            continue
        verdict = "✅可用" if r.get("hits_in_titles", 0) >= 3 else ("🟡可疑" if r.get("hits_in_titles") else "❌无命中")
        print("{} {:<18} {:<26} HTTP{} {:>7}B 锚点{:>4} 标题含查询词{:>3} {:>7}ms".format(
            verdict, r["id"], r["name"], r.get("status"), r.get("bytes", 0),
            r.get("anchors", 0), r.get("hits_in_titles", 0), r.get("ms", 0)))
        if r.get("hits_in_titles"):
            print("     样例：{}".format(" ｜ ".join(r["sample_hits"])))
        elif r.get("anchors"):
            print("     无命中样例：{}".format(" ｜ ".join(r["sample_any"])))
        if r.get("hits_in_titles", 0) >= 3:
            ok.append(r["id"])

    print("\n可用于「真关键词检索」的候选：{}".format("、".join(ok) if ok else "无"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
