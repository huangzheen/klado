/* ════════════════════════════════════════════════════════════════════════════
   theme.js — light/dark switching.

   The attribute `<html data-theme>` is what actually selects a palette; everything
   else here is bookkeeping. A theme is chosen in this order:

     1. what the reader picked last time (localStorage),
     2. the operating system preference (`prefers-color-scheme`),
     3. light.

   `data-bs-theme` is kept in lock-step so Bootstrap/Tabler components follow.

   ⚠️ The first paint must not flash the wrong theme, so an inline copy of
   `apply()` runs in <head> before the stylesheets. Keep the two in sync — the
   inline one is deliberately tiny and does the same three steps.
   ════════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var KEY = 'klado-theme';
  var THEMES = ['light', 'dark'];

  function stored() {
    try {
      var v = localStorage.getItem(KEY);
      return THEMES.indexOf(v) >= 0 ? v : null;
    } catch (e) {
      return null;  // private mode / storage disabled — fall through to the OS
    }
  }

  function systemPrefers() {
    try {
      return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
        ? 'dark' : 'light';
    } catch (e) {
      return 'light';
    }
  }

  function current() {
    var attr = document.documentElement.getAttribute('data-theme');
    return THEMES.indexOf(attr) >= 0 ? attr : 'light';
  }

  function apply(theme) {
    if (THEMES.indexOf(theme) < 0) theme = 'light';
    document.documentElement.setAttribute('data-theme', theme);
    document.documentElement.setAttribute('data-bs-theme', theme);
    var meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.setAttribute('content', theme);
  }

  /* An iframe is a SEPARATE document with its own <html data-theme>, and it read
   * localStorage once — when it booted. The Inbox preview is one of these: the
   * reader opens a message in light, then hits the theme toggle, and the frame
   * stays light because nothing here ever told it. Push the change down.
   *
   * `persist: false` on the child: localStorage is per-origin and already holds
   * the new value (the top frame wrote it), so the child must not re-write it.
   * Only same-origin frames that boot this same script are touched; a report's
   * `/r/<slug>` document carries its own colours and has no `kladoTheme`. */
  function applyToFrames(theme) {
    var frames;
    try {
      frames = document.querySelectorAll('iframe');
    } catch (e) {
      return;
    }
    Array.prototype.forEach.call(frames, function (frame) {
      try {
        var win = frame.contentWindow;
        // A cross-origin frame throws on access; that is a document we do not own.
        if (!win || !win.kladoTheme) return;
        if (win.document.documentElement.getAttribute('data-theme') === theme) return;
        win.kladoTheme.set(theme, false);
      } catch (e) { /* cross-origin or not loaded yet */ }
    });
  }

  function set(theme, persist) {
    apply(theme);
    if (persist !== false) {
      try { localStorage.setItem(KEY, theme); } catch (e) { /* not fatal */ }
    }
    syncButtons();
    applyToFrames(theme);
    // Charts read their colours once, at construction, so a theme change cannot
    // repaint them by itself. Let the pages redraw whatever is on screen.
    try {
      document.dispatchEvent(new CustomEvent('klado:theme', { detail: { theme: theme } }));
    } catch (e) { /* older engines: the event is a nicety, not a requirement */ }
  }

  function toggle() {
    var root = document.documentElement;
    // Suppress transitions for one frame: without this every surface animates at
    // once and the switch reads as a flicker rather than a repaint.
    root.classList.add('theme-switching');
    set(current() === 'dark' ? 'light' : 'dark');
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { root.classList.remove('theme-switching'); });
    });
  }

  /* The icon shows the theme you would switch *to*, which is the convention readers
     already know from every other app. The chip next to it shows the theme you are
     IN — same split as the language row, and the tooltip states the action in the
     language it is read in (it used to be English-only, which is wrong on a Chinese
     page now that this is a menu row rather than a nav button). */
  function syncButtons() {
    var dark = current() === 'dark';
    var target = dark ? 'light' : 'dark';
    var zh = false;
    try { zh = !!(window.kladoI18n && window.kladoI18n.lang && window.kladoI18n.lang() === 'zh'); }
    catch (e) { /* i18n.js not loaded yet — fall back to English below */ }
    var label = zh
      ? (dark ? '切换到浅色' : '切换到深色')
      : (dark ? 'Switch to light theme' : 'Switch to dark theme');
    var chip = dark ? (zh ? '深色' : 'Dark') : (zh ? '浅色' : 'Light');
    Array.prototype.forEach.call(document.querySelectorAll('[data-theme-toggle]'), function (btn) {
      btn.setAttribute('title', label);
      btn.setAttribute('aria-label', label);
      var icon = btn.querySelector('i');
      if (icon) icon.className = dark ? 'ri-sun-line' : 'ri-moon-line';
      var out = btn.querySelector('[data-theme-label]');
      if (out) out.textContent = chip;
    });
  }

  function init() {
    apply(stored() || systemPrefers());
    syncButtons();
    // The theme chip and tooltip are written in the READER's language, so they have to
    // be recomputed when that language changes. i18n.js re-walks the DOM and re-runs its
    // own `syncToggle()` on `klado:lang`; without this the theme row kept whichever
    // language happened to be live at boot. (Boot order is not guaranteed — this is why
    // `syncButtons` treats a missing `kladoI18n` as "not zh" and corrects itself here.)
    try {
      document.addEventListener('klado:lang', function () { syncButtons(); });
    } catch (e) { /* older engines: the chip stays in the boot language */ }
    // Follow the OS only while the reader has not made an explicit choice.
    try {
      if (window.matchMedia) {
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function (e) {
          if (!stored()) apply(e.matches ? 'dark' : 'light'), syncButtons();
        });
      }
    } catch (e) { /* Safari < 14 has no addEventListener on MediaQueryList */ }
  }

  window.kladoTheme = {
    init: init, current: current, set: set, toggle: toggle,
    themes: THEMES.slice(), key: KEY
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();