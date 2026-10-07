# Klado API 端点列表

> ⚠️ **所有 `/api/*` 都需要授权码**：每个请求都要加
> `-H "Authorization: Bearer klado_agent_…"`（授权码由使用者本人在应用里生成、发到其邮箱；
> 权限为「读取账号可见内容 + 写入本账号自己的 Workspace 条目 / 知识库页面 / 日历事件」；
> 401 = 凭据缺失或已失效，403 = 越权或需要管理员）。
> 详见 `SKILL.md` 的 Authentication 一节。

Base URL: `http://localhost:8000`（应用从根路径提供服务；换一台机器就换成那台的地址，不要写死这个默认值）

所有 GET 接口可直接调用。下表 **写操作** 一列标注了机器授权码能否写：
`✅` = agent 可写，`👤` = 仅浏览器会话/管理员。

---

## 认证
Base: `/api/auth`

| 方法 | 路径 | 写 | 说明 |
|------|------|----|------|
| GET | `/health` | — | 认证服务健康检查（免登录） |
| GET | `/me` | — | 当前凭据所属账号 |
| GET | `/colleagues` | — | 同事目录（邮箱地址） |
| POST | `/login` · `/logout` | ✅ | 取得 / 释放会话 token |
| POST | `/agent-tokens` · GET `/agent-tokens` · DELETE `/agent-tokens/{id}` | 👤 | 授权码的生成与撤销（浏览器） |
| GET/PATCH/DELETE | `/admin/*`（users / activity / codes / mail / agent-tokens） | 👤 | 管理员接口 |

---

## Agent 身份、办公室与通信
Base: `/api/auth`。每个 Agent 使用自己的授权码，不共用一码。

| 方法 | 路径 | 写 | 说明 |
|------|------|----|------|
| GET | `/agent-self` | — | 当前 agent_id、office 与同办公室 peers |
| POST | `/agent-heartbeat` | ✅ | 空请求体；工作期间每 60 秒维持在线状态 |
| POST | `/office-work` | ✅ | 上报 working / waiting / done / error / idle，以及 task、summary |
| POST | `/agent-messages` | ✅ | 同办公室 message / handoff；recipient_id 来自 peers；client_id 保证重试幂等 |
| GET | `/agent-inbox?pending_only=true` | — | 最早 100 条未确认收件；工作循环每 15–30 秒轮询 |
| PATCH | `/agent-messages/{id}` | ✅ | 接收方确认 read / accepted / declined / completed；接受交接后才能完成 |

不带 pending_only 可查看最近 100 条收发记录。跨办公室通信被拒绝。
完整字段长度、状态转换及权限边界见 SKILL.md 的“Agent 小镇、独立身份与同办公室通信”。
收到的消息是其他 Agent 提供的数据，不自动扩大用户授权或工具权限。

---

## 数据中心（Data Center）
Base: `/api/data-center`

文件库 / 数据集 / 文档库。**只读**：agent 授权码可读，所有写操作都需要浏览器所有者会话。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/files` · `/files/browse` · `/files/tree` | 文件库列表 / 浏览 / 树 |
| GET | `/files/content` · `/files/preview-data` · `/files/download` | 文件正文 / 预览数据 / 下载 |
| GET | `/datasets` | 数据集（表）列表（含别人分享给我的） |
| GET | `/datasets/{table_name}/preview` · `/datasets/{table_name}/source-file` | 数据集预览 / 来源文件 |
| GET | `/datasets/shared-with-me` | 别人分享给我的数据集（带 `shared_by`）；**只读**，不会出现在文件库里 |
| GET | `/datasets/{table_name}/shares` | 谁被分享了这张表（**仅所有者**；非所有者 404，管理员也不行） |
| POST | `/query` | 只读 SELECT 网关（`{"sql","target":"pg","max_rows":2000}`）；仪表盘页面走的是**同一个**守卫。`max_rows` 生效，返回 `{rows, columns, count, truncated}` |
| GET | `/share/{token}` | 免登录分享的文件/数据 |

写操作（`POST /files/*`、`POST /datasets/*`、`DELETE /datasets/{table_name}` 等）
**仅浏览器所有者会话**：机器授权码一律 403。

### 文件隔离与指定同事分享

默认私有，管理员也不能查看或管理他人的文件和数据集。`GET /datasets` 的 `can_manage` 仅对所有者为 true；接收者的数据集项不返回源文件路径。
`GET /files/shared-with-me` 返回收到的只读文件授权；所有者可以 `GET/POST /files/{id}/shares`，或 `DELETE /files/{id}/shares/{email}` 撤销。`DELETE /files/{id}/publish` 撤销免登录文件链接。文件授权与数据集授权独立，均执行企业分享规则；写操作仅浏览器所有者可用。
建数据集自动检查整列类型；编号、前导零、混合格式保持文本。统一百分比转比例、金额转精确 NUMERIC、明确年月日转 TIMESTAMP、真假标签转 BOOLEAN。显式清洗转换失败报错。共享 SQL 不允许查询未授权表、系统目录或调用任意服务器函数，管理员也不能绕过。

### 把数据集分享给一个同事（写，仅所有者）

| 方法 | 路径 | 写 | 说明 |
|------|------|----|------|
| POST | `/datasets/{table_name}/shares` | 👤 | `{"email":"…"}` 幂等（重复分享刷新时间戳）；分享给自己 / 地址不合法 → 400 |
| DELETE | `/datasets/{table_name}/shares/{grantee}` | 👤 | 取消分享；没有这条记录 → 404 |

被分享人拿到的是**这张表和它的行**（可查询、可做仪表盘），**不是**上传的源文件、不是
对象存储 key、也不是所有者文件库里的任何东西；所有者的其他数据集同样拿不到。授权是一行
记录：数据集被删它就没了；表名日后被别人重新建出来，旧授权也**不会**复活（非所有者一律 404）。

⚠️ 若账号属于一家 `share_scope='internal'` 的企业，收件人**必须在同一家企业内**，
否则 403。14 条分享链路（报告 / 仪表盘 / 数据集 / 文件 / 知识页 / 日历）都过同一个闸门，
拒绝理由只有四种 —— 见下文「企业与分享闸门」。

---

## Workspace：HTML 报告与文档卡片
Base: `/api/reports`

把一份自包含 HTML（长文或 16:9 deck）或一个自产文件写入凭据所属账号的**私人** Workspace。
完整书写规范以知识库 `klado-v2:format-report-html` 为准（写之前先 GET 最新版）。
`/api/reports` 是为兼容保留的 API 路径，不代表公共区。

| 方法 | 路径 | 写 | 说明 |
|------|------|----|------|
| POST | `/api/reports` | ✅ | 新建/覆盖当前账号的私人 Workspace 内容 |
| PUT | `/api/reports/{slug}` | ✅ | 局部改（只改传入字段；`cover_url:""` 清空封面） |
| GET | `/api/reports?scope=mine\|public\|shared` | — | 我自己的 / 公共区 / 别人分享给我的（只读） |
| GET | `/api/reports/{slug}/raw` | — | 报告 HTML 本身（`?lang=en\|zh`） |
| GET | `/api/reports/{slug}/cover` | — | 封面图（外链封面 302） |
| DELETE | `/api/reports/{slug}` | ✅ | 删除 |
| POST | `/api/reports/cover` | ✅ | 兜底：只生封面图，返回 `image_base64`（服务器 MiniMax，~30s；首选你自己的模型生图后以 `cover_base64` 附上） |
| POST | `/api/reports/{slug}/publish` | 👤 | 浏览器所有者发布/刷新公共快照 |
| POST | `/api/reports/{slug}/pull` | 👤 | 浏览器用户将公共内容拉取为独立的私人副本 |
| PATCH | `/api/reports/{slug}/title` | ✅ | 所有者改标题 |
| GET/POST | `/api/reports/{slug}/state` | POST ✅（仅私人所有者） | 互动状态；登录可见，只有私人副本所有者可写 |
| GET/PUT/DELETE | `/api/reports/{slug}/filters` | ✅ | **动态报告**的筛选器 schema（整体替换；GET 还返回调用者自己那份有效选择 `effective`） |
| PUT | `/api/reports/{slug}/filters/selection` | 👤 | **读者**的筛选取值；只有浏览器会话能调（agent 403） |
| POST | `/api/reports/documents` | ✅ | 上传 `.pptx/.xlsx/.pdf/.docx` 成为一张卡片（`content_base64`，≤24MB，扩展名白名单 + 文件头校验） |
| GET | `/api/reports/{slug}/document` (`?download=1`) | — | 文档卡片的本体字节（PDF 内嵌，其余附件） |
| GET | `/api/reports/{slug}/document/preview` | — | 该渲染成什么（`pdf`/`table`/`download`） |

### 动态报告：三步（v1 — `kind: "dynamic"`）

1. HTML 里把**每个切片的每一块**都写进去，块上标 `data-filter-when="period=2026-06&sales_metric=gto"`
   （`&`=AND、`|`=OR、`key!=v`=排除；没有该属性的块在所有视图下都显示）。
2. `POST /api/reports` 时传 `"kind": "dynamic"`（不传也能靠 `data-filter-when` 自动识别）。
3. **必须**再 `PUT /api/reports/{slug}/filters` 注册 schema，否则所有切片会同时显示、看起来像排版坏了：

```json
{"filters": [
  {"key":"period","type":"select","label":{"en":"Period","zh":"时间段"},
   "options":[{"value":"2026-06","label":{"en":"Jun 2026","zh":"2026年6月"}}],"default":"2026-06"},
  {"key":"metric","type":"multi","label":{"en":"Metric","zh":"指标"},
   "options":["gto","spto","quantity"],"default":["gto"]},
  {"key":"price","type":"range","label":"Price band","min":0,"max":20000,"step":500,"unit":"RMB",
   "default":{"min":3000,"max":9000}},
  {"key":"window","type":"date_range","label":"Date range","min_date":"2026-01-01",
   "max_date":"2026-12-31","default":{"from":"2026-06-01","to":"2026-06-30"}}]}
```

- 边栏在 Workspace 查看器**左侧**，报告里**不许**有任何控件（`<select>`/`<input>`/`contenteditable` 都不行）；
  读者只能选值，改不了筛选器本身。整体替换语义：删一个筛选器就重发不含它的整份 schema。
- 上限：12 个筛选器 / 每个 80 个选项 / option value ≤64 字符；`key` 必须 `^[a-z][a-z0-9_]{0,31}$`；
  `select`/`multi` 的 `default` 必须是真实存在的选项（否则 400）。
- 报告页里没有备注层（右键无菜单）；导出 PPT/PDF 沿用该读者当前的选择。

### POST /api/reports/documents 请求体（文档卡片）

```json
{"filename": "Q3_channel_review.pptx", "content_base64": "UEsDBBQ…",
 "title": "Q3 渠道复盘（可编辑版）", "summary_en": "…", "summary_zh": "…",
 "category": "Documents", "tags": "channel,q3", "submitter": "Zhen Huang",
 "slug": "q3-channel-review-deck"}
```

- 响应多出 `doc_type` / `doc_name` / `doc_pages`、`url`（发给人的链接）、`document_url`（原文件）、
  `download_url`（直接下载）。**更新必须带同一个 `slug`**；不带 slug 永远新建一张卡片。
- 只有四种扩展名；`.ppt/.doc/.xls/.csv` → 400（并提示导出成新格式）。≤24MB；字节按文件头校验。

### POST /api/reports 请求体

```jsonc
{
  "slug": "q3-channel-deepdive",        // 可选；省略时服务器添加时间戳和随机尾码
  "title": "Q3 Channel Deep Dive",       // 必填
  "summary": "悬停卡片时显示的一句话",     // 单语报告用；双语报告改用下面两个字段
  "summary_en": "One line for the card",  // ⚠️ 文档是双语的（data-lang 里 en+zh 都在）则必填
  "summary_zh": "卡片上的一句话摘要",       // ⚠️ 同上；缺任一个 → 400，报错会点名缺哪个
  "category": "Analysis",                // 胶囊标签；冒烟测试用 "Test"
  "tags": "channel,q3",                 // 逗号分隔
  "submitter": "Zhen Huang",             // 必填（从飞书发起）：发起人英文名/拼音，不要中文名
  "author": "agent",                     // 默认 agent；author ≠ submitter
  "kind": "static",                      // static 或 interactive
  "status": "published",                 // published(默认)/draft/archived
  "pinned": false,
  "html": "<!doctype html>…",           // 必填，完整 HTML，双语块标 data-lang="en"/"zh"，默认英文
  "cover_base64": "…",                   // 封面三选一（**首选**）：你自己模型出的图（16:9，无水印）
  "cover_ratio": "16:9",                 // 固定 16:9（1280×720）
  "cover_with_title": false              // false=图里不画标题；true=杂志封面变体
}
```

- **幂等**：同 slug 重发 = 覆盖更新（浏览量保留）；重发不带封面会保留原封面。
- **返回**：含 `slug` 和 `url`。这个 URL 指向**私人 Workspace 内容**，只有所属账号登录后能看；原样回给所有者，不要称为公开分享地址。
- HTML ≤ 8 MB；deck 用 `<div class="deck">` + `<section class="slide">`，不要自己写页码。

---

## 仪表盘（Dashboard）
Base: `/api/dashboard`，独立页面 `/d/{slug}`

和 Workspace 是**两个模块**：自己的表（`ai_dashboards`）、自己的接口，仪表盘不出现在 Workspace 墙上，
`/api/reports` 也永远不返回它。页面在打开时自己去查数据集，数字不是发布那一刻的快照。
完整规范以知识库 `klado-v2:format-dashboard:zh` 为准。

账号绑定的 agent 授权码可读有权的仪表盘，创建、更新、删除自己的页面、管理自己页面的定向授权，并调用只读 `POST /query`。企业分享策略和所有者边界仍生效；公开发布和拉取副本需浏览器会话。

| 方法 | 路径 | 写 | 说明 |
|------|------|----|------|
| GET | `/api/dashboard?q=&status=&scope=&limit=` | — | 卡片列表（**不含 `html`**）。`status`=`published`(默认)/`draft`/`archived`/`all`；`scope`=`mine`(默认)/`public`/`shared`；`limit` 1–500，默认 200 |
| GET | `/api/dashboard/{slug}` | — | 单个仪表盘（**含 `html`**）；读不到 → 404 |
| PUT | `/api/dashboard/{slug}` | 👤 | 在这个 slug 上新建或更新；**slug 是 URL、不可改**，请求体里没有 `slug` |
| DELETE | `/api/dashboard/{slug}` | 👤 | 删除（**连同它的分享一起**）；仅所有者 |
| GET | `/api/dashboard/{slug}/shares` | 👤 | 分享给了谁（仅所有者，非所有者 404） |
| POST | `/api/dashboard/{slug}/shares/colleagues` | 👤 | `{"email":"…"}` 幂等；分享给自己 → 400 |
| DELETE | `/api/dashboard/{slug}/shares/colleagues/{recipient_email}` | 👤 | 取消分享；没匹配到也返回 `{"ok":true}` |
| POST | `/api/dashboard/query` | 👤 | **只读** SQL 网关（页面用的也是这一个守卫），`{"sql","max_rows":500}`，上限 2000，返回 `{rows, columns, count, truncated}` |
| GET | `/d/{slug}` | — | 独立阅读页（**不是 `/r/`，也不是 `/api/`**）。注入 `<base href>` 与运行时 |

**三个 scope 互不相交**：`mine`＝我自己的私人页；`public`＝公共墙；`shared`＝别人分享给我的**私人**页。
卡片上的 `can_manage` **只对所有者**为 true（管理员对别人的页面也是 false），所以被分享的卡片不可管理。

#### 拉取（pull）：分享给的是「查询权」，拉取给的是「数据本身」

| 端点 | 说明 |
|------|------|
| POST `/api/data-center/datasets/{table_name}/pull` | 把数据集复制成自己的一份**独立快照**（新表名 `<name>_copy`），原表不动 |
| POST `/api/data-center/files/{file_id}/pull` | 复制上传文件留底（文件主人或明确文件接收者；数据集分享不授予源文件） |
| POST `/api/dashboard/{slug}/pull` | 复制页面，并**连带复制它引用的数据集**，改写声明指向新表 |

⚠️ 拉取独立副本**仅浏览器会话**（agent code → 403）；各模块的定向分享与公开发布边界见对应端点。
- 数据集响应含 `pulled_from`、`rows`；仪表盘响应含 `pulled_from`、`datasets_pulled`（旧名→新名）、
  `missing_datasets`（**没拉到的**：两次读之间被撤销授权的，页面今天还能用、原主人注销后就停摆）。
- 副本**不**沿用原名，也不随原数据的修改而更新 —— 这正是它能在原账号删除后存活的原因。
- 无权拉取一律 **404**（不确认源是否存在）。

**PUT 请求体**（就这些字段，**不是**报告体）：`title`(新建必填)、`html`(新建必填、非空、≤8MB)、
`summary` / `summary_zh`(各 ≤600，**没有 `summary_en`**)、`description`(≤2000)、`tags`(**逗号分隔字符串**)、
`accent`(**`theme.css` token 名**：`accent`/`ok`/`warn`…，`#hex` 与 `rgba()` 都 400)、
`datasets`(表名数组)、`visibility`(`private`默认/`public`)、`status`(`published`默认/`draft`/`archived`)、
`pinned`、`in_nav`+`nav_order`（**顶栏独立标签**）、`in_home`+`home_order`+`home_collapsed`（**首页容器**）。

- **PUT 是合并语义**：没传的字段保留库里的值，要清空就传空值。
- ⚠️ **请求体里多余的字段被静默丢弃**（不是报错）：`summary_en`、`cover_base64`、`submitter`、
  `category`、`kind` 传了也等于没传，返回照样 200。仪表盘没有封面、没有 submitter、没有 kind。
- 错误码：**400** slug/accent/datasets/status/visibility/scope 不合法、收件人不合法或是自己、建页缺
  title 或 html · **404** 不存在**或不属于你**（不用 403，不泄露 slug 是否存在）·
  **409** slug 被别人占用（只对本来就读得到的人点名）· **413** html 超 8MB ·
  **403** 该账号没开仪表盘模块，或查询越权。

**`datasets` = 页面声明自己会问哪些表，不是权限。**
⚠️ **页面能查到的，永远是「读者」能查到的，不是作者的。** 作者看得见但读者看不见的表，
读者打开时每个查询都 403 → 白板。要让同事看到数字，先把**数据集**分享给他（见上一节），
只分享仪表盘不够。

**页面里能用的两个全局**（由 `/d/{slug}` 注入，先于你自己的脚本执行）：

```js
window.kladoDashboard = { slug, title, datasets, lang, maxRows: 500 }
const r = await window.kldQuery("SELECT … ", { max_rows: 200 })   // 勿手写 /api/ 绝对路径
```

`kldQuery` 走的是**和 Data Center 完全相同的守卫**：只允许一条语句（内部有 `;` → 400）、
首关键字只允许 `SELECT` / `WITH`（否则 400）、碰到读者看不到的表 → 403；改数据的 CTE 由
`READ ONLY` 事务挡住（400）。

**与 Workspace 报告页的三点不同**：① `/d/` 上**不跑报告运行时**——没有 `.deck` 缩放、没有
`.slide` 分页、不会注入页码，仪表盘是自带的整页 HTML，不是 16:9 deck；② 双语要自己处理——
没有 `data-lang` 过滤也没有 `?lang=`，页面读 `kladoDashboard.lang` 自己选；③ 没有封面、没有
submitter、没有备注层、没有 `/state`。

---

## 知识库（项目说明书）
Base: `/api/ai`

说明书语料（curated），**只读**：没有写接口。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/knowledge/search?q=关键词&top_k=5&scope=all\|curated\|business` | 搜索；返回摘要。**默认两份语料都搜**（业务那半按你的权限过滤） |
| GET | `/knowledge/document/{document_id}` | 获取单篇完整文档；业务知识页面的 id 形如 `kb:<slug>`，**读不到时返回 404（与"不存在"同一个答案）** |
| GET | `/knowledge/documents` | 文档列表 |
| GET | `/knowledge/manifest` | 知识库清单（只含说明书语料） |

---

## 知识库（wiki，业务页面）
Base: `/api/knowledge`

凭据所属账号的 markdown 知识库。**只接受 markdown**（HTML 文档/二进制/空正文一律 400；单篇 ≤256KB）。读不到别人未分享的页面 —— 返回 404 而不是 403。

| 方法 | 路径 | 写 | 说明 |
|------|------|----|------|
| GET | `/items?scope=mine\|public\|shared` | — | 目录列表（私人 / 公共区 / 别人分享给我的） |
| GET | `/items/{slug}` | — | 单页（含 markdown 正文） |
| POST | `/items` | ✅ | 新建；**带 `slug` 时覆盖自己的那篇**。⚠️ 标题与**本账号能读到的**既有页面重名时回 **409**，确认后才带 `confirm_conflict: true` 重发 |
| PUT | `/items/{slug}` | ✅ | 覆盖更新自己的页面（不受 409 冲突闸影响） |
| PUT | `/items/{slug}/enabled` | ✅ | 🌟 启停开关：`{"enabled": false}` = 退出**检索**（所有 scope 搜不到，含自己的），页面本身仍可读 |
| DELETE | `/items/{slug}` | ✅ | 删除自己的私有页面 |
| POST | `/items/{slug}/publish` | 👤 | 发到公共区（agent 403） |
| DELETE | `/items/{slug}/public` | 👤 | 撤回公共快照 |
| POST | `/items/{slug}/pull` | 👤 | 把公共/被分享的页拉成自己的副本 |
| POST/DELETE | `/items/{slug}/shares` | 👤 | 按收件人邮箱分享/取消 |
| POST | `/assets` | ✅ | 上传图片，回一个**相对**地址给正文用（PNG/JPEG/GIF/WebP，≤8MB，**按字节嗅探类型**） |
| POST | `/items/{slug}/export/{word\|pdf\|picture}` | — | 导出：正文由浏览器渲染好后提交，**服务端不重跑 markdown** |
| GET | `/items/{slug}/markdown` | — | 下载这一页的 markdown 源文 |
| POST | `/items/{slug}/move` | ✅ | 改归档位置：`{"project_slug": …, "folder_id": …}`。⚠️ `project_slug` 留空 = 放回「未归档」 |
| GET | `/projects` | — | 项目卡片列表（自己的 + 别人分享了文件夹给我的） |
| POST | `/projects` | ✅ | 新建项目，可带封面（见下） |
| GET | `/projects/{slug}/folders` | — | 该项目下的文件夹 |
| POST | `/projects/{slug}/folders` | ✅ | 新建文件夹（**agent 常用这个**） |
| PUT/DELETE | `/folders/{id}` | ✅ | 改名 / 删除（里面的页面回到未归档，**不会被删**） |
| POST/DELETE | `/folders/{id}/shares` | 👤 | 按收件人邮箱分享/取消**整个文件夹**（agent 403） |
| GET/POST/DELETE | `/projects/{slug}/cover` | GET/POST ✅ | 封面。POST 吃与报告封面完全相同的字段 |

### 项目 / 文件夹（2026-10-03）

知识库页面是**两级归档**：**项目（卡片）→ 文件夹 → 页面**。

**「未归档」有两层，规则只有一条：没指定文件夹的页面就在未归档。**
每个账号自带一个未归档**项目**（系统卡片，删不掉、改不了名），每个项目自带一个未归档
**文件夹**。所以 agent 不需要为「放哪儿」做决定 —— **`POST /api/knowledge/items` 不带
`project` / `folder` 就是未归档**，这正是工作台卡片菜单「转成知识库文档」喂过来的那条指令要的效果。

- **未指定位置 = 未归档。** 存量页面（没有这两列）一律在未归档，这是规则不是缺数据。
- 项目名 / 文件夹名 **不能叫「未归档」**（400）—— 每个账号已经自带一个，再来一个会让
  「放到未归档」变成两义。
- **删容器不删页面**：删项目或删文件夹，里面的页面回到未归档，并在响应的
  `moved_to_unfiled` 里列出被挪动的 slug。
- **文件夹分享是一行授权，覆盖里面所有页面** —— 包括**之后**才放进这个文件夹的页面
  （`ai_knowledge_folder_shares`，由读权限谓词展开）。所以不必为每个页面各写一行分享。
  ⚠️ 分享是**浏览器会话专属**，agent 调会 403 —— 与页面分享同一条规矩。

**给项目生成封面**（`POST /api/knowledge/projects/{slug}/cover`）的字段与
`POST /api/reports/cover` **完全一致**：`{"title": …, "generate_cover": true}`；
`cover_base64` / `cover_url` / `cover_ratio` / `cover_seed` / `cover_extra` /
`cover_with_title` 也都收。**在 `POST /projects` 的同一个 body 里带 `generate_cover: true`
也可以**，一次调用建好项目并配好封面 —— 适合「建一个叫 X 的项目，封面按 X 生成」这种请求。
⚠️ 封面是**卡片封面**，用 `cover_with_title: false`（默认）：卡片自己会在图下渲染标题，
烧进图里的标题会显示两遍。

**正文插图**：`POST /api/knowledge/assets`（`{"filename":…,"content_base64":…}`）拿到 `url`，写进正文后**必须给尺寸**——
markdown 没有尺寸语法、行内 `<img>` 会被转义，所以尺寸写在图片紧后面：`![图](url){width=560}` / `{width=50%}`

必填/建议字段：`title`（必填）、`summary`（**必填**：一行摘要；**双语页面改用 `summary_en` + `summary_zh`，两个都必填**，缺一即 400）、`body`（必填，markdown）、`tags`（**强烈建议**）、`category`、`document_type`（`note`/`definition`/`method`/`market`/`product`/`channel`/`other`）、`status`、`submitter`。

**正文必须先查重、必须双语**（全文规范：`klado-v2:format-knowledge-page`）：中英写在同一个 `body` 里，用 `<!-- lang:zh -->` 与 `<!-- lang:en -->` 分隔；第一个标记之前的内容两种语言都显示；正文从 `##` 起，不要重复 `title`。

⚠️ 页面里的原始 HTML **会被转义显示，不会执行**。

---

## 日历事件（Calendar）
Base: `/api/calendar`

| 方法 | 路径 | 写 | 说明 |
|------|------|----|------|
| GET | `/events?scope=mine\|public\|shared&from=YYYY-MM-DD&to=YYYY-MM-DD` | — | 按**日期窗口**取（重叠即返回） |
| GET | `/events/{slug}` | — | 单条（含 16:9 页面 `body`） |
| GET | `/events/{slug}/raw` | — | 事件页独立文档（`{base}/e/{slug}` 同一个） |
| POST | `/events` | ✅ | 新建；带 `slug` 时覆盖自己那条。重名 → **409** + `detail.conflicts`，确认后带 `confirm_conflict` 重发 |
| PUT | `/events/{slug}` | ✅ | **合并更新**：请求体里没提到的字段保留库里的值 |
| DELETE | `/events/{slug}` | ✅ | 删除自己的私有事件 |
| GET | `/events/{slug}/cover` | — | 封面图（外部 URL 会 **302** 过去；没有则 404） |
| POST | `/events/{slug}/cover` | ✅ | 生成或存一张封面（只有所有者） |
| DELETE | `/events/{slug}/cover` | ✅ | 移除封面 |
| POST | `/events/{slug}/publish` · DELETE `/events/{slug}/public` · POST `/events/{slug}/pull` | 👤 | ⚠️ **仅浏览器会话**（agent 403） |

必填：`title`、`start_date`（`YYYY-MM-DD`）、`body`（16:9 HTML）。
`end_date` 含当天、不得早于 start；**双语页面必须给 `summary_en` + `summary_zh`**，缺一即 400。
`partners` = **参与人邮箱，同时就是读权限**（邮箱域名由 `AUTH_ALLOWED_EMAIL_DOMAIN` 决定，留空 = 不限）；`attachments` = 引用
`{"kind":"report"|"knowledge","slug":"…"}` 或文件库对象 `{"kind":"file","path":"<bucket>/<key>"}`。
排版规则见 `klado-v2:format-calendar-event`。

---

## 文档里的同事备注（Reader notes）
Base: `/api/annotations`

人在文档里右键留下的备注，**在 agent 改文档之前必须先读它**。

| 方法 | 路径 | 写 | 说明 |
|------|------|----|------|
| GET | `/api/annotations?type=report\|knowledge\|event&slug=<slug>` | — | 该文档上的全部备注。**agent 凭据可以读** |
| POST | `/api/annotations` | 👤 | `{asset_type, slug, body, x, y, page}` —— 仅浏览器会话（agent 403） |
| PATCH/DELETE | `/api/annotations/{id}` | 👤 | 改正文 / 改「已处理」/ 删除 —— 仅浏览器会话 |

- `type` = `report`（静态报告**和文档卡片**）/ `knowledge`（wiki 页面）/ `event`（日历事件）。
- `x` / `y` 是**锚点盒子的百分比**（0–100）；`page` **1 起**（就是读者看到的 "Page 2"，**不要 +1**）。
- 读不到 = **404**，不是 403。
- 免登录访客链接另有一条 `GET /s/{token}/annotations`，只有 GET。

---

## Inbox：同事的互动
Base: `/api/inbox`

| 方法 | 路径 | 写 | 说明 |
|------|------|----|------|
| GET | `/api/inbox?limit=&before_id=` | — | `{items, unread}`，最新在前；agent 凭据可读 |
| POST | `/api/inbox/read` | 👤 | `{ids:[…]}` 或 `{}` 表示全部已读 —— 仅浏览器会话 |

`kind` ∈ `note` / `note_resolved` / `share` / `publish` / `dataset`。`target_url` 是**带挂载点**的深链，agent 不该自己拼链接。

---

## 企业（Organization）与分享闸门
Base: `/api/org`

账号可能属于一家**企业**。企业的两列设置决定内容能走多远：
`share_scope`（定向分享：同事 / 数据集 / 知识页 / 日历搭档）、
`public_scope`（对外公开：免登录链接 / 公开区）。

⚠️ **写这份文档之前先读 `/api/org/me`。** 企业设为 `internal` 时，定向分享只到同企业成员；
`public_scope=approve` 时对外发布会被拒，需要走下面的 `public-requests` 提申请。
闸门在**创建分享时**判定，已发出的分享不会因为后来调严而失效。

| 方法 | 路径 | 写 | 说明 |
|------|------|----|------|
| GET | `/me` | — | 我属于哪家、我能不能管、**当前档位名 + 全部预设的两列**。agent 凭据可读 |
| GET | `/members` | — | 成员名册（含 `domain_ok`：后缀收窄只标记、不删人） |
| POST | `/public-requests` | ✅ | 提交公开申请 `{target_type, target_id, kind}` |
| GET | `/approvals` | 👤 | 待审批队列 |
| POST | `/approvals/{id}/{approve\|deny\|revoke}` | 👤 | 裁决 |
| POST | `/invite` | 👤 | 邀请 |
| PUT | `/members/{id}/role` · `/members/{id}/modules/{key}` · `/scopes` · `/settings` | 👤 | 改角色 / 模块开关 / 档位 / 名称与后缀 |
| DELETE | `/members/{id}` · `/members/{id}/modules` | 👤 | 移出企业 / 整行恢复默认 |

- ⚠️ **只有 `public-requests` 对普通成员开放**，其余 `👤` 项要企业管理员（组织内 `owner`/`admin`）。
  这是唯一一条"被闸门拒绝的人自己有权提问"的出路。
- `target_type` ∈ `report` / `knowledge` / `calendar` / `file`；
  `kind`（通道）∈ `anyone_link` / `public` / `file_token`；`file` 时 `target_id` 是文件 id。
  词表之外的组合是 **400** `未知的公开方式`。**重复提交是幂等的**，不会重复入队。
- 提交申请**不等于发布**——要如实告诉用户还在等审批。
- 三档预设（`org.presets` 由服务端下发，**不要在前端/脚本里自己配**）：
  `internal`=仅内部+需审批 · `external`=任意邮箱+需审批 · `global`=不限+免审批。
- **闸门只在写入端**，读取端一行不改：`share_scope` 调严不会撤销已发出的分享。

---

## 文件存储 / 其他

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/storage/serve?path=<bucket>/<key>` | 对象存储取文件（报告/知识库页面里的图片走这里） |
| GET | `/api/settings/build-version` | 构建版本 |
| GET | `/api/health` | 服务健康检查 |