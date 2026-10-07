"""Opt-in placement regression against a disposable database named klado_audit.

Supply POSTGRES_* for that database. Never run against the application's database.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.db import pg_connection_kwargs
from routers import reports
from services import report_projects as filing

OWNER = 'placement-owner@example.test'
OTHER = 'placement-other@example.test'


def main():
    if pg_connection_kwargs()['dbname'] != 'klado_audit':
        raise SystemExit('Refusing to write outside disposable klado_audit database')
    reports._ensure_table()
    project = filing.create_project(OWNER, 'Placement regression')
    folder = filing.create_folder(project.slug, OWNER, 'Normal')
    system = next(f for f in filing.list_folders(project.slug, OWNER) if f.system)
    unfiled, unfiled_id = filing.ensure_unfiled(OWNER)
    extra = filing.create_folder(unfiled, OWNER, 'Catch-all subfolder')
    slugs = ['placement-normal', 'placement-system', 'placement-global',
             'placement-extra', 'placement-public', 'placement-other']
    try:
        with reports._db() as conn, conn.cursor() as cur:
            for slug in slugs:
                cur.execute("INSERT INTO ai_reports (slug,title,owner_email,visibility,kind) "
                            "VALUES (%s,%s,%s,%s,'static')",
                            (slug, slug, OTHER if slug.endswith('other') else OWNER,
                             'public' if slug.endswith('public') else 'private'))
            for slug, proj, fid in (
                (slugs[0], project.slug, folder.id), (slugs[1], project.slug, system.id),
                (slugs[2], unfiled, unfiled_id), (slugs[3], unfiled, extra.id),
                (slugs[4], project.slug, system.id), (slugs[5], project.slug, system.id),
            ):
                cur.execute('UPDATE ai_reports SET project_slug=%s,folder_id=%s WHERE slug=%s',
                            (proj, fid, slug))
            cur.execute('SELECT slug,updated_at FROM ai_reports WHERE slug = ANY(%s)', (slugs,))
            times = dict(cur.fetchall())
            filing.normalize_legacy_placement(cur)
            filing.normalize_legacy_placement(cur)
            cur.execute('SELECT slug,project_slug,folder_id,updated_at FROM ai_reports '
                        'WHERE slug = ANY(%s)', (slugs,))
            rows = {r[0]: r[1:] for r in cur.fetchall()}
        expected = [(project.slug, folder.id), (project.slug, None), (None, None),
                    (None, extra.id), (project.slug, system.id), (project.slug, system.id)]
        for slug, placement in zip(slugs, expected):
            assert rows[slug][:2] == placement, (slug, rows[slug], placement)
            assert rows[slug][2] == times[slug], 'migration reordered a document'
        print('PASS legacy migration: idempotent, owner-scoped, private-only, timestamps preserved')

        request = SimpleNamespace(state=SimpleNamespace(current_user={'email': OWNER}, auth_kind='browser'))
        async def listed(proj, fid):
            return await reports.list_reports(request, q='', status='', scope='mine',
                                              category='', tag='', project=proj, folder_id=fid, limit=200)
        for proj, fid, slug in [(project.slug, system.id, slugs[1]),
                                (unfiled, unfiled_id, slugs[2]), ('', unfiled_id, slugs[2]), (unfiled, extra.id, slugs[3])]:
            result = asyncio.run(listed(proj, fid))
            assert [r['slug'] for r in result['reports']] == [slug], result
        assert filing.get_folder(system.id, OWNER).report_count == 1
        print('PASS real SQL: system folder counts and filtered lists agree')
        for proj, fid, placement in [(project.slug, system.id, (project.slug, None)),
                                      (unfiled, unfiled_id, ('', None)),
                                      (unfiled, extra.id, ('', extra.id))]:
            moved = filing.move_report(slugs[0], OWNER, proj, fid)
            assert (moved['project_slug'], moved['folder_id']) == placement, moved
        moved = filing.move_report(slugs[0], OWNER, project.slug, folder.id)
        assert moved['folder_id'] == folder.id
        filing.delete_folder(folder.id, OWNER)
        result = asyncio.run(listed(project.slug, system.id))
        assert {r['slug'] for r in result['reports']} == {slugs[0], slugs[1]}
        assert set(filing.delete_project(project.slug, OWNER)) == {slugs[0], slugs[1]}
        with reports._db() as conn, conn.cursor() as cur:
            cur.execute('SELECT slug,project_slug,folder_id FROM ai_reports WHERE slug = ANY(%s)',
                        ([slugs[4], slugs[5]],))
            for slug, proj, fid in cur.fetchall():
                assert (proj, fid) == (project.slug, system.id), 'delete changed another scope'
        print('PASS real SQL: move, delete folder, delete project preserve reports')
    finally:
        with reports._db() as conn, conn.cursor() as cur:
            cur.execute('DELETE FROM ai_reports WHERE slug = ANY(%s)', (slugs,))
            cur.execute('DELETE FROM ai_report_projects WHERE owner_email = %s', (OWNER,))
    print('3 PostgreSQL regression groups passed; fixtures cleaned')


if __name__ == '__main__':
    main()
