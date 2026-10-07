"""Knowledge documents as version-controlled files — the single source of truth.

Since 2026-09-23 the knowledge base is **file-sourced**: the Markdown files under
``api/knowledge_docs/`` are authoritative and the database
(``public.ai_knowledge_documents``) is a derived copy that is rebuilt from them on
startup. This module is the only writer of knowledge documents in the deployment —
the HTTP write surface (``POST``/``DELETE /api/ai/knowledge/documents``) was removed
at the same time, so "a reader cannot write or delete" is a structural property
rather than a policy.

Why files win
-------------
Before this change the documents existed only in the deployment database, with no
disk source at all: every content fix had to go through an ad-hoc script, and there
was no diff, no history and no review. Making the files authoritative gives all
three, and requires no cluster or database credentials to edit the knowledge base —
a Git push is the whole admin path.

Behaviour, in the order it matters
----------------------------------
* **Hash-skipped.** A document is only written when ``document_hash`` — a digest of
  every persisted field, front matter included — differs from what is stored, so an
  ordinary restart performs zero writes and leaves ``updated_at`` untouched (which
  keeps cross-environment content comparisons meaningful).
  ⚠️ Changing the hash rule itself (as on 2026-09-29, from body-only to whole
  document) makes **every** stored digest stale, so the first startup after that
  deploy re-writes the whole knowledge base once: expect ``applied`` = all
  documents, ``skipped`` = 0, and then zero on every restart after it.
* **Never implicitly deleted.** Removing a file does *not* remove the document.
  Deletion is an explicit, versioned act: list the id in ``_retired.txt``.
* **Fail loudly.** A malformed file, a duplicate id or an empty directory raises
  ``KnowledgeStoreError`` rather than silently deploying a partial knowledge base.
* **Observable.** Every pass records its outcome under the ``app_settings`` key
  ``knowledge_docs.last_apply`` — ok *and* failed — because the container log needs
  an action key to read, and "the reconciler silently stopped running" is exactly the
  failure that would otherwise look like a healthy deployment.
* **Exact round trip.** ``tools/export_knowledge_docs.py`` walks the other way and
  reproduces these files byte-for-byte (the store strips surrounding whitespace on
  write, so the file body is the stripped form).

File format
-----------
``api/knowledge_docs/<document_id with ':' replaced by '__'>.md``::

    ---
    document_id: klado-v2:data-sql-cookbook
    title: data-sql-cookbook
    document_type: rule
    source: klado-runtime-curation
    version: 1.2
    tags: [sql, 查询, 陷阱]
    ---

    (body — exactly the stored content)

``document_id``/``title``/``document_type``/``source``/``version`` are required;
``tags`` is optional; any other ``key: value`` line becomes an extra metadata key
and is preserved verbatim. Files whose name starts with ``_`` are metadata, not
documents.

Setting ``KNOWLEDGE_DOCS_AUTOAPPLY=0`` disables the startup pass (diagnostics only —
the database then silently drifts from the files).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.db import connect_main
from services.ai.knowledge_store import (
    KnowledgeDocument,
    KnowledgeStoreError,
    _normalise_metadata,
    delete_document as _delete_document,
    ensure_schema as _ensure_schema,
    upsert_document as _upsert_document,
    validate_document as _validate_document,
)

DOCUMENT_SUFFIX = ".md"
RETIRED_FILENAME = "_retired.txt"
LAST_APPLY_KEY = "knowledge_docs.last_apply"
REQUIRED_KEYS = ("document_id", "title", "document_type", "source", "version")

_FRONT_MATTER = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.S)
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")


def default_docs_dir() -> Path:
    """Resolve the document directory for both a checkout and the container image."""
    override = os.environ.get("KNOWLEDGE_DOCS_DIR")
    if override:
        return Path(override)
    # api/services/ai/knowledge_docs.py  ->  api/knowledge_docs
    # /app/services/ai/knowledge_docs.py ->  /app/knowledge_docs
    return Path(__file__).resolve().parents[2] / "knowledge_docs"


def autoapply_enabled() -> bool:
    return os.environ.get("KNOWLEDGE_DOCS_AUTOAPPLY", "1").strip().lower() not in {"0", "false", "no", "off"}


def document_hash(document: KnowledgeDocument) -> str:
    """Digest of **everything the store writes**, not just the body.

    ⚠️ This used to hash ``document.content`` alone, and that was a real defect
    (found 2026-09-29). The front matter is where ``version``, ``tags`` and
    ``document_type`` live, and ``upsert_document`` stores all of them — but the
    reconciler compared a body-only digest, so a **front-matter-only edit was
    skipped as "unchanged"** and the database kept the previous revision. The
    symptom is silent: the deploy is green, the image is new, the document's
    version/tags in the database are simply the old ones. It cost a whole
    retrieval improvement (Chinese tags on 15 documents) that never shipped while
    the pass reported ``applied: 4, skipped: 28``.

    Rule: **any field the store persists must be in this digest.** The values are
    normalised the same way ``upsert_document`` normalises them, so the digest
    computed from a file equals the digest computed from the row it produces —
    otherwise every startup would rewrite the whole knowledge base forever
    (covered by ``test_stored_row_hashes_back_to_the_same_digest``).
    """
    raw_metadata = {k: v for k, v in (document.metadata or {}).items() if k != "content_sha256"}
    payload = {
        "title": (document.title or "").strip(),
        "document_type": (document.document_type or "").strip().lower(),
        "source": (document.source or "").strip(),
        "version": (document.version or "").strip(),
        "metadata": _normalise_metadata(raw_metadata),
        "content": (document.content or "").strip(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ── front matter ─────────────────────────────────────────────────────────────

def _parse_scalar(raw: str) -> Any:
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        items = []
        for part in inner.split(","):
            item = part.strip().strip('"').strip("'").strip()
            if item:
                items.append(item)
        return items
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_document(text: str, *, source_name: str) -> KnowledgeDocument:
    """Parse one ``.md`` file into a knowledge document, or explain why it cannot be."""
    match = _FRONT_MATTER.match(text)
    if not match:
        raise KnowledgeStoreError(f"{source_name}: missing the leading '---' front matter block")

    fields: dict[str, Any] = {}
    for offset, line in enumerate(match.group(1).splitlines(), start=2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, raw = stripped.partition(":")
        key = key.strip()
        if not separator or not _KEY.match(key):
            raise KnowledgeStoreError(f"{source_name}:{offset}: expected 'key: value', got {line.strip()!r}")
        if key in fields:
            raise KnowledgeStoreError(f"{source_name}:{offset}: duplicate key {key!r}")
        fields[key] = _parse_scalar(raw)

    missing = [key for key in REQUIRED_KEYS if not str(fields.get(key) or "").strip()]
    if missing:
        raise KnowledgeStoreError(f"{source_name}: front matter is missing {', '.join(missing)}")

    tags = fields.pop("tags", [])
    if isinstance(tags, str):
        tags = [tags] if tags.strip() else []

    document = KnowledgeDocument(
        document_id=str(fields.pop("document_id")).strip(),
        title=str(fields.pop("title")).strip(),
        content=text[match.end():].strip(),
        document_type=str(fields.pop("document_type")).strip(),
        source=str(fields.pop("source")).strip(),
        version=str(fields.pop("version")).strip(),
        metadata={**fields, "tags": tags},
    )
    errors = _validate_document(document)
    if errors:
        raise KnowledgeStoreError(f"{source_name}: " + "; ".join(errors))
    return document


# ── loading ──────────────────────────────────────────────────────────────────

def load_documents(directory: Path | str | None = None) -> list[KnowledgeDocument]:
    """Load every document file, raising if any of them is unusable."""
    root = Path(directory) if directory else default_docs_dir()
    if not root.is_dir():
        raise KnowledgeStoreError(f"knowledge docs directory not found: {root}")

    documents: list[KnowledgeDocument] = []
    errors: list[str] = []
    for path in sorted(root.glob(f"*{DOCUMENT_SUFFIX}")):
        if path.name.startswith("_"):
            continue
        try:
            documents.append(parse_document(path.read_text(encoding="utf-8"), source_name=path.name))
        except KnowledgeStoreError as exc:
            errors.append(str(exc))
        except OSError as exc:
            errors.append(f"{path.name}: cannot read ({exc})")

    seen: dict[str, str] = {}
    for document in documents:
        if document.document_id in seen:
            errors.append(f"duplicate document_id {document.document_id!r} (also in another file)")
        seen.setdefault(document.document_id, document.title)

    if errors:
        raise KnowledgeStoreError("knowledge docs are invalid: " + " | ".join(errors))
    if not documents:
        raise KnowledgeStoreError(f"no knowledge documents found in {root}")
    return documents


def retired_document_ids(directory: Path | str | None = None) -> list[str]:
    """Explicit, versioned deletion list. A missing file means 'delete nothing'."""
    root = Path(directory) if directory else default_docs_dir()
    path = root / RETIRED_FILENAME
    if not path.is_file():
        return []
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry and entry not in ids:
            ids.append(entry)
    return ids


# ── applying ─────────────────────────────────────────────────────────────────

def _stored_hashes() -> dict[str, str]:
    """Digest of what is currently stored, computed with the same rule as the files.

    Every persisted column is read — not just ``content``. Reading only the body
    was what made front-matter-only edits invisible to the reconciler (see
    ``document_hash``).
    """
    try:
        conn = connect_main()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT document_id, title, content, document_type, source, version, metadata
                FROM public.ai_knowledge_documents
                """
            )
            return {
                row[0]: document_hash(KnowledgeDocument(
                    document_id=row[0],
                    title=row[1] or "",
                    content=row[2] or "",
                    document_type=row[3] or "",
                    source=row[4] or "",
                    version=row[5] or "",
                    metadata=row[6] if isinstance(row[6], dict) else {},
                ))
                for row in cur.fetchall()
            }
        finally:
            cur.close()
            conn.close()
    except KnowledgeStoreError:
        raise
    except Exception as exc:
        raise KnowledgeStoreError(f"could not read stored knowledge documents: {exc}") from exc


def save_apply_summary(summary: dict[str, Any]) -> None:
    """Record the last pass under ``app_settings`` so it is readable from outside.

    The container log needs an action key this deployment does not expose, so a
    reconciler that stopped running would be indistinguishable from a healthy one.
    This is the only non-document write in the module.
    """
    try:
        conn = connect_main()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (%s, %s::jsonb, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (LAST_APPLY_KEY, json.dumps(summary, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            cur.close()
            conn.close()
    except Exception as exc:  # observability must never take the deployment down
        print(f"WARNING: could not record the knowledge docs pass: {exc}", flush=True)


def load_apply_summary() -> dict[str, Any] | None:
    """Read back the last recorded pass (used by tests and operations)."""
    try:
        conn = connect_main()
        cur = conn.cursor()
        try:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (LAST_APPLY_KEY,))
            row = cur.fetchone()
        finally:
            cur.close()
            conn.close()
    except Exception as exc:
        raise KnowledgeStoreError(f"could not read the knowledge docs pass record: {exc}") from exc
    if not row:
        return None
    value = row[0]
    return value if isinstance(value, dict) else json.loads(value)


def apply_knowledge_docs(
    documents: list[KnowledgeDocument] | None = None,
    directory: Path | str | None = None,
) -> dict[str, Any]:
    """Reconcile the database with the files. Idempotent; safe on every startup."""
    root = Path(directory) if directory else default_docs_dir()
    docs = documents if documents is not None else load_documents(root)

    _ensure_schema()
    stored = _stored_hashes()

    applied: list[str] = []
    skipped = 0
    for document in docs:
        digest = document_hash(document)
        if stored.get(document.document_id) == digest:
            skipped += 1
            continue
        metadata = {**(document.metadata or {}), "content_sha256": digest}
        _upsert_document(replace(document, metadata=metadata))
        applied.append(document.document_id)

    deleted: list[str] = []
    for document_id in retired_document_ids(root):
        if _delete_document(document_id):
            deleted.append(document_id)

    result = {
        "status": "ok",
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source_dir": str(root),
        "total": len(docs),
        "applied": len(applied),
        "skipped": skipped,
        "deleted": deleted,
        "applied_ids": applied,
    }
    save_apply_summary(result)
    return result


def startup_apply() -> dict[str, Any] | None:
    """Startup entry point used by the lifespan hook."""
    if not autoapply_enabled():
        summary = {
            "status": "disabled",
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "note": "KNOWLEDGE_DOCS_AUTOAPPLY=0 — the database is not being reconciled",
        }
        save_apply_summary(summary)
        print(f"Klado knowledge docs: {summary}", flush=True)
        return None
    try:
        return apply_knowledge_docs()
    except KnowledgeStoreError as exc:
        # Record the failure before letting it surface as a startup warning: a pass
        # that never ran and a pass that failed must not look the same from outside.
        save_apply_summary({
            "status": "failed",
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "source_dir": str(default_docs_dir()),
            "error": str(exc)[:2000],
        })
        raise