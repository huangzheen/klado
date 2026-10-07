#!/usr/bin/env python3
"""Collect notices from the installed image without copying machine configuration.

Package license files (including native libraries bundled in wheels) remain in
site-packages too. Debian copyright files stay in /usr/share/doc; this index adds
source package/version links for recipients who redistribute the whole image.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata as md
import json
import platform
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
from services.license_audit import runtime_tree, classify, normalise


def is_notice_file(path: Path) -> bool:
    """Include nested notice text, never executable modules or local bytecode."""
    return (not any(part == '__pycache__' for part in path.parts)
            and path.suffix.lower() not in {'.py', '.pyi', '.pyc', '.pyo'}
            and bool(re.search(r"licen[cs]e|copying|copyright|notice", str(path), re.I)))


def collect(output: Path) -> dict:
    tree = runtime_tree()
    bad = [name for name, licence in tree.items() if classify(licence) in {"blocking", "unknown"}]
    if bad:
        raise ValueError("unapproved runtime licences: " + ", ".join(bad))
    constraints = {normalise(line): line.split("==", 1)[1].strip()
                   for line in (ROOT / "api" / "constraints.txt").read_text().splitlines()
                   if line and not line.startswith("#")}
    for name in tree:
        if constraints.get(name) != md.version(name):
            raise ValueError("runtime version differs from audited constraints: " + name)
    output.mkdir(parents=True, exist_ok=True)
    packages = []
    for dist in sorted(md.distributions(), key=lambda d: normalise(d.metadata["Name"])):
        name = normalise(dist.metadata["Name"])
        if name == "psycopg2-binary" and any("psycopg2_binary.libs" in str(p) for p in dist.files or []):
            raise ValueError("psycopg2 must be built against system libpq, not bundled wheel libraries")
        notices = []
        for entry in dist.files or []:
            path = Path(str(entry))
            if not is_notice_file(path):
                continue
            source = Path(dist.locate_file(entry))
            if not source.is_file():
                continue
            # Paths are package-relative; never record installation directories.
            parts = [part for part in path.parts if part not in {"..", ".", "/"}]
            target = output / "python" / name / Path(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            data = source.read_bytes()
            target.write_bytes(data)
            notices.append({"file": target.relative_to(output).as_posix(),
                            "sha256": hashlib.sha256(data).hexdigest()})
        packages.append({"name": name, "version": dist.version, "licence": tree[name],
                         "release": f"https://pypi.org/project/{name}/{dist.version}/",
                         "notices": notices})
    supplements = ROOT / "third_party" / "python" / "manifest.json"
    if supplements.exists():
        for extra in json.loads(supplements.read_text())["packages"]:
            package = next((p for p in packages if p["name"] == extra["name"]), None)
            if package is None:
                continue
            if package["version"] != extra["version"]:
                raise ValueError("supplemental licence version differs: " + extra["name"])
            for entry in extra["notices"]:
                source = supplements.parent / extra["name"] / entry["file"]
                data = source.read_bytes()
                if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    raise ValueError("supplemental licence hash differs: " + extra["name"])
                target = output / "python" / extra["name"] / "supplement" / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                package["notices"].append({"file": target.relative_to(output).as_posix(),
                                           "sha256": entry["sha256"]})
    debian = []
    if Path("/usr/bin/dpkg-query").exists():
        rows = subprocess.check_output([
            "dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n",
        ]).decode().splitlines()
        for row in rows:
            name, version, source_name, source_version = row.split("\t")
            base = name.split(":")[0]
            source = Path("/usr/share/doc") / base / "copyright"
            record = {"name": name, "version": version,
                      "source": f"https://sources.debian.org/src/{quote(source_name, safe='')}/{quote(source_version, safe='')}/"}
            if source.is_file():
                target = output / "debian" / base / "copyright"
                target.parent.mkdir(parents=True, exist_ok=True)
                data = source.read_bytes()
                target.write_bytes(data)
                record.update(file=target.relative_to(output).as_posix(), sha256=hashlib.sha256(data).hexdigest())
            debian.append(record)
        common = Path("/usr/share/common-licenses")
        for source in common.iterdir():
            if source.is_file():
                target = output / "debian" / "common-licenses" / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
    native_file = ROOT / "third_party" / "native" / "manifest.json"
    native = json.loads(native_file.read_text())
    for owner in native["owners"]:
        if md.version(owner["name"]) != owner["version"]:
            raise ValueError("native notice profile version differs: " + owner["name"])
    from cryptography.hazmat.backends.openssl.backend import backend
    ssl_version = next(p["version"] for p in native["packages"] if p["name"] == "OpenSSL")
    if not backend.openssl_version_text().startswith("OpenSSL " + ssl_version + " "):
        raise ValueError("embedded OpenSSL version differs from native notices")
    interpreter_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    target = output / "interpreter" / "LICENSE.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    data = interpreter_license.read_bytes()
    target.write_bytes(data)
    python_version = platform.python_version()
    manifest = {"python": packages, "debian": debian,
                "interpreter": {"name": "CPython", "version": python_version,
                                "source": f"https://www.python.org/ftp/python/{python_version}/Python-{python_version}.tar.xz",
                                "file": "interpreter/LICENSE.txt", "sha256": hashlib.sha256(data).hexdigest()},
                "native_supplement": "third_party/native/manifest.json"}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"collected runtime notices: {len(packages)} Python packages, {len(debian)} Debian packages")
    missing = [p["name"] for p in packages if not p["notices"]]
    print("packages without embedded notice files: " + (", ".join(missing) or "none"))
    if missing:
        raise ValueError("full licence materials missing: " + ", ".join(missing))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "third_party" / "runtime")
    collect(parser.parse_args().output)
