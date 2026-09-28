#!/usr/bin/env bash
# Дамп PostgreSQL на ВМ. Запускается на самой ВМ, например из cron:
#   0 3 * * * bash $HOME/instrument/deploy/backup.sh >> $HOME/backups/backup.log 2>&1
# Установка без Docker (setup-vm.sh): берёт DATABASE_URL из .env.
# Установка через docker compose: дампит контейнер db.
set -euo pipefail

cd "$(dirname "$0")/.."
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p "$BACKUP_DIR"

set -a; source .env; set +a
OUT="$BACKUP_DIR/instrument-$(date +%F-%H%M).sql.gz"
if [[ -n "${DATABASE_URL:-}" ]]; then
  pg_dump --no-owner "$DATABASE_URL" | gzip > "$OUT"
else
  # Те же умолчания, что в docker-compose.yml.
  docker compose exec -T db pg_dump -U "${POSTGRES_USER:-instrument}" "${POSTGRES_DB:-instrument}" | gzip > "$OUT"
fi
find "$BACKUP_DIR" -name 'instrument-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
echo "$(date -Is) backup ok: $OUT"
