"""Organizations: the tenant that enterprise rules hang off.

Why this exists
---------------
Klado had two flat roles — `user` and `admin` — and no concept of a group. That works
until somebody says "our documents stay inside the company", because then the boundary
is a property of a *set of people*, and a role column cannot express it. This module
adds that set.

Three things are deliberately **not** in here, because each of them is a trap:

**No `org_id` column on `app_users`.** Membership lives in its own table. A nullable
`org_id` on the account row looks simpler and is not: every single query in the product
would grow an `OR org_id IS NULL` branch, and the first one somebody forgets to write
is a data leak. Membership as a row also leaves room for an invitation that exists before
the person has an account, which is the normal order of events here.

**A person belongs to at most one organization** (`org_members_one_org_per_user`). Not a
preference — a consequence of "an invited address lands in the inviting company". If
somebody could be in two, then "share to my company" has two answers and the share gate
becomes a query with an `IN`, and a bug in it publishes across a boundary.

**Personal users get a one-person organization** rather than a NULL. Same reason: one
code path. `kind='personal'` is what makes them visible as a separate population in the
console without a second query shape.

What this module does *not* do
------------------------------
It does not decide who may see whose content. Enterprise admins manage *accounts* —
who is in the company, what modules they have, what they may share outward — and
deliberately cannot read the company's documents (that was an explicit product decision).
So none of the content queries here change, and there is deliberately **no read-scope
plumbing anywhere**: it would be scaffolding sprinkled through the product before
anything needs it. A future read-scope feature belongs here, in one place, not spread
across the content queries.

The share gate itself lives below, in `may_share_to()` / `may_publish()`.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Iterable, Optional

import psycopg2.extras

from klado_shared.db import connect_main

# ── Vocabulary ───────────────────────────────────────────────────────────────

KIND_PERSONAL = "personal"
KIND_ENTERPRISE = "enterprise"
KINDS = (KIND_PERSONAL, KIND_ENTERPRISE)

#: How far a member's **targeted shares** (colleague / dataset / knowledge / calendar)
#: may reach. Distinct from `PUBLIC_SCOPES` below on purpose — see the note there.
SCOPE_INTERNAL = "internal"     # 同企业成员
SCOPE_EXTERNAL = "external"     # 任意合法邮箱
SCOPE_GLOBAL = "global"         # 与个人用户、超管同权
SHARE_SCOPES = (SCOPE_INTERNAL, SCOPE_EXTERNAL, SCOPE_GLOBAL)

#: How far **outward-facing** publication may reach: an anyone-link (no login at all)
#: or the Workspace public area.
PUBLIC_FORBID = "forbid"        # 禁止
PUBLIC_APPROVE = "approve"      # 需企业管理员逐份批准
PUBLIC_ALLOW = "allow"         # 不限
PUBLIC_SCOPES = (PUBLIC_FORBID, PUBLIC_APPROVE, PUBLIC_ALLOW)

#: The three settings the product talks about, as one-click presets. Stored as two
#: columns because the middle state is real and a single enum cannot express it:
#: "we will happily share with a client's address, but nothing gets published".
#: Row 3 is a company pre-approving itself, which is why `allow` skips the queue.
SCOPE_PRESETS = {
    "internal": (SCOPE_INTERNAL, PUBLIC_APPROVE),    # 默认
    "external": (SCOPE_EXTERNAL, PUBLIC_APPROVE),
    "global": (SCOPE_GLOBAL, PUBLIC_ALLOW),
}

ORG_ROLE_OWNER = "owner"
ORG_ROLE_ADMIN = "admin"
ORG_ROLE_MEMBER = "member"
ORG_ROLES = (ORG_ROLE_OWNER, ORG_ROLE_ADMIN, ORG_ROLE_MEMBER)
#: Roles that may administer the organization. `owner` is not a synonym for `admin`
#: here: the console lists them separately so an account can be transferred away
#: without a second "is this the last one?" check.
ADMIN_ORG_ROLES = (ORG_ROLE_OWNER, ORG_ROLE_ADMIN)

STATUS_ACTIVE = "active"
STATUS_INVITED = "invited"
STATUS_DISABLED = "disabled"
MEMBER_STATUSES = (STATUS_ACTIVE, STATUS_INVITED, STATUS_DISABLED)

ORG_ACTIVE = "active"
ORG_SUSPENDED = "suspended"
ORG_STATUSES = (ORG_ACTIVE, ORG_SUSPENDED)

#: A suffix is a DNS name: labels of alphanumerics and hyphens, dots between them.
#: Rejects "acme.com, other.com" being pasted into a single field, which would
#: otherwise become a suffix that matches nothing and silently lock every employee out.
_SUFFIX_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
#: Conservative catch-all for the recipient side. Same shape `routers/reports.py` uses.
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)

#: Slugs are used in URLs and in the console's own links, so they are restricted to
#: what survives a path segment without escaping. Chinese is allowed — a company may
#: well be named 某某科技 — but not a slash, a dot, or whitespace.
_SLUG_RE = re.compile(r"^[\w一-鿿][\w一-鿿.-]{0,62}$")

MAX_NAME = 120
MAX_SLUG = 63
MAX_DOMAINS = 20


# ── Validation (pure, so the tests need no database) ─────────────────────────

class OrgError(ValueError):
    """A rejected organization or membership change. `str(exc)` is a bilingual pair.

    Services raise these; routers render them with `pick(str(exc), lang)`. Keeping the
    message at the raise site is what stops a new endpoint from shipping an untranslated
    string — the router never has to invent one.
    """


def clean_email(value: str) -> str:
    """Normalised address, or "" when it is not a usable one.

    Returns "" rather than raising: "is this address shareable" is a predicate, and a
    caller combining it with other conditions should not have to catch.

    ⚠️ The `str()` coercion is what makes the contract total. This function is called on
    request bodies, on CSV cells and on anything a caller typed, so a non-string turns
    up eventually — and `(42).strip()` raising `AttributeError` from a *validator* turns
    a "this address is not shareable" answer into a 500 on a path that had a perfectly
    good answer available.
    """
    address = str(value or "").strip().lower()
    return address if EMAIL_RE.match(address) else ""


def clean_domain(value: str) -> str:
    """One normalised suffix, or "". Accepts a leading "@" and upper case."""
    text = str(value or "").strip().lower().lstrip("@").rstrip(".")
    return text if _SUFFIX_RE.match(text) else ""


def clean_domains(values: Iterable[str]) -> list[str]:
    """Normalise, de-duplicate, sort. Raises on an unusable entry.

    ⚠️ Rejects rather than drops. Silently discarding "acme.com, other.com" would leave
    an organization that only half matches, and the symptom — employees who "can't
    register" — reads like a mail problem, not a typo in an admin field.
    """
    out: list[str] = []
    for raw in values or ():
        for piece in re.split(r"[,\s;]+", (raw or "").strip()):
            if not piece:
                continue
            domain = clean_domain(piece)
            if not domain:
                raise OrgError(
                    f"「{piece}」不是一个有效的邮箱后缀 / 「{piece}」 is not a valid email "
                    "suffix — write it like acme.com, one per entry"
                )
            if domain not in out:
                out.append(domain)
    if len(out) > MAX_DOMAINS:
        raise OrgError(
            f"最多只能设置 {MAX_DOMAINS} 个邮箱后缀 / at most {MAX_DOMAINS} email suffixes "
            "are allowed"
        )
    return sorted(out)


def clean_slug(value: str) -> str:
    text = (value or "").strip().lower()
    if not _SLUG_RE.match(text):
        raise OrgError(
            "企业标识只能用字母、数字、汉字、点、横线和中划线 / an organization slug may "
            "contain letters, digits, Chinese characters, dots and hyphens"
        )
    return text[:MAX_SLUG]


def clean_name(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise OrgError("企业名称不能为空 / the organization name cannot be empty")
    return text[:MAX_NAME]


def _one_of(value: str, allowed: tuple[str, ...], what: str, pair: str) -> str:
    text = (value or "").strip().lower()
    if text not in allowed:
        raise OrgError(f"{pair}（可选：{', '.join(allowed)}） / {what} must be one of: "
                       f"{', '.join(allowed)}")
    return text


# ── Schema ───────────────────────────────────────────────────────────────────
#
# ⚠️ `IF NOT EXISTS` on every statement, and this is the only place these tables are
# declared. `account_lifecycle` deliberately does NOT list them: an organization is not
# owned by any one person, so a closing account must not cascade it away. The membership
# rows go (they reference `app_users` with ON DELETE CASCADE), the org survives with
# whatever members remain — which is what an operator closing one leaver expects.
DDL = """
CREATE TABLE IF NOT EXISTS orgs (
    id            SERIAL PRIMARY KEY,
    slug          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'enterprise'
                  CHECK (kind IN ('personal', 'enterprise')),
    share_scope   TEXT NOT NULL DEFAULT 'internal'
                  CHECK (share_scope IN ('internal', 'external', 'global')),
    public_scope  TEXT NOT NULL DEFAULT 'approve'
                  CHECK (public_scope IN ('forbid', 'approve', 'allow')),
    email_domains TEXT[] NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'suspended')),
    -- ⚠️ The modules THIS COMPANY is licensed to offer, written by the platform operator
    -- in the console and read by the company's own administrators when they decide which
    -- modules each member gets. NULL means "no company-specific restriction, follow the
    -- deployment default" — which is what every row on an existing install has, and what
    -- `modules.org_offer(None)` means. `'{}'` is NOT the same and is deliberately
    -- distinguishable from it: it is "this company is licensed for no switchable module".
    -- A nullable column is the only way to say both, and a boolean beside the list would
    -- be a second fact that can disagree with it.
    --
    -- A non-empty list is a CEILING, never a grant: `modules.resolve()` intersects it with
    -- the deployment allowlist, so a company can never end up with more than the
    -- installation has, and an operator can never widen a company by accident.
    module_keys   TEXT[],
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by    TEXT NOT NULL DEFAULT ''
);

-- ⚠️ `CREATE TABLE IF NOT EXISTS` leaves an EXISTING table's columns alone, so a column
-- added to the CREATE above is missing at runtime — not at startup — on every install
-- that predates it. The symptom is a company with modules silently un-licensable and a
-- console dropdown that saves nothing. Stated here for the same reason the
-- `org_public_approvals` CHECK below is restated as an ALTER.
--
-- ⚠️ No DEFAULT, and that is the point: `DEFAULT '{}'` would make "never configured" and
-- "configured to nothing" the same stored value, and the console would then be unable
-- to express the second one. Nullable is what carries the distinction.
ALTER TABLE orgs
    ADD COLUMN IF NOT EXISTS module_keys TEXT[];

CREATE TABLE IF NOT EXISTS org_members (
    id          SERIAL PRIMARY KEY,
    org_id      INT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    -- NULL while an invitation is outstanding: the person may not have an account yet,
    -- which is the normal order of events (invite, then register).
    user_id     INT REFERENCES app_users(id) ON DELETE CASCADE,
    email       TEXT NOT NULL,
    org_role    TEXT NOT NULL DEFAULT 'member'
                CHECK (org_role IN ('owner', 'admin', 'member')),
    status      TEXT NOT NULL DEFAULT 'invited'
                CHECK (status IN ('active', 'invited', 'disabled')),
    invited_by  TEXT NOT NULL DEFAULT '',
    joined_at   TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, email)
);

-- ⚠️ The two indexes below are doing different jobs, and neither substitutes for the
-- other. `org_members_email_idx` is what `register/complete` uses to find the pending
-- invitation for an address. `org_members_one_org_per_user` is the invariant "a person
-- is in at most one organization" — but it CANNOT catch a double invitation, because
-- both rows have `user_id IS NULL` and PostgreSQL treats NULLs as distinct in a unique
-- index. That check is application-level; see `conflicting_invitations()`.
CREATE INDEX IF NOT EXISTS org_members_user_idx
    ON org_members (user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS org_members_email_idx
    ON org_members (lower(email));
CREATE UNIQUE INDEX IF NOT EXISTS org_members_one_org_per_user
    ON org_members (user_id) WHERE user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS org_public_approvals (
    id              SERIAL PRIMARY KEY,
    org_id          INT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    -- ⚠️ `target_*` rather than a `report_id`: three of the families publish outward
    -- (reports, knowledge items, calendar events) and all three key on a slug, not an
    -- integer. A `report_id` column would have left knowledge and calendar approvals
    -- with nowhere to point, and the obvious workaround — one nullable column per
    -- family — is a schema that grows every time somebody adds a fourth.
    -- `target_id` is deliberately NOT a foreign key to any main-app table: see below.
    target_type     TEXT NOT NULL
                    CHECK (target_type IN ('report', 'knowledge', 'calendar', 'file', 'dashboard')),
    target_id       TEXT NOT NULL,
    requester_email TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('anyone_link', 'public')),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'denied', 'revoked')),
    decided_by      TEXT NOT NULL DEFAULT '',
    decided_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- One live request per (document, channel, person). ⚠️ This unique index is what makes
-- `ON CONFLICT DO NOTHING` in `request_public_approval()` actually conflict — without
-- it every click of "request approval" appended another pending row, and the admin
-- queue filled with duplicates of the same document.
CREATE UNIQUE INDEX IF NOT EXISTS org_public_approvals_one_request
    ON org_public_approvals (org_id, target_type, target_id, kind, lower(requester_email));
CREATE INDEX IF NOT EXISTS org_public_approvals_pending_idx
    ON org_public_approvals (status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS org_public_approvals_target_idx
    ON org_public_approvals (target_type, target_id);

-- ⚠️ The CHECK above is not enough on its own: `CREATE TABLE IF NOT EXISTS` leaves an
-- existing table's constraints alone, so widening the vocabulary has to be restated as
-- an ALTER. The DROP is what makes the pair idempotent — `ADD CONSTRAINT` on a name
-- that already exists raises, and `ensure_schema()` runs on every start of both
-- processes. Both statements are in the caller's single transaction, so the table is
-- never observable without the constraint.
ALTER TABLE org_public_approvals
    DROP CONSTRAINT IF EXISTS org_public_approvals_target_type_check;
ALTER TABLE org_public_approvals
    ADD CONSTRAINT org_public_approvals_target_type_check
    CHECK (target_type IN ('report', 'knowledge', 'calendar', 'file', 'dashboard'));

-- ⚠️ `target_id` has NO foreign key, deliberately. `ai_reports` and friends are created
-- by `api/`'s own bootstrap, and a foreign key from the shared layer into the main
-- app's schema would make the console depend on that schema existing — exactly the
-- coupling `klado_shared/` exists to avoid. A purge therefore leaves an approval row
-- pointing at a document that is gone; `prune_orphan_approvals()` clears those.
"""


def prune_orphan_approvals() -> int:
    """Delete approvals whose document no longer exists. Returns how many went.

    Called at startup rather than by a foreign key, for the reason in the DDL comment.
    Each family is checked with its own table, and each existence test is guarded —
    on a fresh install the main app's tables may not exist yet, and the console must
    not die on a table it does not own.
    """
    removed = 0
    checks = (("report", "ai_reports", "slug"),
              ("knowledge", "ai_knowledge_items", "slug"),
              ("calendar", "ai_calendar_events", "slug"),
              ("dashboard", "ai_dashboards", "slug"))
    with _db() as conn:
        with conn.cursor() as cur:
            for target_type, table, column in checks:
                cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                if not cur.fetchone()[0]:
                    continue
                cur.execute(
                    f"DELETE FROM org_public_approvals a WHERE a.target_type = %s "
                    f"AND NOT EXISTS (SELECT 1 FROM {table} t WHERE t.{column} = a.target_id)",
                    (target_type,))
                removed += cur.rowcount
    return removed


@contextmanager
def _db():
    conn = connect_main()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema() -> None:
    """Create the tables. Idempotent — safe on every process start, on both ports."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(_retire_pre_release_approval_table())
            cur.execute(DDL)


def _retire_pre_release_approval_table() -> str:
    """SQL that drops the first (never-released) shape of `org_public_approvals`.

    ⚠️ This table was introduced and reshaped within the same unreleased change: it
    first carried a `report_id INT`, which could not describe a knowledge item or a
    calendar event. `CREATE TABLE IF NOT EXISTS` would happily leave the old shape in
    place, and the new columns would then be missing at runtime rather than at
    startup. So the old shape is detected and dropped — but ONLY when it is both
    recognisably old and empty, which makes this safe to run against an installation
    that somehow has data in it: the table is left alone rather than dropped.

    Returns one statement. Deliberately a no-op once `target_type` exists, so this
    stops costing anything a few releases from now.

    ⚠️ The `IF to_regclass(...) IS NOT NULL THEN` **wrapper** is load-bearing on a
    fresh install, and the obvious rewrite is wrong. Putting
    `to_regclass('org_public_approvals') IS NOT NULL AND … NOT EXISTS (SELECT 1 FROM
    org_public_approvals)` into one flat condition does NOT work: PostgreSQL parses
    the whole `IF` expression as a single statement, so the subquery naming the
    table is resolved at *plan* time and raises `UndefinedTable` before short-circuit
    evaluation can skip it. Left-to-right evaluation order does not save it.
    Splitting it into a nested `IF` does work, because PL/pgSQL only parses the inner
    statement once control flow actually reaches it. That failure took down the whole
    admin console (`Application startup failed. Exiting.`) on the first ever run — the
    one moment a fresh install cannot recover from by retrying.
    """
    return """
    DO $$
    BEGIN
        IF to_regclass('org_public_approvals') IS NOT NULL THEN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'org_public_approvals'
                         AND column_name = 'report_id')
               AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name = 'org_public_approvals'
                                 AND column_name = 'target_type')
               AND NOT EXISTS (SELECT 1 FROM org_public_approvals) THEN
                DROP TABLE org_public_approvals;
            END IF;
        END IF;
    END $$
    """


# ── Lookups ──────────────────────────────────────────────────────────────────

def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _one_row(cur) -> Optional[dict]:
    """The next row as a dict, or None.

    ⚠️ This helper exists because `cur.fetchone() and dict(cur.fetchone())` was written
    three times in this file and is wrong every time: the first call CONSUMES the row
    and the second returns None, so `dict(None)` raised TypeError. All three call sites
    were live 500s — `membership_of` is on every `/api/org/*` request, so the entire
    enterprise-administration surface was down in the running app while every unit test
    passed (they fake the store).

    psycopg2 has no "peek", so the row has to be bound before it is used. There is now
    exactly one place that does it, and `SingleFetchTests` asserts there is no second.
    """
    row = cur.fetchone()
    return dict(row) if row is not None else None


def get_org(org_id: int) -> Optional[dict]:
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM orgs WHERE id = %s", (org_id,))
            return _one_row(cur)


def get_org_by_slug(slug: str) -> Optional[dict]:
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM orgs WHERE lower(slug) = lower(%s)", ((slug or "").strip(),))
            return _one_row(cur)


def membership_of(email: str) -> Optional[dict]:
    """The `org_members` row for an address, if any.

    ⚠️ Matches on `email`, not `user_id`, and deliberately so: a person is identified
    by their address everywhere in this product (every content table carries
    `owner_email`), so this is the join key the rest of the code already has. Resolving
    `user_id → email` first would need the account to exist, which it does not while an
    invitation is outstanding.

    Returns a row even when `status='disabled'` — callers that care must check, because
    "which company is this address in" and "may this address act" are different
    questions and collapsing them is how a disabled member keeps their reach.
    """
    address = clean_email(email)
    if not address:
        return None
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT m.*, o.kind, o.slug, o.name, o.status AS org_status, "
                        "       o.share_scope, o.public_scope, o.email_domains, "
                        "       o.module_keys "
                        "FROM org_members m JOIN orgs o ON o.id = m.org_id "
                        "WHERE lower(m.email) = %s", (address,))
            # ⚠️ `fetchone()` ONCE, into a local. This read `cur.fetchone() and
            # dict(cur.fetchone())`, which consumes the row on the first call and hands
            # the second one `None` — so `dict(None)` raised TypeError and every caller
            # got a 500. It reads like an idiom and is not one; psycopg2 has no "peek",
            # so the value has to be bound before it is used. See `_one_row`.
            return _one_row(cur)


def org_of(email: str) -> Optional[dict]:
    """The organization an address belongs to, or None (an operator, or unknown)."""
    return membership_of(email)


def module_licence_of(email: str) -> Optional[tuple[str, ...]]:
    """The module licence of the company `email` belongs to, or None for "no ceiling".

    ⚠️ `None` and `()` are DIFFERENT answers and both are real. `None` is "this company
    has never been licensed" — follow the deployment default, which is what every company
    on an existing install is. `()` is "the operator unticked every box" — the company may
    not offer a single switchable module. Collapsing them is the one mistake this column
    must not make: `if licence:` in either caller then reads "no restriction", so an
    operator who deliberately closed a company down would be handing its administrators a
    full set of checkboxes that all fail on save.

    A non-member also answers None, and for the same reason: there is no company, so
    there is no company-specific restriction.
    """
    row = membership_of(email)
    if not row or row.get("module_keys") is None:
        return None
    return tuple(str(k).strip().lower() for k in (row.get("module_keys") or ()) if k)


def orgs_for_domains(domains: Iterable[str]) -> list[dict]:
    """Every ACTIVE organization whose suffix list contains one of `domains`.

    ⚠️ Unnest rather than loop with a query per domain: registration calls this on every
    request, and a company with three suffixes should not cost three round trips. The
    `status = 'active'` filter is what makes a suspended company's suffix stop
    admitting new people while its existing members keep working.
    """
    values = [clean_domain(d) for d in (domains or ())]
    values = [d for d in values if d]
    if not values:
        return []
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT DISTINCT o.* FROM orgs o "
                "WHERE o.status = 'active' AND o.email_domains && %s "
                "ORDER BY o.id",
                (values,))
            return _rows(cur)


def matching_org_for_email(email: str) -> Optional[dict]:
    """The single active organization whose suffixes cover this address, else None.

    ⚠️ Returns None when the address matches MORE THAN ONE organization, and the caller
    has to treat that as an error rather than as "no match". This is the one place a
    genuine ambiguity can arise: two companies can both register `acme.com`, and the
    unique index that would normally prevent it is on `orgs.slug`, not on
    `email_domains` — a domain is shared infrastructure, and refusing to let two
    employers of the same company have separate tenants is a product decision, not a
    data-integrity one. Picking the lowest `id` here would silently sign somebody into
    the wrong company.
    """
    address = clean_email(email)
    if "@" not in address:
        return None
    found = orgs_for_domains([address.split("@", 1)[1]])
    return found[0] if len(found) == 1 else None


def conflicting_orgs_for_email(email: str) -> list[dict]:
    """Organizations that claim this address — zero, one, or several."""
    address = clean_email(email)
    if "@" not in address:
        return []
    return orgs_for_domains([address.split("@", 1)[1]])


def pending_invitations_for(email: str) -> list[dict]:
    """Outstanding invitations for an address, across ALL organizations.

    ⚠️ This is the application-level half of the "one person, one organization" rule.
    `org_members_one_org_per_user` cannot catch a double invitation: both rows have
    `user_id IS NULL`, and PostgreSQL treats NULLs as distinct in a unique index. So
    two companies can each hold a pending invitation for the same address, and only a
    query like this one notices. Registration calls it and refuses the address when the
    answer is "more than one" — see `register/complete`.
    """
    address = clean_email(email)
    if not address:
        return []
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT m.*, o.name AS org_name, o.slug AS org_slug, o.kind "
                        "FROM org_members m JOIN orgs o ON o.id = m.org_id "
                        "WHERE lower(m.email) = %s AND m.status = %s "
                        "ORDER BY o.id",
                        (address, STATUS_INVITED))
            return _rows(cur)


def members_of(org_id: int) -> list[dict]:
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT m.*, "
                # Derived, never stored: a company can narrow its suffix list at any
                # time, and nobody's membership may be revoked as a side effect. An
                # existing account whose address no longer matches is flagged so the
                # administrator can decide — see the design note on suffix changes.
                "EXISTS (SELECT 1 FROM unnest(o.email_domains) d "
                "        WHERE lower(split_part(m.email, '@', 2)) = d) AS domain_ok, "
                "u.role AS account_role, u.disabled, u.deleted_at "
                "FROM org_members m JOIN orgs o ON o.id = m.org_id "
                "LEFT JOIN app_users u ON u.id = m.user_id "
                "WHERE m.org_id = %s ORDER BY "
                "  CASE m.org_role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, "
                "  lower(m.email)",
                (org_id,))
            return _rows(cur)


def is_org_admin(email: str) -> bool:
    """Does this address administer its organization?

    ⚠️ `status='active'` on BOTH the member row and the organization. A disabled member
    and a suspended company both have to lose this, and checking only the member row
    would leave a suspended company fully administrable — the exact state a suspension
    exists to prevent.
    """
    row = membership_of(email)
    return bool(
        row
        and row.get("org_role") in ADMIN_ORG_ROLES
        and row.get("status") == STATUS_ACTIVE
        and row.get("org_status") == ORG_ACTIVE
    )


def create_org(name: str, *, kind: str = KIND_ENTERPRISE, slug: str = "",
               domains: Iterable[str] = (), share_scope: str = SCOPE_INTERNAL,
               public_scope: str = PUBLIC_APPROVE, created_by: str = "") -> dict:
    """Create an organization. `slug` is derived from the name when not given.

    A generated slug is suffixed until it is free rather than failing, because the name
    is the thing the operator typed and two companies called "Marketing" is ordinary;
    making them invent a unique identifier by hand would be friction with no payoff.
    """
    clean_kind = _one_of(kind, KINDS, "kind", "企业类型无效 / invalid organization kind")
    clean_name_ = clean_name(name)
    base = clean_slug(slug) if slug else clean_slug(_ascii_fallback(clean_name_))
    suffixes = clean_domains(domains)
    scope = _one_of(share_scope, SHARE_SCOPES, "share scope", "分享范围无效 / invalid share scope")
    public = _one_of(public_scope, PUBLIC_SCOPES, "public scope", "公开范围无效 / invalid public scope")

    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            candidate, n = base, 1
            while True:
                cur.execute("SELECT 1 FROM orgs WHERE lower(slug) = lower(%s)", (candidate,))
                if not cur.fetchone():
                    break
                n += 1
                candidate = f"{base[:MAX_SLUG - 4]}-{n}"
            cur.execute(
                "INSERT INTO orgs (slug, name, kind, share_scope, public_scope, "
                "                  email_domains, status, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (candidate, clean_name_, clean_kind, scope, public, suffixes,
                 ORG_ACTIVE, (created_by or "").strip().lower()))
            return dict(cur.fetchone())


def _ascii_fallback(name: str) -> str:
    """A slug seed from a name that may have no ASCII in it.

    Keeps letters/digits/hyphens and turns everything else into a hyphen; a name written
    entirely in Chinese yields a seed of hyphens, which then gets a numeric suffix and
    remains unique. Readable slugs are a nicety here — the console shows the name.
    """
    kept = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "-")) else "-"
                   for ch in (name or ""))
    kept = "-".join(part for part in kept.split("-") if part)
    return kept or "org"


# ── The share gate ───────────────────────────────────────────────────────────
#
# ⚠️ This is the ONE place "may this person hand this to that person" is decided.
# Before this existed the question was answered in nine places, two of which had
# copied the same five-line function verbatim, and seven of which did not answer it at
# all. Every write path that grants or publishes something calls in here.

#: Why a share was refused. Codes, not sentences, so a router renders them with
#: `pick()` in one place and a test can assert on the code rather than on prose.
OK = ""
NOT_SAME_ORG = "not_same_org"
NEEDS_APPROVAL = "needs_approval"
PUBLIC_FORBIDDEN = "public_forbidden"
BAD_RECIPIENT = "bad_recipient"

#: The wording for each refusal, and the HTTP status it maps to.
#:
#: ⚠️ One table, nine call sites. When this lived as `raise HTTPException(...pick("…"))`
#: at each of the nine write paths, the same rule acquired nine phrasings and four
#: different status codes within a fortnight — and "your company's policy does not
#: allow this" arriving as 400 (looks like a typo in a form) in one module and 403 in
#: the next is the kind of inconsistency nobody notices until somebody writes support
#: instructions from it.
#:
#: Each string is a `中文 / English` pair, which is the repository's translation memory:
#: the router passes it through `pick(..., lang)` and a browser gets one language while
#: a machine caller gets both.
DENIALS: dict[str, tuple[int, str]] = {
    NOT_SAME_ORG: (403,
                   "只能分享给本企业的成员 / you can only share with members of your own "
                   "organization — your administrator can widen this in the enterprise "
                   "settings"),
    NEEDS_APPROVAL: (403,
                     "对外公开需要企业管理员批准 / publishing outside your organization "
                     "needs your administrator's approval — submit a request and it will "
                     "be reviewed"),
    PUBLIC_FORBIDDEN: (403,
                       "本企业不允许对外公开 / your organization does not allow "
                       "publishing outside it"),
    BAD_RECIPIENT: (400, "邮箱地址格式不正确 / that email address is not valid"),
}


def denial(reason: str) -> tuple[int, str]:
    """`(status_code, bilingual_message)` for a refusal code.

    ⚠️ An unknown code is a 500-shaped programming error surfacing as a normal refusal,
    so it raises rather than defaulting: a new `may_share_to` reason that nobody added
    wording for should fail loudly in the tests, not read as "share blocked" to a user
    with no explanation. The fallback tuple is the operator's 500 text.
    """
    if reason in DENIALS:
        return DENIALS[reason]
    raise OrgError(f"未知的拒绝理由 / unknown denial reason: {reason!r}")

#: Channels for `may_publish()`. The two are distinct because they are distinct
#: promises: an anyone-link is a bearer credential (the URL *is* the key, and
#: `ai_report_anyone_links` has no owner column to scope it by), while the public area
#: is a listing any signed-in user can browse.
CHANNEL_ANYONE_LINK = "anyone_link"
CHANNEL_PUBLIC = "public"
# A Data Center file's share_token: a bearer credential for an object in storage,
# the same kind of promise as an anyone-link and governed the same way.
CHANNEL_FILE_TOKEN = "file_token"
PUBLIC_CHANNELS = (CHANNEL_ANYONE_LINK, CHANNEL_PUBLIC, CHANNEL_FILE_TOKEN)

#: A personal user is in no company, so nothing restricts them. This is the value a
#: platform operator and an address with no organization also get, and it is
#: deliberately the permissive one: an installation that has just upgraded must keep
#: working, and a company that wants a boundary has to create one.
UNRESTRICTED = (SCOPE_GLOBAL, PUBLIC_ALLOW, None)


def share_profile(email: str) -> tuple[str, str, Optional[int]]:
    """`(share_scope, public_scope, org_id)` for an address.

    ⚠️ Returns the PERMISSIVE profile for an address with no organization, and that is
    a decision rather than a fallback. It covers two populations that must not be
    locked down: platform operators (who belong to no company, by design) and personal
    users (whose whole point is that nobody restricts them). It also means a *suspended*
    company reads as unrestricted — see `effective_profile` for why that is bounded.
    """
    row = membership_of(email)
    if not row:
        return UNRESTRICTED
    if row.get("org_status") != ORG_ACTIVE:
        return UNRESTRICTED
    return (row.get("share_scope") or SCOPE_INTERNAL,
            row.get("public_scope") or PUBLIC_APPROVE,
            row.get("org_id"))


def effective_profile(email: str) -> tuple[str, str, Optional[int]]:
    """`share_profile` for the person making the request, always the stricter answer.

    ⚠️ The rule is one sentence: **an address with no organization is unrestricted; an
    address in one is held to that organization's rules — no matter what state either is
    in.** The member's own `status` and the organization's `status` deliberately do not
    change the outcome.

    An earlier version returned the permissive profile for a disabled member and for a
    suspended company, on the reasoning that "they are not really in it any more". That
    is exactly backwards for a gate: a suspension is an administrative decision to stop
    the company reading as a company, and answering it with "unrestricted" hands its
    members a wider reach at the moment their administrator is trying to close the
    company down. Whether a disabled person may *act at all* is decided at sign-in
    (`auth_store.authenticate` and `main._local_identity`), not here; this function is
    only ever asked "given that you may act, how far may you reach".
    """
    row = membership_of(email)
    if not row:
        return UNRESTRICTED
    return (row.get("share_scope") or SCOPE_INTERNAL,
            row.get("public_scope") or PUBLIC_APPROVE,
            row.get("org_id"))


def _org_addresses(org_id: int) -> set[str]:
    """Every address that counts as "in this organization".

    Includes `status='invited'`, deliberately. Reports are shared with "future
    registrants" — the share row is created before the person has an account, and
    excluding invitations would mean a company's own new hire could not be sent
    anything until they had signed up and been told to go and ask for it again.
    Excludes `disabled`, which is an explicit operator decision about one person.
    """
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT lower(email) FROM org_members "
                        "WHERE org_id = %s AND status IN ('active', 'invited')", (org_id,))
            return {row[0] for row in cur.fetchall()}


def may_share_to_many(owner_email: str, recipients: Iterable[str]
                      ) -> tuple[bool, str, list[str]]:
    """`ok` for a batch. Returns `(all_allowed, reason_code, offending_addresses)`.

    ⚠️ One query for the whole batch, not one per recipient. The reports share dialog
    accepts twenty addresses, and the naive shape is twenty round trips before a single
    row is written — on a share button. The owner's profile is fetched once and the
    organization's whole roster once; the loop is then pure set membership.

    The reason code is the FIRST offence, and the offending list is complete. The UI
    needs both: the code to phrase the error, the list to mark the specific fields.
    """
    share_scope, _public_scope, org_id = effective_profile(owner_email)
    cleaned = [clean_email(r) for r in (recipients or ())]
    bad = [r for r, c in zip(recipients or (), cleaned) if not c]
    if bad:
        return False, BAD_RECIPIENT, bad
    if share_scope in (SCOPE_GLOBAL, SCOPE_EXTERNAL):
        return True, OK, []

    roster = _org_addresses(org_id)
    outside = [c for c in cleaned if c not in roster]
    if outside:
        return False, NOT_SAME_ORG, outside
    return True, OK, []


def may_share_to(owner_email: str, recipient_email: str) -> tuple[bool, str]:
    """Single-recipient form of `may_share_to_many`. See it for the reasoning."""
    allowed, reason, _offenders = may_share_to_many(owner_email, [recipient_email])
    return allowed, reason


#: The families that publish outward, and the value each stores in `target_type`.
#: Reports also have an anyone-link on top of the public area, which is a `kind`, not a
#: family — one report can be both listed publicly and handed out as a bearer URL.
TARGET_REPORT = "report"
TARGET_KNOWLEDGE = "knowledge"
TARGET_CALENDAR = "calendar"
# Data Center files. ⚠️ Added after the inventory found `publish_file` minting
# a bearer token (`share_token`) with no gate — and, as it turns out, with no reader
# either. Minting an ungated credential is still a hole: the moment somebody wires up
# the endpoint that serves it, it is a live share path with no policy behind it.
TARGET_FILE = "file"
TARGET_DASHBOARD = "dashboard"
TARGET_TYPES = (TARGET_REPORT, TARGET_KNOWLEDGE, TARGET_CALENDAR, TARGET_FILE, TARGET_DASHBOARD)


# ── Membership changes ───────────────────────────────────────────────────────
#
# ⚠️ Every function here takes `expected_org_id` and refuses when the row belongs to
# somebody else. That parameter is not authorisation — the caller already proved who it
# is; it is the assertion that the target row is inside the scope it was derived from.
# Without it, an enterprise admin who guessed a member id would be editing a row in
# another company, and the only thing standing in the way is knowing the number.


class MemberScopeError(OrgError):
    """The member is not in an organization this caller administers. Rendered as 404.

    404 rather than 403: "that member belongs to another company" is a fact about
    somebody else's company, and confirming it turns an id into a directory.
    """


def _assert_member_in_scope(cur, member_id: int, expected_org_id: Optional[int]) -> dict:
    cur.execute("SELECT * FROM org_members WHERE id = %s", (member_id,))
    row = cur.fetchone()
    if not row:
        raise MemberScopeError("成员不存在 / no such member")
    if expected_org_id is not None and row["org_id"] != expected_org_id:
        raise MemberScopeError("成员不存在 / no such member")
    return dict(row)


def upsert_invitation(org_id: int, email: str, role: str, invited_by: str = "") -> dict:
    """Create or refresh an invitation. Idempotent per (org, address).

    A re-invite by somebody else re-stamps `invited_by` and does NOT change the role of
    an existing ACTIVE member — re-inviting a colleague must not be able to demote them
    as a side effect, so the role is only written when the row is not yet accepted.
    """
    address = clean_email(email)
    if not address:
        raise OrgError("邮箱地址无效 / that email address is not valid")
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO org_members (org_id, email, org_role, status, invited_by) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (org_id, email) DO UPDATE "
                "  SET invited_by = EXCLUDED.invited_by, "
                "      org_role = CASE WHEN org_members.status = 'invited' "
                "                       THEN EXCLUDED.org_role "
                "                       ELSE org_members.org_role END "
                "RETURNING *",
                (org_id, address, role, STATUS_INVITED, (invited_by or "").strip().lower()))
            return dict(cur.fetchone())


def attach_existing_account(org_id: int, email: str) -> Optional[int]:
    """Bind an outstanding invitation to the account that already exists.

    Returns the user id, or None when there is no invitation or no account.

    ⚠️ The "one person, one organization" check lives here and nowhere else. Two
    companies can both hold a pending invitation for the same address, because both
    rows have `user_id IS NULL` and a unique index cannot see that. This is the one
    moment the duplicate becomes real, so this is where it is refused.
    """
    address = clean_email(email)
    if not address:
        return None
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM app_users WHERE lower(email) = %s", (address,))
            found = cur.fetchone()
            if not found:
                return None
            user_id = found[0]
            cur.execute("SELECT id FROM org_members WHERE org_id = %s AND lower(email) = %s",
                        (org_id, address))
            mine = cur.fetchone()
            if not mine:
                raise OrgError(
                    "该邮箱不在本企业成员名单中 / that address is not a member of this "
                    "organization")
            cur.execute(
                "SELECT o.name FROM org_members m JOIN orgs o ON o.id = m.org_id "
                "WHERE m.user_id = %s AND m.org_id <> %s", (user_id, org_id))
            elsewhere = cur.fetchall()
            if elsewhere:
                raise OrgError(
                    f"该邮箱已属于另一个企业（{elsewhere[0][0]}）/ that address already "
                    f"belongs to another organization ({elsewhere[0][0]})")
            cur.execute("UPDATE org_members SET user_id = %s, status = %s, joined_at = NOW() "
                        "WHERE id = %s", (user_id, STATUS_ACTIVE, mine[0]))
            return user_id


def _refuse_removing_last_admin(cur, row: dict) -> None:
    cur.execute("SELECT count(*) FROM org_members WHERE org_id = %s "
                "AND status = 'active' AND org_role IN ('owner', 'admin')", (row["org_id"],))
    if (cur.fetchone() or (0,))[0] <= 1:
        raise OrgError(
            "这是本企业最后一位管理员，不能降级 / this is the last administrator of the "
            "organization and cannot be demoted — promote somebody else first")


def set_member_role(member_id: int, role: str, expected_org_id: Optional[int] = None) -> dict:
    """Change a member's organization role.

    ⚠️ Refuses to remove the LAST administrator. The alternative is a company that can
    never be administered again, discovered by the next person to open the page.
    """
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            row = _assert_member_in_scope(cur, member_id, expected_org_id)
            if role not in ADMIN_ORG_ROLES and row["org_role"] in ADMIN_ORG_ROLES:
                _refuse_removing_last_admin(cur, row)
            cur.execute("UPDATE org_members SET org_role = %s WHERE id = %s RETURNING *",
                        (role, member_id))
            return dict(cur.fetchone())


def disable_member(member_id: int, expected_org_id: Optional[int] = None) -> dict:
    """Take a member out of the organization WITHOUT closing their account.

    The row is kept and marked `disabled` rather than deleted. Two reasons: the address
    stops being in the roster, which is what keeps shares to it from being offered, and
    a record survives of why a person is no longer listed. Their documents stay theirs.
    """
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            row = _assert_member_in_scope(cur, member_id, expected_org_id)
            if row["org_role"] in ADMIN_ORG_ROLES:
                _refuse_removing_last_admin(cur, row)
            cur.execute("UPDATE org_members SET status = %s WHERE id = %s RETURNING *",
                        (STATUS_DISABLED, member_id))
            return dict(cur.fetchone())


def set_member_module(member_id: int, module_key: str, enabled: bool,
                      expected_org_id: Optional[int] = None) -> dict:
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            row = _assert_member_in_scope(cur, member_id, expected_org_id)
            if not row.get("user_id"):
                raise OrgError(
                    "该成员还没有账号，等他接受邀请后再设置 / this member has no account "
                    "yet; set their modules after they accept the invitation")
            _require_account_modules()
            account_modules_set(row["user_id"], module_key, enabled)
            return _member_with_modules(cur, row["id"])


def clear_member_module(member_id: int, module_key: str,
                        expected_org_id: Optional[int] = None) -> dict:
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            row = _assert_member_in_scope(cur, member_id, expected_org_id)
            if not row.get("user_id"):
                raise OrgError(
                    "该成员还没有账号，等他接受邀请后再设置 / this member has no account "
                    "yet; set their modules after they accept the invitation")
            _require_account_modules()
            account_modules_clear(row["user_id"], module_key)
            return _member_with_modules(cur, row["id"])


def clear_member_modules(member_id: int,
                         expected_org_id: Optional[int] = None) -> int:
    """Drop EVERY module override for one member. Returns how many rows went away.

    ⚠️ The count is the return value rather than a detail nobody reads, because "0" is
    the interesting one: it means the member was already following the deployment
    default, and the operator asked for exactly that state. Reporting it as an error
    would make a correct button look broken.

    Same refusal as the per-key clear for a member who has not registered yet — there is
    no `user_id` to key an `account_modules` row on, so a silent success would be a lie.
    """
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            row = _assert_member_in_scope(cur, member_id, expected_org_id)
            if not row.get("user_id"):
                raise OrgError(
                    "该成员还没有账号，等他接受邀请后再设置 / this member has no account "
                    "yet; set their modules after they accept the invitation")
            _require_module_reset()
            cur.execute("SELECT module_key FROM account_modules WHERE user_id = %s",
                        (row["user_id"],))
            # ⚠️ This cursor is a `RealDictCursor`, so a row is a dict and `r[0]` is a
            # KeyError. It fired only when the member actually HAD overrides — clearing a
            # member nobody had narrowed returned 200 and wrote nothing — which is the
            # worst possible shape for a bug: the happy path is the broken one.
            keys = [r["module_key"] for r in cur.fetchall()]
            account_modules_reset(row["user_id"])
            return len(keys)


def _member_with_modules(cur, member_id: int) -> dict:
    cur.execute("SELECT * FROM org_members WHERE id = %s", (member_id,))
    row = _one_row(cur)
    if row is None:
        return {}
    if row.get("user_id"):
        cur.execute("SELECT module_key, enabled FROM account_modules WHERE user_id = %s",
                    (row["user_id"],))
        # ⚠️ The caller's cursor is a `RealDictCursor`, so each row is a dict. This read
        # `r[0]: r[1]` and raised KeyError — but ONLY when the member had at least one
        # override, because the comprehension never evaluates its body for an empty
        # result. So "clear a module for somebody who was never narrowed" answered 200
        # and the ordinary case was a 500, after the row was already written.
        row["module_overrides"] = {r["module_key"]: r["enabled"] for r in cur.fetchall()}
    else:
        row["module_overrides"] = {}
    return row


def set_scopes(org_id: int, share_scope: str, public_scope: str) -> dict:
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("UPDATE orgs SET share_scope = %s, public_scope = %s "
                        "WHERE id = %s RETURNING *", (share_scope, public_scope, org_id))
            row = cur.fetchone()
            if not row:
                raise OrgError("企业不存在 / no such organization")
            return dict(row)


def list_approvals(org_id: Optional[int] = None, status: str = "pending") -> list[dict]:
    """Publication requests, newest first.

    `org_id=None` means "every organization" and is only reachable by an operator; the
    router derives it from the session, never from the query string.
    """
    clause = "" if org_id is None else "AND a.org_id = %s"
    params = (status,) if org_id is None else (status, org_id)
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT a.*, o.name AS org_name FROM org_public_approvals a "
                "JOIN orgs o ON o.id = a.org_id "
                f"WHERE a.status = %s {clause} ORDER BY a.created_at DESC, a.id DESC",
                params)
            rows = _rows(cur)
    for row in rows:
        for key in ("decided_at", "created_at"):
            if row.get(key):
                row[key] = row[key].isoformat()
    return rows


# `account_modules` lives in the main app, and the shared layer must not import it
# (`api-admin/` may not import `api.*`). The main app installs these hooks at
# startup; without them a module change raises, which is better than a silent no-op.
account_modules_set = None      # type: ignore[assignment]
account_modules_clear = None    # type: ignore[assignment]
account_modules_reset = None    # type: ignore[assignment]


def bind_account_modules(setter, clearer, resetter=None) -> None:
    """Wire the `account_modules` helpers in. Called once, by the main app.

    ⚠️ Deliberately a hook rather than an import. `account_modules` writes a table the
    main app creates; a shared-layer import would mean the console's first startup on a
    fresh database failed on a table that does not exist yet. With a hook, the caller
    gets a clear error from `_require_account_modules()` instead of an ImportError at
    boot — and a console on a database where the main app has never run simply cannot
    change module entitlements, which is the truth.

    ⚠️ `resetter` is optional and for "drop every override for this account" — the whole
    row at once, which a per-key loop cannot express. A caller that binds only the first
    two keeps working; `clear_member_modules()` then refuses with the same "not available
    in this process" message rather than pretending to have cleared anything.
    """
    global account_modules_set, account_modules_clear, account_modules_reset
    account_modules_set, account_modules_clear = setter, clearer
    # ⚠️ Only ever overwritten, never left as a stale value from a previous binding:
    # a re-bind with two args must clear the third, or a test that just unwound its
    # hooks would find a live one still installed.
    account_modules_reset = resetter


def _require_account_modules() -> None:
    if account_modules_set is None or account_modules_clear is None:
        raise OrgError(
            "模块权限服务尚未就绪 / the module-permission service is not available in "
            "this process")


def _require_module_reset() -> None:
    if account_modules_reset is None:
        raise OrgError(
            "模块权限服务尚未就绪 / the module-permission service is not available in "
            "this process")


def has_public_approval(org_id: int, target_type: str, target_id: str, requester: str,
                        channel: str) -> bool:
    """Has this exact document been approved for outward publication by this person?

    Scoped to the document AND the requester, not to the channel alone: approving one
    export does not approve the next one, and an approval is a decision somebody made
    about a specific document. A company-level boolean was considered and rejected —
    it collapses the approval queue into a switch, and then there is no record of which
    content ever left the building, which is the thing worth having.
    """
    if org_id is None or not target_id:
        return False
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM org_public_approvals "
                "WHERE org_id = %s AND target_type = %s AND target_id = %s AND kind = %s "
                "  AND lower(requester_email) = lower(%s) AND status = 'approved' "
                "LIMIT 1",
                (org_id, target_type, target_id, channel, clean_email(requester)))
            return cur.fetchone() is not None


def may_publish(owner_email: str, channel: str, target_type: str, target_id: str
                ) -> tuple[bool, str]:
    """May this person make this document reachable on `channel`?"""
    if channel not in PUBLIC_CHANNELS or target_type not in TARGET_TYPES:
        return False, PUBLIC_FORBIDDEN
    _share_scope, public_scope, org_id = effective_profile(owner_email)
    if public_scope == PUBLIC_ALLOW:
        return True, OK
    if public_scope == PUBLIC_FORBID:
        return False, PUBLIC_FORBIDDEN
    # PUBLIC_APPROVE: an existing approval for this document settles it.
    if has_public_approval(org_id, target_type, target_id, owner_email, channel):
        return True, OK
    return False, NEEDS_APPROVAL


def request_public_approval(owner_email: str, target_type: str, target_id: str,
                            channel: str) -> dict:
    """Open (or re-open) a pending approval for one document. Idempotent per document.

    Returns the row so the caller can tell the requester "already waiting" without a
    second query. Re-requesting after a denial is allowed and resets the decision —
    otherwise a denied export is a permanent mark on the document, and the only fix is
    an operator editing rows by hand.
    """
    _share_scope, public_scope, org_id = effective_profile(owner_email)
    if org_id is None:
        raise OrgError(
            "你不在任何企业内，无需审批 / you are not in an organization, so no "
            "approval is needed")
    if public_scope == PUBLIC_ALLOW:
        raise OrgError(
            "本企业已允许直接公开 / this organization already allows direct publication")
    if channel not in PUBLIC_CHANNELS or target_type not in TARGET_TYPES:
        raise OrgError("未知的公开方式 / unknown publication target")
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # ⚠️ ON CONFLICT names the unique index explicitly. The earlier
            # `ON CONFLICT DO NOTHING` never conflicted — there was no unique index on
            # the table at all — so every click of the button appended another pending
            # row and the administrator's queue filled with copies of one document.
            cur.execute(
                "INSERT INTO org_public_approvals "
                "  (org_id, target_type, target_id, requester_email, kind, status) "
                "VALUES (%s, %s, %s, %s, %s, 'pending') "
                "ON CONFLICT (org_id, target_type, target_id, kind, lower(requester_email)) "
                "DO UPDATE SET status = 'pending', decided_by = '', decided_at = NULL "
                "RETURNING *",
                (org_id, target_type, (target_id or "").strip(), clean_email(owner_email),
                 channel))
            row = cur.fetchone()
            if not row:
                raise OrgError("无法登记公开申请 / could not record the publication request")
            return dict(row)


#: The three things that can happen to a request, and the spellings accepted for each.
#:
#: ⚠️ The stored value is the PAST participle (`approved`) because that is what lands in
#: the `status` column and in the audit trail. The route is `POST …/{decision}` and reads
#: as an imperative (`…/approve`), and both the console and the Settings card build that
#: URL by concatenating a verb. Accepting only the past tense made a perfectly reasonable
#: `…/approve` a 400 whose message — "unknown approval decision" — points at the caller
#: for something that is really a vocabulary mismatch.
#:
#: The past forms are kept as aliases rather than dropped: they are what the stored
#: statuses are, and a caller replaying a decision from an audit log should not have to
#: know which half of the pair the UI happens to use.
DECISIONS = {
    "approve": "approved", "approved": "approved",
    "deny": "denied", "denied": "denied",
    "revoke": "revoked", "revoked": "revoked",
}


def decide_public_approval(approval_id: int, decision: str, actor: str,
                           expected_org_id: Optional[int] = None) -> dict:
    """Approve / deny / revoke one request. `actor` is recorded for the audit trail.

    ⚠️ `expected_org_id` is an assertion, not a filter: it makes the UPDATE fail when the
    row belongs to another company, so an enterprise admin who guessed an approval id
    cannot decide somebody else's request. Without it the WHERE clause would just not
    match and the caller would be told "no such approval request" — which is the right
    answer for a wrong id and the WRONG answer for a right id in the wrong company,
    because the two need different responses.
    """
    resolved = DECISIONS.get(str(decision or "").strip().lower())
    if resolved is None:
        raise OrgError("未知的审批结果 / unknown approval decision")
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, org_id FROM org_public_approvals WHERE id = %s",
                        (approval_id,))
            existing = _one_row(cur)
            if not existing or (expected_org_id is not None
                                and existing["org_id"] != expected_org_id):
                raise MemberScopeError("审批记录不存在 / no such approval request")
            cur.execute("UPDATE org_public_approvals SET status = %s, decided_by = %s, "
                        "decided_at = NOW() WHERE id = %s RETURNING *",
                        (resolved, clean_email(actor), approval_id))
            return _one_row(cur)


# ── Backfill ─────────────────────────────────────────────────────────────────

def backfill_personal_orgs() -> dict:
    """Give every pre-existing non-operator account a one-person organization.

    Run once at startup, and on every startup after — it is a single INSERT…SELECT
    over a LEFT JOIN, so a settled database costs one indexed scan and writes nothing.

    ⚠️ Operators are EXCLUDED, and that is the design's decision rather than an
    oversight: `app_users.role='admin'` is the platform level, above every
    organization, so an operator belongs to none of them. Giving one a personal org
    would make "is this person an operator" answerable two ways, and the two could
    disagree after a demotion.

    ⚠️ Accounts already in an organization are left alone, including members whose
    invitation is still outstanding. Somebody mid-registration must not be handed a
    personal org by a startup sweep — the moment they finish, they would be in two.

    The slug is derived from the address's local part, not the whole address, so
    `ann@acme.com` and `ann@other.com` do not collide on the seed (the uniqueness
    suffix would still separate them, but the readable form is worth having).
    """
    with _db() as conn:
        with conn.cursor() as cur:
            # Three statements, and the temporary table between them is the point.
            #
            # The obvious single-statement version — insert the orgs in one CTE and join
            # them back by slug in the next — does not work: PostgreSQL runs the
            # sub-statements of a data-modifying WITH against ONE snapshot, so `made`
            # cannot see its own rows and the membership insert matches nothing. It
            # fails *quietly* too: the orgs exist, the memberships do not, and the next
            # run repairs it, so the symptom is "backfill took two startups".
            #
            # Materialising the candidate set makes the slug an ordinary value: computed
            # once, and genuinely readable by the statement after the INSERT.
            cur.execute("""
                CREATE TEMP TABLE _org_backfill_candidates ON COMMIT DROP AS
                SELECT u.id AS user_id,
                       lower(u.email) AS email,
                       coalesce(nullif(u.display_name, ''), u.email) AS name,
                       'p-' || regexp_replace(lower(split_part(u.email, '@', 1)),
                                              '[^a-z0-9]+', '-', 'g') || '-' || u.id
                           AS slug
                FROM app_users u
                WHERE u.deleted_at IS NULL
                  AND lower(coalesce(u.role, 'user')) <> 'admin'
                  AND NOT EXISTS (SELECT 1 FROM org_members m
                                  WHERE m.user_id = u.id
                                     OR lower(m.email) = lower(u.email))
            """)
            cur.execute("""
                INSERT INTO orgs (slug, name, kind, share_scope, public_scope,
                                  email_domains, status, created_by)
                SELECT slug, name, 'personal', 'global', 'allow', '{}', 'active', 'backfill'
                FROM _org_backfill_candidates
                ON CONFLICT (slug) DO NOTHING
            """)
            cur.execute("""
                INSERT INTO org_members (org_id, user_id, email, org_role, status, joined_at)
                SELECT o.id, c.user_id, c.email, 'owner', 'active', NOW()
                FROM _org_backfill_candidates c
                JOIN orgs o ON o.slug = c.slug
                ON CONFLICT (org_id, email) DO NOTHING
                RETURNING id
            """)
            return {"members": len(cur.fetchall())}


# ── Operator view (the admin console) ────────────────────────────────────────
#
# Everything above this line answers questions for ONE organization. Everything below
# answers them for the whole installation, and is therefore only ever reached through the
# console's `requires_admin` gate — never through `/api/org/*`, which resolves an
# organization from the caller's own membership row.
#
# ⚠️ Two rules hold for all of it:
#
#   1. **Read, don't derive.** Counts and rosters come from SQL. Reconstructing a total in
#      Python from a list the query already truncated is how a number on a dashboard
#      disagrees with the number on the page it links to.
#   2. **Tightening never revokes.** Changing `share_scope` or `public_scope` decides what
#      may happen NEXT. Every grant already handed out is an explicit row, and this
#      module has no code path that walks them. That asymmetry is deliberate (design
#      §4.1) and is why there is no `reconcile_shares()` here to be tempted by.


def list_orgs(kind: Optional[str] = None, q: str = "", limit: int = 200) -> list[dict]:
    """Every organization, with the numbers a list row needs.

    ⚠️ The counts are subqueries rather than a JOIN + GROUP BY on a second pass, and the
    `COUNT(*) FILTER` clauses are the reason: `members` counts every roster row while
    `admins` counts only active owners and admins. A plain `count(*)` over both would
    report the same number twice, and the console's "N admins" column would then be a
    number nobody can reconcile with the members table it links to.
    """
    if kind is not None:
        kind = _one_of(kind, KINDS, "企业类型", "invalid organization kind")
    where, params = [], []
    if kind:
        where.append("o.kind = %s")
        params.append(kind)
    needle = (q or "").strip()
    if needle:
        # ILIKE on the two things an operator actually types: a company name, or a
        # fragment of a member's address. The member arm is a join-free EXISTS so it
        # cannot multiply the rows and inflate the counts above.
        where.append("(o.name ILIKE %s OR o.slug ILIKE %s OR EXISTS ("
                     "SELECT 1 FROM org_members m2 WHERE m2.org_id = o.id "
                     "AND m2.email ILIKE %s))")
        params += [f"%{needle}%", f"%{needle}%", f"%{needle}%"]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT o.*, "
                "  (SELECT count(*) FROM org_members m WHERE m.org_id = o.id) AS members, "
                "  (SELECT count(*) FROM org_members m WHERE m.org_id = o.id "
                "     AND m.status = 'invited') AS invited, "
                "  (SELECT count(*) FROM org_members m WHERE m.org_id = o.id "
                "     AND m.status = 'active' "
                "     AND m.org_role IN ('owner', 'admin')) AS admins, "
                "  (SELECT count(*) FROM org_public_approvals a "
                "     WHERE a.org_id = o.id AND a.status = 'pending') AS pending_approvals, "
                "  (SELECT array_agg(m.email ORDER BY m.org_role, m.email) "
                "     FROM org_members m WHERE m.org_id = o.id "
                "     AND m.status = 'active' "
                "     AND m.org_role IN ('owner', 'admin')) AS admin_emails "
                f"FROM orgs o {clause} ORDER BY o.kind, o.name LIMIT %s",
                (*params, int(limit)))
            return [dict(r) for r in _rows(cur)]


def org_totals() -> dict:
    """The overview's four numbers and two queues, each from its own authoritative row.

    ⚠️ `pending_approvals` is a count of the approval table, not a sum over the
    organizations above: an approval whose organization was suspended still deserves to
    appear in an operator's queue, and deriving it from `list_orgs()` would quietly
    filter exactly the cases worth looking at.
    """
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT "
                "  count(*) FILTER (WHERE kind = 'enterprise') AS enterprises, "
                "  count(*) FILTER (WHERE kind = 'personal') AS personal_orgs, "
                "  count(*) FILTER (WHERE status = 'suspended') AS suspended "
                "FROM orgs")
            orgs_row = dict(cur.fetchone())
            cur.execute("SELECT count(*) AS total, "
                        "count(*) FILTER (WHERE status = 'active') AS active, "
                        "count(*) FILTER (WHERE status = 'invited') AS invited "
                        "FROM org_members")
            members_row = dict(cur.fetchone())
            cur.execute("SELECT count(*) AS pending FROM org_public_approvals "
                        "WHERE status = 'pending'")
            pending = (cur.fetchone() or {}).get("pending") or 0
    return {
        "enterprises": orgs_row.get("enterprises") or 0,
        "personal_users": orgs_row.get("personal_orgs") or 0,
        "suspended_orgs": orgs_row.get("suspended") or 0,
        "members_total": members_row.get("total") or 0,
        "members_active": members_row.get("active") or 0,
        "members_invited": members_row.get("invited") or 0,
        "pending_approvals": pending,
    }


def list_personal_members(limit: int = 500) -> list[dict]:
    """Every self-registered person, with the account state the console acts on.

    ⚠️ Joined to `app_users` with a LEFT JOIN, deliberately. A member row whose account
    was closed still shows up — with `account_deleted` set — because "this person signed
    themselves up and was later closed" is exactly the row an operator is looking for. An
    INNER JOIN would have quietly hidden every closed self-registered account, and the
    count on the overview card would stop matching the recycle bin.
    """
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT m.id, m.email, m.org_id, m.status, m.created_at, m.joined_at, "
                "       o.slug, o.status AS org_status, "
                "       u.id AS user_id, u.display_name, u.role AS account_role, "
                "       u.disabled, u.deleted_at, u.last_login_at "
                "FROM org_members m "
                "JOIN orgs o ON o.id = m.org_id AND o.kind = 'personal' "
                "LEFT JOIN app_users u ON u.id = m.user_id "
                "ORDER BY m.created_at DESC, m.id DESC LIMIT %s", (int(limit),))
            rows = []
            for row in _rows(cur):
                row = dict(row)
                row["account_deleted"] = bool(row.get("deleted_at"))
                row.pop("deleted_at", None)
                for key in ("created_at", "joined_at", "last_login_at"):
                    if row.get(key):
                        row[key] = row[key].isoformat()
                rows.append(row)
            return rows


def _clean_module_keys(keys: Iterable) -> list:
    """Validate one company's module licence, returning what may be stored.

    ⚠️ **An empty list is a statement, not an absence.** It means "this company is
    licensed for no switchable module", and it is stored as such — the same rule
    `modules._configured_keys()` applies to the deployment allowlist. The alternative,
    treating empty as "not set", makes it impossible for an operator to say a company has
    nothing, and the only way to get there would be to delete the row, which is not a
    gesture a checkbox can make. The company still keeps its required modules (sign-in,
    inbox, settings, data center): those are infrastructure, not product.

    Unknown and non-switchable keys are **refused** rather than dropped, for the reason
    `set_modules` in the console refuses a typo: a silently ignored key is read by the
    operator as "that module is now closed", which is a different and much worse belief
    than "that was a typo".
    """
    from klado_shared import modules as module_registry

    cleaned, unknown = [], []
    for item in (keys or ()):
        key = str(item or "").strip().lower()
        if not key or key in cleaned:
            continue
        if key not in module_registry.BY_KEY:
            unknown.append(key)
            continue
        if not module_registry.BY_KEY[key].toggleable:
            raise OrgError(
                f"模块 {key} 不能单独开关，它是系统必需的 / module {key} is required "
                f"infrastructure and cannot be switched per company")
        cleaned.append(key)
    if unknown:
        raise OrgError(
            f"未知的模块：{'、'.join(unknown)}（可用："
            f"{'、'.join(k for k in module_registry.ALL_KEYS if module_registry.BY_KEY[k].toggleable)}）"
            f" / unknown module(s): {', '.join(unknown)}")
    return cleaned


def set_org_modules(org_id: int, keys: Iterable) -> dict:
    """Replace the list of modules this company is licensed to hand out.

    ⚠️ Its OWN function, not another `update_org` keyword, and that is a deliberate
    shape. Two reasons, and the second is the one people forget:

    * it is a policy boundary with its own vocabulary, its own validator and its own
      refusal — the same kind of thing as `set_scopes()` and `transfer_org_owner()` in
      this file, all of which are separate calls;
    * `update_org`'s parameter list is PINNED by a test on purpose. The company settings
      form is a batch of small fields that may all be saved at once; the licence is not
      one of them, and folding it in means a name typo and a licence change become the
      same request — so a refused licence also refuses the name, and the operator is
      left believing the form did not save.

    `None` (never licensed) and `[]` (licensed to nothing) are different and both are
    reachable from here; see `_clean_module_keys`.
    """
    cleaned = _clean_module_keys(keys)
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("UPDATE orgs SET module_keys = %s WHERE id = %s RETURNING *",
                        (cleaned, org_id))
            row = cur.fetchone()
            if not row:
                raise OrgError("企业不存在 / no such organization")
            return dict(row)


def update_org(org_id: int, *, name: Optional[str] = None,
               email_domains: Optional[list] = None,
               share_scope: Optional[str] = None,
               public_scope: Optional[str] = None,
               status: Optional[str] = None) -> dict:
    """Change an organization. Every field is optional; `None` means "leave it".

    ⚠️ `slug` is deliberately NOT settable. It is the permanent public handle — it is in
    every shared link, every document id and every knowledge filename that references the
    company — and a slug that changes is a set of 404s that nobody can predict. A company
    that outgrows its name gets a new organization, which is a deliberate act with its own
    migration, not a field edit.

    Validators run here rather than in the router, so the console and any future caller
    apply exactly the same rules: a `clean_*` that returns empty is a refusal, not a
    silent no-op that leaves the row looking edited.
    """
    sets, params = [], []
    if name is not None:
        cleaned = clean_name(name)
        if not cleaned:
            raise OrgError("企业名称无效 / that organization name is not valid")
        sets.append("name = %s")
        params.append(cleaned)
    if email_domains is not None:
        domains = clean_domains(email_domains)
        # ⚠️ An empty list is refused rather than accepted, and the reason is now a
        # NARROWER one than it used to be. It used to read "clearing them means nobody
        # can be invited and nobody new can be matched to us" — the first half stopped
        # being true when the invite endpoint stopped consulting this list (an
        # administrator can invite any address; see `routers/org_admin.py::invite_member`).
        # A message that keeps claiming a rule nobody enforces is worse than no message,
        # because the reader believes the form is protecting something.
        #
        # What is left is registration routing: with no suffix, nobody who signs
        # themselves up is ever matched to this company. That is a real and silent loss,
        # and it reads as a settings mistake — the company looks configured and simply
        # never appears on anybody's signup. Narrowing the list is supported; emptying it
        # is not.
        if not domains:
            raise OrgError(
                "至少要保留一个企业邮箱后缀，否则自助注册的同事不会自动归属本企业 / an "
                "organization must keep at least one email suffix — narrowing the list is "
                "allowed, clearing it is not, because an empty list means nobody who "
                "signs themselves up is ever matched to this company")
        sets.append("email_domains = %s")
        params.append(domains)
    if share_scope is not None:
        sets.append("share_scope = %s")
        params.append(_one_of(share_scope, SHARE_SCOPES, "分享档位", "invalid share scope"))
    if public_scope is not None:
        sets.append("public_scope = %s")
        params.append(_one_of(public_scope, PUBLIC_SCOPES, "公开档位", "invalid public scope"))
    if status is not None:
        sets.append("status = %s")
        params.append(_one_of(status, ORG_STATUSES, "企业状态", "invalid organization status"))
    if not sets:
        return get_org(org_id) or {}
    params.append(org_id)
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"UPDATE orgs SET {', '.join(sets)} WHERE id = %s RETURNING *",
                        tuple(params))
            row = cur.fetchone()
            if not row:
                raise OrgError("企业不存在 / no such organization")
            return dict(row)


def transfer_org_owner(org_id: int, email: str, expected_org_id: Optional[int] = None) -> dict:
    """Hand an organization to somebody else, in one step.

    ⚠️ The demotion and the promotion are ONE transaction, and the order is fixed: the old
    owner becomes an admin first, then the new one becomes the owner. Doing it the other
    way round would pass through a state with no owner at all, and `_refuse_removing_last_admin`
    — which is there to make that state unreachable — would have to be bypassed rather
    than obeyed.

    Both rows must be active members of THIS organization. Promoting an invited address
    would mint an administrator for somebody who has never signed in.
    """
    address = clean_email(email)
    if not address:
        raise OrgError("邮箱地址无效 / that email address is not valid")
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if expected_org_id is not None and expected_org_id != org_id:
                raise MemberScopeError("企业不存在 / no such organization")
            cur.execute("SELECT * FROM org_members WHERE org_id = %s AND email = %s",
                        (org_id, address))
            incoming = cur.fetchone()
            if not incoming:
                raise MemberScopeError(
                    "该邮箱不是本企业成员 / that address is not a member of this "
                    "organization")
            if incoming["status"] != STATUS_ACTIVE:
                raise OrgError(
                    "该成员还没有接受邀请或已被停用 / this member has not accepted the "
                    "invitation, or has been disabled")
            cur.execute("UPDATE org_members SET org_role = 'admin' "
                        "WHERE org_id = %s AND org_role = 'owner'", (org_id,))
            cur.execute("UPDATE org_members SET org_role = 'owner' WHERE id = %s "
                        "RETURNING *", (incoming["id"],))
            return dict(cur.fetchone())


def list_admins(org_id: int) -> list[dict]:
    """Active owners and admins of one organization, for the console's drawer."""
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT m.id, m.email, m.org_role, m.status, m.joined_at, m.user_id "
                "FROM org_members m WHERE m.org_id = %s AND m.status = 'active' "
                "AND m.org_role IN ('owner', 'admin') "
                "ORDER BY m.org_role, m.email", (org_id,))
            return [dict(r) for r in _rows(cur)]
