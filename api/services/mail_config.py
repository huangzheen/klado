"""SMTP settings, stored in the database rather than in the environment.

The implementation is `klado_shared/mail_config.py`. Both processes send mail — the main
app for verification codes, invitations and agent codes; the console for the operator's
"send a test" button and for resending a code by hand — so both have to read and write the
*same* `mail_settings` row. Two copies would mean the console could save a password the
app then ignored, which presents as "I configured SMTP and nothing works".

The password column is encrypted at rest and is never returned by any endpoint. `KEEP` is
the sentinel that lets a save leave it alone: an operator editing the port must not have to
retype the password to do it.
"""
from __future__ import annotations

from core import _shared_path  # noqa: F401  — side effect: repository root on sys.path
from klado_shared import _reexport, mail_config as _impl

# A *live view*, not a re-export list: reads, writes and deletes all forward, so
# `patch.object(<this module>, "_db")` still stubs the function under test. See
# klado_shared/_reexport.py for why a list would have looked like it worked.
_reexport.live_view(_impl, __name__)
