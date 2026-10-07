---
document_id: klado-v2:format-report-html:zh
title: format-report-html
document_type: format
source: klado-workspace
version: 2.16
tags: [format-report-html, report, 报告, html, deck, 分页, 幻灯片, 16:9, workspace, 工作区, 发布, 分享, 公共区, cover, 封面, bilingual, 双语, submitter, pptx, dynamic]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/format-report-html.md
---

> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造；业务数字必须走批准的只读数据源，不得凭印象编造。
> 旧工具名（`search_knowledge` / `query_database` / `create_xlsx` …）是历史名，真实端点见该文档 §4。
> **报告与封面链路是真实可用的**（`POST /api/reports`、`/api/reports/cover`）—— 旧文档把它们列为退役，只是名字过时。

# HTML 报告书写规范

一句话：**你写一份自包含的 HTML，PUT 到 `/api/dashboard/{slug}`，它就变成当前账号仪表盘墙上的一张卡片**；
点开卡片看到的就是这份文档本身。

> ⚠️ **2026-10-05 起，HTML 一律交付到仪表盘（`/api/dashboard`），不再交付到 Workspace
> （`/api/reports`）。** 仪表盘不再只是"会自己查数据的活页面"——它现在是**所有 HTML 的落点**：
> 静态快照、互动页、动态筛选页都在这里。
>
> Workspace 仍然存在，但它现在**只放文件**（.pptx / .xlsx / .docx / .pdf，
> `POST /api/reports/documents`）。仪表盘里没有能存二进制的列，所以 Office 文档无处可去。
>
> 这条规则的两面：
> * **写 HTML → 仪表盘。** 读 `klado-v2:format-dashboard:zh`（发布接口、筛选、注解）。
> * **本文仍然管 HTML 长什么样** —— 双语、16:9 deck、密度、封面、提交人。规范没搬走，
>   只是交付的地方换了。

## 0. 两个模块的分工（先看这个，别弄反）

| | 仪表盘 `/api/dashboard` | Workspace `/api/reports` |
|---|---|---|
| 装什么 | **所有 HTML**：deck、长文、互动页、动态筛选页 | **只有文件**：.pptx / .xlsx / .docx / .pdf |
| 表 | `ai_dashboards` | `ai_reports` |
| 页面上 | `/d/{slug}` | `/r/{slug}`（文件卡片） |
| 谁在墙上 | 顶栏的 Dashboard 模块 | Workspace 模块 |

⚠️ 地址不同（`/d/` vs `/r/`），**slug 相同也不会互跳**。发给别人的链接按新地址给。

## 1. 交付形态

| 形态 | 怎么写 | 长什么样 |
| --- | --- | --- |
| **16:9 分页 deck**（**默认，一律用这个**） | 外层写 `<div class="deck">`，每页一个 `<section class="slide">` | 横版演示页，键盘 `↓` 整页翻页，每页底部有分页线与页码 |
| **长文报告**（仅例外） | 直接写普通 HTML | 一条可滚动的文档；**只有用户明确要求长文时才用** |

> ⚠️ 写 deck 之前**先读 §1.7 密度与可读性** —— 分页报告最常见的失败不是分页写错，
> 而是**每页内容太少 / 字号太小**（画布缩放后 11px 只剩 8px）。§1.8 有可直接复制的页面配方。

两种形态**可以混用在同一份文档里**（deck 之外的普通内容会随卡片一起进来，但通常没必要）。
⚠️ **2026-09-27 起：默认且唯一的形态就是 16:9 分页 deck。** 不写 `class="slide"` 就是长文，
分页运行时不会被注入；发布响应会带 `format: "long-form"` 与 `format_warning` —— 那是**缺陷信号**，
请改成 deck 重发。确需长文（用户明确要求）时，要在交付说明里写明原因。

### 16:9 deck 的硬约定

- 每页是**固定 1920×1080 设计画布**，运行时按窗口等比缩放，**不重排** —— 这样屏幕上看到的和导出/打印的逐像素一致。
- 一页一个 `<section class="slide">`；**不要自己写页码、分页符或 `<hr>`**，运行时会给每页自动加一条分页线 + 页码条（`3 / 12`）。
- 不要用 `vw` / `vh` / 百分比高度排版，按 1920×1080 的绝对值思考；一页装不下就**另起一页**，不要缩字号硬塞。
- 页面内边距由运行时给（上 76 / 左右 92 / 下 100px，下方留给页码条）——**不要覆盖 `.slide` 的 padding**。
- 可用的布局类（全部可被你自己样式覆盖）：`.s-kicker` `.s-title` `.s-sub` `.s-lead` `.s-body` `.s-cols(-3/-4)` `.s-kpis/.s-kpi` `.s-card` `.s-note` `.s-table` `.s-code` `.s-chip` `.s-hero` `.s-swatch`，以及密度组件 `.s-bars/.s-bar` `.s-meter` `.s-dn/.s-up`（见 §1.7.4）。
- `data-deck-title="…"` 会显示在每页页码条上；当前页会写进 URL 的 `#p3`，因此 `…/r/<slug>#p4` 这类链接能直接定位到第 4 页。

### 两种形态都适用的内容规则

- **资源用相对路径**：服务端会给文档注入 `<base href>`（线上 `/`），所以 `logo.png`、`api/products` 这类相对写法在本地预览 / test / 生产都能解析。**不要**写带挂载点的 `/...` 绝对路径。
- **图片走对象存储**（`product-images/…`，用 `api/storage/serve?path=…`）；不要把大图 base64 塞进 HTML。
- **免登录分享**只能读报告正文或保存状态里明确引用的对象存储图片，不能调用其它受保护的实时 API；如需让任何人打开后看到完整内容，把必要的数据写进 HTML 或保存状态。
- **deck 字体用项目内置字族** `Roboto Condensed` / `CoolSans SC Narrow`（运行时 CSS 已加载）；
  不要引外链字体。PPTX 会嵌入这两套字体，其它字体不能假设收件人的 PowerPoint 已安装。
- 单份 HTML **≤ 8 MB**。
- 报告先落到当前账号的私人工作区；要更新原稿就**重发同一个 slug**（覆盖更新，`updated_at` 刷新，浏览量保留）。公共区为单独快照，更新原稿后须由所有者重新发布。⚠️ 更新**必须**带上首次发布响应里的 `slug` —— 不带 slug 会生成新卡片。

### 另一种分类：静态 / 互动

上面讲的是**版面**（deck / 长文）。还有一个**正交**的分类：

| | 静态（默认） | 互动 | **动态** |
| --- | --- | --- | --- |
| HTML 里有 JS | 没有，纯 HTML/CSS | 内嵌 `<script>`，读者能右键标注、改字 | 不需要（运行时会注入；读者只**选筛选值**） |
| 读者的改动 | —— | **存服务端**，按 `slug` 走 `GET/POST /api/reports/{slug}/state` | 不写文档；只有**按人保存的筛选选择**（`PUT /api/reports/{slug}/filters/selection`） |
| 一个文档多个视图 | —— | —— | ✅ `data-filter-when="period=2026-06"` 声明切片，边栏切换 |
| 同事的备注 | 文档内右键 | 互动页用自己的右键菜单（没有备注层） | **没有备注层**（同样是右键菜单冲突） |

**默认静态。** 只有用户明确要"能点、能标、要留痕"时才做互动页；只有明确要"按时间段/口径/区间
切换看同一份分析"时才做动态页。三个形态共用本文的全部规则（deck 硬约定、双语、密度自检、封面、
`submitter`、发布字段、`/r/{slug}`）。

- 互动页的状态接口、前端流程、checklist、细分标注页排版配料 → **`klado-v2:format-interactive-report:zh`**
- 动态页的筛选器 schema、`data-filter-when` 语义、边栏行为、自检 → **`klado-v2:format-dynamic-report:zh`**
- 想要的是**文件本身**（.pptx/.xlsx/.pdf/.docx）而不是网页 → **`klado-v2:format-workspace-documents:zh`**

⚠️ 三种 HTML 形态都**不是**给"交个 Office 文件"用的：卡片可以是文档（`POST /api/reports/documents`），
但那是一条独立的链路，别把 `.pptx` 的内容硬塞进 HTML 假装是文件。

⚠️ **没有 `format` 入参**：是不是 deck 由 HTML 自己决定（写了 `<section class="slide">` 就是）。
发布响应里的 `format` / `format_warning` 是**服务端回给你的判断**。

## 1.5 双语：一份文档同时装两种语言（**必须做**）

每份对外报告都要有**英文与中文两版**，装在**同一个 HTML 文件**里。默认显示**英文**。

### 怎么标

给每一块翻译内容加 `data-lang`：

```html
<!-- deck：一页一个 data-lang；英中各自成套 -->
<div class="deck" data-deck-title="Q3 Channel Deep Dive">
  <section class="slide" data-lang="en">…English page 1…</section>
  <section class="slide" data-lang="en">…English page 2…</section>
  <section class="slide" data-lang="zh">…中文第 1 页…</section>
  <section class="slide" data-lang="zh">…中文第 2 页…</section>
</div>

<!-- 长文：任意块级元素都能带 data-lang -->
<h1 data-lang="en">Channel Deep Dive</h1>
<h1 data-lang="zh">渠道深度复盘</h1>
<p data-lang="en">13 channel codes are in scope.</p>
<p data-lang="zh">范围内共 13 个渠道代码。</p>
<p>This paragraph has no data-lang — it shows in BOTH languages.</p>
```

规则：

- **不带 `data-lang` 的内容是共享的**（两种语言都显示）——适用于封面 logo、脚注、图表标题这类无需翻译的东西。
- **顺序**：先 EN 后 ZH。语言过滤是运行时的，但导出/打印会按文档顺序，EN 在前读起来最顺。
- **页码按语言各算一套**：6 页英文 + 6 页中文，在任一语言里都显示 `3 / 6`，不会变成 `3 / 12`。
- 英文版**不要重排成另一种版式**——同一页、同一视觉结构，只换文字。
- **封面图不写字**（见 §3）：一张无字封面同时服务两种语言。
- ⚠️ **改一份已发布的报告，必须同时改两版**：修改要在**同一次** `PUT` / 重发里把 EN 和 ZH 一起改到，
  不允许只改你当前打开的那一版。只改一版会留下一份"英文写着 A、中文还写着 B"的文档 ——
  两个读者拿到不同结论，而且没有任何东西会提醒你。**新增一页 = 两页（EN + ZH）；删一页 = 删两页；
  改一个数 = 两处都改。** 定稿前自检：EN 与 ZH 页数相同、同一页讲同一件事、同一个数。

### 摘要（`summary`）也要双语 —— 双语报告必填，缺一即 400

卡片悬停时看到的**摘要**同样要两种语言各一行：

| 字段 | 什么时候用 |
|---|---|
| `summary_en` | 英文一行摘要 |
| `summary_zh` | 中文一行摘要 |
| `summary` | **单语报告**用；双语报告可以不传，服务端会取 `summary_en` |

- ⚠️ **只要文档是双语的（`data-lang` 里 en 和 zh 都在），`summary_en` 与 `summary_zh` 就都是必填**：
  缺任何一个，`POST /api/reports` 直接回 **400**（报错正文会点名缺哪个字段）。只传 `summary`
  不够 —— 那等于给中文读者配了英文摘要。`PUT` 在**改了 `html` 或任何 summary 字段**时同样校验；
  单纯改名不会触发。
- 卡片悬停幕布上会**同时显示这两行**（EN 一行、中文一行），所以两行都要写得像"一句话"，不要写成两句话。
- 已有存量报告没有这一对 —— **下一次重发/改内容时补上**，那时会要求它。

### 读者怎么选语言

- **默认英文**：`/r/{slug}`（或 `/api/reports/{slug}/raw`）不带参数就是 `en`。
- 公共区副本 `/r/{public-slug}` 和指定同事收到的私人原稿 `/r/{slug}` 都要求接收者先注册并登录。所有者也可以从 Workspace 右键菜单生成免登录、可撤销的 `/s/{token}` 链接；不要自行拼造这个 token。
- `?lang=zh` 切中文；运行时也会把选择写回 URL，链接可分享。
- 在 Workspace 里**点卡片会先问语言**（英中二选一，预选是上次的选择；只有双语报告才会问，单语报告直接打开）。
- 文档内部也有切换控件：deck 在底部工具条上有 `EN | 中文`，长文报告右下角有浮动切换。
- 焦点在报告里按 `ESC` 是交回应用（关查看器），不是切语言。

## 1.6 术语：必须参照知识库，不要自己翻译

英文版里出现的业务术语、字段名、渠道名、指标缩写，**一律使用知识库里既定的英文说法**，
不要自创译名，也不要同一术语两种译法。

| 要翻译什么 | 查哪份文档 |
| --- | --- |
| 数字、百分比、日期、单位、口径的写法 | `klado-v2:format-output-standards:zh` |
| 图表配色 / 字体 / 图型 | `klado-v2:format-chart-style:zh` |

用法：先检索再动笔 ——

```
GET /api/ai/knowledge/search?q=<术语>&top_k=5
GET /api/ai/knowledge/document/klado-v2:format-output-standards:zh
```

拿不准时**用知识库里的原文**，不要意译。中英两版必须指同一个东西、给同一个数。

## 1.7 密度与可读性（硬规则 —— 写之前先读这一节）

背景，先想清楚再看写什么：deck 是**固定 1920×1080 画布，整体缩放到窗口**（常见 1280–1600 宽 → 约 ×0.66–0.75）。
所以画布上的字号到屏幕上要**打七折**：**18px 看起来只有 12–13px，11px 只有 8px。**

🔴 报告翻车只有两种形态：**半空的页**（一页只有 3 行，大片留白）和**塞满小字的页**（9 个月数据挤在 13px 的手写条形里）。
**"字小"不等于"信息多"，字小 = 看不见。** 密度靠"每页一个论点 + 撑得住的证据"，不靠缩字号。

### 1.7.1 字号下限（画布 px）

| 用途 | 下限 | 建议 |
| --- | --- | --- |
| 正文 / 表格单元格 | **18px** | 20–22px |
| 表格表头 / `.s-kicker` / 图例 | **14px** | 15–16px |
| 图表数据标签 / `.s-note` 脚注 | **16px** | 18px |
| KPI 数值 `.s-kpi-v` | 40px | 46–64px |

- **绝不要出现 11–13px**，也不要写 inline `font-size` 去压小字号 —— 装不下就**另起一页**。
- 不确定用多大就用 kit 的类：`.s-table` 19px / `.s-note` 18px / `.s-kpi-v` 46px / 页面根字号 18px —— 它们都在安全线上。

### 1.7.2 一页该装多少

内容区是 **1736 × 904**（1920×1080 减去 76/92/100 内边距）。

| 页面 | 目标内容量 |
| --- | --- |
| 封面 | 标题 + scope 一行 + **一句结论**（别只放三行字就翻过去） |
| 结论页 | **4–8 个 KPI**（排成两行）+ 一句结论 |
| 数据页 | **一张 ≥ 8 行的表**，或 2–4 组并排的图表 |
| 明细页 | **10–15 行**表格（Top-N 至少给 10 行） |
| 归因 / 建议页 | 3–4 张卡片，每条都带支撑数字 |

- **留白自检（两条都要过）**：
  1. **最大连续空白** —— 在 904px 的内容区里找**最长的一段完全空白**（既没有文字、
     也没有条形/色块这类图形）：数据 / 明细 / 趋势 / 结构页 **≤ 15%**，
     结论 / 归因 / 卡片页 **≤ 25%**，封面 **≤ 30%**；**任何一页 ≥ 35% 一律不合格。**
  2. **文字行数**：内容页 **≥ 20 行**，数据页 **≥ 30 行**，封面 **≥ 15 行**。
- 不达标怎么办：把相邻两页并成一页，或补上对照数据（对比期、占比、YoY、排名、目标、
  累计集中度 CR1–CR5）；反过来，一张表超过 ~15 行就分页，不要缩行高。
- ⚠️ **别用"最后一条元素的底边"当留白指标**：页面底部贴一条注释就能把范围一直拉到页脚，
  **半空的页也会显示"填满"**。要找的是**最长的那段空白**，而且要**逐页**量（分页文档只有当前页是可见的）。
- **双语不增加页数**：EN / ZH 是**同一页的两种语言**，先把每页做厚再复制一份；不要出现"两种语言 × 一堆空页"。

> **实测对照**（同一份渠道数据的两种写法）：发布前那一版 6 页平均最大空白 **52%**、最差 **82%**，
> 每页另有 18–24 处 11–13px 小字；按本节重排后 7 页平均 **13%**、最差 **15%**，字号越界 **0 处**。
> 最直观的一条：**原版封面 82% 是空白** —— 整页只有三行字。差距不在"信息不够"，
> 而在**信息没有被排到页面上**。

### 1.7.3 每页必须"论点 + 证据"

- **标题写结论，不写栏目名**。`YTD sell-in is down 56% YoY` ✅；`Monthly sell-in` ❌（那只能当 `.s-kicker`）。
- **每个数字都要有参照**：YoY / 占比 / 排名 / 目标 / 上一期。孤立的绝对值读者无法判断好坏。
- **让读者能自己核对**：说了"下滑是全月性的"，就得给带 YoY 列的月份表；说了"集中度风险"，就得给 Top 10 + 占比。
- **一条结论只说一次**，不要在 5 页里重复同一句话。
- 表格末尾给**合计行**（YTD / Total）—— 读者最先找的就是那一行。

### 1.7.4 图表用配方，不要手搓

> **配色 / 字体 / 图型选型看专门的规范：`klado-v2:format-chart-style:zh`**
> （一套中性色板与内置字族 `Roboto Condensed` + `CoolSans SC Narrow`、
> 数据形状→图型选型表、可复制的折线/点阵/哑铃/堆叠/瀑布配方、图表自检）。
> 本节只讲"密度与字号"这一层。

手写 `<div style="height:10px;width:71%">` 是最典型的坏味道：缩放后看不见、还没有轴。
运行时已内置密度友好的组件（`.s-bars` / `.s-bar` / `.s-meter` / `.s-dn` / `.s-up`），条高 14–18px（画布 px）、标签 20px，直接用就不会踩坑：

```html
<!-- 配方 A · 对比条：每月 / 每渠道 / 每 SKU 一行 -->
<div class="s-bars">
  <div class="s-bar"><span class="k">Mar</span>
    <span class="t"><i style="width:100%"></i><i class="now" style="width:57%"></i></span>
    <span class="v"><b>99</b> → <b>56</b> <b class="dn">-43%</b></span>
  </div>
</div>

<!-- 配方 B · 表格里嵌条形：数字、YoY、占比同一行，一页可放 12 行 -->
<table class="s-table">
  <tr><td>Mar</td><td class="n">99</td><td class="n">56</td><td class="n s-dn">-43%</td>
      <td style="width:34%"><span class="s-meter" style="--v:57%"></span></td></tr>
</table>
```

选型顺序：**4 个数以内 → KPI 卡**；**单维排名 / 跨期对比 → 配方 B**；**需要肉眼比长度 → 配方 A**；**多维交叉 → 2–3 栏小表**。能不上图就别上图。

## 1.8 页面配方（复制即用）

下面每一种配方都写明了「什么时候用」和「密度下限」，逐条照抄即可。

**① 封面** —— 下半页不留白，放三个最重要的 KPI：

```html
<section class="slide" data-lang="en">
  <div class="s-kicker">CHANNEL · 2026 YTD</div>
  <h1 class="s-title">Channel Sales Review</h1>
  <p class="s-sub">Sell-in (GTO) through Sep 2026 · invoice basis · Offline + Online</p>
  <div class="s-body" style="justify-content:flex-end">
    <div class="s-kpis" style="grid-template-columns:repeat(3,1fr)">
      <div class="s-kpi"><div class="s-kpi-l">2026 YTD GTO</div><div class="s-kpi-v">¥212.9M</div><div class="s-kpi-d dn">-56.0% YoY</div></div>
      <div class="s-kpi"><div class="s-kpi-l">Units</div><div class="s-kpi-v">31,251</div><div class="s-kpi-d dn">-48.6% YoY</div></div>
      <div class="s-kpi"><div class="s-kpi-l">ASP</div><div class="s-kpi-v">¥6,813</div><div class="s-kpi-d">+14% vs 2025</div></div>
    </div>
  </div>
</section>
```

**② 结论页** —— 8 个 KPI 排两行（**必须显式指定列数**，否则 `.s-kpis` 会自动铺成一行）：

```html
<div class="s-body">
  <div class="s-kpis" style="grid-template-columns:repeat(4,1fr)"> …8 个 .s-kpi… </div>
  <div class="s-note">2025 same period: ¥484.2M / 60,767 units. Peak months Mar / Jun / Sep all under half of 2025.</div>
</div>
```

**③ 趋势页**（主力配方：表格 + YoY 列 + 内嵌条 + 合计行，12 行正好一页）：

```html
<div class="s-body">
  <table class="s-table">
    <thead><tr><th>Month</th><th class="n">2025</th><th class="n">2026</th><th class="n">YoY</th><th style="width:30%">2026 vs 2025</th></tr></thead>
    <tbody>
      <tr><td>Mar</td><td class="n">99</td><td class="n">56</td><td class="n s-dn">-43%</td><td><span class="s-meter" style="--v:57%"></span></td></tr>
      <!-- … -->
      <tr><td><b>YTD</b></td><td class="n"><b>485</b></td><td class="n"><b>213</b></td><td class="n s-dn"><b>-56%</b></td><td><span class="s-meter" style="--v:44%"></span></td></tr>
    </tbody>
  </table>
</div>
```

**④ 结构页**（三个维度并排，每栏一张小表；标题用 `.s-card-h`）：

```html
<div class="s-body s-cols-3">
  <div><div class="s-card-h">By operation channel</div><table class="s-table"> <!-- 4–6 行 --> </table></div>
  <div><div class="s-card-h">By brand</div><table class="s-table"> … </table></div>
  <div><div class="s-card-h">By value class</div><table class="s-table"> … </table></div>
</div>
```

**⑤ 明细页**（Top-N ≥ 10 行，最后一列放占比条）：

```html
<thead><tr><th>VIB</th><th>Brand</th><th class="n">Units</th><th class="n">GTO</th><th class="n">Share</th><th style="width:22%"></th></tr></thead>
<tr><td>SKU-0001</td><td>Brand A</td><td class="n">5,818</td><td class="n">¥39.1M</td><td class="n">18.4%</td>
    <td><span class="s-meter" style="--v:100%"></span></td></tr>
```

**⑥ 归因 / 建议页**（左"数据说了什么"、右"要做什么/盯什么"，每条带数字）：

```html
<div class="s-body s-cols">
  <div class="s-card"><div class="s-card-h">What the data says</div>
    <ul style="margin:0;padding-left:24px;font-size:20px;line-height:1.7;color:var(--deck-mid)">
      <li>…每条都带数字…</li>
    </ul>
  </div>
  <div class="s-card"><div class="s-card-h">What to do / watch</div> … </div>
</div>
```

**⑦ 附页（口径与来源）** —— 数据源、期间、筛选条件、口径与注意事项、快照时点：

```html
<div class="s-body">
  <table class="s-table">
    <tr><td>Source</td><td>sales_invoice</td></tr>
    <tr><td>Scope</td><td>category = '…' · channel = offline + online</td></tr>
    <tr><td>Period</td><td>2026-01 … 2026-09, compared with the same months of 2025</td></tr>
    <tr><td>Basis</td><td>sell-in / GTO, invoice basis</td></tr>
    <tr><td>Snapshot</td><td>frozen at publish time · 2026-09-26</td></tr>
  </table>
  <div class="s-note">…口径差异 / 数据缺口说明…</div>
</div>
```

一份完整报告推荐页序：**封面 → 结论 → 趋势 → 结构 → 明细 → 归因/建议 → 口径附页**（EN 一套 + ZH 一套）。

## 1.9 发布前自检（逐条勾，不合格就改）

1. **每页最大连续空白**达标？数据 / 明细 / 趋势 / 结构页 ≤ 15%、结论 / 归因 / 卡片页 ≤ 25%、
   封面 ≤ 30%，**任何页 ≥ 35% 不合格**；行数：内容页 ≥ 20、数据页 ≥ 30、封面 ≥ 15。
   （只有 1–3 行的页，要么合并，要么补对照数据）
2. 画布上没有任何 **< 18px** 的字（尤其是 inline 的 `font-size:11px` / `13px`）？
3. 每页标题是**结论句**，不是栏目名？
4. 每个关键数字都有**参照**（YoY / 占比 / 排名 / 目标 / 上期）？
5. 数据页 ≥ 8 行、明细/Top-N 页 ≥ 10 行？
6. 结论页的每条结论，都能在后面某页找到支撑表？
7. 图表用的是配方 A/B，没有手搓的 10px 条形？
8. 双语：EN/ZH 页数一致，且没有因为双语产生空页？
9. 需要可编辑 PPTX 时，是否按 `klado-v2:format-pptx-export:zh` 为图表、表格、徽章
   和互动状态提供标记及同一份数据模型？是否分别导出 EN/ZH 检查？

## 1.10 页面结构（标题区 / 洞察 / 内容 / 页脚）—— 不要页眉

**这是报告页面布局的唯一 owner** —— `format-chart-style` §4 也指向这里，避免同一件事两处说法。

| 区块 | 高度（1920×1080 画布） | 内容 |
| --- | --- | --- |
| **标题区** | 自适应 | 页面顶部**只有主标题 + 副标题**。**不要页眉**：章节/工作流标签、页眉页码、页眉细线一律不放 |
| **洞察区** | 自适应 | 本页核心结论（一句话）：**墨色标题 + 左侧 6px 强调色竖条** |
| **内容区** | 自适应 | ≥2 个图表面板，或 4 个独立区域 |
| **页脚区** | 40px | 来源 · 时间范围 · 过滤条件 |

- **不要页眉（2026-09-28 定稿）**：早期版本的 44px 页眉区（章节标签 + 页码 + 细线）已废除——
  顶部就主标题 + 副标题，然后直接进洞察/内容，不要再画一条"页眉横线"。
- 像素值按 **1920×1080 画布**给（deck 会等比缩放到窗口，常见 ≈×0.7）。
  ⚠️ `format-html-slides` §4 那组 32px/21px 是**更小画布**的值，**不要照抄**。
- **页码完全交给运行时**：deck 运行时会在每页底部自动加分页线 + 页码条（`3 / 12`），
  报告里**不要自己再画任何页码**（没有页眉页码，也不要自己补页码徽章），避免出现两个页码。
- **页脚承载口径的公共部分**（来源 / 期间 / 过滤）；图表面板内只写该图**特有**的口径（指标 / 分母）。
- 洞察区**标题用墨色 + 强调色左竖条**，不要整行用强调色 —— 强调色与语义色会互相抢，
  理由见 `klado-v2:format-chart-style:zh` §2.3。

## 1.11 可编辑 PPTX：写 HTML 时就提供语义和数据

**新报告要读 `klado-v2:format-pptx-export:zh`**。它规定项目字体、
`data-pptx-role` / `data-pptx-export` / `data-pptx-chart-ref` 等标记，
以及 `data-report-deck-model` v1 单一数据模型。可选的 `data-deck-field` / `data-deck-table`
会从模型直接生成网页文字和表格，导出时再核对，避免读者改过内容却导出旧稿。
旧报告无需这些标记也能导出；但要让图表在 PowerPoint 的「编辑数据」里修改，
必须提供类别、系列、数值，而不能只画 CSS 色块。

## 2. 发到服务器的接口

⚠️ 下面这张表是**仪表盘**的。Workspace 只剩文件卡片（`POST /api/reports/documents`），
完整说明见 `klado-v2:format-workspace-documents:zh`。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `PUT` | **`/api/dashboard/{slug}`** | **新建或覆盖一个页面 —— HTML 的交付入口**（`slug` 是 URL，不可改；地址被占且属于别人 = 409） |
| `GET` | `/api/dashboard?scope=mine\|public\|shared` | 卡片列表（只含元数据，HTML 按需再取）；默认 `mine` |
| `GET` | `/api/dashboard/{slug}` | 一张卡片 **+ 它的 `html`**（agent 改页前先取回来） |
| `GET` | **`/d/{slug}`** | 页面自己的地址，需登录；私有页仅所有者可看 |
| `GET` | `/api/dashboard/{slug}/cover` | 封面图 |
| `GET/PUT/DELETE` | **`/api/dashboard/{slug}/filters`** | 这个页的**筛选器定义**（见 `klado-v2:format-dynamic-report:zh`） |
| `PUT` | `/api/dashboard/{slug}/filters/selection` | **读者自己的**筛选取值；必须浏览器会话（agent 403） |
| `GET`/`POST` | `/api/dashboard/{slug}/state` | **互动页**的标注状态；≤64KB |
| `GET/POST/PATCH/DELETE` | `/api/annotations?type=dashboard\|report\|knowledge\|event&slug=` | 同事备注。**agent 凭据可读** —— 改页前先读它 |
| `GET/POST/DELETE` | `/api/dashboard/{slug}/shares…` | 分享与撤销（写操作必须浏览器会话） |
| `POST` | `/api/dashboard/query` | 页面运行时自己查数据集用（浏览器会话） |
| `DELETE` | `/api/dashboard/{slug}` | 删除（连同分享与读者视图） |

⚠️ **2026-09-26 起，所有 `/api/*` 都要登录**（防未授权访问）：

```bash
curl -u 'you@example.com:<password>' "$BASE/api/dashboard?scope=mine"
# 或：-H "Authorization: Basic $(printf 'you@example.com:<password>' | base64)"
```

缺凭证 **401**、账号被禁用 **403**。
也**不要**把密码之类的东西写进页面 HTML。

`PUT /api/dashboard/{slug}` 请求体：

页面归属**由请求凭据对应的账号决定**，不能靠请求体指定归属。
替用户发布的 agent 应使用该用户自己的授权码，不能用另一个账号的凭据代发。

```jsonc
{
  "title":   "Q3 Channel Deep Dive",  // 必填，卡片标题
  "summary": "一句话结论 —— 悬停卡片时显示的唯一文字，写结论别写目录",
  "summary_zh": "中文摘要",           // 双语页必填：卡片按读者语言取这一对
  "summary_en": "English summary",
  "langs":   "en,zh",                 // 逗号分隔；空 = 未声明
  "description": "可选，一句话说明",
  "tags":    "channel,q3",
  "submitter": "Zhen Huang",          // ← **从飞书来的必须填**：谁要的这份页面（英文名或拼音，不是中文名）
  "accent":  "accent",                // theme.css 的 token 名；写 #ff8800 会被 400 拒
  "html":    "<!doctype html>…",      // 必填，完整 HTML 文档
  "status":  "published",             // published(默认) | draft | archived
  "visibility": "private",            // private(默认) | public（public 必须浏览器会话）
  "pinned":  false,
  "in_nav":  false, "nav_order": 0,   // 进顶栏自己的入口
  "in_home": false, "home_order": 0,  // 进首页的容器
  "datasets": ["band_monthly_2026"],  // 页面允许自己查的数据集（不需要查就别给）

  // 三种封面写法，任选其一（都不给就没有封面，卡片显示占位外观）
  // "cover_base64": "…",             // ← **首选**：你自己的生图模型出的图（16:9，无水印）
  // "cover_url":    "https://…"      // ← 图片已经在外部托管（"" = 清空封面）
  "generate_cover": true,             // ← 兜底：让服务器用 MiniMax 生图（见 §3）
  "cover_ratio":    "16:9",
  "cover_with_title": false,
  "cover_seed":     12345,            // 可选，复现同一张封面
}
```

⚠️ **新建必须带封面**，否则 400：一张没有图的卡片是墙上什么都不说的灰块。
`PUT` 是**合并语义**——没提到的字段保持原值，所以重发只带 `html` 不会把标题、封面、放置标记清掉。

**幂等**：同一个 `slug` 重发就是覆盖更新，可以放心重跑。

> **`summary` 怎么写**：卡片只有封面 + 标题，**悬停时才显示这一个字段** ——
> 所以它要写**结论/看点**（"份额差收窄到 0.3pp，主要渠道同时失守"），不要写
> "本报告分析了…"这类目录式开场。控制在 1–2 句、约 60 字以内，超过 3 行会被截断。

## 2.5 提交人（`submitter`）——**从飞书来的必须填**

仪表盘卡片会展示提交人，所以交付时**必须带上提交人**：

```jsonc
{ "submitter": "Zhen Huang" }
```

- 值用**英文名或拼音**（例如 `Zhen Huang` 或 `Huang Zhen`）—— **不要用中文名**，界面是英文优先的，中文名在卡片副行里也容易被截断。
- 首选飞书资料里的英文名；没有就用拼音。重名时补一句 `Zhen Huang (ou_xxxxxxxx)`。
- 提交人**不要**再写进 `summary`。
- ⚠️ 2026-10-05 补记：`ai_dashboards` 早期**没有** `submitter` 这一列，所以
  `PUT /api/dashboard/{slug}` 会接受这个字段然后**静默丢掉**——请求 200、卡片上没有提交人。
  现在列已补上（`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`），行为与 Workspace 一致。
  这条记在这里是因为它是本仓库明令避免的那一类：**「屏上有效、保存无效」的控件／字段比没有更糟。**

## 3. 封面图：首选 agent 自己的模型生图，服务器 MiniMax 仅兜底

卡片是「**封面图 + 标题**」的形态，摘要只在悬停时出现。

**首选路径（2026-09-30 起）**：用**你自己（agent）的生图模型**出图——飞书 agent 支持
**文生图与图生图**（用户给了参考图/产品图时直接以图生图）——然后把图作为
`cover_base64` 随发布请求一起发。要求：**16:9**（1280×720 或同比例）、
配色用固定中性色板（见 §3.5）、**图上不得有任何水印/角标/AI 标识**（生图参数能关水印就显式关掉）。
提示词写法见 `klado-v2:media-image-prompt:zh`。

**兜底路径**：你的运行环境完全没有生图能力时，由部署侧调 **MiniMax** 生成
（发行方无需持有任何图像 API key）：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `POST` | `/api/reports/cover` | 只生图，返回 `image_base64`（无状态，可自行保存或复用） |

```jsonc
// POST /api/reports/cover
{ "title": "Q3 渠道深度复盘 — 份额差在收窄", "ratio": "16:9", "with_title": false }
// → { "prompt": "…实际使用的提示词…", "image_base64": "…", "mime": "image/jpeg",
//     "width": 1280, "height": 720, "model": "image-01", "aspect_ratio": "16:9" }
```

- **比例固定用 `16:9`**：卡片封面框就是 16:9（1280×720），别的比例会被裁切。
- **默认不要把标题画进图里**（`with_title: false`）：卡片会自己渲染标题，图里再画一遍就重复了。
  `with_title: true` 是「杂志封面」变体，只有把图单独当 KV/海报用时才用。
- 生成一次约 **30 秒**，发布时带上 `generate_cover: true` 会让整个发布请求等这么久；想控制节奏就先调
  `/api/reports/cover` 拿到图，再带着 `cover_base64` 发布。
- 提示词的完整规则见知识库文档 `klado-v2:media-image-prompt:zh`。

失败时怎么排查：接口的错误信息里带 MiniMax 的 `base_resp.status_code` ——
`1008` 余额不足、`1026` 描述涉及敏感内容（改写标题/补充要求后重试）、`2013` 参数异常（多半是提示词超过 1500 字符上限）、`1002` 限流稍后重试。

## 3.5 封面字段名：**必须和 `html` 在同一次发布里发**

**真实踩过的坑**：先单独调 `POST /api/reports/cover` 拿到图，然后发布时**忘了把图带上**
（或者字段名写错），结果报告发出来了、卡片上空着 —— 因为图从来没进库。

| 要传什么 | 字段名（认这些写法） |
| --- | --- |
| 图片 base64（自己出图 / 从 `/cover` 拿到的图） | `cover_base64` · `coverBase64` · `cover_image` · `coverImage` · `image_base64` · `imageBase64` |
| 外链图片 | `cover_url` · `coverUrl` · `image_url` · `imageUrl` |
| 让服务器生图 | `generate_cover: true` · `generateCover` |
| 比例 / 是否把标题画进图 | `cover_ratio`（`aspect_ratio` 也认）· `cover_with_title` |

> **封面配色是固定的中性色板**（冷白底 + 石墨黑文字 + 低饱和深石墨蓝主色），
> 不随业务或品牌变化；旧的 `brand` / `cover_brand` / `palette` 参数已不再影响配色。详见
> `klado-v2:media-image-prompt:zh` §报告封面。

- **一次调用搞定最省事**：发布时带上 `"cover_base64": "<你自己模型出的 16:9 无水印封面>"`，
  图随 `html` 一起落库，不会有"图在外面、卡片空着"的中间态。
- 兜底两段式（先 `/cover` 拿 MiniMax 图、再发布）也可以，但**第二步必须带 `cover_base64`**；
  有生图能力的 agent 也可以在发布里带 `"generate_cover": true` 让服务器一步生成——
  会多等约 30 秒，仅限你没有生图能力或用户点名要服务器出图时。
- 传了**无法识别**的封面字段（如 `cover_png`、`image`、`thumbnail`）接口会 **400** 报错，
  不再静默忽略 —— 这是刻意做的：宁可报错，也不要你以为发了、卡片却空着。
- 事后补封面：`PUT /api/reports/{slug}` 带 `cover_base64` / `generate_cover` 即可（同 slug 幂等）。
  ⚠️ 这是**补**一张还没有的封面。已发布过的报告做日常更新时**不要**这么做 —— 见 §3.6。
- 怎么确认真的带上了：发布响应里 `has_cover: true` 且 `cover_w/cover_h` 有值；
  `GET /api/reports/{slug}/cover` 返回 200 图片。

## 3.6 更新报告时**不要再生成封面**

报告已经在卡片墙上、封面也在了 —— **改内容的时候不要碰封面**。

**更新时只要不带任何封面字段，服务端就会保留原封面**（既定行为：`_cover_from_body()` 没收到
封面就返回 `None`，`_store_cover()` 随即原样留着 —— 既不会清空，也不会重新生成）。

- ✅ **正确**：重发 / `PUT` 同一个 `slug`，body 里**只放要改的字段**（`html`、`title`…），
  **一个字都不要提封面** → 封面不动、卡片图不闪、不多等 30 秒。
- ❌ **错误**：更新时习惯性带上 `"generate_cover": true` —— 白等约 30 秒、白耗一次生图额度，
  还可能把一张本来没问题的封面换掉（外部看到的是"报告内容没怎么变，封面变了"）。
- 只有两种情况才重新生成：① **用户明确说要换封面**；② 响应里 `has_cover` 是 `false`
  （这份报告本来就还没有封面）。要**主动清空**封面才需要显式发 `"cover_url": ""`。
- ⚠️ 这条和 §3.5 的"封面必须和 `html` 同一次发"**不矛盾**：那条说的是**首次发布**（没有封面就得建）；
  这条说的是**已经发布过的报告做更新**（有封面就别再建）。

## 4. 最小可用骨架

> 完整、**高密度**的配方见 **§1.8 页面配方**。
> 下面的骨架只是"结构正确"的下限 —— 按它写出来多半会是一份半空的报告。

```html
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Q3 渠道深度复盘</title>
<style>/* 自己的样式 */</style>
</head>
<body>
<div class="deck" data-deck-title="Q3 渠道深度复盘">
  <section class="slide">
    <div class="s-kicker">COOLING · 2026 Q3</div>
    <h1 class="s-title">Q3 渠道深度复盘</h1>
    <p class="s-sub">渠道级售出对比 · 6 个渠道 · 净零售口径</p>
  </section>
  <section class="slide">
    <div class="s-kicker">结论</div>
    <h2 class="s-title s-title-sm">份额差在收窄</h2>
    <div class="s-body"><div class="s-kpis">
      <div class="s-kpi"><div class="s-kpi-l">份额差</div><div class="s-kpi-v">+0.3pp</div></div>
    </div></div>
  </section>
</div>
</body>
</html>
```

## 5. 标准工作流

1. `GET /api/ai/knowledge/search?q=…` 取业务口径与规则；需要数字时走 `POST /api/chatgpt/sql-query`。
2. 写 HTML（长文或 deck，见 §1），**两种语言各写一遍**（见 §1.5），术语照 §1.6 查。
   deck 按 **§1.8 页面配方**走，写完过一遍 **§1.9 发布前自检**（最常被漏的是字号下限与"每页一个论点 + 证据"）。
3. `POST /api/reports`，带上 `title / summary / category / tags / html / submitter` 与一种封面写法。
4. 服务器返回 `slug`，此时报告只在该账号的**我的工作区**。用户可在前端右键卡片按邮箱分享给同事（收件人须注册登录），生成免登录链接，或发布到公共区形成独立只读快照。
5. 要改私人原稿就重发同一个 `slug`；如需同步公共区，再点“更新公共副本”。

## 6. 常见错误

| 状态 | 含义 |
| --- | --- |
| `400` | 参数问题：标题为空、`slug` 非法（只允许小写字母/数字/`.`/`_`/`-`）、封面 base64 解不开、提示词超 1500 字符 |
| `413` | HTML 超过 8 MB |
| `422` | 缺必填字段（`title` / `html`） |
| `404` | `slug` 不存在 |
| `502` | MiniMax 生图失败（错误信息里带对方状态码） |
