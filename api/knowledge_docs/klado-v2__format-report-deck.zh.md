---
document_id: klado-v2:format-report-deck:zh
title: format-report-deck
document_type: format
source: klado-workspace
version: 1.0
tags: [format-report-deck, deck, runtime, 分页, 幻灯片, 16:9, slide, 翻页, 页码, pptx, PowerPoint, 导出, export, pdf, png, 字体, font, 窄体, 密度, 分享]
runtime_contract: internal-tools-only
source_path: docs/report-deck.md
---

> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造；业务数字必须走批准的只读数据源，不得凭印象编造。

> **这份文档什么时候读**：报告的**通用 HTML 规则**（交付形态、双语、发布接口、封面、
> 发布前自检）在 `klado-v2:format-report-html:zh`——先读那篇。
> 本篇只覆盖 **16:9 分页 deck** 这一种形态：**怎么写骨架、画布有多大、字号下限、
> 各种导出（PPTX / PDF / 图片）各支持到什么程度、分享出去对方能看到什么**，
> 以及**运行时的实现事实与坑**（字体子集、内联、翻页）。颜色与图型选型在
> `klado-v2:format-chart-style:zh`，PPTX 导出语义在 `klado-v2:format-pptx-export:zh`。

# 16:9 分页报告（Report Deck）书写规范

一句话：报告里写 `<div class="deck">` + `<section class="slide">…</section>`，
它就变成**横向 16:9、键盘整页翻页**的横版文档；不写就还是普通长文报告。

## 导出可编辑 PPTX

Workspace 卡片右键菜单中的 Export to PPT 由固定脚本导出当前偏好语言，不调用 agent 或模型。
后端用 Chromium 计算 1920×1080 画布上的位置、大小和颜色，再生成 PowerPoint
原生文本框、形状、表格与图表；图片仍是图片。CSS 多层渐变或材质纹理作为局部图片
嵌入，叠在上面的文字继续是可编辑文本。普通长文报告没有固定分页，接口会
返回 422 并说明原因。

互动报告导出时会读取该报告已保存的 `/api/reports/{slug}/state`，等待脚本用这份状态
重绘页面后再提取，因此标注、改过的标题和备注会进入 PPTX。导出器不会执行保存请求。
依赖其它实时 API 的报告必须由服务端显式提供该接口的数据快照；未知数据请求会明确报错，
不会把加载失败的提示当成报告内容导出。

文字按段落组织：同一句里的粗体、斜体、下划线或不同字体是同一个文本框内的
独立文字样式；简单的 HTML 列表合并为一个含多段的文本框，便于整段编辑。
表格不使用 PowerPoint 默认主题；单元格填充、内边距、垂直对齐与四边框
直接取浏览器计算出的 CSS。简单徽章（`.badge`、`.mini-badge`、`.rrp`、`.channel-badge`）的
背景、文字和均匀边框写进同一个 PowerPoint 原生形状，编辑时可整体移动或改字。
徽章中的 `<br>` 会保留为形状内的真正换行。CSS 细边框按导出画布比例换算线宽，
并绘制在边框盒内，避免相邻格线看起来变粗。纯色、绝对定位的空伪元素可导出为
可编辑形状（例如本报告图表里的细线）；它们不是 SVG 图片。
纯色以及叠在纯色背景上的半透明颜色会先合成为同一组 8 位 RGB 数值，再写入 PPTX。
为避免 Office 把很淡的装饰效果渲染成深色边框，模糊投影和半透明阴影不导出；
用于标注的实色侧边线仍保留为可编辑线条。
渐变和纹理使用局部图片保留颜色；抗锯齿及 Office 的色彩管理仍可能造成
视觉差异，因此不承诺跨设备逐像素一致。

项目内置的 `Roboto Condensed` 与 `CoolSans SC Narrow` 会从 WOFF2 转成固定字重的
完整 TrueType 字体并嵌入 PPTX；导出的文字仍可编辑，也不依赖收件人提前安装字体。
其它自定义字体不会被自动嵌入，缺字或不同 Office 版本的排版仍须实测。

新报告可在 HTML 中加入 `data-pptx-role="badge"`、`data-pptx-export="omit|image"`、
`data-pptx-chart-ref`、`data-pptx-table-ref` 和 `data-pptx-bind`，明确导出语义。
`data-pptx-export="image"` 只供图片或装饰使用，不能用于需要编辑的文字/表格/图表。
通过 `<script type="application/json" data-report-deck-model>` 提供 `version:1` 模型后，
分页运行时的 `data-deck-field` / `data-deck-table` 会从模型生成网页内容并自动附加
导出绑定；`data-deck-chart` 会关联同一模型里的图表数据。互动报告在加载 `/state` 后
调用 `reportDeckData.setState(...)`，再从 `reportDeckData.model` 重绘自定义部分。
导出器会拒绝模型与网页文字、表格不一致的报告。完整写法见知识库
`klado-v2:format-pptx-export:zh`。

支持范围是 .deck / .slide、普通文字与颜色背景、边框、图片、规则 HTML 表格
和内置的 .s-meter 条形。.s-meter 会成为可编辑形状；若要让图表在 PowerPoint
的「编辑数据」里修改数值，应在图表容器附上结构化数据：

```html
<div class="my-chart"
  data-pptx-chart='{"type":"column","categories":["Jan","Feb"],
                    "series":[{"name":"Sales","values":[10,20],"color":"#2563eb"}]}'>
  <!-- 浏览器里的 HTML 图表仍由作者自行渲染 -->
</div>
```

type 支持 bar、column、line、pie。转换器用容器的实际位置与大小创建原生图表；
数据长度不符、非数值或未知图型会报错。原生图表和浏览器 CSS 图表由不同渲染器
绘制，细节不保证逐像素一致。

### HTML 图表与 PPT 图表的边界

网页上的柱条、线条和标签可以只是 CSS 元素；这些元素本身不包含完整的图表数据模型。
脚本不能可靠地仅凭外观还原类别、数值、系列及轴的含义。报告里只画了 CSS 图表、
没有 `data-pptx-chart` 的图表，导出后是可单独编辑的 PowerPoint 原生形状，
不是可通过「编辑数据」修改数值的图表对象，也不是整张截图。

要生成真正的 PowerPoint 图表，报告作者需提供上例的结构化数据。
后续若要让现有报告也自动得到原生图表，应先为报告补充这份数据或制定可验证的
图表标记约定，再逐类实现转换。即使转为原生图表，PowerPoint 与浏览器的排版和
渲染机制不同，无法同时保证所有细节逐像素一致；导出后仍需检查字体、坐标轴、
标签和间距。

CSS 纯位移（例如 `translateY(-50%)`）按浏览器测得的最终位置导出，仍可编辑。
canvas、内联 svg、视频、iframe、滤镜/裁剪、旋转/缩放/倾斜及不支持的伪元素内容会明确报错，不会暗中
截成整页图片。其它未嵌入的自定义字体必须在打开 PPTX 的电脑上可用。
即使内置字体已嵌入，导出后仍应在目标 PowerPoint 环境逐页复核。

## 导出 PDF 与图片

右键菜单的 Export to PDF / Export to Picture 用隔离 Chromium 渲染报告的当前保存状态，
直接截取浏览器画面。16:9 deck 每页输出一张 1920×1080 PNG；PDF 将同样的 PNG
逐页嵌入，单页图片下载为 PNG，多页图片下载为按页编号的 PNG ZIP。双语报告先选语言。
这两种格式保留颜色、字体和布局的浏览器画面，但导出的内容是位图，不可像 PPTX
那样单独编辑。字体、图片或必要数据加载失败时会报错，避免输出缺内容的文件。
需要其他实时 API 的报告，必须先为导出器提供明确的只读数据快照。

## Workspace 分享

私有报告所有者可从右键菜单按指定 `@example.com` 邮箱分享。收件人注册并登录后可只读
查看，也可拉取独立副本后编辑；原稿不受影响。Share link with everyone 生成随机、
可撤销的免登录只读链接，不会自动发布到公共区。免登录链接只能读取该报告、其保存的
标注状态，以及报告正文明确引用的对象存储图片；其他需要登录的实时 API 无法通过该
链接调用。希望免登录读者看到完整报告时，把必要数据写入 HTML 或状态快照中。

服务端在送报告时会把分页运行时**内联**进去（`api/routers/reports.py` 的
`_inline_deck_runtime`），所以报告本身不用带运行时文件，下载下来的 `.html`
也是自包含的。

## 最小骨架

```html
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>季度渠道复盘</title>
<!-- 本页样式写在 <style> 里，报告不要依赖本机文件 -->
<style>/* 你自己的样式 */</style>
</head>
<body>
<div class="deck" data-deck-title="季度渠道复盘">
  <section class="slide">
    <div class="s-kicker">COOLING · 2026 Q3</div>
    <h1 class="s-title">季度渠道复盘</h1>
    <p class="s-sub">渠道级售出对比 · 6 个渠道</p>
  </section>
  <section class="slide">
    <div class="s-kicker">结论</div>
    <h2 class="s-title s-title-sm">份额差在收窄</h2>
    <!-- … -->
  </section>
</div>
</body>
</html>
```

- `.deck` 外层的 `data-deck-title` 会显示在每页的页码条上（不写则用 `<title>`）。
- **外层 `.deck` 可省略**：直接写若干 `<section class="slide">` 会被自动包起来。
- 一页 = 一个 `.slide`，**别再手工分页**（不要自己插 `<hr>` 或写页码）。

## 页面画布：固定 1920×1080

每一页都是**固定的 1920×1080 设计画布**，运行时按窗口大小整体缩放，**不重排**。
这是刻意的：只有画布固定，屏幕上看的样子和导出/打印出来的样子才逐像素一致。
所以：

- 不要用 `vw` / `vh` / 百分比高度来排版，按 1920×1080 的绝对值思考。
- 一页装不下的内容就**另起一页**，不要缩字号硬塞。
- 页面内边距是 `76px 92px 100px`（下方 100px 留给页码条）。**不要覆盖 `.slide`
  的 padding**；要留白用 `.s-body` 的 `gap`。

## 内容密度与可读性（硬规则）

**缩放是会惩罚字号的。** 画布固定 1920×1080，运行时按窗口等比缩放；常见窗口
1280–1600 宽 → 缩放约 **×0.66–0.75**。也就是说**画布上 18px 的字，屏幕上只有
12–13px；写 11px，屏幕上只剩 8px。**

低密度报告只有两种形态，都是密度问题：

| 症状 | 长什么样 | 修法 |
|---|---|---|
| **半空的页** | 一页只有 3 行，正文区大片留白 | 与相邻页合并，或补对照数据（对比期 / 占比 / YoY / 排名 / 目标） |
| **塞满小字的页** | 9 个月数据挤在 13px 的手写条形里 | 用配方 A/B 重画（见下），装不下就分页 |

**"字小"不等于"信息多"** —— 填满画布却看不见，比留白更糟。

### 字号下限（画布 px）

| 用途 | 下限 | 建议 |
|---|---|---|
| 正文 / 表格单元格 | **18px** | 20–22px |
| 表头 / `.s-kicker` / 图例 | **14px** | 15–16px |
| 图表数据标签 / `.s-note` | **16px** | 18px |
| `.s-kpi-v` | 40px | 46–64px |

**绝不要出现 11–13px**。不要写 inline `font-size` 压字号；kit 自带的尺寸
（`.s-table` 19px、`.s-note` 18px、页面根字号 18px）本来就在安全线上。

### 一页装多少

内容区 **1736 × 904**（1920×1080 减去内边距）。

| 页面 | 目标 |
|---|---|
| 封面 | 标题 + scope + **一句结论**（下半页不放空） |
| 结论页 | **4–8 个 KPI**（两行）+ 一句结论 |
| 数据页 | **一张 ≥ 8 行的表**，或 2–4 组并排图表 |
| 明细页 | **10–15 行**（Top-N 至少 10 行） |
| 归因 / 建议页 | 3–4 张卡片，每条带数字 |

- 留白自检（两条都要过）：
  1. **最大连续空白** —— 内容区 904px 里最长的一段完全空白（无文字、无图形）：
     数据 / 明细 / 趋势 / 结构页 ≤ **15%**，结论 / 归因 / 卡片页 ≤ **25%**，
     封面 ≤ **30%**，**任何一页 ≥ 35% 直接不合格**；
  2. **文字行数** —— 内容页 ≥ 20 行，数据页 ≥ 30 行，封面 ≥ 15 行。

  ⚠️ 别拿"最后一条元素的底边"当留白指标 —— 页面底部贴一条注释就能把范围拉到页脚，
  半空的页也会显示"填满"。要找**最长的那段空白**，并**逐页**量（分页文档只有当前页可见）。
- 不达标就**合并相邻页**或**补对照数据**；一张表超过 ~15 行则分页，不要缩行高。
- **双语不增加页数**：EN / ZH 是同一页的两种语言，先做厚再复制。
- 每页标题写**结论**不写栏目名（`Monthly sell-in` 只能当 `.s-kicker`）；
  每个关键数字都要有参照（YoY / 占比 / 排名）；表尾给合计行。
- 一条结论只说一次，别在 5 页里重复。

**实测对照**（同一份渠道数据）：改动前 6 页平均最大空白 **52%**、最差 **82%**（封面整页只有三行字），
每页另有 18–24 处 11–13px 小字；按本节重排后 7 页平均 **13%**、最差 **15%**，字号越界 0 处。

### 图表配方（不要手搓条形）

手写 `<div style="height:10px;width:71%">` 缩放后既看不见也没有轴。运行时内置了
密度友好的组件，条高 14–18px（画布 px）、标签 20px：

```html
<!-- A · 对比条 -->
<div class="s-bars">
  <div class="s-bar"><span class="k">Mar</span>
    <span class="t"><i style="width:100%"></i><i class="now" style="width:57%"></i></span>
    <span class="v"><b>99</b> → <b>56</b> <b class="dn">-43%</b></span>
  </div>
</div>

<!-- B · 表格里嵌条形（数字 + YoY + 占比同一行，一页可放 12 行） -->
<tr><td>Mar</td><td class="n">99</td><td class="n">56</td><td class="n s-dn">-43%</td>
    <td style="width:34%"><span class="s-meter" style="--v:57%"></span></td></tr>
```

选型：≤4 个数用 KPI 卡；单维排名 / 跨期对比用 **B**；要肉眼比长度用 **A**；
多维交叉用 2–3 栏小表。能不上图就别上图。

面向 agent 的同一套规则在知识库文档 `klado-v2:format-report-html:zh`
（§1.7 密度 / §1.8 配方 / §1.9 发布前自检）。

## 图表配色 / 字体 / 选型

面向前端与 agent 的**完整规范在知识库** `klado-v2:format-chart-style:zh`
（三套品牌色阶与官网出处、内置字族、数据形状→图型选型表、可复制的折线/点阵/哑铃/堆叠/瀑布配方、
图表自检）。本节只记录**运行时事实**（字体文件在哪、怎么接的）。

## 字体（打包在仓库里，不依赖系统字体）

运行时 CSS（`vendor/report-deck.css`）里带 4 个 `@font-face`，字体文件在
`vendor/fonts/`：

| 字族 | 用途 | 来源 |
|---|---|---|
| `Roboto Condensed`（可变 100–900，latin + latin-ext） | 全部拉丁/数字 | **从应用壳 `index.html` 原样抽出** —— 与应用逐字节一致。许可 Apache-2.0 |
| `CoolSans SC Narrow`（400 / 700） | 全部中文 | Noto Sans SC 的轮廓与字宽**横向缩放到 88%**。许可 SIL OFL 1.1（衍生版随附许可，已按规定改名） |

**为什么中文用 88%**：这是社区推导出的「Roboto Condensed ÷ Roboto 平均字宽比」，
即让中文与 Roboto Condensed 在**窄度上对齐**的比例（实测：8 个汉字 @100px = 704px = 8×0.88×100）。
不做这件事时，中文会比拉丁字宽得多，中西混排很跳。

**几个必须知道的点：**
- `@font-face` 的 `url()` 是**文档相对**（`vendor/fonts/…`），因为运行时 CSS 会被服务端
  **内联进报告 HTML**（`reports.py::_inline_deck_runtime`），不是当外链样式表加载。**别把它改成文件相对路径**。
- 生成的子集覆盖 **GB/T 2312 全量（7554 字）+ 拉丁/标点/货币**，两个文件各约 1.0 MB（WOFF2）。
- ⚠️ 生成子集时，GB2312 的**第二字节可以取到 `0xFE`**，`range()` 上界必须写 `0xFF`；写成 `0xFE` 会**漏掉整列**
  （宁 `c4fe`、渠 `c7fe` …）——这种缺陷页面**不报错**，只是静默回落系统字体，唯一可靠的自检是**量汉字宽度**。
- ⚠️ 运行时字体栈已把 `"Roboto Condensed", "CoolSans SC Narrow"` 放在最前；在此之前运行时栈是
  `-apple-system, "PingFang SC"…`，**报告里的拉丁字从来不是应用字体**（比应用宽约 16–19%）。
- 字体没装/没加载时会回落到系统字体栈，不会报错——所以**改完字体要量宽度，不要只看截图**。

## 分页风格线与页码

运行时会给每页自动加一条**分页线 + 页码条**（`.deck-foot`：一条 hairline，
右侧 `3 / 12`，`≤12` 页时还带圆点进度）。**不要自己写页码**，也不要覆盖
`.deck-foot` 的样式。

按「全部页面」按钮（或 `#all`）会切到叠放视图，此时页与页之间会画出**虚线分页线**
（`.deck-break`）——那也正是打印/导出的版式。

## 翻页操作

| 操作 | 结果 |
|---|---|
| `↓` `→` `PageDown` `Space` `Enter` | 下一页（整页） |
| `↑` `←` `PageUp` `Backspace` | 上一页 |
| `Home` / `End` | 首页 / 末页 |
| 鼠标滚轮 | 翻页（累计滚动超过阈值才触发，带 320ms 锁） |
| 点击页右半 / 左 14% 边缘 | 下一页 / 上一页 |
| 底部圆角工具条 | 上一页 · 页码 · 下一页 · 全部页面 |

当前页会写进 URL 的 `#p3`（`history.replaceState`，不污染浏览历史），所以
`…/r/<slug>#p4` 这类链接可以直接定位到某一页，并且刷新后还在那一页。
`data-deck-click="off"` 可以关掉点击翻页。

在 Workspace 内查看时，**焦点在应用里按下这些键也会被转发进报告**（iframe 是
独立文档，按键不会跨帧冒泡）；反过来，焦点在报告里按 `ESC` 会把事件交回应用，
所以 ESC 关查看器的行为不受影响。

## 可以用的布局组件（可选，全部单类名，可被你自己的样式覆盖）

| 类名 | 用途 |
|---|---|
| `.s-kicker` | 页面顶部的全大写小标签 |
| `.s-title` / `.s-title-sm` | 主标题 / 次级标题 |
| `.s-sub` / `.s-lead` | 副标题 / 导语 |
| `.s-body` | 内容区（`flex:1`，撑满剩余高度） |
| `.s-cols` / `.s-cols-3` / `.s-cols-4` | 2 / 3 / 4 栏栅格 |
| `.s-kpis` + `.s-kpi`(`-l` `-v` `-d`) | KPI 卡片组（自动适配列数） |
| `.s-card` / `.s-card-h` | 通用卡片 / 卡片标题 |
| `.s-note` | 左侧竖线提示块 |
| `.s-table` | 表格（`th`/`td` 已配色，`.n` 右对齐数字） |
| `.s-code` | 深色代码块 |
| `.s-chip` / `.s-muted` / `.s-small` / `.s-center` / `.s-hero` | 小标签 / 灰字 / 小字 / 居中 / 整页居中 |
| `.s-swatch` | 细渐变装饰条 |

## 注意事项

- **图片**：走对象存储（`product-images/…`），用相对路径（如
  `api/storage/serve?path=product-images/x.png`）。服务端会注入 `<base>`，所以
  同一个文件在本地预览 / test / 生产都能解析；**不要**写带挂载点的
  `/...` 绝对路径。
- **字体**：用系统字体栈（运行时默认已带 `PingFang SC` / `Microsoft YaHei` /
  `Noto Sans CJK SC`）。不要引外链字体——服务器渲染时拉不到。
- **数据**：报告是**静态快照**，发布那一刻的数字就冻在里面；要更新就重发同一个 slug。
- **想要长文**：别用 deck。普通报告继续按长文写，两者互不影响。
- **本地调试**：骨架里可以显式写
  `<link rel="stylesheet" href="vendor/report-deck.css">` 和
  `<script src="vendor/report-deck.js"></script>`，从静态预览直接改；服务端送报告
  时会把这两个标签**替换成内联内容**，不会重复执行（运行时本身幂等）。

## 运行时在哪

- 源文件：`frontend/out/vendor/report-deck.{css,js}`（前端只有这一份）
- 服务端内联：`api/routers/reports.py` → `_inline_deck_runtime()`
- 只有文档里出现 `class="deck"` 或 `class="slide"` 才会内联；普通报告原样返回
