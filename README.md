# instrument

Веб-инструмент недельной аналитики: загружаешь xlsx-выгрузки → получаешь
**единую таблицу на учётную неделю** (ISO, Пн–Вс) по всем источникам,
с разбивкой по дням, сравнением с прошлой неделей, динамикой за N недель
и графиками. Данные хранятся в PostgreSQL.

## Как это работает

1. **Загрузка** — перетаскиваешь один или несколько `.xlsx`. Для каждого файла
   указывается «источник» (по умолчанию имя файла без дат/месяцев:
   `Продажи_сентябрь_2026.xlsx` → `Продажи`). Новый файл того же источника
   **заменяет** его данные за свой период, так что повторная загрузка не задваивает.
2. **Разбор** — парсер сам определяет раскладку листа:
   * «длинная»: колонка с датой + числовые колонки (метрики) + текстовые (разрезы:
     магазин, менеджер…, до 50 значений);
   * «широкая»: даты в строке-заголовке, названия метрик слева.

   Строки «Итого/Всего», колонки «№/Номер/Код/ID/Артикул» пропускаются.
   Если в «транзакционной» выгрузке на день много строк — добавляется метрика
   «Количество записей».
3. **Неделя** — метрики × Пн…Вс + «Итого нед.», «Пред. нед.», «Δ». Экспорт в xlsx.
4. **Динамика** — метрики × последние 4/8/12/26 недель.
5. **Метрики** — переименование, скрытие и тип агрегации за неделю:
   `сумма` (выручка), `среднее` (средний чек, конверсия — средневзвешенное),
   `последнее` (остатки). Тип угадывается по названию, поправь руками где надо.

## Структура

```
app/
  main.py      FastAPI: API, Basic Auth, статика
  parser.py    xlsx → факты (метрика, день, разрезы, значение)
  service.py   загрузка в БД, недельные/дневные агрегаты
  export.py    выгрузка недели в xlsx
  db.py        пул соединений и схема PostgreSQL
  static/      интерфейс (HTML/CSS/JS без сборки и без CDN)
deploy/
  create-vm.sh         создание ВМ в Yandex Cloud через yc CLI
  cloud-init.yaml.tpl  первичная настройка ВМ (Docker)
  deploy.sh            выкладка кода и запуск docker compose
  backup.sh            ежедневный pg_dump
docker-compose.yml     app + postgres
tests/                 тесты парсера и агрегаций
```

## Локальный запуск

```bash
echo -e "POSTGRES_PASSWORD=dev\nAPP_PASSWORD=dev" > .env
docker compose up -d --build
open http://localhost        # логин admin / dev
```

Тесты:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q
```

## Деплой на Yandex Cloud (ВМ + PostgreSQL на ней же)

### 0. Что нужно локально

* `yc` — [CLI Yandex Cloud](https://yandex.cloud/ru/docs/cli/quickstart):
  `curl -sSL https://storage.yandexcloud.net/yandexcloud-yc/install.sh | bash`, затем `yc init`
  (выбрать облако и каталог).
* `jq`, `rsync`, `openssl`, ssh-ключ (`ssh-keygen -t ed25519`, если нет).

### 1. Создать ВМ

```bash
bash deploy/create-vm.sh
```

Создаёт сеть `instrument-net`, подсеть в `ru-central1-a`, группу безопасности
(входящие 22 и 80) и ВМ Ubuntu 22.04, 2 vCPU (50%), 4 ГБ RAM, 20 ГБ SSD, с публичным IP.
cloud-init ставит Docker. Параметры переопределяются переменными окружения:

```bash
VM_NAME=instrument ZONE=ru-central1-b CORES=2 MEMORY=4 CORE_FRACTION=100 \
SSH_ALLOW_CIDR=203.0.113.10/32 bash deploy/create-vm.sh
```

В конце скрипт печатает IP.

### 2. Выложить приложение

```bash
VM_HOST=<IP> bash deploy/deploy.sh
```

При первом запуске создаётся локальный `.env` со случайными паролями
(БД и вход на сайт — `APP_USER` / `APP_PASSWORD`). **Не теряй и не коммить его**:
пароль БД зашит в уже созданный том PostgreSQL. Повторный `deploy.sh` — это
обновление кода, данные в БД сохраняются.

### 3. Проверка и диагностика

```bash
curl -u admin:<APP_PASSWORD> http://<IP>/api/weeks        # API отвечает
ssh deploy@<IP> 'cd instrument && docker compose ps'        # оба контейнера Up/healthy
ssh deploy@<IP> 'cd instrument && docker compose logs --tail=100 app'
ssh deploy@<IP> 'cd instrument && docker compose logs --tail=100 db'
ssh deploy@<IP> 'sudo cat /var/log/cloud-init-output.log'  # если Docker не встал
```

Частые проблемы:

| Симптом | Причина / что делать |
|---|---|
| `deploy.sh` висит на «Жду cloud-init» | ВМ ещё ставит пакеты, 2–3 мин; смотри `cloud-init-output.log` |
| `docker: permission denied` по ssh | группа docker применяется к новым сессиям — переподключись |
| `pull access denied` / таймаут на образах | Docker Hub недоступен; в `daemon.json` уже прописано зеркало `mirror.gcr.io`, проверь `docker info \| grep -A1 Mirrors` |
| `password authentication failed` в логах app | `.env` пересоздан после первого запуска. Верни старый пароль или (данные потеряются!) `docker compose down -v` |
| 401 в браузере | логин/пароль из `.env` (`APP_USER` / `APP_PASSWORD`) |

### 4. Бэкапы БД

На ВМ:

```bash
(crontab -l 2>/dev/null; echo "0 3 * * * bash $HOME/instrument/deploy/backup.sh >> $HOME/backups/backup.log 2>&1") | crontab -
```

Дампы лежат в `~/backups`, хранятся 14 дней. Восстановление:

```bash
gunzip -c ~/backups/instrument-XXXX.sql.gz | docker compose exec -T db psql -U instrument instrument
```

Бэкап на том же диске не спасёт от удаления ВМ — для надёжности настрой
[снимки диска по расписанию](https://yandex.cloud/ru/docs/compute/operations/snapshot-control/create-schedule)
или копируй дампы в Object Storage.

## Безопасность — важно

* Сайт защищён HTTP Basic Auth, но **по HTTP пароль идёт открытым текстом**.
  Для боевого режима: домен + HTTPS (например, Caddy перед приложением
  с автоматическим сертификатом Let's Encrypt) и закрытый порт 80.
* SSH по умолчанию открыт всему интернету; ограничь `SSH_ALLOW_CIDR=<твой IP>/32`
  при создании ВМ.
* PostgreSQL наружу не публикуется — доступен только контейнеру приложения.
