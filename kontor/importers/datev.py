"""DATEV-Kontoumsatzexport aus Lexware Banking (Spec 18.7a).

- Datum ist die Valuta (Spalte 6): ab 14.11.2024 fehlt das Buchungsdatum.
- Ein zweites identisches Vorkommen (IBAN, Valuta, Betrag, Name, Zweck) ist eine Exportdublette.
- Eine eigene IBAN als Gegenkonto oder im Namensfeld (so zeigen es manche Banken) oder ein eigener Firmenname heißt: intern."""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal

IBAN = re.compile(r"^[A-Z]{2}\d{10,}")
DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


def _split(line: str) -> list[str]:
    out, cur, quoted = [], "", False
    for ch in line:
        if ch == '"':
            quoted = not quoted
            continue
        if ch == ";" and not quoted:
            out.append(cur)
            cur = ""
            continue
        cur += ch
    out.append(cur)
    return out


def _cents(text: str) -> int:
    return int((Decimal(text.replace(".", "").replace(",", ".")) * 100).to_integral_value())


def parse_datev(text: str, own_ibans: set[str], own_names: list[str]) -> list[dict]:
    rows, seen, skipped = [], {}, 0
    for line in text.splitlines():
        if not line.strip():
            continue
        c = _split(line) + [""] * 20
        iban = c[1].replace(" ", "")
        day = c[5] if DATE.match(c[5]) else c[4]
        if not iban or not DATE.match(day or "") or not re.match(r"^-?[\d.]+,\d{2}$", c[6].strip()):
            skipped += 1
            continue
        d, m, y = day.split(".")
        amount = _cents(c[6].strip())
        name = c[7].strip()
        purpose = (c[11] + c[12]).strip()
        key = f"{iban}|{y}-{m}-{d}|{amount}|{name}|{purpose}"
        n = seen.get(key, 0)
        seen[key] = n + 1
        cp_iban = next((x.replace(" ", "") for x in (c[8], c[9], c[10], c[13], c[14])
                        if IBAN.match(x.replace(" ", ""))), "")
        name_iban = name.replace(" ", "")
        internal = (cp_iban in own_ibans) or (name_iban in own_ibans and name_iban != iban) \
            or any(re.search(p, name, re.I) for p in own_names)
        rows.append({"iban": iban, "date": f"{y}-{m}-{d}", "amount": amount, "counterparty": name,
                     "purpose": purpose, "counterparty_iban": cp_iban, "internal": internal, "duplicate": n > 0,
                     "external_id": "datev:" + hashlib.sha1(f"{key}|{n}".encode()).hexdigest(),
                     "raw": c[:30]})
    return rows
