#!/usr/bin/env bash
# Kick off the BenchWeave ADC web frontend (config/control + live graph).
#
# Usage:
#   ./scripts/run_adc_web.sh                # listens on 127.0.0.1:8000
#   BENCHWEAVE_PORT=8080 ./scripts/run_adc_web.sh
#   BENCHWEAVE_HOST=0.0.0.0 ./scripts/run_adc_web.sh   # expose on the LAN
#   BENCHWEAVE_RELOAD=1 ./scripts/run_adc_web.sh       # dev auto-reload
#
# SECURITY: the app has no authentication, CORS policy or CSRF protection —
# it is built for a single operator on localhost. Anyone who can reach the
# port can reconfigure the board, start captures and write files, so binding
# beyond 127.0.0.1 is an explicit opt-in at your own risk.
set -euo pipefail

# Resolve the repository root (this script lives in scripts/).
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

HOST="${BENCHWEAVE_HOST:-127.0.0.1}"
PORT="${BENCHWEAVE_PORT:-8000}"
RELOAD_ARGS=()
if [[ "${BENCHWEAVE_RELOAD:-0}" != "0" ]]; then
  RELOAD_ARGS+=(--reload)
fi

echo "BenchWeave ADC web frontend"
echo "  local:   http://localhost:${PORT}"
if [[ "$HOST" != "127.0.0.1" ]]; then
  echo "  network: http://${HOST}:${PORT}  (UNAUTHENTICATED — see the note in this script)"
fi
echo
exec uv run uvicorn benchweave.web.app:app --host "$HOST" --port "$PORT" "${RELOAD_ARGS[@]}"
