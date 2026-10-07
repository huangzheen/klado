#!/usr/bin/env python3
"""
Add a standalone HTML document to Klado → Workspace.

The document lands privately in the credential owner's **Workspace**. Its own
`{base}/r/{slug}` address requires the owner's login. To share, the owner publishes
a separate public snapshot in the web UI; its link works for signed-in users.
The legacy API path remains `/api/reports` for compatibility.

Typical use (agent-authored report):

    python3 scripts/publish_report.py report.html \\
        --title "Channel Review — Brand A vs Brand B, August 2026" \\
        --summary "Channel-level sell-out comparison across 6 channels." \\
        --category Analysis --tags "cooling arena,competitor"

Re-publishing the same `--slug` overwrites that report in place, so the command is safe
to re-run after editing the HTML. **Omit `--slug` and every run is a new card** — since
2026-09-27 a slug-less publish always gets `-YYYYMMDD-HHMM-xxxxxx` (timestamp + 6 random
characters) appended, so two publications can never share an address.

The document must be a **16:9 paged deck** (`<div class="deck">` + one
`<section class="slide">` per page — see knowledge doc `klado-v2:format-report-deck`): that is the project
rule, and this command refuses a long-form document unless you pass
`--allow-long-form`. The server reports `format` on every publish.

Notes
-----
* The document is stored as-is (≤ 8 MB). Use **relative** asset/API paths
  (`logo.png`, `api/products`): the server injects a `<base href>` for the
  runtime mount point, so the file keeps working if the app moves to a sub-path.
* `--base-url` is whatever address the app answers on — `http://localhost:8000`
  for a default local run. There is one install, so nothing here is a
  production/staging choice.
* **Send credentials.** Pass `--email`/`--password` (or set
  `KLADO_EMAIL`/`KLADO_PASSWORD`) for a registered account; they are sent as HTTP
  Basic, which is what agents use. `--token` / `KLADO_TOKEN` still works for a
  Bearer session token. An unauthenticated publish is refused with 401.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

DEFAULT_BASE_URL = ""
PROD_BASE_URL = ""

MAX_HTML_BYTES = 8 * 1024 * 1024


def _derive_title(html: str, fallback: str) -> str:
    """Prefer the document's own <title>, then its <h1>, then the file name."""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match and match.group(1).strip():
        return re.sub(r"\s+", " ", match.group(1)).strip()
    match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
    if match:
        text = re.sub(r"<[^>]+>", " ", match.group(1))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            return text
    return fallback


def _auth_header(args) -> str:
    """`Basic <base64(email:password)>` — the credential agents use."""
    if args.email:
        raw = f"{args.email}:{args.password or ''}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode()
    return f"Bearer {args.token}" if args.token else ""


def _request(base_url: str, path: str, method: str, payload: dict | None, auth: str) -> tuple[int, dict]:
    url = base_url.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if auth:
        headers["Authorization"] = auth
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw)
        except Exception:  # noqa: BLE001
            body = {"detail": raw.decode("utf-8", "replace")[:400]}
        return exc.code, body


def _fail(message: str) -> None:
    # A bare "HTTP 401" is the most likely thing anyone hits now that auth is on,
    # so say what to do about it instead of making them read the API's docstring.
    if "HTTP 401" in message:
        message += ("\n  → /api/* requires an account: add --email you@example.com --password <pw>"
                    " (or export KLADO_EMAIL / KLADO_PASSWORD)")
    elif "HTTP 403" in message:
        message += "\n  → the account was disabled by an admin; ask them to re-enable it"
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def cmd_list(args, auth: str) -> None:
    status, body = _request(args.base_url, "/api/reports?status=all&limit=500", "GET", None, auth)
    if status != 200:
        _fail(f"GET /api/reports → HTTP {status}: {body.get('detail')}")
    reports = body.get("reports") or []
    if not reports:
        print("(workspace is empty)")
        return
    print(f"{len(reports)} Workspace item(s) on {args.base_url}\n")
    for item in reports:
        flag = "📌 " if item.get("pinned") else "   "
        print(f"{flag}{item['slug']:<46} {item.get('status',''):<10} {item.get('title','')}")


def cmd_delete(args, auth: str) -> None:
    status, body = _request(args.base_url, f"/api/reports/{args.delete}", "DELETE", None, auth)
    if status != 200:
        _fail(f"DELETE → HTTP {status}: {body.get('detail')}")
    print(f"deleted: {args.delete}")


def cmd_publish(args, auth: str) -> None:
    source = Path(args.file)
    if not source.is_file():
        _fail(f"not a file: {source}")
    html = source.read_text(encoding="utf-8")
    size = len(html.encode("utf-8"))
    if size > MAX_HTML_BYTES:
        _fail(f"{source} is {size} bytes; the server limit is {MAX_HTML_BYTES} (8 MB)")

    # The project rule (2026-09-27) is that a published report is a 16:9 deck; long-form is
    # the exception. The server *reports* the format but does not refuse one yet — an
    # already-installed agent's SKILL.md is a static package, so a 400 there would break
    # publishing before the new rule reaches it. This command is ours, so it holds the line
    # locally, with an explicit escape hatch.
    if ('class="slide"' not in html and "class='slide'" not in html
            and not args.allow_long_form):
        _fail("no <section class=\"slide\"> — this is a long-form report, and the project "
              "rule is 16:9 paged decks (knowledge doc klado-v2:format-report-deck; "
              "klado-v2:format-report-html §1.3). Pass --allow-long-form to publish "
              "it anyway.")

    title = (args.title or "").strip() or _derive_title(html, source.stem)
    payload = {
        "title": title,
        "html": html,
        "summary": args.summary or "",
        # A bilingual document must carry a summary per language (the server refuses it
        # otherwise); pass them through when given so the script is not a way around the rule.
        "summary_en": args.summary_en or "",
        "summary_zh": args.summary_zh or "",
        "category": args.category or "Ad-hoc",
        "tags": args.tags or "",
        "author": args.author or "agent",
        "submitter": (args.submitter or "").strip(),
        "status": args.status,
        "pinned": bool(args.pin),
    }
    if args.slug:
        payload["slug"] = args.slug

    if args.clear_cover:
        payload["cover_url"] = ""
    elif args.cover:
        image = Path(args.cover)
        if not image.is_file():
            _fail(f"not an image file: {image}")
        payload["cover_base64"] = base64.b64encode(image.read_bytes()).decode("ascii")
    elif args.generate_cover:
        payload["generate_cover"] = True
        payload["cover_ratio"] = args.cover_ratio
        payload["cover_with_title"] = bool(args.cover_with_title)
        if args.cover_seed:
            payload["cover_seed"] = args.cover_seed

    if args.dry_run:
        preview = dict(payload)
        preview["html"] = f"<{size} bytes>"
        if "cover_base64" in preview:
            preview["cover_base64"] = f"<{len(preview['cover_base64'])} b64 chars>"
        print(json.dumps(preview, indent=2, ensure_ascii=False))
        return

    status, body = _request(args.base_url, "/api/reports", "POST", payload, auth)
    if status not in (200, 201):
        _fail(f"POST /api/reports → HTTP {status}: {body.get('detail')}")

    print(f"published: {body.get('slug')}")
    print(f"  title    {body.get('title')}")
    if body.get("submitter"):
        print(f"  submitter {body.get('submitter')}")
    print(f"  status   {body.get('status')}    size {size} bytes")
    if body.get("format"):
        print(f"  format   {body.get('format')}")
    if body.get("format_warning"):
        print(f"  ⚠️  {body.get('format_warning')}")
    if body.get("has_cover"):
        origin = body.get("cover_model") or "cover"
        print(f"  cover    {body.get('cover_w')}x{body.get('cover_h')} via {origin}"
              f"  -> {urljoin(args.base_url + '/', body.get('cover_url') or '')}")
    elif args.generate_cover or args.cover:
        print("  cover    (requested but the response carries none — check the server log)")
    # This is the owner's private address until they publish a public snapshot;
    # /api/reports/{slug}/raw also requires an authenticated owner.
    print(f"  open at  {args.base_url.rstrip('/')}/r/{body.get('slug')}")
    print(f"  workspace  {args.base_url.rstrip('/')}/?report={body.get('slug')}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add an HTML report to your private Klado Workspace.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("file", nargs="?", help="path to the report .html file")
    parser.add_argument("--title", help="card title (default: the document's <title> / <h1>)")
    parser.add_argument("--slug", help="stable id; re-using it overwrites that report. "
                                       "Omit it and the server appends -YYYYMMDD-HHMM-xxxxxx, "
                                       "so the run creates a NEW card")
    parser.add_argument("--allow-long-form", action="store_true",
                        help="publish a document with no <section class=\"slide\"> (long-form "
                             "is the exception; the rule is 16:9 paged decks)")
    parser.add_argument("--summary", help="one-liner shown on the card")
    parser.add_argument("--summary-en",
                        help="English one-liner — REQUIRED when the document is bilingual "
                             "(the server 400s a bilingual report without both summaries)")
    parser.add_argument("--summary-zh",
                        help="中文一句话摘要 — 双语报告必填（缺任一个服务端回 400）")
    parser.add_argument("--category", help="card chip, e.g. Analysis / Inventory / Ad-hoc")
    parser.add_argument("--tags", help="comma separated tags")
    parser.add_argument("--author", default=os.environ.get("KLADO_REPORT_AUTHOR", "agent"),
                        help="what generated the document (default: agent)")
    parser.add_argument("--submitter", default=os.environ.get("KLADO_SUBMITTER", ""),
                        help="who submits it — the Feishu username; shown on the card")
    parser.add_argument("--status", default="published", choices=["published", "draft", "archived"])
    parser.add_argument("--pin", action="store_true", help="pin the card to the top of the grid")
    cover = parser.add_argument_group("cover", "card cover image")
    cover.add_argument("--cover", metavar="PATH",
                       help="upload this image file as the card cover (jpeg/png/…)")
    cover.add_argument("--generate-cover", action="store_true",
                       help="ask the server to generate the cover from --title (MiniMax)")
    cover.add_argument("--cover-ratio", default="16:9",
                       help="cover aspect ratio (default 16:9, matches the card box)")
    cover.add_argument("--cover-with-title", action="store_true",
                       help="draw the title INTO the image (magazine-cover variant; default is "
                            "no text, because the card renders the title itself)")
    cover.add_argument("--cover-seed", type=int, help="reproduce a previous cover")
    cover.add_argument("--clear-cover", action="store_true", help="remove the existing cover")
    parser.add_argument("--base-url", default=os.environ.get("KLADO_BASE_URL", DEFAULT_BASE_URL),
                        help=f"target environment (default {DEFAULT_BASE_URL}; prod is {PROD_BASE_URL})")
    parser.add_argument("--email", default=os.environ.get("KLADO_EMAIL", ""),
                        help="account email; sent as HTTP Basic — required, every /api route needs an identity")
    parser.add_argument("--password", default=os.environ.get("KLADO_PASSWORD", ""),
                        help="account password (or set KLADO_PASSWORD)")
    parser.add_argument("--token", default=os.environ.get("KLADO_TOKEN", ""),
                        help="Bearer session token, an alternative to --email/--password")
    parser.add_argument("--list", action="store_true", help="list your Workspace items and exit")
    parser.add_argument("--delete", metavar="SLUG", help="delete a report and exit")
    parser.add_argument("--dry-run", action="store_true", help="print the payload without publishing")
    args = parser.parse_args()

    auth = _auth_header(args)
    if args.list:
        cmd_list(args, auth)
        return
    if args.delete:
        cmd_delete(args, auth)
        return
    if not args.file:
        parser.error("a report .html file is required (or use --list / --delete)")
    cmd_publish(args, auth)


if __name__ == "__main__":
    main()
