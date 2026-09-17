#!/usr/bin/env bash
# Per-boot startup: bring up MariaDB, ensure the schema exists, run migrations.
# Idempotent and tolerant of restarts. Returns after reconciliation completes.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Ensure MariaDB data/run directories exist and are owned by the mysql user.
# mariadbd's default socket is /run/mysqld/mysqld.sock. On some images /var/run
# is a real directory rather than a symlink to /run, so create both paths.
sudo mkdir -p /var/lib/mysql /run/mysqld /var/run/mysqld
sudo chown -R mysql:mysql /var/lib/mysql /run/mysqld /var/run/mysqld

# Initialize the data directory on first boot only.
if [ ! -d /var/lib/mysql/mysql ]; then
  sudo mariadb-install-db --user=mysql --datadir=/var/lib/mysql >/dev/null 2>&1 || true
fi

# Start the server if it is not already accepting connections.
if ! sudo mysqladmin ping >/dev/null 2>&1; then
  sudo bash -c 'nohup mariadbd --user=mysql >/var/log/mariadbd.out 2>&1 &'
fi

# Wait until the server is ready (up to ~30s).
for _ in $(seq 1 30); do
  if sudo mysqladmin ping >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# Ensure the application database and user exist.
sudo mariadb <<'SQL'
CREATE DATABASE IF NOT EXISTS qqq CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
CREATE USER IF NOT EXISTS 'qqq'@'%' IDENTIFIED BY 'change-me';
CREATE USER IF NOT EXISTS 'qqq'@'localhost' IDENTIFIED BY 'change-me';
GRANT ALL PRIVILEGES ON qqq.* TO 'qqq'@'%';
GRANT ALL PRIVILEGES ON qqq.* TO 'qqq'@'localhost';
FLUSH PRIVILEGES;
SQL

# Apply database migrations.
# shellcheck disable=SC1091
source .venv/bin/activate
alembic upgrade head

echo "start: MariaDB ready and migrations applied"
