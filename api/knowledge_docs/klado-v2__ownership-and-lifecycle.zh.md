---
document_id: klado-v2:ownership-and-lifecycle:zh
title: ownership-and-lifecycle
document_type: format
source: klado-workspace
version: 1.1
tags: [account, 账号, 删除, delete, 恢复, restore, 回收站, recycle-bin, 拉取, pull, 归属, ownership, 分享, share, 授权, grant, 生命周期, lifecycle, 快照, snapshot]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/ownership-and-lifecycle.md
---

> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造。

# 数据的归属与生命周期

> **给后来读代码的人**：本文同时是接口规范和设计说明。接口部分回答"怎么调"，
> 设计说明（§1 / §2 / §6）回答"为什么是这样"——后者是改这块之前真正要看的部分。

## 0. 一句话

**数据归账号所有。** 分享给的是「查询权」，拉取给的是「数据本身」。
账号删除时数据跟着进回收站，30 天内可恢复，到期才真删；
但别人**已经拉取走**的副本是别人的，原账号删除不影响它。

| 关注点 | 代码位置 |
|---|---|
| 三阶段生命周期（软删/恢复/清除）+ 影响面盘点 | `api/services/account_lifecycle.py` |
| 拉取（数据集 / 文件 / 仪表盘） | `api/services/pulls.py` |
| 管理员接口（预览/关闭/恢复/清除/回收站/清扫） | `api/routers/auth.py`（admin 段） |
| 拉取接口 | `api/routers/data_center.py`、`api/routers/dashboard.py` |
| 登录门（`deleted_at` 判定） | `api/services/auth_store.py::authenticate` |
| 启动时清扫到期账号 | `api/main.py`（lifespan） |
| 端到端（真 HTTP + 真 PG） | `api/tests/verify_account_lifecycle_http.py` |
| 纯逻辑单测（无库无服务） | `api/tests/test_account_lifecycle.py`、`test_pulls.py` |
| 真浏览器验证 | `api/tests/verify_account_ui.py` |

---

## 1. 分享和拉取为什么必须是两件事

这是整个模块的地基，一句话就能说清，但很容易在实现时被合并，然后数据归属就错了：

|  | 分享 share | 拉取 pull |
|---|---|---|
| 给的是 | **查询权** | **数据本身** |
| 落在哪 | `dataset_shares` 里一行 | 新表 / 新对象 / 新页面 |
| 对方能改吗 | 不能 | 能（是他自己的） |
| 原主人改数据后 | 对方看到新数据 | 副本不变（快照） |
| 原主人撤销分享 | 立刻失效 | **不影响** |
| 原主人删账号 | **一起没了** | **不受影响** |

所以「拉取」不是更好看的分享，它是所有权转移。
`pulls.py` 里每一条实现约束都是从上面这张表推出来的，不是随手加的。

## 2. 副本必须换名字（最容易做错的一条）

`CREATE TABLE new AS SELECT * FROM old` —— 如果 `new` 就叫 `old`，
那**根本没有副本**，还是同一张表，主人一删副本跟着没。

所以 `_free_name()` 先用 `to_regclass()` 查真实占用情况再决定名字，
后缀是 `_copy` / `_copy2` / …：
- 用 `to_regclass` 而不是读 `_import_registry`：崩在 CREATE 之后、注册之前的导入，
  或绕过 `delete_pg_table` 手工 DROP 的表，注册表里查不到但**名字是真的被占了**。
- 文件同理：副本的对象键必须是 `{新主人}/xxx_copy.csv`。
  ⚠️ 不能是 `{主人}/xxx.csv` —— 主人给自己拉副本时那就是源 key 本身，永远「已占用」。

## 3. 拉仪表盘必须连数据集一起拉

仪表盘是**活查询**，按名字引用数据集。只复制行、名字不改，
产出的东西看着是私有的、其实不是，而且原主人一删就停摆。

`pull_dashboard()` 因此先拉每个数据集，再用 `mapping` 改写声明。
拉不到的（两次读之间被撤销了）**照原名保留**并在响应里报 `missing_datasets`：
页面今天还能用，但必须在响应里讲清楚哪些数字还是借来的，
不能装作副本是完整的。

## 4. 软删不碰任何数据

`soft_delete()` 只做三件事：写 `deleted_at` / `purge_after` / `deleted_by`、
撤销 agent code、消耗未用登录码。**不动任何内容行。**

因此 `restore()` 是精确的：清掉标记即可，行 id 都不变。两个具体的坑：

- ⚠️ **不要在软删里写 `disabled = TRUE`**。`disabled` 是管理员的开关，有自己的语义
  （"这个账号被停了，但它还在"）。折进去之后，恢复会顺手把管理员单独停用的账号重新启用。
  登录门由 `authenticate()` 里的 `deleted_at` 判定，与"这个人"无关的、由生命周期自己管。
- ⚠️ **`restore()` 不删任何行**，包括这个账号**发出去**的分享。
  它的数据集根本没被 DROP，所有链接都还指向活着的表；删掉授权行会打断能用的链接，
  而数据就摆在旁边。
- 不恢复**别人给**它的授权：那些指向别人的数据集，主人可能已经撤销了，
  默默重建等于归还一个可能已被收回的访问权。

## 5. 到期清除：级联与不对称

`purge_account()` 的顺序是**内容先删、账号行最后**。
存储对象删不掉时账号行必须留着等下一次重试，
否则就是"人没了、数据还挂着、没人认领"。

级联清单是**数据驱动**的（`OWNED_CONTENT` / `OWNED_SIDECARS` 两个元组），
不是每个模块手写一条 DELETE —— 手写的清单在新增模块时会悄悄停下，
失败是一行没人看得见的记录。

| 不删 / 不动 | 为什么 |
|---|---|
| `ai_knowledge_documents` | 没有 owner 列：它是 `api/knowledge_docs/*.md` 每次启动按哈希对账出来的**派生物**，删了下次启动原样写回来。这是产品内容，不是用户数据。 |
| `access_log` | `user_email` 是文本字段，审计痕迹要活得比它描述的账号久。把 `user_id` 置空即可。 |
| `mail_settings` | **一张部署级单行**（id=1），只有 `updated_by`，没有 `owner_email`。按 owner 删要么 SQL 报错，要么让一个无关账号注销就带走全站 SMTP 配置。 |
| 别人拥有的内容 | 别人 Inbox 里收到的一条消息（把 `actor_email` 置空，不删别人的行）、别人日历上的一个搭档（移除这个人）。**不对称是故意的**：该删的是这个人，不是别人的内容。 |

## 6. 影响面从「别人那一侧」写

`dependents()` 回答的不是"这个账号有多少行"，而是"**我删了会弄坏谁**"：

- **别人的仪表盘正在查这个账号的数据集**（最要紧：仪表盘是活查询，DELETE 一落它就停）
- 谁持有它数据集的 grant
- 哪些内容当前公开可见

`total` 也有坑：文件是**一行聚合**（count），按 list 长度算会把 200 个文件算成 1；
`public_items` 是 `reports` 的**派生视图**，再算一遍就把每条公开报告数两次。
少算自己拥有的仪表盘是最容易犯的一个——那正是操作者要失去的东西。

## 7. 接口

```
GET    /api/auth/admin/users/{id}/impact          影响面预览（只读，不改任何东西）
DELETE /api/auth/admin/users/{id}?confirm=<邮箱>   关闭 → 进回收站
POST   /api/auth/admin/users/{id}/restore          恢复（仅限 30 天内）
DELETE /api/auth/admin/users/{id}/purge?confirm=<邮箱>  永久清除
GET    /api/auth/admin/recycle-bin?include_expired=
POST   /api/auth/admin/recycle-bin/sweep          立即清理到期账号

POST /api/data-center/datasets/{table}/pull       拉取数据集
POST /api/data-center/files/{id}/pull             拉取文件（仅文件主人）
POST /api/dashboard/{slug}/pull                   拉取仪表盘（连数据集）
```

- **必须邮箱二次确认**：删除/清除是「输入该账号邮箱」。
  这是最便宜且真正拦得住误点的手段，同时兼作"你看到的影响面就是正在执行的那一个"的凭据
  （服务端拿它和账号比对）。
- **仅浏览器可用**：用共享的 `routers/reports.py::_require_browser`。
  ⚠️ 不能只靠 `main.py::_AGENT_WRITE_ALLOWED_PREFIXES` —— `/api/dashboard` 在白名单里，
  前缀判断会让 agent 复制同事的页面。
- `days_left` **向上取整**：剩 29 小时要说「2 天」。
  这是给人看的承诺，少报一天比多报一天糟糕得多。

## 8. 错误码

| 情况 | 结果 |
|---|---|
| 软删后拿旧 cookie | 401（`main.py::_local_identity` 查 `deleted_at`，不只是 `disabled`） |
| 软删后用未过期的 agent code | 401（`resolve_agent_token` 额外校验属主状态） |
| 对不存在的 / 看不见的账号做任何操作 | 404，不是 403 |
| 清空 prompt 里邮箱打错 | 400，不删任何东西 |
| 拉取无权的数据集 | 404（不确认它是否存在） |
| 拉取别人的文件 | 404（分享从不暴露源文件，拉取不能成为绕过它的路） |

## Data Center 归属与视图

- 文件与数据集是独立资源：文件是上传的原件，数据集是可查询的行。界面保留独立的“文件”和“数据集”列表，分别提供“我的”和“同事分享”范围。
- 新内容默认归当前用户私有。列表、下载、预览、SQL、导入记忆和后台任务都按账号过滤；管理员也使用这条内容边界。
- 所有者可更新、重命名、删除、分享和撤销；接收者只能读取或拉取自己的独立副本，不能转授、修改、删除、生成公开链接。
- 分享数据集只给行查询权，不给上传源文件、对象路径、同文件的其他 Sheet 或所有者其他数据集。文件需要单独分享。
- 指定同事分享遵循企业的定向分享策略；文件免登录链接遵循对外发布策略。撤销链接后旧 token 失效，已经下载或拉取的独立副本不被追溯删除。
- 旧无主记录仅迁给配置的历史所有者（`AUTH_ADMIN_EMAILS` 第一项）；没有配置时隐藏，不能视为全员共有。
- 文件夹是每个账号文件的路径分组。相同路径可属于不同用户，删除自己的文件夹只删除自己的对象。未登记的历史对象不向普通账号开放。

### 文件授权 API

`GET /api/data-center/files/shared-with-me` 列出收到的文件授权。
所有者使用 `GET/POST /api/data-center/files/{id}/shares` 查看/添加指定邮箱授权，
`DELETE /api/data-center/files/{id}/shares/{email}` 撤销；接收者使用既有下载/预览接口读取。
`DELETE /api/data-center/files/{id}/publish` 撤销免登录链接。

### 类型识别与更新

建数据集默认使用自动类型识别，检查整列而非少量样本：编号和前导零保留文本，
明确年月日识别为时间，统一金额或百分比转换为数值，明确真假标签识别为布尔。
混合币种、混合百分比和数字、模糊日期保持文本。百分比 `20%` 存为 `0.2`。
清洗界面可改选文本、数值、日期或布尔；显式转换遇到坏值报错，不静默变成空值。
更新会检查现有列类型并拒绝会丢数据的收窄；不会因为刷新视图移除数据集列表。
