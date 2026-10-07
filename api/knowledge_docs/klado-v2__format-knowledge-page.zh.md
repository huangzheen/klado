---
document_id: klado-v2:format-knowledge-page:zh
title: format-knowledge-page
document_type: format
source: klado-workspace
version: 1.2
tags: [format-knowledge-page, 业务知识库, 知识库, 知识页面, wiki, knowledge, 条目, 笔记, markdown, summary, 摘要, 双语, bilingual, 中英, 冲突, 重复, 去重, 发布, 检索, internal-only]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/format-knowledge-page.md
---

> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造。本文只讲**怎么写业务知识库的页面**。

# 业务知识库页面：怎么写、怎么改

**两套知识库别搞混**：

| 语料 | 是什么 | 检索 `scope` | 谁来写 |
|---|---|---|---|
| **项目使用说明书** | 平台怎么用：能力、路由、接口、格式规范（本文就属于这一套） | `curated` | 维护者，随镜像部署 |
| **业务知识库**（wiki） | 业务知道什么：定义、口径、方法、市场与渠道笔记 | `business` | **人 + 他们的 agent，逐账号隔离权限** |

⚠️ **`/api/ai/knowledge/search` 的默认 `scope` 是 `all`（两份都搜）**，自 2026-10-01 起。不传 scope 时业务知识也会被搜到，但**只搜这个账号有权限看的**（自己的 / 公共快照 / 分享给我的）。

写入业务知识库 = `POST /api/knowledge/items`（改自己的页面用 `PUT /api/knowledge/items/{slug}`）。

---

## 1. 三条硬规则

### 1.1 `summary` 必须写 —— 双语的页面就写**两条**

`summary` 是**一行摘要**，不是可选项：

- 它是**页面列表悬停时看到的那几行** —— 没有它，鼠标停在标题上只有标题和提交人，读者不知道点进去是不是自己要找的。
- 它是**检索结果里的那一行** —— 没有 summary 的页面在选择列表里等于没有说明。
- 写"这一页讲什么、给谁看"，不要写"本文介绍了…"这种空话。

**双语页面用 `summary_en` + `summary_zh`，两个都必填**（与报告同一条规则，见
`klado-v2:format-report-html:zh` §1.5）：

| 字段 | 什么时候用 |
|---|---|
| `summary_en` | 英文一行摘要 |
| `summary_zh` | 中文一行摘要 |
| `summary` | **单语页面**用；双语页面可以不传，服务端会取 `summary_en` |

- ⚠️ 只要 `body` 里同时有 `<!-- lang:zh -->` 和 `<!-- lang:en -->`，两个就都是必填，缺任何一个回 **400**，
  报错正文会点名缺哪个。只传 `summary` 不够 —— 那等于给中文读者配了英文摘要。
- 悬停提示里会**两行都显示**，所以两行都要写成"一句话"，不要写成两句话。
- 单语页面（只有一种语言）不受影响，继续用 `summary`。

### 1.2 必须中英双语，读者可切换

每一页**同时装中文和英文两版**，写在**同一个 `body`** 里，用标记行分隔：

```markdown
<!-- lang:zh -->
## 渠道口径

线上（自营）与线下（经销）分开统计；口径以本项目既有的同名页面为准。

<!-- lang:en -->
## Channel scope

Online (direct) and offline (distributor) sales are counted separately; the definitions
follow the existing page on the same subject in the business knowledge base.
```

规则：

- **标记行只有两种**：`<!-- lang:zh -->` 和 `<!-- lang:en -->`（大小写不敏感，`zh-CN` / `en-US` 也认）。
  用**行首独立的 HTML 注释**，两侧可以有空格。
- **第一个标记之前的内容是共享的**，两种语言都显示 —— 适用于表格、字段名清单、链接这类无需翻译的东西。
- **顺序：先中文、后英文**（读者默认看到中文，可一键切到 EN）。
- 阅读页标题右侧会出现 **`ZH | EN` 切换按钮**；**只有一页里真的有 ≥2 个语言段时才出现**，
  单语页面直接原样显示、不摆一个点了没用的开关。
- 标记行**不进正文**：渲染前就被摘掉，不会当作文本显示。编辑器里它显示成一枚 `ZH` / `EN` 小徽标，
  **保存时原样写回** —— 所以双语页面可以放心在网页里编辑。

### 1.3 改页面必须同时改两版

和报告同一条规则（见 `klado-v2:format-report-html:zh` §1.5）：

> **同一个 `PUT` 里必须把 ZH 和 EN 都改到**，不允许只改当前读到的那一版。

只改一版会留下"中文写着 A、英文还写着 B"的页面：两个读者拿到不同结论，而**没有任何东西会提醒你**。
新增一节 = 两节；删一条 = 删两条；改一个数 = 两处都改。

---

## 2. 写之前：先查有没有写过

**这是流程的一部分，不是可选的礼貌。** 页面列表是"这个账号能读到的全部业务知识"，
同一个主题被两个账号各写一遍，检索时两份都在、还可能互相矛盾 —— 这是最坏的结果：知识库越写越不可信。

```
GET /api/ai/knowledge/search?q=<主题>&scope=business
```

- 命中**同一主题**的页面 → **不要直接写**。把命中的页面（标题 + slug + 摘要）摆给用户看，
  说清"已有这一页、你要写的是不是同一件事"，然后：

  | 情况 | 怎么做 |
  |---|---|
  | 已有的是**自己**的页面 | 直接 `PUT` 更新那一页，别新建 |
  | 已有的是**别人的**、内容确实冲突 | 问用户：更新别人的（需要对方授权）还是另起一页 |
  | 确实是**不同主题**，只是名字像 | 写成新页面，但标题要能区分开 |

- **服务端也拦一道**：新建（`POST`）时，如果这个标题已经存在于**本账号能读到的**别的页面上，
  返回 **409**，`detail.conflicts` 里列出冲突页面（`slug` / `title` / `owner_email` / `visibility`）。

  ```json
  {"detail": {"error": "conflict", "message": "a page with this title already exists",
              "title": "渠道口径速查",
              "conflicts": [{"slug": "channel-caliber-notes", "title": "渠道口径速查",
                             "owner_email": "someone@example.com", "visibility": "public"}]}}
  ```

  收到 409 时**不要原样重发**。先把这个列表给用户看、等用户明确说"另起一页"，
  再带上 `"confirm_conflict": true` 重发。**没有用户确认就不要带这个字段。**
  （只拦 `POST`；`PUT` 更新自己的页面不受影响。）

---

## 3. 字段与写法

```bash
curl -sS -X POST "$BASE/api/knowledge/items" "${AUTH[@]}" -H 'Content-Type: application/json' -d '{
  "title": "渠道口径速查",
  "summary": "线上/线下渠道的归属与口径速查，给做渠道复盘的同事",
  "category": "渠道",
  "tags": "渠道,口径,速查,线上,线下",
  "document_type": "channel",
  "body": "<!-- lang:zh -->\n## 归属\n…\n\n<!-- lang:en -->\n## Scope\n…\n"
}'
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `title` | ✅ | 页面标题。它**就是**页面的大标题 —— **正文里不要再写一遍 `# 同样的标题`** |
| `summary` | 单语页面 | 一行摘要，见 §1.1 |
| `summary_en` / `summary_zh` | 双语页面（两个都必填） | 中英各一行；缺一个回 400 |
| `body` | ✅ | **只有 markdown**，见下 |
| `tags` | 强烈建议 | 逗号分隔。**tags 是最强的检索信号（权重约是正文的 10 倍）**，不写 tags 等于搜不到 |
| `category` | | 左侧目录的分组名（如 `渠道` / `产品` / `测试`） |
| `document_type` | | `note` / `definition` / `method` / `market` / `product` / `channel` / `other` |
| `status` | | `published`（默认）/ `draft` |
| `submitter` | | **提交人的英文名或拼音**（如 `Zhen Huang`），不是中文名 —— 悬停提示里展示的就是它 |
| `slug` | | 可选。省略时由标题生成；带上已有 slug = 覆盖更新那一页 |

**正文**：

- **markdown only**：HTML 文档、二进制、空正文一律 **400**。单个页面 ≤ 256 KB。
- **标题只写一次**：页面大标题由 `title` 字段渲染。**正文最开头那一行 `# …` 会被自动去掉** ——
  它按定义就是同一个大标题（双语页面里**每个语言段**的开头也一样）。所以正文请从 `##` 起，
  不要在正文里再写一遍标题。
- **正文里的 HTML 会被转义后当文本显示**，不会执行 —— 页面是给别人读的，不接收任何标记。
- 支持的代码块语言：`sql` / `json` / `bash` / `python` / `javascript` / `yaml`，
  以及 `mermaid`（会渲染成流程图；语法写错时显示源码而不是空白）。**不认识的语言不会被瞎猜**。
- 表格、引用、任务清单都用标准 GFM 写法即可。

### 3.1 图片：先上传拿链接，再在正文里定尺寸

页面可以插图，但**图必须有一个读者访问得到的地址**，所以先上传：

```bash
# 1) 上传本地图片，拿回一个相对地址（agent 凭据即可）
curl -sS -X POST "$BASE/api/knowledge/assets" "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d '{"filename": "渠道结构.png", "content_base64": "'"$(base64 < 渠道结构.png)"'"}'
# → {"path":"knowledge-assets/20260930-…-渠道结构.png",
#    "url":"/api/storage/serve?path=knowledge-assets%2F…",
#    "markdown":"![caption](/api/storage/serve?path=knowledge-assets%2F…)"}
```

- **只接受 PNG / JPEG / GIF / WebP**，≤ 8 MB。类型是**按字节嗅探**的，不是看文件名。
- 返回的地址是**相对路径**（`/api/storage/serve?path=…`）：浏览器按挂载点解析，导出时服务端
  也按同一个挂载点解析 —— **不要把返回的地址改写成带域名的绝对地址**。
- 也可以直接引用**已有的**图片地址（例如 `product-images/…` 里的产品图）。

**尺寸就写在图片后面**，一个花括号：

```markdown
![渠道结构](/api/storage/serve?path=knowledge-assets%2F…){width=560}
![半宽示意](/api/storage/serve?path=knowledge-assets%2F…){width=50%}
```

| 写法 | 含义 |
|---|---|
| `{width=560}` | 560 像素（不写单位就是像素） |
| `{width=50%}` | 正文栏宽度的 50% |
| 不写 | 按原始尺寸，最大不超过栏宽 |

- ⚠️ **markdown 本身没有尺寸语法，行内 `<img>` 也不行**（正文里的 HTML 会被转义成文字）。
  这个花括号是本项目的约定，写在**图片紧后面**、中间不许有别的东西。
- 标记**不会显示给读者**，渲染时就被摘掉了；编辑器里也看不到它，但**保存时会原样写回**，
  所以网页上编辑一次不会把尺寸弄丢。
- **导出（Word / PDF / 图片）按同一套尺寸复刻**：`width=50%` 在 Word 里就是正文栏的一半宽，
  不会变成满幅。装了 Word 的同事打开就是你在页面上看到的样子。

---

## 4. 权限：哪些能写、哪些必须人来点

| 动作 | 谁能做 | 端点 |
|---|---|---|
| 新建 / 改自己的页面 | **人和 agent 都能**（agent 改的是凭据所属账号的知识） | `POST` / `PUT /api/knowledge/items[/{slug}]` |
| 发布到公共区（只读快照） | **只限浏览器会话** | `POST /api/knowledge/items/{slug}/publish` |
| 撤回公共快照 | 只限浏览器会话 | `DELETE /api/knowledge/items/{slug}/public` |
| 拉一份别人的公共/共享页面到自己的库里 | 只限浏览器会话 | `POST /api/knowledge/items/{slug}/pull` |
| 给同事开/关读权限 | 只限浏览器会话 | `POST` / `DELETE /api/knowledge/items/{slug}/shares` |

- 公共区、共享、pull 这三件事都会**扩大别人的可见范围**，所以 agent 凭据一律 **403** —— 
  被要求做这些时，明确说"这需要在网页里点"，不要重试、不要声称已成功。
- **读不到 = 404，不是 403**：本账号不能读的 slug 和"不存在"给同一个答案，避免探测别人有没有某页。

**导出**（页面右键菜单，与 Workspace 卡片菜单同一套，只把 "Export to PPT" 换成 "Export to Word"）：
`Export to Word` / `Export to PDF` / `Export to Picture` / `Download markdown`。

- 导出的是**读者当前选中的那个语言**那一版；图片、**图片宽度**、代码高亮、表格都跟着走。
- ⚠️ 正文由**浏览器渲染好之后**提交给 `POST /api/knowledge/items/{slug}/export/{word,pdf,picture}`
  —— 服务端**不重新实现一遍 markdown**。两套渲染器必然漂移，这个仓库已经为此吃过亏
  （见 `may_read` 与 `READABLE_PREDICATE`）。所以这三个接口是 POST + 正文，不是 GET。
- Word 导出是一个 `.doc`（内容是 Word 版式 HTML，**图片以 data URI 内嵌**，所以文件自带图、离线也看得到）：
  标题、表格、图片宽度都在，能直接用 Word 打开和打印；但标题/表格走的是 Word 的 HTML 导入，
  不是原生的 Word 样式对象 —— 拿来读没问题，拿来重新排版要另说。

---

## 5. 交付前自检

1. `summary` 写了吗？是"给谁看、讲什么"，不是"本文介绍了…"？**双语页面 `summary_en` 和 `summary_zh` 都写了吗？**
2. 双语两版都在吗？**这一版改动的每一处，另一版也改了吗？**
3. 写之前 `scope=business` 搜过了吗？有冲突的话，**用户确认过了吗**（`confirm_conflict` 只在确认后带）？
4. 正文开头是不是又写了一遍标题？去掉它。
5. `tags` 写了吗？中文关键词有没有覆盖到业务里的口语说法？
