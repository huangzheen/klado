"""Guard: every user-facing string must be translatable, and no native dialogs.

Two invariants that both broke in practice while the bilingual build went in, and
that nothing else in the test suite would have caught:

1. **A string that looks bilingual but will not split reaches the reader with both
   languages at once.** `core/i18n.py::split_pair` requires CJK on the left, a
   letter on the right, and whitespace on both sides of the slash. A sentence whose
   English side is bare punctuation — `里留了笔记。 /.` — fails all three, so the
   reader sees the raw `中文 / English`. Scanned with the runtime's own regex, so
   the verdict cannot drift from the behaviour.

2. **The native dialogs came back.** `window.confirm/alert/prompt` cannot be styled
   and cannot be localized at all, and two of the call sites were English-only in an
   app that otherwise speaks the reader's language. `kldDialog` replaces all three;
   it is the only sanctioned way (see AGENTS.md).

Only *source* strings are judged. `frontend/out/i18n.js` holds the `DICT`, whose
entries are translation targets — a Chinese value there is the point, not a leak —
and its keys are checked transitively through the HTML scan.

Run directly for a readable report; `tests/test_i18n.py` asserts the same rules.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "api"))

from core.i18n import split_pair  # noqa: E402

INDEX = REPO / "frontend" / "out" / "index.html"
# ⚠️ Every hand-written source file that can put copy in front of a reader.
# office.js joined the list with the Agent office (2026-10-05): it builds the
# status cards and the roster in JS, so a guard that only reads index.html
# watches a file that is no longer where the strings live. That failure is
# silent, which is the worst kind — the audit still prints "✅ 三项守卫都通过"
# while the office is full of unsplittable pairs.
SOURCES = [
    INDEX,
    REPO / "frontend" / "out" / "office.js",
]

CJK = r"\u4e00-\u9fff"
# Deliberately as loose as possible: any string with CJK *and* a slash *and*
# something after it goes to `split_pair` to judge. The pre-filter must NOT
# require the spaces around the slash — `来源：/ Source:` (one space short) is
# exactly the kind of near-miss that reaches the reader as a bilingual string,
# and a stricter pre-filter would wave it through.
LOOKS_LIKE_A_PAIR = re.compile(rf"^[\s\S]*[{CJK}][\s\S]*/[\s\S]+$")
# JS single/double-quoted literals (escapes resolved just enough to compare).
LITERAL = re.compile(r"""(?<![\w$])(['"])((?:\\.|(?!\1)[^\\\n])*)\1""")
# `kldDialog` defines `confirm` itself, and the migration note names the natives.
NATIVE_DIALOG = re.compile(
    r"window\.(?:alert|confirm|prompt)\(|(?<![\.\w])(?:alert|confirm)\(")


def untranslatable_pairs() -> list[tuple[str, int, str]]:
    """`(file, line, text)` for every source string that will not split."""
    hits: list[tuple[str, int, str]] = []
    for path in SOURCES:
        src = path.read_text(encoding="utf-8")
        for m in LITERAL.finditer(src):
            # Third-party bundles are inlined verbatim (SheetJS's `var XLSX={}` date
            # table is one of them) and minified onto a single very long line. Our own
            # copy is hand-written and never sits on a line like that — and a library's
            # internal format strings are not something the pair rule should be asked
            # about.
            line_start = src.rfind("\n", 0, m.start()) + 1
            if "\n" not in src[line_start:line_start + 4000]:
                continue
            text = (m.group(2).replace("\\'", "'").replace('\\"', '"')
                    .replace("\\n", "\n"))
            if not re.search(f"[{CJK}]", text):
                continue
            # Markup is not copy. These strings are HTML fragments assembled with
            # `+=`, and every one of them contains a `</span>`-style closing tag whose
            # slash would otherwise read as a pair separator.
            if re.search(r"</?[a-zA-Z]", text):
                continue
            # A literal that *ends* at its slash is half a pair: the rendered string is
            # the concatenation, and the right-hand side arrives in a later literal
            # (`'… 事件 / ' + 'Show … events'`). Judged alone it could never split, but
            # it is never shown alone either.
            if text.rstrip().endswith(("/", "／")):
                continue
            if not LOOKS_LIKE_A_PAIR.match(text):
                continue
            if split_pair(text) is not None:
                continue
            hits.append((path.name, src.count("\n", 0, m.start()) + 1,
                         " ".join(text.split())))
    return hits


_REVERSED_PAIR = re.compile(
    rf"^\s*(?P<en>[A-Za-z][^/]*?)\s*/\s*(?P<zh>[{CJK}][^/]*?)\s*$")


def reversed_pairs() -> list[tuple[str, int, str]]:
    """`(file, line, text)` for pairs written English-first.

    The convention is `中文 / English`, and `split_pair` depends on it: the left side
    has to carry CJK. Write it the other way round and the whole pair is handed to
    the reader verbatim — **both** languages, which is precisely the failure this
    file exists to catch. It is a one-character slip that no compiler sees, and the
    only symptom is a card reading "Live page / 动态页面" to an English reader.

    A separate rule from `untranslatable_pairs` because that one only asks "does it
    split"; this asks "does it split *and* is it the right way round".
    """
    hits: list[tuple[str, int, str]] = []
    for path in SOURCES:
        src = path.read_text(encoding="utf-8")
        for m in LITERAL.finditer(src):
            line_start = src.rfind("\n", 0, m.start()) + 1
            if "\n" not in src[line_start:line_start + 4000]:
                continue
            text = (m.group(2).replace("\\'", "'").replace('\\"', '"')
                    .replace("\\n", "\n"))
            if not re.search(f"[{CJK}]", text):
                continue
            if re.search(r"</?[a-zA-Z]", text):
                continue
            if not _REVERSED_PAIR.match(text):
                continue
            # Documented exception, not an oversight. The language toggle's tooltip is
            # generated per language by `i18n.js::syncToggle()`; writing it as a
            # `中文 / English` pair would make `processElement` re-derive both sides on
            # every pass and fight that function for the same attribute. See AGENTS.md.
            if "Language / 语言" in text:
                continue
            hits.append((path.name, src.count("\n", 0, m.start()) + 1,
                         " ".join(text.split()) + "   ← 应为 中文 / English"))
    return hits



def native_dialogs() -> list[tuple[str, int, str]]:
    """`(file, line, text)` for every leftover native dialog call site."""
    out = []
    for path in SOURCES:
        for n, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            if "kldDialog" in line or "replaces" in line:
                continue
            if NATIVE_DIALOG.search(line) and "function confirm" not in line:
                out.append((path.name, n, line.strip()))
    return out


def main() -> int:
    failed = False
    for title, hits in (("拆不开的双语串（读者会同时看到两种语言）", untranslatable_pairs()),
                        ("中英写反的 pair（应为 中文 / English）", reversed_pairs()),
                        ("残留的原生弹窗（应一律用 kldDialog）", native_dialogs())):
        print(f"\n=== {title}：{len(hits)} 处 ===")
        for name, line, text in hits:
            print(f"  {name}:{line}  {text[:110]}")
        failed = failed or bool(hits)
    print("\n" + ("❌ 有待修的项" if failed else "✅ 三项守卫都通过"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
