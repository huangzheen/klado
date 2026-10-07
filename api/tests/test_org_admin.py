"""Enterprise administration: scope, and the refusals that matter.

The whole point of `/api/org/*` being a separate prefix is that it is gated by
something other than `requires_admin`. So the first half of this file is about SCOPE —
who can reach which organization, and what they are told when they cannot:

* an enterprise admin's organization comes from their **membership row**, never from a
  query parameter, because a parameter is something the caller controls;
* an organization they may not see is **404, not 403** — "that company exists but is not
  yours" is a directory of every company in the installation;
* a platform operator is not narrowed to anything, and says so when they forget to name
  an organization.

The second half is the set of destructive mistakes this endpoint makes easy to make and
expensive to recover from: demoting the last administrator, removing somebody from the
company in a way that closes their account, and letting an invitation put one person in
two companies.

No server, no database. The stores are faked; `_scoped_org` and the organization lookups
are the real code.
"""
import os
import sys
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from types import SimpleNamespace

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
for _p in (REPO_DIR, API_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from klado_shared import orgs                                  # noqa: E402
from klado_shared.config import settings                      # noqa: E402
from routers import org_admin                                  # noqa: E402

ORG_ADMIN = "boss@corp.example"
ORG_MATE = "mate@corp.example"
OPERATOR = "root@platform.example"
ORG_ID = 7
OTHER_ORG_ID = 8


def _client(identity, *, get_org=None, also_org_admin=None, **route_patches):
    """A TestClient whose caller is `identity`, with the lookups faked.

    `get_org` is a VALUE (an org row, or None), not a patcher — several tests need to
    say "this organization does not exist", and threading a `patch.object` through to
    express that makes the call sites unreadable.
    """
    app = FastAPI()

    @app.middleware("http")
    async def _who(request: Request, call_next):
        request.state.current_user = identity
        request.state.auth_kind = "browser"
        return await call_next(request)

    # ⚠️ `is_org_admin` follows the identity under test, it is not hard-coded True.
    # It used to be, which quietly made every OPERATOR also an enterprise administrator
    # for the duration of every test — and the router, which resolves a company's scope
    # from that predicate before it considers the operator flag, then refused an
    # operator naming a company they do not belong to. The three operator-scope tests
    # failed against a router whose operator branch was correct.
    #
    # A real operator has no company, so `membership_of` is None for them and the
    # predicate is False. An individual test that genuinely needs a dual-identity
    # caller passes `also_org_admin=True` to say so explicitly.
    if also_org_admin is None:
        also_org_admin = str(identity.get("role", "")).lower() != "admin"
    patches = [patch.object(orgs, "membership_of",
                            return_value=(_membership(identity.get("email") or "", ORG_ID)
                                          if also_org_admin else None)),
               patch.object(orgs, "is_org_admin", return_value=also_org_admin),
               patch.object(orgs, "get_org",
                            side_effect=(lambda _i: get_org) if get_org is not None
                            else _orgs_by_id)]
    patches += [p for p in route_patches.values() if p is not None]
    app.include_router(org_admin.router)
    for p in patches:
        p.start()
    client = TestClient(app, raise_server_exceptions=False)
    client._started_patches = patches       # noqa: SLF001 — unwound by the fixture below
    return client


def _stop(client):
    for p in reversed(getattr(client, "_started_patches", [])):
        p.stop()


def _membership(email, org_id, role="admin", status="active", org_status="active",
                module_keys=None):
    # ⚠️ `module_keys` is present and defaults to None, matching the column: absent from
    # the dict and set to None both mean "never licensed", and a fixture that simply
    # forgot the key would test the unconfigured branch while looking like it tested
    # something else.
    return {"org_id": org_id, "email": email, "org_role": role, "status": status,
            "org_status": org_status, "name": "Corp", "slug": "corp", "kind": "enterprise",
            "share_scope": "internal", "public_scope": "approve",
            "email_domains": ["corp.example"], "module_keys": module_keys}


def _orgs_by_id(org_id):
    if org_id == ORG_ID:
        return {"id": ORG_ID, "slug": "corp", "name": "Corp", "kind": "enterprise",
                "share_scope": "internal", "public_scope": "approve",
                "email_domains": ["corp.example"], "status": "active"}
    if org_id == OTHER_ORG_ID:
        return {"id": OTHER_ORG_ID, "slug": "other", "name": "Other", "kind": "enterprise",
                "share_scope": "global", "public_scope": "allow",
                "email_domains": ["other.example"], "status": "active"}
    return None


def _request(*patches):
    return patch.multiple


class _Scope:
    """A client with its patches managed as a context manager."""

    def __init__(self, identity, *, also_org_admin=None, **route_patches):
        self._client = _client(identity, also_org_admin=also_org_admin, **route_patches)

    def __enter__(self):
        return self._client

    def __exit__(self, *exc):
        _stop(self._client)
        return False


# ── who administers what ─────────────────────────────────────────────────────

class ScopeTests(unittest.TestCase):
    def test_ordinary_member_is_refused(self):
        member = {"email": ORG_MATE, "role": "user"}
        with _Scope(member) as c, \
             patch.object(orgs, "is_org_admin", return_value=False), \
             patch.object(orgs, "membership_of", return_value=_membership(ORG_MATE, ORG_ID,
                                                                        role="member")):
            resp = c.get("/api/org/members")
        self.assertEqual(resp.status_code, 403)

    def test_no_session_is_401(self):
        with _Scope({"email": ""}) as c:
            resp = c.get("/api/org/members")
        self.assertEqual(resp.status_code, 401)

    def test_an_admin_gets_their_own_organization_with_no_parameter(self):
        """⚠️ The load-bearing one. The scope is derived from the membership row; a
        parameter never widens it."""
        seen = []

        def _members(org_id):
            seen.append(org_id)
            return []

        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "members_of", side_effect=_members):
            resp = c.get("/api/org/members")
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(resp.json()["org_id"], ORG_ID)
        self.assertEqual(seen, [ORG_ID])

    def test_an_admin_cannot_widen_the_scope_with_a_parameter(self):
        """Passing another company's id must be REFUSED, not honoured. 404 rather than
        403, because confirming the company exists is itself the leak."""
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c:
            resp = c.get("/api/org/members", params={"org_id": OTHER_ORG_ID})
        self.assertEqual(resp.status_code, 404,
                         "an enterprise admin reached another company")

    def test_an_operator_may_name_any_organization(self):
        with _Scope({"email": OPERATOR, "role": "admin"}) as c, \
             patch.object(orgs, "members_of", return_value=[]):
            resp = c.get("/api/org/members", params={"org_id": OTHER_ORG_ID})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(resp.json()["org_id"], OTHER_ORG_ID)

    def test_an_operator_who_forgets_to_name_one_is_told_plainly(self):
        with _Scope({"email": OPERATOR, "role": "admin"}) as c:
            resp = c.get("/api/org/members")
        self.assertEqual(resp.status_code, 400)
        self.assertIn(" / ", resp.json()["detail"])

    def test_an_invisible_organization_is_404_not_403(self):
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "get_org", return_value=None):
            resp = c.get("/api/org/members", params={"org_id": OTHER_ORG_ID})
        self.assertEqual(resp.status_code, 404)

    def test_an_operator_naming_a_company_that_is_not_there_gets_404(self):
        """⚠️ Distinct from the case above, and the reason this test exists.

        For an enterprise admin a foreign `org_id` is refused by `_resolve` — the scope
        check — so that test passes without ever reaching `get_org`. Only an operator
        gets all the way to the lookup, and only that path can turn a missing company
        into a 403 and start answering "does company N exist?" one id at a time.
        """
        with _Scope({"email": OPERATOR, "role": "admin"}, get_org=None) as c, \
             patch.object(orgs, "get_org", return_value=None):
            resp = c.get("/api/org/members", params={"org_id": 4242})
        self.assertEqual(resp.status_code, 404,
                         f"a missing company answered {resp.status_code}")


class MeTests(unittest.TestCase):
    def test_an_operator_belongs_to_no_company(self):
        """Operators are the platform level. Handing one a personal organization would
        make "is this an operator" answerable two ways."""
        with _Scope({"email": OPERATOR, "role": "admin"}) as c, \
             patch.object(orgs, "membership_of", return_value=None):
            body = c.get("/api/org/me").json()
        self.assertTrue(body["is_operator"])
        self.assertIsNone(body["org_id"])

    def test_a_member_reports_no_administrative_power(self):
        row = _membership(ORG_MATE, ORG_ID, role="member")
        with _Scope({"email": ORG_MATE, "role": "user"}) as c, \
             patch.object(orgs, "membership_of", return_value=row), \
             patch.object(orgs, "is_org_admin", return_value=False):
            body = c.get("/api/org/me").json()
        self.assertFalse(body["is_operator"])
        self.assertFalse(body["is_org_admin"])
        self.assertFalse(body["can_invite"])
        self.assertEqual(body["org_id"], ORG_ID)

    def test_a_suspended_company_stops_being_administrable(self):
        """The company being suspended must actually take the power away — otherwise
        `status` on `orgs` is decorative and the only way to close a company is to
        demote everyone by hand."""
        row = _membership(ORG_ADMIN, ORG_ID, role="admin", org_status="suspended")
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "membership_of", return_value=row), \
             patch.object(orgs, "is_org_admin", return_value=False):
            resp = c.get("/api/org/members")
        self.assertEqual(resp.status_code, 403)


# ── invitations ──────────────────────────────────────────────────────────────

class InviteTests(unittest.TestCase):
    def _invite(self, org=None):
        return _Scope({"email": ORG_ADMIN, "role": "user"}, get_org=org)

    def test_an_address_outside_the_company_suffix_IS_invited(self):
        """⚠️ The rule this file used to enforce, inverted on purpose.

        The suffix list answers "which company claims this person when they sign
        themselves up" — a question about somebody already typing their own email into
        the signup form. An invitation is the opposite: an administrator has decided, by
        name, that this person belongs here. Refusing off-suffix addresses made the
        button useless for a contractor, a partner, or an executive on a personal mailbox,
        which is exactly when an invitation is the right tool.

        What still bounds it, and this test says nothing about any of it on purpose: the
        code grants nothing until it is redeemed, `attach_existing_account` refuses an
        address that already belongs to another company (the next test), and the member
        lands under the company's own share and publication policy."""
        with self._invite(None) as c, \
             patch.object(orgs, "upsert_invitation",
                          return_value={"id": 3, "status": "invited"}) as upsert, \
             patch.object(orgs, "attach_existing_account", return_value=55), \
             patch("routers.org_admin.auth_store.get_user_by_email", return_value=None):
            resp = c.post("/api/org/invite", json={"email": "outsider@elsewhere.test"})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assertEqual(resp.json()["email"], "outsider@elsewhere.test")
        upsert.assert_called_once()

    def test_a_company_with_no_suffix_configured_can_still_invite(self):
        """The 409 that used to live here is gone, and its absence is the same decision
        seen from the other side: with no suffix configured, self-service signup matches
        nobody — but an administrator naming an address by hand is not a signup."""
        org = dict(_orgs_by_id(ORG_ID), email_domains=[])
        with self._invite(org) as c, \
             patch.object(orgs, "upsert_invitation",
                          return_value={"id": 3, "status": "invited"}), \
             patch.object(orgs, "attach_existing_account", return_value=55), \
             patch("routers.org_admin.auth_store.get_user_by_email", return_value=None):
            resp = c.post("/api/org/invite", json={"email": ORG_MATE})
        self.assertEqual(resp.status_code, 200, resp.text[:200])

    def test_a_malformed_address_is_still_refused(self):
        """⚠️ Dropping the suffix gate did NOT drop the only real validation on this
        endpoint. `clean_email` returning empty is what stops a typo becoming a member
        row, and without the suffix check it is the sole thing standing between this
        handler and a garbage address."""
        with self._invite(None) as c, \
             patch.object(orgs, "upsert_invitation") as upsert:
            resp = c.post("/api/org/invite", json={"email": "not-an-address"})
        self.assertEqual(resp.status_code, 400, resp.text[:200])
        upsert.assert_not_called()

    def test_a_malformed_address_is_400_not_403(self):
        with self._invite(None) as c:
            resp = c.post("/api/org/invite", json={"email": "nonsense"})
        self.assertEqual(resp.status_code, 400)

    def test_an_invitation_to_a_second_company_is_refused_at_the_service(self):
        """⚠️ ⚠️ **This is now the ONLY company-boundary check on the invite path**, and
        that is worth stating rather than leaving implied. A personal mailbox that two
        companies both invited has a valid suffix for neither, and since any address is
        now invitable, `attach_existing_account` is the single place that can see it.
        It is called on this path, and one person must not end up in two tenants."""
        with self._invite(None) as c, \
             patch.object(orgs, "upsert_invitation",
                          return_value={"id": 3, "status": "invited"}), \
             patch.object(orgs, "attach_existing_account",
                          side_effect=orgs.OrgError(
                              "该邮箱已属于另一个企业 / that address already belongs to "
                              "another organization")):
            resp = c.post("/api/org/invite", json={"email": ORG_MATE})
        self.assertEqual(resp.status_code, 400, resp.text[:200])
        self.assertIn(" / ", resp.json()["detail"])

    def test_a_happy_path_invites_and_reports_whether_an_account_existed(self):
        with self._invite(None) as c, \
             patch.object(orgs, "upsert_invitation",
                          return_value={"id": 3, "status": "invited"}), \
             patch.object(orgs, "attach_existing_account", return_value=55), \
             patch("routers.org_admin.auth_store.get_user_by_email", return_value=None):
            resp = c.post("/api/org/invite", json={"email": ORG_MATE})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        body = resp.json()
        self.assertTrue(body["joined_existing_account"])
        self.assertEqual(body["email"], ORG_MATE)

    def test_a_new_address_gets_an_invitation_code(self):
        issued = []

        class _Store:
            @staticmethod
            def get_user_by_email(_a):
                return None

            @staticmethod
            def create_code(address, ttl_seconds=None):
                issued.append((address, ttl_seconds))
                return "CODE123"

        with self._invite(None) as c, \
             patch.object(orgs, "upsert_invitation",
                          return_value={"id": 3, "status": "invited"}), \
             patch.object(orgs, "attach_existing_account", return_value=None), \
             patch("routers.org_admin.auth_store", _Store), \
             patch("routers.org_admin._mail_invite", return_value=("sent", "")):
            resp = c.post("/api/org/invite", json={"email": ORG_MATE})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assertEqual([a for a, _ in issued], [ORG_MATE])
        self.assertFalse(resp.json()["joined_existing_account"])
        # The code is in the response as well as in the mail: an unconfigured mail
        # server must still produce an invitation somebody can finish.
        self.assertEqual(resp.json()["code"], "CODE123")
        self.assertEqual(resp.json()["mail"], "sent")

    def test_the_invitation_code_outlives_the_verification_code(self):
        """An invitation is read hours later, so it cannot share the 10-minute code TTL.

        `create_code`'s default is the verification TTL; passing none would mint a code
        that is already dead by the time the mail is opened, and the test above would
        still pass because it only checked that a code came back.
        """
        ttls = []

        class _Store:
            @staticmethod
            def get_user_by_email(_a):
                return None

            @staticmethod
            def create_code(_a, ttl_seconds=None):
                ttls.append(ttl_seconds)
                return "CODE123"

        with self._invite(None) as c, \
             patch.object(orgs, "upsert_invitation",
                          return_value={"id": 3, "status": "invited"}), \
             patch.object(orgs, "attach_existing_account", return_value=None), \
             patch("routers.org_admin.auth_store", _Store), \
             patch("routers.org_admin._mail_invite", return_value=("sent", "")):
            resp = c.post("/api/org/invite", json={"email": ORG_MATE})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assertEqual(ttls, [settings.AUTH_INVITE_TTL_SECONDS], ttls)

    def test_a_mail_failure_does_not_fail_the_invitation(self):
        """The same rule the existing invitations and agent codes follow: a missing mail
        server must not dead-end, because the code comes back in the response."""
        class _Store:
            @staticmethod
            def get_user_by_email(_a):
                return None

            @staticmethod
            def create_code(_a, ttl_seconds=None):
                return "CODE123"

        with self._invite(None) as c, \
             patch.object(orgs, "upsert_invitation",
                          return_value={"id": 3, "status": "invited"}), \
             patch.object(orgs, "attach_existing_account", return_value=None), \
             patch("routers.org_admin.auth_store", _Store), \
             patch("routers.org_admin._mail_invite", return_value=("unconfigured", "")):
            resp = c.post("/api/org/invite", json={"email": ORG_MATE})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assertEqual(resp.json()["code"], "CODE123")
        self.assertNotEqual(resp.json()["mail"], "sent")


# ── destructive operations ───────────────────────────────────────────────────

class MemberChangeTests(unittest.TestCase):
    def _call(self, method, path, **body):
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "set_member_role", return_value={"id": 5}) as setter:
            resp = c.request(method, path, **body)
        return resp, setter

    def test_demoting_the_last_administrator_is_refused(self):
        """⚠️ The failure this prevents is a company that can never be administered
        again, discovered by the next person to open the page — and the only fix is an
        operator in a database console."""
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "set_member_role", side_effect=orgs.OrgError(
                 "这是本企业最后一位管理员，不能降级 / this is the last administrator")):
            resp = c.put("/api/org/members/5/role", json={"org_role": "member"})
        self.assertEqual(resp.status_code, 400, resp.text[:200])

    def test_removing_a_member_does_not_close_the_account(self):
        """⚠️ Returned explicitly because it is the easy mistake: the endpoint is under
        "administration", it removes a person from a company, and somebody reads it as
        a close. The account and every document it owns are untouched."""
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "disable_member", return_value={"id": 5}) as disabler:
            resp = c.delete("/api/org/members/5")
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertFalse(resp.json()["account_closed"])

    def test_the_removal_is_scoped_to_the_callers_company(self):
        """⚠️ Asserting the ARGUMENT, not just that the service was reached.

        `test_a_member_in_another_company_is_404` patches `disable_member` and so never
        exercises the service; and a router that simply omitted `expected_org_id` would
        pass a "was it called?" assertion while the service fell back to "any company".
        The scope has to be visible in the call.
        """
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "disable_member", return_value={"id": 5}) as disabler:
            resp = c.delete("/api/org/members/5")
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        disabler.assert_called_once()
        self.assertEqual(disabler.call_args.kwargs.get("expected_org_id"), ORG_ID)

    def test_a_role_change_is_scoped_to_the_callers_company(self):
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "set_member_role", return_value={"id": 5}) as setter:
            resp = c.put("/api/org/members/5/role", json={"org_role": "admin"})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(setter.call_args.kwargs.get("expected_org_id"), ORG_ID)

    def test_a_member_in_another_company_is_404(self):
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "disable_member", side_effect=orgs.MemberScopeError(
                 "成员不存在 / no such member")):
            resp = c.delete("/api/org/members/999")
        self.assertEqual(resp.status_code, 404)

    def test_an_unknown_module_is_404_before_anything_is_written(self):
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "set_member_module") as setter:
            resp = c.put("/api/org/members/5/modules/nope", json={"enabled": False})
        self.assertEqual(resp.status_code, 404)
        setter.assert_not_called()

    def test_a_member_without_an_account_cannot_get_modules(self):
        """An invitation is not an account. Silently succeeding here would write a row
        keyed to nothing."""
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "set_member_module", side_effect=orgs.OrgError(
                 "该成员还没有账号 / this member has no account yet")):
            resp = c.put("/api/org/members/5/modules/workspace", json={"enabled": False})
        self.assertEqual(resp.status_code, 400, resp.text[:200])

    def test_null_means_follow_the_deployment_default(self):
        """⚠️ The third state of the tri-state cell. It is a NULL in the body, not a
        third route and not `enabled: false` — "switched off" and "nobody said anything"
        are different facts, and only one of them is a row."""
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "clear_member_module",
                          return_value={"id": 5}) as clearer, \
             patch.object(orgs, "set_member_module") as setter:
            resp = c.put("/api/org/members/5/modules/workspace", json={"enabled": None})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        clearer.assert_called_once()
        setter.assert_not_called()

    def test_a_missing_body_is_also_follow_the_default(self):
        """An empty `{}` must not fall through to `bool(None) = False` and quietly mean
        "switched off" — the exact inversion the tri-state exists to prevent."""
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "clear_member_module",
                          return_value={"id": 5}) as clearer, \
             patch.object(orgs, "set_member_module") as setter:
            resp = c.put("/api/org/members/5/modules/workspace", json={})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        clearer.assert_called_once()
        setter.assert_not_called()


class ResetMemberModulesTests(unittest.TestCase):
    """"恢复默认" on a matrix row: `DELETE` on the COLLECTION of one member's modules."""

    def test_it_clears_every_module_in_one_call(self):
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "clear_member_modules", return_value=4) as reset:
            resp = c.delete("/api/org/members/5/modules")
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        reset.assert_called_once()
        self.assertEqual(resp.json()["cleared"], 4)
        self.assertEqual(resp.json()["module_overrides"], {},
                         "the response must state the state the member is now in")

    def test_a_member_who_was_never_narrowed_is_a_success(self):
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "clear_member_modules", return_value=0):
            resp = c.delete("/api/org/members/5/modules")
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(resp.json()["cleared"], 0)

    def test_it_is_scoped_to_the_callers_company(self):
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "clear_member_modules", side_effect=orgs.MemberScopeError(
                 "该成员不属于本企业 / that member is not in this organization")) as reset:
            resp = c.delete("/api/org/members/5/modules")
        self.assertEqual(resp.status_code, 404, resp.text[:200])
        reset.assert_called_once()

    def test_the_singular_route_still_sets_one_module(self):
        """The two routes are not aliases. `/modules/{key}` narrows ONE module; a DELETE
        on `/modules` drops the row. Merging them would make "恢复默认" mean "turn
        everything off"."""
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c:
            paths = {r.path for r in org_admin.router.routes}
        self.assertIn("/api/org/members/{member_id}/modules", paths)
        self.assertIn("/api/org/members/{member_id}/modules/{module_key}", paths)


# ── sharing policy ───────────────────────────────────────────────────────────

# ── what a company is licensed to hand out ──────────────────────────────────
#
# The platform operator licences each company a set of modules in the console; the
# company's own administrators may only tick those. The interesting part is not that the
# dialog is filtered — a dialog is a hint — but that the WRITE is refused. Without the
# refusal the licence is enforced by nothing, and the filter is the kind of UI that gets
# removed the first time somebody wants one more module.

class OrgModuleLicenceTests(unittest.TestCase):
    """`orgs.module_keys` — the per-company ceiling, and the refusal that enforces it."""

    def _org(self, keys):
        # `None` stays `None` — "never configured" is not "configured to nothing".
        return dict(_orgs_by_id(ORG_ID), module_keys=(None if keys is None
                                                     else list(keys)))

    def _client_for(self, keys):
        return _Scope({"email": ORG_ADMIN, "role": "user"}, get_org=self._org(keys))

    def test_a_company_licensed_for_nothing_may_hand_out_nothing(self):
        with self._client_for([]) as c, \
             patch.object(orgs, "set_member_module") as setter:
            resp = c.put("/api/org/members/5/modules/calendar", json={"enabled": True})
        self.assertEqual(resp.status_code, 400, resp.text[:200])
        setter.assert_not_called()

    def test_an_unconfigured_company_may_hand_out_everything_switchable(self):
        with self._client_for(None) as c, \
             patch.object(orgs, "set_member_module", return_value={"id": 5}) as setter:
            resp = c.put("/api/org/members/5/modules/calendar", json={"enabled": True})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        setter.assert_called_once()

    def test_a_licensed_company_may_hand_out_what_it_was_given(self):
        with self._client_for(["workspace", "knowledge"]) as c, \
             patch.object(orgs, "set_member_module", return_value={"id": 5}) as setter:
            resp = c.put("/api/org/members/5/modules/knowledge", json={"enabled": False})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        setter.assert_called_once()

    def test_a_module_outside_the_licence_is_refused(self):
        with self._client_for(["workspace"]) as c, \
             patch.object(orgs, "set_member_module") as setter:
            resp = c.put("/api/org/members/5/modules/calendar", json={"enabled": True})
        self.assertEqual(resp.status_code, 400, resp.text[:200])
        setter.assert_not_called()

    def test_clearing_a_module_outside_the_licence_is_refused_too(self):
        """The `null` path drops a row. If it were left unguarded, an administrator could
        use it as a probe for which keys exist, and — worse — a stale row the operator has
        since withdrawn would stay in the table being read as "a decision somebody made".
        """
        with self._client_for(["workspace"]) as c, \
             patch.object(orgs, "clear_member_module") as clearer:
            resp = c.put("/api/org/members/5/modules/calendar", json={"enabled": None})
        self.assertEqual(resp.status_code, 400, resp.text[:200])
        clearer.assert_not_called()

    def test_a_required_module_is_never_offered(self):
        """`inbox` is infrastructure, not product. A company cannot be licensed out of
        it, because a company that cannot be told what happened to it is worse than one
        that has a module too many."""
        with self._client_for(["workspace"]) as c:
            body = c.get("/api/org/me").json()
        self.assertNotIn("inbox", body["org"]["module_options"])
        with self._client_for(["workspace"]) as c, \
             patch.object(orgs, "set_member_module") as setter:
            resp = c.put("/api/org/members/5/modules/inbox", json={"enabled": False})
        self.assertEqual(resp.status_code, 400, resp.text[:200])
        setter.assert_not_called()


class MeCarriesTheLicenceTests(unittest.TestCase):
    """`/api/org/me` is what the page renders its checkboxes from."""

    def _me(self, keys):
        row = _membership(ORG_ADMIN, ORG_ID, module_keys=keys)
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "membership_of", return_value=row), \
             patch.object(orgs, "is_org_admin", return_value=True):
            return c.get("/api/org/me").json()

    def test_the_raw_licence_travels_alongside_what_it_leaves_open(self):
        body = self._me(["workspace", "knowledge"])
        self.assertEqual(body["org"]["module_licence"], ["workspace", "knowledge"])
        self.assertEqual(body["org"]["module_options"], ["workspace", "knowledge"])

    def test_module_options_is_the_OFFER_not_the_licence(self):
        """A licence naming a module the deployment has withdrawn must not appear as an
        option: the administrator would tick it, the write would succeed, and
        `resolve()` would ignore the row — a checkbox that reports success and changes
        nothing."""
        body = self._me(["workspace", "no-such-module"])
        self.assertEqual(body["org"]["module_licence"],
                         ["workspace", "no-such-module"])
        self.assertEqual(body["org"]["module_options"], ["workspace"])

    def test_an_unconfigured_company_still_gets_the_whole_switchable_set(self):
        body = self._me(None)
        self.assertIsNone(body["org"]["module_licence"])
        self.assertIn("workspace", body["org"]["module_options"])
        self.assertIn("calendar", body["org"]["module_options"])


class ModuleKeyValidationTests(unittest.TestCase):
    """`orgs._clean_module_keys` — the shared validator both processes call."""

    def test_an_unknown_key_is_refused_rather_than_dropped(self):
        with self.assertRaises(orgs.OrgError) as ctx:
            orgs._clean_module_keys(["workspace", "nope"])
        self.assertIn("nope", str(ctx.exception))

    def test_a_required_module_is_refused(self):
        with self.assertRaises(orgs.OrgError):
            orgs._clean_module_keys(["inbox"])

    def test_case_and_whitespace_are_normalised(self):
        self.assertEqual(orgs._clean_module_keys([" Workspace ", "CALENDAR"]),
                         ["workspace", "calendar"])

    def test_an_empty_list_is_a_licence_of_nothing_and_is_returned_as_one(self):
        self.assertEqual(orgs._clean_module_keys([]), [])

    def test_unconfigured_and_configured_to_nothing_are_different_answers(self):
        """⚠️ The distinction the whole feature rests on. `None` is "this company was
        never licensed" and follows the deployment; `()` is "the operator unticked every
        box" and offers nothing. A truthiness check on either side of this — which is what
        the first version of `org_offer` had — reads the second as the first, so closing a
        company down hands its administrators a full set of checkboxes whose every save is
        refused."""
        with patch.object(orgs, "membership_of", return_value={"module_keys": None}):
            self.assertIsNone(orgs.module_licence_of("a@corp.example"))
        with patch.object(orgs, "membership_of", return_value={"module_keys": []}):
            self.assertEqual(orgs.module_licence_of("a@corp.example"), ())
        with patch.object(orgs, "membership_of", return_value=None):
            self.assertIsNone(orgs.module_licence_of("a@corp.example"))
        with patch.object(orgs, "membership_of",
                          return_value={"module_keys": ["workspace"]}):
            self.assertEqual(orgs.module_licence_of("a@corp.example"), ("workspace",))


class ScopePolicyTests(unittest.TestCase):
    def _put(self, body):
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "set_scopes",
                          return_value=dict(_orgs_by_id(ORG_ID), **body)) as setter:
            resp = c.put("/api/org/scopes", json=body)
        return resp, setter

    def test_a_preset_writes_the_two_columns(self):
        """⚠️ The preset is not a third state. It is written AS the two columns, so
        reading it back is a pure function of them and there is no way for the three
        representations to disagree."""
        resp, setter = self._put({"preset": "external"})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        setter.assert_called_once_with(ORG_ID, "external", "approve")

    def test_the_two_columns_can_be_set_independently(self):
        """The middle state the single enum could not express: share with a client's
        address, but publish nothing."""
        resp, setter = self._put({"share_scope": "external", "public_scope": "forbid"})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        setter.assert_called_once_with(ORG_ID, "external", "forbid")

    def test_an_unknown_preset_is_refused_before_any_write(self):
        resp, setter = self._put({"preset": "wide-open"})
        self.assertEqual(resp.status_code, 400, resp.text[:200])
        setter.assert_not_called()

    def test_an_invalid_scope_value_is_refused(self):
        resp, setter = self._put({"share_scope": "somewhere-else"})
        self.assertEqual(resp.status_code, 400)
        setter.assert_not_called()

    def test_the_preset_is_read_back_from_the_columns(self):
        for name, (share, public) in orgs.SCOPE_PRESETS.items():
            with self.subTest(preset=name):
                org_row = {"share_scope": share, "public_scope": public}
                self.assertEqual(org_admin._preset_of(org_row), name)
        self.assertEqual(org_admin._preset_of(
            {"share_scope": "external", "public_scope": "forbid"}), "",
            "a combination that is not a preset must read back as none, not as a lie")

    def test_the_page_is_told_which_preset_is_current_and_which_exist(self):
        """⚠️ The Settings card used to carry its OWN table of the three presets and got
        one of the three wrong: it paired `internal` with "nothing may be published",
        while `SCOPE_PRESETS["internal"]` is `("internal", "approve")` — publish one
        document at a time. The page rendered, the badge said "current", and clicking a
        preset wrote the wrong two columns.

        A duplicated vocabulary is a bug with no error message, so the fix is structural:
        `/api/org/me` publishes the mapping and the page asks for it. This asserts the
        published mapping IS the shared one, for every preset, plus the `orgs` round trip.
        """
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c:
            resp = c.get("/api/org/me")
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        org = resp.json()["org"]
        self.assertEqual(org["presets"],
                         [{"key": k, "share_scope": s, "public_scope": p}
                          for k, (s, p) in orgs.SCOPE_PRESETS.items()],
                         "the page must be offered exactly the shared vocabulary")
        for key, (share, public) in orgs.SCOPE_PRESETS.items():
            with self.subTest(preset=key):
                # The published pair, fed back through the reader, has to name itself.
                self.assertEqual(org_admin._preset_of(
                    {"share_scope": share, "public_scope": public}), key)
        self.assertEqual(org["preset"], org_admin._preset_of(org),
                         "the current preset must be derived from the two columns")

    def test_an_admin_cannot_configure_another_company(self):
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "set_scopes") as setter:
            resp = c.put("/api/org/scopes", json={"preset": "global"},
                         params={"org_id": OTHER_ORG_ID})
        self.assertEqual(resp.status_code, 404)
        setter.assert_not_called()


class OrgSettingsTests(unittest.TestCase):
    """`PUT /api/org/settings` — the company's own name and email suffixes.

    The interesting property is NOT that it writes. It is that it writes *partially*:
    a form that changes the name and leaves the suffix box alone must not blank the
    suffix list, and a suffix list is the one field whose accidental emptying locks the
    administrator out of their own company.
    """

    def _put(self, body, **extra):
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "update_org",
                          return_value=dict(_orgs_by_id(ORG_ID), **body)) as setter:
            resp = c.put("/api/org/settings", json=body, **extra)
        return resp, setter

    def test_the_name_and_the_suffixes_are_written_together(self):
        resp, setter = self._put({"name": "Corp GmbH", "email_domains": ["corp.example"]})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        setter.assert_called_once_with(ORG_ID, name="Corp GmbH",
                                       email_domains=["corp.example"])

    def test_an_omitted_field_is_left_alone_rather_than_blanked(self):
        """⚠️ This is the whole reason the model uses `None` and not `""`/`[]`. Pydantic
        gives an absent field its default, so a name-only form must arrive as
        `email_domains=None` and reach `update_org` as "do not touch"."""
        resp, setter = self._put({"name": "Renamed"})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertIsNone(setter.call_args.kwargs["email_domains"])

    def test_narrowing_the_suffix_list_is_allowed(self):
        resp, setter = self._put({"email_domains": ["corp.example", "corp.co.uk"]})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(setter.call_args.kwargs["email_domains"],
                         ["corp.example", "corp.co.uk"])

    def test_clearing_the_suffix_list_is_refused(self):
        """A refusal, not a silent no-op: an empty list is a lockout, and the admin has
        to be told which field caused it."""
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "update_org", side_effect=orgs.OrgError(
                 "至少要保留一个企业邮箱后缀，否则自助注册的同事不会自动归属本企业 / an "
                 "organization must keep at least one email suffix — narrowing the list "
                 "is allowed, clearing it is not")) as setter:
            resp = c.put("/api/org/settings", json={"email_domains": []})
        self.assertEqual(resp.status_code, 400, resp.text[:200])
        self.assertIn("suffix", resp.json()["detail"])
        setter.assert_called_once()

    def test_an_admin_cannot_edit_another_company(self):
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "update_org") as setter:
            resp = c.put("/api/org/settings", json={"name": "Not mine"},
                         params={"org_id": OTHER_ORG_ID})
        self.assertEqual(resp.status_code, 404)
        setter.assert_not_called()

    def test_the_slug_is_not_settable(self):
        """⚠️ Asserted on the RESPONSE MODEL, not on the handler. A route accepts whatever
        pydantic allows, so the guarantee that a caller cannot move a company's permanent
        public handle has to be proved by the model refusing to carry the field."""
        self.assertNotIn("slug", org_admin.OrgSettingsIn.model_fields)
        self.assertEqual(set(org_admin.OrgSettingsIn.model_fields), {"name", "email_domains"})

    def test_an_ordinary_member_cannot_reach_these_settings(self):
        with _Scope({"email": ORG_MATE, "role": "user"}) as c, \
             patch.object(orgs, "is_org_admin", return_value=False), \
             patch.object(orgs, "update_org") as setter:
            resp = c.put("/api/org/settings", json={"name": "Nope"})
        self.assertEqual(resp.status_code, 403, resp.text[:200])
        setter.assert_not_called()


# ── publication approvals ────────────────────────────────────────────────────

class ApprovalTests(unittest.TestCase):
    def test_an_unknown_decision_is_400_not_404(self):
        """⚠️ Three routes (`/approve`, `/deny`, `/revoke`) would make a typo read as
        "no such approval request" — the same message the genuinely-missing case gives,
        for a completely different mistake."""
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "decide_public_approval", side_effect=orgs.OrgError(
                 "未知的审批结果 / unknown approval decision")) as decide:
            resp = c.post("/api/org/approvals/1/lukewarm")
        self.assertEqual(resp.status_code, 400, resp.text[:200])

    def test_an_approval_in_another_company_is_404(self):
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "decide_public_approval",
                          side_effect=orgs.MemberScopeError(
                              "审批记录不存在 / no such approval request")):
            resp = c.post("/api/org/approvals/1/approved")
        self.assertEqual(resp.status_code, 404)

    def test_the_decision_is_scoped_to_the_callers_company(self):
        """⚠️ The scope is an ASSERTION inside the UPDATE, not a filter the router
        applies afterwards. Passing it is what stops a guessed id from deciding another
        company's request."""
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "decide_public_approval",
                          return_value={"id": 1, "status": "approved"}) as decide:
            resp = c.post("/api/org/approvals/1/approved")
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        decide.assert_called_once_with(1, "approved", ORG_ADMIN, expected_org_id=ORG_ID)

    # ── the escape hatch from the gate ─────────────────────────────────────────

    def test_the_request_endpoint_still_refuses_a_non_member(self):
        """Weaker than `_resolve`, not absent.

        The endpoint now resolves from the membership row instead of the admin role, so
        the assertion has to move with it: a stranger with no company has nothing to be
        approved by, and a 400 saying so is a different answer from a 403 pretending
        they are a member who may not ask.
        """
        with _Scope({"email": "stranger@example.invalid", "role": "user"}) as c, \
             patch.object(orgs, "effective_profile",
                          return_value=("global", "allow", None)), \
             patch.object(orgs, "is_org_admin", return_value=False):
            resp = c.post("/api/org/public-requests", json={
                "target_type": "report", "target_id": "q3", "kind": "public"})
        self.assertEqual(resp.status_code, 400, resp.text[:300])

    def test_the_request_endpoint_is_scoped_to_the_callers_company(self):
        """Same guarantee as the decision route: naming another company is a 404, not a
        write against it."""
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "effective_profile",
                          return_value=("internal", "approve", ORG_ID)), \
             patch.object(orgs, "is_org_admin", return_value=True):
            resp = c.post("/api/org/public-requests?org_id=999", json={
                "target_type": "report", "target_id": "q3", "kind": "public"})
        self.assertEqual(resp.status_code, 404, resp.text[:300])

    def test_a_member_can_request_publication(self):
        """⚠️ Without this the gate is a dead end: the product says "ask your
        administrator" and offers no way to ask.

        ⚠️ The identity here used to be `ORG_ADMIN`, in a test whose name says "a
        member" — so it proved the one case that was never in doubt and stayed green
        through the whole period the endpoint was broken for actual members. A fake that
        cannot walk into the branch under test is worse than no fake: it reports the
        branch as covered. The caller is a plain member and `is_org_admin` is False.
        """
        plain = f"member{ORG_ID}@example.invalid"
        with _Scope({"email": plain, "role": "user"}) as c, \
             patch.object(orgs, "effective_profile",
                          return_value=("internal", "approve", ORG_ID)), \
             patch.object(orgs, "is_org_admin", return_value=False), \
             patch.object(orgs, "request_public_approval",
                          return_value={"id": 9, "status": "pending"}) as request:
            resp = c.post("/api/org/public-requests",
                          json={"target_type": "report", "target_id": "q3",
                                "kind": "public"})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        request.assert_called_once_with(plain, "report", "q3", "public")

    def test_a_request_without_a_document_is_refused(self):
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "request_public_approval") as request:
            resp = c.post("/api/org/public-requests",
                          json={"target_type": "report", "kind": "public"})
        self.assertEqual(resp.status_code, 400)
        request.assert_not_called()

    def test_the_approval_list_is_scoped_to_the_callers_company(self):
        with _Scope({"email": ORG_ADMIN, "role": "user"}) as c, \
             patch.object(orgs, "list_approvals", return_value=[]) as listed:
            resp = c.get("/api/org/approvals")
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        listed.assert_called_once_with(org_id=ORG_ID, status="pending")

    def test_an_operator_may_see_every_companys_queue(self):
        with _Scope({"email": OPERATOR, "role": "admin"}) as c, \
             patch.object(orgs, "list_approvals", return_value=[]) as listed:
            resp = c.get("/api/org/approvals")
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        listed.assert_called_once_with(org_id=None, status="pending")


# ── the prefix itself ────────────────────────────────────────────────────────

class PrefixSeparationTests(unittest.TestCase):
    """`/api/org/*` must not be reachable by the installation-level gate, and the
    operator surface must not answer to an enterprise admin."""

    def test_no_org_endpoint_is_in_the_operator_route_list(self):
        """`core/access.requires_admin` is a regex list read by middleware, before any
        handler runs. An `/api/org` entry there would make the whole prefix
        operator-only — the exact inversion of what it is for."""
        from core import access
        import re as _re
        for _method, pattern in access._ADMIN_ONLY_ROUTES:      # noqa: SLF001
            self.assertIsNone(_re.search(r"/api/org", pattern.pattern),
                              f"operator gate claims {pattern.pattern}")

    def test_every_org_path_is_under_the_org_prefix(self):
        app = FastAPI()
        app.include_router(org_admin.router)
        paths = {r.path for r in app.routes if getattr(r, "path", "").startswith("/api/")}
        self.assertTrue(paths)
        for path in paths:
            self.assertTrue(path.startswith("/api/org/"), path)

    def test_the_router_does_not_depend_on_the_operator_predicate(self):
        """`is_admin_identity` may only ever WIDEN, never gate.

        Checked with `ast`, not with a substring search: this module's docstring spends
        several paragraphs explaining why `requires_admin` is deliberately not used here,
        so `assertNotIn("requires_admin", source)` fails on the explanation of the very
        rule it is trying to enforce. A test that cannot tell prose from code is not
        checking the code.
        """
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(org_admin))

        imported, called = set(), []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.Name):
                called.append((node.id, getattr(node, "lineno", 0)))

        for forbidden in ("requires_admin", "may_write_app_setting",
                          "_ADMIN_ONLY_ROUTES"):
            self.assertNotIn(forbidden, imported,
                             f"the router imports the operator gate {forbidden}")
            self.assertFalse([c for c in called if c[0] == forbidden],
                             f"the router calls the operator gate {forbidden}")

        # `is_admin_identity` is used only to RECOGNISE an operator. A negated use would
        # be the installation policy leaking into the company one, and it is invisible
        # in review because the name reads the same either way.
        #
        # ⚠️ Asserted as a property over ALL call sites, never as a count. This used to
        # say `assertEqual(len(uses), 2)`, which is a proxy that goes stale the moment a
        # legitimate third resolver is added — and the fix for the publication-request
        # bug added exactly that, producing a red test with no defect behind it. A
        # count also cannot catch the thing it was written for: a fourth call in the
        # wrong form still satisfies `len(uses) == 4`. The minimum keeps the floor under
        # it, so deleting a resolver is still caught.
        self.assertIn("is_admin_identity", imported)
        uses = [(node, src) for node, src in
                ((n, inspect.getsource(org_admin).splitlines()[n.lineno - 1])
                 for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "is_admin_identity")]
        self.assertGreaterEqual(len(uses), 2, [src for _n, src in uses])
        for _node, src in uses:
            self.assertIn("if is_admin_identity(", src,
                          f"is_admin_identity must be a widening branch: {src.strip()}")

    def test_no_org_endpoint_is_writable_by_an_agent_code(self):
        """An agent access code may READ the organization, never administer it.

        ⚠️ This is asserted as a *negative* because the failure mode is a silent
        capability grant, not an error. `_AGENT_WRITE_ALLOWED_PREFIXES` is a list of
        prefixes somebody extends when they add an endpoint ("the agent should be able
        to invite people too"), and `/api/org` sitting next to `/api/dashboard` in that
        list would let a machine demote the last administrator of a company or reset an
        employee's module grants. Nothing crashes; the API simply does more than its
        documentation says.
        """
        from main import _agent_may_write
        app = FastAPI()
        app.include_router(org_admin.router)
        paths = sorted({r.path for r in app.routes
                        if getattr(r, "path", "").startswith("/api/org")})
        self.assertTrue(paths)
        for path in paths:
            self.assertFalse(_agent_may_write(path),
                             f"an agent code may administer {path} — the write "
                             f"allowlist in main.py has grown a /api/org entry")

    def test_reading_the_org_is_still_allowed_to_an_agent_code(self):
        """The other half of the test above, and the reason it is not just "off".

        Read stays allowed, and deliberately so: an agent that can write a knowledge page
        or a report needs to know whether that document may be shared outside, or it will
        author content its owner's company policy forbids — and the answer is
        `GET /api/org/me`. Asserting only the refusal would pass just as well if the whole
        prefix had been switched off, which is a different and equally wrong product.
        """
        from main import _AGENT_POST_READONLY
        import re as _re
        readable = {r.pattern for r in _AGENT_POST_READONLY}
        self.assertTrue(readable, "the POST-readonly list is empty, which is suspicious")
        # `_agent_may_write` only answers the WRITE question, and that is the right
        # question here: the read methods are governed by `_READ_METHODS`, not by this
        # list, so a non-empty read-only list plus a refused write is a readable prefix.
        from main import _agent_may_write
        self.assertFalse(_agent_may_write("/api/org/approvals/7/approve"))
        # …and the name one letter away must not ride along. The allowlist matches with
        # `startswith`, so `/api/organizer/…` would inherit company administration
        # without anybody editing the list.
        self.assertFalse(_agent_may_write("/api/organizer/anything"),
                         "a prefix match is taking a neighbouring name with it")


if __name__ == "__main__":
    unittest.main()
