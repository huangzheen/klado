"""Organizations, as the operator sees them: every company, and every document that ever
asked to leave one.

Two things in here are worth stating before the code.

**The console can do things an enterprise administrator cannot.** Suspending a company,
changing its share scope, and deciding its publication approvals are all operator acts.
That is the design (a company's own admins manage its members; the operator manages the
company), and it is why every route here goes through `require_write` and none of them
appears in the main app.

⚠️ **Tightening never revokes.** Changing `public_scope` from `approve` to `forbid` stops
the *next* publication. It does not withdraw an approval somebody already granted, and it
does not un-publish a document that is already public. Every grant is an explicit row and
this module has no code path that walks them. That asymmetry is deliberate: silently
revoking a link a colleague already sent to a client is worse than the leak it prevents,
and the operator who needs it undone has an explicit "revoke" button on the approval row.

**404 versus 403 does not arise here** — the operator sees every organization, so there is
is nothing to conceal. It is the opposite rule from `/api/org/*`, and mixing the two
mental models in one process is exactly the confusion the two prefixes exist to prevent.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from admin_console import access
from klado_shared import modules, orgs
from klado_shared.i18n import pick, request_lang

router = APIRouter(prefix="/api/admin-console/orgs", tags=["企业"])


def _translate(exc: orgs.OrgError, request: Request) -> HTTPException:
    """Service refusals → HTTP. 404 for "belongs to another company", 400 for a rule the
    caller broke. Identical to the main app's `_org_error`, and the reason it is duplicated
    rather than imported: importing it would mean reaching into `api/routers/org_admin.py`,
    which is the coupling `api-admin/` must not have. The two are kept in step by
    `api/tests/test_shared_layer.py`'s prefix-separation scan, which fails if either grows
    an `api.*` import."""
    status = 404 if isinstance(exc, orgs.MemberScopeError) else 400
    return HTTPException(status_code=status, detail=pick(str(exc), request_lang(request)))


def _call(fn, request: Request, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except orgs.OrgError as exc:
        raise _translate(exc, request) from exc


# ── reads ────────────────────────────────────────────────────────────────────

@router.get("")
async def list_all(request: Request, kind: str = Query(""),
                    q: str = Query(""), limit: int = Query(200, ge=1, le=1000)):
    access.require_operator(request)
    return {"orgs": _call(orgs.list_orgs, request, kind or None, q, limit)}


@router.get("/{org_id}")
async def detail(org_id: int, request: Request):
    access.require_operator(request)
    org = _call(orgs.get_org, request, org_id)
    if not org:
        raise HTTPException(status_code=404, detail=pick(
            "企业不存在 / no such organization", request_lang(request)))
    members = _call(orgs.members_of, request, org_id)
    # ⚠️ The drawer shows THREE module facts and they are not interchangeable, so all
    # three travel together: `module_licence` is what the operator ticked,
    # `module_offer` is what that leaves the company able to hand out after intersecting
    # with the deployment default, and `deployment_default` is the ceiling above that.
    # Rendering the first while the operator is looking at the second is how a company
    # ends up licensed for a module the installation has switched off — a box that ticks,
    # saves, and then offers nothing.
    return {
        "org": org,
        "members": members,
        "admins": _call(orgs.list_admins, request, org_id),
        # `None` (never configured) and `[]` (configured to nothing) are different, and
        # the drawer has to be able to tell them: the second is a licence to offer no
        # switchable module at all, and rendering it as "unconfigured" would show the
        # operator a full set of switches that saves to nothing.
        "module_licence": (list(org.get("module_keys"))
                           if org.get("module_keys") is not None else None),
        "module_offer": list(modules.org_offer(org.get("module_keys"))),
        "deployment_default": list(modules.deployment_default()),
        "catalogue": modules.catalogue(),
    }


@router.get("/{org_id}/approvals")
async def approvals(org_id: int, request: Request, status: str = Query("pending")):
    """Every publication request this company has made — the "what ever left the building"
    ledger. Readable with any status, including `approved`, `denied` and `revoked`."""
    access.require_operator(request)
    if not _call(orgs.get_org, request, org_id):
        raise HTTPException(status_code=404, detail=pick(
            "企业不存在 / no such organization", request_lang(request)))
    return {"approvals": _call(orgs.list_approvals, request, org_id, status)}


# ── writes ───────────────────────────────────────────────────────────────────

class OrgPatch(BaseModel):
    name: Optional[str] = None
    email_domains: Optional[list] = None
    share_scope: Optional[str] = None
    public_scope: Optional[str] = None
    status: Optional[str] = None


@router.patch("/{org_id}")
async def patch_org(org_id: int, body: OrgPatch, request: Request):
    """Change the company. Every field optional; omitted means "leave it".

    ⚠️ There is no `slug` field, and that is not an oversight — see
    `orgs.update_org()`'s docstring. The slug is in every shared link that already
    exists, and a company that outgrows its name gets a new organization rather than a
    redirect nobody maintains.
    """
    actor = access.require_write(request)
    body = body.model_dump(exclude_unset=True, exclude_none=True)
    if not body:
        raise HTTPException(status_code=400, detail=pick(
            "没有要修改的字段 / nothing to change", request_lang(request)))
    org = _call(orgs.update_org, request, org_id, **body)
    _log(actor, "org.update", f"{org_id} {sorted(body)}")
    return {"org": org}


class OrgModulesIn(BaseModel):
    """The modules this company may hand out to its members.

    ⚠️ A dedicated route, not a field on `OrgPatch`. `[]` is a real value here and not
    "unset" — it is a company licensed for no switchable module, and it is the only way
    an operator can say that with a checkbox — so a PATCH could not tell "the operator
    cleared every box" from "the caller did not mention it", and a partial save of the
    company form would have wiped the licence. The validator lives in
    `orgs._clean_module_keys`, so the console and the main app cannot disagree about what
    a legal key is.
    """
    keys: list = []


@router.put("/{org_id}/modules")
async def set_org_modules(org_id: int, body: OrgModulesIn, request: Request):
    actor = access.require_write(request)
    org = _call(orgs.set_org_modules, request, org_id, body.keys)
    _log(actor, "org.modules", f"{org_id} {sorted(org.get('module_keys') or [])}")
    return {"org": org,
            "module_licence": list(org.get("module_keys") or []),
            "module_offer": list(modules.org_offer(org.get("module_keys")))}


class PresetIn(BaseModel):
    """One of the three one-click presets.

    ⚠️ A preset is NOT a fourth stored state. Each name maps to a pair of columns, and the
    table comes from `orgs.SCOPE_PRESETS` rather than being written out here — the presets
    are product vocabulary, and a second copy in a router is a list that quietly stops
    matching the one the gate validates against. (An earlier version of this file did
    exactly that, and invented two names that existed nowhere else.)
    """
    preset: str


@router.put("/{org_id}/preset")
async def set_preset(org_id: int, body: PresetIn, request: Request):
    actor = access.require_write(request)
    pair = orgs.SCOPE_PRESETS.get((body.preset or "").strip().lower())
    if not pair:
        raise HTTPException(status_code=400, detail=pick(
            f"未知的预设（{'、'.join(orgs.SCOPE_PRESETS)}）/ unknown preset; expected one "
            f"of {', '.join(orgs.SCOPE_PRESETS)}", request_lang(request)))
    share, public = pair
    org = _call(orgs.set_scopes, request, org_id, share, public)
    _log(actor, "org.preset", f"{org_id} {body.preset}")
    return {"org": org, "applied": {"share_scope": share, "public_scope": public}}


class TransferIn(BaseModel):
    email: str


@router.post("/{org_id}/transfer")
async def transfer(org_id: int, body: TransferIn, request: Request):
    """Hand the company to an existing ACTIVE member, in one transaction.

    ⚠️ This is the console's escape hatch for a company whose owner left the business. The
    enterprise admin cannot use it — the person who needs to replace a departed owner is
    usually not the owner, and an operator is the only party who can act outside the
    company's own hierarchy.
    """
    actor = access.require_write(request)
    member = _call(orgs.transfer_org_owner, request, org_id, body.email)
    _log(actor, "org.transfer", f"{org_id} -> {body.email}")
    return {"member": member}


class DecisionIn(BaseModel):
    decision: str = Field(description="approved | denied | revoked")


@router.post("/approvals/{approval_id}/{decision}")
async def decide(approval_id: int, decision: str, request: Request,
                 body: Optional[DecisionIn] = None):
    """Decide on behalf of the whole installation — the "代批" from the design.

    ⚠️ `expected_org_id` is left None on purpose, and that is what makes this an operator
    power rather than a bug: with it set, the UPDATE is scoped to one company; without it,
    any pending request anywhere may be decided. The main app's `/api/org/approvals/...`
    is the same function with the scope supplied, and the difference between the two call
    sites is the entire difference between the two roles.
    """
    actor = access.require_write(request)
    # ⚠️ Checked against the SHARED vocabulary, not a copy of it. This used to hold its
    # own tuple of the three past-tense words, so the console accepted `…/approved` and
    # refused `…/approve` while the Settings card — which calls the same function on the
    # main app — builds the imperative form. Two lists, one meaning.
    if str(decision or "").strip().lower() not in orgs.DECISIONS:
        raise HTTPException(status_code=400, detail=pick(
            "未知的审批结果 / unknown approval decision", request_lang(request)))
    row = _call(orgs.decide_public_approval, request, approval_id, decision,
                actor.get("email") or "", None)
    _log(actor, "approval.decide", f"{approval_id} {row.get('status')}")
    return {"approval": row}


def _log(actor: dict, kind: str, detail: str = "") -> None:
    """Write to the same access log the main app writes to.

    ⚠️ Best-effort by design. A console action that has already been committed must not be
    reported as a failure because the audit insert hit a problem; the alternative — rolling
    back a policy change because the log was full — is the wrong trade. The gap shows up as
    a missing line in the audit view, which is a far smaller problem than a policy change
    that silently did not happen.
    """
    from klado_shared import accounts
    try:
        accounts.log_access(actor, kind, "CONSOLE", f"/api/admin-console/{kind}", detail)
    except Exception:  # noqa: BLE001 — never fail a committed action on its audit trail
        import logging
        logging.getLogger(__name__).warning("could not write the console audit line: %s",
                                            kind, exc_info=True)
