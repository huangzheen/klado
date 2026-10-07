---
document_id: klado-v2:format-number-readability:zh
title: format-number-readability
document_type: format
source: klado-runtime-curation
version: 1.5
tags: [format-number-readability, number, readability, 数字格式, 数字, 单位, 百分比, 金额, 价格, 数量, 进位, 千分位, 小数, 万, 亿, internal-only]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/format-number-readability.md
---

> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造；业务数字必须走批准的只读数据源，不得凭印象编造。
> 旧工具名（`search_knowledge` / `query_database` / `create_xlsx` …）是历史名，真实端点见该文档 §4。


---

# 数字与可读性格式规范（Number And Readability Formatting Rules）
<!-- type: rule -->

本文档适用于 ChatGPT 正文回答、数据表格、管理层报告、slide deck、appendix 和图表说明。

目标：让数据更易读、更适合管理层快速判断，同时避免不同回答中数值格式不一致。

---

## 1. 价格格式（Price Formatting）

面向消费者的 price、RRP、current price、benchmark price 使用人民币符号和千分位。

正确写法：

- `¥4,999`
- `¥12,900`
- `¥17,900`

价格变动使用箭头：

- `¥9,990 → ¥12,900`
- `¥13,900 → ¥17,900`

禁止写法：

- `RMB 12900`
- `12900 RMB`
- `12.9k RMB`
- `¥12.9K`

价格讨论必须显示具体价格。百分比只能作为辅助，不要单独出现。

---

## 2. 金额类指标（Monetary Metrics）

Sales、CM1、SPTO、revenue、value 等金额类指标使用紧凑英文单位。

正确写法：

- `234M`
- `23.5M`
- `0.8M`
- `23.5K`

单位放在标题、列头或图表标题里，不要每个单元格重复。

推荐列头：

- `Sales (RMB M)`
- `Sell-out SPTO (RMB M)`
- `CM1 contribution (RMB M)`

单元格数值：

- `234`
- `23.5`
- `0.8`

如果列头没有单位上下文，正文中可以写 `RMB 23.5M`，但表格里优先用列头承载单位。

---

## 3. 数量 / 销量格式（Quantity / Volume Formatting）

数量、销量、volume 使用紧凑单位：

- `234K`
- `23.5K`
- `1.2M`

小于 1,000 时用整数：

- `985`
- `240`

不要在同一个表格里混用 raw long numbers 和 compact numbers。

推荐列头：

- `Qty (K units)`
- `Sell-out Qty (K units)`
- `Volume (K units)`

---

## 4. 百分比格式（Percentage Formatting）

变化类百分比必须带正负号：

- `+12.5% YoY`
- `-3.2% YoY`
- `+8%`
- `-5%`

小数位规则：

- 非整数且有必要时保留 1 位小数。
- 不需要精度时用整数。
- 不要显示过多小数，如 `+12.347%`。

价格动作不能只显示百分比；必须配具体价格：

- 正确：`¥9,990 → ¥12,900 (+29%)`
- 错误：`+29% price increase`

---

## 5. Contribution / Share 格式（Contribution / Share Formatting）

产品结构、渠道结构、平台结构中的占比列统一写 **Contribution**，不要写 **Mix**。

正确写法：

- `Contribution`
- `Sales contribution`
- `Qty contribution`
- `Contribution change (ppt)`

Share 仅用于明确的市场份额、品牌份额或渠道份额。

Contribution / share 的变化用 percentage points：

- `+2.3ppt`
- `-1.5ppt`

不要用 `+2.3%` 表示 contribution / share movement，除非它真的是相对增长率。

推荐列头：

- `Contribution`
- `Sales contribution`
- `Qty contribution`
- `Contribution change (ppt)`
- `Share`
- `Share change (ppt)`

---

## 6. 表格与图表标签（Table And Chart Labels）

单位放在标题或列头里，避免每个单元格重复单位。

推荐标签：

- `Sales (RMB M)`
- `Sell-out SPTO (RMB M)`
- `Qty (K units)`
- `Price (RMB)`
- `YoY`
- `Contribution`
- `Contribution change (ppt)`
- `CM1 contribution (RMB M)`

避免：

- `Sales RMB`
- `Qty pcs`
- `Retail SPTO Amount`
- `Amount in RMB with tax`
- 过长的数据库字段式列名

表格列名应服务阅读，不直接暴露复杂字段名；必要字段名可在 footnote 或 methodology 中说明。

---

## 7. 舍入规则（Rounding Rules）

管理层阅读优先，不制造假精度。

价格：

- 产品价格、RRP、current price、benchmark price 显示精确价格。

Sales / CM1 / 金额类指标：

- `>= RMB 10M`: 最多 1 位小数，清楚时可用整数。
- `< RMB 10M`: 通常保留 1 位小数。
- 很小金额可用 `K`，例如 `235K` 或 `23.5K`。

数量：

- `>= 10K`: 最多 1 位小数。
- `< 1K`: 整数。

百分比：

- 最多 1 位小数，除非用户要求更高精度。

---

## 8. ChatGPT 回答可读性（ChatGPT Answer Readability）

正文回答也必须遵守这些格式。

- 先给结论，再给证据。
- 表格只放决策相关列；不要为了展示数据而堆列。
- 每个表格或图表后给一句 takeaway。
- 同一回答里同类指标使用同一种单位。
- 不混用 `234,000,000` 和 `234M`。
- 价格、金额、数量、百分比的口径要一致。
- 数据回答要说明 metric formula、time range、filters、data coverage。
- 不确定时明确标注 assumption 或 open question。

---

## 9. 幻灯片 / 报告可读性（Slide / Report Readability）

Slide、deck、管理层报告遵守以下规则：

- 默认视觉风格：白/浅灰背景，黑灰文字，少量单一强调色，版式克制、清晰。配色、字体与图型规范见 `klado-v2:format-chart-style:zh`。
- 每页只回答一个核心问题。
- 标题写结论，不写泛泛 topic。
- 主页面表格控制在 5-7 行以内；细节放 appendix。
- 每页只突出 1-2 个最重要数字。
- 使用一致的颜色语义：
  - Positive / recommended: one highlight color
  - Risk / decline: one warning color
  - Neutral data: grey / black
- 不要把 price、volume、contribution、feature、recommendation 全挤在一页；必要时拆成 evidence page + recommendation page。
- 每张图必须服务一个明确问题。
- Appendix 表格也需要一句 takeaway，不只是数据堆叠。
