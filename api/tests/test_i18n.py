"""Backend language negotiation and bilingual message picking.

The copy convention (`中文 / English`) is the translation memory, so the two
things worth pinning down are the splitting rule and the negotiation rule:

* what counts as a pair, and — more importantly — what must **not** (paths,
  numeric ranges, already-single-language strings);
* how a language is resolved from ``Accept-Language``, including the q-values
  and the "said nothing" case.

The "said nothing" case is a contract, not an omission: an agent that never
sends the header must keep receiving the whole `中文 / English` string, because
that is what every prompt and skill-pack example shows it. The frontend half is
covered by ``verify_i18n_ui.py``; this file is the server-side half.
"""
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.i18n import parse_accept_language, pick, request_lang, split_pair


def _request(header: str = "", state_lang=None):
    """The smallest stand-in that `request_lang` has to survive."""
    state = SimpleNamespace()
    if state_lang is not None:
        state.lang = state_lang
    return SimpleNamespace(state=state, headers={"accept-language": header})


class SplitPairTests(unittest.TestCase):
    def test_the_convention_splits_into_zh_and_en(self):
        self.assertEqual(split_pair("未登录 / not authenticated"),
                         ("未登录", "not authenticated"))

    def test_both_sides_of_the_slash_must_carry_whitespace(self):
        # This is the rule that protects API paths: "POST /api/reports" has no
        # space before its slash, so it can never be read as a pair. Requiring
        # the space on the left is deliberate, not a limitation.
        self.assertIsNone(split_pair("未登录/ not authenticated"))
        self.assertIsNone(split_pair("未登录 /not authenticated"))
        self.assertEqual(split_pair("未登录  /  not authenticated"),
                         ("未登录", "not authenticated"))

    def test_a_rewritten_api_path_is_never_a_pair(self):
        # "POST /api/..." has no space after the slash and no Chinese on the
        # left; treating it as a pair would corrupt every documented route.
        for text in ("POST /api/reports", "GET /api/ai/knowledge/search",
                     "see /api/data/datasets for details"):
            self.assertIsNone(split_pair(text), text)

    def test_a_numeric_range_is_never_a_pair(self):
        self.assertIsNone(split_pair("1 / 2"))
        self.assertIsNone(split_pair("2026 / 10"))

    def test_a_third_slash_splits_at_the_first_one(self):
        # A known sharp edge, pinned here so nobody "fixes" the expectation by
        # accident: the left group is lazy, so a three-part string is read as
        # (first / rest). The convention is therefore *exactly one* slash — the
        # reason the rule above demands whitespace on both sides is what keeps
        # ordinary text from ever landing here.
        self.assertEqual(split_pair("中文 / English / Third"),
                         ("中文", "English / Third"))

    def test_single_language_strings_are_left_alone(self):
        for text in ("", "Knowledge document not found", "文件不存在",
                     "Invalid token"):
            self.assertIsNone(split_pair(text), text)

    def test_the_right_side_must_start_with_a_letter(self):
        # A trailing figure ("耗时 5 分钟 / 5") is not a sentence, and splitting
        # it would produce a number-only "translation".
        self.assertIsNotNone(split_pair("耗时 5 分钟 / took 5 minutes"))
        self.assertIsNone(split_pair("倒计时 30 / 30"))


class PickTests(unittest.TestCase):
    def test_pick_returns_the_requested_side(self):
        message = "请登录后使用工作区 / sign in to access Workspace"
        self.assertEqual(pick(message, "zh"), "请登录后使用工作区")
        self.assertEqual(pick(message, "en"), "sign in to access Workspace")

    def test_no_preference_keeps_the_whole_bilingual_string(self):
        # The backward-compatibility guarantee for agents.
        message = "请登录后使用工作区 / sign in to access Workspace"
        self.assertEqual(pick(message, None), message)

    def test_an_unsupported_language_keeps_the_whole_string(self):
        message = "请登录后使用工作区 / sign in to access Workspace"
        self.assertEqual(pick(message, "fr"), message)

    def test_a_non_pair_message_is_returned_unchanged(self):
        for lang in ("zh", "en", None):
            self.assertEqual(pick("PermissionError: boom", lang), "PermissionError: boom")

    def test_interpolation_survives(self):
        self.assertEqual(pick("端口 {p} 不合法 / port {p} is invalid", "en"),
                         "port {p} is invalid")


class AcceptLanguageTests(unittest.TestCase):
    def test_the_common_browser_headers(self):
        cases = {
            "zh-CN,zh;q=0.9,en;q=0.8": "zh",
            "zh": "zh",
            "zh-TW": "zh",
            "en-US,en;q=0.9": "en",
            "en-GB": "en",
        }
        for header, expected in cases.items():
            self.assertEqual(parse_accept_language(header), expected, header)

    def test_quality_values_decide_the_winner(self):
        self.assertEqual(parse_accept_language("en;q=0.2,zh;q=0.9"), "zh")
        self.assertEqual(parse_accept_language("zh;q=0.3,en;q=0.8"), "en")

    def test_an_unrelated_language_yields_none(self):
        # Not "en" as a fallback: a French browser is not an English one, and
        # guessing would silently serve it copy it cannot read.
        for header in ("fr-FR,fr;q=0.9", "de", "", "xx-YY"):
            self.assertIsNone(parse_accept_language(header), header)

    def test_a_malformed_quality_value_does_not_raise(self):
        self.assertEqual(parse_accept_language("en;q=not-a-number,zh;q=0.5"), "zh")

    def test_request_lang_prefers_the_resolved_state(self):
        # The middleware already parsed it; the header is not re-read.
        self.assertEqual(request_lang(_request("en", state_lang="zh")), "zh")

    def test_request_lang_falls_back_to_the_header(self):
        self.assertEqual(request_lang(_request("zh-CN,zh;q=0.9")), "zh")

    def test_request_lang_survives_an_object_without_headers(self):
        # `current_identity()` is an error path, and the identity tests hand it a
        # bare stand-in. Reading a missing attribute here would turn a 401 into
        # a 500.
        self.assertIsNone(request_lang(SimpleNamespace(state=SimpleNamespace())))
        self.assertIsNone(request_lang(None))

    def test_request_lang_is_none_when_nothing_was_expressed(self):
        self.assertIsNone(request_lang(_request("")))


class EndToEndNegotiationTests(unittest.TestCase):
    """The contract an HTTP client actually observes."""

    def _client(self):
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse
        from fastapi.testclient import TestClient

        app = FastAPI()

        @app.get("/boom")
        async def boom(request: Request) -> JSONResponse:
            return JSONResponse({"detail": pick(
                "请登录后使用工作区 / sign in to access Workspace",
                request_lang(request))})

        return TestClient(app)

    def test_each_language_gets_its_own_side(self):
        client = self._client()
        for header, expected in (("zh-CN,zh;q=0.9", "请登录后使用工作区"),
                                 ("en-US,en;q=0.9", "sign in to access Workspace")):
            resp = client.get("/boom", headers={"Accept-Language": header})
            self.assertEqual(resp.json()["detail"], expected, header)

    def test_a_headerless_agent_gets_both_sides(self):
        resp = self._client().get("/boom")
        self.assertEqual(resp.json()["detail"],
                         "请登录后使用工作区 / sign in to access Workspace")


class SourceStringGuardTests(unittest.TestCase):
    """Two invariants that broke in practice and that nothing else would catch.

    Both are static checks over the SPA, run here so the normal test run guards
    them. `tools/audit_untranslatable_pairs.py` is the same two rules with a
    readable report.
    """

    def test_no_source_string_looks_bilingual_but_fails_to_split(self):
        # `里留了笔记。 /.` is the shape that bit the Inbox sentence: the English
        # side is bare punctuation, so the pair rule refuses it and the reader
        # gets both languages at once.
        from tools.audit_untranslatable_pairs import untranslatable_pairs

        hits = untranslatable_pairs()
        self.assertEqual(
            # ⚠️ `(file, line, text)`, not `(line, text)`: the scan covers every
            # hand-written source file that can put copy in front of a reader
            # (index.html AND office.js), and a message that hard-codes
            # "index.html" sends the next person to the wrong file.
            [f"{name}:{line}  {text}" for name, line, text in hits], [],
            "these strings mix Chinese and a spaced slash but will not split; "
            "either reword them or give the two sides their own data-lang spans")

    def test_no_pair_is_written_english_side_first(self):
        # A different failure from the one above, and the reason it needs its own
        # rule: the string DOES split, just not the way the convention means. Written
        # `Live page / 动态页面`, `split_pair` finds no CJK on the left and hands the
        # reader the whole thing — both languages — with no test output anywhere.
        from tools.audit_untranslatable_pairs import reversed_pairs

        hits = reversed_pairs()
        self.assertEqual(
            [f"{name}:{line}  {text}" for name, line, text in hits], [],
            "these pairs are English-first; the convention (and split_pair) needs "
            "中文 on the left")

    def test_the_reversed_pair_rule_actually_catches_one(self):
        # A guard that cannot fail is not a guard. Checked against the real source by
        # re-running the rule over a string in the shape that broke.
        from tools.audit_untranslatable_pairs import _REVERSED_PAIR
        self.assertIsNotNone(_REVERSED_PAIR.match("Live page / 动态页面"))
        self.assertIsNotNone(_REVERSED_PAIR.match("Delete / 删除"))
        # …and does not fire on the correct order, on a path, or on a number range.
        self.assertIsNone(_REVERSED_PAIR.match("动态页面 / Live page"))
        self.assertIsNone(_REVERSED_PAIR.match("POST /api/reports"))
        self.assertIsNone(_REVERSED_PAIR.match("1 / 2"))

    def test_no_native_dialog_is_left_behind(self):
        # window.confirm/alert/prompt cannot be styled and cannot be localized at
        # all. kldDialog replaces all three.
        from tools.audit_untranslatable_pairs import native_dialogs

        hits = native_dialogs()
        self.assertEqual(
            [f"{name}:{line}  {text}" for name, line, text in hits], [],
            "use kldDialog.confirm / .prompt / .notify instead of the native dialogs")

    def test_kld_dialog_localises_what_it_is_given(self):
        # The dialog picks the side itself, at open time. Left to the DOM walker
        # it would be a frame late, would never see a `placeholder` (a property
        # assignment is not a mutation), and could not survive a language switch
        # while open — the pair is gone once one side has been taken.
        src = (Path(__file__).resolve().parents[2] / "frontend" / "out" / "index.html")
        block = src.read_text(encoding="utf-8")
        start = block.index("const kldDialog")
        end = block.index("window.kldDialog = kldDialog", start)
        dialog = block[start:end]
        self.assertIn("kladoI18n.t", dialog)
        for field in ("opts.title", "opts.body", "opts.label", "opts.note",
                      "opts.placeholder"):
            self.assertIn(f"_t({field})", dialog, field)
        # The default buttons ship as pairs, so they follow the language too.
        self.assertIn("_t(opts.ok || '确定 / OK')", dialog)
        self.assertIn("_t(opts.cancel || '取消 / Cancel')", dialog)


class DictDirectionTests(unittest.TestCase):
    """`DICT.zh` must be English→Chinese. An entry filed the other way is wrong in
    BOTH directions, and neither half of the UI complains.

    Found the hard way: twenty-one Chinese-first entries (`'作废': 'Revoke'`,
    `'搜索…': 'Search…'`, …) sat in the `zh` block. The mirroring that builds
    `DICT.en` assumes every `DICT.zh` entry is English→Chinese, so it produced
    `DICT.en['Revoke'] = '作废'` — Chinese keyed by English — and `side()` then
    handed that Chinese string to an English page verbatim. The visible symptom was
    one `placeholder` in a hidden overlay, with the `aria-label` beside it correctly
    in English, because only the placeholder had a dictionary entry at all.
    """

    SCRIPT = Path(__file__).resolve().parents[2] / "frontend" / "out" / "i18n.js"

    #: Deliberate divergence, each with the reason it earns its place. The two port
    #: labels are spelled the way the reader's own settings screen spells them —
    #: fullwidth parentheses in Chinese, halfwidth in English — because an operator
    #: matching a string against a config file needs the one from their own screen.
    DELIBERATE = {"SSL（465）", "STARTTLS（587）"}

    def _blocks(self):
        text = self.SCRIPT.read_text(encoding="utf-8")
        zh_at = text.index("\n    zh: {")
        en_at = text.index("\n    en: {")
        return text[zh_at:en_at], text[en_at:]

    def _entries(self, block):
        """(key, value) for every `'k': 'v',` line, continuation lines joined.

        Parsed with a regex rather than executed: this file is the single source of
        the UI's translations, and running it to check it means a syntax error becomes
        a test error instead of a browser error.
        """
        import re
        out = []
        pending_key = None
        buf = ""
        for raw in block.splitlines():
            m = re.match(r"\s*'((?:[^'\\]|\\.)*)'\s*:\s*(.*)$", raw)
            if m:
                pending_key, buf = m.group(1), m.group(2)
            elif pending_key is not None and raw.strip():
                buf += " " + raw.strip()
            if pending_key is not None and re.search(r",\s*$", buf):
                value = re.sub(r",\s*$", "", buf.strip()).strip()
                if value.startswith("'") and value.endswith("'"):
                    out.append((pending_key, value[1:-1]))
                pending_key, buf = None, ""
        return out

    def test_no_zh_entry_maps_to_a_non_chinese_value(self):
        import re
        cjk = re.compile(r"[一-鿿㐀-䶿]")
        zh_block, _en = self._blocks()
        offenders = [(k, v) for k, v in self._entries(zh_block)
                     if not cjk.search(v) and k not in self.DELIBERATE]
        self.assertEqual(
            [f"'{k}' -> '{v}'" for k, v in offenders], [],
            "these are Chinese→English entries sitting in DICT.zh. Move them into "
            "DICT.en: the mirroring below reverses every zh entry, so a misfiled one "
            "becomes DICT.en[english] = chinese and renders Chinese on an English page.")

    def test_the_allowlist_is_not_growing(self):
        """An exemption with no reason is how a real regression gets waved through.

        `i18n.js` carries the same allowlist in `DELIBERATE`, with the reasoning next
        to each key. If the two drift, the runtime check and this test disagree about
        what is wrong, and the runtime one is the one a user sees.
        """
        text = self.SCRIPT.read_text(encoding="utf-8")
        at = text.index("var DELIBERATE = {")
        block = text[at:text.index("};", at)]
        import re
        allowed = set(re.findall(r"'([^']+)'\s*:", block))
        self.assertEqual(
            allowed, self.DELIBERATE,
            "i18n.js DELIBERATE and this test's DELIBERATE have drifted; keep the "
            "reason for each exemption in i18n.js where the check runs")


class ExportSurfaceTests(unittest.TestCase):
    """Everything a module exports must exist.

    `node --check` parses; it does not resolve identifiers, and the static i18n guards
    never execute the file. So deleting a helper and leaving its name in the export
    object is invisible to all three — and it is a `ReferenceError` at module scope,
    which means the page throws while loading and renders nothing.

    That is not hypothetical: removing the console's dead `dict()` left `dict: dict` in
    `api-admin/frontend/i18n.js`'s export, and every console page came up blank with
    `dict is not defined`. The only thing that caught it was a browser script, sixteen
    minutes of console work later.
    """

    SCRIPTS = (
        Path(__file__).resolve().parents[2] / "frontend" / "out" / "i18n.js",
        Path(__file__).resolve().parents[2] / "api-admin" / "frontend" / "i18n.js",
    )

    def test_every_exported_name_is_defined_in_the_same_file(self):
        """Every bare identifier used as a value in the export object must resolve.

        `node --check` parses; it does not resolve identifiers, and the static i18n
        guards never execute the file. So deleting a helper and leaving its name in the
        export object is invisible to all three — and it is a `ReferenceError` at module
        scope, which means the page throws while loading and renders nothing.

        That is not hypothetical: removing the console's dead `dict()` left `dict: dict`
        in `api-admin/frontend/i18n.js`'s export, and every console page came up blank
        with `dict is not defined`. The only thing that caught it was a browser script.

        Only BARE identifiers are checked. The main app exports `lang: function () {...}`,
        which is a function expression and needs no declaration of its own — asserting
        `function <name>(` for every key would fail on a correct file.
        """
        import re
        bare = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*$")
        for path in self.SCRIPTS:
            text = path.read_text(encoding="utf-8")
            match = re.search(r"(\w+)\.kladoI18n\s*=\s*\{(.*?)\n  \};", text, re.S)
            self.assertIsNotNone(match, f"{path.name}: no export object found")
            values = re.findall(r"(\w+)\s*:\s*([^,\n]+)", match.group(2))
            self.assertTrue(values, f"{path.name}: export object is empty")
            for key, raw in values:
                value = raw.strip()
                if not bare.match(value):
                    continue          # a function expression, a literal, a property read
                self.assertRegex(
                    text,
                    rf"(function\s+{re.escape(value)}\s*\(|"
                    rf"(var|let|const)\s+{re.escape(value)}\s*[=;])",
                    f"{path.name} exports `{key}: {value}`, and `{value}` is not "
                    f"declared in that file — a ReferenceError at module scope, so the "
                    f"page throws while loading. `node --check` cannot see this: it "
                    f"parses without resolving identifiers.")

    def test_no_dead_dictionary_is_left_behind(self):
        """The console uses pairs only. A dictionary there would be a trap.

        `if (DICT[text]) return DICT[text]` ignores the current language, so wiring one
        into `translate()` would reproduce the main app's direction bug. Keeping the map
        around "in case" is the dangerous part, not the function — and a comment that
        names it is not a declaration, so comments are stripped before this looks.
        """
        import re
        console = self.SCRIPTS[1].read_text(encoding="utf-8")
        code = re.sub(r"/\*.*?\*/", "", console, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        self.assertNotRegex(
            code, r"\bDICT\b",
            "the console's i18n.js has a DICT again — it is pairs-only by design, and "
            "that map cannot tell the two languages apart")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
