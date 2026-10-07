"""Code shared by BOTH Klado processes: the main app and the admin console.

Why this package exists
-----------------------
The admin console is a separate process, on a separate port, with its own frontend and
its own login page — but it is the *same installation*: the same PostgreSQL, the same
accounts, the same operator. Anything both processes must agree on, byte for byte, lives
here rather than being written twice.

The rule that keeps this honest:

    `api-admin/` must never `import api.*`.

Two copies of an authorization decision is two answers waiting to diverge, and the
divergence is silent — both processes keep working, they just disagree about who may do
what, and the only symptom is a permission that behaves differently depending on which
port you went through. `api/tests/test_shared_layer.py` scans for the import and fails
the build. When the console genuinely needs a piece of main-app behaviour, that piece
moves HERE and `api/` re-exports it, so there is one implementation and one test.

Layout
------
* `config.py`    — Settings (the process reads the same `.env` either way)
* `db.py`        — the single psycopg2 connection resolver
* `session.py`   — stateless signed session tokens, with an audience
* `identity.py`  — "is this person an operator?"
* `i18n.py`      — the bilingual `中文 / English` convention
* `orgs.py`      — organizations, org roles, and the share-scope gate (phase P2)

Import path
-----------
This package lives at the repository root, one level above `api/`, and `api/` is the
import root for the main app (the documented start is `cd api && uvicorn main:app`).
Each entry point therefore puts the repository root on `sys.path` before importing
anything from here — see the bootstrap at the top of `api/core/config.py` and of
`api-admin/main.py`. There is deliberately no `.pth` file and no install step: a
checkout that runs straight from the source tree has to work.
"""
