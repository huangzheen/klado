---
document_id: klado-v2:format-dynamic-report:zh
title: format-dynamic-report
document_type: format
source: klado-workspace
version: 1.0
tags: [dynamic, 动态报告, report, 报告, filter, 筛选器, 边栏, sidebar, slice, 切片, data-filter-when, workspace, 工作区, 时间段, 销售值, gto, spto, quantity, schema, 声明式]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/format-dynamic-report.md
---

> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造；业务数字必须走批准的只读数据源，不得凭印象编造。
> 版面（16:9 deck 硬约定、双语、密度与字号、封面、`submitter`、`/r/{slug}`）、发布字段与
> 错误码**一律以 `klado-v2:format-report-html:zh` 为准**，本文只讲它没覆盖的部分。
> 形态选型见 `klado-v2:format-interactive-report:zh` §0（本文补上第三个形态）。

# 动态报告：agent 定义筛选器，读者切换切片

一句话：**你把各筛选组合的内容都写进同一份 HTML，每一块用 `data-filter-when="period=2026-06"` 声明
自己属于哪个切片，再把这些筛选项用一次 `PUT /api/reports/{slug}/filters` 注册给报告**。之后读者在
Workspace 查看器的**左侧边栏**里选值，你写的那份 HTML 就只显示命中的切片 —— 报告里没有、也不许有
任何筛选器控件，读者只能选值，不能增删改筛选器。

## 0. 三个形态，先选对

| | **静态报告**（默认） | **互动报告** | **动态报告** |
| --- | --- | --- | --- |
| HTML 里有 `<script>` 吗 | 没有（纯 HTML/CSS） | 有（自己的渲染/编辑脚本） | **不需要**（运行时会注入，你只写切片） |
| 读者能改内容吗 | 不能 | 能，改动存服务端 `/state` | **不能**（页面只读） |
| 读者能做什么 | 看、右键写备注 | 右键标注、改字、换色 | **在边栏里选筛选值** |
| 筛选器写在哪 | —— | —— | 报告里**不写**，由 agent 注册（§2）+ 边栏渲染 |
| 什么时候用 | 分析结论、复盘、月报 | "能点、能标、要留痕"的交互页 | 同一份分析要按**时间段 / 销售值口径 / 区间**切换看 |

**默认静态。** 只有用户明确要「能按时间/口径切换看」时才做动态报告；只要看就用静态。
⚠️ 动态报告**不会**得到备注层：右键不会有标注菜单（那是静态报告的），想留痕就用互动报告或静态报告。

## 1. 交付三步

1. **写 HTML**（照 `klado-v2:format-report-html:zh` 的 deck 约定 + 双语法）：把每个切片的内容
   写成一个块，块上标 `data-filter-when`。没标 `data-filter-when` 的块**两种选择下都显示**（共享内容）。
2. **发布**：`POST /api/reports`，`kind` 可以省略 —— 只要 HTML 里有 `data-filter-when`，服务端就把它
   识别成 `dynamic`；也可以显式传 `"kind": "dynamic"`（更稳，强烈建议）。
3. **注册筛选器**：`PUT /api/reports/{slug}/filters`，body 就是 §2 的 schema。这一步之前这个报告已经能
   打开，但边栏是空的（schema 为空 ⇒ 报告按"所有切片都显示"渲染，你会看到内容叠在一起）。

⚠️ **第 3 步不是可选的。** 只发 HTML 不注册 schema = 一份所有切片同时显示的文档，看起来像排版坏了。
⚠️ 第 3 步只能改**你自己私人工作区里的原稿**；公共区快照和别人的副本不能被改（先在原稿上改，再重新发布）。

## 2. 筛选器 schema（这是唯一新增的接口）

`PUT api/reports/{slug}/filters`，body：

```json
{
  "version": 1,
  "filters": [
    { "key": "period", "type": "select", "width": "half",
      "label": { "en": "Period", "zh": "时间段" },
      "options": [
        { "value": "2026-06", "label": { "en": "Jun 2026", "zh": "2026年6月" } },
        { "value": "2026-07", "label": { "en": "Jul 2026", "zh": "2026年7月" } }
      ],
      "default": "2026-06" },

    { "key": "sales_metric", "type": "multi",
      "label": { "en": "Sales value", "zh": "销售值" },
      "options": ["gto", "spto", "quantity"],
      "default": ["gto"] },

    { "key": "price", "type": "range", "width": "half",
      "label": { "en": "Price band", "zh": "价格段" },
      "min": 0, "max": 20000, "step": 500, "unit": "RMB",
      "default": { "min": 3000, "max": 9000 } },

    { "key": "window", "type": "date_range",
      "label": { "en": "Date range", "zh": "日期范围" },
      "min_date": "2026-01-01", "max_date": "2026-12-31",
      "default": { "from": "2026-06-01", "to": "2026-06-30" } }
  ]
}
```

### 2.1 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `key` | ✅ | 切片表达式里用的名字。`^[a-z][a-z0-9_]{0,31}$`（小写、字母开头、不用横杠）。全表唯一。 |
| `type` | ✅ | `select`（单选）/ `multi`（多选，边栏是 chips）/ `range`（数值区间）/ `date_range`（日期区间）。 |
| `label` | ✅ | `{en, zh}`，只给一种语言也行（另一种回落同值）。边栏按读者当前语言取。 |
| `width` | ❌ | `full` / `half`，边栏里的排版提示（两个 `half` 会并排）。 |
| `options` | select/multi ✅ | 每项 `{value, label}`；**也接受裸字符串数组**（`["gto","spto"]`，label 用 value 本身）。 |
| `default` | ❌ | 读者的初始视图。不给就取第一个 option / 整段区间（见 §2.2）。 |
| `min`/`max`/`step`/`unit` | range ✅/❌ | `min<max`；`step` 不传按区间 1/20 推导；`unit` 显示在输入框旁。 |
| `min_date`/`max_date` | date_range ✅ | 都必须是 `YYYY-MM-DD` 字符串。 |

### 2.2 校验规则（服务端**拒绝**而不是替你改）

| 情况 | 结果 |
| --- | --- |
| `key` 不合规 / 重复 | 400，提示必须是 lower_snake_case |
| 超过 **12** 个筛选器 | 400（边栏放不下） |
| `options` 为空、超过 **80** 个、有重复 value、value 超过 64 字符 | 400 |
| `select.default` 不在 options 里 / `multi.default` 里有不存在的值 | 400（**故意的**：默认值渲染不出来 = 卡片一打开就是空白页） |
| `range` 的 `min >= max`、`step <= 0` | 400 |
| `range`/`date_range` 的 default 越界 | **夹到边界**（不报错，不隐藏内容） |
| `date_range` 的日期不是 `YYYY-MM-DD` | 400（`2026-1-1`、`2026/01/01` 都不行） |
| 省略 `default` | 自动取第一个 option / 第一个 option 组 / 整段区间 |
| `label` 只给 `en` 或只给 `zh` | 可以，另一种回落同值 |
| 传空 `filters: []` | 合法：等于"这个报告没有筛选器"，`kind` 会从 HTML 重新推导 |

⚠️ 服务端**不解析、不校验任何业务数字**，它只认形状。切片的语义完全由 §3 的属性决定。

### 2.3 响应

```json
{"ok": true, "slug": "q3-channel-slice", "kind": "dynamic", "configured": true,
 "filter_count": 4, "schema": { … }, "url": "https://…/r/q3-channel-slice"}
```

## 3. HTML 侧：`data-filter-when` 的语义

任何元素（**包括 `<section class="slide">`**）都能标：

```html
<section class="slide" data-filter-when="period=2026-06&sales_metric=gto">…</section>
<div data-filter-when="price=5000">…</div>            <!-- range：这是"本块声明的值" -->
<div data-filter-when="window=2026-06-15">…</div>     <!-- date_range：同上 -->
<div data-filter-when="sales_metric=gto|spto">…</div> <!-- 任选其一命中 -->
<div data-filter-when="sales_metric!=quantity">…</div><!-- 不等于 -->
```

| 规则 | 说明 |
| --- | --- |
| `&` 连接多个条件 | **AND**：全部满足才显示 |
| `\|` 分隔多个值 | **OR**：命中任一即可（`select` 下等于枚举） |
| `key!=v` | 排除。多选下表示"选择集合里没有 v" |
| `select` | 与读者的值**相等**才命中 |
| `multi` | 读者选中的集合**包含** `v` 才命中；**读者一个都不选时，所有 `key=v` 的块都不显示** |
| `range` | 块的 `v` 是数字，命中条件 `选择.min <= v <= 选择.max` |
| `date_range` | 块的 `v` 是 `YYYY-MM-DD`，命中条件 `选择.from <= v <= 选择.to` |
| **条件里的 key 不在 schema 里** | **视为命中**（内容保留），控制台 warn 一次 —— 不因为 schema 缺失/版本旧就把内容藏掉 |
| 没有 `data-filter-when` 的元素 | 永远显示（共享内容：标题、脚注、封面、说明） |
| 属性值写坏了（没有 `=`） | 该条件忽略，内容保留 |

### 3.1 只写 CSS 也能用（给"不想写 JS"的报告）

运行时会把当前选择写到根元素上，你可以直接用它做样式：

```html
<html data-filters="period=2026-06;sales_metric=gto,quantity;price=3000..9000"
      data-filter-period="2026-06" data-filter-sales_metric="gto,quantity" data-filter-price="3000..9000">
```

### 3.2 想自己重绘的报告（可选）

- 事件：每次选择变化后，`document` 与 `window` 上都会派发 `report:filters`，
  `event.detail = { selection, schema }`。**先用 `data-filter-when` 兜底**，再在事件里做额外效果。
- API：`window.reportFilters = { schema, selection, effective, apply(selection), reset(), isVisible(el), serialize() }`。
- 宿主（Workspace 边栏）会向 frame `postMessage({type:'report-filters', selection})`；
  运行时收到后同样会应用并派发事件。
- ⚠️ **你的脚本不需要自己取数、也不需要自己存**。选择值由宿主持久化（§5.4），
  页面加载时服务端已经把你（这个读者）的选择注入了 —— 不会先闪一下默认视图。

## 4. 读者侧到底发生什么（决定了你怎么写内容）

1. 打开卡片 → 查看器右侧是报告、**左侧是边栏**。边栏是 Workspace 的界面，不是报告的一部分。
2. 边栏控件由 §2 的 schema 生成：`select` → 下拉；`multi` → 可多选的 chips；`range` → 两个数字框；
   `date_range` → 两个日期框。另有 `Reset`（回到你给的 default）。
3. 改一次 → 立即生效（走上面的 postMessage），并在 500ms 防抖后存到服务端（**按人存**）。
4. 重新打开同一报告 → 还是这个人上次的选择。**别人的选择互不影响**，公共区/拉取副本各自独立。
5. 报告里的分页会跟着变：被筛掉的 `<section class="slide">` 不再计入页码（读者看到的是 `3 / 7`，
   不是 `3 / 12`）。
6. 导出 PPT / PDF **沿用这个读者当前的选择**（导出的是他正在看的那一版）。
7. `/s/{token}` 免登录链接：只读，且**没有人**的选择（用你的 default），边栏也不会出现。

### 4.1 读者**不能**做的事（写在代码和产品规则里，不是"暂时没做"）

- 不能在报告页里增删改任何筛选器，也不能改你的切片内容（页面是只读快照）；
- 不能改筛选器的顺序、类型、选项 —— 那是你（agent）的 `PUT /filters`；
- 不能用"备注/标注"在动态报告上留痕（没有备注层）。

## 5. 接口一览

### 5.1 注册 / 替换筛选器（agent 写）

```
PUT api/reports/{slug}/filters          body = §2 的 schema
→ 200 {"ok":true,"slug":…,"kind":"dynamic","configured":true,"filter_count":4,"schema":{…},"url":…}
```

- 幂等：整体替换（不是合并）。要删一个筛选器就重发不含它的整份 schema。
- 在 `static` 报告上调用会把它**升级成 `dynamic`**（schema 和 `data-filter-when` 是同一件事的两半）。
- 只能改**调用者自己私人工作区**里那一行：公共区快照 / 别人分享给你的副本 → 404。
- 文档卡片（pptx/xlsx/…）不能有筛选器 → 400。

```
DELETE api/reports/{slug}/filters
→ 200 {"ok":true,"slug":…,"kind":"static","configured":false,"filter_count":0}
```
清空后 `kind` 按 HTML 重新推导（HTML 里还有 `data-filter-when` 就仍是 `dynamic`）。

### 5.2 读一个报告的边栏状态（所有能读该报告的人）

```
GET api/reports/{slug}/filters
→ 200 {"slug":…, "kind":"dynamic", "configured":true,
       "schema": { "version":1, "filters":[ … ] },
       "selection": { … 这个读者存过的（原始值）… },
       "effective": { … 已按下述规则规整的、真正生效的值 … },
       "dropped": [ "metric" ],          /* 存的键/值在当前 schema 里已不存在 */
       "can_edit": true,                 /* 调用者是否是这份原稿的所有者 */
       "updated_at": "…", "selection_updated_at": "…" }
```

`effective` 的规整规则（服务端与注入的运行时**用同一套规则**）：
未知 key 丢弃；`select` 的值不在 options 里 → 回落 default；`multi` 取交集（**显式空数组保留为空**，
表示"一个都不选"；全部值都失效才回落 default）；`range`/`date_range` 夹到边界、反向输入交换。

### 5.3 读者侧会用的（你不用管，知道存在就行）

```
PUT api/reports/{slug}/filters/selection   body = {"selection": {"period":"2026-06", …}}
→ 200 {"ok":true,"slug":…,"selection":{…规整后…},"dropped":[],"updated_at":"…"}
```
只有**浏览器会话**能调（人的视图不该被脚本改）。agent 用 Basic/Bearer 调会 403 ——
要让所有人都默认看某个月，请改 schema 的 `default`。

### 5.4 存哪、什么时候失效

- 选择值存在 `ai_report_filter_selections`，键是 `(报告, 邮箱)`；报告删除时级联删除。
- 报告重新发布（同 slug 覆盖）**不影响**已存的选择；只有 schema 里删掉了某个 key/option 时，
  读者那份会被规整（`dropped` 会告诉他"你的视图被调整了"）。

### 5.5 常见状态码

| 码 | 场景 |
| --- | --- |
| 400 | schema 校验失败（§2.2），`detail` 写清楚是哪个 filter 的哪个字段 |
| 404 | slug 不存在 / 你无权改（不是你的原稿、或它是公共区快照、或它是文档卡片？→ 400） |
| 409 | 报告还没有筛选器却去存选择值 |
| 403 | 用机器凭据调 `/filters/selection`（浏览器专属） |

## 6. 交付自检 checklist

- [ ] HTML 里**没有任何控件**：没有 `<input>` / `<select>` / `contenteditable`
      （有的话这个报告会被识别/连接成互动报告的路子，读者改的东西没人保存 —— 而且它违反产品规则）。
- [ ] **每一条内容都属于某个切片或明确共享**：`default` 选择下页面不能是空的（先自己按 §3 心算一遍，
      或本地截图看一眼）；缺内容比写错内容更常见。
- [ ] 每页（deck 的 `<section class="slide">`）都标了 `data-filter-when`，除非它要在所有选择下都出现。
- [ ] 双语：EN/ZH 两套**各自**都要有对应的切片（`data-lang` 与 `data-filter-when` 可以同时标）。
- [ ] `PUT /filters` 的每个 `default` 都能在 HTML 里找到对应切片（默认视图不能是空白）。
- [ ] 发之前真的点一遍：打开卡片 → 左边改一次 → 右内容变了 → 刷新 → 还是这个选择；
      顺带看一眼页码是不是跟着变了。
- [ ] `key` 用的是小写和下划线（`sales_metric` 而不是 `salesMetric`）。

## 7. 交付

发布、封面、`submitter`、双语、分享链接（`{BASE}/r/<slug>`）、错误码**全部照
`klado-v2:format-report-html:zh` §2–§6**。本文新增的只有两件：① HTML 里标 `data-filter-when`；
② 发布后 `PUT api/reports/{slug}/filters` 注册 schema。缺第 ② 步的报告会被当成"排版坏了"，
所以**把两步当成一次交付做完**，别拆开。