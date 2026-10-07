"""
数据集容器 —— 「一个数据集」不再等于「一张表」。

## 为什么有这一层

`inbox`、`dashboard`、`dataset_query` 这一整套里，**dataset 一直等于一张可查询的物理表**：
`api/services/dataset_query.py` 直接拿 `db.list_datasets()` 的 `table_name` 当 SQL 白名单，
分享端点是 `/datasets/{table_name}/shares`，agent skill 也是这么写的。所以**「dataset = 表」
这个含义是承重的**，改它会同时动查询网关、分享语义和对外文档。

因此这里不重命名，也不改 `GET /api/data-center/datasets` 的形状（它仍然是平铺的表列表，
只是每行多了 `dataset_slug` / `dataset_name`）。这一层是**加在上面**的：

* `dataset_groups` —— 容器。有名字、描述、主人，可以装多张表。
* `_import_registry.dataset_group_id` —— 成员外键。物理表仍在 `public.`，逐表元数据
  （列、owner、清洗规则、row_count）仍在 `_import_registry`，**所以仪表盘、查询网关、
  分享、清洗规则全部不用改**。

用户看到的是「一个数据集，里面有这几张表」；代码里容器叫 group、表叫 dataset，
因为反过来会砸掉上面那套承重契约。把「dataset = 容器」一路改到底是另一件更大的事。

## 多次导入

「往一个数据集里存 3 张表，一周后再存 3 张对应的表」—— 成员身份跨导入保持稳定，靠的是
物理表名不变。所以：

* `overwrite` → `insert_pg_data(mode="replace")`：先 TRUNCATE 再写。
* `append` → `insert_pg_data(mode="append")`：只追加。

写入前的列校验是既有的（`data_center_db._insert_pg_data` 会拒绝表里没有的列），
所以「一周后那张源表多了两列」会被挡住，而不是把已有数据写坏。这是有意的：
追加到结构已经变了的表，是一个需要人决定的错误，不该由导入器猜。

## 可见性

容器本身：主人 + 管理员。成员表：沿用 `_ROW_VISIBLE`（自己的 + 分享给我的）。
所以**分享容器里的一张表，那张表会出现在对方的列表里，对方也能查询**——因为可见性
始终是逐表算的，容器没有额外收紧也没有额外放宽。

容器卡片对「只被分享了其中一张表」的人可见，且只显示他有权看到的那几张表。
（容器级别的分享是自然的下一步，这一版没有。）
"""
from __future__ import annotations

import logging
import re
import threading

import psycopg2
import psycopg2.extras
from psycopg2 import sql

from services import data_center_db as db

_LOG = logging.getLogger(__name__)

GROUPS_TABLE = "public.dataset_groups"
META_TABLE = db.META_TABLE              # public._import_registry
PUBLIC_SCHEMA = db.PUBLIC_SCHEMA

# The wire vocabulary is the reader's ("overwrite" / "append"); the table layer
# spells overwrite as "replace" because that is what a TRUNCATE-then-INSERT is.
MODES = {
    "overwrite": "replace",
    "replace": "replace",
    "append": "append",
}

# An external import is a copy, not a link. This is a hard cap: a source table with
# 40M rows would otherwise be pulled into this process's memory in one go.
MAX_EXTERNAL_ROWS = 200_000
EXTERNAL_CONNECT_TIMEOUT = 8

# Schemas that describe PostgreSQL rather than the customer's data. Importing from
# one of them is never what somebody means, so the picker refuses instead of
# offering a few hundred internal tables.
SYSTEM_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast"}

_schema_ready = False
_schema_lock = threading.Lock()


class GroupError(Exception):
    """A container-level refusal. `status` is what the router turns into."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


# ── schema ───────────────────────────────────────────────────────────────────

def ensure_schema() -> None:
    """Idempotent. Safe to call on every boot and from every entry point."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with db.get_pg_conn() as conn:
            with conn.cursor() as cur:
                # The member link points at `_import_registry`, so that table has to
                # exist first. `init_registry()` is idempotent (CREATE ... IF NOT
                # EXISTS + ADD COLUMN IF NOT EXISTS) and is what the app already calls
                # on boot — calling it here is what makes `ensure_schema()` self-sufficient
                # on a database that has never been touched, which is the case a test
                # (or a restore from a dump of only the container tables) hits.
                db.init_registry()
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {GROUPS_TABLE} (
                        id          SERIAL PRIMARY KEY,
                        slug        TEXT UNIQUE NOT NULL,
                        name        TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        owner_email TEXT NOT NULL DEFAULT '',
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                # The member link. Nullable on purpose: a table imported before this
                # layer existed has no group, and must keep working ungrouped rather
                # than being invented into one.
                cur.execute(f"ALTER TABLE {META_TABLE} "
                            "ADD COLUMN IF NOT EXISTS dataset_group_id INTEGER")
                cur.execute(f"""
                    DO $$
                    BEGIN
                      IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conrelid='{META_TABLE}'::regclass
                          AND conname='import_registry_dataset_group_fk'
                      ) THEN
                        ALTER TABLE {META_TABLE}
                          ADD CONSTRAINT import_registry_dataset_group_fk
                          FOREIGN KEY (dataset_group_id)
                          REFERENCES {GROUPS_TABLE}(id) ON DELETE SET NULL;
                      END IF;
                    END $$
                """)
                cur.execute(f"CREATE INDEX IF NOT EXISTS import_registry_group_idx "
                            f"ON {META_TABLE} (dataset_group_id)")
                cur.execute(f"CREATE INDEX IF NOT EXISTS dataset_groups_owner_idx "
                            f"ON {GROUPS_TABLE} (owner_email, updated_at DESC)")
            conn.commit()
        _schema_ready = True


# ── naming ───────────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    """A URL-safe id for a container, following the repo's existing convention.

    ⚠️ `[\\w]` and NOT `[a-z0-9]`. Python 3's `\\w` is Unicode-aware, so CJK survives —
    which is what `data_center_db._safe_id` already does for table names, and those
    already travel through `/datasets/{table_name}/…` paths. Stripping non-ASCII here
    would make every all-Chinese name (i.e. most of them, in this product) fail with
    "needs a letter or a digit", which is the worst possible answer to a Chinese name.
    There is no pinyin library available, and inventing a transliteration would be a
    worse lie than keeping the characters the user actually typed.

    Only a name with no word characters at all ("!!!", "   ") is refused.
    """
    slug = re.sub(r"[^\w]+", "-", (name or "").strip().lower()).strip("-_")
    if not slug:
        raise GroupError("数据集名需要至少一个字母或数字 / A dataset name needs a letter or a digit")
    return slug[:64]


def _unique_slug(cur, base: str) -> str:
    slug, n = base, 2
    while True:
        cur.execute(f"SELECT 1 FROM {GROUPS_TABLE} WHERE slug=%s", (slug,))
        if not cur.fetchone():
            return slug
        slug, n = f"{base}-{n}", n + 1


# ── visibility ───────────────────────────────────────────────────────────────

def _group_where(viewer_email: str | None) -> tuple[str, list]:
    """A group is visible to its owner, to an admin (viewer_email is None means
    the whole instance), and to anybody who can see at least one table in it.

    That last clause is why sharing one table inside a group is enough for the
    group to appear: visibility has always been computed per table, and this
    does not quietly tighten it.

    ⚠️ `_ROW_VISIBLE` is pasted in UNCHANGED and it names `_import_registry`
    by table, so the subquery below must NOT alias that table — an alias would
    make `_import_registry.owner_email` unresolvable. Same reason `_member_where`
    is used as-is. Do not "tidy" this into `FROM ... AS t`."""
    if viewer_email is None:
        return "TRUE", []
    return (f"(g.owner_email = %s OR EXISTS (SELECT 1 FROM {META_TABLE} "
            f"WHERE {META_TABLE}.dataset_group_id = g.id AND {db._ROW_VISIBLE}))"), \
        [viewer_email, viewer_email, viewer_email]


def _member_where(viewer_email: str | None) -> tuple[str, list]:
    """Per-table visibility, unchanged from the flat list."""
    if viewer_email is None:
        return "TRUE", []
    return db._ROW_VISIBLE, [viewer_email, viewer_email]


# ── reads ────────────────────────────────────────────────────────────────────

def group_labels(table_names: list[str]) -> dict[str, dict]:
    """`{table_name: {"slug":…, "name":…}}` for the tables that belong to a container.

    This is what lets the EXISTING flat `GET /datasets` payload gain a container
    label without `data_center_db` learning that containers exist: the flat list
    is produced exactly as before, and the router merges these labels onto it.
    Ungrouped tables are simply absent from the result — a table imported before
    this layer existed has no container and must keep working.

    One query for the whole page, not one per row: the list is rendered in a single
    go, so the per-row version is the difference between 1 and N round trips.
    """
    names = [t for t in {n for n in table_names if n} if len(t) <= 63]
    if not names:
        return {}
    ensure_schema()
    out: dict[str, dict] = {}
    with db.get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # ⚠️ No alias on `_import_registry`; `_member_where` is only used for the
            # per-table visibility case. Here the caller has ALREADY filtered by
            # visibility (this runs after `db.list_datasets`), so no predicate is
            # needed — the only question is which container a table points at.
            cur.execute(f"""
                SELECT {META_TABLE}.table_name, g.slug, g.name
                FROM {META_TABLE}
                JOIN {GROUPS_TABLE} g ON g.id = {META_TABLE}.dataset_group_id
                WHERE {META_TABLE}.table_name = ANY(%s)
            """, (names,))
            for r in cur.fetchall():
                out[r["table_name"]] = {"slug": r["slug"], "name": r["name"]}
    return out


def attach_group_labels(items: list[dict]) -> list[dict]:
    """Add `dataset_slug` / `dataset_name` to flat dataset rows, in place.

    Always sets both keys, to `None` when the table has no container. The frontend
    branches on the value, and a key that is sometimes missing is a class of bug
    the UI cannot tell from "the API forgot to answer".
    """
    labels = group_labels([i.get("table_name", "") for i in items])
    for item in items:
        hit = labels.get(item.get("table_name", ""))
        item["dataset_slug"] = hit["slug"] if hit else None
        item["dataset_name"] = hit["name"] if hit else None
    return items


def _member_rows(cur, group_id: int, viewer_email: str | None) -> list[dict]:
    where, params = _member_where(viewer_email)
    # ⚠️ No table alias, same reason as `_group_where`: `_ROW_VISIBLE` names
    # `_import_registry` itself.
    cur.execute(f"""
        SELECT {META_TABLE}.table_name, {META_TABLE}.display_name, {META_TABLE}.description,
               {META_TABLE}.row_count, {META_TABLE}.columns, {META_TABLE}.sheet_name,
               {META_TABLE}.source_file, {META_TABLE}.owner_email, {META_TABLE}.created_at,
               {META_TABLE}.updated_at, {META_TABLE}.update_count,
               {META_TABLE}.cleaning_rules, {META_TABLE}.dataset_group_id
        FROM {META_TABLE}
        WHERE {META_TABLE}.dataset_group_id = %s AND {where}
          AND to_regclass('public.' || {META_TABLE}.table_name) IS NOT NULL
        ORDER BY {META_TABLE}.created_at
    """, [group_id, *params])
    rows = []
    for r in cur.fetchall():
        row = dict(r)
        # `columns` is a JSONB list of pairs; the row card wants the same shape the
        # flat dataset list already returns so the UI has one code path.
        cols = row.get("columns")
        if isinstance(cols, list):
            row["col_count"] = len(cols)
        else:
            row["col_count"] = 0
        row["target_db"] = "pg"
        row["table_schema"] = PUBLIC_SCHEMA
        rows.append(row)
    return rows


def list_groups(viewer_email: str | None = None, admin: bool = False) -> list[dict]:
    """Every container the caller may see, newest first, each with its tables."""
    ensure_schema()
    who = None if admin else viewer_email
    where, params = _group_where(who)
    with db.get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT g.id, g.slug, g.name, g.description, g.owner_email,
                       g.created_at, g.updated_at
                FROM {GROUPS_TABLE} g
                WHERE {where}
                ORDER BY g.updated_at DESC
            """, params)
            groups = []
            for r in cur.fetchall():
                g = dict(r)
                g["tables"] = _member_rows(cur, g["id"], who)
                g["table_count"] = len(g["tables"])
                g["row_count"] = sum(int(t.get("row_count") or 0) for t in g["tables"])
                g["can_manage"] = bool(who is None or g["owner_email"] == who)
                groups.append(g)
    return groups


def get_group(slug: str, viewer_email: str | None = None, admin: bool = False) -> dict | None:
    who = None if admin else viewer_email
    for g in list_groups(who, admin=False):
        if g["slug"] == slug:
            return g
    return None


def _require_group(slug: str, viewer_email: str | None, admin: bool) -> dict:
    g = get_group(slug, viewer_email, admin)
    if not g:
        # 404, not 403: whether a container exists is itself information.
        raise GroupError(f"数据集 {slug} 不存在 / No dataset named {slug}", status=404)
    if not admin and viewer_email is not None and g["owner_email"] != viewer_email:
        raise GroupError(f"只有主人能改这个数据集 / Only the owner may change {slug}", status=403)
    return g


# ── writes ───────────────────────────────────────────────────────────────────

def create_group(name: str, description: str = "", owner_email: str = "") -> dict:
    ensure_schema()
    base = slugify(name)
    with db.get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            slug = _unique_slug(cur, base)
            cur.execute(
                f"INSERT INTO {GROUPS_TABLE} (slug, name, description, owner_email) "
                "VALUES (%s, %s, %s, %s) RETURNING id, slug, name, description, "
                "owner_email, created_at, updated_at",
                (slug, (name or "").strip(), (description or "").strip(), owner_email or ""),
            )
            row = dict(cur.fetchone())
        conn.commit()
    row["tables"] = []
    row["table_count"] = 0
    row["row_count"] = 0
    row["can_manage"] = True
    return row


def _jobs_for_slug(owner_email: str, slug: str) -> list[dict]:
    """The refresh schedules pointing at this container, as their owner.

    ⚠️ Scoped to the container's OWNER, not the caller: an admin deleting somebody
    else's dataset must be shown and must be able to delete that owner's schedules,
    not the admin's own (which are by definition not on this slug).
    Imported here rather than at module level because `external_sync` imports this
    module; a function-level import is what the codebase already does for
    `dataset_shares`.
    """
    if not owner_email:
        return []
    from services import external_sync
    return [j for j in external_sync.list_jobs(owner_email) if j.get("slug") == slug]


def delete_preview(slug: str, viewer_email: str | None = None, admin: bool = False) -> dict:
    """What a delete would actually remove, before anybody clicks OK.

    ⚠️ The dialog used to count tables from the CLIENT's last render, which is a
    snapshot from whenever the page last loaded — the count in the confirm box could
    disagree with what the server drops. This is the server's own membership, and it
    also says whether refresh schedules are attached, because a schedule survives
    its container (`external_db_sync_jobs.slug` is TEXT with no foreign key) and goes
    on failing every tick where nobody can see it.
    """
    g = _require_group(slug, viewer_email, admin)
    return {
        "slug": slug,
        "name": g["name"],
        "tables": [{"table_name": t["table_name"],
                    "row_count": int(t.get("row_count") or 0)}
                   for t in g["tables"]],
        "schedules": _jobs_for_slug(g.get("owner_email") or "", slug),
    }


def delete_group(slug: str, viewer_email: str | None = None, admin: bool = False) -> dict:
    """Drop the container AND every table inside it, in ONE transaction.

    ⚠️ All or nothing, and that is the whole point of the rewrite. The previous
    version dropped each table on its own connection and swallowed the failure with
    a log line, then deleted the container regardless — so one failing `DROP` left a
    physical table that nothing in the system could reach any more: no registry row,
    no card, no share. "Deleted" with the data still on disk and nobody able to find
    it. PostgreSQL DDL is transactional, so every `DROP`, the registry rows, the
    grant rows and the container row now go through one connection and one commit:
    if anything raises, `get_pg_conn` rolls the whole thing back and the dataset is
    exactly as it was.

    Schedules are NOT deleted here. They are returned so the caller can ask, and
    `delete_schedules_for_slug` does it — deleting the data and asking about the
    automation in the same click is two decisions, not one.
    """
    g = _require_group(slug, viewer_email, admin)
    dropped = [t["table_name"] for t in g["tables"]]
    from services import dataset_shares
    with db.get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for name in dropped:
                try:
                    cur.execute(sql.SQL("DROP TABLE IF EXISTS {}")
                                .format(sql.Identifier(PUBLIC_SCHEMA, name)))
                except Exception as exc:                       # noqa: BLE001
                    # Raised, not logged: the point is to abandon the whole delete.
                    _LOG.error("dropping group table %s failed: %s", name, exc)
                    raise GroupError(
                        f"表 {name} 删不掉，所以一个都没删：{exc} / "
                        f"Could not drop {name}, so nothing was deleted at all: {exc}",
                        status=500) from exc
                cur.execute(f"DELETE FROM {META_TABLE} WHERE table_name=%s", (name,))
            dataset_shares.revoke_all_for_in_tx(cur, dropped)
            cur.execute(f"DELETE FROM {GROUPS_TABLE} WHERE slug=%s", (slug,))
    return {"slug": slug, "dropped_tables": dropped,
            "schedules": _jobs_for_slug(g.get("owner_email") or "", slug)}


def delete_schedules_for_slug(slug: str, viewer_email: str, admin: bool = False) -> dict:
    """Remove the refresh schedules of a slug that no longer has a container.

    ⚠️ Refused while the container exists: this is the clean-up half of a delete,
    and running it against a live dataset would silently stop its automation — the
    same "looks like it worked" failure the delete itself is being fixed for.

    ⚠️ `admin` drops the owner filter, and it has to. The confirm box lists the
    schedules of the container's **owner**, which is how `delete_preview` finds them;
    an admin deleting somebody else's dataset was just shown that owner's schedules
    and would have had "delete them too" remove zero of them — a green answer for a
    no-op, which is the whole thing this endpoint exists to stop. The blast radius is
    bounded by the slug: only jobs attached to the container that was just deleted.
    """
    if not viewer_email:
        raise GroupError("需要登录 / sign in first", status=401)
    if get_group(slug, viewer_email):
        raise GroupError(
            f"数据集 {slug} 还在，不能只删定时任务 / The dataset {slug} still exists — "
            f"delete it first, or there is nothing to clean up",
            status=409)
    from services.external_sync import JOB_TABLE
    with db.get_pg_conn() as conn:
        with conn.cursor() as cur:
            if admin:
                cur.execute(f"DELETE FROM {JOB_TABLE} WHERE slug=%s", (slug,))
            else:
                cur.execute(f"DELETE FROM {JOB_TABLE} WHERE slug=%s AND owner_email=%s",
                            (slug, viewer_email))
            removed = cur.rowcount or 0
    return {"slug": slug, "deleted_schedules": removed}


def _normalise_mode(mode: str | None) -> str:
    # Strip FIRST, then decide whether the mode was given at all. `(mode or
    # "overwrite").strip()` treats `""` as "not provided" but rejects `"   "` — and a
    # form field holding only spaces is the same "not provided" as an empty one, so
    # that asymmetry was a trap rather than a validation.
    key = (mode or "").strip().lower() or "overwrite"
    if key not in MODES:
        raise GroupError(
            f"mode 只能是 overwrite 或 append / mode must be overwrite or append, got {mode!r}")
    return MODES[key]


def write_member_table(slug: str, table_name: str, columns: list[tuple[str, str]],
                       records: list[dict], mode: str = "overwrite",
                       owner_email: str | None = None) -> dict:
    """Create a table inside a group, or write into one that is already there.

    `columns` and `records` are only needed the first time: an existing table
    keeps its own shape and the column check in `_insert_pg_data` refuses a
    record whose keys it does not have."""
    ensure_schema()
    g = _require_group(slug, owner_email, admin=owner_email is None)
    db_mode = _normalise_mode(mode)
    name = db._safe_id(table_name)
    if not name:
        raise GroupError("表名不合法 / That table name is not usable")

    existed = bool(db.get_dataset(name))
    # ⚠️ A name collision is a DECISION, not an accident to resolve silently. The
    # write below is `TRUNCATE` + re-file, and the target name DEFAULTS TO THE
    # SOURCE NAME (`body.table_name or body.source_table`, and the form leaves
    # "save as" blank) — so importing a table whose name collides lands here
    # routinely.
    #
    # Refused here, both of the same-owner cases:
    #   · the table is filed in a DIFFERENT container — it would be emptied and
    #     then re-filed under this one, i.e. it silently leaves its old home.
    #     `attach_table` below deliberately refuses exactly that ("a move is
    #     somebody's decision"); this path did not go through it.
    #   · the table is not in any container — nothing signals the loss, because
    #     it never appears anywhere new; `overwrite` just empties a table the
    #     person may not have been looking at.
    #
    # ⚠️ The cross-OWNER case is deliberately NOT handled here. It is already
    # refused by `db._check_import_owner` right before the TRUNCATE, and moving
    # that check would change an established, tested message ("目标数据集不属于你")
    # for no gain. The guard asks "is this name MINE but somewhere else?", which
    # is the case that had no protection at all.
    if existed:
        cur = db.get_dataset(name) or {}
        mine = bool(owner_email) and (cur.get("owner_email") or "") == owner_email
        if mine and cur.get("dataset_group_id") != g["id"]:
            current = cur.get("dataset_group_id")
            if current:
                other = get_group_by_id(int(current), owner_email, admin=False)
                where = other["slug"] if other else "另一个数据集"
                raise GroupError(
                    f"{name} 已经在 {where} 里了，先把它移出来 / {name} is already in {where}",
                    status=409)
            raise GroupError(
                f"{name} 这个名字已经被一张不属于任何容器的表占用了，覆盖会先把它清空 / "
                f"{name} is already a table outside any container, and overwriting "
                f"would empty it. 换一个「存成」名字，或者先把那张表归入容器。 / "
                f"Use a different name, or file that table into a container first.",
                status=409)
    if not existed:
        if not columns:
            raise GroupError("新表需要列定义 / A new table needs its columns")
        # `source_file` records WHERE it came from, so a table that arrived from
        # another database is distinguishable from one that arrived from a
        # spreadsheet, without a new column.
        db.create_pg_table(name, columns, "", (table_name or name).strip(),
                           f"group:{g['slug']}", "", [], owner_email=owner_email or "")
    total = db.insert_pg_data(name, records, mode=db_mode, owner_email=owner_email)
    with db.get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {META_TABLE} SET dataset_group_id=%s WHERE table_name=%s",
                        (g["id"], name))
            cur.execute(f"UPDATE {GROUPS_TABLE} SET updated_at=NOW() WHERE id=%s", (g["id"],))
        conn.commit()
    return {"dataset": g["slug"], "table_name": name, "rows": total,
            "mode": mode if mode in ("overwrite", "append") else db_mode,
            "created": not existed}


def attach_table(slug: str, table_name: str, owner_email: str | None = None,
                 admin: bool = False) -> dict:
    """Put an ALREADY EXISTING table into a container. Moves no data.

    The file-import path creates its table through `batch-update-from-file`, which
    knows how to read a sheet; it has no idea what a container is. Rather than teach
    that endpoint about containers — and duplicate the whole cleaning/staging chain —
    the import runs as it always did and this links the result afterwards.

    Two things are refused rather than guessed:

    * a table the caller cannot manage. Attaching somebody else's table would put
      their rows inside a container they cannot see, and then they would vanish from
      that person's list entirely (`_ROW_VISIBLE` is per table, but the container
      card is drawn from the group).
    * a table that already belongs to a DIFFERENT container. That is a move, and a
      move is somebody's decision to make, not a side effect of an import.
    """
    g = _require_group(slug, owner_email, admin=admin)
    name = db._safe_id((table_name or "").strip())
    if not name:
        raise GroupError("表名不合法 / That table name is not usable")
    row = db.get_dataset(name, viewer_email=owner_email, admin=admin)
    if not row:
        raise GroupError(f"数据集 {name} 不存在 / No dataset named {name}", status=404)
    if not admin and owner_email is not None and row.get("owner_email") != owner_email:
        raise GroupError(f"只有主人能移动这张表 / Only the owner may move {name}", status=403)
    current = row.get("dataset_group_id")
    if current and int(current) != int(g["id"]):
        other = get_group_by_id(int(current), owner_email, admin=admin)
        where = f"{other['slug']}" if other else "另一个数据集"
        raise GroupError(
            f"{name} 已经在 {where} 里了，先把它移出来 / {name} is already in {where}",
            status=409)
    with db.get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {META_TABLE} SET dataset_group_id=%s WHERE table_name=%s",
                        (g["id"], name))
            cur.execute(f"UPDATE {GROUPS_TABLE} SET updated_at=NOW() WHERE id=%s", (g["id"],))
        conn.commit()
    return {"dataset": g["slug"], "table_name": name, "moved": bool(current)}


def get_group_by_id(group_id: int, viewer_email: str | None = None,
                    admin: bool = False) -> dict | None:
    """One container by numeric id — used to name the container a table is leaving."""
    who = None if admin else viewer_email
    for g in list_groups(who, admin=False):
        if int(g["id"]) == int(group_id):
            return g
    return None


# ── importing from another PostgreSQL ────────────────────────────────────────

def _check_identifier(value: str, what: str) -> str:
    """Only a plain identifier survives. Anything that needs quoting is refused
    rather than quoted, because a name reaching here came from a person's form."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,62}", value or ""):
        raise GroupError(f"{what} 只能是字母/数字/下划线 / {what} may only contain "
                         f"letters, digits and underscores: {value!r}")
    return value


def read_external_table(spec: dict, source_table: str,
                        limit: int = MAX_EXTERNAL_ROWS) -> tuple[list[dict], list[tuple[str, str]]]:
    """Read one table from ANY PostgreSQL — Aliyun RDS, AWS RDS and Cloud SQL all
    speak this protocol, which is the whole point: one code path, no vendor SDKs.

    Four things are deliberate, and each of them is a security property:

    * **Read-only is enforced by the server, not by convention.** The session sets
      `default_transaction_read_only`, so an import cannot write to somebody's
      production database even if a future edit adds a stray statement.
    * **The password is never stored and never logged.** It goes into the
      connection and nowhere else; errors are reported by host/database only.
    * **Identifiers are quoted, never formatted into SQL.**
    * **A connect timeout**, so a wrong host fails in seconds instead of parking
      a worker until the client gives up.
    """
    ensure_schema()
    import psycopg2
    import psycopg2.extras

    host = (spec.get("host") or "").strip()
    if not host:
        raise GroupError("外部数据库需要 host / The external database needs a host")
    port = int(spec.get("port") or 5432)
    database = _check_identifier((spec.get("database") or "").strip(), "database")
    user = (spec.get("user") or "").strip()
    password = spec.get("password") or ""
    if not user:
        raise GroupError("外部数据库需要 user / The external database needs a user")
    schema = _check_identifier((spec.get("schema") or "public").strip(), "schema")
    source_table = _check_identifier((source_table or "").strip(), "table")
    limit = max(1, min(int(limit or MAX_EXTERNAL_ROWS), MAX_EXTERNAL_ROWS))

    conn = None
    try:
        conn = psycopg2.connect(host=host, port=port, dbname=database, user=user,
                                password=password,
                                connect_timeout=EXTERNAL_CONNECT_TIMEOUT,
                                application_name="klado-external-import")
        conn.autocommit = False
        # ⚠️ `SET default_transaction_read_only = on` ALONE DOES NOT WORK, and this is
        # the whole safety story of external import, so it is worth being explicit about
        # why: it only sets the flag for transactions started *after* it. We are already
        # inside one (the SET itself opened it), so a `DELETE` on the next line runs in a
        # read-write transaction and goes through. Measured on PostgreSQL 15.19: that
        # variant let the DELETE land; `set_session(readonly=True)` raises
        # `ReadOnlySqlTransaction` instead. It must be called before any statement,
        # which is why it is here rather than next to the SET.
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            # Kept as well: it covers any *later* transaction on this session, so a
            # future edit that flips autocommit on cannot silently un-protect us.
            cur.execute("SET default_transaction_read_only = on")
            cur.execute("SET statement_timeout = 60000")
            cur.execute(
                """
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """, (schema, source_table))
            cols = cur.fetchall()
            if not cols:
                raise GroupError(
                    f"{schema}.{source_table} 在那个数据库里不存在 "
                    f"/ {schema}.{source_table} does not exist on that database")
            cur.execute(
                # ⚠️ The dot belongs in the template. `FROM {} {}` joins two
                # identifiers with a SPACE, which reads as a cross join between a
                # table named "public" and one named "upstream" — it fails with
                # UndefinedTable, and the real reason ("there is no table public")
                # is nowhere near the actual problem.
                sql.SQL("SELECT * FROM {}.{} LIMIT %s")
                .format(sql.Identifier(schema), sql.Identifier(source_table)),
                (limit,))
            names = [d.name for d in cur.description]
            records = [dict(zip(names, row)) for row in cur.fetchall()]
        conn.rollback()
    except GroupError:
        # Never let a driver's message carry the connection string back out.
        conn and conn.rollback()
        raise
    except Exception as exc:                             # noqa: BLE001
        # ⚠️ The driver's message can quote the DSN, which carries the password.
        # Report the target, not the reason, so nothing sensitive escapes.
        _LOG.warning("external import from %s/%s failed", host, database)
        raise GroupError(
            f"连不上 {host}:{port}/{database}（{type(exc).__name__}）"
            f" / Could not connect to {host}:{port}/{database}") from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:                             # noqa: BLE001
                pass
    return records, [(n, t) for n, t in cols]


def import_external_postgres(spec: dict, slug: str, table_name: str,
                             source_table: str, mode: str = "overwrite",
                             owner_email: str | None = None) -> dict:
    """Read a table from another PostgreSQL into one of ours.

    The source is only ever READ. What lands in Klado is a copy, owned by the
    caller, in `public.` — the same place a spreadsheet import would put it, so
    the dashboard, the query gateway and the sharing rules cannot tell the
    difference and do not need to."""
    records, columns = read_external_table(spec, source_table)
    if not records:
        raise GroupError(f"{source_table} 在那个数据库里没有数据 / {source_table} has no rows")
    return write_member_table(slug, table_name, columns, records, mode=mode,
                              owner_email=owner_email)


def peek_external_tables(spec: dict) -> list[dict]:
    """List the tables on the external database, so the form can offer a choice
    instead of making somebody type a name they cannot see."""
    import psycopg2

    # ⚠️ Validate before `ensure_schema()`. This function touches two databases —
    # ours and theirs — and a request we are going to refuse should not open the
    # first one. It also keeps the refusal testable without any server.
    host = (spec.get("host") or "").strip()
    if not host:
        raise GroupError("外部数据库需要 host / The external database needs a host")
    port = int(spec.get("port") or 5432)
    database = (spec.get("database") or "").strip()
    user = (spec.get("user") or "").strip()
    # ⚠️ This must filter by the schema the caller is going to IMPORT from, and it
    # used to ignore it entirely (hard-coded "not a system schema"). The result was
    # a picker that offered `analytics.orders` while the import only ever looked in
    # whatever schema the form carried — so a table the user could see and select
    # would fail on submit. List the same place we will read from.
    schema = _check_identifier((spec.get("schema") or "public").strip(), "schema")
    if schema in SYSTEM_SCHEMAS:
        raise GroupError(
            f"不能从系统 schema {schema} 导入 / Cannot import from the system schema {schema}")
    ensure_schema()
    conn = None
    try:
        conn = psycopg2.connect(host=host, port=port, dbname=database,
                                user=user, password=spec.get("password") or "",
                                connect_timeout=EXTERNAL_CONNECT_TIMEOUT,
                                application_name="klado-external-peek")
        conn.autocommit = False
        conn.set_session(readonly=True)      # same reason as read_external_table
        with conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on")
            cur.execute("SET statement_timeout = 30000")
            cur.execute("""
                SELECT table_schema, table_name FROM information_schema.tables
                WHERE table_type = 'BASE TABLE' AND table_schema = %s
                  AND table_schema <> 'information_schema'
                ORDER BY table_schema, table_name
            """, (schema,))
            out = [{"schema": s, "table": t} for s, t in cur.fetchall()]
        conn.rollback()
    except GroupError:
        raise
    except Exception as exc:                             # noqa: BLE001
        _LOG.warning("external peek at %s/%s failed", host, database)
        raise GroupError(
            f"连不上 {host}:{port}/{database}（{type(exc).__name__}）"
            f" / Could not connect to {host}:{port}/{database}") from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:                             # noqa: BLE001
                pass
    return out
