#!/usr/bin/env python3
"""Detect stale duplicate declarations in app.css.

Why this exists
---------------
`app.css` is one 7k-line file with no build step.  A selector may be declared
in several places, and because the cascade resolves equal specificity by source
order, **the last one silently wins**.  An agent that edits the copy near the
front of the file and re-measures gets a rule that never applies — the symptom
recorded in AGENTS.md as "you wrote a new flex rule and not one line of it
passed".  `grep -n` on the selector is the manual version of this check; this
is the version that cannot forget to run.

What counts as a finding
------------------------
Same at-rule context + same selector + same property + same importance, owned by
**more than one rule**.  Three things are deliberately NOT findings, because
counting them turns the guard into noise nobody reads:

  * rules in different at-rule contexts — a `@media` block overriding a desktop
    rule is how responsive CSS is *supposed* to work (app.css's mobile block is
    entirely `!important` by design);
  * `@keyframes` step selectors — `to { transform: … }` appears once per
    animation and is never an element selector;
  * the same selector twice inside ONE rule's comma list
    (`.sku-modal, /* modal */ .sku-modal { … }`) — redundant, not conflicting;
    reported separately as a lint, not as a conflict.

Reading the output
------------------
  conflict  two rules fight over one property; the later one wins. Either fold
            them or make the loser a *more specific* selector so the intent is
            visible in the cascade rather than depending on line order.
  lint      a selector listed twice in one rule. Cosmetic; safe to delete.
"""
import re
import sys
from collections import defaultdict

KEYFRAMES = ("@keyframes", "@-webkit-keyframes")


def _blank_comments(src):
    """Drop comments, preserving every byte offset and newline so line numbers
    stay true."""
    return re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), src, flags=re.S)


def _match_brace(text, open_idx):
    depth, i = 1, open_idx + 1
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def parse_rules(src):
    """-> list of (at_rule_stack, selector, body, line, rule_id, in_comma_dup)"""
    out = []
    counter = [0]

    def walk(chunk, ctx, offset):
        pos = 0
        while True:
            ob = chunk.find("{", pos)
            if ob == -1:
                return
            head = " ".join(chunk[pos:ob].split())
            close = _match_brace(chunk, ob)
            body = chunk[ob + 1:close - 1]
            line = offset + chunk.count("\n", 0, ob)
            if head.startswith(KEYFRAMES):
                pos = close
                continue
            if head.startswith("@"):
                walk(body, ctx + (head,), offset + chunk.count("\n", 0, ob) + 1)
                pos = close
                continue
            counter[0] += 1
            rid = counter[0]
            seen = set()
            for sel in head.split(","):
                sel = " ".join(sel.split())
                if not sel:
                    continue
                dup = sel in seen
                seen.add(sel)
                out.append((ctx, sel, body, line, rid, dup))
            pos = close

    walk(src, (), 1)
    return out


def scan(css_text):
    """-> (conflicts, lints)

    conflicts: {(ctx, selector, prop, important): [(rule_id, line, value), …]}
    lints:     {rule_id: [(selector, line), …]}
    """
    src = _blank_comments(css_text)
    rules = parse_rules(src)

    lints = defaultdict(list)
    for ctx, sel, body, line, rid, dup in rules:
        if dup:
            lints[rid].append((sel, line))

    decls = defaultdict(list)
    for ctx, sel, body, line, rid, dup in rules:
        for d in body.split(";"):
            d = " ".join(d.split())
            if not d or ":" not in d:
                continue
            prop, _, val = d.partition(":")
            decls[(ctx, sel, prop.strip().lower(), "important" in val.lower())].append(
                (rid, line, val.strip())
            )

    by_prop = defaultdict(list)
    for (ctx, sel, prop, imp), hits in decls.items():
        by_prop[(ctx, sel, prop)].append((imp, hits))

    conflicts = {}
    for (ctx, sel, prop), variants in by_prop.items():
        for imp, hits in variants:
            if len({h[0] for h in hits}) > 1:
                conflicts[(ctx, sel, prop, imp)] = sorted(hits)
    return conflicts, dict(lints)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "frontend/out/app.css"
    conflicts, lints = scan(open(path, encoding="utf-8").read())

    if lints:
        print(f"LINT  {len(lints)} rule(s) list a selector twice in the comma list:")
        for rid, sels in sorted(lints.items()):
            uniq = sorted({s for s, _ in sels})
            print(f"        rule #{rid} (line {sels[0][1]}): {', '.join(uniq)}")
        print()

    if not conflicts:
        print("OK    no conflicting duplicate declarations")
        return 0

    print(f"CONFLICT  {len(conflicts)} property/selector pair(s) declared by >1 rule")
    for (ctx, sel, prop, imp), hits in sorted(
        conflicts.items(), key=lambda kv: (-len(kv[1]), kv[0][1], kv[0][2])
    ):
        tag = f"  in {' '.join(ctx)}" if ctx else ""
        print(f"\n  {sel}{tag}   {prop}{'  /* !important */' if imp else ''}")
        values = {h[2] for h in hits}
        kind = "identical values — dead copy/paste residue" if len(values) == 1 else "DIFFERENT values"
        print(f"      {kind}")
        for rid, line, val in hits:
            print(f"        rule #{rid:<5} line {line:>5}: {val[:72]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
