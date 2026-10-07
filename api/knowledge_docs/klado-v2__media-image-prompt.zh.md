---
document_id: klado-v2:media-image-prompt:zh
title: media-image-prompt
document_type: format
source: klado-runtime-curation
version: 1.10
tags: [media-image-prompt, media, image, prompt, 封面, 封面图, 封面配色, 报告封面, 图像, 提示词, 比例, 变体, 生图, 图片, 配色, internal-only]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/media-image-prompt.md
---

> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造；业务数字必须走批准的只读数据源，不得凭印象编造。
> 旧工具名（`search_knowledge` / `query_database` / `create_xlsx` …）是历史名，真实端点见该文档 §4。
> **生图（2026-09-30 起）**：首选**你自己（agent）的生图模型**直接出图；
> 服务器端点 `POST /api/reports/cover`（MiniMax）只是**没有生图能力的 agent 的兜底**（见下）。


---

# 图片生成 Prompt 规则（generate_image）
<!-- type: rule -->

本文档的 prompt 写法（比例、结构、风格约束）适用于**任何生图模型**——你自己的模型或服务器兜底端点都按这套规则写 prompt。

## 用哪个模型生图（2026-09-30 起，取代"一律走服务器"的旧规则）

1. **首选：你自己（agent）的生图模型。** 按下面的 prompt 规则出图，然后把图交给交付物
   （报告封面 → 发布请求里的 `cover_base64`，见 `klado-v2:format-report-html:zh` §3）。
   - **文生图**：所有带生图能力的 agent 都可用。
   - **图生图**：**飞书 agent 支持**——用户给了参考图（产品图、实拍图、风格参考）时，
     直接以图生图，把参考图的构图/材质/风格迁移到目标画面；仍要遵守下面的比例与风格约束。
2. **兜底：服务器端点 `POST /api/reports/cover`**（MiniMax `image-01`）。仅当你的运行环境
   **完全没有任何生图能力**时才用它（见本文档「报告封面」一节）。
3. **水印（硬性要求）**：生成的图片**不得带任何水印**——平台 logo、角标、AI 标识、
   "AI 生成"字样一律不要。生图通道有「水印」开关/参数的，**必须显式关闭**；
   交付前自查一遍画面四角与底部，有水印就重出。

## 前置要求

- 必须先通过 `query_database` 拿到真实数据，严禁用占位数字或估算值。
- 比例：**只用下面这四种，不要自创比例**。

  | 比例 | 形态 | 用在哪 | 参考像素 |
  |---|---|---|---|
  | **16:9** | landscape | 幻灯片 / dashboard / 报告页 / 横版 KV —— **没特殊说明时的默认** | 1600 × 900 |
  | **4:3** | landscape | 传统投影页 / 打印页 / 图文混排 / 需要更高正文区 | 1600 × 1200 |
  | **3:4** | portrait | 竖版海报 / 社媒竖图 / 手机端长图 | 1200 × 1600 |
  | **1:1** | square | 方形信息图 / 社媒方图 / 图标式主视觉 | 1200 × 1200 |

  - **默认规则**：用户没说、也无法从交付物类型判断时 → 用 `16:9`。内容明确是竖版海报 → `3:4`；明确是方图 → `1:1`。
  - **像素只是比例的一种表达**：需要传 `size` 参数时，传该比例对应的值（上表"参考像素"列，或工具支持的等价尺寸），且必须与 prompt 里的画布行是同一个比例。**绝不要用像素反推比例**——历史遗留的 `1536×1024` 是 3:2，属于四种之外的非法比例，必须先从上面四种里选一个再写。
  - 比例与内容冲突时（例如"竖版的 16:9 幻灯片"）→ **先问一句**，不要自己改比例，也不要一次生成两种比例。
- 含小字、图例、坐标轴、数据标签的图片：必须指定 `quality: hd`。
- 字体：Aptos Narrow（所有文字元素均指定）。
- 风格：**固定的中性编辑风（强制要求）**：冷白/白背景 #FFFFFF，主文字 charcoal #1A1A1A，强调色用低饱和深石墨蓝 #2B3A55（全图只用这一种主色），无深色背景、无霓虹/蓝紫主调、无圆形图标、无渐变、无阴影。

## 比例怎么写才高效

图片模型对**开头**的形态词权重最高；尺寸描述散落在多处、说法又不一致时，模型基本是各取一半。所以：

1. **首行就写画布**，不要藏在 prompt 中段：
   ```
   Canvas: 16:9 landscape (1600×900). White background (#FFFFFF).
   ```
   一句话同时给出「比例 + 形态词 + 像素 + 背景」，后面就不用再提画布了。
2. **数字后面必跟形态词**：`16:9 landscape` / `4:3 landscape` / `3:4 portrait` / `1:1 square`。只写 `3:4` 时模型常读成 `4:3`——这是最常见的翻车点。
3. **结尾约束块再提一次比例，且与首行说法完全一致**。首尾各一次就够；**不要三处以上重复**，也**不要把 `16:9` / `widescreen` / `1.78:1` 混着写**——多种说法并存等于让模型自己挑一个。
4. **工具有 `size` 参数就传参**；prompt 里的画布行是第二重保证，不是替代品。两者必须是同一个比例。
5. **不要在 prompt 里描述无法约束的版式**（如"右侧留白 40%"）。用「比例 + 布局分区描述」（top header / left KPI column / center chart / right table）代替。

## Prompt 结构（按此顺序，不要写成一段）

### 1. 交付物类型 + 画布
```
A professional sales performance dashboard slide.
Canvas: 16:9 landscape (1600×900). White background (#FFFFFF), neutral corporate visual style.
Layout: [各区域描述，如 top header / left KPI column / center combo chart / right SKU table]
```

### 2. 文字元素（所有图内文字必须用引号包裹）
```
Header: "Channel 2026 YTD Sales Performance (Jan–Apr)" bold 26px, charcoal #1A1A1A, top-left.
Subtitle: "Sell-out | Sell-in | Channel: Offline + Online" 12px gray #666, below header.
Section label: "KEY TAKEAWAY" 10px bold uppercase slate-blue #2B3A55.
All in-image text rendered verbatim — no paraphrasing, no missing characters.
```
> 专有名词渲染规则：品牌名、系列名、型号这类容易被模型拼错的拉丁词，在 prompt 里**逐字母拼出**防乱码（例如把 `Orion` 写成 `O-r-i-o-n`）；交付前核对画面无缺字、无乱码。

### 3. 图表数据（数字和标签直接写进 prompt）

**折线/组合图** — 用文字描述走势，同时给出每个 x 刻度的精确 y 值：
```
Combo chart: grouped bars (left y-axis, unit: K units, range 0–8) + two lines (right y-axis, unit: RMB M, range 0–130).
X-axis: 4 ticks — "Jan", "Feb", "Mar", "Apr".

Bars (sell-out qty, light gray): Jan=4.3, Feb=2.2, Mar=6.1, Apr=1.3
Bars (sell-in qty, mid gray): Jan=2.4, Feb=2.0, Mar=3.1, Apr=0.4

Line 1 "Sell-out SPTO (RMB M)" — solid slate-blue (#2B3A55), circle markers:
  Jan=43.9 → Feb=22.1 (drops sharply) → Mar=61.3 (rises to peak) → Apr=25.1 (drops)
Line 2 "Sell-in SPTO (RMB M)" — dashed dark gray, diamond markers:
  Jan=24.1 → Feb=20.2 (slight drop) → Mar=98.4 (rises steeply to peak) → Apr=13.3 (drops sharply)

Each data point labeled with its value. Dual y-axis: left for bars, right for lines.
```

**表格** — 每个单元格值都写出来：
```
Table "TOP 5 SKU BY SELL-OUT", columns: SKU | Brand | Sell-out value | Qty | ASP
Row 1: "SKU-0001" | "Brand A" | "¥19.8M" | "1.0K" | "¥19,800"
Row 2: "SKU-0002" | "Brand A" | "¥14.8M" | "1.4K" | "¥10,570"
（每行每格用引号包裹）
Header row: charcoal background, white bold 11px. Data rows: alternating white / light gray.
```

### 4. 约束（放在 prompt 最后）
```
Aspect ratio: 16:9 landscape (unchanged from the canvas line above).
Typography: Aptos Narrow font throughout — headings, labels, axis ticks, table cells, all text elements.
White background (#FFFFFF), charcoal main text (#1A1A1A), one slate-blue accent (#2B3A55) used sparingly.
No dark backgrounds, no neon or blue-purple colour scheme, no circular badge icons, no gradients, no drop shadows.
All labels fully legible, no overlapping text, no blur.
Polished spacing, professional editorial presentation quality.
```

> 结尾这一行只写**「比例 + 形态词」**（与首行同比例），不要在这里再塞像素或第三种说法。四种比例之外的一律不写。

## 常见错误

| ❌ 错误 | ✅ 正确 |
|---|---|
| 数组 `[43.9, 22.1]` 单独出现 | 每个 x 刻度对应精确 y 值 + 走势描述 |
| 文字不加引号 | 所有图内文字用 `"引号"` 包裹 |
| 省略 `quality: hd` | 含小字/图例时始终 `quality: hd` |
| 品牌名 / 型号直接连写 | 逐字母拼出（如 `O-r-i-o-n`）防乱码 |
| 只写 `16:9`、不跟形态词 | `16:9 landscape`（`3:4` 极易被读成 `4:3`） |
| 混用 `1536×1024` / `widescreen` / `1.78:1` / `1024×1024` | 只写四种比例之一，数字后跟形态词 |
| 比例只写在 prompt 中段，或首尾说法不一致 | 首行 + 结尾各一次，说法完全一致 |
| 自作主张改成 3:2、21:9 等 | 比例与内容冲突时先问一句 |


---

# 报告封面（Workspace 卡片封面）

> 本节是**服务器兜底端点**的接口说明（2026-09-30 起）。生图首选**你自己（agent）的模型**
> ——飞书 agent 支持文生图与图生图——生成后把图作为 `cover_base64` 随发布请求带上；
> 只有你的环境**没有任何生图能力**时才走本节的 MiniMax 端点。无论哪条路，
> 配色（固定的一套中性色板）、比例（16:9）、无水印、下面两套提示词模板都照旧执行。

## 接口与比例（兜底端点）

- 端点：`POST /api/reports/cover`，body
  `{"title": "...", "ratio": "16:9", "with_title": false}`。
  也可以在发布报告时直接带 `generate_cover: true`，服务器会顺手生成并落库。

### 配色：固定的一套中性色板

封面的配色是**全站唯一的一套中性色板**（冷白 / 米白背景、石墨黑文字、低饱和深石墨蓝主色 `#2B3A55`），
**不随业务或品牌变化**。旧的 `brand` / `cover_brand` / `palette` 参数已不再影响配色 ——
报告内的图表与封面共用同一个调子（见 `klado-v2:format-chart-style:zh` §2.2）。
- **比例只用 `16:9`**（1280×720）。卡片封面框就是 16:9，别的比例会被 `object-fit: cover` 裁掉。
  这与上面「四种比例」规则的取舍一致：**交付物类型决定比例，这里交付物是卡片封面**。
- 模型固定 `image-01`（MiniMax）——这条只描述上面的兜底端点；你自己的模型不受此限。
  `prompt` 由服务端按下面的模板拼好，**上限 1500 字符**；
  标题过长会返回 400，而不是让上游报 2013。
- 生成一次约 30 秒。

## 两个变体

**默认（`with_title: false`）—— 图里不出现任何文字。** 卡片自己渲染标题，图里再画一遍就重复了。

```text
你是一位高级商业科技媒体的视觉总监。请根据下面的主题，生成一张高级、极简、可读的编辑视觉封面。

【输入】
主题 / 标题：{{完整标题}}
画幅比例：16:9

【核心创作任务】
先理解标题背后真正要表达的关系、变化、动作或结果。
然后把它转译成一个“可读但不直白”的抽象实体意象：
- 必须由一个清晰的核心动作构成，例如连接、汇聚、对齐、推进、拆解、穿透、折叠、平衡、转化、生长或跨越。
- 画面中的物体要有明确的隐喻关系，让观众即使不看标题，也能感到“某件事正在发生”。
- 只讲一个视觉故事，使用少量精确的物体，不堆砌概念。
- 不要把它画成图标、插画说明书或字面场景；要把它做成一个有材质、有结构、有动作的抽象小型装置。
- 可加入一位小比例、真实感的人物，负责与装置发生关键动作（调整、连接、推进、观察或完成最后一步），
  只作为尺度和行动感，不抢主题；若会显得俗套则省略。

【固定视觉语言】
- 整体气质：高级商业编辑视觉、现代瑞士平面设计、克制的科技杂志感。
- 背景：暖白、米白、略带细微纸张纹理。
- 配色仅限：冷白 / 米白背景 + 石墨黑 / 炭灰结构 + 低饱和深石墨蓝主色（#2B3A55），全图只用这一种主色。
- 材质：哑光金属、精密黑色模块、磨砂半透明玻璃、柔和自然阴影。
- **主色的用法（重要）**：画面中面积最大的那块材质（装置主体）必须用主色 #2B3A55；
  石墨黑与炭灰只能用于结构件、小部件、阴影和背景细节 —— **不要让深灰占据主体面积**，
  否则主体会失去应有的颜色重点。
- 除上面列出的色相外不要引入任何其它颜色，保持单一克制的配色。
- 构图必须安静、精确、有大量留白，像一件被摆进画廊的编辑雕塑。
- 禁止赛博朋克、霓虹、蓝紫渐变、机器人、AI 大脑、芯片、科幻城市、复杂仪表盘、满屏 UI、
  卡通、家居风、廉价 3D、夸张光效。

【排版】
- 画面中不得出现任何文字：没有标题、没有字母、没有数字、没有副标题、没有 Logo、没有水印、
  没有边框或装饰性小字。
- 主体居中偏右，左侧保留大面积留白，整体保持单一视觉中心。

画幅比例：16:9（与开头一致，不要改变比例）。
```

**杂志封面变体（`with_title: true`）—— 标题烧进图里。** 只在这张图要单独当 KV/海报用时才用；
把【排版】段换成：

```text
【排版】
- 标题必须逐字准确使用：{{完整标题}}
- 标题使用高级中文无衬线字体，深石墨色；可自动挑选标题中最关键的一个词或数字，
  用低饱和深石墨蓝主色强调。
- 横向比例：标题在左，抽象意象在右；标题约占 50%–55%，意象约占 40%–45%。
- 竖向比例：标题在上，抽象意象在下；仍然保持大面积留白和单一视觉中心。
- 根据标题长度自动断行，确保中文清晰、无错别字。
- 除标题外，不要生成任何可读文字、Logo、水印、副标题、边框或装饰性小字。
```

## 失败排查

错误信息里带 MiniMax 的 `base_resp.status_code`：`1008` 余额不足、`1026` 描述涉及敏感内容
（改写标题后重试）、`2013` 参数异常（多半是提示词超长）、`1002` 限流、`1004`/`2049` key 无效。
