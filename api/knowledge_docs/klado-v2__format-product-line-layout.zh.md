---
document_id: klado-v2:format-product-line-layout:zh
title: format-product-line-layout
document_type: format
source: klado-workspace
version: 1.3
tags: [产品线, 排布图, 排布, 宽度段, 价格段, 生命周期, 徽章, 网格, 矩阵, 模板, product-line, layout, grid, lifecycle, badge, deck]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/format-product-line-layout.md
---

> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造；业务数字必须走批准的只读数据源，不得凭印象编造。
> 本文是**特定报告形态**的模板配方：**产品线排布图** —— 宽度段 × 价格段 × 标签列的网格矩阵，
> 每个格子是一个产品。通用骨架、16:9 deck 硬约定、双语、发布字段一律以
> `klado-v2:format-report-html:zh` 为准；交互实现与状态持久化接口以
> `klado-v2:format-interactive-report:zh` 为准。本文只讲这张图本身的排版与交互配方。
> 线上活例：`r/product-line-layout`（本规范的全部色值/几何值均与该页实现一致，可直接对照）。

# 产品线排布图 — 页面模板

## 0. 这张图是什么

- **形态**：16:9 **单页** deck，整页就是一张 CSS Grid 矩阵，表格撑满页面高度。
- **列**：标签列（产品细分，由数据驱动、可增删）；**行**：价格段；列顶部分组：宽度段。
- **格子**：一格一个产品（容量、上市日期、平台），可为空格；产品可拖拽换位。
- 所有色值/几何参数是**迭代定稿值**，改样式前先对照本文，不要凭感觉另起一套。

## 1. 整体结构与网格

- 布局：CSS Grid。
  - `grid-template-columns: 90px repeat(totalTags, 1fr)`（首列 = 价格段行头，90px）；
  - `grid-template-rows: auto auto repeat(N, 1fr)`（前两行 = 宽度段头行、标签列头行，N = 价格段行数）。
- 外框：`3px solid #1e3a6e`（粗深蓝）。
- **最右列、最底行的内边框必须移除**，否则与外框形成双线。

## 2. 行头 / 角格

| 位置 | 内容 | 样式 |
| --- | --- | --- |
| 第一行角格（宽度段行上方） | "宽度段" | **单 div**，不要用双语言 span（见 §9） |
| 第二行角格（标签列行上方） | "价格段" | 同上 |
| 价格段行头（左 90px 列） | 价格段名 | 19px 加粗，背景 `#dce8fc` |
| 宽度段头 | 20px 加粗标题 + 14px 范围文字 | 背景 `#c8dbf5` |
| 标签列头 | 标签名 | 20px 加粗，上下 padding 8px，背景 `#eef4fc` |

- 宽度段分组竖线：`2px solid #5a7aa8`；普通单元格边框：`1px solid #b8c8de`。

## 3. 格子内三行文字（固定高度）

| 行 | 内容 | 字号 | 字重 | 颜色 | min-height |
| --- | --- | --- | --- | --- | --- |
| 1 | 容量 \| 平台中文名 | 18px | 700 | `#1a3a6b` | 27px |
| 2 | 上市日期 | 16px | 500 | `#4a6a8a` | 24px |
| 3 | 平台代号 | 16px | 700 | `#2563eb` | 24px |

- **某行为空也要渲染空 div**——三行位置永远对齐，不能让下面行顶上来。
- 超长用白色省略号截断：`overflow:hidden; text-overflow:ellipsis`。

## 4. 渠道徽章（格子右侧竖排）

- 尺寸 `24×38px`，圆角 4px，字号 14px 加粗白色，水平垂直居中。
- **线上**徽章：永远在格子**上半**（`top:25%; transform:translateY(-50%)`），背景 `#e07a5f`（赤陶色）。
- **线下**徽章：永远在格子**下半**（`top:75%; transform:translateY(-50%)`），背景 `#b08968`（暖沙色）。
- 渠道取值：`none / online / offline / both`——线上线下可**同时**勾选（both = 两枚徽章）。
- 文字竖排用 `<br>` 分行（`线<br>上`），**不要用 `writing-mode`**（见 §9）。

## 5. 生命周期色底

| 值 | 中文 | 背景 | 文字处理 |
| --- | --- | --- | --- |
| newplatform | 全新平台 | `#66bb6a` | 三行文字白色，层次感：L1 纯白、L2 85% 白、L3 70% 白 |
| new | 平台升级 | `#d4edda` | 默认深色文字 |
| ongoing | 持续销售 | `#d6e4f5` | 默认深色文字 |
| exit | 退市计划 | `#fde2d4` | 默认深色文字 |
| none | 未设置 | `#fafbfc` | 默认深色文字 |

## 6. 宽度段图示（纯 CSS 绘制）

统一：高度 42px，圆角 2px，边框 `2px solid #7a9cc8`，背景 `#e8f0fe`；色条宽度按段递增，
用来表达"这一档代表多宽"的比例感。**不要画具体产品外形** —— 外形一旦画死，换品类或换系列就得重做。

| 段 | 色条宽 | 说明 |
| --- | --- | --- |
| W1（最窄） | 18px | 第一档宽度段 |
| W2 | 22px | 第二档 |
| W3 | 26px | 第三档 |
| W4（最宽） | 30px | 第四档 |

段名与档数由数据驱动，上表只是"宽度递增"的比例示范。

## 7. 交互

- **右键空格子** → 添加产品弹窗；**右键有产品的格子** → 编辑 / 删除产品。
- **右键标签列头** → 向左添加标签列 / 向右添加标签列 / 重命名 / 删除（新标签列属于当前宽度段）。
- **右键宽度段头** → 重命名标签和范围。
- **右键价格段头** → 重命名 / 删除价格段。
- **拖拽**：有产品的格子可拖到空白格子，落点自动更新该产品的 `widthId / tagId / priceId`。
- **标题 / 副标题**：`contenteditable`，blur 时保存到 state（字段：`titleEn / titleZh / subtitleEn / subtitleZh`）。
- 状态保存、防抖、相对路径等通用纪律见 `klado-v2:format-interactive-report:zh` §1，不在此重复。

## 8. 图例

- 与副标题**同一行、右对齐**；字号 16px（与副标题一致）。
- 内容覆盖两组编码：渠道（线上 / 线下）+ 生命周期（全新平台 / 平台升级 / 持续销售 / 退市计划），
  色块与 §4/§5 一致。

## 9. 禁忌（踩过的坑，别再踩）

1. **不要**用 `writing-mode: vertical-lr` 做徽章竖排文字——第二个字会溢出背景；用 `<br>` 分行。
2. **不要**全局 sed 替换 font-size——会误伤平台注入的样式；必须用精确的 CSS 块替换。
3. **不要**在 grid 单元格里放双语言 span（`data-lang="en"` + `data-lang="zh"` 两个并列 div）——
   `display:none` 的元素在 grid 里仍会占自动排列位，打乱整张表；格内文字用单 div 中文即可。
4. **不要**在报告 update PUT 请求里带 `cover` 字段（封面只在创建时提交，update 带上会被拒/被误改）。
5. **不要**在 `.product-cell` 上用 `overflow:hidden`——会把右侧的渠道徽章裁掉。
