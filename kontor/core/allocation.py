"""Istversteuerung: jede Zahlung wird anteilig auf die Steuersätze und in Netto/Steuer zerlegt.
Die Zahlung, die den Rest begleicht, nimmt genau den Rest, damit die Summen auf den Cent stimmen."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .errors import KontorError
from .money import round_cents


@dataclass(frozen=True)
class Part:
    rate: str
    net: int
    tax: int


def remaining_by_rate(groups: list[dict], prepaid: list[dict], paid_parts: list[dict]) -> list[tuple[str, int, int]]:
    due = {g["rate"]: [g["basis"] + g["tax"], g["tax"]] for g in groups}
    for p in prepaid:
        due[p["rate"]][0] -= p["gross"]
        due[p["rate"]][1] -= p["tax"]
    for pp in paid_parts:
        due[pp["tax_rate"]][0] -= pp["net"] + pp["tax"]
        due[pp["tax_rate"]][1] -= pp["tax"]
    return [(rate, g, t) for rate, (g, t) in due.items()]


def allocate(payment: int, remaining: list[tuple[str, int, int]]) -> list[Part]:
    total = sum(g for _, g, _ in remaining)
    if total == 0:
        raise KontorError("Auf diesem Dokument ist nichts mehr offen.")
    if payment == total:
        return [Part(rate, g - t, t) for rate, g, t in remaining if g or t]
    open_groups = [r for r in remaining if r[1]]            # nie auf einen schon ausgeglichenen Satz buchen
    parts, left = [], payment
    for i, (rate, g, t) in enumerate(open_groups):
        share = left if i == len(open_groups) - 1 else round_cents(Decimal(payment) * g / total)
        left -= share
        tax = round_cents(Decimal(share) * t / g) if g else 0
        parts.append(Part(rate, share - tax, tax))
    return parts
