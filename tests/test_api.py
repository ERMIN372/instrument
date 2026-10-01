"""Сквозные тесты API на настоящем PostgreSQL.

Запуск: TEST_DATABASE_URL=postgresql://user@host/db pytest
БД очищается перед каждым тестом. Без переменной тесты пропускаются.
"""
import datetime as dt
import io
import os

import pytest
from openpyxl import Workbook, load_workbook

DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="нужна TEST_DATABASE_URL")

HEADER = ["Дата", "Код номенклатуры", "Наименование номенклатуры", "Единица измерения",
          "Количество", "Количество базовых"]
BUN = ("001", "Булка с маком 80 г, упак 20 шт")
LOAF = ("002", "Хлеб злаковый 280 г, упак 4 шт")


def xlsx(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for day, (code, name), qty in rows:
        ws.append([day.strftime("%d.%m.%Y"), code, name, "шт", qty, qty])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def days(start: dt.date, n: int):
    return [start + dt.timedelta(days=i) for i in range(n)]


W38 = dt.date(2026, 9, 14)
W39 = dt.date(2026, 9, 21)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("APP_PASSWORD", "pw")
    import psycopg

    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS movements, uploads, items, sources CASCADE")

    import importlib

    from app import db
    importlib.reload(db)  # пул создаётся на импорте с текущим DATABASE_URL
    from app import main
    importlib.reload(main)
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        c.auth = ("admin", "pw")
        yield c


def upload(c, source, content, name="file.xlsx"):
    r = c.post("/api/upload", files=[("files", (name, content))], data={"sources": [source]})
    res = r.json()["results"][0]
    assert res["ok"], res
    return res


def test_auth_required(client):
    client.auth = None
    assert client.get("/api/meta").status_code == 401
    assert client.get("/healthz").status_code == 200


def test_pivot_week_day_and_replace(client):
    # Выпуск: булка 10/день две недели, хлеб только в W39.
    upload(client, "Выпуск", xlsx(
        [(d, BUN, 10) for d in days(W38, 14)] + [(d, LOAF, 5) for d in days(W39, 7)]))
    # Остатки: срез на каждый день 14.09–28.09, суммировать нельзя.
    upload(client, "Остатки на начало", xlsx([(d, BUN, 100 + i) for i, d in enumerate(days(W38, 15))]))

    meta = client.get("/api/meta").json()
    assert [s["name"] for s in meta["sources"]] == ["Выпуск", "Остатки на начало"]
    assert [s["agg"] for s in meta["sources"]] == ["sum", "last"]  # «остат» в имени
    assert meta["sources"][1]["close_label"] == "Остатки на конец"
    assert [w["iso"] for w in meta["weeks"]] == ["2026-W40", "2026-W39", "2026-W38"]

    # Неделя 21–27.09: на начало — срез 21.09, на конец — 28.09; колонка «на конец» — последняя.
    p = client.get("/api/pivot", params={"mode": "week", "date": "2026-09-23"}).json()
    assert [(c["name"], c["kind"], c["date"]) for c in p["columns"]] == [
        ("Выпуск", "sum", None), ("Остатки на начало", "open", "2026-09-21"),
        ("Остатки на конец", "close", "2026-09-28")]
    rows = {r["code"]: r for r in p["rows"]}
    bun = rows["001"]["values"]
    assert bun[0] == {"cur": 70, "prev": 70, "delta": 0}
    assert bun[1]["cur"] == 107 and bun[1]["prev"] == 100  # 21.09 и 14.09
    assert bun[2]["cur"] == 114 and bun[2]["prev"] == 107  # 28.09 и 21.09
    assert rows["002"]["values"][0]["cur"] == 35 and rows["002"]["values"][0]["delta"] is None
    assert [(c["covered"], c["of"]) for c in p["columns"]] == [(7, 7), (1, 1), (1, 1)]

    # Среза на 05.10 нет — «на конец» пустая и помечена.
    p = client.get("/api/pivot", params={"mode": "week", "date": "2026-09-28"}).json()
    assert [(c["covered"], c["of"]) for c in p["columns"]] == [(0, 7), (1, 1), (0, 1)]
    assert {r["code"]: r for r in p["rows"]}["001"]["values"][2]["cur"] is None

    d = client.get("/api/pivot", params={"mode": "day", "date": "2026-09-23"}).json()
    bun = {r["code"]: r for r in d["rows"]}["001"]["values"]
    assert bun[0]["cur"] == 10 and bun[1]["cur"] == 109 and bun[1]["prev"] == 102
    assert bun[2]["cur"] == 110 and bun[2]["prev"] == 103  # на начало следующего дня

    # Повторная загрузка W39 с другими цифрами заменяет только W39.
    res = upload(client, "Выпуск", xlsx([(d, BUN, 1) for d in days(W39, 7)]))
    assert res["replaced"] == 14
    p = client.get("/api/pivot", params={"mode": "week", "date": "2026-09-21"}).json()
    rows = {r["code"]: r for r in p["rows"]}
    assert rows["001"]["values"][0]["cur"] == 7
    assert rows["001"]["values"][0]["prev"] == 70
    assert "002" not in rows  # хлеба больше нет ни в W39, ни в W38 — строка не нужна


def test_by_days_trend_item_and_export(client):
    upload(client, "Выпуск", xlsx([(d, BUN, i) for i, d in enumerate(days(W38, 14))]))
    sid = client.get("/api/meta").json()["sources"][0]["id"]

    b = client.get("/api/by-days", params={"source_id": sid, "date": "2026-09-21"}).json()
    row = b["rows"][0]
    assert row["days"] == [7, 8, 9, 10, 11, 12, 13] and row["total"] == 70 and row["prev"] == 21

    t = client.get("/api/trend", params={"source_id": sid, "end": "2026-09-21", "count": 3}).json()
    assert t["rows"][0]["values"] == [None, 21, 70]

    it = client.get("/api/item/001", params={"date": "2026-09-21"}).json()
    assert it["sources"][0]["total"] == 70 and it["sources"][0]["weeks"][-2]["value"] == 21

    x = client.get("/api/export.xlsx", params={"mode": "week", "date": "2026-09-21"})
    assert x.status_code == 200
    wb = load_workbook(io.BytesIO(x.content))
    assert wb.sheetnames == ["Сводная", "По дням · Выпуск"]
    ws = wb["Сводная"]
    assert [ws.cell(4, c).value for c in range(1, 8)] == [
        "Категория", "Код", "Номенклатура", "Ед.", "Выпуск", "Выпуск · пред.", "Выпуск · Δ"]
    assert (ws["E5"].value, ws["F5"].value) == (70, 21)
    assert ws["G5"].value.startswith("=IF(")
    assert ws["E7"].value == "=SUMIFS(E5:E5,$D$5:$D$5,$D7)"


def test_errors(client):
    assert client.get("/api/pivot", params={"date": "nope"}).status_code == 400
    assert client.get("/api/by-days", params={"source_id": 42}).status_code == 404
    r = client.post("/api/upload", files=[("files", ("bad.xlsx", b"not an xlsx"))])
    assert r.json()["results"][0]["ok"] is False
    upload(client, "А", xlsx([(W39, BUN, 1)]))
    upload(client, "Б", xlsx([(W39, BUN, 1)]))
    ids = [s["id"] for s in client.get("/api/meta").json()["sources"]]
    assert client.patch(f"/api/sources/{ids[1]}", json={"name": "А"}).status_code == 409
    assert client.patch(f"/api/sources/{ids[1]}", json={"agg": "avg"}).status_code == 400
    assert client.patch(f"/api/sources/{ids[1]}", json={"hidden": True}).status_code == 200
    p = client.get("/api/pivot", params={"date": "2026-09-21"}).json()
    assert [c["name"] for c in p["columns"]] == ["А"]


def test_rename_keeps_old_name_as_alias(client):
    upload(client, "ЗаказыПоДням", xlsx([(W39, BUN, 5)]))
    sid = client.get("/api/meta").json()["sources"][0]["id"]
    assert client.patch(f"/api/sources/{sid}", json={"name": "Заказ покупателей"}).status_code == 200
    # Файл со старым именем попадает в переименованную колонку, а не создаёт новую.
    assert client.get("/api/source-name", params={"filename": "ЗаказыПоДням_сентябрь_2026.xlsx"}).json() == {
        "source": "Заказ покупателей"}
    res = upload(client, "", xlsx([(W39 + dt.timedelta(days=1), BUN, 7)]), name="заказыподнЯм_2026.xlsx")
    assert res["source"] == "Заказ покупателей"
    meta = client.get("/api/meta").json()
    assert [(s["name"], s["aliases"]) for s in meta["sources"]] == [("Заказ покупателей", ["ЗаказыПоДням"])]
    # Возврат старого имени убирает его из алиасов, новое уходит в алиасы.
    client.patch(f"/api/sources/{sid}", json={"name": "ЗаказыПоДням"})
    assert client.get("/api/meta").json()["sources"][0]["aliases"] == ["Заказ покупателей"]


def test_rename_migration(client):
    for name in ["ЗаказыПоДням", "ОстаткиПоДням", "ПланПроизводстваПоДням", "Прочее",
                 "ФактПроизводстваПоДням", "ПланированиеПроизводства"]:
        upload(client, name, xlsx([(W39, BUN, 1)]))
    from app import db

    with db.pool.connection() as conn:
        db.rename_sources(conn)
    meta = client.get("/api/meta").json()
    assert [s["name"] for s in meta["sources"]] == [
        "Остатки на начало периода", "Заказ склада", "План производства",
        "Выпуск производства", "Заказ покупателей", "Прочее"]
    assert meta["sources"][0]["close_label"] == "Остатки на конец периода"
    p = client.get("/api/pivot", params={"date": "2026-09-21"}).json()
    assert [c["name"] for c in p["columns"]][-1] == "Остатки на конец периода"

    # Повторный запуск ничего не трогает: ручной порядок сохраняется.
    ids = [s["id"] for s in meta["sources"]]
    client.patch(f"/api/sources/{ids[5]}", json={"position": 0})
    with db.pool.connection() as conn:
        db.rename_sources(conn)
    assert client.get("/api/meta").json()["sources"][0]["name"] == "Прочее"


def test_frontend_revalidated_after_deploy(client):
    r = client.get("/static/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js", headers={"if-none-match": r.headers["etag"]}).status_code == 304
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/api/meta").headers


def test_end_of_period(client):
    # Заказ склада: булка 10..24 по дням 14.09–28.09, хлеб — только 21.09 (на конец недели его нет).
    upload(client, "Заказ склада", xlsx(
        [(d, BUN, 10 + i) for i, d in enumerate(days(W38, 15))] + [(W39, LOAF, 3)]))
    from app import db

    # Старая БД: ограничение без end — миграция расширяет его и переводит «Заказ склада» на end.
    with db.pool.connection() as conn:
        conn.execute("ALTER TABLE sources DROP CONSTRAINT sources_agg_check")
        conn.execute("ALTER TABLE sources ADD CONSTRAINT sources_agg_check CHECK (agg IN ('sum', 'last'))")
        conn.commit()
        db.add_end_agg(conn)
    sid = client.get("/api/meta").json()["sources"][0]["id"]
    assert client.get("/api/meta").json()["sources"][0]["agg"] == "end"

    # Неделя 21–27.09: одна колонка на своём месте, срез на 28.09; пред. неделя — на 21.09.
    p = client.get("/api/pivot", params={"mode": "week", "date": "2026-09-23"}).json()
    assert [(c["kind"], c["date"], c["covered"]) for c in p["columns"]] == [("end", "2026-09-28", 1)]
    rows = {r["code"]: r for r in p["rows"]}
    assert rows["001"]["values"][0] == {"cur": 24, "prev": 17, "delta": (24 - 17) / 17}
    assert rows["002"]["values"][0] == {"cur": None, "prev": 3, "delta": None}

    # День 23.09 → срез на 24.09; неделя 28.09 → среза на 05.10 нет.
    d = client.get("/api/pivot", params={"mode": "day", "date": "2026-09-23"}).json()
    assert d["columns"][0]["date"] == "2026-09-24" and d["rows"][0]["values"][0]["cur"] == 20
    p = client.get("/api/pivot", params={"mode": "week", "date": "2026-09-28"}).json()
    assert (p["columns"][0]["date"], p["columns"][0]["covered"]) == ("2026-10-05", 0)

    b = client.get("/api/by-days", params={"source_id": sid, "date": "2026-09-21"}).json()
    assert {r["code"]: (r["total"], r["prev"]) for r in b["rows"]} == {"001": (24, 17), "002": (None, 3)}
    t = client.get("/api/trend", params={"source_id": sid, "end": "2026-09-21", "count": 2}).json()
    assert {r["code"]: r["values"] for r in t["rows"]}["001"] == [17, 24]
    it = client.get("/api/item/001", params={"date": "2026-09-21"}).json()
    assert it["sources"][0]["total"] == 24
    x = client.get("/api/export.xlsx", params={"mode": "week", "date": "2026-09-21"})
    ws = load_workbook(io.BytesIO(x.content))["По дням · Заказ склада"]
    assert "на конец недели" in ws["A2"].value and ws["L5"].value == 24

    # Ручной выбор не перетирается повторным запуском миграции.
    assert client.patch(f"/api/sources/{sid}", json={"agg": "sum"}).status_code == 200
    with db.pool.connection() as conn:
        db.add_end_agg(conn)
    assert client.get("/api/meta").json()["sources"][0]["agg"] == "sum"


def test_mail_attachment_lands_in_pivot(client):
    from email.message import EmailMessage

    from app import mail_import

    msg = EmailMessage()
    msg["From"] = "robot@firma.ru"
    msg.set_content("выгрузка")
    msg.add_attachment(xlsx([(d, BUN, 3) for d in days(W39, 7)]), maintype="application",
                       subtype="octet-stream", filename="Выпуск_сентябрь_2026.xlsx")
    res = mail_import.process_message(msg.as_bytes(), ["robot@firma.ru"])
    assert res[0]["ok"] and res[0]["source"] == "Выпуск"

    p = client.get("/api/pivot", params={"mode": "week", "date": W39.isoformat()}).json()
    assert [c["name"] for c in p["columns"]] == ["Выпуск"]
    assert client.get("/api/mail").json()["enabled"] is False
