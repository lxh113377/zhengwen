# AC-OBS-09 · 三视口与降级实测记录

> 对应验收标准：**AC-OBS-09**（`memory/08-ac-obs.md`）—— 3D 性能兜底：三视口（1440/768/375）无横向溢出；`prefers-reduced-motion` 下自动降级为静态版。
> 实测日期：2026-09-18 ｜ 实测人：AI agent（agent-browser · Chrome/CDP headless）｜ 服务：`python src/app.py`（127.0.0.1:8848，标准库零依赖）

---

## 一、实测环境与方法

| 项 | 值 |
|---|---|
| 被测页面 | `http://127.0.0.1:8848/`（证闻 3D 滚轮叙事页，`src/web/`） |
| 服务健康检查 | `GET /api/health` → 200，`model_available:false`（规则版路径），语料 3 主题 / 7 来源 / 26 证据 / 3 组分歧 |
| 工具 | agent-browser（CDP 驱动 Chrome，headless），逐视口 `set viewport` 后用页面内 JS 实测 |
| 判据 | `document.documentElement.scrollWidth == clientWidth == window.innerWidth`（文档级无横向滚动）；同时枚举全量元素 `getBoundingClientRect()` 越界者逐个归因 |
| 扫描位置 | 每视口至少扫首屏（#s1）与尾部面板（#s5），375 增扫 #s3（分歧面板，内容最密） |

## 二、三视口横向溢出实测（原始数据）

| 视口 | 扫描位 | innerWidth | docScroll | docClient | 结论 |
|---|---|---|---|---|---|
| **1440×900** | #s1 / #s5 | 1440 | **1440** | 1440 | ✅ 无溢出 |
| **768×900** | #s1 / #s5 | 768 | **768** | 768 | ✅ 无溢出 |
| **375×812** | #s1 / #s3 / #s5 | 375 | **375** | 375 | ✅ 无溢出 |

**越界元素归因（三视口唯一命中）**：`.drawer`（证据抽屉）及其子元素。
- CSS 依据：`src/web/css/main.css` L534 起 —— `.drawer { position: fixed; top: 0; right: 0; width: min(520px, 100vw); }`，屏外挂载为**设计行为**（未打开时 `aria-hidden="true"`，打开时滑入）。
- 关键证据：**文档 scrollWidth 始终等于视口宽**（fixed 定位元素不产生文档级横向滚动）→ 不构成横向溢出缺陷。
- 复核方式：三视口 × 多滚动位重复扫描，越界元素集合**恒等**且仅为该抽屉家族 → 排除其他布局缺陷。

## 三、`prefers-reduced-motion` 降级实测

模拟方式：`agent-browser set media reduced-motion` → 重新加载页面 → 页面内断言。

| 断言项 | 期望 | 实测 | 结论 |
|---|---|---|---|
| `matchMedia('(prefers-reduced-motion: reduce)').matches` | true | **true** | ✅ 模拟生效 |
| `#scene`（WebGL canvas）`display` | none | **none** | ✅ CSS 媒体查询生效（`main.css` L623-631） |
| `#scene-fallback` 兜底可见 | 显示 | **hidden=false** | ✅ 静态兜底版面接管 |
| `window.__sceneStatus` | ok=false 且注明原因 | `{ok:false, reason:"用户已开启「减少动效」→ 使用静态版面"}` | ✅ JS 降级路径生效（`scene.js` L309） |
| `#runtime-note` 运行状态标注 | 含「已降级」 | **「3D 场景：已降级（用户已开启「减少动效」→ 使用静态版面）」** | ✅ 用户可见、非静默降级 |

**静态版面下功能可用性抽测**：点击示例问题「5号线开通后客流多少？」→ 返回规则版回答（置信度 0.38 / 阈值 0.34，含证据编号 `ev-metro-02-p1`）→ **静态版面问答闭环完整可用**。

**恢复验证**：`set media light` 重载后 `__sceneStatus = {ok:true, reason:"运行中", nodes:39}` → 3D 路径恢复，降级不粘滞。

## 四、结论

1. **AC-OBS-09 三视口判据：通过** —— 1440 / 768 / 375 文档级零横向溢出。
2. **AC-OBS-09 降级判据：通过** —— `prefers-reduced-motion` 下 canvas 隐藏、静态兜底显示、状态显式注明降级原因（非静默）。
3. 降级为**双保险设计**：CSS 层（`@media (prefers-reduced-motion: reduce)` 隐藏 #scene）+ JS 层（`scene.js` 初始化前 matchMedia 检查 → `degrade()`），两层实测均触发。

## 五、证据文件清单（`06-运行实拍截图/`）

| 文件 | 内容 |
|---|---|
| `viewport-1440-s1.png` / `viewport-1440-s5.png` | 1440 首屏 / 尾部面板 |
| `viewport-768-s1.png` / `viewport-768-s5.png` | 768 首屏 / 尾部面板 |
| `viewport-375-s1.png` / `viewport-375-s3.png` / `viewport-375-s5.png` | 375 首屏 / 分歧面板 / 尾部面板 |
| `reduced-motion-1440.png` | reduced-motion 静态降级版面（1440） |

## 六、代码证据

- 降级判据入口：`src/web/js/scene.js` L25-27（`reducedMotion()`）、L309（初始化前检查 → `degrade()`）
- CSS 媒体查询：`src/web/css/main.css` L623-631（`@media (prefers-reduced-motion: reduce)`：隐藏 `#scene`、显示 `.scene-fallback`、动画时长归零）
- 抽屉设计定位：`src/web/css/main.css` L534-538
