/* =============================================================================
   证闻 · 交互层
   职责：拉语料图谱 → 驱动 3D 镜头 → 承载问答 → 呈现证据 / 分歧 / 轨迹
   纪律：任何失败都不把裸错误串抛给用户；规则版与模型版在界面上必须可区分。
   ========================================================================== */

(function () {
  'use strict';

  const $ = function (sel, root) { return (root || document).querySelector(sel); };
  const $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  const state = {
    scene: null,
    graph: null,
    health: null,
    busy: false,
    // 本轮问答返回的全部证据（离线 + 实时）—— 实时证据不落在服务端语料里，
    // 证据抽屉必须先查这个缓存，否则实时引用会「点回原文」失败。
    evidence: {},
    sceneMode: 'corpus',   // corpus = 离线语料图谱 ｜ live = 本次实时检索图谱
    liveStats: null,
  };

  /* ── 工具 ──────────────────────────────────────────────────────────── */

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function setStatus(text, kind) {
    const dot = $('#dock-dot');
    const label = $('#dock-status');
    if (label) label.textContent = text;
    if (dot) dot.className = 'dot' + (kind ? ' is-' + kind : '');
  }

  async function api(path, options) {
    const res = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options || {}));
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok || !data || data.ok === false) {
      const msg = (data && data.error) || ('请求失败（HTTP ' + res.status + '）');
      const hint = (data && data.hint) || '';
      const err = new Error(msg);
      err.hint = hint;
      throw err;
    }
    return data;
  }

  /* ── 顶栏 / 运行时状态 ─────────────────────────────────────────────── */

  function renderRuntime() {
    const note = $('#runtime-note');
    const h = state.health || {};
    const sceneStatus = window.__sceneStatus || {};

    const lines = [];
    lines.push('语料：' + (h.corpus || '—') + '（' + (h.corpus_kind || '—') + '）');
    // 逐源列出许可：合成语料与真实开放数据必须能被分开识别（三标签制）
    if (h.sources && h.sources.length) {
      h.sources.forEach(function (s) {
        lines.push('　· ' + (s.id || '?') + ' ' + (s.name || '') + '｜' + (s.license || '—') + (s.retrieved ? '｜取得 ' + s.retrieved : ''));
      });
    } else {
      lines.push('许可：' + (h.license || '—'));
    }
    lines.push('依据闸门：原始词命中 ≥' + (h.min_original_hits != null ? h.min_original_hits : '—') + ' 个（同义扩展词不计入）');
    lines.push('语料规模：' + (h.stats ? (h.stats.topics + ' 主题 / ' + h.stats.sources + ' 来源 / ' + h.stats.evidence + ' 条证据 / ' + h.stats.divergences + ' 组分歧') : '—'));
    lines.push('生成路径：' + (h.model_available ? ('在线模型（' + (h.provider || '') + '）') : '未配置模型凭据 → 规则版'));
    lines.push('拒答阈值：' + (h.refuse_threshold != null ? h.refuse_threshold : '—'));
    if (h.live) {
      lines.push('实时源：可用 ' + h.live.available_sources + ' 个 ｜ 实测不可用 ' + h.live.unavailable_sources +
        ' 项 ｜ 通用检索插槽 ' + (h.live.web_search_slot_configured ? '已配置' : '未配置'));
      // 分层判据必须公开可核验：哪一档才算「依据」，哪一档只是「线索」
      if (h.live.strict_min_bigram_hits != null) {
        lines.push('分层判据：可核实依据（实时命中 ≥' + h.live.strict_min_bigram_hits + ' 个查询原始词）' +
          ' ｜ 相关线索（≥' + h.live.related_min_bigram_hits + '，最多 ' + h.live.max_related + ' 条，不作依据）');
      }
    }
    lines.push('3D 场景：' + (sceneStatus.ok ? sceneStatus.reason : ('已降级（' + (sceneStatus.reason || '未启动') + '）')));
    lines.push('3D 展示：' + (state.sceneMode === 'live' && state.liveStats
      ? ('本次实时检索图谱（' + state.liveStats.sources + ' 源 / ' + state.liveStats.evidence + ' 条证据）')
      : ('离线语料图谱' + (state.liveMiss
          ? '（本次未取得实时证据：' + state.liveMiss + '）'
          : '（提问命中实时证据后自动切换）'))));

    if (note) note.textContent = lines.join('\n');
    if (note) note.style.whiteSpace = 'pre-line';

    setStatus(h.model_available ? '在线模型就绪' : '规则版就绪', h.model_available ? 'model' : 'rule');

    // 顶栏徽章：语料是「合成演示 + 公开来源」混合时，必须让人一眼看出两类内容并存
    // （三标签制要求：合成数据不得被当成真实报道）
    const badge = $('#demo-badge');
    if (badge && h.badge) {
      badge.textContent = h.badge;
      badge.title = (h.sources || []).map(function (s) {
        return (s.id || '?') + '：' + (s.name || '') + '（' + (s.license || '—') + '）';
      }).join('\n') || '语料许可信息见运行状态';
    }

    // 只读演示横幅：免登录入口必须让访问者一眼看清「这是演示环境、只读、数据含合成成分」
    if (h.readonly && !$('#readonly-banner')) {
      const bar = document.createElement('div');
      bar.id = 'readonly-banner';
      bar.className = 'readonly-banner';
      bar.setAttribute('role', 'status');
      bar.textContent = '演示环境 · 只读 ｜ 免登录访问、不写入任何数据。语料为「合成演示 + 公开来源（世界银行 CC BY 4.0）」，每条回答均标注来源与许可。';
      document.body.appendChild(bar);
      document.body.classList.add('has-readonly-banner');
    }
  }

  function renderStats() {
    const h = state.health;
    if (!h || !h.stats) return;
    Object.keys(h.stats).forEach(function (k) {
      const el = document.querySelector('[data-stat="' + k + '"]');
      if (el) el.textContent = h.stats[k];
    });
  }

  function renderDivergences() {
    const stage = $('#divergence-stage');
    if (!stage || !state.graph) return;

    const divs = state.graph.nodes.filter(function (n) { return n.type === 'divergence'; });
    if (!divs.length) {
      stage.innerHTML = '<p class="muted">本语料未标注多源分歧。</p>';
      return;
    }

    const esc2 = esc;
    stage.innerHTML = divs.map(function (dv) {
      const sides = state.graph.links
        .filter(function (l) { return l.source === dv.id && l.kind === 'divergence'; })
        .map(function (l) {
          return state.graph.nodes.find(function (n) { return n.id === l.target; });
        })
        .filter(Boolean);

      const sideHtml = sides.map(function (s) {
        return '<div class="dv-side"><span class="src">' + esc2(s.doc_id || '') + '</span><span class="txt">' + esc2(s.label) + '</span></div>';
      }).join('');

      return '<article class="dv-card">' +
        '<h4>' + esc2(dv.label) + '</h4>' +
        '<p>' + esc2(dv.summary || '') + '</p>' +
        (sideHtml ? '<div class="dv-sides">' + sideHtml + '</div>' : '') +
        '</article>';
    }).join('');
  }

  /* ── 问答 ──────────────────────────────────────────────────────────── */

  function addUserMsg(text) {
    const thread = $('#thread');
    const el = document.createElement('div');
    el.className = 'msg msg-user';
    el.innerHTML = '<div class="bubble">' + esc(text) + '</div>';
    thread.appendChild(el);
    scrollThread();
  }

  function badgeFor(data) {
    if (data.refused) return '<span class="badge badge-refuse">已拒答</span>';
    if (data.mode === 'model') return '<span class="badge badge-model">大模型生成' + (data.provider ? '（' + esc(data.provider) + '）' : '') + '</span>';
    return '<span class="badge badge-rule">规则版</span>';
  }

  /**
   * 实时核证区块：逐源如实列出「查了谁、命中几条、用了多久」。
   * 这是「假查证 vs 真查证」在界面上的分界线 —— 查了没命中也要写出来，不许只报好消息。
   */
  function liveBlock(data) {
    const live = data.live;
    if (!live) return '';
    const rows = (live.sources || []).map(function (s) {
      const txt = s.ok ? (s.hits ? '命中 ' + s.hits + ' 条' : '未命中') : '本次未取得结果（' + (s.error || '未知') + '）';
      return '<li><span>' + esc(s.name) + '</span><span>' + esc(txt) + '</span>' +
        '<span class="t-ms">' + (s.ms != null ? s.ms + ' ms' : '') + '</span></li>';
    }).join('');
    // 三种状态必须视觉可分：命中 / 未命中 / 联网失败 —— 「查了没查到」也是一种结果，不能看起来像查到了
    const cls = live.offline ? ' is-offline' : (live.hits ? '' : ' is-empty');
    return '<div class="live-box' + cls + '">' +
      '<div class="live-head">' +
      '<span class="badge badge-live">实时核证</span>' +
      '<span class="live-note">' + esc(live.note || '') + '</span>' +
      '</div>' +
      (rows ? '<details class="live-details" open><summary>本次逐源结果（' + (live.sources || []).length + ' 源）</summary><ul class="live-list">' + rows + '</ul></details>' : '') +
      (live.fetched_at ? '<div class="note">抓取时间：' + esc(live.fetched_at) + '（实时来源的时效以该时间为准）</div>' : '') +
      '</div>';
  }

  /**
   * 相关线索区块（分层输出第二层，2026-09-19）。
   *
   * 为什么需要：用户报障「拒答率太高、像假查证」。真实原因是召回不足，
   * 但直接放宽闸门 = 把弱相关条目当依据。故新增这一层：
   * 查到什么就照实列出来，但**必须一眼看出它不是依据**。
   * 纪律：线索不得染指结论区 —— 独立成块、标注「未核实」、不给引用编号、不进模型 prompt。
   */
  function relatedBlock(data) {
    const items = data.related || [];
    if (!items.length) return '';
    const rows = items.map(function (r) {
      const meta = [r.source_name, r.published, r.fetched_at ? ('抓取 ' + r.fetched_at) : '']
        .filter(Boolean).map(esc).join(' ｜ ');
      const link = r.url
        ? '<a class="rel-link" href="' + esc(r.url) + '" target="_blank" rel="noopener noreferrer">打开原文 ↗</a>'
        : '';
      return '<li>' +
        '<div class="rel-title">' + esc(r.title || '') + '</div>' +
        '<div class="rel-meta">' + meta + (link ? '　' + link : '') + '</div>' +
        (r.text && r.text !== r.title ? '<div class="rel-text">' + esc(r.text) + '</div>' : '') +
        '</li>';
    }).join('');
    return '<div class="related-box">' +
      '<div class="live-head">' +
      '<span class="badge badge-related">相关线索 ' + items.length + ' 条</span>' +
      '<span class="live-note">主题相关但 <b>未经核实</b>，只可作为找线索的起点，<b>不构成依据</b></span>' +
      '</div>' +
      '<ul class="related-list">' + rows + '</ul>' +
      '</div>';
  }

  function renderAnswer(data) {
    const thread = $('#thread');
    const el = document.createElement('div');
    el.className = 'msg msg-ai';

    // 缓存本轮全部证据（含实时）供证据抽屉使用
    (data.evidence || []).forEach(function (e) { state.evidence[e.id] = e; });

    const conf = typeof data.confidence === 'number' ? data.confidence.toFixed(2) : '—';
    const liveHits = data.live ? data.live.hits : 0;
    const relatedHits = data.live ? (data.live.related_hits || 0) : (data.related || []).length;
    const head =
      '<div class="head">' +
      badgeFor(data) +
      (liveHits ? '<span class="badge badge-live">实时可核实证据 ' + liveHits + ' 条</span>' : '') +
      (relatedHits ? '<span class="badge badge-related">相关线索 ' + relatedHits + ' 条（未核实）</span>' : '') +
      '<span class="badge badge-conf">离线语料置信度 ' + conf + (data.threshold != null ? ' / 阈值 ' + data.threshold : '') + '</span>' +
      (data.timing && data.timing.server_total_ms != null ? '<span class="badge badge-conf">' + data.timing.server_total_ms + ' ms</span>' : '') +
      '</div>';

    const answerCls = data.refused ? 'answer is-refused' : 'answer';
    let body = '<div class="' + answerCls + '">' + esc(data.answer) + '</div>';

    if (data.refused && data.refuse_reason) {
      body += '<div class="note">拒答依据：' + esc(data.refuse_reason) + '</div>';
    }

    if (data.degraded_reason && !data.refused) {
      body += '<div class="note">降级说明：' + esc(data.degraded_reason) + '</div>';
    }

    // 引用（可点击 → 证据抽屉）。实时引用加「实时·」前缀，与离线语料一眼可分。
    if (data.citations && data.citations.length) {
      body += '<div class="cites">' + data.citations.map(function (c) {
        const ev = state.evidence[c.id];
        const isLive = !!(ev && ev.live);
        return '<button class="cite' + (isLive ? ' is-live' : '') + '" type="button" data-ev="' + esc(c.id) + '">' +
          (isLive ? '实时·' : '') + esc(c.id) + '</button>';
      }).join('') + '</div>';
    }

    // 实时核证区块（逐源状态 + 抓取时间）
    body += liveBlock(data);

    // 相关线索区块（未核实）—— 拒答时也照常展示：
    // 「没有可核实依据」不等于「什么都没查到」，把查到的线索如实交出去才算真查证。
    body += relatedBlock(data);

    // 本次检出的多源分歧
    if (data.divergences && data.divergences.length) {
      body += data.divergences.map(function (dv) {
        return '<div class="dv-inline"><b>多源差异 · ' + esc(dv.topic) + '</b><br>' + esc(dv.summary) + '</div>';
      }).join('');
    }

    // 决策轨迹（全过程留痕）
    if (data.trace && data.trace.length) {
      body += '<details class="trace-box"><summary>决策轨迹（' + data.trace.length + ' 步）</summary><ul class="trace-list">' +
        data.trace.map(function (t) {
          return '<li><span>' + esc(t.step) + '</span><span>' + esc(t.detail) + '</span><span class="t-ms">' + (t.ms ? t.ms + ' ms' : '') + '</span></li>';
        }).join('') + '</ul></details>';
    }

    if (data.dropped_citations && data.dropped_citations.length) {
      body += '<div class="note">越界引用已丢弃：' + esc(data.dropped_citations.join(', ')) + '</div>';
    }

    el.innerHTML = head + body;
    thread.appendChild(el);

    $$('.cite', el).forEach(function (btn) {
      btn.addEventListener('click', function () { openEvidence(btn.getAttribute('data-ev')); });
    });

    scrollThread();
  }

  function scrollThread() {
    const body = $('#dock-body');
    if (body) body.scrollTop = body.scrollHeight;
  }

  /**
   * 3D 页切换为「本次实时检索到的证据网络」（本轮定案）。
   *
   * 🔴 本次没有实时证据时必须**回落**到离线语料图谱：
   *    浏览器实测抓到过一个真实缺陷 —— 首次提问有实时证据后，第二次提问（无实时证据）
   *    3D 仍停在上一轮的实时网络上（节点数不变），用户会误以为那就是本次结果。
   *    这比白屏更糟：白屏至少不撒谎。因此「无命中 → 回落 + 标注为离线图谱」。
   */
  function applyLiveGraph(data) {
    const g = data.live_graph;
    const stats = (g && g.stats) || { sources: 0, evidence: 0 };
    if (!state.scene) { state.sceneMode = 'corpus'; state.liveStats = null; return; }
    if (stats.evidence > 0 && state.scene.setGraph(g)) {
      state.sceneMode = 'live';
      state.liveStats = stats;
      state.liveMiss = '';
      return;
    }
    if (state.graph) state.scene.setGraph(state.graph);
    state.sceneMode = 'corpus';
    state.liveStats = null;
    state.liveMiss = (data.live && data.live.note) || '本次未取得实时证据';
  }

  async function ask(query) {
    if (state.busy) return;
    const q = (query || '').trim();
    if (!q) return;

    state.busy = true;
    const btn = $('#btn-send');
    const input = $('#input');
    if (btn) btn.disabled = true;
    if (input) input.value = '';
    setStatus('检索中…', 'busy');
    addUserMsg(q);

    try {
      const data = await api('/api/ask', { method: 'POST', body: JSON.stringify({ query: q, session: 'web' }) });
      renderAnswer(data);
      applyLiveGraph(data);
      renderRuntime();
      const liveNote = (data.live && data.live.hits) ? '（实时可核实证据 ' + data.live.hits + ' 条）' : '';
      const relNote = (data.live && data.live.related_hits) ? '（另有相关线索 ' + data.live.related_hits + ' 条，未核实）' : '';
      setStatus(
        (data.refused ? '已按闸门拒答；本轮已实时检索权威源，结果见下方' : (data.mode === 'model' ? '在线模型作答' : '规则版作答')) + liveNote + relNote,
        data.refused ? 'error' : (data.mode === 'model' ? 'model' : 'rule'));
    } catch (err) {
      const thread = $('#thread');
      const el = document.createElement('div');
      el.className = 'msg msg-ai';
      el.innerHTML = '<div class="head"><span class="badge badge-refuse">请求失败</span></div>' +
        '<div class="answer is-refused">' + esc(err.message || '服务暂时不可用') + '</div>' +
        (err.hint ? '<div class="note">' + esc(err.hint) + '</div>' : '');
      thread.appendChild(el);
      scrollThread();
      setStatus('请求失败', 'error');
    } finally {
      state.busy = false;
      if (btn) btn.disabled = false;
      if (input) input.focus();
    }
  }

  /* ── 证据抽屉 ──────────────────────────────────────────────────────── */

  function evidenceHtml(ev) {
    const liveTag = ev.live ? '<span class="badge badge-live">实时抓取</span>' : '';
    return '<article class="ev-block' + (ev.live ? ' is-live' : '') + '">' +
      '<div class="ev-id">' + liveTag + esc(ev.id) + '</div>' +
      '<p class="ev-text">' + esc(ev.text) + '</p>' +
      '<dl>' +
      '<dt>' + (ev.live ? '标题' : '文档') + '</dt><dd>' + esc(ev.doc_title) + '</dd>' +
      '<dt>来源</dt><dd>' + esc(ev.publisher) + '</dd>' +
      '<dt>' + (ev.live ? '发布' : '日期') + '</dt><dd>' + esc(ev.published) + '</dd>' +
      (ev.fetched_at ? '<dt>抓取时间</dt><dd>' + esc(ev.fetched_at) + '</dd>' : '') +
      '<dt>类型</dt><dd>' + esc(ev.kind) + '</dd>' +
      '<dt>许可</dt><dd>' + esc(ev.license) + '</dd>' +
      // 原文入口：离线真实来源（世界银行）与实时来源都可点回原文 —— 「每一条结论都能点回原文」
      (ev.source_url
        ? '<dt>原文</dt><dd><a href="' + esc(ev.source_url) + '" target="_blank" rel="noopener noreferrer">' + esc(ev.source_url) + '</a></dd>'
        : '') +
      // 许可要求的署名串（CC BY 4.0 等）
      (ev.attribution ? '<dt>署名</dt><dd>' + esc(ev.attribution) + '</dd>' : '') +
      (ev.matched && ev.matched.length ? '<dt>命中词</dt><dd>' + esc(ev.matched.join(' / ')) + '</dd>' : '') +
      '</dl></article>';
  }

  async function openEvidence(evId) {
    const drawer = $('#drawer');
    const scrim = $('#scrim');
    const body = $('#drawer-body');
    if (!drawer || !body) return;

    body.innerHTML = '<p class="muted">正在读取证据…</p>';
    drawer.classList.add('is-open');
    drawer.setAttribute('aria-hidden', 'false');
    if (scrim) { scrim.hidden = false; requestAnimationFrame(function () { scrim.classList.add('is-open'); }); }

    // 先查本轮问答带回的证据缓存 —— 实时证据只存在于这次响应里，
    // /api/evidence/<id> 只认识离线语料，不查缓存就会「实时引用点开报错」。
    const cached = state.evidence[evId];
    if (cached && cached.live) {
      body.innerHTML = evidenceHtml(cached);
      return;
    }

    try {
      const data = await api('/api/evidence/' + encodeURIComponent(evId));
      const ev = Object.assign({}, data.evidence, cached || {});
      body.innerHTML = evidenceHtml(ev);
    } catch (err) {
      body.innerHTML = '<p class="muted">' + esc(err.message || '证据读取失败') + '</p>';
    }
  }

  function closeEvidence() {
    const drawer = $('#drawer');
    const scrim = $('#scrim');
    if (drawer) { drawer.classList.remove('is-open'); drawer.setAttribute('aria-hidden', 'true'); }
    if (scrim) {
      scrim.classList.remove('is-open');
      setTimeout(function () { scrim.hidden = true; }, 320);
    }
  }

  /* ── 滚动驱动 ──────────────────────────────────────────────────────── */

  /**
   * 叙事进度 = #story 已滚出的比例，**每次都由实时布局重算**。
   *
   * 为什么不能用 ScrollTrigger 的缓存 progress（2026-09-19 实测根因，非推测）：
   *   #story 的高度不是首屏就定型的 —— 它取决于 /api/graph 返回后
   *   renderDivergences() 往 #s3 注入的分歧卡（4 张，把 #s3 由 ~900px 撑到 ~2948px，
   *   整页由 5×vh 涨到 6616px）。而 ScrollTrigger 只在 load / resize /
   *   visibilitychange 时重算 start/end，**DOM 内容注入不会触发重算**。
   *   于是出现两种失效（本机实测，1440×900）：
   *     · 正常网络：load 晚于注入 → end=5714（= maxScroll）→ 正常；
   *     · fonts.googleapis.com **挂起**（境内典型，load 永不触发）或
   *       接口慢于 load → end 停在创建时的 3600 → 滚动条过 63% 后 progress
   *       恒为 1，3D 背景「后半段不随滚动移动」。
   *   改读实时 rect 后，任何布局变化（接口延迟、字体挂起、窗口缩放）都自动生效。
   */
  function storyProgress(story) {
    const rect = story.getBoundingClientRect();
    const total = rect.height - window.innerHeight;
    const p = total > 0 ? (-rect.top / total) : 0;

    // 兜底（2026-09-19，用户报障「后半段 3D 不动」且是**概率性**的）：
    //   #story 的几何推算有时会与**文档真实可滚动范围**不一致（布局尚未定型、内容注入、
    //   平台注入横幅、字体回流等都可能造成）——此时进度会提前停在 1，表现就是「后半段不动」。
    //   本机与线上跑同一套代码时好时坏，正说明不能只信这一个中间变量。
    //   判据：两者差值超过一个容差时，**以真实滚动范围为准** ——
    //   需求本身是「3D 跟住用户手上的滚动条」，不是「跟住某个矩形的高度」。
    const doc = document.scrollingElement || document.documentElement;
    const maxScroll = Math.max(0, doc.scrollHeight - window.innerHeight);
    if (maxScroll > 0 && Math.abs(maxScroll - total) > 24) {
      const pDoc = doc.scrollTop / maxScroll;
      return pDoc < 0 ? 0 : (pDoc > 1 ? 1 : pDoc);
    }

    return p < 0 ? 0 : (p > 1 ? 1 : p);
  }

  function bindScroll() {
    const story = $('#story');
    if (!story) return;

    const push = function () {
      if (state.scene) state.scene.setProgress(storyProgress(story));
    };

    // 滚动事件只做「排一帧」，真正的重算合并到 rAF 里（滚动期间不重复计算布局）
    let ticking = false;
    const onScroll = function () {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(function () { ticking = false; push(); });
    };

    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll, { passive: true });
    // 内容注入 / 字体回流导致 #story 高度变化时也要重算（ScrollTrigger 不感知这些）
    if (window.ResizeObserver) {
      new ResizeObserver(onScroll).observe(story);
    }
    push();

    if (!window.ScrollTrigger || !window.gsap) return;

    gsap.registerPlugin(ScrollTrigger);

    // 面板进入视口时轻微上浮（编辑排版的呼吸感）
    $$('.panel-inner').forEach(function (el) {
      gsap.fromTo(el, { y: 26, opacity: 0 }, {
        y: 0, opacity: 1, duration: 0.75, ease: 'power2.out',
        scrollTrigger: { trigger: el, start: 'top 86%', toggleActions: 'play none none reverse' },
      });
    });
  }

  /** 内容注入后刷新 ScrollTrigger：它的触发点是创建时算好的，不会自己感知 DOM 高度变化。 */
  function refreshScrollTriggers() {
    if (window.ScrollTrigger && window.ScrollTrigger.refresh) {
      window.ScrollTrigger.refresh();
    }
  }

  /* ── 启动 ──────────────────────────────────────────────────────────── */

  async function boot() {
    // 事件绑定先做，保证即使接口失败交互仍可用
    const composer = $('#composer');
    if (composer) {
      composer.addEventListener('submit', function (e) {
        e.preventDefault();
        ask($('#input') ? $('#input').value : '');
      });
    }

    $$('.chip').forEach(function (chip) {
      chip.addEventListener('click', function () { ask(chip.getAttribute('data-q')); });
    });

    const btnTop = $('#btn-ask-top');
    if (btnTop) btnTop.addEventListener('click', function () {
      const input = $('#input');
      if (input) input.focus();
      const dock = $('#dock');
      if (dock) dock.classList.remove('is-collapsed');
    });

    const btnCollapse = $('#btn-collapse');
    if (btnCollapse) btnCollapse.addEventListener('click', function () {
      const dock = $('#dock');
      if (dock) dock.classList.toggle('is-collapsed');
    });

    const btnClose = $('#btn-close-drawer');
    if (btnClose) btnClose.addEventListener('click', closeEvidence);
    const scrim = $('#scrim');
    if (scrim) scrim.addEventListener('click', closeEvidence);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeEvidence(); });

    bindScroll();

    try {
      const [health, graph] = await Promise.all([api('/api/health'), api('/api/graph')]);
      state.health = health;
      state.graph = graph;

      renderStats();
      renderDivergences();
      renderRuntime();

      if (window.ZhengwenScene) {
        state.scene = window.ZhengwenScene.boot(graph);
      }
      // 分歧卡/统计数字是**接口回来后才注入**的，页面高度在此刻才定型 ——
      // 必须显式刷新 ScrollTrigger（面板上浮动画的触发点同理），
      // 否则触发点停在创建时的旧布局上（见 storyProgress 的实测说明）。
      refreshScrollTriggers();

      // 场景启动状态可能变化，重绘一次运行时信息；顺带兜一次晚到的布局回流
      setTimeout(function () { renderRuntime(); refreshScrollTriggers(); }, 120);
    } catch (err) {
      setStatus('语料加载失败', 'error');
      const note = $('#runtime-note');
      if (note) note.textContent = '语料加载失败：' + (err.message || '未知错误') + '。问答仍可尝试。';
      const stage = $('#divergence-stage');
      if (stage) stage.innerHTML = '<p class="muted">语料未加载，无法展示分歧。</p>';
    } finally {
      document.body.classList.remove('is-loading');
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
