"""The Agent Skill page: the package an operator hands to an outside agent, as documents.

Read-only, and reachable while the console is read-only — the same argument as the
项目说明书 page. This one answers a different question though, and the two must not be
confused:

* **项目说明书** (`/manual`) is what the agent *retrieves at runtime* over HTTP — the
  `ai_knowledge_documents` corpus, 19 Markdown files, a database table.
* **Agent Skill** (this page) is the *package* the agent is installed with: `SKILL.md`
  plus its references, shipped as `agent.zip` and handed over as a file. No HTTP, no
  retrieval, no database.

They are related — `SKILL.md` points at the manual, and both describe the same system —
but they have different lifecycles, different delivery and different readers, and a page
that showed the package while claiming to show the manual would be worse than no page.

⚠️ A missing package is a **state, not an error**: on a checkout where nobody ran
`scripts/build_agent_skill.sh` the page says so and names the path, rather than 500ing.
An operator asking "what would the agent get" deserves an answer in either case.

No write route
--------------
The package is a build artifact of `agent_skill/klado/`. Editing it from the console
would put `agent.zip` out of step with its source, which is the one divergence
`api/tests/test_agent_skill_package.py` exists to catch.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from admin_console import access
from klado_shared import agent_skill
from klado_shared.i18n import pick, request_lang

router = APIRouter(prefix="/api/admin-console", tags=["Agent Skill"])


@router.get("/agent-skill")
async def agent_skill_index(
    request: Request,
    search: str = Query("", max_length=200, description="文件名或路径的���由文本"),
):
    """The package's entries plus the facts the page header shows.

    One request for both, so the header cannot show a count that disagrees with the list
    — the same reason `/manual` returns them together.
    """
    access.require_operator(request)
    missing = agent_skill.missing()
    if missing is not None:
        return {"search": search, "entries": [], "summary": missing}
    return {
        "search": search,
        "entries": agent_skill.list_entries(search=search),
        "summary": agent_skill.summary(),
    }


@router.get("/agent-skill/{entry:path}")
async def agent_skill_entry(request: Request, entry: str):
    """One file's content.

    Separate from the list because the list omits bodies: `SKILL.md` alone is ~66 KB, so
    this is the only route where a large payload matters.

    ⚠️ `{entry:path}`, not `{entry}`. Six of the seven entries live in sub-directories
    (``references/dashboards/1-exec-overview.html``), and a single-segment parameter gives
    404 on every one of them. The slash the browser sends as ``%2F`` is decoded to a real
    ``/`` before routing, so the path arrives as three segments and no one-segment route
    can match it — the list worked and every file below the root 404'd, which looks
    exactly like a broken page rather than a wrong route.

    The value is still only ever used as a lookup key against the package's own listing
    (`get_entry`); it is never joined onto a filesystem path.
    """
    access.require_operator(request)
    lang = request_lang(request)
    if agent_skill.missing() is not None:
        raise HTTPException(status_code=409, detail=pick(
            "技能包尚未构建 / the agent skill package has not been built", lang))
    try:
        found = agent_skill.get_entry(entry)
    except agent_skill.PackageMissing as exc:
        raise HTTPException(status_code=404, detail=pick(
            f"技能包条目读取失败：{exc} / could not read the package entry", lang))
    if found is None:
        raise HTTPException(status_code=404, detail=pick(
            "技能包条目不存在 / no such entry in the agent skill package", lang))
    return found
