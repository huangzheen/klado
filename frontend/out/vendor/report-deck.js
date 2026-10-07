/* ============================================================================
   Report Deck — 16:9 paged presentation runtime for the Klado Workspace
   ----------------------------------------------------------------------------
   Contract: <div class="deck"> <section class="slide">…</section> … </div>
   (the wrapper is optional — bare .slide elements are auto-wrapped)

   Behaviour
   * every .slide is a fixed 1920x1080 page, scaled to fit — never reflowed
   * Down / Right / PageDown / Space  → next page   (a whole page at a time)
     Up / Left / PageUp / Backspace   → previous page
     Home / End                       → first / last page
   * mouse wheel, the on-screen pager, and click-to-advance also work
   * the current page is kept in location.hash (#p3) so the URL is shareable
   * `#all` (or ?deck=all) switches to the all-pages layout, where the dashed
     page-break rules are drawn between pages — that is also the print/export
     layout
   * the host page can drive it: iframe.contentWindow.postMessage(
       {type:'report-deck', action:'next'|'prev'|'goto'|'lang', index, lang}, '*')
     and this runtime posts {type:'report-deck:state', …} back to its parent.

   Language layer (one document, both languages)
   ---------------------------------------------
   A report ships BOTH languages inside one file; every translated block/page
   carries `data-lang="en"` / `data-lang="zh"`. `?lang=` picks which one renders,
   defaulting to **en** (English is the house default). Blocks without `data-lang`
   are shared and always shown.

   The runtime works with or without a deck: a long-form bilingual document gets
   the language layer (plus a small floating switch) and no pager, a deck gets
   both. Page numbers are computed per language, so 6 EN + 6 ZH pages read
   "3 / 6" in either language.

   Inlined into the served document by api/routers/reports.py, so a report never
   depends on this file being reachable and a downloaded .html is self-contained.
   ========================================================================== */
(function () {
  'use strict';

  // The server may inline this file into a document that also links it, so a
  // second copy must be a no-op.
  if (window.__reportDeckLoaded) return;
  window.__reportDeckLoaded = true;

  // Optional v1 content model.  Bound text and tables are rendered from the
  // same object the PPTX extractor validates, so saved edits cannot silently
  // drift from a second, hand-written export-only copy of the content.
  var modelTag = document.querySelector('script[type="application/json"][data-report-deck-model]');
  var reportModel = window.reportDeckModel || null;
  if (modelTag && !reportModel) {
    try { reportModel = JSON.parse(modelTag.textContent); }
    catch (e) { console.error('Invalid Report Deck model JSON', e); }
  }
  function modelAt(path) {
    if (!reportModel || reportModel.version !== 1 ||
        !/^[A-Za-z_$][\w$]*(?:\.(?:[A-Za-z_$][\w$]*|\d+))*$/.test(path))
      throw new Error('Invalid Report Deck model binding: ' + path);
    return path.split('.').reduce(function (value, part) {
      if (value == null || !Object.prototype.hasOwnProperty.call(value, part))
        throw new Error('Missing Report Deck model value: ' + path);
      return value[part];
    }, reportModel);
  }
  function bindModel(root) {
    if (!reportModel) return;
    root = root || document;
    Array.prototype.forEach.call(root.querySelectorAll('[data-deck-field]'), function (el) {
      var path = el.getAttribute('data-deck-field');
      var value = modelAt(path);
      if (value == null || typeof value === 'object') throw new Error('Text field must be scalar: ' + path);
      el.textContent = String(value);
      el.setAttribute('data-pptx-bind', path);
    });
    Array.prototype.forEach.call(root.querySelectorAll('table[data-deck-table]'), function (table) {
      var path = table.getAttribute('data-deck-table');
      var rows = modelAt(path);
      if (!Array.isArray(rows) || rows.some(function (row) { return !Array.isArray(row); }))
        throw new Error('Table model must be an array of rows: ' + path);
      var headerRows = parseInt(table.getAttribute('data-deck-header-rows') || '0', 10);
      table.replaceChildren();
      rows.forEach(function (row, i) {
        var tr = table.insertRow();
        row.forEach(function (value) {
          var cell = document.createElement(i < headerRows ? 'th' : 'td');
          cell.textContent = String(value == null ? '' : value);
          tr.appendChild(cell);
        });
      });
      table.setAttribute('data-pptx-table-ref', path);
    });
    Array.prototype.forEach.call(root.querySelectorAll('[data-deck-chart]'), function (el) {
      var path = el.getAttribute('data-deck-chart');
      if (!modelAt(path) || typeof modelAt(path) !== 'object')
        throw new Error('Chart model must be an object: ' + path);
      el.setAttribute('data-pptx-chart-ref', path);
      // The report's own chart renderer uses the same reportModel object.
      // The exporter uses this reference to build a native PowerPoint chart.
    });
  }
  window.reportDeckData = {
    get model() { return reportModel; },
    get: modelAt,
    bind: bindModel,
    set: function (value) {
      if (!value || value.version !== 1) throw new Error('Report Deck model needs version: 1');
      reportModel = value;
      window.reportDeckModel = value;
      bindModel();
      return value;
    },
    setState: function (state) {
      if (!reportModel) throw new Error('No Report Deck model');
      reportModel.state = state;
      window.reportDeckModel = reportModel;
      bindModel();
      return state;
    }
  };
  if (reportModel) window.reportDeckModel = reportModel;

  var PAGE_W = 1920, PAGE_H = 1080, GUTTER = 22, BAR_SPACE = 60, WHEEL_LOCK_MS = 320;
  var DEFAULT_LANG = 'en';
  var LANG_LABELS = { en: 'EN', zh: '中文' };

  var KEY_NEXT = { ArrowDown: 1, ArrowRight: 1, PageDown: 1, ' ': 1, Spacebar: 1, Enter: 1 };
  var KEY_PREV = { ArrowUp: 1, ArrowLeft: 1, PageUp: 1, Backspace: 1 };

  // ── language helpers (shared by both modes) ────────────────────────────────
  // 'zh-CN' / 'ZH_cn' → 'zh': a report declares base codes, callers may send full ones.
  function normLang(value) {
    return String(value == null ? '' : value).trim().toLowerCase().split(/[-_]/)[0];
  }

  function langLabel(code) {
    return LANG_LABELS[code] || String(code || '').toUpperCase();
  }

  function urlLang() {
    var raw = null;
    try { raw = new URLSearchParams(location.search).get('lang'); } catch (e) { /* ignore */ }
    return normLang(raw) || DEFAULT_LANG;
  }

  /** Languages the document actually declares, in document order. */
  function declaredLangs() {
    var found = [];
    Array.prototype.forEach.call(document.querySelectorAll('[data-lang]'), function (el) {
      var code = normLang(el.getAttribute('data-lang'));
      if (code && found.indexOf(code) < 0) found.push(code);
    });
    return found;
  }

  /**
   * Hide every block whose data-lang is not the active one.
   *
   * A class rather than per-language CSS selectors: CSS cannot read a variable
   * into a selector, and one `.deck-lang-off` rule covers any language code.
   */
  function applyLangFilter(lang) {
    Array.prototype.forEach.call(document.querySelectorAll('[data-lang]'), function (el) {
      var code = normLang(el.getAttribute('data-lang'));
      el.classList.toggle('deck-lang-off', !!code && code !== lang);
    });
    document.documentElement.setAttribute('data-deck-lang', lang);
  }

  function init() {
    if (reportModel) bindModel();
    var deck = document.querySelector('.deck');
    if (!deck) {
      var bare = [].slice.call(document.querySelectorAll('.slide'));
      if (bare.length) {
        deck = document.createElement('div');
        deck.className = 'deck';
        bare[0].parentNode.insertBefore(deck, bare[0]);
        bare.forEach(function (s) { deck.appendChild(s); });
      }
    }

    // The language layer runs for ANY document that declares data-lang — a plain
    // long-form bilingual report has no slides but still needs it.
    var lang = urlLang();
    var langs = declaredLangs();
    applyLangFilter(lang);
    window.__reportDeckLang = lang;

    var slides0 = deck ? [].slice.call(deck.querySelectorAll('.slide')) : [];
    if (!slides0.length) {
      if (langs.length > 1) mountLangBar(null, langs);
      publishLangApi(null, langs);
      document.documentElement.classList.add('deck-ready');
      return;                                  // long-form: language layer only
    }

    var title = deck.getAttribute('data-deck-title') || document.title || '';
    var index = 0, mode = 'paged', locked = 0, wheelAcc = 0;

    /** Pages of the active language (+ any page that declares none), minus filter-hidden ones.
     *
     * A dynamic report's slices are hidden by `report-filters.js` (a class + the `hidden`
     * attribute). A hidden PAGE must not be counted: the reader would otherwise see
     * "3 / 12" for a 7-page view and the deck would stop on blank pages.
     */
    function activeSlides() {
      return [].slice.call(deck.querySelectorAll('.slide')).filter(function (s) {
        if (s.hidden || s.classList.contains('report-filter-off')) return false;
        var code = normLang(s.getAttribute('data-lang'));
        return !code || code === lang;
      });
    }
    var slides = activeSlides();

    // ── per-page footer: the page-break rule + page number ──────────────────
    // Rebuilt on every language switch, because the page count is per language.
    function buildFooters() {
      Array.prototype.forEach.call(deck.querySelectorAll('.deck-foot'), function (el) { el.remove(); });
      slides.forEach(function (slide, i) {
        var foot = document.createElement('div');
        foot.className = 'deck-foot';
        var dots = '';
        if (slides.length <= 12) {
          var spans = '';
          for (var d = 0; d < slides.length; d++) {
            spans += '<i' + (d === i ? ' class="on"' : '') + '></i>';
          }
          dots = '<span class="deck-foot-dots">' + spans + '</span>';
        }
        foot.innerHTML =
          '<span class="deck-foot-title">' + escapeHtml(title) + '</span>' +
          dots +
          '<span class="deck-foot-n">' + (i + 1) + ' / ' + slides.length + '</span>';
        slide.appendChild(foot);
      });
    }
    buildFooters();

    // ── pager chrome (outside the pages, so it is never part of an export) ──
    //
    // ⚠️ The bar is for a STANDALONE document. When the Workspace viewer embeds
    // us in #rpt-frame it already draws its own bar (Back / title / language /
    // page counter / all-pages toggle), so a second floating bar is a duplicate
    // of the same controls sitting in the middle of the screen. Detection is
    // "am I in a frame at all" — NOT "is my host the app", because a report
    // pasted into any other iframe would then lose its only controls too. A
    // standalone tab, a downloaded file and the /s/{token} share link are all
    // top-level documents, so they keep the bar.
    var embedded = window.parent && window.parent !== window;
    var progress = document.createElement('div');
    progress.className = 'deck-progress';
    var bar = document.createElement('div');
    bar.className = 'deck-bar';
    var langControl = langs.length > 1
      ? '<span class="deck-lang" role="group" aria-label="Language">' +
          langs.map(function (c) {
            return '<button type="button" data-deck-lang="' + escapeHtml(c) + '">' + escapeHtml(langLabel(c)) + '</button>';
          }).join('') +
        '</span>'
      : '';
    bar.innerHTML =
      '<button type="button" class="wide" data-deck-all title="显示全部页面 / 分页线">全部页面</button>' +
      langControl +
      '<span class="deck-bar-hint">↓ 翻页</span>' +
      '<button type="button" data-deck-prev title="上一页">‹</button>' +
      '<span class="deck-bar-n">1 / ' + slides.length + '</span>' +
      '<button type="button" data-deck-next title="下一页">›</button>';
    deck.appendChild(progress);
    if (!embedded) deck.appendChild(bar);   // embedded: the host bar owns these controls

    var barN = bar.querySelector('.deck-bar-n');
    var btnPrev = bar.querySelector('[data-deck-prev]');
    var btnNext = bar.querySelector('[data-deck-next]');
    markLangButtons();

    // Every write below targets the (possibly detached) bar. Detached nodes keep
    // working fine — textContent, disabled, classList are all local — so no guard
    // is needed on the elements themselves; only the listeners at the bottom,
    // which are attached to a node that is no longer in the document, are moot.
    function markLangButtons() {
      Array.prototype.forEach.call(bar.querySelectorAll('[data-deck-lang]'), function (b) {
        b.classList.toggle('on', normLang(b.getAttribute('data-deck-lang')) === lang);
      });
    }

    // ── paging ─────────────────────────────────────────────────────────────
    function apply() {
      // Clear FIRST: `slides` holds only the active language, so toggling just
      // those would leave the other language's page carrying a stale
      // `is-current` (invisible only because the language filter hides it).
      Array.prototype.forEach.call(deck.querySelectorAll('.slide'), function (s) { s.classList.remove('is-current'); });
      slides.forEach(function (s, i) { if (mode === 'paged' && i === index) s.classList.add('is-current'); });
      // ⚠️ `slides` can be EMPTY: a dynamic report whose filters exclude every page it
      // has. Then there is no page to number — say "0 / 0" instead of "1 / 0" and a
      // NaN-width progress bar, which reads as a broken viewer rather than an empty view.
      barN.textContent = (slides.length ? index + 1 : 0) + ' / ' + slides.length;
      btnPrev.disabled = !slides.length || index === 0;
      btnNext.disabled = !slides.length || index === slides.length - 1;
      progress.style.width = (slides.length ? (index + 1) / slides.length * 100 : 0) + '%';
      if (mode === 'paged') scale();
      syncUrl();
      notify();
    }

    function goTo(i, opts) {
      var next = Math.max(0, Math.min(slides.length - 1, i | 0));
      if (next === index && !(opts && opts.force)) return;
      index = next;
      apply();
      if (opts && opts.scroll) slides[index].scrollIntoView({ block: 'center', behavior: 'smooth' });
    }

    function next() {
      if (mode === 'all') { slides[Math.min(slides.length - 1, index + 1)].scrollIntoView({ block: 'center', behavior: 'smooth' }); return; }
      goTo(index + 1);
    }
    function prev() {
      if (mode === 'all') { slides[Math.max(0, index - 1)].scrollIntoView({ block: 'center', behavior: 'smooth' }); return; }
      goTo(index - 1);
    }

    // ── scale-to-fit (the page box itself never changes) ───────────────────
    function scale() {
      if (mode !== 'paged') return;
      // ⚠️ Measure the VIEWPORT, not `deck`'s own box. `.deck` is `position: fixed; inset: 0`,
      // but an author can hand it a fixed size (see the note in report-deck.css): then this
      // math runs on the design canvas (1920x1080), the scale comes out ~.90 whatever the
      // viewer's size, and every page is cropped by `overflow: hidden`. The viewport is the
      // truth for "fit"; `deck.clientWidth` stays as the fallback.
      var root = document.documentElement;
      // BAR_SPACE exists so the floating bar never covers the page. With no bar
      // (embedded) that reserve is dead space and the page comes out smaller than
      // it needs to be, so give it back.
      var barSpace = embedded ? 0 : BAR_SPACE;
      var availW = (root.clientWidth || deck.clientWidth) - GUTTER * 2;
      var availH = (root.clientHeight || deck.clientHeight) - barSpace - GUTTER * 2;
      if (availW <= 0 || availH <= 0) return;
      var s = Math.min(availW / PAGE_W, availH / PAGE_H);
      deck.style.setProperty('--deck-scale', Math.max(0.05, Math.min(s, 3)).toFixed(4));
    }

    // ── language switch ───────────────────────────────────────────────────
    function setLang(next) {
      var target = normLang(next) || DEFAULT_LANG;
      if (target === lang) return;
      lang = target;
      window.__reportDeckLang = lang;
      applyLangFilter(lang);
      slides = activeSlides();
      index = Math.min(index, Math.max(0, slides.length - 1));
      buildFooters();
      markLangButtons();
      barN.textContent = (index + 1) + ' / ' + slides.length;
      if (mode === 'all') apply(); else apply();
    }

    // ── filter-driven page set ────────────────────────────────────────────
    /** Re-count the pages after the FILTER layer changed which slices are visible.
     *
     * The controls live in the HOST app (the Workspace viewer's sidebar), so this document
     * has no idea when a slice was hidden or shown; `report-filters.js` calls this after
     * every apply. Rebuilding the footers and the counter is the same problem a language
     * switch solves ("the set of visible pages changed"), so it goes through the same
     * steps — a stale "3 / 12" on a 7-page view is the symptom of skipping them.
     */
    function refreshPages() {
      var next = activeSlides();
      var same = next.length === slides.length
        && next.every(function (s, i) { return s === slides[i]; });
      if (same) {
        // Same pages, same order: nothing to rebuild. Re-apply anyway — a slice INSIDE the
        // current page may have appeared or disappeared, and that can change its scale.
        apply();
        return;
      }
      slides = next;
      index = Math.min(index, Math.max(0, slides.length - 1));
      buildFooters();
      if (mode === 'all') { setMode('all'); return; }   // the page-break rules follow the page set
      barN.textContent = (slides.length ? index + 1 : 0) + ' / ' + slides.length;
      apply();
    }

    // ── mode: paged  ⇄  all pages (visible page-break rules, print layout) ─
    function setMode(m) {
      mode = m === 'all' ? 'all' : 'paged';
      deck.classList.toggle('deck--all', mode === 'all');
      document.documentElement.classList.toggle('deck-all', mode === 'all');
      [].slice.call(deck.querySelectorAll('.deck-break')).forEach(function (el) { el.remove(); });
      if (mode === 'all') {
        // dashed page-break rules between the pages (of the ACTIVE language only)
        slides.forEach(function (slide, i) {
          if (i === slides.length - 1) return;
          var br = document.createElement('div');
          br.className = 'deck-break';
          br.textContent = '分页 · ' + (i + 1) + ' / ' + slides.length;
          slide.parentNode.insertBefore(br, slide.nextSibling);
        });
        slides.forEach(function (s) { s.classList.add('is-current'); });
        setTimeout(function () { if (slides[index]) slides[index].scrollIntoView({ block: 'center' }); }, 30);
      }
      bar.querySelector('[data-deck-all]').classList.toggle('wide', mode === 'all');
      bar.querySelector('[data-deck-all]').textContent = mode === 'all' ? '单页浏览' : '全部页面';
      apply();
    }

    // ── URL state (?lang is omitted for the default so links stay clean) ────
    function syncUrl() {
      try {
        var query = lang === DEFAULT_LANG ? '' : '?lang=' + encodeURIComponent(lang);
        var want = location.pathname + query + (mode === 'all' ? '#all' : '#p' + (index + 1));
        if (location.pathname + location.search + location.hash !== want) {
          history.replaceState(null, '', want);
        }
      } catch (e) { /* file:// or a sandboxed frame can refuse history */ }
    }
    function applyHash() {
      var h = (location.hash || '').replace(/^#/, '');
      if (h === 'all') {
        if (mode !== 'all') setMode('all');
        return true;
      }
      var m = /^p(\d+)$/.exec(h);
      if (m) {
        var target = Math.max(0, Math.min(slides.length - 1, parseInt(m[1], 10) - 1));
        if (mode === 'all') setMode('paged');
        goTo(target, { force: true });
        return true;
      }
      return false;
    }

    // ⚠️ A hash change is a SAME-DOCUMENT navigation: no reload, so init() never
    // runs again. Without this listener, typing #p5 or following a #p5 link would
    // leave the deck exactly where it was. Our own URL writes go through
    // history.replaceState, which does not fire hashchange, so no feedback loop.
    window.addEventListener('hashchange', applyHash);

    function notify() {
      try {
        if (window.parent && window.parent !== window) {
          window.parent.postMessage({
            type: 'report-deck:state', index: index, count: slides.length,
            mode: mode, lang: lang, langs: langs
          }, '*');
        }
      } catch (e) { /* ignore */ }
    }

    // ── input ──────────────────────────────────────────────────────────────
    function typingInField(target) {
      if (!target) return false;
      var tag = (target.tagName || '').toLowerCase();
      return tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable;
    }

    document.addEventListener('keydown', function (e) {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
      if (typingInField(e.target)) return;
      var key = e.key;
      if (key === 'Home') { e.preventDefault(); goTo(0); return; }
      if (key === 'End') { e.preventDefault(); goTo(slides.length - 1); return; }
      if (key === 'Escape') {
        // ESC is the host's (it closes the Workspace viewer). Hand it up rather
        // than swallowing it: once the frame has focus, no key event of ours
        // ever reaches the parent document.
        try {
          if (window.parent && window.parent !== window) {
            window.parent.postMessage({ type: 'report-deck:escape' }, '*');
          }
        } catch (err) { /* ignore */ }
        return;
      }
      if (KEY_NEXT[key]) { e.preventDefault(); next(); return; }
      if (KEY_PREV[key]) { e.preventDefault(); prev(); return; }
    });

    document.addEventListener('wheel', function (e) {
      if (mode !== 'paged') return;
      var now = Date.now();
      if (now < locked) return;
      wheelAcc += e.deltaY;
      if (Math.abs(wheelAcc) < 60) return;
      locked = now + WHEEL_LOCK_MS;
      var dir = wheelAcc > 0 ? 1 : -1;
      wheelAcc = 0;
      if (dir > 0) next(); else prev();
    }, { passive: true });

    deck.addEventListener('click', function (e) {
      if (deck.getAttribute('data-deck-click') === 'off') return;
      if (e.target.closest('a, button, input, textarea, select, label, .deck-bar, .deck-foot')) return;
      var sel = window.getSelection();
      if (sel && String(sel).length) return;             // the user is selecting text
      var r = deck.getBoundingClientRect();
      if (e.clientX - r.left < r.width * 0.14) prev(); else next();
    });

    bar.addEventListener('click', function (e) {
      var t = e.target.closest('button');
      if (!t) return;
      if (t.hasAttribute('data-deck-lang')) setLang(t.getAttribute('data-deck-lang'));
      else if (t.hasAttribute('data-deck-all')) setMode(mode === 'all' ? 'paged' : 'all');
      else if (t.hasAttribute('data-deck-prev')) prev();
      else if (t.hasAttribute('data-deck-next')) next();
    });

    // driven by the host page (the Workspace viewer forwards keys here)
    window.addEventListener('message', function (e) {
      var d = e.data;
      if (!d || d.type !== 'report-deck') return;
      if (d.action === 'next') next();
      else if (d.action === 'prev') prev();
      else if (d.action === 'goto') goTo(d.index);
      else if (d.action === 'mode') setMode(d.mode);
      else if (d.action === 'lang') setLang(d.lang);
      else if (d.action === 'state') notify();
    });

    window.addEventListener('resize', scale);
    if (window.ResizeObserver) new ResizeObserver(scale).observe(deck);

    applyHash();
    apply();
    deck.setAttribute('data-deck-ready', '1');
    document.documentElement.classList.add('deck-ready');

    window.reportDeck = {
      next: next, prev: prev, goTo: goTo, setMode: setMode, setLang: setLang,
      // Called by the filter runtime (report-filters.js) after it hides or shows slices.
      refreshPages: refreshPages,
      get index() { return index; },
      get count() { return slides.length; },
      get mode() { return mode; },
      get lang() { return lang; },
      get langs() { return langs.slice(); }
    };
  }

  /**
   * Floating language switch for documents without slides (long-form reports) and
   * as a fallback when there is no deck bar. Without it a bilingual long-form
   * report opened standalone would have no way to switch.
   */
  function mountLangBar(_unused, langs) {
    if (document.querySelector('.deck-langbar')) return;
    var bar = document.createElement('div');
    bar.className = 'deck-langbar';
    bar.setAttribute('role', 'group');
    bar.setAttribute('aria-label', 'Language');
    bar.innerHTML = langs.map(function (c) {
      return '<button type="button" data-deck-lang="' + escapeHtml(c) + '">' + escapeHtml(langLabel(c)) + '</button>';
    }).join('');
    bar.addEventListener('click', function (e) {
      var t = e.target.closest('button');
      if (t) switchLangEverywhere(t.getAttribute('data-deck-lang'));
    });
    document.body.appendChild(bar);
    markLangButtonsEverywhere(window.__reportDeckLang);
  }

  /** Used by the long-form switch (no deck instance to hold the state). */
  function switchLangEverywhere(next) {
    var target = normLang(next) || DEFAULT_LANG;
    window.__reportDeckLang = target;
    applyLangFilter(target);
    markLangButtonsEverywhere(target);
    try {
      var query = target === DEFAULT_LANG ? '' : '?lang=' + encodeURIComponent(target);
      history.replaceState(null, '', location.pathname + query + location.hash);
    } catch (e) { /* ignore */ }
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({ type: 'report-deck:state', mode: 'doc', lang: target }, '*');
      }
    } catch (e) { /* ignore */ }
  }

  function markLangButtonsEverywhere(lang) {
    Array.prototype.forEach.call(document.querySelectorAll('[data-deck-lang]'), function (b) {
      b.classList.toggle('on', normLang(b.getAttribute('data-deck-lang')) === normLang(lang));
    });
  }

  function publishLangApi(_unused, langs) {
    window.reportDeck = {
      setLang: switchLangEverywhere,
      get lang() { return window.__reportDeckLang || DEFAULT_LANG; },
      get langs() { return langs.slice(); }
    };
    window.addEventListener('message', function (e) {
      var d = e.data;
      if (d && d.type === 'report-deck' && d.action === 'lang') switchLangEverywhere(d.lang);
    });
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
