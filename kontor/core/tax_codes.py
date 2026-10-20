"""Steuerarten der Belege, ELSTER-Kennzahlen, Voranmeldungszeiträume und Fristen.
Vor der ersten Abgabe gegen das aktuelle ELSTER-Formular prüfen (Spec 7)."""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from .errors import KontorError
from .money import percent_of, round_cents

TAX_KINDS = {
    "inland_19": "Inland 19 %",
    "inland_7": "Inland 7 %",
    "ohne_vorsteuer": "ohne Vorsteuer (Kleinunternehmer, privat, steuerfrei)",
    "rc_eu": "Reverse Charge EU (§ 13b Abs. 1)",
    "rc_drittland": "Reverse Charge Drittland (§ 13b Abs. 2)",
}
INLAND_RATES = {"inland_19": "19", "inland_7": "7"}
RC_RATE = "19"

KZ_LABELS = {
    "81": "Umsätze 19 % (Bemessungsgrundlage, volle Euro)",
    "86": "Umsätze 7 % (Bemessungsgrundlage, volle Euro)",
    "46": "§ 13b Abs. 1, Leistender im EU-Ausland: Bemessungsgrundlage",
    "47": "§ 13b Abs. 1: Steuer",
    "84": "§ 13b, übrige (z. B. Drittland): Bemessungsgrundlage",
    "85": "§ 13b, übrige: Steuer",
    "66": "Vorsteuer aus Rechnungen",
    "67": "Vorsteuer aus § 13b-Leistungen",
    "83": "Verbleibende Vorauszahlung (negativ: Überschuss)",
    "45": "Übrige nicht steuerbare Umsätze, Leistungsort nicht im Inland (Drittland B2B)",
    "21": "Nicht steuerbare sonstige Leistungen nach § 18b Satz 1 Nr. 2 (EU-Unternehmer, Reverse Charge)",
}

# Steuerart im Ausgang (Spec 18.4). Rechnungstexte stehen hier an einer Stelle.
TAX_TREATMENTS = {
    "inland": "Inland",
    "drittland_b2b": "Drittland, Unternehmer: nicht steuerbar (§ 3a Abs. 2 UStG)",
    "eu_b2b_rc": "EU, Unternehmer: Reverse Charge",
}
TREATMENT_NOTES = {
    "drittland_b2b": "Nicht im Inland steuerbare Leistung (§ 3a Abs. 2 UStG). Die Steuer schuldet der Leistungsempfänger.",
    "eu_b2b_rc": "Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge, § 13b UStG). "
                 "USt-IdNr. Leistender: {seller_vat}, USt-IdNr. Leistungsempfänger: {buyer_vat}.",
}
EU_COUNTRIES = {"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT", "LT",
                "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"}


def complete_amounts(tax_kind: str, net: int | None, tax: int | None, gross: int | None) -> tuple[int, int, int]:
    if tax_kind not in TAX_KINDS:
        raise KontorError(f"Steuerart {tax_kind!r} gibt es nicht.")
    if tax_kind in INLAND_RATES:
        rate = Decimal(INLAND_RATES[tax_kind])
        if net is not None and gross is not None and tax is None:
            tax = gross - net
            if abs(tax - percent_of(net, rate)) > 1:
                raise KontorError(f"Netto {net / 100:.2f} und Brutto {gross / 100:.2f} passen nicht zu {rate} %.")
        if net is not None and tax is not None:
            if gross is not None and net + tax != gross:
                raise KontorError("Netto + Steuer ergibt nicht den Bruttobetrag.")
            return net, tax, net + tax
        if gross is not None:
            n = round_cents(Decimal(gross) * 100 / (100 + rate))
            return n, gross - n, gross
        if net is not None:
            t = percent_of(net, rate)
            return net, t, net + t
        raise KontorError("Betrag fehlt.")
    base = net if net is not None else gross
    if base is None:
        raise KontorError("Betrag fehlt.")
    if tax_kind == "ohne_vorsteuer":
        return base, 0, base
    return base, percent_of(base, RC_RATE), base        # Reverse Charge: gezahlt wird netto


def period_bounds(spec: str) -> tuple[date, date]:
    if re.fullmatch(r"\d{4}", spec):                  # ganzes Jahr (Jahreszahler, Spec 18.3)
        return date(int(spec), 1, 1), date(int(spec), 12, 31)
    m = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", spec)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        start = date(y, mo, 1)
    else:
        q = re.fullmatch(r"(\d{4})-Q([1-4])", spec)
        if not q:
            raise KontorError(f"Zeitraum {spec!r}: JJJJ-MM, JJJJ-Qn oder JJJJ.")
        y, mo = int(q.group(1)), (int(q.group(2)) - 1) * 3 + 1
        start = date(y, mo, 1)
    months = 1 if m else 3
    end_month = start.month + months
    end = date(start.year + (end_month - 1) // 12, (end_month - 1) % 12 + 1, 1) - timedelta(days=1)
    return start, end


def period_of(day: date, mode: str) -> str:
    if mode == "jahr":
        return str(day.year)
    return f"{day.year}-{day.month:02d}" if mode == "monat" else f"{day.year}-Q{(day.month - 1) // 3 + 1}"


def deadline(spec: str) -> date:
    """10. des Folgemonats, Wochenende → Montag. Feiertage und Dauerfristverlängerung nicht berücksichtigt.
    Jahreszahler: Umsatzsteuererklärung bis 31.07. des Folgejahres (ohne Berater)."""
    if re.fullmatch(r"\d{4}", spec):
        return date(int(spec) + 1, 7, 31)
    _, end = period_bounds(spec)
    d = end + timedelta(days=10)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d
