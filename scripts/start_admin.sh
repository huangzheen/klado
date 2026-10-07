#!/usr/bin/env bash
# Start the Klado admin console — the second process, on its own port.
#
# Symmetric with starting the main app, and deliberately not a replacement for it: the two
# are separate deployment units with separate failure domains. Neither script starts the
# other, and neither checks whether the other is running. If the console needs
# `app_users` to exist, the answer is "start the main app", not "have the console create
# an accounts table and hope".
#
#   bash scripts/start_admin.sh              # background, logs to /tmp/klado-admin.log
#   bash scripts/start_admin.sh --foreground # stay attached, Ctrl-C to stop
#   KLADO_ADMIN_PORT=9000 bash scripts/start_admin.sh   # override for one run
#
# ⚠️ The port is read through the application's own Settings object, not by grepping
# `.env` and not by sourcing it. A `.env` is a dotenv file, not a shell script: a value
# like `AUTH_BOOTSTRAP_ADMIN_HASH=pbkdf2_sha256$240000$…` is perfectly legal there and is
# mangled by `source` — the shell eats `$240000` as a variable, and under `set -u` the
# whole script dies with "unbound variable" before uvicorn is ever reached. Asking Python
# also means there is exactly one answer to "what port does this installation use":
# `klado_shared/config.py`.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_ADMIN_DIR="$REPO_ROOT/api-admin"
LOG_FILE="${KLADO_ADMIN_LOG:-/tmp/klado-admin.log}"
PYTHON="${KLADO_PYTHON:-$REPO_ROOT/.venv312/bin/python}"
FOREGROUND=0

for arg in "$@"; do
  case "$arg" in
    --foreground|-f) FOREGROUND=1 ;;
    -h|--help) sed -n '3,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

if [[ ! -x "$PYTHON" ]]; then
  echo "no interpreter at $PYTHON" >&2
  echo "set KLADO_PYTHON, or create the virtualenv it points at" >&2
  exit 1
fi

# The application's own config layer loads `.env` into `os.environ` before anything reads
# it, so nothing here has to export anything. `KLADO_ADMIN_PORT` in the environment still
# wins, because `load_dotenv` is called with `override=False`.
read_port() {
  ( cd "$REPO_ROOT" && "$PYTHON" -c \
    'import sys; sys.path.insert(0, "klado_shared/.."); from klado_shared.config import settings; print(settings.KLADO_ADMIN_PORT)' )
}

PORT="${KLADO_ADMIN_PORT:-$(read_port)}"
HOST="${KLADO_ADMIN_HOST:-127.0.0.1}"

cd "$API_ADMIN_DIR"

# A port check *before* starting, so a second invocation says "already running" instead of
# dying with an opaque "address already in use" from deep inside the ASGI server. The
# health endpoint is asked rather than the port alone, because a process holding :8787 that
# is not this console would answer the port check too — and then the operator would
# believe the console is up.
if curl -fsS --max-time 2 "http://$HOST:$PORT/api/admin-console/health" >/dev/null 2>&1; then
  echo "the admin console is already answering on http://$HOST:$PORT" >&2
  exit 1
fi

echo "starting the admin console on http://$HOST:$PORT (log: $LOG_FILE)"

if [[ "$FOREGROUND" == "1" ]]; then
  exec "$PYTHON" -m uvicorn main:app --host "$HOST" --port "$PORT"
fi

nohup "$PYTHON" -m uvicorn main:app --host "$HOST" --port "$PORT" \
  >"$LOG_FILE" 2>&1 &
PID=$!

# ⚠️ Wait for the health endpoint, not for the PID. A process can be alive for two seconds
# and still be dying — on a bad import, a missing table, a port already taken. Polling
# the endpoint is the only check that says the thing people actually care about: "can I
# open it in a browser". A fixed sleep would report success for a console that never came
# up, and this script's whole job is to not do that.
for _ in $(seq 1 40); do
  if curl -fsS --max-time 1 "http://$HOST:$PORT/api/admin-console/health" >/dev/null 2>&1; then
    echo "ready: http://$HOST:$PORT (pid $PID)"
    exit 0
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "the console exited during startup; last lines of $LOG_FILE:" >&2
    tail -n 30 "$LOG_FILE" >&2 || true
    exit 1
  fi
  sleep 0.5
done

echo "the console is still not answering after 20s; last lines of $LOG_FILE:" >&2
tail -n 30 "$LOG_FILE" >&2 || true
kill "$PID" 2>/dev/null || true
exit 1
