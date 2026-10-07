#!/usr/bin/env python
"""Move every HTML Workspace item into the Dashboard module (2026-10-05).

**Dry-run by default. `--apply` is required to write anything.**

Why this exists
---------------
HTML used to land in Workspace (`ai_reports`) and only Workspace. The Dashboard module
(`ai_dashboards`) was for pages that query their own datasets. The product decision is
now that **Dashboard is where HTML lands**, so the six production pages have to move.

What moves and what does NOT
----------------------------
Only rows with real HTML. A Workspace item with `doc_type` set is an Office file
(xlsx / pptx / docx / pdf) uploaded through `POST /api/reports/documents`; those stay,
because `ai_dashboards` has no column that can hold a binary and there is nowhere to
put them. `html = ''` is the same population seen from the other side.

What is carried across
----------------------
`html`, `summary` / `summary_zh` / `summary_en`, `langs`, `tags`, `accent`, `kind`,
`filters_json`, `state_json`, the seven cover columns, `created_at` / `updated_at`,
`views`, and `owner_email`.

What is deliberately NOT carried
--------------------------------
`project_slug` / `folder_id` — the Dashboard wall has no projects or folders. `pinned`
is a per-wall preference and each wall keeps its own. `source_report_id` and `doc_*`
have no Dashboard counterpart.

⚠️ The slug is kept. `/r/{slug}` and `/d/{slug}` are different addresses, so a reader
who bookmarked the old one lands on a 404 — that is the honest outcome, and the reason
this prints the slug list before it does anything.

Re-running is safe: a slug already present in `ai_dashboards` is reported and skipped
unless `--force`, which overwrites the dashboard row from the report row.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "api"))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

# The seven cover columns, named because the copy below is an explicit list and an
# explicit list is exactly where a newly added column gets forgotten.
COVER_COLS = ("cover_data", "cover_mime", "cover_w", "cover_h",
              "cover_prompt", "cover_model", "cover_url")

# report column -> dashboard column. Same name on both sides unless listed here.
# ⚠️ `slug` and `title` lead the list because they are the two NOT NULL columns a page
# cannot do without, and they are the page's ADDRESS. They were left out of the first
# version of this list, which produced a NOT NULL violation naming `slug` on the first
# real run — after a dry-run that had printed a clean plan for all 21 rows.
# ⚠️ `filters_json` is on this list because leaving it off cost a production migration
# (2026-10-05). The two `dynamic` pages arrived with `kind='dynamic'` and an EMPTY
# `filters_json` — a card claiming a filter rail its document does not have, which is
# the "lying about itself" case `_dashboard_kind`'s own docstring warns about. It went
# unnoticed because the row LOOKED migrated: the kind came across, the schema did not,
# and nothing anywhere compares the two.
SAME_NAME = ("slug", "title", "html", "summary", "summary_zh", "summary_en", "langs",
             "tags", "accent", "kind", "filters_json", "submitter", "state_json",
             "state_updated_at", "owner_email", "size_bytes", "views", "created_at",
             "updated_at")


def connect():
    from core.db import connect_main

    return connect_main()


def plan(conn, force: bool):
    """What would move, what would not, and what would collide. Read-only.

    ⚠️ Selects the CARRIED columns as well as the summary ones. The first version read
    only `(slug, kind, doc_type, html_len)` for the preview and then re-read nothing in
    `apply`, so the write path asked a preview row for `html` and died with a KeyError
    — a dry-run that printed a clean plan for a migration that could not run. The preview
    and the writer now read the SAME row, which is the only way a dry-run can promise
    the apply will do what it said.
    """
    carried = list(SAME_NAME) + [c for c in COVER_COLS if c not in SAME_NAME]
    cols = "r.id, r.slug, r.title, r.kind, r.doc_type, r.source_report_id, " + \
           ", ".join("r.%s" % c for c in carried)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            SELECT {cols}, length(coalesce(r.html,'')) AS html_len,
                   (d.id IS NOT NULL) AS already_there
            FROM ai_reports r
            LEFT JOIN ai_dashboards d ON d.slug = r.slug
            ORDER BY r.id
        """)
        rows = [dict(r) for r in cur.fetchall()]
    move, keep, collide = [], [], []
    for row in rows:
        if row["doc_type"] or not row["html_len"]:
            keep.append(row)
        elif row["already_there"]:
            (collide if force else move).append(row)
        else:
            move.append(row)
    return move, keep, collide


def show(move, keep, collide, apply_: bool):
    print("=" * 78)
    print("Workspace → Dashboard：HTML 迁移%s" % ("（DRY-RUN，什么都不会写）" if not apply_ else ""))
    print("=" * 78)
    print("\n将要搬的 %d 条：" % len(move))
    for row in move:
        schema = len(row.get("filters_json") or "")
        flag = ""
        if row["kind"] == "dynamic" and not schema:
            flag = "  ⚠️ kind=dynamic 但没有筛选定义"
        print("  %-34s %-12s html=%-7d filters=%-5d %s%s"
              % (row["slug"][:34], row["kind"], row["html_len"], schema,
                 "（已存在，--force 会覆盖）" if row["already_there"] else "", flag))
    print("\n留在 Workspace 的 %d 条（Office 文档，Dashboard 存不了二进制）：" % len(keep))
    for row in keep:
        print("  %-34s doc_type=%-6s" % (row["slug"][:34], row["doc_type"]))
    if collide:
        print("\n目标已存在、需要 --force 才会覆盖的 %d 条：" % len(collide))
        for row in collide:
            print("  %s" % row["slug"])
    print()


def assert_self_consistent(row: dict) -> None:
    """A carried page must not claim a capability its own fields do not carry.

    ⚠️ Added after the production run above. `kind` and `filters_json` travel
    independently, so a page can arrive saying `dynamic` with an empty schema — and
    because `GET /api/dashboard/{slug}/filters` then reports `configured: false`, the
    rail hides itself while the card still shows a filter button. Both halves being
    absent together is the only state that is actually consistent for each kind.
    """
    kind = row.get("kind") or "static"
    schema = row.get("filters_json") or ""
    if kind == "dynamic" and not schema:
        raise RuntimeError(
            "%s: kind=dynamic but filters_json is empty — the page would advertise a "
            "filter rail it does not have. Refusing to write." % row.get("slug"))


def apply(conn, rows):
    cols = list(SAME_NAME) + [c for c in COVER_COLS if c not in SAME_NAME]
    cols_sql = ", ".join(cols)
    # ⚠️ Plain `%s` placeholders, NOT `r.column`. The row was already materialised by
    # `plan()` into Python values, so there is no `r` in scope here — a version that
    # wrote `r.html` got past a dry-run (which never executes this) and died on the
    # first real run with "missing FROM-clause entry".
    placeholders = ", ".join(["%s"] * len(cols))
    done = []
    with conn.cursor() as cur:
        for row in rows:
            assert_self_consistent(row)
            cur.execute(
                f"""INSERT INTO ai_dashboards ({cols_sql}) VALUES ({placeholders})
                    ON CONFLICT (slug) DO UPDATE SET
                      {', '.join('%s = EXCLUDED.%s' % (c, c) for c in cols if c != 'slug')}
                    RETURNING id""",
                tuple(row.get(c) for c in cols))
            new_id = cur.fetchone()[0]
            # ⚠️ Carry the reader's saved filter view across too, keyed to the NEW row id.
            # Forgetting this loses every reader's picks, and it is invisible in the
            # report itself — the rail just opens on the default as if it never existed.
            # ⚠️ `report_id` here, `doc_id` in the table it goes INTO. The two selection
            # tables do not share a key name: the dashboard one uses the generic
            # `doc_id` so the shared reader in `doc_state.saved_selection` can address
            # either. Writing the same column name into both would have been tidier and
            # would have meant the shared function takes a second parameter that the
            # next caller gets to get wrong.
            cur.execute("SELECT report_id, user_email, selection_json, updated_at "
                        "FROM ai_report_filter_selections WHERE report_id = %s", (row["id"],))
            carried = cur.fetchall()
            for _old, who, selection, when in carried:
                cur.execute(
                    """INSERT INTO ai_dashboard_filter_selections
                           (doc_id, user_email, selection_json, updated_at)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (doc_id, user_email)
                       DO UPDATE SET selection_json = EXCLUDED.selection_json,
                                     updated_at = EXCLUDED.updated_at""",
                    (new_id, who, selection, when))
            cur.execute("DELETE FROM ai_reports WHERE id = %s", (row["id"],))
            done.append((row["slug"], new_id, len(carried)))
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="真的写库。不给这个参数就只打印计划。")
    ap.add_argument("--force", action="store_true",
                    help="目标 slug 已存在时用报告覆盖仪表盘。默认跳过。")
    ap.add_argument("--only", default="", help="只搬这一个 slug（调试用）。")
    args = ap.parse_args()

    conn = connect()
    try:
        from services import dashboard_store
        dashboard_store.ensure_schema()          # so the new columns exist first

        move, keep, collide = plan(conn, args.force)
        if args.only:
            move = [r for r in move if r["slug"] == args.only]
        if not move:
            print("没有要搬的 HTML。先看上面的清单。")
            return 0
        show(move, keep, collide, args.apply)
        if not args.apply:
            print("这是 DRY-RUN。确认无误后加 --apply。")
            return 0

        done = apply(conn, move)
        conn.commit()
        print("已搬 %d 条：" % len(done))
        for slug, new_id, picks in done:
            print("  %-34s → ai_dashboards.id=%-5s 迁移的读者视图 %d 条" % (slug[:34], new_id, picks))
        print("\n留在 Workspace 的 Office 文档：%d 条（未改动）" % len(keep))
        print("\n旧地址 /r/{slug} 现在会 404；新地址是 /d/{slug}。")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
