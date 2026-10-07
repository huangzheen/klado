"""
Tiny HTML-surgery helpers shared by the runtimes the server injects.

The deck runtime (`routers/reports.py`) and the annotation runtime
(`services/annotations.py`) both have to put a stylesheet before `</head>` and a
script before `</body>` of a document whose exact shape they do not control. That
is one rule, so it lives here once: a second copy is a second answer to "what
happens when the tag is missing", and the two runtimes would drift on the very
documents that need both.
"""
from __future__ import annotations


def insert_before_tag(html: str, tag: str, payload: str) -> str:
    """Insert `payload` just before the LAST `tag`; append when it is absent.

    Last, not first: an authored document may legitimately contain the tag more
    than once (a nested template), and the runtime that must win is the one that
    is closest to the end of the document.
    """
    idx = html.lower().rfind(tag)
    if idx == -1:
        return html + "\n" + payload
    return html[:idx] + payload + "\n" + html[idx:]
