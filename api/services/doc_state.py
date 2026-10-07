"""Reader-owned state shared by BOTH HTML surfaces — Workspace reports and Dashboards.

⚠️ WHY THIS FILE EXISTS, and why it is not a convenience. A dynamic report and a
dashboard can each carry a filter schema, a reader's saved view, and an annotation
document. Those are three non-trivial pieces of logic: the schema validator REJECTS
rather than repairs, the selection folder degrades a stale pick to its default instead
of raising, and the tolerant reader exists because a 500 here takes the whole filter rail
down for every reader of that document. Duplicating that into a second router is how the
two copies drift and one of them quietly grows a bug.

⚠️ The extraction happened on 2026-10-05, when HTML moved from Workspace to Dashboard.
The functions below are the reports.py originals, moved verbatim — not rewritten. The
only change is the table name in `saved_selection`, which is now a parameter.

The two features keep separate selection tables (`ai_report_filter_selections` /
`ai_dashboard_filter_selections`) on purpose: a reader who set a view on a report must
not find it applied to a dashboard, because those are different documents that merely
happen to be able to carry the same schema.
"""
from __future__ import annotations

import json
import re
import logging
from typing import Any, Optional

from fastapi import HTTPException

from core.i18n import pick, request_lang

_LOG = logging.getLogger("klado.doc_state")

#: The annotation document ceiling. 64 KB, and the reason is a whole-document one:
#: `state_json` is re-served on every open of an interactive page, so an unbounded
#: document is a per-reader cost paid by everyone who opens it.
MAX_STATE_BYTES = 64 * 1024

# ⚠️ The schema limits, moved with the code that enforces them. Their comment moved too:
# these bounds keep ONE document's rail a rail. Twelve controls is already a lot of rail,
# and eighty options fit a scrolling list without becoming a data dump. They live here
# rather than in either router so the two features cannot end up with different ceilings
# and a filter one of them accepts and the other rejects.
MAX_FILTERS = 12
MAX_FILTER_OPTIONS = 80
MAX_FILTER_VALUE_LEN = 64
FILTER_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
FILTER_TYPES = ("select", "multi", "range", "date_range")
VALID_FILTER_WIDTHS = ("full", "half")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def filter_label(raw: object, key: str, request: Request | None = None) -> dict[str, str]:
    """`{"en": …, "zh": …}` from a string or a partial pair. One language is enough."""
    if isinstance(raw, str):
        en, zh = raw.strip(), ""
    elif isinstance(raw, dict):
        en = str(raw.get("en") or "").strip()
        zh = str(raw.get("zh") or "").strip()
    else:
        en = zh = ""
    if not en and not zh:
        raise HTTPException(status_code=400,
                            detail=pick(f"筛选器 '{key}'：需要标签（label.en 和/或 label.zh） / filter '{key}': a label is required (label.en and/or label.zh)", request_lang(request) if request else None))
    en = (en or zh)[:60]
    return {"en": en, "zh": (zh or en)[:60]}


def filter_number(raw: object, what: str, key: str, request: Request | None = None):
    """A finite JSON number, or a 400 naming the field. Booleans are not numbers."""
    if isinstance(raw, bool) or raw is None:
        raise HTTPException(status_code=400, detail=pick(f"筛选器 '{key}'：{what} 必须是数字 / filter '{key}': {what} must be a number", request_lang(request) if request else None))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=pick(f"筛选器 '{key}'：{what} 必须是数字 / filter '{key}': {what} must be a number", request_lang(request) if request else None)) from None
    if value != value or value in (float("inf"), float("-inf")):
        raise HTTPException(status_code=400, detail=pick(f"筛选器 '{key}'：{what} 必须是有限数字 / filter '{key}': {what} must be a finite number", request_lang(request) if request else None))
    return int(value) if value.is_integer() else value


def clean_filter_schema(raw: object, request: Request | None = None) -> dict:
    """
    Validate an agent-authored filter schema; returns `{"version": 1, "filters": [...]}`.

    Contract (published for agents in `klado-v2:format-dynamic-report`):

        {"filters": [
          {"key": "period", "type": "select", "label": {"en": "Period", "zh": "时间段"},
           "options": [{"value": "2026-06", "label": {"en": "Jun 2026", "zh": "2026年6月"}}],
           "default": "2026-06"},
          {"key": "metric", "type": "multi", "label": {"en": "Metric", "zh": "指标"},
           "options": ["gto", "spto"], "default": ["gto"]},
          {"key": "price", "type": "range", "label": "Price band", "min": 0, "max": 20000,
           "step": 500, "unit": "RMB", "default": {"min": 3000, "max": 9000}},
          {"key": "window", "type": "date_range", "label": "Date range",
           "min_date": "2026-01-01", "max_date": "2026-12-31",
           "default": {"from": "2026-06-01", "to": "2026-06-30"}}]}

    ⚠️ **Rejects, never repairs.** A silently adjusted filter would disagree with the
    report's `data-filter-when` blocks, and the symptom the author would see is "some
    slices never show up" — days later, in a document that looks fine. A 400 at publish
    time is much cheaper. Option lists may also be bare strings (`"options": ["gto"]`),
    which is what a hand-written schema usually looks like.
    """
    body = raw if isinstance(raw, dict) else {}
    filters = raw if isinstance(raw, list) else body.get("filters")
    if filters is None:
        filters = []
    if not isinstance(filters, list):
        raise HTTPException(status_code=400, detail=pick("filters 必须是筛选器对象数组 / filters must be a list of filter objects", request_lang(request) if request else None))
    if len(filters) > MAX_FILTERS:
        raise HTTPException(status_code=400,
                            detail=pick(f"一份报告最多声明 {MAX_FILTERS} 个筛选器（收到 {len(filters)} 个） / a report may declare at most {MAX_FILTERS} filters ({len(filters)} sent)", request_lang(request) if request else None))

    out: list[dict] = []
    seen_keys: set[str] = set()
    for entry in filters:
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail=pick("每个筛选器必须是对象 / each filter must be an object", request_lang(request) if request else None))
        key = str(entry.get("key") or "").strip().lower()
        if not FILTER_KEY_RE.match(key):
            raise HTTPException(status_code=400, detail=pick(f"筛选器 key {key!r} 不可用：小写 snake_case，字母开头，仅字母/数字/下划线（≤32 字符），如 'period'、'sales_metric' / filter key {key!r} is not usable: use lower_snake_case, start with a letter, letters/digits/underscore only (≤32 chars) — e.g. 'period', 'sales_metric'", request_lang(request) if request else None))
        if key in seen_keys:
            raise HTTPException(status_code=400, detail=pick(f"筛选器 '{key}' 重复声明 / filter '{key}' is declared twice", request_lang(request) if request else None))
        seen_keys.add(key)
        ftype = str(entry.get("type") or "select").strip().lower()
        if ftype not in FILTER_TYPES:
            raise HTTPException(status_code=400,
                                detail=pick(f"筛选器 '{key}'：type 必须是 {', '.join(FILTER_TYPES)} 之一 / filter '{key}': type must be one of {', '.join(FILTER_TYPES)}", request_lang(request) if request else None))
        item: dict[str, Any] = {"key": key, "type": ftype,
                                "label": filter_label(entry.get("label"), key, request)}
        width = str(entry.get("width") or "").strip().lower()
        if width in VALID_FILTER_WIDTHS:
            item["width"] = width

        if ftype in ("select", "multi"):
            raw_options = entry.get("options")
            if not isinstance(raw_options, list) or not raw_options:
                raise HTTPException(status_code=400,
                                    detail=pick(f"筛选器 '{key}'：options 必须是非空数组 / filter '{key}': options must be a non-empty list", request_lang(request) if request else None))
            if len(raw_options) > MAX_FILTER_OPTIONS:
                raise HTTPException(status_code=400, detail=pick(f"筛选器 '{key}'：{len(raw_options)} 个选项超出侧栏可展示的 {MAX_FILTER_OPTIONS} 个 / filter '{key}': {len(raw_options)} options is more than the {MAX_FILTER_OPTIONS} a sidebar can show", request_lang(request) if request else None))
            options: list[dict] = []
            values: list[str] = []
            for option in raw_options:
                if isinstance(option, dict):
                    value_raw, label_raw = option.get("value"), option.get("label")
                else:
                    value_raw, label_raw = option, None
                value = str(value_raw if value_raw is not None else "").strip()
                if not value or len(value) > MAX_FILTER_VALUE_LEN:
                    raise HTTPException(status_code=400, detail=pick(f"筛选器 '{key}'：每个选项值需 1–{MAX_FILTER_VALUE_LEN} 个字符 / filter '{key}': every option needs a value of 1–{MAX_FILTER_VALUE_LEN} characters", request_lang(request) if request else None))
                if value in values:
                    raise HTTPException(status_code=400,
                                        detail=pick(f"筛选器 '{key}'：选项值 {value!r} 重复 / filter '{key}': option value {value!r} is repeated", request_lang(request) if request else None))
                values.append(value)
                options.append({"value": value,
                                "label": filter_label(label_raw if label_raw is not None else value, key, request)})
            item["options"] = options
            if ftype == "select":
                default = entry.get("default")
                chosen = values[0] if default is None or default == "" else str(default).strip()
                if chosen not in values:
                    raise HTTPException(status_code=400, detail=pick(f"筛选器 '{key}'：默认值 {chosen!r} 不在选项里（默认值必须是真实选项——报告渲染不出的默认值会让卡片打开时一片空白） / filter '{key}': default {chosen!r} is not one of its options (defaults are required to be a real value — a default the report does not render would open the card on a blank page)", request_lang(request) if request else None))
                item["default"] = chosen
            else:
                default = entry.get("default")
                if default is None:
                    picks = values[:1]
                elif isinstance(default, str):
                    picks = [default.strip()]
                elif isinstance(default, list):
                    picks = [str(v).strip() for v in default]
                else:
                    raise HTTPException(status_code=400,
                                        detail=pick(f"筛选器 '{key}'：multi 的默认值必须是选项值数组 / filter '{key}': multi default must be a list of option values", request_lang(request) if request else None))
                unknown = [v for v in picks if v not in values]
                if unknown:
                    raise HTTPException(status_code=400, detail=pick(f"筛选器 '{key}'：默认值 {', '.join(repr(v) for v in unknown)} 不在选项里 / filter '{key}': default values {', '.join(repr(v) for v in unknown)} are not among its options", request_lang(request) if request else None))
                item["default"] = picks

        elif ftype == "range":
            low = filter_number(entry.get("min"), "min", key, request)
            high = filter_number(entry.get("max"), "max", key, request)
            if low >= high:
                raise HTTPException(status_code=400, detail=pick(f"筛选器 '{key}'：min 必须小于 max / filter '{key}': min must be below max", request_lang(request) if request else None))
            step = entry.get("step")
            step_value = filter_number(step, "step", key, request) if step is not None else (round((high - low) / 20) or 1)
            if step_value <= 0:
                raise HTTPException(status_code=400, detail=pick(f"筛选器 '{key}'：step 必须为正数 / filter '{key}': step must be positive", request_lang(request) if request else None))
            item.update({"min": low, "max": high, "step": step_value})
            if entry.get("unit"):
                item["unit"] = str(entry["unit"])[:16]
            default = entry.get("default") if isinstance(entry.get("default"), dict) else {}
            low_pick = filter_number(default["min"], "default.min", key, request) if default.get("min") is not None else low
            high_pick = filter_number(default["max"], "default.max", key, request) if default.get("max") is not None else high
            if low_pick > high_pick:
                raise HTTPException(status_code=400, detail=pick(f"筛选器 '{key}'：default.min 不能超过 default.max / filter '{key}': default.min must not exceed default.max", request_lang(request) if request else None))
            item["default"] = {"min": max(low, min(high, low_pick)), "max": max(low, min(high, high_pick))}

        else:  # date_range — ISO strings throughout, so plain string comparison is the range test
            low = str(entry.get("min_date") or "").strip()
            high = str(entry.get("max_date") or "").strip()
            for value, name in ((low, "min_date"), (high, "max_date")):
                if not ISO_DATE_RE.match(value):
                    raise HTTPException(status_code=400,
                                        detail=pick(f"筛选器 '{key}'：{name} 必须是 YYYY-MM-DD 格式的字符串 / filter '{key}': {name} must be a YYYY-MM-DD string", request_lang(request) if request else None))
            if low > high:
                raise HTTPException(status_code=400, detail=pick(f"筛选器 '{key}'：min_date 不能晚于 max_date / filter '{key}': min_date must be on or before max_date", request_lang(request) if request else None))
            item.update({"min_date": low, "max_date": high})
            default = entry.get("default") if isinstance(entry.get("default"), dict) else {}
            from_pick = str(default.get("from") or low).strip()
            to_pick = str(default.get("to") or high).strip()
            for value, name in ((from_pick, "default.from"), (to_pick, "default.to")):
                if not ISO_DATE_RE.match(value):
                    raise HTTPException(status_code=400,
                                        detail=pick(f"筛选器 '{key}'：{name} 必须是 YYYY-MM-DD 格式的字符串 / filter '{key}': {name} must be a YYYY-MM-DD string", request_lang(request) if request else None))
            if from_pick > to_pick:
                raise HTTPException(status_code=400, detail=pick(f"筛选器 '{key}'：default.from 不能晚于 default.to / filter '{key}': default.from must be on or before default.to", request_lang(request) if request else None))
            item["default"] = {"from": max(low, min(high, from_pick)), "to": max(low, min(high, to_pick))}

        out.append(item)
    return {"version": 1, "filters": out}


def effective_selection(schema: object, raw: object) -> tuple[dict, list[str]]:
    """
    Fold a saved selection onto a schema. Returns `(effective, dropped_keys)`.

    **Never raises.** The schema moves (an agent adds a period, retires a metric) while
    readers' selections sit in the database, so a value that no longer matches must
    degrade to that filter's default instead of breaking the sidebar — the response
    reports what it dropped, and `dropped` is how a caller can tell "your saved view
    was adjusted" from "your saved view was honoured".
    """
    item_list = [f for f in ((schema or {}).get("filters") or []) if isinstance(f, dict)]
    picked = raw if isinstance(raw, dict) else {}
    effective: dict[str, Any] = {}
    dropped: list[str] = []

    for item in item_list:
        key, ftype = item.get("key") or "", item.get("type") or "select"
        present = key in picked and picked.get(key) not in (None, "")
        value = picked.get(key)
        if ftype == "select":
            options = [o.get("value") for o in item.get("options") or []]
            chosen = str(value).strip() if present and not isinstance(value, (dict, list)) else ""
            if chosen in options:
                effective[key] = chosen
                continue
            if present:
                dropped.append(key)
            effective[key] = item.get("default") or (options[0] if options else "")
        elif ftype == "multi":
            options = [o.get("value") for o in item.get("options") or []]
            raw_picks = [value] if isinstance(value, str) else (value if isinstance(value, list) else [])
            picks = [str(v).strip() for v in raw_picks if str(v).strip() in options]
            # ⚠️ Three cases, and they must not be collapsed (the injected runtime applies
            # exactly the same rule — `normMulti` in vendor/report-filters.js):
            #   * an EMPTY list is a real answer ("none of these") and is kept, so the rail
            #     shows every option off and the slices naming a value stay hidden;
            #   * a list with at least one live value keeps exactly those values;
            #   * a list of nothing but retired values falls back to the default, because
            #     the reader never chose "none" — their values moved.
            explicitly_empty = isinstance(value, list) and not value
            if picks or explicitly_empty:
                effective[key] = picks
                if present and len(picks) != len(raw_picks):
                    dropped.append(key)
                continue
            if present:
                dropped.append(key)
            effective[key] = list(item.get("default") or [])
        elif ftype == "range":
            low, high = item.get("min"), item.get("max")
            ok = isinstance(value, dict) and value.get("min") is not None and value.get("max") is not None
            if ok:
                try:
                    low_pick, high_pick = float(value["min"]), float(value["max"])
                except (TypeError, ValueError):
                    ok = False
                else:
                    if low_pick > high_pick:
                        low_pick, high_pick = high_pick, low_pick
                    effective[key] = {"min": max(low, min(high, low_pick)),
                                      "max": max(low, min(high, high_pick))}
            if not ok:
                if present:
                    dropped.append(key)
                effective[key] = dict(item.get("default") or {"min": low, "max": high})
        else:  # date_range
            low, high = item.get("min_date"), item.get("max_date")
            ok = isinstance(value, dict) and ISO_DATE_RE.match(str(value.get("from") or "")) \
                and ISO_DATE_RE.match(str(value.get("to") or ""))
            if ok:
                from_pick, to_pick = str(value["from"]), str(value["to"])
                if from_pick > to_pick:
                    from_pick, to_pick = to_pick, from_pick
                effective[key] = {"from": max(low, min(high, from_pick)),
                                  "to": max(low, min(high, to_pick))}
            if not ok:
                if present:
                    dropped.append(key)
                effective[key] = dict(item.get("default") or {"from": low, "to": high})

    known = {item.get("key") for item in item_list}
    for key in picked:
        if key not in known and key not in dropped:
            dropped.append(str(key))
    return effective, dropped


def load_filter_schema(row: dict) -> dict:
    """Parse a stored schema; a row that was never given filters reads as an empty schema.

    ⚠️ Reading is deliberately TOLERANT (`tolerant_schema`) where writing is strict. A
    stored schema can predate a validation rule, or have been written by hand in the
    database — and a 4xx/5xx here would take the whole filter rail down for every reader of
    that report, which is a far worse outcome than rendering a filter whose label is
    missing. Nothing is silently rewritten on disk: this shapes the value for the read.
    """
    raw = row.get("filters_json") or ""
    if not raw:
        return {"version": 1, "filters": []}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        _LOG.warning("report %s has an unreadable filters_json; treating it as empty", row.get("slug"))
        return {"version": 1, "filters": []}
    if not isinstance(parsed, dict):
        return {"version": 1, "filters": []}
    return tolerant_schema(parsed)


def tolerant_schema(parsed: dict) -> dict:
    """Shape a stored schema so every downstream reader may assume `{"value","label"}`.

    Found the hard way (2026-09-30): a row whose `options` were bare strings
    (`["gto","spto"]` — the form the publish contract explicitly ACCEPTS) made
    `GET /api/reports/{slug}/filters` raise `'str' object has no attribute 'get'` and answer
    500, so the rail showed "Could not load filters" for every reader. The write path always
    stores the expanded form; this keeps a row that did not come through it readable.
    """
    filters = parsed.get("filters")
    if not isinstance(filters, list):
        return {"version": 1, "filters": []}
    out: list[dict] = []
    for entry in filters:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        if not key:
            continue
        item = dict(entry)
        item["key"] = key
        item["type"] = str(entry.get("type") or "select").strip().lower()
        raw_label = entry.get("label")
        if isinstance(raw_label, str):
            item["label"] = {"en": raw_label, "zh": raw_label}
        elif isinstance(raw_label, dict):
            english = str(raw_label.get("en") or raw_label.get("zh") or key).strip()
            item["label"] = {"en": english, "zh": str(raw_label.get("zh") or english).strip()}
        else:
            item["label"] = {"en": key, "zh": key}
        options = entry.get("options")
        if isinstance(options, list):
            cleaned = []
            for option in options:
                if isinstance(option, dict):
                    value = option.get("value")
                    label = option.get("label")
                else:
                    value, label = option, None
                value = str(value if value is not None else "").strip()
                if not value:
                    continue
                if isinstance(label, str):
                    label = {"en": label, "zh": label}
                elif not isinstance(label, dict):
                    label = {"en": value, "zh": value}
                else:
                    english = str(label.get("en") or label.get("zh") or value).strip()
                    label = {"en": english, "zh": str(label.get("zh") or english).strip()}
                cleaned.append({"value": value, "label": label})
            item["options"] = cleaned
        out.append(item)
    return {"version": 1, "filters": out}


def no_json_constant(name: str):
    """
    `NaN` / `Infinity` / `-Infinity` are accepted by Python's json but are NOT valid
    JSON — a reader's `JSON.parse` would choke on the document we handed back. Refuse
    them at write time, where the caller can still fix the payload.
    """
    raise ValueError(f"{name} is not valid JSON")


def state_text(raw: bytes, request: Request | None = None) -> str:
    """
    Validate a POSTed state document and return the text to store, verbatim.

    Verbatim on purpose: the contract is "GET gives back what you POSTed", so we do not
    re-serialise (a re-dump would silently drop duplicate keys and reformat numbers).
    The parse is only a gate — it proves the bytes really are JSON, so the read side can
    serve them as `application/json` without a reader ever seeing invalid JSON.
    """
    if len(raw) > MAX_STATE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=pick(f"state 为 {len(raw)} 字节，上限 {MAX_STATE_BYTES} 字节（64 KB） / state is {len(raw)} bytes; the limit is {MAX_STATE_BYTES} bytes (64 KB)", request_lang(request) if request else None),
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=pick("state 请求体必须是 UTF-8 JSON / state body must be UTF-8 JSON", request_lang(request) if request else None)) from exc
    if not text.strip():
        raise HTTPException(status_code=400, detail=pick("state 请求体为空 —— 请 POST 一个 JSON 文档 / state body is empty — POST a JSON document", request_lang(request) if request else None))
    try:
        json.loads(text, parse_constant=no_json_constant)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=pick(f"state 请求体不是合法 JSON: {exc} / state body is not valid JSON: {exc}", request_lang(request) if request else None)) from exc
    return text


def saved_selection(cur, table: str, doc_id: int, email: str) -> tuple[dict, Optional[str]]:
    """The reader's own saved selection (`{}` when they never touched the rail).

    ⚠️ `table` is a parameter, not a constant, and it is NOT interpolated from user
    input — both callers pass a module-level literal. The two features keep SEPARATE
    selection tables on purpose: a reader who has set a view on a report must not find
    it applied to a dashboard, because the two are different documents that happen to
    be able to carry the same filter schema."""
    cur.execute(
        "SELECT selection_json, updated_at FROM %s "
        "WHERE doc_id = %%s AND user_email = %%s" % table, (doc_id, email))
    row = cur.fetchone()
    if not row:
        return {}, None
    try:
        saved = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        saved = {}
    return (saved if isinstance(saved, dict) else {}), (row[1].isoformat() if row[1] else None)
