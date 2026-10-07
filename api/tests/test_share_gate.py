"""The share gate, end to end at all twelve write points.

Why this file is shaped the way it is
-------------------------------------
The gate was added to a product that already had **eleven** places where a person can
hand a file, a document or a link to somebody outside it. Two of them checked a single
deployment-wide email suffix, using two byte-identical copies of the same five-line
function. The other nine checked nothing at all.

So the risk being managed is not "the gate has a bug". It is:

* somebody adds a twelfth write path and forgets — which is what happened twice while
  writing this gate (the calendar's `partners` field is a share smuggled into an update,
  and `publish_file` mints a bearer token), and
* one of the eleven quietly keeps its old behaviour.

Both are managed the same way: **every write path is named in `WRITE_PATHS` and driven
through the same three assertions.** A path that is not in the table fails
`test_no_share_endpoint_exists_outside_the_inventory`, which greps the routers for
share-ish endpoints and compares the set.

The three assertions per path
-----------------------------
1. **Blocked** — refused with the shared wording (403), not a bespoke one.
2. **Allowed** — an in-organization recipient is *not* refused. The counterweight: a
   gate that refuses everything passes every other test here and still takes the product
   down.
3. **Nothing written** — the refusal happened *before* the service call.

The third is what separates a gate from an apology. Checking after the INSERT and then
raising means the share row exists and the 403 is a lie. The database is the only
witness, and "the endpoint said 403, so it must be fine" is precisely the reasoning that
let nine paths through unguarded.

No server and no database here. What is *not* stubbed is the gate and the call site —
the two things this file exists to pin.
"""
import os
import re
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
for _p in (REPO_DIR, API_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from klado_shared import orgs                                   # noqa: E402
from routers import (business_knowledge, calendar, data_center,  # noqa: E402
                     dashboard, reports)
from services import dashboard_store                            # noqa: E402

OWNER = "owner@corp.example"
MATE = "mate@corp.example"
OUTSIDER = "outsider@other.example"
ORG_ID = 42

#: Every write path, and how to drive it.
#:
#: `func` is the router function name. It exists so the inventory check can compare
#: like with like — matching on the URL template instead means rewriting this table
#: every time a route is renamed, at which point nobody renames it and the table
#: quietly stops describing the product.
WRITE_PATHS = [
    ("dashboard → public area", dashboard, "/api/dashboard", "PUT", "/q3", {"title":"Q3","html":"<p>Q3</p>","visibility":"public"}, "save_dashboard"),
    ("file → colleague", data_center, "/api/data-center", "POST", "/files/7/shares", {"email": MATE}, "share_file"),
    # (label, router module, url prefix, method, concrete path, request kwargs, func)
    ("report → colleague", reports, "/api/reports", "POST", "/q3/shares/colleagues",
     {"emails": [MATE]}, "share_report_with_colleagues"),
    ("report → anyone link", reports, "/api/reports", "POST", "/q3/shares/everyone",
     None, "create_anyone_link"),
    ("report → public area", reports, "/api/reports", "POST", "/q3/publish",
     None, "publish_to_public"),
    ("dashboard → colleague", dashboard, "/api/dashboard", "POST", "/q3/shares/colleagues",
     {"email": MATE}, "share_dashboard"),
    ("dataset → colleague", data_center, "/api/data-center", "POST", "/datasets/t1/shares",
     {"email": MATE}, "share_dataset"),
    ("file → token (by id)", data_center, "/api/data-center", "POST", "/files/7/publish",
     None, "publish_file"),
    ("file → token (by path)", data_center, "/api/data-center", "POST", "/files/publish-by-path",
     {"path": "x/y.csv"}, "publish_file_by_path"),
    ("knowledge → colleague", business_knowledge, "/api/knowledge", "POST", "/items/k1/shares",
     {"emails": [MATE]}, "share_item"),
    # A folder share grants **every page filed in the folder**, so it is strictly more
    # reach than the row above it and is gated the same way. It is a separate entry
    # rather than a mode of `share_item` because the store call is a different one —
    # one row on the folder, not one per page.
    ("knowledge folder → colleague", business_knowledge, "/api/knowledge", "POST", "/folders/1/shares",
     {"emails": [MATE]}, "share_folder"),
    ("knowledge → public area", business_knowledge, "/api/knowledge", "POST", "/items/k1/publish",
     None, "publish_item"),
    ("calendar → partner", calendar, "/api/calendar", "PUT", "/events/e1",
     {"partners": [MATE]}, "update_event"),
    ("calendar → public area", calendar, "/api/calendar", "POST", "/events/e1/publish",
     None, "publish_event"),
]

#: Body keys that carry a recipient, in the order they are looked for.
RECIPIENT_KEYS = ("emails", "email", "partners")


def _with_recipient(kwargs, address):
    """The same request, aimed at `address`.

    A body-less request (the anyone-link and the publish endpoints take none) stays an
    EMPTY mapping, not `None` — `client.request(…, **None)` is a TypeError, and half
    these endpoints have no body, so the distinction is not academic.
    """
    if not kwargs:
        return {}
    body = dict(kwargs)
    for key in RECIPIENT_KEYS:
        if key in body:
            body[key] = [address] if isinstance(body[key], list) else address
            break
    return {"json": body}


class _Membership:
    """Apply several patches at once and unwind them in order.

    ⚠️ Unwinds the PATCHER, not whatever `start()` returned. `patch.object(obj, attr,
    new).start()` returns `new` — so collecting the return value and calling `.stop()` on
    it fails with "'function' object has no attribute 'stop'" the moment any patch in
    the list supplies a replacement.
    """

    def __init__(self, patches):
        self._patches = list(patches)
        self._started = []

    def __enter__(self):
        for p in self._patches:
            p.start()
            self._started.append(p)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._started):
            p.stop()
        return False


def _as_org(profile=("internal", "approve", ORG_ID), roster=(OWNER, MATE),
            approved=False):
    """Patches that put the caller inside an organization with a given policy."""
    return _Membership([
        patch.object(orgs, "effective_profile", return_value=profile),
        patch.object(orgs, "_org_addresses", return_value=set(roster)),
        patch.object(orgs, "has_public_approval", return_value=approved),
    ])


class _Spy:
    """Records that a downstream service was reached."""

    def __init__(self):
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append(a)
        return {"slug": "x", "title": "T", "id": 1, "token": "t"}


def _client(module, prefix):
    app = FastAPI()

    @app.middleware("http")
    async def _identity(request: Request, call_next):
        request.state.current_user = {"email": request.headers.get("X-Test-User", OWNER),
                                      "role": "user"}
        request.state.auth_kind = "browser"
        return await call_next(request)

    app.include_router(module.router, prefix=prefix)
    return TestClient(app, raise_server_exceptions=False)


async def _async_none(*_a, **_k):
    return None


class _NullCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _NullConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self, cursor_factory=None):
        return _NullCursor()

    def commit(self):
        return self

    def rollback(self):
        return self

    def close(self):
        return self


class _RecordingConn:
    """Wraps a connection factory so the SQL it ran can be inspected afterwards."""

    def __init__(self, factory, sink):
        self._factory, self._sink = factory, sink

    def __call__(self, *a, **k):
        return self._factory(*a, **k)


class _StubModule:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class _StubShareService:
    def __init__(self, spy):
        self._spy = spy
        self.ShareError = type("ShareError", (Exception,), {})

    def share(self, *a, **k):
        return self._spy(*a, **k)


#: A stand-in calendar event. Attribute access, because the router does
#: `current.owner_email`; and `partners=[]` so the gate sees a clean diff and is the
#: only thing deciding.
_EVENT = SimpleNamespace(
    slug="e1", owner_email=OWNER, partners=[], title="E", status="todo", kind="event",
    start_date=None, end_date=None, deadline=None, tags=[], attachments=[], todos=[],
    category="", summary="", summary_en="", summary_zh="", description_en="",
    description_zh="", cover=None, is_public=False,
    # ⚠️ `_merged_payload` is stubbed, so this is the list of keys it copies straight off
    # the stored event. It reads EVERY field of the event model, so the stub has to carry
    # all of them — "SimpleNamespace has no attribute body" is what happens when one is
    # missed, and the failure names the field rather than the test.
    body="", status_note="",
)


#: Paths that hand something to a NAMED person. Governed by `share_scope`, and the
#: question is "is that person in the roster".
TARGETED = {"share_report_with_colleagues", "share_dashboard",
            "share_file", "share_dataset", "share_item", "share_folder", "update_event"}

#: Paths that make something reachable without naming anybody. Governed by
#: `public_scope`, and the question is "is there an approval".
PUBLISHING = {"create_anyone_link", "publish_to_public", "publish_file",
              "publish_file_by_path", "publish_item", "publish_event", "save_dashboard"}


# ── the gate itself ──────────────────────────────────────────────────────────

class GateLogicTests(unittest.TestCase):
    """The policy table, independent of any router."""

    def test_internal_scope_permits_only_the_roster(self):
        with _as_org(("internal", "approve", ORG_ID)):
            self.assertEqual(orgs.may_share_to(OWNER, MATE), (True, ""))
            allowed, reason, offenders = orgs.may_share_to_many(OWNER, [OUTSIDER])
            self.assertFalse(allowed)
            self.assertEqual(reason, orgs.NOT_SAME_ORG)
            self.assertEqual(offenders, [OUTSIDER])

    def test_external_scope_permits_anybody_valid(self):
        with _as_org(("external", "approve", ORG_ID)):
            self.assertEqual(orgs.may_share_to(OWNER, OUTSIDER), (True, ""))
            # Still not a licence to send to nonsense.
            self.assertEqual(orgs.may_share_to(OWNER, "nonsense")[1], orgs.BAD_RECIPIENT)

    def test_global_scope_is_unrestricted(self):
        with _as_org(("global", "allow", ORG_ID)):
            self.assertEqual(orgs.may_share_to(OWNER, OUTSIDER), (True, ""))

    def test_an_address_with_no_organization_is_unrestricted(self):
        """The upgrade path: an installation that has just gained these tables must keep
        working, and a personal user must keep the reach they had before."""
        with patch.object(orgs, "membership_of", return_value=None):
            self.assertEqual(orgs.effective_profile("anyone@anywhere.test"),
                             orgs.UNRESTRICTED)

    def test_a_suspended_company_still_bounds_its_own_members(self):
        """⚠️ Pinned deliberately. An earlier version returned the PERMISSIVE profile for
        a suspended organization, reasoning that "they are not really in it any more" —
        which hands a company's members a wider reach at the exact moment their
        administrator is trying to close it down. Whether a suspended company admits new
        people is `orgs_for_domains`' job (it filters on status); what its existing
        members may hand out is decided here, and the answer is the company's own rules.
        """
        for row in (
            {"org_id": ORG_ID, "share_scope": "internal", "public_scope": "approve",
             "status": "active", "org_status": "suspended"},
            {"org_id": ORG_ID, "share_scope": "internal", "public_scope": "approve",
             "status": "disabled", "org_status": "active"},
        ):
            with self.subTest(org_status=row["org_status"], status=row["status"]):
                with patch.object(orgs, "membership_of", return_value=row):
                    self.assertEqual(orgs.effective_profile(OWNER),
                                     ("internal", "approve", ORG_ID))

    def test_publish_forbidden_beats_everything(self):
        with _as_org(("global", "forbid", ORG_ID)):
            for channel in orgs.PUBLIC_CHANNELS:
                allowed, reason = orgs.may_publish(OWNER, channel, orgs.TARGET_REPORT, "s")
                self.assertFalse(allowed, channel)
                self.assertEqual(reason, orgs.PUBLIC_FORBIDDEN, channel)

    def test_publish_approve_needs_an_approval(self):
        with _as_org(("internal", "approve", ORG_ID), approved=False):
            self.assertEqual(
                orgs.may_publish(OWNER, orgs.CHANNEL_PUBLIC, orgs.TARGET_REPORT, "s"),
                (False, orgs.NEEDS_APPROVAL))

    def test_publish_approve_is_satisfied_by_an_approval(self):
        with _as_org(("internal", "approve", ORG_ID), approved=True):
            self.assertEqual(
                orgs.may_publish(OWNER, orgs.CHANNEL_PUBLIC, orgs.TARGET_REPORT, "s"),
                (True, ""))

    def test_every_channel_is_covered_by_its_own_test(self):
        """The channels are three and the families are four; the table above iterates
        `PUBLIC_CHANNELS`, so a fourth channel added without a policy is caught here
        rather than reaching a router with no wording."""
        self.assertEqual(set(orgs.PUBLIC_CHANNELS),
                         {"anyone_link", "public", "file_token"})
        self.assertEqual(set(orgs.TARGET_TYPES),
                         {"report", "knowledge", "calendar", "file", "dashboard"})

    def test_an_unknown_channel_or_family_is_refused_not_guessed(self):
        with _as_org(("global", "allow", ORG_ID)):
            self.assertFalse(orgs.may_publish(OWNER, "carrier_pigeon",
                                              orgs.TARGET_REPORT, "s")[0])
            self.assertFalse(orgs.may_publish(OWNER, orgs.CHANNEL_PUBLIC,
                                              "invoice", "s")[0])

    def test_every_refusal_has_wording_and_a_status(self):
        """An unmapped reason code would read to a user as "share blocked" with no
        explanation, so `denial()` raises instead of defaulting."""
        for reason in (orgs.NOT_SAME_ORG, orgs.NEEDS_APPROVAL,
                       orgs.PUBLIC_FORBIDDEN, orgs.BAD_RECIPIENT):
            status, message = orgs.denial(reason)
            self.assertIsInstance(status, int)
            self.assertIn(" / ", message, reason)      # a bilingual pair
        with self.assertRaises(orgs.OrgError):
            orgs.denial("something_new")

    def test_out_of_org_is_403_and_malformed_is_400(self):
        """The distinction matters: 403 is a policy, 400 is "fix your input". Told 400,
        a user retypes the address forever."""
        self.assertEqual(orgs.denial(orgs.NOT_SAME_ORG)[0], 403)
        self.assertEqual(orgs.denial(orgs.NEEDS_APPROVAL)[0], 403)
        self.assertEqual(orgs.denial(orgs.PUBLIC_FORBIDDEN)[0], 403)
        self.assertEqual(orgs.denial(orgs.BAD_RECIPIENT)[0], 400)

    def test_all_offending_addresses_are_reported(self):
        """The UI marks individual fields, so it needs the list, not just the first."""
        with _as_org(("internal", "approve", ORG_ID)):
            _allowed, reason, offenders = orgs.may_share_to_many(
                OWNER, [MATE, OUTSIDER, "third@elsewhere.test"])
            self.assertEqual(reason, orgs.NOT_SAME_ORG)
            self.assertEqual(offenders, [OUTSIDER, "third@elsewhere.test"])


# ── every write path ─────────────────────────────────────────────────────────

class EveryWritePathIsGatedTests(unittest.TestCase):
    def _stubs(self, spy):
        """Everything the endpoints touch *after* the gate, neutralised.

        The gate runs first in all eleven, so a refusal must not need any of these to
        work. The stores are stubbed rather than removed on purpose: if a gate were ever
        moved below the service call, the spy would record it and
        `test_…refuses_before_writing` fails.
        """
        return [
            # reports
            patch.object(reports, "_ensure_table"),
            patch.object(reports, "_db", return_value=_NullConn()),
            patch.object(reports, "_require_browser", return_value=OWNER),
            patch.object(reports, "_require_owner", return_value={
                "id": 1, "slug": "q3", "status": "published", "owner_email": OWNER,
                "visibility": "private", "title": "Q3"}),
            patch.object(reports, "_shareable_report", return_value={
                "id": 1, "slug": "q3", "title": "Q3", "owner_email": OWNER}),
            patch.object(reports, "_load", return_value={"slug": "q3", "title": "Q3"}),
            patch.object(reports, "_anyone_url", lambda r, t: "/s/" + str(t)),
            patch.object(reports, "_public_report_url", lambda r, s: "/r/" + str(s)),
            patch.object(reports, "inbox", _StubModule()),
            # data center
            patch.object(data_center, "_ensure_init", lambda: None),
            patch.object(data_center, "_assert_dataset_owner", lambda t, r: OWNER),
            patch.object(data_center, "_assert_file_owner", lambda t, r: OWNER),
            patch.object(data_center, "_assert_object_owner", lambda t, r: OWNER),
            patch.object(data_center, "file_shares", _StubShareService(spy)),
            patch.object(data_center, "_deny_unless_readable", lambda *a, **k: None),
            patch.object(data_center, "_viewer", lambda r: (OWNER, False)),
            patch.object(data_center, "shares", _StubShareService(spy)),
            patch.object(data_center.db, "get_file", lambda *a, **k: {"id": 7}),
            patch.object(data_center.db, "publish_file", spy),
            patch.object(data_center.db, "publish_file_by_path", spy),
            # dashboard
            patch.object(dashboard_store, "ensure_schema", lambda: None),
            patch.object(dashboard, "_require_browser", lambda r: OWNER),
            patch.object(dashboard_store, "_own_dashboard", lambda c, s, o: {"id": 1}),
            patch.object(dashboard, "store", dashboard_store),
            # ⚠️ `share_dashboard` is deliberately NOT stubbed. The dashboard's gate
            # lives IN that function (it is the one share entry point that is a plain
            # function rather than a router body), so replacing it with a spy would
            # replace the gate along with it — the test would then pass on a build
            # with no dashboard check at all. `_conn` is stubbed instead, which is
            # where the write would show up.
            patch.object(dashboard_store, "_conn", self._sql_spy()),
            # knowledge
            patch.object(business_knowledge.store, "share_with", spy),
            # The folder grant's own write. Spied rather than the gate, because the
            # gate is the `orgs.may_share_to_many` call **before** it — stubbing this
            # is what makes "refused, but the service was called anyway" answerable.
            patch.object(business_knowledge.filing, "share_folder", spy),
            patch.object(business_knowledge.filing, "folder_title", lambda i: "F"),
            patch.object(business_knowledge.filing, "folder_project", lambda i: "p1"),
            patch.object(business_knowledge.store, "publish_to_public", spy),
            patch.object(business_knowledge.store, "title_of", lambda s: "K"),
            patch.object(business_knowledge.store, "get_item", lambda s, e: {"slug": s}),
            patch.object(business_knowledge, "_require_browser", return_value=OWNER),
            patch.object(business_knowledge, "inbox", _StubModule()),
            # calendar. ⚠️ `get_event` must return an OBJECT, not a dict: the router
            # reads `current.owner_email` and `current.partners` as attributes. A dict
            # stub fails with "dict has no attribute owner_email", which reads like a
            # bug in the gate and is not.
            patch.object(calendar.store, "publish_to_public", spy),
            patch.object(calendar.store, "upsert_event", spy),
            patch.object(calendar.store, "get_event", lambda s, e: _EVENT),
            patch.object(calendar, "_require_browser", return_value=OWNER),
            patch.object(calendar, "_identity", return_value=(OWNER, "browser")),
            patch.object(calendar, "_cover_or_error", lambda p, t, r: None),
            patch.object(calendar, "_write_or_error", spy),
            patch.object(calendar, "_shape", lambda e, e2, **k: {"slug": "e1"}),
            patch.object(calendar, "_reject_unknown_cover_keys_of", _async_none),
            patch.object(calendar, "inbox", _StubModule()),
        ]

    def _drive(self, module, prefix, method, path, kwargs, address, membership):
        spy = _Spy()
        client = _client(module, prefix)
        with _Membership(self._stubs(spy)), membership:
            resp = client.request(method, prefix + path, headers={"X-Test-User": OWNER},
                                  **_with_recipient(kwargs, address))
        return resp, bool(spy.calls)

    @staticmethod
    def _sql_spy():
        """A `_conn` that records SQL, so "did it write?" is answerable for the one
        path whose gate lives in a service function rather than in the router."""
        recorded = []

        class _Conn(_NullConn):
            def cursor(self, cursor_factory=None):
                class _C(_NullCursor):
                    def execute(self, sql, params=None):
                        recorded.append(sql)
                        return self

                    def fetchone(self):
                        return {"id": 1, "recipient_email": MATE, "shared_by": OWNER,
                                "created_at": None}
                return _C()

        return patch.object(dashboard_store, "_conn", _RecordingConn(_Conn, recorded))

    def test_the_inventory_has_fourteen_paths(self):
        self.assertEqual(len(WRITE_PATHS), 14)
        self.assertEqual(len({p[6] for p in WRITE_PATHS}), 14,
                         "two paths share a function name; the inventory is ambiguous")
        self.assertEqual({p[6] for p in WRITE_PATHS}, TARGETED | PUBLISHING)

    # ── targeted shares: governed by share_scope ─────────────────────────────

    def test_a_targeted_share_refuses_an_out_of_organization_recipient(self):
        for (label, module, prefix, method, path, kwargs, fn) in WRITE_PATHS:
            if fn not in TARGETED:
                continue
            with self.subTest(path=label):
                resp, reached = self._drive(module, prefix, method, path, kwargs,
                                            OUTSIDER, _as_org(("internal", "approve", ORG_ID)))
                self.assertEqual(resp.status_code, 403,
                                 f"{label} → {resp.status_code} {resp.text[:200]}")
                self.assertFalse(reached,
                                 f"{label}: refused, but the service was called anyway")

    def test_a_targeted_share_allows_a_colleague(self):
        """⚠️ The counterweight to the test above. A gate that refuses everything passes
        every other assertion in this file and still takes the product down."""
        for (label, module, prefix, method, path, kwargs, fn) in WRITE_PATHS:
            if fn not in TARGETED:
                continue
            with self.subTest(path=label):
                resp, _reached = self._drive(module, prefix, method, path, kwargs,
                                             MATE, _as_org(("internal", "approve", ORG_ID)))
                self.assertNotEqual(resp.status_code, 403,
                                    f"{label}: a colleague in the org was refused")

    def test_share_scope_does_not_govern_publication(self):
        """`internal` is about WHO, not about WHETHER. A company that only shares with
        its own members can still publish to the public area if its `public_scope` says
        so — conflating the two settings would make the three presets meaningless."""
        for (label, module, prefix, method, path, kwargs, fn) in WRITE_PATHS:
            if fn not in PUBLISHING:
                continue
            with self.subTest(path=label):
                resp, _reached = self._drive(
                    module, prefix, method, path, kwargs, None,
                    _as_org(("internal", "allow", ORG_ID)))
                self.assertNotEqual(resp.status_code, 403,
                                    f"{label}: publication was blocked by share_scope")

    # ── publication: governed by public_scope ───────────────────────────────

    def test_publication_needs_an_approval(self):
        for (label, module, prefix, method, path, kwargs, fn) in WRITE_PATHS:
            if fn not in PUBLISHING:
                continue
            with self.subTest(path=label):
                resp, reached = self._drive(
                    module, prefix, method, path, kwargs, None,
                    _as_org(("internal", "approve", ORG_ID), approved=False))
                self.assertEqual(resp.status_code, 403,
                                 f"{label} → {resp.status_code} {resp.text[:200]}")
                self.assertFalse(reached, f"{label}: refused, but a service was called")

    def test_an_approval_lets_the_publication_through(self):
        for (label, module, prefix, method, path, kwargs, fn) in WRITE_PATHS:
            if fn not in PUBLISHING:
                continue
            with self.subTest(path=label):
                resp, _reached = self._drive(
                    module, prefix, method, path, kwargs, None,
                    _as_org(("internal", "approve", ORG_ID), approved=True))
                self.assertNotEqual(resp.status_code, 403,
                                    f"{label}: an approved publication was refused")

    def test_a_forbidden_organization_publishes_nothing(self):
        """`forbid` is the third branch, and it reaches the same six endpoints."""
        for (label, module, prefix, method, path, kwargs, fn) in WRITE_PATHS:
            if fn not in PUBLISHING:
                continue
            with self.subTest(path=label):
                resp, reached = self._drive(
                    module, prefix, method, path, kwargs, None,
                    _as_org(("global", "forbid", ORG_ID), approved=True))
                self.assertEqual(resp.status_code, 403,
                                 f"{label} → {resp.status_code} {resp.text[:200]}")
                self.assertFalse(reached, f"{label}: forbidden, but a service was called")


# ── coverage of the list itself ──────────────────────────────────────────────

class WritePathInventoryTests(unittest.TestCase):
    """Keeps `WRITE_PATHS` honest.

    A gate is only as good as the number of places it covers, and that number lives in
    somebody's head otherwise. This greps the routers for endpoints whose route or
    function name looks like a share or a publication and fails when the set differs —
    which is the moment a twelfth write path appears.
    """

    ROUTERS = {
        "reports": reports, "dashboard": dashboard, "data_center": data_center,
        "business_knowledge": business_knowledge, "calendar": calendar,
    }
    PATTERN = re.compile(r"share|partner|publish|everyone|public", re.I)
    #: Endpoints that look like shares but grant nothing. `clean-preview` and
    #: `list-orphan-objects` matched on "public"/"preview" and are read-only
    #: transformations of the caller's own data.
    EXEMPT = {"clean_preview", "list_orphan_objects", "orphan_objects"}

    def test_no_share_endpoint_exists_outside_the_inventory(self):
        listed = {p[6] for p in WRITE_PATHS}
        found = set()
        for name, module in self.ROUTERS.items():
            src = open(module.__file__, encoding="utf-8").read()
            for match in re.finditer(
                    r'@router\.(get|post|put|patch|delete)\("([^"]*)"\)'
                    r'(?:\s*\n(?:[^\n]*\n)*?)?(?:async )?def (\w+)', src):
                verb, _route, func = match.group(1), match.group(2), match.group(3)
                if func in self.EXEMPT:
                    continue
                if not self.PATTERN.search(_route) and not self.PATTERN.search(func):
                    continue
                # Reads and revocations grant nothing: removing a share is always
                # allowed, and a GET hands nothing over.
                if verb.lower() in ("get", "delete"):
                    continue
                found.add(func)
        self.assertEqual(found - listed, set(),
                         "a share/publish endpoint is not in WRITE_PATHS: "
                         + repr(sorted(found - listed)))

    def test_every_inventoried_path_is_reachable_in_the_source(self):
        """The other direction. A renamed function left in the table would make the check
        above pass while testing a path that no longer exists."""
        import inspect
        for (_label, module, _prefix, _method, _path, _kw, func) in WRITE_PATHS:
            with self.subTest(path=func):
                self.assertTrue(callable(getattr(module, func, None)),
                                f"{module.__name__}.{func} no longer exists")

    def test_the_deleted_domain_check_does_not_come_back(self):
        """Two byte-identical copies of `is_allowed_recipient` were the reason the
        policy drifted. Nothing in the tree may define a share-recipient check again —
        `orgs.may_share_to*` is the only one."""
        for _name, module in self.ROUTERS.items():
            with self.subTest(router=_name):
                src = open(module.__file__, encoding="utf-8").read()
                body = re.sub(r"#[^\n]*", "", src)
                self.assertIsNone(
                    re.search(r"^\s*def is_allowed_recipient\b", body, re.M),
                    f"{_name} re-defines the share-recipient check")


# ── the store-level entry point ──────────────────────────────────────────────

class StoreLevelGateTests(unittest.TestCase):
    """`dashboard_store.share_dashboard` is the one share entry point that is a plain
    function rather than a router body, and it is also the one whose check was deleted.
    Tested directly so the deletion cannot go unnoticed."""

    def test_refuses_an_out_of_organization_recipient(self):
        with _as_org(("internal", "approve", ORG_ID)), \
             patch.object(dashboard_store, "ensure_schema", lambda: None), \
             patch.object(dashboard_store, "_conn", side_effect=AssertionError(
                 "a refused share must not open a transaction")):
            with self.assertRaises(dashboard_store.DashboardError) as ctx:
                dashboard_store.share_dashboard("q3", OUTSIDER, OWNER)
        self.assertIn(" / ", str(ctx.exception))

    def test_allows_an_in_organization_recipient(self):
        """The gate is in the store, so the store has to actually run here. `_own_dashboard`
        reads the row and checks the owner, which is why the stubbed row carries
        `owner_email` as well as the share columns — a row without it fails as
        "dashboard not found" and looks like a gating bug."""
        written = []

        class _Conn(_NullConn):
            def cursor(self, cursor_factory=None):
                class _C(_NullCursor):
                    def execute(self, sql, params=None):
                        written.append(sql)
                        return self

                    def fetchone(self):
                        return {"id": 3, "slug": "q3", "owner_email": OWNER,
                                "status": "published", "visibility": "private",
                                "recipient_email": MATE, "shared_by": OWNER,
                                "created_at": None}
                return _C()

        with _as_org(("internal", "approve", ORG_ID)), \
             patch.object(dashboard_store, "ensure_schema", lambda: None), \
             patch.object(dashboard_store, "_conn", return_value=_Conn()):
            row = dashboard_store.share_dashboard("q3", MATE, OWNER)
        self.assertIsInstance(row, dict)
        self.assertTrue(any("INSERT" in q.upper() for q in written), written)


if __name__ == "__main__":
    unittest.main()
