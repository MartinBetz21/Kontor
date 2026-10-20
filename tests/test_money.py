from decimal import Decimal

import pytest

from kontor.core.errors import KontorError
from kontor.core.money import euro_floor, fmt_eur, fmt_qty, parse_eur, parse_qty, percent_of, round_cents


def test_round_cents_half_up():
    assert round_cents(Decimal("4773.94")) == 4774
    assert round_cents(Decimal("0.5")) == 1
    assert round_cents(Decimal("-0.5")) == -1


def test_percent_of():
    assert percent_of(25126, "19") == 4774
    assert percent_of(750000, "19") == 142500
    assert percent_of(100, "7") == 7


def test_euro_floor():
    assert euro_floor(925099) == 9250
    assert euro_floor(-925099) == -9250


def test_fmt_eur():
    assert fmt_eur(119000) == "1.190,00 €"
    assert fmt_eur(-100000) == "-1.000,00 €"
    assert fmt_eur(5) == "0,05 €"


@pytest.mark.parametrize("text,cents", [
    ("1.234,50", 123450), ("750", 75000), ("0,5", 50), ("1234.5", 123450),
    ("1.000", 100000), ("-1.000,00 €", -100000), (" 299,00 € ", 29900),
])
def test_parse_eur(text, cents):
    assert parse_eur(text) == cents


@pytest.mark.parametrize("text", ["", "abc", "1,2,3", "1,234"])
def test_parse_eur_rejects(text):
    with pytest.raises(KontorError):
        parse_eur(text)


def test_qty():
    assert parse_qty("3") == Decimal("3")
    assert parse_qty("1,5") == Decimal("1.5")
    assert fmt_qty("3") == "3"
    assert fmt_qty(Decimal("1.500")) == "1,5"
    with pytest.raises(KontorError):
        parse_qty("1,2345")
