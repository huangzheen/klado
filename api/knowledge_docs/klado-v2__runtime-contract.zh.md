---
document_id: klado-v2:runtime-contract:zh
title: runtime-contract
document_type: rule
source: klado-workspace
version: 1.4
tags: [runtime-contract, runtime, contract, capability, capabilities, limits, unavailable, retired, tools, endpoints, permissions, 能力, 不可用, 已退役, internal-only]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/runtime-contract.md
---

# 运行时契约（所有知识库文档的最高优先级）

**一句话**：知识库文档描述的是"规则期望你能做什么"；**能不能真的调用，以本文档为准**。
各文档开头那段短指针指向这里 —— 这样每份文档都能带上警示，又不会用同一段一千多字的样板
把正文挤到 500 字摘录窗口之外。

## 1. 怎么读知识库

- `GET /api/ai/knowledge/search?q=&top_k=` — 关键词检索，返回文档与摘录
- `GET /api/ai/knowledge/document/{document_id}` — 取整份文档正文
- `GET /api/ai/knowledge/manifest` — 清单（id / 类型 / 版本）

`/api/*` 使用账号绑定的 agent 授权码（`Authorization: Bearer klado_agent_…`）或浏览器会话。
Agent 可读该账号可读的内容，并写自己的 Workspace 文档、知识页、知识页图片、日历事件和 Dashboard。
Data Center 上传、建表、更新、删除和授权写操作仍需浏览器会话，并且只能管理自己的内容；
管理员身份不扩大对他人内容的访问权。只读 SQL 使用 `POST /api/data-center/query`。
`/r/{slug}`、`/d/{slug}` 等独立阅读页仍执行内容访问权限；只有有效的 `/s/{token}` 和
`/api/data-center/share/{token}` bearer 链接可免登录。不能把普通文档地址当作公开分享链接。

## 2. 能力清单：哪些是真的

| 能力 | 状态 | 真实端点 / 看哪里 |
| --- | --- | --- |
| 读知识库 | ✅ 可用 | `/api/ai/knowledge/*` |
| 发布 HTML 报告 / deck | ✅ 可用 | `POST /api/reports` — 规范见 `klado-v2:format-report-html:zh` |
| 发布动态报告（带筛选器） | ✅ 可用 | `POST /api/reports` + `PUT /api/reports/{slug}/filters` — 规范见 `klado-v2:format-dynamic-report:zh` |
| 把自带的 pptx / xlsx / pdf / docx 交进工作区 | ✅ 可用 | `POST /api/reports/documents`（文件字节 base64 上传，**不是**服务端生成）— 见 `klado-v2:format-workspace-documents:zh` |
| 生成报告封面图 | ✅ 可用 | `POST /api/reports/cover`，或发布时带 `generate_cover: true` — 见 `klado-v2:media-image-prompt:zh` §报告封面 |
| 日历事件 | ✅ 可用 | `/api/calendar/events` — 规范见 `klado-v2:format-calendar-event:zh` |
| 知识库页面（wiki） | ✅ 可用 | `/api/knowledge/items` — 规范见 `klado-v2:format-knowledge-page:zh` |
| **生成** xlsx / ppt / pdf | ❌ 不可用 | 服务端不产出 Office 文件。你在自己那一侧产出文件后，可以用上面那行的上传端点把它交进工作区 |
| 外部联网调研 | ❌ 本项目不提供 | 用你自己的检索能力 |
| Data Center 上传、建表、更新、删除、分享 | 浏览器所有者可用；agent 不可写 | `/api/data-center`，默认私有，指定同事只读分享 |

## 3. 两条不可违反的规则

1. **不要假装能力**：不存在的能力就直说"不可用" —— 不要 invoke、simulate、promise，也不要给一个假链接。
2. **数字必须来自真实来源**：规则里需要"活数据"的地方，必须取自查证过的来源，**不得编造**。
   报告是**静态快照** —— 它冻结的是发布当时的数据。

## 4. 历史遗留名（读到旧文档时对照）

2026-09-22 内嵌助手与其工具白名单退役，旧文档里的工具名是**历史名**：

| 旧名 | 含义 / 现状 |
| --- | --- |
| `search_knowledge` | `GET /api/ai/knowledge/search` |
| `get_document` | `GET /api/ai/knowledge/document/{document_id}` |
| `create_html_report` · `create_html_deck` | `POST /api/reports`（**2026-09-26 起真实可用**） |
| `generate_image` | `POST /api/reports/cover`（**真实可用**） |
| `create_xlsx` · `create_ppt*` · `pdf` | ❌ 生成不可用 | 但你**自己产出**的 `.pptx/.xlsx/.pdf/.docx` 可以用 `POST /api/reports/documents` 交进工作区（旧工具名 `create_xlsx` 等仍不可调用） |

**不要因为某份旧文档提到 web / Drive 数据，就假定这些数据存在。**

## 5. 文档分工（同一件事只应有一个 owner）

| 主题 | owner |
| --- | --- |
| 报告结构 / 密度 / 双语 / 发布接口 / 封面 / 导出 | `klado-v2:format-report-html:zh` |
| 静态 vs 互动形态、互动状态接口 | `klado-v2:format-interactive-report:zh` |
| 动态报告的筛选器 schema | `klado-v2:format-dynamic-report:zh` |
| 文档卡片（上传 pptx/xlsx/pdf/docx） | `klado-v2:format-workspace-documents:zh` |
| 回答模式（Quick / Diagnostic / Management / PM memo / Creative） | `klado-v2:format-output-standards:zh` |
| 图表（配色 / 字体 / 选型 / 画法） | `klado-v2:format-chart-style:zh` |
| 数字 / 单位 / 百分比展示 | `klado-v2:format-number-readability:zh` |
| 汇报一页纸 | `klado-v2:format-meeting-one-pager:zh` |
| 产品线排布图模板 | `klado-v2:format-product-line-layout:zh` |
| 可编辑 PPTX 导出约定 | `klado-v2:format-pptx-export:zh` |
| 图像提示词（含报告封面配色） | `klado-v2:media-image-prompt:zh` |
| 知识库页面（wiki）写法 | `klado-v2:format-knowledge-page:zh` |
| 日历事件字段与 16:9 页面 | `klado-v2:format-calendar-event:zh` |
| 任务 → 读哪份文档 | `klado-v2:task-routing-playbook:zh` |

**2026-09-29 起 `klado-v2:format-html-slides` 已退役**并从知识库删除（记入 `_retired.txt`）：
它教的 `bm-slide` 标记与 960×540 内联 CSS 画布已不被 Workspace 渲染，但它仍是一个**能命中且排第一**
的正常文档，会把 agent 引到作废规范上。**不要检索它，也不要提到它。**

上面的 owner 表就是"这类活该读哪份"的权威答案；`task-routing-playbook` 只负责把任务映射到这张表。
