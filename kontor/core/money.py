"""Beträge sind ganze Cent. Eingaben kommen deutsch („1.234,50“) oder mit Punkt als Dezimaltrenner („1234.5“)."""
from __future__ import annotations

import re
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

from .errors import KontorError

_ONE = Decimal("1")


def round_cents(value: Decimal) -> int:
    """Decimal-Betrag in Cent → ganze Cent, kaufmännisch gerundet."""
    return int(value.quantize(_ONE, rounding=ROUND_HALF_UP))


def percent_of(cents: int, rate: str | Decimal) -> int:
    return round_cents(Decimal(cents) * Decimal(rate) / 100)


def euro_floor(cents: int) -> int:
    """Volle Euro, Richtung null abgerundet (ELSTER-Bemessungsgrundlagen)."""
    return int((Decimal(cents) / 100).to_integral_value(rounding=ROUND_DOWN))


def fmt_eur(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    euros, rest = divmod(abs(cents), 100)
    return f"{sign}{euros:,}".replace(",", ".") + f",{rest:02d} €"


def _normalize(text: str) -> str:
    raw = text.replace("€", "").replace(" ", "").replace(" ", "").strip()
    if not raw:
        raise KontorError("Leerer Betrag")
    if "," in raw:
        if raw.count(",") > 1:
            raise KontorError(f"Kein Betrag: {text!r}")
        whole, frac = raw.split(",")
        if len(frac) > 2:
            raise KontorError(f"Mehr als zwei Nachkommastellen: {text!r}")
        return whole.replace(".", "") + "." + frac
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", raw):   # 1.000 / 1.234.567 → Tausenderpunkte
        return raw.replace(".", "")
    return raw


def parse_eur(text: str) -> int:
    raw = _normalize(text)
    if not re.fullmatch(r"-?\d+(\.\d{1,2})?", raw):
        raise KontorError(f"Kein Betrag: {text!r}")
    try:
        return round_cents(Decimal(raw) * 100)
    except InvalidOperation as exc:
        raise KontorError(f"Kein Betrag: {text!r}") from exc


def parse_qty(text: str) -> Decimal:
    raw = str(text).strip().replace(",", ".")
    if not re.fullmatch(r"-?\d+(\.\d{1,3})?", raw):
        raise KontorError(f"Keine Menge (max. 3 Nachkommastellen): {text!r}")
    return Decimal(raw)


def fmt_qty(q: str | Decimal) -> str:
    d = Decimal(str(q)).normalize()
    return format(d, "f").replace(".", ",")
