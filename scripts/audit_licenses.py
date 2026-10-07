#!/usr/bin/env python3
"""Print, and optionally refresh, the installed-dependency inventory in THIRD_PARTY_NOTICES.md.

    python3 scripts/audit_licenses.py             # print the report
    python3 scripts/audit_licenses.py --write     # rewrite the inventory block in place
    python3 scripts/audit_licenses.py --versions  # one row per package, with versions

The walk itself lives in api/services/license_audit.py so that this script and
api/tests/test_pdf_io.py can never disagree about which packages ship.
"""
from __future__ import annotations

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "api"))

from services.license_audit import (  # noqa: E402 — path is set up above
    declared_tree,
    grouped,
    iter_versions,
    render_markdown,
    runtime_tree,
    LicenseAuditError,
)

BEGIN = "<!-- BEGIN GENERATED INVENTORY -->"
END = "<!-- END GENERATED INVENTORY -->"
NOTICES = os.path.join(REPO, "THIRD_PARTY_NOTICES.md")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="rewrite the inventory block in THIRD_PARTY_NOTICES.md")
    parser.add_argument("--versions", action="store_true",
                        help="also print one row per distribution with its version")
    parser.add_argument("--declared-only", action="store_true",
                        help="audit only the active declared graph (default: all installed packages)")
    args = parser.parse_args()

    try:
        tree = declared_tree() if args.declared_only else runtime_tree()
    except (LicenseAuditError, ValueError) as exc:
        print(f"INCOMPLETE: {exc}", file=sys.stderr)
        return 1
    if not tree:
        print("no distributions reachable from api/requirements.txt are installed here", file=sys.stderr)
        return 1

    buckets = grouped(tree)
    blocking = buckets.get("blocking") or []
    weak = buckets.get("weak-copyleft") or []
    unknown = buckets.get("unknown") or []

    print(f"{'declared dependency tree' if args.declared_only else 'installed runtime'}: {len(tree)} distributions")
    for key in ("blocking", "weak-copyleft", "unknown", "permissive"):
        names = buckets.get(key) or []
        print(f"  {key:14s} {len(names):3d}  {', '.join(names[:12])}{' …' if len(names) > 12 else ''}")
    print()

    if args.versions:
        print("| distribution | version | licence |")
        print("|---|---|---|")
        for name, version, license_text in iter_versions(tree):
            print(f"| `{name}` | {version} | {license_text or '—'} |")
        print()

    if args.write and (blocking or unknown):
        print("refusing to write an unapproved inventory", file=sys.stderr)
        return 1
    if args.write:
        with open(NOTICES, encoding="utf-8") as handle:
            text = handle.read()
        if BEGIN not in text or END not in text:
            print(f"{NOTICES} has no inventory markers; nothing written", file=sys.stderr)
            return 1
        head, _, rest = text.partition(BEGIN)
        _, _, tail = rest.partition(END)
        stamp = _stamp()
        with open(NOTICES, "w", encoding="utf-8") as handle:
            versions = ["| distribution | version | licence |", "|---|---|---|"]
            for name, version, licence in iter_versions(tree):
                licence = licence.replace("|", "\\|")
                versions.append(f"| `{name}` | {version} | {licence} |")
            handle.write(f"{head}{BEGIN}\n{stamp}\n{render_markdown(tree)}\n" +
                         "\n".join(versions) + f"\n{END}{tail}")
        print(f"inventory block in {os.path.relpath(NOTICES, REPO)} refreshed")
    elif not args.versions:
        print(render_markdown(tree))

    if blocking:
        print(f"\nBLOCKING: {', '.join(blocking)} — these require review under the project release policy",
              file=sys.stderr)
    if unknown:
        print(f"\nUNCLASSIFIED: {', '.join(unknown)} — confirm these before publishing",
              file=sys.stderr)
    return 1 if blocking or unknown else 0


def _stamp() -> str:
    """A date the reader can date the inventory by, without a timezone library."""
    import datetime
    return f"<!-- 生成于 {datetime.date.today().isoformat()}，由 scripts/audit_licenses.py 生成 -->"


if __name__ == "__main__":
    raise SystemExit(main())
