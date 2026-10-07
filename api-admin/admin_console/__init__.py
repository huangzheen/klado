"""The admin console's own code.

⚠️ **The package name is load-bearing, not cosmetic.** The main app's importable trees are
`core` and `routers` (it is started as `cd api && uvicorn main:app`, so both are
top-level). Naming the console's directories the same thing made `routers` and `core`
*ambiguous* the moment anything put both trees on one `sys.path` — which is exactly what
a test that checks the boundary has to do, and what an operator debugging both processes
at once does with a `PYTHONPATH`. Python resolves the first match on the path, so the
console's router imports silently became the main app's, and the "separate process" was
separated only by which working directory it was started from.

`admin_console` cannot collide with anything in `api/`, so the console is importable
beside the main app rather than instead of it — which is what makes the boundary
*testable* instead of merely intended. `api/tests/test_admin_console.py` depends on it.
"""
