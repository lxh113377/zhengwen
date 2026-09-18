# CloudBase 部署方案（长期入口 · 可复跑）

> 建立：2026-09-19 ｜ 目的：提供**长期有效**的对外入口（用户红线：**禁止任何临时部署地址**）。
> 实测结论与失败路径**全部留痕**，便于评委/复核者按本文件独立复现。

---

## 一、为什么选 CloudBase（对比实测）

| 方案 | 成本 | 实时核证（出站） | 实测结论 |
|---|---|---|---|
| 临时沙箱（Cloud Studio 类） | 0 | — | 🔴 短时效、旧址实测 HTTP 400 → **已按用户红线永久停用** |
| PythonAnywhere 免费档 | 0 | ❌ **13 个实时源全部被平台白名单拦掉**（逐源 `Tunnel connection failed: 403`） | 站点可用，但实时核证不可用 → 仅作备选兜底 |
| PythonAnywhere Developer | $10/月 | ✅ | 未采用（成本） |
| **CloudBase 免费体验环境** | **0** | ✅ **不受限**（12 源正常返回，含央视网站内检索 / 360 资讯聚合） | ✅ **采用** |

## 二、环境与地址（长期有效）

| 项 | 值 |
|---|---|
| 环境 ID | `qwer-d4gf2r76o8829463b`（ap-shanghai，**体验版**） |
| 到期时间 | **2027-03-14**（覆盖 9/30 提交 → 10 月复赛 → 11 月总决赛；到期前可续） |
| **对外入口（L1）** | **`https://qwer-d4gf2r76o8829463b.service.tcloudbase.com/`** |
| 备选域名 | `https://qwer-d4gf2r76o8829463b-1458054906.ap-shanghai.app.tcloudbase.com/`（同路由，实测同样 200） |
| 云函数 | `zhengwen-api`（**Event 型**，运行时 `Python3.9`，handler `index.main_handler`，超时 60s / 512MB） |
| HTTP 访问路由 | 域 `*` + 路径 `/` + 上游类型 **SCF** + `zhengwen-api` |

## 三、复现步骤（3 条命令）

```bash
# 1) 从 src/ 生成函数代码目录（40 文件；含 LF 行尾的事件适配层 index.py）
python src/tools/cb_prepare.py

# 2) 部署（首次部署会自动创建 HTTP 访问路由；覆盖已存在函数必须 --force）
tcb fn deploy zhengwen-api --force

# 3) 冒烟自检（健康检查 + 一次真实提问 + 越界拒答对照）
python src/tools/cb_smoke.py        # 内部验证用；核心断言见下节
```

环境绑定见仓库根 `cloudbaserc.json`（`envId` / `functions[]` 的唯一来源，**不含任何密钥**）。

## 四、冒烟自检（2026-09-19 实测）

| 检查 | 结果 |
|---|---|
| `GET /` | 200（返回 3D 叙事页） |
| `GET /web/css/main.css`、`/web/js/app.js`、`/web/vendor/three.min.js` | 均 200 |
| `GET /api/graph` | 200，`stats = {topics: 7, sources: 32, evidence: 148, divergences: 4}`（与离线语料一致） |
| `GET /api/health` | 200，`readonly: true`、`sessions_tracked: 0`、12 可用源 / 19 项实测不可用 |
| `POST /api/ask`「加装电梯业主需要出多少钱？」 | 200，`refused: false`，**实时可核实 6 条 + 线索 5 条**，离线置信度 0.861，证据来自中国经济网 / 央视网，耗时 ≈6.8 s |
| `POST /api/ask`「帮我给这只猫起个名字吧」 | `refused: true`（**闸门未被放宽的对照**） |

> 免费体验环境**未注入模型凭据** → 线上走**规则版**并如实标注 `mode=rule`；凭据经环境变量注入后即可切 `mode=model`（密钥禁止落盘）。

## 五、踩坑留痕（全部为实测，不是推测）

1. **HTTP 访问路由只能由 `tcb fn deploy --path <path>` 自动创建**：两个域名都是「system internal domain」，
   `tcb routes add/edit` 一律被拒（`invalid custom domain` / `system internal domain`）。用 `--path` 部署即可自动建路由。
2. **HTTP 型函数（`scf_bootstrap` + 9000 端口）在体验版走不通**：路由被建成 SCF 类型后访问报
   `FUNCTIONS_PARAM_INVALID: FunctionType parameter is invalid`；手工改成 `WEB_SCF` 路由后持续返回 **HTTP 443**
   （函数 Web 服务未起来），且体验版默认**未开日志服务**（`tcb fn log` 报 `topic not exist`）无法定位 → 放弃该路径。
3. **改用 Event 型函数 + HTTP 事件适配层 `index.py`**（把云接入事件翻译给内部 `_run_request`，与本地同一套路由实现）。
   运行时 `Python3.9`：本项目全部模块带 `from __future__ import annotations`、无 3.10+ 运行时语法（已扫描核验）。
4. **函数目录结构必须扁平**：曾把数据与页面放进嵌套 `src/`，导致函数根目录的同名副本被优先导入 →
   页面 404、语料为空（而实时检索不依赖文件所以仍可用，掩盖了问题）。现由 `cb_prepare.py` 生成**单份扁平结构**。
5. **CLI 交互确认无法用管道喂 `y`**（`"Y" | tcb …` 会被当成 No/取消）→ 覆盖部署必须用 `--force`。

## 六、维护须知

- **到期续期**：体验环境 **2027-03-14** 到期，到期前在控制台续期（单次 6 个月）。
- **改代码后必须重跑** `cb_prepare.py`（生成物 ≠ `src/` 会导致线上与仓库不一致）+ `tcb fn deploy --force`。
- 静态快照站（L3）与本地运行不受本方案影响。
- ⚠️ **行尾口径**：仓库根 `.gitattributes` 统一按 **LF** 入库/检出，而本机工作副本与提交包内副本为 **CRLF** →
  两者**仅行尾不同**，逐字节 SHA256 会因此不同；比对时请先统一行尾（这是刻意选择：固定的 LF 口径可避免
  每次检出随机漂移，而部署与台账始终以本机工作副本为准）。
