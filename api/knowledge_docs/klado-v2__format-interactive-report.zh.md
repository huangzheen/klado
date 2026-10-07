---
document_id: klado-v2:format-interactive-report:zh
title: format-interactive-report
document_type: format
source: klado-workspace
version: 1.7
tags: [interactive, 互动, report, 报告, annotator, 标注, state, 状态, callout, 右键, 状态保存, 静态, 编辑, 热力图, 细分, 矩阵, segment, layout, 排版, checklist]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/format-interactive-report.md
---

> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造；业务数字必须走批准的只读数据源，不得凭印象编造。
> 本文是**报告形态**的选择与写法：**静态报告**（纯 HTML/CSS 快照）与**互动报告**
> （内嵌 JS + 状态存服务端）。两者共用同一套发布链路与排版规范 —— 骨架、16:9 deck 硬约定、
> 双语、密度与字号、页面配方、封面、发布字段，**一律以 `klado-v2:format-report-html:zh` 为准**，
> 本文只讲它没覆盖的部分（形态选型、状态接口、排版配料、静态报告的额外纪律）。

# 互动式报告与静态报告

## 0. 先选形态

| | **静态报告**（默认） | **互动报告** | **动态报告** |
| --- | --- | --- | --- |
| HTML 里有没有 JS | **没有**（纯 HTML/CSS） | 内嵌 `<script>` | 不需要（运行时注入；读者**只选筛选值**） |
| 读者能不能改内容 | 不能，看的是快照 | 能：右键标注、改颜色、改文字，**改动存服务端** | 不能。只有筛选选择按人保存 |
| 有没有备注层（右键） | ✅ 有 | ❌ 没有（右键归页面自己） | ❌ 没有（右键归切片运行时） |
| 跨电脑 / 分享给别人 | 天然一致（快照） | 一致（状态在服务端，不在浏览器） | 一致（选择在服务端，页面对所有人一致） |
| 什么时候用 | 分析结论、复盘、月报 —— **绝大多数情况** | 只有用户明确要"能点、能标、要留痕"的交互页 | 只有用户明确要"按时间段 / 销售值口径 / 区间切换看" |

**默认静态。** 形态选错的表现：给一份只要看的复盘塞了 JS（白担风险），给一份要集体标注的
页面做了静态（读者一刷新标注就没了），或者给一份要切换口径的报告做成两个几乎一样的文件。
动态报告的筛选器 schema、`data-filter-when` 切片语义与自检 →
**`klado-v2:format-dynamic-report:zh`**。

## 1. Workspace 互动报告：状态持久化接口

互动页的标注状态**不存浏览器**，存服务端，按报告的 `slug` 走：

```
GET  api/reports/{slug}/state    → 200，body 就是存的 JSON（没存过返回 {}）
POST api/reports/{slug}/state    → body 任意 JSON，整份覆盖，≤64KB
                                   → {"ok":true,"slug":"...","updated_at":"..."}
```

- ⚠️ **必须用相对路径** `api/reports/{slug}/state`，**不要写 root-absolute 的 `/api/...`** ——
  平台把应用挂在 `/` 子路径下，写成 `/api/...` 会落到域名根、被平台网关接走
  （回一个 200 的 JSON 错误体，看起来像"保存成功"）。报告里其它资源同理（见
  `klado-v2:format-report-html:zh` §1 的 `<base href>` 说明）。
- 两个接口都要求登录。浏览器自动附带会话 Cookie；报告 HTML **不要硬编码密码或 agent token**。
  只有私人工作区中报告的所有者能 POST。公共区是只读快照，其他用户须先点“拉取”取得独立副本。
- HTML 应带 `<meta name="report-kind" content="interactive">`，发布 JSON 也传 `"kind":"interactive"`。
  服务端会自动识别常见编辑控件，但显式标记能避免漏判。
- 服务端把状态当**黑盒**：不解析、不校验 schema、不做合并。**整份覆盖**（两个读者同时存 =
  后写覆盖先写，所以前端要做防抖），**没有版本历史**。超 64KB 返回 413，body 不是合法 JSON
  返回 400，`slug` 不存在返回 404。

### 前端流程（照抄）

1. 页面加载时 `fetch('api/reports/<slug>/state', {cache:'no-store', credentials:'same-origin'})` 拉状态；
   **非空就 `Object.assign` 覆盖默认值**（空对象 `{}` 时保持页面自己的默认内容）。
2. 任何编辑 **600ms 防抖后 POST 回整份状态**；不要让用户点"保存"按钮。
3. **不要用 localStorage。** 每个私人副本有自己的服务器状态，公共快照不受编辑影响。

### 可编辑 PPTX 与互动状态共用一份模型

新互动报告同时遵守 `klado-v2:format-pptx-export:zh`。HTML 中的
`<script type="application/json" data-report-deck-model>` 是初始模型；报告加载 `/state`
后调用 `window.reportDeckData.setState(savedState)`，再用
`window.reportDeckData.model` 绘制自定义图表、热力图和标注。
可编辑文字用 `data-deck-field="state.note"`，规则表格用 `data-deck-table="tables.matrix"`；
用户编辑后先更新模型，再按上面的 600ms 防抖规则保存**整份** state。
不要只修改 DOM：PPTX 会核对模型绑定与页面显示内容，不一致时明确拒绝导出。
如果报告用了自己的渲染器，也要在每次状态变化后同步设置 `window.reportDeckModel`
为最新的 v1 模型，让在线显示与 PPTX 读取同一份数据。

## 2. 互动报告 checklist

- [ ] **互动式报告必须可编辑（2026-09-28 定稿）**：标题 / 副标题以及关键标注文本一律
      `contenteditable`，blur 后走 600ms 防抖存 state —— "能看不能改"就不算互动报告。
      活例：`r/product-line-layout` 的 `titleEn/titleZh/subtitleEn/subtitleZh` 四字段。
- [ ] 所有交互元素有 hover 提示（右键、点色点）。
- [ ] 编辑后立即防抖保存，不要等用户点按钮。
- [ ] 空状态要有占位提示（`:empty:before`），否则空白格子看起来像坏了。
- [ ] 弹框 / 右键菜单用 `position:fixed`，`z-index` 1000+（不要被表格线或 deck 页码条盖住）。
- [ ] 不要在 HTML 里硬编码 agent token；浏览器会话 Cookie 会随同源 state 请求发送。
- [ ] **数据计算在前端做**（聚合、求和、范围标签），后端只存状态。

### 发布

- 和静态报告走**同一个** `POST /api/reports`。字段、封面、`submitter`、双语、deck 约定、
  "没有 `format` 入参 / `format_warning` 是服务端的判断" —— **owner 是
  `klado-v2:format-report-html:zh` §1–§2，本文不重复**。
- 互动页额外两件：HTML 带 `<meta name="report-kind" content="interactive">`，发布 JSON 传 `"kind":"interactive"`。
-  **允许内联 `<script>` 运行**（deck 运行时本身就是内联注入的，已实测），
  但**不要引任何外部 CDN 脚本** —— 导出/打印与离线查看都拉不到，且有 CSP 风险。
- **新 slug 用业务命名**（如 `segment-annotator`），**不要带 `demo`** —— 带 demo 的地址
  会被当成试验页，用户不会拿去分享。

## 3. 细分标注工具：排版配方（参照实现）

下面这组数值来自实际交付并验收过的细分标注页，做同类"细分矩阵 + 标注"页面时**直接照用**，
不要从零调。⚠️ 尺寸都是 **1920×1080 画布**上的值（deck 会等比缩放到窗口，屏幕上看是 ×0.7 左右），
下限纪律见 `klado-v2:format-report-html:zh` §1.7。

### 3.1 布局

- 单页 16:9，固定 **1920×1080** 容器，`overflow:hidden`，所有子元素 flex 自适应，不靠固定像素堆叠。
  （套进 `.deck` 的单个 `<section class="slide">` 就自带这套画布与缩放，不必自己写适配。）
- 左右双面板（线上蓝 / 线下橙），面板内：左边表格 + 右侧 callout 列。**表格占 58% 宽，callout 列 `flex:1`**。
- 表头 **54px**、body 行 **56px**、合计行 **46px**，三档固定高度；`table-layout:fixed` 保证等宽等高。
- callout 列用 `flex:1` 均分高度、和左边表格对齐：父级 **`align-items:stretch`**，不要用 `flex-start`。

### 3.2 字体（大会议室投屏标准）

| 元素 | 字号 |
| --- | --- |
| 页面大标题 | 38px |
| 副标题 | 20px |
| 面板标题 | 28px |
| 表格数字 | 23px |
| 表头 | 20px |
| 行标签 | 19px |
| 合计行 | 20px |
| callout 标题（名字） | 24px |
| callout 尺寸×价格行 | 22px |
| callout 描述 | 20px |
| 份额徽章 | 20px（灰底 `#9aa6b5`） |

### 3.3 颜色

- 热力图：**线上蓝 `#1d6fd6`、线下橙 `#e8620c`**，四档透明度 **14% / 3d% / 7a% / 100%**。
- 阈值：**≥15 最深、≥7 深、≥3 中、≥0.5 浅、<0.5 留白**。
- highlight 框色：**红 `#dc2626`（进攻）/ 绿 `#16a34a`（放手）/ 黄 `#eab308`（未来）**；
  **callout 名字的文字色 = 框色**，份额数字固定蓝 `#1d6fd6`。

### 3.4 callout 结构

- 三行：可编辑名字（`contenteditable`，颜色 = 框色） / 尺寸×价格 + 份额（黑色 + 蓝色） / 描述（`contenteditable`）。
- **自动聚合**：连续矩形格子 → 一行 `80cm × 5–9K · 29.6%`；不连续格子 → 每个格子一行
  `80cm × 12–15K · 4.0%`，行距收到 **1px**，看起来像一整体。
- **只渲染实际用到的 callout**，空的不占位。
- 连续外框技巧：相邻同组格子之间的表格线设 `border-color:transparent`，外边缘用
  `box-shadow: inset` 画 **5px** 粗边；格子加 `position:relative; z-index:2` 盖过网格线。

### 3.5 交互

- **右键格子** → 弹出 `Callout 1–5` + 清除。
- **点色点** → 弹色盘换色。
- 标题 / 副标题 / callout 名字 / 描述都是 `contenteditable`，直接点改。
- 每次编辑都按 §1 的流程存回服务端。

## 4. 静态报告：额外纪律

静态报告共用 `format-report-html` 的全部规则（deck、双语、密度自检、封面、`submitter`），
另加这几条：

- HTML 里**不要嵌 JS**，纯 HTML/CSS 即可 —— 渲染更快、封面截图更稳。
- **份额 / 结构类数字必须来自权威的只读数据源**（走账号可用的取数接口），不要自己用 SQL 重算
  —— 手搓的「价格段 × 宽度段」这类矩阵与权威口径常差 ±1pp，页面一旦被拿来对账就会露馅。
- **渠道内占比要自己除总销售额**：保留 **1 位小数**，**<0.1 留白**（不写 0.0%）。
- ⚠️ **网关偶发拦截**：取数接口被网关拦掉（405）时**退避重试 2–3 次**再判定失败。
- **发布前必须本地 headless Chrome 截图自检**（逐页看一眼留白与字号），**不要盲发**。
- **同 slug 重复发布就是覆盖**（幂等）。小修改**重发同一个 slug**，不要新建 slug 攒一堆近似卡片。
- 画表格边框连续性时，用 edge 单元格分别加 class（`zt`/`zb`/`zl`/`zr`），且 CSS 选择器要写到
  `.body table.mx td.xx` 这一级才能盖过 `table.mx td` 的默认 border。

## 5. 交付

两个形态的发布、封面、`submitter`、分享链接（`{BASE}/r/<slug>`）、错误码，**全部照
`klado-v2:format-report-html:zh` §2–§6 执行**，本文不重复。互动报告额外要做的只有两件：
① 按 §1 接上状态接口；② 发之前在浏览器里真的点一遍（右键、换色、改字、刷新后状态还在）。
