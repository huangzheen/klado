"""The module registry and its resolution order.

`core/modules.py` is the single answer to "what exists and who may use it". These
are pure-logic tests over that answer — no server, no database — because the
expensive mistake here is not a wrong answer but a rule that quietly stops being
applied: a module nobody can turn off, a dependency that is not pulled in, a
retired key that starts crashing startup.
"""
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from core import modules as M


class RegistryShapeTests(unittest.TestCase):
    def test_every_route_prefix_in_the_app_is_claimed(self):
        # The gate answers 403 for a path no module claims. That is deliberate —
        # a route added without registering its module should fail closed — so
        # the list below is the contract, and a new router prefix that is not in
        # `core/modules.py` is a bug in the registry, caught here rather than in
        # production where it reads as "the app is broken".
        for prefix in (
            "/api/auth", "/api/health", "/api/storage", "/api/annotations",
            "/api/data-center", "/api/inbox", "/api/reports", "/api/export",
            "/api/knowledge", "/api/ai/knowledge", "/api/calendar",
            "/api/settings", "/api/dashboard",
        ):
            self.assertIsNotNone(M.module_for_path(prefix), prefix)
            self.assertIsNotNone(M.module_for_path(prefix + "/something"), prefix)

    def test_standalone_document_pages_map_to_their_module(self):
        self.assertEqual(M.module_for_path("/r/some-slug").key, M.WORKSPACE)
        self.assertEqual(M.module_for_path("/e/some-slug").key, M.CALENDAR)
        self.assertEqual(M.module_for_path("/d/some-slug").key, M.DASHBOARD)

    def test_infrastructure_has_no_tab(self):
        # `core` answers endpoints but is not a product module: it must not turn
        # into a navigation entry, or every account gets a "Core" tab.
        core = M.BY_KEY[M.CORE]
        self.assertTrue(core.required)
        self.assertFalse(core.toggleable)
        self.assertFalse(core.in_nav)

    def test_catalogue_is_ordered_and_carries_what_the_picker_needs(self):
        cat = M.catalogue()
        orders = [c["order"] for c in cat]
        self.assertEqual(orders, sorted(orders))
        for entry in cat:
            for key in ("key", "label_zh", "label_en", "blurb_zh", "blurb_en",
                        "icon", "required", "toggleable", "in_nav", "requires"):
                self.assertIn(key, entry)

    def test_every_nav_mark_is_a_file_that_actually_exists(self):
        # `nav_mark` is a bare filename handed straight to `<img src=…>`, resolved
        # against the served `frontend/out`. Nothing at runtime checks it: a name
        # with no file behind it renders as a **broken image**, not an error, and
        # the tab keeps its box so the nav still lays out normally. That is exactly
        # what happened to `datacenter-icon.png` — declared in the registry from
        # the start, file added much later, so the Data Center tab sat there with a
        # dead `<img>` and nothing said so. Assert the file, not the name.
        served = Path(__file__).resolve().parents[2] / "frontend" / "out"
        marks = {m.key: m.nav_mark for m in M.MODULES if m.nav_mark}
        self.assertTrue(marks, "no module declares a nav_mark — did the drawn marks get lost?")
        for key, mark in marks.items():
            self.assertTrue((served / mark).is_file(), f"{key}: nav_mark={mark!r} 不在 {served}")

    def test_nav_mark_is_reached_by_the_nav_tab_css(self):
        # A tab whose mark is an `<img class="workspace-mark">` (display:block)
        # needs its own selector in the `display:inline-flex` rule, or the mark
        # takes a line of its own and the label is pushed below it — the tab grows
        # from 34px to ~53px and nothing complains. `renderNav()` builds the id
        # from the module key; the promoted dashboard tabs have no id and go in as
        # a class instead. Both spellings have to be present.
        css = (Path(__file__).resolve().parents[2] / "frontend" / "out" / "app.css").read_text()
        block = re.search(r"([^{}]*\.nav-tab-dash[^{}]*)\{[^}]*display:\s*inline-flex", css)
        self.assertIsNotNone(block, "app.css 里找不到带 .nav-tab-dash 的 inline-flex 规则")
        selectors = block.group(1)
        for m in M.MODULES:
            if not m.nav_mark:
                continue
            self.assertIn(f"#nav-tab-{m.key}", selectors,
                          f"{m.key}: 带 nav_mark 却没进 app.css 的 inline-flex 选择器")
        self.assertIn(".nav-tab-dash", selectors)


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        """Pin the configuration source to `KLADO_MODULES`.

        ⚠️ `deployment_default()` has TWO sources and the table wins (that is the whole
        point of the console's Modules page — a live control surface has to be able to
        change what the app enforces). So these tests, which only ever patched the
        environment variable, were reading a row out of the real `app_settings` table:
        they claimed "no database" and were wrong. On a machine where a console run had
        once saved an allowlist they silently tested the leftover row instead of the
        variable they set, and failed with an answer that had nothing to do with the
        rule under test.

        Every test here is about the ENVIRONMENT contract, so the table is stubbed out
        as "nothing saved" and the precedence itself is tested separately, in
        `AllowlistSourceTests` below.
        """
        self._table = patch.object(M, "_table_keys", return_value=None)
        self._table.start()
        self.addCleanup(self._table.stop)

    def test_unset_config_means_every_module(self):
        # An existing install must keep working with no `.env` change.
        with patch.object(M.settings, "KLADO_MODULES", ""):
            self.assertEqual(set(M.deployment_default()), set(M.ALL_KEYS))

    def test_a_configured_allowlist_closes_over_dependencies(self):
        with patch.object(M.settings, "KLADO_MODULES", "dashboard"):
            keys = M.deployment_default()
        self.assertIn(M.DASHBOARD, keys)
        self.assertIn(M.DATACENTER, keys)      # dashboard requires it
        self.assertIn(M.INBOX, keys)           # required module
        self.assertIn(M.SETTINGS, keys)
        self.assertNotIn(M.WORKSPACE, keys)

    def test_a_typo_in_config_is_an_error_not_a_silent_disable(self):
        with patch.object(M.settings, "KLADO_MODULES", "datacenter,workspce"):
            with self.assertRaises(M.ModuleConfigError) as exc:
                M.deployment_default()
        self.assertIn("workspce", str(exc.exception))

    def test_an_account_can_narrow_but_never_widen(self):
        with patch.object(M.settings, "KLADO_MODULES", "datacenter,workspace"):
            base = M.deployment_default()
            self.assertNotIn(M.KNOWLEDGE, base)
            # Asking for a module the deployment does not offer is ignored.
            widened = M.resolve({M.KNOWLEDGE: True})
            self.assertNotIn(M.KNOWLEDGE, widened)
            # Asking to drop one it does offer works.
            self.assertNotIn(M.WORKSPACE, M.resolve({M.WORKSPACE: False}))

    def test_required_modules_survive_an_account_asking_to_drop_them(self):
        with patch.object(M.settings, "KLADO_MODULES", ""):
            keys = M.resolve({M.INBOX: False, M.SETTINGS: False, M.CORE: False})
        self.assertIn(M.INBOX, keys)
        self.assertIn(M.SETTINGS, keys)
        self.assertIn(M.CORE, keys)

    def test_a_retired_module_in_an_old_row_is_ignored(self):
        # A row written before a module was removed must not crash every request.
        with patch.object(M.settings, "KLADO_MODULES", ""):
            keys = M.resolve({"competitor-desk": True})
        self.assertNotIn("competitor-desk", keys)
        self.assertIn(M.WORKSPACE, keys)

    def test_disabling_a_module_does_not_break_the_ones_that_need_it(self):
        # Narrowing must never produce a set that cannot serve itself.
        for key in M.ALL_KEYS:
            with patch.object(M.settings, "KLADO_MODULES", ""):
                keys = M.resolve({k: False for k in M.ALL_KEYS if k != key})
            for dep in M.BY_KEY[key].requires:
                if key in keys:
                    self.assertIn(dep, keys, f"{key} is on but its dependency {dep} is not")

    def test_for_client_reports_locked_only_for_switchable_modules(self):
        with patch.object(M.settings, "KLADO_MODULES", "datacenter,workspace"):
            payload = M.for_client(M.deployment_default())
        self.assertNotIn(M.INBOX, payload["locked"])        # required, not a choice
        self.assertNotIn(M.CORE, payload["locked"])
        self.assertIn(M.KNOWLEDGE, payload["locked"])
        self.assertIn(M.DASHBOARD, payload["locked"])
        self.assertEqual(payload["enabled"],
                         [M.CORE, M.DATACENTER, M.INBOX, M.WORKSPACE, M.SETTINGS])
        self.assertFalse(next(c for c in payload["catalogue"]
                              if c["key"] == M.CORE)["in_nav"])


class CompanyLicenceTests(unittest.TestCase):
    """`org_offer()` and `resolve(licence=…)` — the per-company ceiling.

    ⚠️ The pin to `KLADO_MODULES` is repeated rather than inherited, because this class
    sits outside `ResolutionTests` and `deployment_default()` reads the console's table
    first. Without it these tests would be asserting against whatever an operator last
    saved — the exact failure `ResolutionTests.setUp` was written to prevent.
    """

    def setUp(self):
        self._table = patch.object(M, "_table_keys", return_value=None)
        self._table.start()
        self.addCleanup(self._table.stop)

    def test_no_licence_means_the_whole_switchable_registry(self):
        with patch.object(M.settings, "KLADO_MODULES", ""):
            offer = M.org_offer(None)
        self.assertIn(M.WORKSPACE, offer)
        self.assertIn(M.CALENDAR, offer)
        # Required infrastructure is not a choice, so it is not an option either.
        self.assertNotIn(M.INBOX, offer)
        self.assertNotIn(M.CORE, offer)
        self.assertNotIn(M.SETTINGS, offer)
        self.assertNotIn(M.DATACENTER, offer)

    def test_a_licence_narrows_and_never_widens(self):
        with patch.object(M.settings, "KLADO_MODULES", ""):
            offer = M.org_offer([M.WORKSPACE, M.CALENDAR])
        self.assertEqual(set(offer), {M.WORKSPACE, M.CALENDAR})

    def test_a_licence_cannot_manufacture_a_module_the_deployment_withdrew(self):
        """⚠️ The order is the contract: deployment first, licence second. Reversed, a
        company would be offered a module the installation has switched off, the
        administrator would tick it, the write would be accepted, and `resolve()` would
        ignore the row — a checkbox that reports success and does nothing."""
        with patch.object(M.settings, "KLADO_MODULES", M.WORKSPACE):
            offer = M.org_offer([M.WORKSPACE, M.CALENDAR])
        self.assertEqual(set(offer), {M.WORKSPACE})

    def test_an_empty_licence_is_honoured_as_nothing(self):
        with patch.object(M.settings, "KLADO_MODULES", ""):
            self.assertEqual(M.org_offer([]), ())

    def test_a_required_module_in_the_licence_is_ignored_not_obeyed(self):
        with patch.object(M.settings, "KLADO_MODULES", ""):
            self.assertEqual(M.org_offer([M.INBOX]), ())

    def test_an_unknown_key_in_the_licence_is_ignored(self):
        with patch.object(M.settings, "KLADO_MODULES", ""):
            self.assertEqual(set(M.org_offer([M.WORKSPACE, "no-such"])), {M.WORKSPACE})

    def test_resolve_narrows_by_the_licence(self):
        with patch.object(M.settings, "KLADO_MODULES", ""):
            keys = set(M.resolve({}, licence=[M.WORKSPACE]))
        self.assertIn(M.WORKSPACE, keys)
        self.assertNotIn(M.CALENDAR, keys)
        # Required infrastructure survives: a company that could be switched off from
        # its own inbox and settings would have no way to be told what happened to it.
        self.assertIn(M.INBOX, keys)
        self.assertIn(M.SETTINGS, keys)

    def test_a_stale_grant_cannot_survive_a_narrowed_licence(self):
        """The case that justifies touching `resolve()` at all. A row written while the
        company was licensed for the module stays in `account_modules` forever; without
        the licence in the resolution it would keep granting access after the operator
        withdrew it, and the console would be a screen that does nothing."""
        with patch.object(M.settings, "KLADO_MODULES", ""):
            keys = set(M.resolve({M.CALENDAR: True}, licence=[M.WORKSPACE]))
        self.assertNotIn(M.CALENDAR, keys)
        self.assertIn(M.WORKSPACE, keys)

    def test_no_licence_keeps_the_answer_it_always_gave(self):
        with patch.object(M.settings, "KLADO_MODULES", ""):
            self.assertEqual(set(M.resolve({})), set(M.ALL_KEYS))
            self.assertEqual(set(M.resolve({M.CALENDAR: False})),
                             set(M.ALL_KEYS) - {M.CALENDAR})


class AllowlistSourceTests(unittest.TestCase):
    """Which of the two sources wins, and what each one does with a bad value.

    This is the console's contract, and it had no test of its own: `ResolutionTests`
    only ever touched the environment variable, so the table — the source the operator
    actually uses — was unpinned, untested, and quietly answering instead.

    `_table_keys` is stubbed rather than mocked at the database, so nothing here needs a
    server. The three behaviours that differ between the sources are the point:

    * precedence — the table wins, or the console could never change anything;
    * an unknown key is an ERROR from the environment (a startup typo) and a silent
      drop from the table (edited live, must not take the app down);
    * an empty list is an empty list, not "unset" — an operator who unchecks everything
      has said something, and "every module" is a different statement.
    """

    def test_the_table_wins_over_the_environment_variable(self):
        with patch.object(M, "_table_keys", return_value=("inbox",)), \
             patch.object(M.settings, "KLADO_MODULES", "calendar,knowledge"):
            keys = M.deployment_default()
        self.assertIn(M.INBOX, keys)
        self.assertNotIn(M.CALENDAR, keys,
                         "the console's saved allowlist must actually be enforced")
        self.assertNotIn(M.KNOWLEDGE, keys)

    def test_nothing_saved_falls_through_to_the_environment(self):
        with patch.object(M, "_table_keys", return_value=None), \
             patch.object(M.settings, "KLADO_MODULES", "calendar"):
            self.assertIn(M.CALENDAR, M.deployment_default())

    def test_an_empty_saved_list_is_empty_not_unset(self):
        """⚠️ Required modules come back because `close_over_requires` puts them there,
        which is what keeps an inbox after somebody unchecks everything. The point of
        the assertion is that the switchable ones are GONE — a falsy read that treated
        `[]` as "no opinion" would hand back the whole product instead."""
        with patch.object(M, "_table_keys", return_value=()), \
             patch.object(M.settings, "KLADO_MODULES", ""):
            keys = set(M.deployment_default())
        for required in (M.CORE, M.DATACENTER, M.INBOX, M.SETTINGS):
            self.assertIn(required, keys)
        for switchable in (M.WORKSPACE, M.DASHBOARD, M.KNOWLEDGE, M.CALENDAR):
            self.assertNotIn(switchable, keys)

    def test_an_unknown_key_is_fatal_from_the_env_and_survivable_from_the_table(self):
        """The asymmetry is deliberate and is the difference between a startup error and
        an outage. `KLADO_MODULES` is read before anything is served; a table value can
        be edited while the app is running, so a bad row must fail CLOSED (the module
        stays off) rather than raise on a request path."""
        with patch.object(M, "_table_keys", return_value=None), \
             patch.object(M.settings, "KLADO_MODULES", "workspce"):
            with self.assertRaises(M.ModuleConfigError):
                M.deployment_default()

        # ⚠️ The REAL `_table_keys` here, with only the storage read stubbed — the
        # unknown-key handling and the warning live inside it, so stubbing the function
        # would have tested a thing that does not exist.
        from klado_shared import deployment as shared_deployment
        with patch.object(shared_deployment, "read",
                          return_value={"keys": ["inbox", "workspce"]}), \
             patch.object(M.settings, "KLADO_MODULES", ""):
            with self.assertLogs(M._LOG, level="WARNING"):
                keys = M._configured_keys()
        self.assertIn(M.INBOX, keys)
        self.assertNotIn("workspce", keys)

    def test_an_unreadable_table_falls_back_to_the_environment(self):
        """⚠️ A settings read that raises must not take the app down. `_table_keys`
        swallows and returns None, so the environment answer is still available — a
        database hiccup narrowing the product to four modules would be a far worse
        outcome than ignoring the row."""
        from klado_shared import deployment as shared_deployment
        with patch.object(shared_deployment, "read", side_effect=RuntimeError("db down")), \
             patch.object(M.settings, "KLADO_MODULES", "calendar"), \
             self.assertLogs(M._LOG, level="WARNING"):
            keys = M.deployment_default()
        self.assertIn(M.CALENDAR, keys)


class EveryRealRouteIsClaimedTests(unittest.TestCase):
    """Every route the app actually serves must belong to a module.

    ⚠️ There used to be a test here, and it was the wrong shape. It asserted that a
    hand-written list of prefixes was claimed — so it only ever checked prefixes somebody
    remembered to add to it. `/api/org/*` was not on that list, was not in any module,
    and the middleware therefore answered **403 for the whole enterprise-administration
    API in the running app** while every unit test passed: they mount the router on a
    bare FastAPI app, without the middleware that refuses unclaimed paths.

    So this enumerates the routers themselves. A new router file, or a new prefix on an
    existing one, is now covered the day it is written rather than the day somebody
    thinks to update a list.

    The two exemptions are both deliberate and both checked here rather than skipped in
    place: `/api/health` and `/api/auth/admin/*` are the operator's own surfaces, which
    `core` claims by prefix anyway, and the static mount is not a module's business.
    """
    #: Routers that are not request surfaces at all.
    SKIP_FILES = {"__init__.py"}

    def _install_map(self):
        """`{router attribute: mount prefix}`, read out of `main.py`.

        ⚠️ The prefix is NOT in the router. Almost every module here declares
        `APIRouter()` with no prefix and `main.py` supplies it at mount time, so
        enumerating `router.routes` alone yields twelve relative paths and every
        `/api/...` lookup misses. `main.py` is the only place the real mount points
        exist, so it is the only place worth reading — and reading it rather than
        restating it is what keeps this test from drifting.
        """
        import re

        source = (Path(__file__).resolve().parent.parent / "main.py").read_text("utf-8")
        out = {}
        for match in re.finditer(
                r"include_router\(\s*([A-Za-z_][\w.]*)\.router\s*(?:,\s*prefix=\"([^\"]*)\")?",
                source):
        # `org_admin_router` in main.py is the module `routers.org_admin`; the aliases
        # are stripped here and matched by the imported module name.
            out[match.group(1).rsplit("_router", 1)[0]] = match.group(2) or ""
        return out

    def _router_paths(self):
        """`(path, method, file)` for every route the app really serves."""
        import importlib
        import pkgutil

        import routers
        prefixes = self._install_map()
        found, unmounted = [], []
        for info in pkgutil.iter_modules(routers.__path__):
            if info.name in self.SKIP_FILES or info.name.startswith("_"):
                continue
            module = importlib.import_module(f"routers.{info.name}")
            router = getattr(module, "router", None)
            if router is None:
                continue
            # `main.py` mounts some routers under an alias (`org_admin_router`), and some
            # modules carry a second router for the standalone document pages. Anything
            # whose mount prefix cannot be found is reported, not skipped silently.
            prefix = prefixes.get(info.name)
            if prefix is None:
                unmounted.append(info.name)
                continue
            for route in router.routes:
                path = getattr(route, "path", None)
                if not path:
                    continue
                found.append((prefix + path, sorted(getattr(route, "methods", []) or []),
                              info.name))
        self.assertEqual(unmounted, [],
                         f"routers/{', '.join(unmounted)} are not mounted by main.py, so "
                         f"this test cannot see their paths — either main.py changed or "
                         f"the module name no longer matches the alias")
        return found

    def test_every_declared_route_belongs_to_a_module(self):
        unclaimed = []
        for path, methods, filename in self._router_paths():
            if M.module_for_path(path) is None:
                unclaimed.append(f"{filename}: {path} {methods}")
        self.assertEqual(
            unclaimed, [],
            "⚠️ the middleware answers 403 for a path no module claims, so each of these "
            "routes is DEAD in the running app while its unit tests pass. Add the prefix "
            "to the module that owns it in core/modules.py.")

    def test_the_suite_is_not_vacuous(self):
        """⚠️ A route-enumerating test that enumerates nothing passes forever and proves
        nothing — the exact failure mode of the hand-written list it replaced. If the
        import of `routers` ever stops working, or the prefix regex in `main.py` stops
        matching, this is what notices."""
        paths = self._router_paths()
        self.assertGreater(len(paths), 150, f"only {len(paths)} routes were enumerated")
        files = {f for _p, _m, f in paths}
        # ⚠️ Every router module must have contributed. A module that fails to import
        # would otherwise shrink the set quietly and turn the test above into a pass.
        for expected in ("org_admin", "auth", "settings", "reports", "data_center",
                         "calendar", "inbox", "knowledge", "dashboard", "export",
                         "storage", "annotations"):
            self.assertIn(expected, files, f"routers/{expected}.py contributed no routes")
        # And the enumeration must carry the FULL mount prefix, not the router's own
        # relative path — the defect that made this enumerate twelve paths the first time.
        found = {p for p, _m, _f in paths}
        for expected in ("/api/org/me", "/api/org/settings",
                         "/api/org/members/{member_id}/modules",
                         "/api/settings/modules/accounts/{user_id}/{module_key}",
                         "/api/auth/admin/users", "/api/dashboard/"):
            self.assertIn(expected, found,
                          f"{expected} is not in the enumerated route set — the mount "
                          f"prefixes parsed out of main.py do not match the routers")

    def test_org_administration_is_claimed_by_a_required_module(self):
        """⚠️ Not just "claimed" — claimed by one that can never be switched off. An
        `/api/org/*` owned by a toggleable module would mean a company whose members all
        lack that module cannot administer the company at all, and the failure would look
        like a permissions problem rather than a registry mistake."""
        owner = M.module_for_path("/api/org/me")
        self.assertIsNotNone(owner, "/api/org/me is not reachable by any module")
        self.assertTrue(owner.required,
                        f"/api/org/* is owned by the switchable module {owner.key!r}")
        self.assertFalse(owner.toggleable, owner.key)
        self.assertFalse(owner.in_nav,
                         "organization administration must not become a navigation tab")


class PathGateTests(unittest.TestCase):
    def test_paths_outside_the_api_and_document_space_are_not_gated(self):        # The shell, its assets and the public pages belong to no module; the gate
        # must stay out of the way or the app cannot load.
        for path in ("/", "/index.html", "/theme.css", "/logo.png", "/privacy",
                     "/s/some-share-token", "/favicon.ico"):
            self.assertIsNone(M.module_for_path(path), path)

    def test_a_prefix_match_is_by_path_segment(self):
        # `/api/reports-export` must not be captured by `/api/reports`.
        self.assertIsNone(M.module_for_path("/api/reports-export"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
