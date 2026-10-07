---
document_id: klado-v2:format-chart-style:zh
title: format-chart-style
document_type: format
source: klado-workspace
version: 1.6
tags: [format-chart-style, chart, charts, visualization, palette, color, font, density, waffle, sparkline, internal-only, 图表, 配色, 图型, 字号, 折线, 瀑布, 点阵, 哑铃]
migration: klado-v2
runtime_contract: internal-tools-only
source_path: knowledge/format-chart-style.md
---

> **运行时契约**（全文见 `klado-v2:runtime-contract:zh`）：只调用文档明确给出的能力，
> 没有的能力直说"不可用"、不要编造；业务数字必须走批准的只读数据源，不得凭印象编造。
> 旧工具名（`search_knowledge` / `query_database` / `create_xlsx` …）是历史名，真实端点见该文档 §4。
> **报告与封面链路是真实可用的**（`POST /api/reports`、`/api/reports/cover`）—— 旧文档把它们列为退役，只是名字过时。

# 报告图表规范（配色 / 字体 / 选型 / 画法）

一句话：**图表不是装饰，是「一个论点 + 撑得住的证据」。** 这份规范管三件事 ——
**用哪套色阶、用什么字体、什么数据形状配什么图**。

配套文档：`klado-v2:format-report-html:zh`（报告结构 / **§1.7 密度与可读性** / §1.8 页面配方 / §1.9 自检）。
**写图表以前，先读 `format-report-html` §1.7** —— 字号地板与留白口径在那边，本篇只管图表本身。

---

## 1. 四条不可协商

1. **一个图一个结论。** 图的存在是为了让人相信标题那句话。说不出这句话的图，删掉。
2. **值 ∝ 视觉长度。** 所有坐标要真算出来，不许"看着差不多"。柱状图**永不断轴**（长度必须从 0 起算）；
   用面积编码时数值要开方（`√v`），否则 4 倍数值会被画成 16 倍面积。
3. **每张图必须写「数据口径」。** 三件套：**指标 / 期间 / 范围**，另外凡占比必须写**分母**。
   `1 点 = 1%` 这类"编码口径"也要写。**只有编码口径、没有数据口径的图不合格。**
4. **集中度类图表必须写出 SKU 型号。** 写 `CR1`、`CR2–CR5` 是不够的 ——
   必须给出具体型号（CR1 → `SKU-0001`；CR2–CR5 → 四款型号全列）。

---

## 2. 色阶

### 2.1 中性色与语义色（**全站固定**）

| 用途 | 色值 |
| --- | --- |
| 主文本 / 主数据（当不用主色时） | `#1A1A1A` |
| 次级文本 / 轴标签 | `#4D4D4D` |
| 中性分割线 / 网格 | `#CCCCCC` |
| 中性背景 | `#F5F5F5` |
| 白色底面 | `#FFFFFF` |
| **风险红（下坡 / 告警）** | `#AE0505` |
| **正向绿（改善 / 推荐）** | `#006B31` |
| **信息蓝（信息 / 引用）** | `#0A439C` |

**语义色不随主题变** —— 趋势好坏是事实，不该因为报告讲的是哪块业务就换颜色。

### 2.2 一套中性色板（全站唯一）

图表只用**一套**中性色板 —— 主数据一档、对比系列一档、其余一档，配浅底面板与中性网格。
**不再按品牌切换色板**：一份报告不需要靠颜色说明它属于谁。

| 档位 | 色值 | 用途 |
| --- | --- | --- |
| main（主数据） | `#2B3A55` | 最重要的一档；面积最大的那块 |
| alt（对比系列） | `#5A6E8C` | 第二档（去年同期 / 对照对象） |
| faint（第三档） | `#B2B2B2` | 其余系列 / 背景量 |
| 面板底 | `#F2F4F7` | 图表面板浅底 |
| 正文墨 / 次级 | `#1A1A1A` / `#4D4D4D` | 标题 / 轴标签 |
| 网格 | `#E5E5E5` | 中性分割线 |
| 语义红 | `#AE0505` | 下坡 / 告警（与 §2.1 一致） |

- 这套色板与部署侧封面用的中性色板一致（冷白底 + 石墨黑文字 + 低饱和深石墨蓝主色），
  所以报告里的图与卡片封面是同一个调子。

### 2.3 用色规则

- **明度即数据**：同一张图里，最重要的一档用 `main`，次重要用 `alt`，其余用 `faint`。**不要靠色相堆区分度。**
- **每页只在一处承担主角。** 一页里主色可以同时出现在"洞察区竖条 + 主数据系列"（同一语义：这是本页重点），
  但**不得再引入第二种强调色**。
- ⚠️ **主色与风险红要分工**：中性深蓝 `#2B3A55` 与语义风险红 `#AE0505` 若同页大面积并用，警示会失去强度。
  做法：**洞察区标题用墨色 + 主色左竖条**，红只留给真正的告警（占画布 <1%，例如只出现在"下坡标注"一处）。
- 语义色（风险红 / 正向绿 / 信息蓝）**跨报告固定**，不随不同业务或主题变化。
- ⚠️ **中性灰档位别太浅**：`#CCCCCC` 在浅底上太浅、点阵数不出来。第三档用 `#B2B2B2` 承担；
  若要纯灰阶多档，往配色板补 `#808080` 与 `#B3B3B3`，**不要用彩色硬凑区分度**。

---

## 3. 字体（**已打包，不要外链**）

### 3.1 两个字族（运行时已内置，直接用，不要引 CDN）

| 字族 | 用途 | 文件 | 许可 |
| --- | --- | --- | --- |
| **`Roboto Condensed`**（可变 100–900） | 全部拉丁 / 数字 | `vendor/fonts/roboto-condensed-latin.woff2` + `-latin-ext` | Apache-2.0 |
| **`CoolSans SC Narrow`**（400 / 700） | 全部中文 | `vendor/fonts/coolsans-sc-narrow-{400,700}.woff2` | SIL OFL 1.1 |

单元格里的字体栈：`"Roboto Condensed","CoolSans SC Narrow",-apple-system,"PingFang SC","Microsoft YaHei",sans-serif`。

`CoolSans SC Narrow` = **Noto Sans SC 的全部轮廓与字宽横向缩放到 88%**。
88% 不是随手取的：它是「**Roboto Condensed ÷ Roboto 的平均字宽比**」，也就是让中文与
Roboto Condensed 在**窄度上对齐**的比例。不做这件事时中文会明显比拉丁宽，中西混排很跳。

### 3.2 三条硬规则

1. **不要外链字体**（`fonts.googleapis.com` 之类）—— 报告要自包含，导出与内网都取不到。
2. **改完字体要"量宽度"，不要只看截图**：字体没加载时页面**不报错**，只是静默回落系统字体，
   看起来"只是有点宽"。自检口径：
   - 汉字：10 个汉字 @ `100px` → **880px**（= 10 × 0.88 × 100）
   - 拉丁：量一行 40px/700 的英文短句的 px 宽度；字体没生效时会明显变宽，两次量值不一致就说明字体没加载。
3. ⚠️ **别用 `font-variant-numeric: tabular-nums` 做列对齐** —— 当前子集里它是**空操作**
   （等宽与比例数字实测同宽）。列对齐请用 `text-align:right`。

### 3.3 字号

沿用 `format-report-html` §1.7.1：**正文/表格 ≥18px，图表标签与脚注 ≥16px，标签类 ≥14px**（均为 1920 画布 px）。
⚠️ deck 是固定 1920×1080 画布按窗口等比缩放（常见 ≈×0.7），所以画布 18px 到屏幕上只有 12–13px。

---

## 4. 图表所在页面的页面结构

**布局的 owner 是 `klado-v2:format-report-html:zh` §1.10**（标题区 / 洞察 / 内容 / 页脚，含像素值）。
本节只补图表相关的两点，不重复那份规范：

```
标题区：       页面顶部只有主标题 + 副标题（不要页眉：无章节标签、无页眉页码、无页眉细线）
洞察区：       本页核心结论（一句话，墨色标题 + 左侧 6px 强调色竖条）
内容区：       ≥2 个图表面板 / 4 个独立区域
页脚区（40px）：来源 xxx · 时间范围 xxx · 过滤条件 xxx
```

- **页码完全交给 deck 运行时**（每页底部的分页线 + 页码条），报告里不要自己画页码。
- **页脚承载口径的"公共部分"**（来源/期间/过滤），图表面板内只写该图**特有的口径**（指标/分母）。
- 图表**面板底色用浅中性底**（见 §2.2），配 1px 中性细线；**不用灰底**、不用重边框、不用阴影。

---

## 5. 选型表：什么数据形状，配什么图

| 你要表达什么 | 用哪个 | 一页几行 | 关键点 |
| --- | --- | --- | --- |
| 跨期 / 跨对象比大小 | **A 横向对比条** | 6–12 | 标两期数值与变化率 |
| 数字 + 占比同时看 | **B 表格内嵌条** | 8–14 | 表尾给合计行 |
| 时间序列的**形态**（涨跌节奏） | **C 发丝折线** | 9–14 点 | 只标 1 处关键点，别逐点标字 |
| 占比 / 集中度（要"数得出来"） | **D 点阵 waffle（1 点 = 1%）** | 100 点 | 必须写 SKU 型号与分母 |
| 两个时点同一批对象的变化 | **E 哑铃** | 6–12 | 左旧右新，变化率写在右侧 |
| 构成（份额堆叠） | **F 100% 堆叠条** | 4–8 段 | 只放 4–8 段，多的并成"其余" |
| 增减分解（谁贡献了多少） | **G 瀑布** | 3–6 步 | 首尾是总数，中间是正负项 |
| ≤4 个数 | 只要 KPI 卡 | — | 不要为 3 个数开一张表 |

**选择顺序**：先看数据形状（时间 / 类别 / 占比 / 分解），再看读者要"读结论"还是"核对数字"。
**永远不要因为"这个图型好看"而选它。**

---

## 6. 画法（全部自包含，可直接复制）

### 6.1 先在这页声明主题 token

把下面这段放在 `<section class="slide" style="…">` 或图表面板容器上（全站一套，改色只改这一处）：

```html
style="--mk-ink:#1A1A1A;--mk-mid:#4D4D4D;--mk-grid:#E5E5E5;--mk-panel:#F2F4F7;
       --mk-main:#2B3A55;--mk-alt:#5A6E8C;--mk-faint:#B2B2B2;--mk-sem:#AE0505"
```

### 6.2 运行时**已内置**的两个组件（直接用，别自己写）

```html
<!-- A 横向对比条 -->
<div class="s-bars">
  <div class="s-bar"><span class="k">Mar</span>
    <span class="t"><i style="width:100%"></i><i class="now" style="width:57%"></i></span>
    <span class="v"><b>99</b> → <b>56</b> <b class="dn">-43%</b></span></div>
</div>

<!-- B 表格内嵌条：数字、YoY、占比同一行 -->
<tr><td>Mar</td><td class="n">99</td><td class="n">56</td><td class="n s-dn">-43%</td>
    <td style="width:34%"><span class="s-meter" style="--v:57%"></span></td></tr>
```

条形高度由组件保证（14–18px 画布 px），**不要手搓 `<div style="height:10px">`**。

### 6.3 C 发丝折线（纯 SVG —— 坐标必须自己算）

```html
<!-- 规律：y = y0 - (v / vmax) * (y1 - y0)   —— 两条线必须同一个 vmax -->
<svg width="1124" height="232" viewBox="0 0 1124 232">
  <!-- 网格 0 / mid / max -->
  <line x1="104" y1="190" x2="1016" y2="190" stroke="var(--mk-grid)" stroke-width="1"/>
  <text x="90" y="198" text-anchor="end" style="font-size:16px;font-weight:600;fill:var(--mk-mid)">¥0M</text>
  <!-- 去年（alt，浅）/ 今年（main，深） -->
  <polyline points="104,86 218,148 332,31 446,205 560,148 674,60 788,190 902,123 1016,66"
            fill="none" stroke="var(--mk-alt)" stroke-width="2"/>
  <polyline points="104,193 218,176 332,113 446,218 560,195 674,148 788,203 902,201 1016,131"
            fill="none" stroke="var(--mk-main)" stroke-width="2.5"/>
  <!-- 关键点只标一处，标注用语义色 -->
  <text x="332" y="18" text-anchor="middle" style="font-size:16px;font-weight:800;fill:var(--mk-mid)">¥99M</text>
  <text x="332" y="169" text-anchor="middle" style="font-size:16px;font-weight:800;fill:var(--mk-sem)">¥56M ↓</text>
</svg>
```

**只标 1 处关键点。** 逐点写数字就会失控 —— 数字交给 A/B 两个表格型组件。

### 6.4 D 点阵 waffle（1 点 = 1%）

```html
<!-- 100 个圆点 = 100%；顺序：先主色、再 alt、再 faint；必须写型号与分母 -->
<div style="display:flex;align-items:center;gap:26px">
  <svg width="236" height="236" viewBox="0 0 236 236"><!-- 10×10，cell=23.6, r≈7.1 --></svg>
  <div style="font-size:19px;line-height:1.7">
    <div><i style="background:var(--mk-main);display:inline-block;width:14px;height:14px;border-radius:50%"></i>
      CR1 · <b>SKU-0001</b><em style="color:var(--mk-mid)">18%</em></div>
    <div><i style="background:var(--mk-alt);…"></i>CR2–CR5 · 4 款<em style="color:var(--mk-mid)">28%</em>
      <span style="display:block;font-size:18px;color:var(--mk-mid)">
        SKU-0002 · SKU-0003 · SKU-0004 · SKU-0005</span></div>
    <div><i style="background:var(--mk-faint);…"></i>其余 SKU<em style="color:var(--mk-mid)">54%</em></div>
  </div>
</div>
```

**必须配的口径**：`1 点 = 1% · 共 100 点 = <分母> 100% · 口径：<指标> · <期间> · <范围>`。

### 6.5 E 哑铃（两个时点）

```html
<div style="display:grid;grid-template-columns:150px 1fr;gap:14px;align-items:center;font-size:20px">
  <span>线下门店</span>
  <span style="position:relative;display:block;height:20px">
    <i style="position:absolute;left:0;   top:9px;width:96.1%;height:2px;background:var(--mk-grid)"></i>
    <i style="position:absolute;left:88%; top:5px;width:10px;height:10px;border-radius:50%;background:var(--mk-alt)"></i>
    <i style="position:absolute;left:96.1%;top:3px;width:14px;height:14px;border-radius:50%;background:var(--mk-main)"></i>
  </span>
</div>
```

左点 = 旧时点（`alt`），右点 = 新时点（`main`）；变化率写在右侧数值列。

### 6.6 F 100% 堆叠条 / G 瀑布

```html
<!-- F：段数 4–8，多的并成"其余"；每段必须能读出数值 -->
<div style="display:flex;height:22px;border-radius:0 99px 99px 0;overflow:hidden">
  <i style="width:96.1%;background:var(--mk-main)"></i>
  <i style="width:3.9%; background:var(--mk-alt)"></i>
</div>

<!-- G：首尾是总数，中间是正负项；正负用语义色，不用主色 -->
<div class="s-bars">
  <div class="s-bar"><span class="k">量</span>
    <span class="t"><i class="now" style="width:87%"></i></span>
    <span class="v">-235.4 <b class="dn">87%</b></span></div>
  <div class="s-bar"><span class="k">价 / 结构</span>
    <span class="t"><i class="now" style="width:13%"></i></span>
    <span class="v">-35.9 <b class="dn">13%</b></span></div>
</div>
```

### 6.7 交付前自查"值 ∝ 视觉"

1. 从渲染结果里把坐标**反推回数值**（`implied = (y0 - y) / px_per_unit`），逐个比对原始数据；
2. 刻度线性：`|y(max) − y(0)|` 必须等于 `2 × |y(mid) − y(0)|`；
3. waffle 必须是 1 点 = 1% 且点数 = 100（或明写单位）。

实测基准：18 个数据点反推误差 **0.000**、`y(100)−y(0)` 与 `2×(y(50)−y(0))` 完全相等。
**这一步花 1 分钟，能挡掉最常见的"图形在撒谎"。**

---

## 7. 图表自检（发布前逐条勾）

1. 每张图能一句话说出它的结论？标题写的是**结论**而不是栏目名？
2. 每张图都写了**数据口径**（指标 / 期间 / 范围 / 分母）？
3. 集中度图写了 **SKU 型号**？
4. 值 ∝ 视觉（§6.7 三步）？柱状图没断轴？面积用了 `√v`？
5. 配色用了**一套**中性色板 + 中性灰，语义色跨页固定，每页强调色只有一处？
6. 字号：图表标签 ≥16px、正文/表格 ≥18px（画布 px）？
7. 标签无重叠、无裁切；长标签**不要**塞进固定行高（用 `min-height`，否则折行会溢出）？
8. 图表面板用主题浅底 + 1px 中性细线，没有阴影 / 重边框 / 发光 / 3D？

---

## 8. 常见错误

| 错误 | 为什么不行 | 正确做法 |
| --- | --- | --- |
| 手搓 `<div style="height:10px">` 当柱形 | 画布缩放后看不见，且没有轴 | 用 §6.2 的组件或按 §6.3 算坐标 |
| 只有"1 点 = 1%"却没有数据口径 | 读者不知道这是哪条渠道、哪段期间、什么指标 | 编码口径 + 数据口径都要写 |
| CR1 / CR2–CR5 不写型号 | 读者无法核对，也无法行动 | 写出 VIB 型号 |
| 断轴（柱状图不从 0 起） | 长度与数值脱钩，等于撒谎 | 永远从 0 起；差异太小就换图型 |
| 用色相区分 5 个以上系列 | 色觉异常读者看不懂，观感也乱 | 明度阶梯 + 最多 2 个色相 |
| 每一页都用强调色当标题色 | 强调色与语义色抢，警示失效 | 标题墨色 + 强调色竖条 |
| 为 3 个数开一张表 | 浪费一整页 | 用 KPI 卡 |
| 引 Google Fonts / CDN 图表库 | 导出与内网取不到，报告不自包含 | 用内置字体与自包含 SVG |

---

## 9. 出处与许可

- **方法论**（"先判数据形状再选图型""明度即数据""不断轴""面积开方""确定性伪随机""单位分解 1 点 = 1%"）
  参考了公开项目 **lieflat-charts**（`github.com/larashero3-dotcom/lieflat-charts`）。
  ⚠️ 该项目采用 **PolyForm Noncommercial 1.0.0**，**禁止商业使用** —— 因此
  **本规范的文字、配色、字体、代码全部为自行实现，未复制该项目的任何文件**。
  若将来要直接使用它的模板或代码，必须先取得作者的商业许可。
- **配色**：中性色板由本规范自行定义（低饱和深石墨蓝 `#2B3A55` + 一档中性灰阶 + 固定的语义色），
  不取自任何品牌官网；这套色板与部署侧封面共用同一个调子。
- **字体**：`Roboto Condensed`（Apache-2.0）取自本应用自身的 `index.html`；
  `CoolSans SC Narrow` 由 `Noto Sans SC`（SIL OFL 1.1）派生，已按 OFL 的保留字体名规定改名，
  许可原文随文件放在 `vendor/fonts/`。
- ⚠️ 改本文档后需要**部署**才会被知识库摄取（摄取只发生在容器启动时）。
