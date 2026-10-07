/* Klado admin console — the bilingual `中文 / English` convention, frontend half.
 *
 * ⚠️ This is a SECOND implementation of a rule the main app also has
 * (`frontend/out/i18n.js`). That duplication is deliberate and the reason is the console's
 * one hard constraint: it may not load an asset out of `api/`, because then "a separate
 * program on its own port" would be a fiction with a shared asset directory. The backend
 * half (`klado_shared/i18n.py::pick`) is genuinely shared — it is Python and both
 * processes import it — but this half is DOM code and only one of the two programs has
 * this DOM.
 *
 * The SPLIT RULE is the translation memory, so it has to match the Python half exactly:
 *
 *     one slash, a space on BOTH sides, CJK on the left, a letter on the right.
 *
 * Getting it wrong is not cosmetic. Too loose and `https://a/b` or `2026/10/02` becomes
 * two languages in one sentence; too tight and a `来源：/ Source:` pair never splits and
 * the reader sees both sides at once. Both failures look identical to the user — they see
 * Chinese and English together — and the compiler sees neither.
 *
 * What is deliberately NOT here: a dictionary of registered terms. New copy is written as
 * a pair and works immediately, with no registration step, which is the whole point.
 */
(function (global) {
  'use strict';

  var STORAGE_KEY = 'klado-admin-lang';

  /* The rule, in one place, and the only place it appears. */
  var PAIR = /([\u4e00-\u9fff][^/]*?)\s\/\s([A-Za-z][^/]*)/;

  /* Exact-match fallbacks for strings that are already monolingual — a product name, a
   * module key, an email address.
   *
   * ⚠️ **NOT WIRED UP, DELIBERATELY.** There was a `dict()` helper here that read this
   * map, and `translate()` did not call it, so the whole thing was dead code. It was
   * deleted rather than connected, because connecting it would have imported a bug from
   * the main app: `if (DICT[text]) return DICT[text]` ignores the current language, so a
   * Chinese-keyed entry answers an English page with Chinese, and — since the map holds
   * only one direction — the reverse lookup it fell back to was gated on `lang === 'zh'`,
   * making a string untranslatable in English no matter which side it was written on.
   * That is exactly the defect the main app's `DICT.zh` had (twenty-one entries filed in
   * the wrong direction, found by the "Chinese left in English mode" audit), and it is
   * why the console uses pairs and nothing else: `pickPair` is symmetric, so a language
   * switch round-trips with no original-value bookkeeping to get wrong.
   *
   * If a monolingual string genuinely needs translating here, write it as a pair.
   * Reach for a second map only together with the direction invariant that keeps the
   * two maps from disagreeing. */

  /* Attribute values that are user-visible and therefore need the same treatment. */
  var ATTRS = ['title', 'placeholder', 'aria-label', 'data-i18n-title'];

  var lang = 'zh';
  var originals = new WeakMap();

  function pickPair(text) {
    if (typeof text !== 'string') return text;
    var m = text.match(PAIR);
    if (!m) return text;
    return (lang === 'zh' ? m[1] : m[2]).trim();
  }

  function translate(text) {
    if (typeof text !== 'string' || !text) return text;
    return pickPair(text);
  }

  /* One element. Records the original so a language switch can put it back — without
   * that, processing twice compounds the damage (`中文 / 中文` and then nothing to
   * split). */
  function processElement(el) {
    var original = originals.get(el);
    if (original === undefined) {
      original = {
        text: el.textContent,
        attrs: {},
        wroteText: undefined,
        wroteAttrs: {}
      };
      ATTRS.forEach(function (name) {
        if (el.hasAttribute(name)) original.attrs[name] = el.getAttribute(name);
      });
      originals.set(el, original);
    }

    /* `data-i18n-skip` marks a container whose text is USER CONTENT — a name, a subject
     * line, a pasted value. Splitting it would mangle a person's name that happens to
     * contain a slash. The rule is the same as the main app's and for the same reason. */
    if (el.hasAttribute('data-i18n-skip')) return;

    /* ⚠️ THE LOOP GUARD, and the reason the page hung when this file was first written.
     *
     * `textContent = x` is a childList mutation, and the observer below is watching for
     * childList mutations. So an unconditional write is a cycle: walk → write → observe
     * → walk → write. The first version of this file did exactly that, the main thread
     * never came back, and the console hung on sign-in with no error in the log — the
     * browser was not crashing, it was spinning.
     *
     * Writing only when the value has actually changed closes it, and it is also the
     * honest form: a translate step that produces the string already there has done
     * nothing, and saying so is free.
     */
    if (el.children.length === 0) {
      /* App writes replace our baseline. In particular the login error starts
       * empty: restoring that initial value would erase every authentication
       * failure after the observer sees it. Only own values we last wrote. */
      if (original.wroteText === undefined || el.textContent !== original.wroteText) {
        original.text = el.textContent;
      }
      var next = translate(original.text);
      if (next !== el.textContent) el.textContent = next;
      original.wroteText = next;
    }
    ATTRS.forEach(function (name) {
      var current = el.getAttribute(name);
      if (current === null) {
        delete original.attrs[name];
        delete original.wroteAttrs[name];
        return;
      }
      if (original.wroteAttrs[name] === undefined || current !== original.wroteAttrs[name]) {
        original.attrs[name] = current;
      }
      var value = translate(original.attrs[name]);
      if (value !== current) el.setAttribute(name, value);
      original.wroteAttrs[name] = value;
    });
  }

  function walk(root) {
    var scope = root || document.body;
    if (!scope) return;
    var nodes = scope.querySelectorAll
      ? scope.querySelectorAll(
          '*:not(script):not(style):not([data-i18n-skip]):not([data-i18n-skip] *)')
      : [];
    Array.prototype.forEach.call(nodes, processElement);
    if (scope.nodeType === 1) processElement(scope);
  }

  /* Coalesce, do not run inline. Two reasons, and the second is the one that bites:
   *
   *  1. a render touches a dozen nodes at once, and a `walk` per mutation is a walk of
   *     the whole document per mutation;
   *  2. a `MutationObserver` callback that mutates the DOM it is observing is delivered
   *     in the *same* microtask checkpoint, so an unwalked write here re-enters
   *     immediately. Batching onto an animation frame puts the walk after the batch has
   *     settled, which is what makes the guard in `processElement` sufficient.
   *
   * A Set rather than a single root, so nothing is dropped: the first version kept only
   * the first root and later subtrees in the same frame stayed untranslated.
   */
  var pending = null;
  function schedule(root) {
    if (!root) return;
    if (pending) { pending.add(root); return; }
    pending = new Set([root]);
    requestAnimationFrame(function () {
      var roots = pending;
      pending = null;
      roots.forEach(walk);
    });
  }

  /* A string built by concatenation is not its own text node, so the walker never sees
   * it. Those call sites have to call `t()` on each half explicitly — which is why `t` is
   * exported rather than kept private. */
  function t(text) { return translate(text); }

  function setLang(next) {
    lang = next === 'en' ? 'en' : 'zh';
    document.documentElement.lang = lang;
    try { localStorage.setItem(STORAGE_KEY, lang); } catch (e) { /* private mode */ }
    walk(document);
    syncToggle();
    document.dispatchEvent(new CustomEvent('klado:lang', { detail: { lang: lang } }));
  }

  function currentLang() { return lang; }

  /* The language button's own label is generated, never a pair. ⚠️ It cannot be a pair:
   * `processElement` caches the original attribute and re-derives both sides on every
   * pass, so it and this function would fight over the same attribute — the visible
   * symptom is a tooltip that says "Language" while the page is in Chinese. */
  function syncToggle() {
    var btn = document.querySelector('[data-lang-toggle]');
    if (!btn) return;
    btn.textContent = lang === 'zh' ? 'EN' : '中';
    btn.title = lang === 'zh' ? 'Switch to English' : '切换到中文';
  }

  function init() {
    var stored = null;
    try { stored = localStorage.getItem(STORAGE_KEY); } catch (e) { /* ignore */ }
    /* ⚠️ Chinese unless the operator has said otherwise — the browser's language
     * deliberately does NOT take part (user's ruling, 2026-10-03).
     *
     * The previous order was `stored || (browser starts with 'zh' ? 'zh' : 'en')`, and
     * that is the rule the MAIN app still uses. Dropping it here is intentional even
     * though the two halves are otherwise kept in step: the console is a tool for one
     * team, not a public site, so the useful default is the language the team works in
     * rather than the language of whichever machine opened the page. An operator on an
     * English-locale laptop used to land on an English console and had to find the
     * toggle every time.
     *
     * `stored === 'en' ? 'en' : 'zh'` rather than `stored || 'zh'` on purpose: it accepts
     * only a value this module could have written, so a hand-edited or stale
     * localStorage entry cannot put the page into a language no `pickPair` branch
     * matches — which would render every pair as "中文 / English" at once. This is the
     * same validation `setLang` already performs, so the two agree by construction
     * rather than by luck.
     */
    lang = stored === 'en' ? 'en' : 'zh';
    document.documentElement.lang = lang;
    walk(document);
    syncToggle();
    /* Dynamic content. A MutationObserver rather than a manual `render()` call, because
     * forgetting one call site shows up as a page that is half-translated, which is the
     * kind of thing that ships. `childList` only — attribute writes are handled inline
     * by `processElement`, and watching them would put the walk on the one path the
     * guard above exists to keep quiet. */
    new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        var added = records[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          var node = added[j];
          schedule(node.nodeType === 1 ? node : node.parentElement);
        }
      }
    }).observe(document.body, { childList: true, subtree: true });
    document.addEventListener('click', function (e) {
      var toggle = e.target.closest && e.target.closest('[data-lang-toggle]');
      if (toggle) setLang(lang === 'zh' ? 'en' : 'zh');
    });
  }

  global.kladoI18n = {
    t: t, pick: pickPair, walk: walk, setLang: setLang,
    currentLang: currentLang, syncToggle: syncToggle, PAIR: PAIR
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})(window);
