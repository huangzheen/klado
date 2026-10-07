#!/usr/bin/env python3
"""Verify the licence/source bytes that recipients actually receive."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify_entry(root: Path, entry: dict) -> None:
    relative = Path(entry['file'])
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('notice path escapes distribution')
    target = root / relative
    if target.is_symlink() or not target.is_file():
        raise ValueError('notice/source file missing: ' + relative.as_posix())
    data = target.read_bytes()
    if not data:
        raise ValueError('notice/source file empty: ' + relative.as_posix())
    if hashlib.sha256(data).hexdigest() != entry['sha256']:
        raise ValueError('notice/source file hash differs: ' + relative.as_posix())


def verify(root: Path = ROOT) -> dict[str, int]:
    counts = {}
    for family in ['frontend', 'python', 'native', 'runtime', 'sources']:
        folder = root / 'third_party' / family
        manifest = json.loads((folder / 'manifest.json').read_text())
        if family == 'runtime':
            packages = manifest['python']
            for package in manifest['debian']:
                if 'file' in package:
                    verify_entry(folder, package)
            verify_entry(folder, manifest['interpreter'])
        else:
            packages = manifest['packages']
        for package in packages:
            if family == 'sources':
                verify_entry(folder, package)
            else:
                notices = package['notices']
                if not notices:
                    raise ValueError('empty licence materials: ' + package['name'])
                for entry in notices:
                    verify_entry(folder / package['name'] if family == 'python' else folder, entry)
        counts[family] = len(packages)
    return counts


if __name__ == '__main__':
    print('verified distributed licence/source materials:', verify())
