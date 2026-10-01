import base64
import datetime as dt
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import psycopg
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import db, export, service
from .parser import parse_xlsx, source_from_filename

STATIC = Path(__file__).parent / "static"
MAX_FILE_MB = 50


@asynccontextmanager
async def lifespan(_app):
    db.init()
    yield
    db.pool.close()


app = FastAPI(title="Instrument", lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """HTTP Basic Auth на всё, кроме /healthz. Выключена, если APP_PASSWORD пуст."""
    password = os.environ.get("APP_PASSWORD", "")
    if password and request.url.path != "/healthz":
        user = os.environ.get("APP_USER", "admin")
        ok = False
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(u.encode(), user.encode()) & secrets.compare_digest(
                    p.encode(), password.encode()
                )
            except ValueError:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="instrument"'})
    return await call_next(request)


@app.middleware("http")
async def no_stale_frontend(request: Request, call_next):
    """Страница и статика — только с проверкой у сервера (ETag → 304), иначе после
    деплоя браузер берёт старый app.js к новому API."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(HTTPException)
async def http_error(_request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def _date(value: str | None, conn=None) -> dt.date:
    """Дата из запроса; без неё — последний день с данными (или сегодня)."""
    if value:
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            raise HTTPException(400, f"Кривая дата: {value}") from None
    row = conn.execute("SELECT MAX(day) AS d FROM movements").fetchone() if conn else None
    return (row and row["d"]) or dt.date.today()


def _found(value, what: str):
    if value is None:
        raise HTTPException(404, f"Нет такого: {what}")
    return value


@app.get("/healthz")
def healthz():
    with db.pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


# ---------- загрузка ----------

def _ingest_file(name: str, source: str, data: bytes) -> dict:
    if len(data) > MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"файл больше {MAX_FILE_MB} МБ")
    parsed = parse_xlsx(data)
    with db.pool.connection() as conn:
        return service.ingest(conn, name, source, parsed)


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...), sources: list[str] = Form(default=[])):
    results = []
    for i, f in enumerate(files):
        name = f.filename or f"file{i + 1}.xlsx"
        source = (sources[i].strip() if i < len(sources) else "") or source_from_filename(name)
        data = await f.read()
        try:
            res = await run_in_threadpool(_ingest_file, name, source, data)
            results.append({"file": name, "ok": True, **res})
        except Exception as e:  # noqa: BLE001 — ошибка одного файла не должна валить пачку
            results.append({"file": name, "ok": False, "source": source, "error": str(e)})
    return {"results": results}


@app.get("/api/source-name")
def source_name(filename: str):
    """Источник по имени файла; переименованный — под текущим именем."""
    name = source_from_filename(filename)
    with db.pool.connection() as conn:
        src = service.resolve_source(conn, name)
    return {"source": src["name"] if src else name}


@app.get("/api/uploads")
def uploads():
    with db.pool.connection() as conn:
        return conn.execute(
            """SELECT u.id, u.filename, s.name AS source, u.date_from, u.date_to, u.rows, u.uploaded_at
               FROM uploads u JOIN sources s ON s.id = u.source_id
               ORDER BY u.uploaded_at DESC"""
        ).fetchall()


@app.delete("/api/uploads/{upload_id}")
def delete_upload(upload_id: int):
    with db.pool.connection() as conn:
        n = conn.execute("DELETE FROM uploads WHERE id = %s", (upload_id,)).rowcount
    if not n:
        raise HTTPException(404, "Нет такой загрузки")
    return {"ok": True}


# ---------- источники ----------

class SourcePatch(BaseModel):
    name: str | None = None
    agg: str | None = None
    hidden: bool | None = None
    position: int | None = None
    close_name: str | None = None


@app.patch("/api/sources/{source_id}")
def patch_source(source_id: int, body: SourcePatch):
    fields = body.model_dump(exclude_unset=True)
    if "agg" in fields and fields["agg"] not in ("sum", "last", "end"):
        raise HTTPException(400, "agg: sum | last | end")
    if "name" in fields:
        fields["name"] = (fields["name"] or "").strip()
        if not fields["name"]:
            raise HTTPException(400, "Пустое имя источника")
    if "close_name" in fields:
        fields["close_name"] = (fields["close_name"] or "").strip() or None  # пусто — по имени источника
    if not fields:
        return {"ok": True}
    sets = [f"{k} = %s" for k in fields]  # ключи — только поля модели
    params = list(fields.values())
    if "name" in fields:
        # Старое имя — в алиасы, чтобы файлы со старым именем шли в эту же колонку.
        # В SET справа name ещё старое.
        sets.append("aliases = CASE WHEN name = %s THEN aliases"
                    " ELSE array_append(array_remove(array_remove(aliases, %s), name), name) END")
        params += [fields["name"], fields["name"]]
    try:
        with db.pool.connection() as conn, conn.transaction():
            n = conn.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id = %s", [*params, source_id]).rowcount
            if n and "name" in fields:
                conn.execute("UPDATE sources SET aliases = array_remove(aliases, %s) WHERE id <> %s",
                             (fields["name"], source_id))
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "Источник с таким именем уже есть") from None
    if not n:
        raise HTTPException(404, "Нет такого источника")
    return {"ok": True}


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int):
    with db.pool.connection() as conn:
        n = conn.execute("DELETE FROM sources WHERE id = %s", (source_id,)).rowcount
    if not n:
        raise HTTPException(404, "Нет такого источника")
    return {"ok": True}


# ---------- таблицы ----------

@app.get("/api/meta")
def meta():
    with db.pool.connection() as conn:
        return service.meta(conn)


@app.get("/api/pivot")
def pivot(mode: str = "week", date: str | None = None):
    if mode not in ("week", "day"):
        raise HTTPException(400, "mode: week | day")
    with db.pool.connection() as conn:
        return service.pivot(conn, mode, _date(date, conn))


@app.get("/api/by-days")
def by_days(source_id: int, date: str | None = None):
    with db.pool.connection() as conn:
        return _found(service.by_days(conn, _date(date, conn), source_id), "источник")


@app.get("/api/trend")
def trend(source_id: int, end: str | None = None, count: int = 8):
    count = max(2, min(count, 52))
    with db.pool.connection() as conn:
        return _found(service.trend(conn, _date(end, conn), count, source_id), "источник")


@app.get("/api/item/{code:path}")
def item(code: str, date: str | None = None):
    with db.pool.connection() as conn:
        return _found(service.item_detail(conn, code, _date(date, conn)), "товар")


@app.get("/api/export.xlsx")
def export_xlsx(mode: str = "week", date: str | None = None, category: str | None = None, q: str | None = None):
    if mode not in ("week", "day"):
        raise HTTPException(400, "mode: week | day")
    with db.pool.connection() as conn:
        day = _date(date, conn)
        table = service.pivot(conn, mode, day)
        ids = dict.fromkeys(c["id"] for c in table["columns"])  # остаток даёт 2 колонки, лист — один
        days = [service.by_days(conn, day, sid) for sid in ids]
    content = export.workbook(table, days, category, q)
    period = table["period"].get("iso") or table["period"]["date"]
    fname = f"instrument_{period}.xlsx"
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
