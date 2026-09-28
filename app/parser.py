"""Разбор xlsx в плоские факты: (метрика, день, разрезы) -> (сумма, кол-во).

Поддерживаются две раскладки листа:
  * «длинная» — одна колонка с датой, остальные колонки: числа (метрики)
    и текст (разрезы, например магазин или менеджер);
  * «широкая» — строка-заголовок с датами по колонкам, слева названия метрик.

Строки «Итого/Всего» пропускаются, чтобы не задваивать суммы.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePath

from openpyxl import load_workbook

MAX_SCAN_ROWS = 30       # в скольких первых строках искать заголовок
MAX_DIM_VALUES = 50      # текстовая колонка с бОльшим числом значений — не разрез
MIN_WIDE_DATES = 3       # сколько дат в строке делают лист «широким»
ROW_COUNT = "Количество записей"

TOTAL_RE = re.compile(r"^\s*(итог|всего|total)", re.I)
AVG_RE = re.compile(
    r"%|средн|\bср\.|конверс|доля|процент|рейтинг|\bavg\b|average|\brate\b|\bctr\b|\bcr\b", re.I
)
LAST_RE = re.compile(r"остат|баланс|сальдо|на конец", re.I)
SKIP_COL_RE = re.compile(
    r"^\s*(№|n\b|#)|номер|\bкод\b|\bid\b|артикул|\bинн\b|телефон|штрих|^\s*(год|месяц|неделя|день|week|year|month)\s*$",
    re.I,
)
DATE_STR_RE = re.compile(r"^\d{1,4}[./-]\d{1,2}[./-]\d{2,4}")
DATE_FORMATS = ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y")
NUM_JUNK_RE = re.compile(r"[\s  ]|руб\.?|₽|\$|€", re.I)
NUM_RE = re.compile(r"-?\d+(\.\d+)?")
MONTH_RE = re.compile(
    r"\b(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*", re.I
)


@dataclass
class ParsedFile:
    # (метрика, день, разрезы-json) -> [сумма, кол-во исходных значений]
    facts: dict[tuple[str, dt.date, str], list] = field(default_factory=dict)
    # метрика -> агрегация по умолчанию ('sum' | 'avg' | 'last'), в порядке появления
    metrics: dict[str, str] = field(default_factory=dict)
    sheets: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def days(self) -> list[dt.date]:
        return sorted({k[1] for k in self.facts})


def to_date(v) -> dt.date | None:
    if isinstance(v, dt.datetime):
        d = v.date()
    elif isinstance(v, dt.date):
        d = v
    elif isinstance(v, str) and DATE_STR_RE.match(v.strip()):
        s = v.strip().split()[0]
        d = None
        for fmt in DATE_FORMATS:
            try:
                d = dt.datetime.strptime(s, fmt).date()
                break
            except ValueError:
                continue
    else:
        return None
    if d is None or not 2000 <= d.year <= 2100:
        return None
    return d


def to_number(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        s = NUM_JUNK_RE.sub("", v).rstrip("%").replace(",", ".")
        if NUM_RE.fullmatch(s):
            return float(s)
    return None


def text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return re.sub(r"\s+", " ", str(v)).strip()


def guess_agg(name: str, percent: bool = False) -> str:
    if LAST_RE.search(name):
        return "last"
    if percent or AVG_RE.search(name):
        return "avg"
    return "sum"


def source_from_filename(filename: str) -> str:
    """«Продажи_сентябрь_2026.xlsx» -> «Продажи»: без дат, месяцев и номеров,
    чтобы файлы за разные периоды попадали в один источник."""
    stem = PurePath(filename).stem
    s = re.sub(r"[_\-.()\[\]]+", " ", stem)  # «_» — словесный символ, мешает \b ниже
    s = MONTH_RE.sub(" ", s)
    s = re.sub(r"\d+", " ", s)
    s = re.sub(r"\b(неделя|нед|week|w)\b", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s or stem


class _Sheet:
    """Лист как сетка значений + отметки, в каких ячейках процентный формат."""

    def __init__(self, ws):
        self.name = ws.title
        self.grid: list[list] = []
        self.pct: set[tuple[int, int]] = set()
        if hasattr(ws, "reset_dimensions"):
            ws.reset_dimensions()  # иначе read-only режим верит битому <dimension> в файле
        for r, row in enumerate(ws.iter_rows()):
            vals = []
            for c, cell in enumerate(row):
                vals.append(cell.value)
                fmt = getattr(cell, "number_format", None) or ""
                if "%" in fmt and isinstance(cell.value, (int, float)):
                    self.pct.add((r, c))
            while vals and vals[-1] in (None, ""):
                vals.pop()
            self.grid.append(vals)

    def cell(self, r: int, c: int):
        row = self.grid[r]
        return row[c] if c < len(row) else None


class _Collector:
    def __init__(self):
        self.facts: dict[tuple[str, dt.date, str], list] = defaultdict(lambda: [0.0, 0])
        self.metrics: dict[str, str] = {}

    def metric(self, name: str, agg: str):
        self.metrics.setdefault(name, agg)

    def add(self, metric: str, day: dt.date, dims: dict, value: float, pct: bool = False):
        if pct:
            value *= 100  # 0.125 в ячейке с форматом % -> 12.5
        key = (metric, day, json.dumps(dims, ensure_ascii=False, sort_keys=True))
        acc = self.facts[key]
        acc[0] += value
        acc[1] += 1


def _is_total_row(row: list) -> bool:
    return any(isinstance(v, str) and TOTAL_RE.match(v) for v in row)


def _parse_wide(sh: _Sheet, out: _Collector) -> bool:
    for hr in range(min(MAX_SCAN_ROWS, len(sh.grid))):
        dates = {c: d for c, v in enumerate(sh.grid[hr]) if (d := to_date(v))}
        if len(dates) < MIN_WIDE_DATES or len(set(dates.values())) != len(dates):
            continue
        # Под датами должны стоять числа, иначе это строка «длинной» таблицы
        # с несколькими колонками дат.
        below = [sh.cell(r, c) for r in range(hr + 1, min(hr + 21, len(sh.grid))) for c in dates]
        below = [v for v in below if v not in (None, "")]
        if not below or sum(to_number(v) is not None for v in below) / len(below) < 0.5:
            continue
        break
    else:
        return False

    first_date_col = min(dates)
    carry: list[str] = [""] * first_date_col
    for r in range(hr + 1, len(sh.grid)):
        row = sh.grid[r]
        labels = [text(v) if isinstance(v, str) else "" for v in row[:first_date_col]]
        labels += [""] * (first_date_col - len(labels))
        if not any(labels):
            continue
        # Объединённые ячейки групп: пустые левые подписи берём из строк выше.
        first_filled = next(i for i, lbl in enumerate(labels) if lbl)
        labels[:first_filled] = carry[:first_filled]
        carry = labels
        if _is_total_row(row):
            continue
        name = " / ".join(lbl for lbl in labels if lbl)
        for c, day in dates.items():
            x = to_number(sh.cell(r, c))
            if x is None:
                continue
            pct = (r, c) in sh.pct
            out.metric(name, guess_agg(name, pct))
            out.add(name, day, {}, x, pct)
    return True


def _parse_long(sh: _Sheet, out: _Collector) -> bool:
    counts: dict[int, int] = defaultdict(int)
    for row in sh.grid:
        for c, v in enumerate(row):
            if to_date(v):
                counts[c] += 1
    if not counts:
        return False
    date_col = max(counts, key=lambda c: (counts[c], -c))
    data_idx = [r for r, row in enumerate(sh.grid) if to_date(sh.cell(r, date_col))]
    first = data_idx[0]

    header: list = []
    for r in range(first - 1, max(-1, first - 6), -1):
        if sum(v not in (None, "") for v in sh.grid[r]) >= 2:
            header = sh.grid[r]
            break

    def col_name(c: int) -> str:
        return text(header[c]) if c < len(header) and text(header[c]) else f"Колонка {c + 1}"

    width = max(len(sh.grid[r]) for r in data_idx)
    metric_cols: dict[int, tuple[str, bool]] = {}
    dim_cols: dict[int, str] = {}
    for c in range(width):
        if c == date_col:
            continue
        name = col_name(c)
        if SKIP_COL_RE.search(name):
            continue
        vals = [(r, sh.cell(r, c)) for r in data_idx if sh.cell(r, c) not in (None, "")]
        if not vals:
            continue
        nums = sum(to_number(v) is not None for _, v in vals)
        if nums / len(vals) >= 0.8:
            pct = any((r, c) in sh.pct for r, _ in vals) or any(
                isinstance(v, str) and v.strip().endswith("%") for _, v in vals
            )
            metric_cols[c] = (name, pct)
        elif sum(to_date(v) is not None for _, v in vals) / len(vals) < 0.5:
            if len({text(v) for _, v in vals}) <= MAX_DIM_VALUES:
                dim_cols[c] = name

    for name, pct in metric_cols.values():
        out.metric(name, guess_agg(name, pct))

    kept: list[tuple[dt.date, dict]] = []
    for r in data_idx:
        row = sh.grid[r]
        if _is_total_row(row):
            continue
        day = to_date(row[date_col])
        dims = {dn: text(sh.cell(r, c)) or "—" for c, dn in dim_cols.items()}
        kept.append((day, dims))
        for c, (name, _) in metric_cols.items():
            x = to_number(sh.cell(r, c))
            if x is not None:
                out.add(name, day, dims, x, (r, c) in sh.pct)

    # Счётчик строк нужен только «транзакционным» выгрузкам, где на день
    # приходится много строк (заказы, звонки). В дневных сводках это шум.
    days = {day for day, _ in kept}
    if days and len(kept) / len(days) > 1.5:
        out.metric(ROW_COUNT, "sum")
        for day, dims in kept:
            out.add(ROW_COUNT, day, dims, 1.0)
    return bool(out.facts)


def parse_xlsx(data: bytes) -> ParsedFile:
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    per_sheet: list[tuple[str, _Collector]] = []
    result = ParsedFile()
    for ws in wb.worksheets:
        sh = _Sheet(ws)
        col = _Collector()
        if not (_parse_wide(sh, col) or _parse_long(sh, col)) or not col.facts:
            result.warnings.append(f"Лист «{sh.name}»: не нашёл дат и чисел, пропущен")
            continue
        per_sheet.append((sh.name, col))
    wb.close()

    # Несколько листов с данными — метрики префиксуем именем листа, чтобы не слиплись.
    multi = len(per_sheet) > 1
    for sheet_name, col in per_sheet:
        result.sheets.append(sheet_name)
        prefix = f"{sheet_name}: " if multi else ""
        for name, agg in col.metrics.items():
            result.metrics.setdefault(prefix + name, agg)
        for (name, day, dims), acc in col.facts.items():
            result.facts[(prefix + name, day, dims)] = acc
    return result
