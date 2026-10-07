"""The one psycopg2 connection resolver — see `klado_shared/db.py` for the rules.

This module re-exports the shared implementation unchanged. It exists because the
main app's twenty-odd modules already say `from core.db import connect_main`, and
rewriting all of them would be a large diff for no behavioural gain: there is still
exactly one implementation, in the shared package, and that is what both processes use.
"""
from __future__ import annotations

from core import _shared_path  # noqa: F401  — side effect: repository root on sys.path
from klado_shared.db import (STATEMENT_TIMEOUT_ENV, _pg_kwargs, connect_main,
                             pg_connection_kwargs)

__all__ = ["STATEMENT_TIMEOUT_ENV", "connect_main", "pg_connection_kwargs"]
