"""Workspace projects and folders — the containers a report is filed into.

The shape, and why it is two levels
----------------------------------
**项目 (card) → 文件夹 → 报告.** Deliberately the same three-level filing the
Knowledge page uses (`services/ai/knowledge_projects.py`), because the two walls
are meant to be read with one eye: a project is what a person thinks of as a
"main folder" and what the Workspace page shows as a card; opening one gives a
current-directory style page, and the one level that matters inside it is the
folder. Nesting folders inside folders was not built — every extra level is a
second thing to resolve on every read, and nothing in the request needs it.

未归档
------
Every account has a **system project** (its own catch-all, the card you see
first), and every project has a **system folder** (its own catch-all inside it).
So "the report has no folder" always resolves to *a* 未归档 and the client never
has to ask where an unfiled report went. Neither is a special case in SQL: they
are rows with ``system = TRUE``, created on demand by `ensure_unfiled()`.

⚠️ **A report with `project_slug IS NULL` is in the unfiled project.** That is the
default for every report published before this feature existed, so NULL is a real
answer rather than missing data. `move_report()` is the only writer of the two
columns — publish and pull deliberately do NOT copy them, see `move_report`.

Cover
-----
A project card reuses the Workspace cover mechanism end to end: the same seven
columns `ai_reports` keeps, the same `COVER_ALIASES` request fields, and the same
`services/report_cover.py::resolve_cover()`. Nothing here calls the image model
directly.

Two deliberate differences from the knowledge implementation
-----------------------------------------------------------
1. **No folder sharing.** A knowledge folder share grants every page filed in it;
   reports share one document at a time through `ai_report_colleague_shares`, and
   the three scopes are *disjoint* by design (`routers/reports.py::_SCOPE_SQL`).
   Borrowing somebody else's report therefore has no container to live in, which
   is why the frontend keeps Public and Shared-with-me on a flat list. Adding a
   folder grant later is a new permission tier, not a tweak to this file.
2. **The counts below go through MY_FILES, not a "readable" union.** A project
   card counts the files *I* filed, because that is what the wall is: my filing
   structure. `visibility = 'private'` is part of that predicate on purpose — a
   published snapshot and a pulled copy are copies of a document, not files I
   filed, and counting them would make the 未归档 card disagree with the list
   behind it (the mine scope is `visibility = 'private'`). Count and list are
   built from one string so they cannot drift.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import psycopg2.extras

MAX_TITLE = 120
MAX_SUMMARY = 300

#: Same shape as the knowledge one, on a different table. 10 hex characters of the
#: account's own address: deterministic, so the unfiled project needs no lookup to
#: find, and per-account, so two accounts never collide on a globally unique slug.
UNFILED_SLUG_PREFIX = "unfiled-"

#: A report title is `routers/reports.py::MAX_TITLE` long and a knowledge page's is
#: 200, so this file's limit is the report's — a title this module truncates is a
#: title the card grid would have shown in full.
UNFILED_TITLE = "未归档 / Unfiled"

# Guarded by the same lock style as the report tables: this is called from request
# handlers, and two requests for a new account must not both create its unfiled
# project.
_unfiled_lock = threading.Lock()


class ReportProjectError(RuntimeError):
    """A refused container write. The router maps this to 400."""


@dataclass(frozen=True)
class ReportProject:
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
    report_count: int = 0
    created_at: Any = None
    updated_at: Any = None


@dataclass(frozen=True)
class ReportFolder:
    id: int
    project_slug: str
    title: str
    system: bool = False
    sort_order: int = 0
    report_count: int = 0
    can_manage: bool = False


# ── the tables ───────────────────────────────────────────────────────────────

def _ensure_tables() -> None:
    """Delegate to the router that owns `ai_reports`' schema.

    ⚠️ **A late import, on purpose.** The DDL for these three tables and the two
    new `ai_reports` columns lives beside the table itself, in
    `routers/reports.py::_ensure_table()` — one place, so there is no second
    schema to keep in step. Importing that module at the top of this file would
    be a cycle (it imports this one for the endpoints). Inside the function it is
    resolved after both modules exist, which is the same trick
    `routers/business_knowledge.py::project_cover` uses for its response class.
    """
    from routers import reports as reports_router

    reports_router._ensure_table()


def normalize_legacy_placement(cur) -> None:
    """Repair the old explicit system IDs in the schema transaction.

    Only private reports in containers owned by the same account qualify. Keep
    normal folder IDs inside the account's system project; only its slug is NULL.
    Do not change updated_at: repairing placement must not reorder documents.
    """
    cur.execute("""
        UPDATE ai_reports SET folder_id = NULL
        FROM ai_report_folders f JOIN ai_report_projects p ON p.slug = f.project_slug
        WHERE ai_reports.folder_id = f.id AND f.system
          AND ai_reports.owner_email = p.owner_email AND ai_reports.visibility = 'private'
          AND (ai_reports.project_slug = p.slug
               OR (p.system AND ai_reports.project_slug IS NULL))
    """)
    cur.execute("""
        UPDATE ai_reports SET project_slug = NULL
        FROM ai_report_projects p
        WHERE ai_reports.project_slug = p.slug AND p.system
          AND ai_reports.owner_email = p.owner_email AND ai_reports.visibility = 'private'
    """)


def _db():
    """The reports connection, from the module that owns it."""
    from routers import reports as reports_router

    return reports_router._db()


def _text(value: Any, limit: int = 0) -> str:
    """Normalise to a stripped string, optionally truncated.

    Copied rather than imported: `routers.reports` has no `_text`, and importing
    the knowledge one would make the Workspace's containers depend on the wiki's
    service for a three-line helper.
    """
    if value is None:
        return ""
    out = str(value).strip()
    if limit and len(out) > limit:
        out = out[:limit].rstrip()
    return out


# ── the unfiled project / folder, created on demand ──────────────────────────

def unfiled_slug(email: str) -> str:
    """The slug of this account's own 未归档 project. Pure; writes nothing."""
    import hashlib

    digest = hashlib.sha256(_text(email).lower().encode("utf-8")).hexdigest()
    return UNFILED_SLUG_PREFIX + digest[:10]


def ensure_unfiled(email: str) -> tuple[str, int]:
    """This account's unfiled project slug and its unfiled folder id.

    Idempotent and safe to call on every read: the row is found by its
    deterministic slug, and the folder by the partial unique index on ``system``.
    """
    _ensure_tables()
    slug = unfiled_slug(email)
    with _unfiled_lock, _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT slug FROM ai_report_projects WHERE slug = %s", (slug,))
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO ai_report_projects (slug, title, system, owner_email) "
                    "VALUES (%s, %s, TRUE, %s) ON CONFLICT (slug) DO NOTHING",
                    (slug, UNFILED_TITLE, _text(email)),
                )
            cur.execute("SELECT id FROM ai_report_folders WHERE project_slug = %s AND system", (slug,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO ai_report_folders (project_slug, title, system, sort_order) "
                    "VALUES (%s, %s, TRUE, 0) RETURNING id",
                    (slug, UNFILED_TITLE),
                )
                row = cur.fetchone()
    return slug, int(row["id"])


def _is_unfiled_name(title: str) -> bool:
    """Whether a person-typed name is the reserved 未归档.

    ⚠️ **Both sides of the pair, and the pair itself.** A user types 未归档; they do
    not type the two-language constant. Matching only `UNFILED_TITLE` would let the
    Chinese half through — which is the half everybody actually types — and the
    wall would then show two cards reading 未归档. The English side is refused with
    it so the name cannot be half-matched either.
    """
    wanted = {UNFILED_TITLE, "未归档", "Unfiled", "unfiled"}
    return _text(title).strip().casefold() in {w.casefold() for w in wanted}


def clean_slug(value: Any, title: str = "") -> str:
    """A URL slug for a project title.

    Reuses `routers/reports.py::_clean_slug` rather than a second slugifier: that one
    already refuses to strip a Chinese title to nothing (it falls back to a hash of
    the title, see `_slugify`), and a second implementation would eventually be the
    one that forgets.
    """
    from routers import reports as reports_router

    return reports_router._clean_slug(value, title)


# ── the one predicate the wall and the directory share ───────────────────────
#
# "A file of mine, filed here." `visibility = 'private'` is part of it on purpose:
# the mine scope in `routers/reports.py::_SCOPE_SQL` is
# `visibility = 'private' AND owner_email = %s`, so a card that counted anything
# else would disagree with the list behind it — and a card that says "3 reports"
# over a list showing 1 is the reader's first evidence that the numbers are wrong.
# ⚠️ **One `%s` for the owner, and it is the only one.** `NOT_SHARED_PREDICATE`
# deliberately has no share branch, which is also what makes these counts
# computable in one pass: a share is per-report, not per-folder, so there is no
# folder-level grant to OR in.
MY_FILES = "owner_email = %s AND visibility = 'private'"


# ── projects ─────────────────────────────────────────────────────────────────

_SELECT_PROJECT = ("id, slug, title, summary, system, owner_email, cover_mime, cover_w, cover_h, "
                   "cover_prompt, cover_model, cover_url, created_at, updated_at")


def _row_to_project(row: dict) -> ReportProject:
    return ReportProject(
        id=row["id"], slug=row["slug"], title=row["title"], summary=row.get("summary") or "",
        system=bool(row.get("system")), owner_email=row.get("owner_email") or "",
        has_cover=bool(row.get("has_cover")) or bool(row.get("cover_url")),
        cover_url=row.get("cover_url") or "",
        cover_w=row.get("cover_w"), cover_h=row.get("cover_h"),
        cover_prompt=row.get("cover_prompt") or "",
        folder_count=row.get("folder_count") or 0, report_count=row.get("report_count") or 0,
        created_at=row.get("created_at"), updated_at=row.get("updated_at"),
    )


def list_projects(email: str) -> list[ReportProject]:
    """The card wall: my own projects.

    ⚠️ **Mine only, and that is the scope split, not an omission.** A borrowed
    report is filed in its owner's project; showing a wall of projects the reader
    cannot open would be a wall of dead ends — the same reasoning that keeps
    Public and Shared-with-me on a flat list in the client.

    The unfiled project always comes first and always exists: it is where an
    unfiled report is shown, so a wall without it would hide those reports.

    ⚠️ ``cover_data`` is never selected (a card list is dozens of rows and a JPEG
    each would be hundreds of MB). Only `GET …/cover` reads the bytes, in its own
    query — the same rule `ai_report_folders` follows.
    """
    ensure_unfiled(email)
    # Two subqueries, two `%s` each: the project slug (only for a non-system card,
    # so it is spliced rather than bound) and the owner. See `_files_in_project`.
    sql = ("SELECT " + _SELECT_PROJECT + ","
           " (p.cover_data IS NOT NULL) AS has_cover,"
           # ⚠️ **No table alias on the inner queries.** The placed-files predicate
           # spells `ai_reports.project_slug`, so aliasing the inner table would
           # break the reference — and it breaks it as a 500 from PostgreSQL
           # ("invalid reference to FROM-clause entry"), which names neither the
           # statement nor the alias.
           " (SELECT count(*) FROM ai_reports WHERE"
           # The unfiled project OWNS the rows with `project_slug IS NULL`, so a
           # card that counted only `project_slug = p.slug` would show 未归档 as
           # empty while its own folder listed the reports — two numbers about the
           # same pile, one of them wrong.
           "   CASE WHEN p.system THEN ai_reports.project_slug IS NULL"
           "        ELSE ai_reports.project_slug = p.slug END AND"
           "   " + MY_FILES + ") AS report_count,"
           " (SELECT count(*) FROM ai_report_folders f WHERE f.project_slug = p.slug) AS folder_count"
           " FROM ai_report_projects p WHERE p.owner_email = %s"
           " ORDER BY p.system DESC, p.updated_at DESC")
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # ⚠️ **Two emails, because the statement has two `%s`** — one inside the
            # placed-files subquery (`MY_FILES`) and one in the `WHERE`. The folder
            # count below names `p.slug`, which needs no parameter at all. Passing a
            # third email is a 500 from psycopg2 ("tuple index out of range") that
            # names neither the statement nor the cause, so the number is written out
            # rather than filled in by a loop that a future edit would silently grow.
            cur.execute(sql, (email, email))
            rows = [dict(r) for r in cur.fetchall()]
    projects = [_row_to_project(row) for row in rows]
    unfiled = unfiled_slug(email)
    projects.sort(key=lambda p: (p.slug != unfiled, not p.system, p.slug))
    return projects


def get_project(slug: str, email: str) -> ReportProject | None:
    """One project, or None when it is missing **or** the caller is not its owner.

    None rather than 403: a stranger must not learn that a project with this slug
    exists. (Unlike the knowledge page there is no folder grant to widen this to,
    so ownership is the whole rule.)
    """
    _ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT " + _SELECT_PROJECT + " FROM ai_report_projects p WHERE p.slug = %s",
                        (_text(slug),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email:
                return None
    return _row_to_project(dict(row))


def create_project(email: str, title: str, *, summary: str = "", cover: dict | None = None) -> ReportProject:
    """Create a project, with its own 未归档 folder, in one transaction.

    The system folder is created **here** rather than lazily: a project the user
    just created and can see but cannot file anything into looks broken, and the
    partial unique index would let a second one appear if the lazy path raced.

    ⚠️ **The name checks come BEFORE `_ensure_tables()`.** They are pure input
    validation, and the answer does not depend on a single row — so a caller that
    typed the reserved name should get its 400 without the request first opening a
    database connection, and the function should be testable without a seam at all.
    """
    title = _text(title, limit=MAX_TITLE)
    if not title:
        raise ReportProjectError("缺少项目名称 / the project needs a title")
    if _is_unfiled_name(title):
        # ⚠️ The account already HAS a 未归档 — it is the system project. A second
        # one is not a duplicate the reader can tell apart: the wall would show the
        # same name twice, and "put it in 未归档" would become ambiguous.
        raise ReportProjectError(
            "「未归档」是每个账号自带的，用它来收纳没有指定文件夹的报告"
            " / 未归档 already exists on every account — it is where reports with no "
            "folder go, so a second project by that name would be ambiguous")
    _ensure_tables()
    slug = clean_slug(None, title)
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            attempt = 0
            while True:
                cur.execute("SELECT 1 FROM ai_report_projects WHERE slug = %s", (slug,))
                if cur.fetchone() is None:
                    break
                attempt += 1
                if attempt > 6:
                    raise ReportProjectError("无法分配项目 slug / could not allocate a project slug")
                slug = f"{clean_slug(None, title)[:60]}-{attempt + 1}"
            cur.execute(
                "INSERT INTO ai_report_projects (slug, title, summary, owner_email) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (slug, title, _text(summary, limit=MAX_SUMMARY), _text(email)),
            )
            project_id = cur.fetchone()["id"]
            cur.execute("INSERT INTO ai_report_folders (project_slug, title, system, sort_order) "
                        "VALUES (%s, %s, TRUE, 0)", (slug, UNFILED_TITLE))
            if cover:
                _write_cover(cur, project_id, cover)
    return get_project(slug, email)  # type: ignore[return-value]


def update_project(slug: str, email: str, *, title: str = "", summary: str = "") -> ReportProject:
    _ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, title, system, owner_email FROM ai_report_projects WHERE slug = %s",
                        (_text(slug),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email:
                raise PermissionError("report project not found")
            if row["system"] and title and _text(title) != (row["title"] or ""):
                # The unfiled project is what every unfiled report resolves to.
                # Renaming it does not move those reports, it just makes the card a
                # lie.
                raise ReportProjectError("「未归档」不能改名 / the 未归档 project cannot be renamed")
            cur.execute("UPDATE ai_report_projects SET title = COALESCE(NULLIF(%s, ''), title), "
                        "summary = %s, updated_at = NOW() WHERE id = %s",
                        (_text(title, limit=MAX_TITLE), _text(summary, limit=MAX_SUMMARY), row["id"]))
    return get_project(slug, email)  # type: ignore[return-value]


def delete_project(slug: str, email: str) -> list[str]:
    """Delete a project and return the slugs of the reports that fell back to 未归档.

    Deleting the container must not delete its content: the reports go back to
    未归档, which is exactly what "the folder I put this in is gone" should mean.
    The system project cannot be deleted — it is the fallback everything lands in.
    """
    _ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, slug, system, owner_email FROM ai_report_projects WHERE slug = %s",
                        (_text(slug),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email:
                raise PermissionError("report project not found")
            if row["system"]:
                raise ReportProjectError("「未归档」不能删除 / the 未归档 project cannot be deleted")
            # ⚠️ Only the caller's own **private** files. A public snapshot in the
            # project belongs to the public area, where placement is not a filing
            # decision anybody made in this project, and resetting it would move a
            # document the caller is not looking at.
            cur.execute("SELECT slug FROM ai_reports WHERE project_slug = %s "
                        "AND visibility = 'private' AND owner_email = %s ORDER BY slug", (row["slug"], email))
            moved = [r["slug"] for r in cur.fetchall()]
            cur.execute("UPDATE ai_reports SET project_slug = NULL, folder_id = NULL, "
                        "updated_at = NOW() WHERE project_slug = %s AND visibility = 'private' AND owner_email = %s",
                        (row["slug"], email))
            # `ai_report_folders` rows go with the project (ON DELETE CASCADE).
            cur.execute("DELETE FROM ai_report_projects WHERE id = %s", (row["id"],))
    return moved


# ── folders ──────────────────────────────────────────────────────────────────

# ⚠️ **Every column is table-qualified.** This is a JOIN against
# `ai_report_projects`, and `id`, `system`, `created_at` and `updated_at` all exist in
# **both** tables — so the unqualified form this list had before
# `project_system` was added is a 500 from PostgreSQL ("column reference \"id\" is
# ambiguous") that names neither the statement nor the caller. Qualifying is not
# tidiness here, it is the difference between the endpoint existing and not.
_SELECT_FOLDER = ("f.id, f.project_slug, f.title, f.system, f.sort_order")


def _row_to_folder(row: dict, *, can_manage: bool = False) -> ReportFolder:
    return ReportFolder(
        id=row["id"], project_slug=row["project_slug"], title=row["title"],
        system=bool(row.get("system")), sort_order=row.get("sort_order") or 0,
        report_count=row.get("report_count") or 0, can_manage=can_manage,
    )


def list_folders(project_slug: str, email: str) -> list[ReportFolder]:
    """The folders of one project the caller owns.

    Returns [] for a project that is not the caller's, which is the same answer as
    a project that does not exist — see `get_project`.
    """
    _ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # `slug` is selected as well as owner/system: the folder statements below
            # filter on `project_slug`, and reusing the caller's argument there would
            # mean trusting a value that has not been matched against the row.
            cur.execute("SELECT slug, owner_email, system FROM ai_report_projects WHERE slug = %s",
                        (_text(project_slug),))
            project = cur.fetchone()
            if not project or project["owner_email"] != email:
                return []
            # ⚠️ **`p.system AS project_system` is load-bearing, not decoration.**
            # `_folder_with_count` has to know whether the project it belongs to is
            # the unfiled one, because a system folder's count is
            # `project_slug IS NULL` for the unfiled project and `project_slug = %s`
            # for every other one. Without this column the flag is always false, so
            # 未归档's own folder counts `project_slug = 'unfiled-…'` — which matches
            # nothing, because unfiled reports carry NULL. The result is the wall's
            # own catch-all reporting itself permanently empty, which reads as data
            # loss rather than as a broken query.
            cur.execute("SELECT " + _SELECT_FOLDER + ", p.system AS project_system "
                        "FROM ai_report_folders f "
                        "JOIN ai_report_projects p ON p.slug = f.project_slug "
                        "WHERE f.project_slug = %s ORDER BY f.system DESC, f.sort_order, f.id",
                        (project["slug"],))
            rows = [dict(r) for r in cur.fetchall()]
            return [_folder_with_count(cur, row, email, can_manage=True) for row in rows]


def _folder_with_count(cur, row: dict, email: str, *, can_manage: bool) -> ReportFolder:
    """Fill in a folder's file count, on an open cursor.

    ⚠️ The **system** folder is the unfiled one, and the files in it are the rows
    with `folder_id IS NULL` *inside this project*. Counting only
    `folder_id IS NULL` — without the project — would report every project's
    catch-all as holding the whole account's unfiled pile, so two cards would show
    the same number and one of them would be about a project the reader is not in.
    The project clause is written here rather than shared with `list_projects`
    because the two sides count different shapes of "filed here" and a shared
    helper with a flag per shape is how the flag ends up wrong on one side only.
    """
    project_system = bool(row.get("project_system"))
    if project_system:
        project_clause = "ai_reports.project_slug IS NULL"
        params: list[Any] = []
    else:
        project_clause = "ai_reports.project_slug = %s"
        params = [row["project_slug"]]
    if row.get("system"):
        folder_clause = "ai_reports.folder_id IS NULL"
    else:
        folder_clause = "ai_reports.folder_id = %s"
        params.append(row["id"])
    params.append(email)
    cur.execute("SELECT count(*) AS n FROM ai_reports WHERE " + project_clause + " AND "
                + folder_clause + " AND " + MY_FILES, params)
    row["report_count"] = cur.fetchone()["n"]
    return _row_to_folder(row, can_manage=can_manage)


def get_folder(folder_id: int, email: str) -> ReportFolder | None:
    _ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT f.id, f.project_slug, f.title, f.system, f.sort_order, "
                        "p.owner_email, p.system AS project_system "
                        "FROM ai_report_folders f JOIN ai_report_projects p ON p.slug = f.project_slug "
                        "WHERE f.id = %s", (int(folder_id),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email:
                return None
            return _folder_with_count(cur, dict(row), email, can_manage=True)


def create_folder(project_slug: str, email: str, title: str) -> ReportFolder:
    """A folder inside a project the caller owns."""
    # ⚠️ Before `_ensure_tables()`, for the same reason as `create_project`: the name
    # checks are pure input validation and must not cost a database round trip.
    title = _text(title, limit=MAX_TITLE)
    if not title:
        raise ReportProjectError("缺少文件夹名称 / the folder needs a title")
    if _is_unfiled_name(title):
        # Same reservation as the project's, for the same reason: every project
        # already has a 未归档 folder, so a second one in the rail would leave two
        # rows the reader cannot tell apart when both mean "put it in 未归档".
        raise ReportProjectError(
            "「未归档」每个项目已经自带了 / every project already has a 未归档 folder")
    _ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, slug, owner_email, system FROM ai_report_projects WHERE slug = %s",
                        (_text(project_slug),))
            project = cur.fetchone()
            if not project or project["owner_email"] != email:
                raise PermissionError("report project not found")
            cur.execute("SELECT 1 FROM ai_report_folders "
                        "WHERE project_slug = %s AND lower(btrim(title)) = lower(%s)",
                        (project["slug"], title))
            if cur.fetchone() is not None:
                # Two folders with the same name in one project is a filing scheme
                # nobody can file into. Compared case-insensitively because the two
                # scripts that type the name disagree about case constantly.
                raise ReportProjectError("这个项目里已经有同名文件夹了 / this project already has a folder with that name")
            cur.execute("SELECT COALESCE(MAX(sort_order), 0) + 10 AS n FROM ai_report_folders "
                        "WHERE project_slug = %s", (project["slug"],))
            order = cur.fetchone()["n"]
            cur.execute("INSERT INTO ai_report_folders (project_slug, title, sort_order) "
                        "VALUES (%s, %s, %s) RETURNING id", (project["slug"], title, order))
            folder_id = cur.fetchone()["id"]
    return get_folder(folder_id, email)  # type: ignore[return-value]


def rename_folder(folder_id: int, email: str, title: str) -> ReportFolder:
    _ensure_tables()
    title = _text(title, limit=MAX_TITLE)
    if not title:
        raise ReportProjectError("缺少文件夹名称 / the folder needs a title")
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT f.id, f.system, f.project_slug, p.owner_email "
                        "FROM ai_report_folders f JOIN ai_report_projects p ON p.slug = f.project_slug "
                        "WHERE f.id = %s", (int(folder_id),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email:
                raise PermissionError("report folder not found")
            if row["system"]:
                raise ReportProjectError("「未归档」不能改名 / the 未归档 folder cannot be renamed")
            if _is_unfiled_name(title):
                raise ReportProjectError(
                    "「未归档」每个项目已经自带了 / every project already has a 未归档 folder")
            cur.execute("SELECT 1 FROM ai_report_folders "
                        "WHERE project_slug = %s AND lower(btrim(title)) = lower(%s) AND id <> %s",
                        (row["project_slug"], title, int(folder_id)))
            if cur.fetchone() is not None:
                raise ReportProjectError("这个项目里已经有同名文件夹了 / this project already has a folder with that name")
            cur.execute("UPDATE ai_report_folders SET title = %s, updated_at = NOW() WHERE id = %s",
                        (title, row["id"]))
    return get_folder(folder_id, email)  # type: ignore[return-value]


def delete_folder(folder_id: int, email: str) -> list[str]:
    """Delete a folder; its files go back to 未归档 rather than being deleted.

    Same promise as deleting a project: the container is the user's filing
    decision, the reports are the work. Deleting a folder must never be a way to
    lose a report.
    """
    _ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT f.id, f.system, p.owner_email FROM ai_report_folders f "
                        "JOIN ai_report_projects p ON p.slug = f.project_slug WHERE f.id = %s",
                        (int(folder_id),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email:
                raise PermissionError("report folder not found")
            if row["system"]:
                raise ReportProjectError("「未归档」不能删除 / the 未归档 folder cannot be deleted")
            cur.execute("SELECT slug FROM ai_reports WHERE folder_id = %s "
                        "AND visibility = 'private' AND owner_email = %s ORDER BY slug", (row["id"], email))
            moved = [r["slug"] for r in cur.fetchall()]
            # ⚠️ The project is **kept** and only the folder is cleared. Resetting
            # `project_slug` as well would drop the file into 未归档 and leave a hole
            # in the project it was filed in — "I deleted a folder" must not also
            # mean "I moved the file out of the project".
            cur.execute("UPDATE ai_reports SET folder_id = NULL, updated_at = NOW() "
                        "WHERE folder_id = %s AND visibility = 'private' AND owner_email = %s", (row["id"], email))
            cur.execute("DELETE FROM ai_report_folders WHERE id = %s", (row["id"],))
    return moved


# ── moving a report ──────────────────────────────────────────────────────────

def move_report(slug: str, email: str, project_slug: str, folder_id: int | None) -> dict:
    """File a report under ``project_slug`` / ``folder_id``, or back into 未归档.

    **The only writer of the two placement columns.** Everything else — the report
    PUT, publish, pull, upload, to-static — leaves them alone, so "where does this
    report live" has one answer in the table and one code path on the server.

    ⚠️ **Publish and pull deliberately do NOT copy the placement**, even though they
    copy almost every other column. A public snapshot and a pulled copy are
    *copies of a document*, not files filed in a folder: the pulled copy in
    particular has no folder of its own yet, and inheriting the source's would file
    a colleague's report into a folder the reader never chose. They land in 未归档,
    which is also what keeps a project's count equal to the list behind it.

    Ownership is checked on **both** the report and the target folder, server-side —
    a menu that filtered the target list in the browser would still be a request
    the browser can be made to send.
    """
    _ensure_tables()
    project_slug = _text(project_slug)
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, slug, owner_email, visibility FROM ai_reports "
                        "WHERE slug = %s FOR UPDATE", (_text(slug),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email or row["visibility"] != "private":
                # 404 rather than 403, everywhere else in this module.
                raise PermissionError("report not found")
            if not project_slug:
                # Moving to 未归档 writes NULLs rather than the unfiled project's
                # slug. Both spell the same place, and NULL is the one a report
                # published before this feature already has.
                cur.execute("UPDATE ai_reports SET project_slug = NULL, folder_id = NULL, "
                            "updated_at = NOW() WHERE id = %s", (row["id"],))
            else:
                # The folder must exist, must belong to the named project, and that
                # project must be the caller's — otherwise a caller could file their
                # own report into a stranger's folder.
                cur.execute(
                    """
                    SELECT f.id, f.system, p.system AS project_system FROM ai_report_folders f
                    JOIN ai_report_projects p ON p.slug = f.project_slug
                    WHERE f.id = %s AND f.project_slug = %s AND p.owner_email = %s
                    """,
                    (folder_id, project_slug, email),
                )
                target = cur.fetchone()
                if target is None:
                    raise ReportProjectError(
                        "目标文件夹不存在或不属于你 / that folder does not exist or is not yours")
                # System containers represent NULL placement on both read and write.
                stored_project = None if target["project_system"] else project_slug
                stored_folder = None if target["system"] else int(folder_id)
                cur.execute("UPDATE ai_reports SET project_slug = %s, folder_id = %s, "
                            "updated_at = NOW() WHERE id = %s",
                            (stored_project, stored_folder, row["id"]))
            cur.execute("SELECT project_slug, folder_id FROM ai_reports WHERE id = %s", (row["id"],))
            placed = cur.fetchone()
    return {"slug": row["slug"],
            "project_slug": placed["project_slug"] or "",
            "folder_id": placed["folder_id"]}



# ── the cover, reusing the Workspace mechanism ───────────────────────────────

def _write_cover(cur, project_id: int, cover: dict) -> None:
    cur.execute(
        "UPDATE ai_report_projects SET cover_data = %s, cover_mime = %s, cover_w = %s, cover_h = %s, "
        "cover_prompt = %s, cover_model = %s, cover_url = %s, updated_at = NOW() WHERE id = %s",
        (cover.get("data"), cover.get("mime") or "", cover.get("w"), cover.get("h"),
         cover.get("prompt") or "", cover.get("model") or "", cover.get("url") or "", project_id),
    )


def set_cover(slug: str, email: str, cover: dict | None, *, clear: bool = False) -> ReportProject:
    """Store a resolved cover on a project the caller owns.

    A dedicated endpoint rather than part of the project PUT, because the project
    PUT is a whole-row overwrite and a cover-only save must not blank the title —
    the same reasoning the Workspace report cover uses.
    """
    _ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, owner_email FROM ai_report_projects WHERE slug = %s", (_text(slug),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email:
                raise PermissionError("report project not found")
            if clear:
                cur.execute("UPDATE ai_report_projects SET cover_data = NULL, cover_mime = '', "
                            "cover_w = NULL, cover_h = NULL, cover_prompt = '', cover_model = '', "
                            "cover_url = '' WHERE id = %s", (row["id"],))
            elif cover:
                _write_cover(cur, row["id"], cover)
    return get_project(slug, email)  # type: ignore[return-value]


def cover_bytes(slug: str, email: str) -> tuple[bytes, str, str]:
    """``(bytes, mime, external_url)`` for a project cover, like `ai_reports`.

    The only place ``cover_data`` is read — see the note in `list_projects`.
    Permission is decided in the same query rather than by a second call to
    `get_project`: the two answers must never disagree.
    """
    _ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT cover_data, cover_mime, cover_url, owner_email "
                "FROM ai_report_projects WHERE slug = %s AND owner_email = %s",
                (_text(slug), email),
            )
            row = cur.fetchone()
    if not row:
        return b"", "", ""
    data, external = row.get("cover_data"), (row.get("cover_url") or "")
    if data:
        return bytes(data), (row.get("cover_mime") or "") or "image/jpeg", external
    return b"", "", external
