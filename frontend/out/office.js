/* ════════════════════════════════════════════════════════════════════════════
   office.js — the Agent office on the landing page (2026-10-05)

   Twelve isometric desks. An agent that connects takes one, the card floating
   over its head says what it is doing, and the animation on that card and at
   the desk is the same state — so the room is readable without reading it.

   ⚠️ ONE RULE SHAPES THE WHOLE FILE: the scene is a function of the seat list
   the server sent. Nothing about a seat, a name or a presence is discovered from
   the DOM, and there is no button that puts an agent anywhere. A desk belongs to a
   REGISTERED ACCOUNT; the agent on it is there because that account's agent
   credential has actually been used against this deployment.

   Presence and work are separate: access timestamps establish presence, while
   office_updates contains explicitly shared tasks and outcomes. Never infer a
   task from an API path. Expired updates are shown as historical, not live work.

       AgentOffice.get()                  // the room as data: who holds each desk
       AgentOffice.refresh()              // re-read the seat list

   ⚠️ WHY `t()` IS CALLED EVERYWHERE INSTEAD OF WRITING ONE LANGUAGE. The
   strings below are `中文 / English` pairs and the DOM walker only ever sees
   text that is ALREADY in the document, so anything this file builds has to
   pick its own side — the same rule `kldDialog` follows. The `klado:lang`
   listener at the bottom re-renders on a language switch, because a value that
   was translated once and cached would keep the old language forever.
   ════════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  /* ── Projection ──────────────────────────────────────────────────────────
     True isometric: the x axis at 30°, so a unit of x and a unit of y land the
     same distance from the origin and every cube is a rhombus. `U` is pixels
     per world unit and is cancelled out of the label mapping (that works in %
     of the viewBox), so it only controls how much detail the drawing can carry. */
  var CX = 0.8660254, CY = 0.5, U = 30;
  function px(x, y, z) { z = z || 0; return [(x - y) * CX * U, (x + y) * CY * U - z * U]; }

  /* ── Materials ───────────────────────────────────────────────────────────
     A material is THREE tokens, one per visible face, because an SVG `fill`
     cannot be "the token but 20% darker" and a shade factor computed in JS
     would be a colour literal in the wrong file. `mat('desk')` returns
     `var(--off-m-desk)`, `-a` and `-b`; theme.css owns all three, for both
     themes. Adding a surface here means adding tokens there first. */
  function mat(name) {
    return {
      top: 'var(--off-m-' + name + ')',
      a: 'var(--off-m-' + name + '-a)',
      b: 'var(--off-m-' + name + '-b)',
    };
  }

  /* Presence is derived by the server from recent agent calls or work reports.
     Work states below are independent and only populated by explicit reports. */
  var PRESENCE = {
    never:  { key: 'never',  label: '未到过 / Never arrived',      color: 'var(--off-st-never)',  icon: 'ri-door-line',       work: false },
    idle:   { key: 'idle',   label: '来过，现在不在 / Was here, away now', color: 'var(--off-st-idle)', icon: 'ri-time-line', work: false },
    active: { key: 'active', label: '近期在线 / Recently online',    color: 'var(--off-st-active)', icon: 'ri-checkbox-circle-line', work: true },
  };
  var WORK = {
    working: {label:'进行中 / Working', color:'var(--accent)'},
    waiting: {label:'待确认 / Needs input', color:'var(--warn)'},
    done: {label:'已完成 / Completed', color:'var(--ok)'},
    error: {label:'需处理 / Needs attention', color:'var(--danger)'},
    idle: {label:'待命 / Available', color:'var(--text-3)'}
  };
  function latest(holder) { return holder && holder.updates && holder.updates[0]; }
  function workLabel(holder) {
    var u = latest(holder);
    if (!u) return t('尚未上报任务 / No task reported');
    var state = WORK[u.state] || WORK.idle;
    return ((holder.presence !== 'active' || holder.work_stale) ? t('最近： / Last: ') : '') + t(state.label);
  }
  var STATE_ORDER = ['active', 'idle', 'never'];
  var MIN_LABEL = 15;   /* replaced below by the server's own window */

  /* ── The six faces ───────────────────────────────────────────────────────
     A reader picks an animal and a shirt in the user menu, and their agent wears
     them. Both lists are duplicated from `klado_shared/accounts.py`
     (`AVATAR_ANIMALS` / `AVATAR_CLOTHS`) on purpose: this file must not spend a
     round trip before the room can be drawn, and the server validates whatever
     arrives, so a stale cached SPA can send a name this build has never heard of
     and be told 400 rather than being trusted.

     ⚠️ Every colour an animal or a shirt uses is a `theme.css` token, and this
     file now contains NO colour literal at all. That is a change, not a detail:
     the old `HUES` array was one — twelve hex values picked to identify a person.
     It is gone because the shirt does that job now, and better: a hue nobody
     chose is a hue nobody recognises. The one thing that is still loud in the
     room is the state colour, which is what the room is read by. */
  var ANIMALS = [
    { key: 'tiger',  label: '老虎 / Tiger' },
    { key: 'ox',     label: '牛 / Ox' },
    { key: 'horse',  label: '马 / Horse' },
    { key: 'cat',    label: '猫 / Cat' },
    { key: 'rabbit', label: '兔子 / Rabbit' },
    { key: 'hippo',  label: '河马 / Hippo' },
  ];
  var CLOTHS = [
    { key: 'red',    label: '红 / Red' },
    { key: 'blue',   label: '蓝 / Blue' },
    { key: 'yellow', label: '黄 / Yellow' },
    { key: 'green',  label: '绿 / Green' },
    { key: 'black',  label: '黑 / Black' },
  ];
  /* ⚠️ A seat must always be drawable. An account whose stored value is not in
     the vocabulary — a newer server, a half-applied migration, a hand-edited row
     — falls back here rather than producing an animal with no geometry, which
     would be a desk with a person-shaped hole in it. */
  function animalOf(key) { return ANIMALS.some(function (a) { return a.key === key; }) ? key : ANIMALS[0].key; }
  function clothOf(key) { return CLOTHS.some(function (c) { return c.key === key; }) ? key : CLOTHS[1].key; }

  /* ── The room ───────────────────────────────────────────────────────────
     Two rows of six across a central aisle, which is what makes the room
     legible from above: a row you can count at a glance.

     ⚠️ `PITCH` is load-bearing, and it is a geometry constraint rather than a
     taste one. In this projection a step of PISTCH along x moves a label
     `PITCH*0.866*U` px RIGHT and `PITCH*0.5*U` px DOWN, so neighbouring cards
     cascade down the diagonal instead of lining up. The cards have to clear
     that cascade, and the only way to clear it is to make the down-step bigger
     than a card is tall: 4.2 units → 109px right / 63px down against a 51px
     card. The first version used 3.18 (82px right / 48px down) and tried to
     fix the 3px shortfall by lifting alternate cards — which is backwards,
     because a card lifted UP moves TOWARDS its up-left neighbour. The overlap
     got worse, not better. The aisle is the only thing that got the lift.

     ⚠️ The room is no longer a fixed twelve. It is sized from the seat list that
     comes back from `/api/auth/office-seats`, so `ROOM`, `COLS` and `ROWS` are
     computed inside `boot()` rather than written here. What stays here is the two
     numbers that must NOT move: `PITCH` (the card cascade, above) and the desk's
     own size. Growing the room grows it along those two axes only. */
  var PITCH = 4.2, DESK_W = 2.36, DESK_D = 1.24;
  /* Rows are 7 units apart, which is the current 6×2 room's own spacing, and it is
     also what keeps one row's cards clear of the next row's: 7 units is 105px of
     screen, against a 51px card. Rows alternate facing so the room is read from the
     middle outwards. */
  var ROW_PITCH = 7, ROW_Y0 = 2.55;
  var WALL_H = 2.7;
  /* Keep the supported capacity at 39 desks. Both office browser suites exercise
     a full room at this limit; increasing it requires rechecking cards and
     character clearance together. Cards follow the scene scale so they do
     not cover adjacent desks; zoom and the roster provide readable detail. */
  var SEATS_DEFAULT = 12, SEATS_STEP = 6, SEATS_MAX = 39;
  /* State that outlives a rebuild, because the teardown and the toolbar button
     both need it. */
  /* State that outlives a rebuild, because `teardown()` and the toolbar button
     both need it. `WIRED` and `REBUILD` used to live here too and are gone: with
     `on()` remembering every listener and `TICK` holding the interval, "is the
     room already wired?" is a question with no reader left, and a flag nobody
     reads is a flag that will be wrong. */
  var DATA = null, VISIBLE = 0, TICK = null;

  /* ── Rebuild bookkeeping ───────────────────────────────────────────────────
     ⚠️ Growing the room is a REBUILD, not an edit: the floor, the walls, the
     windows, the aisle and the grid all change size, and there is no honest way
     to add three desks to a drawn room without redrawing it. The price of a
     rebuild is that everything the last run attached has to come off first, and
     "everything" is where this file used to be wrong.

     The listeners were attached inline, so the second run attached a SECOND
     wheel handler, a SECOND pointermove handler and a SECOND interval, each
     closing over its own SEATS. Press "more desks" twice and every drag moved the
     room three times, three intervals repainted three rooms, and the labels were
     placed three times — a leak whose symptom is "the office got busier when I
     asked for more desks", which is exactly backwards.

     So there is ONE way to attach anything here: `on()`, which remembers how to
     undo it. A bare `addEventListener` in this file is a bug, and the reason is
     written here so the next reader knows it is not a style preference. */
  var CLEAN = [];
  function on(target, type, fn, opts) {
    target.addEventListener(type, fn, opts);
    CLEAN.push(function () { target.removeEventListener(type, fn, opts); });
  }
  function teardown() {
    if (TICK) { clearInterval(TICK); TICK = null; }
    CLEAN.forEach(function (off) { try { off(); } catch (e) { /* a detached node */ } });
    CLEAN = [];
    /* The drawn room and the cards over it are children of two containers that
       live in the HTML, not in the closure — so they survive `boot()` returning
       and would be found by the next run alongside its own freshly built copies.
       Emptied here, by id, so the teardown does not need the closure it is
       tearing down. */
    ['off-svg', 'off-labels'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.textContent = '';
    });
  }

  /* ⚠️ These three live at MODULE level, not inside `boot()`, and that is a
     scope fact with a real failure behind it: `loadSeats()` runs outside the
     builder and reports a failed fetch with `t(...)`, and while these were
     inside it the error path threw "t is not defined" — so the one message
     that tells the reader the room could not be loaded was itself the thing
     that broke. A helper only the happy path uses can live in the builder; a
     helper the error path uses cannot. */
  function t(s) { return (window.kladoI18n && kladoI18n.t) ? kladoI18n.t(s) : s; }
  function mmss(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    return (Math.floor(s / 60) < 10 ? '0' : '') + Math.floor(s / 60) + ':' + (s % 60 < 10 ? '0' : '') + (s % 60);
  }
  /* "Last seen" is a real elapsed time and it spans four orders of magnitude: a
     desk whose agent was here four minutes ago and one last seen this morning both
     render, and `mm:ss` said `5793:41` for the second — technically a duration and
     practically unreadable. So the unit is chosen, not assumed, and the pair is
     unit-free on the English side: the number and the unit are built separately
     and only the WORDS are a pair, so switching language never leaves a Chinese
     unit sitting on an English line. */
  function ago(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    if (s < 60) return { n: s, u: '秒前 / sec ago' };
    if (s < 3600) return { n: Math.floor(s / 60), u: '分钟前 / min ago' };
    if (s < 86400) return { n: Math.floor(s / 3600), u: '小时前 / h ago' };
    return { n: Math.floor(s / 86400), u: '天前 / d ago' };
  }
  /* The number is language-neutral, so it is concatenated AFTER translation rather
     than before — building `'…' + n + ' 分钟前 / min ago'` and translating the whole
     string is what let a Chinese unit survive on an English page. */
  function agoLabel(ms) { var a = ago(ms); return a.n + ' ' + t(a.u); }
  /* Truncate on DISPLAY width, after the language has been chosen: a CJK glyph
     is about twice a Latin one, so counting characters would leave the English
     cards half empty and clip the Chinese ones mid-glyph. */
  /* ── The heads ───────────────────────────────────────────────────────────
     Six beasts on a human body, drawn front-on at 16 user units across. The
     envelope is FIXED and it is the card's, not the animal's: the topmost pixel
     any of them may use is y = -50 (the rabbit's ears), and `placeLabel` puts
     the card's stalk to land just above it. An animal that drew past that line
     would be drawn behind the card that belongs to it — and the failure is
     invisible in a static assertion, because the card and the agent are two
     different elements in two different trees.

     The six are told apart by SILHOUETTE first (ears, horns, muzzle length) and
     coat second, because at this size a tiger and a cat are 90px apart on a wall
     and a colour difference is not what a reader uses. Every colour is a token;
     `theme.css` owns all eighteen. */
  function beastHead(kind) {
    var g = E('g', { class: 'off-beast', transform: 'translate(0,7) scale(1.22)' });
    var coat = 'var(--off-fur-' + kind + ')';
    var dk = 'var(--off-fur-' + kind + '-d)';
    var pale = 'var(--off-fur-pale)';
    /* The hairline is the same one the furniture carries, for the same reason: at
       this size a coat colour and the desk behind it are close in value in the
       light theme, and without an edge the figure is a smudge. */
    var ed = { stroke: 'var(--off-edge)', 'stroke-width': .6, 'stroke-linejoin': 'round' };
    function P(tag, at) { for (var k in ed) if (at[k] === undefined) at[k] = ed[k]; return E(tag, at); }
    /* Eyes are the one place a highlight is worth two paths: a flat dot at 15
       user units reads as a hole, and the room is read for "who is switched on". */
    function eye(x, y, r) {
      g.appendChild(E('circle', { cx: x, cy: y, r: r, fill: dk }));
      g.appendChild(E('circle', { cx: x - r * .34, cy: y - r * .38, r: r * .44, fill: pale }));
    }
    function ear(x, y, r) {
      g.appendChild(E('circle', { cx: x, cy: y, r: r, fill: coat, stroke: 'var(--off-edge)', 'stroke-width': .6 }));
      g.appendChild(E('circle', { cx: x, cy: y, r: r * .5, fill: pale }));
    }
    function stripes(list) {
      list.forEach(function (s) {
        g.appendChild(E('path', { d: s, fill: 'none', stroke: dk, 'stroke-width': 1.1, 'stroke-linecap': 'round' }));
      });
    }
    function nose(x, y, w) {
      g.appendChild(E('path', { d: 'M' + (x - w) + ',' + (y - .5) + 'L' + (x + w) + ',' + (y - .5) + 'L' + x + ',' + (y + 1.5) + 'Z', fill: dk }));
    }
    function dots(list) {
      list.forEach(function (d) { g.appendChild(E('circle', { cx: d[0], cy: d[1], r: .48, fill: dk })); });
    }

    if (kind === 'tiger') {
      ear(-6.6, -40.6, 3.3); ear(6.6, -40.6, 3.3);
      g.appendChild(P('circle', { cx: 0, cy: -34, r: 8.2, fill: coat }));
      stripes(['M-2.4,-41.4v2.6', 'M0,-42v3', 'M2.4,-41.4v2.6',
               'M-8,-36.2l2.4,1', 'M-8,-32.6l2.4,-1', 'M8,-36.2l-2.4,1', 'M8,-32.6l-2.4,-1']);
      g.appendChild(P('ellipse', { cx: 0, cy: -29.4, rx: 4.7, ry: 3.2, fill: pale }));
      nose(0, -31.2, 1.2);
      dots([[-3.2, -30.2], [-1.6, -30.8], [1.6, -30.8], [3.2, -30.2], [-2.4, -28.8], [2.4, -28.8]]);
      eye(-3.1, -35.6, 1.5); eye(3.1, -35.6, 1.5);
    } else if (kind === 'ox') {
      /* Horns first, so the head overlaps their base and they read as coming out
         of the skull rather than being stuck to it. */
      g.appendChild(P('path', { d: 'M-5.4,-39.6C-9.4,-41.4 -12.2,-44.6 -11.6,-48.4', fill: 'none', stroke: pale, 'stroke-width': 2.4, 'stroke-linecap': 'round' }));
      g.appendChild(P('path', { d: 'M5.4,-39.6C9.4,-41.4 12.2,-44.6 11.6,-48.4', fill: 'none', stroke: pale, 'stroke-width': 2.4, 'stroke-linecap': 'round' }));
      g.appendChild(P('ellipse', { cx: -8.2, cy: -33.4, rx: 2.6, ry: 1.6, fill: coat, transform: 'rotate(-18 -8.2 -33.4)' }));
      g.appendChild(P('ellipse', { cx: 8.2, cy: -33.4, rx: 2.6, ry: 1.6, fill: coat, transform: 'rotate(18 8.2 -33.4)' }));
      g.appendChild(P('ellipse', { cx: 0, cy: -34, rx: 8.6, ry: 7.8, fill: coat }));
      g.appendChild(P('path', { d: 'M-3.4,-41.4C-1.6,-42.6 1.6,-42.6 3.4,-41.4C1.6,-40.2 -1.6,-40.2 -3.4,-41.4Z', fill: dk }));
      g.appendChild(P('ellipse', { cx: 0, cy: -28.6, rx: 5.9, ry: 3.7, fill: pale }));
      g.appendChild(E('ellipse', { cx: -2, cy: -29.4, rx: .8, ry: 1.1, fill: dk }));
      g.appendChild(E('ellipse', { cx: 2, cy: -29.4, rx: .8, ry: 1.1, fill: dk }));
      eye(-3.4, -36, 1.4); eye(3.4, -36, 1.4);
    } else if (kind === 'horse') {
      g.appendChild(P('path', { d: 'M-3.8,-40.4L-4.9,-49.2L-1.2,-42.4Z', fill: coat }));
      g.appendChild(P('path', { d: 'M3.8,-40.4L4.9,-49.2L1.2,-42.4Z', fill: coat }));
      g.appendChild(P('ellipse', { cx: 0, cy: -33.6, rx: 6.6, ry: 8.8, fill: coat }));
      /* The mane sits down the back of the skull, which for a front-on horse is a
         dark wedge above the muzzle — the only place the silhouette would
         otherwise be a plain egg. */
      g.appendChild(P('path', { d: 'M-4.4,-40.6C-6.2,-37 -6.4,-31.4 -4.8,-26.4L-2.6,-27.4C-3.8,-31.6 -3.8,-36.6 -2.6,-39.6Z', fill: dk }));
      g.appendChild(P('path', { d: 'M-2.2,-43.4C0,-44.6 2,-44.6 3,-43.4C1.8,-41.6 -1.4,-41.6 -2.2,-43.4Z', fill: dk }));
      g.appendChild(P('ellipse', { cx: 0, cy: -27.8, rx: 3.8, ry: 3.5, fill: pale }));
      g.appendChild(E('ellipse', { cx: -1.5, cy: -28.6, rx: .62, ry: .95, fill: dk }));
      g.appendChild(E('ellipse', { cx: 1.5, cy: -28.6, rx: .62, ry: .95, fill: dk }));
      eye(-3.4, -36.8, 1.5); eye(3.4, -36.8, 1.5);
    } else if (kind === 'cat') {
      g.appendChild(P('path', { d: 'M-6.4,-38.6L-6.8,-45.8L-1.4,-41.4Z', fill: coat }));
      g.appendChild(P('path', { d: 'M6.4,-38.6L6.8,-45.8L1.4,-41.4Z', fill: coat }));
      g.appendChild(P('path', { d: 'M-5.9,-40.6L-6.1,-44.2L-3,-41.6Z', fill: pale }));
      g.appendChild(P('path', { d: 'M5.9,-40.6L6.1,-44.2L3,-41.6Z', fill: pale }));
      g.appendChild(P('circle', { cx: 0, cy: -34, r: 7.7, fill: coat }));
      stripes(['M-2,-40.6v2.2', 'M0,-41.2v2.6', 'M2,-40.6v2.2', 'M-7.5,-35.4l2,.8', 'M7.5,-35.4l-2,.8']);
      g.appendChild(P('ellipse', { cx: 0, cy: -30.4, rx: 3.6, ry: 2.5, fill: pale }));
      nose(0, -31.8, 1);
      eye(-2.9, -35.6, 1.6); eye(2.9, -35.6, 1.6);
      stripes(['M-5.2,-30.2l-4.4,-1.4', 'M-5.2,-28.6l-4.6,.6', 'M5.2,-30.2l4.4,-1.4', 'M5.2,-28.6l4.6,.6']);
    } else if (kind === 'rabbit') {
      g.appendChild(P('ellipse', { cx: -3.4, cy: -44.4, rx: 2.5, ry: 7.2, fill: coat }));
      g.appendChild(P('ellipse', { cx: 3.4, cy: -44.4, rx: 2.5, ry: 7.2, fill: coat }));
      g.appendChild(E('ellipse', { cx: -3.4, cy: -44.4, rx: 1.2, ry: 5, fill: pale }));
      g.appendChild(E('ellipse', { cx: 3.4, cy: -44.4, rx: 1.2, ry: 5, fill: pale }));
      g.appendChild(P('circle', { cx: 0, cy: -33.4, r: 7.5, fill: coat }));
      g.appendChild(P('ellipse', { cx: 0, cy: -29.4, rx: 3.3, ry: 2.3, fill: pale }));
      nose(0, -31, .9);
      eye(-2.8, -35, 1.5); eye(2.8, -35, 1.5);
      /* The two front teeth. They are the whole reason a reader can tell a rabbit
         from a cat at a glance without reading the roster. */
      g.appendChild(E('rect', { x: -1.3, y: -28.3, width: 1.1, height: 2.4, rx: .5, fill: pale, stroke: 'var(--off-edge)', 'stroke-width': .4 }));
      g.appendChild(E('rect', { x: .2, y: -28.3, width: 1.1, height: 2.4, rx: .5, fill: pale, stroke: 'var(--off-edge)', 'stroke-width': .4 }));
    } else {
      /* hippo. The widest head of the six and the smallest ears, which is the
         whole identification: a hippo is a muzzle with a little head on top. */
      ear(-7, -39.8, 1.9); ear(7, -39.8, 1.9);
      g.appendChild(P('ellipse', { cx: 0, cy: -33.6, rx: 9.2, ry: 7.6, fill: coat }));
      g.appendChild(P('ellipse', { cx: 0, cy: -28.2, rx: 7.2, ry: 4.5, fill: pale }));
      g.appendChild(E('ellipse', { cx: -2.2, cy: -31.2, rx: 1, ry: 1.5, fill: dk }));
      g.appendChild(E('ellipse', { cx: 2.2, cy: -31.2, rx: 1, ry: 1.5, fill: dk }));
      g.appendChild(P('path', { d: 'M-5.6,-25.8C-2.6,-24.4 2.6,-24.4 5.6,-25.8', fill: 'none', stroke: dk, 'stroke-width': .8, 'stroke-linecap': 'round' }));
      eye(-4, -37.2, 1.3); eye(4, -37.2, 1.3);
    }
    // Soft side shading and a headset give the small faces depth and a working identity.
    g.appendChild(E('path', {d:'M5,-39 Q12,-33 5,-27 Q8,-33 5,-39', fill:dk, opacity:'.16'}));
    g.appendChild(E('path', {d:'M-8,-34 Q-10,-46 0,-46 Q10,-46 8,-34', fill:'none', stroke:'var(--off-m-chair-a)', 'stroke-width':1.9}));
    [-8,8].forEach(function(x) { g.appendChild(E('rect', {x:x-1.5,y:-37,width:3,height:7,rx:1.5,fill:'var(--off-monitor)'})); });
    g.appendChild(E('path', {d:'M9,-32 Q10,-27 4,-27', fill:'none',stroke:'var(--off-monitor)','stroke-width':1.1}));
    g.appendChild(E('circle', {cx:3.4,cy:-27,r:1.2,fill:'var(--off-monitor)'}));
    return g;
  }

  /* ── SVG element helper ───────────────────────────────────────────────────
     At module level, and not inside the room builder, because `beastHead` is:
     the six animals are a self-contained piece of drawing that has to be
     reachable from two places (the room, and the picker in the user menu), and
     a helper that only one caller can see is a helper the second caller will
     quietly re-implement. */
  var NS = 'http://www.w3.org/2000/svg';
  function E(tag, at, kids) {
    var e = document.createElementNS(NS, tag);
    if (at) for (var k in at) { if (at[k] !== null && at[k] !== undefined) e.setAttribute(k, at[k]); }
    if (kids) (Array.isArray(kids) ? kids : [kids]).forEach(function (c) { if (c) e.appendChild(c); });
    return e;
  }

  function clip(s, halfWidth) {
    var w = 0, out = '';
    for (var i = 0; i < s.length; i++) {
      var cjk = s.charCodeAt(i) > 0x2e80;
      var w2 = cjk ? 2 : 1;
      if (w + w2 > halfWidth) return out + '…';
      out += s[i]; w += w2;
    }
    return out;
  }

  /* Everything below builds the room, so it can only run once the seat
     list is known: the grid shape, the floor and the walls are all a
     function of how many people there are. */
  /* ⚠️ The second parameter is `asked`, NOT `visible`, and that is not a style
     choice — it is a bug that was in this function for one build. The tick gate
     below is `function visible()`, a DECLARATION, and a function declaration is
     hoisted to the top of its scope and shadows a parameter of the same name.
     So `visible || SEATS_DEFAULT` was `function visible() || 12` → 12, which
     happened to be right, while `Math.min` and `Math.max` downstream produced
     NaN, `COLS`/`ROWS` were NaN, and the room came out with no desks and a floor
     full of `NaN,NaN` polygons.

     Nothing threw. `boot` ran to completion, `window.AgentOffice` was exported,
     the SVG was populated, and the only evidence was one "seat list is empty"
     assertion and a console full of SVG warnings that read like a drawing
     problem. A name collision is the cheapest possible failure and the hardest
     to see, so both names are now distinct on purpose. */
  function boot(data, asked) {
    /* ⚠️ TWELVE desks, ALWAYS — not "as many accounts as there are", and not
       "as many accounts as there are, capped at twelve" either.

       The first version of this clamped the room to the account count, on the
       reasoning that "a desk is a claim that somebody works here, so do not draw
       nine empty ones". The user asked for twelve desks and a button to ask for
       more, and on a deployment with one registered account the clamp drew a
       room with one desk in it AND hid the button, because the button's own state
       was computed from the same number. So the feature the reader was promised
       was invisible on the deployment they were looking at — which is worse than
       not having built it, because a missing feature is obvious and a missing
       feature that reports itself as "nothing left to add" is a bug report.

       The reasoning was not wrong so much as aimed at the wrong thing. An empty
       desk in an office is furniture that exists for the next person, not a
       claim that somebody works there; the thing that must never be invented is
       an OCCUPANT, and that rule is untouched — a desk with nobody assigned to it
       is drawn as bare furniture with the empty ring, never with a figure.

       So the room is a room, sized by what a reader can see, and the people are
       placed into it. The one number that is NOT a free choice is the headcount:
       "注册了之后会有一张位子" is a promise this page made before any of the
       drawing existed, and a room smaller than the company would break it. So the
       size is the largest of {twelve, the account count, what the reader asked
       for}, capped at `SEATS_MAX` — which is a rendering guard and nothing else.
       Growing by headcount means a deployment that gains a colleague sees the
       room get a desk on the next load, and a reader who wants more room than the
       company needs can ask for it. */
    var total = data && data.seats ? data.seats.length : 0;
    var n = Math.min(SEATS_MAX, Math.max(SEATS_DEFAULT, total, asked || 0));
    var COLS = Math.max(2, Math.min(8, Math.ceil(Math.sqrt(n * 2))));
    var ROWS = Math.max(1, Math.ceil(n / COLS));
    var ROOM = {
      w: 0.92 * 2 + (COLS - 1) * PITCH + DESK_W,
      d: ROW_Y0 * 2 + (ROWS - 1) * ROW_PITCH + 2.5,
      wallH: WALL_H,
    };
    var DOOR = { x: 0.7, y: ROOM.d + 0.9 };
    DATA = data || { seats: [] };
    VISIBLE = n;

    /* ⚠️ A rebuild, not an edit. Growing the desk count changes the floor, the
       walls, the windows and the aisle, so the room is genuinely a different room
       and the honest thing is to draw it again. That is only safe because
       everything below is torn down first: the previous run's `svg` and label
       children are removed, and the listeners and the interval below are attached
       exactly once for the life of the page. Without that, a reader who pressed
       "more desks" twice would have two wheel handlers, two resize handlers and
       two intervals, each drawing the same card — a leak that looks like the room
       getting busier. */
    teardown();

    var SEATS = [];
    (function () {
      for (var r = 0; r < ROWS; r++) {
        for (var c = 0; c < COLS; c++) {
          var idx = r * COLS + c;
          if (idx >= n) break;
          // Every row faces the same way: screen, keyboard, then chair along +y.
          var back = true;
          var y = ROW_Y0 + r * ROW_PITCH;
          var seat = (DATA.seats && DATA.seats[idx]) || null;
          SEATS.push({
            i: idx,
            num: idx + 1,
            col: c, back: back,
            x: 0.92 + c * PITCH, y: y, w: DESK_W, d: DESK_D,
            // Chairs sit on the keyboard side in every row.
            cx: 0.92 + c * PITCH + DESK_W / 2,
            chY: y + DESK_D + 0.5,
            /* The holder of the desk, and whether their agent has been here. Both
               come from the server; nothing here invents either. */
            holder: seat,
            presence: seat ? (seat.presence || 'never') : 'never',
            avatar: seat ? (seat.avatar || null) : null,
            agent: null, el: null,
          });
        }
      }
    })();

    /* ── Small helpers ─────────────────────────────────────────────────────── */
    function H(tag, cls, text) {
      var e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text !== undefined) e.textContent = text;
      return e;
    }
    function pts(list) {
      return list.map(function (p) { var q = px(p[0], p[1], p[2] || 0); return q[0].toFixed(1) + ',' + q[1].toFixed(1); }).join(' ');
    }

    /* ── Cuboid → three polygons. The camera sits at +x +y, so those are the
       two side faces that are ever visible; the fourth is never drawn. ──────── */
    function box(x, y, w, d, z, h, m, cls, edge) {
      var A = [x, y, z + h], B = [x + w, y, z + h], Cc = [x + w, y + d, z + h], D = [x, y + d, z + h];
      var b0 = [x + w, y, z], c0 = [x + w, y + d, z], d0 = [x, y + d, z];
      var g = E('g', cls ? { class: cls } : null);
      /* The hairline is not a style choice. Isometric furniture is a stack of
         same-value faces; without an edge the stack is one shape, and the light
         theme — where the faces differ by 4% — has nothing left to read. */
      var st = edge ? { stroke: 'var(--off-edge)', 'stroke-width': .55, 'stroke-linejoin': 'round' } : null;
      function face(pts3, fill) {
        var a2 = { points: pts(pts3), fill: fill };
        if (st) for (var k in st) a2[k] = st[k];
        return E('polygon', a2);
      }
      g.appendChild(face([b0, c0, Cc, B], m.a));
      g.appendChild(face([d0, c0, Cc, D], m.b));
      g.appendChild(face([A, B, Cc, D], m.top));
      return g;
    }

    /* The contact shadow. A desk standing on a floor with nothing under it reads
       as a sticker; this is the one cheap thing that makes it sit on the floor. */
    function contact(x, y, w, d) {
      var c = px(x + w / 2, y + d / 2, 0.03);
      return E('ellipse', { cx: c[0], cy: c[1], rx: w * CX * U * .62, ry: w * CY * U * .62, fill: 'var(--off-shadow)' });
    }

    /* ══════════════════════════════════════════════════════════════════════
       Build the room. Static geometry only — it is built once and never rebuilt,
       because a per-frame re-projection of ~400 polygons is a lot of DOM churn
       for a picture that does not move.
       ══════════════════════════════════════════════════════════════════════ */
    var stage = document.getElementById('off-stage');
    var svg = document.getElementById('off-svg');
    var labels = document.getElementById('off-labels');
    if (!stage || !svg || !labels) return;

    var defs = E('defs');
    var clipFloor = E('clipPath', { id: 'off-floor-clip' });
    defs.appendChild(clipFloor);
    var grad = E('linearGradient', { id: 'off-sweep-grad', x1: '0', y1: '0', x2: '1', y2: '0' });
    grad.appendChild(E('stop', { offset: '0', 'stop-color': 'var(--text-on-accent)', 'stop-opacity': '0' }));
    grad.appendChild(E('stop', { offset: '.5', 'stop-color': 'var(--text-on-accent)', 'stop-opacity': '.8' }));
    grad.appendChild(E('stop', { offset: '1', 'stop-color': 'var(--text-on-accent)', 'stop-opacity': '0' }));
    defs.appendChild(grad);
    svg.appendChild(defs);

    var world = E('g');
    svg.appendChild(world);

    /* Content bounds, so the viewBox can be fitted to a fixed ratio — the stage
       carries the same ratio, and that is what keeps the % label mapping exact
       through a resize. */
    var bb = { x0: 1e9, y0: 1e9, x1: -1e9, y1: -1e9 };
    function grow(list) {
      list.forEach(function (p) {
        var q = px(p[0], p[1], p[2] || 0);
        if (q[0] < bb.x0) bb.x0 = q[0];
        if (q[0] > bb.x1) bb.x1 = q[0];
        if (q[1] < bb.y0) bb.y0 = q[1];
        if (q[1] > bb.y1) bb.y1 = q[1];
      });
    }
    grow([[0, 0, 0], [ROOM.w, 0, 0], [ROOM.w, ROOM.d, 0], [0, ROOM.d, 0], [0, 0, ROOM.wallH]]);
    /* ⚠️ The floor is drawn 0.6 units WIDER than the room on every side (see the
       `box(-0.6, -0.6, …)` call below), so measuring only the room shell left the
       bottom edge of the floor hanging 18 viewBox units outside the box — about
       13px of screen at a 1280px stage. The room therefore sat low in the frame
       and looked like it needed to be dragged up by hand. Anything that is
       actually painted has to be in here, not just the nominal room. */
    grow([[-.6, -.6, 0], [ROOM.w + .6, -.6, 0], [ROOM.w + .6, ROOM.d + .6, 0], [-.6, ROOM.d + .6, 0]]);
    /* 2.0, not 1.6, and the reason is the card rather than the room: a card is
       anchored with its bottom edge 2.35 world units (70.5px) above the seat and
       grows upward, so its top edge is ~110px up. At 1.6 the bound was 48px and
       the frame was sized by the room, not by what is drawn on it — the top row's
       cards would be clipped by the viewBox, which is the one thing the
       label-clipping rule above exists to prevent. */
    grow(SEATS.map(function (s) { return [s.cx, s.chY, 2.0]; }));
    grow([[DOOR.x, DOOR.y, 0]]);

    /* The stage ratio is a CSS decision (a phone gets a taller frame), so JS asks
       the stylesheet rather than deciding. `data-ratio` then keeps the two in
       step, and the stylesheet stays the single place the breakpoint lives. */
    var narrowMQ = window.matchMedia('(max-width: 720px)');
    /* The stage's height comes from its aspect-ratio, and on a laptop that is
       enough to push the bottom row of desks below the fold — the reader scrolls
       to see the room the page just told them was all there. So the height is
       capped at what is actually left under the stage, and the room letterboxes
       into that box instead of overflowing it. LEGEND_H is what sits under the
       stage, so it is part of what has to fit, not part of what may be clipped. */
    var LEGEND_H = 46;
    function capStage() {
      var top = stage.getBoundingClientRect().top + window.scrollY;
      /* Measured, not assumed: the legend wraps to two rows on a narrow screen and
         a hardcoded height under-counts it, which is the same overflow all over
         again. Falls back to the guess only before the legend exists. */
      var lg = document.getElementById('off-legend');
      var below = (lg && lg.getBoundingClientRect().height ? lg.getBoundingClientRect().height : LEGEND_H);
      var avail = window.innerHeight - top - below - 12;
      stage.style.setProperty('--off-stage-cap', Math.max(260, Math.round(avail)) + 'px');
    }
    function fitViewBox() {
      var w = bb.x1 - bb.x0, h = bb.y1 - bb.y0;
      /* Just enough for a card (104px wide, so ~52 either side of its head) and a
         card's height above the tallest head. Bigger padding does not make the
         room clearer, it just pads the frame with floor. */
      var padX = 66, padY = 84;
      var vw = w + padX * 2, vh = h + padY * 2;
      var want = narrowMQ.matches ? 1000 / 860 : 1000 / 660;
      /* Expand the short axis rather than cropping: a label clipped by the
         viewBox is a label that is silently lying about being there. */
      if (vw / vh < want) vw = vh * want; else vh = vw / want;
      var vx = (bb.x0 + bb.x1) / 2 - vw / 2, vy = (bb.y0 + bb.y1) / 2 - vh / 2;
      stage.setAttribute('data-ratio', narrowMQ.matches ? 'tall' : 'wide');
      svg.setAttribute('viewBox', vx.toFixed(1) + ' ' + vy.toFixed(1) + ' ' + vw.toFixed(1) + ' ' + vh.toFixed(1));
      VIEW = { x: vx, y: vy, w: vw, h: vh };
      /* Cap AFTER the ratio is on the element, and place the cards after that, so
         every label is positioned against the box the room is finally drawn in. */
      capStage();
      SEATS.forEach(placeLabel);
    }
    var VIEW = null;

    /* ── Floor, grid, aisle ─────────────────────────────────────────────── */
    world.appendChild(box(-0.6, -0.6, ROOM.w + 1.2, ROOM.d + 1.2, 0, 0.02, mat('floor')));
    clipFloor.appendChild(E('polygon', { points: pts([[-0.6, -0.6, .04], [ROOM.w + .6, -.6, .04], [ROOM.w + .6, ROOM.d + .6, .04], [-.6, ROOM.d + .6, .04]]) }));

    (function grid() {
      var g = E('g', { opacity: .8 });
      for (var x = 0; x <= ROOM.w; x += 1.7) g.appendChild(E('line', { x1: px(x, 0, .02)[0], y1: px(x, 0, .02)[1], x2: px(x, ROOM.d, .02)[0], y2: px(x, ROOM.d, .02)[1], stroke: 'var(--off-route)', 'stroke-width': .4, opacity: .35 }));
      for (var y = 0; y <= ROOM.d; y += 1.7) g.appendChild(E('line', { x1: px(0, y, .02)[0], y1: px(0, y, .02)[1], x2: px(ROOM.w, y, .02)[0], y2: px(ROOM.w, y, .02)[1], stroke: 'var(--off-route)', 'stroke-width': .4, opacity: .35 }));
      world.appendChild(g);
    })();

    (function aisle() {
      var z = 0.035;
      world.appendChild(E('polygon', { points: pts([[.4, 6.0, z], [ROOM.w - .4, 6.0, z], [ROOM.w - .4, 8.2, z], [.4, 8.2, z]]), fill: 'var(--off-aisle)' }));
      var a = px(0.4, 7.1, z), b = px(ROOM.w - 0.4, 7.1, z);
      world.appendChild(E('path', { d: 'M' + a[0] + ',' + a[1] + 'L' + b[0] + ',' + b[1], stroke: 'var(--off-route)', 'stroke-width': 1.4, opacity: .55, 'stroke-dasharray': '7 5' }));
    })();

    /* The light band. Clipped to the floor so it cannot wash the walls, and
       slow enough (16s) that it reads as a room with daylight in it rather than
       as something that is animating. */
    world.appendChild(E('g', { 'clip-path': 'url(#off-floor-clip)' },
      E('rect', { class: 'off-sweep', x: -560, y: bb.y0 - 300, width: 260, height: bb.y1 - bb.y0 + 600, fill: 'url(#off-sweep-grad)', opacity: 'var(--off-sweep-op)' })));

    /* ── Back walls + windows ──────────────────────────────────────────── */
    (function walls() {
      var h = ROOM.wallH;
      world.appendChild(E('polygon', { points: pts([[0, 0, 0], [0, ROOM.d, 0], [0, ROOM.d, h], [0, 0, h]]), fill: 'var(--off-m-wall-b)' }));
      world.appendChild(E('polygon', { points: pts([[0, 0, 0], [ROOM.w, 0, 0], [ROOM.w, 0, h], [0, 0, h]]), fill: 'var(--off-m-wall-a)' }));
      /* `win(axis, from, to)`: axis 'x' is the wall at x=0 (varying y), axis 'y'
         is the wall at y=0 (varying x). The first version passed one `plane` for
         both, so the second set of three windows was drawn on the FIRST wall at
         y≈0 — a 12-unit light-blue streak across the corner instead of windows. */
      function win(axis, from, to) {
        var at = function (v, z) { return axis === 'x' ? [0.02, v, z] : [v, 0.02, z]; };
        /* ⚠️ `at` returns a WORLD POINT — an [x, y, z] triple — and `px` takes
           three scalars. This used to call `px(at(from, 1.52)[0], at(from, 1.52)[1])`,
           which projects (x, y) and DROPS the z: every window's middle bar was
           drawn at floor level, and the six `NaN` polygon warnings in the console
           came from the same line's neighbours. `proj` is the one place that
           unpacks a world point, so a window cannot be drawn in the wrong plane
           by forgetting an index again. */
        function proj(v, z) { var p = at(v, z); return px(p[0], p[1], p[2]); }
        var quad = [at(from, 1.0), at(to, 1.0), at(to, 2.05), at(from, 2.05)];
        world.appendChild(E('polygon', { points: pts(quad), fill: 'var(--off-glass)' }));
        world.appendChild(E('polygon', { points: pts(quad), fill: 'none', stroke: 'var(--off-route)', 'stroke-width': .6, opacity: .8 }));
        var m0 = proj(from, 1.52), m1 = proj(to, 1.52);
        world.appendChild(E('line', { x1: m0[0].toFixed(1), y1: m0[1].toFixed(1), x2: m1[0].toFixed(1), y2: m1[1].toFixed(1),
          stroke: 'var(--off-route)', 'stroke-width': .5, opacity: .8 }));
      }
      [3.2, 8.4, 13.6].forEach(function (y) { win('x', y, y + 4.1); });
      [3.4, 9.0, 14.6].forEach(function (x) { win('y', x, x + 4.0); });
      /* The door on the right-hand wall: agents arrive through the mat at the
         front, and this is where that mat leads. */
      world.appendChild(E('polygon', { points: pts([[ROOM.w - .02, .6, 0], [ROOM.w - .02, 2.5, 0], [ROOM.w - .02, 2.5, 2.05], [ROOM.w - .02, .6, 2.05]]), fill: 'var(--off-m-wall-a)' }));
    })();

    (function board() {
      var x = 6.2;
      world.appendChild(E('polygon', { points: pts([[x, .06, .95], [x + 4.6, .06, .95], [x + 4.6, .06, 2.5], [x, .06, 2.5]]), fill: 'var(--surface)', stroke: 'var(--border)', 'stroke-width': .7 }));
      [3.1, 2.4, 1.6].forEach(function (w, i) {
        var yy = 1.25 + i * 0.36;
        world.appendChild(E('polygon', { points: pts([[x + .3, .05, yy], [x + .3 + w, .05, yy], [x + .3 + w, .05, yy + .1], [x + .3, .05, yy + .1]]), fill: i ? 'var(--border-strong)' : 'var(--accent)', opacity: .5 }));
      });
    })();

    /* ── Set dressing ───────────────────────────────────────────────────── */
    function plant(x, y) {
      var g = E('g', { class: 'off-plant' });
      g.appendChild(box(x, y, .62, .62, 0, .42, mat('pot')));
      var base = px(x + .31, y + .31, .42), top = px(x + .31, y + .31, 0);
      g.appendChild(E('line', { x1: base[0], y1: base[1], x2: top[0], y2: top[1], stroke: 'var(--off-m-plant-a)', 'stroke-width': 1.2 }));
      for (var i = 0; i < 3; i++) {
        var yy = .85 + i * .34, w = .78 - i * .16, cx = x + .31 - w / 2;
        g.appendChild(E('polygon', { points: pts([[cx, y + .31, yy], [cx + w, y + .31, yy], [cx + w * .72, y + .31, yy + .2], [cx + w * .28, y + .31, yy + .2]]), fill: i % 2 ? 'var(--off-m-plant)' : 'var(--off-m-plant-a)' }));
      }
      world.appendChild(g);
    }
    plant(.35, .35); plant(ROOM.w - 1.15, .35); plant(.35, ROOM.d - 1.15); plant(ROOM.w - 1.15, ROOM.d - 1.15);

    (function coffee() {
      var x = ROOM.w - 3.1, y = .5;
      world.appendChild(box(x, y, 2.1, .9, 0, .98, mat('wood')));
      world.appendChild(box(x + .22, y + .2, .66, .5, .98, .62, mat('chair')));
      var b = px(x + .55, y + .45, 1.6);
      world.appendChild(E('path', { class: 'steam', d: 'M' + b[0] + ',' + b[1] + 'c3,-5 -3,-8 0,-12 c3,-4 0,-7 0,-7', stroke: 'var(--off-monitor-off)', fill: 'none' }));
    })();

    (function rack() {
      var x = ROOM.w - 5.4, y = .42;
      world.appendChild(box(x, y, .9, .72, 0, 1.5, mat('chair')));
      for (var i = 0; i < 3; i++) {
        var p = px(x + .18 + i * .22, y + .73, 1.3);
        world.appendChild(E('circle', { class: 'off-led', cx: p[0], cy: p[1], r: 1.7, fill: i === 0 ? 'var(--accent)' : 'var(--ok)', style: '--i:' + i }));
      }
    })();

    /* ── The mat agents walk in over ────────────────────────────────────── */
    world.appendChild(E('polygon', { points: pts([[DOOR.x, ROOM.d + .1], [DOOR.x + 2.3, ROOM.d + .1], [DOOR.x + 2.3, ROOM.d + 1.0], [DOOR.x, ROOM.d + 1.0]]), fill: 'var(--off-m-floor-a)', opacity: .8 }));

    /* ── Desks ──────────────────────────────────────────────────────────── */
    function buildDesk(s) {
      var G = E('g', { class: 'off-desk', 'data-seat':s.num });
      G.appendChild(contact(s.x - .1, s.y - .1, s.w + .2, s.d + .2));
      // Draw supports first, then the desktop; no panels painted over the top.
      [[.14,.12],[s.w-.24,.12],[.14,s.d-.22],[s.w-.24,s.d-.22]]
        .sort(function(a,b){return a[0]+a[1]-b[0]-b[1];})
        .forEach(function(v){G.appendChild(box(s.x+v[0],s.y+v[1],.1,.1,.04,.74,mat('chair'),'off-desk-leg',.55));});
      G.appendChild(box(s.x+s.w-.61,s.y+.23,.44,.73,.4,.3,mat('desk'),'off-drawer',.55));
      G.appendChild(box(s.x,s.y,s.w,s.d,.78,.09,mat('wood'),'off-desktop',.7));
      // A slim monitor with a separate foot and post, above the far edge.
      var mx=s.x+s.w/2-.61,my=s.y+.2,M=E('g',{class:'off-mon'});
      M.appendChild(box(s.cx-.22,my-.05,.44,.32,.87,.035,mat('chair'),null,.45));
      M.appendChild(box(s.cx-.045,my+.015,.09,.08,.9,.2,mat('chair'),null,.45));
      M.appendChild(box(mx,my,1.22,.08,1.06,.62,mat('chair'),'off-monitor-frame',.65));
      M.appendChild(E('polygon',{class:'screen',points:pts([[mx+.05,my+.084,1.11],[mx+1.17,my+.084,1.11],[mx+1.17,my+.084,1.63],[mx+.05,my+.084,1.63]]),fill:'var(--off-monitor)'}));
      var clines=E('g');
      [.77,.58,.42].forEach(function(w,i){
        var z=1.2+i*.12;
        clines.appendChild(E('polygon',{class:'cline',points:pts([[mx+.14,my+.086,z],[mx+.14+w,my+.086,z],[mx+.14+w,my+.086,z+.035],[mx+.14,my+.086,z+.035]]),fill:'var(--accent)',style:'--i:'+i}));
      });
      M.appendChild(clines);
      M.appendChild(E('polygon',{class:'caret',points:pts([[mx+.14,my+.088,1.16],[mx+.18,my+.088,1.16],[mx+.18,my+.088,1.23],[mx+.14,my+.088,1.23]]),fill:'var(--surface)'}));
      G.appendChild(M);
      // Keyboard and mouse are on the same side as the chair, never behind the screen.
      var kx=s.cx-.48,ky=s.y+s.d-.4,K=E('g',{class:'off-keyboard'});
      K.appendChild(box(kx-.06,ky-.04,1.34,.38,.872,.014,mat('desk'),null,.35));
      K.appendChild(box(kx,ky,.93,.28,.89,.025,mat('chair'),null,.45));
      for(var i=0;i<7;i++){
        var key=box(kx+.055+i*.12,ky+.055,.075,.14,.92,.009,mat('desk'),'key',.2);
        key.style.setProperty('--i',i);K.appendChild(key);
      }
      K.appendChild(box(kx+1.07,ky+.06,.14,.21,.89,.04,mat('chair'),'off-mouse',.4));
      G.appendChild(K);
      // Five-spoke base, upholstered seat and back, with open sides and armrests.
      var C=E('g',{class:'off-chair'}),chx=s.cx-.36,chy=s.chY-.34;
      C.appendChild(contact(chx-.08,chy-.08,.88,.88));
      for(var j=0;j<5;j++){
        var angle=j*Math.PI*2/5,wx=s.cx+Math.cos(angle)*.43,wy=s.chY+Math.sin(angle)*.43;
        var foot=px(wx,wy,.07),hub=px(s.cx,s.chY,.12);
        C.appendChild(E('line',{x1:hub[0],y1:hub[1],x2:foot[0],y2:foot[1],stroke:'var(--off-m-chair-b)','stroke-width':2,'stroke-linecap':'round'}));
        C.appendChild(E('ellipse',{cx:foot[0],cy:foot[1]+1,rx:2.2,ry:1.4,fill:'var(--off-m-chair-a)'}));
      }
      C.appendChild(box(s.cx-.065,s.chY-.065,.13,.13,.1,.34,mat('chair'),null,.4));
      C.appendChild(box(chx,chy+.58,.72,.11,.51,.58,mat('chair'),'off-chair-back',.65));
      C.appendChild(box(chx,chy,.72,.68,.44,.1,mat('chair'),'off-chair-seat',.65));
      [-.43,.36].forEach(function(dx){
        C.appendChild(box(s.cx+dx,s.chY-.13,.065,.07,.52,.16,mat('chair'),null,.4));
        C.appendChild(box(s.cx+dx-.015,s.chY-.2,.095,.43,.67,.045,mat('chair'),null,.4));
      });
      var r=px(s.cx,s.chY,.46);
      C.appendChild(E('ellipse',{class:'off-emptyring',cx:r[0],cy:r[1],rx:11,ry:5,fill:'none',stroke:'var(--text-3)','stroke-width':.8,'stroke-dasharray':'2 3',opacity:0}));
      G.appendChild(C);

      /* Selection ring, for the click that ties a seat in the room to its row in
         the roster. Always in the SVG (it is geometry) but only ever visible on
         the selected seat. */
      var c = px(s.cx, s.chY, .05);
      var ring = E('ellipse', { class: 'off-selring', cx: c[0], cy: c[1], rx: 17, ry: 8.5, fill: 'none', stroke: 'var(--accent)', 'stroke-width': 1.6, opacity: 0 });
      G.appendChild(ring);
      s.ring = ring;
      s.emptyring = C.querySelector('.off-emptyring');
      s.mon = M;
      s.clines = clines.querySelectorAll('.cline');
      s.caret = M.querySelector('.caret');
      s.screen = M.querySelector('.screen');
      s.deskG = G;
      s.el = G;
      return G;
    }
    SEATS.forEach(function (s) { world.appendChild(buildDesk(s)); });

    /* ══════════════════════════════════════════════════════════════════════
       The people. One group per seat, built once; a state change is a class swap
       on the wrapper, so the previous state's animation cannot survive it and
       there is exactly one place where "what state is this" is written down.
       ══════════════════════════════════════════════════════════════════════ */
    var PARTS = ['halo', 'glowp', 'ring', 'wlines', 'caret2', 'think', 'mag', 'qmark', 'sand', 'tick', 'plus1', 'bang', 'steam'];
    function buildAgent(s) {
      var g = E('g', { class: 'off-agent', opacity: 0, 'data-seat':s.num });
      g.appendChild(E('title'));
      var shadow = E('ellipse', { class: 'off-shadow', cx: 0, cy: 1, rx: 11, ry: 4.6, fill: 'var(--off-shadow)' });
      var inner = E('g', { class: 'inner' });
      var p = E('g', { class: 'off-person', transform:'scale(1.18)' });

      /* The decorations. Each is a small piece of the same sentence, and only
         one is ever visible — see `applyState`. The CSS class is passed
         separately from the lookup key because two of them share a name with
         something on the desk (`.caret`), and a helper that stamped the key over
         the class would quietly stop the writing caret from blinking. */
      var parts = {};
      function deco(key, node, css) {
        node.setAttribute('class', css || key);
        node.setAttribute('opacity', '0');
        parts[key] = node;
        return node;
      }

      /* ⚠️ A person is the one object in the room that must never merge with the
         furniture behind them, so every part carries the same hairline the desks
         do. The SHIRT is the loudest thing in the room and is chosen by the person
         it belongs to; the legs stay in the room's own neutral so twelve shirts do
         not turn the floor into a paint chart, and so a seated figure still reads
         as a figure when every shirt is a different colour. */
      var pe = { stroke: 'var(--off-edge)', 'stroke-width': .6, 'stroke-linejoin': 'round' };
      function part(tag, at) { for (var k in pe) at[k] = pe[k]; return E(tag, at); }
      var legs = E('g', {class:'stand-legs'});
      legs.appendChild(part('rect', { class: 'leg leg-a', x: -6.4, y: -11, width: 4.6, height: 12, rx: 2.2, fill: 'var(--off-m-chair-a)' }));
      legs.appendChild(part('rect', { class: 'leg leg-b', x: 1.8, y: -11, width: 4.6, height: 12, rx: 2.2, fill: 'var(--off-m-chair-a)' }));
      p.appendChild(legs);
      p.appendChild(E('g', {class:'sit-legs',fill:'none',stroke:'var(--off-m-chair-a)','stroke-width':4.8,'stroke-linecap':'round','stroke-linejoin':'round'}, [
        E('path',{d:'M-4,-12 L-10,-8 L-10,-1 L-6,-1'}),
        E('path',{d:'M3,-12 L10,-8 L10,-1 L14,-1'})
      ]));
      var upper = E('g', {class:'off-upper'});
      upper.appendChild(part('rect', { class: 'torso', x: -7.6, y: -27, width: 15.2, height: 17, rx: 5.4, fill: 'var(--shirt)' }));
      upper.appendChild(E('rect', { class: 'badge', x: -3.4, y: -22, width: 6.8, height: 4.4, rx: 1.6, fill: 'var(--off-fur-pale)' }));
      upper.appendChild(part('rect', { class: 'arm', x: -8.2, y: -25, width: 17, height: 4.4, rx: 2.2, fill: 'var(--shirt-d)' }));
      upper.appendChild(E('g',{class:'typing-hands'},[
        part('rect',{x:1,y:-23,width:13,height:4,rx:2,fill:'var(--shirt-d)'}),
        part('rect',{x:1,y:-18,width:13,height:4,rx:2,fill:'var(--shirt-d)'})
      ]));
      upper.appendChild(E('path', {d:'M-4,-25 L0,-21 L4,-25 M0,-21 L0,-12',fill:'none',stroke:'var(--off-fur-pale)','stroke-width':1.1}));
      upper.appendChild(E('rect', {x:3,y:-18,width:3,height:3,rx:.7,fill:'var(--shirt-d)'}));
      p.appendChild(upper);
      s.upperG = upper;
      /* ⚠️ The head used to be a plain circle with a hair path on it, and the
         circle was filled with the person's identifying hue. Both are gone: a
         circle is not a face, and the hue was a colour nobody chose — the reader
         picks an animal and a shirt, and those are the two things that now say
         who is sitting at that desk.

         ⚠️ The head is built from THIS SEAT's animal, not from a placeholder, and
         the shirt variables are set here rather than in `applyState`. Both were
         wrong at first in a way nothing could see: an empty desk is drawn at
         `opacity: 0`, so twenty-one tigers on twenty-one seats and an unset
         `--shirt` (which makes `fill` fall back to black) are invisible in a
         screenshot — and then a seat whose agent arrives is a tiger in a black
         shirt until something else happens to repaint it. An invisible default
         is still a default, and the fix is to not have one.

         ⚠️ `g`, not `s.agentG`. `s.agentG` is assigned at the END of this function,
         so writing to it here would either throw on the first seat or — worse,
         quietly repaint the PREVIOUS seat's agent, which is a bug that passes
         every assertion because both agents end up wearing the last seat's
         shirt. */
      g.style.setProperty('--shirt', 'var(--off-cloth-' + clothOf(s.avatar && s.avatar.cloth) + ')');
      g.style.setProperty('--shirt-d', 'var(--off-cloth-' + clothOf(s.avatar && s.avatar.cloth) + '-d)');
      upper.appendChild(beastHead(animalOf(s.avatar && s.avatar.animal)));
      s.headG = upper.lastChild;
      s.animal = animalOf(s.avatar && s.avatar.animal);
      s.personG = p;

      /* The decorations. Each is a small piece of the same sentence, and only
         one is ever visible — see `applyState`. */
      var wl = E('g');
      [0, 1, 2].forEach(function (i) {
        var r = E('rect', { class: 'wline', x: -7, y: -60 + i * 4.4, width: 14, height: 1.9, rx: .95, fill: 'var(--c)' });
        r.style.setProperty('--i', i);
        wl.appendChild(r);
      });
      deco('wlines', wl);
      deco('caret2', E('rect', { x: 7, y: -51, width: 1.6, height: 3, fill: 'var(--c)' }), 'caret');

      var think = E('g');
      [0, 1, 2].forEach(function (i) {
        var c = E('circle', { cx: -4 + i * 4, cy: -47, r: 1.9, fill: 'var(--c)' });
        c.style.animation = 'off-breathe 1.15s ease-in-out infinite';
        c.style.animationDelay = (i * 0.14) + 's';
        think.appendChild(c);
      });
      deco('think', think);

      deco('mag', E('g', null, [
        E('circle', { cx: 4, cy: -46, r: 5.4, fill: 'var(--surface)', stroke: 'var(--c)', 'stroke-width': 1.5 }),
        E('path', { d: 'M8,-42l4,4', stroke: 'var(--c)', 'stroke-width': 1.8, 'stroke-linecap': 'round' }),
      ]));
      deco('qmark', E('g', null, [
        E('circle', { cx: 9, cy: -46, r: 6.2, fill: 'var(--surface)', stroke: 'var(--c)', 'stroke-width': 1.3 }),
        E('text', { x: 9, y: -42.8, 'text-anchor': 'middle', 'font-size': 9, 'font-weight': 700, fill: 'var(--c)' }),
      ]));
      parts.qmark.querySelector('text').textContent = '?';
      var sand = E('g', null, [
        E('circle', { cx: 0, cy: -46, r: 6.2, fill: 'var(--surface)', stroke: 'var(--c)', 'stroke-width': 1.3 }),
        E('path', { d: 'M-2.6,-48.6h5.2l-2.6,4.4Z', fill: 'var(--c)' }),
      ]);
      deco('sand', sand);
      deco('tick', E('path', { d: 'M-3.6,-34l2.8,2.9 4.6-5.4', fill: 'none', stroke: 'var(--ok)', 'stroke-width': 2.1, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
      var plus = E('text', { x: 0, y: -46.6, 'text-anchor': 'middle', 'font-size': 8.4, 'font-weight': 700, fill: 'var(--surface)' });
      plus.textContent = '+1';
      deco('plus1', E('g', null, [E('rect', { x: -9, y: -56, width: 18, height: 13, rx: 4, fill: 'var(--ok)' }), plus]));
      var bang = E('text', { x: 0, y: -43.8, 'text-anchor': 'middle', 'font-size': 9.4, 'font-weight': 800, fill: 'var(--danger)' });
      bang.textContent = '!';
      deco('bang', E('g', null, [E('circle', { cx: 0, cy: -47, r: 6.2, fill: 'var(--surface)', stroke: 'var(--danger)', 'stroke-width': 1.4 }), bang]));
      deco('halo', E('circle', { cx: 0, cy: -34, r: 11, fill: 'none', stroke: 'var(--c)', 'stroke-width': 1.6 }));
      deco('glowp', E('circle', { cx: 0, cy: -34, r: 10, fill: 'none', stroke: 'var(--ok)', 'stroke-width': 1.4 }));
      deco('ring', E('circle', { cx: 0, cy: -34, r: 10, fill: 'none', stroke: 'var(--danger)', 'stroke-width': 1.6 }));
      deco('steam', E('path', { d: 'M12,-30c3,-5 -3,-8 0,-12 c3,-4 0,-7 0,-7', fill: 'none', stroke: 'var(--off-monitor-off)', 'stroke-width': 1.1, 'stroke-linecap': 'round' }));

      PARTS.forEach(function (k) { if (parts[k]) inner.appendChild(parts[k]); });
      inner.appendChild(p);
      var chat = E('g',{class:'chat-bubble'},[
        E('path',{d:'M10,-62 h22 a4,4 0 0 1 4,4 v9 a4,4 0 0 1 -4,4 h-12 l-5,5 v-5 h-5 a4,4 0 0 1 -4,-4 v-9 a4,4 0 0 1 4,-4',fill:'var(--surface)',stroke:'var(--off-edge)','stroke-width':.8})
      ]);
      [0,1,2].forEach(function(i){ var dot=E('circle',{class:'chat-dot',cx:15+i*6,cy:-53,r:1.5,fill:'var(--text-2)'}); dot.style.setProperty('--i',i); chat.appendChild(dot); });
      inner.appendChild(chat);
      g.appendChild(shadow);
      g.appendChild(inner);
      world.appendChild(g);

      s.agentG = g;
      s.agentInner = inner;
      s.parts = parts;
      s.shadow = shadow;

      /* ⚠️ The wire from the task queue used to be built here, always at opacity 0
         and only ever raised for a `search` state — and it painted itself with
         `var(--off-st-search)`, a token that no longer exists now that the seven work
         states are gone. That combination is the worst of both: an element in the
         DOM that could never be seen, referencing a variable that resolves to
         nothing, kept alive by two `setAttribute('opacity', '0')` calls that made it
         look maintained. It is deleted rather than hidden: "an answer is coming back
         from retrieval" was the only thing it meant, and nothing on this page
         retrieves anything. */
    }
    SEATS.forEach(buildAgent);

    /* ══════════════════════════════════════════════════════════════════════
       The cards over their heads. HTML, not SVG — see the note in app.css: an
       SVG font in user units is unreadable once the room is scaled to a column.
       ══════════════════════════════════════════════════════════════════════ */
    SEATS.forEach(function (s) {
      /* Three lines, not four: the seat number and the clock share the row with
         the name. Every extra line is height the neighbour card has to be lifted
         clear of, and the lift is what stops a row from reading as a wall. */
      var el = H('button', 'off-label is-off');
      el.type = 'button';
      var n = H('div', 'n');
      n.appendChild(H('em'));
      n.appendChild(H('i', 'ri-time-line'));
      n.appendChild(H('span', 'who'));
      var meta = H('span', 'm');
      n.appendChild(meta);
      var task = H('div', 't');
      var bar = H('div', 'bar');
      var fill = H('i');
      bar.appendChild(fill);
      el.appendChild(n); el.appendChild(task); el.appendChild(bar);
      el._icon = n.querySelector('i');
      el._who = n.querySelector('span');
      el._task = task; el._meta = meta; el._fill = fill;
      el.addEventListener('click', function () { select(s.i); });
      labels.appendChild(el);
      s.label = el;
    });

    /* The card is placed from the projection, as a % of the stage, so it lands on
       the head at any stage size without measuring anything.

       ⚠️ Those %s are a % of the stage, but the drawing inside it is a % of the
       viewBox — and once the stage is capped (see `capStage`) the two are no
       longer the same box, because the SVG letterboxes itself inside the stage.
       Placing against the stage directly slides every card off its head by the
       letterbox offset the moment the cap binds. So the content box is computed
       the same way `preserveAspectRatio="xMidYMid meet"` does it, and the card is
       placed against THAT. When the cap does not bind, k makes cw==stage width and
       ch==stage height, and this reduces to the old formula exactly. */
    function placeLabel(s) {
      if (!VIEW) return;
      /* ⚠️ Against the LIVE viewBox, not the base one. The base is the camera's
         starting point; after a drag or a scroll it is no longer what is on
         screen, and a card placed from it is a card that stayed behind. */
      var v = LIVE || VIEW;
      var sw = stage.clientWidth, sh = stage.clientHeight;
      if (!sw || !sh) return;
      var k = Math.min(sw / v.w, sh / v.h);
      var cw = v.w * k, ch = v.h * k;
      var ox = (sw - cw) / 2, oy = (sh - ch) / 2;
      /* ⚠️ 2.35 world units, and the number is the CARD's requirement rather than
         the head's. The tallest head is the rabbit's, whose ears reach y = -50 in
         the agent's local units — and the card is placed with its BOTTOM edge at
         this point, then grows upward. Anchoring at the old 1.15 put the card's
         bottom edge in the middle of the animal's skull, so every beast drew its
         face behind an opaque card and the six animals were indistinguishable.
         2.35 × 30 = 70.5px of viewBox above the seat, which clears 50 plus the
         14px stalk with room to spare. */
      var head = px(s.motion ? s.motion.x : s.cx, s.motion ? s.motion.y : s.chY, 2.65);
      var x = ox + (head[0] - v.x) / v.w * cw;
      var y = oy + (head[1] - v.y) / v.h * ch;
      s.label.style.left = (x / sw * 100).toFixed(3) + '%';
      s.label.style.top = (y / sh * 100).toFixed(3) + '%';
      /* A card whose head has been panned out of frame is a card lying about
         being there — and left in place it would float over whatever the reader
         panned to. Hidden, like the room behind it. */
      var inside = x > -40 && x < sw + 40 && y > -20 && y < sh + 60;
      s.label.classList.toggle('is-off', !s.agent || !inside);
    }

    /* ══════════════════════════════════════════════════════════════════════
       Presence → the whole picture. One function, called from two places.
       ══════════════════════════════════════════════════════════════════════ */
    function applyState(s) {
      var a = s.agent;
      if (!a) { hide(s); return; }
      var st = PRESENCE[a.state] || PRESENCE.never;

      s.agentG.classList.remove('st-active', 'st-idle', 'st-never');
      s.agentG.classList.add('st-' + a.state);
      s.agentInner.setAttribute('class', 'inner');
      s.agentInner.style.setProperty('--c', st.color);
      /* The shirt, and the head's species. Both are CSS custom properties rather
         than attributes because the head is a GROUP OF PATHS: swapping the
         species means replacing the group, not restyling one node. `setHead` does
         exactly that and is idempotent, so `applyState` may call it on every
         refresh without rebuilding a room that did not change. */
      s.agentG.style.setProperty('--shirt', 'var(--off-cloth-' + a.cloth + ')');
      s.agentG.style.setProperty('--shirt-d', 'var(--off-cloth-' + a.cloth + '-d)');
      setHead(s, a.animal);
      // Keep the whole character opaque: transparency exposes desk geometry
      // through its face and body. Away status uses muted colors, a dark monitor
      // and the presence label instead.
      s.agentG.setAttribute('opacity', '1');
      /* The desk carries the state class too, not just the person: the keyboard
         and the screen are furniture, they live in the desk's group. */
      s.el.setAttribute('class', 'off-desk st-' + a.state);

      // Only fresh, explicit reports enable work-specific decorations.
      PARTS.forEach(function (k) { if (s.parts[k]) s.parts[k].setAttribute('opacity', '0'); });
      var update = latest(s.holder);
      s.el.classList.toggle('has-work', a.state === 'active' && update && !s.holder.work_stale && update.state === 'working');
      if (update && !s.holder.work_stale && a.state === 'active') {
        var partKey = {waiting:'qmark',done:'tick',error:'bang'}[update.state];
        if (partKey && s.parts[partKey]) s.parts[partKey].setAttribute('opacity', '1');
      }
      if (a.state === 'active' && s.parts.glowp) s.parts.glowp.setAttribute('opacity', '1');

      /* Monitor: lit while somebody is at the desk, dim when the agent is not. */
      var work = st.work;
      s.screen.setAttribute('fill', work ? 'var(--off-monitor)' : 'var(--off-monitor-off)');
      Array.prototype.forEach.call(s.clines, function (l) { l.setAttribute('opacity', work ? '1' : '0'); });
      s.caret.setAttribute('opacity', '0');
      s.emptyring.setAttribute('opacity', '0');

      /* The card. Clipping happens AFTER translation, so the ellipsis lands on
         the language the reader is actually looking at. */
      s.label.classList.remove('is-off');
      var workState = latest(s.holder);
      s.label.style.setProperty('--c', workState && !s.holder.work_stale && WORK[workState.state] ? WORK[workState.state].color : st.color);
      s.label._icon.className = workState && !s.holder.work_stale ? ({working:'ri-time-line', waiting:'ri-error-warning-line', done:'ri-checkbox-circle-line', error:'ri-close-circle-line', idle:'ri-time-line'}[workState.state] || st.icon) : st.icon;
      s.label._who.textContent = a.name;
      s.label.title = a.name + ' · ' + workLabel(s.holder);
      s.agentG.querySelector('title').textContent = s.label.title;
      s.label.setAttribute('aria-label', s.label.title);
      s.label._task.textContent = latest(s.holder) ? workLabel(s.holder) : t(st.label);
      s.label._meta.textContent = (s.num < 10 ? '0' : '') + s.num;
      s.label.classList.toggle('is-sel', SEL === s.i);
      placeLabel(s);
    }

    /* Replace the head when the species changes, and do nothing when it does not.
       The guard is the whole point: `applyState` runs on every refresh and on
       every language switch, and rebuilding an identical 20-node group each time
       is how a page that "does nothing on refresh" starts to crawl. */
    function setHead(s, animal) {
      if (s.animal === animal && s.headG) return;
      var next = beastHead(animal);
      if (s.headG && s.headG.parentNode) s.headG.parentNode.replaceChild(next, s.headG);
      else if (s.upperG) s.upperG.appendChild(next);
      s.headG = next;
      s.animal = animal;
    }

    function hide(s) {
      s.agentG.setAttribute('opacity', '0');
      s.agentG.setAttribute('class', 'off-agent');
      s.label.classList.add('is-off');
      s.label.classList.remove('is-sel');
      s.screen.setAttribute('fill', 'var(--off-monitor-off)');
      Array.prototype.forEach.call(s.clines, function (l) { l.setAttribute('opacity', '0'); });
      s.caret.setAttribute('opacity', '0');
      s.emptyring.setAttribute('opacity', '.8');
      s.el.setAttribute('class', 'off-desk');
    }

    var SEL = null;
    // Presence and work stay server-owned; motion only changes the scene pose.
    function place(s) {
      if (s.presence === 'never' || (s.holder && s.holder.revoked)) { s.agent = null; hide(s); return; }
      var a = s.agent;
      if (!a) {
        a = s.agent = {
          name: s.holder ? s.holder.name : 'agent',
          /* ⚠️ The species and the shirt are the ACCOUNT'S, sent by the server
             with the seat. They are not derived from the seat index: a reader who
             picks a tiger must get a tiger whether they are the first person to
             register or the fortieth, and anything computed from the index makes
             the choice move the moment somebody joins. */
          animal: animalOf(s.avatar && s.avatar.animal),
          cloth: clothOf(s.avatar && s.avatar.cloth),
          state: s.presence,
          start: s.holder && s.holder.agent_last_at ? Date.parse(s.holder.agent_last_at) : Date.now(),
          walking: false,
        };
        s.el.setAttribute('class', 'off-desk');
      }
      /* An avatar change made while the tab was open arrives on a refresh, so it
         is read every time rather than only when the agent object is created. */
      if (s.avatar) {
        var an = animalOf(s.avatar.animal), cl = clothOf(s.avatar.cloth);
        if (an !== a.animal || cl !== a.cloth) { a.animal = an; a.cloth = cl; }
      }
      a.state = s.presence;
      a.start = Date.parse(s.holder.agent_last_at) || Date.now();
      if (!s.motion) s.motion = {x:s.cx,y:s.chY,phase:0};
      applyState(s);
    }

    var motionMQ = window.matchMedia('(prefers-reduced-motion: reduce)');
    var motionEnabled = true;
    try { motionEnabled = localStorage.getItem('klado-office-motion') !== 'off'; } catch (_) {}
    var motionFrame = 0, motionLast = 0, labelsLast = 0;
    function isBusy(s) {
      var u = latest(s.holder);
      return s.agent && s.presence === 'active' && !s.holder.work_stale && u &&
        ['working','waiting','error'].indexOf(u.state) !== -1;
    }
    function lane(s) { return s.back ? s.y + 4.12 : s.y - 2.88; }
    function planMotion() {
      var free = SEATS.filter(function(s){return s.agent && !isBusy(s);});
      var paired = new Set();
      SEATS.forEach(function(s){s.companion = null;});
      free.forEach(function(s){
        if (paired.has(s.i)) return;
        var near = free.filter(function(o){return o !== s && !paired.has(o.i) && Math.abs(lane(o)-lane(s)) < .1 && Math.abs(o.cx-s.cx) <= PITCH + .1;})
          .sort(function(a,b){return Math.abs(a.cx-s.cx)-Math.abs(b.cx-s.cx);})[0];
        if (near) {s.companion=near; near.companion=s; paired.add(s.i); paired.add(near.i);}
      });
      SEATS.forEach(function(s){
        if (!s.agent) {s.motion=null; return;}
        var busy=!!isBusy(s), peer=s.companion;
        var key=(busy?'busy':'free')+':' +(peer?peer.i:'solo');
        if (s.motion.key !== key) {
          s.motion.key=key; s.motion.phase=0; s.motion.wait=0;
        }
        s.motion.busy=busy;
        var side=peer && s.i>peer.i ? 1 : -1;
        s.motion.target=peer ? {x:(s.cx+peer.cx)/2+side*1.1,y:lane(s)-side*.5}
          : {x:Math.max(1,Math.min(ROOM.w-1,s.cx+(s.col%2 ? -1 : 1)*1.6)),y:lane(s)};
      });
    }
    function moveToward(m, target, distance) {
      var dx=target.x-m.x,dy=target.y-m.y,len=Math.hypot(dx,dy);
      if (len<=distance) {m.x=target.x; m.y=target.y;return true;}
      m.x+=dx/len*distance;m.y+=dy/len*distance;return false;
    }
    function drawMotion(s, mode) {
      var m=s.motion,p=px(m.x,m.y,0),g=s.agentG;
      g.setAttribute('transform','translate('+p[0].toFixed(2)+','+p[1].toFixed(2)+')');
      g.dataset.motion=mode;
      g.classList.toggle('is-seated',mode==='working'||mode==='seated');
      g.classList.toggle('is-working',mode==='working');
      g.classList.toggle('is-walking',mode==='walking'||mode==='returning');
      g.classList.toggle('is-chatting',mode==='chatting');
      s.label.classList.toggle('is-roaming',motionEnabled&&!motionMQ.matches&&mode!=='working'&&mode!=='seated');
      placeLabel(s);
    }
    // Moving labels yield to faces and other labels. Full details remain in the
    // roster, and the figure's native tooltip retains its name while walking.
    function clearMovingLabels(paused) {
      var occupants=SEATS.filter(function(s){return s.agent;});
      if(paused){occupants.forEach(function(s){s.label.classList.remove('is-crowded');});return;}
      var bodies=occupants.map(function(s){return s.personG.getBoundingClientRect();});
      var placed=[];
      function overlaps(a,b){return a.left<b.right+2&&a.right>b.left-2&&a.top<b.bottom+2&&a.bottom>b.top-2;}
      occupants.sort(function(a,b){return (b.i===SEL)-(a.i===SEL) || Number(b.motion.busy)-Number(a.motion.busy) || a.i-b.i;});
      occupants.forEach(function(s){
        if(s.label.classList.contains('is-off'))return;
        var r=s.label.getBoundingClientRect();
        var crowded=bodies.some(function(b){return overlaps(r,b);})||placed.some(function(b){return overlaps(r,b);});
        s.label.classList.toggle('is-crowded',crowded);
        if(!crowded)placed.push(r);
      });
    }
    function animateMotion(now) {
      motionFrame=0;
      var dt=motionLast ? Math.min((now-motionLast)/1000,.08) : 0;motionLast=now;
      if (onScreen()) {
        var paused=!motionEnabled||motionMQ.matches;
        stage.classList.toggle('motion-paused',paused);
        SEATS.forEach(function(s){
          if (!s.agent||!s.motion) return;
          var m=s.motion,mode='resting';
          if (paused) {m.x=s.cx;m.y=s.chY;mode=m.busy?(latest(s.holder).state==='working'?'working':'seated'):'resting';}
          else if (m.busy) {
            var arrived=moveToward(m,{x:s.cx,y:s.chY},dt*2);
            mode=arrived?(latest(s.holder).state==='working'?'working':'seated'):'returning';
          } else {
            // Aisle waypoints keep characters away from desks and monitors.
            var path=[{x:s.cx,y:lane(s)},m.target,{x:s.cx,y:lane(s)},{x:s.cx,y:s.chY}];
            if (m.phase===2) {
              mode=s.companion?'chatting':'resting';m.wait=(m.wait||0)+dt;
              if(m.wait>5){m.phase=3;m.wait=0;}
            } else if(m.phase===5) {
              m.wait=(m.wait||0)+dt;if(m.wait>3){m.phase=0;m.wait=0;}
            } else {
              var target=path[m.phase<2?m.phase:m.phase-1];
              mode='walking';if(moveToward(m,target,dt*1.1))m.phase++;
            }
          }
          drawMotion(s,mode);
        });
        if(paused||now-labelsLast>100){clearMovingLabels(paused);labelsLast=now;}
      }
      motionFrame=requestAnimationFrame(animateMotion);
    }
    function paintMotionButton() {
      var b=document.getElementById('off-motion');if(!b)return;
      var running=motionEnabled&&!motionMQ.matches;
      b.textContent=t(running?'动画开启 / Motion on':'动画关闭 / Motion off');
      b.setAttribute('aria-pressed',String(running));
      b.disabled=motionMQ.matches;
      b.title=t(motionMQ.matches?'系统已开启减少动态效果 / Reduced motion is enabled by your system':'散步与聊天仅为场景动画 / Walking and chatting are scene animations');
    }
    btn('off-motion',function(){
      motionEnabled=!(motionEnabled&&!motionMQ.matches);
      try{localStorage.setItem('klado-office-motion',motionEnabled?'on':'off');}catch(_){}
      paintMotionButton();
    });
    if(motionMQ.addEventListener)on(motionMQ,'change',paintMotionButton);
    CLEAN.push(function(){if(motionFrame)cancelAnimationFrame(motionFrame);if(rt)cancelAnimationFrame(rt);});

    /* ══════════════════════════════════════════════════════════════════════
       The roster. The scene answers "who is at that desk"; this answers "what is
       everyone doing", which a room seen from above cannot.
       ══════════════════════════════════════════════════════════════════════ */
    var rowsEl = document.getElementById('off-rows');
    var subEl = document.getElementById('off-roster-sub');
    var truncNote = document.getElementById('off-trunc');
    var stats = {
      desks: document.getElementById('off-stat-desks'),
      here: document.getElementById('off-stat-on'),
      came: document.getElementById('off-stat-free'),
      never: document.getElementById('off-stat-done'),
    };
    function paint() {
      /* ⚠️ Every number here counts PEOPLE, and only people. The room is drawn at
         twelve desks whether one account is registered or two hundred, so
         `SEATS.length` is a fact about the DRAWING and using it for "注册的人" put
         12 on a deployment with one account. A spare desk is furniture; it is not
         a person, and it does not belong in any of the three states either — so it
         is excluded here, from the legend, and from the roster.

         `held` is the set of desks that have an account behind them. Everything
         below is computed from `held`, never from `SEATS`. */
      var held = (DATA.seats || []).map(function(h) { return {holder:h,presence:h.presence}; });
      var here = held.filter(function (s) { return s.presence === 'active'; }).length;
      var came = held.filter(function (s) { return s.presence !== 'never'; }).length;
      if (stats.desks) stats.desks.textContent = held.length;
      if (stats.here) stats.here.textContent = here;
      if (stats.came) stats.came.textContent = came;
      if (stats.never) stats.never.textContent = held.length - came;
      if (subEl) subEl.textContent = here + ' / ' + held.length + ' ' + t('近期在线 / Recently online');
      /* The only note that is still true: more registered accounts than the room
         can hold. It used to fire on "the room is smaller than the headcount",
         which with the clamp was the normal case and said nothing. */
      if (truncNote) truncNote.textContent = (data && data.truncated)
        ? t('人数超过房间上限，只画了前 ' + SEATS_MAX + ' 个 / Only the first ' + SEATS_MAX + ' are drawn')
        : (total > VISIBLE
            /* ⚠️ The number goes in the parentheses at the END, never at the start
               of the English half. i18n.js's pair pattern requires the English to
               begin `[A-Za-z]`, so `"11 people have no desk yet"` is not a pair at
               all: the walker leaves the whole string alone and the English page
               renders the Chinese half. The ` / ` and the first English word also
               have to sit in the SAME string literal as the Chinese tail, because
               `test_office_seats.py` scans the source with a regex that cannot see
               across a `+` — a ` / ` that ends a literal reads as a pair with an
               empty English side, and that is the failure this line originally
               shipped with. */
            ? t('还有 ' + (total - VISIBLE) + ' 个人没排到工位 / More people than desks — press “More desks” (' + (total - VISIBLE) + ')')
            : '');
      /* ⚠️ The button's own state is drawn from the SAME two numbers the room is,
         not from a separate flag. A button that stays lit when there is nothing
         left to draw is a button that lies, and the reader finds out by pressing
         it and watching nothing happen — which is indistinguishable from the
         button being broken. */
      var more = document.getElementById('off-more');
      if (more) {
        /* ⚠️ The button is NEVER hidden, and it is never disabled either. It used
           to be hidden whenever `total <= VISIBLE`, which on a deployment with one
           account and a room clamped to the account count meant the control the
           user had asked for was simply not on the page — and it could not have
           been discovered by trying it, because it did not exist. A control that
           may be absent is a control nobody can rely on being there.

           So it always says what pressing it does, and the ONE thing that can stop
           it is the rendering ceiling, where the room genuinely cannot get bigger
           and saying "more" would be a lie. */
        var atMax = VISIBLE >= SEATS_MAX;
        more.hidden = false;
        more.disabled = atMax;
        more.classList.toggle('is-max', atMax);
        /* ⚠️ The space before the slash is load-bearing and the pair pattern is
           why: i18n.js matches `中文 / English` with `\s+\/\s+`, so `）/ More` —
           no space — is NOT a pair, the walker leaves the string alone, and the
           English page renders the Chinese half. The English side must also
           start with a Latin letter, which is why the number goes in the
           parentheses at the end and never opens a side. */
        more.textContent = atMax
          ? t('房间已经是上限了 / This room is as big as it gets')
          : t('再加几张（现在是 ' + VISIBLE + ' 张） / More desks (' + VISIBLE + ' now)');
      }
      if (!rowsEl) return;

      rowsEl.textContent = '';
      paintDetail();
      SEATS.forEach(function (s) {
        /* ⚠️ A desk with no account behind it is SKIPPED, not rendered as a person
           who has not arrived. It is furniture, and the roster is a list of people;
           listing twelve rows of which eleven say "this person's agent has not been
           here yet" would be eleven claims about eleven accounts that do not exist.
           The reader who wants to know the room is bigger than the company is
           looking at the room. */
        if (!s.holder) return;
        var st = PRESENCE[s.presence] || PRESENCE.never;
        var a = s.agent;
        var row = H('button', 'off-row' + (a ? ' on' : ' free') + (SEL === s.i ? ' is-sel' : ''));
        row.style.setProperty('--c', st.color);
        row.setAttribute('type', 'button');

        var no = H('span', 'no off-portrait');
        no.appendChild(thumb(animalOf(s.avatar && s.avatar.animal), clothOf(s.avatar && s.avatar.cloth)));
        var who = H('span', 'who');
        var n = H('span', 'n');
        var tt = H('span', 't');
        n.appendChild(H('em'));
        n.appendChild(document.createTextNode(s.holder.name));
        n.appendChild(H('small', '', t(s.holder.revoked?'授权已撤销 / Revoked':st.label)));
        if (a) {
          /* The one line that is a real clock reading: how long since that agent was
             last seen. Nothing else in the room is a live number. */
          tt.textContent = latest(s.holder) ? latest(s.holder).task : t('尚未上报任务 / No task reported');
        } else {
          tt.textContent = t('这个人的 agent 还没来过 / This account’s agent has not been here yet');
        }
        who.appendChild(n); who.appendChild(tt);

        var r = H('span', 'r');
        var gi = H('i', st.icon);
        r.appendChild(gi);
        row.appendChild(no); row.appendChild(who); row.appendChild(r);
        row.addEventListener('click', function () { select(s.i); });
        rowsEl.appendChild(row);
        s.row = row;
      });
    }

    /* The legend is a live count, not a key: it is the one place that answers
       "how many of each state is in the room right now".
       ⚠️ Counted over desks that HAVE an account, for the same reason the stats
       are: the three states describe an agent at a desk, and a spare desk has no
       agent, so counting it as "never arrived" would report eleven people who do
       not exist. */
    function paintLegend() {
      var el = document.getElementById('off-legend');
      if (!el) return;
      el.textContent = '';
      var held = SEATS.filter(function (s) { return !!s.holder; });
      STATE_ORDER.forEach(function (k) {
        var st = PRESENCE[k];
        var d = H('span', 'off-lg');
        d.style.setProperty('--c', st.color);
        d.appendChild(H('i'));
        d.appendChild(document.createTextNode(t(st.label) + ' '));
        var b = H('b', '', String(held.filter(function (s) { return s.presence === k; }).length));
        d.appendChild(b);
        el.appendChild(d);
      });
      if (MIN_LABEL) {
        var note = H('span', 'off-lg-note');
        /* ⚠️ The English side has to START with a Latin letter. i18n.js's pair
           pattern is `中文 / English`, and the English half must begin `[A-Za-z]`,
           so a pair that opens with a curly quote or a digit is not recognised as a
           pair AT ALL — the walker leaves the whole string alone and the English
           page renders the Chinese half. Caught by audit_untranslatable_pairs.py,
           which is the guard for exactly this and which earns its keep. */
        note.textContent = t('「在用」= 最近 ' + MIN_LABEL + ' 分钟内有过 agent 访问 / An agent call in the last ' + MIN_LABEL + ' min means “at the desk”');
        el.appendChild(note);
      }
    }

    function paintDetail() {
      var target = document.getElementById('off-detail');
      if (!target) return;
      target.textContent = '';
      var selected = SEL !== null ? SEATS[SEL] : null;
      if (!selected || !selected.holder) {
        target.appendChild(H('h3', '', t('工作动态 / Work activity')));
        var all = [];
        (DATA.seats || []).forEach(function(h) { (h.updates || []).forEach(function(u) { all.push({holder:h,update:u}); }); });
        all.sort(function(a,b) {return Date.parse(b.update.created_at)-Date.parse(a.update.created_at);});
        if (!all.length) {
          target.appendChild(H('p','off-empty-copy',t('点选工位，查看 agent 的任务与交付。还没有工作上报，办公室正在等待第一条动态。 / Select a desk to see tasks and deliveries. Waiting for the first work update.')));
        } else all.slice(0,8).forEach(function(item) { eventRow(target,item.update,item.holder.name); });
        return;
      }
      var h = selected.holder, u = latest(h);
      var hero = H('div','off-profile');
      hero.appendChild(thumb(animalOf(h.avatar && h.avatar.animal),clothOf(h.avatar && h.avatar.cloth)));
      var bio = H('div');
      bio.appendChild(H('h3','',h.name));
      bio.appendChild(H('p','',t('工位 / Desk') + ' ' + ('0'+selected.num).slice(-2) + ' · ' + t(PRESENCE[h.presence].label) + (h.agent_last_at ? ' · ' + agoLabel(Date.now()-Date.parse(h.agent_last_at)) : '')));
      hero.appendChild(bio); target.appendChild(hero);
      if(h.agent_id && h.editable){
        var edit=H('button','btn btn-ghost',t('编辑 Agent / Edit agent'));
        edit.type='button';edit.addEventListener('click',function(){if(window.KladoTown)window.KladoTown.editAgent(h);});
        target.appendChild(edit);
      }
      if(h.revoked)target.appendChild(H('p','off-empty-copy',t('授权已撤销，保留历史记录 / Access revoked; history retained')));
      if(h.legacy)target.appendChild(H('p','off-empty-copy',t('旧接入记录；请使用独立授权码区分新 Agent / Legacy connection. Use separate codes for new agents.')));

      var status = H('span','off-work-status',workLabel(h));
      status.style.setProperty('--c',u && WORK[u.state] ? WORK[u.state].color : 'var(--text-3)');
      target.appendChild(status);
      if (h.work_stale && h.presence === 'active') target.appendChild(H('p','off-empty-copy',t('任务状态已过期，等待新的进展上报。 / Task update is stale; waiting for fresh progress.')));
      target.appendChild(H('h4','off-task-title',u ? u.task : t('等待任务上报 / Waiting for a task update')));
      if (u && u.summary) target.appendChild(H('p','off-work-summary',u.summary));
      if (h.presence !== 'active' && u) target.appendChild(H('p','off-empty-copy',t('agent 已离线，以上是最后一次上报。 / Agent is away; this is its last reported update.')));
      target.appendChild(H('h4','off-history-title',t('最近记录 / Recent updates')));
      if (!u) target.appendChild(H('p','off-empty-copy',t('接入后可上报任务、进展与完成摘要。 / Agents can report tasks, progress and completion summaries.')));
      (h.updates || []).forEach(function(event) {eventRow(target,event);});
    }
    function eventRow(target,u,name) {
      var row = H('div','off-event');
      var state = WORK[u.state] || WORK.idle;
      row.style.setProperty('--c',state.color);
      row.appendChild(H('span','off-event-time',t(state.label)+' · '+agoLabel(Date.now()-Date.parse(u.created_at))));
      row.appendChild(H('strong','', (name ? name+' · ' : '')+u.task));
      if (u.summary) row.appendChild(H('p','',u.summary));
      target.appendChild(row);
    }

    function select(i) {
      SEL = (SEL === i) ? null : i;
      SEATS.forEach(function (s) {
        var on = SEL === s.i;
        s.ring.setAttribute('opacity', on ? '.9' : '0');
        s.label.classList.toggle('is-sel', on);
      });
      paint();
    }

    /* ══════════════════════════════════════════════════════════════════════
       Controls
       ══════════════════════════════════════════════════════════════════════ */
    var vp = { z: 1, ox: 0, oy: 0, base: null, drag: false, sx: 0, sy: 0, b0x: 0, b0y: 0 };
    /* ⚠️ This used to set the viewBox and stop. The cards are HTML positioned in
       % of the STAGE, and `placeLabel` resolves that % through `VIEW` — the BASE
       viewBox. Panning and zooming change the LIVE viewBox and nothing recomputed
       the cards, so the room slid out from under every card the moment the reader
       dragged: the dashboards drifted away from the heads they belonged to and
       eventually off the stage entirely.

       It passed the card-alignment assertion because that assertion never dragged.
       "The card is above its agent" is a statement about a moment, and the only
       moment it was ever checked was the one where the two boxes are computed from
       the same number.

       So the live view is recorded here and every card is re-placed from it. */
    /* HTML cards must follow the SVG scale as the room grows. Constrain their
       measured layout height to the projected seat step with a 3px gap, and cap
       their scale at the live pixels per viewBox unit to clear neighboring
       characters. The transform keeps the bottom-center anchor fixed when scaled. */
    function fitCards() {
      var cards = labels.querySelectorAll('.off-label');
      if (!cards.length) return;
      var h = cards[0].offsetHeight || 51;
      var v = LIVE || VIEW;
      if (!v) return;
      var sw = stage.clientWidth, sh = stage.clientHeight;
      if (!sw || !sh) return;
      var perUnit = Math.min(sw / v.w, sh / v.h);
      var stepPx = PITCH * CY * U * perUnit;
      // Cards must also fit the scene scale. A fixed 0.6 minimum covered
      // neighboring figures in large rooms; zooming restores readable detail.
      var s = Math.max(0.25, Math.min(1, perUnit, (stepPx - 3) / h));
      stage.style.setProperty('--off-card-scale', s.toFixed(3));
    }

    function applyView() {
      if (!VIEW || !vp.base) return;
      var w = vp.base.w / vp.z, h = vp.base.h / vp.z;
      var cx = vp.base.x + vp.base.w / 2 + vp.ox, cy = vp.base.y + vp.base.h / 2 + vp.oy;
      var x = cx - w / 2, y = cy - h / 2;
      svg.setAttribute('viewBox', x.toFixed(1) + ' ' + y.toFixed(1) + ' ' + w.toFixed(1) + ' ' + h.toFixed(1));
      LIVE = { x: x, y: y, w: w, h: h };
      SEATS.forEach(function (s) { if (s.agent) placeLabel(s); });
      fitCards();
    }
    /* The viewBox actually on screen, which is not the base one after a pan. Kept
       separate from `VIEW` because `VIEW` still answers "what is the room", and
       several places legitimately want the room's own frame rather than the
       camera's. */
    var LIVE = null;
    on(stage, 'wheel', function (e) {
      e.preventDefault();
      vp.z = Math.max(.65, Math.min(3.4, vp.z * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
      applyView();
    }, { passive: false });
    on(stage, 'pointerdown', function (e) {
      vp.drag = true; vp.sx = e.clientX; vp.sy = e.clientY; vp.b0x = vp.ox; vp.b0y = vp.oy;
      stage.classList.add('dragging');
      if (stage.setPointerCapture) stage.setPointerCapture(e.pointerId);
    });
    on(stage, 'pointermove', function (e) {
      if (!vp.drag || !vp.base) return;
      var r = stage.getBoundingClientRect();
      var k = (vp.base.w / vp.z) / r.width;
      vp.ox = vp.b0x - (e.clientX - vp.sx) * k;
      vp.oy = vp.b0y - (e.clientY - vp.sy) * k;
      applyView();
    });
    on(window, 'pointerup', function () { vp.drag = false; stage.classList.remove('dragging'); });
    on(stage, 'click', function (e) {
      if (e.target.closest && e.target.closest('.off-label')) return;
      var s = null, hit = e.target.closest ? e.target.closest('.off-desk') : null;
      for (var i = 0; i < SEATS.length; i++) {
        var g = SEATS[i].agentG;
        if (e.target === g || g.contains(e.target) || hit === SEATS[i].el) { s = SEATS[i]; break; }
      }
      /* Nothing under the pointer that belongs to a seat — a click on the wall or
         the floor is not a request to do anything. An earlier version fell back to
         "the first free desk", which meant clicking the back wall teleported an
         agent into seat 1. */
      if (!s) return;
      /* ⚠️ A desk is selected, never filled. There is no "put somebody here"
         action any more and there must not be one: the user asked for agents to
         take a desk by turning up, not by being placed. */
      select(s.i);
    });

    function btn(id, fn) { var el = document.getElementById(id); if (el) on(el, 'click', fn); }
    btn('off-refresh', function () { loadSeats(true); });
    btn('off-zoom-in', function () { vp.z = Math.min(2.5, vp.z * 1.2); applyView(); });
    btn('off-zoom-out', function () { vp.z = Math.max(.65, vp.z / 1.2); applyView(); });
    function overview() {vp.z=1;vp.ox=0;vp.oy=0;applyView();}
    function focusOffice() {
      var occupied=SEATS.filter(function(s){return s.holder;}).slice(0,6);
      vp.z=narrowMQ.matches?1.25:(occupied.length<=3?2.15:1.7);
      if(occupied.length){
        var center=occupied.reduce(function(a,s){var p=px(s.cx,s.chY,1.1);return [a[0]+p[0],a[1]+p[1]];},[0,0]);
        vp.ox=center[0]/occupied.length-(VIEW.x+VIEW.w/2);
        vp.oy=center[1]/occupied.length-(VIEW.y+VIEW.h/2);
      }else{vp.ox=0;vp.oy=0;vp.z=1.35;}
      applyView();
    }
    btn('off-reset', focusOffice);
    btn('off-overview', overview);
    /* The button that makes the room bigger. It is a REBUILD, so the honest thing
       is to rebuild — and the ONLY thing that can stop it is `SEATS_MAX`. It does
       not consult the account count: the reader is asking for more ROOM, and a
       deployment with one registered account is exactly the case where a room of
       one desk looked broken. The number it grows by is `SEATS_STEP`, and the
       label above says how big the room currently is. */
    btn('off-more', function () {
      if (VISIBLE >= SEATS_MAX) return;
      boot(DATA, Math.min(SEATS_MAX, VISIBLE + SEATS_STEP));
    });

    // Polling changes reported presence and tasks; the animation loop only moves figures.
    function onScreen() {
      if (document.hidden) return false;
      return stage.getBoundingClientRect().width > 0;
    }
    TICK = setInterval(function () {
      if (!onScreen()) return;
      loadSeats(true);
      SEATS.forEach(function (s) {
        if (!s.agent) return;
        s.label._meta.textContent = (s.num < 10 ? '0' : '') + s.num;
        /* The bar is presence, not progress: full means "seen within the window",
           and it never drains, because a stale bar that empties over time would be
           a second, invented clock. */
        s.label._fill.style.width = s.presence === 'active' ? '100%' : '0%';
      });
      paintLegend();
    }, 30000);

    /* Language. The cards and rows are built in JS, so the DOM walker never sees
       them; without this a language switch would leave the room in the old
       language while the rest of the app moved. */
    on(document, 'klado:lang', function () {
      SEATS.forEach(function (s) { if (s.agent) applyState(s); });
      paint();
      paintLegend();
      paintLoad();
      paintMotionButton();
    });
    if (narrowMQ.addEventListener) on(narrowMQ, 'change', function () { fitViewBox(); applyView(); });

    /* ⚠️ There was no resize handler at all, which was invisible while the stage
       and the viewBox always had the same ratio — both just scaled together. It is
       no longer invisible: the cap makes the stage SHORTER than the viewBox wants,
       so the letterbox offset depends on the stage's real size, and a resize would
       leave every card parked where the old box put it. Coalesced to a frame, and
       it re-runs `applyView` so a pan or zoom the reader had set survives — the
       resize moves the frame, it does not reset the reader's place in it. */
    var rt = null;
    on(window, 'resize', function () {
      if (rt) return;
      rt = requestAnimationFrame(function () {
        rt = null;
        fitViewBox();
        if (vp.base) { vp.base = VIEW; }
        applyView();
      });
    });

    /* ── Go ────────────────────────────────────────────────────────────── */
    fitViewBox();
    vp.base = VIEW;
    focusOffice();
    SEATS.forEach(function (s) { hide(s); });
    // Place server-owned occupants, then start the independent ambient animation.
    SEATS.forEach(place);
    planMotion();
    paint();
    paintLegend();
    paintMotionButton();
    motionFrame=requestAnimationFrame(animateMotion);
    /* ⚠️ After `place`, not before: the cards do not exist until the seated ones
       have been built, and `fitCards` measures a real card's layout height. Called
       earlier it finds none, leaves the scale at its previous value, and the room
       renders with cards sized for a room that is no longer the one on screen. */
    fitCards();

    /* ══════════════════════════════════════════════════════════════════════
       The contract, reduced to what is still true. The old four functions were
       written for a simulated room: `setAgent` seated somebody, `setState` changed
       what they were working on, `clearAgent` sent them home. None of those are
       things a desk does any more — a desk belongs to an account, and the agent on
       it is a consequence of that account's agent credential having been used.

       What remains is read-and-refresh: `get()` is the room as data (which is how
       the tests assert against it), and `refresh()` re-reads the seat list, which is
       the only way presence can change. It is exposed rather than fired on a timer
       because polling a landing page every few seconds is a cost the product has not
       asked for; when a real agent-heartbeat lands, this is the one call it needs.
       ══════════════════════════════════════════════════════════════════════ */
    window.AgentOffice = {
      get: function () {
        return SEATS.map(function (s) {
          return {
            seat: s.num,
            holder: s.holder ? s.holder.name : null,
            presence: s.presence,
            agent: !!s.agent,
            /* ⚠️ The species and the shirt are in the public read API, not just in
               the drawing. They are the two things a reader chose, they are what
               identifies a face in a screenshot, and a test that can only see
               "there is an svg in the agent" cannot tell a tiger from a cat. */
            animal: s.agent ? s.agent.animal : (animalOf(s.avatar && s.avatar.animal)),
            cloth: s.agent ? s.agent.cloth : (clothOf(s.avatar && s.avatar.cloth)),
            last_seen: s.holder ? s.holder.agent_last_at : null,
          };
        });
      },
      refresh: function () { return loadSeats(true); },
    };

    /* Registered here, not at module level, because only this closure knows how the
       room was sized. A refresh re-seats the EXISTING desks, and the room only
       needs redrawing when the data has outgrown it — i.e. there are now more
       registered accounts than desks drawn, which would leave somebody without a
       desk on a page that promised every account one.

       ⚠️ This used to be `list.length !== SEATS.length`, which was written when
       the room WAS the account count. With twelve desks always drawn that test is
       true on every single refresh, so a refresh rebuilt the whole room every
       time and threw away the reader's pan, zoom and chosen room size. The
       comparison that means something is the one direction that matters: is the
       room big enough? A shorter list is not a reason to shrink anything. */
    REPAINT = function (data) {
      var selectedId = SEL !== null && SEATS[SEL].holder ? (SEATS[SEL].holder.agent_id || SEATS[SEL].holder.user_id) : null;
      DATA = data;
      var list = (data && data.seats) || [];
      if (list.length > SEATS.length && SEATS.length < SEATS_MAX) {
        /* More people than desks. Growing to fit is not a preference here — the
           room has to hold the company — and the reader's own choice is kept as
           the floor, so a reader who had already asked for a bigger room does not
           lose it. */
        boot(data, Math.max(VISIBLE, list.length));
        return true;
      }
      SEL = selectedId === null ? null : list.findIndex(function(h) {return (h.agent_id || h.user_id) === selectedId;});
      if (SEL < 0 || SEL >= SEATS.length) SEL = null;
      SEATS.forEach(function (s, idx) {
        s.ring.setAttribute('opacity', SEL === s.i ? '.9' : '0');
        var holder = list[idx];
        if (!holder) { s.holder = null; s.presence = 'never'; s.agent = null; place(s); return; }
        s.holder = holder;
        s.presence = holder.presence || 'never';
        s.avatar = holder.avatar || null;
        if (s.agent) s.agent.name = holder.name;
        /* ⚠️ `place(s)` for EVERY seat, not only for the ones without an agent.
           This used to be `else if (s.presence !== 'never') place(s)` followed by a
           separate `applyState(s)`, which meant a seat that already had somebody
           on it never went back through the function that reads the avatar — so
           choosing a new animal updated the picker, the database and the seat list
           and left the figure at the desk wearing the old one. `place()` is
           already the whole of "make this seat agree with the data": it hides an
           empty desk, creates the agent when one is due, re-reads the species and
           the shirt, and repaints. Calling it twice is free. */
        place(s);
      });
      planMotion();
      paint();
      paintLegend();
      return true;
    };
  }

  var REPAINT = null;

  /* ⚠️ The seat list is fetched, not assumed, and a failure is visible rather than
     silently empty: an empty office and a broken office look identical otherwise,
     and "everybody's agent vanished" is exactly the message a failed request would
     send on its own. `boot` runs on the failure path too, with an empty list, so the
     room is still drawn and the reader sees a real (empty) room rather than a page
     that failed to finish loading. */
  var LOAD_OK = null;
  var LOAD_SERIAL = 0;
  function paintLoad() {
    var el = document.getElementById('off-load');
    if (el && LOAD_OK !== null) el.textContent = LOAD_OK
      ? t('实时同步 · 每 30 秒 / Live sync · every 30s')
      : t('同步失败，显示上次数据 · 点击刷新重试 / Sync failed; showing last data. Refresh to retry');
  }
  function loadSeats(repaint) {
    var serial = ++LOAD_SERIAL;
    var controller = new AbortController();
    var timeout = setTimeout(function() {controller.abort();}, 10000);
    return fetch(appAbsUrl('api/auth/office-agents'), { credentials: 'same-origin', signal: controller.signal })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status)); })
      .then(function (data) {
        if (serial !== LOAD_SERIAL) return null;
        if (data && data.active_window_min) MIN_LABEL = data.active_window_min;
        LOAD_OK = true; paintLoad();
        document.dispatchEvent(new CustomEvent('klado:office-data',{detail:data}));
        if (repaint && data && REPAINT) REPAINT(data);
        return data;
      })
      .catch(function () {
        if (serial === LOAD_SERIAL) { LOAD_OK = false; paintLoad(); }
        return null;
      }).finally(function() {clearTimeout(timeout);});
  }

  loadSeats(false).then(function (data) { boot(data || { seats: [] }); });

  /* ══════════════════════════════════════════════════════════════════════════
     The picker in the user menu.
     ══════════════════════════════════════════════════════════════════════════
     ⚠️ It lives HERE rather than in index.html, and that is a decision with a
     cost on both sides. The cost on this side: the menu is markup in index.html
     and this file is a feature of one page, so a reader opening the user menu on
     the Knowledge page is running office.js for a picker. The cost on the other
     side — which is the one that decided it — is that the six thumbnails are
     drawn by the SAME `beastHead()` the room uses. A hand-written second tiger in
     index.html is a tiger that is subtly wrong in a way no test compares, and the
     reader only ever sees one of the two copies at a time, so they can never tell
     which one the desk is showing. One drawing function, two sizes, no drift.

     ⚠️ The thumbnails reuse the real head at real scale inside a `viewBox` rather
     than being drawn small. A second, smaller drawing is the drift this whole
     arrangement exists to prevent. */
  function thumb(kind, cloth) {
    /* The box has to hold the TALLEST head, not a typical one: the rabbit's ears
       reach y=-50 and the hippo's muzzle reaches y=-24, and a viewBox fitted to
       the average one clips the rabbit — which is then an animal with no ears,
       i.e. a cat, in the one place the reader is choosing. */
    var svg = E('svg', { viewBox: '-20 -58 40 53', class: 'au-avatar-thumb', 'aria-hidden': 'true' });
    svg.style.setProperty('--shirt', 'var(--off-cloth-' + cloth + ')');
    svg.style.setProperty('--shirt-d', 'var(--off-cloth-' + cloth + '-d)');
    svg.appendChild(E('rect', { x: -8, y: -26, width: 16, height: 17, rx: 5.4, fill: 'var(--shirt)', stroke: 'var(--off-edge)', 'stroke-width': .6 }));
    svg.appendChild(E('rect', { x: -9, y: -24, width: 18, height: 4.4, rx: 2.2, fill: 'var(--shirt-d)', stroke: 'var(--off-edge)', 'stroke-width': .6 }));
    /* ⚠️ The head keeps its `off-beast` class inside the thumbnail too, so "is
       this the same animal as the one at that desk" is a question about one
       selector rather than two, and a test can compare the two directly. */
    var head = beastHead(kind);
    head.setAttribute('class', 'off-beast');
    svg.appendChild(head);
    return svg;
  }

  var MINE = { animal: ANIMALS[0].key, cloth: CLOTHS[1].key };
  var PICKER = null, SAVING = false, PICKER_STATUS = '', RETRY = null;

  function paintPicker() {
    if (!PICKER) return;
    document.getElementById('au-avatar-title').textContent=t('新 Agent 默认形象 / Default avatar for new agents');
    document.getElementById('au-cloth-title').textContent=t('衣服颜色 / Shirt color');
    document.getElementById('au-avatar-animals').setAttribute('aria-label',t('选择形象 / Choose an avatar'));
    document.getElementById('au-avatar-cloths').setAttribute('aria-label',t('选择衣服颜色 / Choose a shirt color'));
    PICKER.host.setAttribute('aria-busy',String(SAVING));
    PICKER.animals.forEach(function(b,i){
      var a=ANIMALS[i],on=a.key===MINE.animal;
      b.classList.toggle('is-on',on);b.setAttribute('aria-pressed',String(on));
      b.disabled=SAVING;b.title=t(a.label);b.setAttribute('aria-label',t(a.label));
      b.querySelector('span').textContent=t(a.label);
      var svg=b.querySelector('svg');
      svg.style.setProperty('--shirt','var(--off-cloth-'+MINE.cloth+')');
      svg.style.setProperty('--shirt-d','var(--off-cloth-'+MINE.cloth+'-d)');
    });
    PICKER.cloths.forEach(function(b,i){
      var c=CLOTHS[i],on=c.key===MINE.cloth;
      b.classList.toggle('is-on',on);b.setAttribute('aria-pressed',String(on));
      b.disabled=SAVING;b.title=t(c.label);b.setAttribute('aria-label',t(c.label));
      b.querySelector('span').textContent=t(c.label);
    });
    var messages={saving:'正在保存… / Saving…',saved:'已保存，新 Agent 将使用此默认形象 / Saved. New agents will use this default avatar.',
      error:'保存失败，请重试 / Could not save. Please retry.',
      auth:'登录已过期，请重新登录 / Session expired. Please sign in again.',
      denied:'无法保存，请重新登录后重试 / Unable to save. Sign in again and retry.'};
    var status=document.getElementById('au-avatar-status');
    status.textContent=t(messages[PICKER_STATUS]||'点击即保存 / Changes save automatically');
    status.classList.toggle('is-error',['error','auth','denied'].indexOf(PICKER_STATUS)!==-1);
    var retry=document.getElementById('au-avatar-retry');
    retry.hidden=!RETRY||SAVING;retry.textContent=t('重试 / Retry');
  }

  async function choose(animal,cloth) {
    if(SAVING||!PICKER||(animal===MINE.animal&&cloth===MINE.cloth))return;
    var was={animal:MINE.animal,cloth:MINE.cloth};
    MINE={animal:animal,cloth:cloth};SAVING=true;RETRY=null;PICKER_STATUS='saving';paintPicker();
    var controller=new AbortController(),timer=setTimeout(function(){controller.abort();},10000);
    try {
      var r=await fetch(appAbsUrl('api/auth/me/avatar'),{
        method:'PATCH',credentials:'same-origin',signal:controller.signal,
        headers:{'Content-Type':'application/json'},body:JSON.stringify(MINE)
      });
      if(!r.ok){var err=new Error('HTTP '+r.status);err.status=r.status;throw err;}
      var u=await r.json();
      MINE={animal:animalOf(u.avatar_animal),cloth:clothOf(u.avatar_cloth)};
      // renderMenu reads this object again on reopen: keep it in sync with the save.
      if(window.__authUser){window.__authUser.avatar_animal=MINE.animal;window.__authUser.avatar_cloth=MINE.cloth;}
      PICKER_STATUS='saved';
      if(window.AgentOffice)window.AgentOffice.refresh();
    } catch(err) {
      MINE=was;RETRY={animal:animal,cloth:cloth};
      PICKER_STATUS=err.status===401?'auth':err.status===403?'denied':'error';
    } finally {clearTimeout(timer);SAVING=false;paintPicker();}
  }

  function mountPicker() {
    var host=document.getElementById('au-avatar');if(!host||PICKER)return;
    PICKER={host:host,animals:[],cloths:[]};
    ANIMALS.forEach(function(a){
      var b=document.createElement('button');b.type='button';b.className='au-avatar-cell';
      b.appendChild(thumb(a.key,MINE.cloth));b.appendChild(document.createElement('span'));
      b.addEventListener('click',function(){choose(a.key,MINE.cloth);});
      document.getElementById('au-avatar-animals').appendChild(b);PICKER.animals.push(b);
    });
    CLOTHS.forEach(function(c){
      var b=document.createElement('button');b.type='button';b.className='au-cloth-cell';
      b.style.setProperty('--c','var(--off-cloth-'+c.key+')');
      var swatch=document.createElement('i');swatch.setAttribute('aria-hidden','true');b.appendChild(swatch);
      b.appendChild(document.createElement('span'));
      b.addEventListener('click',function(){choose(MINE.animal,c.key);});
      document.getElementById('au-avatar-cloths').appendChild(b);PICKER.cloths.push(b);
    });
    document.getElementById('au-avatar-retry').addEventListener('click',function(){if(RETRY)choose(RETRY.animal,RETRY.cloth);});
    paintPicker();
  }

  function applyMine(user) {
    if(SAVING)return;
    MINE={animal:animalOf(user&&user.avatar_animal),cloth:clothOf(user&&user.avatar_cloth)};
    paintPicker();
  }
  document.addEventListener('klado:lang',paintPicker);

  window.AgentOfficeAvatar = { apply: applyMine, mount: mountPicker, ANIMALS: ANIMALS, CLOTHS: CLOTHS, thumbnail: thumb };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mountPicker);
  else mountPicker();

})();
