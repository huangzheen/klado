"""The project manual — the instruction set an agent reads to work in this installation.

What this is, and what it is not
--------------------------------
Klado has **two** knowledge corpora, and this module is only one of them:

* ``ai_knowledge_documents`` — the **project manual**: how to use this system. Sourced
  from the Markdown files under ``api/knowledge_docs/`` and reconciled on startup, so the
  only writer is a deploy and every signed-in user (and every agent) reads the same
  corpus. **This module.**

* ``ai_knowledge_items`` — the **business knowledge base**: what the business knows,
  written over HTTP by people and their agents, permission-isolated per owner. The main
  app labels it 知识库 / "Knowledge base". **Not this module**, and deliberately absent:
  the console's 项目说明书 page exists so an operator can read *the instructions the agent
  is operating under*, and a page that leads with user-authored content answers a
  different question. An earlier version of this file queried both and mixed them, which
  is how a request for "the manual" came to be answered with "what people wrote down".

The two are genuinely different things: one is version-controlled and deployed, the other
is written at runtime by people. Keeping them in one store would also be wrong — the
manual's ``document_id`` is a deployment key that the startup reconciler owns.

Why here, and not in ``api/services/``
--------------------------------------
The console is a separate process on a separate port and **must not import ``api.*``**
(``api/tests/test_shared_layer.py`` fails the build otherwise). The console also does not
write raw SQL anywhere — every one of its routers calls a ``klado_shared`` module. So the
query has to live here for the console to be able to ask the question at all.

⚠️ **This is a second reader of the same table, not a second authority.** The main app's
``api/services/ai/knowledge_store.py`` decides what an agent is allowed to retrieve and
what ``/api/ai/knowledge/manifest`` calls valid. Nothing here re-decides that: this module
reports the documents' own fields and lets the operator read them. A console that
recomputed the manifest's verdict would be a second answer about whether the agent's
instructions are intact, and two answers is exactly what ``klado_shared`` exists to
prevent.

Read-only by construction
-------------------------
There is no write path here. The manual changes by editing a file under
``api/knowledge_docs/`` and deploying; a console-side write would bypass the reconciler,
the ``_retired.txt`` deletion protocol and the content hashes in one step.
"""
from __future__ import annotations

from typing import Any

from klado_shared.db import connect_main

#: The suffix every document id carries. The manual is **Chinese-only** since
#: 2026-10-03 (user's ruling): the 19 canonical English-side files were deleted and their
#: ids retired, and the 93 cross-references the survivors made to them were rewritten to
#: point at the ``:zh`` ids. Measured first, because the premise did not survive checking:
#: 16 of those 19 pairs were **byte-identical** apart from the ``document_id`` line, so
#: "delete the English copy" was mostly deleting exact duplicates; only three pairs
#: (``format-output-standards`` and two others) actually had English headings.
#:
#: The suffix is retained in the ids and still asserted in the tests. It is no longer a
#: *choice* between two variants, and `list_documents` no longer filters on it — a
#: remaining ``:zh`` filter would imply there was an alternative to switch back to.
CHINESE_SUFFIX = ":zh"

#: Hard cap on a single document returned to a caller. Manual documents are a few
#: kilobytes; this exists so one pathological file cannot turn an operator's page into a
#: multi-megabyte response. Truncation is reported in the payload (``truncated``) rather
#: than applied silently — a viewer showing the first 256 KB as if it were the whole
#: document would be lying quietly.
MAX_CONTENT_CHARS = 256_000

#: Upper bound for any list query here. The console renders a scrollable list, not a
#: data export; an operator who needs the raw rows has `psql`.
MAX_LIMIT = 1000
DEFAULT_LIMIT = 500

_DOCUMENT_COLUMNS = (
    "document_id", "title", "document_type", "source", "version", "is_active",
    "created_at", "updated_at", "length(content) AS content_chars",
)
_DOCUMENT_FROM = "\n      FROM ai_knowledge_documents\n"

#: Columns that free-text search runs against: what an operator would actually type to
#: find a document — its id, its title, a tag, or the file it came from.
_SEARCH_COLUMNS = ("document_id", "title", "document_type", "source",
                   "metadata->>'source_path'", "metadata->>'tags'")


def _dict_rows(cur) -> list[dict[str, Any]]:
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _iso(value: Any) -> Any:
    """Timestamps become ISO strings.

    The console renders times with its own ``when()`` helper, which expects the same shape
    the rest of its API returns. Handing it a raw ``datetime`` would make FastAPI
    serialize it the same way, but doing it here means the value is identical whether it
    came through this module or through Python.
    """
    return value.isoformat() if hasattr(value, "isoformat") else value


def _clamp_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _tags(metadata: dict[str, Any] | None) -> list[str]:
    """The tag list, as a list.

    ⚠️ The column is jsonb and already holds a real array — no comma-splitting guesswork
    here, which is what the business-knowledge table needed. A tag that contains a comma
    is therefore representable, and one that contains a space is too.
    """
    raw = (metadata or {}).get("tags")
    if not isinstance(raw, list):
        return []
    return [str(tag).strip() for tag in raw if str(tag).strip()]


def _document_sql(with_content: bool) -> str:
    columns = list(_DOCUMENT_COLUMNS)
    if with_content:
        columns.insert(1, "content")
    columns.append("metadata")
    return "SELECT " + ", ".join(columns) + _DOCUMENT_FROM


def _shape(row: dict[str, Any], *, with_content: bool) -> dict[str, Any]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    document_id = row["document_id"] or ""
    document = {
        "document_id": document_id,
        "title": row["title"],
        "document_type": row["document_type"],
        "source": row["source"],
        "version": row["version"],
        # ⚠️ `is_active` is deliberately NOT in this payload. It is a column, but the
        # only writer sets it TRUE and retirement deletes the row outright, so it is a
        # constant. Exposing it invited the console to render a "retired" state that
        # cannot exist — see `list_documents`.
        "tags": _tags(metadata),
        "retrieval_role": metadata.get("retrieval_role"),
        "runtime_contract": metadata.get("runtime_contract"),
        "source_path": metadata.get("source_path"),
        "content_sha256": metadata.get("content_sha256"),
        "content_chars": row.get("content_chars", 0),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }
    if with_content:
        content = row.get("content") or ""
        document["content"] = content[:MAX_CONTENT_CHARS]
        document["truncated"] = len(content) > MAX_CONTENT_CHARS
    return document


def _filters(document_type: Any, search: Any) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    # ⚠️ `document_type` is an OPEN vocabulary and is not validated. A deploy can add a
    # document type, and rejecting one would make a legitimate filter a 400. An unknown
    # type filters to zero rows, which is a safe failure — obvious on screen, and it
    # cannot be reached from the page anyway, whose type chips are built from the
    # breakdown this same module returns.
    kind = (document_type or "").strip().lower()
    if kind and kind != "all":
        clauses.append("document_type = %s")
        params.append(kind)


    term = (search or "").strip()
    if term:
        like = f"%{term}%"
        clauses.append("(" + " OR ".join(f"{col} ILIKE %s" for col in _SEARCH_COLUMNS) + ")")
        params.extend([like] * len(_SEARCH_COLUMNS))

    return clauses, params


def list_documents(*, document_type: str = "", search: str = "",
                   limit: Any = DEFAULT_LIMIT,
                   with_content: bool = False) -> list[dict[str, Any]]:
    """The manual, grouped by type then id.

    ⚠️ **Retirement is a hard delete, not a flag.** ``_retired.txt`` makes
    ``knowledge_store.delete_document`` issue ``DELETE FROM ai_knowledge_documents``, so
    a retired document is *gone* from this table — it is not a row left behind with
    ``is_active`` false. An earlier version of this docstring claimed the opposite ("they
    stay in the table so an older agent's citation still resolves"), and the page grew a
    已退役 stat card off that premise. It read **0** immediately after the 19 canonical
    documents were retired, which is the worst possible reading: the count was not wrong
    so much as a stat that could never be anything but wrong.

    So the `retired` count is gone, and the console shows no active/retired badge: the
    only writer of the column sets it TRUE, so ``is_active`` is a constant, and rendering
    a state the system cannot produce is how the wrong claim survived this long.

    ``ORDER BY is_active DESC`` is kept purely as a stable tiebreak — it is a no-op on
    today's data and is harmless if a row is ever deactivated by hand.
    """
    clauses, params = _filters(document_type, search)
    sql = _document_sql(with_content)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY is_active DESC, document_type, document_id LIMIT %s"
    params.append(_clamp_limit(limit))

    with connect_main() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = _dict_rows(cur)
        conn.commit()
    return [_shape(row, with_content=with_content) for row in rows]


def get_document(document_id: str) -> dict[str, Any] | None:
    """One manual document with its content, or ``None``."""
    document_id = (document_id or "").strip()
    if not document_id:
        return None
    sql = _document_sql(True) + " WHERE document_id = %s"
    with connect_main() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, [document_id])
            rows = _dict_rows(cur)
        conn.commit()
    return _shape(rows[0], with_content=True) if rows else None


def summary() -> dict[str, Any]:
    """What an operator reads first: how big the manual is, and what shape it is in.

    Every number comes from its own ``count(*)`` over the row it describes.

    ⚠️ No verdict here. The main app's ``/api/ai/knowledge/manifest`` decides whether this
    set is valid; this reports the facts it would be judged on, and stays silent on the
    judgement.
    """
    with connect_main() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE is_active)     AS active,
                       count(*) FILTER (
                           WHERE metadata->>'retrieval_role' = 'router') AS routers,
                       count(DISTINCT document_type)          AS types,
                       count(DISTINCT metadata->>'runtime_contract') AS contracts,
                       coalesce(sum(length(content)), 0)      AS total_chars
                  FROM ai_knowledge_documents
            """)
            totals = dict(zip([d[0] for d in cur.description], cur.fetchone()))
            cur.execute("""
                SELECT document_type, count(*) AS documents
                  FROM ai_knowledge_documents
                 GROUP BY document_type ORDER BY documents DESC, document_type
            """)
            by_type = [{"document_type": r[0], "documents": int(r[1] or 0)}
                       for r in cur.fetchall()]
            cur.execute("""
                SELECT coalesce(metadata->>'runtime_contract', '(none)') AS contract,
                       count(*) AS documents
                  FROM ai_knowledge_documents
                 GROUP BY 1 ORDER BY documents DESC, contract
            """)
            by_contract = [{"contract": r[0], "documents": int(r[1] or 0)}
                           for r in cur.fetchall()]
        conn.commit()

    for key, value in totals.items():
        totals[key] = int(value or 0)
    return {"totals": totals, "by_type": by_type, "by_contract": by_contract}
