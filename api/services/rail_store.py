"""The left rail: a user's own folder tree, and the short labels they rename modules to.

Two things live here, and they are here together because they are both **per-account
presentation of things the product already owns**:

* `public.klado_folders` — a tree of folders. A folder is a *virtual tag*, not a place:
  the bytes never move. `public.klado_folder_items` records that document X is in folder
  F, and moving a card is one UPDATE. That is the whole reason for the design — a
  filesystem-shaped alternative would mean re-uploading every object on a rename, and a
  rename that can fail halfway is a rename that loses work.
* `public.klado_module_labels` — a short display name per module, per account.

⚠️ **A folder is private to its owner by construction, not by a WHERE clause.** Every
statement here filters on `owner_email`, including the DELETE, because a folder id is a
sequential integer: an unfiltered `DELETE FROM klado_folders WHERE id = 7` is a way to
delete somebody else's folder, and the UI would show it as their own row disappearing.
Same rule as the file table's `_FILE_VISIBLE` in the sharing layer.

The item types are `document` / `dashboard` / `knowledge` — the three things the rail
drags. They are stored as `(item_type, item_slug)` rather than a foreign key, because
they live in three unrelated tables with three different ownership rules. The price is
that **an entry can outlive its target** (the document is deleted, the folder row is
not), which is why `listing()` LEFT JOINs the live records and reports them as
`"missing": true` instead of showing a link to nothing.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extras

from core.db import connect_main

FOLDERS_TABLE = "public.klado_folders"
ITEMS_TABLE = "public.klado_folder_items"
LABELS_TABLE = "public.klado_module_labels"

# What the rail can file away. Adding one means teaching `listing()` how to describe
# it — the type is a closed set on purpose, so a typo in a client is a 400 rather
# than a permanently invisible row.
ITEM_TYPES = ("document", "dashboard", "knowledge")

MAX_NAME = 120
MAX_DEPTH = 6
MAX_ITEMS_PER_FOLDER = 500

_schema_ready = False
_schema_lock = threading.Lock()


class FolderError(RuntimeError):
    """A rail request that went wrong. Bilingual message, mapped to 400 by the router."""


class FolderNotFound(FolderError):
    """Absent, or not this account's. **404, never 403** — see `dashboard_store`
    for why a status code must not confirm that a row exists for somebody else."""


@contextmanager
def _conn():
    """Own the connection lifecycle. `with connect_main() as conn:` commits but never
    closes (psycopg2's context manager is a transaction, not a resource), which leaks
    one connection per request."""
    conn = connect_main()
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema() -> None:
    """Create the three tables. Idempotent, memoised per process."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn = connect_main()
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {FOLDERS_TABLE} (
                        id           SERIAL PRIMARY KEY,
                        owner_email  TEXT NOT NULL,
                        parent_id    INTEGER REFERENCES {FOLDERS_TABLE}(id) ON DELETE CASCADE,
                        name         TEXT NOT NULL,
                        position     INTEGER NOT NULL DEFAULT 0,
                        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                """)
                # ⚠️ The composite index is what makes the tree read cheap. A rail with
                # one account's folders in the hundreds is still one cheap range scan;
                # without it every open is a full scan of a table every account writes to.
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS klado_folders_owner_idx "
                    f"ON {FOLDERS_TABLE} (owner_email, parent_id, position)")
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {ITEMS_TABLE} (
                        id           SERIAL PRIMARY KEY,
                        owner_email  TEXT NOT NULL,
                        folder_id    INTEGER NOT NULL
                                       REFERENCES {FOLDERS_TABLE}(id) ON DELETE CASCADE,
                        item_type    TEXT NOT NULL,
                        item_slug    TEXT NOT NULL,
                        position     INTEGER NOT NULL DEFAULT 0,
                        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                        CONSTRAINT klado_folder_items_unique UNIQUE (owner_email, item_type,
                                                                    item_slug, folder_id)
                    )
                """)
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS klado_folder_items_folder_idx "
                    f"ON {ITEMS_TABLE} (folder_id, position)")
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {LABELS_TABLE} (
                        owner_email  TEXT NOT NULL,
                        module_key   TEXT NOT NULL,
                        label_zh     TEXT NOT NULL DEFAULT '',
                        label_en     TEXT NOT NULL DEFAULT '',
                        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (owner_email, module_key)
                    )
                """)
            conn.commit()
        finally:
            conn.close()
        _schema_ready = True


def reset_schema_cache() -> None:
    """Drop the memo so a repaired table is recreated on the next call.

    Called by the router's `_with_schema`, the same escape hatch `dashboard_store`
    and `routers/reports.py` use."""
    global _schema_ready
    _schema_ready = False


# ── folders ──────────────────────────────────────────────────────────────────

def _clean_name(name: Any) -> str:
    """One line, bounded, no control characters."""
    text = " ".join(str(name or "").split())
    if not text:
        raise FolderError("目录名不能为空 / a folder needs a name")
    if len(text) > MAX_NAME:
        raise FolderError(
            f"目录名 {len(text)} 个字符，上限 {MAX_NAME} 个 / the name is {len(text)} characters; "
            f"the limit is {MAX_NAME}")
    return text


def _owner(email: Any) -> str:
    value = str(email or "").strip().lower()
    if not value:
        raise FolderError("需要一个身份才能操作左侧栏 / the rail needs an identity")
    return value


def _own_folder(cur, folder_id: int, email: str) -> dict:
    """Fetch one folder, or 404. The owner filter is not optional: `folder_id` is a
    sequential integer, so without it the rail is an oracle for other accounts' rows
    and a delete button that erases strangers' folders."""
    cur.execute(f"SELECT id, parent_id, name, position FROM {FOLDERS_TABLE} "
                "WHERE id = %s AND owner_email = %s", (folder_id, email))
    row = cur.fetchone()
    if not row:
        raise FolderNotFound("目录不存在 / no such folder")
    return row


def _depth_of(cur, folder_id: int | None, email: str) -> int:
    """How many ancestors `folder_id` has — a top-level folder is **0**.

    Walks up rather than counting down, so a cycle (which a rename cannot create but a
    future move could) terminates instead of hanging the request.

    ⚠️ The count is the number of HOPS, not the number of rows read. Counting rows puts
    a top-level folder at 1, which makes every real tree one level shallower than the
    ceiling the caller is checking against — the check still passes, just early, and
    the first user who nests `MAX_DEPTH` folders gets a tree deeper than documented.
    """
    depth = 0
    cursor_id = folder_id
    while cursor_id is not None and depth <= MAX_DEPTH + 1:
        cur.execute(f"SELECT parent_id FROM {FOLDERS_TABLE} "
                    "WHERE id = %s AND owner_email = %s", (cursor_id, email))
        row = cur.fetchone()
        if not row:
            break
        parent = row["parent_id"]
        if parent is None:
            break                       # reached the top: this row is the last hop
        cursor_id = parent
        depth += 1
    return depth


def create_folder(email: Any, name: Any, parent_id: int | None = None,
                  position: int = 0) -> dict:
    who = _owner(email)
    ensure_schema()
    label = _clean_name(name)
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if parent_id is not None:
                _own_folder(cur, int(parent_id), who)
                if _depth_of(cur, int(parent_id), who) >= MAX_DEPTH:
                    raise FolderError(
                        f"目录最多 {MAX_DEPTH} 层 / folders nest at most {MAX_DEPTH} deep")
            cur.execute(
                f"INSERT INTO {FOLDERS_TABLE} (owner_email, parent_id, name, position) "
                "VALUES (%s, %s, %s, %s) RETURNING id, parent_id, name, position",
                (who, parent_id, label, int(position or 0)))
            row = cur.fetchone()
    return dict(row)


def list_folders(email: Any) -> list[dict]:
    """Every folder this account owns, flat. The rail builds the tree client-side
    from `parent_id` — one query instead of one per level."""
    who = _owner(email)
    ensure_schema()
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT id, parent_id, name, position FROM {FOLDERS_TABLE} "
                        "WHERE owner_email = %s ORDER BY position, id", (who,))
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def rename_folder(email: Any, folder_id: int, name: Any) -> dict:
    """Rename in place. The bytes are not touched, so this cannot lose a file —
    which is the entire reason a folder is a tag and not a directory."""
    who = _owner(email)
    ensure_schema()
    label = _clean_name(name)
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _own_folder(cur, int(folder_id), who)
            cur.execute(f"UPDATE {FOLDERS_TABLE} SET name = %s "
                        "WHERE id = %s AND owner_email = %s RETURNING id, parent_id, name, position",
                        (label, int(folder_id), who))
            row = cur.fetchone()
    return dict(row)


def delete_folder(email: Any, folder_id: int) -> None:
    """Delete a folder. Sub-folders and filed items go with it (`ON DELETE CASCADE`).

    ⚠️ Deleting is not undoable from the rail, so the client confirms first. The
    items themselves are untouched — a document in a deleted folder is not deleted,
    it simply stops being filed anywhere.
    """
    who = _owner(email)
    ensure_schema()
    with _conn() as conn:
        with conn.cursor() as cur:
            _own_folder(cur, int(folder_id), who)
            cur.execute(f"DELETE FROM {FOLDERS_TABLE} WHERE id = %s AND owner_email = %s",
                        (int(folder_id), who))


# ── items ────────────────────────────────────────────────────────────────────

def _clean_type(item_type: Any) -> str:
    value = str(item_type or "").strip().lower()
    if value not in ITEM_TYPES:
        raise FolderError(
            f"不能归类 {item_type or '(空)'}：只支持 {' / '.join(ITEM_TYPES)}"
            f" / cannot file {item_type or '(nothing)'}; the rail takes "
            f"{', '.join(ITEM_TYPES)}")
    return value


def add_item(email: Any, folder_id: int, item_type: Any, item_slug: Any,
             position: int = 0) -> dict:
    """File one object into a folder.

    ⚠️ This records the *intent* to file a slug; it does not check that the slug
    exists. That check belongs to the store that owns the object, and doing it here
    would mean a second, drift-prone copy of three different ownership rules. The
    consequence is that a client can file a slug that does not exist, and `listing()`
    reports it as `missing` rather than pretending it is there.
    """
    who = _owner(email)
    ensure_schema()
    kind = _clean_type(item_type)
    slug = str(item_slug or "").strip()
    if not slug:
        raise FolderError("需要一个对象才能归档 / a folder needs something in it")
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _own_folder(cur, int(folder_id), who)
            cur.execute(f"SELECT COUNT(*) AS n FROM {ITEMS_TABLE} WHERE folder_id = %s",
                        (int(folder_id),))
            if cur.fetchone()["n"] >= MAX_ITEMS_PER_FOLDER:
                raise FolderError(
                    f"一个目录最多 {MAX_ITEMS_PER_FOLDER} 项 / a folder holds at most "
                    f"{MAX_ITEMS_PER_FOLDER} items")
            # Re-filing into the same folder is a no-op rather than an error: a drag
            # that ends where it started must not look like a failure.
            cur.execute(
                f"INSERT INTO {ITEMS_TABLE} (owner_email, folder_id, item_type, item_slug, position) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (owner_email, item_type, item_slug, folder_id) "
                "DO UPDATE SET position = EXCLUDED.position "
                "RETURNING id, folder_id, item_type, item_slug, position",
                (who, int(folder_id), kind, slug, int(position or 0)))
            row = cur.fetchone()
    return dict(row)


def remove_item(email: Any, item_id: int) -> None:
    """Unfile one object. The object itself is untouched — this only forgets the tag."""
    who = _owner(email)
    ensure_schema()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {ITEMS_TABLE} "
                        "WHERE id = %s AND owner_email = %s", (int(item_id), who))
            if not cur.rowcount:
                raise FolderNotFound("这一项不在你的目录里 / that item is not filed here")


def move_item(email: Any, item_id: int, target_folder: int) -> dict:
    """Move a filed object to another folder of the same account."""
    who = _owner(email)
    ensure_schema()
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT folder_id FROM {ITEMS_TABLE} "
                        "WHERE id = %s AND owner_email = %s", (int(item_id), who))
            if not cur.fetchone():
                raise FolderNotFound("这一项不在你的目录里 / that item is not filed here")
            _own_folder(cur, int(target_folder), who)
            # The unique constraint is (owner, type, slug, folder): moving INTO a
            # folder that already holds the object would violate it, so the existing
            # row is retired first rather than letting the database raise a bare
            # UniqueViolation the router would turn into a 500.
            cur.execute(
                f"DELETE FROM {ITEMS_TABLE} "
                "WHERE owner_email = %s AND item_type = "
                f"  (SELECT item_type FROM {ITEMS_TABLE} WHERE id = %s) "
                "AND item_slug = "
                f"  (SELECT item_slug FROM {ITEMS_TABLE} WHERE id = %s) "
                "AND folder_id = %s",
                (who, int(item_id), int(item_id), int(target_folder)))
            cur.execute(f"UPDATE {ITEMS_TABLE} SET folder_id = %s "
                        "WHERE id = %s AND owner_email = %s "
                        "RETURNING id, folder_id, item_type, item_slug, position",
                        (int(target_folder), int(item_id), who))
            row = cur.fetchone()
    return dict(row)


# ── listing ──────────────────────────────────────────────────────────────────

def list_items(email: Any) -> list[dict]:
    """Every filed object, with the *live* record behind it where one exists.

    ⚠️ The three item types live in three tables with three different ownership rules
    (`ai_reports`/`ai_files` for documents, `ai_dashboards`, `ai_knowledge_items`), so
    this does not try to re-implement any of them. It reads each title under **the
    caller's own** filter and marks the row `missing` when there is nothing to read —
    a filed slug whose document was deleted stays in the folder (the user may want to
    see that it is gone) but must never render as a link to nothing.
    """
    who = _owner(email)
    ensure_schema()
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT id, folder_id, item_type, item_slug, position "
                        f"FROM {ITEMS_TABLE} WHERE owner_email = %s "
                        "ORDER BY folder_id, position, id", (who,))
            rows = [dict(r) for r in cur.fetchall()]

            titles: dict[tuple[str, str], str] = {}
            by_type: dict[str, list[str]] = {}
            for row in rows:
                by_type.setdefault(row["item_type"], []).append(row["item_slug"])

            # Documents: a report OR a file object — either can be filed. The file
            # library's key column is `object_name` (its display name is `filename`),
            # and its table is `_file_library`, not `klado_files`.
            if by_type.get("document"):
                slugs = by_type["document"]
                cur.execute("SELECT slug, title FROM ai_reports "
                            "WHERE owner_email = %s AND slug = ANY(%s)", (who, slugs))
                for r in cur.fetchall():
                    titles[("document", r["slug"])] = r["title"]
                try:
                    cur.execute("SELECT object_name, filename FROM _file_library "
                                "WHERE owner_email = %s AND object_name = ANY(%s)",
                                (who, slugs))
                    for r in cur.fetchall():
                        titles.setdefault(("document", r["object_name"]), r["filename"])
                except psycopg2.errors.UndefinedTable:
                    # The file library is created by the storage layer on first upload;
                    # a fresh deployment may not have it yet. Losing document titles is
                    # survivable, failing the whole rail is not.
                    conn.rollback()

            for table, item_type, column in (
                ("ai_dashboards", "dashboard", "slug"),
                ("ai_knowledge_items", "knowledge", "slug"),
            ):
                slugs = by_type.get(item_type)
                if not slugs:
                    continue
                try:
                    cur.execute(
                        f"SELECT {column} AS slug, title FROM {table} "
                        "WHERE owner_email = %s AND {0} = ANY(%s)".format(column),
                        (who, slugs))
                    for r in cur.fetchall():
                        titles[(item_type, r["slug"])] = r["title"]
                except psycopg2.errors.UndefinedTable:
                    conn.rollback()

    for row in rows:
        key = (row["item_type"], row["item_slug"])
        row["title"] = titles.get(key, "")
        row["missing"] = key not in titles
    return rows


# ── module short labels ──────────────────────────────────────────────────────
def set_module_labels(email: Any, labels: dict) -> dict:
    """Save this account's short labels for modules.

    ⚠️ Keys are checked against the registry, not just their shape. A label row for a
    module key that does not exist is invisible in the rail and unremovable from it,
    so the write refuses it here — a typo must not become permanent state.

    ⚠️ A key whose BOTH sides are blank **deletes** the row rather than writing an
    empty one. "Renamed to nothing" is not a state the rail can show — absence is what
    makes it fall back to the registry's own label — so a caller clearing a name has to
    be able to say so, and skipping the row would leave the old name in place forever
    with no way to remove it.
    """
    from core import modules as registry

    who = _owner(email)
    known = {m.key for m in registry.MODULES}
    cleaned: dict[str, tuple[str, str]] = {}
    cleared: list[str] = []
    for key, value in (labels or {}).items():
        name = str(key or "").strip()
        if name not in known:
            raise FolderError(
                f"没有这个模块：{name or '(空)'} / no such module: {name or '(empty)'}")
        if isinstance(value, str):
            zh, en = "", value.strip()[:MAX_NAME]
        else:
            zh = str((value or {}).get("label_zh") or "").strip()[:MAX_NAME]
            en = str((value or {}).get("label_en") or "").strip()[:MAX_NAME]
        if zh or en:
            cleaned[name] = (zh, en)
        else:
            cleared.append(name)
    with _conn() as conn:
        with conn.cursor() as cur:
            for name in cleared:
                cur.execute(f"DELETE FROM {LABELS_TABLE} "
                            "WHERE owner_email = %s AND module_key = %s", (who, name))
            for key, (zh, en) in cleaned.items():
                cur.execute(
                    f"INSERT INTO {LABELS_TABLE} (owner_email, module_key, label_zh, label_en) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (owner_email, module_key) DO UPDATE "
                    "SET label_zh = EXCLUDED.label_zh, label_en = EXCLUDED.label_en, "
                    "    updated_at = now()",
                    (who, key, zh, en))
    out = {k: {"label_zh": v[0], "label_en": v[1]} for k, v in cleaned.items()}
    for name in cleared:
        out[name] = {"label_zh": "", "label_en": ""}
    return out


def list_module_labels(email: Any) -> dict:
    """This account's short labels. A module with no row is absent from the map, and
    the rail falls back to the registry's own label — absence means "never renamed"."""
    who = _owner(email)
    ensure_schema()
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT module_key, label_zh, label_en FROM {LABELS_TABLE} "
                        "WHERE owner_email = %s", (who,))
            rows = cur.fetchall()
    return {r["module_key"]: {"label_zh": r["label_zh"] or "", "label_en": r["label_en"] or ""}
            for r in rows}
