# Klado — the application image.
#
# ONE image serves both deployment units (the app on :8000 and the admin console on
# :8787). They are separate processes with separate failure domains, so they get
# separate compose services and separate `command`s — but they are the same code
# over the same `klado_shared/`, so building two images would only buy a slower,
# more failure-prone deploy for no isolation that matters.
#
# ⚠️ The directory layout inside the image is load-bearing. `klado_shared/config.py`
# computes `REPO_DIR` as the parent of its own directory and derives *everything*
# from it: the `.env` location, `frontend/out`, and `api-admin/frontend`. Moving
# `klado_shared/` in the image silently breaks the console's SPA and makes the app
# raise "POSTGRES_PASSWORD is not set". Keep `/app/klado_shared` where it is.
#
# ⚠️ Do not "optimise" this by copying a second copy of the SPA into the image.
# `resolve_frontend_dir()` documents that the retired container build did exactly
# that, and the stale duplicate won — the app happily served a frontend that no
# longer matched the code. There is exactly one copy: the repo's.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # ⚠️ A mirror, not pypi.org. This build runs on a machine where pypi.org is
    # unreachable (it times out rather than refusing), and an unreachable index
    # does not fail fast — the build sits in "resolving dependencies" for
    # minutes with no output, which reads exactly like a slow build. TUNA is
    # used because it answered in ~2s from here; Aliyun works too if TUNA is
    # ever unavailable. Override at build time if the network changes:
    #   docker compose build --build-arg PIP_INDEX_URL=…
    # ⚠️ The image is built inside a container, so it cannot read ~/.config/pip
    # on the host. Whatever index this points at has to be reachable from the
    # Docker daemon's network, not just from the shell.
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

WORKDIR /app

# libpq for psycopg2, and the libs Chromium needs. The build toolchain lives in
# this layer only — nothing here survives into the final image, so the runtime
# never carries a compiler.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpq5 \
      curl \
      # libnss3/libnspr4/libatk/libgbm and friends are Chromium's runtime set;
      # without them Playwright's lazy import succeeds but the browser never
      # launches, which surfaces as a confusing error deep in doc_preview.
      libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
      libgbm1 libpango-1.0-0 libcairo2 libasound2 \
      fonts-liberation fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*

# Dependencies in their own layer: requirements.txt changes far less often than
# the application code, so an edit to the app rebuilds in seconds instead of
# re-resolving the whole dependency tree.
COPY api/requirements.txt /app/api/requirements.txt
COPY api/constraints.txt /app/api/constraints.txt
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev libpq-dev \
 && pip install --no-cache-dir -c /app/api/constraints.txt pip -r /app/api/requirements.txt \
 && apt-get purge -y --auto-remove gcc libc6-dev libpq-dev \
 && rm -rf /var/lib/apt/lists/*

# The application. Frontend needs no build step (it is a single hand-written
# HTML file plus vendored assets), so there is nothing to run here.
COPY api/ /app/api/
COPY api-admin/ /app/api-admin/
COPY klado_shared/ /app/klado_shared/
COPY frontend/ /app/frontend/
COPY scripts/ /app/scripts/
COPY agent_skill/ /app/agent_skill/

# ⚠️ The deployment definition ships in the image, which looks redundant — the
# application never reads any of it, and compose reads it from the checkout.
#
# It is here because `ci_check.py` runs INSIDE this image (with `--network none`
# and no `.env`), and `api/tests/test_test_deployment_isolation.py` asserts the
# properties the test deployment depends on: that it cannot name the production
# services, cannot build the production image tag, and cannot publish a port on
# every interface. Those assertions read the compose files.
#
# Without these three lines that test dies with FileNotFoundError in CI while
# passing on a developer machine — which is exactly the failure mode it exists to
# catch, arriving through the test suite instead. Do not remove them as "unused
# build context"; the alternative is a guard that only runs where it is least
# needed.
COPY docker-compose.yml /app/docker-compose.yml
COPY docker-compose.test.yml /app/docker-compose.test.yml
COPY .env.example /app/.env.example

# The signed test deployment guard checks that the CI Secret belongs only to
# its request step. Include the public pipeline definition, never secret values.
# .gitignore always exists, including in the public export;
# internal pipeline YAML is copied only when it is present. .git is excluded
# by .dockerignore. This keeps the public Docker build independent of CI files.
COPY .gi* /app/

# ⚠️ `deploy.sh` is here for the same reason as the three lines above, and it
#    is here for the same mistake: `api/tests/test_deploy_script.py` copies this
#    file out to a temp directory and runs it, and `ci_check.py` runs INSIDE the
#    image with `--network none`. Without this line that guard is green on every
#    developer machine (where the file is in the checkout) and errors 11 times
#    in CI with `FileNotFoundError: '/app/deploy.sh'` — and the first deploy that
#    hits it refuses to replace the running containers.
#
#    Do not "fix" it by skipping the test when the file is missing. A guard that
#    runs only where it is least needed is not a guard.
COPY deploy.sh /app/deploy.sh

# ⚠️ The licence files are here for the same reason, and for the same trap.
#    `api/tests/test_pdf_io.py` walks the declared dependency tree and then asserts that
#    every weak-copyleft distribution is *named* in THIRD_PARTY_NOTICES.md, so it reads
#    that file from the repository root — while `ci_check.py` runs inside this image,
#    where the repository root is `/app`. Shipping the notices is also simply correct:
#    Apache-2.0 section 4(d) requires NOTICE to accompany the distributed product, and
#    LICENSE is the licence the product is offered under.
#
#    Do not "fix" a missing file by skipping the guard. A guard that runs only where it
#    is least needed is not a guard — and a missing attribution is exactly the kind of
#    defect that stays invisible until someone does the diligence.
COPY LICENSE /app/LICENSE
COPY NOTICE /app/NOTICE
COPY THIRD_PARTY_NOTICES.md /app/THIRD_PARTY_NOTICES.md
COPY third_party/ /app/third_party/

# Fail closed for all installed packages, then retain native/OS notices too.
RUN python scripts/audit_licenses.py --versions \
 && python scripts/collect_runtime_licenses.py \
 && python scripts/verify_license_materials.py

# The two services differ only in cwd, matching the documented manual start
# (`cd api && uvicorn main:app`, `cd api-admin && uvicorn main:app`). Start with
# a no-op: compose supplies the real command per service, and running a server
# as the image's default would start one on a port nothing is listening for.
CMD ["true"]
