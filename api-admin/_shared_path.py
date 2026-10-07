"""Put the repository root on `sys.path` so `klado_shared` is importable.

The same job as `api/core/_shared_path.py`, and the same reasoning, kept as a separate
file because the two entry points genuinely are separate: the main app is started as
`cd api && uvicorn main:app`, the console as `cd api-admin && uvicorn main:app`. Neither
has the repository root on the path, and neither may import the other's bootstrap — the
console importing anything out of `api/` is the coupling the shared layer exists to
prevent, and it would fail closed at the first test that checks for it.

The path is derived from this file's own location, so `cd api`, `cd api-admin` and
`uvicorn --app-dir` all agree. Idempotent: the guard makes repeat imports and
`api/core/_shared_path.py` (if a shared module ever pulls it in) harmless whichever runs
first.
"""
from __future__ import annotations

import os
import sys

# …/api-admin/_shared_path.py → …/api-admin → …/<repo>
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
