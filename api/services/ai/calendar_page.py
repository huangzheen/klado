"""The event page template, applied server-side.

Two jobs, both about making every event page look the same and tell the truth:

1. **Fill the data-driven slots** (`data-ev="…"`) from the event's own fields. This is
   what turns the attachments into real **links** — the URL shape is decided here, with
   the mount point resolved (`r/<slug>`, `?kb=<slug>`, `api/storage/serve?path=…`) — so
   an author never hand-writes a URL, and the page can no longer disagree with the event
   it belongs to (the attachment list on screen *is* `event.attachments`).
2. **Inject `vendor/event-page.css`, last**, so the template's fixed boxes win over an
   author's own CSS (same trick as `routers/reports.py::_inline_deck_runtime`).

⚠️ A page with no slots is returned **byte-for-byte unchanged**: every event published
before this template existed keeps rendering exactly as it was published.

⚠️ Links are **relative** (`r/…`, not `/r/…`). A leading slash resolves against the HOST
root and lands on the platform gateway, which answers `{"code":1002,…}` — the same trap
as the file-library Download button and the platform-export fonts.
"""
from __future__ import annotations

import html
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from core.config import resolve_frontend_dir

ASSET = "event-page.css"
_ASSET_CACHE: dict[str, str | None] = {}

# The boxes this module knows how to fill. `desc` is authored prose and is deliberately NOT
# here — only things that must match the event exactly are.
#
# ⚠️ `todo` USED to be authored prose too, and it was the only thing on the page the event
# did not know about. It is a slot now because a to-do line carries **who** and **when**
# (`store.Todo`), which prose cannot — and that is what lets the calendar draw it as a
# milestone. A page that still hard-codes `<ul class="ev-todo"><li>…` keeps rendering
# exactly as written; only an EMPTY `<ul class="ev-todo" data-ev="todo"></ul>` is filled.
SLOTS = ("kicker", "title", "schedule", "deadline", "partners", "attachments", "todo")

# English, like the rest of the page chrome ("Partners", "Deadline"): one label that reads
# on both the `data-lang` slides, instead of filling the Chinese slide with Chinese and the
# English one with a mismatch.
_KIND_WORD = {"report": "Report", "knowledge": "Wiki", "file": "File"}


def _asset() -> str:
    """Read the template stylesheet once per process (None → no style injected)."""
    if ASSET not in _ASSET_CACHE:
        path = os.path.join(resolve_frontend_dir(), "vendor", ASSET)
        try:
            with open(path, encoding="utf-8") as handle:
                _ASSET_CACHE[ASSET] = handle.read()
        except OSError:
            _ASSET_CACHE[ASSET] = None
    return _ASSET_CACHE[ASSET] or ""


def _insert_before(html_text: str, marker: str, block: str) -> str:
    at = html_text.rfind(marker)
    if at < 0:
        return html_text + block
    return html_text[:at] + block + html_text[at:]


def _slot_re(name: str) -> re.Pattern[str]:
    """An **empty** element carrying `data-ev="name"`.

    Empty is the documented contract (`<div data-ev="attachments"></div>`): it keeps the
    fill a plain string replacement instead of an HTML round-trip, which would rewrite the
    author's document (attribute order, self-closing tags, stray doctype) as a side effect.
    """
    return re.compile(
        r'(<(?P<tag>[a-zA-Z][\w-]*)\b[^>]*\bdata-ev=["\']' + re.escape(name) + r'["\'][^>]*>)'
        r'\s*(</(?P=tag)>)', re.I)


def _pretty_name(email: str) -> str:
    """`jane.doe@example.com` → `Jane Doe`.

    The spec asks for the person's name with the address as a small note, and the store only
    knows addresses. A local-part prettification is honest (the real address is printed right
    next to it) and beats showing a bare `jane.doe@example.com` as if it were a name.
    """
    local = (email or "").split("@", 1)[0]
    words = [part for part in re.split(r"[._\-+]+", local) if part]
    return " ".join(word[:1].upper() + word[1:] for word in words) or email


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else ("" if value is None else str(value))


def attachment_href(attachment, *, base_href: str = "/") -> str:
    """Where an attachment reference points, relative to the page's `<base>`.

    `base_href` is accepted for callers that want an absolute URL in a message; the page
    itself always uses the relative form.
    """
    kind, slug = attachment.kind, attachment.slug
    if kind == "report":
        return "r/" + quote(slug, safe="")
    if kind == "knowledge":
        return "?kb=" + quote(slug, safe="")
    # kind == "file": an OSS object key, served through the storage route
    return "api/storage/serve?path=" + quote(slug, safe="") + "&download=1"


def slot_html(name: str, event, *, base_href: str = "/") -> str | None:
    """The markup for one slot, or None when this module does not know the name."""
    if name == "kicker":
        # Deliberately empty since 2026-09-30. It used to render `KIND · category`, and the
        # head band now carries the page's own TITLE instead (see `event-page.css`): a reader
        # wants to know which page this is, and the kind/category are already on the calendar
        # tooltip and in the event panel. Emptying the slot (rather than dropping the name from
        # SLOTS) is what keeps the authored skeleton working — an author may still write the
        # span, and the stylesheet hides it too, so old pages stop showing a stale kicker.
        return ""
    if name == "title":
        return html.escape(getattr(event, "title", "") or "")
    if name == "schedule":
        start, end = _iso(getattr(event, "start_date", "")), _iso(getattr(event, "end_date", ""))
        if not start:
            return '<span class="ev-empty">—</span>'
        text = start + (" → " + end if end and end != start else "")
        return html.escape(text)
    if name == "deadline":
        deadline = _iso(getattr(event, "deadline", ""))
        if not deadline:
            return '<span class="ev-deadline is-none">—</span>'
        return '<span class="ev-deadline">' + html.escape(deadline) + "</span>"
    if name == "partners":
        partners = list(getattr(event, "partners", []) or [])
        if not partners:
            return '<div class="ev-empty">—</div>'
        return "".join(
            '<div class="ev-person">' + html.escape(_pretty_name(email))
            + "<small>" + html.escape(email) + "</small></div>"
            for email in partners)
    if name == "todo":
        # Rows, not a `<div>`: the slot is an empty `<ul class="ev-todo">`, so the list
        # styles already in the template (`.ev-todo li`) keep working, and a page that
        # still writes its own `<li>`s is untouched.
        todos = list(getattr(event, "todos", []) or [])
        if not todos:
            return '<li class="ev-todo-none">' + html.escape("No to-do items yet") + "</li>"
        rows = []
        for item in todos:
            meta = ""
            if item.assignee:
                meta += ('<b title="' + html.escape(item.assignee, quote=True) + '">'
                         + html.escape(_pretty_name(item.assignee)) + "</b>")
            if item.due:
                # ⚠️ A to-do's `due` is the day it is drawn as a milestone on the calendar,
                # so it is printed in full: `2026-10-12` is the value the owner set there.
                meta += '<time datetime="' + html.escape(item.due, quote=True) + '">' \
                        + html.escape(item.due) + "</time>"
            rows.append(
                '<li class="ev-todo-row' + (" is-done" if item.done else "") + '">'
                + '<span class="ev-todo-text">' + html.escape(item.text) + "</span>"
                + ('<span class="ev-todo-meta">' + meta + "</span>" if meta else "")
                + "</li>")
        return "".join(rows)
    if name == "attachments":
        attachments = list(getattr(event, "attachments", []) or [])
        if not attachments:
            return '<div class="ev-empty">—</div>'
        rows = []
        for item in attachments:
            label = item.title or item.slug
            word = _KIND_WORD.get(item.kind, item.kind)
            rows.append(
                '<a class="ev-link" href="' + html.escape(attachment_href(item, base_href=base_href), quote=True)
                + '" target="_blank" rel="noopener">'
                + "<span>" + html.escape(label) + "</span>"
                + "<small>" + html.escape(word) + "</small></a>")
        return '<div class="ev-links">' + "".join(rows) + "</div>"
    return None


def _todo_list_re() -> re.Pattern[str]:
    """Any `.ev-todo` element, whatever is inside it.

    ⚠️ The to-do list needs a rule the other boxes do not: `<ul class="ev-todo">` is what
    every page published before 2026-09-30 has, with the lines **hand-written as `<li>`s**.
    Those pages carry no `data-ev="todo"` at all, so filling the slot would leave them
    showing their old prose for ever — and the owner would edit the to-do list in the panel
    and see nothing change on their own page. The event's field wins (see `render`).
    """
    return re.compile(
        r'(<(?P<tag>ul|ol)\b[^>]*\bclass=["\'][^"\']*\bev-todo\b[^"\']*["\'][^>]*>)'
        r'.*?(</(?P=tag)>)', re.I | re.S)


# A page can list more lines than a panel can usefully edit; past this the rest are dropped
# rather than dragging a 500-item list through the editor.
_MAX_AUTHORED_TODOS = 50


def authored_todos(body: str) -> list[str]:
    """The to-do lines a page writes by hand, as plain text.

    ⚠️ This exists because of a real misalignment, reported 2026-09-30: `/e/<slug>` prints the
    event's `todos` as soon as the event HAS any, so an owner who added one to-do watched the
    page's hand-written lines be replaced — lines the editor had never shown them, because the
    editor only ever read `event.todos`. Read the page's own lines back (via `_todo_list_re`,
    so there is ONE owner of that HTML knowledge) and the editor can show them as rows: the
    owner gives them an owner and a date instead of silently losing them, and what the panel
    edits is what the page displays.

    Empty when the list is missing or has no text; the caller decides when to use it.
    """
    if not body:
        return []
    match = _todo_list_re().search(body)
    if not match:
        return []
    inner = body[match.end(1):match.start(3)]
    lines: list[str] = []
    for item in re.finditer(r"<li\b[^>]*>(.*?)</li>", inner, re.I | re.S):
        # Tags out, entities in, whitespace collapsed: these become a text field's value, so
        # markup (`<b>`, `<a>`, `<br>`) must not leak into the editor as literal angle brackets.
        text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", item.group(1))).split())
        if text:
            lines.append(text)
        if len(lines) >= _MAX_AUTHORED_TODOS:
            break
    return lines


# ── the project description ────────────────────────────────────────────────────
# ⚠️ Same shape of rule as the to-do list, for the same reason: every page published before
# 2026-09-30 hand-writes this box, so it is matched by CLASS and not by a `data-ev` slot. Until
# then the box could only be authored — the editor panel had no field for it, which is the
# question that produced this code ("这段文本在哪里可以编辑？").
def _desc_re() -> re.Pattern[str]:
    """Any `.ev-desc` element, whatever is inside it."""
    return re.compile(
        r'(<(?P<tag>p|div)\b[^>]*\bclass=["\'][^"\']*\bev-desc\b[^"\']*["\'][^>]*>)'
        r'.*?(</(?P=tag)>)', re.I | re.S)


_LANG_SECTION_RE = re.compile(
    r'<section\b[^>]*\bdata-lang=["\'](?P<lang>[A-Za-z_-]+)["\'][^>]*>', re.I)


def _sections(body: str) -> list[tuple[str, int, int]]:
    """(lang, inner-start, inner-end) for every `<section data-lang=…>` block."""
    found = []
    for match in _LANG_SECTION_RE.finditer(body):
        end = body.find("</section>", match.end())
        if end < 0:
            continue
        found.append((match.group("lang").lower(), match.end(), end))
    return found


def _plain_text(inner: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", inner)).split())


def _fill_lang_description(body: str, lang: str, text: str) -> str:
    """Put `text` into the `.ev-desc` of the slide authored for `lang`.

    A slide with no description box is left alone — an author who never wrote one gets the
    panel's text only if the template has somewhere to put it.
    """
    edits = []
    blocks = _sections(body) or [("", 0, len(body))]
    for name, start, end in blocks:
        if name != lang:
            continue
        block = body[start:end]
        filled = _desc_re().sub(
            lambda match, _c=html.escape(text): match.group(1) + _c + match.group(3), block)
        if filled != block:
            edits.append((start, end, filled))
    for start, end, filled in reversed(edits):
        body = body[:start] + filled + body[end:]
    return body


def authored_descs(body: str) -> dict[str, str]:
    """The description each slide writes by hand, keyed by `data-lang` ("" = single-language).

    The panel reads this so that opening an event shows the text the reader sees, exactly like
    `authored_todos` — otherwise the owner edits an empty box while the page displays prose.
    """
    out: dict[str, str] = {}
    if not body:
        return out
    blocks = _sections(body) or [("", 0, len(body))]
    for name, start, end in blocks:
        match = _desc_re().search(body[start:end])
        if match:
            text = _plain_text(body[start + match.end(1):start + match.start(3)])
            if text:
                out[name] = text
    return out


def render(body: str, event, *, base_href: str = "/") -> str:
    """Fill the template's slots and inject its stylesheet.

    A page this module does not know → returned unchanged (older events must keep rendering
    as published). A slot this module does not know is left alone rather than emptied.
    """
    todos = list(getattr(event, "todos", []) or [])
    descriptions = {lang: (getattr(event, "description_" + lang, "") or "").strip()
                    for lang in ("en", "zh")}
    # ⚠️ `todos` and the descriptions also bring in pages with NO slots at all: a `body`
    # written before the template existed can still carry a hand-written `.ev-todo` list or a
    # `.ev-desc` box, and the owner's edit in the panel has to reach them (see below). With
    # neither, the old rule stands exactly.
    if not body or ("data-ev=" not in body and not todos and not any(descriptions.values())):
        return body
    out = body
    for name in SLOTS:
        content = slot_html(name, event, base_href=base_href)
        if content is None:
            continue
        out = _slot_re(name).sub(lambda match, _c=content: match.group(1) + _c + match.group(3), out)
    # The to-do list, again, for the pages that write their own `<li>`s: when the event has
    # to-dos they replace the authored lines — that is what makes the owner's edit visible on
    # a page published before the field existed. With no to-dos the page is left alone: an
    # empty field must not wipe what an author wrote.
    if todos:
        rows = slot_html("todo", event, base_href=base_href) or ""
        out = _todo_list_re().sub(
            lambda match, _c=rows: match.group(1) + _c + match.group(3), out)
    # The description, again for the pages that write their own: the panel's text goes into the
    # slide of ITS language. An empty field leaves that slide untouched, so an author who wrote
    # prose (and an owner who only filled one language) both keep what they had.
    for lang, text in descriptions.items():
        if text:
            out = _fill_lang_description(out, lang, text)
    css = _asset()
    if css:
        out = _insert_before(out, "</head>", "<style data-event-page>\n" + css + "\n</style>")
    return out