#!/usr/bin/env python3
"""QUARANTINED — the thing this file tested no longer exists. (2026-10-05)

    .venv312/bin/python api/tests/verify_report_bilingual_summary_ui.py
    → exits 2 with this notice, on purpose.

What it used to assert
----------------------
A report card's cover veil (`.rpt-cover-veil` / `.rpt-cover-sum`) drew the
report's summary, and a **bilingual** report (one carrying `summary_en` +
`summary_zh`) drew **both** lines, English first, each clamped to 2. A
single-language report drew its one line; a report with no summary drew no veil
at all. The three fixtures below were the spec for exactly that.

Why it is quarantined, not repaired
-----------------------------------
⚠️ **The subject is gone, not the selector.** The Workspace stopped rendering
cards when it became 项目 → 报告: the wall draws projects, a project draws
`#wr-list` of `.wr-row` buttons, and a row is title + optional file size +
optional status badge. Measured, not assumed:

  * `#rpt-grid` / `.rpt-card` / `.rpt-more` — zero occurrences in `index.html`
    (`.rpt-card` survives only in the **Dashboard** wall, which is a different page)
  * `.rpt-cover-veil` / `.rpt-cover-sum` — zero occurrences in markup, anywhere
  * `summary` inside the report row's own markup — **0**

So this is not a selector to update. At the time of writing, **a report's summary
was displayed nowhere in the product** — not on the row, not in the viewer, which
shows a title and nothing else. That had happened as a side effect of the
card→row refactor, and this file was left dead on a `wait_for_selector` timeout
rather than noticed.

What changed on 2026-10-05, and what did not
-------------------------------------------
⚠️ **The bilingual summaries are back — in the row's hover card (`#wr-tip`), not on
the row.** They did not come back to where this file looked for them, and they did
not come back as the "A" option below (a second line under the title); the decision
was the third shape: the row stays one line, and hovering it shows the full title,
**both** summary lines (English first, matching what the knowledge-base hover card
does), the document's full filename, and the submitter and date. Measured on
production that day: 8/8 reports are bilingual, so both lines are populated and
the card is never half-empty in practice.

⇒ **This file stays quarantined, and its coverage is not resurrected here.** The
subject it tested — a cover veil drawn on a report card — is still gone, and
re-pointing the three fixtures at the hover card would be testing a different
feature under this file's name. The bilingual-summaries-in-a-card assertions live in
``verify_wr_row_tip_ui.py`` instead, which also owns the fixtures for it.

Nothing was deleted. The assertion bodies and all three fixtures are in git
history at `537a825`, and they are the ready spec if a cover veil ever comes back.

The decision this needed (resolved 2026-10-05, option C)
-------------------------------------------------------
Restoring it was a design call, not a mechanical repair. What was actually chosen:

  **A. Put the summary back on the row** — a second line under the title. Measured
     and rejected: the row is 32px and the list is 8+ rows in a 267px rail, so two
     lines of summary took it to 63px per row and 256px → ~504px for the list.
  **B. Accept that reports have no summary in the list** — not chosen; the
     summaries were genuinely useful and the API already carried them.
  **C. A hover card beside the row** — chosen. Costs no vertical space, the data
     was already in the list payload (`_SELECT_COLS` has carried `summary_en` /
     `summary_zh` all along), and the card is where the knowledge base and the
     calendar already put this same information.

A test that asserts the summary is *absent* would be a bad substitute for any of
the three: it can only pass one way, and it would break on the improvement. That is
why this exits 2 instead of quietly going green.
"""
import sys

NOTICE = __doc__

if __name__ == "__main__":
    print(NOTICE)
    print("=" * 72)
    print("Exiting 2: this is a missing-coverage signal, not a UI failure.")
    print("Do not read it as 'the bilingual summary is broken on screen' —")
    print("nothing renders it at all, and that needs a product decision (A or B).")
    sys.exit(2)
