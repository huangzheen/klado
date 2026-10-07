"""Put the repository root on `sys.path` so `klado_shared` is importable.

Imported **for its side effect**, first, by every main-app module that reaches the
shared package. Not a `__init__.py` on purpose: `api/core/` is a namespace package
(there is no `core/__init__.py` anywhere in this tree), and adding one would quietly
turn it into a regular package — a structural change with nothing to do with this work.
A named module that does exactly one thing is easier to reason about than a package's
implicit load order.

Why it is needed at all
-----------------------
`klado_shared/` sits one level above `api/`, and `api/` is the import root: the
documented way to start the app is `cd api && … uvicorn main:app`, and the test suite
is `cd api && python -m unittest discover -s tests`. Neither has the repository root on
the path.

Rejected alternatives, for the next person who has this idea:

* **A `.pth` file in site-packages** — requires an install step. This repository has to
  run straight from a source checkout, and the failure mode of a missing `.pth` is an
  `ImportError` on a module that exists.
* **A cwd-relative `sys.path` hack in each module** — the answer would then depend on
  where the process was started from. This file derives it from its own location, so
  `cd api`, `cd api-admin` and `uvicorn --app-dir …` all agree.
* **Making `klado_shared` a subpackage of `api/`** — then the admin console would be
  importing out of the main app's tree, which is precisely the coupling the shared layer
  exists to avoid.

Idempotent: the guard makes repeat imports and the admin console's own bootstrap
harmless whichever runs first.
"""
from __future__ import annotations

import os
import sys

# …/api/core/_shared_path.py → …/api/core → …/api → …/<repo>
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
