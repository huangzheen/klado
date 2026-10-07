"""The one SQL gateway, shared by every module that lets a document read data.

Data Center isolation is table-level: each dataset is its own table stamped with an
owner, so a raw SELECT has to be checked against what the caller may see or it becomes
a way to read another account's table by name. That check lived inside
`routers/data_center.py`, which meant a second module that wanted to let a document
query data would have had to either import a router (wrong layer) or copy the guard
(a second allow-list that drifts). So the guard moved here and both callers use it.

**There must be exactly one implementation of this rule.** A dashboard that can read
data does not get a wider grant than its reader already has; it asks the same question
of the same function. Every account, including operators, uses the same dataset visibility boundary.

The write barrier is not any check in this file — it is `SET TRANSACTION READ ONLY`
inside `services.data_center_db.query_pg()`. The statement-shape checks below exist to
give a readable error before the database has to say no.
"""
from __future__ import annotations

import re

from fastapi import HTTPException

from core.i18n import pick, request_lang
from services import data_center_db as db


def _lang(request):
    return request_lang(request) if request else None


# ── statement shape ────────────────────────────────────────────────────────────

def leading_keyword(sql_text: str) -> str:
    """The first SQL keyword, skipping leading whitespace and comments.

    The comments matter: `-- 口径说明\nSELECT …` is valid SQL that a plain
    `startswith("SELECT")` test rejects without ever reading the statement.

    Written as a loop rather than one regex: comment-then-whitespace is the awkward part
    (a regex needs whitespace on both sides of every comment, and getting that wrong stops
    after the first one), and a loop cannot backtrack.
    """
    body = (sql_text or "").lstrip()
    while body.startswith("--") or body.startswith("/*"):
        if body.startswith("--"):
            nl = body.find("\n")
            body = "" if nl < 0 else body[nl + 1:].lstrip()
        else:
            end = body.find("*/")
            if end < 0:
                return ""          # unterminated block comment → nothing readable
            body = body[end + 2:].lstrip()
    m = re.match(r"[A-Za-z]+", body)
    return m.group(0).upper() if m else ""


# A relation name in a statement: a plain or double-quoted identifier. Every FROM/JOIN
# target in the token stream is checked against what the caller may see.
_IDENT_RE = re.compile(r'"(?:""|[^"])+"|[^\W\d][\w$]*')
_CTE_RE = re.compile(r"(?:\bwith\b|,)\s*([^\W\d][\w$]*)\s+as\s*\(", re.I)
_TOKEN_RE = re.compile(r'"(?:""|[^"])+"|[^\W\d][\w$]*|\S')
SYSTEM_SCHEMAS = ("information_schema", "pg_catalog")
# Words that end a relation list rather than naming an alias.
_NON_ALIAS_KEYWORDS = {
    "SELECT", "FROM", "WHERE", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS",
    "NATURAL", "OUTER", "ON", "USING", "GROUP", "ORDER", "LIMIT", "OFFSET", "HAVING",
    "UNION", "EXCEPT", "INTERSECT", "WINDOW", "RETURNING", "AND", "OR", "NOT", "IS",
    "NULL", "TRUE", "FALSE", "AS", "WITH", "VALUES", "DISTINCT", "ALL",
}


def _sql_code(text: str) -> str:
    """Strip PostgreSQL comments and literal bodies, preserving quoted identifiers.

    Commented comma joins and nested comments must not hide actual relations.
    E strings and dollar quotes are literals as well, not a second SQL program.
    """
    out, i = [], 0
    while i < len(text):
        if text.startswith('--', i):
            end = text.find('\n', i)
            i = len(text) if end < 0 else end
            out.append(' ')
        elif text.startswith('/*', i):
            depth, i = 1, i + 2
            while i < len(text) and depth:
                if text.startswith('/*', i): depth, i = depth + 1, i + 2
                elif text.startswith('*/', i): depth, i = depth - 1, i + 2
                else: i += 1
            out.append(' ')
        elif text[i] == "'":
            escape = i > 0 and text[i-1] in 'eE' and (i < 2 or not text[i-2].isalnum())
            i += 1
            while i < len(text):
                if escape and text[i] == "\\": i += 2
                elif text.startswith("''", i): i += 2
                elif text[i] == "'": i += 1; break
                else: i += 1
            out.append("''")
        elif text[i] == '$' and (tag := re.match(r'\$(?:[A-Za-z_][\w]*)?\$', text[i:])):
            delimiter = tag.group()
            end = text.find(delimiter, i + len(delimiter))
            i = len(text) if end < 0 else end + len(delimiter)
            out.append("''")
        elif text[i] == '"':
            start, i = i, i + 1
            while i < len(text):
                if text.startswith('""', i): i += 2
                elif text[i] == '"': i += 1; break
                else: i += 1
            out.append(text[start:i])
        else:
            out.append(text[i]); i += 1
    return ''.join(out)


def referenced_relations(sql_text: str) -> set[str]:
    """Relation names a read-only statement reads from, for the visibility check.

    Scans the token stream rather than regexing clauses: a clause-shaped regex either
    stops too early (missing the table after a JOIN — a silent hole) or runs past the
    JOIN into the next clause. Walking FROM/JOIN by hand also picks up the relation
    inside a subquery, because the scan never consumes a `(...)` body.

    String literals are removed first, so a `'from secret'` inside a value cannot
    invent a relation. CTE names are skipped — `WITH x AS (…) … FROM x` reads the CTE,
    not a table called `x` — while the CTE's own body is still scanned.
    """
    stripped = _sql_code(sql_text or "")
    ctes = {m.group(1).lower() for m in _CTE_RE.finditer(stripped)}
    toks = _TOKEN_RE.findall(stripped)
    out: set[str] = set()
    want = False
    skipped_until = -1          # tokens already consumed as a relation / its alias
    for i, tok in enumerate(toks):
        if i <= skipped_until:
            continue
        up = tok.upper()
        if up in ("FROM", "JOIN", "TABLE"):
            want = True
            continue
        if not want:
            continue
        if tok == ",":
            continue          # a comma between relations: stay in the list
        if not _IDENT_RE.fullmatch(tok):
            want = False          # `(`, an operator, a keyword: the relation list ended
            continue
        name = tok.strip('"')
        last = i
        # a qualified name is three tokens: ident . ident
        if (i + 2 < len(toks) and toks[i + 1] == "."
                and _IDENT_RE.fullmatch(toks[i + 2])):
            name += "." + toks[i + 2].strip('"')
            last = i + 2
        out.add(name)
        # Skip an alias, if one is there: `FROM sales s, other o`. An identifier that is
        # not a SQL keyword is an alias; `AS` may or may not precede it. Getting this
        # wrong is a hole either way — miss the alias and the comma list stops early
        # (missing `other`), so the keyword list below has to be the closing tokens.
        nxt = last + 1
        if nxt < len(toks) and toks[nxt].upper() == "AS":
            nxt += 1
        if nxt < len(toks) and _IDENT_RE.fullmatch(toks[nxt]) and toks[nxt].upper() not in _NON_ALIAS_KEYWORDS:
            nxt += 1
        # a comma continues the list; anything else ends it. `nxt` is processed by the
        # caller (the comma branch below); everything between is consumed for good.
        skipped_until = nxt - 1
        want = (nxt < len(toks) and toks[nxt] == ",")
    return {n for n in out if n.lower() not in ctes}


def assert_query_allowed(request, sql_text: str, viewer_email: str, admin: bool) -> None:
    """Refuse a raw query that reaches outside the caller's own datasets.

    Data Center isolation lives at the *table* level (each dataset is its own table,
    stamped with an owner), so the raw SELECT gateway has to apply the same rule the
    dataset endpoints do — otherwise it is a way to read every other account's table
    by name. Administrators use the same boundary; system catalogs are not dataset resources.
    """
    # SELECT can execute server-side functions that themselves read arbitrary tables.
    # Only analytical built-ins are exposed; a read-only transaction alone is not isolation.
    safe = set("count sum avg min max abs round floor ceil ceiling power sqrt mod greatest least coalesce nullif concat concat_ws lower upper length char_length substring substr trim btrim ltrim rtrim replace regexp_replace split_part position strpos to_char to_date to_timestamp date_trunc date_part extract make_date now current_date array_agg string_agg json_agg jsonb_agg json_build_object jsonb_build_object row_number rank dense_rank lag lead first_value last_value ntile percentile_cont percentile_disc generate_series unnest cast numeric decimal integer bigint text varchar timestamp date boolean in as over filter values select with exists grouping rollup cube distinct".split())
    clean = _sql_code(sql_text or "")
    if re.search(r'\bU\s*&\s*"', clean, re.I):
        raise HTTPException(403, pick("请使用普通 SQL 标识符 / Use ordinary SQL identifiers", _lang(request)))
    for qualified in re.finditer(r'(?:"([^"\n]+)"|([^\W\d][\w$]*))\s*\.\s*(?:"[^"\n]+"|[^\W\d][\w$]*)\s*\(', clean):
        if (qualified.group(1) or qualified.group(2)).lower() != 'pg_catalog':
            raise HTTPException(403, pick("不允许调用自定义数据库函数 / Custom database functions are not allowed", _lang(request)))
    for match in re.finditer(r'(?:"([^"\n]+)"|([^\W\d][\w$]*))\s*\(', clean):
        name = (match.group(1) or match.group(2)).lower()
        if name not in safe:
            raise HTTPException(403, pick("此查询调用了不允许的函数 / This query calls a function that is not allowed", _lang(request)))
    visible = {d["table_name"].lower() for d in db.list_datasets(viewer_email, admin=False)}
    for relation in referenced_relations(sql_text):
        schema, _, table = relation.rpartition(".")
        base = (table or relation).lower()
        if (schema and schema.lower() != "public") or base not in visible:
            raise HTTPException(
                403,
                pick(f"无权查询 {relation}：只能查询你自己的数据集"
                     f" / not your dataset — use GET /api/data-center/datasets",
                     _lang(request)),
            )


def normalise_statement(sql_text: str) -> str:
    """Strip one optional trailing semicolon; return the text to run.

    Callers check for an interior `;` themselves so the error can name the reason.
    """
    text = (sql_text or "").strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    return text


def run_scoped_query(request, sql_text: str, *, viewer_email: str, admin: bool,
                     max_rows: int | None = None) -> dict:
    """The whole gateway: one read-only statement the caller is entitled to run.

    ⚠️ `WITH ... SELECT` is allowed as of 2026-09-27: a CTE is ordinary read-only SQL,
    and the old `startswith("SELECT")` test forced agents to rewrite every CTE into
    nested subqueries (an agent's own handover doc records that workaround). The write
    barrier is NOT this text check — it is `SET TRANSACTION READ ONLY` inside
    `db.query_pg()`, which no prefix can talk its way past. These checks only keep the
    statement count at one and give a readable error before the DB has to say no.
    """
    from psycopg2 import errors as pg_errors

    sql_text = normalise_statement(sql_text)
    if ";" in sql_text:
        raise HTTPException(400, pick("只允许一条 SELECT 查询 / only one SELECT statement is allowed", _lang(request)))
    head = leading_keyword(sql_text)
    if head not in ("SELECT", "WITH"):
        raise HTTPException(400, pick("只允许 SELECT 或 WITH ... SELECT 查询 / only SELECT or WITH ... SELECT queries are allowed", _lang(request)))
    assert_query_allowed(request, sql_text, viewer_email, admin)
    try:
        return db.query_pg(sql_text, max_rows=max_rows)
    except HTTPException:
        raise
    except pg_errors.ReadOnlySqlTransaction as e:
        # Reached when the statement *looked* read-only (e.g. `WITH x AS (DELETE …)`) and
        # the database refused it. A client mistake, not a server fault.
        raise HTTPException(400, pick(f"只允许只读查询（READ ONLY 事务拒绝了它）：{e} / read-only queries only (the READ ONLY transaction rejected it): {e}", _lang(request)))
    except pg_errors.InsufficientPrivilege as e:
        # 42501. Deliberately NOT a 400: "your SQL is malformed" would confirm that the
        # object named in the query exists, and confirming existence is exactly what this
        # module exists to refuse. 403 with a message that says nothing about the object.
        raise HTTPException(403, pick("查询被拒绝 / the query was refused", _lang(request)))
    except (pg_errors.ProgrammingError, pg_errors.DataError) as e:
        # SQLSTATE classes 42 (syntax error / undefined function / undefined column /
        # undefined table) and 22 (data exception). Every one of them is the caller's own
        # statement being wrong — `ROUND(float, 2)` does not exist in PostgreSQL, a
        # misspelled column, a type mismatch. The database is fine.
        #
        # ⚠️ These used to fall through to the generic handler below and answer **500**,
        # which says "the server broke" about a typo in the caller's SQL. The ReadOnly
        # branch directly above already states the rule this generalises: *a client
        # mistake is not a server fault.* Ordering matters — `ReadOnlySqlTransaction` is
        # an `InternalError` subclass and `InsufficientPrivilege` is a `ProgrammingError`
        # subclass, so both have to be caught before these two.
        raise HTTPException(400, pick(f"查询无法执行：{e} / the query could not be executed: {e}", _lang(request)))
    except Exception as e:
        # What is left is the database or the connection failing, which IS a server fault:
        # `OperationalError`, `InterfaceError`, `IoError`, and the non-read-only
        # `InternalError`s.
        raise HTTPException(500, pick(str(e), _lang(request)))
