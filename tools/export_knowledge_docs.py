#!/usr/bin/env python3
"""Read-only export of the deployed knowledge base into ``api/knowledge_docs/``.

This is NOT a publishing path. Since 2026-09-23 the files under
``api/knowledge_docs/`` are the single source of truth and are applied to the
database by ``services.ai.knowledge_docs`` on startup; the database is derived.
This script only walks the other way, and exists for two reasons:

1. the one-time migration that created those files, and
2. disaster recovery: rebuild the files from a deployment whose source was lost.

Running it against a deployment that already matches the files is a no-op.
It writes files only; it never touches the database (``/api/data-center/query``
is SELECT-only).

Usage:
    python3 tools/export_knowledge_docs.py --base http://localhost:8000 [--apply]
    python3 tools/export_knowledge_docs.py --base http://localhost:8000 --diff  # dry run
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_DIR = REPO_ROOT / "api" / "knowledge_docs"

# Front-matter keys that carry the document identity; everything else in the
# stored metadata is emitted as an extra scalar so the export stays lossless.
IDENTITY_KEYS = ("document_id", "title", "document_type", "source", "version")


def request(opener, url):
    try:
        with opener.open(url, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:400]


def filename_for(document_id: str) -> str:
    return document_id.replace(":", "__") + ".md"


def render(document: dict) -> str:
    metadata = document.get("metadata") or {}
    tags = [str(t) for t in (metadata.get("tags") or [])]
    extras = {k: v for k, v in metadata.items() if k != "tags"}

    lines = ["---"]
    for key in IDENTITY_KEYS:
        lines.append(f"{key}: {document.get(key, '')}")
    lines.append("tags: [" + ", ".join(tags) + "]")
    for key in sorted(extras):
        value = extras[key]
        if isinstance(value, list):
            value = "[" + ", ".join(str(v) for v in value) + "]"
        lines.append(f"{key}: {value}")
    lines.append("---")
    # The store strips surrounding whitespace on write, so the exported body must
    # be the stripped form for a byte-exact round trip.
    return "\n".join(lines) + "\n\n" + (document.get("content") or "").strip() + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="e.g. http://localhost:8000")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--diff", action="store_true", help="report differences, write nothing")
    args = ap.parse_args()

    api = f"{args.base.rstrip('/')}/api/ai/knowledge"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    status, manifest = request(opener, f"{api}/manifest?limit=500")
    if status != 200:
        raise SystemExit(f"manifest failed: {status} {manifest}")
    documents = manifest["documents"]
    print(f"# {len(documents)} documents, manifest valid={manifest['valid']} issues={len(manifest['issues'] or [])}")

    written = unchanged_local = 0
    for entry in sorted(documents, key=lambda d: d["document_id"]):
        document_id = entry["document_id"]
        status, body = request(opener, f"{api}/document/" + urllib.parse.quote(document_id, safe=""))
        if status != 200:
            raise SystemExit(f"fetch {document_id} failed: {status} {body}")
        path = TARGET_DIR / filename_for(document_id)
        want = render(body)
        have = path.read_text(encoding="utf-8") if path.exists() else None
        if have == want:
            unchanged_local += 1
            continue
        print(f"  {'would write' if not args.apply else 'writing'} {path.name}"
              f"  ({'new' if have is None else f'{len(have)} -> {len(want)} chars'})")
        if args.apply:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(want, encoding="utf-8")
            written += 1

    print(f"# written={written} already-current={unchanged_local} (dir {TARGET_DIR})")
    if not args.apply:
        print("# dry run — pass --apply to write")


if __name__ == "__main__":
    main()