"""The module registry — one place that says what exists and who may use it.

Klado started as one application with one set of pages. It is being built towards
being *assembled* out of modules that can be sold, enabled or withheld
independently, so the question "may this account use the dashboard?" has to have
exactly one answer, written down once.

This file is that answer. Everything else asks it:

* the auth middleware refuses a request whose module the caller does not have
  (`main.py::enforce_modules`);
* `/api/auth/me` hands the SPA the caller's entitlements, and the SPA renders
  only the tabs and page containers for them;
* the Settings page reads the registry to build its module picker — so a new
  module appears there without touching that page.

Three rules keep this from rotting:

1. **A module declares its own paths.** No other file maintains a list of what
   belongs to it, so a route cannot quietly escape its module's gate.
2. **Dependencies are one-directional and explicit.** `dashboard` needs
   `datacenter` (it reads datasets); nothing needs `dashboard`. The closure is
   computed here, so enabling a module can never leave it calling an endpoint
   the caller cannot reach.
3. **Infrastructure is required, product is optional.** `inbox` and `settings`
   are how the other modules report and are administered, so they are always on.
   The five product modules are what a deployment or an account turns on.

Resolution order for one account: the deployment's allowlist
(`KLADO_MODULES`; unset means "everything, which is why an existing install
keeps working with no configuration), then that account's row in
`account_modules` if it has one, then the dependency closure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

from klado_shared.config import settings

_LOG = logging.getLogger(__name__)

# Key used by the dependency graph and the API contract. Never localise this.
DATACENTER = "datacenter"
INBOX = "inbox"
WORKSPACE = "workspace"
DASHBOARD = "dashboard"
KNOWLEDGE = "knowledge"
CALENDAR = "calendar"
SETTINGS = "settings"
# Not a product module: identity, health, and the object/annotation services
# every other module needs. Always on, never in the nav, never sold.
CORE = "core"


@dataclass(frozen=True)
class Module:
    """One independently-openable area of the product.

    `api_prefixes` and `doc_prefixes` are what the middleware matches a request
    path against. They are here rather than in `main.py` so that adding a module
    cannot produce a route that is not gated: if it is not declared, it is not
    reachable by a module, and `main.py` says so out loud.
    """

    key: str
    label_zh: str
    label_en: str
    order: int
    # Always on, whatever any allowlist says.
    required: bool = False
    # Other modules that must be on for this one to work. Closed transitively.
    requires: tuple[str, ...] = ()
    api_prefixes: tuple[str, ...] = ()
    # Standalone, chrome-free document pages the module serves.
    doc_prefixes: tuple[str, ...] = ()
    # SPA page container id, so the shell can hide a module by removing it.
    page_ids: tuple[str, ...] = ()
    # One-line description for the Settings module picker.
    blurb_zh: str = ""
    blurb_en: str = ""
    # A module an account may switch on is offered in the picker; required ones
    # are not (there is nothing to choose).
    toggleable: bool = True
    # False for infrastructure: it has endpoints but no tab and no page.
    in_nav: bool = True
    # ⚠️ True for a module that belongs in the LEFT RAIL. This is NOT the same as
    # `in_nav = False`: that one means "no page at all", and its page container is
    # torn down with it. A rail module keeps its page and is usually in the top bar
    # too — the rail is a SECOND way in, not a replacement.
    #
    # Deliberately a separate field rather than overloading `in_nav`: the two are
    # independent (in both bars / top bar only / rail only / neither) and collapsing
    # them into one flag is how "the Dashboard page stopped existing" happens.
    in_rail: bool = False
    # ⚠️ There is deliberately NO third "rail only" flag. It existed while Dashboard
    # was kept out of the top bar, and it was the wrong model twice over: the user
    # ruled that the nav must be on EVERY page, so a module the nav may hide is a
    # contradiction; and a flag that is `False` for every module is a trap, not an
    # option. Two booleans say "both / either / neither" on their own.
    # Icon set shown on the module's top-bar tab. Always a Remix class, so a module
    # added later needs no new image asset.
    icon: str = "ri-file-chart-line"
    # Optional `<img>` mark that replaces `icon` on the tab, for the modules that
    # already ship a drawn mark. Empty means "use the icon".
    nav_mark: str = ""
    extras: dict = field(default_factory=dict)


# Declared in navigation order. `order` is the sort key; the SPA does not assume
# the declaration order matches, because an account's enabled set is a subset.
MODULES: tuple[Module, ...] = (
    Module(
        key=CORE, label_zh="基础", label_en="Core", order=0,
        required=True, toggleable=False, in_nav=False,
        # ⚠️ `/api/org` is here, and it is here because of what its ABSENCE did: the
        # middleware answers 403 for a path no module claims, so an unclaimed
        # `/api/org/*` made the entire enterprise-administration API unreachable in the
        # running app while every unit test passed — the suites mount the router on a
        # bare FastAPI app, without the middleware that would have refused it.
        # `core` is the right owner: identity and organization, no tab, and `required`,
        # so it can never be switched off and strand the company settings. The
        # authorization is NOT this module's job — `routers/org_admin.py::_resolve()`
        # derives the caller's company from their own membership row.
        #
        # ⚠️ `/api/rail` joined the list for exactly the same reason. The left rail is
        # chrome that spans every module (its folders file documents, dashboards AND
        # knowledge pages), so it belongs to no single feature — and a prefix no module
        # claims is answered 403 by the middleware before the handler ever runs. The
        # symptom of getting this wrong is a rail whose every endpoint is 403 in the
        # running app and perfectly fine in the unit tests, because the suites mount
        # routers on a bare FastAPI app without that middleware.
        api_prefixes=("/api/auth", "/api/health", "/api/storage", "/api/annotations",
                      "/api/org", "/api/rail"),
        blurb_zh="登录、健康检查、文件与文档批注、企业管理。",
        blurb_en="Sign-in, health, object storage, document notes, and organization "
                 "administration.",
        icon="ri-cog-line",
    ),
    Module(
        key=DATACENTER, label_zh="数据中心", label_en="Data Center", order=10,
        required=True, toggleable=False,
        api_prefixes=("/api/data-center",),
        page_ids=("datacenter-page",),
        in_rail=True,
        blurb_zh="上传文件、按需清洗，生成你自己的数据集。",
        blurb_en="Upload files, clean them, and build the datasets you work from.",
        icon="ri-database-2-line", nav_mark="datacenter-icon.png",
    ),
    Module(
        key=INBOX, label_zh="收件箱", label_en="Inbox", order=20,
        required=True, toggleable=False,
        api_prefixes=("/api/inbox",),
        page_ids=("page-inbox",),
        blurb_zh="同事的分享、文档批注与数据更新。",
        blurb_en="Colleague shares, document notes, and data updates.",
        icon="ri-inbox-line", nav_mark="inbox-icon.png",
    ),
    Module(
        key=WORKSPACE, label_zh="工作台", label_en="Workspace", order=30,
        api_prefixes=("/api/reports", "/api/export"),
        doc_prefixes=("/r/",),
        page_ids=("page-reports",),
        in_rail=True,
        blurb_zh="报告与文档：agent 写 HTML，你审阅、发布、分享。",
        blurb_en="Reports and documents: your agent writes the HTML, you review, publish, share.",
        icon="ri-folder-line", nav_mark="workspace-icon.png",
    ),
    Module(
        key=DASHBOARD, label_zh="仪表盘", label_en="Dashboard", order=40,
        requires=(DATACENTER,),
        api_prefixes=("/api/dashboard",),
        doc_prefixes=("/d/",),
        page_ids=("page-dashboard",),
        # ⚠️ In BOTH bars: `in_rail` for the left rail, `in_nav` (the default) for the
        # top bar. It was `rail_only` for a while, on the reasoning that seven module
        # tabs are already a lot — and the user read the missing tab as the module
        # having been deleted. The rail is a second way in; it never replaces the top
        # bar, which is the one piece of chrome that is on every page.
        in_rail=True,
        blurb_zh="可调用数据集的动态页面，可放进左侧栏或首页容器。",
        blurb_en="Live pages that query datasets, fileable in the left rail or onto the home page.",
        icon="ri-dashboard-line", nav_mark="dashboard-icon.png",
    ),
    Module(
        key=KNOWLEDGE, label_zh="知识库", label_en="Knowledge base", order=50,
        api_prefixes=("/api/knowledge", "/api/ai/knowledge"),
        page_ids=("page-knowledge",),
        in_rail=True,
        blurb_zh="项目说明书与业务 wiki，可由 agent 写入。",
        blurb_en="The project manual and the business wiki, both writable by your agent.",
        icon="ri-book-open-line", nav_mark="knowledge-icon.png",
    ),
    Module(
        key=CALENDAR, label_zh="日历", label_en="Calendar", order=60,
        api_prefixes=("/api/calendar",),
        doc_prefixes=("/e/",),
        page_ids=("page-calendar",),
        in_rail=True,
        blurb_zh="agent 建的日程，带一页 16:9 详情页。",
        blurb_en="Events your agent creates, each with a 16:9 detail page.",
        icon="ri-calendar-line", nav_mark="calendar-icon.png",
    ),
    Module(
        key=SETTINGS, label_zh="设置", label_en="Settings", order=90,
        required=True, toggleable=False,
        api_prefixes=("/api/settings",),
        page_ids=("page-system-settings",),
        blurb_zh="账号、授权码、邮件投递与模块开关。",
        blurb_en="Accounts, access codes, mail delivery, and module switches.",
        icon="ri-settings-3-line",
    ),
)

BY_KEY: dict[str, Module] = {m.key: m for m in MODULES}
# Declaration order is the fallback order, so a caller that does not care about
# sorting still gets a stable, sensible sequence.
ALL_KEYS: tuple[str, ...] = tuple(m.key for m in MODULES)


class ModuleConfigError(RuntimeError):
    """`KLADO_MODULES` named a module that does not exist."""


#: `app_settings` key the admin console writes. See `deployment.py` for why these two
#: settings are in a table while everything else in this project lives in `.env`.
DEPLOYMENT_DEFAULT_KEY = "modules.deployment_default"


def _table_keys() -> tuple[str, ...] | None:
    """The allowlist as last saved in the console, or None when nothing has been saved.

    ⚠️ This is what makes the console's module page real rather than decorative. The
    allowlist used to come only from `KLADO_MODULES`, so a page that let an operator edit
    it would have changed nothing the app enforced — a settings screen that lies, in the
    one place where the operator is deciding what their staff can reach.

    Unknown keys are **dropped with a warning**, not raised, and the difference from the
    environment path below is deliberate. `KLADO_MODULES` is read at startup, so a typo
    there can be reported as a startup error and stop the deployment before it serves
    anything. A table value can be edited while the app is running, and raising would mean
    one bad row turns a policy mistake into an outage on a request path. Failing *closed*
    — ignoring the key — is the safe direction: the module stays off, exactly as the
    operator's other keys imply.
    """
    try:
        from klado_shared import deployment
        stored = deployment.read(DEPLOYMENT_DEFAULT_KEY, None)
    except Exception:  # noqa: BLE001 — never take the app down over a settings read
        _LOG.warning("module allowlist unreadable; falling back to KLADO_MODULES",
                     exc_info=True)
        return None
    if isinstance(stored, dict):
        stored = stored.get("keys")
    if not isinstance(stored, list):
        return None
    keys, unknown = [], []
    for item in stored:
        key = str(item or "").strip().lower()
        if not key:
            continue
        if key in BY_KEY:
            if key not in keys:
                keys.append(key)
        else:
            unknown.append(key)
    if unknown:
        _LOG.warning("modules.deployment_default names unknown module(s) %s; ignoring "
                     "them. known modules are %s", ", ".join(unknown), ", ".join(ALL_KEYS))
    return tuple(keys)


def _configured_keys() -> tuple[str, ...] | None:
    """The deployment's allowlist, or None when unset (= every module).

    ⚠️ Two sources, and the precedence is the whole contract: **the table wins**. The
    console is the live control surface; `KLADO_MODULES` seeds a fresh install and stays
    meaningful only until somebody saves from the console once. The reverse order would
    mean the console could never change anything on a deployment that sets the variable —
    which is most of them.

    The one case where the table does NOT win is an empty save. An operator who unchecks
    every module has said something, and "every module" is a different statement, so an
    empty list is honoured as empty (and `close_over_requires` puts back the modules marked
    `required`, which is why the product still has an inbox afterwards).
    """
    from_table = _table_keys()
    if from_table is not None:
        return from_table
    raw = (getattr(settings, "KLADO_MODULES", "") or "").strip()
    if not raw:
        return None
    keys = [k.strip().lower() for k in raw.split(",") if k.strip()]
    unknown = [k for k in keys if k not in BY_KEY]
    if unknown:
        # A typo must not silently disable a module someone is relying on.
        raise ModuleConfigError(
            f"KLADO_MODULES names unknown module(s): {', '.join(unknown)}; "
            f"known modules are {', '.join(ALL_KEYS)}")
    return tuple(dict.fromkeys(keys))


def deployment_default() -> tuple[str, ...]:
    """Modules every account gets before any per-account override.

    Unset config means all of them, which is what an existing install expects
    and what keeps a fresh checkout working with no `.env` change.
    """
    keys = _configured_keys()
    if keys is None:
        return ALL_KEYS
    return close_over_requires(keys)


def close_over_requires(keys: Iterable[str]) -> tuple[str, ...]:
    """`keys` plus everything they transitively require, in registry order.

    Required modules are added too: turning a product module on cannot take
    `inbox` away, because the module would then have no way to tell the account
    that anything happened.
    """
    wanted = set(keys)
    pending = list(wanted)
    while pending:
        key = pending.pop()
        module = BY_KEY.get(key)
        if module is None:
            continue
        for dep in module.requires:
            if dep not in wanted:
                wanted.add(dep)
                pending.append(dep)
    for module in MODULES:
        if module.required:
            wanted.add(module.key)
    return tuple(k for k in ALL_KEYS if k in wanted)


def org_offer(licence: Iterable[str] | None) -> tuple[str, ...]:
    """The switchable modules one COMPANY is licensed to hand out to its members.

    `licence` is `orgs.module_keys` — the platform operator's per-company list, written in
    the admin console. `None` means the operator has expressed no opinion about this
    company and the deployment default stands, which is what an existing install expects
    (same rule, and for the same reason, as `_configured_keys()`: a fresh deployment must
    keep working before anybody has opened the console).

    ⚠️ `None` and an EMPTY licence are different, and the check is `is not None` for
    exactly that reason. An operator who unticked every box has said "this company offers
    nothing", and `if licence:` would read that as "no restriction" — handing the
    administrators a full set of checkboxes whose every save is refused. That is the
    failure a truthiness check on a licence always produces, and it is why the column is
    nullable rather than defaulting to `'{}'`.

    ⚠️ Three facts are folded together here, in this order, and the order is the contract:
    the **deployment** decides what exists, the **licence** may only remove, and the
    **registry** decides which of the survivors are switchable at all. An operator can
    therefore never widen a company by accident, and required infrastructure
    (`core`, `inbox`, `settings`, `datacenter`) always survives — closing those would
    leave a company with no way to be told what happened to it.
    """
    ceiling = set(deployment_default())
    if licence is not None:
        ceiling &= {k for k in licence if k in BY_KEY}
    wanted = {k for k in ceiling if BY_KEY[k].toggleable}
    # Dependency closure first, then re-filter: `close_over_requires` puts back every
    # `required` module, and those are by definition not switchable, so letting them
    # through would hand the dialog a row it cannot offer.
    return tuple(k for k in close_over_requires(wanted) if BY_KEY[k].toggleable)


def resolve(user_overrides: dict[str, bool] | None = None,
            licence: Iterable[str] | None = None) -> tuple[str, ...]:
    """The modules one account may use.

    `user_overrides` is `{module_key: enabled}` from `account_modules`. A key that
    is absent falls back to the deployment default, so an account only has to
    carry a row for the module it differs on.

    Overrides can only ever **narrow**. A row that asks for a module the
    deployment does not offer is ignored, not honoured: otherwise switching a
    module off for an edition would be undone by a stale entitlement, and the
    switch in `.env` would be a claim the server does not honour.

    `licence` is the same narrowing one level up — the company the account belongs
    to, and `None` for "no company-specific restriction". It is a keyword-only argument
    on purpose: it is read from the caller's membership row, so only the request path
    that already knows who the caller is can supply it, and every other caller (the
    console's "effective" preview, the unit tests) keeps the deployment-only answer it
    has always had.

    ⚠️ `is not None`, never truthiness — see `org_offer` for why an empty licence means
    "nothing" rather than "everything".
    """
    ceiling = set(deployment_default())
    if licence is not None:
        ceiling &= {k for k in licence if k in BY_KEY}
    base = set(ceiling)
    for key, enabled in (user_overrides or {}).items():
        if key not in BY_KEY:
            continue          # a retired module in an old row is not an error
        if enabled:
            if key in ceiling:
                base.add(key)
        else:
            base.discard(key)
    return close_over_requires(base)


def _under(path: str, prefix: str) -> bool:
    """Is `path` the prefix itself or something inside it?

    Segment-aware on purpose: a bare `startswith` lets `/api/reports` swallow
    `/api/reports-export`, so a second module's routes would be gated by (or
    escape) the first one's entitlement. Prefixes ending in `/` (`/r/`) are
    already segment-shaped.
    """
    if prefix.endswith("/"):
        return path.startswith(prefix)
    return path == prefix or path.startswith(prefix + "/")


def module_for_path(path: str) -> Module | None:
    """The module that owns a request path, or None when nothing claims it.

    None is the important case: an unclaimed path is *not* silently allowed. The
    middleware answers 403 for it, so a route added without declaring its module
    fails closed instead of quietly opening a hole.
    """
    for module in MODULES:
        if any(_under(path, p) for p in module.api_prefixes):
            return module
        if any(_under(path, p) for p in module.doc_prefixes):
            return module
    return None


def catalogue() -> list[dict]:
    """The registry as the SPA and the Settings picker need it.

    `page_ids` travels because the shell turns modules into navigation without a
    hand-maintained page→tab table: a page container id is looked up here, and the
    module that claims it owns the tab. `nav_mark` keeps the drawn marks the
    existing tabs already use without freezing them into the SPA.
    """
    return [
        {
            "key": m.key,
            "label_zh": m.label_zh,
            "label_en": m.label_en,
            "blurb_zh": m.blurb_zh,
            "blurb_en": m.blurb_en,
            "icon": m.icon,
            "nav_mark": m.nav_mark,
            "page_ids": list(m.page_ids),
            "order": m.order,
            "required": m.required,
            "toggleable": m.toggleable,
            "requires": list(m.requires),
            "in_nav": m.in_nav,
            "in_rail": m.in_rail,
        }
        for m in sorted(MODULES, key=lambda m: m.order)
    ]


def for_client(enabled: Iterable[str]) -> dict:
    """The payload `/api/auth/me` and `/api/health` attach: what is on, and what could be.

    `catalogue` travels with it so the Settings picker never has to hardcode the
    module list — add a module to the registry and it shows up there.
    """
    on = set(enabled)
    return {
        "enabled": sorted(on, key=lambda k: BY_KEY[k].order if k in BY_KEY else 999),
        "locked": sorted((set(ALL_KEYS) - on) & {k for k in ALL_KEYS if BY_KEY[k].toggleable}),
        "catalogue": catalogue(),
    }
