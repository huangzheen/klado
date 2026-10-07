"""The four emails Klado sends, and the honest report of whether they left.

The implementation is `klado_shared/mailer.py`. Shared because delivery is configured in
one table and read by both processes, and because the failure vocabulary
(`sent` / `unconfigured` / `failed` / `error`) is a contract the callers branch on: a
missing mail server must leave the code usable, not abort the sign-up that minted it.

⚠️ `print()` and not `_LOG` for the "not configured" notices. `core/logring.py` swallows
app-level logging, so a `_LOG.warning` about a missing mailbox is invisible in the server
log — which is where somebody looks when a code does not arrive. Kept here rather than
moved: it is a property of this file, not of the destination.

⚠️ The banner lookup reads `mail_asset_dirs()` from the shared config, NOT the main app's
`resolve_frontend_dir()`. The two answer different questions and merging them would point
the main app's static mount at the console's bundle.
"""
from __future__ import annotations

from core import _shared_path  # noqa: F401  — side effect: repository root on sys.path
from klado_shared import _reexport, mailer as _impl

# A *live view*, not a re-export list: reads, writes and deletes all forward, so
# `patch.object(<this module>, "_db")` still stubs the function under test. See
# klado_shared/_reexport.py for why a list would have looked like it worked.
_reexport.live_view(_impl, __name__)
