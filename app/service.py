"""Загрузка движений в БД и сборка таблиц: товары × источники за неделю или день."""
from __future__ import annotations

import datetime as dt
import math
import re
import statistics
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


def date_range(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + i * DAY for i in range((end - start).days + 1)]


def range_info(start: dt.date, end: dt.date) -> dict:
    n = (end - start).days + 1
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": n,
        "label": f"{start:%d.%m.%Y}–{end:%d.%m.%Y} ({n} дн.)",
    }


def range_compare(days: list[dt.date]) -> str:
    """Подпись сравнения произвольного периода: столько же дней сразу перед ним."""
    n = len(days)
    return f"к пред. {n} дн. ({days[0] - n * DAY:%d.%m}–{days[0] - DAY:%d.%m})"


def day_info(d: dt.date) -> dict:
    wd = WEEKDAYS[d.weekday()]
    return {"date": d.isoformat(), "label": f"{wd} {d:%d.%m.%Y}", "short": f"{wd} {d:%d.%m}"}


def wd(d: dt.date) -> str:
    """День недели строчными для подписей: «ср», «пн»."""
    return WEEKDAYS[d.weekday()].lower()


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


def ingest(conn, filename: str, source_name: str, parsed: ParsedFile, via: str = "site") -> dict:
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
            """INSERT INTO uploads (source_id, filename, date_from, date_to, rows, via)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (src["id"], filename, days[0], days[-1], parsed.rows, via),
        ).fetchone()["id"]

        # Новый файл того же источника заменяет его данные только за дни, которые в нём есть:
        # выгрузки идут скользящим окном (−14…+14 дн. от сегодня), поэтому дни вне файла и дни
        # без строк в нём остаются как были. День из файла заменяется целиком — товара, которого
        # в нём больше нет (заказ отменили, остаток обнулился), за этот день не останется.
        replaced = conn.execute(
            "DELETE FROM movements WHERE source_id = %s AND day = ANY(%s)",
            (src["id"], days),
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


def _covered_days(conn, days: list[dt.date]) -> dict[int, set[dt.date]]:
    """Дни периода, покрытые загруженными файлами каждого источника. Для движений (заказ,
    выпуск) нет строки в покрытый день — значит, движения не было, а не «нет данных»."""
    cov: dict[int, set] = defaultdict(set)
    for r in conn.execute(
        "SELECT source_id, date_from, date_to FROM uploads WHERE date_to >= %s AND date_from <= %s",
        (min(days), max(days)),
    ):
        cov[r["source_id"]].update(d for d in days if r["date_from"] <= d <= r["date_to"])
    return cov


def _coverage(conn, days: list[dt.date]) -> dict[int, int]:
    """Сколько дней периода покрыто загруженными файлами каждого источника."""
    return {sid: len(ds) for sid, ds in _covered_days(conn, days).items()}


CURRENT, FUTURE = "на текущий период", "на будущий период"
SPENT_WEEKS = 2  # расход в сводной — за столько недель до периода
BUYERS_RE = re.compile(r"заказ\w* покуп", re.I)  # расход в сводной — по заказу покупателей


def future_after_next(groups: list[tuple[list, list]]) -> list:
    """Раскладка колонок/строк источников по порядку: (свои, «будущие») для каждого источника.
    «Будущий» заказ склада встаёт после следующего источника, как договорились с заказчиком:
    заказ склада на текущий период → заказ покупателей → заказ склада на будущий период."""
    out, pending = [], []
    for own, future in groups:
        out += own + pending
        pending = list(future)
    return out + pending


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


def pivot(conn, mode: str, date: dt.date, date_to: dt.date | None = None) -> dict:
    """Сводная: товары × источники за неделю (mode=week), день (mode=day) или
    произвольный период date…date_to включительно (mode=range).
    Сравнение: с прошлой неделей / с тем же днём прошлой недели / с таким же
    числом дней сразу перед периодом (01–10.09 → 22–31.08).

    Источник-остаток даёт две колонки: «на начало» — срез на первый день периода
    (на своём месте) и «на конец» — срез на первый день следующего периода (в конце
    таблицы). Неделя 21–27.09: начало — 21.09, конец — 28.09."""
    if mode == "week":
        start = monday(date)
        days = week_days(start)
        period = {**week_info(start), "compare": "к пред. неделе"}
    elif mode == "range":
        days = date_range(date, date_to)
        period = {**range_info(date, date_to), "compare": range_compare(days)}
    else:
        days = [date]
        period = {**day_info(date), "compare": "к тому же дню пред. недели"}
    shift = len(days) * DAY if mode == "range" else WEEK
    prev_days = [d - shift for d in days]
    after = days[-1] + DAY
    # Расход — заказ покупателей за две недели до недели начала периода, как во вкладке
    # «Динамика»: неделя свёрнута по способу источника (сумма за Пн–Вс). Период с 05.10 →
    # нед. 39 (21–27.09) и нед. 40 (28.09–04.10); Δ — к неделе раньше, поэтому грузим и нед. 38.
    spent = [monday(days[0]) - (SPENT_WEEKS - i) * WEEK for i in range(SPENT_WEEKS)]
    spent_days = date_range(spent[0] - WEEK, spent[-1] + WEEK)  # +день: срез «на конец»

    srcs = [s for s in sources(conn) if not s["hidden"]]
    data, source_days = _load(conn, sorted({*days, *prev_days, after, after - shift, *spent_days}))
    items = _items(conn, {code for _, code in data})
    covered = _covered_days(conn, [*days, *spent_days])

    # (источник, ключ колонки, заголовок, вид, дни периода, дни пред. периода)
    # Заказ склада (end) — две колонки: на текущий период — срез на первый день периода
    # (14.09), на будущий — на первый день следующего (21.09), после следующего источника.
    def group(s):
        if s["agg"] == "last":
            return [(s, str(s["id"]), s["name"], "open", [days[0]], [prev_days[0]])], []
        if s["agg"] == "end":
            return ([(s, f"{s['id']}t", f"{s['name']} {CURRENT}", "current", [days[0]], [prev_days[0]])],
                    [(s, str(s["id"]), f"{s['name']} {FUTURE}", "end", days, prev_days)])
        return [(s, str(s["id"]), s["name"], s["agg"], days, prev_days)], []

    buyers = [s for s in srcs if BUYERS_RE.search(s["name"])]

    def spent_name(s, label):  # заказ покупателей обычно один — тогда без имени источника
        return f"Расход {label}" if len(buyers) == 1 else f"{s['name']}: расход {label}"

    cols = future_after_next([group(s) for s in srcs]) + [
        (s, f"{s['id']}c", s["close_label"], "close", [after], [after - shift])
        for s in srcs if s["agg"] == "last"
    ] + [  # расход по неделям — в самом конце, после остатков на конец
        (s, f"{s['id']}w{i}", spent_name(s, f"нед. {w.isocalendar()[1]}"), "spent",
         week_days(w), week_days(w - WEEK))
        for s in buyers for i, w in enumerate(spent)
    ]

    rows = []
    for code, item in items.items():
        values, has_any = [], False
        for s, _key, _name, kind, cur_days, old_days in cols:
            cells = data.get((s["id"], code), {})
            agg = "last" if kind == "current" else s["agg"]  # текущий заказ — срез на первый день
            cur = aggregate(agg, cells, cur_days)
            prev = aggregate(agg, cells, old_days)
            has_any |= cur is not None or prev is not None
            values.append({"cur": cur, "prev": prev, "delta": delta(cur, prev)})
        if has_any:
            rows.append({**_item_row(item), "values": values})

    columns = []
    for s, key, name, kind, cur_days, _old in cols:
        col = {"id": s["id"], "key": key, "name": name, "agg": s["agg"], "kind": kind}
        agg = "last" if kind == "current" else s["agg"]
        if agg == "sum":  # сколько дней покрыто загруженными файлами
            col.update(covered=len(covered[s["id"]] & set(cur_days)), of=len(cur_days), date=None)
        else:  # срез: есть ли у источника данные на нужный день
            day = cur_days[-1] + DAY if agg == "end" else cur_days[0]
            col.update(covered=int(day in source_days[s["id"]]), of=1, date=day.isoformat())
        if kind == "spent":  # в заголовке — сама неделя, как в «Динамике»
            col["week"] = week_info(cur_days[0])
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


def by_days(conn, date: dt.date, source_id: int, date_to: dt.date | None = None) -> dict | None:
    """Один источник: товары × дни недели + итог, прошлая неделя, Δ.
    С date_to — дни периода date…date_to, сравнение с таким же числом дней перед ним."""
    src = _source(conn, source_id)
    if not src:
        return None
    if date_to is None:
        start = monday(date)
        days, prev_days = week_days(start), week_days(start - WEEK)
        period = week_info(start)
    else:
        days = date_range(date, date_to)
        prev_days = [d - len(days) * DAY for d in days]
        period = {**range_info(date, date_to), "compare": range_compare(days)}
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
        "period": period,
        "days": [day_info(d) for d in days],
        "source": src,
        "covered": _coverage(conn, days).get(source_id, 0),
        "rows": _sort(rows),
    }


def trend(conn, end: dt.date, count: int, source_id: int, start: dt.date | None = None) -> dict | None:
    """Один источник: товары × последние N недель. Со start — недели, которые задевает период
    start…end, целиком Пн–Вс (03–17.09 → недели 31.08, 07.09 и 14.09): обрезанная неделя
    не сравнима с соседними."""
    src = _source(conn, source_id)
    if not src:
        return None
    last = monday(end)
    if start is not None:
        count = (last - monday(start)).days // 7 + 1
    starts = [last - (count - 1 - i) * WEEK for i in range(count)]
    all_days = [starts[0] + i * DAY for i in range(7 * count + 1)]  # +день: срез «на конец»
    data, _ = _load(conn, all_days, source_id)
    items = _items(conn, {code for _, code in data})

    rows = []
    for code, item in items.items():
        cells = data[(source_id, code)]
        values = [aggregate(src["agg"], cells, week_days(s)) for s in starts]
        rows.append({**_item_row(item), "values": values,
                     "delta": delta(values[-1], values[-2]) if count > 1 else None})
    return {
        "weeks": [week_info(s) for s in starts],
        "period": range_info(start, end) if start is not None else None,
        "source": src,
        "rows": _sort(rows),
    }


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

    groups = []
    for s in sources(conn):
        if s["hidden"]:
            continue
        cells = data.get((s["id"], code), {})
        row = {
            "id": s["id"],
            "name": s["name"],
            "agg": s["agg"],
            "label": None,
            "as_of": None,
            "days": [cells.get(d) for d in days],
            "total": aggregate(s["agg"], cells, days),
            "weeks": [{**week_info(w), "value": aggregate(s["agg"], cells, week_days(w))} for w in starts],
        }
        if s["agg"] != "end":
            groups.append(([row], []))
            continue
        # Заказ склада — как в сводной: на текущий период — заказ на пн этой недели (по дням
        # как есть), на будущий — на пн следующей, под него производят эту неделю. График — по нему.
        current = {**row, "label": CURRENT, "total": cells.get(days[0]), "weeks": []}
        future = {**row, "label": FUTURE, "as_of": (days[-1] + DAY).isoformat(), "days": [None] * 7}
        groups.append(([current], [future]))
    out = future_after_next(groups)
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
    "consumption": ("Потребление", re.compile(r"отгруз|реализац|продаж|заказ\w* покуп", re.I)),
}
RC_WEEKS = 3  # потребление — за столько недель до выбранной
RC_DAILY_MAX = 14  # период длиннее — заказ и выпуск одной суммой, а не колонкой на каждый день


def rc_settings(conn) -> dict[str, int | None]:
    """Какие источники — остаток, заказ, выпуск и потребление. Выбор хранится в settings (общий для всех);
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


def rc_days(start: dt.date) -> tuple[dt.date, list[dt.date], dt.date]:
    """Среда, дни движения Ср–Вс и пн следующей недели: неделя 21–27.09 → 23.09, 23–27.09, 28.09.
    Остаток 1С — на начало дня, поэтому движение самой среды тоже в расчёт."""
    wed = start + 2 * DAY
    return wed, [wed + i * DAY for i in range(5)], start + WEEK


def rc_window(date: dt.date, date_to: dt.date | None = None) -> tuple[dt.date, list[dt.date], dt.date]:
    """Срез остатка, дни движения и день расчётного остатка. Без date_to — неделя даты, как
    договорились с РЦ (Ср, Ср–Вс, пн следующей); с date_to — произвольный период: срез на его
    первый день, движение за все его дни, расчёт на день после последнего (01–10.09 → 01.09,
    01–10.09, 11.09). Период Ср–Вс считается ровно как неделя."""
    if date_to is None:
        return rc_days(monday(date))
    return date, date_range(date, date_to), date_to + DAY


def calc_stock(start: float | None, orders: list, outputs: list) -> float | None:
    """Остаток ср − заказ + выпуск; нет заказа или выпуска за день — 0, нет остатка — не считаем."""
    if start is None:
        return None
    return start - sum(v for v in orders if v is not None) + sum(v for v in outputs if v is not None)


def rc(conn, date: dt.date, date_to: dt.date | None = None) -> dict:
    """Вкладка товародвиженца РЦ по неделе даты: остаток на среду, заказ и выпуск
    по дням Ср–Вс, остаток на понедельник следующей недели (неделя 21–27.09:
    остаток 23.09, заказ/выпуск 23–27.09, остаток 28.09), справа — потребление
    по трём предыдущим неделям (31.08–20.09), чтобы сразу прикинуть заказ.
    Остаток на среду — срез из файла; остаток на понедельник — расчёт по формуле,
    согласованной с РЦ: остаток ср − заказ Ср–Вс + выпуск Ср–Вс (остаток 1С на пн
    не берём: на будущие даты 1С повторяет последний остаток).
    С date_to — произвольный период (см. rc_window): тот же расчёт от его первого дня до дня
    после последнего; длиннее RC_DAILY_MAX дн. — заказ и выпуск одной суммой за период.
    Потребление — три недели до недели конца периода (самые свежие полные недели, как расход
    в автозаказе), свёртка по способу источника (обычно сумма)."""
    start = monday(date_to or date)
    stock_day, flow, close = rc_window(date, date_to)
    past = [start - (RC_WEEKS - i) * WEEK for i in range(RC_WEEKS)]  # от старой к новой
    roles = rc_settings(conn)
    by_id = {s["id"]: s for s in sources(conn)}

    def moves(role, label):  # заказ и выпуск: по дням или одной суммой за длинный период
        if len(flow) <= RC_DAILY_MAX:
            return [(role, label, d, None) for d in flow]
        return [(role, label, None, flow)]

    # (роль, заголовок, день или None, дни для суммы: потребление за неделю, движение за период)
    cols = [("stock", "Остаток", stock_day, None)] + moves("order", "Заказ") + moves("output", "Выпуск") \
        + [("calc", "Остаток расчёт", close, None)] \
        + [("consumption", f"Потребл. нед. {w.isocalendar()[1]}", None, week_days(w)) for w in past]
    # +start: срез «на конец» последней прошлой недели для источников agg = end
    data, source_days = _load(conn, sorted({stock_day, *flow, close, start, *(d for w in past for d in week_days(w))}))
    used = {sid for sid in roles.values() if sid}
    items = _items(conn, {code for sid, code in data if sid in used})
    file_days = _covered_days(conn, flow)

    def value(role, day, days, code):
        sid = roles.get(role)
        if not sid:
            return None
        cells = data.get((sid, code), {})
        if day:
            return cells.get(day)
        # заказ и выпуск за период — сумма дней, как в расчёте по дням, при любом способе источника
        return aggregate(by_id[sid]["agg"] if role == "consumption" else "sum", cells, days)

    n = sum(role == "order" for role, *_ in cols)
    rows = []
    for code, item in items.items():
        values = [value(role, day, days, code) for role, _, day, days in cols]
        values[1 + 2 * n] = calc_stock(values[0], values[1:1 + n], values[1 + n:1 + 2 * n])
        if any(v is not None for v in values):
            rows.append({**_item_row(item), "values": values})

    columns = []
    for i, (role, label, day, days) in enumerate(cols):
        sid = roles.get(role)
        if role == "calc":  # считается, если есть остаток на среду
            sub, covered, of = day_info(day)["short"], columns[0]["covered"], 1
        elif day:
            # остаток — срез: нужен срез на этот день; заказ/выпуск — достаточно, что файл за день загружен
            # (за длинный период — сколько дней покрыто файлами, как у потребления, ниже)
            seen = source_days if role == "stock" else file_days
            sub, covered, of = day_info(day)["short"], int(bool(sid and day in seen[sid])), 1
        else:
            sub = f"{days[0]:%d.%m}–{days[-1]:%d.%m}"
            covered, of = (_coverage(conn, days).get(sid, 0) if sid else 0), len(days)
        columns.append({"key": f"c{i}", "role": role, "label": label, "sub": sub, "covered": covered, "of": of})

    if date_to is None:
        period = {**week_info(start), "label": f"Ср {stock_day:%d.%m} → Пн {close:%d.%m.%Y} · нед. {start.isocalendar()[1]}"}
        note = "Остаток ср — из 1С, остаток пн — расчёт: остаток ср − заказ Ср–Вс + выпуск Ср–Вс."
    else:
        span = f"{flow[0]:%d.%m}–{flow[-1]:%d.%m}"
        info = range_info(date, date_to)
        period = {**info, "label": f"{info['label']} → остаток на {close:%d.%m.%Y}"}
        note = (f"Остаток {stock_day:%d.%m} — из 1С, остаток {close:%d.%m} — расчёт: остаток {stock_day:%d.%m} − "
                f"заказ {span} + выпуск {span}"
                + (f" (дней больше {RC_DAILY_MAX} — одной суммой)." if len(flow) > RC_DAILY_MAX else "."))
    note += f" Потребление — {RC_WEEKS} недели до {start:%d.%m}."
    return {
        "mode": "week" if date_to is None else "range",
        "period": period,
        "note": note,
        "roles": {role: {"id": sid, "name": by_id[sid]["name"]} if sid else None for role, sid in roles.items()},
        "columns": columns,
        "rows": _sort(rows),
    }


# ---------- автозаказ (демо) ----------

AUTO_WEEKS = 8  # история потребления для формулы Г; столько же недель — прогон по истории
# Покрытие: остаток пн + заказ = столько недель потребления. Заказчик: «x2» — при потреблении
# 10 000 и остатке 6 000 заказ 14 000, а не 24 000. k — при σ в формуле Г.
AUTO_DEFAULTS = {"cover_weeks": 2, "k": 1}
AUTO_FORMULAS = [("a", "А · среднее 3 нед."), ("b", "Б · взвешенное 3 нед."),
                 ("c", "В · максимум 3 нед."), ("d", "Г · среднее 8 нед. + σ")]
AUTO_WEIGHTS = (0.2, 0.3, 0.5)  # Б: от старой недели к свежей


def auto_settings(conn) -> dict:
    """Параметры автозаказа, общие для всех: покрытие в неделях потребления и k для σ в формуле Г.
    Чужие ключи (stock_weeks первой версии — запас на конец недели) не берём."""
    row = conn.execute("SELECT value FROM settings WHERE key = 'autoorder'").fetchone()
    saved = row["value"] if row else {}
    return {k: saved.get(k, v) for k, v in AUTO_DEFAULTS.items()}


def save_auto_settings(conn, values: dict) -> None:
    value = {k: v for k, v in values.items() if k in AUTO_DEFAULTS}
    conn.execute(
        """INSERT INTO settings (key, value) VALUES ('autoorder', %s)
           ON CONFLICT (key) DO UPDATE SET value = settings.value || EXCLUDED.value""",
        (Jsonb(value),),
    )


def round_order(x: float) -> float:
    """Без мелочи, как просил заказчик: от 1 000 — до сотен (21 236 → 21 200), от 100 — до десятков,
    меньше — до целых, иначе мелкие позиции округлялись бы в ноль."""
    step = 100 if x >= 1000 else 10 if x >= 100 else 1
    return math.floor(x / step + 0.5) * step


def hist_stats(hist: list) -> tuple[float | None, float | None]:
    """Среднее и разброс σ (стандартное отклонение) по неделям, где потребление известно."""
    vals = [v for v in hist if v is not None]
    if not vals:
        return None, None
    return statistics.fmean(vals), statistics.stdev(vals) if len(vals) > 1 else 0.0


def auto_rates(hist: list, k: float) -> list:
    """Расход в неделю по формулам А–Г: (расход, добавка — у Г k·σ, сколько недель в расчёте).
    hist — потребление за AUTO_WEEKS прошлых недель от старой к свежей, None — неделя загружена
    не целиком и в расчёт не идёт. Нет истории — None."""
    last = [(w, v) for w, v in zip(AUTO_WEIGHTS, hist[-3:]) if v is not None]
    vals = [v for _, v in last]
    mean, sigma = hist_stats(hist)
    n8 = sum(v is not None for v in hist)
    return [
        (sum(vals) / len(vals), 0, len(vals)) if vals else None,
        (sum(w * v for w, v in last) / sum(w for w, _ in last), 0, len(vals)) if vals else None,
        (max(vals), 0, len(vals)) if vals else None,
        (mean, k * sigma, n8) if mean is not None else None,
    ]


def auto_orders(hist: list, stock: float | None, cover_weeks: float, k: float) -> list:
    """Заказ на следующую неделю по формулам А–Г: покрытие × расход в неделю − остаток пн, у Г
    ещё + k·σ; не меньше нуля, с округлением. Остаток пн + заказ = покрытие × расход.
    Нет остатка или истории — None."""
    if stock is None:
        return [None] * len(AUTO_FORMULAS)
    return [None if r is None else round_order(max(0, cover_weeks * r[0] + r[1] - stock))
            for r in auto_rates(hist, k)]


def _num(x: float) -> str:
    """Число в подписи: от 100 — целое с пробелами по тысячам (1 033), меньше — с десятой (11,7)."""
    s = f"{x:,.0f}" if abs(x) >= 100 else f"{x:.1f}".rstrip("0").rstrip(".")
    return s.replace(",", "\u00a0").replace(".", ",")


def auto_offers(hist: list, stock: float | None, cover_weeks: float, k: float, stock_day: dt.date) -> list[dict]:
    """Предложение по формулам А–Г и «почему так» — одной строкой с цифрами расчёта."""
    out = []
    for r, order, full in zip(auto_rates(hist, k), auto_orders(hist, stock, cover_weeks, k), (3, 3, 3, AUTO_WEEKS)):
        if stock is None:
            why = f"нет остатка на {wd(stock_day)} {stock_day:%d.%m}"
        elif r is None:
            why = "нет расхода за прошлые недели"
        elif cover_weeks * r[0] + r[1] <= stock:
            why = f"остатка {_num(stock)} хватает, нужно {_num(cover_weeks * r[0] + r[1])}"
        else:
            rate, extra, n = r
            why = f"расход {_num(rate)}" + (f" (за {n} нед.)" if n < full else "") + f" × {_num(cover_weeks)}"
            if extra:
                why += f" + разброс {_num(extra)}"
            why += f" − остаток {_num(stock)}" if stock >= 0 else f" + минус на складе {_num(-stock)}"
        out.append({"value": order, "why": why})
    return out


def autoorder(conn, date: dt.date, today: dt.date | None = None, date_to: dt.date | None = None) -> dict:
    """Автозаказ (демо): сколько заказать складу на следующую неделю по четырём формулам —
    на экране только предложение и «почему так». Считаем в среду, как во вкладке РЦ, и её же
    источниками: неделя 21–27.09 → остаток на пн 28.09 = ост. 23.09 − заказ 23–27.09 + выпуск
    23–27.09, заказ — на неделю 28.09–04.10, расход — прошлые недели (А–В — три, Г — восемь),
    неполные недели не в счёт.
    Прогон по истории (только в xlsx): те же формулы на AUTO_WEEKS прошлых неделях против
    потребления недели, на которую заказывали, и рядом — фактический «Заказ склада» (первый
    источник со способом end): покрытие = (остаток пн + заказ) / потребление, меньше 1 — к концу
    недели остаток в минусе. Берём только прошедшие недели — на будущие 1С отдаёт заказы
    покупателей не целиком.
    С date_to — произвольный период, остаток считаем как во вкладке РЦ (rc_window): срез на первый
    день − заказ + выпуск за период → остаток на день после последнего, заказ — с этого дня. Расход и
    прогон — недели до недели конца периода: самые свежие полные недели перед заказом."""
    today = today or dt.date.today()
    start = monday(date_to or date)
    stock_day, flow, close = rc_window(date, date_to)
    roles = rc_settings(conn)
    params = auto_settings(conn)
    srcs = sources(conn)
    by_id = {s["id"]: s for s in srcs}
    wh = next((s for s in srcs if s["agg"] == "end"), None)
    first = start - 2 * AUTO_WEEKS * WEEK  # история для самой старой недели прогона
    days = date_range(min(first, stock_day), start + 6 * DAY)  # длинный период начинается раньше истории
    data, source_days = _load(conn, days)
    cons_sid = roles["consumption"]
    cons_agg = by_id[cons_sid]["agg"] if cons_sid else "sum"
    cons_days = _covered_days(conn, days).get(cons_sid, set())
    full = {w for w in (first + i * WEEK for i in range(2 * AUTO_WEEKS + 1))
            if all(d in cons_days for d in week_days(w))}

    def cells(sid, code):
        return data.get((sid, code), {}) if sid else {}

    def cons(code, w):  # потребление за неделю; в загруженной неделе нет строк — значит, ноль
        if not cons_sid or w not in full:
            return None
        v = aggregate(cons_agg, cells(cons_sid, code), week_days(w))
        return 0 if v is None and cons_agg == "sum" else v

    def hist(code, w):  # AUTO_WEEKS недель до недели w, от старой к свежей
        return [cons(code, w - (AUTO_WEEKS - i) * WEEK) for i in range(AUTO_WEEKS)]

    def stock(code, day, flow):  # расчётный остаток после движения flow от среза на day — как во вкладке РЦ
        orders, outputs = cells(roles["order"], code), cells(roles["output"], code)
        return calc_stock(cells(roles["stock"], code).get(day),
                          [orders.get(d) for d in flow], [outputs.get(d) for d in flow])

    def fact(code, w):  # заказ склада на неделю после w — срез на её пн; товара нет в срезе — 0
        if not wh or w + WEEK not in source_days[wh["id"]]:
            return None
        return cells(wh["id"], code).get(w + WEEK, 0)

    used = {sid for sid in roles.values() if sid}
    items = _items(conn, {code for sid, code in data if sid in used})
    cw, k = params["cover_weeks"], params["k"]

    rows = []
    for code, item in items.items():
        h, o = hist(code, start), stock(code, stock_day, flow)
        if any(h) or o:  # ни расхода, ни остатка — нечего предлагать
            rows.append({**_item_row(item), "offers": auto_offers(h, o, cw, k, stock_day)})

    # Прогон: недели до выбранной, у которых следующая (на неё и заказывали) уже прошла и загружена.
    tested = [w for w in (start - (AUTO_WEEKS - i) * WEEK for i in range(AUTO_WEEKS))
              if w + WEEK in full and w + WEEK + 6 * DAY < today]
    keys = [key for key, _ in AUTO_FORMULAS] + ["fact"]
    covers = defaultdict(list)  # формула -> (остаток пн + заказ) в неделях потребления той недели
    for code in items:
        for w in tested:
            actual, o = cons(code, w + WEEK), stock(code, *rc_days(w)[:2])
            if actual is None or actual <= 0 or o is None:
                continue
            for key, order in zip(keys, auto_orders(hist(code, w), o, cw, k) + [fact(code, w)]):
                if order is not None:
                    covers[key].append((o + order) / actual)

    def summary(key, label):  # дефицит — покрытие меньше недели: к концу недели остаток в минусе
        c = covers[key]
        return {"key": key, "label": label, "n": len(c),
                "hit": sum(x >= cw for x in c) / len(c) if c else None,
                "short": sum(x < 1 for x in c) / len(c) if c else None,
                "cover": statistics.median(c) if c else None}

    warnings = []
    if not cons_sid:
        warnings.append("не выбран источник потребления — во вкладке РЦ")
    elif (n := sum(start - i * WEEK in full for i in (1, 2, 3))) < 3:
        warnings.append(f"расход целиком загружен только за {n} из 3 прошлых нед.")
    if not roles["stock"] or stock_day not in source_days[roles["stock"]]:
        warnings.append(f"нет остатка на {wd(stock_day)} {stock_day:%d.%m}")

    # Как считаем — над таблицей и в xlsx, с номерами недель выбранного периода.
    wk = [(start - (AUTO_WEEKS - i) * WEEK).isocalendar()[1] for i in range(AUTO_WEEKS)]
    a, b, c = (f"нед. {n}" for n in wk[-3:])
    sigma = "разброс σ" if k == 1 else f"{_num(k)} × разброс σ"
    rules = [f"расход = ({a} + {b} + {c}) / 3",
             f"расход = 0,2 × {a} + 0,3 × {b} + 0,5 × {c} — свежая неделя важнее",
             f"расход = самая большая из {a}, {b}, {c}",
             f"расход = среднее за {AUTO_WEEKS} нед. (нед. {wk[0]}–{wk[-1]}), к заказу ещё + {sigma} — "
             "насколько недели отличаются друг от друга, страховка от скачков"]
    span = f"{flow[0]:%d.%m}–{flow[-1]:%d.%m}"
    cons_name = by_id[cons_sid]["name"] if cons_sid else "не выбран"
    if date_to is None:
        period = {**week_info(start), "label": f"Ср {stock_day:%d.%m} → заказ на нед. {close.isocalendar()[1]} · "
                                               f"{close:%d.%m}–{close + 6 * DAY:%d.%m.%Y}"}
    else:
        info = range_info(date, date_to)
        period = {**info, "label": f"{info['label']} → заказ с {close:%d.%m.%Y}"}

    return {
        "mode": "week" if date_to is None else "range",
        "period": period,
        "roles": {role: {"id": sid, "name": by_id[sid]["name"]} if sid else None for role, sid in roles.items()},
        "warehouse": {"id": wh["id"], "name": wh["name"]} if wh else None,
        "params": params,
        "formula": f"Заказ = расход в неделю × {_num(cw)} − остаток на {wd(close)} {close:%d.%m}",
        "formula_note": (f"чтобы остаток + заказ = {_num(cw)} нед. расхода. Меньше нуля — 0; от 1 000 округляем "
                         "до сотен, от 100 — до десятков."),
        "basis": (f"Расход — «{cons_name}» за неделю пн–вс, неполные недели не считаем. Остаток на {wd(close)} — "
                  f"как во вкладке РЦ: остаток {wd(stock_day)} {stock_day:%d.%m} − заказы {span} + выпуск {span}."),
        "formulas": [{"key": key, "label": label, "rule": rule} for (key, label), rule in zip(AUTO_FORMULAS, rules)],
        "warnings": warnings,
        "rows": _sort(rows),
        "backtest": {
            "weeks": [week_info(w + WEEK) for w in tested],
            "rows": [summary(key, label) for key, label in AUTO_FORMULAS]
            + ([summary("fact", f"Как заказали: {wh['name']}")] if wh else []),
        },
    }
