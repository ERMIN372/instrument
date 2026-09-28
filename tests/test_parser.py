import datetime as dt
import io

import pytest
from openpyxl import Workbook

from app.parser import parse_xlsx, source_from_filename, to_date, to_number

D = dt.date
HEADER = ["Дата", "UID номенклатуры", "Код номенклатуры", "Наименование номенклатуры",
          "Единица измерения", "Количество", "Количество базовых"]


def _xlsx(rows, header=HEADER, preamble=()) -> bytes:
    wb = Workbook()
    ws = wb.active
    for line in preamble:
        ws.append(line)
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_1c_export_packs_kg_duplicates_and_totals():
    data = _xlsx([
        ["01.09.2026", "u1", "00000168402", "Булка для гамбургера замороженная 80 г, упак 24 шт", "упак, 24 шт", 10, 240],
        ["01.09.2026", "u1", "00000168402", "Булка для гамбургера замороженная 80 г, упак 24 шт", "упак, 24 шт", 1, 24],
        [dt.datetime(2026, 9, 2), "u2", "00000210097", "Хлеб для мясного производства, вес", "кг", 286.5, 286.5],
        ["02.09.2026", "u3", "00000099305", "Ватрушка венгерская 110 г, упак 20 шт", "шт", -20, -20],
        ["Итого", None, None, None, None, 277.5, 530.5],
    ], preamble=[["Выгрузка из 1С"], []])

    p = parse_xlsx(data)
    assert p.movements[(D(2026, 9, 1), "00000168402")] == [11, 264]  # повтор сложен, в штуках
    assert p.movements[(D(2026, 9, 2), "00000210097")] == [286.5, 286.5]
    assert p.days == [D(2026, 9, 1), D(2026, 9, 2)]
    bun = p.items["00000168402"]
    assert (bun.base_unit, bun.pack_size, bun.category) == ("шт", 24, "Булка")
    assert p.items["00000210097"].base_unit == "кг"
    assert p.items["00000099305"].pack_size == 20  # из названия, когда единица «шт»
    assert (p.rows, p.merged, p.negative, p.skipped) == (4, 1, 1, 1)
    assert len(p.warnings) == 3


def test_minimal_columns_any_order():
    data = _xlsx([[5, "01.09.2026", "Слойка с малиной"]], header=["Количество", "Дата", "Номенклатура"])
    p = parse_xlsx(data)
    assert p.movements == {(D(2026, 9, 1), "Слойка с малиной"): [5, 5]}  # без кода ключ — название


def test_missing_columns_is_a_clear_error():
    with pytest.raises(ValueError, match="заголовка"):
        parse_xlsx(_xlsx([["a", 1]], header=["Что-то", "Сколько"]))


def test_header_without_rows_is_an_error():
    with pytest.raises(ValueError, match="нет ни одной строки"):
        parse_xlsx(_xlsx([]))


def test_helpers():
    assert to_date("27.09.2026") == D(2026, 9, 27)
    assert to_date("2026-09-27 00:00:00") == D(2026, 9, 27)
    assert to_date("Итого") is None
    assert to_number("1 200,5") == 1200.5
    assert to_number("12 шт") is None
    assert to_number(True) is None


@pytest.mark.parametrize("name, expected", [
    ("Выпуск_сентябрь_2026.xlsx", "Выпуск"),
    ("Отгрузки с 01.09 по 30.09.xlsx", "Отгрузки"),
    ("Остатки W39.xlsx", "Остатки"),
    ("2026.xlsx", "2026"),
])
def test_source_from_filename(name, expected):
    assert source_from_filename(name) == expected
