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
    # Остатки: срез на каждый день, неделя должна брать последний день, а не сумму.
    upload(client, "Остатки", xlsx([(d, BUN, 100 + i) for i, d in enumerate(days(W38, 14))]))

    meta = client.get("/api/meta").json()
    assert [s["name"] for s in meta["sources"]] == ["Выпуск", "Остатки"]
    assert [s["agg"] for s in meta["sources"]] == ["sum", "last"]  # «остат» в имени
    assert [w["iso"] for w in meta["weeks"]] == ["2026-W39", "2026-W38"]

    p = client.get("/api/pivot", params={"mode": "week", "date": "2026-09-23"}).json()
    rows = {r["code"]: r for r in p["rows"]}
    bun = rows["001"]["values"]
    assert bun[0] == {"cur": 70, "prev": 70, "delta": 0}
    assert bun[1]["cur"] == 113 and bun[1]["prev"] == 106  # остаток на воскресенье
    assert rows["002"]["values"][0]["cur"] == 35 and rows["002"]["values"][0]["delta"] is None
    assert [(s["covered"], s["of"]) for s in p["sources"]] == [(7, 7), (7, 7)]

    d = client.get("/api/pivot", params={"mode": "day", "date": "2026-09-23"}).json()
    bun = {r["code"]: r for r in d["rows"]}["001"]["values"]
    assert bun[0]["cur"] == 10 and bun[1]["cur"] == 109 and bun[1]["prev"] == 102

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
    assert [s["name"] for s in p["sources"]] == ["А"]
