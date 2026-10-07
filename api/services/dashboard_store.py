"""Dashboards — the tables, every statement, and the rules that need no database.

A dashboard is a page that asks for its own numbers: the agent writes the HTML, a reader
opens `/d/{slug}`, and the page calls `POST /api/dashboard/query` for whatever it wants
to draw. That is the whole product idea, and it is why the module owns its own tables
rather than borrowing `ai_reports` — Dashboard has to be sellable without Workspace.

Two things here are worth reading before changing anything:

* **`accent` is a token name, never a colour.** Every colour in this app lives in
  `theme.css` (AGENTS.md), so a stored `#ff8800` would be a card that is right in one
  theme and unreadable in the other. `validate_accent` refuses it with a 400 rather than
  accepting a value it cannot honour.
* **`datasets` is an allow-list, and it is checked as data, not as SQL.** The names are
  identifier-shaped so they can be compared against what the caller may see; the actual
  isolation is `services/dataset_query.py`, which asks the same question for a dashboard
  page that it asks for a Data Center query. There is deliberately no second allow-list
  here.

The split between the pure functions (`normalise_slug`, `validate_accent`,
`parse_datasets`, `scope_predicate`, `require_owner`, `card`) and the ones that talk to
PostgreSQL is deliberate: the rules worth arguing about are the ones a test can pin
without a database, and the DDL is the part there is nothing to decide.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extras

from core.db import connect_main
from klado_shared import orgs
from services import auth_store

TABLE = "public.ai_dashboards"
SHARES_TABLE = "public.ai_dashboard_colleague_shares"
#: One reader's saved filter view. Separate from the report equivalent on purpose —
#: see the DDL comment where it is created.
SELECTIONS_TABLE = "public.ai_dashboard_filter_selections"

# ── shapes ───────────────────────────────────────────────────────────────────

# A slug is a URL: `[a-z0-9][a-z0-9-]{2,63}` — 3 to 64 characters, no dot, no
# underscore, because this address is handed to people and typed by hand. Uppercase is
# refused rather than lowercased for the *validation* (the value is lowercased first, so
# a caller sending "Q3-Review" gets "q3-review" and one sending "Q3 review" is a 400).
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
# A theme token: `--accent`, `--ok`, `--warn` … written without the leading dashes.
ACCENT_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
# A dataset name is a relation name we generated from an upload, so it is an identifier.
DATASET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")

VISIBILITIES = ("private", "public")
STATUSES = ("published", "draft", "archived")
SCOPES = ("mine", "public", "shared")

MAX_TITLE = 240
MAX_SUMMARY = 600
MAX_DESCRIPTION = 2000
MAX_TAGS = 300
MAX_HTML_BYTES = 8 * 1024 * 1024        # the same ceiling a Workspace report has
MAX_DATASETS = 50
MAX_LIST_LIMIT = 500

DEFAULT_QUERY_ROWS = 500
MAX_QUERY_ROWS = 2000


# ── errors ───────────────────────────────────────────────────────────────────
#
# Messages are written as `中文 / English` pairs — the same translation memory the
# router and the SPA use. The router maps the class to a status code; the pair is
# already localisable by the time it reaches `pick()`.

class DashboardError(RuntimeError):
    """A dashboard request that went wrong. Bilingual message.

    ⚠️ `status` exists so a POLICY refusal can say so. `_fail()` in the router maps a
    plain `DashboardError` to **400**, which is right for "your input is malformed" and
    badly wrong for "your organization does not permit this" — a user shown 400 retypes
    the address forever, because from where they sit it is a typo. The organization gate
    raises this with `status=403`; everything else leaves it at the default.
    """

    status = 400

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        if status is not None:
            self.status = status


class DashboardPolicyError(DashboardError):
    """Refused by the sharing policy, not by the input. Always 403.

    A named subclass rather than a `status=` argument, so the router can branch on the
    TYPE as it already does for `DashboardConflict` / `DashboardTooLarge`, and so a
    refusal cannot be downgraded to 400 by a future edit to the mapping.
    """

    status = 403


class DashboardNotFound(DashboardError):
    """Absent, or not this caller's to see. **404, never 403** — the status code must
    not confirm that a slug exists for an account that cannot read it."""


class DashboardConflict(DashboardError):
    """The slug is taken by somebody else."""


class DashboardTooLarge(DashboardError):
    """The document is bigger than the ceiling."""


# ── schema ───────────────────────────────────────────────────────────────────

_schema_ready = False
_schema_lock = threading.Lock()


@contextmanager
def _conn():
    """Own the connection lifecycle. `with connect_main() as conn:` is NOT this:
    psycopg2's connection context manager commits, it never closes, so that shape
    leaks one connection per request."""
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
    """Create/upgrade the two tables. Idempotent, memoised per process.

    Memoisation is safe for the same reason `routers/reports.py` memoises its own:
    nothing in the app drops these tables by itself, and a table that really did
    disappear is repaired by `_with_schema` in the router, which clears this flag and
    retries once.
    """
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
                    CREATE TABLE IF NOT EXISTS {TABLE} (
                        id               SERIAL PRIMARY KEY,
                        slug             TEXT UNIQUE NOT NULL,
                        title            TEXT NOT NULL,
                        -- One line per language, the way a report card carries it: the
                        -- wall shows `summary` in the reader's language and
                        -- `summary_zh` in the other one.
                        summary          TEXT NOT NULL DEFAULT '',
                        summary_zh       TEXT NOT NULL DEFAULT '',
                        description      TEXT NOT NULL DEFAULT '',
                        tags             TEXT NOT NULL DEFAULT '',
                        -- A theme.css token name, NOT a colour (see the module docstring).
                        accent           TEXT NOT NULL DEFAULT '',
                        -- Which datasets this page is allowed to ask about. A declared
                        -- list is what the reader is told it may use; the real check is
                        -- `services/dataset_query.py`, which is the reader's own grant.
                        datasets         TEXT NOT NULL DEFAULT '[]',
                        html             TEXT NOT NULL DEFAULT '',
                        owner_email      TEXT NOT NULL,
                        visibility       TEXT NOT NULL DEFAULT 'private',
                        status           TEXT NOT NULL DEFAULT 'published',
                        pinned           BOOLEAN NOT NULL DEFAULT FALSE,
                        -- Content-level placement, set by the agent at publish time
                        -- because a dashboard is written knowing how it should be
                        -- presented. A per-user rearrangement is a different concern
                        -- and is not in this stage.
                        in_nav           BOOLEAN NOT NULL DEFAULT FALSE,
                        nav_order        INTEGER NOT NULL DEFAULT 0,
                        in_home          BOOLEAN NOT NULL DEFAULT FALSE,
                        home_order       INTEGER NOT NULL DEFAULT 0,
                        home_collapsed   BOOLEAN NOT NULL DEFAULT FALSE,
                        -- Cover bytes, exactly the same seven columns `ai_reports` keeps,
                        -- so `services/report_cover.py::resolve_cover` can serve both
                        -- features instead of a second copy drifting. A dashboard is a
                        -- card on a wall; a card with no image is a grey placeholder.
                        cover_data       BYTEA,
                        cover_mime       TEXT NOT NULL DEFAULT '',
                        cover_w          INTEGER,
                        cover_h          INTEGER,
                        cover_prompt     TEXT NOT NULL DEFAULT '',
                        cover_model      TEXT NOT NULL DEFAULT '',
                        cover_url        TEXT NOT NULL DEFAULT '',
                        size_bytes       INTEGER NOT NULL DEFAULT 0,
                        views            INTEGER NOT NULL DEFAULT 0,
                        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                # `CREATE TABLE IF NOT EXISTS` does nothing for a table that already
                # exists, so an install created before dashboards had covers would keep
                # its old shape and every cover write would fail with
                # 'column "cover_data" does not exist'. Add them idempotently.
                for _col, _ddl in (
                    ("cover_data", "BYTEA"),
                    ("cover_mime", "TEXT NOT NULL DEFAULT ''"),
                    ("cover_w", "INTEGER"),
                    ("cover_h", "INTEGER"),
                    ("cover_prompt", "TEXT NOT NULL DEFAULT ''"),
                    ("cover_model", "TEXT NOT NULL DEFAULT ''"),
                    ("cover_url", "TEXT NOT NULL DEFAULT ''"),
                ):
                    cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {_col} {_ddl}")

                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {SHARES_TABLE} (
                        id              SERIAL PRIMARY KEY,
                        dashboard_id    INTEGER NOT NULL REFERENCES ai_dashboards(id) ON DELETE CASCADE,
                        recipient_email TEXT NOT NULL,
                        shared_by       TEXT NOT NULL,
                        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (dashboard_id, recipient_email)
                    )
                """)
                cur.execute(f"CREATE INDEX IF NOT EXISTS ai_dashboards_owner_idx "
                            f"ON {TABLE} (owner_email, updated_at DESC)")
                cur.execute(f"CREATE INDEX IF NOT EXISTS ai_dashboards_placement_idx "
                            f"ON {TABLE} (in_nav, nav_order, in_home, home_order)")
                cur.execute(f"CREATE INDEX IF NOT EXISTS ai_dashboard_shares_recipient_idx "
                            f"ON {SHARES_TABLE} (recipient_email)")

                # ── 2026-10-05: HTML moved here from Workspace ────────────────────
                #
                # A dashboard used to be only ever a page that queried its own datasets,
                # and the columns below did not exist because there was nothing to hold.
                # HTML now lands here too, so a page can ALSO carry the two pieces of
                # reader-owned state a Workspace report could carry:
                #
                #   * `filters_json` — the author's schema, served by
                #     `GET/PUT/DELETE /api/dashboard/{slug}/filters`;
                #   * `state_json`   — this reader's annotation document, served by
                #     `GET/POST /api/dashboard/{slug}/state`;
                #
                # plus `kind` and `langs`, because the card wall and the reader branch on
                # them and a page that arrived from Workspace already knows its own.
                #
                # ⚠️ `ADD COLUMN IF NOT EXISTS` for every one, for the same reason the
                # cover columns above are: `CREATE TABLE IF NOT EXISTS` does nothing to
                # a table that already exists, so an install from before this change
                # would keep its old shape and every write here would fail with
                # 'column "filters_json" does not exist' — while the deployment looks
                # perfectly healthy.
                for _col, _ddl in (
                    # The report wall reads one line per language; a card that only has
                    # `summary_zh` shows an empty line to an English reader.
                    ("summary_en", "TEXT NOT NULL DEFAULT ''"),
                    ("langs", "TEXT NOT NULL DEFAULT ''"),
                    # ⚠️ `submitter` is here because the PUBLISH CONTRACT asks for it:
                    # a page that arrives from a chat request has to show who asked for
                    # it, and that is the field the reader looks at. Without the column
                    # the agent's value was accepted by the payload model and then
                    # dropped on the floor — a field that is "screen-effective" for the
                    # sender and "save-ineffective" for the file, which is the one
                    # failure mode this codebase treats as worse than a missing feature.
                    ("submitter", "TEXT NOT NULL DEFAULT ''"),
                    ("kind", "TEXT NOT NULL DEFAULT 'static'"),
                    ("filters_json", "TEXT NOT NULL DEFAULT ''"),
                    ("state_json", "TEXT"),
                    ("state_updated_at", "TIMESTAMPTZ"),
                ):
                    cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {_col} {_ddl}")

                # ⚠️ A SEPARATE table from `ai_report_filter_selections`, on purpose. A
                # reader who set a view on a report must not find it applied to a
                # dashboard: they are different documents that merely happen to be able
                # to carry the same schema, and a pick is a statement about ONE document.
                # Cascades with the dashboard, so deleting a page cannot leave stray
                # selection rows behind.
                # ⚠️ The key column is `doc_id`, NOT `dashboard_id`, even though every
                # other column here is dashboard-shaped. The shared reader in
                # `doc_state.saved_selection` takes a TABLE name and addresses the row by
                # `doc_id`, and naming this one differently would mean the function works
                # for reports and silently returns "no saved view" for every dashboard.
                # A generic column on a per-feature table is the smaller cost than a
                # reader that is quietly wrong on one of its two callers.
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {SELECTIONS_TABLE} (
                        doc_id        INTEGER NOT NULL REFERENCES {TABLE}(id) ON DELETE CASCADE,
                        user_email    TEXT NOT NULL,
                        selection_json TEXT NOT NULL DEFAULT '{{}}',
                        updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (doc_id, user_email)
                    )""")
                cur.execute("CREATE INDEX IF NOT EXISTS ai_dashboard_filter_selections_owner_idx "
                            f"ON {SELECTIONS_TABLE} (user_email)")
            # ⚠️ psycopg2 runs DDL inside the transaction: closing without a commit rolls
            # the CREATE TABLE back and every query then 500s with "relation does not
            # exist" while the deployment looks healthy.
            conn.commit()
        finally:
            conn.close()
        _schema_ready = True


def reset_schema_flag() -> None:
    """Forget that the tables exist, so the next `ensure_schema()` rebuilds them."""
    global _schema_ready
    with _schema_lock:
        _schema_ready = False


# ── pure rules ───────────────────────────────────────────────────────────────

def _text(value: Any, *, limit: int | None = None) -> str:
    text = "" if value is None else str(value).strip()
    return text[:limit] if limit else text


def normalise_slug(raw: Any, fallback_title: str = "") -> str:
    """The slug a dashboard is published under, or a 400-shaped `DashboardError`.

    Lowercased and trimmed. Empty falls back to the title, and a title with no
    ASCII in it falls back to a hash of that title — a constant prefix would make
    every Chinese-titled dashboard collide on one address and silently overwrite the
    one before it.
    """
    slug = (raw or "").strip().lower()
    if not slug:
        derived = re.sub(r"[^a-z0-9]+", "-", str(fallback_title or "").strip().lower())
        slug = derived.strip("-")[:64].strip("-")
    if not slug:
        digest = hashlib.md5(str(fallback_title or "dashboard").encode("utf-8")).hexdigest()[:8]
        slug = f"dash-{digest}"
    if not SLUG_RE.match(slug):
        raise DashboardError(
            "slug 只能是 3–64 个小写字母、数字和中划线，且以字母或数字开头"
            " / slug must be 3-64 characters of lowercase letters, digits and hyphens, "
            "starting with a letter or a digit")
    return slug


def validate_accent(raw: Any) -> str:
    """A theme token name (`accent`, `ok`, `warn`), or nothing.

    ⚠️ This is a regression guard, not a formality. A card that stores `#ff8800` is
    correct in the light theme and wrong in the dark one, and nobody finds out until a
    reader switches. `theme.css` owns every colour; a dashboard names one.
    """
    token = (raw or "").strip()
    if not token:
        return ""
    if not ACCENT_RE.match(token):
        raise DashboardError(
            f"accent 必须是 theme.css 里的颜色 token 名称（如 accent、ok、warn），不是颜色值：{token!r}"
            f" / accent must be a theme.css token name (accent, ok, warn …), not a colour value: {token!r}")
    return token


def parse_datasets(raw: Any) -> list[str]:
    """The dataset names a page may name, in order, de-duplicated.

    Accepts the list an HTTP body sends or the JSON text the column stores. Anything
    that is not a list of identifier-shaped strings is **refused**, not dropped: a name
    that was silently ignored would leave the page asking for a table it was told about
    and getting a 403 from the very guard that protects it.
    """
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DashboardError(
                "datasets 必须是数据集名称的 JSON 数组 / datasets must be a JSON array of dataset names"
            ) from exc
    if not isinstance(raw, list):
        raise DashboardError(
            "datasets 必须是数据集名称的数组 / datasets must be a list of dataset names")
    if len(raw) > MAX_DATASETS:
        raise DashboardError(
            f"datasets 最多 {MAX_DATASETS} 个（收到 {len(raw)} 个） / at most {MAX_DATASETS} datasets (got {len(raw)})")
    out: list[str] = []
    for entry in raw:
        name = entry.strip() if isinstance(entry, str) else None
        if name is None or not DATASET_RE.match(name):
            raise DashboardError(
                f"数据集名必须是标识符形式（字母或下划线开头，随后是字母、数字或下划线）：{entry!r}"
                f" / a dataset name must be identifier-shaped (a letter or underscore first, then"
                f" letters, digits or underscores): {entry!r}")
        if name not in out:
            out.append(name)
    return out


def datasets_json(names: list[str]) -> str:
    return json.dumps(list(names or []), ensure_ascii=False)


def datasets_of(row: dict) -> list[str]:
    """The dataset names on a row, as a list. Public because the reader's injected
    runtime needs the same view `card()` gives the wall.

    ⚠️ The column stores JSON *text*. Handing that text to a page would give the
    document the string `"[]"`, and `datasets[0]` would then be `"["` — a malformed
    query built from a field that looked fine in the payload.
    """
    return _read_datasets((row or {}).get("datasets"))


def _read_datasets(raw: Any) -> list[str]:
    """`datasets` as stored — tolerant, because this is a READ path and a list that
    cannot be parsed must not take the whole wall down."""
    try:
        return parse_datasets(raw)
    except DashboardError:
        return []


def clean_status(raw: Any, default: str = "published") -> str:
    value = _text(raw).lower() or default
    if value not in STATUSES:
        raise DashboardError(
            f"status 只能是 {', '.join(STATUSES)} 之一（收到 {value!r}）"
            f" / status must be one of {', '.join(STATUSES)} (got {value!r})")
    return value


def clean_visibility(raw: Any, default: str = "private") -> str:
    value = _text(raw).lower() or default
    if value not in VISIBILITIES:
        raise DashboardError(
            f"visibility 只能是 {', '.join(VISIBILITIES)} 之一（收到 {value!r}）"
            f" / visibility must be one of {', '.join(VISIBILITIES)} (got {value!r})")
    return value


def _int(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# ── the three list scopes ────────────────────────────────────────────────────
#
# `mine` is private-and-mine, `public` is the public wall, `shared` is a colleague's
# private row that was shared with my address. The three are DISJOINT, which is the
# point: a wall that mixed them would advertise manage actions (`can_manage`) on rows
# the viewer cannot change. `owner_email <> %s` in `shared` is what keeps a row I own
# out of my own "shared with me" list, and the visibility halves keep the public wall
# out of the other two.

SCOPE_SQL: dict[str, str] = {
    "mine": f"owner_email = %s AND {TABLE}.visibility = 'private'",
    "public": f"{TABLE}.visibility = 'public'",
    "shared": (f"{TABLE}.visibility = 'private' AND {TABLE}.status = 'published'"
               f" AND {TABLE}.owner_email <> %s"
               f" AND EXISTS (SELECT 1 FROM {SHARES_TABLE} cs"
               f" WHERE cs.dashboard_id = ai_dashboards.id AND cs.recipient_email = %s)"),
}
_SCOPE_PARAMS = {"mine": 1, "public": 0, "shared": 2}


def scope_predicate(scope: str, email: str) -> tuple[str, list[str]]:
    """The WHERE clause for one scope, plus the parameters it consumes.

    Parameters are counted from the same table the clauses are written in, so adding a
    scope cannot desynchronise the two.
    """
    name = _text(scope).lower() or "mine"
    if name not in SCOPE_SQL:
        raise DashboardError(
            f"scope 只能是 {', '.join(SCOPES)} 之一（收到 {scope!r}）"
            f" / scope must be one of {', '.join(SCOPES)} (got {scope!r})")
    return SCOPE_SQL[name], [email] * _SCOPE_PARAMS[name]


# ── the card the SPA renders ─────────────────────────────────────────────────

_SELECT_COLS = (
    "id, slug, title, summary, summary_zh, description, tags, accent, datasets, html, "
    "owner_email, visibility, status, pinned, in_nav, nav_order, in_home, home_order, "
    "home_collapsed, size_bytes, views, created_at, updated_at, "
    "cover_mime, cover_w, cover_h, cover_prompt, cover_model, cover_url, "
    "(cover_data IS NOT NULL) AS has_cover, "
    # A boolean, not the recipient list: the wall asks "was this shared with me?", and
    # shipping every grantee for 200 rows would leak addresses into a list view.
    f"EXISTS (SELECT 1 FROM {SHARES_TABLE} cs "
    "WHERE cs.dashboard_id = ai_dashboards.id AND cs.recipient_email = %s) AS shared_with_me, "
    # ⚠️ AFTER the `html,` the line above is stripped from — `get_dashboard` removes the
    # document with `replace(", html,", ",")` for list views, and a column placed before
    # that marker would take `html` out of the wrong position and 500 every list call.
    # These four ride along so the reader can tell a static page from a filterable one
    # and show the card's own line in the reader's language. `state_json` is NOT here:
    # it is this reader's document, served by its own endpoint, and a wall that carried
    # it would ship every reader's annotations to every reader.
    "summary_en, langs, kind, filters_json, submitter"
)


def card(row: dict, viewer_email: str = "", *, shared_with_me: bool = False) -> dict:
    """A DB row shaped for the list / detail payloads. `html` is added by the caller
    that actually has it."""
    return {
        "id": row.get("id"),
        "slug": row.get("slug") or "",
        "title": row.get("title") or "",
        "summary": row.get("summary") or "",
        "summary_zh": row.get("summary_zh") or "",
        # ⚠️ BOTH summary halves plus the declared languages, because the wall has to
        # pick the reader's line and cannot know the reader's language without them.
        # Shipping only `summary_zh` made an English reader see an empty line — the
        # field was stored the whole time and simply was not handed out.
        "summary_en": row.get("summary_en") or "",
        "langs": row.get("langs") or "",
        "submitter": row.get("submitter") or "",
        "description": row.get("description") or "",
        "tags": [t.strip() for t in (row.get("tags") or "").split(",") if t.strip()],
        "accent": row.get("accent") or "",
        "datasets": datasets_of(row),
        # ⚠️ `kind` and `has_filters` are the reader's ONLY cue that a page has a filter
        # rail, and they are here because the wall cannot afford to ship the schema
        # itself: 200 cards × 12 filters each is a list payload nobody should send. The
        # schema arrives with the one `GET /api/dashboard/{slug}/filters` the rail makes
        # when a reader opens the page.
        #
        # Without them a page with filters shows its slices and no way to choose values,
        # which reads as a broken page rather than a missing control.
        "kind": row.get("kind") or "static",
        "has_filters": bool(row.get("filters_json")),
        "status": row.get("status") or "published",
        "visibility": row.get("visibility") or "private",
        "owner_email": row.get("owner_email") or "",
        "pinned": bool(row.get("pinned")),
        "in_nav": bool(row.get("in_nav")),
        "nav_order": _int(row.get("nav_order")),
        "in_home": bool(row.get("in_home")),
        "home_order": _int(row.get("home_order")),
        "home_collapsed": bool(row.get("home_collapsed")),
        # Same shape the workspace wall uses: a boolean plus a path to fetch, never the
        # bytes. 200 dashboard cards with inline images would be megabytes of base64 in
        # a list view that only draws 200 × 10 KB thumbnails.
        "has_cover": bool(row.get("has_cover")) or bool(row.get("cover_url")),
        "cover_url": (f"/api/dashboard/{row['slug']}/cover"
                      if row.get("has_cover") else (row.get("cover_url") or "")),
        "cover_w": row.get("cover_w"),
        "cover_h": row.get("cover_h"),
        # ⚠️ Ownership, never role. An operator does NOT get manage on somebody else's
        # row, exactly as the workspace wall does not advertise actions the viewer lacks.
        "can_manage": bool(viewer_email and (row.get("owner_email") or "") == viewer_email),
        "shared_with_me": bool(shared_with_me or row.get("shared_with_me")),
        "size_bytes": _int(row.get("size_bytes")),
        "views": _int(row.get("views")),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


# ── permissions ──────────────────────────────────────────────────────────────

def require_owner(row: dict | None, email: str) -> dict:
    """The row, or a 404-shaped error for anyone who is not its owner.

    404 rather than 403 on purpose: a share management endpoint that answers 403 for
    "exists but not yours" and 404 for "no such slug" is a slug oracle.
    """
    if not row or (row.get("owner_email") or "") != (email or "").strip().lower():
        raise DashboardNotFound("仪表盘不存在 / dashboard not found")
    return row


def require_readable(row: dict | None, email: str) -> dict:
    """The row, or a 404-shaped error when this caller may not read it."""
    if not row or not may_read(row, email):
        raise DashboardNotFound("仪表盘不存在 / dashboard not found")
    return row


def may_read(row: dict, email: str) -> bool:
    """Owner → public-and-published → a private row shared with my address."""
    address = (email or "").strip().lower()
    if not row or not address:
        return False
    if (row.get("owner_email") or "") == address:
        return True
    if row.get("visibility") == "public" and row.get("status") == "published":
        return True
    if row.get("visibility") != "private" or row.get("status") != "published":
        return False
    if "shared_with_me" in row:
        # The row came from a SELECT that already answered this exact question.
        return bool(row["shared_with_me"])
    return _share_exists(row.get("id"), address)


def _share_exists(dashboard_id: Any, email: str) -> bool:
    if not dashboard_id or not email:
        return False
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {SHARES_TABLE} "
                        "WHERE dashboard_id = %s AND recipient_email = %s",
                        (dashboard_id, email))
            return cur.fetchone() is not None


# ── read ─────────────────────────────────────────────────────────────────────

def list_dashboards(*, email: str, scope: str = "mine", q: str = "",
                    status: str = "published", limit: int = 200) -> list[dict]:
    """Dashboard rows for one scope. Cards only — `html` is fetched on demand."""
    ensure_schema()
    where, params = scope_predicate(scope, email)
    want = _text(status).lower() or "published"
    if want != "all":
        where = f"{where} AND {TABLE}.status = %s"
        params = params + [clean_status(want)]
    if _text(q):
        # `summary_zh` is searched alongside `summary` because the two are the same one
        # line in two languages: a reader who searches the Chinese copy must find it.
        where += (f" AND ({TABLE}.title ILIKE %s OR {TABLE}.summary ILIKE %s"
                  f" OR {TABLE}.summary_zh ILIKE %s OR {TABLE}.slug ILIKE %s"
                  f" OR {TABLE}.tags ILIKE %s)")
        like = f"%{_text(q)}%"
        params += [like] * 5
    sql = (f"SELECT {_SELECT_COLS} FROM {TABLE} WHERE {where}"
           f" ORDER BY pinned DESC, updated_at DESC LIMIT %s")
    # The EXISTS in `_SELECT_COLS` consumes the first parameter, before the WHERE ones.
    params = [email] + params + [max(1, min(_int(limit, 200) or 200, MAX_LIST_LIMIT))]
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def get_dashboard(slug: str, *, email: str = "", include_html: bool = False) -> dict | None:
    """One row, or None. Visibility is the caller's business, not this function's.

    `email` fills the `shared_with_me` flag in the SELECT, so a reader gets the grant
    state without a second round trip.
    """
    ensure_schema()
    cols = _SELECT_COLS if include_html else _SELECT_COLS.replace(", html,", ",")
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {cols} FROM {TABLE} WHERE slug = %s",
                        ((email or "").strip().lower(), slug))
            row = cur.fetchone()
    return dict(row) if row else None


def set_cover(slug: str, cover: dict | None, *, clear: bool = False) -> bool:
    """Write (or clear) the cover columns for one dashboard.

    Deliberately **outside** `save_dashboard`'s `_STORED_FIELDS` merge. That merge is
    text fields with "a field you did not mention keeps its stored value"; cover bytes
    are a different thing, and folding them in would give `cover_*` a merged lifecycle
    they do not want (a PUT that changes the title must not be able to silently drop
    the picture). Reports store theirs the same separate way — see
    `routers/reports.py::_store_cover`.
    """
    ensure_schema()
    with _conn() as conn:
        with conn.cursor() as cur:
            if clear:
                cur.execute("UPDATE " + TABLE + " SET cover_data = NULL, cover_mime = '', "
                            "cover_w = NULL, cover_h = NULL, cover_prompt = '', "
                            "cover_model = '', cover_url = '' WHERE slug = %s", (slug,))
            elif cover and cover.get("data") is not None:
                cur.execute(
                    "UPDATE " + TABLE + " SET cover_data = %s, cover_mime = %s, cover_w = %s, "
                    "cover_h = %s, cover_prompt = %s, cover_model = %s, cover_url = '' "
                    "WHERE slug = %s",
                    (psycopg2.Binary(cover["data"]), cover.get("mime") or "image/jpeg",
                     cover.get("w"), cover.get("h"), cover.get("prompt") or "",
                     cover.get("model") or "", slug))
            elif cover and cover.get("url"):
                cur.execute(
                    "UPDATE " + TABLE + " SET cover_data = NULL, cover_mime = '', cover_w = NULL, "
                    "cover_h = NULL, cover_prompt = '', cover_model = '', cover_url = %s "
                    "WHERE slug = %s", (cover["url"], slug))
            else:
                return False
            return bool(cur.rowcount)
    return False


def get_cover(slug: str) -> dict | None:
    """The stored cover for one dashboard: bytes, or an external URL. `None` if neither."""
    ensure_schema()
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT cover_data, cover_mime, cover_w, cover_h, cover_url "
                        f"FROM {TABLE} WHERE slug = %s", (slug,))
            row = cur.fetchone()
    if not row:
        return None
    if row.get("cover_data") is not None:
        data = row["cover_data"]
        return {"data": bytes(data) if isinstance(data, memoryview) else data,
                "mime": row.get("cover_mime") or "image/jpeg",
                "w": row.get("cover_w"), "h": row.get("cover_h"), "url": ""}
    if row.get("cover_url"):
        return {"data": None, "mime": "", "w": None, "h": None, "url": row["cover_url"]}
    return None


def bump_views(slug: str, viewer_email: str) -> bool:
    """Count one honest view.

    A public page and the owner's own page count; a page somebody was *shared* does
    not. A share is a colleague looking at something on the owner's behalf, and folding
    that into the owner's readership would make the number say something false.
    """
    ensure_schema()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {TABLE} SET views = views + 1 "
                        "WHERE slug = %s AND (visibility = 'public' OR owner_email = %s)",
                        (slug, (viewer_email or "").strip().lower()))
            counted = bool(cur.rowcount)
    return counted


# ── write ────────────────────────────────────────────────────────────────────

_STORED_FIELDS = ("title, summary, summary_zh, description, tags, accent, datasets, html, "
                  "visibility, status, pinned, in_nav, nav_order, in_home, home_order, "
                  "home_collapsed, summary_en, langs, submitter")


def _merged(fields: dict, current: dict) -> dict:
    """Every field the caller did NOT send keeps its stored value.

    One rule for the whole model: the alternative silently resets whatever the caller
    did not mention, and the way that bites is exactly the way a re-publish of one card
    is worse than a no-op — an agent that fixes a typo in `title` would wipe the page.
    Clearing stays expressible: send the empty value.
    """
    def pick(name: str, default: Any = "") -> Any:
        return fields[name] if name in fields else current.get(name, default)
    return {
        "title": _text(pick("title"), limit=MAX_TITLE),
        "summary": _text(pick("summary"), limit=MAX_SUMMARY),
        "summary_zh": _text(pick("summary_zh"), limit=MAX_SUMMARY),
        "description": _text(pick("description"), limit=MAX_DESCRIPTION),
        "tags": _text(pick("tags"), limit=MAX_TAGS),
        "accent": validate_accent(pick("accent")),
        "datasets": datasets_json(parse_datasets(pick("datasets"))),
        "html": str(pick("html") or ""),
        "visibility": clean_visibility(pick("visibility", "private")),
        "status": clean_status(pick("status", "published")),
        "pinned": bool(pick("pinned", False)),
        "in_nav": bool(pick("in_nav", False)),
        "nav_order": _int(pick("nav_order", 0)),
        "in_home": bool(pick("in_home", False)),
        "home_order": _int(pick("home_order", 0)),
        "home_collapsed": bool(pick("home_collapsed", False)),
    }


def assert_publication_allowed(email: str, slug: str) -> None:
    allowed, reason = orgs.may_publish(email, orgs.CHANNEL_PUBLIC, orgs.TARGET_DASHBOARD, slug)
    if not allowed:
        status, message = orgs.denial(reason)
        raise DashboardPolicyError(message, status)


def save_dashboard(fields: dict, email: str, slug: str) -> dict:
    """Create the dashboard, or update the one already at this slug.

    A slug is a URL and is not editable: `slug` is the address, and the caller edits
    everything else under it. A slug that belongs to somebody else is a 409 — the one
    case where naming the conflict is the right answer, because the caller cannot get
    past it in any other way.
    """
    ensure_schema()
    owner = (email or "").strip().lower()
    if not owner:
        raise DashboardError("请先登录 / sign in first")
    supplied = {k: v for k, v in (fields or {}).items() if v is not None}
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT id, owner_email, {_STORED_FIELDS} FROM {TABLE} "
                        "WHERE slug = %s FOR UPDATE", (slug,))
            existing = cur.fetchone()
            if existing and (existing["owner_email"] or "") != owner:
                # ⚠️ A conflict is only *named* to a caller who could already have
                # found this slug. For anybody else a 409 is a probing oracle —
                # "is there a dashboard at this slug?", answered for a page they may
                # not read, which both the GET and the standalone reader refuse to do.
                # Same reasoning as `require_readable`: 404, never 403/409.
                if not may_read(existing, owner):
                    raise DashboardNotFound("仪表盘不存在 / dashboard not found")
                raise DashboardConflict(
                    f"slug '{slug}' 已被另一个账号占用，请换一个"
                    f" / slug '{slug}' belongs to another account; choose a new one")
            current = dict(existing) if existing else {}
            if not current:
                # Creating: the two things a page cannot do without.
                if not _text(supplied.get("title")):
                    raise DashboardError("缺少标题 / title is required")
                if not str(supplied.get("html") or "").strip():
                    raise DashboardError(
                        "html 必须是非空 HTML 文档 / html must be a non-empty HTML document")
            values = _merged(supplied, current)
            if values["visibility"] == "public":
                assert_publication_allowed(owner, slug)
            size = len(values["html"].encode("utf-8"))
            if size > MAX_HTML_BYTES:
                raise DashboardTooLarge(
                    f"html 为 {size} 字节，上限 {MAX_HTML_BYTES} 字节（8 MB）"
                    f" / html is {size} bytes; the limit is {MAX_HTML_BYTES} bytes (8 MB)")
            cur.execute(f"""
                INSERT INTO {TABLE} (slug, {_STORED_FIELDS}, owner_email, size_bytes)
                VALUES (%(slug)s, %(title)s, %(summary)s, %(summary_zh)s, %(description)s,
                        %(tags)s, %(accent)s, %(datasets)s, %(html)s, %(visibility)s,
                        %(status)s, %(pinned)s, %(in_nav)s, %(nav_order)s, %(in_home)s,
                        %(home_order)s, %(home_collapsed)s, %(owner_email)s, %(size_bytes)s)
                ON CONFLICT (slug) DO UPDATE SET
                    title = EXCLUDED.title, summary = EXCLUDED.summary,
                    summary_zh = EXCLUDED.summary_zh, description = EXCLUDED.description,
                    tags = EXCLUDED.tags, accent = EXCLUDED.accent,
                    datasets = EXCLUDED.datasets, html = EXCLUDED.html,
                    visibility = EXCLUDED.visibility, status = EXCLUDED.status,
                    pinned = EXCLUDED.pinned, in_nav = EXCLUDED.in_nav,
                    nav_order = EXCLUDED.nav_order, in_home = EXCLUDED.in_home,
                    home_order = EXCLUDED.home_order,
                    home_collapsed = EXCLUDED.home_collapsed,
                    size_bytes = EXCLUDED.size_bytes, updated_at = NOW()
                RETURNING id, created_at, updated_at
            """, {**values, "slug": slug, "owner_email": owner, "size_bytes": size})
            saved = dict(cur.fetchone() or {})
    saved["slug"] = slug
    return saved


def delete_dashboard(slug: str, email: str) -> bool:
    """Delete one dashboard **and its shares**.

    Two mechanisms, deliberately: `ON DELETE CASCADE` is the database's promise, and
    the explicit DELETE is this application's. Relying on the constraint alone means a
    dashboard deleted through any other path (a maintenance script, a future bulk
    endpoint) behaves differently from one deleted through the API, and the difference
    would be a share row pointing at an id nothing owns.
    """
    ensure_schema()
    owner = (email or "").strip().lower()
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT id, owner_email FROM {TABLE} WHERE slug = %s FOR UPDATE",
                        (slug,))
            row = cur.fetchone()
            if not row or (row["owner_email"] or "") != owner:
                raise DashboardNotFound("仪表盘不存在 / dashboard not found")
            cur.execute(f"DELETE FROM {SHARES_TABLE} WHERE dashboard_id = %s", (row["id"],))
            cur.execute(f"DELETE FROM {TABLE} WHERE slug = %s AND owner_email = %s",
                        (slug, owner))
            deleted = cur.rowcount
    if not deleted:
        raise DashboardNotFound("仪表盘不存在 / dashboard not found")
    return True


# ── reader-owned state: annotations and filter views (2026-10-05) ────────────
#
# Both of these moved here with the HTML. They are deliberately NOT folded into
# `save_dashboard`'s merged-field PUT: a reader saving an annotation is not a
# re-publication, so neither may touch `updated_at` and reorder the card wall, exactly
# as `routers/reports.py` keeps `state_json` out of its publish path.


def get_state(slug: str, *, email: str = "") -> str:
    """The stored annotation document of one dashboard, verbatim. `""` when none.

    ⚠️ Read permission is the CALLER's decision, not this function's — it is handed
    whichever page the caller was allowed to read and returns that page's document. It
    is told what to do by `require_readable`; refusing here would duplicate a rule the
    share/publication model already owns."""
    ensure_schema()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT state_json FROM {TABLE} WHERE slug = %s", (slug,))
            row = cur.fetchone()
    return (row[0] if row and row[0] else "") or ""


def save_state(slug: str, text: str, email: str) -> str:
    """Overwrite one dashboard's annotation document. Returns the new timestamp.

    ⚠️ Owner + private, and `updated_at` is NOT touched: the card wall sorts by
    `updated_at DESC`, so an annotation would otherwise push a page to the top of
    everyone's wall every time somebody coloured a cell."""
    ensure_schema()
    owner = (email or "").strip().lower()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TABLE} SET state_json = %s, state_updated_at = NOW() "
                "WHERE slug = %s AND owner_email = %s AND visibility = 'private' "
                "RETURNING state_updated_at", (text, slug, owner))
            row = cur.fetchone()
    if not row:
        raise DashboardNotFound("仪表盘不存在 / dashboard not found")
    return row[0].isoformat() if row[0] else ""


def set_filters(slug: str, stored: str, kind: str, email: str) -> dict:
    """Write the author's filter schema and the kind it implies. Owner only.

    `kind` is passed in rather than re-derived here, because the derivation needs the
    document (`routers/dashboard.py::_dashboard_kind` owns it) and this module is the
    one place that should not know how a document is parsed."""
    ensure_schema()
    owner = (email or "").strip().lower()
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT id, owner_email FROM {TABLE} WHERE slug = %s FOR UPDATE",
                        (slug,))
            row = cur.fetchone()
            if not row or (row["owner_email"] or "") != owner:
                raise DashboardNotFound("仪表盘不存在 / dashboard not found")
            cur.execute(f"UPDATE {TABLE} SET filters_json = %s, kind = %s WHERE id = %s",
                        (stored, kind, row["id"]))
            return {"id": row["id"], "kind": kind}


def saved_selection(dashboard_id: Any, email: str) -> tuple[dict, str | None]:
    """This reader's own filter picks for one dashboard (`{}` when they never used the
    rail). Delegates the JSON shape to the shared reader, so the two features cannot
    disagree about what a missing or corrupt row means."""
    from services import doc_state

    ensure_schema()
    with _conn() as conn:
        with conn.cursor() as cur:
            return doc_state.saved_selection(cur, "ai_dashboard_filter_selections",
                                             dashboard_id, email)


def save_selection(dashboard_id: Any, email: str, selection: dict) -> str:
    """Save this reader's picks. Returns the new timestamp."""
    from services import doc_state

    ensure_schema()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {SELECTIONS_TABLE}
                        (dashboard_id, user_email, selection_json, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (dashboard_id, user_email)
                    DO UPDATE SET selection_json = EXCLUDED.selection_json,
                                  updated_at = NOW()
                    RETURNING updated_at""",
                (dashboard_id, email, json.dumps(selection, ensure_ascii=False)))
            row = cur.fetchone()
    return row[0].isoformat() if row and row[0] else ""


# ── shares ───────────────────────────────────────────────────────────────────

def _own_dashboard(cur, slug: str, email: str) -> dict:
    """The row behind a slug, if the caller owns it. 404-shaped otherwise — every share
    endpoint goes through here, so none of them can leak the existence of a slug."""
    cur.execute(f"SELECT id, owner_email FROM {TABLE} WHERE slug = %s", (slug,))
    return require_owner(cur.fetchone(), email)


def list_shares(slug: str, email: str) -> list[dict]:
    """Everyone this dashboard is shared with. Owner only — 404 for anyone else."""
    ensure_schema()
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            row = _own_dashboard(cur, slug, email)
            cur.execute(f"SELECT recipient_email, shared_by, created_at FROM {SHARES_TABLE} "
                        "WHERE dashboard_id = %s ORDER BY created_at, recipient_email",
                        (row["id"],))
            out = []
            for share_row in cur.fetchall():
                item = dict(share_row)
                item["created_at"] = (item["created_at"].isoformat()
                                      if item.get("created_at") else None)
                out.append(item)
    return out


def share_dashboard(slug: str, recipient: str, email: str) -> dict:
    """Grant one colleague read access. Idempotent; returns the share row."""
    ensure_schema()
    owner = (email or "").strip().lower()
    # ⚠️ The organization gate replaces the copy of `is_allowed_recipient` that used to
    # sit in this file, character for character identical to the one in
    # `routers/reports.py`. Two copies of one policy is two answers waiting to diverge;
    # the single-domain check they performed was also not the question being asked.
    allowed, reason, _offenders = orgs.may_share_to_many(owner, [recipient])
    if not allowed:
        status, message = orgs.denial(reason)
        raise DashboardPolicyError(message, status)
    address = _text(recipient).lower()
    if address == owner:
        raise DashboardError(
            "所有者本来就有访问权限 / the owner already has access")
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            row = _own_dashboard(cur, slug, owner)
            cur.execute(f"""
                INSERT INTO {SHARES_TABLE} (dashboard_id, recipient_email, shared_by)
                VALUES (%s, %s, %s)
                ON CONFLICT (dashboard_id, recipient_email)
                DO UPDATE SET shared_by = EXCLUDED.shared_by
                RETURNING recipient_email, shared_by, created_at
            """, (row["id"], address, owner))
            saved = dict(cur.fetchone() or {"recipient_email": address,
                                            "shared_by": owner, "created_at": None})
    saved["created_at"] = saved["created_at"].isoformat() if saved.get("created_at") else None
    return saved


def revoke_share(slug: str, recipient: str, email: str) -> bool:
    """Remove one grant. Owner only — 404 for anyone else."""
    ensure_schema()
    address = _text(recipient).lower()
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            row = _own_dashboard(cur, slug, email)
            cur.execute(f"DELETE FROM {SHARES_TABLE} "
                        "WHERE dashboard_id = %s AND recipient_email = %s",
                        (row["id"], address))
            removed = bool(cur.rowcount)
    return removed
