/* ============================================================================
   Report Filters — declarative slice runtime for dynamic Klado reports
   ----------------------------------------------------------------------------
   A dynamic report ships its slices ALREADY WRITTEN — one element per filter
   combination — and this runtime decides which of them is on screen. The reader
   never gets a control: the filter UI lives in the Workspace viewer sidebar and
   reaches us through postMessage. This file creates NO DOM. Ever.

   Contract — the server inlines this BEFORE us:

     script[data-report-filters-state]      ← the tag carries the JSON below
     window.__reportFilters = {
       slug: "q3-region-review",
       readOnly: false,                    // true for the /s/{token} share view
       schema: { version: 1, filters: [ … ] },
       selection: { period: "2026-06", metric: ["gto","spto"] }
     };
     …and its closing tag is written escaped below, because this file is inlined
     verbatim into a script block: a literal closing tag would end the block early.

   ⚠️ The opening tag is spelled WITHOUT angle brackets above on purpose. The server
   strips a previously-inlined copy by re-matching that tag, so a literal
   `<`+`script` in this file's own text makes the strip match INSIDE the runtime and
   a re-published report stacks a second copy (measured: 2 tags after one re-inject).
   Keep this file free of the literal sequence — `report-deck.js` is, for the same reason.

   schema filter shapes (see api docs for the authoritative list):

     { key:"metric", type:"multi"|"select"|"range"|"date_range",
       label:{ en:"Metric", zh:"指标" },
       options:[ { value:"gto", label:{en:"GTO",zh:"GTO"} } ],   // select/multi
       default: "gto" | ["gto"] | {min:3000,max:9000} | {from,to},
       min:0, max:20000, step:500, unit:"RMB",                   // range
       min_date:"2026-01-01", max_date:"2026-12-31" }            // date_range

   A slice declares itself with `data-filter-when` — `&` = AND, `|` = OR:

     <section class="slide" data-filter-when="metric=gto&period=2026-06">
     <div data-filter-when="price=3000|9000">      any of these prices shows it
     <div data-filter-when="channel!=online">      excluded value

   `range` / `date_range` compare the element's own declared value against the
   selection: hit when `sel.min <= value <= sel.max` (ISO strings for dates).

   Host → runtime (works in read-only mode too — the host may still drive):
     frame.contentWindow.postMessage({ type:'report-filters',
                                       selection:{ metric:['spto'] } }, '*')
   Runtime → document (and window): `report:filters` CustomEvent with
     { selection: effective, schema }

   Inlined by api/routers/reports.py, so a downloaded .html stays self-contained.
   ========================================================================== */
(function () {
  'use strict';

  // Inlined into a document that may also link it (and the server may inject it
  // twice) — a second copy must be a no-op.
  if (window.__reportFiltersLoaded) return;
  window.__reportFiltersLoaded = true;

  var boot = window.__reportFilters || {};
  var readOnly = !!boot.readOnly;                       // toggles nothing: same behaviour
  var slug = boot.slug == null ? '' : String(boot.slug);

  var schemaObj = (boot.schema && typeof boot.schema === 'object') ? boot.schema : {};
  var schemaVersion = schemaObj.version || 1;

  /** Schema filters, in schema order. Anything without a usable key is dropped. */
  var filters = [];
  var filterByKey = {};
  if (Array.isArray(schemaObj.filters)) {
    schemaObj.filters.forEach(function (f) {
      if (!f || typeof f !== 'object') return;
      var key = f.key == null ? '' : String(f.key);
      if (!key || Object.prototype.hasOwnProperty.call(filterByKey, key)) return;
      filters.push(f);
      filterByKey[key] = f;
    });
  }

  /** The raw, caller-supplied selection. Never trusted: `effective` is derived. */
  var raw = {};
  if (boot.selection && typeof boot.selection === 'object' && !Array.isArray(boot.selection)) {
    for (var sk in boot.selection) {
      if (Object.prototype.hasOwnProperty.call(boot.selection, sk)) {
        raw[sk] = cloneVal(boot.selection[sk]);
      }
    }
  }

  var effective = {};                    // recomputed by recompute()
  var warnedKeys = {};                   // unknown-key console.warn fires once per key

  // ── small helpers ─────────────────────────────────────────────────────────
  function cloneVal(v) {
    if (Array.isArray(v)) return v.slice();
    if (v && typeof v === 'object') {
      var out = {};
      for (var k in v) { if (Object.prototype.hasOwnProperty.call(v, k)) out[k] = v[k]; }
      return out;
    }
    return v;
  }

  function num(x) {
    if (x === null || x === undefined || x === '') return null;
    var n = Number(x);
    return isFinite(n) ? n : null;
  }

  /** An ISO calendar day, or null. Kept as a string — string compare is enough. */
  function iso(x) {
    if (x === null || x === undefined) return null;
    var s = String(x).trim();
    return /^\d{4}-\d{2}-\d{2}$/.test(s) ? s : null;
  }

  /** Option values, accepting BOTH shapes: `[{value,label}]` and bare strings.
   *
   * The server normalises on write, but the runtime must not go blind on a schema that
   * reached it unnormalised (a downloaded-and-republished report, or a row written
   * straight into the database). A bare string used to yield '' for every option, i.e.
   * every slice hidden — the same class of failure as a 500 on the read endpoint.
   */
  function optionValues(f) {
    var out = [];
    if (f && Array.isArray(f.options)) {
      f.options.forEach(function (o) {
        var value = (o && typeof o === 'object') ? o.value : o;
        if (value === null || value === undefined || value === '') return;
        out.push(String(value));
      });
    }
    return out;
  }

  // ── normalisation: raw selection + schema → what the slices are matched on ─
  //
  // Unknown keys are dropped, out-of-range values are clamped, and anything the
  // caller got wrong falls back to the schema's own default rather than hiding
  // the whole report. An empty schema therefore yields {} — no slice is touched.
  function normSelect(v, f) {
    var opts = optionValues(f);
    if (v !== null && v !== undefined && opts.indexOf(String(v)) >= 0) return String(v);
    if (f['default'] !== null && f['default'] !== undefined) return String(f['default']);
    // No default declared: an arbitrary middle-of-nowhere value would hide every
    // slice, so fall back to the first offered option (or '' when there are none).
    return opts.length ? opts[0] : '';
  }

  function normMulti(v, f) {
    var opts = optionValues(f);
    var picked = [];
    function collect(src) {
      var list = Array.isArray(src) ? src : (src === null || src === undefined ? [] : [src]);
      list.forEach(function (x) {
        var s = String(x);
        if (opts.indexOf(s) >= 0 && picked.indexOf(s) < 0) picked.push(s);
      });
    }
    collect(v);
    // ⚠️ An EXPLICITLY empty list is a real answer ("none of these") and must survive:
    // the server stores it, the sidebar shows every chip off, and the slices naming a
    // value stay hidden. Only a missing / entirely retired value falls back to the
    // default — the reader never chose "none" there, their values moved.
    // (The server applies the same rule; see `_effective_selection` in reports.py.)
    var explicitlyEmpty = Array.isArray(v) && v.length === 0;
    if (!picked.length && !explicitlyEmpty) collect(f['default']);
    return picked;
  }

  function normRange(v, f) {
    var lo = num(f.min), hi = num(f.max);
    var src = (v && typeof v === 'object') ? v
      : ((f['default'] && typeof f['default'] === 'object') ? f['default'] : null);
    var a = src ? num(src.min) : null;
    var b = src ? num(src.max) : null;
    if (a === null) a = lo;                          // a missing bound opens up
    if (b === null) b = hi;
    if (a === null) a = b;
    if (b === null) b = a;
    if (lo !== null) { if (a !== null && a < lo) a = lo; if (b !== null && b < lo) b = lo; }
    if (hi !== null) { if (a !== null && a > hi) a = hi; if (b !== null && b > hi) b = hi; }
    if (a !== null && b !== null && a > b) { var t = a; a = b; b = t; }   // reversed input
    return { min: a, max: b };
  }

  function normDateRange(v, f) {
    var lo = iso(f.min_date), hi = iso(f.max_date);
    var src = (v && typeof v === 'object') ? v
      : ((f['default'] && typeof f['default'] === 'object') ? f['default'] : null);
    var a = src ? iso(src.from) : null;
    var b = src ? iso(src.to) : null;
    if (a === null) a = lo;
    if (b === null) b = hi;
    if (a === null) a = b;
    if (b === null) b = a;
    if (lo !== null) { if (a !== null && a < lo) a = lo; if (b !== null && b < lo) b = lo; }
    if (hi !== null) { if (a !== null && a > hi) a = hi; if (b !== null && b > hi) b = hi; }
    if (a !== null && b !== null && a > b) { var t = a; a = b; b = t; }
    return { from: a, to: b };
  }

  function normOne(v, f) {
    var type = String(f.type || 'select');
    if (type === 'multi') return normMulti(v, f);
    if (type === 'range') return normRange(v, f);
    if (type === 'date_range') return normDateRange(v, f);
    return normSelect(v, f);
  }

  function recompute() {
    var next = {};
    filters.forEach(function (f) {
      var key = String(f.key);
      next[key] = normOne(raw[key], f);
    });
    effective = next;
    return effective;
  }

  // ── matching ──────────────────────────────────────────────────────────────
  function condHit(f, key, value) {
    if (!f) return true;                             // unknown key → never hides
    if (f.type === 'multi') {
      var sel = effective[key];
      return Array.isArray(sel) && sel.indexOf(String(value)) >= 0;
    }
    if (f.type === 'range') {
      var r = effective[key] || {};
      var n = num(value);
      if (n === null) return false;
      return (r.min === null || r.min <= n) && (r.max === null || n <= r.max);
    }
    if (f.type === 'date_range') {
      var d = effective[key] || {};
      var day = iso(value);
      if (day === null) return false;
      return (d.from === null || d.from <= day) && (d.to === null || day <= d.to);
    }
    return String(effective[key]) === String(value);   // select
  }

  /**
   * One `data-filter-when` attribute → one boolean. `&` joins conditions with
   * AND, `|` lists alternatives (OR), a leading `!` on `=` negates the test.
   */
  function elementMatches(el) {
    var spec = el.getAttribute('data-filter-when') || '';
    var conds = spec.split('&');
    for (var i = 0; i < conds.length; i++) {
      var cond = conds[i].trim();
      if (!cond) continue;
      var neg = false;
      var at = cond.indexOf('!=');
      var op = '=';
      if (at >= 0) { op = '!='; neg = true; } else { at = cond.indexOf('='); }
      if (at < 0) continue;                          // malformed → ignored, not hidden
      var key = cond.slice(0, at).trim();
      var expr = cond.slice(at + op.length);
      if (!key) continue;
      if (!Object.prototype.hasOwnProperty.call(filterByKey, key)) {
        // A condition we cannot evaluate must not hide the author's content.
        warnUnknown(key);
        continue;                                    // counts as a hit
      }
      var f = filterByKey[key];
      var values = expr.split('|');
      var any = false;
      for (var j = 0; j < values.length; j++) {
        if (condHit(f, key, values[j].trim())) { any = true; break; }
      }
      if (neg ? any : !any) return false;
    }
    return true;
  }

  function warnUnknown(key) {
    if (warnedKeys[key]) return;
    warnedKeys[key] = true;
    try {
      console.warn('report-filters: data-filter-when references unknown filter "' + key +
        '" (not in schema) — condition ignored, content kept visible');
    } catch (e) { /* ignore */ }
  }

  // ── root markers, for reports that want plain-CSS hooks ───────────────────
  function keep(v) { return v === null || v === undefined ? '' : String(v); }

  function keyValue(key, value, f) {
    if (f && f.type === 'multi') return keep(Array.isArray(value) ? value.join(',') : value);
    if (f && f.type === 'range') return value && typeof value === 'object'
      ? keep(value.min) + '..' + keep(value.max) : '';
    if (f && f.type === 'date_range') return value && typeof value === 'object'
      ? keep(value.from) + '..' + keep(value.to) : '';
    return keep(value);
  }

  function serialize() {
    var parts = [];
    filters.forEach(function (f) {
      var key = String(f.key);
      parts.push(key + '=' + keyValue(key, effective[key], f));
    });
    return parts.join(';');
  }

  function markRoot() {
    var root = document.documentElement;
    if (!root) return;
    root.setAttribute('data-filters', serialize());
    filters.forEach(function (f) {
      var key = String(f.key);
      root.setAttribute('data-filter-' + key, keyValue(key, effective[key], f));
    });
  }

  // ── the one job: show / hide every declared slice ─────────────────────────
  function applyToDom() {
    var els = document.querySelectorAll('[data-filter-when]');
    Array.prototype.forEach.call(els, function (el) {
      // ⚠️ The per-key root marker is `data-filter-<key>` — for a filter literally
      // keyed "when" that attribute IS `data-filter-when`, so the <html> element
      // matches this selector too. The root carries markers, never a slice.
      if (el === document.documentElement) return;
      var hit = elementMatches(el);
      // Both markers are written on every pass, so a slice whose condition starts
      // or stops matching comes back / goes away with no bookkeeping.
      if (hit) {
        el.hidden = false;
        el.classList.remove('report-filter-off');
      } else {
        el.hidden = true;
        el.classList.add('report-filter-off');
      }
    });
    markRoot();
    // Pagination must be recomputed: hidden pages no longer count.
    try {
      if (window.reportDeck && typeof window.reportDeck.refreshPages === 'function') {
        window.reportDeck.refreshPages();
      }
    } catch (e) { /* a broken deck must never break filtering */ }
    var detail = { selection: cloneVal(effective), schema: schema() };
    try {
      document.dispatchEvent(new CustomEvent('report:filters', { detail: detail }));
      window.dispatchEvent(new CustomEvent('report:filters', { detail: detail }));
    } catch (e) { /* ignore */ }
  }

  function schema() {
    return { version: schemaVersion, filters: filters };
  }

  function run() {
    recompute();
    applyToDom();
  }

  // ── public API ────────────────────────────────────────────────────────────
  var api = {
    get slug() { return slug; },
    get readOnly() { return readOnly; },
    get schema() { return schema(); },
    get selection() { return cloneVal(raw); },
    get effective() { return cloneVal(effective); },
    /** Partial, per-key update — keys not mentioned keep their current value. */
    apply: function (selection) {
      if (selection && typeof selection === 'object' && !Array.isArray(selection)) {
        for (var k in selection) {
          if (!Object.prototype.hasOwnProperty.call(selection, k)) continue;
          if (Object.prototype.hasOwnProperty.call(filterByKey, k)) raw[k] = cloneVal(selection[k]);
        }
      }
      run();
      return api.effective;
    },
    /** Back to the schema defaults (an empty selection normalises to them). */
    reset: function () {
      raw = {};
      run();
      return api.effective;
    },
    /** Is `el` (or any wrapper of it) currently on screen? Used for page counts. */
    isVisible: function (el) {
      if (!el || el.nodeType !== 1) return false;
      if (el.hidden) return false;
      var node = el;
      while (node && node.nodeType === 1) {
        if (node.classList && node.classList.contains('report-filter-off')) return false;
        node = node.parentNode;
      }
      return true;
    },
    serialize: serialize
  };
  window.reportFilters = api;

  // Host-driven: the Workspace sidebar pushes the current selection in. Accepted
  // in read-only mode too — the share view simply has no sidebar to push from.
  window.addEventListener('message', function (e) {
    var d = e.data;
    if (!d || d.type !== 'report-filters' || !d.selection) return;
    try { api.apply(d.selection); } catch (err) { /* ignore */ }
  });

  // These files are inlined just before the closing body tag, so the DOM is
  // already parsed; the guard covers a document that inlines us in <head> instead.
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run);
  else run();
})();