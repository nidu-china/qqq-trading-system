#!/usr/bin/env bash
# Idempotent repository bootstrap for the QQQ 0DTE trading system.
# Runs after the repository is checked out. Safe to run repeatedly.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# System packages: Python venv support + MariaDB (MySQL-compatible) server.
# mariadb-server pulls in the mariadb client (mysql/mysqladmin), so no separate
# client package is needed. Installed individually to avoid apt resolver conflicts.
# Idempotent: apt skips already-installed packages.
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq python3.12-venv
sudo apt-get install -y -qq mariadb-server

# Python backend (editable install with dev extras).
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"

# Frontend dependencies and production build (served by FastAPI).
(cd frontend && pnpm install && pnpm run build)

# Local .env for paper mode, pointing at the local MariaDB instance.
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i 's#^DATABASE_URL=.*#DATABASE_URL=mysql+asyncmy://qqq:change-me@127.0.0.1:3306/qqq?charset=utf8mb4#' .env
  sed -i 's/^TRADING_MODE=.*/TRADING_MODE=paper/' .env
fi

echo "install: done"
