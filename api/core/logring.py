"""In-process log ring buffer — lets /api/debug/logs serve logs from process start.

Installed as early as possible (api/main.py import time) on the root logger
and uvicorn's loggers, so startup-phase diagnostics (DB bootstrap, migrations,
router registration) are captured exactly as they hit stdout/stderr.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

_lock = threading.Lock()
_ring: deque = deque(maxlen=4000)  # [{'ts','level','logger','msg'}]


class RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "msg": self.format(record),
            }
            with _lock:
                _ring.append(entry)
        except Exception:
            self.handleError(record)


_handler = RingBufferHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))


def install(capacity: int = 4000) -> None:
    """Attach the ring buffer to root + uvicorn loggers. Idempotent."""
    # deque.maxlen is read-only. Rebuild the bounded deque when callers ask
    # for a different capacity, while retaining the newest existing records.
    global _ring
    with _lock:
        if _ring.maxlen != capacity:
            _ring = deque(_ring, maxlen=capacity)
    installed = getattr(_handler, "_bu_installed", False)
    if not installed:
        for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
            lg = logging.getLogger(name)
            lg.addHandler(_handler)
        _handler._bu_installed = True


def snapshot(limit: int = 400, level: str = "", q: str = "") -> list[dict]:
    """Return the most recent `limit` entries, newest last, with optional filters."""
    order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    min_no = order.get(level.upper())
    q_low = q.lower() if q else ""
    with _lock:
        items = list(_ring)
    out = []
    for e in items:
        if min_no is not None and order.get(e["level"], 20) < min_no:
            continue
        if q_low and q_low not in e["msg"].lower() and q_low not in e["logger"].lower():
            continue
        out.append(e)
    return out[-limit:]
