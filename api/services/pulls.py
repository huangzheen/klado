"""Pulling shared data: making it yours, so it outlives the person who shared it.

The rule this module implements, as decided: **an account's data belongs to that
account, but anything you have pulled is yours.** Sharing hands over *access* — a live
view of somebody's rows that dies with their account. Pulling hands over *the data*.

The difference is the whole point, and it is why pulling is not a nicer share:

* a share is one row in `dataset_shares`. The grantee can query, cannot write, and
  loses everything the moment the owner closes their account.
* a pull is a new object with a new owner: a new PG table for a dataset, a new object
  in the file library, a new row in `ai_dashboards`. Nothing points back at the
  original, so closing the original account changes nothing about the copy.

Two consequences that drove the design, both learned the hard way:

* **A dataset copy must not reuse the source's table name.** `CREATE TABLE new AS
  SELECT * FROM old` under the old name is not a copy at all — it is the same table,
  and dropping the owner's account would take the copy with it. The name therefore
  always carries a suffix, and `_free_name` proves it is unused before creating it.
* **A pulled dashboard must pull its datasets too.** A dashboard is a live query that
  names its datasets; a copy pointed at the original's tables is not private and dies
  with them. `_pull_dashboard` therefore pulls each dataset and rewrites the
  declaration, which is why it returns the name mapping it used.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from psycopg2 import sql

from services import data_center_db as db
from services import dashboard_store as store
from services import dataset_shares as shares

log = logging.getLogger(__name__)


class PullError(RuntimeError):
    """A pull could not be completed. Carries a bilingual message."""


#: How many times to try a name before giving up. The suffix space is bounded, so a
#: collision is almost always "already pulled this once" rather than a real clash.
_NAME_TRIES = 50


def _safe_name(table_name: str) -> str:
    """The dataset's real table name, or None when it is not one we may touch.

    Reuses the Data Center's own normaliser so the name a pull creates is judged by
    exactly the same rule that will judge it later. A dataset whose name does not
    survive that rule was never queryable in the first place.
    """
    try:
        name = db._safe_id(table_name)
    except Exception:                                        # noqa: BLE001
        return None
    if not name or not name[0].isalpha() and name[0] != "_":
        return None
    return name


def _table_exists(name: str) -> bool:
    with db.get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",))
            row = cur.fetchone()
    return bool(row and row[0])


def _free_name(base: str) -> str:
    """A table name that is not taken. Prefers `<base>_copy`, then numbered variants.

    ⚠️ The check is a real `to_regclass` lookup rather than a read of `_import_registry`,
    because the registry is not the only thing that can occupy a name: a table dropped
    outside `delete_pg_table`, or an import that crashed after creating the table and
    before registering it, both leave a name that the registry says is free.
    """
    base = base[:48]
    for suffix in ["copy", *[f"copy{i}" for i in range(2, _NAME_TRIES)]]:
        candidate = f"{base}_{suffix}"
        if not _table_exists(candidate):
            return candidate
    raise PullError("无法生成可用的数据集名 / could not find a free dataset name")


def _assert_can_pull(table_name: str, email: str) -> dict:
    """The dataset row, when `email` may take a copy of it. 404-equivalent otherwise.

    Two ways to qualify: you own it (why would you pull your own? — allowed, and it
    makes a snapshot), or it was shared with you. Ownership is checked first so an
    owner pulling their own dataset gets the nicer error path.
    """
    name = _safe_name(table_name)
    if not name:
        raise PullError("数据集名不合法 / not a valid dataset name")
    row = db.get_dataset(name, viewer_email=email)
    if row:
        return row
    granted = shares.shared_with_me(email)
    if name in granted:
        # Read it as the owner so the row's metadata is complete, then let the copy
        # path below stamp the new owner.
        info = db.get_dataset(name, viewer_email=None)
        if info:
            return info
    raise PullError("数据集不存在或未分享给你 / dataset not found or not shared with you")


def pull_dataset(table_name: str, email: str) -> dict:
    """Copy a dataset into `email`'s own Data Center. Returns the new dataset row.

    The copy is made with `CREATE TABLE … AS SELECT`, so it takes a *snapshot*: rows
    added to the original afterwards do not appear, which is what makes it a copy rather
    than another view onto somebody else's live data.
    """
    row = _assert_can_pull(table_name, email)
    src = _safe_name(row["table_name"])
    new_name = _free_name(src)
    # `columns` is JSONB, so psycopg2 hands it back already decoded. Parsing it again
    # would raise on a list — the JSON path is only for a row that was serialised.
    raw_columns = row.get("columns")
    columns = (json.loads(raw_columns) if isinstance(raw_columns, (str, bytes))
               else list(raw_columns or []))

    with db.get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("CREATE TABLE public.{} AS SELECT * FROM public.{}").format(
                    sql.Identifier(new_name), sql.Identifier(src)))
            cur.execute(sql.SQL("SELECT count(*) FROM public.{}").format(sql.Identifier(new_name)))
            copied = int(cur.fetchone()[0])
        conn.commit()

    display = f"{row.get('display_name') or src} ({src})"
    # ⚠️ `columns` is JSONB text but `modules` is a real Postgres ARRAY — normalising
    # both as JSON produces a string like "['sales']" in an array column, and the
    # registry then reads back as a module nobody can switch.
    raw_modules = row.get("modules")
    if isinstance(raw_modules, str):
        # A Postgres array literal, not JSON: `['core','sales']`. Splitting on commas
        # leaves the quotes attached (`"'core'"`), which then names a module that does
        # not exist. Strip the brackets and each element's quotes and spaces.
        raw_modules = [m.strip().strip("'\"") for m in raw_modules.strip("[]{}").split(",")]
        raw_modules = [m for m in raw_modules if m]
    db.create_pg_table(
        new_name, columns, row.get("fingerprint") or "", display,
        # The provenance is honest on purpose: the copy came from another account's
        # dataset, not from an upload of theirs. Anyone reading this later should not
        # be told it was re-imported from a file it never saw.
        source_file=f"pulled:{src}", sheet_name=row.get("sheet_name") or "",
        modules=list(raw_modules or []),
        source_file_path=None, owner_email=email, _created_copy=True,
    )
    with db.get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public._import_registry SET row_count=%s WHERE table_name=%s",
                        (copied, new_name))
        conn.commit()

    log.info("dataset pulled %s -> %s by %s (%s rows)", src, new_name, email, copied)
    result = db.get_dataset(new_name, viewer_email=email) or {"table_name": new_name}
    result["pulled_from"] = src
    result["rows"] = copied
    return result


def pull_file(file_id: int, email: str) -> dict:
    """Copy an uploaded file into `email`'s own library.

    A file owner or an explicit file grantee may pull it. A dataset grant never
    grants the source spreadsheet; the file needs its own separate grant. Pulling
    must not become a way
    round that, which is why this calls `get_file` with the caller's own address and
    lets it return None. Pulling your own file is the snapshot case: keep a private
    copy that no later edit or account closure can take back.

    The object is copied server-side (`copy_object`) rather than downloaded and
    re-uploaded, so a large workbook never passes through this process's memory.

    ⚠️ The new object key is namespaced by the *new* owner. Reusing the source key
    would leave both library rows pointing at one object, and deleting either account
    would delete the bytes the other one still needs.
    """
    row = db.get_file(file_id, viewer_email=email)
    if not row:
        raise PullError("文件不存在或不属于你 / file not found or not yours")
    src_key = row["object_name"]
    filename = row.get("filename") or src_key.rsplit("/", 1)[-1]
    new_key = _free_object_key(email, src_key, filename)

    from services import oss_storage
    oss_storage.copy_object(db.RAW_BUCKET, new_key, src_key)
    # ⚠️ Argument order is (object_name, filename), NOT the other way round — they read
    # alike and swapping them writes a library row whose key is a filename.
    db._insert_library_row(new_key, filename, int(row.get("size_bytes") or 0),
                           row.get("sheets_meta") or [], owner_email=email)
    log.info("file pulled %s -> %s by %s", src_key, new_key, email)
    new_row = db._get_library_row_by_object(new_key) or {}
    new_row["pulled_from"] = src_key
    return new_row


def _free_object_key(email: str, src_key: str, filename: str) -> str:
    """An object key under the new owner's namespace that is not already taken.

    ⚠️ The first candidate cannot be `{owner}/{filename}` alone: when the puller owns
    the source (the snapshot case) that is *literally the source's key*, so it always
    reads as taken and the loop runs out. Hence `_copy`, and the source key can never be
    the destination — which is the point, since a copy sharing a key with its original
    would be deleted twice over.
    """
    base = filename.rsplit("/", 1)[-1]            # the filename, not its folder
    stem, dot, ext = base.rpartition(".")
    if not stem:                                  # no extension: rpartition puts it all in ext
        stem, dot, ext = base, "", ""
    local = f"{email}/{stem}_copy{dot}{ext}"
    if not _object_exists(local):
        return local
    for i in range(2, _NAME_TRIES):
        candidate = f"{email}/{stem}_copy{i}{dot}{ext}"
        if not _object_exists(candidate):
            return candidate
    raise PullError("无法生成可用的文件名 / could not find a free file name")


def _object_exists(key: str) -> bool:
    """Whether that object key is already taken.

    ⚠️ The storage call's return value has to be passed through. Dropping it makes
    every key read as "taken", so `_free_object_key` burns through its whole name
    space and raises — a pull that can never succeed, from a helper that looks like a
    three-line guard.
    """
    from services import oss_storage
    try:
        return bool(oss_storage.object_exists(db.RAW_BUCKET, key))
    except Exception:                                        # noqa: BLE001
        # A storage error must not be read as "the name is free" — that would let the
        # copy overwrite somebody's object.
        raise PullError("无法访问存储 / storage is unavailable")


def pull_dashboard(slug: str, email: str) -> dict:
    """Copy a dashboard into `email`'s own pages, datasets and all.

    A dashboard is a live query that names its datasets by name. Copying the row while
    leaving those names pointing at the original would produce a page that looks
    private, is not, and stops working when the original owner closes their account.
    So each dataset is pulled first and the copy is rewritten to the new names.

    A dataset that cannot be pulled (revoked between the two reads, say) is left
    pointing at its original name: the page still works today, and `missing_datasets`
    tells the reader exactly which numbers are borrowed rather than pretending the copy
    is complete.
    """
    row = store.get_dashboard(slug, email=email, include_html=True)
    if not row or not store.may_read(row, email):
        raise PullError("页面不存在或无权访问 / page not found")

    source_datasets = store.datasets_of(row)
    mapping: dict[str, str] = {}
    missing: list[str] = []
    for name in source_datasets:
        try:
            mapping[name] = pull_dataset(name, email)["table_name"]
        except Exception:                                    # noqa: BLE001
            missing.append(name)
            log.warning("dashboard %s: could not pull dataset %s", slug, name)

    new_slug = _free_slug(store, row.get("title") or slug)
    store.save_dashboard({
        "title": row.get("title") or new_slug,
        "html": row.get("html") or "",
        "summary": row.get("summary") or "",
        "datasets": [mapping.get(n, n) for n in source_datasets],
        "visibility": "private",
        "accent": row.get("accent"),
        "status": "published",
    }, email, new_slug)
    log.info("dashboard pulled %s -> %s by %s (datasets=%s missing=%s)",
             slug, new_slug, email, mapping, missing)
    out = store.get_dashboard(new_slug, email=email) or {"slug": new_slug}
    out["pulled_from"] = slug
    out["datasets_pulled"] = mapping
    out["missing_datasets"] = missing
    return out


def _free_slug(store, title: str) -> str:
    """A slug for the copy, distinct from the original's and from any other page.

    `normalise_slug` is the same rule publishing uses, so the copy is judged by the
    identical standard rather than a second, looser one.
    """
    base = store.normalise_slug(f"{title}-copy") or "page-copy"
    for i in range(_NAME_TRIES):
        candidate = base if i == 0 else f"{base}-{i}"
        try:
            store.normalise_slug(candidate)
        except Exception:                                    # noqa: BLE001
            continue
        if not store.get_dashboard(candidate):
            return candidate
    raise PullError("无法生成可用的页面名 / could not find a free page name")
