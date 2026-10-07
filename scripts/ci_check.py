#!/usr/bin/env python3
"""Checks that need neither a live database nor production credentials."""
import compileall
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    os.environ.setdefault("POSTGRES_PASSWORD", "ci-placeholder")
    os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
    os.environ.setdefault("POSTGRES_PORT", "1")
    for directory in ("api", "api-admin", "klado_shared", "scripts"):
        if not compileall.compile_dir(repo / directory, quiet=1):
            return 1
    return subprocess.call(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
        cwd=repo / "api",
    )


if __name__ == "__main__":
    raise SystemExit(main())
