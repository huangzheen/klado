---
document_id: klado-v2:format-meeting-one-pager:zh
title: format-meeting-one-pager
document_type: format
source: klado-runtime-curation
version: 1.4
tags: [format-meeting-one-pager, meeting, one, pager, 一页纸, 汇报, 会议, 纪要, 待办, 所需支持, 目的, 背景, GM, 管理层, internal-only]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/format-meeting-one-pager.md
---

> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造；业务数字必须走批准的只读数据源，不得凭印象编造。
> 旧工具名（`search_knowledge` / `query_database` / `create_xlsx` …）是历史名，真实端点见该文档 §4。


---

# 汇报一页纸规范（Meeting One-Pager Rules）
<!-- type: rule -->

## 触发条件

用户说"写一页纸"/"一页纸"/"One Pager"/"准备会议材料"/"1:1 汇报"/"汇报材料"/"会议 brief"等即触发本规范。

---

## 一页纸框架（遵循以下 5 条）

### 1) 会议（话题）目的

写明会议目的类型，例如：`Decision needed`、`Progress Update`、`Info Sharing`。

### 2) 背景

简述该话题的由来和业务意义。

### 3) 主体内容

若为决策型会议，主体内容应覆盖：
- 决策选项
- 话题负责人（或团队）的推荐建议
- 推荐建议依据（数据/事实）

### 4) 跟进待办事项（如有）

所有 follow-up 必须落实到：
- 负责人（Owner）
- 交付物（Deliverable）
- 交付时间（Due Date）

### 5) 所需支持（如有）

明确写清需要谁提供什么支持。

---

## 允许内容

- 文字
- 数据图表

---

## 内容要求

- 结构清晰
- 因果明晰
- 思考深刻
- 文字精炼

---

## 字体要求

- 默认字体：Aptos Narrow

---

## klado 交付

把一页纸生成为站内 HTML/PDF 报告或 HTML deck：文件保存到 klado Data Center 并以下载链接返回；绝不把内容发送到 Google Drive，也不要声称已在外部保存成功。
