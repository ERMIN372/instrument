#!/usr/bin/env bash
# Установка на уже работающую ВМ без Docker — как остальные сайты на ней:
#   * своя БД и роль в уже запущенном PostgreSQL;
#   * venv + systemd-сервис uvicorn на 127.0.0.1:$APP_PORT (наружу порт не открывается);
#   * наружу публикует существующий nginx (см. deploy/nginx-instrument.conf).
#
# Запуск на ВМ из папки проекта:   bash deploy/setup-vm.sh
# Повторный запуск = обновление: .env и пароли не трогаются, зависимости
# доустанавливаются, сервис перезапускается.
set -euo pipefail

APP_PORT="${APP_PORT:-8030}"
PG_PORT="${PG_PORT:-5432}"
DB_NAME="${DB_NAME:-instrument}"
DB_USER="${DB_USER:-instrument}"
SERVICE="${SERVICE:-instrument}"
# Как зайти в PostgreSQL суперпользователем (переопредели, если у тебя иначе).
PG_ADMIN="${PG_ADMIN:-sudo -u postgres psql -p $PG_PORT}"

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(id -un)"
cd "$APP_DIR"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nОШИБКА: %s\n' "$*" >&2; exit 1; }

for bin in ss curl openssl python3; do
  command -v "$bin" >/dev/null || die "нет утилиты $bin (ss — пакет iproute2)"
done

say "Проверяю порт $APP_PORT"
# Занятый порт допустим, только если его держит уже установленный instrument.
ours_port="$(grep -oP -- '--port \K[0-9]+' "/etc/systemd/system/$SERVICE.service" 2>/dev/null || true)"
if ss -tlnH "sport = :$APP_PORT" | grep -q . \
   && ! { [[ "$ours_port" == "$APP_PORT" ]] && systemctl is-active --quiet "$SERVICE"; }; then
  ss -tlnH "sport = :$APP_PORT" >&2
  die "порт $APP_PORT занят другим процессом. Возьми другой: APP_PORT=8040 bash deploy/setup-vm.sh"
fi

say "Проверяю python3-venv"
python3 -m venv --help >/dev/null 2>&1 || die "нет модуля venv: sudo apt install -y python3-venv"

if [[ ! -f .env ]]; then
  say "Создаю роль и БД «$DB_NAME» в PostgreSQL на порту $PG_PORT"
  taken="$($PG_ADMIN -tAc "SELECT (SELECT count(*) FROM pg_roles WHERE rolname = '$DB_USER')
                              + (SELECT count(*) FROM pg_database WHERE datname = '$DB_NAME')")"
  [[ "$taken" == "0" ]] || die "роль $DB_USER или БД $DB_NAME уже есть на порту $PG_PORT (чужой проект?).
  Задай свои имена: DB_NAME=instrument2 DB_USER=instrument2 bash deploy/setup-vm.sh"

  DB_PASS="$(openssl rand -hex 16)"
  $PG_ADMIN -v ON_ERROR_STOP=1 -v usr="$DB_USER" -v pass="$DB_PASS" -v db="$DB_NAME" <<'SQL'
CREATE ROLE :"usr" LOGIN PASSWORD :'pass';
CREATE DATABASE :"db" OWNER :"usr";
SQL

  say "Пишу .env (пароли — только здесь, не коммить)"
  umask 077
  cat > .env <<EOF
DATABASE_URL=postgresql://$DB_USER:$DB_PASS@127.0.0.1:$PG_PORT/$DB_NAME
# Вход по паролю выключен. Включить: вписать пароль и sudo systemctl restart instrument
APP_USER=admin
APP_PASSWORD=
EOF
  umask 022
else
  say ".env уже есть — БД и пароли не трогаю"
fi

say "Ставлю зависимости в .venv"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

say "Настраиваю systemd-сервис $SERVICE (127.0.0.1:$APP_PORT)"
sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<EOF
[Unit]
Description=Instrument: недельная сводная по выгрузкам 1С
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $APP_PORT --proxy-headers
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null
sudo systemctl restart "$SERVICE"

say "Проверка здоровья"
for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:$APP_PORT/healthz" >/dev/null 2>&1; then
    echo "OK: сервис отвечает на http://127.0.0.1:$APP_PORT"
    grep -q '^APP_PASSWORD=.' .env && echo "    вход: admin / пароль из .env" || echo "    вход без пароля"
    echo "Дальше — nginx: deploy/nginx-instrument.conf"
    exit 0
  fi
  sleep 1
done
sudo journalctl -u "$SERVICE" -n 50 --no-pager >&2
die "сервис не ответил на /healthz — логи выше"
