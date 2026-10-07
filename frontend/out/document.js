/* ════════════════════════════════════════════════════════════════════════════
   document.js — helpers for standalone agent documents (`/d/<slug>`).

   Loaded by the server next to `theme.js` and `document.css`. It provides the
   three things every dashboard needs and no author should re-invent: number
   formatting, a table renderer, and four charts that read the design tokens.

   ⚠️ Charts read their colours AT CONSTRUCTION, so a theme switch cannot repaint
   them by itself. Everything here therefore redraws on `klado:theme` — the event
   `theme.js` dispatches. A dashboard that ignores it keeps light-theme colours on a
   dark page, which is the one chart bug that is visible from across the room.
   ════════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var SERIES = ['var(--c1)', 'var(--c2)', 'var(--c3)', 'var(--c4)', 'var(--c5)', 'var(--c6)'];

  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === 'class') n.className = attrs[k];
      else if (k === 'text') n.textContent = attrs[k];
      else if (k === 'html') n.innerHTML = attrs[k];
      else if (attrs[k] != null) n.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }

  function num(v) { return typeof v === 'number' ? v : parseFloat(String(v == null ? '' : v).replace(/[, ]/g, '')); }

  /* ── formatting ───────────────────────────────────────────────────────────
     Compact by default: a KPI that reads 856564 is a data dump, one that reads
     856.6K is a figure. `kldFmt.full` is there for when the exact number matters. */
  var fmt = {
    compact: function (v, digits) {
      var n = num(v); if (n == null || isNaN(n)) return '—';
      var a = Math.abs(n), d = digits == null ? 1 : digits;
      if (a >= 1e9) return (n / 1e9).toFixed(d) + 'B';
      if (a >= 1e6) return (n / 1e6).toFixed(d) + 'M';
      if (a >= 1e4) return (n / 1e3).toFixed(0) + 'K';
      if (a >= 1e3) return n.toLocaleString('en-US');
      return String(Math.round(n * 100) / 100);
    },
    full: function (v) {
      var n = num(v); if (n == null || isNaN(n)) return '—';
      return n.toLocaleString('en-US', { maximumFractionDigits: 2 });
    },
    /* Percent from a RATIO (0.386) or from an already-scaled value (38.6). The
       threshold is 1.5, so a genuine 140% growth rate is not mistaken for a ratio. */
    pct: function (v, digits) {
      var n = num(v); if (n == null || isNaN(n)) return '—';
      var s = Math.abs(n) <= 1.5 ? n * 100 : n;
      return (s >= 0 ? '' : '−') + Math.abs(s).toFixed(digits == null ? 1 : digits) + '%';
    },
    money: function (v, unit) {
      var n = num(v); if (n == null || isNaN(n)) return '—';
      return (n < 0 ? '−' : '') + '¥' + fmt.compact(Math.abs(n)) + (unit || '');
    },
    signed: function (v, render) {
      var n = num(v); if (n == null || isNaN(n)) return '—';
      return (n > 0 ? '+' : n < 0 ? '−' : '') + render(Math.abs(n));
    }
  };

  /* The sign class is the whole point of a delta, so it is one function rather than
     three call sites that each get the threshold wrong. */
  function deltaClass(v) {
    var n = num(v); if (n == null || isNaN(n) || n === 0) return 'kld-flat';
    return n > 0 ? 'kld-up' : 'kld-down';
  }

  /* ── table ────────────────────────────────────────────────────────────────
     `cols` = [{key, label, num?, render?}]. A missing `render` prints the raw
     value; pass one for anything that needs a unit or a colour. */
  function table(host, rows, cols, opts) {
    var o = opts || {};
    if (!rows || !rows.length) {
      host.replaceChildren(el('div', { class: 'kld-empty', text: o.empty || '没有数据 / No data' }));
      return;
    }
    var thead = el('thead', null, [el('tr', null, cols.map(function (c) {
      return el('th', { class: c.num ? 'num' : '', text: c.label });
    }))]);
    var tbody = el('tbody', null, rows.map(function (r) {
      return el('tr', null, cols.map(function (c) {
        var raw = r[c.key];
        var v = c.render ? c.render(raw, r) : raw;
        var td = el('td', { class: c.num ? 'num' : (c.head ? 'rowhead' : '') });
        if (v && v.nodeType) td.appendChild(v); else td.textContent = (v == null || v === '') ? '—' : v;
        return td;
      }));
    }));
    var t = el('table', { class: 'kld-table' }, [thead, tbody]);
    if (o.foot) t.appendChild(el('tfoot', null, [el('tr', null, cols.map(function (c) {
      var v = o.foot[c.key];
      return el('td', { class: c.num ? 'num' : '', text: v == null ? '' : v });
    }))]));
    host.replaceChildren(t);
  }

  /* ── horizontal bars ────────────────────────────────────────────────────
     `rows` = [{label, value, color?, suffix?}]. Bars are scaled to the LARGEST
     value, not to the sum: a share-of-total chart and a "rank these" chart are
     different questions and the same pixels answer them differently. */
  function bars(host, rows, opts) {
    var o = opts || {};
    if (!rows || !rows.length) { host.replaceChildren(el('div', { class: 'kld-empty', text: o.empty || '没有数据 / No data' })); return; }
    var max = Math.max.apply(null, rows.map(function (r) { return Math.abs(num(r.value) || 0); })) || 1;
    host.replaceChildren(el('div', { class: 'kld-bars' }, rows.map(function (r, i) {
      var v = num(r.value) || 0;
      return el('div', { class: 'kld-bar-row' }, [
        el('div', { class: 'name', text: r.label, title: r.label }),
        el('div', { class: 'kld-track' }, [el('div', {
          class: 'kld-fill ' + (r.color || ('c' + ((i % 6) + 1))),
          style: 'width:' + Math.max(1.5, (Math.abs(v) / max) * 100).toFixed(2) + '%'
        })]),
        el('div', { class: 'val', text: (o.format ? o.format(v) : fmt.compact(v)) + (r.suffix || '') })
      ]);
    })));
  }

  /* ── signed bars ────────────────────────────────────────────────────────
     For "who is down": the sign is the finding, and one --c1 scale hides it. */
  function sbars(host, rows, opts) {
    var o = opts || {};
    if (!rows || !rows.length) { host.replaceChildren(el('div', { class: 'kld-empty', text: o.empty || '没有数据 / No data' })); return; }
    var max = Math.max.apply(null, rows.map(function (r) { return Math.abs(num(r.value) || 0); })) || 1;
    host.replaceChildren(el('div', { class: 'kld-sbars' }, rows.map(function (r) {
      var v = num(r.value) || 0, w = (Math.abs(v) / max) * 50;
      return el('div', { class: 'kld-sbar-row' }, [
        el('div', { class: 'name', text: r.label, title: r.label }),
        el('div', { class: 'kld-sbar' }, [el('i', {
          class: v < 0 ? 'neg' : 'pos',
          style: v < 0 ? 'right:50%;width:' + w.toFixed(2) + '%'
                        : 'left:50%;width:' + w.toFixed(2) + '%'
        })]),
        el('div', { class: 'val ' + deltaClass(v), text: o.format ? o.format(v) : fmt.pct(v) })
      ]);
    })));
  }

  var NS = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs) {
    var n = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }

  /* ── donut ───────────────────────────────────────────────────────────────
     `arcs` stroke-dasharray on one circle, which is the only way to do a donut
     without measuring path lengths. `gap` keeps adjacent slices from touching. */
  function donut(host, arcs, opts) {
    var o = opts || {}, R = 54, C = 2 * Math.PI * R;
    var total = arcs.reduce(function (a, x) { return a + Math.max(0, num(x.value) || 0); }, 0) || 1;
    var s = svg('svg', { class: 'kld-donut', viewBox: '0 0 132 132', role: 'img',
                          'aria-label': o.label || 'donut chart' });
    s.appendChild(svg('circle', { cx: 66, cy: 66, r: R, fill: 'none',
                                  stroke: 'var(--c-muted)', 'stroke-width': 18 }));
    var off = 0;
    arcs.forEach(function (a, i) {
      var frac = Math.max(0, num(a.value) || 0) / total;
      var len = Math.max(0, frac * C - (o.gap == null ? 2 : o.gap));
      if (len <= 0) return;
      s.appendChild(svg('circle', {
        cx: 66, cy: 66, r: R, fill: 'none',
        stroke: a.color || SERIES[i % SERIES.length], 'stroke-width': 18,
        'stroke-dasharray': len.toFixed(2) + ' ' + (C - len).toFixed(2),
        'stroke-dashoffset': (-off * C).toFixed(2),
        transform: 'rotate(-90 66 66)'
      }));
      off += frac;
    });
    if (o.center) {
      var t = svg('text', { x: 66, y: 66, 'text-anchor': 'middle', 'dominant-baseline': 'central',
                            fill: 'var(--text-1)', 'font-size': 19, 'font-weight': 650 });
      t.textContent = o.center; s.appendChild(t);
    }
    var wrap = el('div', { class: 'kld-donut-wrap' }, [s]);
    if (o.legend !== false) {
      wrap.appendChild(el('div', { class: 'kld-legend' }, arcs.map(function (a, i) {
        return el('div', { class: 'it' }, [
          el('span', { class: 'sw', style: 'background:' + (a.color || SERIES[i % SERIES.length]) }),
          el('span', { class: 'nm', text: a.label }),
          el('span', { class: 'vv', text: o.format ? o.format(num(a.value) || 0) : fmt.compact(a.value) })
        ]);
      })));
    }
    host.replaceChildren(wrap);
  }

  /* ── sparkline / line ───────────────────────────────────────────────────
     `points` = [{x, y}]. Read the geometry from the CONTAINER, not from a fixed
     viewBox, so the same code fits a 40px tile and a full-width panel. */
  function spark(host, points, opts) {
    var o = opts || {}, w = host.clientWidth || 240, h = o.height || 46, pad = 3;
    var pts = (points || []).map(function (p) { return { x: num(p.x) || 0, y: num(p.y) || 0 }; })
      .filter(function (p) { return !isNaN(p.x) && !isNaN(p.y); });
    if (pts.length < 2) { host.replaceChildren(el('div', { class: 'kld-empty', text: o.empty || '没有数据 / No data' })); return; }
    var xs = pts.map(function (p) { return p.x; }), ys = pts.map(function (p) { return p.y; });
    var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
    var y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
    if (y1 === y0) { y1 = y0 + 1; y0 -= 1; }
    var sx = function (x) { return pad + (x1 === x0 ? 0 : (x - x0) / (x1 - x0)) * (w - pad * 2); };
    var sy = function (y) { return h - pad - (y - y0) / (y1 - y0) * (h - pad * 2); };
    var d = pts.map(function (p, i) { return (i ? 'L' : 'M') + sx(p.x).toFixed(2) + ' ' + sy(p.y).toFixed(2); }).join(' ');
    var s = svg('svg', { class: 'kld-spark', viewBox: '0 0 ' + w + ' ' + h, preserveAspectRatio: 'none',
                          role: 'img', 'aria-label': o.label || 'trend' });
    if (o.area !== false) {
      s.appendChild(svg('path', { d: d + ' L' + sx(x1).toFixed(2) + ' ' + (h - pad) + ' L' + sx(x0).toFixed(2) + ' ' + (h - pad) + ' Z',
                                  fill: o.fill || 'var(--accent-soft)', stroke: 'none' }));
    }
    s.appendChild(svg('path', { d: d, fill: 'none', stroke: o.stroke || 'var(--c1)',
                                'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
    host.replaceChildren(s);
  }

  /* ── theme ───────────────────────────────────────────────────────────────
     Redraw on `klado:theme`. Every chart here paints from `var(--cN)`, which the
     token file re-points — but only for elements created AFTER the switch, so the
     live chart has to be rebuilt. Registered functions are re-invoked in order. */
  var redraw = [];
  function onTheme(fn) { redraw.push(fn); return fn; }
  document.addEventListener('klado:theme', function () {
    redraw.forEach(function (fn) { try { fn(); } catch (e) { /* a dead chart must not
      take the page's other charts with it */ } });
  });
  /* The first paint happens before the container has a width on a slow frame;
     one rAF later it does, and a sparkline drawn at 0 width is a flat line. */
  window.addEventListener('resize', function () {
    redraw.forEach(function (fn) { try { fn(); } catch (e) {} });
  });

  window.kld = {
    fmt: fmt, deltaClass: deltaClass, table: table, bars: bars, sbars: sbars,
    donut: donut, spark: spark, onTheme: onTheme, series: SERIES, el: el
  };
})();
