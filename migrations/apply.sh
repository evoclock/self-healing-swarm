#!/bin/bash
set -euo pipefail

host="${PGHOST:-postgres}"
user="${PGUSER:-migrator}"
db="${PGDATABASE:-swarm}"
export PGPASSWORD="${PGPASSWORD:-migrator}"

until pg_isready -h "$host" -U "$user" -d "$db"; do
  sleep 1
done

psql -h "$host" -U "$user" -d "$db" -v ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);
SQL

shopt -s nullglob
files=(/migrations/*.sql)
if [ "${#files[@]}" -eq 0 ]; then
  echo "no sql files in /migrations" >&2
  exit 1
fi

for path in $(printf '%s\n' "${files[@]}" | sort); do
  base="$(basename "$path")"
  already="$(psql -h "$host" -U "$user" -d "$db" -tA -v ON_ERROR_STOP=1 \
    -c "SELECT 1 FROM schema_migrations WHERE filename = '$base'")"
  if [ "$already" = "1" ]; then
    echo "skip $base"
    continue
  fi
  echo "apply $base"
  psql -h "$host" -U "$user" -d "$db" -v ON_ERROR_STOP=1 --single-transaction -f "$path"
  psql -h "$host" -U "$user" -d "$db" -v ON_ERROR_STOP=1 \
    -c "INSERT INTO schema_migrations (filename) VALUES ('$base')"
done

echo "migrations applied"
