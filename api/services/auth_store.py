"""Accounts, verification codes and agent access codes.

The implementation is `klado_shared/accounts.py`. It moved because the admin console is a
second process on the same database, and "how do I verify a password" or "what does
revoking an agent code delete" cannot have two answers — a console that hashed differently
would lock every operator out, and a console with its own copy of the revoke rule would
quietly disagree with the app about what is still valid.

This file re-exports, and delegates through `__getattr__` rather than restating a name
list. Two reasons:

* **Delegation keeps object identity.** `auth_store.issue_session is
  klado_shared.session.issue_session` stays true, so a test can assert there is one
  implementation instead of asserting that two copies agree today.
* **A list would have to be maintained.** This module has ~50 public names, several of
  them underscore-prefixed (`_public`, `_db`, `_ensure_schema`) and used by callers. A
  hand-written `__all__` that misses one turns into an `AttributeError` on a code path
  nothing exercises — which is exactly how `routers/org_admin.py` came to call
  `auth_store.issue_invite_code`, a function that has never existed.

The cost is that a definition is no longer greppable at its old path. `rg 'def
create_user' api/services/auth_store.py` finds nothing, and that is correct: the file that
should be read is `klado_shared/accounts.py`.

Import path: `core._shared_path` first, because `klado_shared` sits one level above
`api/` and `api/` is the import root.
"""
from __future__ import annotations

from core import _shared_path  # noqa: F401  — side effect: repository root on sys.path
from klado_shared import _reexport, accounts as _impl

# A *live view*, not a re-export list: reads, writes and deletes all forward, so
# `patch.object(<this module>, "_db")` still stubs the function under test. See
# klado_shared/_reexport.py for why a list would have looked like it worked.
_reexport.live_view(_impl, __name__)
