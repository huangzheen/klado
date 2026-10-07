"""Walk the declared dependency tree and classify every licence in it.

This exists because the previous audit of this project read ``api/requirements.txt`` and
the front-end vendor directory, and *missed* a GPL-3.0 package (``pcodedmp``) that was two
levels down from ``extract-msg``. Reading the direct requirements is not the same question
as "what does the shipped image contain", and only the second one is the one that has to be
answered correctly.

Validate the active declared graph using packaging markers/extras, then audit every
installed distribution in the clean delivery image. Developer virtualenv contents
must not be used to generate the release inventory.
"""
from __future__ import annotations

import importlib.metadata as md
import re
from collections.abc import Iterator
from pathlib import Path
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

#: Licences blocked by the project Python release policy pending separate review. The alternation is ordered so the longer and more specific names win, and
#: the GPL arm is bounded by a lookaround so it cannot match inside "AGPL" or "LGPL".
BLOCKING = re.compile(
    r"AGPL|GNU AFFERO|Affero General Public"
    r"|(?<![\w-])GPL(?![A-Za-z])|GNU GENERAL PUBLIC LICENSE"
    r"|SSPL|Server Side Public|CDDL|Common Development and Distribution"
    r"|EPL|Eclipse Public License|EUPL|European Union Public Licence"
    r"|Commons Clause|PolyForm|BUSL|Sustainable Use|Elastic License",
    re.IGNORECASE,
)

#: Weak copyleft that may ship but must be attributed.
WEAK_COPYLEFT = re.compile(r"LGPL|Lesser General Public|MPL|Mozilla Public", re.IGNORECASE)

#: Everything else we have seen: MIT, BSD, ISC, Apache-2.0, PSF, Pillow's MIT-CMU,
#: matplotlib's PSF-derived licence.
PERMISSIVE = re.compile(
    r"MIT|BSD|ISC|Apache|Python Software Foundation|PSF|Public Domain|"
    r"Python License|Artistic|0BSD|Unlicense|zlib",
    re.IGNORECASE,
)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def normalise(name: str) -> str:
    """``'markitdown[pptx]>=0.1.0 ; python_version>"'`` -> ``'markitdown'``."""
    return canonicalize_name(Requirement(name).name)


class LicenseAuditError(ValueError):
    """A missing dependency or unreadable requirement makes an audit incomplete."""


def _requirements(root: Path | None = None) -> list[Requirement]:
    lines = ((root or repo_root()) / "api" / "requirements.txt").read_text().splitlines()
    return [Requirement(line.split("#", 1)[0].strip()) for line in lines
            if line.split("#", 1)[0].strip()
            and line.strip() != "--no-binary=psycopg2-binary"]


def declared_roots(root: Path | None = None) -> list[str]:
    """The distributions ``api/requirements.txt`` asks for."""
    return [canonicalize_name(req.name) for req in _requirements(root)
            if req.marker is None or req.marker.evaluate()]


def _license_of(dist: md.Distribution) -> str:
    expression = dist.metadata.get("License-Expression") or ""
    if not expression:
        classifiers = dist.metadata.get_all("Classifier") or []
        expression = "; ".join(
            sorted({c.split("::")[-1].strip() for c in classifiers if c.startswith("License ::")})
        )
    return expression or (dist.metadata.get("License") or "").replace("\n", " ").strip()


def declared_tree(root: Path | None = None) -> dict[str, str]:
    """``{distribution: licence string}`` for everything reachable from the requirements.

    Evaluate environment markers and propagate requested extras. Missing active
    dependencies fail closed; inactive platforms and unrequested extras are ignored.
    """
    installed: dict[str, md.Distribution] = {}
    for dist in md.distributions():
        name = dist.metadata.get("Name") or dist.name or ""
        installed[canonicalize_name(name)] = dist

    seen: dict[str, str] = {}
    activated: dict[str, set[str]] = {}
    processed: dict[str, frozenset[str]] = {}
    queue = [(req, {""}) for req in _requirements(root)]
    while queue:
        req, parent_extras = queue.pop()
        if req.marker and not any(req.marker.evaluate({"extra": extra})
                                  for extra in parent_extras):
            continue
        name = canonicalize_name(req.name)
        if name not in installed:
            raise LicenseAuditError(f"active dependency not installed: {name}")
        extras = activated.setdefault(name, {""})
        extras.update(req.extras)
        dist = installed[name]
        if req.specifier and not req.specifier.contains(dist.version, prereleases=True):
            raise LicenseAuditError(f"installed {name}=={dist.version} does not satisfy {req.specifier}")
        if processed.get(name) == frozenset(extras):
            continue
        processed[name] = frozenset(extras)
        seen[name] = _license_of(dist)
        for requirement in dist.requires or []:
            queue.append((Requirement(requirement), set(extras)))
    return dict(sorted(seen.items()))


def runtime_tree(root: Path | None = None) -> dict[str, str]:
    """Validate the active graph, then inventory every installed distribution.

    Run in the clean delivery image. A developer virtualenv may include unrelated
    packages; those are deliberately not silently hidden from a runtime audit.
    """
    declared_tree(root)
    return dict(sorted((canonicalize_name(dist.metadata["Name"]), _license_of(dist))
                       for dist in md.distributions()))


def classify(license_text: str) -> str:
    """One of ``blocking``, ``weak-copyleft``, ``permissive``, ``unknown``."""
    text = license_text or ""
    if BLOCKING.search(text):
        return "blocking"
    if WEAK_COPYLEFT.search(text):
        return "weak-copyleft"
    if PERMISSIVE.search(text):
        return "permissive"
    return "unknown"


def grouped(tree: dict[str, str]) -> dict[str, list[str]]:
    """``{licence class: [distribution names]}``."""
    buckets: dict[str, list[str]] = {}
    for name, license_text in tree.items():
        buckets.setdefault(classify(license_text), []).append(name)
    return {key: sorted(value) for key, value in sorted(buckets.items())}


def render_markdown(tree: dict[str, str]) -> str:
    """The inventory block that lives in THIRD_PARTY_NOTICES.md between its markers."""
    buckets = grouped(tree)
    labels = {
        "blocking": "🔴 阻断 —— 出现在这里即不可发布",
        "weak-copyleft": "🟡 弱 copyleft —— 保留许可、署名并履行源码与替换义务",
        "permissive": "🟢 宽松",
        "unknown": "⚪ 未识别 —— 必须人工确认后再发布",
    }
    total = len(tree)
    lines = [
        f"共 **{total}** 个发行包。",
        "",
    ]
    for key in ("blocking", "weak-copyleft", "unknown", "permissive"):
        names = buckets.get(key) or []
        lines.append(f"### {labels[key]}（{len(names)}）")
        lines.append("")
        lines.append(", ".join(f"`{name}`" for name in names) if names else "_（无）_")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def iter_versions(tree: dict[str, str]) -> Iterator[tuple[str, str, str]]:
    """``(name, version, licence)`` for the inventory."""
    installed: dict[str, md.Distribution] = {}
    for dist in md.distributions():
        name = canonicalize_name(dist.metadata.get("Name") or dist.name or "")
        installed[name] = dist
    for name, license_text in tree.items():
        dist = installed.get(name)
        yield name, (dist.version if dist else "?"), license_text
