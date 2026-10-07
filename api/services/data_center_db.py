"""
Data Center 数据库管理器
- psycopg2（同步）操作唯一的 klado.public
- 元数据表：public._import_registry
"""
from __future__ import annotations

import json
import uuid
import logging
import os
import re
from datetime import datetime
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool
from psycopg2 import sql

logger = logging.getLogger(__name__)

PUBLIC_SCHEMA = "public"
META_TABLE = "public._import_registry"
FILE_TABLE = "public._file_library"
QI_MAP_TABLE = "public._qi_sheet_mappings"
# Smart Import jobs live in the shared public schema. Klado is a local,
# single-machine deployment (one PostgreSQL instance, one environment): there is
# no cloud/CodeUp stage to reconcile against, and no legacy `system.import_jobs`
# table in the deployed contract.
IMPORT_JOB_TABLE = "public.system_import_jobs"
RAW_BUCKET = "datacenter-raw"

# Tables that may be maintained by Smart Import. Application/system tables are
# deliberately excluded so a spreadsheet can never overwrite them.
#
# ⚠️ This allowlist is intentionally EMPTY. Its one entry (`channel_mapping`) was
# the old sales-channel lookup table, removed when channels were dropped from the
# backend. Do NOT widen this to "every table" — the allowlist stays a narrow,
# explicit safety boundary; an empty one is the correct state, not a bug. It is
# used as `table_name = ANY(%s)`, and an empty list is valid SQL that matches
# zero rows, so an empty Smart Import surface is simply a no-op.
SMART_IMPORT_TABLES: set[str] = set()


# ── 归属（owner）隔离 ──────────────────────────────────────────────────────────
# Explicit viewers, including administrators, get their own content plus grants.
# None is reserved for trusted internal maintenance, never an HTTP caller.
_ROW_VISIBLE = ("((owner_email = %s AND owner_email <> '')"
                " OR table_name IN (SELECT s.table_name FROM public.dataset_shares s"
                " WHERE s.grantee_email = %s AND s.shared_by = _import_registry.owner_email))")
_FILE_VISIBLE = "(owner_email = %s AND owner_email <> '')"


def _owner_filter(viewer_email: str | None, admin: bool) -> tuple[str, list]:
    if viewer_email is None:
        return "TRUE", []
    return _ROW_VISIBLE, [viewer_email, viewer_email]


def _file_filter(viewer_email: str | None, admin: bool) -> tuple[str, list]:
    if viewer_email is None:
        return "TRUE", []
    return _FILE_VISIBLE, [viewer_email]


def may_read_object(object_name: str, viewer_email: str | None, admin: bool) -> bool:
    if viewer_email is None:
        return True
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT owner_email FROM {FILE_TABLE} WHERE object_name=%s", (object_name,))
            row = cur.fetchone()
    if row and row[0] and row[0] == viewer_email:
        return True
    from services import file_shares
    return bool(row and file_shares.may_read_object(object_name, viewer_email))


def _visible_object_names(viewer_email: str | None, admin: bool) -> set[str] | None:
    if viewer_email is None:
        return None
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT object_name FROM {FILE_TABLE} WHERE {_FILE_VISIBLE}", (viewer_email,))
            return {r[0] for r in cur.fetchall()}


def _adopt_legacy_owner(cur, table: str) -> None:
    from core.identity import legacy_owner
    owner = legacy_owner()
    if owner:
        cur.execute(f"UPDATE {table} SET owner_email=%s WHERE owner_email=''", (owner,))


# ── OSS 存储（替代 MinIO）──────────────────────────────────────────────────────
from services import annotations, inbox, oss_storage


def save_source_file(filename: str, data: bytes) -> str:
    """上传原始文件到 OSS，返回 object path（不含 bucket 前缀）。"""
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    object_name = f"{datetime.now().strftime('%Y-%m')}/{ts}_{uuid.uuid4().hex[:12]}_{filename}"
    oss_storage.put_object(RAW_BUCKET, object_name, data)
    return object_name


def get_source_file(object_name: str) -> tuple[bytes, str]:
    """从 OSS 下载原始文件，返回 (bytes, content_type)。"""
    data = oss_storage.get_object(RAW_BUCKET, object_name)
    # 根据扩展名猜 content_type
    ext = object_name.rsplit(".", 1)[-1].lower() if "." in object_name else ""
    ct_map = {
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xls": "application/vnd.ms-excel",
        "xlsm": "application/vnd.ms-excel.sheet.macroenabled.12",
        "csv": "text/csv",
    }
    return data, ct_map.get(ext, "application/octet-stream")


# ── 连接池 ────────────────────────────────────────────────────────────────────

_pg_pool: psycopg2.pool.ThreadedConnectionPool | None = None

def _pool_kwargs() -> dict:
    # Reuse the hardened connection parameters (keepalives + statement_timeout)
    # from core.db so pooled connections behave like connect_main() ones.
    from core.db import _pg_kwargs

    return _pg_kwargs()


def _ensure_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(minconn=1, maxconn=8, **_pool_kwargs())
    return _pg_pool


class _PooledConn:
    """Context manager: borrows a psycopg2 connection from the pool and returns it on exit."""
    def __enter__(self):
        self._pool = _ensure_pool()
        self._conn = self._pool.getconn()
        return self._conn

    def __exit__(self, exc_type, *_):
        if exc_type:
            self._conn.rollback()
        else:
            try:
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        self._pool.putconn(self._conn)


def get_pg_conn() -> _PooledConn:
    """Return a context-manager that yields a pooled psycopg2 connection."""
    return _PooledConn()


# ── 初始化 ────────────────────────────────────────────────────────────────────

def init_registry():
    """确保 _import_registry 元数据表存在"""
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {META_TABLE} (
                    id           SERIAL PRIMARY KEY,
                    table_name   TEXT UNIQUE NOT NULL,
                    display_name TEXT,
                    target_db    TEXT NOT NULL DEFAULT 'pg',
                    fingerprint  TEXT NOT NULL,
                    columns      JSONB NOT NULL,
                    row_count    INTEGER DEFAULT 0,
                    source_file  TEXT,
                    sheet_name   TEXT,
                    modules      TEXT[] DEFAULT '{{}}',
                    description  TEXT DEFAULT '',
                    created_at   TIMESTAMP DEFAULT NOW(),
                    updated_at   TIMESTAMP DEFAULT NOW(),
                    update_count INTEGER DEFAULT 0,
                    owner_email  TEXT NOT NULL DEFAULT ''
                )
            """)
            for col, definition in [
                ("target_db",       "TEXT NOT NULL DEFAULT 'pg'"),
                ("modules",         "TEXT[] DEFAULT '{}'"),
                ("description",     "TEXT DEFAULT ''"),
                ("update_count",    "INTEGER DEFAULT 0"),
                ("source_file_path", "TEXT DEFAULT NULL"),
                ("cleaning_rules",  "JSONB DEFAULT NULL"),
                ("owner_email",     "TEXT NOT NULL DEFAULT ''"),
            ]:
                cur.execute(f"""
                    ALTER TABLE {META_TABLE}
                    ADD COLUMN IF NOT EXISTS {col} {definition}
                """)
            # target_db historically selected separate databases/schemas. All data
            # now lives in klado.public, so legacy values must not route
            # reads or writes elsewhere.
            cur.execute(f"UPDATE {META_TABLE} SET target_db='pg' WHERE target_db IS DISTINCT FROM 'pg'")
            cur.execute(
                """
                DO $$
                BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid='public._import_registry'::regclass
                      AND conname='import_registry_public_target_only'
                  ) THEN
                    ALTER TABLE public._import_registry
                      ADD CONSTRAINT import_registry_public_target_only CHECK (target_db='pg');
                  END IF;
                END $$
                """
            )

            # A database rebuild keeps OSS objects but recreates metadata. Make
            # the known public import targets immediately available again.
            cur.execute(
                """
                SELECT c.table_name, c.column_name, c.data_type,
                       COALESCE(s.n_live_tup, 0)::bigint
                FROM information_schema.columns c
                LEFT JOIN pg_stat_user_tables s
                  ON s.schemaname='public' AND s.relname=c.table_name
                WHERE c.table_schema='public' AND c.table_name = ANY(%s)
                ORDER BY c.table_name, c.ordinal_position
                """,
                (list(SMART_IMPORT_TABLES),),
            )
            targets: dict[str, dict] = {}
            for table_name, column_name, data_type, estimated_rows in cur.fetchall():
                item = targets.setdefault(table_name, {"columns": [], "rows": estimated_rows})
                item["columns"].append([column_name, data_type])
            for table_name, item in targets.items():
                cur.execute(
                    f"""
                    INSERT INTO {META_TABLE}
                      (table_name, display_name, target_db, fingerprint, columns, row_count, modules, description)
                    VALUES (%s,%s,'pg',%s,%s,%s,%s,%s)
                    ON CONFLICT (table_name) DO UPDATE SET
                      target_db='pg', columns=EXCLUDED.columns
                    """,
                    (
                        table_name, table_name, f"public:{table_name}",
                        json.dumps(item["columns"]), item["rows"],
                        [],
                        f"Smart Import target in public.{table_name}",
                    ),
                )
            logger.info("Smart Import registry normalized: schema=public targets=%d", len(targets))
            pruned = _prune_orphan_datasets_stmt(cur)
            if pruned:
                logger.info("pruned %d orphan dataset registry row(s) with no backing table", pruned)
            _adopt_legacy_owner(cur, META_TABLE)
        conn.commit()


def prune_orphan_datasets() -> int:
    """删除 _import_registry 中「指向不存在的表」的孤儿行，返回删除条数。幂等。

    为什么必须做：注册表是元数据，表是实体，两者会失配 —— 一次数据库重建
    （OSS 对象与注册表还在，public 表被重建掉了）就留下这种行，而 Data Center
    的 Datasets 页会把它渲染成一张永远为空的卡片。所以每次 `init_registry()`
    都清一次，且读接口（`list_datasets` / `get_dataset`）另外按 `to_regclass`
    过滤，这样即使自愈这一轮没跑到，也不会显示幽灵卡片。
    """
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            pruned = _prune_orphan_datasets_stmt(cur)
        conn.commit()
    return pruned


def _prune_orphan_datasets_stmt(cur) -> int:
    """按调用方给的游标执行孤儿行清理，便于在 init_registry 的同一连接里就地做。"""
    cur.execute(
        f"DELETE FROM {META_TABLE} r "
        f"WHERE to_regclass('public.' || r.table_name) IS NULL"
    )
    return cur.rowcount or 0


def init_file_library():
    """确保 _file_library 文件库表存在"""
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {FILE_TABLE} (
                    id            SERIAL PRIMARY KEY,
                    filename      TEXT NOT NULL,
                    object_name   TEXT NOT NULL,
                    size_bytes    BIGINT DEFAULT 0,
                    sheets_meta   JSONB DEFAULT '[]',
                    uploaded_at   TIMESTAMP DEFAULT NOW(),
                    owner_email   TEXT NOT NULL DEFAULT ''
                )
            """)
            for col, definition in [
                ("share_token", "TEXT DEFAULT NULL"),
                # Per-user Data Center: the account that owns this file. Empty means
                # "predates ownership" and is adopted only by the configured legacy owner.
                ("owner_email", "TEXT NOT NULL DEFAULT ''"),
            ]:
                cur.execute(f"""
                    ALTER TABLE {FILE_TABLE}
                    ADD COLUMN IF NOT EXISTS {col} {definition}
                """)
            _adopt_legacy_owner(cur, FILE_TABLE)
        conn.commit()
    # 表结构就绪后顺手对齐 id 序列（原因见 align_file_library_sequence）
    try:
        align_file_library_sequence()
    except Exception:
        logger.warning("init_file_library: aligning id sequence failed", exc_info=True)


def align_file_library_sequence() -> int | None:
    """把 _file_library.id 的序列对齐到 MAX(id)，幂等，返回对齐后的序列值。

    为什么必须做：序列一旦落后于 MAX(id)，下一次 nextval 就会撞上已存在的主键，
    INSERT 报唯一冲突而 OSS 对象已经写好 —— 于是留下「OSS 有、文件库无」的孤儿，
    Publish Link 这类依赖 file_id 的功能随之报「无法获取文件 ID」。

    序列落后有真实来源：显式指定 id 的插入（register_oss_file 的冲突重试分支、
    快照恢复写回）都不推进序列，而序列只前进不回退（删行也不会回退）。
    """
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            new_value = _align_sequence_stmt(cur)
        conn.commit()
    return new_value


def _align_sequence_stmt(cur) -> int | None:
    """把序列对齐到 MAX(id) —— 用调用方给的游标执行，便于在已回滚的连接上就地修复。"""
    cur.execute("SELECT pg_get_serial_sequence(%s, 'id')", (FILE_TABLE,))
    seq = cur.fetchone()[0]
    if not seq:
        return None
    # ⚠️ `setval(seq, 0)` is an error, not a no-op ("value 0 is out of bounds"), so an
    # EMPTY library must skip the call entirely — it used to raise on every startup and
    # be swallowed as a warning. With no rows there is nothing to align to: nextval
    # starts at 1 by itself.
    cur.execute(f"SELECT MAX(id) FROM {FILE_TABLE}")
    current = cur.fetchone()[0]
    if current is None:
        return None
    cur.execute("SELECT setval(%s, %s, true) AS v", (seq, int(current)))
    return int(cur.fetchone()[0])


def init_qi_mappings():
    """确保 Quick Import sheet→dataset 映射记忆表存在"""
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {QI_MAP_TABLE} (
                    file_slug  TEXT NOT NULL,
                    sheet_name TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    mode       TEXT NOT NULL DEFAULT 'append',
                    cleaning_rules JSONB DEFAULT NULL,
                    updated_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (file_slug, sheet_name)
                )
            """)
            cur.execute(f"""
                ALTER TABLE {QI_MAP_TABLE}
                ADD COLUMN IF NOT EXISTS cleaning_rules JSONB DEFAULT NULL
            """)
            cur.execute(
                f"DELETE FROM {QI_MAP_TABLE} q WHERE NOT EXISTS "
                f"(SELECT 1 FROM {META_TABLE} r WHERE r.table_name=q.table_name)"
            )
        conn.commit()


def _qi_owner_key(file_slug: str, owner_email: str) -> str:
    import hashlib
    if not owner_email:
        return file_slug  # internal compatibility only
    return hashlib.sha256(owner_email.lower().encode()).hexdigest()[:16] + ':' + file_slug


def get_qi_mappings(file_slug: str, owner_email: str = "") -> dict:
    """返回当前账号的 sheet→dataset 记忆映射。"""
    file_slug = _qi_owner_key(file_slug, owner_email)
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT sheet_name, table_name, mode, cleaning_rules FROM {QI_MAP_TABLE} WHERE file_slug=%s",
                (file_slug,),
            )
            return {
                row["sheet_name"]: {
                    "table_name": row["table_name"],
                    "mode": row["mode"],
                    "cleaning_rules": row["cleaning_rules"],
                }
                for row in cur.fetchall()
            }


def save_qi_mappings(file_slug: str, mappings: list[dict], owner_email: str = "") -> None:
    """Upsert sheet→dataset 映射"""
    file_slug = _qi_owner_key(file_slug, owner_email)
    if not mappings:
        return
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            for m in mappings:
                cur.execute(
                    f"""
                    INSERT INTO {QI_MAP_TABLE} (file_slug, sheet_name, table_name, mode, cleaning_rules, updated_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (file_slug, sheet_name) DO UPDATE
                      SET table_name = EXCLUDED.table_name,
                          mode = EXCLUDED.mode,
                          cleaning_rules = EXCLUDED.cleaning_rules,
                          updated_at = NOW()
                    """,
                    (file_slug, m["sheet_name"], m["table_name"],
                     m.get("mode", "append"),
                     json.dumps(m["cleaning_rules"]) if m.get("cleaning_rules") else None),
                )
        conn.commit()


def _serialize_import_job(row: dict | None) -> dict | None:
    if not row:
        return None
    result = dict(row)
    result["id"] = str(result["id"])
    for key in ("created_at", "finished_at"):
        if result.get(key) is not None:
            result[key] = result[key].isoformat()
    detail = result.get("error_detail") or {}
    result["owner_email"] = detail.get("owner_email", "")
    result["results"] = detail.get("results", [])
    result["current_sheet"] = detail.get("current_sheet")
    result["error"] = detail.get("error")
    result.pop("error_detail", None)
    return result


def create_import_job(file_name: str, request_key: str, mappings_count: int, owner_email: str = "") -> tuple[dict, bool]:
    """Create a persistent import job, reusing a recent identical request."""
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT * FROM {IMPORT_JOB_TABLE}
                WHERE module='smart_import'
                  AND created_at > NOW() - INTERVAL '10 minutes'
                  AND error_detail->>'request_key'=%s
                  AND status IN ('pending', 'running', 'completed')
                ORDER BY created_at DESC LIMIT 1
                """,
                (request_key,),
            )
            existing = cur.fetchone()
            if existing:
                return _serialize_import_job(existing), True
            cur.execute(
                f"""
                INSERT INTO {IMPORT_JOB_TABLE}
                  (file_name, module, status, rows_total, rows_ok, rows_error, error_detail)
                VALUES (%s, 'smart_import', 'pending', %s, 0, 0, %s)
                RETURNING *
                """,
                (file_name, mappings_count, psycopg2.extras.Json({"request_key": request_key, "owner_email": owner_email, "results": []})),
            )
            row = cur.fetchone()
        conn.commit()
    return _serialize_import_job(row), False


def get_import_job(job_id: str) -> dict | None:
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM {IMPORT_JOB_TABLE} WHERE id=%s AND module='smart_import'",
                (job_id,),
            )
            return _serialize_import_job(cur.fetchone())


def update_import_job(
    job_id: str,
    *,
    status: str,
    rows_ok: int,
    rows_error: int,
    detail: dict,
) -> dict | None:
    finished = status in {"completed", "error"}
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE {IMPORT_JOB_TABLE}
                SET status=%s, rows_ok=%s, rows_error=%s, error_detail=error_detail || %s::jsonb,
                    finished_at=CASE WHEN %s THEN NOW() ELSE NULL END
                WHERE id=%s AND module='smart_import'
                RETURNING *
                """,
                (status, rows_ok, rows_error, psycopg2.extras.Json(detail), finished, job_id),
            )
            row = cur.fetchone()
        conn.commit()
    return _serialize_import_job(row)


def update_cleaning_rules(table_name: str, rules: dict) -> None:
    """Store cleaning_rules in _import_registry for a dataset."""
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {META_TABLE} SET cleaning_rules=%s, updated_at=NOW() WHERE table_name=%s",
                (json.dumps(rules), table_name),
            )
        conn.commit()


# ── 文件库操作 ────────────────────────────────────────────────────────────────

def stage_file(filename: str, data: bytes, sheets_meta: list, folder: str = "",
               owner_email: str = "") -> dict:
    """上传文件到 OSS 并写入文件库，返回文件记录。

    folder 为目标目录路径（不含首尾斜杠）；owner_email 是上传者，写入库行做归属隔离。
    """
    folder = folder.strip("/")
    prefix = folder + "/" if folder else ""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    object_name = f"{prefix}{ts}_{uuid.uuid4().hex[:12]}_{filename}"
    oss_storage.put_object(RAW_BUCKET, object_name, data)
    row = _insert_library_row(object_name, filename, len(data), sheets_meta, owner_email)
    if row.get("uploaded_at"):
        row["uploaded_at"] = row["uploaded_at"].isoformat()
    return row


def _insert_library_row(object_name: str, filename: str, size_bytes: int, sheets_meta: list,
                        owner_email: str = "") -> dict:
    """往文件库 INSERT 一行；主键冲突时对齐序列后重试一次。

    OSS 对象此时已经写好了，所以插入绝不能静默失败：否则会留下
    「OSS 有、文件库无」的孤儿对象，依赖 file_id 的功能（Publish Link 等）随之失效。
    """
    statement = f"""
        INSERT INTO {FILE_TABLE} (filename, object_name, size_bytes, sheets_meta, owner_email)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *
    """
    params = (filename, object_name, size_bytes, json.dumps(sheets_meta), owner_email or "")
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute(statement, params)
                row = cur.fetchone()
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                logger.warning(
                    "file library insert hit a unique violation (object=%s) — "
                    "id sequence was behind MAX(id); realigning and retrying", object_name,
                )
                # 就地修复：rollback 后这条连接是干净的，setval 不参与事务，立即生效
                with conn.cursor() as fix_cur:
                    _align_sequence_stmt(fix_cur)
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as retry_cur:
                    retry_cur.execute(statement, params)
                    row = retry_cur.fetchone()
        conn.commit()
    return dict(row)


def browse_files(prefix: str = "", viewer_email: str | None = None, admin: bool = False) -> dict:
    """列出 OSS 指定前缀下的文件夹和文件，并从文件库补充 sheets_meta。

    非管理员只看到自己的对象（以及历史遗留的无归属对象）—— 归属记在文件库那一行上，
    OSS 对象本身不带所有者。
    """
    try:
        objects = oss_storage.list_objects(RAW_BUCKET, prefix=prefix, recursive=False)
    except Exception as e:
        # 不再静默吞掉 OSS 异常——返回错误信息让前端/诊断端点能看到真实原因
        logging.getLogger(__name__).error("browse_files OSS list failed: %s", e)
        return {"prefix": prefix, "folders": [], "files": [], "error": f"对象列表读取失败: {e} / OSS list failed: {e}"}

    visible = _visible_object_names(viewer_email, admin)

    # 从文件库建立 object_name → record 的映射
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT id, filename, object_name, sheets_meta FROM {FILE_TABLE}")
            lib_map = {r["object_name"]: dict(r) for r in cur.fetchall()}

    folders, files = [], []
    for obj in objects:
        if obj.is_dir:
            # 只保留仍有可见内容的目录，否则别的用户的目录名会透出来
            if visible is not None and not any(p.startswith(obj.key) for p in visible):
                continue
            name = obj.key.rstrip("/").rsplit("/", 1)[-1]
            folders.append({"type": "folder", "name": name, "path": obj.key})
        else:
            name = obj.key.rsplit("/", 1)[-1]
            if name.startswith(".keep"):
                continue
            if visible is not None and obj.key not in visible:
                continue
            lib = lib_map.get(obj.key, {})
            files.append({
                "type": "file",
                "name": lib.get("filename") or name,
                "path": obj.key,
                "size_bytes": obj.size,
                "last_modified": obj.last_modified.isoformat() if obj.last_modified else None,
                "sheets_meta": lib.get("sheets_meta") or [],
                "file_id": lib.get("id"),
            })

    return {"prefix": prefix, "folders": folders, "files": files}


def list_all_objects(viewer_email: str | None = None, admin: bool = False) -> list:
    """递归列出所有对象，构建完整文件树（供左侧树面板使用）。

    非管理员只看到自己的（或历史遗留的）对象；目录条目只从这些对象派生，
    所以空目录不会残留。
    """
    try:
        objects = oss_storage.list_objects(RAW_BUCKET, recursive=True)
    except Exception as e:
        logging.getLogger(__name__).error("list_all_objects OSS list failed: %s", e)
        raise

    visible = _visible_object_names(viewer_email, admin)

    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT id, filename, object_name, sheets_meta FROM {FILE_TABLE}")
            lib_map = {r["object_name"]: dict(r) for r in cur.fetchall()}

    seen_folders: set = set()
    result = []

    for obj in objects:
        path = obj.key
        parts = path.split("/")

        # Skip .keep placeholder files
        is_marker = parts[-1].startswith(".keep")
        if visible is not None and path not in visible:
            continue

        # Emit intermediate folder entries (deduplicated, top-down)
        for i in range(1, len(parts)):
            folder_path = "/".join(parts[:i]) + "/"
            if folder_path not in seen_folders:
                seen_folders.add(folder_path)
                result.append({
                    "type": "folder",
                    "name": parts[i - 1],
                    "path": folder_path,
                })

        if is_marker:
            continue
        lib = lib_map.get(path, {})
        result.append({
            "type": "file",
            "name": lib.get("filename") or parts[-1],
            "path": path,
            "size_bytes": obj.size,
            "last_modified": obj.last_modified.isoformat() if obj.last_modified else None,
            "sheets_meta": lib.get("sheets_meta") or [],
            "file_id": lib.get("id"),
        })

    return result


def create_folder(path: str, owner_email: str = ""):
    """在 OSS 中创建文件夹（写入 .keep 占位文件）。"""
    prefix = path.strip("/") + "/"
    import hashlib
    key = prefix + ".keep-" + hashlib.sha256(owner_email.encode()).hexdigest()[:16]
    oss_storage.put_object(RAW_BUCKET, key, b"")
    if not _get_library_row_by_object(key):
        _insert_library_row(key, ".keep", 0, [], owner_email)


def register_oss_file(path: str, sheets_meta: list, size_bytes: int,
                      owner_email: str = "") -> dict:
    """将 OSS 上已有文件注册到 _file_library。已存在则返回现有记录（并补齐空字段）。

    owner_email 只在**新建**库行时写入：已存在的行保留原主人，注册动作不会改归属
    （否则任何人按路径注册一下就「接管」了别人的文件）。
    """
    filename = path.rsplit("/", 1)[-1]
    row = _get_library_row_by_object(path)
    if row:
        row = _backfill_library_row(row, sheets_meta, size_bytes)
    else:
        try:
            row = _insert_library_row(path, filename, size_bytes, sheets_meta, owner_email)
        except psycopg2.errors.UniqueViolation:
            # 兜底：对齐序列后仍冲突（并发下的显式 id 插入）。用 MAX(id)+1 直插，
            # 然后重新对齐序列 —— 否则这个滞后会留给下一次普通插入，制造孤儿对象。
            with get_pg_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(f"""
                        INSERT INTO {FILE_TABLE} (id, filename, object_name, size_bytes, sheets_meta, owner_email)
                        VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM {FILE_TABLE}), %s, %s, %s, %s, %s)
                        RETURNING *
                    """, (filename, path, size_bytes, json.dumps(sheets_meta), owner_email or ""))
                    row = dict(cur.fetchone())
                conn.commit()
            try:
                align_file_library_sequence()
            except Exception:
                logger.warning("register_oss_file: aligning id sequence failed", exc_info=True)
    if row.get("uploaded_at"):
        row["uploaded_at"] = row["uploaded_at"].isoformat()
    return row


def _get_library_row_by_object(object_name: str) -> dict | None:
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {FILE_TABLE} WHERE object_name=%s", (object_name,))
            row = cur.fetchone()
    return dict(row) if row else None


def _backfill_library_row(row: dict, sheets_meta: list, size_bytes: int) -> dict:
    """补齐已有行的空字段：按需登记时不解析内容，sheets_meta / size_bytes 可能是空的。

    补上 sheets_meta 很重要 —— 否则先 Publish Link 登记过的文件，
    之后走 Quick Import 会读到一个没有 sheet 列表的记录。
    """
    patch: dict[str, Any] = {}
    if sheets_meta and not (row.get("sheets_meta") or []):
        patch["sheets_meta"] = json.dumps(sheets_meta)
    if size_bytes and not row.get("size_bytes"):
        patch["size_bytes"] = size_bytes
    if not patch:
        return row
    assignments = ", ".join(f"{col}=%s" for col in patch)
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"UPDATE {FILE_TABLE} SET {assignments} WHERE id=%s RETURNING *",
                (*patch.values(), row["id"]),
            )
            updated = cur.fetchone()
        conn.commit()
    return dict(updated) if updated else row


def delete_file_by_path(object_name: str):
    """按 OSS 路径删除文件，同时清理文件库记录。"""
    oss_storage.remove_object(RAW_BUCKET, object_name)
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {FILE_TABLE} WHERE object_name=%s", (object_name,))
        conn.commit()


def delete_folder_by_path(prefix: str, owner_email: str):
    """Delete only this owner's objects, even in a directory used by others."""
    prefix = prefix.rstrip("/") + "/"
    owned = _visible_object_names(owner_email, False) or set()
    for key in owned:
        if key.startswith(prefix):
            delete_file_by_path(key)


def may_manage_folder(prefix: str, owner_email: str) -> bool:
    prefix = prefix.rstrip("/") + "/"
    return any(key.startswith(prefix) for key in (_visible_object_names(owner_email, False) or set()))


def _check_object_destination(source: str, destination: str) -> None:
    if source == destination:
        return
    # Never overwrite a registered object, including one the caller cannot see.
    if _get_library_row_by_object(destination):
        raise ValueError("目标位置已有文件 / a file already exists at the destination")
    if oss_storage.object_exists(RAW_BUCKET, destination):
        raise ValueError("目标位置已有文件 / a file already exists at the destination")


def move_file(from_path: str, to_prefix: str) -> str:
    """把文件移动到另一个目录，返回新路径。"""
    to_prefix = to_prefix.strip("/")
    filename = from_path.rsplit("/", 1)[-1]
    new_path = (to_prefix + "/" if to_prefix else "") + filename
    if new_path == from_path:
        return new_path
    _check_object_destination(from_path, new_path)
    oss_storage.copy_object(RAW_BUCKET, new_path, from_path)
    oss_storage.remove_object(RAW_BUCKET, from_path)
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {FILE_TABLE} SET object_name=%s WHERE object_name=%s",
                        (new_path, from_path))
        conn.commit()
    return new_path


def rename_file(from_path: str, new_name: str) -> str:
    """在同一目录下重命名文件，返回新路径。"""
    last_slash = from_path.rfind("/")
    folder = from_path[:last_slash + 1] if last_slash >= 0 else ""
    new_path = folder + new_name
    if new_path == from_path:
        return new_path
    _check_object_destination(from_path, new_path)
    oss_storage.copy_object(RAW_BUCKET, new_path, from_path)
    oss_storage.remove_object(RAW_BUCKET, from_path)
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {FILE_TABLE} SET object_name=%s, filename=%s WHERE object_name=%s",
                (new_path, new_name, from_path)
            )
        conn.commit()
    return new_path


def list_files(viewer_email: str | None = None, admin: bool = False) -> list[dict]:
    """返回文件库列表，按上传时间倒序；非管理员只看到自己的行。"""
    where, params = _file_filter(viewer_email, admin)
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM {FILE_TABLE} WHERE {where} ORDER BY uploaded_at DESC",
                params,
            )
            rows = [dict(r) for r in cur.fetchall()]
    # ⚠️ A `.keep` row is a FOLDER MARKER, not a document — it is the empty object
    # that makes a folder exist in a store with no real directories, and
    # `browse_files` drops any folder with no visible object under it. Deleting the
    # row would therefore make every EMPTY folder vanish from the wall the moment
    # it is created, so the row has to stay; what must not happen is a person being
    # offered it as a source file.
    #
    # This was the one reader that forgot: `browse_files`, the orphan scan and the
    # tree all skip `.keep`, and this one did not — so "Add table" and "Update" both
    # listed a folder placeholder next to the real files, with no sheets under it.
    # A dropdown whose only entry is `.keep` and whose sheet list is empty reads as
    # a broken dialog, which is exactly how it was reported.
    rows = [r for r in rows
            if not str(r.get("object_name") or "").rsplit("/", 1)[-1].startswith(".keep")]
    for r in rows:
        if r.get("uploaded_at"):
            r["uploaded_at"] = r["uploaded_at"].isoformat()
    return rows


def get_file(file_id: int, viewer_email: str | None = None, admin: bool = False) -> dict | None:
    """按 id 取文件行；`viewer_email` 给了就同时做归属过滤（不可见返回 None）。"""
    where, params = _file_filter(viewer_email, admin)
    if viewer_email is not None:
        where = "(" + where + " OR EXISTS (SELECT 1 FROM public.file_shares s WHERE s.file_id=_file_library.id AND s.grantee_email=%s AND s.shared_by=_file_library.owner_email))"
        params.append(viewer_email)
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM {FILE_TABLE} WHERE id=%s AND {where}",
                [file_id, *params],
            )
            row = cur.fetchone()
    if not row:
        return None
    row = dict(row)
    if row.get("uploaded_at"):
        row["uploaded_at"] = row["uploaded_at"].isoformat()
    return row


def publish_file(file_id: int) -> str:
    """生成或返回已有的 share_token，返回 token 字符串。

    用单条 UPDATE ... COALESCE 保证并发下也只有一个 token（旧实现是
    SELECT 后再 UPDATE，两个并发请求会各生成一个 token 互相覆盖）。
    """
    import secrets
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"UPDATE {FILE_TABLE} SET share_token=COALESCE(share_token, %s) "
                f"WHERE id=%s RETURNING share_token",
                (secrets.token_urlsafe(24), file_id),
            )
            row = cur.fetchone()
        conn.commit()
    if not row:
        raise ValueError("文件不存在 / file not found")
    return row["share_token"]


def publish_file_by_path(path: str, owner_email: str = "") -> dict:
    """按 OSS 路径发布文件，返回 {token, file_id, object_name}。幂等。

    文件库里没有对应行时按需登记，不再要求「上传时登记成功」才能发布：
    写进 datacenter-raw 的路径不止 stage_file 一条（Smart Import 的源文件、
    数据库快照 zip、AI 生成文档等），它们都可能只有对象、没有库行。
    `owner_email` 是发布者 —— 按需登记时写入归属，避免新建的行无主。
    """
    if not oss_storage.object_exists(RAW_BUCKET, path):
        raise FileNotFoundError(path)
    size_bytes = 0
    try:
        size_bytes = oss_storage.object_size(RAW_BUCKET, path)
    except Exception:
        logger.warning("publish_file_by_path: could not stat %s", path, exc_info=True)
    record = register_oss_file(path, [], size_bytes, owner_email=owner_email)
    token = publish_file(record["id"])
    return {"token": token, "file_id": record["id"], "object_name": record["object_name"]}


def list_orphan_objects() -> dict:
    """列出 OSS 上有对象、但 _file_library 里没有对应行的文件。只读。

    这类文件以前无法使用任何依赖 file_id 的功能（Publish Link 报
    「无法获取文件 ID」）。现在发布走 publish_file_by_path 会按需登记，
    但这里仍然保留自检入口，便于发现其它写路径漏登记的情况。
    """
    try:
        objects = oss_storage.list_objects(RAW_BUCKET, recursive=True)
    except Exception as e:
        logging.getLogger(__name__).error("list_orphan_objects OSS list failed: %s", e)
        return {"count": 0, "files": [], "error": f"对象列表读取失败: {e} / OSS list failed: {e}"}
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT object_name FROM {FILE_TABLE}")
            known = {r[0] for r in cur.fetchall()}
    orphans = [
        {
            "path": obj.key,
            "size_bytes": obj.size,
            "last_modified": obj.last_modified.isoformat() if obj.last_modified else None,
        }
        for obj in objects
        if not obj.is_dir
        and obj.key.rsplit("/", 1)[-1] != ".keep"
        and obj.key not in known
    ]
    orphans.sort(key=lambda o: o["path"])
    return {"count": len(orphans), "files": orphans}


def get_file_by_token(token: str) -> dict | None:
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM {FILE_TABLE} WHERE share_token=%s",
                (token,),
            )
            row = cur.fetchone()
    if not row:
        return None
    row = dict(row)
    if row.get("uploaded_at"):
        row["uploaded_at"] = row["uploaded_at"].isoformat()
    return row


def delete_file(file_id: int):
    """从文件库和 MinIO 删除文件。"""
    row = get_file(file_id)
    if not row:
        return
    oss_storage.remove_object(RAW_BUCKET, row["object_name"])
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {FILE_TABLE} WHERE id=%s", (file_id,))
        conn.commit()


# ── 查询辅助 ──────────────────────────────────────────────────────────────────

def _table_exists_pg(cur, table_name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s",
        (table_name,)
    )
    return cur.fetchone() is not None


def find_by_fingerprint(fingerprint: str, viewer_email: str | None = None,
                        admin: bool = False) -> dict | None:
    where, params = _owner_filter(viewer_email, admin)
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM {META_TABLE} WHERE fingerprint=%s AND {where} LIMIT 1",
                [fingerprint, *params],
            )
            row = cur.fetchone()
            if not row:
                return None
            row = dict(row)
            # verify physical table still exists
            if not _table_exists_pg(cur, row["table_name"]):
                cur.execute(f"DELETE FROM {META_TABLE} WHERE table_name=%s", (row["table_name"],))
                conn.commit()
                return None
            row["target_db"] = "pg"
            row["table_schema"] = PUBLIC_SCHEMA
            return row


def list_datasets(viewer_email: str | None = None, admin: bool = False) -> list[dict]:
    """Datasets the caller may see, newest first.

    Rows whose backing table is gone are filtered out here (not only pruned at
    startup) so a stale registry can never render a ghost card.

    ⚠️ `source_file_path` is selected because it is what files a dataset into a
    folder: it is the object key of the file the dataset was imported from, which is
    the same key space the folder tree is built from, so the client can group a
    dataset under a folder without guessing from `source_file` (a display filename,
    which is not unique and is not a path).
    """
    where, params = _owner_filter(viewer_email, admin)
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT table_name, display_name, target_db, row_count,
                       source_file, source_file_path, sheet_name, modules, description,
                       created_at, updated_at, update_count, columns, owner_email
                FROM {META_TABLE}
                WHERE {where}
                  AND to_regclass('public.' || table_name) IS NOT NULL
                ORDER BY updated_at DESC
            """, params)
            rows = [dict(r) for r in cur.fetchall()]
            for row in rows:
                row["target_db"] = "pg"
                row["table_schema"] = PUBLIC_SCHEMA
        return rows


def get_dataset(table_name: str, viewer_email: str | None = None,
                admin: bool = False) -> dict | None:
    where, params = _owner_filter(viewer_email, admin)
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM {META_TABLE} WHERE table_name=%s AND {where}",
                [table_name, *params],
            )
            row = cur.fetchone()
            if not row:
                return None
            result = dict(row)
            result["target_db"] = "pg"
            result["table_schema"] = PUBLIC_SCHEMA
            return result


def _safe_id(name: str) -> str:
    return re.sub(r"[^\w]", "_", name).lower()


# ── PostgreSQL 表操作 ─────────────────────────────────────────────────────────

def drop_pg_table_if_exists(table_name: str) -> None:
    """Drop a public table and its _import_registry entry."""
    safe_name = _safe_id(table_name)
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                    sql.Identifier(PUBLIC_SCHEMA, safe_name)
                )
            )
            cur.execute(
                f"DELETE FROM {META_TABLE} WHERE table_name = %s",
                (safe_name,),
            )
        conn.commit()


# How far a stored value can survive being re-typed. Used only to decide whether a
# re-import may silently change a column, or has to stop and say so.
#
# ⚠️ This ordering is what makes re-importing a corrected file safe. The importer infers
# column types from the *values it happens to see*: a column that is all whole numbers
# imports as BIGINT, the same column with one decimal imports as DOUBLE PRECISION.
# `CREATE TABLE IF NOT EXISTS` does not touch an existing table, so without this
# reconciliation a corrected re-import lands in the old column and the decimals are
# **truncated with a 200 back** — the caller is told the file imported and the numbers
# are quietly wrong. Reproduced end to end: a `pct` column first imported as [-20, -22]
# (BIGINT) and re-imported as [-20.44, -22.31] came back as [-20, -22].
_TYPE_RANK = {
    "BOOLEAN": 1,
    "BIGINT": 2,
    "DOUBLE PRECISION": 3,
    "NUMERIC": 3,
    "TEXT": 4,
}
# Types the importer emits that mean the same thing as each other.
_TYPE_EQUIV = {("DOUBLE PRECISION", "NUMERIC")}


class DatasetSchemaError(RuntimeError):
    """The re-import would change a dataset's schema in a way that loses data.

    Raised before anything is written, so the existing dataset is untouched. Routers
    turn this into 400: it is a fact about the caller's two files, not a server fault.
    """


def reconcile_column_types(table_name: str, columns: list[tuple[str, str]], owner_email: str | None = None, *, _created_copy: bool = False) -> None:
    """Widen (or refuse to narrow) an existing dataset table's columns before an import.

    Called *before* the CREATE TABLE, so a first import finds no table and returns
    immediately. On a re-import the table exists, and the caller is about to write into
    the columns it already has.
    """
    wanted = {c: t for c, t in columns}
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            _check_import_owner(cur, table_name, owner_email, creating=True, created_copy=_created_copy)
            cur.execute("SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema=%s AND table_name=%s", (PUBLIC_SCHEMA, table_name))
            if cur.fetchone() is None:
                return
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s",
                (PUBLIC_SCHEMA, table_name),
            )
            # ⚠️ `information_schema.columns.data_type` answers in **lower case**
            # ("bigint", "double precision", "text") while the importer emits upper case
            # ("BIGINT", "DOUBLE PRECISION"). Comparing them raw makes every lookup in
            # _TYPE_RANK miss, the function silently does nothing, and the truncation this
            # exists to prevent comes back — looking exactly like the bug never being fixed.
            existing = {c: t.upper() for c, t in cur.fetchall()}

            widened, refused = [], []
            for col, want in wanted.items():
                have = existing.get(col)
                if have is None or have == want:
                    continue
                if (have, want) in _TYPE_EQUIV or (want, have) in _TYPE_EQUIV:
                    continue
                have_rank = _TYPE_RANK.get(have)
                want_rank = _TYPE_RANK.get(want)
                if have_rank is None or want_rank is None:
                    continue          # a type this importer never emits: leave it alone
                if want_rank > have_rank:
                    widened.append((col, have, want))
                else:
                    refused.append((col, have, want))

            for col, have, want in widened:
                cur.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN {} TYPE {}").format(
                    sql.Identifier(PUBLIC_SCHEMA, table_name),
                    sql.Identifier(col), sql.SQL(want)))
            if refused:
                # Narrowing is the caller's decision, not ours: it can lose values, and
                # the only honest way to do it is to delete the dataset and re-import.
                conn.rollback()
                detail = "; ".join(f"{c} {h} → {w}" for c, h, w in refused)
                raise DatasetSchemaError(
                    f"cannot narrow column(s) in an existing dataset without losing data: {detail}. "
                    f"Delete the dataset and import again if the narrower type is what you want."
                )
        conn.commit()


def _check_import_owner(cur, table_name: str, owner_email: str | None, *, creating=False, created_copy=False):
    if owner_email is None:
        return  # trusted internal maintenance only
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"smart_import:public.{table_name}",))
    cur.execute(f"SELECT owner_email FROM {META_TABLE} WHERE table_name=%s", (table_name,))
    row = cur.fetchone()
    if row:
        if row[0] != owner_email or not owner_email:
            raise DatasetSchemaError("目标数据集不属于你 / Target dataset is not yours")
    elif creating and not created_copy:
        cur.execute("SELECT to_regclass(%s)", ("public." + table_name,))
        if cur.fetchone()[0] is not None:
            raise DatasetSchemaError("目标名称已被占用 / Target name is already reserved")
    elif not creating:
        raise DatasetSchemaError("数据集不存在 / Dataset not found")


def create_pg_table(
    table_name: str,
    columns: list[tuple[str, str]],
    fingerprint: str,
    display_name: str,
    source_file: str,
    sheet_name: str,
    modules: list[str],
    source_file_path: str | None = None,
    owner_email: str = "",
    *, _created_copy: bool = False,
) -> str:
    """Create/replace a dataset table and register it, stamping `owner_email`.

    归属只在**新建**注册行时写：`ON CONFLICT` 分支刻意不更新 owner_email，
    已存在的行保留原主人 —— 否则任何一次导入都会把表「接管」到自己名下。
    """
    # _created_copy is reserved for the trusted pull service after its successful
    # CREATE TABLE AS SELECT; HTTP import requests never accept this flag.
    safe_name = _safe_id(table_name)
    reconcile_column_types(safe_name, columns, owner_email=owner_email or None, _created_copy=_created_copy)
    col_defs = sql.SQL(", ").join(
        sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(t)) for c, t in columns
    )
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            if owner_email:
                _check_import_owner(cur, safe_name, owner_email, creating=True, created_copy=_created_copy)
            cur.execute(
                sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
                    sql.Identifier(PUBLIC_SCHEMA, safe_name), col_defs,
                )
            )
            cur.execute(f"""
                INSERT INTO {META_TABLE}
                  (table_name, display_name, target_db, fingerprint, columns,
                   row_count, source_file, sheet_name, modules, source_file_path, owner_email)
                VALUES (%s,%s,'pg',%s,%s,0,%s,%s,%s,%s,%s)
                ON CONFLICT (table_name) DO UPDATE SET
                  fingerprint=EXCLUDED.fingerprint,
                  columns=EXCLUDED.columns,
                  source_file=EXCLUDED.source_file,
                  source_file_path=EXCLUDED.source_file_path,
                  updated_at=NOW()
            """, (
                safe_name, display_name, fingerprint,
                json.dumps(columns), source_file, sheet_name,
                modules, source_file_path, owner_email or "",
            ))
        conn.commit()
    return safe_name


def insert_pg_data(table_name: str, records: list[dict], mode: str = "replace", owner_email: str | None = None) -> int:
    """
    Write an import into a public table and return the table's new row count.

    This is the one place a dataset's contents change, so it is also the one
    place the Inbox hears about it: a re-import is a fact the dataset's readers
    will otherwise only discover by noticing a number moved.
    """
    previous = _dataset_row_count(table_name)
    total = _insert_pg_data(table_name, records, mode, owner_email)
    if table_name in _INBOX_QUIET_TABLES:
        return total
    _announce_dataset_update(table_name, previous, total, mode)
    return total


def _dataset_row_count(table_name: str) -> int | None:
    """Rows the registry believed were there before this import (None = unknown)."""
    try:
        row = get_dataset(table_name)
        return int(row["row_count"]) if row and row.get("row_count") is not None else None
    except Exception:                      # noqa: BLE001 — the import must not depend on this
        return None


def _announce_dataset_update(table_name: str, previous: int | None, total: int, mode: str) -> None:
    """Broadcast one dataset update. Never raises — see `services/inbox.py`."""
    try:
        row = get_dataset(table_name) or {}
        name = row.get("display_name") or table_name
        if previous is None:
            delta = f"现有 {total:,} 行"
        elif previous == total:
            delta = f"行数未变（{total:,} 行）"
        else:
            delta = f"{previous:,} → {total:,} 行（{total - previous:+,}）"
        from services import dataset_shares
        recipients = {row.get("owner_email")} | {r["grantee_email"] for r in dataset_shares.shares_of(table_name)}
        for recipient in recipients - {None, ""}:
            inbox.emit(kind="dataset", actor_email="", recipient_email=recipient,
                   target_type="dataset", target_slug=table_name, target_title=name,
                   target_url=annotations.doc_url("dataset", table_name),
                   summary=f"数据集 {name}（{table_name}）已{'覆盖' if mode == 'replace' else '追加'}导入：{delta}")
    except Exception as exc:               # noqa: BLE001
        logger.warning("dataset inbox notice failed (%s): %s", table_name, exc)


# Imports whose side effects already announce themselves, or that run on every
# page load — a line in everyone's Inbox for each would be noise, not signal.
_INBOX_QUIET_TABLES: set[str] = set()


def _insert_pg_data(table_name: str, records: list[dict], mode: str = "replace", owner_email: str | None = None) -> int:
    if not records:
        if mode == "replace":
            with get_pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"smart_import:public.{table_name}",))
                    _check_import_owner(cur, table_name, owner_email)
                    cur.execute(sql.SQL("TRUNCATE TABLE {}").format(sql.Identifier(PUBLIC_SCHEMA, table_name)))
                    cur.execute(
                        f"UPDATE {META_TABLE} SET row_count=0, updated_at=NOW(), "
                        f"update_count=update_count+1 WHERE table_name=%s",
                        (table_name,),
                    )
                conn.commit()
        return 0
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            # Serialize imports targeting the same public table. This prevents
            # concurrent retries from interleaving TRUNCATE/INSERT operations.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"smart_import:public.{table_name}",))
            _check_import_owner(cur, table_name, owner_email)
            cols = list(records[0].keys())
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = %s
                """,
                (table_name,),
            )
            existing_cols = {row[0] for row in cur.fetchall()}
            if not existing_cols:
                raise ValueError(f"Smart Import 目标表 public.{table_name} 不存在 / Smart Import target public.{table_name} does not exist")
            unexpected_cols = [col for col in cols if col not in existing_cols]
            if unexpected_cols:
                names = ", ".join(unexpected_cols)
                raise ValueError(
                    f"导入被拒绝：public.{table_name} 不包含字段 {names}。"
                    "Smart Import 不会自动扩展表结构；请先通过受控数据库迁移添加字段，或在导入前移除这些列。"
                    f" / Import rejected: public.{table_name} has no column {names}. "
                    "Smart Import never extends the schema; add the columns via a controlled "
                    "migration, or drop them from the import."
                )
            # Validate schema before a replace-mode TRUNCATE so an invalid
            # workbook can never empty an existing dataset.
            if mode == "replace":
                cur.execute(sql.SQL("TRUNCATE TABLE {}").format(sql.Identifier(PUBLIC_SCHEMA, table_name)))
            col_sql = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
            # Preserve text sentinels. Invalid numeric casts fail and roll back.
            values = [[r.get(c) for c in cols] for r in records]
            psycopg2.extras.execute_values(
                cur,
                sql.SQL("INSERT INTO {} ({}) VALUES %s").format(
                    sql.Identifier(PUBLIC_SCHEMA, table_name), col_sql,
                ).as_string(cur),
                values,
                page_size=5000,
            )
            # update row count with actual total (handles append correctly)
            cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(PUBLIC_SCHEMA, table_name)))
            total = cur.fetchone()[0]
            cur.execute(
                f"UPDATE {META_TABLE} SET row_count=%s, updated_at=NOW(), "
                f"update_count=update_count+1 WHERE table_name=%s",
                (total, table_name)
            )
        conn.commit()
    return total


def delete_pg_table(table_name: str):
    # Grants live here rather than in the router so the cleanup cannot be skipped
    # by a future delete path: a leftover grant on a dropped table would hand the
    # old grantees access again the day some later upload reuses the name.
    from services import dataset_shares

    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(PUBLIC_SCHEMA, table_name)))
            cur.execute(f"DELETE FROM {META_TABLE} WHERE table_name=%s", (table_name,))
        conn.commit()
    dataset_shares.revoke_all_for(table_name)


def query_pg(sql_text: str, max_rows: int | None = None) -> dict:
    """
    Run one statement and return its rows.

    ⚠️ **Read-only is enforced by PostgreSQL, not by the caller's text check.** The first
    statement of the transaction is `SET TRANSACTION READ ONLY`, so `WITH … SELECT` can be
    accepted (CTEs are read-only SQL; allowed 2026-09-27) without opening a write path —
    a data-modifying CTE like `WITH x AS (DELETE …) SELECT …` fails at the database
    instead of relying on someone having enumerated every dangerous keyword.
    Callers still validate the statement shape for a readable error; this is the barrier
    that actually holds.

    `max_rows` caps the returned rows. It exists because the docs have promised it all
    along (agents have been sending `"max_rows": 2000` into a model that silently dropped
    the field): one extra row is fetched to report `truncated` honestly rather than
    guessing from `len(rows) == max_rows`.
    """
    cap = int(max_rows) if max_rows and int(max_rows) > 0 else None
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL search_path TO pg_catalog, public")
            cur.execute(sql_text)
            rows = cur.fetchmany(cap + 1) if cap else cur.fetchall()
            truncated = bool(cap and len(rows) > cap)
            rows = rows[:cap] if cap else rows
            out = [dict(r) for r in rows]
            columns = [desc[0] for desc in cur.description] if cur.description else []
            return {"rows": out, "columns": columns, "count": len(out), "truncated": truncated}


def preview_pg(table_name: str, limit: int = 100) -> dict:
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                sql.SQL("SELECT * FROM {} LIMIT %s").format(sql.Identifier(PUBLIC_SCHEMA, table_name)),
                (min(limit, 1000),),
            )
            rows = [dict(r) for r in cur.fetchall()]
            columns = [desc[0] for desc in cur.description] if cur.description else []
    return {"rows": rows, "columns": columns, "count": len(rows)}


# ── 通用 metadata 更新 ────────────────────────────────────────────────────────

def delete_period_pg(table_name: str, col: str, period_val: str) -> int:
    """Delete rows where the period col matches the given year-month (PostgreSQL)."""
    ym = period_val[:7]  # e.g. "2025-01"
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE TO_CHAR({}::date, 'YYYY-MM') = %s").format(
                    sql.Identifier(PUBLIC_SCHEMA, table_name), sql.Identifier(col),
                ),
                (ym,),
            )
            count = cur.rowcount
            # Update row_count in registry to reflect actual remaining rows
            cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(PUBLIC_SCHEMA, table_name)))
            new_total = cur.fetchone()[0]
            cur.execute(
                f"UPDATE {META_TABLE} SET row_count=%s, updated_at=NOW() WHERE table_name=%s",
                (new_total, table_name)
            )
        conn.commit()
    return count


def period_stats_pg(table_name: str, col: str) -> dict:
    """Return grouped period counts using safely quoted public identifiers."""
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            identifier = sql.Identifier(col)
            cur.execute(
                sql.SQL("SELECT {} AS period, COUNT(*) AS count FROM {} GROUP BY {} ORDER BY {}").format(
                    identifier,
                    sql.Identifier(PUBLIC_SCHEMA, table_name),
                    identifier,
                    identifier,
                )
            )
            rows = [dict(row) for row in cur.fetchall()]
    return {"rows": rows, "columns": ["period", "count"], "count": len(rows)}



def update_modules(table_name: str, modules: list[str]):
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {META_TABLE} SET modules=%s, updated_at=NOW() WHERE table_name=%s",
                (modules, table_name)
            )
        conn.commit()


def update_source_file_path(table_name: str, path: str):
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {META_TABLE} SET source_file_path=%s, updated_at=NOW() WHERE table_name=%s",
                (path, table_name)
            )
        conn.commit()


def update_description(table_name: str, description: str):
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {META_TABLE} SET description=%s, updated_at=NOW() WHERE table_name=%s",
                (description, table_name)
            )
        conn.commit()


def update_display_name(table_name: str, display_name: str):
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {META_TABLE} SET display_name=%s, updated_at=NOW() WHERE table_name=%s",
                (display_name, table_name)
            )
        conn.commit()


# ─── 首页 Dashboard KPI 数据 ──────────────────────────────────────────────────


