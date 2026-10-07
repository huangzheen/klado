"""Explicit read-only grants on uploaded files, independent of dataset grants."""
from __future__ import annotations

import psycopg2.extras
from services.dataset_shares import ShareError, _check_email, _conn


def ensure_schema():
    with _conn() as conn, conn.cursor() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS public.file_shares (
            file_id INTEGER NOT NULL REFERENCES public._file_library(id) ON DELETE CASCADE,
            grantee_email TEXT NOT NULL, shared_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY(file_id, grantee_email))''')
        cur.execute('CREATE INDEX IF NOT EXISTS file_shares_grantee_idx ON public.file_shares(grantee_email)')
        conn.commit()


def _owner(cur, file_id, email):
    cur.execute('SELECT owner_email FROM public._file_library WHERE id=%s', (file_id,))
    row = cur.fetchone()
    if not row or not email or row[0] != email:
        raise ShareError('文件不存在或不属于你 / file not found or not yours')


def share(file_id: int, grantee_email: str, owner_email: str):
    grantee = _check_email(grantee_email)
    if grantee == owner_email:
        raise ShareError('不能分享给自己 / cannot share with yourself')
    with _conn() as conn, conn.cursor() as cur:
        _owner(cur, file_id, owner_email)
        cur.execute('''INSERT INTO public.file_shares(file_id,grantee_email,shared_by)
            VALUES (%s,%s,%s) ON CONFLICT(file_id,grantee_email) DO NOTHING''',
            (file_id, grantee, owner_email))
        conn.commit()
    return {'file_id': file_id, 'grantee_email': grantee, 'shared_by': owner_email}


def revoke(file_id: int, grantee_email: str, owner_email: str):
    with _conn() as conn, conn.cursor() as cur:
        _owner(cur, file_id, owner_email)
        cur.execute('DELETE FROM public.file_shares WHERE file_id=%s AND grantee_email=%s',
                    (file_id, _check_email(grantee_email)))
        removed = cur.rowcount > 0
        conn.commit()
    return removed


def shares_of(file_id: int):
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute('SELECT grantee_email,shared_by,created_at FROM public.file_shares WHERE file_id=%s', (file_id,))
        return [dict(r) for r in cur.fetchall()]


def shared_with_me(email: str):
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute('''SELECT f.id,f.filename,f.object_name,f.size_bytes,f.uploaded_at,f.owner_email
            FROM public._file_library f JOIN public.file_shares s ON s.file_id=f.id
            WHERE s.grantee_email=%s AND s.shared_by=f.owner_email ORDER BY s.created_at DESC''', (email,))
        return [dict(r) for r in cur.fetchall()]


def may_read(file_id: int, email: str):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute('''SELECT 1 FROM public.file_shares s JOIN public._file_library f ON f.id=s.file_id
            WHERE s.file_id=%s AND s.grantee_email=%s AND s.shared_by=f.owner_email''', (file_id, email))
        return cur.fetchone() is not None


def may_read_object(path: str, email: str):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute('''SELECT 1 FROM public.file_shares s JOIN public._file_library f ON f.id=s.file_id
            WHERE f.object_name=%s AND s.grantee_email=%s AND s.shared_by=f.owner_email''', (path, email))
        return cur.fetchone() is not None
