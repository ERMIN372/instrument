"""Загрузка движений в БД и сборка таблиц: товары × источники за неделю или день."""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict

from psycopg.types.json import Jsonb

from .parser import ParsedFile

DAY = dt.timedelta(days=1)
WEEK = dt.timedelta(days=7)
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
LAST_RE = re.compile(r"остат|сальдо|на конец", re.I)
OPEN_RE = re.compile(r"на начало", re.I)


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

def resolve_source(conn, name: str) -> dict | None:
    """Источник по имени или прежнему имени (без учёта регистра), имя — в приоритете.
    Сравниваем в Python: lower() в PostgreSQL при локали C не знает кириллицу."""
    key = name.casefold()
    rows = conn.execute("SELECT id, name, agg, aliases FROM sources ORDER BY id").fetchall()
    return next((r for r in rows if r["name"].casefold() == key), None) or next(
        (r for r in rows if any(a.casefold() == key for a in r["aliases"])), None)


def ingest(conn, filename: str, source_name: str, parsed: ParsedFile) -> dict:
    days = parsed.days
    with conn.transaction():
        src = resolve_source(conn, source_name) or conn.execute(
            """INSERT INTO sources (name, agg, position)
               VALUES (%s, %s, (SELECT COALESCE(MAX(position), 0) + 1 FROM sources))
               ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
               RETURNING id, name, agg""",
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
        "source": src["name"],
        "date_from": days[0].isoformat(),
        "date_to": days[-1].isoformat(),
        "rows": parsed.rows,
        "items": len(parsed.items),
        "replaced": replaced,
        "warnings": parsed.warnings,
    }


# ---------- справочники ----------

def close_label(src: dict) -> str:
    """Заголовок колонки «остаток на конец»: заданный вручную или из имени источника."""
    if src.get("close_name"):
        return src["close_name"]
    name = src["name"]
    return OPEN_RE.sub("на конец", name) if OPEN_RE.search(name) else f"{name} на конец"


def sources(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT s.id, s.name, s.agg, s.position, s.hidden, s.close_name, s.aliases,
                  MIN(u.date_from) AS date_from, MAX(u.date_to) AS date_to,
                  COUNT(u.id) AS uploads
           FROM sources s LEFT JOIN uploads u ON u.source_id = s.id
           GROUP BY s.id ORDER BY s.position, s.id"""
    ).fetchall()
    return [{**r, "close_label": close_label(r)} for r in rows]


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
    Дни источника считаются по всем товарам: по ним видно, есть ли срез остатков на дату."""
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


def aggregate(agg: str, cells: dict, days: list[dt.date]) -> float | None:
    """sum — сумма по дням периода; last — остаток на начало периода: срез на его первый день.
    Остаток на конец — тот же срез на первый день следующего периода (см. pivot).
    end — значение на конец периода: срез на первый день следующего (неделя 21–27.09 → 28.09),
    поэтому вызывающие грузят на день больше периода."""
    if agg == "last":
        return cells.get(days[0])
    if agg == "end":
        return cells.get(days[-1] + DAY)
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
    Сравнение: с прошлой неделей / с тем же днём прошлой недели.

    Источник-остаток даёт две колонки: «на начало» — срез на первый день периода
    (на своём месте) и «на конец» — срез на первый день следующего периода (в конце
    таблицы). Неделя 21–27.09: начало — 21.09, конец — 28.09."""
    if mode == "week":
        start = monday(date)
        days = week_days(start)
        period = {**week_info(start), "compare": "к пред. неделе"}
    else:
        days = [date]
        period = {**day_info(date), "compare": "к тому же дню пред. недели"}
    prev_days = [d - WEEK for d in days]
    after = days[-1] + DAY

    srcs = [s for s in sources(conn) if not s["hidden"]]
    data, source_days = _load(conn, sorted({*days, *prev_days, after, after - WEEK}))
    items = _items(conn, {code for _, code in data})
    coverage = _coverage(conn, days)

    # (источник, ключ колонки, заголовок, вид, дни периода, дни пред. периода)
    cols = [
        (s, str(s["id"]), s["name"], "open", [days[0]], [prev_days[0]]) if s["agg"] == "last"
        else (s, str(s["id"]), s["name"], s["agg"], days, prev_days)  # sum | end
        for s in srcs
    ] + [
        (s, f"{s['id']}c", s["close_label"], "close", [after], [after - WEEK])
        for s in srcs if s["agg"] == "last"
    ]

    rows = []
    for code, item in items.items():
        values, has_any = [], False
        for s, _key, _name, _kind, cur_days, old_days in cols:
            cells = data.get((s["id"], code), {})
            cur = aggregate(s["agg"], cells, cur_days)
            prev = aggregate(s["agg"], cells, old_days)
            has_any |= cur is not None or prev is not None
            values.append({"cur": cur, "prev": prev, "delta": delta(cur, prev)})
        if has_any:
            rows.append({**_item_row(item), "values": values})

    columns = []
    for s, key, name, kind, cur_days, _old in cols:
        col = {"id": s["id"], "key": key, "name": name, "agg": s["agg"], "kind": kind}
        if kind == "sum":
            col.update(covered=coverage.get(s["id"], 0), of=len(days), date=None)
        else:  # срез: есть ли у источника данные на нужный день
            day = after if kind == "end" else cur_days[0]
            col.update(covered=int(day in source_days[s["id"]]), of=1, date=day.isoformat())
        columns.append(col)

    return {
        "mode": mode,
        "period": period,
        "days": [day_info(d) for d in days],
        "columns": columns,
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
    data, _ = _load(conn, prev_days + days + [days[-1] + DAY], source_id)  # +день: срез «на конец»
    items = _items(conn, {code for _, code in data})

    rows = []
    for code, item in items.items():
        cells = data[(source_id, code)]
        total = aggregate(src["agg"], cells, days)
        prev = aggregate(src["agg"], cells, prev_days)
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
    all_days = [starts[0] + i * DAY for i in range(7 * count + 1)]  # +день: срез «на конец»
    data, _ = _load(conn, all_days, source_id)
    items = _items(conn, {code for _, code in data})

    rows = []
    for code, item in items.items():
        cells = data[(source_id, code)]
        values = [aggregate(src["agg"], cells, week_days(s)) for s in starts]
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
    all_days = [starts[0] + i * DAY for i in range(7 * weeks + 1)]  # +день: срез «на конец»
    data, _ = _load(conn, all_days, code=code)

    out = []
    for s in sources(conn):
        if s["hidden"]:
            continue
        cells = data.get((s["id"], code), {})
        out.append({
            "id": s["id"],
            "name": s["name"],
            "agg": s["agg"],
            "days": [cells.get(d) for d in days],
            "total": aggregate(s["agg"], cells, days),
            "weeks": [{**week_info(w), "value": aggregate(s["agg"], cells, week_days(w))} for w in starts],
        })
    return {
        "item": _item_row(item),
        "period": week_info(start),
        "days": [day_info(d) for d in days],
        "sources": out,
    }


# ---------- товародвиженец РЦ ----------

RC_ROLES = {
    # роль -> (заголовок, как узнать источник по имени, если не выбран вручную)
    "stock": ("Остаток", re.compile(r"остат", re.I)),
    "order": ("Заказ", re.compile(r"заказ\w* покуп|заказ", re.I)),
    "output": ("Выпуск", re.compile(r"выпуск|факт", re.I)),
}


def rc_settings(conn) -> dict[str, int | None]:
    """Какие источники — остаток, заказ и выпуск. Выбор хранится в settings (общий для всех);
    не выбран или источник удалён — первый подходящий по имени."""
    srcs = sources(conn)
    ids = {s["id"] for s in srcs}
    row = conn.execute("SELECT value FROM settings WHERE key = 'rc'").fetchone()
    saved = row["value"] if row else {}
    out = {}
    for role, (_label, name_re) in RC_ROLES.items():
        sid = saved.get(role)
        if sid not in ids:
            # «Заказ покупателей» раньше «Заказа склада»: шаблон с покуп ищется первым
            alts = name_re.pattern.split("|")
            sid = next((s["id"] for alt in alts for s in srcs if re.search(alt, s["name"], re.I)), None)
        out[role] = sid
    return out


def save_rc_settings(conn, roles: dict[str, int | None]) -> None:
    value = {k: v for k, v in roles.items() if k in RC_ROLES}
    conn.execute(
        """INSERT INTO settings (key, value) VALUES ('rc', %s)
           ON CONFLICT (key) DO UPDATE SET value = settings.value || EXCLUDED.value""",
        (Jsonb(value),),
    )


def rc(conn, date: dt.date) -> dict:
    """Вкладка товародвиженца РЦ по неделе даты: остаток на среду, заказ и выпуск
    по дням Чт–Вс, остаток на понедельник следующей недели (неделя 21–27.09:
    остаток 23.09, заказ/выпуск 24–27.09, остаток 28.09).
    Остатки — значения источника на день как есть (срез), без пересчёта."""
    start = monday(date)
    wed = start + 2 * DAY
    flow = [wed + i * DAY for i in range(1, 5)]  # Чт–Вс
    close = start + WEEK                           # Пн следующей недели
    roles = rc_settings(conn)
    by_id = {s["id"]: s for s in sources(conn)}

    # (роль, день, заголовок)
    cols = [("stock", wed, "Остаток")] + [("order", d, "Заказ") for d in flow] \
        + [("output", d, "Выпуск") for d in flow] + [("stock", close, "Остаток")]
    data, source_days = _load(conn, [wed, *flow, close])
    used = {sid for sid in roles.values() if sid}
    items = _items(conn, {code for sid, code in data if sid in used})

    rows = []
    for code, item in items.items():
        values = [data.get((roles[role], code), {}).get(day) if roles[role] else None for role, day, _ in cols]
        if any(v is not None for v in values):
            rows.append({**_item_row(item), "values": values})

    columns = []
    for i, (role, day, label) in enumerate(cols):
        sid = roles[role]
        columns.append({
            "key": f"c{i}",
            "role": role,
            "label": label,
            "day": day_info(day),
            "covered": bool(sid and day in source_days[sid]),
        })
    return {
        "period": {
            **week_info(start),
            "label": f"Ср {wed:%d.%m} → Пн {close:%d.%m.%Y} · нед. {start.isocalendar()[1]}",
        },
        "roles": {role: {"id": sid, "name": by_id[sid]["name"]} if sid else None for role, sid in roles.items()},
        "columns": columns,
        "rows": _sort(rows),
    }
