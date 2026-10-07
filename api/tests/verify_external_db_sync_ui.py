"""The saved-connection UI, and the isolation it claims.

⚠️ The product rule under test: a database connection is private to the person who
operates it; the local COPY it produces may be shared. A test that only checks "I can
see my own connection" would pass against a feature that leaks everything, so the
load-bearing assertions here are the ones made from a SECOND account:

  · account B's dialog shows an empty connection list and an empty schedule list;
  · B cannot reach A's connection by id — 404, and the body is the "not yours"
    message, never a host or a database name;
  · the schedule list is filtered by account too, not just the connection list.

⚠️ Also asserted, because they are the two ways this feature quietly becomes a lie:
  · the password box is NEVER filled from the server (there is no endpoint that can
    return one, so "use this connection" must say so instead of pretending);
  · closing the dialog clears the password field — cancelling is the path that used
    to leave a source password sitting in a DOM node.

Needs a live server and the object store, so it is not in `scripts/ci_check.py`.
`test_external_db_sync.py` is the half CI can run.

    ../.venv312/bin/python tests/verify_external_db_sync_ui.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright                        # noqa: E402

from services import auth_store, data_center_db as db, dataset_groups  # noqa: E402

BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8000")
SHOTS = os.environ.get("KLADO_SHOTS", "/tmp/klado-sync-shots")

SOURCE_SCHEMA = "sync_probe_src"
SOURCE_TABLE = f"orders_{int(time.time())}"

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
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL {name} :: {detail}")


def make_source_table():
    """A real table on the same server, so the import actually transfers rows."""
    with db.get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SOURCE_SCHEMA}")
            cur.execute(f"CREATE TABLE {SOURCE_SCHEMA}.{SOURCE_TABLE} "
                        f"(id INTEGER, item TEXT, amount NUMERIC)")
            cur.execute(f"INSERT INTO {SOURCE_SCHEMA}.{SOURCE_TABLE} VALUES "
                        f"(1,'widget',10.5),(2,'gadget',20.25),(3,'thing',30)")
        conn.commit()


def drop_source_table():
    with db.get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {SOURCE_SCHEMA}.{SOURCE_TABLE}")
            cur.execute(f"DROP SCHEMA IF EXISTS {SOURCE_SCHEMA} CASCADE")
        conn.commit()


def make_user(email, pw):
    auth_store._ensure_schema()
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
        cur.execute("INSERT INTO app_users (email, password_hash, display_name, role) "
                    "VALUES (%s, %s, 'Sync probe', 'admin')", (email, auth_store._hash(pw)))
        conn.commit()


def drop_user(email):
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
        conn.commit()


def login(page, email, pw):
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#auth-login-email", timeout=20000)
    page.fill("#auth-login-email", email)
    page.fill("#auth-login-password", pw)
    page.click("#auth-login-btn")
    page.wait_for_timeout(3500)


def open_dialog(page, slug):
    page.evaluate("s => dc.openImportExternal(s)", slug)
    page.wait_for_timeout(1800)


def conn_rows(page):
    """The saved-connection rows.

    ⚠️ The id is read out of the row's own `onclick="dc.dcConnUse(N)"`, because the
    list is rendered markup with no input to select — and a test that reaches for a
    `<select>` that used to be there will silently report "a second account sees no
    connections" for the wrong reason.
    """
    return page.evaluate(r"""() => Array.from(
        document.querySelectorAll('#dc-ie-conns .dc-conn-row')).map(r => {
            const use = r.querySelector('.dc-at-go');
            const m = (use ? use.getAttribute('onclick') || '' : '').match(/dcConnUse\((\d+)\)/);
            return {id: m ? m[1] : null, text: r.textContent.replace(/\s+/g, ' ').trim()};
        })""")


def main():
    os.makedirs(SHOTS, exist_ok=True)
    stamp = int(time.time())
    owner = f"syncowner{stamp}@example.test"
    other = f"syncother{stamp}@example.test"
    pw = "SyncProbe123!"

    # The source connection points at THIS deployment's own PostgreSQL. The password
    # is read from the environment, never printed.
    pg_host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    pg_port = os.environ.get("POSTGRES_PORT", "5432")
    pg_db = os.environ.get("POSTGRES_DB", "klado")
    pg_user = os.environ.get("POSTGRES_USER", "klado")
    pg_pw = os.environ.get("POSTGRES_PASSWORD", "")

    make_source_table()
    make_user(owner, pw)
    make_user(other, pw)
    print(f"probe users {owner} / {other}, source {SOURCE_SCHEMA}.{SOURCE_TABLE}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context(viewport={"width": 1440, "height": 1000}).new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            login(page, owner, pw)
            page.click("#nav-tab-datacenter")
            page.wait_for_timeout(4000)

            group = dataset_groups.create_group(f"sync-probe-{stamp}", "sync probe", owner)
            slug = group["slug"]
            print(f"container {slug}")

            open_dialog(page, slug)
            check("the dialog opened", page.evaluate(
                "() => document.getElementById('dc-ie-modal').classList.contains('open')"))
            check("there is a saved-connections section",
                  page.locator("#dc-ie-conns").count() == 1, "#dc-ie-conns missing")
            check("an empty account is told so, not shown a bare box",
                  page.locator("#dc-ie-conns .dc-conn-empty").count() == 1,
                  "the empty state is missing")
            check("a fresh account sees no connections",
                  conn_rows(page) == [], f"rows={conn_rows(page)}")

            # ── save one ──
            page.fill("#dc-ie-host", pg_host)
            page.fill("#dc-ie-port", pg_port)
            page.fill("#dc-ie-database", pg_db)
            page.fill("#dc-ie-user", pg_user)
            page.fill("#dc-ie-password", pg_pw)
            page.fill("#dc-ie-schema", SOURCE_SCHEMA)
            page.on("dialog", lambda d: d.accept("probe source"))
            # ⚠️ By id, never by label: the headless browser renders the ENGLISH half
            # of every 中文 / English pair, so a Chinese-text selector times out and
            # reads like a broken button. Ids also survive a copy reword.
            page.click("#dc-ie-conn-save")
            page.wait_for_timeout(2500)
            opts = conn_rows(page)
            check("the connection was saved and listed", len(opts) == 1, f"rows={opts}")
            check("the row shows the host and database",
                  bool(opts) and pg_host in opts[0]["text"] and pg_db in opts[0]["text"],
                  f"row={opts[0]['text'] if opts else None}")
            check("the row says whether a password is stored",
                  bool(opts) and ("password stored" in opts[0]["text"]
                                  or "no password" in opts[0]["text"]),
                  f"row={opts[0]['text'] if opts else None}")
            check("the password box was cleared after saving",
                  page.input_value("#dc-ie-password") == "",
                  "the source password is still in the form")

            # ── "use" must not hand a password back ──
            # Click the row's own Use action — a real click, so this also proves the
            # action is hit-testable rather than merely present.
            page.click("#dc-ie-conns .dc-conn-row .dc-at-go")
            page.wait_for_timeout(700)
            check("using a connection marks its row as current",
                  page.locator("#dc-ie-conns .dc-conn-row.is-current").count() == 1,
                  "no row is marked in use")
            check("using a connection fills the connection fields",
                  page.input_value("#dc-ie-host") == pg_host,
                  f"host={page.input_value('#dc-ie-host')}")
            check("using a connection does NOT fill the password",
                  page.input_value("#dc-ie-password") == "",
                  "a stored password was written back into the page")
            check("and the field says why",
                  "stored" in (page.get_attribute("#dc-ie-password", "placeholder") or "").lower(),
                  page.get_attribute("#dc-ie-password", "placeholder"))

            # ── a saved connection cannot connect until the password is retyped ──
            # ⚠️ This is the design, asserted rather than worked around. The API has
            # no endpoint that returns a password, so "use this connection" leaves
            # the box empty; the very next action therefore fails until the owner
            # types it again. A test that quietly re-filled the password before
            # clicking would hide the one thing a reader needs to be told.
            page.click("#dc-ie-peek")
            page.wait_for_timeout(3000)
            peeked_empty = page.evaluate("() => document.getElementById('dc-ie-tables').textContent")
            check("using a saved connection does NOT connect on its own",
                  SOURCE_TABLE not in peeked_empty,
                  f"the table list filled in without a password: {peeked_empty[:120]!r}")
            check("and it says why, rather than failing silently",
                  bool(peeked_empty.strip()) and SOURCE_TABLE not in peeked_empty,
                  "the table list is empty with no explanation")

            # ── peek + import + schedule, with the password retyped ──
            page.fill("#dc-ie-password", pg_pw)
            page.click("#dc-ie-peek")
            page.wait_for_timeout(2500)
            check("the source table is offered",
                  SOURCE_TABLE in page.content(),
                  "the table list did not include the probe table")
            page.evaluate("t => dc.dcIePick(t)", SOURCE_TABLE)
            page.fill("#dc-ie-target", f"copied_{stamp}")
            # ⚠️ Click the LABEL, not the input: the switch is a checkbox drawn with
            # `appearance: none` behind it, and the interval is a radio group behind
            # five pill faces. `page.check()` needs a visible box and `select_option`
            # needs a <select> — neither exists any more, and both were quietly
            # "the old control" rather than the behaviour being tested.
            page.click(".dc-ie-switchrow")
            page.wait_for_timeout(300)
            check("turning on the switch enables the interval pills",
                  page.is_enabled('#dc-ie-pills .dc-ie-pill:nth-child(2) input'))
            page.click("#dc-ie-pills .dc-ie-pill:nth-child(2)")   # the 1h face
            page.wait_for_timeout(200)

            page.click("#dc-ie-ok")
            page.wait_for_timeout(4000)
            check("the import reported no error", not errors, "; ".join(errors[:2]))

            # the local copy exists and belongs to this container
            copy = f"copied_{stamp}"
            meta = db.get_dataset(copy)
            check("the local copy was created", meta is not None, f"{copy} missing")
            if meta:
                check("and it is filed under this container",
                      meta.get("dataset_group_id") == group["id"],
                      f"group={meta.get('dataset_group_id')}")

            open_dialog(page, slug)
            page.wait_for_timeout(2000)
            # ⚠️ Read the API, not the JS variable. `dc._jobs` is filled by an
            # un-awaited fetch in `openImportExternal`, so sampling it is a race —
            # and a race that reads as "the schedule was not created", i.e. it
            # reports a working feature as broken.
            jobs = page.evaluate("""async () => {
                const r = await fetch(appAbsUrl('api/data-center/db-sync-jobs'));
                const d = await r.json().catch(() => ({}));
                return {status: r.status, all: d.jobs || []};
            }""")
            check("the schedule list answered 200", jobs["status"] == 200, f"{jobs}")
            jobs = [j for j in jobs["all"] if j["slug"] == slug]
            check("a schedule was created", len(jobs) == 1, f"jobs={jobs}")
            check("the schedule points at the saved connection",
                  bool(jobs) and jobs[0]["connection_id"] == int(opts[0]["id"]),
                  f"job={jobs[0] if jobs else None}")
            check("the schedule row names the copy and the period",
                  bool(jobs) and copy in page.content(),
                  "the schedule row is not describing the job")
            # ⚠️ POSITIVE CONTROL for the second-account check further down. "B sees
            # zero rows" is vacuous if the selector matches nothing at all — which is
            # exactly what happened when this selector still said `.dc-at-row` after
            # both lists became row cards: it matched nothing, so B's emptiness and
            # a completely unrendered list looked identical. The owner must be shown
            # to actually have rows here.
            check("the owner's schedule list really does render rows",
                  page.locator("#dc-ie-jobs .dc-job-row").count() == 1,
                  "rows=%d" % page.locator("#dc-ie-jobs .dc-job-row").count())
            check("the source password appears nowhere in the owner's page either",
                  not pg_pw or pg_pw not in page.content(),
                  "the page contains the source password")
            page.screenshot(path=os.path.join(SHOTS, "schedules.png"), full_page=True)

            # ── a schedule for a copy that ALREADY EXISTS ──────────────────────
            # This is the capability the 2026-10-06 redesign added: before it, the
            # only way to get a schedule was to tick a box DURING an import, so a
            # copy imported last week could never start refreshing. Two things are
            # load-bearing here, and both were ways to ship a button that does
            # nothing: the form has to REFUSE without a saved connection (it cannot
            # invent one), and it has to refuse LOUDLY rather than quietly.
            page.click("#dc-ie-sched-new")
            page.wait_for_timeout(500)
            check("the schedule form opens on demand",
                  page.locator("#dc-ie-sched-form").is_visible())
            check("and it says which connection it will use",
                  "pick a connection" in page.locator("#dc-ie-sched-conn").inner_text(),
                  f"chip={page.locator('#dc-ie-sched-conn').inner_text()!r}")
            page.fill("#dc-ie-sched-source", SOURCE_TABLE)
            page.fill("#dc-ie-sched-target", copy)
            page.click("#dc-ie-sched-create")
            page.wait_for_timeout(1500)
            # Reopening the dialog cleared the in-use connection, which is exactly
            # why the refusal has to be a message and not a disabled button nobody
            # can work out the reason for.
            check("creating one with no connection picked is refused, not silently ignored",
                  page.locator("#dc-ie-sched-form").is_visible()
                  and "needs a saved connection" in page.content(),
                  "the form closed as if it had worked")
            page.click("#dc-ie-conns .dc-conn-row .dc-at-go")   # Use
            page.wait_for_timeout(500)
            # ⚠️ NOT a second `#dc-ie-sched-new` click. That button is a disclosure
            # toggle — clicking it again CLOSES the form, and the two fills below then
            # hit a hidden input and time out. The form is still open from above; the
            # refusal deliberately left it open so the reason stays on screen.
            page.fill("#dc-ie-sched-source", SOURCE_TABLE)
            page.fill("#dc-ie-sched-target", copy)
            page.click("#dc-ie-pills .dc-ie-pill:nth-child(3)")   # the 6h face
            page.wait_for_timeout(200)
            page.click("#dc-ie-sched-create")
            page.wait_for_timeout(2500)
            check("a second schedule can be added for the same local copy",
                  page.locator("#dc-ie-jobs .dc-job-row").count() == 2,
                  "rows=%d" % page.locator("#dc-ie-jobs .dc-job-row").count())
            check("it closed the form once it worked",
                  not page.locator("#dc-ie-sched-form").is_visible())
            jobs2 = page.evaluate("""async () => {
                const r = await fetch(appAbsUrl('api/data-center/db-sync-jobs'));
                const d = await r.json().catch(() => ({}));
                return d.jobs || [];
            }""")
            fresh = [j for j in jobs2 if j["slug"] == slug
                     and j["target_table"] == copy and j["interval_minutes"] == 360]
            check("the new schedule carries the interval that was picked",
                  len(fresh) == 1, f"jobs={[(j['target_table'], j['interval_minutes']) for j in jobs2]}")
            check("and it says when it will next run",
                  bool(fresh) and fresh[0].get("next_run_at"),
                  f"next_run_at={fresh[0].get('next_run_at') if fresh else None}")
            check("the schedule row shows that time to the reader",
                  bool(fresh) and "next" in page.content().lower(),
                  "the row does not show a next run")
            page.screenshot(path=os.path.join(SHOTS, "two-schedules.png"), full_page=True)

            # ── run it now ──
            if jobs:
                # ⚠️ `.dc-job-row`, and it has been two other class names: `.dc-at-row`
                # until the 2026-10-06 redesign, then `.dc-conn-row` shared with the
                # connection list, now its own row type. A stale name fails quietly in
                # two different ways — `page.click` just times out, while a COUNT
                # assertion matches zero elements and passes forever. That is why the
                # owner-side positive control above exists.
                page.click("#dc-ie-jobs .dc-job-row span.dc-at-go")
                page.wait_for_timeout(4000)
                after = page.evaluate("""async (id) => {
                    const r = await fetch(appAbsUrl('api/data-center/db-sync-jobs/' + id + '/run'),
                                          {method: 'POST'});
                    return {status: r.status, body: await r.json().catch(() => ({}))};
                }""", jobs[0]["id"])
                check("pull-now reported a result",
                      after["status"] == 200 and after["body"].get("status") in ("ok", "failed"),
                      f"{after}")
                check("and it succeeded against the real table",
                      after["body"].get("status") == "ok",
                      f"error={after['body'].get('error')}")

            # ── cancel clears the password ──
            page.fill("#dc-ie-password", "typed-then-cancelled")
            page.click("#dc-ie-cancel")
            page.wait_for_timeout(600)
            check("closing the dialog clears the password field",
                  page.input_value("#dc-ie-password") == "",
                  "the password survived a cancel")

            # ══ the isolation assertions, from a second account ══
            page2 = browser.new_context(viewport={"width": 1440, "height": 1000}).new_page()
            errors2: list[str] = []
            page2.on("pageerror", lambda e: errors2.append(str(e)))
            login(page2, other, pw)
            page2.click("#nav-tab-datacenter")
            page2.wait_for_timeout(4000)
            open_dialog(page2, slug)
            page2.wait_for_timeout(1500)
            check("a second account sees NO connections",
                  conn_rows(page2) == [], f"B sees {conn_rows(page2)}")
            check("a second account sees NO schedules",
                  not page2.locator("#dc-ie-jobs .dc-job-row").count(),
                  "B sees someone else's schedule")
            # ⚠️ Not `pg_db not in content` — the database is called "klado" and that
            # is the product's own name, so it is on every page including B's. The
            # string only a connection listing would produce is host:port/database,
            # plus the label this probe chose.
            fingerprint = pg_host + ":" + pg_port + "/" + pg_db
            check("the other account sees no connection line on the page",
                  fingerprint not in page2.content() and "probe source" not in page2.content(),
                  f"B's page contains {fingerprint!r} or the saved label")
            page2.screenshot(path=os.path.join(SHOTS, "second-account.png"), full_page=True)

            # and by id, directly
            probed = page2.evaluate(
                """async (id) => {
                    const r = await fetch(appAbsUrl('api/data-center/db-connections/' + id));
                    return {status: r.status, body: (await r.text()).slice(0, 200)};
                }""", int(opts[0]["id"]))
            check("fetching somebody else's connection by id is refused",
                  probed["status"] == 404, f"status={probed['status']} body={probed['body']}")
            check("and the refusal does not confirm what it holds",
                  fingerprint not in probed["body"],
                  probed["body"])

            probed2 = page2.evaluate(
                """async (id) => {
                    const r = await fetch(appAbsUrl('api/data-center/db-sync-jobs'));
                    const d = await r.json().catch(() => ({}));
                    const list = d.jobs || [];
                    return {status: r.status, mine: list.length,
                            runs: await Promise.all(list.map(j =>
                                fetch(appAbsUrl('api/data-center/db-sync-jobs/' + j.id + '/run'),
                                      {method:'POST'}).then(x => x.status)))};
                }""", int(opts[0]["id"]))
            check("the other account's schedule list is empty",
                  probed2["status"] == 200 and probed2["mine"] == 0,
                  f"{probed2}")

            check("no uncaught error for the owner", not errors, "; ".join(errors[:2]))
            check("no uncaught error for the other account", not errors2, "; ".join(errors2[:2]))
            browser.close()
    finally:
        drop_source_table()
        drop_user(owner)
        drop_user(other)
        try:
            dataset_groups.delete_group(slug, owner)
        except Exception:  # noqa: BLE001 — cleanup must not mask a test result
            pass
        with db.get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS public.copied_{stamp}")
                cur.execute("DELETE FROM public._import_registry WHERE table_name = %s",
                            (f"copied_{stamp}",))
                cur.execute("DELETE FROM public.external_db_sync_jobs WHERE slug = %s", (slug,))
            conn.commit()
        print("cleaned up")

    print(f"\n{PASS} passed, {FAIL} failed")
    for f in FAILURES:
        print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
