"""Формулы автозаказа (демо) — без БД."""
import pytest

from app.service import auto_orders, round_order

# Пример, который согласовывали с заказчиком: потребление за 8 недель, последние три — 900/1000/1200.
HIST = [950, 1050, 880, 1100, 1000, 900, 1000, 1200]


def test_round_order():
    assert round_order(21236) == 21200  # пример заказчика
    assert round_order(21250) == 21300
    assert round_order(456) == 460
    assert round_order(37.4) == 37  # мелкие позиции не уходят в ноль
    assert round_order(0) == 0


def test_formulas_stock_two_weeks():
    # Запас 2 нед.: заказ = 3 × потребление − остаток пн (300).
    a, b, c, d = auto_orders(HIST, 300, 2, 1)
    assert a == 2800  # 3 × 1033,3 − 300 = 2800
    assert b == 2900  # 3 × (0,2·900 + 0,3·1000 + 0,5·1200) − 300 = 2940
    assert c == 3300  # 3 × 1200 − 300
    assert d == 2800  # 3 × 1010 + 1 × 106,0 − 300 = 2836


def test_formulas_edge_cases():
    assert auto_orders(HIST, None, 2, 1) == [None] * 4  # нет остатка на среду — не считаем
    assert auto_orders([None] * 8, 100, 2, 1) == [None] * 4  # нет истории
    assert auto_orders(HIST, 10_000, 2, 1) == [0] * 4  # склад и так полон — не меньше нуля
    # Неполная неделя (None) не в счёт: веса Б делятся на оставшиеся (0,3 и 0,5).
    hist = HIST[:-3] + [None, 1000, 1200]
    a, b, c, d = auto_orders(hist, 0, 0, 0)
    assert a == 1100 and c == 1200
    assert b == round_order((0.3 * 1000 + 0.5 * 1200) / 0.8)
    assert d == pytest.approx(round_order(sum(hist[:-3] + [1000, 1200]) / 7))
