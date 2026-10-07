"""The HTML/text bodies of the four emails.

The implementation is `klado_shared/mail_templates.py`. No database, no I/O, no
configuration — pure strings, which is exactly why it moved with the rest: the console's
"preview the real templates" button has to show what the app will actually send, and a
second copy of the wording would drift from the copy people receive.

Every user-visible string here is a `中文 / English` pair, split by
`klado_shared/i18n.pick()`. That rule is the backend half of the translation memory
described in AGENTS.md; the frontend half is `frontend/out/i18n.js`. Both halves must
agree on the split or a reader sees two languages at once.
"""
from __future__ import annotations

from core import _shared_path  # noqa: F401  — side effect: repository root on sys.path
from klado_shared import _reexport, mail_templates as _impl

# A *live view*, not a re-export list: reads, writes and deletes all forward, so
# `patch.object(<this module>, "_db")` still stubs the function under test. See
# klado_shared/_reexport.py for why a list would have looked like it worked.
_reexport.live_view(_impl, __name__)
