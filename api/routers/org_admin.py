"""Enterprise administration: `/api/org/*`.

⚠️ **This prefix is separate from `/api/admin/*` on purpose.** The operator endpoints
govern the *installation* (every account, the module allowlist, the mail server, the
recycle bin) and their gate is `core/access.requires_admin`, which runs in the
middleware before any handler. Enterprise administration governs *one company* and
belongs to a person who is not an operator.

Folding them together would have meant one of two bad things: adding an org dimension
to `_ADMIN_ONLY_ROUTES` — a regex list read by middleware that has no idea who the
caller is — or a "maybe the caller is an enterprise admin" branch inside the existing
predicate, which would quietly widen the operator policy that every destructive route
in the product depends on.

## The scope rule, in one sentence

**A caller's organization comes from their membership row. A query parameter may only
confirm it, never choose it.**

The consequence is that every handler must call `_resolve()` and use the id it RETURNS.
Getting this wrong is not subtle in the code and very subtle in review:

```python
_user, scope = _scoped_org(request, org_id)      # scope is the caller's own org
if scope is None and org_id is not None:         # skipped — an admin HAS a scope
    _visible_org_or_404(request, org_id)
target = org_id if org_id is not None else scope  # …so target is the OTHER company
```

That was a real, shipped version of `list_members`: an enterprise administrator could
read another company's member list by adding `?org_id=`. `_resolve()` closes it by
returning the answer, and by refusing a parameter that disagrees with the caller's own
company — silently doing something else is worse than saying no, because the caller
then believes they changed the organization they named.

404 rather than 403 throughout: "that company exists but is not yours" is a directory of
every company in the installation.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core import modules as core_modules
from core.config import settings
from core.i18n import pick, request_lang
from klado_shared import orgs
from klado_shared.identity import is_admin_identity
from services import account_modules, auth_store

router = APIRouter(prefix="/api/org", tags=["企业管理"])
log = logging.getLogger(__name__)


# ── identity and scope ───────────────────────────────────────────────────────

def _caller(request: Request) -> dict:
    user = getattr(request.state, "current_user", None) or {}
    if not user.get("email"):
        raise HTTPException(status_code=401, detail=pick(
            "请先登录 / sign in first", request_lang(request)))
    return user


def _org_error(exc: orgs.OrgError, request: Request) -> HTTPException:
    """Turn a domain refusal into the right HTTP answer.

    ⚠️ Two statuses, and the split is the point. `MemberScopeError` is 404: "that member
    belongs to another company" is a fact about somebody else's company, and confirming
    it turns an id into a directory of every account in the installation. Everything else
    is 400 — a rule the caller broke, with a message saying which rule.

    Leaving this out is not a crash: FastAPI turns the unhandled exception into a 500,
    which reads as "the server is broken" for what is actually a policy the caller needs
    to be told about.
    """
    status = 404 if isinstance(exc, orgs.MemberScopeError) else 400
    return HTTPException(status_code=status, detail=pick(str(exc), request_lang(request)))


def _call(fn, *args, request: Request, **kwargs):
    """Run a service call, translating its refusals. Every handler goes through this."""
    try:
        return fn(*args, **kwargs)
    except orgs.OrgError as exc:
        raise _org_error(exc, request) from exc


def _vocab(allowed: tuple, value: str, request: Request, pair: str) -> str:
    """One value out of a fixed vocabulary, or a 400 naming the options.

    ⚠️ Routed through the shared module rather than a local `in` check, so a new role or
    scope value is added in one place and this endpoint cannot fall behind it — the
    alternative is a list of valid values in the router that quietly stops matching.
    """
    try:
        return orgs._one_of(value, allowed, pair.split(" /")[0], pair)
    except orgs.OrgError as exc:
        raise _org_error(exc, request) from exc


def _resolve(request: Request, requested: Optional[int] = None
             ) -> tuple[dict, Optional[int], str]:
    """`(caller, RESOLVED org_id, lang)` — what every handler actually needs.

    ⚠️ The returned id is the ANSWER, not the input:

    * platform operator → the parameter, or `None` for "not narrowed to one company";
    * enterprise admin  → their own company, and a parameter naming a different one is
      **refused** rather than ignored.

    Refusing beats ignoring. A handler that quietly acted on the caller's own company
    while they believed they were editing another one is worse than a 404: the 200 they
    got back would be about the wrong organization.
    """
    user = _caller(request)
    email = (user.get("email") or "").strip().lower()
    lang = request_lang(request)
    # ⚠️ Order is load-bearing, and it is the whole operator-vs-company question:
    #
    #   1. a company administrator resolves to THEIR company, and a parameter naming a
    #      different one is refused — the scope is never widened by a parameter;
    #   2. an operator who ALSO administers a company is a company administrator here.
    #      The page never names a company, so `requested` is None and the old early
    #      return answered None, which every handler below turned into "name the
    #      organization to look at" — a 400 on the roster of the company the caller owns.
    #   3. an operator with no company keeps the operator's own scope: whatever they
    #      name, or None to mean "not narrowed to one company".
    #
    # Rule 1 is checked first, so a dual-identity caller naming a DIFFERENT company is
    # still refused exactly like any other administrator — holding the operator flag must
    # not become a way around the company boundary on the page that enforces it.
    if orgs.is_org_admin(email):
        own = orgs.membership_of(email)
        org_id = own["org_id"]
        if requested is not None and requested != org_id:
            raise HTTPException(status_code=404, detail=pick(
                "企业不存在 / no such organization", lang))
        return user, org_id, lang
    if is_admin_identity(user):
        return user, requested, lang
    raise HTTPException(status_code=403, detail=pick(
        "只有企业管理员可以管理企业成员 / only an enterprise administrator can "
        "manage the members of this organization", lang))


def _resolve_member(request: Request, requested: Optional[int] = None
                    ) -> tuple[dict, Optional[int], str]:
    """`(caller, RESOLVED org_id, lang)` for a **member**, not an administrator.

    ⚠️ Deliberately weaker than `_resolve()`, and the reason is a bug this file's own
    docstring predicted. `may_publish()` holds *every* member of a company to that
    company's `public_scope`, so a plain member publishing outward gets
    `NEEDS_APPROVAL` — a message that tells them to "submit a request and it will be
    reviewed". The request endpoint then called `_resolve()`, which demands
    `is_org_admin`, and answered a member who took that instruction literally with
    "only an enterprise administrator can manage the members of this organization" —
    a message about a completely different action. So the product said *ask your
    administrator*, offered no way to ask, and refused the one person it was addressed
    to: a dead end dressed as a policy, on the endpoint whose docstring says it exists
    to prevent exactly that.

    The org comes from the membership row, the same way `effective_profile()` finds it,
    so the person who was refused and the person who may ask are decided by the same
    rule. A non-member has no company, so there is nothing to approve — and
    `orgs.request_public_approval` says so with a better message than a 403 would.
    """
    user = _caller(request)
    email = (user.get("email") or "").strip().lower()
    lang = request_lang(request)
    # ⚠️ Same widening-vs-early-return shape as `_resolve()`, and the same reason: an
    # operator who also belongs to a company has to resolve to THAT company here, or
    # `requested` arrives as None from a page that never names one.
    if orgs.is_org_admin(email):
        own = orgs.membership_of(email)
        org_id = own["org_id"]
        if requested is not None and requested != org_id:
            raise HTTPException(status_code=404, detail=pick(
                "企业不存在 / no such organization", lang))
        return user, org_id, lang
    if is_admin_identity(user):
        return user, requested, lang
    _share, _public, org_id = orgs.effective_profile(email)
    if org_id is None:
        raise HTTPException(status_code=400, detail=pick(
            "你不在任何企业内，无需审批 / you are not in an organization, so there is "
            "nothing to approve", lang))
    if requested is not None and requested != org_id:
        raise HTTPException(status_code=404, detail=pick(
            "企业不存在 / no such organization", lang))
    return user, org_id, lang


def _require_org(request: Request, requested: Optional[int], what: str
                 ) -> tuple[dict, dict, str]:
    """`(caller, org_row, lang)` for one organization that must exist and be visible."""
    user, org_id, lang = _resolve(request, requested)
    if org_id is None:
        raise HTTPException(status_code=400, detail=pick(
            f"请指定要{what}的企业 / name the organization to {what}", lang))
    row = orgs.get_org(org_id)
    if not row:
        raise HTTPException(status_code=404, detail=pick(
            "企业不存在 / no such organization", lang))
    return user, row, lang


# ── shapes ───────────────────────────────────────────────────────────────────

class MemberOut(BaseModel):
    id: int
    email: str
    org_role: str
    status: str
    invited_by: str = ""
    joined_at: Optional[str] = None
    #: Derived at read time, never stored. A company can narrow its suffix list at any
    #: moment and nobody's membership may be revoked as a side effect, so an account
    #: whose address no longer matches is FLAGGED for the administrator rather than
    #: removed.
    domain_ok: bool = True
    account_role: str = ""
    account_disabled: bool = False
    account_deleted: bool = False
    module_overrides: dict = Field(default_factory=dict)


class InviteIn(BaseModel):
    email: str
    org_role: str = "member"


class RoleIn(BaseModel):
    org_role: str


class ScopesIn(BaseModel):
    #: Either a preset name ("internal" / "external" / "global") or the two columns
    #: spelled out. The preset is not a third state: it is WRITTEN AS the two columns,
    #: and reading it back is a pure function of them (`_preset_of`).
    preset: str = ""
    share_scope: str = ""
    public_scope: str = ""


class OrgSettingsIn(BaseModel):
    #: `None` means "leave it alone" for both fields. That is deliberately different from
    #: an empty list, which `orgs.update_org` refuses — see its docstring. A partial form
    #: submission (change the name, forget the suffix box) must not blank the suffix list.
    name: Optional[str] = None
    email_domains: Optional[list] = None


# ── the organization ─────────────────────────────────────────────────────────

@router.get("/me")
async def my_organization(request: Request):
    """The caller's own company, and whether they administer anything at all.

    The Settings page needs this on load, and it is the only endpoint that can answer
    "what am I allowed to administer" without the caller having to ask first.

    ⚠️ Both identities are reported and neither implies the other. A platform operator
    administers the installation from a separate console; an enterprise administrator
    administers their own company from the Settings page. An address can hold both, so
    this must not answer "operator" and stop.
    """
    user = _caller(request)
    email = (user.get("email") or "").strip().lower()
    # ⚠️ The operator check does NOT short-circuit here, and that is the point of the
    # rewrite. This used to answer an operator outright — `is_operator: True,
    # is_org_admin: False, org: None` — without ever looking up a membership, which
    # made the two identities mutually exclusive in the answer even though they are not
    # exclusive in the data: an address can be a platform operator AND own a company.
    #
    # The page this feeds is the ENTERPRISE admin page, and the operator's own console
    # is a separate application on a separate port. So the membership lookup runs for
    # everybody, and both flags are reported: an operator who owns a company gets
    # `is_operator: True` AND their company's `org`/`is_org_admin`, and the client keys
    # the page off `is_org_admin`. An operator with no company still gets the honest
    # `org: None` — they have none — but it is now an answer read from the data rather
    # than an assumption made from the role.
    row = orgs.membership_of(email)
    operator = False
    if is_admin_identity(user):
        # A recognised operator, but NOT an early return. See the note above: the two
        # identities are independent, so the membership lookup below still has to run.
        # The branch only records the flag; it never decides the answer on its own.
        operator = True
    if not row:
        return {"is_operator": operator, "is_org_admin": False, "org": None,
                "org_id": None, "can_invite": operator}
    power = (row.get("org_role") in orgs.ADMIN_ORG_ROLES
             and row.get("status") == orgs.STATUS_ACTIVE
             and row.get("org_status") == orgs.ORG_ACTIVE)
    return {
        "is_operator": operator,
        "is_org_admin": power,
        "can_invite": power,
        "org_id": row["org_id"],
        "org": {"id": row["org_id"], "name": row.get("name"), "slug": row.get("slug"),
                "kind": row.get("kind"), "share_scope": row.get("share_scope"),
                "public_scope": row.get("public_scope"),
                # ⚠️ The preset NAME and the list of names both come from the shared
                # vocabulary, never from the page. A first version of the Settings card
                # carried its own table of the three presets and got one of the three
                # wrong — it paired `internal` with "nothing may be published", while the
                # product's `internal` is "publish one document at a time". The page
                # rendered, and no unit test noticed, because both copies were correct
                # Python and correct JavaScript. The copy is the bug.
                "preset": _preset_of(row),
                "presets": [
                    {"key": key, "share_scope": share, "public_scope": public}
                    for key, (share, public) in orgs.SCOPE_PRESETS.items()
                ],
                "email_domains": list(row.get("email_domains") or []),
                # ⚠️ TWO fields, and they answer different questions. `module_licence`
                # is what the platform operator set for this company in the console and
                # may be `[]`; `module_options` is what that licence actually leaves the
                # administrator able to hand out, after intersecting with the deployment
                # default and dropping everything that is not switchable.
                #
                # The page must render the SECOND, never the first. A page that listed
                # the raw licence would offer a module the deployment has withdrawn —
                # and an administrator who ticked it would get a row the server then
                # ignores, so the box would tick and the module would stay closed. That is
                # worse than no control at all: it is a control that reports success and
                # does nothing.
                # `null` here means "never configured" and `[]` means "configured to
                # nothing" — the same distinction the column makes, passed through
                # unchanged. The page only ever renders `module_options`; carrying the
                # raw licence is for the operator-facing console and for explaining an
                # empty dialog in a support conversation.
                "module_licence": (list(row.get("module_keys"))
                                   if row.get("module_keys") is not None else None),
                "module_options": list(core_modules.org_offer(row.get("module_keys")))},
    }


@router.get("/members", response_model=dict)
async def list_members(request: Request, org_id: Optional[int] = Query(None)):
    """Members of the caller's organization.

    ⚠️ `domain_ok=False` is the one row shape worth explaining. Narrowing an
    organization's suffix list does NOT remove anybody — that would be an irreversible
    consequence of a settings change. The account keeps working, keeps its content, and
    is simply marked so the administrator can decide what to do about it.
    """
    _user, org, _lang = _require_org(request, org_id, "查看")
    rows = orgs.members_of(org["id"])
    return {
        "org_id": org["id"],
        "members": [MemberOut(
            id=r["id"], email=r["email"], org_role=r["org_role"], status=r["status"],
            invited_by=r.get("invited_by") or "",
            joined_at=r["joined_at"].isoformat() if r.get("joined_at") else None,
            domain_ok=bool(r.get("domain_ok", True)),
            account_role=r.get("account_role") or "user",
            account_disabled=bool(r.get("disabled")),
            account_deleted=bool(r.get("deleted_at")),
            # So the page can render the three-state control without a second request
            # per row.
            module_overrides=account_modules.overrides_for(r.get("user_id")),
        ) for r in rows],
    }


@router.post("/invite")
async def invite_member(body: InviteIn, request: Request, org_id: Optional[int] = Query(None)):
    """Invite one address into the organization. Idempotent per address.

    ⚠️ **Any valid address is invitable, including one outside the company's own email
    suffixes.** The suffix list is a REGISTRATION ROUTING rule, not an invitation policy:
    it answers "which company claims this person when they sign themselves up", and that
    is a question about somebody who is *already* typing their own email into the signup
    form. An invitation is the opposite: an administrator has decided, by name, that this
    specific person belongs here.

    That is why the check was removed rather than weakened. It used to refuse any address
    off the suffix list, which made the feature useless in exactly the case it exists for
    — a contractor on their own domain, a partner at another company, an executive who
    reads at a personal address. The administrator was left holding a "invite" button
    that refused the one address they had a reason to type.

    ⚠️ What actually bounds this, and it is not the suffix list:
      * the invitation is a CODE. Nothing happens until the holder registers with it, so
        no access is handed over by the act of inviting;
      * `attach_existing_account` refuses an address that already belongs to another
        company, so one person cannot be collected into two tenants;
      * becoming a member puts them under this company's SHARE and PUBLICATION policy,
        which is the setting that governs what they may reach. The `domain_ok` flag in
        `list_members` marks an invited outsider plainly, so the roster never hides who
        is visiting from outside — the administrator can see it and narrow the policy
        without losing the member.
    """
    user, org, lang = _require_org(request, org_id, "邀请")
    address = orgs.clean_email(body.email)
    if not address:
        raise HTTPException(status_code=400, detail=pick(
            "邮箱地址无效 / that email address is not valid", lang))
    role = _vocab(orgs.ORG_ROLES, body.org_role, request, "企业角色无效 / invalid organization role")

    row = _call(orgs.upsert_invitation, org["id"], address, role, request=request,
                invited_by=(user.get("email") or "").strip().lower())
    # An account that already exists is joined immediately: an invitation is for people
    # who have not registered YET, not a way to re-admit somebody who already has one.
    joined = _call(orgs.attach_existing_account, org["id"], address, request=request)
    out = {"org_id": org["id"], "email": address, "org_role": role,
           "status": row.get("status"), "joined_existing_account": bool(joined),
           "member_id": row.get("id")}
    if joined or auth_store.get_user_by_email(address):
        return out
    # From here on somebody who has no account yet is being invited, and the code is
    # the only way they can finish signing up. It is handed back in the response *and*
    # emailed, so an unconfigured mail server still produces an invitation.
    ttl = int(settings.AUTH_INVITE_TTL_SECONDS or 1209600)
    try:
        code = auth_store.create_code(address, ttl_seconds=ttl)
    except Exception as exc:                       # noqa: BLE001 — surfaced as 502
        log.warning("org invite: could not issue a code for %s", address, exc_info=True)
        raise HTTPException(status_code=502, detail=pick(
            f"邀请码生成失败：{exc} / could not create the invitation code: {exc}",
            lang)) from exc
    status, note = _mail_invite(address, code, max(1, ttl // 86400),
                                (user.get("display_name") or "").strip()
                                or (user.get("email") or ""),
                                _asset_base_url(request))
    out["code"] = code
    out["expires_in"] = ttl
    out["mail"] = status
    if status != "sent":
        # print(), not log — core/logring.py swallows app-level output (see the note in
        # routers/auth.py::register_start). The code is in the response, so this is a
        # "hand it over by hand" situation, not a lost invitation.
        print(f"WARNING: the org invitation for {address} was not emailed ({status}: "
              f"{note}); the code is in this response", flush=True)
    return out


def _asset_base_url(request: Request) -> str:
    """Absolute base for the links inside an email, from the request that was sent.

    Empty when the request carries no host — the templates then omit the links instead
    of embedding an address that belongs to somebody else's installation. Mirrors
    `routers/auth.py`; the two are duplicated on purpose because a shared helper here
    would put a main-app import back into the console's dependency graph.
    """
    host = (request.headers.get("x-forwarded-host")
            or request.headers.get("host") or "").split(",")[0].strip()
    if not host:
        return ""
    proto = (request.headers.get("x-forwarded-proto")
             or request.url.scheme or "https").split(",")[0].strip()
    return f"{proto}://{host}{(settings.APP_BASE_PATH or '').rstrip('/')}"


def _mail_invite(address: str, code: str, ttl_days: int, invited_by: str,
                 asset_base: str) -> tuple[str, str]:
    """Best-effort delivery. Returns `(status, reason)`; status is never "sent" on a lie.

    ⚠️ A missing mail server is NOT a failure of this endpoint — the same rule the
    existing invitations and agent codes follow, so an unconfigured deployment still
    hands out a usable code instead of dead-ending. It stays readable from the members
    list and from this response.
    """
    try:
        from services import mailer
        return mailer.send_invite(address, code, ttl_days, asset_base + "/", invited_by,
                                  asset_base=asset_base), ""
    except Exception as exc:                           # noqa: BLE001 — never fatal here
        log.warning("org invite: could not email %s", address, exc_info=True)
        return "error", str(exc)


@router.put("/members/{member_id}/role")
async def set_member_role(member_id: int, body: RoleIn, request: Request,
                          org_id: Optional[int] = Query(None)):
    user, org_id_resolved, _lang = _resolve(request, org_id)
    role = _vocab(orgs.ORG_ROLES, body.org_role, request, "企业角色无效 / invalid organization role")
    row = _call(orgs.set_member_role, member_id, role, request=request,
                expected_org_id=org_id_resolved)
    return {"member": row}


@router.delete("/members/{member_id}")
async def remove_member(member_id: int, request: Request, org_id: Optional[int] = Query(None)):
    """Remove a member from the organization. The ACCOUNT is untouched.

    ⚠️ This is not a close. The account keeps every document it owns; it simply stops
    being in this company, which is what "remove from the company" means. Closing an
    account is `account_lifecycle.soft_delete()` — a separate, deliberate act with a
    30-day promise attached. The `account_closed: false` in the response exists so a
    caller reading the response cannot mistake one for the other.

    A disabled row is KEPT rather than deleted: the address leaves the roster (so it is
    no longer offered as a share target) while leaving a record of why.
    """
    _user, org_id_resolved, _lang = _resolve(request, org_id)
    row = _call(orgs.disable_member, member_id, request=request,
                expected_org_id=org_id_resolved)
    return {"member": row, "account_closed": False}


@router.put("/scopes")
async def set_scopes(body: ScopesIn, request: Request, org_id: Optional[int] = Query(None)):
    """Set the company's sharing and publication policy.

    ⚠️ Tightening does NOT retroactively revoke anything already shared. Every grant is
    an explicit row, and silently withdrawing working links because somebody changed a
    setting would be the more surprising behaviour. That is a product decision and it is
    written here rather than left implicit in the code.
    """
    _user, org, lang = _require_org(request, org_id, "配置")
    if body.preset:
        if body.preset not in orgs.SCOPE_PRESETS:
            raise HTTPException(status_code=400, detail=pick(
                f"未知的预设（可选：{'、'.join(orgs.SCOPE_PRESETS)}） / unknown preset; "
                f"choose one of: {', '.join(orgs.SCOPE_PRESETS)}", lang))
        share, public = orgs.SCOPE_PRESETS[body.preset]
    else:
        share = _vocab(orgs.SHARE_SCOPES, body.share_scope or org["share_scope"],
                       request, "分享范围无效 / invalid share scope")
        public = _vocab(orgs.PUBLIC_SCOPES, body.public_scope or org["public_scope"],
                        request, "公开范围无效 / invalid public scope")
    row = _call(orgs.set_scopes, org["id"], share, public, request=request)
    return {"org": row, "preset": _preset_of(row)}


def _preset_of(org_row: dict) -> str:
    """The preset name for a pair, or `""` for a combination that is not one."""
    for name, pair in orgs.SCOPE_PRESETS.items():
        if pair == (org_row.get("share_scope"), org_row.get("public_scope")):
            return name
    return ""


@router.put("/settings")
async def set_org_settings(body: OrgSettingsIn, request: Request,
                           org_id: Optional[int] = Query(None)):
    """The company's own name and the email suffixes that identify its staff.

    ⚠️ Two fields, and they are not equally dangerous. Narrowing the suffix list only
    FLAGS members whose address no longer matches — `list_members` computes `domain_ok`
    at read time and nobody is removed as a side effect. Clearing it is refused outright
    by `orgs.update_org`, because with no suffix the company can neither invite nor
    match a new signup, which reads as a mistake and locks the administrator out of the
    very page they are editing.

    ⚠️ `slug` is not here and cannot be added: it is the permanent public handle, already
    baked into every shared link and document id. A company that outgrows its name gets a
    new organization.
    """
    _user, org, _lang = _require_org(request, org_id, "配置")
    row = _call(orgs.update_org, org["id"], name=body.name,
                email_domains=body.email_domains, request=request)
    return {"org": row, "preset": _preset_of(row)}


# ── member module entitlements ───────────────────────────────────────────────

@router.put("/members/{member_id}/modules/{module_key}")
async def set_member_module(member_id: int, module_key: str, body: dict, request: Request,
                            org_id: Optional[int] = Query(None)):
    """Switch one module on or off for one member. **Two states, and only two.**

    ⚠️ `enabled: null` (follow the deployment default) is still accepted, and the page no
    longer offers it. It is kept because it is the honest way to *un-say* something: an
    administrator who ticked a box by mistake needs a way to take the row back out, and
    `resolve()` reads an absent key as "no opinion" — which is a different state from
    `false` and is worth being able to return to. What changed is the UI, not the grammar.

    ⚠️ Two ceilings, and the second is the one this rewrite added. The per-account row can
    only ever REMOVE access, because `core.modules.resolve()` intersects it with the
    deployment allowlist. But an *enterprise* now has its own ceiling above that: the
    platform operator licences each company a set of modules in the console
    (`orgs.module_keys`), and an administrator may not write a row for a module outside
    it. Without that check the ceiling would be decorative in the only place it matters —
    the write — and the dialog would be the only thing enforcing a policy the API does not.

    The refusal is a 400, not a 404: the module exists, the caller simply may not hand it
    out, and saying "no such module" would send them looking for a typo.
    """
    _user, org_id_resolved, lang = _resolve(request, org_id)
    module = core_modules.BY_KEY.get(module_key)
    if module is None:
        raise HTTPException(status_code=404, detail=pick(
            f"没有名为 {module_key} 的模块 / no such module: {module_key}", lang))
    org_row = orgs.get_org(org_id_resolved) if org_id_resolved else None
    offered = core_modules.org_offer((org_row or {}).get("module_keys"))
    if module_key not in offered:
        raise HTTPException(status_code=400, detail=pick(
            f"本企业没有「{module.label_zh} / {module.label_en}」的模块权限，请在管理后台里"
            f"由平台管理员为该企业开放 / this organization is not licensed for the "
            f"{module.label_en} module; ask a platform administrator to enable it in the "
            f"admin console", lang))
    enabled = body.get("enabled")
    if enabled is None:
        row = _call(orgs.clear_member_module, member_id, module_key, request=request,
                    expected_org_id=org_id_resolved)
    else:
        row = _call(orgs.set_member_module, member_id, module_key, bool(enabled),
                    request=request, expected_org_id=org_id_resolved)
    return {"member": row, "options": list(offered)}


@router.delete("/members/{member_id}/modules")
async def reset_member_modules(member_id: int, request: Request,
                               org_id: Optional[int] = Query(None)):
    """Send one member back to following the deployment default on every module.

    ⚠️ `DELETE` on the COLLECTION, not on one module: this is the "恢复默认" button on a
    matrix row, and it has to be one call that drops all of that member's overrides.
    Looping in the browser would be a partial reset if the tab closed halfway.

    A member who has never been narrowed has nothing to clear, and that is a success
    (200), not a 404 — the state the operator asked for is the state they got.
    """
    _user, org_id_resolved, _lang = _resolve(request, org_id)
    cleared = _call(orgs.clear_member_modules, member_id, request=request,
                    expected_org_id=org_id_resolved)
    return {"member_id": member_id, "cleared": cleared, "module_overrides": {}}


# ── publication approvals ────────────────────────────────────────────────────

@router.get("/approvals")
async def list_approvals(request: Request, org_id: Optional[int] = Query(None),
                         status: str = Query("pending")):
    """Publication requests waiting on a decision.

    An operator with no `org_id` sees every organization's queue — that is the
    installation-wide view, and it is the one thing here that is NOT narrowed.
    """
    _user, org_id_resolved, _lang = _resolve(request, org_id)
    return {"approvals": orgs.list_approvals(org_id=org_id_resolved, status=status)}


@router.post("/approvals/{approval_id}/{decision}")
async def decide_approval(approval_id: int, decision: str, request: Request,
                          org_id: Optional[int] = Query(None)):
    """Approve / deny / revoke one publication request.

    ⚠️ `POST …/{decision}` rather than three routes, so an unknown verb is a 400 from
    the shared vocabulary check — "you typed the wrong word" — instead of a 404 reading
    as "no such approval request", which is the same answer the genuinely-missing case
    gives for a completely different mistake.
    """
    user, org_id_resolved, _lang = _resolve(request, org_id)
    actor = (user.get("email") or "").strip().lower()
    row = _call(orgs.decide_public_approval, approval_id, decision, actor,
                request=request, expected_org_id=org_id_resolved)
    return {"approval": row}


@router.post("/public-requests")
async def request_publication(request: Request, org_id: Optional[int] = Query(None)):
    """Open (or re-open) a publication request for one document.

    ⚠️ This is what a member hits when the gate refuses with `needs_approval`, and it is
    the only way out for them: without it the product says "ask your administrator" and
    offers no way to ask, which is a dead end dressed as a policy.

    `_resolve_member`, **not** `_resolve` — see its docstring. An administrator gate here
    makes the sentence in the refusal message above untrue for exactly the reader it was
    written for.
    """
    user, org_id_resolved, lang = _resolve_member(request, org_id)
    if org_id_resolved is None:
        raise HTTPException(status_code=400, detail=pick(
            "超管请指定企业 / name the organization", lang))
    payload = await request.json()
    target_type = str(payload.get("target_type") or "")
    target_id = str(payload.get("target_id") or "").strip()
    channel = str(payload.get("kind") or "")
    if not target_id:
        raise HTTPException(status_code=400, detail=pick(
            "请指定要公开的文档 / say which document", lang))
    row = _call(orgs.request_public_approval,
                (user.get("email") or "").strip().lower(), target_type, target_id,
                channel, request=request)
    return {"approval": row, "org_id": org_id_resolved}
