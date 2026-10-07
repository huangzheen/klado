"""Self-hosted knowledge base service layer.

Only ``knowledge_store`` lives here: the embedded AI assistant was retired, so
this package no longer contains agent orchestration, memory, or LLM plumbing.
The knowledge base itself is still served over HTTP by ``routers/knowledge.py``
for external agents.

This is the only knowledge store. The Custom-GPT-only mirror that used to live in
``services/chatgpt_knowledge_store`` was retired on 2026-09-23: it was seeded once
and never updated, so it had drifted into a stale duplicate of this table.
"""