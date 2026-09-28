#!/usr/bin/env bash
# Заливает код на ВМ и поднимает docker compose (приложение + PostgreSQL).
# Использование: VM_HOST=<ip> bash deploy/deploy.sh
set -euo pipefail

VM_HOST="${VM_HOST:?Укажи VM_HOST=<публичный IP ВМ>}"
VM_USER="${VM_USER:-deploy}"
REMOTE_DIR="${REMOTE_DIR:-/home/$VM_USER/instrument}"
TARGET="$VM_USER@$VM_HOST"

cd "$(dirname "$0")/.."

# .env с паролями создаётся один раз локально и больше не перегенерируется,
# иначе новый POSTGRES_PASSWORD не совпадёт с уже инициализированной БД.
if [[ ! -f .env ]]; then
  echo "==> Генерирую .env с новыми паролями"
  cat > .env <<EOF
POSTGRES_DB=instrument
POSTGRES_USER=instrument
POSTGRES_PASSWORD=$(openssl rand -hex 16)
APP_USER=admin
APP_PASSWORD=$(openssl rand -base64 12 | tr -d '/+=')
APP_PORT=80
EOF
  chmod 600 .env
  echo "    Логин/пароль для сайта лежат в .env (APP_USER / APP_PASSWORD)"
fi

echo "==> Жду окончания cloud-init на $VM_HOST"
ssh -o StrictHostKeyChecking=accept-new "$TARGET" 'cloud-init status --wait >/dev/null; docker compose version'

echo "==> Копирую код в $REMOTE_DIR"
rsync -az --delete \
  --exclude .git --exclude __pycache__ --exclude '*.pyc' --exclude .venv \
  ./ "$TARGET:$REMOTE_DIR/"

echo "==> Собираю и запускаю контейнеры"
ssh "$TARGET" "cd '$REMOTE_DIR' && docker compose up -d --build && docker compose ps"

echo "==> Проверка здоровья"
for i in $(seq 1 30); do
  if curl -fsS "http://$VM_HOST/healthz" >/dev/null 2>&1; then
    echo "OK: http://$VM_HOST/"
    exit 0
  fi
  sleep 2
done
echo "Приложение не ответило на /healthz. Логи:" >&2
ssh "$TARGET" "cd '$REMOTE_DIR' && docker compose logs --tail=100 app" >&2
exit 1
