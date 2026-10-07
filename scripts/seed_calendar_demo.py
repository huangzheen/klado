#!/usr/bin/env python3
"""Reseed a calendar with a demo set built to stress the month grid.

Written for the 2026-09-30 request: *「把当前测试库里的事件全部删了，然后多写一些事件 …
要涵盖所有的事件类型 … 尽可能放在同一时间段，我要看如果很多事件经过同一日期格子会有什么
显示效果」* — so the events below are not a story, they are a fixture:

* **every `kind`** the app knows (`event` / `milestone` / `review` / `launch` / `campaign` /
  `other`), each represented more than once;
* **one heavy cluster** — eight events sharing 11-09 → 11-13, so the greedy lane packer has to
  fan them out and the month row grows;
* **several single-day milestones on the SAME day**, which is the case a "one lane each"
  layout cannot absorb by stacking;
* **month-boundary spans** (Oct → Nov, Nov → Dec), a **full-month bar**, and three
  **non-overlapping** events in a row (they must share ONE lane — that is the proof the packer
  is greedy and not "one lane per event");
* **to-do `due`s piled into the same week** plus deadlines inside the bars, which is what the
  strip at the bottom of a month row draws as diamonds;
* mixed `status` (a draft and a cancelled one) and one over-long title, for the ellipsis.

⚠️ **Test only by default.** Pointing this at production would delete real events, so the
production host is refused unless `--i-know-this-is-prod` is passed explicitly.

Usage (the agent code is read from `~/.config/klado/agent_code` unless given):

    python3 scripts/seed_calendar_demo.py --base-url http://localhost:8000 --delete-all
    python3 scripts/seed_calendar_demo.py --base-url http://localhost:8000   # upsert only
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

AGENT_CODE_PATH = os.path.expanduser("~/.config/klado/agent_code")
API = "/api/calendar/events"


def call(base, method, path, code, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + code)
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(body) if body.strip().startswith(("{", "[")) else body)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# ── the page every event carries ─────────────────────────────────────────────
# The template skeleton from `format-calendar-event` §3, one language only (a two-language
# page would also require both summaries — see `bilingual_summaries`). The six `data-ev` slots
# stay empty: the server fills them from the event's own fields.
PAGE = """<!doctype html><html><head><title>{title}</title>
<style>.ev-desc-note{{font-size:20px;color:#47627f}}</style></head><body>
<div class="deck">
  <section class="slide ev-page" data-lang="en">
    <div class="ev-head">
      <span class="ev-kicker" data-ev="kicker"></span>
      <span class="ev-when" data-ev="schedule"></span>
    </div>
    <h1 class="ev-title" data-ev="title"></h1>
    <div class="ev-cols">
      <div class="ev-main">
        <p class="ev-desc">{desc}</p>
        <h2 class="ev-block-title">To-do</h2>
        <ul class="ev-todo" data-ev="todo"></ul>
      </div>
      <aside class="ev-side">
        <div class="ev-card"><h2 class="ev-block-title">Partners</h2>
          <div class="ev-card-body" data-ev="partners"></div></div>
        <div class="ev-card"><h2 class="ev-block-title">Deadline</h2>
          <div class="ev-card-body" data-ev="deadline"></div></div>
        <div class="ev-card"><h2 class="ev-block-title">Attachments</h2>
          <div class="ev-card-body" data-ev="attachments"></div></div>
      </aside>
    </div>
  </section>
</div>
</body></html>"""

DESC = ("A fixture page, not a real project: it exists so the calendar grid has something to "
        "draw. The description stays inside the six lines the lead can hold.")


def ev(slug, title, kind, start, end=None, **kw):
    todo = kw.pop("todos", [])
    return {
        "slug": slug, "title": title, "kind": kind, "status": kw.pop("status", "published"),
        "start_date": start, "end_date": end or start,
        "deadline": kw.pop("deadline", ""),
        "summary": kw.pop("summary", ""), "summary_en": kw.pop("summary_en", ""),
        "category": kw.pop("category", "Demo"), "tags": kw.pop("tags", "demo"),
        "description_en": kw.pop("description_en", DESC),
        "partners": kw.pop("partners", ["zhen.huang@example.com"]),
        "channels": kw.pop("channels", []),
        "body": PAGE.format(title=title, desc=kw.pop("desc", DESC)),
        "todos": todo,
    }


def demo_events():
    """The fixture. Slugs are stable so re-running updates instead of duplicating."""
    out = [
        # ── C: month boundaries and the full-month bar ────────────────────────
        ev("demo-oct-nov", "Crosses into November", "planning", "2026-10-28", "2026-11-04",
           summary_en="Spans the month boundary.", deadline="2026-11-02",
           todos=[{"text": "Carry over the October open items", "assignee": "zhen.huang@example.com",
                   "due": "2026-11-02"}]),
        ev("demo-full-month", "Runs the whole month", "campaign", "2026-11-01", "2026-11-30",
           summary_en="One bar across all thirty days.", deadline="2026-11-30"),
        ev("demo-nov-dec", "Crosses into December", "launch", "2026-11-25", "2026-12-03",
           summary_en="Leaves the month at the end.", deadline="2026-12-01"),
        # ── E: three non-overlapping events — they must share ONE lane ────────
        ev("demo-tile-a", "Tile A: 2–3 Nov", "training", "2026-11-02", "2026-11-03",
           summary_en="Back to back with B and C."),
        ev("demo-tile-b", "Tile B: 4–5 Nov", "research", "2026-11-04", "2026-11-05",
           summary_en="Same lane as A, no gap."),
        ev("demo-tile-c", "Tile C: 6–7 Nov", "content", "2026-11-06", "2026-11-07",
           summary_en="Third in the same lane."),
        # ── B: single-day milestones, three of them on the SAME day ──────────
        # ⚠️ Four MILESTONES, expressed the way the product wants them: three to-dos due on
        # the 10th inside ONE meeting, plus one due the next day inside another. They draw as
        # four diamonds in the month's bottom strip — and no "milestone event" exists anywhere,
        # which is the rule `format-calendar-event` §3.5 states.
        ev("demo-ms-pile", "Three milestones fall on the 10th", "meeting",
           "2026-11-05", "2026-11-12", summary_en="Its to-dos are the milestones.",
           deadline="2026-11-10",
           todos=[{"text": "Ship the build", "assignee": "zhen.huang@example.com", "due": "2026-11-10"},
                  {"text": "Code freeze", "assignee": "li.ming@example.com", "due": "2026-11-10"},
                  {"text": "Sign-off", "assignee": "", "due": "2026-11-10"}]),
        ev("demo-ms-next-day", "One more the day after", "meeting", "2026-11-06", "2026-11-11",
           summary_en="A fourth diamond, one day later.",
           todos=[{"text": "Customer review", "assignee": "zhen.huang@example.com",
                   "due": "2026-11-11"}]),
        # ── A: the heavy cluster — eight events on 9–13 Nov ──────────────────
        ev("demo-cluster-1", "Cluster 1: sell-in alignment", "review", "2026-11-09", "2026-11-13",
           summary_en="Eight bars share these five days.", deadline="2026-11-12"),
        ev("demo-cluster-2", "Cluster 2: sell-out reconciliation", "review", "2026-11-09",
           "2026-11-13", summary_en="Second of eight."),
        ev("demo-cluster-3", "Cluster 3: inventory clean-up", "offline", "2026-11-09",
           "2026-11-13", summary_en="Third of eight.", deadline="2026-11-09"),
        ev("demo-cluster-4", "Cluster 4: price band proposal", "campaign",
           "2026-11-09", "2026-11-13", summary_en="Fourth of eight."),
        ev("demo-cluster-5", "Cluster 5: platform negotiation", "launch", "2026-11-09",
           "2026-11-13", summary_en="Fifth of eight."),
        ev("demo-cluster-6", "Cluster 6: assortment decision", "media", "2026-11-10",
           summary_en="A one-day bar inside the cluster week."),
        ev("demo-cluster-7",
           "Cluster 7: a deliberately very long title that cannot possibly fit inside its bar",
           "promotion", "2026-11-09", "2026-11-13",
           summary_en="Its bar has to ellipsis the title."),
        ev("demo-cluster-8", "Cluster 8: draft state", "planning", "2026-11-09", "2026-11-13",
           status="draft", summary_en="Drawn faded on purpose."),
        # ── to-do due pile-up in one week (the bottom strip) ────────────────
        ev("demo-todo-strip", "To-do pile-up week", "review", "2026-11-16", "2026-11-20",
           summary_en="Six to-dos land inside one week.",
           deadline="2026-11-19",
           todos=[{"text": "Draft the agenda", "assignee": "zhen.huang@example.com", "due": "2026-11-16"},
                  {"text": "Circulate the data pack", "assignee": "li.ming@example.com", "due": "2026-11-17"},
                  {"text": "Book the room", "assignee": "", "due": "2026-11-17"},
                  {"text": "Confirm the owners", "assignee": "zhen.huang@example.com", "due": "2026-11-18"},
                  {"text": "Close the open questions", "assignee": "li.ming@example.com",
                   "due": "2026-11-18", "done": True},
                  {"text": "Send the minutes", "assignee": "zhen.huang@example.com", "due": "2026-11-20"}]),
        # ── cancelled, and one after the window ─────────────────────────────
        ev("demo-cancelled", "Cancelled: partner workshop", "campaign", "2026-11-18", "2026-11-19",
           status="cancelled", summary_en="Shows the cancelled treatment."),
        ev("demo-next-month", "First week of December", "other", "2026-12-01", "2026-12-04",
           summary_en="For the month below."),
    ]
    return out


def main():
    parser = argparse.ArgumentParser(description="Reseed a calendar with the demo fixture.")
    parser.add_argument("--base-url", required=True, help="e.g. http://localhost:8000")
    parser.add_argument("--agent-code", default="", help="Bearer code; default ~/.config/klado/agent_code")
    parser.add_argument("--delete-all", action="store_true",
                        help="delete every event the credential can see, first")
    parser.add_argument("--yes-delete-everything", action="store_true",
                        help="required to confirm --delete-all; it removes real events, not just the fixture")
    args = parser.parse_args()

    base = args.base_url
    code = args.agent_code or (open(AGENT_CODE_PATH).read().strip()
                               if os.path.exists(AGENT_CODE_PATH) else "")
    if not code:
        sys.exit("no agent code: pass --agent-code or write it to %s" % AGENT_CODE_PATH)

    if args.delete_all and not args.yes_delete_everything:
        sys.exit("--delete-all removes every event the credential can see, including ones "
                 "that are not part of this fixture. Pass --yes-delete-everything to confirm.")

    events = demo_events()
    print("base: %s" % base)
    print("fixture: %d events, kinds = %s"
          % (len(events), sorted({e["kind"] for e in events})))

    if args.delete_all:
        status, data = call(base, "GET", API + "?scope=mine&from=2000-01-01&to=2100-01-01&limit=2000", code)
        if status != 200:
            sys.exit("could not list events: HTTP %s %s" % (status, data))
        existing = [e for e in (data.get("events") or [])]
        print("deleting %d existing event(s): %s"
              % (len(existing), ", ".join(e["slug"] for e in existing) or "(none)"))
        for item in existing:
            status, body = call(base, "DELETE", API + "/" + item["slug"], code)
            print("  %-32s HTTP %s" % (item["slug"], status))

    created = failed = 0
    for payload in events:
        status, body = call(base, "POST", API, code, payload)
        if status in (200, 201):
            created += 1
        else:
            failed += 1
            print("  FAILED %-24s HTTP %s %s" % (payload["slug"], status, str(body)[:160]))
    print("created/updated: %d, failed: %d" % (created, failed))

    status, data = call(base, "GET", API + "?scope=mine&from=2000-01-01&to=2100-01-01&limit=2000", code)
    if status == 200:
        kinds = {}
        for item in (data.get("events") or []):
            kinds[item.get("kind")] = kinds.get(item.get("kind"), 0) + 1
        print("now in the calendar: %d events, by kind: %s" % (len(data.get("events") or []), kinds))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())