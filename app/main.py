import base64
import datetime as dt
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

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


def _date(value: str | None, default: dt.date | None = None) -> dt.date:
    if not value:
        if default is None:
            raise HTTPException(400, "Нужна дата")
        return default
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"Кривая дата: {value}") from None


def _latest_week(conn) -> dt.date:
    weeks = service.available_weeks(conn)
    return dt.date.fromisoformat(weeks[0]["start"]) if weeks else service.monday(dt.date.today())


@app.get("/healthz")
def healthz():
    with db.pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


def _ingest_file(name: str, source: str, data: bytes) -> dict:
    if len(data) > MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"файл больше {MAX_FILE_MB} МБ")
    parsed = parse_xlsx(data)
    if not parsed.facts:
        raise ValueError("не нашёл ни одной даты с числами. " + "; ".join(parsed.warnings))
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
    return {"source": source_from_filename(filename)}


@app.get("/api/uploads")
def uploads():
    with db.pool.connection() as conn:
        return conn.execute(
            """SELECT id, filename, source, sheets, date_from, date_to, facts, uploaded_at
               FROM uploads ORDER BY uploaded_at DESC"""
        ).fetchall()


@app.delete("/api/uploads/{upload_id}")
def delete_upload(upload_id: int):
    with db.pool.connection() as conn:
        n = conn.execute("DELETE FROM uploads WHERE id = %s", (upload_id,)).rowcount
    if not n:
        raise HTTPException(404, "Нет такой загрузки")
    return {"ok": True}


@app.get("/api/sources")
def sources():
    with db.pool.connection() as conn:
        rows = conn.execute("SELECT DISTINCT source FROM metrics ORDER BY source").fetchall()
    return [r["source"] for r in rows]


@app.get("/api/weeks")
def weeks():
    with db.pool.connection() as conn:
        return service.available_weeks(conn)


@app.get("/api/dims")
def dims():
    with db.pool.connection() as conn:
        return service.dimensions(conn)


@app.get("/api/week")
def week(start: str | None = None, dim_key: str | None = None, dim_value: str | None = None):
    with db.pool.connection() as conn:
        day = _date(start, _latest_week(conn))
        return service.week_table(conn, day, dim_key, dim_value)


@app.get("/api/trend")
def trend(end: str | None = None, count: int = 8, dim_key: str | None = None, dim_value: str | None = None):
    count = max(2, min(count, 52))
    with db.pool.connection() as conn:
        day = _date(end, _latest_week(conn))
        return service.trend_table(conn, day, count, dim_key, dim_value)


@app.get("/api/series/{metric_id}")
def series(metric_id: int, date_from: str, date_to: str, dim_key: str | None = None, dim_value: str | None = None):
    with db.pool.connection() as conn:
        res = service.series(conn, metric_id, _date(date_from), _date(date_to), dim_key, dim_value)
    if res is None:
        raise HTTPException(404, "Нет такой метрики")
    return res


@app.get("/api/metrics")
def metrics():
    with db.pool.connection() as conn:
        return conn.execute(
            """SELECT m.id, m.source, m.name, m.label, m.agg, m.hidden,
                      (SELECT MAX(day) FROM facts f WHERE f.metric_id = m.id) AS last_day
               FROM metrics m ORDER BY m.source, m.position, m.id"""
        ).fetchall()


class MetricPatch(BaseModel):
    label: str | None = None
    agg: str | None = None
    hidden: bool | None = None


@app.patch("/api/metrics/{metric_id}")
def patch_metric(metric_id: int, body: MetricPatch):
    if body.agg is not None and body.agg not in ("sum", "avg", "last"):
        raise HTTPException(400, "agg: sum | avg | last")
    fields = body.model_dump(exclude_unset=True)
    if "label" in fields:
        fields["label"] = (fields["label"] or "").strip() or None
    if not fields:
        return {"ok": True}
    sets = ", ".join(f"{k} = %s" for k in fields)
    with db.pool.connection() as conn:
        n = conn.execute(f"UPDATE metrics SET {sets} WHERE id = %s", [*fields.values(), metric_id]).rowcount
    if not n:
        raise HTTPException(404, "Нет такой метрики")
    return {"ok": True}


@app.get("/api/export/week.xlsx")
def export_week(start: str | None = None, dim_key: str | None = None, dim_value: str | None = None):
    with db.pool.connection() as conn:
        day = _date(start, _latest_week(conn))
        table = service.week_table(conn, day, dim_key, dim_value)
    fname = f"instrument_{table['week']['iso']}.xlsx"
    return Response(
        export.week_xlsx(table),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


@app.exception_handler(HTTPException)
async def http_error(_request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
