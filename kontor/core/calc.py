"""Summen nach EN 16931: Zeilensumme gerundet, Steuer je Satz einmal auf die Satz-Summe gerundet."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from .money import parse_qty, percent_of, round_cents


@dataclass(frozen=True)
class Line:
    quantity: str
    unit_price: int
    tax_rate: str


@dataclass(frozen=True)
class Allowance:
    amount: int        # negativ = Abzug, positiv = Zuschlag
    tax_rate: str


@dataclass(frozen=True)
class TaxGroup:
    rate: str
    basis: int
    tax: int


@dataclass(frozen=True)
class Totals:
    lines_net: int
    allowances: int    # Summe der Abzüge, positiv
    charges: int       # Summe der Zuschläge, positiv
    net: int
    tax: int
    gross: int
    groups: tuple[TaxGroup, ...]

    def as_dict(self) -> dict:
        d = asdict(self)
        d["groups"] = [asdict(g) for g in self.groups]
        return d


def line_total(quantity: str, unit_price: int) -> int:
    return round_cents(parse_qty(quantity) * unit_price)


def compute(lines: list[Line], allowances: list[Allowance]) -> Totals:
    basis: dict[str, int] = {}
    lines_net = 0
    for line in lines:
        total = line_total(line.quantity, line.unit_price)
        lines_net += total
        basis[line.tax_rate] = basis.get(line.tax_rate, 0) + total
    allow = charge = 0
    for a in allowances:
        basis[a.tax_rate] = basis.get(a.tax_rate, 0) + a.amount
        if a.amount < 0:
            allow += -a.amount
        else:
            charge += a.amount
    groups = tuple(
        TaxGroup(rate, b, percent_of(b, rate))
        for rate, b in sorted(basis.items(), key=lambda kv: Decimal(kv[0]), reverse=True)
    )
    net = sum(g.basis for g in groups)
    tax = sum(g.tax for g in groups)
    return Totals(lines_net, allow, charge, net, tax, net + tax, groups)
