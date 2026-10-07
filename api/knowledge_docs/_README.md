# Knowledge documents — the source of truth

Every `*.md` file in this directory **is** a deployed knowledge document. The files
are baked into the image and reconciled into `public.ai_knowledge_documents` on
startup by `api/services/ai/knowledge_docs.py`. The database is a derived copy.

## Editing the knowledge base

1. Edit the document's file (or add a new one).
2. Bump its `version:` if the content changed in a way readers should notice.
3. Commit, push to `test` (verify), then `main` (production). The reconciler runs at
   container start; a routine restart writes nothing because the **whole document**
   (front matter included) is compared by hash first.

> ⚠️ **A front-matter-only edit is a real change and must deploy** (2026-09-29).
> `version`, `tags` and `document_type` are stored in the database, so any of them
> changing has to trigger a write. This used to be silently broken — the digest
> covered `content` only, so editing tags alone was skipped as "unchanged" and the
> database kept the old revision (15 of 19 edited documents never shipped). If you
> ever touch the digest or the front-matter handling, run
> `python -m unittest tests.test_knowledge_docs` — the "front-matter edits must
> deploy" tests exist precisely to catch that regression.
>
> Changing the digest rule itself re-writes every document once on the next startup
> (`applied` = all, `skipped` = 0); that is expected and observable in
> `knowledge_docs.last_apply`.

There is **no write API**. `GET /api/ai/knowledge/{search,document,documents,manifest}`
are the only knowledge endpoints, so read access can be shared without granting any
ability to write or delete. Removing a file does not delete the document — to delete
one, add its `document_id` to `_retired.txt`.

## File format

```markdown
---
document_id: klado-v2:example-document
title: example-document
document_type: rule
source: klado-workspace
version: 1.2
tags: [example, template]
---

(body — exactly the stored content)
```

- **Required:** `document_id`, `title`, `document_type`, `source`, `version`.
  `document_type` must be one of `rule`, `playbook`, `dictionary`, `brief`,
  `format`, `reference`, `knowledge`; `version` must look like `1.0` / `1.2.0`.
- **Optional:** `tags: [a, b, c]` (compared lower-cased, de-duplicated, max 20 —
  these decide Chinese retrieval ranking, so keep them).
- **Any other `key: value` line** is preserved as an extra metadata key.
- The filename is cosmetic (`:` becomes `__`); `document_id` inside the file is what
  matters. Files starting with `_` are metadata, not documents.
- The body is exactly the stored content, and the store strips surrounding
  whitespace, so it must not rely on leading/trailing blank lines.

## 中文说明书

当前语料只有中文源文件（`*.zh.md`）。既有 `document_id` 的 `:zh` 后缀保留，
不再添加同主题英文 canonical 副本，也不再新增中文“变体”。修改现有源文件，
更新版本并同步 agent skill 的版本记录；部署时协调器发布这些源文件。
API 路径、字段名、代码和文档 id 保持原样。

## Constraints the reconciler enforces

- A malformed file, a duplicate `document_id`, or an empty directory **aborts the
  pass with an error** rather than deploying a partial knowledge base.
- Nothing is ever deleted implicitly.
- `tools/export_knowledge_docs.py` walks the other way (database → files) and
  reproduces these files byte-for-byte; it is for disaster recovery, not publishing.

## How to confirm the reconciler actually ran

It records every pass — ok *and* failed — in `app_settings`:

```sql
SELECT value, updated_at FROM app_settings WHERE key = 'knowledge_docs.last_apply';
```

```json
{"status": "ok", "at": "2026-09-23T04:5x:xx+00:00", "source_dir": "/app/knowledge_docs",
 "total": 27, "applied": 0, "skipped": 27, "deleted": [], "applied_ids": []}
```

This exists because the container log needs an action key this deployment does not
expose, so "the reconciler silently stopped running" would otherwise look exactly
like a healthy deployment. Read it from `POST /api/data-center/query`, which is
SELECT-only:

- `status: "ok"`, `applied: 0`, `skipped: 27` → nothing to do; the database matches
  these files. `updated_at` should be older than the current deploy.
- `applied: [...]` non-empty → those documents were (re)written by this deploy.
- `status: "failed"` → the files were rejected; `error` says why. Nothing was
  written (or deleted), so the database still holds the previous revision.
- `status: "disabled"` → `KNOWLEDGE_DOCS_AUTOAPPLY=0`; the database is **not** being
  reconciled and will drift from these files.