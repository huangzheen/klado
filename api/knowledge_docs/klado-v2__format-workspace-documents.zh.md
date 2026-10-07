---
document_id: klado-v2:format-workspace-documents:zh
title: format-workspace-documents
document_type: format
source: klado-workspace
version: 1.3
tags: [document, 文档卡片, pptx, 幻灯片, xlsx, excel, pdf, docx, word, 上传, 附件, workspace, 工作区, 下载, download, 预览, preview, base64, 备注, notes]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/format-workspace-documents.md
---

> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造。报告（HTML）的发布规范见 `klado-v2:format-report-html:zh`，
> 动态报告的筛选器见 `klado-v2:format-dynamic-report:zh` —— 本文只讲**二进制文档**这一条路。

# Workspace 文档卡片：把 pptx / xlsx / pdf / docx 交进工作区

一句话：**同事要的不是一份"讲这份 PPT 的网页"，而是那份 PPT 本身**。把文件的字节 base64 编码，
`POST api/reports/documents` 一次，它就变成 Workspace 里的一张卡片：同一个卡片墙、同一套
私人工作区/公共区/分享、点开能看（PDF 直接内嵌、Excel 服务端转表格、**PPT/Word 转成 HTML 在读者浏览器里渲染**）、能下载原件。

| 你要交付的东西 | 用什么 | 端点 |
| --- | --- | --- |
| 分析结论、复盘、月报（要排版、要投影） | **HTML 报告** | `POST api/reports`（见 `format-report-html`） |
| 同事明确要的 `.pptx` / `.xlsx` / `.pdf` / `.docx` | **文档卡片** | `POST api/reports/documents`（本文） |

**默认用 HTML 报告**：它排版可控、可导出可编辑 PPT、可双语、能按筛选器切换。
只有对方**要一个文件**（要拿去改、要发给客户、要走审批流、是他自己的模板）时才用文档卡片。

## 0. 一次调用

```json
POST api/reports/documents
{
  "filename": "Q3_channel_review.pptx",
  "content_base64": "UEsDBBQABgAIAAAAIQ…",
  "title": "Q3 渠道复盘（可编辑版）",
  "summary_en": "The editable deck behind the Q3 channel review.",
  "summary_zh": "Q3 渠道复盘的可编辑幻灯片。",
  "category": "Documents",
  "tags": "channel,q3",
  "submitter": "张三",
  "slug": "q3-channel-review-deck"
}
```

```json
201 {
  "slug": "q3-channel-review-deck",
  "title": "Q3 渠道复盘（可编辑版）",
  "kind": "document",
  "doc_type": "pptx",
  "doc_name": "Q3_channel_review.pptx",
  "doc_pages": 14,
  "size_bytes": 3145728,
  "url":          "https://…/r/q3-channel-review-deck",
  "document_url": "https://…/api/reports/q3-channel-review-deck/document",
  "download_url": "https://…/api/reports/q3-channel-review-deck/document?download=1"
}
```

`url` 就是发给人的链接（同 `format-report-html` 的规则：绝对地址、跟随发布环境 test/prod）。
`download_url` 是"点一下直接下载"的地址。

## 1. 四个接受的类型

| 扩展名 | 类型 | 点开看到 |
| --- | --- | --- |
| `.pdf` | pdf | 浏览器内嵌 PDF（原文件，不转） |
| `.xlsx` | xlsx | 转成表格在线看（每张可见 sheet 一节，前 200 行） |
| `.pptx` | pptx | **转 HTML 预览**（一页一张 1280×720，可滚动），可下载原件 |
| `.docx` | docx | **转 HTML 预览**，可下载原件 |

- ⚠️ **`.ppt` / `.doc` / `.xls` / `.csv` 不接受**：400 并在 `detail` 里告诉你怎么存
  （"Save the file as .pptx and upload that"）。旧格式预览不了，收下只会变成一张打不开的卡片。
- 文件名里的路径会被剥掉（只留 basename），控制字符会被清掉，最长 160 字符。
- 没有扩展名时可以用 `media_type` 指定（`"media_type": "application/vnd…presentationml.presentation"`）。

## 2. 传输格式与限制

- **字节走 `content_base64`**（也接受 `content` / `file_base64` / `data_base64`，或一个完整
  `data:…;base64,…` URI —— data URI 里声明的类型**不被信任**，类型由扩展名决定）。
  与知识页图片同一套做法：你拿的是 API 凭据，不是对象存储凭据，所以**没有**预签名上传地址。
- **上限 24 MB**（原始字节）。超了 413，`detail` 里带着实际大小与上限。
  ⚠️ **没有分片上传**。大 PPT 先压图（PowerPoint 的"压缩图片"）、拆分成两个文件，
  或者干脆改成 HTML 报告 —— 报告上限 8 MB，但它可以在页面里引对象存储的图片。
- 服务端会**校验文件头**：OOXML 必须是 `PK`（zip），PDF 必须是 `%PDF`。扩展名对不上内容 → 400
  （"the sent bytes are not an OOXML file"）。**改后缀名不算换格式**，导出一次再传。

## 3. 卡片出现在哪、怎么被读

- 落在**调用者账号的私人工作区**，和报告同一个网格（同一个搜索、分类、状态、重命名、封面）。
- 卡片上显示：类型徽章（PPTX/XLSX/PDF/DOCX）、文件名、大小；没有封面图时用类型图标占位。
- 点开 = `/r/<slug>`：一个阅读页，正文是**服务端生成的 HTML 预览**，在读者自己的浏览器里渲染
  （**不需要 JS**）。四种类型都走这一条路，**预览的样子照着文件自己的软件来**：
  - **pptx** = 一张张幻灯片卡片，每张 1280×720、上方标 `Slide N`；
  - **docx** = 白纸页面，按文件里**自带的分页符**分成一页一页（上方标 `Page N`）；
  - **xlsx** = Excel 式网格：**顶上有列标 A B C…、左边有行号**，sheet 名在底部标签条上；
  - **pdf** = 每一页光栅化成纸页（和 docx 同一套版式），上方标 `Page N`。
  灰底 + 白纸 + 投影，目的就是"看起来像在 Office 里看"。右上角永远有下载按钮。
  ⚠️ 两个 2026-10-01 的改动，别改回去：
  ① 预览原来由服务端渲染成 PDF，而**容器里没有中文字体**，中文全变成方框；改成 HTML 由读者浏览器
  渲染后，字体来自读者自己的机器。② PDF 原来直接丢给浏览器内置 PDF 阅读器（`<iframe>` 原始文件），
  现在自己光栅化成纸页 —— 原因是**备注层盖不住浏览器插件**，"在第 3 页留个言"必须由我们画。
- **可以像报告一样加备注**（2026-10-01 起）：右键点某张纸/某张幻灯片/某个 sheet → `Add a note here`，
  **备注记在它所属的那一页上**（第 2 页的备注只会画在第 2 页，滚到第 1 页就看不见了），
  记录里带**作者 + 时间戳**，会进文档所有者的 **Inbox**。`/s/{token}` 的访客**能看已有的备注、不能写**
  （写备注要登录会话，那条记录署名到人）。
- 右键菜单对文档卡片只有 **Download .pptx**（没有 Export to PPT/PDF/Picture，也没有 Download HTML）——
  它本来就是文件，导出接口对它返回 422。
- 分享、公共区、拉取副本、`/s/{token}` 免登录链接**全部和报告一样**：
  - `POST api/reports/{slug}/publish` 把卡片发布到公共区（只读快照，别人点"Pull"可拿到自己的副本）；
  - **`{BASE}/r/<slug>` 需要登录**（发给同事用，他有账号）；**要发给没有账号的人，用 `/s/{token}`**
    （由所有者在卡片右键生成，可撤销，免登录、只读、能看预览、能下载）。

### 3.1 预览的保真度（先说清楚，避免"我传的排版去哪了"）

预览是**读内容**用的，不是"像素级还原"：

- pptx：文字（字号/粗斜体/颜色/对齐/项目符号/缩进）、纯色块与边框、圆角、**真表格**（含合并单元格）、
  图片都渲染；**旋转、渐变/图案填充、主题色、组合形状、图表、SmartArt、视频、OLE 对象**
  渲染成中性占位框（图表会把标题带进来）；母版/版式背景与演讲者备注不渲染。**一页幻灯片 = 一张卡片**，
  页数多于 300 时截断并明说。
- docx：标题层级、段落与 run 级样式、项目符号/编号、表格（含横向合并）、图片、分页符都渲染；
  **页眉页脚、脚注、文本框、分栏、段落底纹**不渲染。⚠️ **分页只按文件里自带的分页符切** ——
  没有分页符的长文档是**一张长纸**而不是一叠纸。真分页要重新实现 Word 的断行器，猜错会切掉半行字，
  所以有意不做；**要真正的分页排版就用 HTML 报告**。
- xlsx：值（日期转 ISO、公式取缓存值）、可见 sheet、前 200 行 × 60 列；**千分位会渲染** ——
  单元格自己的数字格式里带 `,`（如 `#,##0`）就按它分组，否则只有 ≥ 10000 的纯数字才分组
  （**这样 `2026` 这种年份永远不会被写成 `2,026`**）。**单元格样式、颜色、合并单元格、列宽、
  图表**不渲染。行号按 Excel 来：**第一行是第 1 行**，所以第一条数据在第 2 行。
- pdf：逐页光栅化（1.5 倍缩放、PNG），所以**高清缩放会糊**；一页最多带 60 页 / 约 9MB 图片，
  超了就截断并写明"showing the first N of M pages"。要原样的 PDF 就下载。
- 结论：**要让读者看到"你要他看到的排版"，用 HTML 报告**；文档卡片的定位是"文件本身"。

### 3.2 怎么写这个文件，读者才读得舒服

预览是"把这几个格子读出来"，所以**文件的内部结构直接决定它能被读懂多少**。四条经验，都来自
实际读出来的效果：

- **数字必须是数字**（xlsx）：把 `18420000` 写成文本 `"18420000"` 或 `"18,420,000"`，预览就只是
  一串字符 —— 不能右对齐、不能求和、也不会被再分组。**给它数字格式**（金额/数量用 `#,##0`），
  千分位就会按你的格式渲染出来；年份、编号这类不要加 `,`。
- **第一行是表头**：预览把每张 sheet 的第一行当表头加粗显示。别把标题合并单元格放在第一行
  （合并单元格预览里只显示第一个值），标题交给卡片的 `title`。
- **sheet 名有意义**：每张可见 sheet 会成为一个带标题的小节，`Sheet1` 对读者没有信息量。
  预览只取前 200 行 × 60 列，**要读者看到的部分放在最前**。
- **pptx / docx 的关键信息要写成文字**：预览是转出来的 PDF，图表、SmartArt、组合形状、动画、
  页眉页脚、文本框都不会出现（见 §3.1）。结论性的句子写进正文段落里，别只放在一张图上。

⚠️ 一个前提：**这些规则是为了"能被读懂"，不是为了排版好看**。真要精确控制版式（分页、栏宽、
字体、配色），用 HTML 报告，别用文档卡片 —— 那张卡片的定位就是"文件本身"。

### 3.3 预览会被缓存

**四种类型的 HTML 预览都不缓存** —— 纯 Python 从存好的字节生成（不开浏览器），毫秒级，每次请求现做
（PDF 光栅化最贵，单页约 100ms）。`GET api/reports/{slug}/document/preview` 会告诉你该渲染什么
（**现在四种类型都答 `{"kind":"html", …}`**，xlsx 额外带 `sheets` 行数据；渲染失败会降级成 `download`
并给出原因，**不会** 500。

（`/document/preview.pdf` 仍然在，仍然能用：它把同一份 HTML 交给无头 Chromium 渲染成 PDF 并按
`updated_at` 缓存 —— 需要一份可打印/可下载的 PDF 时用它。⚠️ **它故意不带备注层**：打印出来的纸没法留言。）

## 4. 更新 / 删除

- **带 `slug` 就是替换**（幂等）：新文件的字节会写成新的对象，旧对象在没有别的副本引用它之后被清理。
- **不带 `slug` 永远新建**（地址自动加 `-YYYYMMDD-HHMM-xxxxxx` 后缀）：连发两次不会互相覆盖，
  但会多一张卡片 —— 更新请务必带上第一次响应里的 `slug`。
- `PATCH api/reports/{slug}`（`PUT`）可以改**标题、摘要、分类、标签、状态、封面**；
  **不能**改 `html` / `kind`（400）—— 换文件请重新 `POST api/reports/documents` 并带上同一个 slug。
- `DELETE api/reports/{slug}` 删除卡片；**当没有任何副本（公共区快照 / 别人的拉取副本）再引用同一份
  字节时**，对象存储里的文件也会一并清理。别指望它当"文件清理工具"用。

## 5. 常见错误与处理

| 码 | `detail` 说什么 | 你该做什么 |
| --- | --- | --- |
| 400 | `'ppt' is not one of the four accepted types… Save the file as .pptx` | 导出成新格式再传 |
| 400 | `the sent bytes are not an OOXML file (zip, 'PK' …)` | 你传的不是这个文件（或被压缩/加密过）—— 重新导出 |
| 400 | `content_base64 is not valid base64` | 用标准 base64（`base64.b64encode`），不要 URL-safe 变体 |
| 413 | `the file is … bytes; the limit is 25165824 bytes (24 MB)` | 压图 / 拆分 / 改用 HTML 报告 |
| 409 | `that slug is an HTML report` | 文档和 HTML 报告不能共用地址，换 slug |
| 409 | `slug belongs to another card` | 那个 slug 不是你的原稿（公共区快照/别人的），换 slug |
| 502 | `the file could not be stored` / `the card could not be saved` | 服务端存储故障，**稍后重试**（卡片没有建出来，不会留下半张卡） |
| 403 | 写操作被拒 | 你的机器凭据只允许写 `/api/reports`、`/api/knowledge`、`/api/calendar/events` 这些前缀；本文的端点就在 `/api/reports` 下，正常可用 |

## 6. 自检 checklist

- [ ] 扩展名是 `.pptx` / `.xlsx` / `.pdf` / `.docx` 之一，且**确实**是这个格式（导出，不是改名）。
- [ ] 文件 < 24 MB。
- [ ] 给了 `title`（不给会用文件名，`Q3_channel-review.pptx` → `Q3 channel-review`）。
- [ ] 给了 `submitter`（同事的名字，卡片上显示谁发来的）。
- [ ] 双语摘要：`summary_en` + `summary_zh` 各一行（卡片悬停时显示；和报告一样，两种语言各给一行）。
- [ ] **更新用同一个 `slug`**，新建才省略 `slug`。
- [ ] （xlsx）**要给人看的数字是数字单元格**、带 `#,##0`（金额/数量）；年份与编号**不要**加千分位。
- [ ] （xlsx）**第一行是表头**，没有合并单元格；sheet 名有意义；要读者看的内容在前 200 行内。
- [ ] （pptx/docx）**结论写进文字**，别只放在图表/SmartArt/文本框里（预览里它们不出现）。
- [ ] （docx）想让内容**分成几页**，就在该换页的地方插一个真正的分页符（Word 里 Ctrl+Enter）——
      预览只按文件自带的分页符切页，不会替你估算。
- [ ] 交完之后如果要改内容，**先 `GET /api/annotations?type=report&slug=<slug>` 看同事留了什么备注**
      （每条带作者和时间），改完再 `PUT`/重新上传；你写的变更不会自动通知他们。
- [ ] 交付说明里把 `url` 或 `download_url` 原样贴给对方（它们已经带好了 test/prod 的环境与挂载点）。