---
document_id: klado-v2:format-output-standards:zh
title: format-output-standards
document_type: format
source: klado-runtime-curation
version: 1.7
tags: [format-output-standards, 回答模式, answer-modes, 输出结构, 管理层deck, 管理层, 证据来源, 数字格式, 幻灯片, 决策, 备忘录, 复盘, 结论先行, slide, output, standards, report, deck, slides, internal-only]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/format-output-standards.md
---

> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造；业务数字必须走批准的只读数据源，不得凭印象编造。
> 旧工具名（`search_knowledge` / `query_database` / `create_xlsx` …）是历史名，真实端点见该文档 §4。


---

# 输出规范（Output Standards）
<!-- type: rule -->

适用：任何需要结构化输出、图表/表格呈现、管理层汇报、诊断分析或决策建议的回答。

不适用：只问一个具体数字/排名，且不需要图表或结构化呈现的简单取数场景（快速取数模式）。

基础规则（结论先行、数据透明、Mermaid 降级）已写入系统指令，任何场景均生效。本文档定义扩展输出规范，包括 PM 决策层、证据来源标注、可视化选择和诊断维度框架。

---

## 1. 结构

- **结论先行**：第一段先给 headline takeaway，再展开证据。
- **Scope 说明**：数据回答必含 metric formula、time range、filters、数据源表、数据口径与覆盖说明。
- **不隐藏不确定性**：字段/映射不明确时问一个聚焦问题，或采用保守默认并明确标注。

## 2. 证据来源标注

来自不同源的内容必须分开标注，不混淆：

- **Internal facts**：Klado 数据库（sell-in / sell-out / products）
- **Market facts**：外部市场数据（market data）
- **Knowledge / Playbook**：`search_knowledge` 检出的 Supabase 文档
- **Memory**：`internal context memory (not callable by the agent)` 检出的历史决策
- **Web facts**：web search（仅限公开外部信息）
- **Assumptions**：明确写"假设"

## 3. PM 决策层

凡是用户要求分析、诊断、Business Case、GTM、产品定义、上市复盘、管理层建议，输出不能停在信息总结。必须把证据转成产品经理可以执行的选择。

默认使用以下决策标签之一：

| 决策 | 适用时机 |
|---|---|
| `Proceed` / 推进 | 证据支持机会明确，风险可控，下一步应进入执行或审批 |
| `Revise` / 调整 | 方向成立，但产品定义、价格、渠道或资源配置需要修改 |
| `Test` / 测试 | 机会存在，但关键假设未验证，需先做小样本、渠道试点或用户/竞品验证 |
| `Delay` / 暂缓 | 外部窗口、内部准备、样机、价格或渠道条件未成熟 |
| `Stop` / 停止 | 核心假设被数据否定，继续投入会稀释资源或伤害组合 |

PM 建议必须回答五件事：

1. **Business impact**：对增长、份额、毛利、库存、渠道关系或品牌定位有什么影响。
2. **Product implication**：对规格、平台、价格带、卖点、人群、组合角色有什么要求。
3. **Channel action**：哪个渠道先做、怎么做、谁需要配合、哪些动作要停或改。
4. **Next validation**：下一步验证什么指标、用什么数据、何时复盘。
5. **Risks and assumptions**：明确关键假设，列出会推翻结论的风险条件。

如果证据不足，给 `Test` 或 `Delay`，并明确最小验证动作；不要把缺信息包装成确定结论。

## 4. 可视化

优先尝试 Mermaid chart；渲染失败立即降级为编号文本或数据表格，不要重试：

- 流程 / 决策树 → Mermaid flowchart
- 时间序列 → Mermaid timeline 或 Mermaid xychart
- 漏斗 / 矩阵 → Mermaid funnel / quadrantChart
- 不能渲染的情况 → 给数据表格

Mermaid 注意事项：
- 短 ASCII node ID（A、B、N1 等），避免特殊字符
- label 避免 HTML（`<br/>` 等）和长引号
- 渲染失败时立即降级为编号文本

### 4.1 图表画布与版式规范（通用）

所有需要图表的分析任务（含 deep-dive / performance / management deck）统一使用以下规范，避免在场景文档中重复维护：

1. **画布尺寸**（16:9 固定 —— 发布到工作区的报告一律 16:9 分页 deck）
- 单图默认：`1200 × 675`
- 并排双图：每图 `760 × 420`
- 移动端兜底：`720 × 960`（纵向堆叠）

2. **页面布局顺序（默认）**
- 第一屏：总览图（规模/趋势）
- 第二屏：结构图（份额/占比）
- 第三屏：明细表（Top SKU / Top segment）
- 第四屏：关键对象走势图（每页最多 3 张，超出分页）

3. **图内元素位置**
- 标题：左上（包含口径：时间/范围/过滤条件）
- 图例：右上（不得遮挡峰值）
- 注释：右下（固定标注数据源和时间范围）
- 数据标签：只标注峰值、低值、最新值

4. **坐标轴与尺度**
- 双轴图必须显式标注左轴和右轴口径
- 月份轴默认近 24M，标签按季度抽样
- 异常点（突增/突降）必须加注释，不可静默忽略

5. **可读性阈值**
- 同图折线不超过 5 条；超出时做 Top N + 其他
- 柱状图类目不超过 12 个；超出时做 Top10 + 其他
- 字号建议：标题 24、坐标轴 14、图例 13、数据标签 12

## 5. 数字格式

遵循 `format-number-readability.md`。

### 5.1 表格格式规范（全局生效）

以下规则适用于所有分析输出中的任何表格：

1. **居中对齐**：所有表格（标题行、数据行）一律居中对齐。
2. **禁止缩写 SI / SO**：列标题必须使用全称 `Sell-in` / `Sell-out`（含 Sell-in YoY、Sell-out SPTO 等组合形式）；禁止出现 `SI` / `SO` 缩写列标题。
3. **禁止缩写 VC**：必须使用全称 `Value Class`；任何列标题、段落标题、行标签禁止出现 `VC` 缩写。
4. **Contribution 不写 Mix**：占比列统一写 `Contribution`，禁止写 `Mix`。
5. **月度表格必须含合计行**：按 Month 维度展示数据的表格，末行必须加 **合计 / Total** 行，汇总所有数值列（含 Qty、GTO/SPTO、差值等）；YoY 列合计写"—"。
6. **大区表格不截断**：大区（`sales_cluster`）对比表必须列出所有相关大区（排除 CNHQ），不因规模小而省略，不加省略号；默认按 Sell-in GTO 降序。
7. **SKU 表格必须含 Brand 列**：凡涉及 SKU 明细的表格，必须包含 `Brand` 列（来自 `products.brand`）。
8. **新品口径注释**：凡含新品数据的表格（含"新品 Qty Contribution"等列），必须在表格下方加注：
   > *新品定义：SOS（首次上市日期）距分析期末 ≤ 6 个月；以 `products.sos` 为准。用户另有指定时以用户定义为准并注明。*

## 6. 表现/诊断类分析的必备维度

任何"X 表现 / 为什么 / 诊断"类问题，分析框架必须包含：

1. **Headline conclusion**（先结论后证据）
2. **Scope**（时间、品类、渠道、品牌）
3. **Sell-out**（终端动销）
4. **Sell-in**（渠道进货）
5. **库存信号**（sell-in vs sell-out 差值趋势）
6. **趋势**（月度走势）
7. **产品结构**（按 platform / RRP / 价格段拆分）
8. **Top SKU 驱动**
9. **新品贡献**（用 `products.sos` 识别新品）
10. **Sales cluster 表现**（如适用）
11. **行动建议**

不要只交一张销售表。

## 7. Business Study / Business Case 输出

仅当用户明确要做 Business Study / Business Case / GTM / launch plan / post-launch review 时启用此流程，普通数据查询不走此流程：

- 识别任务类型：opportunity exploration / concept validation / management business case / GTM / pricing / post-launch review
- 先给 Mermaid flowchart（短 ASCII node ID），失败给编号文本
- 分步推进，**一次只问一个问题**，用户回答后说明如何使用再问下一个
- 区分标注 `confirmed facts` / `assumptions` / `missing inputs` / `risks`
- 结论必须用内部数据 + 外部市场数据 + 产品组合三类证据支持

## 8. Core Evidence 三类证据组合

Business Study/Case 必须包含：
- **Internal**：内部动销数据（sell-out）、SKU momentum、channel behavior、seasonality
- **Market**：外部市场数据的 size、trend、price bands、brand share
- **Portfolio**：产品组合的 SKU / brand / platform / RRP、gap、cannibalization 风险

## 9. Product Brief 处理

用户提及 product / platform / SKU family → 先检索知识库里既有的对应说明页；
- 该页解释产品定义（特性、定位）
- 实时数据走账号可用的取数接口
- 市场假设用外部市场数据验证
- 在结论中明确标注 documented facts / live facts / web facts / assumptions

## 10. 视觉物料 / 图片

- 报告封面按 `klado-v2:media-image-prompt:zh` §报告封面 的规则生成（16:9、无水印）。
- 图片生成目前**只覆盖报告封面**；正文里的产品图 / 实拍图从文件库取真实 URL，
  **不要用 AI 生成图替代真实产品图**。
- 需要既有素材时先 `GET /api/ai/knowledge/search?q=<关键词>`。

---

## 11. HTML 幻灯片

**owner 是 `klado-v2:format-report-html:zh`** —— 取它的全文（HTML 骨架、16:9 分页 deck 约定、
密度与字号下限、页面配方、发布接口、封面）。本文不重复。

1. `GET /api/ai/knowledge/search?q=report+deck+html` → `GET /api/ai/knowledge/document/klado-v2:format-report-html:zh`
2. 图表配色 / 字体 / 图型按 `klado-v2:format-chart-style:zh`
3. 先跑取数覆盖每张内容页的数据论点，再一次性生成所有页面

触发词：用户明确说"HTML 幻灯片""HTML deck""HTML 演示""生成 HTML"。不含 HTML 关键词的幻灯片/PPT 请求走内容生成模式，不触发此节。

> ⚠️ **2026-09-29：`klado-v2:format-html-slides` 已退役**（从知识库删除，记入 `_retired.txt`）——
> 它教的 `bm-slide` 标记与 960×540 内联 CSS 画布**已不被 Workspace 渲染**，留着只会把 agent 引到作废的
> 规范上。正文归档在仓库 `docs/legacy-format-html-slides.md`。不要再去检索或引用它。

---

## 12. 回答模式（Answer Modes）

根据用户任务类型选择对应的输出模式：

### 快速回答（Quick answer）

适用：用户直接问一个具体事实或数字。

结构：
1. 结论
2. 支撑数据（紧凑）
3. 数据来源 / 时间范围

### 诊断式回答（Diagnostic answer）

适用：用户问"分析表现 / 为什么 / 怎么改善"。

结构：
1. 标题结论
2. 数据范围与覆盖说明
3. 核心指标
4. 驱动因素
5. 风险 / 待确认事项
6. 行动建议

### 管理层回答（Management answer）

适用：输出是给管理层或领导看的一页纸、deck。

结构：
1. Executive takeaway（一句话结论）
2. 业务含义
3. 建议行动
4. 附录证据

### PM 决策备忘录（PM decision memo）

适用：用户问要不要做、是否上市、如何定价、如何调整、如何对齐产品 / 销售 / 管理层。

结构：
1. **决策**：Proceed / Revise / Test / Delay / Stop
2. **Business impact**：对增长、份额、毛利、库存、渠道关系或品牌定位有什么影响
3. **Product implication**：对规格、平台、价格带、卖点、人群、组合角色有什么要求
4. **Channel action**：哪个渠道先做、怎么做、谁需要配合、哪些动作要停或改
5. **Next validation**：下一步验证什么指标、用什么数据、何时复盘
6. **Risks and assumptions**：明确关键假设，列出会推翻结论的风险条件

### 创意回答（Creative answer）

适用：用户要营销概念、文案、海报、品牌口号。

结构：
1. 目标受众
2. 消费者洞察
3. 概念方向
4. 核心信息
5. 支撑证据
6. 示例文案 / 视觉方向

---

## 13. 禁止事项（Do Not）

- 不要从记忆回答需要数据支持的问题；必须先查 Klado。
- 不要用 web search 的信息覆盖或替代内部数据。
- 不要只交一张原始数据表，没有结论。
- 不要混用不同来源的数据而不加标注。

---

## 13. Management Deck / Slide Deck 输出

仅当用户要生成 PPT、slide deck 或 slide report 时启用本节。视觉规范（颜色 / 字体 / 布局）使用 `klado-v2:format-chart-style:zh`。

### 报告架构（McKinsey 前后页结构）

> **Front pages**：结论 → 建议 → 决策逻辑  
> **Back pages**：分析过程 → 数据附录

标准章节顺序：

1. **Executive Summary**
2. **Management Recommendation**
3. **Decision Logic**
4. **核心分析页**（按项目性质拆分，如：Why X is not the main lever / Where to act）
5. **Appendix A**：市场与 Top SKU 特征信号
6. **Appendix B–D**：各产品或专题深挖

核心原则：

> 结论先行。证据放附录。

### 证据展示规则

- 展示具体价格，如 **¥9,990 → ¥12,900**，不要只展示百分比。
- 百分比仅作辅助参考，不要单独呈现。
- 数据页必须注明时间范围和数据来源。
- 管理层报告排名默认按**销售额**，不按数量。
- 报告分析单元默认为**平台 × 价格段**，不要停在 SKU 粒度。

### 市场数据附录规则

市场数据附录不能停在价格段增长，需要：

> **价格段趋势 + Top SKU 表现 + 新品 / 功能信号**

对标竞品的规格页：

- 用市场数据确认基础规格（尺寸、噪音、能耗标签、上市日期、销量、价格等）。
- 用公开产品页 / 电商列表补充消费者功能卖点。
- 必须区分 **market-confirmed specs** 和 **web-sourced feature claims**。
- 展示尺寸时写清单位与维度，不要混淆深度与宽度。

### 页面过于拥挤时

1. 减少 SKU 行数。
2. 每个功能压缩为一个核心卖点。
3. 细节移至附录。
4. 用图表替代密集表格。
5. 确认无文字重叠。
- 不要把产品 brief 当成当前实际销售表现。
- 不要凭直觉选竞品：对标范围按用户明确给出的口径确定。
- 不要在用户只需要一个决策的时候堆砌过多指标。
