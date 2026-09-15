#!/usr/bin/env bash
# Kick off the BenchWeave ADC web frontend (config/control + live graph).
#
# Usage:
#   ./scripts/run_adc_web.sh                # listens on 0.0.0.0:8000
#   BENCHWEAVE_PORT=8080 ./scripts/run_adc_web.sh
#   BENCHWEAVE_HOST=127.0.0.1 ./scripts/run_adc_web.sh
set -euo pipefail

# Resolve the repository root (this script lives in scripts/).
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

HOST="${BENCHWEAVE_HOST:-0.0.0.0}"
PORT="${BENCHWEAVE_PORT:-8000}"

echo "BenchWeave ADC web frontend"
echo "  local:   http://localhost:${PORT}"
echo "  network: http://${HOST}:${PORT}"
echo
exec uv run uvicorn benchweave.web.app:app --host "$HOST" --port "$PORT" --reload
