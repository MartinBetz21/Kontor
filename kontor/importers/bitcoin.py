"""Bitcoin-Exporte und Tageskurse lesen (Spec 18.14). Reine Funktionen, ohne Datenbank und ohne Netz.

Jeder Parser liefert nur die Felder, die Kontor speichert (Zeitpunkt, Art, sats, Gebühr, EUR, Notiz). Adressen,
Transaktions-IDs und Lightning-Rechnungen werden nie übernommen: Die Zeilen werden Feld für Feld neu aufgebaut,
die Rohzeile wird verworfen.

- `kontor`: eigene CSV (Datum;Wallet;Art;sats;Gebühr sats;EUR;Vertragsdatum;Notiz, optional Gebühr EUR, Ziel-Wallet)
- `bitbox`: Export der BitBoxApp je Konto (Time, Type, Amount, Unit, Fee, Fee Unit, Address, Transaction ID, Note)
- `coinfinity`: Orderexport (Kauf und Verkauf mit EUR, Service- und Mining-Fee)
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from ..core.errors import KontorError
from ..core.money import parse_eur, round_cents

SATS_JE_BTC = Decimal(100_000_000)
ARTEN = ("kauf", "verkauf", "kundenzahlung", "zahlung", "umbuchung", "eingang", "ausgang", "verlust")


def _rows(text: str, delimiter: str | None = None) -> list[list[str]]:
    text = text.lstrip("﻿")
    first = text.splitlines()[0] if text.strip() else ""
    delim = delimiter or (";" if first.count(";") > first.count(",") else ",")
    return [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any(c.strip() for c in r)]


def _dezimal(raw: str, feld: str) -> Decimal:
    """1234.56, 1.234,56 oder 1234,56."""
    s = (raw or "").strip().replace(" ", "")
    if not s:
        return Decimal(0)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise KontorError(f"{feld}: keine Zahl ({raw!r})") from exc


def _sats(amount: Decimal, unit: str, feld: str) -> int:
    unit = unit.strip().lower()
    value = amount * SATS_JE_BTC if unit == "btc" else amount if unit in ("sat", "sats", "satoshi", "") else None
    if value is None:
        raise KontorError(f"{feld}: Einheit {unit!r} unbekannt (satoshi oder BTC)")
    if value != value.to_integral_value():
        raise KontorError(f"{feld}: {amount} {unit} sind keine ganzen sats")
    return abs(int(value))


def _datum(raw: str) -> str:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    raise KontorError(f"Datum {raw!r}: JJJJ-MM-TT oder TT.MM.JJJJ")


def _zeitpunkt(raw: str) -> datetime:
    raw = raw.strip()
    try:
        return datetime.fromisoformat(raw.replace(" UTC", "Z"))
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    raise KontorError(f"Zeitpunkt {raw!r} nicht lesbar")


def _utc_tag(t: datetime) -> str:
    """Kurse gelten je UTC-Kalendertag; Zeitpunkte mit Zone werden dorthin umgerechnet, ohne Zone gelten sie als UTC."""
    return (t.astimezone(timezone.utc) if t.tzinfo else t).date().isoformat()


def _bewegung(zeit: str, datum: str, art: str, sats: int, *, gebuehr_sats: int = 0, eur: int | None = None,
              gebuehr_eur: int = 0, vertragsdatum: str | None = None, notiz: str = "", wallet: str = "",
              ziel: str = "") -> dict:
    return {"zeit": zeit, "datum": datum, "art": art, "sats": sats, "gebuehr_sats": gebuehr_sats, "eur": eur,
            "gebuehr_eur": gebuehr_eur, "vertragsdatum": vertragsdatum, "notiz": notiz, "wallet": wallet, "ziel": ziel}


def _spalten(header: list[str], pflicht: tuple[str, ...], quelle: str) -> dict[str, int]:
    idx = {h.strip(): i for i, h in enumerate(header) if h.strip()}
    fehlt = [p for p in pflicht if p not in idx]
    if fehlt:
        raise KontorError(f"{quelle}: Spalten fehlen ({', '.join(fehlt)})")
    return idx


def _get(row: list[str], idx: dict[str, int], name: str) -> str:
    i = idx.get(name)
    return row[i].strip() if i is not None and i < len(row) else ""


# -- eigene CSV -----------------------------------------------------------------------------------------------------
def parse_kontor(text: str) -> list[dict]:
    rows = _rows(text, ";")
    if not rows:
        return []
    idx = _spalten(rows[0], ("Datum", "Wallet", "Art", "sats"), "Kontor-CSV")
    out = []
    for n, r in enumerate(rows[1:], 2):
        art = _get(r, idx, "Art").lower()
        if art not in ARTEN:
            raise KontorError(f"Zeile {n}: Art {art!r} (erlaubt: {', '.join(ARTEN)})")
        datum = _datum(_get(r, idx, "Datum"))
        eur, geb_eur, vd = _get(r, idx, "EUR"), _get(r, idx, "Gebühr EUR"), _get(r, idx, "Vertragsdatum")
        out.append(_bewegung(datum, datum, art, int(_get(r, idx, "sats")),
                             gebuehr_sats=int(_get(r, idx, "Gebühr sats") or 0), eur=parse_eur(eur) if eur else None,
                             gebuehr_eur=parse_eur(geb_eur) if geb_eur else 0, vertragsdatum=_datum(vd) if vd else None,
                             notiz=_get(r, idx, "Notiz"), wallet=_get(r, idx, "Wallet"), ziel=_get(r, idx, "Ziel-Wallet")))
    return out


# -- BitBoxApp ------------------------------------------------------------------------------------------------------
BITBOX_ARTEN = {"received": "eingang", "sent": "ausgang", "sent_to_yourself": "umbuchung"}


def parse_bitbox(text: str) -> list[dict]:
    """`sent_to_yourself` ist eine Umbuchung innerhalb des Kontos: es geht nur die Netzwerkgebühr ab."""
    rows = _rows(text)
    if not rows:
        return []
    idx = _spalten(rows[0], ("Time", "Type", "Amount", "Unit"), "BitBox-Export")
    out = []
    for n, r in enumerate(rows[1:], 2):
        typ = _get(r, idx, "Type").lower()
        if typ not in BITBOX_ARTEN:
            raise KontorError(f"Zeile {n}: Type {typ!r} unbekannt")
        t = _zeitpunkt(_get(r, idx, "Time"))
        sats = _sats(_dezimal(_get(r, idx, "Amount"), "Amount"), _get(r, idx, "Unit"), f"Zeile {n} Amount")
        fee_raw = _get(r, idx, "Fee")
        fee = _sats(_dezimal(fee_raw, "Fee"), _get(r, idx, "Fee Unit") or _get(r, idx, "Unit"), f"Zeile {n} Fee") \
            if fee_raw else 0
        art = BITBOX_ARTEN[typ]
        out.append(_bewegung(t.isoformat(), _utc_tag(t), art, sats, gebuehr_sats=0 if art == "eingang" else fee,
                             notiz=_get(r, idx, "Note")))
    return out


# -- Orderexport einer Börse ----------------------------------------------------------------------------------------
def parse_coinfinity(text: str) -> list[dict]:
    """Kauf: Amount EUR ist der volle gezahlte Betrag (Gebühren sind Anschaffungsnebenkosten, Total Fee EUR steht nur
    zur Information dabei). Verkauf: Amount EUR ist die Auszahlung netto, der Erlös brutto = Amount Crypto × Rate,
    die Differenz ist die Gebühr. Die Mining Fee eines Verkaufs ist eine Netzwerkgebühr in sats."""
    rows = _rows(text, ";")
    if not rows:
        return []
    idx = _spalten(rows[0], ("Order ID", "Type", "Date", "Amount EUR", "Amount Crypto", "Crypto"), "Orderexport")
    out = []
    for n, r in enumerate(rows[1:], 2):
        if _get(r, idx, "Crypto").upper() not in ("BTC", "XBT"):
            continue
        typ = _get(r, idx, "Type").lower()
        t = _zeitpunkt(_get(r, idx, "Date"))
        tag = _utc_tag(t)
        crypto = _dezimal(_get(r, idx, "Amount Crypto"), "Amount Crypto")
        sats = _sats(crypto, "BTC", f"Zeile {n} Amount Crypto")
        netto = round_cents(_dezimal(_get(r, idx, "Amount EUR"), "Amount EUR") * 100)
        notiz = f"Order {_get(r, idx, 'Order ID')}".strip()
        if typ in ("buy", "kauf"):
            fee = round_cents(_dezimal(_get(r, idx, "Total Fee EUR"), "Total Fee EUR") * 100)
            out.append(_bewegung(t.isoformat(), tag, "kauf", sats, eur=netto, gebuehr_eur=fee, notiz=notiz))
        elif typ in ("sell", "verkauf"):
            brutto = round_cents(crypto * _dezimal(_get(r, idx, "Rate EUR"), "Rate EUR") * 100)
            mining = _get(r, idx, "Mining Fee Crypto")
            out.append(_bewegung(t.isoformat(), tag, "verkauf", sats, eur=brutto, gebuehr_eur=brutto - netto,
                                 gebuehr_sats=_sats(_dezimal(mining, "Mining Fee"), "BTC", f"Zeile {n} Mining Fee")
                                 if mining else 0, vertragsdatum=tag, notiz=notiz))
        else:
            raise KontorError(f"Zeile {n}: Type {typ!r} (buy oder sell)")
    return out


FORMATE = {"kontor": parse_kontor, "bitbox": parse_bitbox, "coinfinity": parse_coinfinity}


# -- Tageskurse -----------------------------------------------------------------------------------------------------
KRAKEN_URL = "https://api.kraken.com/0/public/OHLC?pair=XBTEUR&interval=1440&since="


def parse_kraken_ohlc(text: str, heute: date) -> list[tuple[str, Decimal]]:
    """Antwort der Kraken-OHLC-Abfrage (Tageskerzen 00:00–24:00 UTC): Schlusskurs als Kurs des Kalendertags.
    Die Kerze des laufenden Tages (`heute`, UTC) ist nicht geschlossen und fehlt."""
    data = json.loads(text)
    if data.get("error"):
        raise KontorError(f"Kraken meldet: {', '.join(data['error'])}")
    candles = next((v for k, v in data.get("result", {}).items() if k != "last"), [])
    out = []
    for c in candles:
        tag = datetime.fromtimestamp(int(c[0]), tz=timezone.utc).date()
        if tag < heute:
            out.append((tag.isoformat(), Decimal(str(c[4]))))
    return out


BITSTAMP_URL = "https://www.bitstamp.net/api/v2/ohlc/btceur/?step=86400&limit=1000&start="


def parse_bitstamp_ohlc(text: str, heute: date) -> list[tuple[str, Decimal]]:
    """Antwort der Bitstamp-OHLC-Abfrage (BTC/EUR, Tageskerzen 00:00–24:00 UTC), gleich gelesen wie Kraken."""
    data = json.loads(text)
    if "data" not in data:
        raise KontorError(f"Bitstamp meldet: {data.get('reason') or data.get('errors') or data}")
    out = []
    for c in data["data"].get("ohlc", []):
        tag = datetime.fromtimestamp(int(c["timestamp"]), tz=timezone.utc).date()
        if tag < heute:
            out.append((tag.isoformat(), Decimal(str(c["close"]))))
    return out


def kraken_since(tag: str) -> int:
    """Zeitstempel (UTC, Sekunden) eines Kalendertags, für den Parameter since."""
    return int(datetime.fromisoformat(tag).replace(tzinfo=timezone.utc).timestamp())
