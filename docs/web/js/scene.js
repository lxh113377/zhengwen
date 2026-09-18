/* =============================================================================
   证闻 · 3D 场景层
   职责：把「事件 → 来源 → 证据」的语料结构渲染成空间，并由滚动进度驱动镜头。
   设计取舍：全部使用程序化几何（点 / 线 / 二十面体），不做实物建模 ——
             12 天周期内这是唯一可行的路线，也是「离线可演示」的前提。
   降级：无 WebGL 或用户要求减少动效时，本模块整体退出，页面内容不受影响。
   ========================================================================== */

(function () {
  'use strict';

  const CANVAS_ID = 'scene';
  const FALLBACK_ID = 'scene-fallback';

  const COLORS = {
    topic: 0x18181b,
    source: 0x3f3f46,
    evidence: 0x71717a,
    divergence: 0xec4899,
    accent: 0xec4899,
  };

  const NODE_SIZE = { topic: 0.52, source: 0.3, evidence: 0.13, divergence: 0.26 };

  function reducedMotion() {
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function hasWebGL() {
    try {
      const c = document.createElement('canvas');
      return !!(window.WebGLRenderingContext && (c.getContext('webgl') || c.getContext('experimental-webgl')));
    } catch (e) {
      return false;
    }
  }

  /**
   * 确定性布局：同一份语料每次渲染位置一致。
   * 不用随机数 —— 演示需要可复现，随机布局会让截图与录像对不上。
   */
  function layout(graph) {
    const byType = { topic: [], source: [], evidence: [], divergence: [] };
    graph.nodes.forEach(function (n) { (byType[n.type] || byType.evidence).push(n); });

    const positions = {};
    const R = 9.5;

    // 主题：沿一段圆弧分布，给镜头横移留出空间
    byType.topic.forEach(function (n, i) {
      const t = byType.topic.length === 1 ? 0.5 : i / (byType.topic.length - 1);
      const angle = (-0.62 + t * 1.24) * Math.PI;
      positions[n.id] = {
        x: Math.cos(angle) * R * 0.62,
        y: (i % 2 === 0 ? 1 : -1) * 1.1,
        z: Math.sin(angle) * R * 0.62,
      };
    });

    // 来源：围绕所属主题
    byType.source.forEach(function (n, i) {
      const parentLink = graph.links.find(function (l) { return l.target === n.id && l.kind !== 'divergence'; });
      const base = (parentLink && positions[parentLink.source]) || { x: 0, y: 0, z: 0 };
      const k = i % 3;
      const ang = (k / 3) * Math.PI * 2 + 0.6;
      const rad = 3.1;
      positions[n.id] = {
        x: base.x + Math.cos(ang) * rad,
        y: base.y + (k - 1) * 0.85,
        z: base.z + Math.sin(ang) * rad,
      };
    });

    // 证据：围绕所属来源，形成可读的簇
    byType.evidence.forEach(function (n, i) {
      const parentLink = graph.links.find(function (l) { return l.target === n.id && l.kind !== 'divergence'; });
      const base = (parentLink && positions[parentLink.source]) || { x: 0, y: 0, z: 0 };
      const k = i % 4;
      const ang = (k / 4) * Math.PI * 2 + 0.35;
      const rad = 1.25;
      positions[n.id] = {
        x: base.x + Math.cos(ang) * rad,
        y: base.y + Math.sin(ang) * rad * 0.55,
        z: base.z + Math.sin(ang) * rad,
      };
    });

    // 分歧：落在它连接的两条证据之间 —— 视觉上就是「两种说法的交汇处」
    byType.divergence.forEach(function (n) {
      const targets = graph.links
        .filter(function (l) { return l.source === n.id && l.kind === 'divergence'; })
        .map(function (l) { return positions[l.target]; })
        .filter(Boolean);
      if (!targets.length) { positions[n.id] = { x: 0, y: 2.4, z: 0 }; return; }
      let x = 0, y = 0, z = 0;
      targets.forEach(function (p) { x += p.x; y += p.y; z += p.z; });
      positions[n.id] = { x: x / targets.length, y: y / targets.length + 0.9, z: z / targets.length };
    });

    return { positions: positions, byType: byType };
  }

  /**
   * 镜头轨迹：与叙事 5 段对齐（见《选题定案与产品定义》§3.5）
   */
  function cameraAt(p) {
    // p: 0 → 1
    const keys = [
      { at: 0.00, pos: [0, 3.2, 26], look: [0, 0, 0] },
      { at: 0.15, pos: [-7.5, 2.4, 19], look: [-3.2, 0.4, 0] },
      { at: 0.40, pos: [7.0, 2.0, 18], look: [3.4, 0.2, 0] },
      { at: 0.65, pos: [0, 1.4, 13.5], look: [0, 1.0, 0] },
      { at: 0.85, pos: [-2.2, 2.6, 15.5], look: [-1.4, 0.6, 0] },
      { at: 1.00, pos: [0, 4.4, 25], look: [0, 0.2, 0] },
    ];

    let a = keys[0], b = keys[keys.length - 1];
    for (let i = 0; i < keys.length - 1; i++) {
      if (p >= keys[i].at && p <= keys[i + 1].at) { a = keys[i]; b = keys[i + 1]; break; }
    }
    const span = (b.at - a.at) || 1;
    let t = Math.min(1, Math.max(0, (p - a.at) / span));
    t = t * t * (3 - 2 * t); // smoothstep

    const lerp = function (u, v) { return u + (v - u) * t; };
    return {
      pos: [lerp(a.pos[0], b.pos[0]), lerp(a.pos[1], b.pos[1]), lerp(a.pos[2], b.pos[2])],
      look: [lerp(a.look[0], b.look[0]), lerp(a.look[1], b.look[1]), lerp(a.look[2], b.look[2])],
    };
  }

  class Scene {
    constructor(canvas) {
      this.canvas = canvas;
      this.three = new THREE.Scene();
      this.three.background = null;

      this.camera = new THREE.PerspectiveCamera(46, 1, 0.1, 260);
      this.renderer = new THREE.WebGLRenderer({
        canvas: canvas,
        alpha: true,
        antialias: window.devicePixelRatio < 2,
        powerPreference: 'high-performance',
      });
      this.renderer.setClearColor(0x000000, 0);

      this.progress = 0;
      this.target = 0;
      this.clock = new THREE.Clock();
      this.ready = false;
      this.paused = false;
      this.lowFpsSince = null;   // 低帧率起始时刻（null = 当前帧率正常）
      this.downscaled = false;   // 是否已降过分辨率（只降一次）

      this.group = new THREE.Group();
      this.three.add(this.group);
      this.three.add(new THREE.AmbientLight(0xffffff, 0.85));
      const key = new THREE.DirectionalLight(0xffffff, 0.6);
      key.position.set(6, 10, 8);
      this.three.add(key);

      this._nodeData = [];
      this._divNodes = [];

      this._onResize = this._onResize.bind(this);
      this._loop = this._loop.bind(this);
      window.addEventListener('resize', this._onResize, { passive: true });
      document.addEventListener('visibilitychange', this._onVisibility.bind(this));
    }

    _onVisibility() {
      this.paused = document.hidden;
    }

    _onResize() {
      const w = window.innerWidth;
      const h = window.innerHeight;
      // 降级后不得被 resize 冲掉：否则用户一改窗口尺寸兜底就失效（与 lowFpsSince 判据配套）
      this.renderer.setPixelRatio(this.downscaled ? 1 : Math.min(window.devicePixelRatio || 1, 2));
      this.renderer.setSize(w, h, false);
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
    }

    /** 由 /api/graph 构建场景（首次） */
    build(graph) {
      this._buildInto(graph);
      this.ready = true;
      this._onResize();
      this.clock.start();
      requestAnimationFrame(this._loop);
    }

    /**
     * 用新图谱重建场景 —— 实时核证每次提问都会产出新的证据网络，
     * 3D 页据此切换（用户选定的方案：3D 页渲染「本次提问检索到的证据网络」）。
     * 返回 false 表示图谱为空：调用方保留原场景并明示原因，不留白屏、不假装有内容。
     */
    setGraph(graph) {
      if (!graph || !graph.nodes || !graph.nodes.length) return false;
      this._buildInto(graph);
      return true;
    }

    _buildInto(graph) {
      const packed = layout(graph);
      this.group.clear();
      // 重建必须清空节点缓存：原实现只 push 不清空，二次构建会累积「幽灵节点」
      // （实时检索每次提问都重建，不清空会越积越多并影响性能判据）。
      this._nodeData = [];
      this._divNodes = [];

      const verts = {};
      packed.byType.evidence.concat(packed.byType.topic, packed.byType.source, packed.byType.divergence).forEach(function (n) {
        verts[n.id] = packed.positions[n.id];
      });

      // 连线：主题→来源→证据
      const linePositions = [];
      graph.links.forEach(function (l) {
        const a = verts[l.source], b = verts[l.target];
        if (!a || !b) return;
        linePositions.push(a.x, a.y, a.z, b.x, b.y, b.z);
      });
      const lineGeo = new THREE.BufferGeometry();
      lineGeo.setAttribute('position', new THREE.Float32BufferAttribute(linePositions, 3));
      const lines = new THREE.LineSegments(
        lineGeo,
        new THREE.LineBasicMaterial({ color: 0x18181b, transparent: true, opacity: 0.13 })
      );
      this.group.add(lines);

      // 节点：按类型分组，用 Points 批量渲染（性能友好）
      const self = this;
      ['topic', 'source', 'evidence', 'divergence'].forEach(function (type) {
        const nodes = packed.byType[type] || [];
        if (!nodes.length) return;

        const pos = [];
        const col = [];
        const color = new THREE.Color(COLORS[type === 'divergence' ? 'divergence' : type] || COLORS.evidence);

        nodes.forEach(function (n) {
          const v = packed.positions[n.id];
          pos.push(v.x, v.y, v.z);
          col.push(color.r, color.g, color.b);
          self._nodeData.push({ id: n.id, type: type, data: n, pos: v, phase: Math.random() * Math.PI * 2 });
        });

        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
        geo.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));

        const mat = new THREE.PointsMaterial({
          size: NODE_SIZE[type] * 1.9,
          vertexColors: true,
          transparent: true,
          opacity: type === 'evidence' ? 0.72 : 1,
          sizeAttenuation: true,
          depthWrite: false,
        });

        const points = new THREE.Points(geo, mat);
        self.group.add(points);
        if (type === 'divergence') self._divNodes.push(points);
      });
    }

    /** 滚动进度注入（0 → 1） */
    setProgress(p) {
      this.target = Math.min(1, Math.max(0, p));
    }

    _loop() {
      if (this.ready && !this.paused) {
        const dt = Math.min(this.clock.getDelta(), 0.05);
        const t = this.clock.elapsedTime;

        // 低帧率自适应：持续掉帧则降低渲染分辨率（移动端 / 低端设备保底）。
        // 判据用「低帧率持续挂钟时间」而非「帧数」——2026-09-18 实测：
        //   按帧计数时帧率越低越难凑满 24 帧（10fps→2.4s，1fps→24s，0.4fps→60s），
        //   在 CPU 限速 x100（实测 0.4fps、<26fps 帧占比 85.7%）下 8s 内**永不触发**，
        //   恰好掩盖了「卡到几乎不动」这个最需要兜底的场景。
        //   改为时间判据后，任何持续 ≥1s 的低帧率都会降级一次（只降一次，不反复）。
        const fps = dt > 0 ? 1 / dt : 60;
        const now = performance.now();
        if (fps < 26) {
          if (this.lowFpsSince === null) this.lowFpsSince = now;
          if (!this.downscaled && now - this.lowFpsSince >= 1000) {
            this.downscaled = true;
            this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1));
          }
        } else if (fps > 50) {
          this.lowFpsSince = null;   // 帧率恢复正常即重置计时窗口
        }

        this.progress += (this.target - this.progress) * Math.min(1, dt * 7.5);

        const shot = cameraAt(this.progress);
        this.camera.position.set(shot.pos[0], shot.pos[1], shot.pos[2]);
        this.camera.lookAt(shot.look[0], shot.look[1], shot.look[2]);

        // 缓慢自转 + 呼吸，让静态语料「活」起来（幅度刻意很小，避免干扰阅读）
        this.group.rotation.y = Math.sin(t * 0.075) * 0.14 + this.progress * 0.3;
        this.group.position.y = Math.sin(t * 0.42) * 0.12;

        // 分歧节点在「多源比对」段（0.40–0.68）高亮脉动
        const inBand = this.progress > 0.36 && this.progress < 0.70;
        const pulse = inBand ? 1 + Math.sin(t * 3.2) * 0.26 : 1;
        this._divNodes.forEach(function (pts) {
          pts.material.size = NODE_SIZE.divergence * 1.9 * pulse;
          pts.material.opacity = inBand ? 1 : 0.55;
        });

        this.renderer.render(this.three, this.camera);
      }
      requestAnimationFrame(this._loop);
    }
  }

  /** 入口：可用则启动，不可用则安静降级 */
  window.ZhengwenScene = {
    boot: function (graph) {
      const canvas = document.getElementById(CANVAS_ID);
      const fallback = document.getElementById(FALLBACK_ID);

      function degrade(reason) {
        if (canvas) canvas.style.display = 'none';
        if (fallback) { fallback.hidden = false; }
        window.__sceneStatus = { ok: false, reason: reason };
      }

      if (!canvas) { degrade('canvas 缺失'); return null; }
      if (!window.THREE) { degrade('Three.js 未加载'); return null; }
      if (!hasWebGL()) { degrade('当前环境不支持 WebGL'); return null; }
      if (reducedMotion()) { degrade('用户已开启「减少动效」→ 使用静态版面'); return null; }

      try {
        const scene = new Scene(canvas);
        scene.build(graph);
        window.__sceneStatus = { ok: true, reason: '运行中', nodes: graph.nodes.length };
        return scene;
      } catch (err) {
        degrade('WebGL 初始化失败：' + (err && err.message ? err.message : '未知错误'));
        return null;
      }
    },
  };
})();
