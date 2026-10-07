"""Per-request language resolution — see `klado_shared/i18n.py`.

Re-exported, not reimplemented. The pair-splitting rule is the translation memory: if
the backend half and the frontend half (`frontend/out/i18n.js`) ever disagree, a message
splits in one and not the other, and the reader sees both languages at once. There is
one implementation now, and a change to it changes both processes.
"""
from __future__ import annotations

from core import _shared_path  # noqa: F401  — side effect: repository root on sys.path
from klado_shared.i18n import (SUPPORTED, parse_accept_language, pick, request_lang,
                               split_pair)

__all__ = ["SUPPORTED", "parse_accept_language", "pick", "request_lang", "split_pair"]
