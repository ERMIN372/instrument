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


def test_customer_example():
    # Ответ заказчика на «x2»: потребление 10 000, остаток пн 6 000 → заказ 14 000, а не 24 000.
    assert auto_orders([10_000] * 8, 6_000, 2, 1) == [14_000] * 4


def test_formulas_cover_two_weeks():
    # Покрытие 2 нед.: остаток пн + заказ = 2 × потребление, заказ = 2 × потребление − 300.
    a, b, c, d = auto_orders(HIST, 300, 2, 1)
    assert a == 1800  # 2 × 1033,3 − 300 = 1767
    assert b == 1900  # 2 × (0,2·900 + 0,3·1000 + 0,5·1200) − 300 = 1860
    assert c == 2100  # 2 × 1200 − 300
    assert d == 1800  # 2 × 1010 + 1 × 106,0 − 300 = 1826


def test_formulas_edge_cases():
    assert auto_orders(HIST, None, 2, 1) == [None] * 4  # нет остатка на среду — не считаем
    assert auto_orders([None] * 8, 100, 2, 1) == [None] * 4  # нет истории
    assert auto_orders(HIST, 10_000, 2, 1) == [0] * 4  # склад и так полон — не меньше нуля
    # Неполная неделя (None) не в счёт: веса Б делятся на оставшиеся (0,3 и 0,5).
    hist = HIST[:-3] + [None, 1000, 1200]
    a, b, c, d = auto_orders(hist, 0, 1, 0)
    assert a == 1100 and c == 1200
    assert b == round_order((0.3 * 1000 + 0.5 * 1200) / 0.8)
    assert d == pytest.approx(round_order(sum(hist[:-3] + [1000, 1200]) / 7))
