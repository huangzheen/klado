---
document_id: klado-v2:task-routing-playbook:zh
title: task-routing-playbook
document_type: rule
source: klado-runtime-curation
version: 1.11
retrieval_role: router
tags: [task-routing-playbook, task, routing, playbook, klado, internal-only]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/task-routing-playbook.md
---

> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造。
> 旧工具名（`create_xlsx` …）是历史名，真实端点见该文档 §4。


---

# 任务路由与回答剧本
<!-- type: rule -->

本文档是 agent 的完整路由参考。当系统指令路由原则无法覆盖当前场景时，检索本文档获取详细路由规则。

核心原则：

> 先判断任务类型 → 选正确的知识文件和工具 → 按检索到的规则完整执行 → 结论先行。

⚠️ **本文档只做路由判定，不是答案。** 它提到每一份文档、因而在关键词检索里会跟很多查询"看起来相关"
（检索层已用 `retrieval_role: router` 下调本文的正文权重）。**确定任务类型后，必须按 §1 表格取对应
`format-*` 文档的全文再执行** —— 不得用本文的表格代替那份规范。
反面例子：用户要"做一份 16:9 deck"时，答案是 `klado-v2:format-report-html:zh` 的全文，
而不是本表那句话。

---

## 1. 完整路由场景表

⚠️ **这张表里两种 id 前缀，代表两个语料，检索方式不同**（2026-10-01 更正）：

| 写法 | 住在哪 | 怎么取 |
|---|---|---|
| `format-*`、`klado-v2:*` | **项目说明书**（curated，机器人的操作规范） | `GET /api/ai/knowledge/search?q=…`；也可以 `GET /api/ai/knowledge/document/<id>` 直接读全文 |
| `kb:xxx` | **业务知识库 wiki**（同事写的业务内容） | `GET /api/ai/knowledge/search?q=…`（**默认就搜**，2026-10-01 起；按你的账号权限过滤）；拿到 `kb:<slug>` 后 `GET /api/ai/knowledge/document/kb:<slug>` 读全文 |

表里的 `kb:` 前缀不是装饰——它告诉你**这条线索住在哪一套语料里**：同一个话题，业务内容在 wiki、操作规范在说明书（两套语料的对照表见 `format-knowledge-page` §1 的「两套知识库别搞混」）。

| 用户想要什么 | 典型提问 | 先 `GET /api/ai/knowledge/search` 检 | 再调用 |
|---|---|---|---|
| 用户要**安排一件事**：定时间、定参与人、定截止、挂材料（问的是「什么时候」） | 「把这个复盘会排到 9 月 1-12 号」「加一个 10 月新品上市的里程碑」「帮我在日历上建一个事件，@ 上李明」 | `format-calendar-event`（字段 / 16:9 事件页排版 / 参与人=读权限） | `POST` / `PUT /api/calendar/events`；⚠️ 发布到公共区必须人在网页上点 |
| 用户要制作一份给管理层或领导看的汇报材料，包括一页纸、PPT deck 等通用汇报格式 | "帮我做一个 Q1 复盘的一页纸""做一份给总经理的管理层 deck""把这份分析做成汇报格式" | `format-meeting-one-pager` + `format-output-standards` | 用户明确要保存才调 `POST /api/reports` |
| 用户要把一份做好的 HTML 报告 / 复盘 / 分析发到 **Workspace**（或做成 16:9 分页 deck） | "把这份分析发到 Workspace""做一份渠道复盘的报告""放进工作区" | `format-report-html`（结构 / 密度 / 双语 / 发布字段）+ `format-chart-style`（配色 / 图型） | `POST /api/reports` —— 写入凭据所属账号的私人 Workspace；封面见下一行 |
| 用户要交一个**自己产出的文件**（可编辑的 pptx / xlsx / pdf / docx） | "把这个 deck 交进工作区""发一份可编辑的 Excel" | `format-workspace-documents` | `POST /api/reports/documents` |
| 用户要一份**按条件切换**的报告（时间段 / 指标 / 区间） | "这份报告要能切时间段看""做一个带筛选器的报告" | `format-dynamic-report` | `POST /api/reports` + `PUT /api/reports/{slug}/filters` |
| 用户要一页**可标注 / 可改字**的互动页 | "要能右键标注、能改字""改完刷新还在" | `format-interactive-report` | `POST /api/reports`（互动状态经 `/api/reports/{slug}/state`） |
| 用户要把 deck 导出成**可编辑的 PPTX** | "导出的 ppt 要能编辑" | `format-pptx-export` | Workspace 卡片右键 Export to PPT |
| 用户要把一条**知识**写下来（定义、口径、方法、笔记），或要新建/修改**知识库**的页面 | "把这条口径记到知识库""帮我在知识库里建一页""更新一下那页笔记" | `format-knowledge-page`（写法 / `summary` / 中英双语 / 写前查冲突） | `POST` / `PUT /api/knowledge/items` —— 写进凭据所属账号的知识库；⚠️ 发布到公共区、改共享范围必须在网页里由人来点 |
| 用户要给报告配封面图 | "给这份报告配个封面" | `media-image-prompt`（§报告封面） | 首选你自己生图模型的 `cover_base64`；兜底 `POST /api/reports/cover`，或发布时带 `generate_cover: true` |
| 用户要画图表、选图型、定报告内配色/字号 | "这个数据该用什么图""图表配色怎么定""报告里的图怎么画" | `format-chart-style` | — |
| 用户只想确认数字的展示方式或图表的可读性规范 | "这些数字应该用什么单位？""图表标注怎么更清晰？" | `format-number-readability` + `format-output-standards` | — |
| 用户要选**回答的结构**（Quick / Diagnostic / Management / PM memo / Creative） | "这次该怎么组织回答" | `format-output-standards` §12 | — |

---

## 2. 多知识文件联合使用

一个任务经常需要多个 Knowledge。以下是常见的组合示例：

**"把这份分析做成一份发给管理层的一页纸，并发布到 Workspace"**
- `format-output-standards` — 管理层输出的架构与证据展示规则
- `format-meeting-one-pager` — 一页纸结构规范
- `format-chart-style` — 图表配色与选型
- `format-number-readability` — 数字格式
- `format-report-html` — 报告骨架、双语/密度与发布接口
- `media-image-prompt` — 封面图

**"做一份带筛选器的季度复盘报告"**
- `format-report-html` — 报告骨架与发布字段
- `format-dynamic-report` — 筛选器 schema 与 `data-filter-when`
- `format-chart-style` + `format-number-readability` — 呈现层

**"把这个复盘会排到日历，并挂上报告和参考资料"**
- `format-calendar-event` — 字段 / 16:9 事件页排版 / 参与人=读权限 / `attachments`
- `format-report-html` — 被挂上去的报告怎么写

## 3. 三层架构调用约束（避免歧义）

1. **呈现层（format-*）**：统一定义可视化、数字格式、管理层输出结构。
   - 报告本体（结构 / 密度 / 双语 / 发布接口）→ `klado-v2:format-report-html:zh`
   - **图表（配色 / 字体 / 图型选型 / 画法）→ `klado-v2:format-chart-style:zh`**
   - 数字展示 → `format-number-readability`；互动页 → `format-interactive-report`；动态报告 → `format-dynamic-report`；
     文档卡片 → `format-workspace-documents`；知识库页面 → `format-knowledge-page`；日历事件 → `format-calendar-event`；
     PPTX 导出 → `format-pptx-export`
2. **路由层（本文件）**：只负责任务判定与文档编排，不重复写方法细节。

工具命名约定：
- 文档中出现 `GET /api/ai/knowledge/search` 时，统一用于拉取规则/模板知识。

## 4. 追问规则

不要过度提问。能合理默认就先做，并说明默认。

只在以下情况才追问**一个**聚焦的问题：

- 报告的覆盖范围或对象无法确定。
- 用户要一份最终的管理层建议，但目标（objective）缺失。
- 用户要创意产出，但品牌 / 产品 / 受众不清晰。

## 不可用的路线

视频/动画、HyperFrames、外部联网调研、Google Drive 写入、学习卡片、agent 自管的记忆写入均不可用。不要对这些路线尝试任何工具调用。

另外两条**本部署已确认**的限制：

- **账号绑定的 agent 授权码**可写自己的 Workspace 文档、知识页、知识页图片、日历事件与 Dashboard，
  可读该账号拥有或收到授权的资源。Data Center 上传、建表、更新、删除和分享需浏览器所有者会话；
  公开发布与拉取副本仍是浏览器行为。角色不能代替他人的内容所有权。
- **服务端 xlsx / ppt / pdf 生成不可用**（不产出 Office 文件）；**图像生成只覆盖报告封面**
  （`POST /api/reports/cover`），其余图片请在你那一侧产出。
  能力清单以 `klado-v2:runtime-contract:zh` §2 为准。
