"""
Startup database bootstrap checks.

The production container may point at an already-created RDS/PostgreSQL
database. On startup we verify that the expected schema objects exist; when
they do not, we apply the bundled initialization SQL once.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg2

from core.db import connect_main


_REQUIRED_TABLES = (
    "public._import_registry",
    "public.app_settings",
)

_BOOTSTRAP_MARKER_KEY = "db_bootstrap.initialized"

_IGNORED_SQLSTATES = {
    "42710",  # duplicate_object
    "42701",  # duplicate_column
    "42P06",  # duplicate_schema
    "42P07",  # duplicate_table
}


def _is_ignorable_privilege_statement(statement: str) -> bool:
    normalized = " ".join(statement.lower().split())
    sequence_owner_statement = (
        "alter sequence " in normalized
        and (" owned by " in normalized or " owner to " in normalized)
    )
    return sequence_owner_statement


def _console(message: str) -> None:
    print(message, flush=True)


def _sql_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts"


def _strip_psql_meta(sql: str) -> str:
    lines = []
    for line in sql.splitlines():
        if line.lstrip().startswith("\\"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _split_sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    quote_char = ""
    dollar_tag = ""
    line_comment = False
    block_comment = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""

        if line_comment:
            current.append(ch)
            if ch == "\n":
                line_comment = False
            i += 1
            continue

        if block_comment:
            current.append(ch)
            if ch == "*" and nxt == "/":
                current.append(nxt)
                block_comment = False
                i += 2
            else:
                i += 1
            continue

        if quote_char:
            current.append(ch)
            if ch == quote_char:
                if nxt == quote_char:
                    current.append(nxt)
                    i += 2
                    continue
                quote_char = ""
            i += 1
            continue

        if dollar_tag:
            if sql.startswith(dollar_tag, i):
                current.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = ""
            else:
                current.append(ch)
                i += 1
            continue

        if ch == "-" and nxt == "-":
            current.append(ch)
            current.append(nxt)
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            current.append(ch)
            current.append(nxt)
            block_comment = True
            i += 2
            continue
        if ch in ("'", '"'):
            current.append(ch)
            quote_char = ch
            i += 1
            continue
        if ch == "$":
            j = i + 1
            while j < len(sql) and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            if j < len(sql) and sql[j] == "$":
                dollar_tag = sql[i:j + 1]
                current.append(dollar_tag)
                i = j + 1
                continue
        if ch == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            i += 1
            continue

        current.append(ch)
        i += 1

    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


def _missing_tables(conn, table_names: tuple[str, ...]) -> list[str]:
    with conn.cursor() as cur:
        missing = []
        for table_name in table_names:
            cur.execute("SELECT to_regclass(%s)", (table_name,))
            if cur.fetchone()[0] is None:
                missing.append(table_name)
        return missing


def _bootstrap_marker_exists(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", ("public.app_settings",))
        if cur.fetchone()[0] is None:
            return False
        cur.execute("SELECT 1 FROM public.app_settings WHERE key = %s", (_BOOTSTRAP_MARKER_KEY,))
        return cur.fetchone() is not None


def _mark_bootstrap_initialized(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.app_settings (key, value, updated_at)
            VALUES (%s, '{"status":"initialized"}'::jsonb, NOW())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_at = NOW()
            """,
            (_BOOTSTRAP_MARKER_KEY,),
        )


def _execute_sql_file(conn, path: Path) -> tuple[int, int]:
    sql = _strip_psql_meta(path.read_text(encoding="utf-8"))
    executed = 0
    skipped = 0
    for statement in _split_sql_statements(sql):
        try:
            with conn.cursor() as cur:
                cur.execute(statement)
            executed += 1
        except psycopg2.Error as exc:
            duplicate_primary_key = (
                exc.pgcode == "42P16"
                and "multiple primary keys" in str(exc).lower()
            )
            ignorable_privilege = (
                exc.pgcode == "42501"
                and _is_ignorable_privilege_statement(statement)
            )
            if exc.pgcode in _IGNORED_SQLSTATES or duplicate_primary_key or ignorable_privilege:
                # A duplicate-object error leaves the current transaction in
                # an aborted state on PostgreSQL. Clear it before continuing
                # with the remaining idempotent bootstrap statements.
                conn.rollback()
                skipped += 1
                continue
            raise RuntimeError(f"{path.name} failed near: {statement[:160]}") from exc
    return executed, skipped


def ensure_database_initialized() -> None:
    """Check required tables and apply bundled initialization SQL if needed."""
    if os.environ.get("SKIP_DB_BOOTSTRAP", "").lower() in {"1", "true", "yes"}:
        _console("Klado DB bootstrap: skipped by SKIP_DB_BOOTSTRAP")
        return

    script_dir = _sql_dir()
    init_sql = script_dir / "init_public.sql"

    _console("Klado DB bootstrap: checking required tables")
    conn = connect_main()
    try:
        conn.autocommit = True
        missing = _missing_tables(conn, _REQUIRED_TABLES)
        marker_exists = _bootstrap_marker_exists(conn)
        applied_baseline = False
        if missing:
            if not init_sql.is_file():
                raise RuntimeError(f"DB bootstrap SQL not found: {init_sql}")
            _console(f"Klado DB bootstrap: missing tables: {', '.join(missing)}")
            executed, skipped = _execute_sql_file(conn, init_sql)
            _console(
                f"Klado DB bootstrap: applied {init_sql.name} "
                f"(executed={executed}, skipped_existing={skipped})"
            )
            _mark_bootstrap_initialized(conn)
            applied_baseline = True
        elif not marker_exists:
            _mark_bootstrap_initialized(conn)
            _console("Klado DB bootstrap: core tables exist; initialization marker created")
        else:
            _console("Klado DB bootstrap: core tables already initialized")
        if not applied_baseline:
            if not init_sql.is_file():
                raise RuntimeError(f"DB bootstrap SQL not found: {init_sql}")
            executed, skipped = _execute_sql_file(conn, init_sql)
            _console(
                f"Klado DB bootstrap: schema baseline replayed from {init_sql.name} "
                f"(executed={executed}, skipped_existing={skipped})"
            )
        # ── Schema migrations ───────────────────────────────────────────────
        _console("Klado DB bootstrap: checking schema migrations")
        # Retired-feature tables: the competitor/scraper feature, the
        # jd/tmall e-commerce imports and the JD/Tmall price monitor were
        # retired (2026-09-08). All tables were already empty; drop them and
        # remove any registry leftovers on every startup until gone.
        cleanup_cur = conn.cursor()
        try:
            retired = (
                "public.competitor_products",
                "public.competition_settings",
                "public.jd",
                "public.tmall",
                # child table first: monitor_price_history.target_id has an FK
                # referencing monitor_targets.id
                "public.monitor_price_history",
                "public.monitor_targets",
            )
            for legacy in retired:
                cleanup_cur.execute("SELECT to_regclass(%s)", (legacy,))
                if cleanup_cur.fetchone()[0] is not None:
                    cleanup_cur.execute(f"DROP TABLE {legacy}")
                    _console(f"  → removed retired table {legacy}")
            cleanup_cur.execute(
                "DELETE FROM public._import_registry WHERE table_name IN %s",
                (("competitor_products", "competition_settings", "jd", "tmall",
                  "monitor_targets", "monitor_price_history"),),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            _console(f"  → competition tables cleanup skipped: {e}")
        finally:
            cleanup_cur.close()
        # Retired-feature tables: the embedded AI chat assistant was
        # retired (2026-09-22). Its runtime state tables are no longer part of
        # _REQUIRED_TABLES and no code reads them, so drop them on every
        # startup until gone. No FK dependencies between the three.
        cleanup_cur = conn.cursor()
        try:
            retired_ai = (
                "public.ai_background_tasks",
                "public.ai_conversations",
                "public.ai_memory_items",
            )
            for legacy in retired_ai:
                cleanup_cur.execute("SELECT to_regclass(%s)", (legacy,))
                if cleanup_cur.fetchone()[0] is not None:
                    cleanup_cur.execute(f"DROP TABLE {legacy}")
                    _console(f"  → removed retired table {legacy}")
            conn.commit()
        except Exception as e:
            conn.rollback()
            _console(f"  → AI runtime tables cleanup skipped: {e}")
        finally:
            cleanup_cur.close()
        # Legacy leftovers (2026-09-22 DB audit). Two groups:
        #   * ytd08_probe4/5 — scratch tables created by a past Data Center import
        #     and registered in _import_registry, which surfaced them as importable
        #     datasets in both environments.
        #   * docs_chunks / auth_users / system_ai_logs / sf_applied_blocks /
        #     sf_channel_forecasts — legacy objects carried in from the old `system`
        #     schema dump, empty and referenced by no code.
        # Their DDL has been removed from every init/replay SQL copy, so the replay
        # can no longer recreate them.
        #   * the two FK constraints below live on tables that SURVIVE, so they are
        #     dropped explicitly before auth_users goes away.
        #   * sf_applied_blocks must go before sf_channel_forecasts (it holds the FK).
        # Drop the table and PostgreSQL removes its OWNED BY sequences and its own
        # constraints automatically.
        cleanup_cur = conn.cursor()
        try:
            for owner, constraint in (
                ("docs_documents", "documents_uploaded_by_fkey"),
                ("system_import_jobs", "import_jobs_created_by_fkey"),
            ):
                cleanup_cur.execute(
                    f"ALTER TABLE public.{owner} DROP CONSTRAINT IF EXISTS {constraint}"
                )
            for legacy in (
                "public.docs_chunks",
                "public.sf_applied_blocks",
                "public.sf_channel_forecasts",
                "public.system_ai_logs",
                "public.auth_users",
                "public.ytd08_probe4",
                "public.ytd08_probe5",
                # Second knowledge store, retired 2026-09-23. It was seeded once
                # by seed_from_agent_if_empty() and never updated afterwards, so
                # it drifted into a stale 25-document duplicate of
                # public.ai_knowledge_documents while every published contract
                # pointed at /api/ai/knowledge/*. Full backup (both
                # environments, 25 rows each) lives with the retirement notes.
                "public.chatgpt_knowledge_documents",
                # Sales-channel lookup, retired 2026-10-01 with the rest of the
                # channel concept (the Calendar channel picker, the
                # `GET /api/settings/channel-mapping` endpoint and the event
                # `channels` field). Nothing reads it, and its DDL is gone from
                # every init/replay SQL copy, so the replay can no longer
                # recreate it. Drop any copy a database still carries.
                "public.channel_mapping",
                # Data Center "Documents" module, retired 2026-10-01 (upload +
                # OCR extraction + the extracted-text library). The router
                # (`/api/ocr`), its `services/ocr_db` access layer and the
                # `document_library` knowledge source are gone; no code reads
                # either table. `docs_documents` is dropped after the
                # `documents_uploaded_by_fkey` constraint (detached in the ALTER
                # loop above) is gone; that constraint pointed at `auth_users`,
                # which also goes away in this block. ⚠️ Their DDL still lives in
                # scripts/init_public.sql (and the init_db/reinitialize copies),
                # so the replay recreates them each startup before this cleanup
                # removes them again; removing that DDL is a follow-up owned
                # outside this change.
                "public.docs_documents",
                "public._ocr_jobs",
            ):
                cleanup_cur.execute("SELECT to_regclass(%s)", (legacy,))
                if cleanup_cur.fetchone()[0] is not None:
                    cleanup_cur.execute(f"DROP TABLE {legacy}")
                    _console(f"  → removed retired table {legacy}")
            cleanup_cur.execute(
                "DELETE FROM public._import_registry WHERE table_name IN %s",
                (("ytd08_probe4", "ytd08_probe5"),),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            _console(f"  → legacy leftovers cleanup skipped: {e}")
        finally:
            cleanup_cur.close()
        _console("  → schema migrations verified")
    finally:
        conn.close()