"""Per-request language resolution and bilingual message picking.

Klado's copy convention predates i18n: a user-facing string is written as
``中文 / English`` — one slash with spaces on both sides, Chinese on the left,
English on the right. That convention is the translation memory:

* ``pick(message, lang)`` returns the side matching ``lang``;
* ``request_lang(request)`` resolves the caller's language from
  ``Accept-Language`` (set by every browser; agents usually send none).

When ``lang`` is ``None`` — an API client that expressed no preference —
``pick`` returns the string unchanged, i.e. the historical bilingual form.
Browsers therefore always get one language; machine callers see exactly what
they saw before.

⚠️ **The split rule lives here and in the frontend, and they must stay identical.**
`frontend/out/i18n.js` has its own copy. A message that splits in one and not the other
shows the reader both languages at once, which is the one outcome the convention exists
to prevent. Both halves changed together; changing only this one is a bug.
"""
from __future__ import annotations

import re

SUPPORTED = ("zh", "en")

# Chinese on the left, " / ", Latin on the right. The lookahead requires at least
# one CJK character somewhere before the slash; the right side must start with a
# letter. That combination never occurs in paths ("POST /api/…" has no space after
# the slash) or in numeric ranges ("1 / 2" has no CJK).
# ⚠️ The CJK class is written as \u escapes, not literal characters, so the pattern
# cannot be broken by a file being read under the wrong encoding. It is the backend
# half of the pair-splitting rule; the frontend half lives in `frontend/out/i18n.js`
# and the two must stay identical.
_PAIR = re.compile(r"^(?=[\s\S]*[\u4e00-\u9fff])([\s\S]*?)\s+/\s+([A-Za-z][\s\S]*)$")


def split_pair(message: str) -> tuple[str, str] | None:
    """`(zh, en)` when the string follows the bilingual convention, else None."""
    if not message:
        return None
    m = _PAIR.match(message.strip())
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def pick(message: str, lang: str | None) -> str:
    """The message in `lang`, or the original string when lang is None/unknown."""
    if lang not in SUPPORTED:
        return message
    pair = split_pair(message)
    if pair is None:
        return message
    return pair[0] if lang == "zh" else pair[1]


def request_lang(request) -> str | None:
    """`'zh'` / `'en'` from the request's `Accept-Language`, else None.

    Reads `request.state.lang` when the middleware already resolved it; falls back to
    parsing the header directly so early error paths can use it too.

    Every lookup goes through `getattr`: the callers are error paths, and an
    identity resolver is exactly the code that tests hand a bare stand-in object
    to. A missing `state` or `headers` means "no language expressed", never a
    crash on the way to a 401.
    """
    cached = getattr(getattr(request, "state", None), "lang", None)
    if cached in SUPPORTED:
        return cached
    headers = getattr(request, "headers", None)
    raw = headers.get("accept-language", "") if headers is not None else ""
    return parse_accept_language(raw)


def parse_accept_language(header: str) -> str | None:
    """The first supported tag in an `Accept-Language` header, honouring q-values.

    `zh-CN,en;q=0.8` → `'zh'`; `en-US` → `'en'`; empty / no supported tag → None.
    """
    if not header:
        return None
    entries: list[tuple[float, str]] = []
    for part in header.split(","):
        tag, _, rest = part.partition(";")
        q = 1.0
        for param in rest.split(";"):
            name, _, value = param.strip().partition("=")
            if name == "q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
        entries.append((q, tag.strip()))
    for _, tag in sorted(entries, key=lambda e: -e[0]):
        lower = tag.lower()
        if lower.startswith("zh"):
            return "zh"
        if lower.startswith("en"):
            return "en"
    return None
