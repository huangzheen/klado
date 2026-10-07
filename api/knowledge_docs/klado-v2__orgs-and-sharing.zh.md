---
document_id: klado-v2:orgs-and-sharing:zh
title: orgs-and-sharing
document_type: rule
source: klado-workspace
version: 1.1
tags: [org, 企业, 企业管理员, 租户, tenant, 分享, share, 闸门, 审批, approval, 档位, scope, 成员, 邀请, 角色, 模块, module, 后台, console, internal-tools-only]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/orgs-and-sharing.md
---

> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造。

# 企业、分享闸门与两个管理界面

> **给后来读代码的人**：接口部分回答"怎么调"，设计说明（§1 / §2 / §3 / §8）回答
> "为什么是这样"——后者是改这块之前真正要看的部分。

## 0. 一句话

**企业（org）是分享策略和组织管理范围；单机只有一套环境。** 一个人属于**至多一个**企业，个人用户也有一个 `kind='personal'`
的单人企业。企业的两列设置决定**分享能走多远**：谁能收到定向分享、什么东西可以对外公开。
判定只有一个入口 `klado_shared/orgs.py` 的 `may_share_to()` / `may_publish()`，
14 条写入链路全部经过它。

| 关注点 | 代码位置 |
|---|---|
| 词表 + 闸门 + 审批 + DDL（唯一真相源） | `klado_shared/orgs.py` |
| 企业管理员端点（`/api/org/*`） | `api/routers/org_admin.py` |
| 部署管理后台端点（`/api/admin-console/*`） | `api-admin/admin_console/`（独立进程） |
| 账号行模块开关（组织成员开关的实体） | `api/services/account_modules.py` |
| 跨 app 共享层的唯一实现 | `klado_shared/modules.py` |
| 14 条链路清单守卫 | `api/tests/test_share_gate.py::WritePathInventoryTests` |
| 纯逻辑单测 | `api/tests/test_share_gate.py`、`test_org_members.py`、`test_org_admin.py` |
| 真 HTTP + 真 PG | `api/tests/verify_org_settings_http.py` |
| 真浏览器 | `api/tests/verify_account_ui.py`、`shot_settings_admin_ui.py` |

---

## 1. 为什么企业不是 `app_users` 上的一列

Klado 原来只有 `user` / `admin` 两个扁平角色，没有"一群人"的概念。
一旦有人说"我们的文档不出公司"，边界就变成了**一组人的属性**，
而角色列表达不了集合。三条设计约束：

1. **`app_users` 上没有 `org_id`。** 成员关系在自己的表 `org_members` 里。
   账号行上一个可空的 `org_id` 看着更简单，实际后果是产品里每一条查询都要长出
   `OR org_id IS NULL` 分支，而**第一个忘记写的人就是一个数据泄露**。
   成员作为行，还留下了"人还没有账号、邀请已经存在"这个正常顺序的空间。
2. **一个人至多属于一个企业**（唯一索引 `org_members_one_org_per_user`）。
   这是"被邀请的邮箱落进邀请方公司"的推论，不是偏好——能属于两个企业，
   "分享给我的公司"就有两个答案，闸门变成带 `IN` 的查询，而它的 bug 会跨边界发布。
3. **个人用户也有一个单人企业**（`kind='personal'`），不是 NULL。同一套代码路径；
   `kind` 让个人用户在后台里成一个**可分辨的总体**，不需要第二种查询形状。

---

## 2. 两列设置，不是两个枚举

产品的"三档"是**两列**存储，因为中间状态是真实的、单一枚举表达不了：
"我们很乐意分享给客户邮箱，但什么都不对外公开"。

| 列 | 取值 | 管的是 |
|---|---|---|
| `share_scope` | `internal` / `external` / `global` | **定向分享**（同事 / 数据集 / 知识页 / 日历搭档）能走多远 |
| `public_scope` | `forbid` / `approve` / `allow` | **对外公开**（免登录链接 / 公开区）能不能发布 |

⚠️ **这两个词表刻意不同名**（`SHARE_SCOPES` vs `PUBLIC_SCOPES`），
但都落在 `klado_shared/orgs.py` 一个文件里。`SCOPE_PRESETS` 把三档预设映射成两列：

```python
SCOPE_PRESETS = {
    "internal": ("internal", "approve"),   # 默认
    "external": ("external", "approve"),
    "global":   ("global",   "allow"),     # 等于企业管理员预先自批，所以 allow 跳过审批队列
}
```

⚠️ **词表只有服务端一份。** 第一版 Settings 卡在前端自带了一份三档表，
把 `internal` 配成 `(internal, forbid)`，而真实是 `(internal, approve)`：
页面照常渲染，还标着"当前"，点击会写错两列。
**两份都正确的 Python 和正确的 JavaScript 之间没有任何东西会报错——复制词表就是 bug。**
现在 `/api/org/me` 同时下发 `org.preset`（当前档位名）和 `org.presets`（全部预设及其两列），
前端只保留文案。

`PUT /api/org/scopes` 仍然接受两列的任意组合，所以「预设」是 UI 糖，不是数据约束。

---

## 3. 闸门：唯一判定点

```python
def share_scope_of(email) -> tuple[str, int | None]:
    """→ (share_scope, org_id)。个人用户与超管恒为 ('global', None)。"""

def may_share_to(owner_email, recipient_email) -> tuple[bool, str]:
    """定向分享。返回 (允许, 拒绝理由码)。"""

def may_publish(owner_email, channel, target_type, target_id) -> tuple[bool, str]:
    """channel ∈ {'anyone_link', 'public'}。批准过的走 org_public_approvals 缓存判定。"""
```

判定表（`owner` 是内容拥有者）：

| owner 档位 | 定向分享（同事 / 数据集 / 知识 / 搭档） | 免登录链接 / 公开区 |
|---|---|---|
| 超管 / 个人用户（`global`） | 任意合法邮箱 | ✅ 免审批 |
| 企业 `share_scope='internal'`（默认） | **仅同企业成员** | 需审批（`approve`）/ 禁止（`forbid`） |
| 企业 `share_scope='external'` | 任意合法邮箱 | 同上 |
| 企业 `share_scope='global'` | 任意合法邮箱 | ✅ 免审批 |

### 3.1 闸只加在写入端，读取端一行不改

分享本来就是**显式授予**（`dataset_shares` 里一行、报告 share 表一行），
受赠者手里已经有一条既存的 grant。档位后来被调严时**不追溯撤销已发出的分享**——
否则企业管理员改个设置就会静默让别人打不开文档。
UI 上在分享对话框里按当前档位禁用不合规的目标并给出理由。

> 「不追溯」是产品决策，不是省事。如果将来要求改严即断，需要在读取端补一个
> 「授予时档位」快照字段——本次不做。

### 3.2 拒绝理由只有四种，由一张表统一措辞

`orgs.DENIALS` 把四个拒绝码映射到 `(HTTP 状态, 双语文案)`：
`NOT_SAME_ORG` / `NEEDS_APPROVAL` / `PUBLIC_FORBIDDEN` 都是 **403**，`BAD_RECIPIENT` 是 400。

⚠️ 曾经考虑在 11 个写入点各写一份 `raise HTTPException(...)`，那意味着同一条规则有
11 种说法和好几种状态码——而"你们公司的规定不允许"在一个模块返回 400
（用户看来就是"地址打错了"）、在另一个返回 403，是那种没人会发现、
直到有人照着它写客服话术才暴露的不一致。

**未映射的拒绝码抛错而不是兜底**：新加一条 `may_share_to` 的理由却忘了写文案，
应该在测试里炸掉，而不是在用户面前显示成"分享被拒绝"。

#### 实现中发现的一个真 bug：状态码被下游覆盖

闸门给出 403，但仪表盘走 `raise DashboardError(...)` → router 的 `_fail()` 把普通
`DashboardError` 映射成 **400**。于是**策略拒绝被伪装成输入错误**——用户看到 400
会以为自己打错了，于是一直重敲同样的地址。

修法是给 `DashboardError` 家族加具名的 `DashboardPolicyError`（固定 403），
并让 `_fail()` 像处理 `DashboardConflict` / `DashboardTooLarge` 那样**按类型**分支。
这样拒绝**不能**被将来某次编辑降级回 400。

---

## 4. 十四条分享与发布链路

按真正扩大受众的写入端点清点，而不是按数据表数量清点：

| # | 链路 | 端点 |
|---|---|---|
| 1 | 报告 → 同事 | `POST /api/reports/{slug}/shares/colleagues` |
| 2 | 报告 → 免登录链接 | `POST /api/reports/{slug}/shares/everyone` |
| 3 | 报告 → 公开区 | `POST /api/reports/{slug}/publish` |
| 4 | 仪表盘 → 同事 | `POST /api/dashboard/{slug}/shares/colleagues` |
| 5 | 数据集 → 同事 | `POST /api/data-center/datasets/{t}/shares` |
| 6 | 文件 → token（按 id） | `POST /api/data-center/files/{id}/publish` |
| 7 | 文件 → token（按路径） | `POST /api/data-center/files/publish-by-path` |
| 8 | 知识页 → 同事 | `POST /api/knowledge/items/{slug}/shares` |
| 9 | 知识页 → 公开区 | `POST /api/knowledge/items/{slug}/publish` |
| 10 | 日历 → 搭档 | `PUT /api/calendar/events/{slug}`（新增 `partners`） |
| 11 | 日历 → 公开区 | `POST /api/calendar/events/{slug}/publish` |
| 12 | 知识文件夹 → 同事 | `POST /api/knowledge/folders/{id}/shares` |
| 13 | 上传文件 → 同事 | `POST /api/data-center/files/{id}/shares` |
| 14 | 仪表盘 → 公开区 | `PUT /api/dashboard/{slug}`（`visibility=public`） |

`test_share_gate.py::WritePathInventoryTests` 将真正的写路由与 `WRITE_PATHS` 比对；
新增分享链路必须纳入清单并验证企业闸门在写入前执行。管理员不代替个人资源所有者发授权。
仪表盘的闸位于 `dashboard_store.share_dashboard`，不能在测试里替换掉闸本身。
日历合并更新只判断新增搭档，避免旧跨企业搭档阻止普通字段保存。
文件 token 有真实免登录读取路由 `GET /api/data-center/share/{token}`；
所有者可用 `DELETE /api/data-center/files/{id}/publish` 撤销。指定邮箱文件授权与 token 独立，
且文件授权与数据集授权独立；分享 Sheet 不会自动分享原始工作簿。

---

## 5. 审批模型

`public_scope='approve'` 时，对外发布会**被拒并给出一个提交入口**
（`POST /api/org/public-requests`），而不是静默失败。管理员在
`GET /api/org/approvals` 看到队列，用 `POST /api/org/approvals/{id}/{decision}` 裁决。

⚠️ **提交入口必须对普通成员开放，这是唯一一个不用管理员身份解析的端点**
（`org_admin.py::_resolve_member` 而不是 `_resolve`）。`may_publish()` 对**每个成员**生效，
所以普通成员对外发布就会撞上 `NEEDS_APPROVAL`，而那条拒绝文案明确写着
"submit a request and it will be reviewed"。第一版这个端点调的是 `_resolve()`，
于是照着文案操作的成员收到的是
"只有企业管理员可以管理企业成员 / only an enterprise administrator can manage the
members of this organization"——**一句讲成员管理的话，回应一个申请公开的动作**。
产品指了一条出路，又只对它写给的那个人关上。
⚠️ 对应的单测当时**用管理员身份**、却叫 `test_a_member_can_request_publication`，
所以端点坏着的整段时间里它一直是绿的：**fake 走不进被测分支，比没有 fake 更糟**，
它把没覆盖的分支报成已覆盖。成员身份写进测试之后，同一处变异立刻红。

- 批准过的目标缓存在 `org_public_approvals`，所以 `may_publish()` 的第二次判定是 O(1)，
  不需要在每次发布时重扫企业。
- **重复提交是幂等的**：唯一索引 `(org_id, target_type, target_id, kind, lower(requester_email))`
  + `ON CONFLICT … DO UPDATE` 把它重新打开成 `pending`。
  ⚠️ 曾经写成 `ON CONFLICT DO NOTHING` 但**表上根本没有唯一索引**，于是每点一次按钮
  就往管理员队列里多塞一份同一篇文档。
- 审批动词**接受祈使式与过去式两种拼写**（`approve/approved`、`deny/denied`、
  `revoke/revoked`），存过去式。`orgs.DECISIONS` 是归一化词表，
  console 与 Settings 卡都用祈使式拼 URL。
- `public_scope='global'` / `'allow'` 跳过队列：这是企业管理员**预先自批**。

---

## 6. `/api/org/*`：企业管理员端点

`_resolve()` 从**成员行**取权限（不是从账号的 `role`），跨企业一律 **404** 而不是 403。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/me` | 我属于哪家企业、我能不能管、**当前档位名 + 全部预设的两列** |
| GET | `/members` | 成员名册（含 `domain_ok`：后缀收窄不删人，只标记） |
| POST | `/invite` | 邀请（收件人后缀必须 ∈ 本企业 `email_domains`） |
| PUT | `/members/{id}/role` | 改企业内角色 |
| DELETE | `/members/{id}` | 移出企业 |
| PUT | `/members/{id}/modules/{key}` | 成员模块开关（`{"enabled": true/false/null}`） |
| DELETE | `/members/{id}/modules` | 整行恢复默认（一次调用清掉所有收窄） |
| PUT | `/scopes` | 改两列档位 |
| PUT | `/settings` | 改企业名称 + 邮箱后缀（`slug` **不可**通过它设置） |
| GET | `/approvals` | 待审批队列 |
| POST | `/approvals/{id}/{decision}` | 裁决 |
| POST | `/public-requests` | 提交对外公开申请 |

⚠️ **一个邮箱被两个企业同时邀请**：`UNIQUE (org_id, email)` 挡不住（org 不同），
`org_members_one_org_per_user` 也挡不住（此时 `user_id` 都是 NULL，
PostgreSQL 的 NULL 不参与唯一比较）。所以在**接受邀请时**必须用应用层显式查
`conflicting_orgs_for_email()`，有就拒绝。**这是本设计里唯一一处需要应用层判重的约束**，
纯 SQL 约束抓不到，测试必须覆盖。

---

## 7. 成员模块开关 = `account_modules` 行

⚠️ **`org_members` 上没有 `modules` 列。** 成员能开哪些模块，就是这个账号的
`account_modules` 行——`org_members` 视图在读取时拼装。
理由是它本来就存在：部署后台管的是"账号有什么模块"，企业后台管的是
"我的同事有什么模块"，**同一份实体、两种作用域**，不该存两遍。

三态用 `{"enabled": null}` 表达"跟随部署"，不新增"单模块"DELETE 路由：

| 写法 | 存储 | 矩阵里的字形 |
|---|---|---|
| `{"enabled": true}` | 一行 `enabled=true` | `✓` |
| `{"enabled": false}` | 一行 `enabled=false` | `✕` |
| `{"enabled": null}` | **删掉这一行** | `–` |
| `DELETE /members/{id}/modules` | 删掉该成员的全部行 | 全部 `–` |

⚠️ **字形必须是文字（`✓ / ✕ / –`），不是图标 class。** 本项目**没有任何图标字体**，
`<i class="ri-check">` 是个加载失败的空盒子；第一版里 off 与 inherit 因此长得一模一样。
**一个会加载失败的字体比一个朴素字符更糟。** 同理：写死 px 宽度的占位按钮在中文下会错位，
要复制真按钮。

⚠️ 授权矩阵的实数是**部署默认 ∩ 账号行**，再取依赖闭包；账号行**只能收窄**，
不能突破部署白名单。`klado_shared/modules.py` 用三个 hook 让两个 app 各自落地同一份算法：
`set_module(user_id, key, enabled)` / `clear_one(user_id, key)` / `clear_for(user_id)`。
⚠️ **重绑时少传第三个必须把它清掉**，否则上一轮绑的 `resetter` 会一直活着。

⚠️ `/api/org/*` 归属 **required 的 `core` 模块**（`required=True` 所以永不可关，
`in_nav=False` 所以不成导航项）。`core` 的 `api_prefixes` 必须包含 `/api/org`——
漏注册时中间件 **fail-closed 返回 403**，症状是"整个企业管理员 API 全废"。

---

## 8. 两个界面的职责切分

⚠️ **企业管理员管账号，读不了公司的文档**——这是明确的产品决策。
所以 `orgs.py` 里没有一条内容查询被改动，产品里也**刻意没有任何读取作用域的管道**：
那会是在没人需要它之前就把脚手架撒进产品。将来真要做读取作用域，代码属于这里、
一处，而不是分散进那些内容查询。

|  | 主应用 Settings（`:8000`） | 管理后台（`:8787`） |
|---|---|---|
| 前缀 | `/api/org/*` | `/api/admin-console/*` |
| 身份 | **成员行**里的 `org_role ∈ {owner, admin}` + `status=active` + `org_status=active` | `require_operator`（部署级超管） |
| 管什么 | 本公司成员、角色、档位、模块开关、审批 | 所有企业、部署设置、账号生命周期 |
| 前端 | `frontend/out/index.html`（主 SPA 的一部分） | `api-admin/frontend/`（独立 SPA） |
| 进程 | `:8000` | `:8787`（**不 import `api.*`**） |

**准入只有一处判定**：`GET /api/org/me` 的 `is_org_admin`。
`navTo` 的闸门与 `sysSettings.loadAdminPanel` 共用同一个谓词
（`adminPageCanOpenSettings()`）——两处各判一次就会有一处漏。

⚠️ **超管不挂任何 org。** `app_users.role` 是部署级超管标记，
org 级角色一律走 `org_members.org_role`。超管在 `/api/org/me` 拿到
`{"is_operator": true, "org": null}`，这是刻意的：把他塞进某个企业会让
"超管能看所有人的文档"变成"超管恰好是 A 公司的管理员"。

⚠️ **会话 token 带 audience。** 同一个签名机制两套 cookie：
`klado_session`（主应用）与 `klado_admin_session`（后台）。
cookie **不按端口隔离**，所以 audience 是唯一把两份会话分开的东西——
漏掉它就是一个后台 cookie 能开主应用会话。`klado_shared/session.py` 是唯一实现。

## 9. 两个 app 的 i18n 不是同一套实现（改文案前先看这条）

主应用与 console 各有一份 `i18n.js`，**结构刻意不同**，因为踩过的坑不同：

| | 主应用 `frontend/out/i18n.js` | console `api-admin/frontend/i18n.js` |
|---|---|---|
| 文案来源 | `中文 / English` pair **+ `DICT`** | **只有 pair** |
| 字典结构 | `DICT.zh`（英文→中文）与 `DICT.en`（中文→英文）两张表，`mirrorEnglishFirstEntries()` 由前者推导后者 | 无 |
| 为什么 | 有大量单语言字符串（产品名、模块 key、`例如 465`） | 9 个页面全部用 pair 写 |

⚠️ **主应用的两张表方向不能反。** 曾经有 **21 条「中文→英文」的条目被写进 `DICT.zh`**，
而 `mirrorEnglishFirstEntries()` 假设那张表每条都是英文开头，于是反向生成
`DICT.en['Revoke'] = '作废'` ⇒ **英文页面拿到中文**。
判方向要看 **value** 不能看 key（key 里含 `中文` 是合法的：模板字面量）。
`test_i18n.py::DictDirectionTests` 静态守这条，`i18n.js` 载入时也 `console.error` 报一次。

⚠️ **「i18n 只拥有自己写过的值」。** 为了让字典条目能切回原值，`processTextNode` /
`processElement` 会记住原值并在切语言时写回；可是**原值只在 app 没改过它之前才是真的**。
文档查看器在打开卡片时做 `dlBtn.title = 'Download .pptx'`，若无条件写回标记里的
`Download HTML`，**每个文档卡片的下载按钮都会说错格式**，中文下更变成「下载 HTML」——
**格式名在两种语言里都丢了**。所以新增了 `WROTE`（记录 i18n 自己最后写下的值）：
当前值等于 i18n 写的 ⇒ i18n 拥有它，随便翻译/还原；不等于 ⇒ **app 改的**，
把基线挪到当前值上再翻译。

> 两条性质必须**同时**成立，缺一条就是缺陷：
> ① 字典条目能往返（`搜索…` → `Search…` → `搜索…`）；
> ② 运行时设的 `title` / `placeholder` / `aria-label` 能扛过语言切换。
> 只做 ① 会在查看器上产生一个不抛异常的假文案；只做 ② 字典就永远切不回去。
> 两条都有断言：`verify_i18n_ui.py`（浏览器侧，含 ②）与 `DictDirectionTests`（静态侧）。
