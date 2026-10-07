"""The module registry: which features this deployment offers and to whom.

The implementation is `klado_shared/modules.py`. It is the *authorization source of truth*
for the whole product, and it is now read by two processes: the main app to draw
navigation and to run the middleware gate, the console to show the deployment default
that enterprise administrators are measured against.

⚠️ That is why this moved rather than being copied. `Module` declares `api_prefixes`,
`requires`, `required` and `in_nav`; the middleware matches paths by segment against
`api_prefixes`, and an account's entitlement is `deployment_default ∩ account_modules`
closed over `requires`. A console with its own registry would admit a module the app
refuses, and the symptom would be a page that loads in the console and 403s in the app.

Adding a module means editing `klado_shared/modules.py` and nothing else — the registry is
the single source, and a router that is not declared in it fails closed.
"""
from __future__ import annotations

from core import _shared_path  # noqa: F401  — side effect: repository root on sys.path
from klado_shared import _reexport, modules as _impl

# A *live view*, not a re-export list: reads, writes and deletes all forward, so
# `patch.object(<this module>, "_db")` still stubs the function under test. See
# klado_shared/_reexport.py for why a list would have looked like it worked.
_reexport.live_view(_impl, __name__)
