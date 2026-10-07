/* ════════════════════════════════════════════════════════════════════════════
   i18n.js — Chinese/English switching.

   Klado's copy convention IS the translation memory: a user-facing string is
   written as `中文 / English` — one slash with a space on each side, Chinese on
   the left, English on the right. This runtime walks the DOM and keeps only the
   side matching the selected language, so existing copy needs no edits and new
   copy just follows the convention.

     1. what the reader picked last time (localStorage `klado-lang`),
     2. the browser language (`navigator.language`, zh* → zh, else en).

   A MutationObserver processes nodes the app adds later (status lines, dialogs,
   rendered lists), so dynamic copy works with no call-site changes. Containers
   holding USER content — rendered reports, knowledge pages, document previews —
   are marked `data-i18n-skip` and never touched.

   Strings that exist in only one language are looked up in DICT (exact match of
   the full trimmed string). Missing entries fail soft: the string shows as
   written. `kladoI18n.t(s)` applies the same rule to a string before insertion.

   ⚠️ Keep the split rule identical to `api/core/i18n.py` — the backend picks the
   side of the very same strings for its messages.
   ════════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var KEY = 'klado-lang', SUPPORTED = ['zh', 'en'];

  /* Single-language strings, keyed by their exact trimmed text.
     `zh` maps an English-only string to Chinese; `en` maps a Chinese-only
     string to English. Entries fail soft — an unknown string shows as written.
     Keys are matched AFTER the pair rule, so a key containing ` / ` is only
     consulted when the pair regex (CJK on the left of the first ` / `) did not
     match. Mixed-script keys work in both directions (`Replace（覆盖）` lives in
     both maps). */
  var DICT = {
    zh: {
      // ── shell / nav ──
      'Data Center': '数据中心',
      'Inbox': '收件箱',
      'Workspace': '工作台',
      // ⚠️ The nav tabs are STATIC MARKUP, not the registry: measured in the browser,
      // every `#nav-center [data-nav-key]` has no `data-lang-*` span, so `renderNav()`
      // never rebuilt them and they are plain English translated by this table. That is
      // why the other six read correctly in Chinese and this one did not — a missing
      // entry here is a visible untranslated tab, not a silent no-op.
      'Dashboard': '仪表盘',
      'Knowledge base': '知识库',
      'Calendar': '日历',
      'Settings': '设置',
      'System Settings': '系统设置',
      'Back to Home': '回到主页',
      'Home': '主页',
      'Online': '在线',
      'Sign in required': '需要登录',
      'API connected': 'API 已连接',
      'API building…': 'API 启动中…',
      // ── generic verbs / buttons ──
      'Open': '打开', 'Edit': '编辑', 'Delete': '删除', 'Rename': '重命名',
      'Export': '导出', 'Cover': '封面', 'Create': '创建', 'Save': '保存',
      'Cancel': '取消', 'OK': '确定', 'Close': '关闭', 'Back': '返回',
      'Refresh': '刷新', 'Details': '详情', 'Search': '搜索', 'Pick': '选择',
      'Upload': '上传', 'Preview': '预览', 'Reset': '重置', 'Loading…': '加载中…',
      'Uploading…': '上传中…', 'Working…': '处理中…', 'Today': '今天',
      'Publish': '发布', 'Configure': '配置',
      // ── auth card ──
      'Sign in to continue': '登录后继续',
      'Sign in': '登录',
      'Register': '注册',
      'Sign out': '退出登录',
      'Email': '邮箱',
      'Password': '密码',
      'Work email': '工作邮箱',
      'Send verification code': '发送验证码',
      'Verification code': '验证码',
      '(at least 8 characters)': '（至少 8 个字符）',
      'Display name': '显示名',
      '(optional)': '（可选）',
      'Create account': '创建账号',
      '← Use a different email': '← 换一个邮箱',
      // ── workspace (reports) ──
      'My workspace': '我的工作台',
      'Public area': '公开区',
      'Shared with me': '共享给我的',
      'Search title / tag / summary…': '搜索标题 / 标签 / 摘要…',
      'All statuses': '全部状态',
      'Published': '已发布',
      'Drafts': '草稿',
      'Draft': '草稿',
      'Archived': '已归档',
      'All categories': '全部分类',
      'Status': '状态',
      'Category': '分类',
      'Refresh Workspace': '刷新工作台',
      'Workspace areas': '工作台分区',
      'Add report': '添加报告',
      'Add a report to my workspace': '把一份报告加进我的工作台',
      'Pull to edit': '拉取为可编辑副本',
      'No cover': '无封面',
      'Report type': '报告类型',
      'Interactive': '交互式',
      'Static': '静态',
      'Ad-hoc': '临时',
      'Report HTML *': '报告 HTML *',
      'Slug (optional)': 'Slug（可选）',
      'Title': '标题',
      'Title *': '标题 *',
      'Summary': '摘要',
      'Rename report': '重命名报告',
      'Add privately': '私密添加',
      'Share with colleagues': '共享给同事',
      'Share link with everyone': '共享给所有人',
      'Open in new tab': '在新标签页打开',
      'Download HTML': '下载 HTML',
      'Download markdown': '下载 markdown',
      'Copy link': '复制链接',
      'Grant access': '授予访问',
      'Disable link': '停用链接',
      'Anyone link': '任何人链接',
      'Publish Link': '发布链接',
      'More actions': '更多操作',
      'Only the addresses listed below can open this report. Each colleague must register and sign in first. Sharing is read-only; they can pull an independent copy to edit.':
        '只有下面列出的地址能打开这份报告。同事需要先注册并登录。共享是只读的；对方可以拉取一份独立副本去编辑。',
      'Only the addresses listed below can read this page. Each colleague must register and sign in first. Sharing is read-only; they can pull an independent copy to edit.':
        '只有下面列出的地址能读这一页。同事需要先注册并登录。共享是只读的；对方可以拉取一份独立副本去编辑。',
      'Anyone with this link can view the report without registering or signing in. The link is read-only and does not publish the report to the Public area.':
        '任何人拿这个链接都能看这份报告，无需注册登录。链接是只读的，也不会把报告发布到公开区。',
      "Provided by the report's author": '由报告作者提供',
      'Filters': '筛选',
      'Search colleagues by name or email · separate addresses with commas': '按姓名或邮箱搜索同事 · 多个地址用逗号分隔',
      'Colleague link': '同事链接',
      'Pull a copy': '拉取一份副本',
      'Public snapshot': '公开快照',
      'Public': '公开',
      'Disabled · not retrieved': '已停用 · 不参与检索',
      'editing': '编辑中',
      'not saved yet': '还没保存',
      // ── knowledge base ──
      'No page open': '未打开页面',
      'Choose a page on the left, or create one.': '在左侧选一页，或新建一页。',
      'Pages are markdown — the same pages your agent can read and edit.': '页面是 markdown —— 你的 agent 可读可编辑。',
      'No pages yet. Use ': '还没有页面。点 ',
      ' to create one.': ' 即可创建。',
      'New page': '新建页面',
      'Search pages…': '搜索页面…',
      'Knowledge areas': '知识库分区',
      'My knowledge': '我的知识',
      'Refresh knowledge base': '刷新知识库',
      'Page actions': '页面操作',
      'Business knowledge base — markdown wiki, readable and editable by your agent': '业务知识库 — markdown wiki，你的 agent 可读可编辑',
      'Editing — the source stays markdown, you see the formatting': '编辑中 — 源是 markdown，你看到的是排版',
      'New page — markdown, and the symbols stay out of your way': '新页面 — markdown，符号不会挡你的路',
      'A title is required': '标题必填',
      'The page body is empty': '页面正文是空的',
      'This page is bilingual — fill in both Summary — EN and Summary — 中文': '这一页是双语的 — Summary — EN 和 Summary — 中文 都要填',
      'Saving…': '保存中…',
      'Saved': '已保存',
      'Tags (comma separated)': '标签（逗号分隔）',
      'Author': '作者',
      // ── inbox ──
      'Mark all read': '全部标为已读',
      'For me': '发给我的',
      'Public activity': '公开动态',
      'All': '全部',
      'Inbox filters': '收件箱筛选',
      'Inbox messages': '收件箱消息',
      'Refresh the inbox': '刷新收件箱',
      'Select a message to preview the document here.': '选一条消息，在这里预览文档。',
      'Everything stays read-only; use “Open page” to work on the document itself.': '这里始终只读；用「打开页面」去处理文档本身。',
      'This dataset opens in the Data Center — use “Open page”.': '这个数据集要在数据中心打开 — 用「打开页面」。',
      'Open page': '打开页面',
      'Could not load the inbox.': '收件箱加载失败。',
      'Nothing in this filter.': '这个筛选下没有消息。',
      'Document preview': '文档预览',
      'Event page': '活动页面',
      // ── calendar ──
      'Back to this month': '回到本月',
      'Refresh calendar': '刷新日历',
      'Calendar areas': '日历分区',
      'My calendar': '我的日历',
      // ── the view switcher (calendar) ──
      // ⚠️ These are STATIC markup, exactly like 'My calendar' above, so they are DICT
      // entries and not `中文 / English` pairs: a pair in the button would be re-split by the
      // DOM walker on every mutation, and the aria-label carries the long form below.
      'Timeline': '时间轴',
      'Month': '月历',
      'Calendar view': '日历视图',
      // ⚠️ The month grid's own heading and arrows are built by the renderer, so they go
      // through `kladoI18n.t()` as pairs and are NOT listed here — a DICT key can only match a
      // string that is already single-language.
      'Event details': '活动详情',
      'Event actions': '活动操作',
      "Edit this event's own fields — dates, deadline, partners, attachments": '编辑活动自身字段 — 日期、截止、参与人、附件',
      'Export this page': '导出这一页',
      'Cover (same generator as a Workspace report)': '封面（与工作台报告同一个生成器）',
      'Event types': '活动类型',
      'Search…': '搜索…',
      // ── data center ──
      'Files': '文件',
      'Datasets': '数据集',
      'Search datasets…': '搜索数据集…',
      'Upload a file to create your first dataset.': '上传一个文件，创建你的第一个数据集。',
      'Drop files to upload here': '把文件拖到这里上传',
      'Select a file to preview': '选一个文件预览',
      'Select destination folder': '选择目标文件夹',
      'Select File': '选择文件',
      'Select Sheet': '选择工作表',
      'Excel Sheets': 'Excel 工作表',
      'Smart Import': '智能导入',
      'New Dataset (Clean & Import)': '新建数据集（清洗并导入）',
      'Quick Import (Overwrite)': '快速导入（覆盖）',
      'New Subfolder': '新建子文件夹',
      'New Folder': '新建文件夹',
      'Folder name': '文件夹名',
      'New file name': '新文件名',
      'Delete Folder': '删除文件夹',
      'Rename File': '重命名文件',
      'Move File': '移动文件',
      'Move Here': '移动到此处',
      'Move to…': '移动到…',
      'Moving:': '正在移动：',
      'Upload files to current folder': '上传文件到当前文件夹',
      'New folder in current location': '在当前位置新建文件夹',
      'Refresh view': '刷新视图',
      'Import Mode': '导入模式',
      'Import Matched': '导入已匹配项',
      'Update Dataset': '更新数据集',
      'Execute Update': '执行更新',
      'Detect automatically': '自动检测',
      '— or load a file': '— 或加载一个文件',
      'No datasets yet': '还没有数据集',
      'Total Rows': '总行数',
      'Periods': '期数',
      'Range': '范围',
      'No period column found.': '没找到周期列。',
      'Name': '名称',
      'Size': '大小',
      'Modified': '修改时间',
      'Sheet': '工作表',
      'The data layer': '数据层',
      'Clear All': '全部清除',
      // ── settings / admin / agent codes ──
      'Admin': '管理员',
      'Accounts, access and mail delivery.': '账号、访问与邮件发送。',
      'Users & access': '用户与访问',
      'Search email / name…': '搜索邮箱 / 姓名…',
      'No accounts yet.': '还没有账号。',
      'Mail delivery': '邮件发送',
      'SMTP server': 'SMTP 服务器',
      'Port': '端口',
      'Encryption': '加密方式',
      'Sign-in account': '登录账号',
      'Sender address': '发件地址',
      'Send test': '发送测试',
      'Save access': '保存访问',
      'Current password': '当前密码',
      'Repeat new password': '再输一遍新密码',
      'Update password': '更新密码',
      'invited': '已邀请',
      // ── mixed-script keys (both maps) ──
      'SSL（465）': 'SSL (465)',
      'STARTTLS（587）': 'STARTTLS (587)',
      'Replace（覆盖）': '覆盖',
      'Append（追加）': '追加',
    },
    en: {
      // ── viewer bar ──
      '全部页面': 'All pages',
      '存为静态报告': 'Save as static report',
      // ⚠️ The mirror of `'Dashboard': '仪表盘'` in DICT.zh. Both sides are needed: the
      // tab is static English markup, so zh mode needs the English key, while any
      // Chinese literal '仪表盘' in the app needs the reverse for an English reader.
      '仪表盘': 'Dashboard',
      // ── generic ──
      '取消': 'Cancel', '确定': 'OK', '关闭': 'Close', '全选': 'Select all',
      '清空': 'Clear', '配置': 'Configure', '点击': 'Click', '加载中…': 'Loading…',
      // ── auth / settings labels ──
      '新密码（至少 8 位）': 'New password (at least 8 characters)',
      '当前密码必填 —— 会话 Cookie 证明的是"这个会话"，不是"坐在键盘前的人"。':
        'The current password is required — the session cookie proves "this session", not "the person at the keyboard".',
      'Summary — 中文': 'Summary — Chinese',
      '用途 / 设备名（便于以后辨认）': 'Purpose / device name (easier to recognize later)',
      '发送测试邮件到（留空 = 你自己的地址）': 'Send test email to (blank = your own address)',
      '明文（不建议）': 'Plain (not recommended)',
      // ── data-center import wizard ──
      '数据清洗': 'Clean',
      '预览 & 导入': 'Preview & Import',
      '基础配置': 'Basic setup',
      '跳过前 N 行': 'Skip first N rows',
      '原始数据预览（前 50 行）': 'Raw data preview (first 50 rows)',
      '删除全空行': 'Drop fully empty rows',
      '去重': 'Deduplicate',
      '行筛选': 'Row filter',
      '值替换': 'Value replacement',
      '行筛选条件': 'Row filter conditions',
      '添加条件': 'Add condition',
      '尚无筛选条件': 'No filter conditions yet',
      '值替换规则': 'Value replacement rules',
      '添加规则': 'Add rule',
      '尚无替换规则': 'No replacement rules yet',
      '从文件名生成列': 'Derive columns from file name',
      '添加列': 'Add column',
      '尚无派生列 · 例：列名「数据月份」+ 正则 (\\d{6}) → 从 客户库存_202608.xlsx 抓出 202608 填入每行':
        'No derived columns yet · e.g. column "数据月份" + regex (\\d{6}) → pulls 202608 out of 客户库存_202608.xlsx into every row',
      '筛选值 · 点击': 'Filter values · click',
      '删除列 · 双击列名重命名 · 列头可改类型': 'Delete column · double-click a name to rename · change the type in the header',
      '预览清洗结果 & 导入': 'Preview cleaned & import',
      '确认数据后填写目标信息，点击导入': 'Check the data, fill in the target info, then click import',
      '原始行数': 'Raw rows',
      '清洗后行数': 'Cleaned rows',
      '保留列数': 'Columns kept',
      '点击"预览"后显示': 'Shows after you click "Preview"',
      '目标表名 *': 'Target table *',
      '目标数据库': 'Target database',
      '写入模式': 'Write mode',
      '← 上一步': '← Back',
      '预览清洗结果': 'Preview cleaned',
      '下一步 →': 'Next →',
      '导入数据集': 'Import dataset',
      // ── mixed-script keys (both maps) ──
      'SSL（465）': 'SSL (465)',
      'STARTTLS（587）': 'STARTTLS (587)',
      'Replace（覆盖）': 'Replace',
      'Append（追加）': 'Append',
      '作废': 'Revoke',
      '吊销': 'Revoke',
      '新建': 'Create',
      '重发邮件': 'Resend email',
      '重新生成并发送': 'Regenerate & resend',
      '生成并发送': 'Generate & send',
      '已复制到剪贴板': 'Copied to clipboard',
      '任何人可通过此链接下载文件，无需登录。': 'Anyone with this link can download the file without signing in.',
      '已存密码无法解密': 'Stored passwords cannot be decrypted',
      '未配置': 'Not configured',
      '来源：无': 'Source: none',
      // ── placeholders / titles (attributes) ──
      '例如 465': 'e.g. 465',
      '例如 Feishu agent': 'e.g. Feishu agent',
      '例如 user@example.com': 'e.g. user@example.com',
      '例如 klado@klado.team': 'e.g. klado@klado.team',
      '例如 smtp.qiye.aliyun.com': 'e.g. smtp.qiye.aliyun.com',
      '留空 = 用上面的 Sign-in account': 'Leave blank = same as Sign-in account',
      '客户端专用密码': 'Client security password',
      '搜索…': 'Search…',
      '双语报告必填': 'Required for bilingual reports',
      '把当前内容（含已保存的标注）存为一份静态快照报告': 'Save the current content (with its saved notes) as a static snapshot report',
      // ── first-run language picker stays bilingual on purpose (no entry) ──
    }
  };

  /* ── the reverse of every English-first entry, derived ──────────────────────
     ⚠️ `DICT.zh` is written as `'Email': '邮箱'` — an English literal and the Chinese
     it reads as. That is HALF of what `side()` needs: with only that entry, a node
     saying `Email` becomes `邮箱` in Chinese and, because the node's cached original is
     `Email` and `DICT.en` has no `'邮箱'` key, it STAYS `邮箱` when the reader switches
     back. One-way dictionaries are the reason a page can look correct in the language it
     was opened in and wrong in the other.

     The reverse mapping is not extra information — it is the same pair read from the
     other end — so it is computed rather than maintained. A hand-written second list
     would be a second place to forget an entry, and the failure is silent: the string
     is simply never translated back.

     ⚠️ Only `DICT.zh` is mirrored, never the other way. An entry in `DICT.en` may be a
     Chinese-first phrase an author wanted to keep alongside an existing `DICT.zh` key
     (`'Summary — 中文': 'Summary — Chinese'`), and mirroring that would overwrite a
     deliberate mapping. Existing `DICT.en` entries always win.

     ⚠️ **And only entries that really are English-first are mirrored at all.** Twenty-one
     Chinese-first entries (`'作废': 'Revoke'`, `'搜索…': 'Search…'`, …) had been typed
     into the `zh` block, where the mirroring assumed the opposite direction and produced
     `DICT.en['Revoke'] = '作废'` — a Chinese string keyed by its English text, which is
     then handed to `side()` in English mode and rendered verbatim. The symptom was one
     search box in a hidden overlay reading `搜索…` on a page declared `lang="en"`, with
     `aria-label` on the same element correctly in English, because only the placeholder
     had a dictionary entry. It was in the wrong block, so it was wrong in BOTH
     directions, and the guard that reports "Chinese left in English mode" found it only
     because the other attribute happened to be right.

     The direction test is on the VALUE, not the key: a key may legitimately contain
     Chinese (`'This page is bilingual — fill in both Summary — EN and Summary — 中文'`
     names its own fields), so "key has CJK" would misfile a correct entry. A value with
     no CJK is unambiguous — it is English, in a block that is supposed to be Chinese.
     */
  (function mirrorEnglishFirstEntries() {
    var zh = DICT.zh || {};
    var en = DICT.en || (DICT.en = {});
    var CJK = /[一-鿿㐀-䶿]/;
    Object.keys(zh).forEach(function (english) {
      var chinese = zh[english];
      if (typeof chinese !== 'string' || !chinese) return;
      /* A value with no Chinese is not a Chinese rendering: the entry is filed the
         wrong way round, and reversing it would invent a backwards mapping. Skip it
         and let it be found by the invariant test rather than mirrored into the map it
         was never part of. */
      if (!CJK.test(chinese)) return;
      if (!Object.prototype.hasOwnProperty.call(en, chinese)) en[chinese] = english;
    });
  })();

  /* ⚠️ The invariant, checked at load, because a misfiled entry has no other symptom.
     `DICT.zh` answers "what does this source string read as, in Chinese", so a value
     with no Chinese in it is either a mistake or a string that does not belong in this
     block. Reported rather than thrown: a console error survives a bad deploy and names
     the entries, where an exception would take the whole page down over two strings
     that are legible without translation.

     The allowlist is the deliberate divergence, and each entry earns its place:
     `SSL（465）` and `STARTTLS（587）` are the *same label* written two ways. Chinese
     mail settings spell the port in fullwidth parentheses and English in halfwidth, and
     an operator matching a string against a config file needs the one from their own
     settings screen. Both spellings are in both maps on purpose — this is the one place
     the two languages are allowed to differ, so it is named rather than auto-corrected.
     */
  (function assertDictDirections() {
    var CJK = /[一-鿿㐀-䶿]/;
    var DELIBERATE = {
      'SSL（465）': 'mail settings spell the port in fullwidth parens in Chinese',
      'STARTTLS（587）': 'same, for the submission port'
    };
    var offenders = Object.keys(DICT.zh || {}).filter(function (k) {
      return !CJK.test(DICT.zh[k]) && !DELIBERATE[k];
    });
    if (offenders.length && window.console && console.error) {
      console.error('[klado i18n] DICT.zh entries whose value is not Chinese — they ' +
        'belong in DICT.en, and mirroring them inverts both directions:',
        offenders);
    }
  })();

  function stored() {
    try {
      var v = localStorage.getItem(KEY);
      return SUPPORTED.indexOf(v) >= 0 ? v : null;
    } catch (e) { return null; }
  }

  function browserLang() {
    try {
      var langs = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language];
      for (var i = 0; i < langs.length; i++) {
        if (/^zh/i.test(langs[i] || '')) return 'zh';
        if (/^en/i.test(langs[i] || '')) return 'en';
      }
      return 'en';
    } catch (e) { return 'en'; }
  }

  var lang = stored() || browserLang();

  /* zh on the left, ` / `, Latin on the right — identical to api/core/i18n.py. */
  var PAIR = /^(?=[\s\S]*[\u4e00-\u9fff])([\s\S]*?)\s+\/\s+([A-Za-z][\s\S]*)$/;
  var ATTRS = ['placeholder', 'title', 'aria-label'];
  var ORIG = new WeakMap();
  /* What i18n itself last wrote, per element / per text node. Without this
     there is no way to tell 'i18n translated this' from 'the app set this',
     and restoring a cached original has to pick one — and picking wrong
     overwrites live UI with markup that is no longer true. */
  var WROTE = new WeakMap();     // node/attr-cell → the string as first seen

  function isSkipped(el) {
    if (!el || !el.closest) return false;
    // `.doc-anno-layer` — colleague-note bubbles (vendor/annotations.js) are user
    // content and get inserted into live containers we cannot mark from here.
    return !!(el.closest('[data-i18n-skip], .doc-anno-layer'));
  }

  function side(text) {
    var t = text.trim();
    var m = PAIR.exec(t);
    if (m) {
      var picked = (lang === 'zh' ? m[1] : m[2]).trim();
      return picked || text;
    }
    var d = DICT[lang];
    return d && Object.prototype.hasOwnProperty.call(d, t) ? d[t] : null;
  }

  function processTextNode(node) {
    var parent = node.parentElement;
    if (!parent || parent.nodeName === 'SCRIPT' || parent.nodeName === 'STYLE') return;
    if (isSkipped(parent)) return;
    var current = node.nodeValue;
    if (!current || !current.trim()) return;
    var baseline = ORIG.get(node);
    /* ⚠️ `WROTE` is what makes a restore safe to do at all. `side()` answers null for
       "this language has no entry", which on the FIRST pass means "leave it alone" and
       on the way BACK means "put the original back" — and the two cannot share one
       fallback. So i18n records what it itself wrote, and treats anything else as the
       app's: the viewer's download button is `dlBtn.title = 'Download .pptx'`, set
       at click time, and restoring the markup's 'Download HTML' over it was a real
       regression caught by `verify_dynamic_report_ui.py`.

       So: a value i18n wrote is i18n's to translate. A value the app replaced is the
       app's — re-baseline onto it and translate THAT, so a later language switch still
       works on what is actually on screen. */
    /* ⚠️ `rebase` exists because clearing `WROTE` on "no change needed" was wrong,
       and it froze every STATIC pair in the app on the first language switch.

       The write at the bottom of this function is a `characterData` mutation, so
       the observer schedules this same node again on the next frame. On that
       second pass `replacement` is already what is on screen (same language), so
       the old `else` cleared `WROTE`.
       The third pass — the actual language switch — then saw
       `current !== baseline && current !== WROTE.get(node)`, concluded the value
       was the app's, and re-baselined onto the ALREADY-SPLIT string. The pair was
       gone by then, so `side()` had nothing to split and the label was stuck in
       whichever language happened to be active at load.

       Symptom: text that nobody re-renders stays in the boot language forever.
       The room's own strings were fine because they are rebuilt from fresh pairs
       on `klado:lang`; the landing page's breadcrumb is static markup and froze,
       and that is what `verify_office_ui.py` caught.

       So `WROTE` is only cleared when we really did re-baseline onto app text —
       a re-visit of our own output must leave the record alone. */
    var rebase = false;
    if (baseline === undefined) {
      baseline = current;
      ORIG.set(node, baseline);
      rebase = true;
    } else if (current !== baseline && current !== WROTE.get(node)) {
      baseline = current;
      ORIG.set(node, baseline);
      rebase = true;
    }
    var replacement = side(baseline) || baseline;
    if (replacement !== current) {
      node.nodeValue = replacement;
      WROTE.set(node, replacement);
    } else if (rebase) {
      WROTE.set(node, undefined);
    }
  }

  function processElement(el) {
    if (el.nodeName === 'SCRIPT' || el.nodeName === 'STYLE') return;
    if (isSkipped(el)) return;
    ATTRS.forEach(function (attr) {
      if (!el.hasAttribute(attr)) return;
      var current = el.getAttribute(attr);
      if (!current || !current.trim()) return;
      var orig = ORIG.get(el);
      var map = orig && typeof orig === 'object' ? orig : {};
      if (orig !== map) { ORIG.set(el, map); }
      var wrote = WROTE.get(el);
      if (!wrote || typeof wrote !== 'object') { wrote = {}; WROTE.set(el, wrote); }
      /* Same ownership rule as in `processTextNode`, and for the same reason.
         The document viewer sets `#rpt-download`'s title to the card's real format at
         click time — `dlBtn.title = 'Download .pptx'`. Restoring the markup's
         'Download HTML' over that made the button name the wrong thing for every
         document card, and in Chinese mode it read 「下载 HTML」: the format name was
         gone in BOTH languages, because the only string left was the markup's. */
      var baseline = Object.prototype.hasOwnProperty.call(map, attr) ? map[attr] : current;
      if (current !== baseline && current !== wrote[attr]) {
        baseline = current;                 /* the app set it at runtime — re-baseline */
      }
      map[attr] = baseline;
      var replacement = side(baseline) || baseline;
      if (replacement !== current) {
        el.setAttribute(attr, replacement);
        wrote[attr] = replacement;
      } else {
        wrote[attr] = undefined;
      }
    });
  }

  function walk(root) {
    if (!root) return;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    var n;
    while ((n = walker.nextNode())) processTextNode(n);
    var els = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (var i = 0; i < els.length; i++) processElement(els[i]);
    processElement(root);
  }

  var pending = null;
  function schedule(root) {
    /* Coalesce roots instead of dropping them: when several mutation batches land
       inside one frame (a page render touches status bar, toolbar and list in one
       go), only the first root used to survive and later subtrees were never
       walked — dynamically inserted copy stayed untranslated until the next
       full pass. Same rAF coalescing, nothing dropped. */
    if (pending) { pending.add(root); return; }
    pending = new Set([root]);
    requestAnimationFrame(function () {
      var roots = pending;
      pending = null;
      roots.forEach(walk);
    });
  }

  var observer = new MutationObserver(function (muts) {
    for (var i = 0; i < muts.length; i++) {
      var m = muts[i];
      if (m.type === 'characterData' && m.target.parentElement) {
        schedule(m.target.parentElement);
      } else if (m.type === 'childList') {
        for (var j = 0; j < m.addedNodes.length; j++) {
          var n = m.addedNodes[j];
          if (n.nodeType === 1) schedule(n); else if (n.nodeType === 3 && n.parentElement) schedule(n.parentElement);
        }
      } else if (m.type === 'attributes' && m.target) {
        processElement(m.target);
      }
    }
  });

  function syncToggle() {
    /* The chip shows the language the app is IN, not the one a click would switch to.
       It used to be a bare nav button, where "EN" had to mean "click for English"; the
       control now lives in the user menu as `<label> <chip>`, where a target reads as
       "you are currently in English" on a Chinese page. The tooltip still carries the
       target, so the action is still stated — just where it belongs. */
    var current = lang === 'zh' ? '中文' : 'English';
    // Each label is written in the language it is *read* in, not one fixed string
    // per side: a Chinese tooltip on an English page is as wrong as an English
    // one on a Chinese page. Neither is a `中文 / English` pair on purpose —
    // `processElement` caches the original attribute value and would keep
    // recomputing a pair's side, fighting this function on every mutation.
    var tip = lang === 'zh' ? '切换到英文' : 'Switch to Chinese';
    Array.prototype.forEach.call(document.querySelectorAll('[data-lang-toggle]'), function (btn) {
      var label = btn.querySelector('[data-lang-label]');
      if (label) label.textContent = current; else btn.textContent = current;
      btn.setAttribute('title', tip);
      btn.setAttribute('aria-label', tip);
    });
  }

  function apply() {
    document.documentElement.lang = lang;
    document.documentElement.setAttribute('data-lang', lang);
    walk(document.body || document.documentElement);
    syncToggle();
    try {
      document.dispatchEvent(new CustomEvent('klado:lang', { detail: { lang: lang } }));
    } catch (e) { /* the event is a nicety */ }
  }

  function setLang(next, persist) {
    if (SUPPORTED.indexOf(next) < 0) return;
    lang = next;
    if (persist !== false) {
      try { localStorage.setItem(KEY, lang); } catch (e) { /* not fatal */ }
    }
    apply();
  }

  /* For strings the app builds before insertion: same rule as the DOM walker. */
  function t(s) {
    if (typeof s !== 'string') return s;
    var replacement = side(s);
    return replacement || s;
  }

  window.kladoI18n = {
    lang: function () { return lang; },
    setLang: setLang,
    t: t,
    dict: DICT,
    supported: SUPPORTED.slice(),
    key: KEY,
    apply: apply
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      apply();
      observer.observe(document.body, {
        childList: true, subtree: true, characterData: true,
        attributes: true, attributeFilter: ATTRS
      });
    });
  } else {
    apply();
    observer.observe(document.body, {
      childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ATTRS
    });
  }
})();