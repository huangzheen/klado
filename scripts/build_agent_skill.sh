#!/usr/bin/env bash
# Build the downloadable agent skill package from agent_skill/klado.
#
# The zip is what users download ({base}/agent.zip) and hand to 豆包工作, but the
# source of truth is agent_skill/ in this repo — otherwise the only way to change the
# skill would be to unzip a binary inside the repository.
#
# Usage:  bash scripts/build_agent_skill.sh
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=frontend/out/agent.zip
# A fresh deterministic archive: no stale entries, filesystem timestamps or local
# metadata. The root licence is canonical and included even in a standalone ZIP.
python3 - <<'PY'
from pathlib import Path
import zipfile
root = Path('agent_skill')
entries = {p.relative_to(root).as_posix(): p.read_bytes()
           for p in (root / 'klado').rglob('*') if p.is_file()
           and not any(part.startswith('.') or part == '__pycache__' for part in p.parts)}
entries['klado/LICENSE'] = Path('LICENSE').read_bytes()
out = Path('frontend/out/agent.zip')
temporary = out.with_suffix('.zip.tmp')
with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for name, data in sorted(entries.items()):
        info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, data)
temporary.replace(out)
PY
echo "built $OUT"
unzip -l "$OUT" | tail -7
