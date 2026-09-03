#!/usr/bin/env bash
# Launcher for the trading web service terminal.
# Ensures MariaDB and migrations are ready (idempotent) before starting the
# server, so the app comes up even if the per-boot `start` step did not run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

bash "$ROOT/.cursor/start.sh"

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
exec qqq-trader trade
