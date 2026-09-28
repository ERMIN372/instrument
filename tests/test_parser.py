import datetime as dt
import io
import json

from openpyxl import Workbook

from app.parser import ROW_COUNT, parse_xlsx, source_from_filename
from app.service import delta, period_value

D = dt.date


def _xlsx(fill) -> bytes:
    wb = Workbook()
    fill(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_long_layout_with_dimension_and_total_row():
    def fill(wb):
        ws = wb.active
        ws.append(["Отчёт по продажам"])
        ws.append([])
        ws.append(["Дата", "Магазин", "Выручка", "Средний чек"])
        ws.append([dt.datetime(2026, 9, 21), "Центр", 1000, 500])
        ws.append([dt.datetime(2026, 9, 21), "Север", 300, 300])
        ws.append([dt.datetime(2026, 9, 22), "Центр", "1 200,50", 400])
        ws.append(["Итого", None, 2500.5, None])

    p = parse_xlsx(_xlsx(fill))
    assert p.metrics == {"Выручка": "sum", "Средний чек": "avg"}
    center = json.dumps({"Магазин": "Центр"}, ensure_ascii=False)
    assert p.facts[("Выручка", D(2026, 9, 21), center)] == [1000.0, 1]
    assert p.facts[("Выручка", D(2026, 9, 22), center)] == [1200.5, 1]
    assert p.days == [D(2026, 9, 21), D(2026, 9, 22)]  # «Итого» не попало
    assert ROW_COUNT not in p.metrics  # 1.5 строки на день — не транзакции


def test_transactional_file_gets_row_count():
    def fill(wb):
        ws = wb.active
        ws.append(["Дата заказа", "Номер заказа", "Сумма"])
        for i in range(6):
            ws.append([dt.datetime(2026, 9, 21 + i // 3), 1000 + i, 100])

    p = parse_xlsx(_xlsx(fill))
    assert "Номер заказа" not in p.metrics
    assert p.facts[(ROW_COUNT, D(2026, 9, 21), "{}")] == [3.0, 3]
    assert p.facts[("Сумма", D(2026, 9, 22), "{}")] == [300.0, 3]


def test_wide_layout_with_group_carry_and_percent_format():
    def fill(wb):
        ws = wb.active
        ws.append(["Группа", "Показатель"] + [dt.datetime(2026, 9, 21 + i) for i in range(7)] + ["Итого"])
        ws.append(["Магазин 1", "Выручка"] + [10] * 7 + [70])
        ws.append([None, "Конверсия"] + [0.125] * 7 + [None])
        ws.append(["Остаток на складе", None] + [50 - i for i in range(7)] + [None])
        for c in range(3, 10):
            ws.cell(row=3, column=c).number_format = "0.0%"

    p = parse_xlsx(_xlsx(fill))
    assert p.metrics == {
        "Магазин 1 / Выручка": "sum",
        "Магазин 1 / Конверсия": "avg",
        "Остаток на складе": "last",
    }
    assert p.facts[("Магазин 1 / Конверсия", D(2026, 9, 21), "{}")] == [12.5, 1]
    assert len(p.days) == 7


def test_multiple_sheets_prefix_metrics():
    def fill(wb):
        a = wb.active
        a.title = "Москва"
        a.append(["Дата", "Выручка"])
        a.append([dt.datetime(2026, 9, 21), 1])
        b = wb.create_sheet("СПб")
        b.append(["Дата", "Выручка"])
        b.append([dt.datetime(2026, 9, 21), 2])
        wb.create_sheet("Пусто")

    p = parse_xlsx(_xlsx(fill))
    assert set(p.metrics) == {"Москва: Выручка", "СПб: Выручка"}
    assert p.sheets == ["Москва", "СПб"]
    assert any("Пусто" in w for w in p.warnings)


def test_source_from_filename():
    assert source_from_filename("Продажи_сентябрь_2026.xlsx") == "Продажи"
    assert source_from_filename("Звонки 21.09-27.09.xlsx") == "Звонки"
    assert source_from_filename("2026.xlsx") == "2026"


def test_period_aggregations():
    cells = {D(2026, 9, 21): (10.0, 2), D(2026, 9, 23): (30.0, 1)}
    days = [D(2026, 9, 21) + dt.timedelta(days=i) for i in range(7)]
    assert period_value("sum", cells, days) == 40.0
    assert period_value("avg", cells, days) == 40.0 / 3
    assert period_value("last", cells, days) == 30.0
    assert period_value("sum", {}, days) is None
    assert delta(110, 100) == 0.1
    assert delta(5, 0) is None
