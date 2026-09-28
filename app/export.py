"""Выгрузка недельной таблицы в xlsx."""
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

FONT = "Arial"
NUM_FMT = {"sum": "#,##0.##", "avg": "#,##0.00", "last": "#,##0.##"}
AGG_LABEL = {"sum": "сумма", "avg": "среднее", "last": "последнее"}


def week_xlsx(table: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = table["week"]["iso"]

    ws["A1"] = f"Единая таблица: {table['week']['label']}"
    ws["A1"].font = Font(name=FONT, bold=True, size=13)

    headers = ["Метрика", "Агрегация"] + [d["label"] for d in table["days"]] + ["Итого нед.", "Пред. нед.", "Δ к пред."]
    hdr_fill = PatternFill("solid", fgColor="E8EAF0")
    thin = Side(style="thin", color="C8CCD6")
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=3, column=c, value=h)
        cell.font = Font(name=FONT, bold=True)
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(bottom=thin)

    r = 4
    for group in table["groups"]:
        cell = ws.cell(row=r, column=1, value=group["source"])
        cell.font = Font(name=FONT, bold=True, color="3B4A6B")
        r += 1
        for row in group["rows"]:
            fmt = NUM_FMT[row["agg"]]
            values = [row["name"], AGG_LABEL[row["agg"]], *row["days"], row["total"], row["prev"]]
            for c, v in enumerate(values, 1):
                cell = ws.cell(row=r, column=c, value=v)
                cell.font = Font(name=FONT, bold=(c == len(values) - 1))
                if c > 2:
                    cell.number_format = fmt
            d = ws.cell(row=r, column=len(values) + 1, value=row["delta"])
            d.font = Font(name=FONT)
            d.number_format = "+0.0%;-0.0%;0.0%"
            r += 1

    r += 1
    note = ws.cell(
        row=r, column=1,
        value="Значения рассчитаны сервисом instrument на момент выгрузки. "
              "Итого: сумма — сумма дней; среднее — взвешенное по исходным строкам; "
              "последнее — значение последнего дня с данными.",
    )
    note.font = Font(name=FONT, italic=True, color="6B7280", size=9)

    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 12
    for c in range(3, len(headers) + 1):
        ws.column_dimensions[get_column_letter(c)].width = 13
    ws.freeze_panes = "C4"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
