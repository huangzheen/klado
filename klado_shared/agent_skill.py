"""The agent skill package, as a set of documents an operator can read.

What this is
------------
``agent_skill/klado/`` in the repository becomes ``frontend/out/agent.zip`` — the file a
user downloads from ``<base>/agent.zip`` and hands to their AI assistant. This module reads that
**zip**, so the console shows the package an agent would actually receive rather than a
description of it.

⚠️ **The zip, not the source directory.** This is not a preference, it is the whole point.
The source and the zip are two files, and a deploy that ships a stale zip is a
perfectly ordinary way to end up with an agent running yesterday's rules while the
repository says otherwise. ``api/tests/test_agent_skill_package.py`` compares them and
fails the build when they differ, so reading the zip cannot silently go stale. Reading
``agent_skill/`` instead would create a second answer to "what did the agent get" — and
two answers is exactly what this package exists to prevent (same rule as
`klado_shared/knowledge.py`).

When the zip is missing the module says so instead of falling back. A fallback would
make the page look fine on a machine where the package was never built, which is the one
case where an operator most needs to be told.

Why here, and not in ``api/services/``
--------------------------------------
The console is a separate process on a separate port and **must not import ``api.*``**
(``api/tests/test_shared_layer.py`` fails the build otherwise). The console's routers
call a ``klado_shared`` module and nothing else, so the reading has to live here.

Read-only by construction
-------------------------
There is no write path. The package changes by editing ``agent_skill/klado/`` and running
``scripts/build_agent_skill.sh``; it is a build artifact like the manual, and a
console-side edit would put the zip out of step with the source in one step — the exact
divergence the consistency test exists to catch.
"""
from __future__ import annotations

import os
import re
import zipfile
from typing import Any

from klado_shared.config import REPO_DIR

#: The built package. Overridable for a checkout that keeps it elsewhere, and for tests;
#: built from :data:`REPO_DIR` rather than the caller's cwd, because the two entry
#: points run from different directories (``cd api`` versus ``cd api-admin``).
def package_path() -> str:
    return os.environ.get("AGENT_SKILL_ZIP", "").strip() or os.path.join(
        REPO_DIR, "frontend", "out", "agent.zip")

#: Hard cap on one entry's body. The largest file in the package is `SKILL.md` at ~66 KB,
#: so this is generous rather than binding; it exists so a future 20 MB asset cannot turn
#: an operator's page into a response nobody wants. Truncation is reported, not silent —
#: a viewer showing the first 256 KB as if it were the whole file would be lying.
MAX_CONTENT_CHARS = 256_000

#: The root folder inside the zip. Entries are reported **without** it: `SKILL.md` reads
#: better in a list than `klado/SKILL.md`, and the folder is the same on every entry.
PACKAGE_ROOT = "klado/"

#: How an entry is rendered. Markdown goes through the console's markdown renderer; the
#: rest is shown as source. The key is the file extension and is OPEN, like
#: `document_type` on the manual page — an unknown extension is still listable and
#: readable, it just renders as source.
_DOC_KINDS = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".html": "source",
    ".htm": "source",
    ".py": "source",
    ".sh": "source",
    ".json": "source",
    ".css": "source",
    ".js": "source",
    ".txt": "source",
    ".yml": "source",
    ".yaml": "source",
}

#: `SKILL_PACKAGE_VERSION: 2026-10-02.03` — the line in SKILL.md that says which
#: generation of the package is being held. Read out of the body rather than parsed from
#: front matter because the file itself calls it the only thing that identifies the
#: generation ("the only thing that says which generation of the package you are holding").
_STAMP = re.compile(r"^SKILL_PACKAGE_VERSION:[ \t]*(\S+)[ \t]*$", re.M)

#: The SKILL.md front matter, which is a YAML block this module deliberately does not
#: parse — it only needs `name` and `description`, and a hand-rolled reader of a YAML
#: header is a way to be wrong in a new place.
_FM_KEY = re.compile(r"^(name|description):[ \t]*(.*)$")


class PackageMissing(RuntimeError):
    """The package has not been built, or is not where this checkout keeps it.

    A distinct exception rather than an empty list: "there is no package" and "the
    package has no files" must not look the same on screen.
    """


def _archive() -> zipfile.ZipFile:
    path = package_path()
    if not os.path.isfile(path):
        raise PackageMissing(f"agent.zip not found at {path}")
    return zipfile.ZipFile(path)


def _entry_name(name: str) -> str:
    return name[len(PACKAGE_ROOT):] if name.startswith(PACKAGE_ROOT) else name


def _label(name: str) -> str:
    """What the list shows as the row's title.

    The basename, because that is how the file is referred to inside the package — every
    reference in `SKILL.md` is by name, not by path. The full path is right beside it.
    """
    return name.rsplit("/", 1)[-1] or name


def _kind(name: str) -> str:
    _, ext = os.path.splitext(name)
    return _DOC_KINDS.get(ext.lower(), "source")


def _read(name: str) -> str:
    with _archive() as archive:
        try:
            raw = archive.read(name)
        except KeyError as exc:
            raise PackageMissing(f"entry not in package: {name}") from exc
    # `replace` rather than `strict`: a package that is not valid utf-8 should still be
    # readable as text, and the console renders source files inside `<pre>`. Silently
    # dropping the file would be worse than showing a replacement character.
    return raw.decode("utf-8", errors="replace")


def _front_matter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for line in text[3:end].splitlines():
        m = _FM_KEY.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"')
    return out


def _entry(name: str, info: zipfile.ZipInfo) -> dict[str, Any]:
    return {
        "path": _entry_name(name),
        "label": _label(_entry_name(name)),
        "kind": _kind(name),
        "bytes": info.file_size,
        "zip_path": name,
    }


def list_entries(*, search: str = "") -> list[dict[str, Any]]:
    """Every file in the package, SKILL.md first, then the rest by path.

    The zip stores directories as zero-length entries. They are skipped here: the path
    already carries the hierarchy (``references/dashboards/…``), and a row that cannot be
    opened is a row that teaches nothing.
    """
    with _archive() as archive:
        entries = [_entry(i.filename, i) for i in archive.infolist() if not i.is_dir()]
    entries.sort(key=lambda e: (e["path"] != "SKILL.md", e["path"]))
    term = (search or "").strip().lower()
    if term:
        entries = [e for e in entries
                   if term in e["path"].lower() or term in e["label"].lower()]
    return entries


def get_entry(name: str) -> dict[str, Any] | None:
    """One file's content, or ``None`` when the package has no such entry.

    The lookup is **by exact name from the listing**, never by joining a caller-supplied
    string onto a path. A zip is read, never extracted, so there is no traversal to guard
    against — but the guard is that the name has to be one the package actually has,
    which also keeps ``..`` out of every query by construction.
    """
    name = (name or "").strip().lstrip("/")
    if not name:
        return None
    with _archive() as archive:
        wanted = PACKAGE_ROOT + name
        info = archive.getinfo(wanted) if wanted in archive.namelist() else None
        if info is None and name in archive.namelist():
            info = archive.getinfo(name)
        if info is None:
            return None
        raw = archive.read(info)
    text = raw.decode("utf-8", errors="replace")
    entry = _entry(info.filename, info)
    entry["content"] = text[:MAX_CONTENT_CHARS]
    entry["truncated"] = len(text) > MAX_CONTENT_CHARS
    if entry["path"] == "SKILL.md":
        entry["front_matter"] = _front_matter(text)
        stamp = _STAMP.search(text)
        entry["package_version"] = stamp.group(1) if stamp else None
    return entry


def summary() -> dict[str, Any]:
    """The package's own facts: what it is, how big, and what generation it is.

    The version comes out of SKILL.md's own stamp rather than being recorded here — the
    same reason `api/tests/test_agent_skill_package.py` reads it from the file: a copy of
    the number in this module is a copy that can be wrong.
    """
    with _archive() as archive:
        info = archive.getinfo(PACKAGE_ROOT + "SKILL.md") \
            if PACKAGE_ROOT + "SKILL.md" in archive.namelist() else None
        stamp_text = archive.read(info).decode("utf-8", errors="replace") if info else ""
    stamp = _STAMP.search(stamp_text)
    front = _front_matter(stamp_text)
    entries = list_entries()
    kinds: dict[str, int] = {}
    for entry in entries:
        kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
    return {
        "entries": len(entries),
        "total_bytes": sum(e["bytes"] for e in entries),
        "zip_bytes": os.path.getsize(package_path()),
        "by_kind": [{"kind": k, "entries": n}
                    for k, n in sorted(kinds.items())],
        "package_version": stamp.group(1) if stamp else None,
        "name": front.get("name"),
        "description": front.get("description"),
        "built": True,
    }


def missing() -> dict[str, Any] | None:
    """The fact to show when there is no package, or ``None`` when there is one.

    ⚠️ A **missing package is a state, not an error.** It happens on any checkout where
    nobody ran ``scripts/build_agent_skill.sh``, and an operator opening this page is
    asking "what would the agent get" — "nothing, and here is why" is a better answer
    than a 500. So the router calls this first and renders a state; it only calls
    :func:`summary` when this returns ``None``.

    Every key :func:`summary` returns is present here too, so the page has one shape to
    render rather than two.
    """
    if os.path.isfile(package_path()):
        return None
    path = package_path()
    return {
        "built": False,
        "reason": f"agent.zip not found at {path}",
        "entries": 0, "total_bytes": 0, "zip_bytes": 0, "by_kind": [],
        "package_version": None, "name": None, "description": None,
    }
