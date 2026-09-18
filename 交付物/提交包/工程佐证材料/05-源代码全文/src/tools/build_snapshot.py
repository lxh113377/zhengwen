"""证闻 · 静态快照兜底站生成器

为什么需要它（29 号第 6 页）：
  「若作品为可运行的程序，需提供可运行的链接或应用程序源代码。对于网页应用，
    确保链接稳定可访问。」复赛为线上评选，评委只能点链接 —— 一旦线上环境不可达，
    必须还有一个「能看见界面与问答流程」的兜底形态。

三层冗余里的 L3：
  L1 线上真实系统（完整功能）  → 云沙箱部署
  L2 免登录只读入口（READONLY=1）→ 降低访问门槛
  L3 静态快照站（本脚本产出）   → 网络异常 / 链接失效时的最后一道

产物特性（全部为硬约束，不是「尽量」）：
  ① 单文件 index.html，**零外部依赖**（CSS 内联、字体走系统字体栈）
  ② 离线可打开（file:// 双击即可），无任何 CDN / 外部脚本
  ③ 不含本机绝对路径、不含密钥（提交包洁净门禁）
  ④ 明确标注「静态快照 · 非实时」，并说明与真实系统的差异 —— 不伪装成实时系统

用法：
    python app.py                 # 先起服务（另开一个终端）
    python tools/build_snapshot.py
    python tools/build_snapshot.py --base http://127.0.0.1:8848 --out ../../交付物/提交包/静态快照站
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.abspath(os.path.join(BASE_DIR, "..", "交付物", "提交包", "静态快照站"))
DEFAULT_SHOTS = os.path.abspath(
    os.path.join(BASE_DIR, "..", "交付物", "提交包", "工程佐证材料", "06-运行实拍截图")
)

# 快照要固化的问答（覆盖三类结果，让评委看到系统「什么时候答、什么时候拒、什么时候提示分歧」）
SNAPSHOT_QUERIES: list[str] = [
    "5号线开通后客流多少？",
    "加装电梯业主需要出多少钱？",
    "社区图书馆延时开放效果如何？",
    "5号线客流预测口径有没有矛盾？",
    "城镇化率的中国口径和全球口径能直接比吗？",
    "中国互联网使用人口占比是多少？",
    "今天天气怎么样？（越界测试）",
    "苹果手机最新款多少钱",
]

# 内嵌截图（相对路径引用，随快照站一起拷过去）
SHOTS = [
    ("viewport-1440-s1.png", "桌面 1440 · 首屏"),
    ("viewport-768-s1.png", "平板 768 · 首屏"),
    ("viewport-375-s3.png", "手机 375 · 分歧面板"),
    ("reduced-motion-1440.png", "开启「减少动效」→ 自动降级为静态版面"),
]


def get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_ask(base: str, query: str) -> dict:
    body = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/api/ask", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""), quote=True)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def render_answer_card(base: str, data: dict) -> str:
    if data.get("refused"):
        badge = '<span class="b b-refuse">已拒答</span>'
    elif data.get("mode") == "model":
        badge = f'<span class="b b-model">大模型生成（{esc(data.get("provider"))}）</span>'
    else:
        badge = '<span class="b b-rule">规则版</span>'

    parts = [
        '<article class="card">',
        f'<h3>{esc(data.get("query"))}</h3>',
        '<p class="meta">'
        f'{badge}<span class="b b-conf">置信度 {esc(data.get("confidence"))} / 阈值 {esc(data.get("threshold"))}</span>'
        "</p>",
        f'<pre class="answer">{esc(data.get("answer"))}</pre>',
    ]

    if data.get("refused") and data.get("refuse_reason"):
        parts.append(f'<p class="note">拒答依据：{esc(data["refuse_reason"])}</p>')
    if data.get("degraded_reason") and not data.get("refused"):
        parts.append(f'<p class="note">降级说明：{esc(data["degraded_reason"])}</p>')

    # 证据（含许可与原文入口）
    if data.get("citations"):
        rows = []
        for c in data["citations"]:
            try:
                ev = get(base, "/api/evidence/" + urllib.parse.quote(c["id"]))["evidence"]
            except (urllib.error.URLError, KeyError, ValueError):
                continue
            link = (
                f'<a href="{esc(ev.get("source_url"))}" target="_blank" rel="noopener noreferrer">原文入口</a>'
                if ev.get("source_url")
                else ""
            )
            rows.append(
                "<li>"
                f'<code>{esc(ev["id"])}</code> {esc(ev["text"])}'
                f'<span class="src">— {esc(ev["doc_title"])}｜{esc(ev["publisher"])}｜许可：{esc(ev["license"])} {link}</span>'
                "</li>"
            )
        parts.append('<div class="blk"><h4>证据（每条都能点回原文）</h4><ul class="ev">' + "".join(rows) + "</ul></div>")

    if data.get("divergences"):
        items = "".join(
            f'<li><b>{esc(d["topic"])}</b><br>{esc(d["summary"])}</li>' for d in data["divergences"]
        )
        parts.append('<div class="blk"><h4>多源差异（不替你合并成单一结论）</h4><ul class="dv">' + items + "</ul></div>")

    if data.get("trace"):
        steps = "".join(
            f'<li><span>{esc(t["step"])}</span><span>{esc(t["detail"])}</span></li>' for t in data["trace"]
        )
        parts.append(
            f'<details class="blk"><summary>决策轨迹（{len(data["trace"])} 步 · 全过程留痕）</summary>'
            f'<ul class="trace">{steps}</ul></details>'
        )

    parts.append("</article>")
    return "".join(parts)


def build(base: str, out_dir: str, shots_dir: str) -> str:
    health = get(base, "/api/health")
    graph = get(base, "/api/graph")
    answers = []
    for q in SNAPSHOT_QUERIES:
        try:
            answers.append(post_ask(base, q))
        except (urllib.error.URLError, ValueError) as err:
            print(f"  ! 跳过「{q}」：{err}")

    assets_dir = os.path.join(out_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    # 拷贝截图（缺失不致命，只是少了几张图）
    shot_html = []
    for name, caption in SHOTS:
        src = os.path.join(shots_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(assets_dir, name))
            shot_html.append(
                f'<figure><img src="assets/{esc(name)}" alt="{esc(caption)}" loading="lazy">'
                f"<figcaption>{esc(caption)}</figcaption></figure>"
            )
        else:
            print(f"  ! 缺少截图：{name}")

    stats = health.get("stats", {})
    sources = health.get("sources", [])
    saved_at = "2026-09-18"

    src_rows = "".join(
        f'<tr><td>{esc(s.get("id"))}</td><td>{esc(s.get("name"))}</td><td>{esc(s.get("license"))}</td>'
        f'<td>{esc(s.get("terms_url") or "—")}</td></tr>'
        for s in sources
    )

    model_path_text = (
        "在线模型（" + esc(health.get("provider")) + "）"
        if health.get("model_available")
        else "未配置模型凭据 → 规则版（界面如实标注，不冒充 AI 生成）"
    )
    answers_html = "".join(render_answer_card(base, a) for a in answers)
    shots_html = "".join(shot_html) if shot_html else "<p>（未找到截图文件，跳过）</p>"

    css = """
:root{--ink:#09090B;--sec:#3F3F46;--line:rgba(9,9,11,.12);--bg:#FAFAFA;--accent:#EC4899;--ok:#15803D}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
header{background:var(--ink);color:#FAFAFA;padding:40px 24px}
header .w{max-width:960px;margin:0 auto}
h1{font-size:34px;margin:0 0 8px;letter-spacing:-.02em}
h2{font-size:22px;margin:36px 0 12px;letter-spacing:-.01em}
h3{font-size:18px;margin:0 0 6px}
h4{font-size:14px;margin:18px 0 8px;letter-spacing:.04em;text-transform:uppercase;color:var(--sec)}
main{max-width:960px;margin:0 auto;padding:0 24px 80px}
.banner{background:var(--accent);color:var(--ink);padding:10px 24px;font-size:13.5px;font-weight:600;text-align:center}
.hint{background:#fff;border:1px solid var(--line);border-left:4px solid var(--accent);padding:14px 16px;margin:20px 0;font-size:14.5px}
table{width:100%;border-collapse:collapse;font-size:14px;margin:10px 0}
th,td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}
th{background:#fff;font-weight:600}
.card{background:#fff;border:1px solid var(--line);padding:18px 20px;margin:16px 0}
.meta{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 12px}
.b{font-size:12px;padding:2px 8px;border:1px solid var(--line);border-radius:999px;color:var(--sec)}
.b-rule{border-color:#B45309;color:#B45309}
.b-model{border-color:var(--ok);color:var(--ok)}
.b-refuse{border-color:#B91C1C;color:#B91C1C}
.answer{white-space:pre-wrap;font:14px/1.75 inherit;background:#FAFAFA;border:1px solid var(--line);padding:12px 14px;margin:8px 0;overflow-wrap:anywhere}
.note{font-size:13.5px;color:#B45309}
.blk{margin:14px 0}
ul.ev,ul.dv,ul.trace{margin:8px 0;padding-left:20px}
ul.ev li,ul.dv li,ul.trace li{font-size:14px;margin-bottom:8px}
.src{display:block;color:var(--sec);font-size:12.5px}
.trace li span{margin-right:10px}
figure{margin:0 0 18px}
img{width:100%;border:1px solid var(--line);display:block}
figcaption{font-size:12.5px;color:var(--sec);margin-top:6px}
footer{border-top:1px solid var(--line);margin-top:40px;padding-top:16px;font-size:13px;color:var(--sec)}
a{color:#B91C1C}
"""

    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>证闻 · 静态快照兜底站</title>
<meta name="description" content="证闻对话式新闻智能体 · 静态快照（离线可打开，非实时系统）">
<style>{css}</style>
</head>
<body>
<div class="banner">静态快照 · 非实时 ｜ 本页为离线兜底形态：可查看界面、问答流程与证据溯源，但不能实时提问</div>
<header><div class="w">
<h1>证闻 · 对话式新闻智能体</h1>
<p>把新闻从「单向阅读」变成「可追问、可溯源」—— 每一条结论都能点回原文。</p>
<p style="opacity:.8;font-size:14px">静态快照生成于 {esc(saved_at)} ｜ 源服务：本地演示实例（快照时状态如下）</p>
</div></header>

<main>
<div class="hint">
<b>与真实系统的差异（如实说明）：</b>本页是快照，不能实时提问；真实系统为可运行服务（Python 标准库实现，零第三方依赖），
支持实时检索、拒答判定、多源分歧提示与决策轨迹。静态页用于「线上环境不可达」时仍能看到系统行为与证据链。
</div>

<h2>一、运行状态（快照时刻）</h2>
<table>
<tr><th>项</th><th>值</th></tr>
<tr><td>服务</td><td>{esc(health.get("service"))}</td></tr>
<tr><td>语料</td><td>{esc(health.get("corpus"))}（{esc(health.get("corpus_kind"))}）</td></tr>
<tr><td>语料徽章</td><td>{esc(health.get("badge"))}</td></tr>
<tr><td>规模</td><td>{esc(stats.get("topics"))} 主题 / {esc(stats.get("sources"))} 来源 / {esc(stats.get("evidence"))} 条证据 / {esc(stats.get("divergences"))} 组分歧</td></tr>
<tr><td>生成路径</td><td>{model_path_text}</td></tr>
<tr><td>拒答阈值</td><td>{esc(health.get("refuse_threshold"))}</td></tr>
<tr><td>依据闸门</td><td>查询原始词命中 ≥ {esc(health.get("min_original_hits"))} 个（同义扩展词不计入依据判定）</td></tr>
</table>

<h2>二、语料来源与许可（逐源标注）</h2>
<table>
<tr><th>编号</th><th>来源</th><th>许可</th><th>条款</th></tr>
{src_rows}
</table>
<p style="font-size:13.5px;color:var(--sec)">合成语料为演示主力（边界场景可控）；世界银行数据为真实数值补充（CC BY 4.0，允许商用，义务为署名 + 不暗示背书 + 不使用其名称与标识）。两类内容在界面与证据中始终分开标注。</p>

<h2>三、问答快照（三类结果各取样）</h2>
{answers_html}

<h2>四、界面实拍（三视口与降级）</h2>
{shots_html}

<h2>五、如何运行真实系统</h2>
<ol style="font-size:14.5px">
<li>解压源码包，进入 <code>src/</code> 目录</li>
<li>直接启动：<code>python app.py</code>（零第三方依赖，Python 3.10+）</li>
<li>只读演示入口：<code>python app.py --readonly</code>（或设置 <code>READONLY=1</code>）</li>
<li>浏览器访问 <code>http://127.0.0.1:8848/</code>（只读模式另有 <code>/demo</code>）</li>
<li>自检回归：<code>python rag.py</code>（输出 23/23 通过）</li>
</ol>

<footer>
本快照由 <code>src/tools/build_snapshot.py</code> 依据运行中的服务状态生成；页面为单文件自包含，无外部依赖，可离线打开。<br>
世界银行数据按 CC BY 4.0 使用，署名格式：The World Bank: World Development Indicators；世界银行未参与、未背书本项目。
</footer>
</main>
</body>
</html>
"""

    index_path = os.path.join(out_dir, "index.html")
    with open(index_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(page)
    return index_path


def main() -> None:
    ap = argparse.ArgumentParser(description="生成证闻静态快照兜底站")
    ap.add_argument("--base", default="http://127.0.0.1:8848", help="运行中的服务地址")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出目录")
    ap.add_argument("--shots-dir", default=DEFAULT_SHOTS, help="实拍截图目录")
    args = ap.parse_args()

    print(f"从 {args.base} 抓取快照…")
    index = build(args.base, os.path.abspath(args.out), os.path.abspath(args.shots_dir))
    size = os.path.getsize(index)
    print(f"已生成：{index}（{size / 1024:.1f} KB）")
    print(f"离线打开：file:///{index.replace(os.sep, '/')}")


if __name__ == "__main__":
    main()
