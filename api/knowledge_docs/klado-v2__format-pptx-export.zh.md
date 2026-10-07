---
document_id: klado-v2:format-pptx-export:zh
title: format-pptx-export
document_type: format
source: klado-workspace
version: 1.2
tags: [format-pptx-export, pptx, PowerPoint, editable, model, binding, export, 可编辑, 导出, 字体绑定, 导出意图, 模型, internal-only]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/format-pptx-export.md
---

# Report Deck → 可编辑 PPTX：写 HTML 时的规则

先遵守 `klado-v2:format-report-html:zh` 的分页、双语、字号与发布规则。PPTX 导出器
使用浏览器计算最终几何和颜色，再写 PowerPoint 原生对象；它**不调用 agent**。
下面的标记给它可靠的语义与数据，避免从色块猜测图表、从文字猜测表格。
没有标记的旧报告继续按原来的 DOM 转换。

## 1. 字体

网页与 PPTX 共用项目内的 `Roboto Condensed`（拉丁/数字）及
`CoolSans SC Narrow`（中文）。报告引用 `vendor/report-deck.css`，不要引入 CDN 字体。
导出器从项目打包的 WOFF2 生成固定 400/700 字重，并把完整字体嵌进 PPTX；
文字仍然是可编辑文字。新的自定义字体**不会**被自动下载或嵌入。
中文打包字体是 GB2312 子集，超出字库的字可能由系统字体补位；发布前检查缺字。
不同 Office 版本的排版仍可能略有差异，重要报告要在目标 PowerPoint 打开核对。

## 2. 给元素标明导出意图

| 标记 | 用法 |
| --- | --- |
| `data-pptx-id="sales-title"` | 本页可见元素的稳定唯一 ID；重复 ID 会报错。 |
| `data-pptx-role="badge"` | 将纯色、均匀边框的小徽章及文字写入**一个**原生 PowerPoint 形状；`<br>` 保留为真实换行。 |
| `data-pptx-chart-ref="charts.sales"` | 从第 3 节的同一数据模型生成真正可编辑的图表；旧的 `data-pptx-chart='{"type":…}'` 仍可用。 |
| `data-pptx-table-ref="tables.matrix"` | 对原生 `<table>` 的可见单元格逐格核对模型；PPTX 保留原生表格。 |
| `data-pptx-bind="fields.title"` | 核对网页文字与模型值，不一致就拒绝导出。 |
| `data-pptx-export="native"` | 默认；不能保持可编辑的效果会报错。 |
| `data-pptx-export="omit"` | 忽略工具栏、保存按钮等不属于幻灯片的控件。 |
| `data-pptx-export="image"` | 仅限真正的图片或无法用原生对象表示的装饰背景；整个元素会作为局部图片。**不要用于文字、表格、图表。** |

`canvas`、内联 SVG、旋转/缩放/滤镜等不是可编辑形状的通用映射。
真正的图片可以用 `<img>`；若图标必须可编辑，就用简单的 CSS 边框/色块/线条
或 HTML 元素表示。不要把一个整页容器标为 `image` 来掩盖转换问题。

## 3. 一份模型同时驱动网页和 PPTX

把内容与图表原始数据放入一个 v1 JSON 模型，**不要另写一份“导出专用数据”**。
分页运行时会把 `data-deck-field` 的文字及 `data-deck-table` 的表格行从模型渲染到 HTML，
同时自动加上对应的 `data-pptx-bind` / `data-pptx-table-ref`。图表容器上的
`data-deck-chart` 会自动得到 `data-pptx-chart-ref`；网页中的图表仍由报告自身脚本
使用同一 `reportDeckData.model` 绘制，PPTX 用模型的类别、数值和系列创建原生图表。

```html
<script type="application/json" data-report-deck-model>
{"version":1,
 "fields":{"titleZh":"渠道销售","titleEn":"Channel Sales"},
 "tables":{"mix":[["渠道","金额"],["线上","¥12m"]]},
 "charts":{"sales":{"type":"column","categories":["线上","线下"],
   "series":[{"name":"金额","values":[12,18],"color":"#2563eb"}]}},
 "state":{"note":"待标注"}}
</script>
<div class="deck"><section class="slide" data-lang="zh">
  <h1 data-deck-field="fields.titleZh"></h1>
  <table data-deck-table="tables.mix" data-deck-header-rows="1"></table>
  <div data-deck-chart="charts.sales" style="width:600px;height:360px"></div>
  <p data-deck-field="state.note" contenteditable="true"></p>
  <span data-pptx-role="badge">线<br>上</span>
  <button data-pptx-export="omit">保存</button>
</section></div>
```

模型路径只用 `fields.titleZh`、`tables.mix.0.1` 这种点分隔写法。
表格的模型值可以是字符串或数字；行数、列数、文本必须与 HTML 一致。
如果表格需要自定义热力图色阶，浏览器脚本要在生成表格后**从同一模型**计算单元格颜色，
导出器会读取最终 CSS 颜色。`data-deck-chart` 只提供数据关联，不会替网页绘图。

## 4. 互动报告的状态

`/api/reports/{slug}/state` 仍是服务端保存的唯一读者状态，≤64 KB，整份覆盖。
加载后用 `window.reportDeckData.setState(savedState)` 更新模型并刷新绑定的文字/表格；
自定义图表与标注也要从 `window.reportDeckData.model.state` 重绘。
每次编辑先更新模型，再按 `klado-v2:format-interactive-report:zh` 的 600ms 防抖规则
POST **整份 state**。不要只改 DOM、却忘记更新模型。
导出器等待状态加载与重绘完成后读取 `window.reportDeckModel`；网页绑定的文字或表格
如果与模型不一致，会明确报错，不会交出旧内容的 PPTX。

## 5. 发布前自检

- 中文和英文分别打开报告并导出一次；确认每页的状态、数字、语言一致。
- 真图表提供类别、系列和数值；只用 CSS 画的“图表”只能作为单独形状编辑，不能在 PPT 的「编辑数据」中修改。
- 检查字体缺字、文字换行、徽章、表格线宽和图片；PPTX 是可编辑对象，不能承诺所有 Office 版本逐像素一致。
- 不要用 `data-pptx-export="image"` 包住整张幻灯片或需要编辑的证据。
