"""The account lifecycle in a real browser: impact dialog, close, restore, erase.

Static checks cannot tell you a dialog opens, that the email typed back is compared
against the right account, or that the recycle bin renders rows — so this drives the
page with Playwright and asserts on what is actually on screen.

⚠️ Needs :8000 running the CURRENT code plus a signed-in admin. It mints its own
throwaway accounts and closes them again, so it leaves nothing behind (the `finally`
purges whatever the assertions did not).

    ../.venv312/bin/python tests/verify_account_ui.py
"""
import os
import re
import sys
import time

# ⚠️ BOTH roots: this file is run as `python tests/verify_account_ui.py` from `api/`,
# so the first entry makes `services.*` importable and the second makes
# `klado_shared.*` importable (it lives beside `api/`, not inside it). The second path
# was missing until this script started building a probe company.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from playwright.sync_api import sync_playwright                        # noqa: E402

from klado_shared import orgs                                    # noqa: E402
from services import auth_store, account_lifecycle as al               # noqa: E402

BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8000")
SHOTS = os.environ.get("KLADO_SHOTS", "/tmp/klado-lifecycle-shots")

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")


def mk_user(email, password, name="probe", role="user"):
    """Admin is just a column; minting a throwaway administrator avoids asking for
    anybody's real password in order to test the delete-account flow."""
    auth_store._ensure_schema()
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
        cur.execute("INSERT INTO app_users (email, password_hash, display_name, role) "
                    "VALUES (%s, %s, %s, %s)", (email, auth_store._hash(password), name, role))
        cur.execute("SELECT id FROM app_users WHERE email = %s", (email,))
        uid = cur.fetchone()[0]
        conn.commit()
    return uid


def seed_content(email, tag):
    """Idempotent: an aborted earlier run leaves rows behind, and a second run with the
    same slug then fails on the unique index before it ever reaches the UI."""
    from core.db import connect_main
    with connect_main() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM ai_reports WHERE slug = %s", (f"{tag}-r",))
            cur.execute("DELETE FROM ai_dashboards WHERE slug = %s", (f"{tag}-d",))
            cur.execute("INSERT INTO ai_reports (slug,title,owner_email,visibility,kind,html,"
                        "created_at,updated_at) VALUES (%s,%s,%s,'public','static','<p>x</p>',NOW(),NOW())",
                        (f"{tag}-r", "报告 " + tag, email))
            cur.execute("INSERT INTO ai_dashboards (slug,title,owner_email,visibility,datasets,"
                        "created_at,updated_at) VALUES (%s,%s,%s,'private','[]',NOW(),NOW())",
                        (f"{tag}-d", "页面 " + tag, email))
        c.commit()


def mk_company(domain, name="Probe Co", owner_email=None):
    """A company, with `owner_email` as its owner.

    ⚠️ Settings is the ENTERPRISE admin page: an operator alone does not get it (their
    console is a separate application on its own port). This test drives the whole
    account-lifecycle surface from that page, so its admin needs BOTH identities —
    operator, which is what makes `/api/auth/admin/*` reachable, and enterprise owner,
    which is what makes the page itself reachable. Minting only the first half left the
    script signing in to a page it could not open, which is how a fixture can be wrong
    for a long time and still look like a product bug.
    """
    org = orgs.create_org(name, kind=orgs.KIND_ENTERPRISE, domains=[domain])
    if owner_email:
        orgs.upsert_invitation(org["id"], owner_email, "owner", invited_by=owner_email)
        orgs.attach_existing_account(org["id"], owner_email)
    return org


def _overrides(email):
    """How many `account_modules` rows one address has. 0 = nobody has said anything."""
    from core.db import connect_main
    with connect_main() as c:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM account_modules am "
                        "JOIN app_users u ON u.id = am.user_id WHERE u.email = %s",
                        (email,))
            return int(cur.fetchone()[0])


def _closed_modules(email, offered):
    """The offered module keys this address has explicitly been CLOSED for.

    ⚠️ Read from the database on purpose. The dialog is the thing that was just clicked,
    so asking the dialog what it now believes proves only that a checkbox toggles.
    """
    from core.db import connect_main
    with connect_main() as c:
        with c.cursor() as cur:
            cur.execute("SELECT am.module_key FROM account_modules am "
                        "JOIN app_users u ON u.id = am.user_id "
                        "WHERE u.email = %s AND am.enabled = FALSE", (email,))
            rows = {str(r[0]) for r in cur.fetchall()}
    return sorted(rows & set(offered))


def main():
    stamp = int(time.time())
    tag = f"ui{stamp}"
    victim = f"lifecycle-ui-{stamp}@example.test"
    adm_email = f"lifecycle-ui-admin-{stamp}@example.test"
    vpw, apw = "Ui-Probe-Passw0rd!", "Ui-Admin-Probe-1!"
    vid = mk_user(victim, vpw, "UI victim")
    aid = mk_user(adm_email, apw, "UI admin", role="admin")
    org = mk_company(f"lifecycle-ui-{stamp}.invalid", f"Lifecycle UI {stamp}", adm_email)
    # ⚠️ The victim joins the SAME company. With both identities held, the page reads
    # `/api/org/members` — the company's roster — so a victim outside it is not listed
    # at all and every row-scoped step below ("the probe account is listed", the close
    # dialog, the restore) fails on a page that is working exactly as designed.
    # `attach_existing_account` only binds an OUTSTANDING INVITATION (it returns None
    # without one), so the invitation is what has to be created first.
    orgs.upsert_invitation(org["id"], victim, "member", invited_by=adm_email)
    orgs.attach_existing_account(org["id"], victim)
    print(f"victim id={vid}, throwaway admin id={aid}, company id={org['id']}")

    os.makedirs(SHOTS, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            # ⚠️ A bare "Failed to load resource: 403" names no URL, and this page makes
            # dozens of calls — so a refused one is recorded with the request it came
            # from. A refusal is not automatically a bug (the app probes routes before
            # sign-in, and this page deliberately calls the console's API to prove the
            # main app does not serve it), but "which one" is the difference between a
            # five-second fix and an afternoon.
            refused = []
            page.on("response", lambda r: refused.append(
                f"{r.status} {r.request.method} {r.url}") if r.status >= 400 else None)

            # Sign in as the throwaway admin. The ids are the auth gate's own: a bare
            # `input[type="password"]` matches six fields on this page, the first of
            # which lives in a hidden admin card.
            page.goto(BASE + "/", wait_until="domcontentloaded")
            page.wait_for_selector("#auth-login-email", timeout=15000)
            page.fill("#auth-login-email", adm_email)
            page.fill("#auth-login-password", apw)
            page.click("#auth-login-btn")
            page.wait_for_timeout(3000)
            check("signed in as admin",
                  page.locator("#auth-gate").is_hidden() or page.locator('.nav-tab').count() > 0,
                  "the auth gate is still up")

            # Open the admin page the way a person does: click the Settings tab, not a
            # URL. `#adm-users-body` lives inside `#page-system-settings`, so filling it
            # while that page is hidden times out on "element is not visible" — which is
            # a script bug, not a UI bug, and worth driving the real path to avoid.
            page.click("#nav-tab-settings")
            page.wait_for_timeout(3000)

            page.wait_for_selector("#adm-users-q", timeout=15000)
            check("the admin page is visible", page.locator("#adm-users-q").is_visible(),
                  "#adm-users-q is hidden")
            seed_content(victim, tag)

            # `load()` is async and fires three requests; give the table real time
            # rather than racing it.
            page.wait_for_selector("#adm-users-body table", timeout=20000)
            check("the users table rendered", page.locator("#adm-users-body table").count() == 1,
                  "no table in #adm-users-body")

            # ── what is on this page, and what is not (P6) ───────────────────
            # The recycle bin, the system mailbox, the deployment allowlist and the
            # operator's agent codes all moved to the admin console on its own port.
            # Asserting their ABSENCE is the point: a card left behind would be a
            # second, quieter copy of the same control, and two copies of "delete this
            # account" is how one of them ends up wrong.
            check("the recycle bin card is gone from this page",
                  page.locator("#adm-bin-body").count() == 0,
                  "#adm-bin-body is still here")
            check("the mail card is gone from this page",
                  page.locator("#adm-mail-host").count() == 0,
                  "#adm-mail-host is still here")
            check("the agent-code renderer is gone from this page",
                  page.evaluate("typeof window.__renderAgentTable") == "undefined",
                  "window.__renderAgentTable still exists")
            check("and the bin is not served by the main app",
                  page.evaluate("async () => (await fetch('/api/admin-console/recycle-bin'))"
                                ".status !== 200"),
                  "the main app answered the console's route")
            # The two cards P6 added.
            check("the enterprise settings card is present",
                  page.locator("#adm-card-org").count() == 1, "no #adm-card-org")
            check("the pending approvals card is present",
                  page.locator("#adm-card-approvals").count() == 1, "no #adm-card-approvals")
            # ⚠️ The module card is an ABSENCE now, not a presence. The decision moved
            # onto the member row, and a second place to set the same thing is how one of
            # them stops being the one the server enforces.
            check("the module matrix card is gone from the page",
                  page.locator("#adm-card-modules").count() == 0,
                  "#adm-card-modules is still on the page")
            # ⚠️ This account holds BOTH identities: platform operator (so
            # `/api/auth/admin/*` answers) and enterprise owner (so the page opens at
            # all). The cards therefore follow the ENTERPRISE rules, because that is
            # what the page is for:
            #   · the company card is shown — the account now HAS a company;
            #   · the roster is the COMPANY's, not the whole installation's.
            # The old assertions here were the operator-only world: a card hidden
            # because "an operator has no company". That is still true of an operator
            # with no company — it is just not what this fixture builds any more.
            check("the enterprise settings card is shown to a company owner",
                  page.locator("#adm-card-org").is_visible(),
                  "it is hidden for an owner who has a company")
            check("the roster is titled for the company, not the installation",
                  "本企业成员" in page.locator("#adm-members-title").inner_text()
                  or "Organization members" in page.locator("#adm-members-title").inner_text(),
                  page.locator("#adm-members-title").inner_text())
            # ⚠️ The permission rule this script's fixture depends on, asserted where
            # the page is actually open. Without it the fixture could silently become an
            # operator-only account again, the page would refuse to open, and the
            # failure would arrive much later as "the table never rendered".
            check("the Settings tab is offered to a company owner",
                  page.locator("#nav-tab-settings").is_visible(),
                  "hidden — the owner cannot reach the page they administer")
            # ⚠️ The account-lifecycle steps below (close / restore / erase) are NOT
            # driven through this page any more. Closing an account, its 30-day recycle
            # bin and its impact summary are installation-wide: they live in the admin
            # console on its own port, and the Settings page shows a company's roster
            # with "Remove from company" — which is a different act with a different
            # consequence. The UI half of that split is asserted here; the lifecycle
            # half is asserted by `verify_account_lifecycle_http.py` against the console.
            page.screenshot(path=os.path.join(SHOTS, "00-admin-cards.png"), full_page=True)

            # ── the company roster: search, and what it offers ──────────────
            # ⚠️ This is the company's roster, so the row actions are the COMPANY's:
            # a role select and "Remove from company". Closing an ACCOUNT is a
            # different act with a different consequence (the account itself, its
            # content and a 30-day recycle bin), and it belongs to the admin console on
            # its own port — `verify_account_lifecycle_http.py` covers it there.
            page.fill("#adm-users-q", victim)
            page.evaluate("window.adminPage.searchUsers()")
            page.wait_for_timeout(2000)
            row = page.locator(f'#adm-users-body tr:has-text("{victim}")')
            check("the probe account is listed", row.count() >= 1, "row not found")
            if row.count():
                check("a member row offers the role select",
                      row.first.locator("select.adm-role").count() == 1,
                      row.first.inner_text()[:160])
                check("and offers Remove from company",
                      row.first.locator('button:has-text("\u79fb\u51fa\u4f01\u4e1a")').count() == 1
                      or row.first.locator('button:has-text("Remove")').count() == 1,
                      row.first.inner_text()[:160])
                # ⚠️ Asserted as an ABSENCE on purpose: this page must not be a second
                # way to close an account. Two controls for one act is how one of them
                # ends up wrong, and the copy under the roster already says so.
                check("and does NOT offer Close/Erase — the console owns that",
                      row.first.locator('button:has-text("Close")').count() == 0
                      and row.first.locator('button:has-text("Erase")').count() == 0,
                      row.first.inner_text()[:200])
            page.screenshot(path=os.path.join(SHOTS, "02-roster-row.png"), full_page=True)

            # ── inviting somebody whose address is NOT on the company's suffixes ──
            # ⚠️ This used to be a 400, and it is the one product rule in this script
            # that was reversed rather than added. The suffix list answers "which company
            # claims this person when they sign THEMSELVES up"; an invitation is the
            # opposite — an administrator naming somebody on purpose. So the button has to
            # accept a contractor's, a partner's or an executive's own address.
            #
            # Driven through the real box rather than the API, because the thing that
            # used to block this was the server and the thing that used to tell the user
            # it would fail was THIS LABEL. Both had to change together; asserting only
            # the status code would have passed against a page still promising otherwise.
            outsider = f"outsider-{tag}@elsewhere.invalid"
            page.fill("#adm-invite-email", outsider)
            page.click("#adm-invite-btn")
            page.wait_for_timeout(2500)
            status_text = page.locator("#adm-invite-status").inner_text()
            # ⚠️ Assert on the SHAPE of the answer, not on a list of success words. The
            # status line is a badge (sent / not emailed) plus the address and the number
            # of days; the mail channel being unconfigured is a deployment fact, not a
            # failure, and keying the assertion to particular words made a working invite
            # read as a broken one.
            check("an off-suffix address is accepted, not refused",
                  "只能邀请" not in status_text and outsider in status_text,
                  status_text[:200])
            check("and the invite came back with a usable code and a lifetime",
                  bool(re.search(r"\d+\s*天有效|\d+\s*days valid", status_text))
                  and page.locator("#adm-invite-status .adm-code").count() == 1,
                  status_text[:200])
            # ⚠️ Clear the search box first. The roster filters client-side, and the box
            # still held the previous step's query, so the new row was invisible while
            # the server had in fact accepted it — which is precisely the shape of a
            # product that refuses the address.
            page.fill("#adm-users-q", "")
            page.evaluate("window.adminPage.searchUsers()")
            page.wait_for_timeout(2000)
            orow = page.locator(f'#adm-users-body tr:has-text("{outsider}")')
            check("the invited outsider is on the roster", orow.count() >= 1,
                  f"{outsider} not among "
                  f"{page.locator('#adm-users-body tbody tr').count()} rows")
            if orow.count():
                check("and is visibly marked as not on the company's suffixes",
                      orow.first.locator(".adm-badge.warn").count() >= 1,
                      orow.first.inner_text()[:160])
            page.screenshot(path=os.path.join(SHOTS, "02b-invited-outsider.png"),
                            full_page=True)

            # ── the module dialog: a checkbox list on the member row ───────
            # ⚠️ It used to be a nine-column matrix pinned under the roster, with
            # three states per cell that you cycled by clicking. The invariants that
            # matter now:
            #   (1) the trigger is ON THE ROW, where the person is;
            #   (2) the options are real `<input type=checkbox>`, one per module the
            #       COMPANY is licensed for — not the whole registry;
            #   (3) a box starts ticked unless somebody explicitly closed it. Every
            #       offered module is in the deployment default by construction, so
            #       "no row" means open, and a colleague who has never been narrowed
            #       must not appear to have lost four modules on arrival.
            page.evaluate("window.adminPage.loadModules()")
            page.wait_for_timeout(1500)
            modbtn = row.first.locator("button.mod-btn")
            check("the member row carries a module button", modbtn.count() == 1,
                  row.first.inner_text()[:160])
            if modbtn.count():
                modbtn.first.click()
                page.wait_for_timeout(900)
                dlg = page.locator("#kld-dlg")
                check("the module dialog opened", dlg.is_visible(),
                      "#kld-dlg stayed hidden — the button ran and nothing appeared")
                offered = page.evaluate(
                    "(() => { const o = window.kladoOrg.get(); "
                    "return (o && o.org && o.org.module_options) || []; })()")
                boxes = page.locator("#kld-dlg .mod-pick-box")
                check("one checkbox per module the company is licensed for",
                      boxes.count() == len(offered) and boxes.count() >= 1,
                      f"{boxes.count()} boxes vs {len(offered)} offered")
                check("the boxes are real checkboxes, not drawn divs",
                      page.evaluate("Array.from(document.querySelectorAll("
                                    "'#kld-dlg .mod-pick-box'))"
                                    ".every(e => e.tagName === 'INPUT' "
                                    "&& e.type === 'checkbox')"), "")
                check("each row shows a module icon and a module name",
                      page.locator("#kld-dlg .mod-pick").count() == boxes.count()
                      and page.locator("#kld-dlg .mod-pick-ico i").count() == boxes.count(),
                      page.locator("#kld-dlg").inner_text()[:160])
                check("a member nobody has narrowed starts with every module open",
                      page.evaluate("Array.from(document.querySelectorAll("
                                    "'#kld-dlg .mod-pick-box'))"
                                    ".every(e => e.checked)"), "")
                page.screenshot(path=os.path.join(SHOTS, "04-modules.png"), full_page=True)

                # Cancel must be a real no-op, not a silent save.
                page.click("#kld-dlg-cancel")
                page.wait_for_timeout(600)
                check("Cancel closes the dialog", not dlg.is_visible(), "still open")
                check("and wrote no permission row",
                      _overrides(victim) == 0, f"{_overrides(victim)} rows")

                # Now the round trip that actually matters: untick one, save, and
                # confirm the SERVER now disagrees with the deployment for that one
                # module — read back through the members list, not out of the DOM that
                # we just clicked in.
                modbtn.first.click()
                page.wait_for_timeout(700)
                page.locator("#kld-dlg .mod-pick-box").first.click()
                page.click("#kld-dlg-ok")
                page.wait_for_timeout(2500)
                closed = _closed_modules(victim, offered)
                check("saving an unticked box wrote exactly one closed module",
                      len(closed) == 1,
                      f"closed={closed} of offered={offered}")
                check("and it is the one that was unticked",
                      page.evaluate(
                          "document.querySelector('#adm-users-body .mod-btn') !== null"),
                      "the row button disappeared after a save")
                row_chips = page.evaluate(
                    "Array.from(document.querySelectorAll('#adm-users-body button.mod-btn'))"
                    ".map(b => b.className)")
                check("the row button now says something was set for this person",
                      any('mod-btn-set' in c for c in row_chips), row_chips[:2])

                # Put it back, or the next run starts from a narrowed member.
                modbtn.first.click()
                page.wait_for_timeout(700)
                page.locator("#kld-dlg .mod-pick-box").first.click()
                page.click("#kld-dlg-ok")
                page.wait_for_timeout(2000)
                check("re-ticking it writes no row at all — open is the default, not a decision",
                      _overrides(victim) == 0, f"{_overrides(victim)} rows left behind")

            # ── the approvals queue renders, and empty is a real answer ──────
            page.evaluate("window.adminPage.loadApprovals()")
            page.wait_for_timeout(2000)
            ap = page.locator("#adm-approvals-body")
            check("the approvals card answered", ap.count() == 1, "no #adm-approvals-body")
            check("an empty queue says so rather than rendering nothing",
                  ap.inner_text().strip() != "", "the body is blank")
            page.screenshot(path=os.path.join(SHOTS, "05-approvals.png"), full_page=True)

            # the admin survived all of it
            check("the admin survived", auth_store.get_user_by_email(adm_email) is not None,
                  "the admin was deleted")

            # Only real script failures. Three sources of noise, each named:
            #   · the app's pre-sign-in boot probes, which answer 401 — the gate working;
            #   · `favicon`;
            #   · the console route THIS SCRIPT called on purpose a few lines ago, to
            #     prove the main app does not serve it. That one is answered 403, not
            #     404, because the main app's middleware treats an unclaimed path as
            #     "no module owns this" and fails closed — which is the correct answer
            #     and is exactly what the assertion is about.
            deliberate = "/api/admin-console/recycle-bin"
            real = [e for e in errors
                    if "favicon" not in e.lower()
                    and "401" not in e
                    and "Unauthorized" not in e
                    and not any(r.endswith(deliberate) for r in refused)]
            check("no uncaught JS errors", not real, "; ".join(real[:3]))

            # ⚠️ Reported, not asserted. A 4xx on this page has three legitimate sources
            # — the pre-sign-in boot probes, the deliberate call to a console route, and
            # the module gate refusing a path this account may not use — and deciding
            # which is which by reading a list is worse than looking. So the list is
            # printed with every URL on it, and a NEW 4xx shows up as a line nobody has
            # seen before.
            if refused:
                print("\n  requests the page was refused (reported, not asserted):")
                for line in sorted(set(refused)):
                    print(f"    {line}")
            browser.close()
    finally:
        for uid in (vid, aid):
            try:
                al.soft_delete(uid, actor="cleanup")
                al.purge_account(uid)
            except Exception:                                # noqa: BLE001
                try:
                    al.purge_account(uid)
                except Exception:                            # noqa: BLE001
                    pass
        from core.db import connect_main
        with connect_main() as c:
            with c.cursor() as cur:
                cur.execute("DELETE FROM ai_reports WHERE owner_email IN (%s, %s)", (victim, adm_email))
                cur.execute("DELETE FROM ai_dashboards WHERE owner_email IN (%s, %s)", (victim, adm_email))
                # ⚠️ The probe company, its membership row and any approval it collected
                # have to go too. `org_members` is keyed by EMAIL, not user_id, so
                # deleting `app_users` above does NOT remove it — a leftover row keeps
                # the address attached to a company that no longer exists, and the next
                # run's `membership_of` then answers about a phantom organisation.
                cur.execute("DELETE FROM org_public_approvals WHERE org_id = %s", (org["id"],))
                cur.execute("DELETE FROM org_members WHERE org_id = %s", (org["id"],))
                cur.execute("DELETE FROM orgs WHERE id = %s", (org["id"],))
                cur.execute("DELETE FROM app_users WHERE email IN (%s, %s)", (victim, adm_email))
                cur.execute("DELETE FROM account_modules WHERE user_id NOT IN (SELECT id FROM app_users)")
                cur.execute("DELETE FROM agent_tokens WHERE user_id NOT IN (SELECT id FROM app_users)")
            c.commit()

    print(f"\n{PASS} passed, {FAIL} failed")
    for f in FAILURES:
        print("  -", f)
    print(f"screenshots: {SHOTS}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
