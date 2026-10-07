"""Main-app configuration.

⚠️ The `Settings` object itself lives in `klado_shared/config.py` and is re-exported
here. It moved because the admin console has to resolve the **same** `SECRET_KEY` and
the **same** database — two copies of this class would be two answers, and the failure
mode is a console that quietly administers a different installation while looking
perfectly healthy. Nothing in this file re-declares a setting; if you are adding one, it
goes in the shared module.

What stays here is the main app's own filesystem layout, which the console has no use
for. Keeping the split honest:

    `klado_shared/config.py`  — what a process needs to exist (env, secrets, DB).
    `api/core/config.py`      — where the main app finds its own files.

Import path
-----------
This package lives one level above `api/`, and `api/` is the import root (the documented
start is `cd api && … uvicorn main:app`). `core/_shared_path.py` puts the repository
root on `sys.path` before the import above, and every module that reaches the shared
package imports that first — so the app, the admin console's shared imports and the test
suite all resolve it the same way. A `.pth` file or an install step was rejected
deliberately: a checkout that runs straight from the source tree has to work.
"""
from __future__ import annotations

import os

from core import _shared_path  # noqa: F401  — side effect: repository root on sys.path
from klado_shared.config import ENV_FILES, REPO_DIR, Settings, settings  # noqa: E402

__all__ = ["ENV_FILES", "REPO_DIR", "Settings", "settings",
           "resolve_frontend_dir", "app_base_href"]


# ── Static UI bundle location ────────────────────────────────────────────────
# One definition for everyone who needs the shipped frontend: main.py mounts it,
# the reports router reads the deck runtime out of it. The candidates are built
# from REPO_DIR, not from the caller's, so the answer never depends on which module
# asks. (`REPO_DIR` comes from the shared config module above.)
def resolve_frontend_dir() -> str:
    """
    Return the static UI directory, or a best-effort path when none exists.

    Order: explicit FRONTEND_DIR → the repo's frontend/out. The two historical
    candidates that used to sit in between (`/frontend` and `api/frontend/out`)
    belonged to the retired container build, which copied a second copy of the SPA
    into the image; keeping them here would silently serve a stale duplicate.
    """
    candidates = [
        os.environ.get("FRONTEND_DIR", ""),
        os.path.join(REPO_DIR, "frontend", "out"),
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return candidate
    return os.environ.get("FRONTEND_DIR", os.path.join(REPO_DIR, "frontend", "out"))


def app_base_href() -> str:
    """
    The mount point the app is served from, always with a leading and trailing
    slash — "/" when it is served from the root, "/klado/" behind a sub-path.

    It belongs here rather than in a router because it is not one route's business:
    a report document, a shared link, an event page, an Inbox deep link and the
    annotation runtime's API base all have to agree on it.
    """
    path = (settings.APP_BASE_PATH or "").strip()
    if not path or path == "/":
        return "/"
    return "/" + path.strip("/") + "/"
