"""Загрузка фактов в БД и сборка недельных/дневных таблиц."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from .parser import ParsedFile

DAY = dt.timedelta(days=1)
WEEK = dt.timedelta(days=7)
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def week_info(start: dt.date) -> dict:
    y, w, _ = start.isocalendar()
    end = start + 6 * DAY
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "iso": f"{y}-W{w:02d}",
        "label": f"Нед. {w} · {start:%d.%m}–{end:%d.%m.%Y}",
    }


# ---------- загрузка ----------

def ingest(conn, filename: str, source: str, parsed: ParsedFile) -> dict:
    days = parsed.days
    with conn.transaction():
        up_id = conn.execute(
            """INSERT INTO uploads (filename, source, sheets, date_from, date_to, facts)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (filename, source, parsed.sheets, days[0], days[-1], len(parsed.facts)),
        ).fetchone()["id"]

        metric_ids = {}
        for pos, (name, agg) in enumerate(parsed.metrics.items()):
            # DO UPDATE-заглушка нужна, чтобы RETURNING вернул id и у существующей метрики;
            # агрегацию существующей метрики не трогаем — её могли поменять руками.
            metric_ids[name] = conn.execute(
                """INSERT INTO metrics (source, name, agg, position) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (source, name) DO UPDATE SET source = EXCLUDED.source
                   RETURNING id""",
                (source, name, agg, pos),
            ).fetchone()["id"]

        # Новый файл того же источника заменяет данные за свой период.
        replaced = conn.execute(
            """DELETE FROM facts f USING metrics m
               WHERE f.metric_id = m.id AND m.source = %s AND f.day BETWEEN %s AND %s""",
            (source, days[0], days[-1]),
        ).rowcount

        with conn.cursor().copy(
            "COPY facts (metric_id, upload_id, day, dims, value, n) FROM STDIN"
        ) as cp:
            for (name, day, dims), (s, n) in parsed.facts.items():
                cp.write_row((metric_ids[name], up_id, day, dims, s, n))

        conn.execute(
            """DELETE FROM uploads u
               WHERE u.source = %s AND u.id <> %s
                 AND NOT EXISTS (SELECT 1 FROM facts f WHERE f.upload_id = u.id)""",
            (source, up_id),
        )
    return {
        "upload_id": up_id,
        "source": source,
        "sheets": parsed.sheets,
        "metrics": len(parsed.metrics),
        "date_from": days[0].isoformat(),
        "date_to": days[-1].isoformat(),
        "facts": len(parsed.facts),
        "replaced": replaced,
        "warnings": parsed.warnings,
    }


# ---------- агрегация ----------

def _daily(conn, date_from: dt.date, date_to: dt.date, dim_key=None, dim_value=None):
    """metric_id -> {день: (сумма, кол-во)} с учётом фильтра по разрезу."""
    sql = """SELECT metric_id, day, SUM(value) AS s, SUM(n) AS n
             FROM facts WHERE day BETWEEN %s AND %s"""
    params: list = [date_from, date_to]
    if dim_key and dim_value is not None:
        sql += " AND dims ->> %s = %s"
        params += [dim_key, dim_value]
    sql += " GROUP BY metric_id, day"
    out: dict[int, dict[dt.date, tuple[float, int]]] = defaultdict(dict)
    for r in conn.execute(sql, params):
        out[r["metric_id"]][r["day"]] = (r["s"], r["n"])
    return out


def day_value(agg: str, cell) -> float | None:
    if cell is None:
        return None
    s, n = cell
    return s / n if agg == "avg" and n else s


def period_value(agg: str, cells: dict, days: list[dt.date]) -> float | None:
    present = [(d, cells[d]) for d in days if d in cells]
    if not present:
        return None
    if agg == "sum":
        return sum(s for _, (s, _) in present)
    if agg == "avg":
        total_n = sum(n for _, (_, n) in present)
        return sum(s for _, (s, _) in present) / total_n if total_n else None
    return max(present)[1][0]  # last: значение последнего дня с данными


def delta(cur, prev):
    if cur is None or prev in (None, 0):
        return None
    return (cur - prev) / abs(prev)


def _metrics(conn, include_hidden=False):
    sql = """SELECT id, source, name, COALESCE(label, name) AS title, agg, hidden, position
             FROM metrics"""
    if not include_hidden:
        sql += " WHERE NOT hidden"
    return conn.execute(sql + " ORDER BY source, position, id").fetchall()


def _group(rows: list[dict]) -> list[dict]:
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row.pop("source"), []).append(row)
    return [{"source": s, "rows": r} for s, r in groups.items()]


def week_table(conn, start: dt.date, dim_key=None, dim_value=None) -> dict:
    start = monday(start)
    prev_start = start - WEEK
    days = [start + i * DAY for i in range(7)]
    prev_days = [prev_start + i * DAY for i in range(7)]
    daily = _daily(conn, prev_start, days[-1], dim_key, dim_value)

    rows = []
    for m in _metrics(conn):
        cells = daily.get(m["id"])
        if not cells:
            continue
        cur = period_value(m["agg"], cells, days)
        prev = period_value(m["agg"], cells, prev_days)
        if cur is None and prev is None:
            continue
        rows.append({
            "source": m["source"],
            "metric_id": m["id"],
            "name": m["title"],
            "agg": m["agg"],
            "days": [day_value(m["agg"], cells.get(d)) for d in days],
            "total": cur,
            "prev": prev,
            "delta": delta(cur, prev),
        })

    weeks = available_weeks(conn)
    starts = [w["start"] for w in weeks]
    iso = start.isoformat()
    older = [s for s in starts if s < iso]
    newer = [s for s in starts if s > iso]
    return {
        "week": week_info(start),
        "days": [{"date": d.isoformat(), "label": f"{WEEKDAYS[i]} {d:%d.%m}"} for i, d in enumerate(days)],
        "groups": _group(rows),
        "prev_week": max(older) if older else None,
        "next_week": min(newer) if newer else None,
    }


def trend_table(conn, end: dt.date, count: int, dim_key=None, dim_value=None) -> dict:
    last = monday(end)
    starts = [last - (count - 1 - i) * WEEK for i in range(count)]
    daily = _daily(conn, starts[0], last + 6 * DAY, dim_key, dim_value)
    week_days = [[s + i * DAY for i in range(7)] for s in starts]

    rows = []
    for m in _metrics(conn):
        cells = daily.get(m["id"])
        if not cells:
            continue
        values = [period_value(m["agg"], cells, wd) for wd in week_days]
        rows.append({
            "source": m["source"],
            "metric_id": m["id"],
            "name": m["title"],
            "agg": m["agg"],
            "values": values,
            "delta": delta(values[-1], values[-2]) if count > 1 else None,
        })
    return {"weeks": [week_info(s) for s in starts], "groups": _group(rows)}


def series(conn, metric_id: int, date_from: dt.date, date_to: dt.date, dim_key=None, dim_value=None):
    m = conn.execute(
        "SELECT id, source, COALESCE(label, name) AS title, agg FROM metrics WHERE id = %s",
        (metric_id,),
    ).fetchone()
    if not m:
        return None
    date_from, date_to = monday(date_from), monday(date_to) + 6 * DAY
    cells = _daily(conn, date_from, date_to, dim_key, dim_value).get(metric_id, {})
    days, d = [], date_from
    while d <= date_to:
        days.append(d)
        d += DAY
    weeks = []
    for i in range(0, len(days), 7):
        chunk = days[i:i + 7]
        weeks.append({**week_info(chunk[0]), "value": period_value(m["agg"], cells, chunk)})
    return {
        "metric": {"id": m["id"], "source": m["source"], "name": m["title"], "agg": m["agg"]},
        "days": [{"date": d.isoformat(), "value": day_value(m["agg"], cells.get(d))} for d in days],
        "weeks": weeks,
    }


def available_weeks(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT DISTINCT date_trunc('week', day)::date AS wk FROM facts ORDER BY wk DESC"
    ).fetchall()
    return [week_info(r["wk"]) for r in rows]


def dimensions(conn) -> dict[str, list[str]]:
    rows = conn.execute(
        """SELECT e.key, array_agg(DISTINCT e.value ORDER BY e.value) AS vals
           FROM (SELECT DISTINCT dims FROM facts) d, jsonb_each_text(d.dims) e
           GROUP BY e.key ORDER BY e.key"""
    ).fetchall()
    return {r["key"]: r["vals"] for r in rows}
