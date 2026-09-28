"""Загрузка движений в БД и сборка таблиц: товары × источники за неделю или день."""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict

from .parser import ParsedFile

DAY = dt.timedelta(days=1)
WEEK = dt.timedelta(days=7)
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
LAST_RE = re.compile(r"остат|сальдо|на конец", re.I)


def monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def week_days(start: dt.date) -> list[dt.date]:
    return [start + i * DAY for i in range(7)]


def week_info(start: dt.date) -> dict:
    y, w, _ = start.isocalendar()
    end = start + 6 * DAY
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "iso": f"{y}-W{w:02d}",
        "label": f"Нед. {w} · {start:%d.%m}–{end:%d.%m.%Y}",
    }


def day_info(d: dt.date) -> dict:
    wd = WEEKDAYS[d.weekday()]
    return {"date": d.isoformat(), "label": f"{wd} {d:%d.%m.%Y}", "short": f"{wd} {d:%d.%m}"}


def delta(cur, prev):
    if cur is None or prev in (None, 0):
        return None
    return (cur - prev) / abs(prev)


# ---------- загрузка ----------

def ingest(conn, filename: str, source_name: str, parsed: ParsedFile) -> dict:
    days = parsed.days
    with conn.transaction():
        src = conn.execute(
            """INSERT INTO sources (name, agg, position)
               VALUES (%s, %s, (SELECT COALESCE(MAX(position), 0) + 1 FROM sources))
               ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
               RETURNING id, agg""",
            (source_name, "last" if LAST_RE.search(source_name) else "sum"),
        ).fetchone()

        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO items (code, uid, name, base_unit, pack_size, category)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (code) DO UPDATE SET
                       uid = COALESCE(EXCLUDED.uid, items.uid),
                       name = EXCLUDED.name,
                       base_unit = EXCLUDED.base_unit,
                       pack_size = COALESCE(EXCLUDED.pack_size, items.pack_size),
                       category = EXCLUDED.category""",
                [(i.code, i.uid, i.name, i.base_unit, i.pack_size, i.category) for i in parsed.items.values()],
            )

        upload_id = conn.execute(
            """INSERT INTO uploads (source_id, filename, date_from, date_to, rows)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (src["id"], filename, days[0], days[-1], parsed.rows),
        ).fetchone()["id"]

        # Новый файл того же источника заменяет его данные за свой период.
        replaced = conn.execute(
            "DELETE FROM movements WHERE source_id = %s AND day BETWEEN %s AND %s",
            (src["id"], days[0], days[-1]),
        ).rowcount

        with conn.cursor().copy(
            "COPY movements (source_id, upload_id, day, item_code, qty, qty_base) FROM STDIN"
        ) as cp:
            for (day, code), (qty, qty_base) in parsed.movements.items():
                cp.write_row((src["id"], upload_id, day, code, qty, qty_base))

        conn.execute(
            """DELETE FROM uploads u
               WHERE u.source_id = %s AND u.id <> %s
                 AND NOT EXISTS (SELECT 1 FROM movements m WHERE m.upload_id = u.id)""",
            (src["id"], upload_id),
        )
    return {
        "upload_id": upload_id,
        "source": source_name,
        "date_from": days[0].isoformat(),
        "date_to": days[-1].isoformat(),
        "rows": parsed.rows,
        "items": len(parsed.items),
        "replaced": replaced,
        "warnings": parsed.warnings,
    }


# ---------- справочники ----------

def sources(conn) -> list[dict]:
    return conn.execute(
        """SELECT s.id, s.name, s.agg, s.position, s.hidden,
                  MIN(u.date_from) AS date_from, MAX(u.date_to) AS date_to,
                  COUNT(u.id) AS uploads
           FROM sources s LEFT JOIN uploads u ON u.source_id = s.id
           GROUP BY s.id ORDER BY s.position, s.id"""
    ).fetchall()


def meta(conn) -> dict:
    days = [r["day"] for r in conn.execute("SELECT DISTINCT day FROM movements ORDER BY day DESC")]
    weeks = sorted({monday(d) for d in days}, reverse=True)
    return {
        "sources": sources(conn),
        "weeks": [week_info(w) for w in weeks],
        "days": [day_info(d) for d in days],
    }


# ---------- агрегация ----------

def _load(conn, days: list[dt.date], source_id=None, code=None):
    """(source_id, код) -> {день: qty_base} и source_id -> дни, где у источника есть данные.
    Дни источника считаются по всем товарам: они нужны агрегации «last»."""
    where = "day = ANY(%s)"
    params: list = [days]
    if source_id is not None:
        where += " AND source_id = %s"
        params.append(source_id)

    source_days: dict[int, set[dt.date]] = defaultdict(set)
    for r in conn.execute(f"SELECT DISTINCT source_id, day FROM movements WHERE {where}", params):
        source_days[r["source_id"]].add(r["day"])

    if code is not None:
        where += " AND item_code = %s"
        params.append(code)
    data: dict[tuple[int, str], dict[dt.date, float]] = defaultdict(dict)
    for r in conn.execute(f"SELECT source_id, item_code, day, qty_base FROM movements WHERE {where}", params):
        data[(r["source_id"], r["item_code"])][r["day"]] = r["qty_base"]
    return data, source_days


def aggregate(agg: str, cells: dict, days: list[dt.date], source_days: set) -> float | None:
    """sum — сумма по дням периода; last — значение на последний день периода,
    за который у источника вообще есть данные (срез остатков)."""
    if agg == "last":
        present = [d for d in days if d in source_days]
        return cells.get(max(present)) if present else None
    vals = [cells[d] for d in days if d in cells]
    return sum(vals) if vals else None


def _coverage(conn, days: list[dt.date]) -> dict[int, int]:
    """Сколько дней периода покрыто загруженными файлами каждого источника."""
    cov: dict[int, set] = defaultdict(set)
    for r in conn.execute(
        "SELECT source_id, date_from, date_to FROM uploads WHERE date_to >= %s AND date_from <= %s",
        (days[0], days[-1]),
    ):
        cov[r["source_id"]].update(d for d in days if r["date_from"] <= d <= r["date_to"])
    return {sid: len(ds) for sid, ds in cov.items()}


def _items(conn, codes) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT code, name, base_unit, pack_size, category FROM items WHERE code = ANY(%s)",
        (list(codes),),
    )
    return {r["code"]: r for r in rows}


def _item_row(item: dict) -> dict:
    return {
        "code": item["code"],
        "name": item["name"],
        "category": item["category"],
        "unit": item["base_unit"],
        "pack": item["pack_size"],
    }


def _sort(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r["category"], r["name"]))


def pivot(conn, mode: str, date: dt.date) -> dict:
    """Сводная: товары × источники за неделю (mode=week) или день (mode=day).
    Сравнение: с прошлой неделей / с тем же днём прошлой недели."""
    if mode == "week":
        start = monday(date)
        days, prev_days = week_days(start), week_days(start - WEEK)
        period = {**week_info(start), "compare": "к пред. неделе"}
    else:
        days, prev_days = [date], [date - WEEK]
        period = {**day_info(date), "compare": "к тому же дню пред. недели"}

    srcs = [s for s in sources(conn) if not s["hidden"]]
    data, source_days = _load(conn, days + prev_days)
    items = _items(conn, {code for _, code in data})
    coverage = _coverage(conn, days)

    rows = []
    for code, item in items.items():
        values, has_any = [], False
        for s in srcs:
            cells = data.get((s["id"], code), {})
            cur = aggregate(s["agg"], cells, days, source_days[s["id"]])
            prev = aggregate(s["agg"], cells, prev_days, source_days[s["id"]])
            has_any |= cur is not None or prev is not None
            values.append({"cur": cur, "prev": prev, "delta": delta(cur, prev)})
        if has_any:
            rows.append({**_item_row(item), "values": values})

    return {
        "mode": mode,
        "period": period,
        "days": [day_info(d) for d in days],
        "sources": [
            {"id": s["id"], "name": s["name"], "agg": s["agg"],
             "covered": coverage.get(s["id"], 0), "of": len(days)}
            for s in srcs
        ],
        "rows": _sort(rows),
    }


def _source(conn, source_id: int) -> dict | None:
    return conn.execute("SELECT id, name, agg FROM sources WHERE id = %s", (source_id,)).fetchone()


def by_days(conn, date: dt.date, source_id: int) -> dict | None:
    """Один источник: товары × дни недели + итог, прошлая неделя, Δ."""
    src = _source(conn, source_id)
    if not src:
        return None
    start = monday(date)
    days, prev_days = week_days(start), week_days(start - WEEK)
    data, source_days = _load(conn, days + prev_days, source_id)
    items = _items(conn, {code for _, code in data})
    sd = source_days[source_id]

    rows = []
    for code, item in items.items():
        cells = data[(source_id, code)]
        total = aggregate(src["agg"], cells, days, sd)
        prev = aggregate(src["agg"], cells, prev_days, sd)
        if total is None and prev is None:
            continue
        rows.append({
            **_item_row(item),
            "days": [cells.get(d) for d in days],
            "total": total,
            "prev": prev,
            "delta": delta(total, prev),
        })
    return {
        "period": week_info(start),
        "days": [day_info(d) for d in days],
        "source": src,
        "covered": _coverage(conn, days).get(source_id, 0),
        "rows": _sort(rows),
    }


def trend(conn, end: dt.date, count: int, source_id: int) -> dict | None:
    """Один источник: товары × последние N недель."""
    src = _source(conn, source_id)
    if not src:
        return None
    last = monday(end)
    starts = [last - (count - 1 - i) * WEEK for i in range(count)]
    all_days = [starts[0] + i * DAY for i in range(7 * count)]
    data, source_days = _load(conn, all_days, source_id)
    items = _items(conn, {code for _, code in data})
    sd = source_days[source_id]

    rows = []
    for code, item in items.items():
        cells = data[(source_id, code)]
        values = [aggregate(src["agg"], cells, week_days(s), sd) for s in starts]
        rows.append({**_item_row(item), "values": values, "delta": delta(values[-1], values[-2])})
    return {"weeks": [week_info(s) for s in starts], "source": src, "rows": _sort(rows)}


def item_detail(conn, code: str, date: dt.date, weeks: int = 12) -> dict | None:
    """Карточка товара: источники × дни выбранной недели + ряды по неделям."""
    item = _items(conn, [code]).get(code)
    if not item:
        return None
    start = monday(date)
    days = week_days(start)
    starts = [start - (weeks - 1 - i) * WEEK for i in range(weeks)]
    all_days = [starts[0] + i * DAY for i in range(7 * weeks)]
    data, source_days = _load(conn, all_days, code=code)

    out = []
    for s in sources(conn):
        if s["hidden"]:
            continue
        cells = data.get((s["id"], code), {})
        sd = source_days[s["id"]]
        out.append({
            "id": s["id"],
            "name": s["name"],
            "agg": s["agg"],
            "days": [cells.get(d) for d in days],
            "total": aggregate(s["agg"], cells, days, sd),
            "weeks": [{**week_info(w), "value": aggregate(s["agg"], cells, week_days(w), sd)} for w in starts],
        })
    return {
        "item": _item_row(item),
        "period": week_info(start),
        "days": [day_info(d) for d in days],
        "sources": out,
    }
