---
document_id: klado-v2:format-dashboard:zh
title: format-dashboard
document_type: format
source: klado-workspace
version: 1.2
tags: [dashboard, 仪表盘, dataset, 数据集, query, 查询, sql, 分享, share, 可见性, visibility, nav, 导航, 首页, 页面, page, 只读, read-only]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/format-dashboard.md
---

> ⚠️ **2026-10-05 起，仪表盘是所有 HTML 的落点。** 以前 HTML 交付到 Workspace
> （`POST /api/reports`），仪表盘只放"会自己查数据的活页面"。现在反过来了：
> **写 HTML → `PUT /api/dashboard/{slug}`**，Workspace 只剩文件卡片。
> 本文因此同时是**交付入口规范**和模块设计说明。
>
> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造；业务数字必须走批准的只读数据源，不得凭印象编造。
> 16:9 deck、双语、`submitter`、封面**以 `klado-v2:format-report-html:zh` 为准**，
> 本文讲交付位置、筛选器、注解与模块本身。

# 仪表盘：所有 HTML 的落点

> **给后来读代码的人**：本文同时是接口规范和设计说明。接口部分回答"怎么调"，
> 设计说明（§1 / §3 / §4）回答"为什么长成这样"——后者是改这个模块前真正要看的部分。

## 0. 一句话

**HTML 交付到仪表盘。** agent 写 HTML → `PUT /api/dashboard/{slug}` →
读者打开 `/d/{slug}` 看到的就是这份文档本身。

仪表盘有两种页面，**同表、同入口、同一套接口**：

| | 快照页 | 活页面 |
|---|---|---|
| 数字什么时候定 | 发布那一刻 | 读者打开时页面自己查 |
| 怎么做 | 直接写数字 | 页面调 `POST /api/dashboard/query` |
| 要声明 `datasets` | 不要 | **要**，声明它允许查哪些数据集 |

两种都能带**筛选器**（读者切时间段/指标）和**注解**（读者在正文上留 note）——
这两套能力 2026-10-05 从报告模块搬过来，因为页面搬过来了。

| 关注点 | 代码位置 |
|---|---|
| 路由 + 独立页面渲染 + 注入的运行时 | `api/routers/dashboard.py`（390 行） |
| 两张表、全部语句、纯校验规则 | `api/services/dashboard_store.py`（724 行） |
| **唯一**的 SQL 网关（Data Center 与仪表盘共用） | `api/services/dataset_query.py`（196 行） |
| 数据集分享 | `api/services/dataset_shares.py` + `api/routers/data_center.py` |
| 模块注册 / 开关 | `api/core/modules.py::DASHBOARD` |
| 端到端身份验证 | `api/tests/verify_dashboard_e2e.py` |
| 纯逻辑单测（无库无服务） | `api/tests/test_dashboard.py` |

---

## 1. 为什么是独立的表，而不是复用 `ai_reports`

**因为仪表盘必须能被单独卖。** 它是 `core/modules.py` 里一个独立模块（`requires=(DATACENTER,)`），
有独立的墙、独立的 `/api/dashboard` 前缀、独立开关。共享 `ai_reports` 会让两者在数据库层面
不可分割——导航栏里看着是两个东西，数据模型里是一个东西，删一个模块就删一半。

`ai_dashboards` 与 `ai_dashboard_colleague_shares` 是两张自己的表（`ensure_schema()`
按需幂等创建，`main.py` 启动时预热一次）。

### 1.1 2026-10-05：HTML 从 Workspace 搬到这里

**搬的理由是产品决定，不是技术限制。** 用户要"HTML 都在仪表盘，agent 也往仪表盘交"。

搬过来之后 `ai_dashboards` 多了六列，全是 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
（老库必须能原地升级，`CREATE TABLE IF NOT EXISTS` 对已存在的表什么也不做）：

| 列 | 装什么 | 从报告那边搬过来的原因 |
|---|---|---|
| `summary_en` | 卡片一行摘要的英文侧 | 原来只有 `summary_zh`，英文读者看到空行 |
| `langs` | `en,zh` | 卡片与阅读器按它选语言 |
| `kind` | `static`/`interactive`/`dynamic` | 决定有没有注解层、筛选侧栏 |
| `filters_json` | 作者定义的筛选器 | 动态页要有筛选器 |
| `state_json` / `state_updated_at` | 读者的注解文档 | 互动页要有状态 |
| `submitter` | 谁要的这份页面 | 发布契约要求；不建列就会被**静默丢弃** |

**没有搬的**：项目 / 文件夹归置（`project_slug`、`folder_id`）、`pinned`、公开快照、
`/r/` 阅读器、pptx/pdf 导出。这些仍属 Workspace 或根本没做。

**代码是复用不是复制**：筛选器校验、取值折叠、宽容读取、注解文档这四块逻辑
原本在 `routers/reports.py` 里，现在整体搬到 **`api/services/doc_state.py`**，
两个 router 都从那里调。复制一份的后果是修 bug 只修一边——
`api/tests/test_dashboard_reader_state.py::SharedOwnershipTests` 就是钉这件事的：
它断言"定义只在一处"，因为行为测试看不出两份拷贝已经分叉。

⚠️ 两张**筛选选择表是分开的**（`ai_report_filter_selections` /
`ai_dashboard_filter_selections`）：读者在报告上调好的视图，不该自动套到一个仪表盘上。
两张表的**主键列名也不同**（`report_id` / `doc_id`），因为共享的读取函数按 `doc_id` 寻址。

`api/routers/dashboard.py` 里刻意**没有**第二个 SQL 白名单：`services/dashboard_store.py`
只负责"slug 形状 / accent / datasets 形状 / 三个 scope / 卡片字段"，"这个人能不能读这张表"
只由 `dataset_query.assert_query_allowed` 一处回答。

## 2. 表长什么样（两张）

`public.ai_dashboards`：`slug`(unique, `^[a-z0-9][a-z0-9-]{2,63}$`)、`title`、`summary` /
`summary_zh` / `summary_en`（一行一个语言，墙上按读者语言取）、`langs`、`submitter`、
`description`、`tags`、`accent`、`datasets`（JSON 数组文本）、`html`、`kind`、
`filters_json`、`state_json` / `state_updated_at`、`owner_email`、`visibility`(`private`默认/
`public`)、`status`(`published`默认/`draft`/`archived`)、`pinned`，放置标记 `in_nav` /
`nav_order` / `in_home` / `home_order` / `home_collapsed`，以及 `size_bytes` / `views` /
`created_at` / `updated_at`。

`public.ai_dashboard_filter_selections`（2026-05 新增，见 §1.1）：`doc_id`
（`on delete cascade`）、`user_email`、`selection_json`、`updated_at`，
`primary key (doc_id, user_email)`。⚠️ 主键列叫 **`doc_id`** 而不是 `dashboard_id`——
共享的读取函数 `doc_state.saved_selection` 按 `doc_id` 寻址，两张选择表统一用它；
起别的名字会让仪表盘的每一次读取都返回"没有保存的视图"，而侧栏不报错，只是默默回到默认值。

`public.ai_dashboard_colleague_shares`：`dashboard_id`（`on delete cascade`）、
`recipient_email`、`shared_by`、`created_at`，`unique (dashboard_id, recipient_email)`。
收件人索引建在 `recipient_email` 上——`shared` scope 正是按它查的。

删除仪表盘时**分享记录一起删**，而且是**两套机制**：`ON DELETE CASCADE` 是数据库的承诺，
`delete_dashboard()` 里那条显式 `DELETE` 是本应用的。只靠约束的话，
"从别的路径删掉的仪表盘"（维护脚本、未来的批量接口）行为会不一样，
留下一条指向无人拥有的 id 的分享记录。

## 3. 为什么只有一套 SQL 守卫

Data Center 的隔离是**表级**的：每个数据集是自己的表，盖着一个 owner。所以一条裸
`SELECT` 必须先对着"调用者能看见什么"检查一遍，否则它就是一条"按名字读别人表"的通道。
这个检查原本长在 `api/routers/data_center.py` 里。

于是第二个"想让页面自己查数据"的模块只有三条路：import 一个 router（层次错了）、
复制一份守卫（第二份会漂移的白名单）、或者**把守卫抽出来**。选第三条：
`services/dataset_query.py`，Data Center 与仪表盘都调它。

- `leading_keyword()` 跳过前导注释（`-- 口径说明\nSELECT …` 是合法 SQL，朴素的
  `startswith("SELECT")` 会拒掉一条根本没读过的语句）；
- `normalise_statement()` 去掉**一个**结尾分号；调用方自己查内部 `;`，好让报错说清原因；
- `referenced_relations()` 扫 token 流而不是正则 FROM/JOIN 子句——正则要么提前停（漏掉
  JOIN 后面那张表，一个静默的洞），要么冲进下一段。先剥掉字符串字面量（`'from secret'`
  不能凭空造出一个表），跳过 CTE 名（`WITH x AS (…) … FROM x` 读的是 CTE，不是表 `x`），
  但 CTE 体本身照样扫；
- `assert_query_allowed()` 拿 `db.list_datasets(viewer_email)` 的结果当白名单；管理员遵循相同边界；
  系统目录和非 public schema 不是可授权数据集，任意服务器函数也不放行。注释不能隐藏逗号连接或 TABLE 子查询。

**写屏障不在这些文本检查里。** 它是 `data_center_db.query_pg()` 里的
`SET TRANSACTION READ ONLY`。文本检查只负责"只允许一条语句""只允许 SELECT / WITH…SELECT"
和一条可读的报错。所以 `WITH x AS (DELETE …) SELECT * FROM x` 这种改数据的 CTE **看起来
像只读**、由数据库以 `ReadOnlySqlTransaction` 挡下，再被翻译成 400（客户端写错了，不是服务端故障）。
`WITH … SELECT` 本身是允许的（2026-09-27 放开：CTE 就是普通只读 SQL，老的 `startswith("SELECT")`
逼着 agent 把每个 CTE 改写成嵌套子查询）。

`routers/data_center.py` 里保留了 `_leading_keyword` / `_referenced_relations` /
`_assert_query_allowed` 三个薄别名——已有单测和读代码的人都从那里认得它们。

## 4. 放置标记为什么长在内容上

`in_nav`（自己的顶栏标签，`nav_order` 排序）、`in_home` / `home_order` /
`home_collapsed`（落地页上的容器）是**内容级**标记，在发布时由 agent 写死——因为一个仪表盘
是**带着它该怎么被呈现的意图**被写出来的。一个"只读监控页"和"落地页第一屏"是两种不同的
页面，agent 知道是哪一种。

**每个读者各自重排**是另一件事，不在这一版。把它做成内容字段，以后要改也不会破坏已有数据；
反过来先做成用户偏好，就得给每一页再存一份。⚠️ 代价是**只有一个全局顺序**：所有读者共享
同一套 `nav_order` / `home_order`。

## 5. 权限模型

- **404，不是 403。** "不是你的"和"不存在"给同一个答案——状态码不能替一个读不到的 slug
  确认它存在。分享管理接口同理（`require_owner` / `_own_dashboard`）。
- **409 只对本来就读得到的人点名。** slug 被别人占用时，如果调用方读得到那一行，就 409
  说清"换一个"；读不到就 404——否则 409 就是"这个 slug 存在吗"的探针（见
  `save_dashboard` 里的 `may_read(existing, owner)`）。
- **三个 scope 互不相交**（`SCOPE_SQL`）：`mine` = 我的**私人**页；`public` = 公共墙；
  `shared` = 别人**私人**且已发布、明确分享给我这个地址的页（`owner_email <> %s` 把我自己
  的行挡在外面）。混在一起会出现"墙上宣传了观众其实没有的操作"。
- **`can_manage` 按归属算，不按角色。** 管理员对别人的行也是 `false`——和 Workspace 的
  "墙上不许出现观众没有的按钮"是同一条规则。
- 卡片里的 `shared_with_me` 是**布尔**而不是收件人列表：200 行卡片附上所有收件人，
  等于把一堆邮箱地址漏进列表视图。
- `bump_views` 只在公共页和所有者自己看时 +1；**被分享的不计**——那是同事替所有者看，
  算进所有者的阅读量等于让这个数字说假话。

## 6. 发布接口

`PUT /api/dashboard/{slug}`：**slug 是 URL，在 body 里没有这个字段、也不可改**。不存在就建，
存在就改。建的时候 `title` 与非空 `html` 必填。

上限（`dashboard_store` 常量）：`title` 240、`summary` / `summary_zh` 600、`description` 2000、
`tags` 300、`html` 8 MB、`datasets` 50 条、`limit` 500、`max_rows` 默认 500 上限 2000。

三条校验（**都是 400，不是静默丢**）：

- `validate_accent()`：必须是 `theme.css` 的 token 名（`^[a-z][a-z0-9-]{0,31}$`，不带前导
  `--`）。存一个 `#ff8800` 的卡片在浅色主题里是对的、在深色主题里读不了，而这件事要等到
  有人切主题才会被发现。`theme.css` 拥有全部颜色，仪表盘只能**指**一个。**这是回归防线，
  不是形式。**
- `parse_datasets()`：必须是标识符形状的字符串数组（`^[A-Za-z_][A-Za-z0-9_]{0,62}$`），
  按原序去重。非法的**拒绝**而不是丢弃——被悄悄忽略的名字会让页面去问一张"它被告知可以问"
  的表，然后被那道本该保护它的守卫 403。
- `normalise_slug()`：小写化；空则回落到标题；标题里没有 ASCII 再回落到标题哈希
  （一个固定前缀会让每个中文标题的仪表盘挤到同一个地址上，静默覆盖前一个）。
  校验用的是**小写之后**的值：发 `"Q3-Review"` 存成 `q3-review`，发 `"Q3 review"` 才是 400。

**`PUT` 是合并语义**（`_merged`）：没传的字段保留库里的值。替代方案（整体覆盖）会静默清掉
调用方没提到的字段，而它咬人的方式恰恰是"修个 title 错字把整页 wipe 了"。要清空就传空值。

### 6.1 哪些字段是"接了但丢掉"的

⚠️ **多余的字段被静默丢弃**（pydantic 默认 `extra="ignore"`，不是报错）：往仪表盘体上发一个
模型里没有的字段，会**返回 200 且什么都没发生**。这是本仓库明令避免的那一类失败——
**比 400 坏得多，因为发的人以为它存下了。**

**2026-10-05 之前**这里有四个真实的坑：`summary_en` / `submitter` / `kind` / `cover_base64`
全部属于"看着像对的字段"，发过去静默消失。`cover_base64` 早就修过；前三个是搬 HTML 的时候
才暴露的（发布契约要求 `submitter`，而表里没有这一列）。现在三个都是**真字段**。

**仍然被丢的**（发过去等于没发）：`category`、`author`、`author_email`、`project_slug`、
`folder_id`、`source_report_id`、`public_link_token` —— 这些属于 Workspace 报告或知识库页面。
想起这些字段就说明在写另一种东西。

`api/tests/test_dashboard_reader_state.py::SilentlyDroppedFieldTests` 把这份清单**钉住**：
清单外的字段一旦变成真字段，测试会失败并报出是哪一个。这样"哪些是故意的"就不再是
一段会腐烂的散文。

## 6.5 筛选器：页面自己声明，读者选值

一个页面想有"按时间段/指标切换"，就把筛选器定义发上来，读者在左侧栏选值。
完整规范（`data-filter-when` 切片语义、四种筛选器形状）见
`klado-v2:format-dynamic-report:zh`，这里只讲仪表盘这一侧的接口。

| 方法 | 路径 | 谁在用 |
|---|---|---|
| `GET` | `/api/dashboard/{slug}/filters` | 读者打开页面时取**定义 + 自己的取值**（一次性） |
| `PUT` | `/api/dashboard/{slug}/filters` | **agent 声明**这个页有哪些筛选器 |
| `DELETE` | `/api/dashboard/{slug}/filters` | 全部删掉，页面回到 `static` |
| `PUT` | `/api/dashboard/{slug}/filters/selection` | **读者**存自己的取值，必须浏览器会话 |

⚠️ **定义是机器凭据的事，取值是人的事。** `PUT .../filters` 放开给 agent（它是对文档的
陈述："这个页面**有**这些筛选器"）；`PUT .../filters/selection` 必须浏览器会话——
一个人的视图，不该被一个机器凭据悄悄搬走。

PUT 一个非空定义会把页面提升为 `dynamic`（"你写了定义 = 你要这个"）；PUT 空列表则删掉
全部筛选器并按正文重新推断 `kind`。⚠️ 推断看的是正文里有没有 `data-filter-when`：
**定义清空了但切片还在的页面仍然是 `dynamic`**，只是所有切片同时显示——
这也是为什么 `kind` 要按正文重新推断而不是直接置 `static`。

请求体：

```jsonc
PUT /api/dashboard/q3-page/filters
{ "version": 1,
  "filters": [
    {"key": "period", "type": "select",
     "label": {"en": "Period", "zh": "时间段"},
     "options": [{"value": "2026-06", "label": {"en": "Jun", "zh": "六月"}}],
     "default": "2026-06"}
  ]}
```

校验在 `api/services/doc_state.py`，**拒绝而不是修正**——一个被悄悄改过的筛选器会和作者
的意图不一致，而读者没有办法发现。`GET` 侧则相反，读取是**宽容**的：库里一行格式旧的
schema 不该让所有读者的侧栏挂掉（`tolerant_schema`）。

## 6.6 注解：读者在正文上留 note

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/dashboard/{slug}/state` | 这个读者的标注文档；没存过时是 `{}`（**不是 404**） |
| `POST` | `/api/dashboard/{slug}/state` | 覆盖写入，≤64KB，**必须浏览器会话** |

⚠️ **保存标注不动 `updated_at`**：卡片墙按 `updated_at DESC` 排序，
一次标注就把这个页面顶到所有人的墙最前面。

⚠️ `kind` 是 `interactive` 或 `dynamic` 的页面**不给**同事备注（`/api/annotations?type=dashboard`），
因为它自己的右键已经有含义了，一次点击弹出两个菜单比只有一个更糟。`static` 页面才有。

`GET` 永远返回 `{}` 而不是 404，是为了让文档里不用写"是 404 还是空状态"这个分支；
`Cache-Control: no-store`，因为一份旧的副本会让刚加的标注看起来像丢了。

## 6.7 迁移：Workspace 里已有的 HTML 怎么过来

`scripts/migrate_html_to_dashboard.py`，**默认 dry-run，加 `--apply` 才写**：

```bash
.venv312/bin/python scripts/migrate_html_to_dashboard.py           # 只打印计划
.venv312/bin/python scripts/migrate_html_to_dashboard.py --apply   # 真搬
```

搬的是**有 HTML 的行**（`html` 非空且没有 `doc_type`）。`doc_type` 有值的那些是
Office 文件，**留在 Workspace** —— `ai_dashboards` 没有能存二进制的列。

⚠️ **slug 保留，但地址变了**：读者收藏的 `/r/{slug}` 会 404，新地址是 `/d/{slug}`。
脚本会把要搬的清单先打出来就是为了这件事。⚠️ 读者的筛选取值会跟着搬
（读 `ai_report_filter_selections`，写 `ai_dashboard_filter_selections`）——
忘了这一步，每个读者的视图都会无声地回到默认值，而侧栏不报错。

## 7. 页面运行时（`GET /d/{slug}`）

`/d/` 在 `main.py::_SESSION_DOCUMENT_PREFIXES`（`("/r/", "/e/", "/d/",)`）里，所以处理器里的
`current_identity()` 拿得到人。**漏加的后果**：已登录读者在处理器内部拿到 401，
读起来像"应用坏了"。

注入两样东西，然后**原封不动**地上存储的 HTML：

1. `<base href="{app_base_href}">`（文档自己已经声明 `<base>` 就不插）——所以页面里写
   `api/dashboard/query` 在任何部署下都落在这个应用里；
2. `<script data-dashboard-runtime>` 到 `<head>`，在你自己的脚本**之前**跑，内容是
   `window.kladoDashboard = {slug, title, datasets, lang, maxRows}` 和
   `window.kldQuery = async (sql, opts) => …`。

**序列化陷阱**（和 `vendor/report-filters.js` 同一个，`tests/test_dashboard.py` 有守卫断言）：
每次渲染都重新注入，旧副本先用正则剥掉。那个正则在找标签本身，**运行时自己的正文里出现一次
字面量 `<script`**，正则就会从文件中间开始匹配，于是重新发布过的文档会叠出**第二份**运行时——
旧的那份先执行，绑在什么都不是的地方。所以开头标签是拼出来的
（`_RUNTIME_TAG = "<" + "script data-dashboard-runtime>"`），JSON 载荷里的 `</` 也转义成 `<\/`。

> 这个模块**没有** import `routers/reports.py`——`/r/{slug}` 那个文档外壳的形状是在
> dashboard.py 里照着重写的。原因就是 §1：一个要能单独立项的模块不能依赖隔壁那 3400 行。

## 8. HTML 规则：和 Workspace 报告的差异

**沿用 `klado-v2:format-report-html:zh` 的**：相对路径资源（服务端注入 `<base>`）、≤8 MB、
系统字体、业务数字必须来自可核实的来源、不写死日期。三点**不同**：

1. **报告运行时不在这里跑。** 没有 `.deck` 缩放、没有 `.slide` 分页、不注入页码，
   `/d/` 上根本没有那套 CSS。仪表盘**不是 16:9 deck**，是一页自带的 HTML。
2. **双语没人替你做。** 没有 `data-lang` 过滤、没有 `?lang=`；页面自己读
   `kladoDashboard.lang` 决定显示哪一侧。摘要是 `summary` + `summary_zh`（**不是**
   `summary_en` / `summary_zh`）。
3. **没有封面、没有 submitter、没有备注层、没有 `/state`。** 交互全部由页面自己负责。

## 9. 数据集分享：授权的是"行"，不是文件

`POST /api/data-center/datasets/{table_name}/shares` `{"email": "…"}` 幂等；
`GET …/shares` 谁被分享了；`DELETE …/shares/{grantee}` 取消（没这条记录 → 404）；
`GET /api/data-center/datasets/shared-with-me` 别人分享给我的（带 `shared_by`）。
**仅所有者**，非所有者——**包括管理员**——一律 404：授权永远不代替别人做。

被分享人得到的是**这张表和它的行**（`POST /api/data-center/query` 与
`POST /api/dashboard/query` 都能读）；**得不到**上传的源文件、它的对象存储 key、所有者文件库
里的任何东西、所有者的其他数据集。`may_write_dataset` 在这个服务里**不是参数**，也没有任何
写路径查这张表——被分享人的访问权限字面上就是"这张表出现在我的列表里且 SELECT 它有答案"。
这也是"没有授权竞态要推理"的原因：删掉那一行，访问就没了。

可见性因此有**两个来源**，`data_center_db._ROW_VISIBLE` 把这句话写一次，
于是每个列表和每个按名查找的答案都一致。

授权是一行记录，所以它**活不过数据集**：表被删，授权一起没；表名日后被别人重新建出来，
旧被分享人**不会**恢复访问（他甚至不能通过 `…/shares` 确认它存在——404）。
这两条都由 `tests/test_dataset_shares.py::CascadeTests` 与 `verify_dashboard_e2e.py` 钉住。

## 10. 仪表盘与数据集分享的关系（最容易搞错的一处）

`datasets` 字段是页面**声明**自己会问哪些表，是给读者看的说明，**不是权限**。
真正的隔离在查询时，由**读者**自己的授权决定：

> 一个作者看得见十二张表的公共仪表盘，被一个一张表都看不见的同事打开时，
> 每个查询都 403，画出来是一块白板。

所以要让人看到数字，**先分享数据集**，只分享仪表盘不够。反过来，作者没在 `datasets` 里
声明但读者确实有权的那张表，查询照样通——声明是给读者和给自己看的账。

## 11. 当前凭据边界

账号绑定的 agent 授权码可读取该账号有权的仪表盘，创建、更新、删除自己的仪表盘，
管理自己页面的指定同事授权，并通过 `POST /api/dashboard/query` 查询有权的数据集。
`/api/dashboard` 已列入机器写入范围，共用 SQL 网关的只读 POST 也有明确放行。
权限仍按所有者和企业分享策略判断；公开发布与拉取独立副本需浏览器会话。
Data Center 的建表、上传、修改和授权写入不在 agent 写入范围内。

模块开关（`core/modules.py::DASHBOARD`）对 API 和 `/d/` 页面**都**生效：
关掉时 `GET /api/dashboard` 和 `GET /d/{slug}` 都是 403（正文带 `module: "dashboard"`），
`/api/health` 照常 200 且不再列出它。`/e/` 当年漏了这个，是整轮返工。

## 12. 验证

- `cd api && ../.venv312/bin/python -m unittest tests.test_dashboard tests.test_dataset_shares tests.test_data_center_sql_guard`
  —— 纯逻辑，不起服务、不连库。
- `cd api && ../.venv312/bin/python -m unittest tests.test_dashboard_reader_state`
  —— 搬过来之后新增的部分：共享逻辑的**归属**、两张选择表分开、`kind` 被查询带出来、
  卡片带 `kind`/`has_filters`、注解家族、以及页面上**只有一个**筛选侧栏。
  里面有一条断言的是"定义只在一处"——行为测试看不出两份拷贝已经分叉。
- `../.venv312/bin/python api/tests/verify_dashboard_filters_ui.py` —— 真浏览器：
  在仪表盘上打开一个动态页，量侧栏的**真实面积**、核对它打的是 `/api/dashboard` 而不是
  `/api/reports`、选中值存到哪个模块、关掉后侧栏会不会留在关掉的视图器上。
  （这个测试抓到过两个真 bug：`openFilters` 没导出，以及它拿报告模块的 `_viewerSlug`
  做竞态守卫，导致仪表盘的侧栏永远停在 "Loading"。）
- `cd api && ../.venv312/bin/python tests/verify_dashboard_e2e.py` —— 真服务器 + 真库 +
  三个真实账号，因为这里值得验的全都是**身份**断言：模块关掉时页面也 403、分享只给行不给文件、
  查询网关共用同一道守卫、被分享的仪表盘可读不可改、私有页是 404 而不是 403、
  删除会带走分享。需要 PostgreSQL 在跑。
