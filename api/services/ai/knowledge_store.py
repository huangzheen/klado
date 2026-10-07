"""Self-hosted knowledge retrieval backed by the Klado deployment database.

The AI service and its knowledge documents share the PostgreSQL connection
injected by Yunxiao/Codeup. No external Worker, Supabase project, or second
business-data connection participates in retrieval.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import psycopg2.extras

from core.db import connect_main


class KnowledgeStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class KnowledgeDocument:
    document_id: str
    title: str
    content: str
    document_type: str
    source: str
    version: str = "1.0"
    metadata: dict[str, Any] | None = None


_DOCUMENT_TYPES = {"rule", "playbook", "dictionary", "brief", "format", "reference", "knowledge"}


def _normalise_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    raw = metadata if isinstance(metadata, dict) else {}
    tags = raw.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    clean_tags = [
        re.sub(r"\s+", " ", str(tag).strip().lower())[:80]
        for tag in tags
        if str(tag).strip()
    ]
    return {
        **{key: value for key, value in raw.items() if key != "tags"},
        "tags": list(dict.fromkeys(clean_tags))[:20],
    }


def validate_document(document: KnowledgeDocument) -> list[str]:
    """Validate the portable contract used by managed knowledge documents."""
    errors: list[str] = []
    if not (document.document_id or "").strip():
        errors.append("document_id is required")
    if not (document.title or "").strip():
        errors.append("title is required")
    if not (document.content or "").strip():
        errors.append("content is required")
    if (document.document_type or "knowledge").strip().lower() not in _DOCUMENT_TYPES:
        errors.append("document_type must be one of: " + ", ".join(sorted(_DOCUMENT_TYPES)))
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}(?:[-+][A-Za-z0-9.-]+)?", (document.version or "").strip()):
        errors.append("version must use a semantic version such as 1.0 or 1.2.0")
    return errors


def ensure_schema() -> None:
    """Create the self-hosted knowledge table on the application's main DB."""
    try:
        conn = connect_main()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.ai_knowledge_documents (
                document_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                document_type TEXT NOT NULL DEFAULT 'knowledge',
                source TEXT NOT NULL DEFAULT 'managed',
                version TEXT NOT NULL DEFAULT '1.0',
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ai_knowledge_documents_active
            ON public.ai_knowledge_documents (is_active, updated_at DESC)
            """
        )
        # Existing production deployments predate the knowledge contract.
        # Additive migrations preserve all prior documents and remain safe to
        # run repeatedly during application startup.
        cur.execute("ALTER TABLE public.ai_knowledge_documents ADD COLUMN IF NOT EXISTS version TEXT NOT NULL DEFAULT '1.0'")
        cur.execute("ALTER TABLE public.ai_knowledge_documents ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb")
        conn.commit()
        cur.close()
        conn.close()
    except Exception as exc:
        raise KnowledgeStoreError(f"could not initialize deployment knowledge store: {exc}") from exc


def _clean_document_id(value: str) -> str:
    document_id = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", (value or "").strip()).strip("-")
    if not document_id:
        raise KnowledgeStoreError("缺少 document_id / document_id is required")
    return document_id[:160]


def upsert_document(document: KnowledgeDocument) -> KnowledgeDocument:
    ensure_schema()
    document_id = _clean_document_id(document.document_id)
    title = (document.title or "").strip()
    content = (document.content or "").strip()
    normalized = KnowledgeDocument(
        document_id=document_id,
        title=title,
        content=content,
        document_type=(document.document_type or "knowledge").strip().lower(),
        source=(document.source or "managed").strip(),
        version=(document.version or "1.0").strip(),
        metadata=_normalise_metadata(document.metadata),
    )
    errors = validate_document(normalized)
    if errors:
        raise KnowledgeStoreError("; ".join(errors))
    try:
        conn = connect_main()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            INSERT INTO public.ai_knowledge_documents
                (document_id, title, content, document_type, source, version, metadata, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, now())
            ON CONFLICT (document_id) DO UPDATE SET
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                document_type = EXCLUDED.document_type,
                source = EXCLUDED.source,
                version = EXCLUDED.version,
                metadata = EXCLUDED.metadata,
                is_active = TRUE,
                updated_at = now()
            RETURNING document_id, title, content, document_type, source, version, metadata
            """,
            (normalized.document_id, normalized.title, normalized.content, normalized.document_type,
             normalized.source, normalized.version, psycopg2.extras.Json(normalized.metadata)),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return KnowledgeDocument(**dict(row))
    except Exception as exc:
        raise KnowledgeStoreError(f"could not save knowledge document: {exc}") from exc


def delete_document(document_id: str) -> bool:
    """Permanently remove an unsuitable managed knowledge document."""
    ensure_schema()
    try:
        conn = connect_main()
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM public.ai_knowledge_documents
            WHERE document_id = %s
            """,
            (_clean_document_id(document_id),),
        )
        changed = cur.rowcount > 0
        conn.commit()
        cur.close()
        conn.close()
        return changed
    except Exception as exc:
        raise KnowledgeStoreError(f"could not delete knowledge document: {exc}") from exc


def list_documents(limit: int = 200) -> list[dict[str, Any]]:
    ensure_schema()
    try:
        conn = connect_main()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT document_id, title, document_type, source, version, metadata, is_active, created_at, updated_at,
                   length(content) AS content_length
            FROM public.ai_knowledge_documents
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (max(1, min(limit, 500)),),
        )
        rows = [dict(row) for row in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as exc:
        raise KnowledgeStoreError(f"could not list knowledge documents: {exc}") from exc


# ── tokenisation & matching ──────────────────────────────────────────────────
# Latin tokens must match WHOLE WORDS. The original implementation used
# `str.count`, i.e. substring counting, so "not" matched "nothing"/"cannot" and
# "and" matched "standard"/"brand" — measured on 2026-09-26: `not` hit 19 of 30
# documents, `and` hit 20, while the actually meaningful `callable` hit 2.
# Chinese keeps substring counting on purpose: CJK has no word boundaries and the
# n-grams below are exactly how we approximate them.
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_.+\-/]*")

# Common English words carry no retrieval signal and, because scoring is
# additive, they used to outrank specific terms. Dropping them also frees slots
# under the 16-token budget in _search_tokens. Never applied to CJK.
_EN_STOPWORDS = frozenset("""
a about after all also am an and any are as at be because been before being but by can
could did do does for from had has have he her here him his how i if in into is it its
just me more most my no not of on or our out over own same she should so some such than
that the their them then there these they this to too under up us very was we were what
when where which while who will with would you your
""".split())


# ── field weights + BM25-style saturation / length normalisation ────────────
# Field weights: title > tags > body (unchanged ordering).
_TITLE_WEIGHT, _TAG_WEIGHT, _BODY_WEIGHT = 120, 80, 8

# ── deliverable intent ──────────────────────────────────────────────────────
# A query that asks for a *deliverable* ("做一份 16:9 deck", "给报告配封面",
# "图表用什么图型") must land on the `format-*` spec, not on a document that
# merely mentions the same words. The routing playbook was the measured loser of
# that race (2026-09-29: 10 of 14 Chinese deliverable queries returned it first)
# because it *indexes* every topic, so it accumulates body hits without being
# about any of them — see _ROUTER_BODY_FACTOR below for that half of the fix.
#
# This is intentionally a small curated vocabulary, not "anything with 报告":
# it is checked against the already-tokenised query (CJK 2/3/4-grams included),
# so a data question like "毛利率为什么下降" carries no intent token and is
# untouched.
_PRESENTATION_INTENT_TOKENS = frozenset("""
deck slide slides pptx ppt powerpoint report poster chart charts palette layout
template dashboard export font typography visualization visualize cover bilingual editable
报告 一页纸 排布图 海报 封面 图表 配色 图型 版式 字号 字体 模板 幻灯片 分页 排版
导出 可视化 调色板 物料 看板 演示 双语 封面图 排布
""".split())

# Applied only when the query carries a presentation intent AND the document is
# declared `document_type: format` — i.e. it is a "how to produce X" spec. Sized
# against the 22-query expectation set in tests/test_knowledge_retrieval.py.
_FORMAT_INTENT_BONUS = 60

# A router's body is a table of contents, not content: it names every document
# and every topic, so body matches are weak evidence of being *about* the query.
# `retrieval_role: router` in the document metadata scales its body contribution
# down; title and tags still count in full. Measured 2026-09-29 (22-query
# expectation set, top-1 correctness): 0.35 → best; removing the factor entirely
# put the routing playbook back on top for 10 of 22 queries.
_ROUTER_BODY_FACTOR = 0.35
# Term-frequency saturation (BM25's k1): tf*(k1+1)/(tf+k1) is 1.0 at tf=1 and
# approaches k1+1 instead of growing without bound, so a long document that merely
# *mentions* a term twenty times stops outranking the document that is *about* it.
#
# k1 = 0.8 is measured, not guessed. On the deployed 30-document corpus with a
# 12-query expectation set (top-1 correctness):
#     raw tf (before)   6/12      k1=0.8   9/12  ← chosen
#     k1=1.2            8/12      k1=2.0   8/12
#     min(tf,2/3/5)     7/8/8     log1p 7   sqrt 6
#
# ⚠️ LENGTH NORMALISATION WAS TRIED AND REJECTED — do not add it back without
# re-measuring. BM25's length norm (1/(1+b*(len/avg-1))) made every single
# configuration worse (top-1 correctness dropped to 3-5/12, top-3 from 10 to 5-8):
# the authoritative documents in this corpus ARE the long ones (the 24 KB data
# dictionary, the 18 KB report spec), while the short ones are templates. Length
# is therefore not a proxy for "prose padding" here.
_TF_K1 = 0.8


def _saturate(term_frequency: int) -> float:
    if term_frequency <= 0:
        return 0.0
    return term_frequency * (_TF_K1 + 1) / (term_frequency + _TF_K1)


def _boundary_phrase_count(phrase: str, text_lower: str) -> int:
    """Boundary-anchored occurrence count for a multi-word ascii phrase."""
    return len(re.findall(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text_lower))


def _count_occurrences(token: str, text_lower: str, words: Counter | None = None) -> int:
    """Occurrences of `token` in `text_lower`, respecting the strategy above."""
    if not token:
        return 0
    if token.isascii():
        if " " in token:                      # whole-phrase token → boundary-anchored
            found = _boundary_phrase_count(token, text_lower)
        elif len(token) >= 3 and words is not None:
            found = words[token]              # exact word only
        else:
            found = text_lower.count(token)   # very short tokens (jd, pa) stay permissive
        if found or "-" not in token:
            return found
        # Hyphenated document ids are a single word to _WORD_RE (`format-report-html`),
        # so the natural prefix-stripped query form is an exact-word miss: measured
        # 2026-09-29, `report-html` and `meeting-one-pager` scored 0 against the whole
        # 33-document corpus while `format-report-html` scored 277. Retry once with the
        # hyphen read as a separator, on both the token and the text.
        return _boundary_phrase_count(token.replace("-", " "), text_lower.replace("-", " "))
    return text_lower.count(token)            # CJK / mixed → substring


def _word_counts(text_lower: str) -> Counter:
    return Counter(_WORD_RE.findall(text_lower))


def _search_tokens(query: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", query.lower()).strip()
    tokens = [normalized] if normalized else []
    tokens.extend(token for token in re.split(r"[^\w\u4e00-\u9fff-]+", normalized) if len(token) > 1)
    # A mixed-script query glues the Latin term to the CJK text ("SPTO是什么意思",
    # "SPTO2026年销量"), and the split above cannot separate them because Latin
    # and CJK are both \w. Without this the ONE discriminating token ("spto") never
    # exists: measured 2026-09-29, "SPTO是什么意思" ranked the data dictionary BELOW
    # `rule-performance-analysis`, which merely uses the term in prose. Latin runs
    # go in before the CJK n-grams so they survive the 16-token budget.
    tokens.extend(match.group(0) for match in _WORD_RE.finditer(normalized) if len(match.group(0)) > 1)
    # Chinese prompts are commonly entered without spaces. Add bounded CJK
    # n-grams so “检索今年更新情况” can retrieve a document titled or containing
    # “检索更新”, without needing an external tokenizer service.
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        for width in (2, 3, 4):
            tokens.extend(segment[index:index + width] for index in range(len(segment) - width + 1))
    # Drop single English stopwords ("the", "not", …). A multi-word token such as
    # "the report" is kept — only bare stopwords are noise.
    kept = []
    for token in dict.fromkeys(tokens):
        if token.isascii() and " " not in token and token in _EN_STOPWORDS:
            continue
        kept.append(token)
    return kept[:16]


def _score(query: str, title: str, content: str, *, document_type: str = "knowledge",
           metadata: dict[str, Any] | None = None,
           idf: dict[str, float] | None = None) -> int:
    """Rank by structured contract signals plus lexical/CJK recall.

    This is intentionally not labelled vector retrieval: it is a deterministic
    hybrid of metadata, title and full-text retrieval that can be audited in
    the application database today.  Embeddings can be added as another signal
    without changing the document or API contract.
    """
    title_lower = title.lower()
    content_lower = content.lower()
    normalised_metadata = _normalise_metadata(metadata)
    tags = " ".join(str(tag).lower() for tag in normalised_metadata.get("tags", []))
    title_words, content_words, tag_words = _word_counts(title_lower), _word_counts(content_lower), _word_counts(tags)
    body_weight = _BODY_WEIGHT
    if str(normalised_metadata.get("retrieval_role") or "").strip().lower() == "router":
        body_weight = max(1, round(_BODY_WEIGHT * _ROUTER_BODY_FACTOR))
    tokens = _search_tokens(query)
    wants_deliverable = any(token in _PRESENTATION_INTENT_TOKENS for token in tokens)
    score = 0
    for token in tokens:
        # IDF (optional, supplied by search()): a token that appears in almost every
        # document carries little information, so its contribution is scaled down by
        # log(1 + N/(1+df)). Without `idf` every weight is unchanged (1.0), which keeps
        # _score deterministic and unit-testable on its own.
        weight = 1.0 if idf is None else float(idf.get(token, 1.0))
        score += round(_saturate(_count_occurrences(token, title_lower, title_words)) * _TITLE_WEIGHT * weight)
        score += round(_saturate(_count_occurrences(token, tags, tag_words)) * _TAG_WEIGHT * weight)
        score += round(_saturate(_count_occurrences(token, content_lower, content_words)) * body_weight * weight)
        if token in {"rule", "规则", "playbook", "流程"} and document_type in {"rule", "playbook"}:
            score += 30
    if wants_deliverable and document_type == "format":
        score += _FORMAT_INTENT_BONUS
    return score


def _managed_documents() -> list[KnowledgeDocument]:
    ensure_schema()
    try:
        conn = connect_main()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT document_id, title, content, document_type, source, version, metadata
            FROM public.ai_knowledge_documents
            WHERE is_active = TRUE
            """
        )
        rows = [KnowledgeDocument(**dict(row)) for row in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as exc:
        raise KnowledgeStoreError(f"could not read managed knowledge documents: {exc}") from exc


def _all_documents() -> list[KnowledgeDocument]:
    """Return the managed self-hosted knowledge set."""
    return _managed_documents()


def rank(query: str, documents: list[KnowledgeDocument], *, top_k: int = 5) -> list[KnowledgeDocument]:
    """Rank an explicit document set. The retrieval half of `search`, with no I/O.

    Split out on 2026-09-29 so the ranking can be regression-tested against the
    version-controlled corpus (``api/knowledge_docs/*.md``) without a database:
    every change to the weights, the tokeniser or a document's tags is otherwise
    unfalsifiable. `search` is this function over the deployed documents.
    """
    query = (query or "").strip()
    if not query:
        raise KnowledgeStoreError("缺少查询词 / query is required")
    tokens = _search_tokens(query)
    idf: dict[str, float] = {}
    if documents and tokens:
        prepared = []
        for document in documents:
            title_lower, content_lower = document.title.lower(), document.content.lower()
            prepared.append((document, title_lower, _word_counts(title_lower),
                             content_lower, _word_counts(content_lower)))
        document_frequency: Counter = Counter()
        for token in tokens:
            for _, title_lower, title_words, content_lower, content_words in prepared:
                if _count_occurrences(token, title_lower, title_words) or \
                        _count_occurrences(token, content_lower, content_words):
                    document_frequency[token] += 1
        total = len(documents)
        idf = {token: math.log(1 + total / (1 + document_frequency[token])) for token in tokens}
    ranked = [
        (_score(query, document.title, document.content,
                document_type=document.document_type, metadata=document.metadata, idf=idf or None), document)
        for document in documents
    ]
    return [document for score, document in sorted(ranked, key=lambda item: item[0], reverse=True) if score > 0][:max(1, min(top_k, 20))]


def search(query: str, *, top_k: int = 5, extra: list[KnowledgeDocument] | None = None,
           include_curated: bool = True) -> list[KnowledgeDocument]:
    """Rank the deployed corpus, optionally merged with caller-supplied documents.

    ``extra`` exists for the **business knowledge base** (``services/ai/
    business_knowledge.py``): its pages must be scored by this exact
    implementation, but they are permission-filtered per account and therefore
    cannot live in ``_all_documents()``, which is the same for everyone. The
    filtering happens in the caller, in SQL, *before* ranking.

    ``include_curated=False`` searches only the supplied documents (``scope=mine``
    and friends) so a scoped search cannot accidentally return the manual.
    """
    query = (query or "").strip()
    if not query:
        raise KnowledgeStoreError("缺少查询词 / query is required")
    pool: list[KnowledgeDocument] = list(_all_documents()) if include_curated else []
    if extra:
        pool.extend(extra)
    return rank(query, pool, top_k=top_k)


def manifest(limit: int = 500) -> dict[str, Any]:
    """Return an auditable manifest for the managed deployed knowledge set."""
    documents = list_documents(limit)
    issues: list[dict[str, str]] = []
    seen_titles: dict[str, str] = {}
    for item in documents:
        title_key = re.sub(r"\s+", " ", str(item.get("title") or "").strip().lower())
        if title_key in seen_titles:
            issues.append({"document_id": str(item.get("document_id")), "issue": f"duplicate title with {seen_titles[title_key]}"})
        else:
            seen_titles[title_key] = str(item.get("document_id"))
        version = str(item.get("version") or "")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}(?:[-+][A-Za-z0-9.-]+)?", version):
            issues.append({"document_id": str(item.get("document_id")), "issue": "missing or invalid version"})
    return {"documents": documents, "valid": not issues, "issues": issues}


def get_document(identifier: str) -> KnowledgeDocument | None:
    needle = (identifier or "").strip()
    if not needle:
        raise KnowledgeStoreError("document identifier is required")
    for document in _all_documents():
        if document.document_id == needle or document.title.lower() == needle.lower():
            return document
    return None
