"""Knowledge projects and folders — the containers a page is filed into.

The shape the user asked for (2026-10-03), and why it is two levels
------------------------------------------------------------------
**项目 (card) → 文件夹 → 页面.** A project is what a person thinks of as a "main
folder" and what the Knowledge page shows as a card; opening one gives a
current-directory style page, and inside it the one level that matters is the
folder. Nesting folders inside folders was deliberately not built: every extra
level is a second thing to resolve on every read, and nothing in the request
needs it.

未归档
------
Two containers, both named 未归档, and the rule that makes them one rule:

* every account has a **system project** (its own catch-all, the card you see
  first), and
* every project has a **system folder** (its own catch-all inside it).

So "the page has no folder" always resolves to *a* 未归档, and the client never
has to ask where unfiled pages went. Neither is a special case in SQL: they are
rows with ``system = TRUE``, created on demand by `ensure_unfiled()`, and the
folder one is pinned by a partial unique index.

⚠️ **A page with `project_slug IS NULL` is in the unfiled project.** That is the
default for every page written before this feature existed, so NULL is a real
answer rather than missing data. `business_knowledge.list_items()` translates the
unfiled slug into ``IS NULL`` for exactly that reason. `resolve_placement()` is
the other half: it turns a stored row into the concrete project/folder the client
should show.

Cover
-----
A project card is a card, and a card with no image is a grey placeholder — so it
reuses the Workspace cover mechanism end to end: the same seven columns
`ai_reports` keeps, the same `COVER_ALIASES` request fields, and the same
`services/report_cover.py::resolve_cover()`. Nothing here calls the image model
directly.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import psycopg2.extras

from services.ai import business_knowledge as pages
from services.ai.business_knowledge import (BusinessKnowledgeError, UNFILED_TITLE,
                                           _db, _text, clean_slug, unfiled_slug)

MAX_TITLE = 120
MAX_SUMMARY = 300

# Guarded by the same lock style as the page tables: this is called from request
# handlers, and two requests for a new account must not both create its unfiled
# project.
_unfiled_lock = threading.Lock()


@dataclass(frozen=True)
class KnowledgeProject:
    id: int
    slug: str
    title: str
    summary: str = ""
    system: bool = False
    owner_email: str = ""
    has_cover: bool = False
    cover_url: str = ""
    cover_w: int | None = None
    cover_h: int | None = None
    cover_prompt: str = ""
    folder_count: int = 0
    item_count: int = 0
    shared_count: int = 0
    created_at: Any = None
    updated_at: Any = None


@dataclass(frozen=True)
class KnowledgeFolder:
    id: int
    project_slug: str
    title: str
    system: bool = False
    sort_order: int = 0
    item_count: int = 0
    shared_emails: list[str] = field(default_factory=list)
    can_manage: bool = False


# ── the unfiled project / folder, created on demand ──────────────────────────

def ensure_unfiled(email: str) -> tuple[str, int]:
    """This account's unfiled project slug and its unfiled folder id.

    Idempotent and safe to call on every read: the row is found by its
    deterministic slug, and the folder by the partial unique index on ``system``.
    """
    pages.ensure_tables()
    slug = unfiled_slug(email)
    with _unfiled_lock, _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT slug FROM ai_knowledge_projects WHERE slug = %s", (slug,))
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO ai_knowledge_projects (slug, title, system, owner_email) "
                    "VALUES (%s, %s, TRUE, %s) ON CONFLICT (slug) DO NOTHING",
                    (slug, UNFILED_TITLE, _text(email)),
                )
            cur.execute("SELECT id FROM ai_knowledge_folders WHERE project_slug = %s AND system", (slug,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO ai_knowledge_folders (project_slug, title, system, sort_order) "
                    "VALUES (%s, %s, TRUE, 0) RETURNING id",
                    (slug, UNFILED_TITLE),
                )
                row = cur.fetchone()
    return slug, int(row["id"])


def is_unfiled_project(slug: str, email: str) -> bool:
    """Whether ``slug`` is this account's catch-all. Used to reject edits to it."""
    return _text(slug) == unfiled_slug(email)


def _is_unfiled_name(title: str) -> bool:
    """Whether a person-typed name is the reserved 未归档.

    ⚠️ **Both sides of the pair, and the pair itself.** A user types 未归档; they do
    not type the two-language constant. Matching only `UNFILED_TITLE` ("未归档 /
    Unfiled") would let the Chinese half through — which is the half everybody
    actually types — and the wall would then show two cards reading 未归档. The
    English side is refused with it so the name cannot be half-matched either.
    """
    wanted = {UNFILED_TITLE, "未归档", "Unfiled", "unfiled"}
    return _text(title).strip().casefold() in {w.casefold() for w in wanted}


def resolve_placement(item, email: str) -> tuple[str, int]:
    """The concrete (project slug, folder id) an item should be shown under.

    A stored NULL means 未归档, so the item is shown under the reader's own
    unfiled project. ⚠️ This is a *display* resolution: a borrowed page stays in the
    folder its author filed it in, even when the reader has never seen that project.
    """
    slug = _text(getattr(item, "project_slug", ""))
    folder_id = getattr(item, "folder_id", None)
    if slug and folder_id:
        return slug, int(folder_id)
    return ensure_unfiled(email)


# ── projects ────────────────────────────────────────────────────────────────

_SELECT_PROJECT = ("id, slug, title, summary, system, owner_email, cover_mime, cover_w, cover_h, "
                   "cover_prompt, cover_model, cover_url, created_at, updated_at")


def _row_to_project(row: dict) -> KnowledgeProject:
    return KnowledgeProject(
        id=row["id"], slug=row["slug"], title=row["title"], summary=row.get("summary") or "",
        system=bool(row.get("system")), owner_email=row.get("owner_email") or "",
        has_cover=bool(row.get("has_cover")) or bool(row.get("cover_url")),
        cover_url=row.get("cover_url") or "",
        cover_w=row.get("cover_w"), cover_h=row.get("cover_h"),
        cover_prompt=row.get("cover_prompt") or "",
        folder_count=row.get("folder_count") or 0, item_count=row.get("item_count") or 0,
        shared_count=row.get("shared_count") or 0,
        created_at=row.get("created_at"), updated_at=row.get("updated_at"),
    )


def list_projects(email: str) -> list[KnowledgeProject]:
    """The card wall: my own projects, plus any project holding a folder shared with me.

    The unfiled project always comes first and always exists — it is where an
    unfiled page is shown, so a wall without it would hide those pages entirely.

    ⚠️ ``cover_data`` is never selected (a card list is dozens of rows and a JPEG
    each would be hundreds of MB). Only `GET …/cover` reads the bytes, in its own
    query — the same rule `ai_calendar_events` follows.
    """
    ensure_unfiled(email)
    # ⚠️ Parameter count, which is the whole trap in this file: **one email per `%s`**,
    # and `READABLE_PREDICATE` carries THREE of them (own row, item share, folder
    # share). So the subquery inside `item_count` wants three, `shared_count` wants one
    # and the `WHERE` wants two — six in all. Adding a branch to the predicate
    # therefore breaks this query, and it breaks it as a 500 from the driver
    # ("tuple index out of range"), not as anything that names the cause.
    sql = ("SELECT " + _SELECT_PROJECT + ","
           " (p.cover_data IS NOT NULL) AS has_cover,"
           # Only pages the caller may read, for the same reason: a count is a
           # read, and a count of somebody else's pages is a leak.
           # ⚠️ **No table alias.** `READABLE_PREDICATE` names `ai_knowledge_items`
           # in full (`…s.item_id = ai_knowledge_items.id`), so aliasing the table
           # here breaks the reference — and it breaks it as a 500 from PostgreSQL
           # ("invalid reference to FROM-clause entry"), which names neither the
           # statement nor the alias.
           " (SELECT count(*) FROM ai_knowledge_items WHERE "
           # ⚠️ Same NULL translation as `list_items`: the unfiled project OWNS the
           # rows with `project_slug IS NULL`, so a card that counted only
           # `project_slug = p.slug` would show 未归档 as empty while its own folder
           # listed the pages — two numbers about the same pile, one of them wrong.
           "  CASE WHEN p.system THEN ai_knowledge_items.project_slug IS NULL"
           "       ELSE ai_knowledge_items.project_slug = p.slug END AND "
           "  " + pages.READABLE_PREDICATE + ") AS item_count,"
           " (SELECT count(*) FROM ai_knowledge_folders f WHERE f.project_slug = p.slug) AS folder_count,"
           " (SELECT count(*) FROM ai_knowledge_folders f "
           "  JOIN ai_knowledge_folder_shares fs ON fs.folder_id = f.id "
           "  WHERE f.project_slug = p.slug AND p.owner_email <> %s) AS shared_count"
           " FROM ai_knowledge_projects p WHERE (p.owner_email = %s OR EXISTS ("
           "SELECT 1 FROM ai_knowledge_folders f "
           "JOIN ai_knowledge_folder_shares fs ON fs.folder_id = f.id "
           "WHERE f.project_slug = p.slug AND fs.recipient_email = %s))"
           " ORDER BY p.system DESC, p.updated_at DESC")
    placeholders = (email,) * sql.count("%s")
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, placeholders)
            rows = [dict(r) for r in cur.fetchall()]
    projects = [_row_to_project(row) for row in rows]
    unfiled = unfiled_slug(email)
    projects.sort(key=lambda p: (p.slug != unfiled, not p.system, p.slug))
    return projects


def get_project(slug: str, email: str) -> KnowledgeProject | None:
    """One project, or None when it is missing **or** the caller may not see it.

    None rather than 403, like every other read here: a stranger must not learn
    that a project with this slug exists.
    """
    pages.ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT " + _SELECT_PROJECT + " FROM ai_knowledge_projects p WHERE p.slug = %s",
                        (_text(slug),))
            row = cur.fetchone()
            if not row:
                return None
            if row["owner_email"] != email:
                cur.execute("SELECT 1 FROM ai_knowledge_folders f "
                            "JOIN ai_knowledge_folder_shares fs ON fs.folder_id = f.id "
                            "WHERE f.project_slug = %s AND fs.recipient_email = %s",
                            (row["slug"], email))
                if cur.fetchone() is None:
                    return None
    return _row_to_project(dict(row))


def create_project(email: str, title: str, *, summary: str = "", cover: dict | None = None) -> KnowledgeProject:
    """Create a project, with its own 未归档 folder, in one transaction.

    The system folder is created **here** rather than lazily: a project the user
    just created and can see but cannot file anything into looks broken, and the
    partial unique index would let a second one appear if the lazy path raced.
    """
    pages.ensure_tables()
    title = _text(title, limit=MAX_TITLE)
    if not title:
        raise BusinessKnowledgeError("缺少项目名称 / the project needs a title")
    if _is_unfiled_name(title):
        # ⚠️ The account already HAS a 未归档 — it is the system project. A second one
        # is not a duplicate the reader can tell apart: the wall would show the same
        # name twice, and "put it in 未归档" would become ambiguous between the two.
        raise BusinessKnowledgeError(
            "「未归档」是每个账号自带的，用它来收纳没有指定文件夹的页面"
            " / 未归档 already exists on every account — it is where pages with no "
            "folder go, so a second project by that name would be ambiguous")
    slug = clean_slug(None, title)
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            attempt = 0
            while True:
                cur.execute("SELECT 1 FROM ai_knowledge_projects WHERE slug = %s", (slug,))
                if cur.fetchone() is None:
                    break
                attempt += 1
                if attempt > 6:
                    raise BusinessKnowledgeError("无法分配项目 slug / could not allocate a project slug")
                slug = f"{clean_slug(None, title)[:60]}-{attempt + 1}"
            cur.execute(
                "INSERT INTO ai_knowledge_projects (slug, title, summary, owner_email) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (slug, title, _text(summary, limit=MAX_SUMMARY), _text(email)),
            )
            project_id = cur.fetchone()["id"]
            cur.execute("INSERT INTO ai_knowledge_folders (project_slug, title, system, sort_order) "
                        "VALUES (%s, %s, TRUE, 0)", (slug, UNFILED_TITLE))
            if cover:
                _write_cover(cur, project_id, cover)
    return get_project(slug, email)  # type: ignore[return-value]


def update_project(slug: str, email: str, *, title: str = "", summary: str = "") -> KnowledgeProject:
    pages.ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, title, system, owner_email FROM ai_knowledge_projects WHERE slug = %s",
                        (_text(slug),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email:
                raise PermissionError("knowledge project not found")
            if row["system"] and title and _text(title) != (row["title"] or ""):
                # The unfiled project is what every unfiled page resolves to. Renaming
                # it does not move those pages, it just makes the card a lie.
                raise BusinessKnowledgeError("「未归档」不能改名 / the 未归档 project cannot be renamed")
            cur.execute("UPDATE ai_knowledge_projects SET title = COALESCE(NULLIF(%s, ''), title), "
                        "summary = %s, updated_at = NOW() WHERE id = %s",
                        (_text(title, limit=MAX_TITLE), _text(summary, limit=MAX_SUMMARY), row["id"]))
    return get_project(slug, email)  # type: ignore[return-value]


def delete_project(slug: str, email: str) -> list[str]:
    """Delete a project and return the slugs of the pages that fell back to 未归档.

    Deleting the container must not delete its content: the pages go back to
    未归档, which is exactly what "the folder I put this in is gone" should mean.
    The system project cannot be deleted — it is the fallback everything lands in.
    """
    pages.ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, system, owner_email FROM ai_knowledge_projects WHERE slug = %s",
                        (_text(slug),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email:
                raise PermissionError("knowledge project not found")
            if row["system"]:
                raise BusinessKnowledgeError("「未归档」不能删除 / the 未归档 project cannot be deleted")
            cur.execute("SELECT slug FROM ai_knowledge_items WHERE project_slug = %s "
                        "AND visibility = 'private' ORDER BY slug", (row["slug"],))
            moved = [r["slug"] for r in cur.fetchall()]
            cur.execute("UPDATE ai_knowledge_items SET project_slug = NULL, folder_id = NULL, "
                        "updated_at = NOW() WHERE project_slug = %s AND visibility = 'private'",
                        (row["slug"],))
            # `ai_knowledge_folders` rows go with the project (ON DELETE CASCADE), and
            # with them the shares — so a folder that had been shared with a colleague
            # stops granting anything the moment its project is gone.
            cur.execute("DELETE FROM ai_knowledge_projects WHERE id = %s", (row["id"],))
    return moved


# ── folders ─────────────────────────────────────────────────────────────────

_SELECT_FOLDER = "id, project_slug, title, system, sort_order"


def _row_to_folder(row: dict, *, can_manage: bool = False, shared: list[str] | None = None) -> KnowledgeFolder:
    return KnowledgeFolder(
        id=row["id"], project_slug=row["project_slug"], title=row["title"],
        system=bool(row.get("system")), sort_order=row.get("sort_order") or 0,
        item_count=row.get("item_count") or 0, shared_emails=shared or [],
        can_manage=can_manage,
    )


def list_folders(project_slug: str, email: str) -> list[KnowledgeFolder]:
    """The folders of one project the caller may see.

    A reader who was given one folder does **not** see the owner's other folders:
    their titles are the owner's filing decisions, and a shared folder that
    revealed the rest of somebody's structure would be a quiet directory listing.
    """
    pages.ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # `slug` is selected as well as owner/system: the folder statements below
            # filter on `project_slug`, and reusing the caller's `slug` argument there
            # would mean trusting a value that has not been matched against the row.
            cur.execute("SELECT slug, owner_email, system FROM ai_knowledge_projects WHERE slug = %s",
                        (_text(project_slug),))
            project = cur.fetchone()
            if not project:
                return []
            mine = project["owner_email"] == email
            if mine:
                cur.execute("SELECT " + _SELECT_FOLDER + " FROM ai_knowledge_folders "
                            "WHERE project_slug = %s ORDER BY system DESC, sort_order, id", (project["slug"],))
                rows = [dict(r) for r in cur.fetchall()]
                return [_folder_with_count(cur, row, email, can_manage=True) for row in rows]
            cur.execute("SELECT " + _SELECT_FOLDER + " FROM ai_knowledge_folders f "
                        "JOIN ai_knowledge_folder_shares fs ON fs.folder_id = f.id "
                        "WHERE f.project_slug = %s AND fs.recipient_email = %s "
                        "ORDER BY f.sort_order, f.id", (project["slug"], email))
            rows = [dict(r) for r in cur.fetchall()]
            return [_folder_with_count(cur, row, email, can_manage=False) for row in rows]


def _folder_with_count(cur, row: dict, email: str, *, can_manage: bool) -> KnowledgeFolder:
    """Fill in a folder's readable page count and its share list, on an open cursor.

    The count goes through the permission predicate rather than counting rows: a
    folder card that says "12 pages" to a reader who may read 3 is the same leak as
    listing 12 titles.

    ⚠️ The **system** folder is the unfiled one, and the pages in it are the rows
    with `folder_id IS NULL`. Counting `folder_id = <system folder>` instead would
    report the catch-all as permanently empty — the one number a reader is most
    likely to look at, and wrong in the direction that looks like data loss.
    """
    clause = "folder_id IS NULL" if row.get("system") else "folder_id = %s"
    params = () if row.get("system") else (row["id"],)
    # One email per `%s`, and READABLE_PREDICATE carries three — same trap as
    # `list_projects`, and the count is spelled out rather than computed so a reader
    # adding a branch sees what has to change.
    cur.execute("SELECT count(*) AS n FROM ai_knowledge_items WHERE " + clause + " AND "
                + pages.READABLE_PREDICATE, params + (email, email, email))
    row["item_count"] = cur.fetchone()["n"]
    return _row_to_folder(row, can_manage=can_manage, shared=_folder_shared_emails(cur, row["id"]))


def get_folder(folder_id: int, email: str) -> KnowledgeFolder | None:
    pages.ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT f.id, f.project_slug, f.title, f.system, f.sort_order, p.owner_email "
                        "FROM ai_knowledge_folders f JOIN ai_knowledge_projects p ON p.slug = f.project_slug "
                        "WHERE f.id = %s", (int(folder_id),))
            row = cur.fetchone()
            if not row:
                return None
            mine = row["owner_email"] == email
            if not mine:
                cur.execute("SELECT 1 FROM ai_knowledge_folder_shares "
                            "WHERE folder_id = %s AND recipient_email = %s", (folder_id, email))
                if cur.fetchone() is None:
                    return None
            return _folder_with_count(cur, dict(row), email, can_manage=mine)


def create_folder(project_slug: str, email: str, title: str) -> KnowledgeFolder:
    """A folder inside a project the caller owns."""
    pages.ensure_tables()
    title = _text(title, limit=MAX_TITLE)
    if not title:
        raise BusinessKnowledgeError("缺少文件夹名称 / the folder needs a title")
    if _is_unfiled_name(title):
        # Same reservation as the project's, for the same reason: every project already
        # has a 未归档 folder, so a second one in the rail would leave two rows the
        # reader cannot tell apart when they mean "put it in 未归档".
        raise BusinessKnowledgeError(
            "「未归档」每个项目已经自带了 / every project already has a 未归档 folder")
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, slug, owner_email, system FROM ai_knowledge_projects WHERE slug = %s",
                        (_text(project_slug),))
            project = cur.fetchone()
            if not project or project["owner_email"] != email:
                raise PermissionError("knowledge project not found")
            cur.execute("SELECT 1 FROM ai_knowledge_folders "
                        "WHERE project_slug = %s AND lower(btrim(title)) = lower(%s)",
                        (project["slug"], title))
            if cur.fetchone() is not None:
                # Two folders with the same name in one project is a filing scheme
                # nobody can file into. Compared case-insensitively because the two
                # scripts that type the name disagree about case constantly.
                raise BusinessKnowledgeError("这个项目里已经有同名文件夹了 / this project already has a folder with that name")
            cur.execute("SELECT COALESCE(MAX(sort_order), 0) + 10 AS n FROM ai_knowledge_folders "
                        "WHERE project_slug = %s", (project["slug"],))
            order = cur.fetchone()["n"]
            cur.execute("INSERT INTO ai_knowledge_folders (project_slug, title, sort_order) "
                        "VALUES (%s, %s, %s) RETURNING id", (project["slug"], title, order))
            folder_id = cur.fetchone()["id"]
    return get_folder(folder_id, email)  # type: ignore[return-value]


def rename_folder(folder_id: int, email: str, title: str) -> KnowledgeFolder:
    pages.ensure_tables()
    title = _text(title, limit=MAX_TITLE)
    if not title:
        raise BusinessKnowledgeError("缺少文件夹名称 / the folder needs a title")
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT f.id, f.system, f.project_slug, p.owner_email "
                        "FROM ai_knowledge_folders f JOIN ai_knowledge_projects p ON p.slug = f.project_slug "
                        "WHERE f.id = %s", (int(folder_id),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email:
                raise PermissionError("knowledge folder not found")
            if row["system"]:
                raise BusinessKnowledgeError("「未归档」不能改名 / the 未归档 folder cannot be renamed")
            cur.execute("SELECT 1 FROM ai_knowledge_folders "
                        "WHERE project_slug = %s AND lower(btrim(title)) = lower(%s) AND id <> %s",
                        (row["project_slug"], title, int(folder_id)))
            if cur.fetchone() is not None:
                raise BusinessKnowledgeError("这个项目里已经有同名文件夹了 / this project already has a folder with that name")
            cur.execute("UPDATE ai_knowledge_folders SET title = %s, updated_at = NOW() WHERE id = %s",
                        (title, row["id"]))
    return get_folder(folder_id, email)  # type: ignore[return-value]


def delete_folder(folder_id: int, email: str) -> list[str]:
    """Delete a folder; its pages go back to 未归档 rather than being deleted.

    Same promise as deleting a project: the container is the user's filing decision,
    the pages are the knowledge. Deleting a folder must never be a way to lose pages.
    """
    pages.ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT f.id, f.system, p.owner_email FROM ai_knowledge_folders f "
                        "JOIN ai_knowledge_projects p ON p.slug = f.project_slug WHERE f.id = %s",
                        (int(folder_id),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email:
                raise PermissionError("knowledge folder not found")
            if row["system"]:
                raise BusinessKnowledgeError("「未归档」不能删除 / the 未归档 folder cannot be deleted")
            cur.execute("SELECT slug FROM ai_knowledge_items WHERE folder_id = %s "
                        "AND visibility = 'private' ORDER BY slug", (row["id"],))
            moved = [r["slug"] for r in cur.fetchall()]
            cur.execute("UPDATE ai_knowledge_items SET folder_id = NULL, updated_at = NOW() "
                        "WHERE folder_id = %s AND visibility = 'private'", (row["id"],))
            cur.execute("DELETE FROM ai_knowledge_folders WHERE id = %s", (row["id"],))
    return moved


# ── folder sharing ──────────────────────────────────────────────────────────

def _folder_shared_emails(cur, folder_id: int) -> list[str]:
    cur.execute("SELECT recipient_email FROM ai_knowledge_folder_shares WHERE folder_id = %s "
                "ORDER BY recipient_email", (folder_id,))
    return [row["recipient_email"] for row in cur.fetchall()]


def share_folder(folder_id: int, email: str, recipients: list[str], shared_by: str) -> list[str]:
    """Grant colleagues read access to a folder — and to every page filed in it.

    **One row per folder, not per page.** That is the whole point: a page filed
    into the folder later inherits the grant with no second write, and revoking
    is a single DELETE instead of a recomputation over whatever the folder held at
    the time. The read path is `business_knowledge.READABLE_PREDICATE`.
    """
    pages.ensure_tables()
    cleaned = sorted({_text(r).lower() for r in recipients if _text(r)})
    if not cleaned:
        raise BusinessKnowledgeError("至少需要一个收件人邮箱 / at least one recipient email is required")
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT f.id, p.owner_email FROM ai_knowledge_folders f "
                        "JOIN ai_knowledge_projects p ON p.slug = f.project_slug WHERE f.id = %s",
                        (int(folder_id),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email:
                raise PermissionError("knowledge folder not found")
            for recipient in cleaned:
                cur.execute(
                    "INSERT INTO ai_knowledge_folder_shares (folder_id, recipient_email, shared_by) "
                    "VALUES (%s, %s, %s) ON CONFLICT (folder_id, recipient_email) DO NOTHING",
                    (row["id"], recipient, _text(shared_by)),
                )
            return _folder_shared_emails(cur, row["id"])


def unshare_folder(folder_id: int, email: str, recipient: str) -> list[str]:
    pages.ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT f.id, p.owner_email FROM ai_knowledge_folders f "
                        "JOIN ai_knowledge_projects p ON p.slug = f.project_slug WHERE f.id = %s",
                        (int(folder_id),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email:
                raise PermissionError("knowledge folder not found")
            cur.execute("DELETE FROM ai_knowledge_folder_shares WHERE folder_id = %s AND recipient_email = %s",
                        (row["id"], _text(recipient).lower()))
            return _folder_shared_emails(cur, row["id"])


def folder_title(folder_id: int) -> str:
    """A folder's title with no permission check.

    Only ever used to label an Inbox line for a share the caller has already been
    authorised to make — the same contract as ``pages.title_of``.
    """
    pages.ensure_tables()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT title FROM ai_knowledge_folders WHERE id = %s", (int(folder_id),))
            row = cur.fetchone()
    return str(row[0]) if row else ""


def folder_project(folder_id: int) -> str:
    """The project a folder belongs to, with no permission check (Inbox labels)."""
    pages.ensure_tables()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT project_slug FROM ai_knowledge_folders WHERE id = %s", (int(folder_id),))
            row = cur.fetchone()
    return str(row[0]) if row else ""


# ── the cover, reusing the Workspace mechanism ──────────────────────────────

def _write_cover(cur, project_id: int, cover: dict) -> None:
    cur.execute(
        "UPDATE ai_knowledge_projects SET cover_data = %s, cover_mime = %s, cover_w = %s, cover_h = %s, "
        "cover_prompt = %s, cover_model = %s, cover_url = %s, updated_at = NOW() WHERE id = %s",
        (cover.get("data"), cover.get("mime") or "", cover.get("w"), cover.get("h"),
         cover.get("prompt") or "", cover.get("model") or "", cover.get("url") or "", project_id),
    )


def set_cover(slug: str, email: str, cover: dict | None, *, clear: bool = False) -> KnowledgeProject:
    """Store a resolved cover on a project the caller owns.

    A dedicated endpoint rather than part of the project PUT, because the project
    PUT is a whole-row overwrite and a cover-only save must not blank the title —
    the same reasoning the Workspace report cover uses.
    """
    pages.ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, owner_email FROM ai_knowledge_projects WHERE slug = %s", (_text(slug),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email:
                raise PermissionError("knowledge project not found")
            if clear:
                cur.execute("UPDATE ai_knowledge_projects SET cover_data = NULL, cover_mime = '', "
                            "cover_w = NULL, cover_h = NULL, cover_prompt = '', cover_model = '', "
                            "cover_url = '' WHERE id = %s", (row["id"],))
            elif cover:
                _write_cover(cur, row["id"], cover)
    return get_project(slug, email)  # type: ignore[return-value]


def cover_bytes(slug: str, email: str) -> tuple[bytes, str, str]:
    """``(bytes, mime, external_url)`` for a project cover, like `ai_reports`.

    The only place ``cover_data`` is read — see the note in `list_projects`.
    Permission is decided in the same query rather than by a second call to
    `get_project`: a cover is public to anyone who may read the project, and the
    two answers must never disagree.
    """
    pages.ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT p.cover_data, p.cover_mime, p.cover_url, p.owner_email "
                "FROM ai_knowledge_projects p WHERE p.slug = %s AND (p.owner_email = %s OR EXISTS ("
                "  SELECT 1 FROM ai_knowledge_folders f "
                "  JOIN ai_knowledge_folder_shares fs ON fs.folder_id = f.id "
                "  WHERE f.project_slug = p.slug AND fs.recipient_email = %s))",
                (_text(slug), email, email),
            )
            row = cur.fetchone()
    if not row:
        return b"", "", ""
    data, external = row.get("cover_data"), (row.get("cover_url") or "")
    if data:
        return bytes(data), (row.get("cover_mime") or "") or "image/jpeg", external
    return b"", "", external
