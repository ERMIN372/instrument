# instrument

Веб-инструмент недельной аналитики по выгрузкам 1С «движение номенклатуры по дням».
Загружаешь несколько xlsx (выпуск, отгрузки, заказы, остатки…) — получаешь
**единую таблицу на учётную неделю**: товары по строкам, источники рядом по колонкам,
с переключением **неделя ↔ день**, сортировкой по любой колонке и раскладкой по дням.
Данные хранятся в PostgreSQL.

## Формат входных файлов

Колонки (порядок, регистр и лишние колонки не важны):

| Дата | UID номенклатуры | Код номенклатуры | Наименование номенклатуры | Единица измерения | Количество | Количество базовых |
|---|---|---|---|---|---|---|

Обязательны «Дата», «Наименование» или «Код», «Количество базовых» или «Количество».
Считаем **в базовых единицах** (шт, кг): «Количество» бывает в упаковках разного размера,
складывать его нельзя. Повторы «день + товар» суммируются, строки без даты («Итого») пропускаются,
отрицательные значения сохраняются и показываются предупреждением.

## Как это работает

1. **Загрузка** — перетаскиваешь один или несколько файлов и для каждого указываешь
   «источник» — это будущая колонка сводной (по умолчанию имя файла без дат/месяцев:
   `Выпуск_сентябрь_2026.xlsx` → `Выпуск`). Новый файл того же источника **заменяет**
   его данные за свой период — повторная загрузка не задваивает.
2. **Сводная** — товары × источники. Переключатель «Неделя / День»: неделя — ISO (Пн–Вс),
   Δ к прошлой неделе; день — Δ к тому же дню прошлой недели. Клик по заголовку — сортировка
   (по убыванию → по возрастанию → снова группировка по категориям). Итоги отдельно по шт и кг.
   Если за период у источника загружены не все дни — в заголовке предупреждение «5/7 дн.».
3. **По дням** — один источник: товары × Пн…Вс + итог недели, прошлая неделя, Δ.
4. **Динамика** — один источник: товары × последние 4/8/12/26 недель.
5. **Карточка товара** (клик по строке) — все источники × дни недели и график по неделям.
6. **Источники** — порядок колонок, переименование, скрытие и способ свёртки периода:
   `сумма` (выпуск, отгрузки) или `остаток` — значение на последний день периода
   (для срезов остатков суммировать дни нельзя). Источник со словом «остат» в имени
   сразу получает `остаток`.
7. **Скачать xlsx** — лист «Сводная» + лист «По дням» на каждый источник; итоги и Δ
   считаются формулами Excel, фильтры категории и поиска учитываются.

## Структура

```
app/
  main.py      FastAPI: API, Basic Auth, статика
  parser.py    xlsx 1С → движения (день, товар, количество)
  service.py   загрузка в БД, сводная неделя/день, по дням, динамика, карточка товара
  export.py    выгрузка в xlsx с формулами
  db.py        пул соединений и схема PostgreSQL
  static/      интерфейс (HTML/CSS/JS без сборки и без CDN)
deploy/
  create-vm.sh         создание ВМ в Yandex Cloud через yc CLI
  cloud-init.yaml.tpl  первичная настройка ВМ (Docker)
  deploy.sh            выкладка кода и запуск docker compose
  backup.sh            ежедневный pg_dump
docker-compose.yml     app + postgres
tests/                 тесты парсера и API
```

## Локальный запуск

```bash
echo -e "POSTGRES_PASSWORD=dev\nAPP_PASSWORD=dev" > .env
docker compose up -d --build
open http://localhost        # логин admin / dev
```

Тесты (API-тесты идут на настоящем PostgreSQL и **очищают** указанную БД — дай отдельную):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/instrument_test pytest -q
```

Без `TEST_DATABASE_URL` выполняются только тесты парсера.

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
curl -u admin:<APP_PASSWORD> http://<IP>/api/meta         # API отвечает
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

Дампы лежат в `~/backups`, хранятся 14 дней. Восстановление (в пустую БД, приложение на паузе):

```bash
cd ~/instrument
docker compose stop app
docker compose exec -T db sh -c 'dropdb -U instrument instrument && createdb -U instrument instrument'
gunzip -c ~/backups/instrument-XXXX.sql.gz | docker compose exec -T db psql -q -U instrument instrument
docker compose start app
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
