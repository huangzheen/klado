/* Reader notes on a document — one implementation, three hosts.
 *
 *   1. a served Workspace report  (inlined by services/annotations.py)
 *   2. a served calendar event page (inlined by routers/calendar.py)
 *   3. the wiki reading pane inside the SPA (loaded with a <script src>)
 *
 * Why one file and not three: this is the whole interaction the reader has with
 * the feature — right-click, type, see a bubble, toggle it off. A per-host copy
 * would be three answers to "where does a note land on a scaled deck", and the
 * wrong one is invisible until somebody reports that their notes are 5px tall.
 *
 * The hosts differ only in WHERE the anchor box is:
 *
 *   deck      — a fixed overlay the size of the visible slide. NOT inside the
 *               slide: `.deck` is scaled with transform:scale(), so a bubble
 *               placed inside it shrinks to unreadable at 0.42.
 *   pages     — the same fixed overlay, but the box follows the SCROLL: the
 *               paginated previews of a Workspace document card (a docx rendered
 *               as sheets of paper, a deck of slides, an xlsx grid, a PDF) put
 *               one sheet under the fold at a time, and a note belongs to the
 *               sheet, not to "62% of a long page".
 *   document  — an absolute layer spanning the whole scrolled document, so notes
 *               scroll with the text instead of floating over one screenful.
 *   container — an absolute layer inside a host element (the wiki pane).
 *
 * A position is therefore always a PERCENTAGE of the anchor box plus a page
 * index, never pixels. See api/services/annotations.py for why.
 */
(function () {
  'use strict';

  var API = { mounted: null };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function clampPct(value) {
    var number = Number(value);
    if (!isFinite(number)) return 0;
    return Math.min(100, Math.max(0, number));
  }

  function shortWhen(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    var pad = function (n) { return String(n).padStart(2, '0'); };
    return (d.getFullYear() === new Date().getFullYear() ? '' : d.getFullYear() + '-')
      + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function who(email) {
    var text = String(email || '');
    var at = text.indexOf('@');
    return at > 0 ? text.slice(0, at) : text;
  }

  // ── one mounted instance ────────────────────────────────────────────────────

  function Session(cfg) {
    this.cfg = cfg || {};
    this.notes = [];
    this.visible = this._storedVisible();
    this.layer = null;
    this.menu = null;
    this.composer = null;
    this.openId = null;
    this.alive = true;
    this._timers = [];
    this._observers = [];
  }

  Session.prototype._storageKey = function () {
    return 'doc-anno:' + this.cfg.type + ':' + this.cfg.slug;
  };

  // Default is SHOWN. A reader who has hidden them once keeps them hidden for
  // that document only — the preference is per document, never global, because
  // "hide the notes on this dense page" is a decision about that page.
  Session.prototype._storedVisible = function () {
    try {
      return window.localStorage.getItem(this._storageKey()) !== '0';
    } catch (e) {
      return true;
    }
  };

  Session.prototype._storeVisible = function () {
    try {
      window.localStorage.setItem(this._storageKey(), this.visible ? '1' : '0');
    } catch (e) { /* private mode: the toggle still works for this session */ }
  };

  Session.prototype.url = function (path) {
    // The document's own <base href> IS the mount point, and it is correct in every
    // environment: the server injects it into a served report/event page, and a
    // downloaded self-contained document carries it. Prefer it over the value baked
    // in at render time — a report downloaded from production and opened on a laptop
    // has the production base baked into its boot script, and the only thing that can
    // still be right there is the base tag the document already declares.
    var base = this.cfg.base || './';
    var docBase = document.baseURI || '';
    if (docBase.charAt(docBase.length - 1) === '/') base = docBase;
    if (base.charAt(base.length - 1) !== '/') base += '/';
    return base + path;
  };

  Session.prototype.readUrl = function () {
    // `cfg.api` is what the server injects: the `/api/annotations` endpoint for a
    // signed-in reader, or a token-scoped read-only URL for a guest on a
    // `/s/{token}` link. Absent (the SPA mounts the pane) it is the normal one.
    return this.cfg.api || this.url('api/annotations');
  };

  Session.prototype.writeUrl = function (path) {
    return this.url('api/annotations' + path);
  };

  // ── host resolution ────────────────────────────────────────────────────────

  Session.prototype.hostMode = function () {
    if (this.cfg.container) return 'container';
    // A paginated Office preview (docx/xlsx/pptx/pdf cards) is a long scroll of sheets, so
    // "where does this note belong" is answered per SHEET, not per document. Checked before
    // the deck: those previews never contain `.deck .slide`, and a report that somehow had
    // both would still want its deck.
    if (this.cfg.pages && document.querySelector(this.cfg.pages)) return 'pages';
    if (document.querySelector('.deck .slide')) return 'deck';
    return 'document';
  };

  // The sheets of a paginated preview, in document order. The index in this list IS the
  // note's `page`, which is what makes the number mean the same thing on every screen size.
  Session.prototype.pageElements = function () {
    if (!this.cfg.pages) return [];
    return [].slice.call(document.querySelectorAll(this.cfg.pages));
  };

  // Which sheet is the reader looking at? The one the middle of the viewport is inside;
  // failing that (a sheet taller than the window, or the gap between two), the nearest one.
  Session.prototype.visiblePage = function () {
    var pages = this.pageElements();
    if (!pages.length) return null;
    var middle = window.innerHeight / 2;
    var best = null;
    var bestGap = Infinity;
    for (var i = 0; i < pages.length; i++) {
      var rect = pages[i].getBoundingClientRect();
      if (!rect.height) continue;
      if (rect.top <= middle && rect.bottom >= middle) return pages[i];
      var gap = Math.abs((rect.top + rect.bottom) / 2 - middle);
      if (gap < bestGap) { bestGap = gap; best = pages[i]; }
    }
    return best;
  };

  Session.prototype.currentSlide = function () {
    if (this.layerMode === 'pages') return this.visiblePage();
    var slides = document.querySelectorAll('.deck .slide');
    if (!slides.length) return null;
    var current = document.querySelector('.deck .slide.is-current');
    if (current) return current;
    // No `.is-current`: the deck may not have initialised yet, or the reader is
    // on the "all pages" stack. The hash is the deck's own deep link, so honour
    // it before falling back to the first slide.
    var match = /^#p(\d+)$/.exec(window.location.hash || '');
    if (match) {
      var wanted = slides[parseInt(match[1], 10) - 1];
      if (wanted) return wanted;
    }
    return slides[0];
  };

  Session.prototype.slideNumber = function (slide) {
    if (!slide) return 0;
    var all = this.layerMode === 'pages' ? this.pageElements()
                                         : document.querySelectorAll('.deck .slide');
    for (var i = 0; i < all.length; i++) {
      if (all[i] === slide) return i + 1;
    }
    return 0;
  };

  // ── the layer ──────────────────────────────────────────────────────────────

  Session.prototype.ensureLayer = function () {
    // A load() that lands after stop() must not resurrect anything. Switching wiki
    // pages or leaving the app mid-fetch is normal, and the orphan this would leave
    // is a layer of somebody else's notes pinned to a page that has moved on.
    if (!this.alive) return null;
    var mode = this.hostMode();
    if (this.layer && this.layerMode === mode) return this.layer;
    if (this.layer) this.layer.remove();
    this.layerMode = mode;

    var layer = el('div', 'doc-anno-layer'
      + (mode === 'deck' || mode === 'pages' ? ' doc-anno-layer--deck' : ''));
    layer.setAttribute('data-doc-anno-layer', this.cfg.type);
    if (mode === 'deck' || mode === 'pages') {
      document.body.appendChild(layer);
    } else if (mode === 'container') {
      var host = this._resolve(this.cfg.container);
      if (!host) return null;
      if (getComputedStyle(host).position === 'static') host.style.position = 'relative';
      host.appendChild(layer);
    } else {
      // A layer as a direct child of <body> anchors to the page, not to the
      // author's own absolutely-positioned elements: giving `body` a position
      // would silently re-anchor anything in the document that already used one.
      document.body.appendChild(layer);
    }
    this.layer = layer;
    this.sync();
    return layer;
  };

  Session.prototype._resolve = function (target) {
    if (!target) return null;
    if (typeof target !== 'string') return target;
    return document.querySelector(target);
  };

  // Put the layer exactly over the anchor box, in real screen pixels.
  Session.prototype.sync = function () {
    if (!this.layer) return;
    if (this.layerMode === 'deck' || this.layerMode === 'pages') {
      var slide = this.currentSlide();
      if (!slide) return;
      var rect = slide.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      this.page = this.slideNumber(slide);
      this.layer.style.left = rect.left + 'px';
      this.layer.style.top = rect.top + 'px';
      this.layer.style.width = rect.width + 'px';
      this.layer.style.height = rect.height + 'px';
      this.layer.style.right = 'auto';
      this.layer.style.bottom = 'auto';
    } else if (this.layerMode === 'document') {
      this.page = 0;
      this.layer.style.left = '0px';
      this.layer.style.top = '0px';
      this.layer.style.width = document.documentElement.clientWidth + 'px';
      // The notes must travel with the text, so the layer is as tall as the
      // document, not as tall as the viewport.
      this.layer.style.height = Math.max(
        document.body.scrollHeight, document.documentElement.scrollHeight) + 'px';
      this.layer.style.right = 'auto';
      this.layer.style.bottom = 'auto';
    } else {
      this.page = 0;
      var host = this._resolve(this.cfg.container);
      if (!host) return;
      this.layer.style.left = '0px';
      this.layer.style.top = '0px';
      this.layer.style.width = '100%';
      this.layer.style.height = '100%';
      this.layer.style.right = 'auto';
      this.layer.style.bottom = 'auto';
    }
    this.render();
  };

  // ── data ───────────────────────────────────────────────────────────────────

  Session.prototype.load = function () {
    var self = this;
    var url = this.readUrl() + '?type=' + encodeURIComponent(this.cfg.type)
      + '&slug=' + encodeURIComponent(this.cfg.slug);
    return fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        if (!self.alive) return [];
        self.notes = (data && data.notes) || [];
        self.ensureLayer();
        self.render();
        self.updateToggle();
        return self.notes;
      })
      .catch(function (err) {
        if (!self.alive) return [];
        // A document that cannot load its notes (a revoked link, a dead session)
        // still has to read as a document. The toggle keeps working; it just has
        // nothing to show, and we say so in its tooltip instead of throwing.
        self.loadError = err && err.message ? err.message : 'unavailable';
        self.updateToggle();
        return [];
      });
  };

  Session.prototype.post = function (body) {
    return fetch(this.writeUrl(''), {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        asset_type: this.cfg.type,
        slug: this.cfg.slug,
        body: body.text,
        x: body.x,
        y: body.y,
        page: body.page || 0,
      }),
    }).then(function (res) {
      if (!res.ok) return res.json().then(function (d) { throw new Error(d.detail || ('HTTP ' + res.status)); });
      return res.json();
    });
  };

  Session.prototype.patch = function (id, payload) {
    return fetch(this.writeUrl('/' + id), {
      method: 'PATCH',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then(function (res) {
      if (!res.ok) return res.json().then(function (d) { throw new Error(d.detail || ('HTTP ' + res.status)); });
      return res.json();
    });
  };

  Session.prototype.remove = function (id) {
    return fetch(this.writeUrl('/' + id), {
      method: 'DELETE',
      credentials: 'same-origin',
    }).then(function (res) {
      if (!res.ok) return res.json().then(function (d) { throw new Error(d.detail || ('HTTP ' + res.status)); });
      return res.json();
    });
  };

  // ── rendering ──────────────────────────────────────────────────────────────

  Session.prototype.forPage = function () {
    var page = this.page || 0;
    return this.notes.filter(function (note) { return (note.page || 0) === page; });
  };

  Session.prototype.render = function () {
    if (!this.layer) return;
    this.layer.classList.toggle('is-hidden', !this.visible);
    this.layer.innerHTML = '';
    this.closeComposer(true);
    var self = this;
    this.forPage().forEach(function (note) { self.layer.appendChild(self.bubble(note)); });
    // Every bubble is visible now, not only the one the reader clicked, so every
    // one of them has to be pushed back inside the page.
    window.requestAnimationFrame(function () {
      if (!self.layer) return;
      [].forEach.call(self.layer.querySelectorAll('.doc-anno'), function (n) { self.keepInside(n); });
    });
    this.updateToggle();
  };

  Session.prototype.bubble = function (note) {
    var self = this;
    var node = el('div', 'doc-anno');
    node.dataset.id = note.id;
    node.dataset.resolved = note.resolved ? '1' : '0';
    node.style.left = clampPct(note.x) + '%';
    node.style.top = clampPct(note.y) + '%';
    if (note.resolved) node.classList.add('is-resolved');
    if (this.openId === note.id) node.classList.add('is-open');

    var dot = el('span', 'doc-anno-dot');
    dot.title = who(note.author) + ' · ' + (note.body || '');
    node.appendChild(dot);
    // `translate(-50%,-50%)` (CSS) puts the dot's CENTRE on the stored point, so the
    // number in the database is the pixel the reader actually clicked. A fixed -7px
    // offset would drift by half the dot's size and change if the dot's size changed.
    void 0;

    var card = el('div', 'doc-anno-card');
    var head = el('div');
    head.appendChild(el('span', 'doc-anno-who', who(note.author)));
    head.appendChild(el('span', 'doc-anno-when', shortWhen(note.created_at)));
    card.appendChild(head);
    card.appendChild(el('div', 'doc-anno-body', note.body || ''));

    var acts = el('div', 'doc-anno-acts');
    if (this.cfg.writable) {
      if (this.cfg.viewer && note.author === this.cfg.viewer) {
        acts.appendChild(this.action('Edit', function () { self.edit(note, node); }));
      }
      acts.appendChild(this.action(note.resolved ? 'Reopen' : 'Mark resolved', function () {
        self.setResolved(note, !note.resolved);
      }));
      if (this.cfg.viewer && note.author === this.cfg.viewer) {
        acts.appendChild(this.action('Delete', function () { self.destroy(note); }, 'danger'));
      }
    }
    if (acts.childNodes.length) card.appendChild(acts);
    node.appendChild(card);

    dot.addEventListener('click', function (event) {
      event.stopPropagation();
      self.openId = self.openId === note.id ? null : note.id;
      self.render();
      if (self.openId === note.id) self.keepInside(node);
    });
    // The bubble is already showing the text; clicking it opens the actions
    // (edit / resolve / delete). Buttons inside stop propagation themselves.
    card.addEventListener('click', function (event) {
      event.stopPropagation();
      self.openId = self.openId === note.id ? null : note.id;
      self.render();
    });
    return node;
  };

  Session.prototype.action = function (label, handler, extra) {
    var button = el('button', extra || '', label);
    button.type = 'button';
    button.addEventListener('click', function (event) {
      event.stopPropagation();
      handler();
    });
    return button;
  };

  // Keep a card inside the page: a note near the right or bottom edge would
  // otherwise open off-screen, and "the note is invisible" reads as "there is
  // no note".
  Session.prototype.keepInside = function (node) {
    if (!this.layer || !node) return;
    var card = node.querySelector('.doc-anno-card');
    if (!card) return;
    var box = this.layer.getBoundingClientRect();
    var rect = card.getBoundingClientRect();
    node.classList.toggle('flip-x', rect.right > box.right);
    node.classList.toggle('flip-y', rect.bottom > box.bottom);
  };

  // ── the toggle ─────────────────────────────────────────────────────────────

  Session.prototype.toggleNode = function () {
    if (this._toggle && this._toggle.isConnected) return this._toggle;
    var button = el('button', 'doc-anno-toggle');
    button.type = 'button';
    var anchor = this._resolve(this.cfg.buttonAnchor);
    if (anchor) {
      if (getComputedStyle(anchor).position === 'static') anchor.style.position = 'relative';
      button.classList.add('doc-anno-toggle--inline');
    }
    (anchor || document.body).appendChild(button);
    var self = this;
    button.addEventListener('click', function (event) {
      event.stopPropagation();
      self.visible = !self.visible;
      self._storeVisible();
      if (self.layer) self.layer.classList.toggle('is-hidden', !self.visible);
      self.updateToggle();
    });
    this._toggle = button;
    return button;
  };

  Session.prototype.updateToggle = function () {
    if (!this.alive) return;
    var button = this.toggleNode();
    var count = this.forPage().length;
    button.dataset.visible = this.visible ? '1' : '0';
    button.innerHTML = '';
    button.appendChild(el('i', 'ri-chat-1-line'));
    button.appendChild(el('span', 'doc-anno-toggle-n', String(count)));
    button.appendChild(el('span', null, this.visible ? 'Notes' : 'Notes off'));
    button.title = this.loadError
      ? 'Notes could not be loaded (' + this.loadError + ')'
      : (this.visible ? 'Hide the ' + count + ' note(s) on this page'
                      : 'Show the ' + count + ' note(s) on this page');
    button.setAttribute('aria-pressed', this.visible ? 'true' : 'false');
  };

  // ── the right-click menu ───────────────────────────────────────────────────

  Session.prototype.closeMenu = function () {
    if (this.menu) { this.menu.remove(); this.menu = null; }
  };

  Session.prototype.openMenu = function (x, y, items, label) {
    this.closeMenu();
    var menu = el('div', 'doc-anno-menu');
    menu.setAttribute('role', 'menu');
    if (label) menu.appendChild(el('div', 'doc-anno-menu-label', label));
    var self = this;
    items.forEach(function (item) {
      var button = el('button', item.danger ? 'danger' : '', '');
      button.type = 'button';
      button.setAttribute('role', 'menuitem');
      button.appendChild(el('i', item.icon || 'ri-arrow-right-line'));
      button.appendChild(el('span', null, item.label));
      button.addEventListener('click', function (event) {
        event.stopPropagation();
        self.closeMenu();
        item.run();
      });
      menu.appendChild(button);
    });
    document.body.appendChild(menu);
    // Clamp into the window: a right-click near the bottom-right corner would
    // otherwise open a menu that runs off both edges.
    var box = menu.getBoundingClientRect();
    menu.style.left = Math.max(6, Math.min(x, window.innerWidth - box.width - 6)) + 'px';
    menu.style.top = Math.max(6, Math.min(y, window.innerHeight - box.height - 6)) + 'px';
    this.menu = menu;
  };

  // Where inside the anchor box did the reader click? One formula for all three
  // hosts: a percentage of the LAYER's own rect, which already accounts for the
  // deck's scale, the page's scroll and the pane's padding.
  Session.prototype.pointAt = function (clientX, clientY) {
    if (!this.layer) return null;
    var box = this.layer.getBoundingClientRect();
    if (!box.width || !box.height) return null;
    return {
      x: clampPct(((clientX - box.left) / box.width) * 100),
      y: clampPct(((clientY - box.top) / box.height) * 100),
      page: this.page || 0,
    };
  };

  // The layer is `pointer-events:none`, so it is never the event target of a
  // click on the document underneath. "Did the reader click the page or the app
  // chrome?" therefore has to be answered by geometry, not by containment.
  var INTERACTIVE = 'a,button,input,textarea,select,label,summary,[contenteditable="true"],[data-no-anno]';

  Session.prototype._isInteractiveTarget = function (target) {
    // A right-click on a link must still offer "open in new tab": hijacking it
    // is how a note layer makes a document worse to read.
    return !!(target && target.closest && target.closest(INTERACTIVE));
  };

  Session.prototype.hitsContent = function (event) {
    if (this._isInteractiveTarget(event.target)) return false;
    if (this.layerMode === 'container') {
      var content = this._resolve(this.cfg.content);
      if (content) return content.contains(event.target);
      var host = this._resolve(this.cfg.container);
      return !!(host && host.contains(event.target));
    }
    if (!this.layer) return false;
    var box = this.layer.getBoundingClientRect();
    return event.clientX >= box.left && event.clientX <= box.right
        && event.clientY >= box.top && event.clientY <= box.bottom;
  };

  Session.prototype.onContextMenu = function (event) {
    if (!this.cfg.writable) return;            // never hijack a guest's browser menu
    var dot = event.target.closest && event.target.closest('.doc-anno');
    if (dot && this.layer && this.layer.contains(dot)) {
      event.preventDefault();
      event.stopPropagation();
      this.noteMenu(event, dot.dataset.id);
      return;
    }
    if (!this.hitsContent(event)) return;
    var point = this.pointAt(event.clientX, event.clientY);
    if (!point) return;
    event.preventDefault();
    event.stopPropagation();
    var self = this;
    this.openMenu(event.clientX, event.clientY, [{
      label: 'Add a note here',
      icon: 'ri-add-circle-line',
      run: function () { self.compose(point); },
    }], point.page ? 'Page ' + point.page : 'This document');
  };

  Session.prototype.noteMenu = function (event, id) {
    var note = this.notes.filter(function (n) { return String(n.id) === String(id); })[0];
    if (!note) return;
    var self = this;
    var items = [];
    if (this.cfg.viewer && note.author === this.cfg.viewer) {
      items.push({ label: 'Edit this note', icon: 'ri-edit-line', run: function () { self.edit(note); } });
    }
    items.push({
      label: note.resolved ? 'Reopen this note' : 'Mark as resolved',
      icon: note.resolved ? 'ri-refresh-line' : 'ri-check-circle-line',
      run: function () { self.setResolved(note, !note.resolved); },
    });
    if (this.cfg.viewer && note.author === this.cfg.viewer) {
      items.push({ label: 'Delete this note', icon: 'ri-delete-bin-line', danger: true,
        run: function () { self.destroy(note); } });
    }
    this.openMenu(event.clientX, event.clientY, items,
      who(note.author) + ' · ' + (note.resolved ? 'resolved' : 'open'));
  };

  // ── the composer ───────────────────────────────────────────────────────────

  Session.prototype.closeComposer = function (silent) {
    if (this.composer) { this.composer.remove(); this.composer = null; }
    if (this._lastFocus && !silent) { try { this._lastFocus.focus(); } catch (e) { /* gone */ } }
    this._lastFocus = null;
  };

  Session.prototype.compose = function (point, existing) {
    this.closeComposer(true);
    if (!this.layer) return;
    this._lastFocus = document.activeElement;
    var self = this;
    var box = el('div', 'doc-anno-compose');
    box.style.left = clampPct(point.x) + '%';
    box.style.top = clampPct(point.y) + '%';
    var area = el('textarea');
    area.placeholder = existing ? 'Edit your note…' : 'What should the author know about this spot?';
    area.value = existing ? existing.body : '';
    box.appendChild(area);
    var acts = el('div', 'doc-anno-compose-acts');
    var cancel = el('button', '', 'Cancel');
    cancel.type = 'button';
    cancel.addEventListener('click', function () { self.closeComposer(); });
    var save = el('button', 'primary', existing ? 'Save' : 'Add note');
    save.type = 'button';
    acts.appendChild(cancel);
    acts.appendChild(save);
    box.appendChild(acts);
    var error = el('div', 'doc-anno-error');
    error.hidden = true;
    box.appendChild(error);
    this.layer.appendChild(box);
    this.composer = box;

    // Flip towards the inside when the editor would open off the page.
    var layerBox = this.layer.getBoundingClientRect();
    if (box.getBoundingClientRect().right > layerBox.right) box.classList.add('flip-x');

    var submit = function () {
      var text = area.value.trim();
      if (!text) { self.fail(box, 'Write something first.'); return; }
      save.disabled = true;
      var work = existing
        ? self.patch(existing.id, { body: text })
        : self.post({ text: text, x: point.x, y: point.y, page: point.page || 0 });
      work.then(function () {
        self.closeComposer(true);
        return self.load();
      }).catch(function (err) {
        save.disabled = false;
        self.fail(box, (err && err.message) || 'Could not save the note.');
      });
    };
    save.addEventListener('click', submit);
    area.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); submit(); }
      if (event.key === 'Escape') { event.preventDefault(); self.closeComposer(); }
      event.stopPropagation();
    });
    area.focus();
  };

  Session.prototype.fail = function (box, message) {
    var error = box.querySelector('.doc-anno-error');
    if (error) { error.textContent = message; error.hidden = false; }
  };

  Session.prototype.edit = function (note) {
    this.compose({ x: note.x, y: note.y, page: note.page || 0 }, note);
  };

  Session.prototype.setResolved = function (note, resolved) {
    var self = this;
    this.patch(note.id, { resolved: !!resolved })
      .then(function () { return self.load(); })
      .catch(function (err) { window.alert(err && err.message ? err.message : 'Could not update the note.'); });
  };

  Session.prototype.destroy = function (note) {
    var self = this;
    if (!window.confirm('Delete this note? The author cannot undo it.')) return;
    this.remove(note.id)
      .then(function () { if (self.openId === note.id) self.openId = null; return self.load(); })
      .catch(function (err) { window.alert(err && err.message ? err.message : 'Could not delete the note.'); });
  };

  // ── wiring ─────────────────────────────────────────────────────────────────

  Session.prototype.start = function () {
    var self = this;
    this.alive = true;
    this.ensureLayer();
    this.toggleNode();
    this.load();

    if (this.layerMode === 'deck') {
      // The deck is one slide at a time and the layer has to follow it. Three
      // signals, because there is no single event for "the current slide
      // changed": the deck's own postMessage, its hash deep link, and a slow
      // poll for a runtime that does neither (an author-inlined older deck).
      window.addEventListener('message', function (event) {
        if (event.data && event.data.type === 'report-deck:state') self.sync();
      });
      window.addEventListener('hashchange', function () { self.sync(); });
      var deck = document.querySelector('.deck');
      if (deck && window.MutationObserver) {
        var observer = new MutationObserver(function () { self.sync(); });
        observer.observe(deck, { attributes: true, attributeFilter: ['class'], subtree: true });
        this._observers.push(observer);
      }
      this._timers.push(window.setInterval(function () { self.sync(); }, 800));
    } else if (this.layerMode === 'pages') {
      // A paginated preview scrolls, so the overlay has to be re-anchored to whichever
      // sheet is under the fold. Scroll is the real signal; resize/load and a slow poll
      // cover the layout settling afterwards (webfonts, late images, a zoom change).
      var syncSheet = function () { self.sync(); };
      window.addEventListener('scroll', syncSheet, { passive: true });
      window.addEventListener('resize', syncSheet);
      window.addEventListener('load', syncSheet);
      if (window.ResizeObserver) {
        var sheetObserver = new ResizeObserver(function () { self.sync(); });
        sheetObserver.observe(document.body);
        this._observers.push(sheetObserver);
      }
      this._timers.push(window.setInterval(function () { self.sync(); }, 900));
    } else {
      // A long document grows while the reader scrolls (lazy images, Mermaid).
      var syncSoon = function () { self.sync(); };
      window.addEventListener('resize', syncSoon);
      window.addEventListener('load', syncSoon);
      if (window.ResizeObserver) {
        var ro = new ResizeObserver(function () { self.sync(); });
        ro.observe(document.body);
        this._observers.push(ro);
      }
    }

    this._onContextMenu = function (event) { self.onContextMenu(event); };
    this._onDismiss = function (event) {
      if (event.key === 'Escape') { self.closeMenu(); self.closeComposer(); }
    };
    this._onClickAway = function (event) {
      if (self.menu && !self.menu.contains(event.target)) self.closeMenu();
      // A click inside a bubble belongs to that bubble: this listener is in the
      // CAPTURE phase, so it runs before the bubble's own handler. Re-rendering
      // here would replace the very node the reader just clicked.
      if (event.target.closest && event.target.closest('.doc-anno')) return;
      if (self.composer && !self.composer.contains(event.target)) self.closeComposer();
      var open = self.layer && self.layer.querySelector('.doc-anno.is-open');
      if (open) { open.classList.remove('is-open'); self.openId = null; }
    };
    document.addEventListener('contextmenu', this._onContextMenu, true);
    document.addEventListener('keydown', this._onDismiss, true);
    document.addEventListener('click', this._onClickAway, true);
    return this;
  };

  Session.prototype.stop = function () {
    this.alive = false;
    document.removeEventListener('contextmenu', this._onContextMenu, true);
    document.removeEventListener('keydown', this._onDismiss, true);
    document.removeEventListener('click', this._onClickAway, true);
    this._timers.forEach(function (id) { window.clearInterval(id); });
    this._timers = [];
    this._observers.forEach(function (o) { o.disconnect(); });
    this._observers = [];
    if (this.layer) { this.layer.remove(); this.layer = null; }
    if (this._toggle) { this._toggle.remove(); this._toggle = null; }
    if (this.menu) { this.menu.remove(); this.menu = null; }
    if (this.composer) { this.composer.remove(); this.composer = null; }
  };

  // ── entry point ────────────────────────────────────────────────────────────

  API.mount = function (cfg) {
    if (!cfg || !cfg.type || !cfg.slug) return null;
    if (API.mounted) API.mounted.stop();
    API.mounted = new Session(cfg).start();
    return API.mounted;
  };
  API.unmount = function () {
    if (API.mounted) { API.mounted.stop(); API.mounted = null; }
  };
  API.refresh = function () {
    return API.mounted ? API.mounted.load() : Promise.resolve([]);
  };
  API.current = function () { return API.mounted; };

  window.DocAnnotations = API;
})();
