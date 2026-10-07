"""Closing an account, restoring it, and erasing it for good.

The implementation is `klado_shared/lifecycle.py`. It is the most important thing in this
package to *not* have two copies of: the cascade lists in `OWNED_CONTENT` and
`OWNED_SIDECARS` decide which rows disappear when somebody's account is purged, and a
second list in the console would drift within a release. The failure mode is not a crash —
it is a purged account that left its dashboards behind, or a live account whose rows the
console believed were deletable.

Both processes therefore purge through the same code path, against the same table, in the
same order (content first, the account row last, so a storage failure leaves a retryable
account rather than a dangling owner).

See `klado_shared/accounts.py` for why this file delegates instead of listing names.
"""
from __future__ import annotations

from core import _shared_path  # noqa: F401  — side effect: repository root on sys.path
from klado_shared import _reexport, lifecycle as _impl

# A *live view*, not a re-export list: reads, writes and deletes all forward, so
# `patch.object(<this module>, "_db")` still stubs the function under test. See
# klado_shared/_reexport.py for why a list would have looked like it worked.
_reexport.live_view(_impl, __name__)
