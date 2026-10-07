---
document_id: klado-v2:format-calendar-event:zh
title: format-calendar-event
document_type: format
source: klado-workspace
version: 1.13
tags: [format-calendar-event, calendar, 日历, 事件, 甘特图, 排期, 里程碑, milestone, deadline, 截止, partner, 附件, todo, 待办, 行动项, cover, 封面, export, 导出, 双语]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/format-calendar-event.md
---

> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造。本文只讲**怎么创建一条日历事件**。

# 日历事件：怎么建、页面怎么排

**Calendar 是第三套东西，别和另外两套混**：

| | 是什么 | 生命周期 | 写入方式 |
|---|---|---|---|
| **Workspace 报告** | 一份分析/复盘（16:9 deck） | 一次交付 | `POST /api/reports` |
| **业务知识库页面** | 一条业务知识（markdown） | 长期维护、反复更新 | `POST /api/knowledge/items` |
| **日历事件** | **一段有起止日期的安排**，带自己的 16:9 说明页 | 有 start/end，会过期 | `POST /api/calendar/events` |

事件 = **日程（日期）+ 一页 16:9 说明 + 参与人 + 附件**。判断标准：**用户问的是"什么时候"，
就用日历**；问的是"结论是什么"，用 Workspace 报告。

---

## 1. 字段

```bash
curl -sS -X POST "$BASE/api/calendar/events" "${AUTH[@]}" -H 'Content-Type: application/json' -d '{
  "title": "Q3 渠道复盘会",
  "start_date": "2026-09-01",
  "end_date": "2026-09-12",
  "deadline": "2026-09-10",
  "kind": "review",
  "category": "渠道",
  "tags": "Q3,渠道,复盘",
  "summary_en": "Q3 channel review: 6 channels, one page of conclusions and the action list.",
  "summary_zh": "Q3 渠道复盘：6 个渠道，一页结论与行动清单。",
  "description_en": "What this review is for, why now, and what \"done\" looks like — the panel at the top of the page.",
  "description_zh": "这次复盘要解决什么、为什么现在做、做到什么算完成 —— 也就是页面上方那块面板里的文字。",
  "partners": ["li.ming@example.com", "wang.fang@example.com"],
  "attachments": [
    {"kind": "report",    "slug": "q3-channel-deep-dive"},
    {"kind": "knowledge", "slug": "channel-caliber-notes"}
  ],
  "todos": [
    {"text": "把 6 个渠道的进销存口径对齐", "assignee": "li.ming@example.com", "due": "2026-09-08"},
    {"text": "对同比异常的渠道做归因",       "assignee": "wang.fang@example.com", "due": "2026-09-10"},
    {"text": "把行动项落到负责人与时间点",   "assignee": "", "due": "", "done": true}
  ],
  "body": "<!doctype html>…（见 §3 的排版规则）…"
}'
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `title` | ✅ | 事件名（甘特条上显示的就是它，**写短**，长的部分交给页面） |
| `start_date` | ✅ | `YYYY-MM-DD`。**只有日期，没有时刻** —— 日历是月格子，时/分是格子展示不了的精度 |
| `end_date` | | 结束日（**含**当天）。省略 = 只占一天。早于 `start_date` 直接 **400** |
| `deadline` | | 截止日，画成独立标记（和 `end_date` 不是一回事：排期到 12 号、10 号要交付） |
| `summary_en` / `summary_zh` | ✅（双语页面） | 一行摘要，见 §2 |
| `summary` | 单语页面 | 双语的页面用上面两个；不传时会取 `summary_en` |
| `description_en` / `description_zh` | | **页面上方那块「项目描述」面板的文字**（2–4 行，最多 1200 字），一个语言一份 —— 与摘要同理，单份会让中文页面显示英文。⚠️ **2026-09-30 起才是字段**：在此之前它只能写在页面 `body` 里，所以编辑器面板根本没有这一格（用户问「这段文本在哪里可以编辑？」就是这么来的）。**留空 = 那一页保持你写在 body 里的原稿**，不是清空 |
| `kind` | | **闭集合，只能从下面 12 个里选**（决定甘特条配色与工具栏的类型筛选器）。⚠️ **不在表里的值不会被报错，而是被静默改成 `other`** —— agent 编一个 `"webinar"` 会拿回一条看起来正常、类型却不是它写的的事件。**不要自创类型。** |
| ⇢ `meeting` | | **会议**（周会、月度对齐、跨部门评审会、供应商/客户沟通会） |
| ⇢ `review` | | **复盘 / 评审**（季度复盘、项目复盘、上市后复盘） |
| ⇢ `launch` | | **上市**（新品上市、NPI、上市发布） |
| ⇢ `promotion` | | **促销**（大促、618/双 11、旺季促销、渠道促销） |
| ⇢ `campaign` | | **品牌战役**（整合营销战役、品牌年度主题） |
| ⇢ `media` | | **媒介投放**（TVC/OTV/OOH、社媒、达人/KOL 投放） |
| ⇢ `content` | | **内容制作**（KV、TVC 拍摄、产品素材、详情页内容） |
| ⇢ `offline` | | **线下活动**（展会、路演、体验会、门店活动） |
| ⇢ `training` | | **培训**（导购/销售培训、产品知识培训、经销商大会） |
| ⇢ `research` | | **调研**（消费者调研、竞品调研、价格带研究） |
| ⇢ `planning` | | **规划**（年度/季度规划、AOP、目标拆解） |
| ⇢ `other` | | **其他**，同时也是"没写类型"和"写了不认识的值"的落点 |
| `partners` | | **参与人的邮箱**（示例 `li.ming@example.com`；可接受的域名由 `AUTH_ALLOWED_EMAIL_DOMAIN` 决定，留空 = 不限）。⚠️ 见 §4：加进来就**等于给了读权限** |
| `attachments` | | 引用已有报告/知识库页面/文件库对象，**不是副本**：`{"kind":"report"\|"knowledge","slug":"…"}`（也接受 `"report:slug"` 简写）或 `{"kind":"file","path":"<bucket>/<key>"}`（文件库里已存在的对象，人可以在网页上直接上传） |
| `todos` | | **待办清单**，每行 `{"text","assignee","due","done"}`（也接受 `"写一段文字"` 简写 = 只有 text）。⚠️ **不再是页面里的散文**：写进这里，页面左栏与日历都由它生成。`assignee` 是邮箱（示例 `li.ming@example.com`；**只是署名，不给读权限** —— 给读权限的只有 `partners`）；`due` 是 `YYYY-MM-DD`，**且不得早于事件的 `start_date`**（它是这条事件的里程碑，事件开始之前没有它可落的那一天；界面里的日期选择器已经限制住，但 API 不拦，写早了所有者打开面板会**存不回去**）；**有 `due` 的那一条会作为里程碑画在日历上那一天** |
| `status` | | `published`（默认）/ `draft` / `cancelled` |
| `body` | ✅ | 事件自己的 **16:9 页面**，见 §3 |
| `slug` | | 可选。带上已有 slug = 覆盖更新（更新自己的事件**必须**带，否则每次都是新建） |

**改事件**：带 `slug` 重发，或 `PUT /api/calendar/events/{slug}`。

⚠️ **`PUT` 是「合并」：请求体里没提到的字段保留库里的值。** 只想改截止日，就只发
`{"deadline": "2026-09-20"}` —— 不必回传整页 `body`（那有几万字），也不会因为漏写而把页面清掉；
省略 `summary_zh` 同样不会让双语事件撞上 §2 的 400。要**清空**就显式发空值：`"partners": []`、
`"attachments": []` 会把人/附件全部移除，但**空的 `body` 仍然 400**（页面是必需的）。
`partners` / `attachments` 一旦传了就是**整体替换**：@ 名单是"现在谁在这个事件上"。

⚠️ **`title` 撞名**（和某个你能读到的事件同名）在**新建**时会 **404/409 拦一次**：
响应里列出冲突的事件，确认后带 `"confirm_conflict": true` 重发。

---

## 2. 摘要必须双语（与报告、知识库同一条规则）

`body` 里同时有 `data-lang="en"` 和 `data-lang="zh"` 时，`summary_en` 与 `summary_zh`
**都必填**，缺一个回 **400** 并点名缺哪个。理由和报告一样：日历格子里那一行摘要，
是读者判断"这个条要不要点开"的唯一依据；只写一行，等于让一半读者读到外语。

---

## 3. 事件的 16:9 页面 —— 用固定模版，不要自己排版

**硬约束先读**：`klado-v2:format-report-html:zh` §1.3（16:9 分页、固定 1920×1080 画布、
不重排）与 §1.5（双语 `data-lang`）**全部适用**。

⚠️⚠️ **不要自己发明版式。** 每个元素的位置都被模版钉死了 —— 这是用户明确要求的结果：以前每页各排各的，
一段摘要长一点就把右边的事实条目挤下去，两个事件放在一起永远对不齐。**你只填内容，位置由模版决定。**

### 3.1 照抄这份骨架（双语 = 两个 slide，各一份）

⚠️ **位置由模版决定，包括这一段**：`.ev-desc` 虽然写在 `.ev-main` 里，渲染出来在**右栏最上面**（左栏整栏留给 To-do）——
这是用户明确要求的排列。你**不要**为了把它挪到右边去改结构，照抄即可。

```html
<style>
  /* 只放你这页特有的东西（比如卡片里的补充说明）。⚠️ 不要重写 .ev-* 的尺寸，
     那些是模版的固定框；服务端会在最后注入模版样式表，你的同名规则会被它盖掉。 */
</style>
<div class="deck">
  <section class="slide ev-page" data-lang="en">
    <div class="ev-head">
      <span class="ev-when" data-ev="schedule"></span>          <!-- 服务端填：start → end -->
    </div>
    <!-- ⚠️ 标题由模版排进上面的页眉行（左侧，与排期同一行）。写完就放在这里，
         位置不用你管；也不要再写 kicker —— 那行 `KIND · category` 已于 2026-09-30 取消。 -->
    <h1 class="ev-title" data-ev="title"></h1>                  <!-- 服务端填：事件 title -->
    <div class="ev-cols">
      <div class="ev-main">
        <!-- 项目描述（渲染成右栏顶部的一段导语，左侧有一条细蓝规）。⚠️ 只装得下 6 行：中文 ≤150 字 / 英文 ≤320 字符，再长会被静默裁掉。
             也可以整段不写、改用 description_en/zh 字段；
             页面编辑器面板里就有这两格，字段填了就以字段为准。 -->
        <p class="ev-desc">项目描述：这个项目要做什么、为什么做、做到什么算完成（2–4 行）。</p>
        <h2 class="ev-block-title">To-do</h2>
        <ul class="ev-todo" data-ev="todo"></ul>   <!-- 服务端填：事件自己的 todos -->
      </div>
      <aside class="ev-side">
        <div class="ev-card"><h2 class="ev-block-title">Partners</h2>
          <div class="ev-card-body" data-ev="partners"></div></div>
        <div class="ev-card"><h2 class="ev-block-title">Deadline</h2>
          <div class="ev-card-body" data-ev="deadline"></div></div>
        <div class="ev-card"><h2 class="ev-block-title">Attachments</h2>
          <div class="ev-card-body" data-ev="attachments"></div></div>
      </aside>
    </div>
  </section>
  <section class="slide ev-page" data-lang="zh"> <!-- 同上，标签换成中文 --> </section>
</div>
```

### 3.2 六个 `data-ev` 槽 —— **留空，由服务端填**

| 槽 | 服务端填什么 | 来自 |
|---|---|---|
| `title` | 事件标题 | `title`（**与事件同名，别另起**）。⚠️ 由模版排进**页眉那一行**（左侧，与排期同排），一页只有这一个标题 |
| `schedule` | `2026-10-12 → 2026-10-16`（同一天只写一个） | `start_date` / `end_date` |
| `deadline` | 截止日（没有就写 `—`） | `deadline` |
| `partners` | 每人一行：姓名 + 小字邮箱 | `partners` |
| `attachments` | **逐条链接**（标题 + 类型） | `attachments` |
| `todo` | 每行：勾选框 + 文字 + 人名 + due date | `todos` |

⚠️ **曾经还有一个 `kicker` 槽（`KIND · category`），2026-09-30 已取消** —— 用户要的是"这一页叫什么"，
而不是一串内部词；`kind` / `category` 仍然在日历悬停卡和事件面板里看得到。老页面里的 kicker 会被模版
样式表隐藏（不用你去改老页面）。

⚠️ **必须是空元素**（`<div data-ev="attachments"></div>`）—— 服务端只填空的，写了内容的不动。
⚠️ **`todo` 是唯一的例外**：事件的 `todos` 非空时，页面里**任何** `.ev-todo` 列表（哪怕是以前手写的
`<li>`）都会被 `todos` 覆盖 —— 否则老页面上你改了待办、页面上却不变。`todos` 为空时页面一个字都不动。
⚠️ **附件不要自己写链接**：你写不出正确的地址形状，而且会和事件里的附件脱节。服务端按类型生成**相对地址**
（报告 `r/<slug>`、知识库 `?kb=<slug>`、文件 `api/storage/serve?path=…`，都在新标签打开）。
⚠️ 加了附件、换了截止，**页面会自动跟上** —— 你只需保证事件的字段是对的。

### 3.3 每格都是固定框，写多了只会在框里被裁掉

| 元素 | 固定框 | 写法要求 |
|---|---|---|
| 标题 | **1 行**（页眉行左侧，44px；与排期同一行） | 太长会被省略号截（不会挤动任何东西）—— 写短一点，页面上只有这一处标题 |
| **项目描述** `.ev-desc` | **右栏顶部一条导语**（210px，带 4px 蓝规、**无底色**）＝ **最多 6 行** | ⚠️⚠️ **它一共只装得下 6 行**（文本宽 710 画布 px、24px 字号、行高 1.5）。实测：**中文 ≤ 150 字、英文 ≤ 320 字符**仍是 5.8 行、完整显示；中文 192 字 / 英文 470 字符就**静默裁掉**（`overflow:hidden`，页面上看不出少了东西）。所以写 **2–4 行**、讲清"做什么/为什么/做到什么算完成"就停 —— 它是**导语**不是正文，正文放待办或 Workspace 报告。写在 `.ev-main` 里，模版把它排到右栏；文字**也可以走字段** `description_en`/`description_zh`（编辑器面板里有这两格，**字段优先于 body 里的原稿**）。⚠️ 2026-09-30 第二轮改版：从"占右栏一半的填充蓝框"收成导语 —— 满色块让它成了全页最抢眼的东西，抢了标题的注意力 |
| 待办 `.ev-todo` | **整条左栏**（右栏只有导语 + 三块事实） | 由 `todos` 填，最多 **30 条**，左栏放不下的会被裁；每条写一件可执行的事。每行最多 160px 高，所以行数少时不会把行撑得又高又空 |
| 右栏三块事实 | **导语之下，各按内容取高、彼此用 1px 发丝线分隔** | Partners / Deadline / Attachments，由服务端填。⚠️ 2026-09-30 第二轮：取消"三个等高填充卡片"（那是"AI 味"的来源），改成"标签在上、值在下"的事实条目 |

**为什么这样：** 位置固定 ⇒ 任何两个事件页放在一起，右栏的三块事实都从同一个高度开始。所以你**不需要**为了对齐去
凑字数、也不需要调 padding；反过来，**不要**把长文塞进标题或卡片 —— 该放长文的地方是项目描述和待办。

### 3.4 第一页的信息量（用户双击就是来看这些的）

标题 + **项目描述** + 待办 + 参与人 + 截止 + **附件链接** —— 六样齐全（`kind` / `category` 自 2026-09-30 起不上页面）。
其中**待办现在来自 `todos` 字段**（不是页面里手写的 `<li>`）：只有进了 `todos` 的行才会出现在页面上、
才能在前端被勾选/增删、才会在日历上画成里程碑。
`deck-foot`（页码条）由 deck 运行时提供，不用自己画。

### 3.5 里程碑：**它在事件里面，不是一个事件类型**

⚠️⚠️ **这一条最容易搞错，先读它。** 里程碑 = **某条事件里、某个待办的 `due`**。它不是、也
不要再建一个"里程碑事件"：

- 你来安排的是**事件**（有起止日期的一段工作），里程碑是这段工作里的**节点**。所以
  「8 号要交样品」应该写成该事件 `todos` 里的一条 `{"text": "…", "due": "2026-11-08"}`。
- 日历会把每条带 `due` 的待办画成**月份行底部那条带里的一个菱形**（见 §3.5 图）。
- ⚠️ `kind: "milestone"` **已经退役**：它曾经是第 6 个类型，现在不再出现在界面里，也不该
  由你写入（写了不会被拒，但会落进 `other` 的配色与筛选里，看起来像一条普通的其他事件）。
  老数据里仍然存在的 `milestone` 事件会继续画成菱形，不会被破坏。

**日历上长这样**

```
  … 7   [8]   9 …            ← 一个格子就是一天
        ◆ CD90 上市          ← 菱形画在其 start_date 那一格，标题跟在右边
```

- **菱形 = 一个点**：带 `due` 的待办在日历上**没有长度**（`due` 就是那天，
  `end_date` 传同一天或不传）。它表达"这一天有事"，不是"这段时间在做这件事"。
- ⚠️ **别用 `event` + 同一天来凑**：那会画成一根很短的条，看起来像"一天的活"，
  和真正的单日节点分不开 —— 这正是 `kind` 存在的意义。
- 节点本身也可以有 16:9 页面（第一页那五样照样要有），双击照样打开。
- 颜色：整张日历是**一套低饱和的莫兰迪配色**（12 个类型各一色，白字在这 12 个底色上的
  对比度都 ≥ 4.8:1），**不要**在页面里另配一套颜色去和它抢。
- ⚠️ **每一条带 `due` 的 `todos` 也会在日历上画一个小菱形**（月份行底部那条带里），
  与事件条同一个低饱和色系。所以「某天要交一件事」用 `todos` 的 `due` 表达就够了 ——
  **千万不要为了同一件事再建一个"里程碑事件"**（类型表里已经没有它了），那只会让同一天
  多出一个说不清来历的标记。

**优先用报告 deck 已有的布局类**（`.s-kicker .s-title .s-sub .s-lead .s-cols .s-card .s-note .s-table .s-chip`），
不要自己从零写一套 grid —— 同一套类才能保证和 Workspace 里的东西看起来是一个系统。

**后端事实**（写页面时会用到）：

- 事件页默认**英文**；`?lang=zh` 切中文。用 `data-lang` 标语言，**不带 `data-lang` 的内容两种
  语言都显示**（适合放图表、logo）。
- 页面是**静态快照**：要在建事件时就把数字写死，**不要**放 `<script>` 去实时取数
  （事件页会在 iframe 里被读，脚本拿不到会话）。
- 单页 ≤ 8MB；不要外链字体、不要 root-absolute 路径（`/api/...` 会落到平台网关上）。
- **附件不要复制内容进来** —— 正文里放链接和标题就够了，内容在它自己的地方维护着。

---

## 4. 参与人 / 共享：一条"人能看见谁"的规则

**三个层次**（与 Workspace、知识库完全一致）：

| 谁能看 | 怎么来的 |
|---|---|
| **所有者** | 建它的人（或他的 agent 凭据所属账号） |
| **参与人**（partner） | `partners` 里被 @ 的邮箱。⚠️ **加进来 = 给了读权限**，两件事是同一件事，不是两份名单 |
| **所有人** | 所有者在网页上点了 **Publish to public**（只读快照） |

- ⚠️ **agent 可以 @ 同事，但不能发布到公共区**：@ 某个人是"把这个人放进这个事件"，
  而发布是"让所有登录用户都看得见" —— 后者一律要人在网页上点。agent 调 publish 会 **403**，
  别重试、别说成功了。
- ⚠️ **每次写入 `partners` 是整体替换**：要保留的人必须一起传，否则会被移出去。
- 读不到的事件一律 **404**（不是 403）—— 别用它探测别人有没有某个事件。

---

## 5. 封面：和 Workspace 报告用的是同一套机制

事件有封面（16:9 JPEG），生成、存储、取图与报告**完全是同一套**（`services/report_cover.py`），
所以调法也照报告那套用：

```bash
# 给这个事件生成一张（服务端出图，账号不需要任何图片凭据）
curl -sS -X POST "$BASE/api/calendar/events/q3-channel-review/cover" "${AUTH[@]}" \
  -H 'Content-Type: application/json' -d '{"ratio": "16:9"}'

# 已有图就直接存（base64 或外部 URL），不要先传后生成
curl -sS -X POST "$BASE/api/calendar/events/q3-channel-review/cover" "${AUTH[@]}" \
  -H 'Content-Type: application/json' -d '{"cover_url": "https://…/cover.jpg"}'

# 取图（我们存的字节；外部 URL 会 302 过去）；删掉
curl -sS "$BASE/api/calendar/events/q3-channel-review/cover" "${AUTH[@]}" -o cover.jpg
curl -sS -X DELETE "$BASE/api/calendar/events/q3-channel-review/cover" "${AUTH[@]}"
```

- 也可以在**发布那一次**一起带上：`"generate_cover": true`（或 `cover_base64` / `cover_url`），
  与 `POST /api/reports` 的字段名、拼写别名完全一致；`"clear_cover": true` 移除。
- ⚠️ 只有**所有者**能生成/替换/删除（别人 404）。`ratio` 缺省 16:9；封面配色由部署侧的**固定中性色板**决定，不随业务或品牌变化（旧的 `brand` 参数已不再影响配色）。
- 抬头与悬停卡片都会显示它 —— 封面是"这条事件长什么样"的第一眼，别拿无关的图凑。

---

## 6. 导出：网页上能导，agent 不用管

人在事件页浮层里点 **Export** 可以导出 Word / PDF / Picture（`POST /api/calendar/events/{slug}/export/{kind}`，
`kind` = `word` / `pdf` / `picture`）。**导出的是浏览器渲染出来的那一页**，所以：

- 只带当前显示的那一页 slide（不是整本 deck），隐藏的语言版本和页面里的脚本都不会进去；
- 有封面就把封面放在最上面。

⚠️ 这是**人工操作**通路（要浏览器渲染页面才能导出）。agent 若要交付文件，仍然用
Workspace 报告 / 知识库那两条各自的路，别在这里绕。

---

## 7. 交付前自检

1. `start_date` 写了吗？`end_date` 没早于它吧？**只有日期**（不要带时/分）？
1b. 页面用了 §3.1 的骨架吗？**六个** `data-ev` 槽（`title` / `schedule` / `deadline` / `partners` / `attachments` / `todo`）**留空**了吗（尤其附件与待办 —— 别自己写内容）？⚠️ 不要写 `kicker`（2026-09-30 取消）。
1c. 有「项目描述」那一格吗？2–4 行，讲清做什么/为什么/完成标准？
1d. 待办写进 `todos` 了吗（**不是**页面里手写的 `<li>`）？每条的 `assignee` 是邮箱、`due` 是 `YYYY-MM-DD` 吗？⚠️ **`due` 早于事件 `start_date` 的千万别写** —— 界面会拒绝保存这条事件（"is due …, before this event starts"），等于给所有者留了一个改不掉的事件。
1e. `kind` 是从那 **12 个**里选的吗（会议用 `meeting`）？⚠️ **别自创类型、也别写 `milestone`** —— 里程碑是**事件里的待办 `due`**，不是一个事件类型（§3.5）。写错不会被拒，只会被静默改成 `other`。
2. 页面第一页有没有**这五样**：摘要、待办、参与人、截止、附件？（双击的人就是来看这五样的）
3. **双语两版都在吗？** `summary_en`/`summary_zh` 都写了吗？
4. `partners` 写全了吗？**原来就在上面的人有没有被这次写入挤掉？**
5. 附件是**引用**（kind + slug / kind=file 的 path）而不是把内容抄一遍吗？
6. 想好封面了吗？没有就 `generate_cover: true`（§5）—— 留空的话卡片/抬头是灰的。
7. ⚠️ **所有者会在网页上直接改这条事件**（改截止、加参与人、传附件）：所以页面正文里
   别写死"截止 9 月 10 日"这类会变的字段，抬头已经显示它们了。
8. 标题够短吗（甘特条上要显示得下）？页面里的标题和 `title` 一致吗？⚠️ 页眉那一行放不下就会被**省略号截掉**。
9. **项目描述数过字数了吗？**它是右栏顶部一条 210px 的导语，**一共只装得下 6 行**：中文 **≤ 150 字**、英文 **≤ 320 字符**（再长就**静默裁掉**，页面上看不出少了东西 —— 别指望用户会来告诉你）。超了就删到 2–4 行，把细节挪进待办或另发一份 Workspace 报告。三块事实排在它下面、各按内容取高。文字写进 `description_en`/`description_zh` 了吗（写 body 里也行，但字段优先）？
10. 标题写短一点 —— 它在**页眉那一行**（44px），太长会被省略号截掉，而不是换行。
