"""删一个数据集：确认框里写得出具体表名，定时任务会被单独问一次。

这是产品真正要的那句话 —— 「点删除时告诉我会删掉哪些表」—— 的唯一可信判据。
旧实现从 `_state.groups` 数表，那只是页面上次渲染时的快照，所以断言必须盯住
**对话框里的真实表名**，而不是任何计数。

⚠️ 这里的每一条否定断言都配一条正对照：

* 「取消之后什么都没删」要靠**点之前**先证明它还在 —— 否则「选择器没匹配到任何东西」
  和「东西没了」返回同一个形状，这条断言恒绿。
* 「没有定时任务的数据集不会被追问」要靠**有定时任务的那个确实被追问了**来配 ——
  这就是 Phase C 存在的理由，不是顺便测的。

⚠️ 全程用真 `page.click`，不用 `el.click()`：后者直接派发到元素、不做命中测试，
一个被盖住的按钮照样「点得动」。

需要一个在跑的服务（默认 `KLADO_BASE`）和它自己的 PostgreSQL，所以不在
`scripts/ci_check.py` 里；`test_dataset_delete.py` 是 CI 能跑的那一半。

    KLADO_BASE=http://127.0.0.1:18161 ../.venv312/bin/python tests/verify_dataset_delete_ui.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright                        # noqa: E402

from services import auth_store, data_center_db as db, dataset_groups, external_sync  # noqa: E402

BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8000")
SHOTS = os.environ.get("KLADO_SHOTS", "/tmp/klado-delete-shots")

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


def make_user(email, pw):
    auth_store._ensure_schema()
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
        cur.execute("INSERT INTO app_users (email, password_hash, display_name, role) "
                    "VALUES (%s, %s, %s, 'admin')", (email, auth_store._hash(pw),
                                                    "Delete probe"))
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


def api(page, path, method="GET"):
    return page.evaluate(
        """async ([p, m]) => {
            const r = await fetch(appAbsUrl('api/data-center/' + p), {method: m});
            const t = await r.text();
            let b = {}; try { b = JSON.parse(t); } catch (e) {}
            return {status: r.status, body: b};
        }""", [path, method])


def table_exists(name):
    with db.get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.' || %s) IS NOT NULL", (name,))
            return bool(cur.fetchone()[0])


def registry_has(name):
    with db.get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM public._import_registry WHERE table_name = %s",
                        (name,))
            return cur.fetchone() is not None


def jobs_for(owner, slug):
    return [j for j in external_sync.list_jobs(owner) if j.get("slug") == slug]


def dialog_open(page):
    return page.evaluate("() => { const d = document.getElementById('kld-dlg');"
                         " return !!d && !d.hidden; }")


def dialog_text(page):
    return page.evaluate("() => { const d = document.getElementById('kld-dlg');"
                         " return d ? d.textContent.replace(/\\s+/g, ' ').trim() : ''; }")


def click_dialog(page, which):
    """`which` is 'ok' or 'cancel' — a real click on the real button."""
    page.click("#kld-dlg-ok" if which == "ok" else "#kld-dlg-cancel")
    page.wait_for_timeout(400)


def listed_names(page):
    """The table names the confirm box actually printed."""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#kld-dlg .dc-del-name'))
                 .map(e => e.textContent.trim())""")


def listed_rows(page):
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#kld-dlg .dc-del-rows'))
                 .map(e => e.textContent.trim())""")


def says(text, en, zh):
    """The English half is there and the Chinese half is NOT.

    ⚠️ This headless browser renders the **English** half of every `中文 / English`
    pair — the same reason the sync guard picks buttons by id and never by Chinese
    text. Asserting the Chinese half would fail for a reason that has nothing to do
    with the feature. Asserting the English half *and* the absence of the Chinese
    one is strictly better than picking a side: an unsplit pair reaches the reader
    with both languages at once and this turns red, which is a real bug.
    """
    return en in text and zh not in text


def main():
    os.makedirs(SHOTS, exist_ok=True)
    stamp = int(time.time())
    owner = f"delowner{stamp}@example.test"
    pw = "DeleteProbe123!"
    pg_host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    pg_port = os.environ.get("POSTGRES_PORT", "5432")
    pg_db = os.environ.get("POSTGRES_DB", "klado")
    pg_user = os.environ.get("POSTGRES_USER", "klado")
    pg_pw = os.environ.get("POSTGRES_PASSWORD", "")

    t_a, t_b = f"probe_a_{stamp}", f"probe_b_{stamp}"
    solo = f"probe_solo_{stamp}"
    slugs = []
    try:
        make_user(owner, pw)
        group = dataset_groups.create_group(f"delete-probe-{stamp}", "delete probe", owner)
        slug = group["slug"]
        slugs.append(slug)
        for name, n in ((t_a, 2), (t_b, 3)):
            dataset_groups.write_member_table(
                slug, name, [("id", "INTEGER"), ("item", "TEXT")],
                [{"id": i, "item": f"row{i}"} for i in range(1, n + 1)],
                owner_email=owner)
        print(f"container {slug} with {t_a}, {t_b}")

        # A real schedule on it, pointed at this deployment's own PostgreSQL but
        # DISABLED, so the scheduler has nothing to run and the delete is the only
        # thing under test.
        conn = external_sync.save_connection(owner, {
            "host": pg_host, "port": pg_port, "database": pg_db,
            "user": pg_user, "password": pg_pw, "schema": "public",
            "label": "delete probe conn"})
        external_sync.create_job(owner, {
            "connection_id": conn["id"], "slug": slug, "source_table": "orders",
            "target_table": t_a, "mode": "overwrite",
            "interval_minutes": 60, "enabled": False})
        check("the fixture really has one schedule", len(jobs_for(owner, slug)) == 1)

        # A second container with no schedule at all, for the "must not ask" half.
        solo_group = dataset_groups.create_group(f"delete-solo-{stamp}", "solo", owner)
        solo_slug = solo_group["slug"]
        slugs.append(solo_slug)
        dataset_groups.write_member_table(
            solo_slug, solo, [("id", "INTEGER")], [{"id": 1}], owner_email=owner)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context(viewport={"width": 1440, "height": 1000}).new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            login(page, owner, pw)
            page.click("#nav-tab-datacenter")
            page.wait_for_timeout(4000)
            page.click('[data-dc-section="datasets"]')
            page.wait_for_timeout(1500)

            # ── open the dataset and press Delete ──
            page.click('.dc-proj[data-slug="%s"]' % slug)
            page.wait_for_timeout(1200)
            check("the dataset folder opened",
                  page.locator(".dc-ds-list").count() >= 1,
                  "no .dc-ds-list in the DOM")

            page.click('[data-dc-del="%s"]' % slug)
            page.wait_for_timeout(1500)
            check("a confirm box opened", dialog_open(page))
            page.screenshot(path=os.path.join(SHOTS, "confirm-lists-tables.png"))

            names = listed_names(page)
            rows = listed_rows(page)
            # ⚠️ The positive control first: if this selector matched nothing the
            # assertions below would be satisfied by an empty dialog.
            check("the box printed one line per table", len(names) == 2, f"names={names}")
            check("and both are the real table names",
                  sorted(names) == sorted([t_a, t_b]), f"names={names}")
            check("each line carries its row count",
                  len(rows) == 2 and all("2" in r or "3" in r for r in rows),
                  f"rows={rows}")

            # ⚠️ MEASURED, not grepped. A `.dc-del-list li` rule that exists but loses
            # to a later one leaves the markup perfectly correct and the rows stacked as
            # list-items instead of name-left/count-right; nothing about the DOM says so.
            layout = page.evaluate(
                """() => {
                    const box = document.querySelector('#kld-dlg .dc-del-list');
                    const li = box ? box.querySelector('li') : null;
                    const name = document.querySelector('#kld-dlg .dc-del-name');
                    const bd = document.querySelector('#kld-dlg .kld-dlg-bd-body');
                    if (!box || !li || !name || !bd) return null;
                    const r = name.getBoundingClientRect(), b = bd.getBoundingClientRect();
                    return {display: getComputedStyle(li).display,
                            boxH: box.getBoundingClientRect().height,
                            nameW: r.width, nameH: r.height,
                            inside: r.left >= b.left - 1 && r.right <= b.right + 1
                                    && r.top >= b.top - 1};
                }""")
            check("the rows are really laid out as rows, not a plain list",
                  layout is not None and layout["display"] == "flex", f"layout={layout}")
            check("and they are on screen with the table name inside the box",
                  layout is not None and layout["boxH"] > 10 and layout["nameW"] > 20
                  and layout["nameH"] > 4 and layout["inside"], f"layout={layout}")
            text = dialog_text(page)
            check("it says the tables cannot be recovered",
                  says(text, "cannot be recovered", "不可恢复"), text[:200])
            check("it says the external source database is untouched",
                  says(text, "external source database is untouched", "外部源数据库"),
                  text[:200])
            check("it says the schedules will be asked about afterwards",
                  says(text, "refresh schedule is attached", "定时任务挂在上面"),
                  text[:260])
            check("and it uses the singular for one schedule",
                  "1 refresh schedules" not in text, text[:260])

            # ── cancelling must change nothing (positive control: it was there) ──
            before = api(page, "dataset-groups/" + slug)
            check("the container is there before cancelling", before["status"] == 200,
                  f"status={before['status']}")
            click_dialog(page, "cancel")
            page.wait_for_timeout(1200)
            check("cancel closed the box", not dialog_open(page))
            after = api(page, "dataset-groups/" + slug)
            check("cancel deleted nothing", after["status"] == 200,
                  f"status={after['status']}")
            check("both physical tables are still there",
                  table_exists(t_a) and table_exists(t_b),
                  f"{t_a}={table_exists(t_a)} {t_b}={table_exists(t_b)}")
            check("and so is the schedule", len(jobs_for(owner, slug)) == 1)

            # ── now really delete it ──
            page.click('[data-dc-del="%s"]' % slug)
            page.wait_for_timeout(1500)
            check("the box opened again with the tables",
                  sorted(listed_names(page)) == sorted([t_a, t_b]),
                  f"names={listed_names(page)}")
            click_dialog(page, "ok")
            page.wait_for_timeout(2500)

            check("a SECOND box asks about the schedules", dialog_open(page),
                  "the schedule prompt never appeared")
            text2 = dialog_text(page)
            check("and it names the schedule's target table", t_a in text2, text2[:240])
            check("and how often it ran",
                  says(text2, "every 1 hour", "每 1 小时"), text2[:240])
            check("and that it is one schedule, not '1 schedules'",
                  "1 schedules" not in text2, text2[:240])
            check("and the prose agrees with the singular title",
                  says(text2, "that schedule is not", "这些定时任务还在"), text2[:240])
            page.screenshot(path=os.path.join(SHOTS, "schedules-asked.png"))
            click_dialog(page, "ok")
            page.wait_for_timeout(2000)

            check("the box is closed and the dataset is gone",
                  (not dialog_open(page)) and api(page, "dataset-groups/" + slug)["status"] == 404,
                  f"open={dialog_open(page)} "
                  f"status={api(page, 'dataset-groups/' + slug)['status']}")
            check("the physical tables are gone",
                  not table_exists(t_a) and not table_exists(t_b),
                  f"{t_a}={table_exists(t_a)} {t_b}={table_exists(t_b)}")
            check("their registry rows are gone too",
                  not registry_has(t_a) and not registry_has(t_b))
            check("nothing is left of the schedules", jobs_for(owner, slug) == [],
                  f"jobs={jobs_for(owner, slug)}")
            check("the other account's data did not go with it",
                  table_exists(solo))

                        # ── no schedule ⇒ no second question (negative, paired with the above) ──
            # ⚠️ No "back" click here: deleting the container already closed the folder
            # (`filterDatasets` finds no slug and calls `closeDsFolder`), so the back
            # button is hidden by now and clicking it waits out the whole timeout.
            page.wait_for_selector('.dc-proj[data-slug="%s"]' % solo_slug, timeout=15000)
            page.click('.dc-proj[data-slug="%s"]' % solo_slug)
            page.wait_for_timeout(1200)
            page.click('[data-dc-del="%s"]' % solo_slug)
            page.wait_for_timeout(1500)
            check("the single-table box opened",
                  listed_names(page) == [solo], f"names={listed_names(page)}")
            click_dialog(page, "ok")
            page.wait_for_timeout(2500)
            check("and NO second box appeared — nothing to ask about",
                  not dialog_open(page),
                  f"still open, saying: {dialog_text(page)[:160]}")
            check("the solo dataset is gone",
                  api(page, "dataset-groups/" + solo_slug)["status"] == 404)
            check("its table is gone", not table_exists(solo))

            check("no uncaught error anywhere", not errors, "; ".join(errors[:3]))
            browser.close()
    finally:
        for s in slugs:
            try:
                dataset_groups.delete_group(s, owner)
            except Exception:  # noqa: BLE001 — cleanup must not mask a result
                pass
        with db.get_pg_conn() as conn:
            with conn.cursor() as cur:
                for name in (t_a, t_b, solo):
                    cur.execute(f"DROP TABLE IF EXISTS public.{name}")
                    cur.execute("DELETE FROM public._import_registry WHERE table_name = %s",
                                (name,))
                for s in slugs:
                    cur.execute("DELETE FROM public.external_db_sync_jobs WHERE slug = %s", (s,))
                    cur.execute("DELETE FROM public.external_db_connections "
                                "WHERE label = %s", ("delete probe conn",))
            conn.commit()
        drop_user(owner)
        print("cleaned up")

    print(f"\n{PASS} passed, {FAIL} failed")
    for f in FAILURES:
        print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())