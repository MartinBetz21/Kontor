"""Lohn (Profilschalter `lohn`, SPEC.md „Payroll“): Mitarbeiter, Merkmale mit Gültigkeit, Bezüge, Monatsabrechnung,
Festschreiben und Storno, Jahreswerte, Buchungen, Lohnsteuer-Anmeldung und Lohnsteuerbescheinigung.

Ein Entwurf wird bei jeder Abfrage neu gerechnet: Lohnsteuer nach dem Programmablaufplan des BMF
(core/lohnsteuer.py), Sozialversicherung nach core/sozialversicherung.py. Festschreiben (Mensch oder Agent) speichert die
Abrechnung mit allem, was in die Rechnung einging, und legt die Entgeltbescheinigung schreibgeschützt ins Archiv.
Monate eines Mitarbeiters werden der Reihe nach festgeschrieben, storniert wird nur der jüngste: Jahreswerte
(Jahresarbeitslohn für sonstige Bezüge, anteilige Beitragsbemessungsgrenze) hängen an den Vormonaten."""
from __future__ import annotations

import calendar
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from ..context import Ctx, require_human
from ..core import lohnsteuer as lst
from ..core import sozialversicherung as sv
from ..core.errors import KontorError
from ..store import audit
from ..store.db import tx
from . import masterdata as md

LOHNARTEN = {"gehalt": "Gehalt", "einmalzahlung": "Einmalzahlung", "sachbezug": "Sachbezug",
             "steuerfrei": "Steuerfreier Bezug", "pauschal": "Pauschal versteuert", "abzug": "Abzug vom Netto"}
# Kennzeichen wie auf der Entgeltbescheinigung: L lohnsteuerpflichtig, S sozialversicherungspflichtig, E einmalig,
# P pauschal versteuert, F frei, N zählt nicht zum Gesamtbrutto
KENNZEICHEN = {"gehalt": "LS", "einmalzahlung": "LSE", "sachbezug": "LS", "steuerfrei": "F", "pauschal": "P",
               "abzug": "N"}
KONTEN = ("gehalt", "sv_ag", "pauschsteuer", "pauschal_lst", "sachbezug", "steuerfrei", "abzug", "verb_lohn",
          "verb_steuer", "verb_sv")
MITARBEITER_FELDER = ("personalnr", "vorname", "nachname", "geburtsdatum", "strasse", "plz", "ort", "land", "iban",
                      "steuer_id", "sv_nummer", "taetigkeit", "eintritt", "austritt", "notiz")
MERKMAL_VORGABEN = {"steuerklasse": 1, "faktor": "", "kinderfreibetraege": "0", "konfession": "",
                    "konfession_ehegatte": "", "freibetrag": 0, "hinzurechnung": 0, "pauschsteuer": 0,
                    "personengruppe": "101", "beitragsgruppe": "1111", "krankenkasse": "", "kv_zusatz": "0", "u1": "0",
                    "u2": "0", "pv_kinder": 0, "pv_elterneigenschaft": 0, "sachsen": 0, "pkv_beitrag": 0,
                    "pkv_pv_beitrag": 0, "rv_aufstockung": 1, "aufwandskonto": ""}
CENT_MERKMALE = ("freibetrag", "hinzurechnung", "pkv_beitrag", "pkv_pv_beitrag")
JA_NEIN_MERKMALE = ("pauschsteuer", "pv_elterneigenschaft", "sachsen", "rv_aufstockung")
PROZENT_MERKMALE = ("kv_zusatz", "u1", "u2")
# ELStAM-Kürzel der Religionszugehörigkeit; jede steuererhebende Gemeinschaft hat ihr eigenes
KONFESSIONEN = {"": "keine", "ev": "evangelisch", "rk": "römisch-katholisch", "ak": "altkatholisch",
                "lt": "lutherisch", "rf": "reformiert", "fr": "französisch-reformiert", "fa": "freireligiös Landesgemeinde",
                "fb": "freireligiös", "fg": "freireligiös", "fm": "freireligiös", "fs": "freireligiös",
                "ib": "israelitisch", "il": "israelitisch", "is": "israelitisch", "iw": "israelitisch",
                "jd": "jüdisch", "jh": "jüdisch"}
MONAT = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SUMMEN = ("brutto", "steuerbrutto", "steuerbrutto_einmal", "svbrutto", "svbrutto_einmal", "lst", "soli", "kist",
          "kist_ehegatte", "kv_an", "rv_an", "av_an", "pv_an", "sv_an", "kv_ag", "rv_ag", "av_ag", "pv_ag", "sv_ag",
          "u1", "u2", "insolvenz", "kv_pauschal", "rv_pauschal", "pauschsteuer", "pauschal_lst", "pauschal_soli",
          "pauschal_kist", "pkv_zuschuss", "pv_zuschuss", "sachbezug", "steuerfrei", "pauschal", "abzug", "netto",
          "auszahlung", "ag_kosten", "sv_tage")


def _require(ctx: Ctx) -> None:
    if not md.has(ctx, "lohn"):
        raise KontorError("Lohn ist für diesen Mandanten aus (Profilschalter lohn).")


def konten(ctx: Ctx) -> dict[str, str]:
    s = md.get_settings(ctx)
    return {k: s[f"lohn_konto_{k}"] for k in KONTEN}


# -- Hilfen ---------------------------------------------------------------------------------------------------------
def _monat(value: str, feld: str = "Monat") -> str:
    value = str(value or "").strip()
    if not MONAT.match(value):
        raise KontorError(f"{feld} als JJJJ-MM ({value!r}).")
    return value


def _tag(value: str, feld: str) -> str:
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        raise KontorError(f"{feld} als JJJJ-MM-TT ({value!r}).") from None


def _erster(monat: str) -> date:
    return date(int(monat[:4]), int(monat[5:]), 1)


def _letzter(monat: str) -> date:
    y, m = int(monat[:4]), int(monat[5:])
    return date(y, m, calendar.monthrange(y, m)[1])


def _monate(jahr: int) -> list[str]:
    return [f"{jahr}-{m:02d}" for m in range(1, 13)]


def _prozent(value, feld: str) -> str:
    try:
        d = Decimal(str(value).strip().replace(",", ".") or "0")
    except InvalidOperation:
        raise KontorError(f"{feld}: Prozent als Zahl, z. B. 2,5.") from None
    if d < 0 or d > 100:
        raise KontorError(f"{feld}: {value!r} liegt nicht zwischen 0 und 100.")
    return str(d.normalize()) if d else "0"


def _cent(value: int | str) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else md.to_cents(str(value))


def _abrunden(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_DOWN))


# -- Mitarbeiter ----------------------------------------------------------------------------------------------------
def mitarbeiter_speichern(ctx: Ctx, data: dict, mid: int | None = None) -> int:
    _require(ctx)
    unknown = set(data) - set(MITARBEITER_FELDER)
    if unknown:
        raise KontorError(f"Unbekannte Felder: {', '.join(sorted(unknown))}")
    clean = {k: (str(v).strip() if v is not None else "") for k, v in data.items()}
    for k in ("eintritt", "geburtsdatum", "austritt"):
        if clean.get(k):
            clean[k] = _tag(clean[k], k.capitalize())
    if "austritt" in clean and not clean["austritt"]:
        clean["austritt"] = None
    if clean.get("steuer_id") and not re.fullmatch(r"\d{11}", clean["steuer_id"]):
        raise KontorError("Steuer-ID hat elf Ziffern.")
    if mid is None:
        missing = [k for k in ("personalnr", "vorname", "nachname", "eintritt") if not clean.get(k)]
        if missing:
            raise KontorError(f"Für einen neuen Mitarbeiter fehlt: {', '.join(missing)}.")
    with tx(ctx.conn):
        if mid is None:
            cols = list(clean)
            cur = ctx.conn.execute(f"INSERT INTO lohn_mitarbeiter ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                   [clean[c] for c in cols])
            mid = cur.lastrowid
            ctx.conn.execute("INSERT INTO lohn_merkmale (mitarbeiter_id, gueltig_ab) VALUES (?, ?)",
                             (mid, clean["eintritt"][:7]))
        else:
            _get_row(ctx, mid)
            if clean:
                ctx.conn.execute(f"UPDATE lohn_mitarbeiter SET {', '.join(f'{k} = ?' for k in clean)} WHERE id = ?",
                                 [*clean.values(), mid])
        audit.log(ctx.conn, ctx.actor, "lohn_mitarbeiter", "lohn_mitarbeiter", mid,
                  {k: v for k, v in clean.items() if k not in ("iban", "steuer_id", "sv_nummer", "geburtsdatum")})
    return mid


def _get_row(ctx: Ctx, mid: int) -> dict:
    row = ctx.conn.execute("SELECT * FROM lohn_mitarbeiter WHERE id = ?", (mid,)).fetchone()
    if not row:
        raise KontorError(f"Mitarbeiter {mid} gibt es nicht.")
    return dict(row)


def mitarbeiter(ctx: Ctx, monat: str | None = None) -> list[dict]:
    """Alle Mitarbeiter, mit `monat` nur die in diesem Monat beschäftigten."""
    rows = [dict(r) for r in ctx.conn.execute("SELECT * FROM lohn_mitarbeiter ORDER BY nachname, vorname")]
    if monat:
        rows = [r for r in rows if _beschaeftigt(r, monat)]
    for r in rows:
        r["name"] = f"{r['vorname']} {r['nachname']}"
    return rows


def _beschaeftigt(m: dict, monat: str) -> bool:
    return m["eintritt"] <= _letzter(monat).isoformat() and (not m["austritt"] or m["austritt"] >= f"{monat}-01")


def get(ctx: Ctx, mid: int) -> dict:
    _require(ctx)
    m = _get_row(ctx, mid)
    m["name"] = f"{m['vorname']} {m['nachname']}"
    m["merkmale"] = [dict(r) for r in ctx.conn.execute(
        "SELECT * FROM lohn_merkmale WHERE mitarbeiter_id = ? ORDER BY gueltig_ab DESC", (mid,))]
    m["bezuege"] = bezuege(ctx, mid)
    m["vortrag"] = [{**dict(r), "daten": json.loads(r["daten"])} for r in ctx.conn.execute(
        "SELECT * FROM lohn_vortrag WHERE mitarbeiter_id = ? ORDER BY jahr DESC", (mid,))]
    return m


def merkmale_setzen(ctx: Ctx, mid: int, ab: str, data: dict) -> dict:
    """Neue Fassung der Merkmale ab Monat `ab`; was nicht angegeben ist, kommt aus der bis dahin geltenden Fassung."""
    _require(ctx)
    ab = _monat(ab, "Gültig ab")
    unknown = set(data) - set(MERKMAL_VORGABEN)
    if unknown:
        raise KontorError(f"Unbekannte Merkmale: {', '.join(sorted(unknown))}")
    _get_row(ctx, mid)
    if _fest_ab(ctx, mid, ab):
        raise KontorError(f"Ab {ab} gibt es schon festgeschriebene Abrechnungen; erst stornieren.")
    neu = {**merkmale_im(ctx, mid, ab, strict=False), **_merkmale_pruefen(data)}
    with tx(ctx.conn):
        cols = ["mitarbeiter_id", "gueltig_ab", *MERKMAL_VORGABEN]
        ctx.conn.execute(f"INSERT OR REPLACE INTO lohn_merkmale ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [mid, ab, *(neu[k] for k in MERKMAL_VORGABEN)])
        audit.log(ctx.conn, ctx.actor, "lohn_merkmale", "lohn_mitarbeiter", mid, {"ab": ab, **_merkmale_pruefen(data)})
    return merkmale_im(ctx, mid, ab)


def _merkmale_pruefen(data: dict) -> dict:
    out: dict = {}
    for k, v in data.items():
        v = "" if v is None else v
        if k == "steuerklasse":
            if str(v).strip() not in "123456" or not str(v).strip():
                raise KontorError("Steuerklasse 1 bis 6.")
            out[k] = int(v)
        elif k == "faktor":
            raw = str(v).strip().replace(",", ".")
            if raw and not re.fullmatch(r"0\.\d{1,3}", raw):
                raise KontorError("Faktor kleiner als 1 mit höchstens drei Nachkommastellen (z. B. 0,912).")
            out[k] = str(Decimal(raw).quantize(Decimal("0.001"))) if raw else ""
        elif k == "kinderfreibetraege":
            raw = str(v).strip().replace(",", ".") or "0"
            if not re.fullmatch(r"\d{1,2}(\.5)?|\d{1,2}\.0", raw):
                raise KontorError("Kinderfreibeträge in halben Schritten, z. B. 1,5.")
            out[k] = str(Decimal(raw).normalize()) if Decimal(raw) else "0"
        elif k in ("konfession", "konfession_ehegatte"):
            if str(v).strip() not in KONFESSIONEN:
                raise KontorError(f"{k}: {v!r} (erlaubt: leer, {', '.join(x for x in KONFESSIONEN if x)}).")
            out[k] = str(v).strip()
        elif k in CENT_MERKMALE:
            out[k] = _cent(v or 0)
        elif k in JA_NEIN_MERKMALE:
            out[k] = 1 if str(v).strip().lower() in ("1", "ja", "true", "on") else 0
        elif k in PROZENT_MERKMALE:
            out[k] = _prozent(v, k)
        elif k == "pv_kinder":
            if not str(v).strip().isdigit():
                raise KontorError("pv_kinder: Zahl der Kinder unter 25.")
            out[k] = int(v)
        elif k == "personengruppe":
            if not re.fullmatch(r"(\d{3})?", str(v).strip()):
                raise KontorError("Personengruppe hat drei Ziffern (z. B. 101, 109); leer = keine Sozialversicherung.")
            out[k] = str(v).strip()
        elif k == "beitragsgruppe":
            if not re.fullmatch(r"\d{4}", str(v).strip()):
                raise KontorError("Beitragsgruppe hat vier Ziffern: KV, RV, AV, PV (z. B. 1111, 0000).")
            out[k] = str(v).strip()
        elif k == "aufwandskonto":
            if str(v).strip() and not re.fullmatch(r"\d{3,5}", str(v).strip()):
                raise KontorError("Aufwandskonto als Nummer, z. B. 6024.")
            out[k] = str(v).strip()
        else:
            out[k] = str(v).strip()
    return out


def merkmale_im(ctx: Ctx, mid: int, monat: str, strict: bool = True) -> dict:
    row = ctx.conn.execute("SELECT * FROM lohn_merkmale WHERE mitarbeiter_id = ? AND gueltig_ab <= ? "
                           "ORDER BY gueltig_ab DESC LIMIT 1", (mid, monat)).fetchone()
    if row:
        return {k: row[k] for k in MERKMAL_VORGABEN} | {"gueltig_ab": row["gueltig_ab"]}
    if strict:
        raise KontorError(f"Für {monat} gibt es keine Merkmale (vor dem Eintritt?).")
    return dict(MERKMAL_VORGABEN)


# -- Bezüge ---------------------------------------------------------------------------------------------------------
def bezug(ctx: Ctx, mid: int, data: dict) -> int:
    """Bezug oder Abzug anlegen. `ab`/`bis` als JJJJ-MM; einmalig: nur `monat` angeben."""
    _require(ctx)
    _get_row(ctx, mid)
    art = str(data.get("lohnart", "")).strip()
    if art not in LOHNARTEN:
        raise KontorError(f"Lohnart {art!r} (erlaubt: {', '.join(LOHNARTEN)}).")
    if data.get("monat"):
        ab = bis = _monat(data["monat"])
    else:
        ab = _monat(data.get("ab", ""), "Ab")
        bis = _monat(data["bis"], "Bis") if data.get("bis") else None
    if art == "einmalzahlung" and bis != ab:
        raise KontorError("Eine Einmalzahlung gilt für genau einen Monat (monat angeben).")
    if bis and bis < ab:
        raise KontorError("Bis liegt vor Ab.")
    betrag = _cent(data.get("betrag", ""))
    if betrag <= 0:
        raise KontorError("Betrag größer als null.")
    satz = ""
    if art == "pauschal":
        satz = _prozent(data.get("satz", ""), "Pauschsteuersatz")
        if satz == "0":
            raise KontorError("Pauschal versteuert braucht den Steuersatz (z. B. 15, 20 oder 25).")
    konto = str(data.get("konto", "") or "").strip()
    if konto and not re.fullmatch(r"\d{3,5}", konto):
        raise KontorError("Konto als Nummer.")
    if _fest_ab(ctx, mid, ab):
        raise KontorError(f"Ab {ab} gibt es schon festgeschriebene Abrechnungen; erst stornieren.")
    with tx(ctx.conn):
        cur = ctx.conn.execute("INSERT INTO lohn_bezuege (mitarbeiter_id, lohnart, bezeichnung, betrag, satz, konto, ab, "
                               "bis) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (mid, art, str(data.get("bezeichnung", "") or LOHNARTEN[art]).strip(), betrag, satz,
                                konto, ab, bis))
        audit.log(ctx.conn, ctx.actor, "lohn_bezug", "lohn_bezuege", cur.lastrowid,
                  {"mitarbeiter": mid, "lohnart": art, "betrag": betrag, "ab": ab, "bis": bis})
    return cur.lastrowid


def bezug_beenden(ctx: Ctx, bid: int, bis: str) -> None:
    """Wiederkehrenden Bezug mit Monat `bis` enden lassen. Liegt `bis` vor `ab`, verschwindet er."""
    _require(ctx)
    row = ctx.conn.execute("SELECT * FROM lohn_bezuege WHERE id = ?", (bid,)).fetchone()
    if not row:
        raise KontorError(f"Bezug {bid} gibt es nicht.")
    bis = _monat(bis, "Bis")
    erster_betroffen = min(_naechster(bis), row["ab"])
    if _fest_ab(ctx, row["mitarbeiter_id"], erster_betroffen):
        raise KontorError("Der Bezug steckt in festgeschriebenen Abrechnungen; erst stornieren.")
    with tx(ctx.conn):
        if bis < row["ab"]:
            ctx.conn.execute("DELETE FROM lohn_bezuege WHERE id = ?", (bid,))
        else:
            ctx.conn.execute("UPDATE lohn_bezuege SET bis = ? WHERE id = ?", (bis, bid))
        audit.log(ctx.conn, ctx.actor, "lohn_bezug_ende", "lohn_bezuege", bid, {"bis": bis})


def _naechster(monat: str) -> str:
    y, m = int(monat[:4]), int(monat[5:])
    return f"{y + m // 12}-{m % 12 + 1:02d}"


def bezuege(ctx: Ctx, mid: int, monat: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM lohn_bezuege WHERE mitarbeiter_id = ?", [mid]
    if monat:
        sql += " AND ab <= ? AND (bis IS NULL OR bis >= ?)"
        args += [monat, monat]
    return [dict(r) for r in ctx.conn.execute(sql + " ORDER BY ab, id", args)]


def vortrag_setzen(ctx: Ctx, mid: int, jahr: int, bis_monat: str, daten: dict, notiz: str = "") -> None:
    """Jahreswerte bis einschließlich `bis_monat` aus einem anderen Programm (Wechsel im laufenden Jahr). Schlüssel
    wie die Summen einer Abrechnung (steuerbrutto, lst, soli, kist, kv_an, rv_ag, svbrutto, sv_tage …)."""
    _require(ctx)
    _get_row(ctx, mid)
    bis_monat = _monat(bis_monat, "Bis Monat")
    if int(bis_monat[:4]) != jahr:
        raise KontorError("Bis-Monat muss im Jahr des Vortrags liegen.")
    unknown = set(daten) - set(SUMMEN)
    if unknown:
        raise KontorError(f"Unbekannte Werte im Vortrag: {', '.join(sorted(unknown))}")
    clean = {k: int(v) if k == "sv_tage" else _cent(v) for k, v in daten.items()}
    if ctx.conn.execute("SELECT 1 FROM lohn_abrechnungen WHERE mitarbeiter_id = ? AND status = 'fest' AND monat <= ? "
                        "AND monat >= ?", (mid, bis_monat, f"{jahr}-01")).fetchone():
        raise KontorError("Für diese Monate gibt es schon Abrechnungen in Kontor.")
    with tx(ctx.conn):
        ctx.conn.execute("INSERT OR REPLACE INTO lohn_vortrag (mitarbeiter_id, jahr, bis_monat, daten, notiz) "
                         "VALUES (?, ?, ?, ?, ?)", (mid, jahr, bis_monat, json.dumps(clean), notiz))
        audit.log(ctx.conn, ctx.actor, "lohn_vortrag", "lohn_mitarbeiter", mid, {"jahr": jahr, "bis": bis_monat, **clean})


def _vortrag(ctx: Ctx, mid: int, jahr: int) -> tuple[str, dict]:
    row = ctx.conn.execute("SELECT bis_monat, daten FROM lohn_vortrag WHERE mitarbeiter_id = ? AND jahr = ?",
                           (mid, jahr)).fetchone()
    return (row["bis_monat"], json.loads(row["daten"])) if row else ("", {})


def _fest_ab(ctx: Ctx, mid: int, monat: str) -> bool:
    return ctx.conn.execute("SELECT 1 FROM lohn_abrechnungen WHERE mitarbeiter_id = ? AND status = 'fest' AND monat >= ?",
                            (mid, monat)).fetchone() is not None


# -- Rechnen --------------------------------------------------------------------------------------------------------
def _tage(m: dict, monat: str) -> int:
    """Beschäftigte Tage im Monat; ganzer Monat = 30 (SV-Tage)."""
    start, ende = _erster(monat), _letzter(monat)
    von = max(start, date.fromisoformat(m["eintritt"]))
    bis = min(ende, date.fromisoformat(m["austritt"])) if m["austritt"] else ende
    if von == start and bis == ende:
        return 30
    return max(0, (bis - von).days + 1)


def _alter_vollendet(geburtsdatum: str, jahre: int) -> date | None:
    """Tag, an dem das Lebensjahr `jahre` vollendet ist (Vortag des Geburtstags, § 187 Abs. 2, § 188 Abs. 2 BGB)."""
    if not geburtsdatum:
        return None
    g = date.fromisoformat(geburtsdatum)
    try:
        geburtstag = g.replace(year=g.year + jahre)
    except ValueError:                                  # 29. Februar
        geburtstag = date(g.year + jahre, 3, 1)
    return geburtstag - timedelta(days=1)


def _kinderlos(m: dict, mk: dict, monat: str) -> bool:
    """Kinderlosenzuschlag zur Pflegeversicherung: ab dem Monat nach Vollendung des 23. Lebensjahres, ohne
    nachgewiesene Elterneigenschaft (§ 55 Abs. 3 SGB XI). Ohne Geburtsdatum gilt er, sobald keine Eltern­eigenschaft
    eingetragen ist."""
    if mk["pv_elterneigenschaft"] or mk["pv_kinder"]:
        return False
    tag = _alter_vollendet(m["geburtsdatum"], 23)
    return tag is None or tag < _erster(monat)


def _jahreswerte(ctx: Ctx, mid: int, monat: str) -> dict:
    """Summen des Jahres vor `monat`: Vortrag plus festgeschriebene Abrechnungen."""
    jahr = int(monat[:4])
    bis, out = _vortrag(ctx, mid, jahr)
    out = {k: out.get(k, 0) for k in SUMMEN}
    for r in ctx.conn.execute("SELECT daten FROM lohn_abrechnungen WHERE mitarbeiter_id = ? AND status = 'fest' "
                              "AND monat >= ? AND monat < ?", (mid, f"{jahr}-01", monat)):
        s = json.loads(r["daten"])["summen"]
        for k in SUMMEN:
            out[k] += s.get(k, 0)
    out["_vortrag_bis"] = bis
    return out


def _fehlende_vormonate(ctx: Ctx, m: dict, monat: str) -> list[str]:
    jahr = int(monat[:4])
    bis, _ = _vortrag(ctx, m["id"], jahr)
    fest = {r[0] for r in ctx.conn.execute("SELECT monat FROM lohn_abrechnungen WHERE mitarbeiter_id = ? AND "
                                           "status = 'fest'", (m["id"],))}
    return [x for x in _monate(jahr) if x < monat and x > bis and _beschaeftigt(m, x) and x not in fest]


def _pap_eingaben(jahr: int, m: dict, mk: dict, monat: str, laufend: int, einmal: int, tage: int, jw: dict,
                  kinderlos: bool, zuschuss: int) -> dict:
    """Eingaben für den Programmablaufplan. Nur Namen, die der PAP des Jahres kennt, gehen hinein."""
    kv, rv, av = mk["beitragsgruppe"][0], mk["beitragsgruppe"][1], mk["beitragsgruppe"][2]
    privat = kv == "0" or mk["pkv_beitrag"] > 0
    teilmonat = tage < 30
    werte: dict = {
        "LZZ": 4 if teilmonat else 2,
        "RE4": round(Decimal(laufend) / tage) if teilmonat and tage else laufend,
        "STKL": mk["steuerklasse"],
        "ZKF": Decimal(mk["kinderfreibetraege"]),
        "R": 1 if mk["konfession"] else 0,
        "KVZ": Decimal(mk["kv_zusatz"]),
        "KRV": 1 if rv == "0" else 0,
        "ALV": 1 if av == "0" else 0,
        # PAP 2025: 2 = privat mit Arbeitgeberzuschuss; PAP 2026 kennt nur 0/1 und den Zuschuss als PKPVAGZ (Cent)
        "PKV": (2 if zuschuss and jahr <= 2025 else 1) if privat else 0,
        "PKPV": mk["pkv_beitrag"] + mk["pkv_pv_beitrag"] if privat else 0,
        "PKPVAGZ": zuschuss if privat else 0,
        "PVZ": 1 if kinderlos else 0,
        "PVS": 1 if mk["sachsen"] else 0,
        "PVA": max(0, min(mk["pv_kinder"], 5) - 1),
        "af": 1 if mk["faktor"] and mk["steuerklasse"] == 4 else 0,
        "f": Decimal(mk["faktor"]) if mk["faktor"] and mk["steuerklasse"] == 4 else Decimal(1),
        "LZZFREIB": mk["freibetrag"],
        "LZZHINZU": mk["hinzurechnung"],
    }
    tag64 = _alter_vollendet(m["geburtsdatum"], 64)
    if tag64 and tag64 < date(jahr, 1, 1):
        werte["ALTER1"], werte["AJAHR"] = 1, tag64.year + 1
    if einmal:
        # voraussichtlicher Jahresarbeitslohn ohne den sonstigen Bezug (R 39b.6 Abs. 2 LStR): bisher gezahlter
        # Arbeitslohn plus laufender Arbeitslohn für die übrigen Monate des Jahres
        rest = 12 - int(monat[5:]) + 1
        werte["SONSTB"] = einmal
        werte["JRE4"] = jw["steuerbrutto"] + jw["steuerbrutto_einmal"] + laufend * rest
        # Jahresfreibetrag und -hinzurechnung für sonstige Bezüge: Monatsbetrag × 12 (ELStAM nennen beide; ein im Jahr
        # beginnender Freibetrag hat einen kleineren Jahresbetrag, das deckt Kontor nicht ab)
        werte["JFREIB"], werte["JHINZU"] = mk["freibetrag"] * 12, mk["hinzurechnung"] * 12
    bekannt = lst.eingaben(jahr)
    return {k: v for k, v in werte.items() if k in bekannt}


def berechnen(ctx: Ctx, mid: int, monat: str) -> dict:
    """Abrechnung eines Monats als Entwurf. Speichert nichts."""
    _require(ctx)
    monat = _monat(monat)
    jahr = int(monat[:4])
    m = _get_row(ctx, mid)
    m["name"] = f"{m['vorname']} {m['nachname']}"
    if not _beschaeftigt(m, monat):
        raise KontorError(f"{m['name']} ist im {monat} nicht beschäftigt.")
    mk = merkmale_im(ctx, mid, monat)
    s = md.get_settings(ctx)
    tage = _tage(m, monat)
    jw = _jahreswerte(ctx, mid, monat)
    posten = bezuege(ctx, mid, monat)
    summe = {art: sum(p["betrag"] for p in posten if p["lohnart"] == art) for art in LOHNARTEN}
    laufend = summe["gehalt"] + summe["sachbezug"]
    einmal = summe["einmalzahlung"]
    kinderlos = _kinderlos(m, mk, monat)

    if not mk["personengruppe"]:
        # kein Beschäftigter im Sinne der Sozialversicherung (z. B. beherrschender Gesellschafter-Geschäftsführer):
        # keine Beiträge, keine Umlagen, keine Meldungen
        beitraege = {**sv.beitraege(jahr, entgelt=0, beitragsgruppe="0000", personengruppe="190"), "art": "keine"}
    else:
        u1, u2 = Decimal(mk["u1"]), Decimal(mk["u2"])
        if mk["personengruppe"] == "109":                   # Minijob: Umlagesätze der Minijob-Zentrale
            w = sv.werte(jahr)
            u1, u2 = Decimal(w["umlage_u1_minijob"]["wert"]), Decimal(w["umlage_u2_minijob"]["wert"])
        beitraege = sv.beitraege(
            jahr, entgelt=laufend, einmal=einmal, beitragsgruppe=mk["beitragsgruppe"],
            personengruppe=mk["personengruppe"], kv_zusatz=Decimal(mk["kv_zusatz"]), pv_kinder=mk["pv_kinder"],
            pv_kinderlos=kinderlos, sachsen=bool(mk["sachsen"]), u1=u1, u2=u2, sv_tage=tage,
            bisher_entgelt=jw["svbrutto"], bisher_sv_tage=jw["sv_tage"], bisher_einmal=jw["svbrutto_einmal"],
            pkv_beitrag=mk["pkv_beitrag"], pkv_pv_beitrag=mk["pkv_pv_beitrag"],
            rv_aufstockung=bool(mk["rv_aufstockung"]), monat=int(monat[5:]))

    lohnsteuer, soli, kist, kist_eg, pauschsteuer, pap = 0, 0, 0, 0, 0, {}
    if mk["pauschsteuer"]:
        # einheitliche Pauschsteuer 2 % für Minijobs (§ 40a Abs. 2 EStG), geht mit den Beiträgen an die Minijob-Zentrale
        pauschsteuer = _abrunden(Decimal(beitraege["pauschsteuer_basis"]) * 2 / 100)
    else:
        eingaben = _pap_eingaben(jahr, m, mk, monat, laufend, einmal, tage, jw, kinderlos,
                                 beitraege["pkv_zuschuss"] + beitraege["pv_zuschuss"])
        out = lst.lohnsteuer(jahr, **eingaben)
        faktor = tage if eingaben.get("LZZ") == 4 else 1
        lohnsteuer = out["lst"] * faktor + out["lst_sonst"]
        soli = out["soli"] * faktor + out["soli_sonst"]
        if mk["konfession"]:
            satz = Decimal(s["lohn_kist_satz"] or "9")
            kist_gesamt = (_abrunden(Decimal(out["kist_basis"]) * satz / 100) * faktor
                           + _abrunden(Decimal(out["kist_basis_sonst"]) * satz / 100))
            if mk["konfession_ehegatte"] and mk["konfession_ehegatte"] != mk["konfession"]:
                # konfessionsverschiedene Ehe: Halbteilung zwischen beiden Gemeinschaften
                kist_eg = kist_gesamt // 2
            kist = kist_gesamt - kist_eg
        pap = {"eingaben": {k: str(v) for k, v in eingaben.items()}, "ergebnis": out, "tagesfaktor": faktor}

    pauschal_lst = pauschal_soli = pauschal_kist = 0
    for p in posten:
        if p["lohnart"] == "pauschal":
            # Pauschalierung nach § 40 EStG: der Arbeitgeber trägt Lohnsteuer, Solidaritätszuschlag (5,5 %) und
            # pauschale Kirchensteuer im vereinfachten Verfahren (Satz in den Einstellungen)
            l = _abrunden(Decimal(p["betrag"]) * Decimal(p["satz"]) / 100)
            pauschal_lst += l
            pauschal_soli += _abrunden(Decimal(l) * Decimal("5.5") / 100)
            if not s["lohn_kist_pauschal"]:
                raise KontorError("Für pauschal versteuerte Bezüge fehlt der Satz der pauschalen Kirchensteuer "
                                  "(Einstellungen → Lohn).")
            pauschal_kist += _abrunden(Decimal(l) * Decimal(s["lohn_kist_pauschal"].replace(",", ".")) / 100)

    an = {k: beitraege[k] for k in ("kv_an", "rv_an", "av_an", "pv_an")}
    ag = {k: beitraege[k] for k in ("kv_ag", "rv_ag", "av_ag", "pv_ag")}
    sv_an, sv_ag = sum(an.values()), sum(ag.values())
    umlagen = beitraege["u1"] + beitraege["u2"] + beitraege["insolvenz"]
    zuschuss = beitraege["pkv_zuschuss"] + beitraege["pv_zuschuss"]
    brutto = laufend + einmal + summe["steuerfrei"] + summe["pauschal"]
    netto = brutto - lohnsteuer - soli - kist - kist_eg - sv_an
    auszahlung = netto - summe["sachbezug"] - summe["abzug"] + zuschuss
    ag_kosten = (brutto + sv_ag + umlagen + beitraege["kv_pauschal"] + beitraege["rv_pauschal"] + pauschsteuer
                 + pauschal_lst + pauschal_soli + pauschal_kist + zuschuss)
    summen = {"brutto": brutto, "steuerbrutto": 0 if mk["pauschsteuer"] else laufend,
              "steuerbrutto_einmal": 0 if mk["pauschsteuer"] else einmal,
              "svbrutto": laufend if beitraege["art"] != "keine" else 0,
              "svbrutto_einmal": einmal if beitraege["art"] != "keine" else 0,
              "lst": lohnsteuer, "soli": soli, "kist": kist, "kist_ehegatte": kist_eg, **an, "sv_an": sv_an, **ag,
              "sv_ag": sv_ag, "u1": beitraege["u1"], "u2": beitraege["u2"], "insolvenz": beitraege["insolvenz"],
              "kv_pauschal": beitraege["kv_pauschal"], "rv_pauschal": beitraege["rv_pauschal"],
              "pauschsteuer": pauschsteuer, "pauschal_lst": pauschal_lst, "pauschal_soli": pauschal_soli,
              "pauschal_kist": pauschal_kist, "pkv_zuschuss": beitraege["pkv_zuschuss"],
              "pv_zuschuss": beitraege["pv_zuschuss"], "sachbezug": summe["sachbezug"], "steuerfrei": summe["steuerfrei"],
              "pauschal": summe["pauschal"], "abzug": summe["abzug"], "netto": netto, "auszahlung": auszahlung,
              "ag_kosten": ag_kosten, "sv_tage": tage if beitraege["art"] != "keine" else 0}
    positionen = [{"id": p["id"], "lohnart": p["lohnart"], "bezeichnung": p["bezeichnung"], "betrag": p["betrag"],
                   "satz": p["satz"], "konto": p["konto"], "kennzeichen": KENNZEICHEN[p["lohnart"]]} for p in posten]
    jahr_bis = {k: jw[k] + summen[k] for k in SUMMEN}
    return {"mitarbeiter_id": mid, "monat": monat, "status": "entwurf", "name": m["name"], "personalnr": m["personalnr"],
            "mitarbeiter": {k: m[k] for k in MITARBEITER_FELDER}, "merkmale": mk, "kinderlos": kinderlos,
            "tage": tage, "positionen": positionen, "summen": summen, "sv": beitraege, "pap": pap,
            "jahr_bis": jahr_bis, "fehlende_vormonate": _fehlende_vormonate(ctx, m, monat)}


def abrechnungen(ctx: Ctx, monat: str) -> list[dict]:
    """Je beschäftigtem Mitarbeiter die festgeschriebene Abrechnung oder den Entwurf. Fehler im Entwurf stehen im
    Feld `fehler`, statt die ganze Liste scheitern zu lassen."""
    _require(ctx)
    monat = _monat(monat)
    out = []
    for m in mitarbeiter(ctx, monat):
        row = ctx.conn.execute("SELECT * FROM lohn_abrechnungen WHERE mitarbeiter_id = ? AND monat = ? AND "
                               "status = 'fest'", (m["id"], monat)).fetchone()
        if row:
            out.append(_aus_row(row))
            continue
        try:
            out.append(berechnen(ctx, m["id"], monat))
        except KontorError as e:
            out.append({"mitarbeiter_id": m["id"], "monat": monat, "status": "fehler", "name": m["name"],
                        "personalnr": m["personalnr"], "fehler": str(e)})
    return out


def _aus_row(row) -> dict:
    d = json.loads(row["daten"])
    return {**d, "id": row["id"], "status": row["status"], "pdf": row["pdf"],
            "festgeschrieben_am": row["festgeschrieben_am"], "storniert_am": row["storniert_am"],
            "storno_grund": row["storno_grund"]}


def abrechnung(ctx: Ctx, aid: int) -> dict:
    row = ctx.conn.execute("SELECT * FROM lohn_abrechnungen WHERE id = ?", (aid,)).fetchone()
    if not row:
        raise KontorError(f"Abrechnung {aid} gibt es nicht.")
    return _aus_row(row)


def _jahr_gesperrt(ctx: Ctx, jahr: int) -> bool:
    return ctx.conn.execute("SELECT 1 FROM journal_locks WHERE year = ?", (jahr,)).fetchone() is not None


def festschreiben(ctx: Ctx, mid: int, monat: str) -> int:
    """Abrechnung festschreiben: speichert sie unveränderlich und legt die Entgeltbescheinigung als PDF
    schreibgeschützt unter archiv/lohn/<jahr>/ ab. Darf auch der Agent (auf Zuruf); das Protokoll nennt, wer es war,
    ein Storno mit Grund macht es rückgängig. Senden an Behörden geht nur über eine Freigabe (services/auftraege)."""
    d = berechnen(ctx, mid, monat)
    if d["fehlende_vormonate"]:
        raise KontorError(f"Erst die Vormonate festschreiben: {', '.join(d['fehlende_vormonate'])}.")
    if _jahr_gesperrt(ctx, int(monat[:4])):
        raise KontorError(f"Das Jahr {monat[:4]} ist gesperrt.")
    s = d["summen"]
    with tx(ctx.conn):
        cur = ctx.conn.execute("INSERT INTO lohn_abrechnungen (mitarbeiter_id, monat, daten, brutto, netto, auszahlung, "
                               "ag_kosten) VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (mid, d["monat"], json.dumps({**d, "status": "fest"}, default=str), s["brutto"],
                                s["netto"], s["auszahlung"], s["ag_kosten"]))
        aid = cur.lastrowid
        audit.log(ctx.conn, ctx.actor, "lohn_festschreiben", "lohn_abrechnungen", aid,
                  {"mitarbeiter": mid, "monat": d["monat"], "brutto": s["brutto"], "lst": s["lst"],
                   "auszahlung": s["auszahlung"]})
    pdf = _pdf_ablegen(ctx, aid)
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE lohn_abrechnungen SET pdf = ? WHERE id = ?", (pdf, aid))
    return aid


def festschreiben_bis(ctx: Ctx, mid: int, monat: str) -> list[int]:
    """Alle offenen Monate eines Mitarbeiters bis einschließlich `monat` der Reihe nach festschreiben."""
    d = berechnen(ctx, mid, monat)
    return [festschreiben(ctx, mid, m) for m in [*d["fehlende_vormonate"], d["monat"]]]


def _pdf_ablegen(ctx: Ctx, aid: int) -> str:
    from ..render.lohn import entgeltbescheinigung
    a = abrechnung(ctx, aid)
    folder = ctx.data / "archiv" / "lohn" / a["monat"][:4]
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{a['monat']}-{_slug(a['personalnr'])}-{aid}.pdf"
    target.write_bytes(entgeltbescheinigung(a, arbeitgeber(ctx)))
    os.chmod(target, 0o444)
    return str(target.relative_to(ctx.data))


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-") or "x"


def stornieren(ctx: Ctx, aid: int, grund: str) -> None:
    """Festgeschriebene Abrechnung stornieren (mit Grund). Nur die jüngste eines Mitarbeiters; die
    Entgeltbescheinigung bleibt im Archiv."""
    if not (grund or "").strip():
        raise KontorError("Für ein Storno einen Grund angeben.")
    a = abrechnung(ctx, aid)
    if a["status"] != "fest":
        raise KontorError("Die Abrechnung ist schon storniert.")
    if _jahr_gesperrt(ctx, int(a["monat"][:4])):
        raise KontorError(f"Das Jahr {a['monat'][:4]} ist gesperrt.")
    later = ctx.conn.execute("SELECT monat FROM lohn_abrechnungen WHERE mitarbeiter_id = ? AND status = 'fest' AND "
                             "monat > ? ORDER BY monat", (a["mitarbeiter_id"], a["monat"])).fetchall()
    if later:
        raise KontorError(f"Erst die späteren Monate stornieren: {', '.join(r[0] for r in later)}.")
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE lohn_abrechnungen SET status = 'storniert', storniert_am = "
                         "strftime('%Y-%m-%dT%H:%M:%S','now','localtime'), storno_grund = ? WHERE id = ?",
                         (grund.strip(), aid))
        audit.log(ctx.conn, ctx.actor, "lohn_storno", "lohn_abrechnungen", aid,
                  {"monat": a["monat"], "grund": grund.strip()})


def festgeschrieben(ctx: Ctx, jahr: int, mid: int | None = None, monate: list[str] | None = None) -> list[dict]:
    sql, args = "SELECT * FROM lohn_abrechnungen WHERE status = 'fest' AND monat >= ? AND monat <= ?", \
        [f"{jahr}-01", f"{jahr}-12"]
    if mid is not None:
        sql += " AND mitarbeiter_id = ?"
        args.append(mid)
    rows = [_aus_row(r) for r in ctx.conn.execute(sql + " ORDER BY monat, mitarbeiter_id", args)]
    return [r for r in rows if monate is None or r["monat"] in monate]


# -- Arbeitgeber ----------------------------------------------------------------------------------------------------
def arbeitgeber(ctx: Ctx) -> dict:
    s = md.get_settings(ctx)
    return {"name": s["name"], "strasse": s["street"], "plz": s["postcode"], "ort": s["city"],
            "steuernummer": s["lohn_steuernummer"] or s["tax_number"],
            "finanzamt_nr": s["lohn_finanzamt_nr"] or s["finanzamt_nr"], "finanzamt": s["lohn_finanzamt_name"],
            "betriebsnummer": s["lohn_betriebsnummer"]}


# -- Auswertungen ---------------------------------------------------------------------------------------------------
def lohnjournal(ctx: Ctx, jahr: int, monat: str | None = None) -> dict:
    """Alle festgeschriebenen Abrechnungen eines Jahres oder Monats mit Summen."""
    _require(ctx)
    rows = festgeschrieben(ctx, jahr, monate=[_monat(monat)] if monat else None)
    total = {k: sum(r["summen"][k] for r in rows) for k in SUMMEN}
    return {"jahr": jahr, "monat": monat, "zeilen": rows, "summe": total}


def lohnkonto(ctx: Ctx, mid: int, jahr: int) -> dict:
    """Lohnkonto (§ 41 EStG, § 4 LStDV): Stammdaten, Merkmale und je Monat die Werte der Abrechnung."""
    _require(ctx)
    m = get(ctx, mid)
    rows = festgeschrieben(ctx, jahr, mid)
    bis, vortrag = _vortrag(ctx, mid, jahr)
    total = {k: vortrag.get(k, 0) + sum(r["summen"][k] for r in rows) for k in SUMMEN}
    return {"jahr": jahr, "mitarbeiter": m, "monate": rows, "vortrag": {"bis": bis, "werte": vortrag}, "summe": total,
            "arbeitgeber": arbeitgeber(ctx)}


def lohnkosten(ctx: Ctx, jahr: int) -> dict:
    """Arbeitgeberkosten je Mitarbeiter und Monat: Brutto, Arbeitgeberanteile, Umlagen, Pauschalsteuern."""
    _require(ctx)
    teile = ("brutto", "sv_ag", "u1", "u2", "insolvenz", "kv_pauschal", "rv_pauschal", "pauschsteuer", "pauschal_lst",
             "pauschal_soli", "pauschal_kist", "pkv_zuschuss", "pv_zuschuss", "ag_kosten")
    je: dict[int, dict] = {}
    for r in festgeschrieben(ctx, jahr):
        e = je.setdefault(r["mitarbeiter_id"], {"name": r["name"], "personalnr": r["personalnr"],
                                                "monate": {}, "summe": {k: 0 for k in teile}})
        e["monate"][r["monat"]] = {k: r["summen"][k] for k in teile}
        for k in teile:
            e["summe"][k] += r["summen"][k]
    gesamt = {k: sum(e["summe"][k] for e in je.values()) for k in teile}
    monate = {mo: sum(e["monate"].get(mo, {}).get("ag_kosten", 0) for e in je.values()) for mo in _monate(jahr)}
    return {"jahr": jahr, "mitarbeiter": list(je.values()), "summe": gesamt, "monate": monate, "teile": teile}


def beitraege_kassen(ctx: Ctx, monat: str) -> list[dict]:
    """Werte für den Beitragsnachweis je Krankenkasse (bzw. Minijob-Zentrale). Kontor übermittelt keine
    SV-Meldungen; die Werte gehen über sv.net oder ein zertifiziertes Programm an die Kasse."""
    _require(ctx)
    monat = _monat(monat)
    out: dict[str, dict] = {}
    for r in festgeschrieben(ctx, int(monat[:4]), monate=[monat]):
        s, mk = r["summen"], r["merkmale"]
        minijob = r["sv"]["art"] == "minijob"
        kasse = "Minijob-Zentrale" if minijob else (mk["krankenkasse"] or "ohne Krankenkasse")
        if r["sv"]["art"] == "keine" and not minijob and not s["u1"] and not s["insolvenz"]:
            continue
        e = out.setdefault(kasse, {"kasse": kasse, "kv": 0, "rv": 0, "av": 0, "pv": 0, "kv_pauschal": 0,
                                   "rv_pauschal": 0, "u1": 0, "u2": 0, "insolvenz": 0, "pauschsteuer": 0, "summe": 0,
                                   "mitarbeiter": 0})
        for k, an, ag in (("kv", "kv_an", "kv_ag"), ("rv", "rv_an", "rv_ag"), ("av", "av_an", "av_ag"),
                          ("pv", "pv_an", "pv_ag")):
            e[k] += s[an] + s[ag]
        for k in ("kv_pauschal", "rv_pauschal", "u1", "u2", "insolvenz", "pauschsteuer"):
            e[k] += s[k]
        e["mitarbeiter"] += 1
    for e in out.values():
        e["summe"] = sum(e[k] for k in ("kv", "rv", "av", "pv", "kv_pauschal", "rv_pauschal", "u1", "u2", "insolvenz",
                                        "pauschsteuer"))
    return sorted(out.values(), key=lambda e: e["kasse"])


# -- Lohnsteuer-Anmeldung -------------------------------------------------------------------------------------------
ZEITRAEUME = {"monat": [(f"{m:02d}", [m]) for m in range(1, 13)],
              "quartal": [(f"4{q}", [3 * q - 2, 3 * q - 1, 3 * q]) for q in range(1, 5)],
              "jahr": [("19", list(range(1, 13)))]}
MONATSNAMEN = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
               "November", "Dezember")


def zeitraum_name(zeitraum: str) -> str:
    if zeitraum == "19":
        return "Kalenderjahr"
    if zeitraum.startswith("4"):
        return f"{zeitraum[1]}. Quartal"
    return MONATSNAMEN[int(zeitraum) - 1]


def zeitraeume(ctx: Ctx, jahr: int) -> list[dict]:
    """Anmeldungszeiträume des Jahres nach der Einstellung lohn_anmeldezeitraum (§ 41a Abs. 2 EStG) mit Werten und
    zuletzt gesendeter Meldung."""
    _require(ctx)
    art = md.get_settings(ctx)["lohn_anmeldezeitraum"]
    out = []
    for z, monate in ZEITRAEUME[art]:
        # offene Monate zählen mit ihren Entwurfswerten (sonst stünde vor dem Festschreiben überall 0 und jede
        # schon gesendete Anmeldung wiche scheinbar ab); `offen` nennt sie
        w = lsta_werte(ctx, jahr, z, entwuerfe=True)
        last = ctx.conn.execute("SELECT * FROM lohn_meldungen WHERE art = 'lsta' AND jahr = ? AND zeitraum = ? AND "
                                "test = 0 AND ok = 1 ORDER BY id DESC LIMIT 1", (jahr, z)).fetchone()
        gesendet = json.loads(last["werte"]) if last else None
        ende = _letzter(f"{jahr}-{monate[-1]:02d}")
        faellig = ende + timedelta(days=10)
        out.append({**w, "name": zeitraum_name(z), "faellig": faellig.isoformat(),
                    "gesendet": dict(last) if last else None,
                    "abweichung": bool(gesendet and gesendet.get("kz") != w["kz"])})
    return out


def _monate_zeitraum(jahr: int, zeitraum: str) -> list[str]:
    for art in ZEITRAEUME.values():
        for z, monate in art:
            if z == zeitraum:
                return [f"{jahr}-{m:02d}" for m in monate]
    raise KontorError(f"Zeitraum {zeitraum!r}: 01–12 (Monat), 41–44 (Quartal) oder 19 (Jahr).")


def lsta_werte(ctx: Ctx, jahr: int, zeitraum: str, entwuerfe: bool = False) -> dict:
    """Kennzahlen der Lohnsteuer-Anmeldung aus den festgeschriebenen Abrechnungen des Zeitraums, mit `entwuerfe` für
    offene Monate aus den Entwürfen. Minijobs mit Pauschsteuer gehören nicht hinein (die geht an die
    Minijob-Zentrale)."""
    _require(ctx)
    monate = _monate_zeitraum(jahr, zeitraum)
    rows = [r for r in festgeschrieben(ctx, jahr, monate=monate) if not r["merkmale"]["pauschsteuer"]]
    offen = []
    for m in mitarbeiter(ctx):
        for mo in monate:
            if _beschaeftigt(m, mo) and not ctx.conn.execute(
                    "SELECT 1 FROM lohn_abrechnungen WHERE mitarbeiter_id = ? AND monat = ? AND status = 'fest'",
                    (m["id"], mo)).fetchone() and mo > _vortrag(ctx, m["id"], jahr)[0]:
                offen.append(f"{m['name']} {mo}")
                if entwuerfe:
                    d = berechnen(ctx, m["id"], mo)
                    if not d["merkmale"]["pauschsteuer"]:
                        rows.append(d)
    kist: dict[str, int] = {}
    for r in rows:
        mk, s = r["merkmale"], r["summen"]
        if s["kist"]:
            kist[mk["konfession"]] = kist.get(mk["konfession"], 0) + s["kist"]
        if s["kist_ehegatte"]:
            kist[mk["konfession_ehegatte"]] = kist.get(mk["konfession_ehegatte"], 0) + s["kist_ehegatte"]
    lohnsteuer = sum(r["summen"]["lst"] + r["summen"]["pauschal_lst"] for r in rows)
    soli = sum(r["summen"]["soli"] + r["summen"]["pauschal_soli"] for r in rows)
    pauschal_kist = sum(r["summen"]["pauschal_kist"] for r in rows)
    kz = {"arbeitnehmer": len({r["mitarbeiter_id"] for r in rows}), "lst": lohnsteuer, "soli": soli,
          "pauschal_kist": pauschal_kist, "kist": dict(sorted(kist.items()))}
    kz["gesamt"] = lohnsteuer + soli + pauschal_kist + sum(kist.values())
    return {"jahr": jahr, "zeitraum": zeitraum, "monate": monate, "kz": kz, "offen": offen}


# -- Lohnsteuerbescheinigung ----------------------------------------------------------------------------------------
def lstb_werte(ctx: Ctx, mid: int, jahr: int) -> dict:
    """Werte der Lohnsteuerbescheinigung eines Jahres (§ 41b EStG): Summen aus Vortrag und Abrechnungen,
    Zeiträume der Besteuerungsmerkmale."""
    _require(ctx)
    k = lohnkonto(ctx, mid, jahr)
    m, rows, s = k["mitarbeiter"], k["monate"], k["summe"]
    if any(r["merkmale"]["pauschsteuer"] for r in rows):
        raise KontorError("Für Minijobs mit Pauschsteuer gibt es keine Lohnsteuerbescheinigung.")
    if not rows and not k["vortrag"]["bis"]:
        raise KontorError(f"Für {m['name']} gibt es {jahr} keine Abrechnungen.")
    von = max(date(jahr, 1, 1), date.fromisoformat(m["eintritt"]))
    bis = min(date(jahr, 12, 31), date.fromisoformat(m["austritt"])) if m["austritt"] else date(jahr, 12, 31)
    perioden: list[dict] = []
    je_monat = {r["monat"]: r["merkmale"] for r in rows}
    for monat in _monate(jahr):
        if not _beschaeftigt(m, monat):
            continue
        # Monate aus dem Vortrag (anderes Programm) tragen die Merkmale, die Kontor für sie kennt
        mk = je_monat.get(monat) or merkmale_im(ctx, mid, monat, strict=False)
        key = (mk["steuerklasse"], mk["faktor"], mk["kinderfreibetraege"], mk["konfession"], mk["konfession_ehegatte"])
        start, ende = max(_erster(monat), von), min(_letzter(monat), bis)
        if perioden and perioden[-1]["_key"] == key and \
                date.fromisoformat(perioden[-1]["bis"]) + timedelta(days=1) == start:
            perioden[-1]["bis"] = ende.isoformat()
        else:
            perioden.append({"_key": key, "von": start.isoformat(), "bis": ende.isoformat(),
                             "steuerklasse": mk["steuerklasse"], "faktor": mk["faktor"],
                             "kinderfreibetraege": mk["kinderfreibetraege"], "konfession": mk["konfession"],
                             "konfession_ehegatte": mk["konfession_ehegatte"]})
    for p in perioden:
        p.pop("_key")
    privat = any(r["merkmale"]["beitragsgruppe"][0] == "0" for r in rows)
    return {"jahr": jahr, "mitarbeiter_id": mid, "name": m["name"], "vorname": m["vorname"], "nachname": m["nachname"],
            "geburtsdatum": m["geburtsdatum"], "steuer_id": m["steuer_id"], "personalnr": m["personalnr"],
            "strasse": m["strasse"], "plz": m["plz"], "ort": m["ort"], "von": von.isoformat(), "bis": bis.isoformat(),
            "merkmale": perioden,
            "zeile3_brutto": s["steuerbrutto"] + s["steuerbrutto_einmal"], "zeile4_lst": s["lst"],
            "zeile5_soli": s["soli"], "zeile6_kist": s["kist"], "zeile7_kist_ehegatte": s["kist_ehegatte"],
            "zeile22a_rv_ag": s["rv_ag"], "zeile23a_rv_an": s["rv_an"],
            "zeile24a_kv_zuschuss": 0, "zeile24c_pv_zuschuss": 0,
            # Zuschüsse zur privaten Kranken- und Pflege-Pflichtversicherung gehören beide in Nummer 24b
            "zeile24b_pkv_zuschuss": s["pkv_zuschuss"] + s["pv_zuschuss"],
            "zeile25_kv_an": s["kv_an"], "zeile26_pv_an": s["pv_an"], "zeile27_av_an": s["av_an"],
            "zeile28_pkv": sum(r["merkmale"]["pkv_beitrag"] + r["merkmale"]["pkv_pv_beitrag"] for r in rows)
            if privat else 0,
            "gesendet": [dict(r) for r in ctx.conn.execute(
                "SELECT * FROM lohn_meldungen WHERE art = 'lstb' AND jahr = ? AND mitarbeiter_id = ? ORDER BY id DESC",
                (jahr, mid))]}


# -- Auffälligkeiten ------------------------------------------------------------------------------------------------
def auffaelligkeiten(ctx: Ctx, jahr: int) -> list[str]:
    _require(ctx)
    out = []
    heute = ctx.now_date()
    s = md.get_settings(ctx)
    for m in mitarbeiter(ctx):
        for mo in _monate(jahr):
            if _erster(mo) > heute or not _beschaeftigt(m, mo):
                continue
            if mo <= _vortrag(ctx, m["id"], jahr)[0]:
                continue
            if _letzter(mo) < heute and not ctx.conn.execute(
                    "SELECT 1 FROM lohn_abrechnungen WHERE mitarbeiter_id = ? AND monat = ? AND status = 'fest'",
                    (m["id"], mo)).fetchone():
                out.append(f"{m['name']}: {mo} ist nicht festgeschrieben.")
        if not m["steuer_id"]:
            out.append(f"{m['name']}: Steuer-ID fehlt (Lohnsteuerbescheinigung).")
    for z in zeitraeume(ctx, jahr):
        ende = date.fromisoformat(z["faellig"])
        if z["gesendet"] is None and ende <= heute + timedelta(days=10) and _letzter(z["monate"][-1]) < heute:
            out.append(f"Lohnsteuer-Anmeldung {z['name']} {jahr} fällig am {ende.strftime('%d.%m.%Y')}, noch nicht gesendet.")
        if z["abweichung"]:
            out.append(f"Lohnsteuer-Anmeldung {z['name']} {jahr}: Werte weichen von der gesendeten ab "
                       "(berichtigte Anmeldung senden).")
    if not (s["lohn_steuernummer"] or s["tax_number"]):
        out.append("Steuernummer der Betriebsstätte fehlt (Einstellungen).")
    for e in elstam(ctx, offen=True):
        if e["abweichung"]:
            felder = ", ".join(f"{k} {v}" for k, v in e["abweichung"].items())
            out.append(f"ELStAM für {e['name'] or e['idnr']} ab {e['gueltig_ab'][:7]} weichen ab ({felder}): "
                       "übernehmen oder verwerfen.")
        elif e["art"] and e["art"].lower().startswith(("ablehn", "fehler")):
            out.append(f"ELStAM-Ablehnung für {e['name'] or e['idnr']}: {e['daten'].get('text', e['art'])}")
    return out


# -- Buchungen ------------------------------------------------------------------------------------------------------
def buchungen(ctx: Ctx, jahr: int) -> list[dict]:
    """Journalzeilen der festgeschriebenen Abrechnungen, je Abrechnung zum Monatsletzten (Lohnaufwand des Monats).
    Die Zahlungen gleichen die Verbindlichkeiten über die Bank aus (Zuordnung auf die Verbindlichkeitskonten)."""
    if not md.has(ctx, "lohn"):
        return []
    k = konten(ctx)
    out: list[dict] = []
    for r in festgeschrieben(ctx, jahr):
        s, mk = r["summen"], r["merkmale"]
        day, ref = _letzter(r["monat"]).isoformat(), f"LOHN{r['id']}"
        text = f"Lohn {r['monat']} {r['name']}"
        aufwand = mk["aufwandskonto"] or k["gehalt"]
        konto: dict[str, int] = {}

        def add(account: str, amount: int) -> None:
            if amount:
                konto[account] = konto.get(account, 0) + amount

        for p in r["positionen"]:
            ziel = p["konto"]
            if p["lohnart"] in ("gehalt", "einmalzahlung", "sachbezug", "pauschal"):
                add(ziel or aufwand, p["betrag"])
            elif p["lohnart"] == "steuerfrei":
                add(ziel or k["steuerfrei"], p["betrag"])
            elif p["lohnart"] == "abzug":
                add(ziel or k["abzug"], -p["betrag"])
        add(k["sachbezug"], -s["sachbezug"])
        add(k["verb_lohn"], -s["auszahlung"])
        add(k["verb_steuer"], -(s["lst"] + s["soli"] + s["kist"] + s["kist_ehegatte"]))
        add(k["verb_sv"], -s["sv_an"])
        sozial = s["sv_ag"] + s["u1"] + s["u2"] + s["insolvenz"] + s["kv_pauschal"] + s["rv_pauschal"]
        add(k["sv_ag"], sozial + s["pkv_zuschuss"] + s["pv_zuschuss"])
        add(k["verb_sv"], -sozial)
        add(k["pauschsteuer"], s["pauschsteuer"])
        add(k["verb_sv"], -s["pauschsteuer"])
        pauschal = s["pauschal_lst"] + s["pauschal_soli"] + s["pauschal_kist"]
        add(k["pauschal_lst"], pauschal)
        add(k["verb_steuer"], -pauschal)
        if sum(konto.values()) != 0:
            raise KontorError(f"Abrechnung {r['id']} geht nicht auf ({sum(konto.values())} Cent).")
        for account, amount in sorted(konto.items(), key=lambda kv: (kv[1] < 0, kv[0])):
            out.append({"date": day, "account": account, "amount": amount,
                        "contra": k["verb_lohn"] if amount > 0 else aufwand, "text": text, "beleg": ref,
                        "tax_rate": "", "tax_account": "", "art": "auto", "ref": ref})
    return out


# -- ELSTER: Lohnsteuer-Anmeldung und Lohnsteuerbescheinigung -------------------------------------------------------
# Kirchensteuer je Gemeinschaft auf ihre Kennzahl der Anmeldung (ERiC-Jahresdokumentation LStA). Lutherisch,
# reformiert und französisch-reformiert sind evangelische Kirchensteuer; die übrigen hängen vom Land ab und werden
# nicht geraten.
KIST_KZ = {"ev": "kz61", "lt": "kz61", "rf": "kz61", "fr": "kz61", "rk": "kz62", "ak": "kz63", "is": "kz64",
           "ib": "kz64", "il": "kz64", "iw": "kz64", "jd": "kz64", "jh": "kz64"}
PLATZHALTER_HERSTELLER_ID = "00000"        # nur für die lokale Prüfung, bevor die eigene Hersteller-ID da ist


def _elster_ag(ctx: Ctx, steuernummer: bool = True):
    from ..elster import eric
    from ..elster import lohn as el
    s, ag = md.get_settings(ctx), arbeitgeber(ctx)
    needed = [("Name", ag["name"]), ("Straße", ag["strasse"]), ("PLZ", ag["plz"]), ("Ort", ag["ort"]),
              ("Steuernummer der Betriebsstätte", ag["steuernummer"]), ("Finanzamtsnummer", ag["finanzamt_nr"])]
    missing = [label for label, v in needed if not v]
    if missing:
        raise KontorError(f"Für ELSTER fehlt in den Einstellungen: {', '.join(missing)}.")
    m = re.fullmatch(r"(.*?)\s+(\d+\s*[a-zA-Z]?(?:\s*-\s*\d+\s*[a-zA-Z]?)?)", ag["strasse"].strip())
    strasse, hausnummer = (m.group(1), m.group(2)) if m else (ag["strasse"], "")
    if s["eric_pfad"]:
        os.environ["KONTOR_ERIC"] = s["eric_pfad"]
    stnr = eric.steuernummer13(ag["steuernummer"], ag["finanzamt_nr"]) if steuernummer else ""
    return el.Arbeitgeber(ag["name"], strasse, hausnummer, ag["plz"], ag["ort"], stnr, email=s["email"],
                          telefon=s["phone"], finanzamt=ag["finanzamt"])


def lsta_satz(ctx: Ctx, jahr: int, zeitraum: str):
    """Die Anmeldung als Datensatz-Objekt. Berichtigt, wenn für den Zeitraum schon echt gesendet wurde."""
    from ..elster import lohn as el
    w = lsta_werte(ctx, jahr, zeitraum)
    if w["offen"]:
        raise KontorError(f"Im Zeitraum ist nicht alles festgeschrieben: {', '.join(w['offen'])}.")
    if _letzter(w["monate"][-1]) >= ctx.now_date():
        raise KontorError("Der Anmeldungszeitraum ist noch nicht vorbei.")
    kz = w["kz"]
    kist: dict[str, int] = {}
    for konf, betrag in kz["kist"].items():
        if konf not in KIST_KZ:
            raise KontorError(f"Kirchensteuer der Gemeinschaft {konf!r}: die Kennzahl hängt vom Land ab und ist in "
                              "Kontor nicht hinterlegt.")
        kist[KIST_KZ[konf]] = kist.get(KIST_KZ[konf], 0) + betrag
    pauschal = sum(r["summen"]["pauschal_lst"] for r in festgeschrieben(ctx, jahr, monate=w["monate"])
                   if not r["merkmale"]["pauschsteuer"])
    berichtigt = ctx.conn.execute("SELECT 1 FROM lohn_meldungen WHERE art = 'lsta' AND jahr = ? AND zeitraum = ? AND "
                                  "test = 0 AND ok = 1", (jahr, zeitraum)).fetchone() is not None
    if berichtigt and not ctx.conn.execute("SELECT 1 FROM lohn_meldungen WHERE art = 'lsta' AND jahr = ? AND zeitraum = ? "
                                           "AND test = 0 AND ok = 1 AND transferticket NOT LIKE 'extern:%'",
                                           (jahr, zeitraum)).fetchone():
        # nur ein anderes Programm hat gesendet: eine Berichtigung ginge über das, was dort gesendet wurde
        if lsta_werte(ctx, jahr, zeitraum)["kz"] == json.loads(ctx.conn.execute(
                "SELECT werte FROM lohn_meldungen WHERE art = 'lsta' AND jahr = ? AND zeitraum = ? AND test = 0 AND ok = 1 "
                "ORDER BY id DESC LIMIT 1", (jahr, zeitraum)).fetchone()[0])["kz"]:
            raise KontorError("Diese Anmeldung hat schon ein anderes Programm gesendet, mit denselben Werten.")
    return el.LStA(jahr, zeitraum, kz["arbeitnehmer"], kz42=kz["lst"] - pauschal, kz41=pauschal, kz49=kz["soli"],
                   kz47=kz["pauschal_kist"], berichtigt=berichtigt, erstellt=ctx.now_date(), **kist), w


def _hersteller(ctx: Ctx, senden: bool) -> str:
    hid = md.get_settings(ctx)["elster_hersteller_id"]
    if senden and not hid:
        raise KontorError("Senden braucht die eigene Hersteller-ID (Einstellungen → ELSTER).")
    return hid or PLATZHALTER_HERSTELLER_ID


def _datenlieferant(ctx: Ctx) -> str:
    s = md.get_settings(ctx)
    return s["managing_directors"] or s["name"]


def lsta_pruefen(ctx: Ctx, jahr: int, zeitraum: str) -> dict:
    """Lokale Prüfung durch ERiC, ohne Versand."""
    _require(ctx)
    from ..elster import eric
    from ..elster import lohn as el
    a, w = lsta_satz(ctx, jahr, zeitraum)
    xml = el.lsta(a, _elster_ag(ctx), _hersteller(ctx, False), True, _datenlieferant(ctx))
    r = _plausibel(eric.pruefen(xml, el.lsta_datenart(jahr)))
    return {"titel": f"Lohnsteuer-Anmeldung {zeitraum_name(zeitraum)} {jahr}", "werte": w,
            **{k: r[k] for k in ("ok", "code", "text", "meldungen")}}


ERIC_NUR_HINWEISE = 610001003     # „Hinweise während der Plausibilitätsprüfung, Datensatz ist aber plausibel“


def _plausibel(r: dict) -> dict:
    if r["code"] == ERIC_NUR_HINWEISE:
        return {**r, "ok": True, "text": "Plausibel, mit Hinweisen."}
    return r


def _lstb_satz(ctx: Ctx, mid: int, jahr: int):
    from ..elster import lohn as el
    w = lstb_werte(ctx, mid, jahr)
    if not w["steuer_id"] or not w["geburtsdatum"]:
        raise KontorError(f"{w['name']}: Steuer-ID und Geburtsdatum fehlen in den Stammdaten.")
    if w["bis"] >= ctx.now_date().isoformat() and w["bis"] == f"{jahr}-12-31":
        raise KontorError(f"{jahr} läuft noch: die Bescheinigung geht nach dem letzten Monat raus.")
    m = re.fullmatch(r"(.*?)\s+(\d+\s*[a-zA-Z]?(?:\s*-\s*\d+\s*[a-zA-Z]?)?)", w["strasse"].strip())
    strasse, hausnummer = (m.group(1), m.group(2)) if m else (w["strasse"], "")
    merkmale = []
    for p in w["merkmale"]:
        merkmale.append(el.Besteuerung(date.fromisoformat(p["von"]), int(p["steuerklasse"]), p["faktor"],
                                       str(Decimal(p["kinderfreibetraege"]).quantize(Decimal("0.1"))),
                                       p["konfession"], p["konfession_ehegatte"]
                                       if p["konfession_ehegatte"] != p["konfession"] else ""))
    if jahr >= 2026 and w["zeile28_pkv"]:
        # ab 2026 gehören die PKV-Beiträge als Werte aus den ELStAM in die Besteuerungsmerkmale; das bildet Kontor
        # noch nicht ab, also lieber nicht senden als unvollständig
        raise KontorError("Lohnsteuerbescheinigung ab 2026 mit privater Kranken-/Pflegeversicherung: noch nicht abgebildet.")
    gesendet = [g for g in w["gesendet"] if g["ok"] and not g["test"]]
    letzte = json.loads(gesendet[0]["werte"]) if gesendet else {}
    b = el.LStB(jahr, date.fromisoformat(w["von"]), date.fromisoformat(w["bis"]), w["steuer_id"], w["personalnr"],
                w["nachname"], w["vorname"], date.fromisoformat(w["geburtsdatum"]), strasse, hausnummer, w["plz"],
                w["ort"], merkmale, brutto=w["zeile3_brutto"], lst=w["zeile4_lst"], soli=w["zeile5_soli"],
                kist_an=w["zeile6_kist"], kist_ehegatte=w["zeile7_kist_ehegatte"], ag_rv=w["zeile22a_rv_ag"],
                an_rv=w["zeile23a_rv_an"], zuschuss_pkv=w["zeile24b_pkv_zuschuss"],
                an_kv=w["zeile25_kv_an"],
                an_pv=w["zeile26_pv_an"], an_av=w["zeile27_av_an"],
                pkv_beitraege=w["zeile28_pkv"] if jahr < 2026 else 0,
                korrektur_nr=len(gesendet), ref_km_id=letzte.get("km_id", ""))
    return b, w, bool(gesendet)


def lstb_pruefen(ctx: Ctx, mid: int, jahr: int) -> dict:
    """Prüfung der Bescheinigung. ERiC prüft bei der Lohnsteuerbescheinigung nur den Rahmen, nicht die Werte."""
    _require(ctx)
    from ..elster import eric
    from ..elster import lohn as el
    b, w, korrektur = _lstb_satz(ctx, mid, jahr)
    xml = el.lstb(b, _elster_ag(ctx), _hersteller(ctx, False), True, _datenlieferant(ctx), korrektur=korrektur)
    r = _plausibel(eric.pruefen(xml, el.lstb_datenart(jahr)))
    return {"titel": f"Lohnsteuerbescheinigung {jahr} {w['name']}", "werte": w,
            **{k: r[k] for k in ("ok", "code", "text", "meldungen")}}


def _senden(ctx: Ctx, art: str, jahr: int, zeitraum: str, mid: int | None, xml: str, datenart: str, werte: dict,
            pin: str, test: bool, titel: str) -> dict:
    from ..elster import eric
    s = md.get_settings(ctx)
    if not s["elster_zertifikat"]:
        raise KontorError("Pfad zum ELSTER-Zertifikat (.pfx) fehlt (Einstellungen → ELSTER).")
    if not pin:
        raise KontorError("PIN des Zertifikats eingeben.")
    folder = ctx.data / "archiv" / "elster"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime_stamp()
    base = folder / f"{art}-{jahr}-{zeitraum or mid}-{'test' if test else 'echt'}-{stamp}"
    result = eric.senden(xml, datenart, Path(s["elster_zertifikat"]), pin, base.with_suffix(".pdf"))
    base.with_suffix(".xml").write_text(xml, encoding="utf-8")
    for f in (base.with_suffix(".xml"), base.with_suffix(".pdf")):
        if f.is_file():
            os.chmod(f, 0o444)
    with tx(ctx.conn):
        cur = ctx.conn.execute(
            "INSERT INTO lohn_meldungen (art, jahr, zeitraum, mitarbeiter_id, test, ok, code, transferticket, werte, "
            "datensatz, protokoll) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (art, jahr, zeitraum, mid, int(test), int(result["ok"]), result["code"], result["transferticket"],
             json.dumps(werte, default=str), base.with_suffix(".xml").name,
             Path(result["protokoll"]).name if result["protokoll"] else ""))
        audit.log(ctx.conn, ctx.actor, "elster_senden", f"lohn_{art}", cur.lastrowid,
                  {"jahr": jahr, "zeitraum": zeitraum, "mitarbeiter": mid, "test": test, "ok": result["ok"],
                   "code": result["code"], "transferticket": result["transferticket"]})
    return {"titel": titel, "test": test, **{k: result[k] for k in ("ok", "code", "text", "meldungen",
                                                                   "transferticket", "protokoll")}}


def datetime_stamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def lsta_senden(ctx: Ctx, jahr: int, zeitraum: str, pin: str, test: bool) -> dict:
    """Lohnsteuer-Anmeldung an ELSTER (nur der Mensch in der Oberfläche)."""
    require_human(ctx)
    _require(ctx)
    from ..elster import lohn as el
    a, w = lsta_satz(ctx, jahr, zeitraum)
    xml = el.lsta(a, _elster_ag(ctx), _hersteller(ctx, True), test, _datenlieferant(ctx))
    return _senden(ctx, "lsta", jahr, zeitraum, None, xml, el.lsta_datenart(jahr), {"kz": w["kz"]}, pin, test,
                   f"Lohnsteuer-Anmeldung {zeitraum_name(zeitraum)} {jahr}")


def lstb_senden(ctx: Ctx, mid: int, jahr: int, pin: str, test: bool) -> dict:
    """Lohnsteuerbescheinigung an ELSTER (nur der Mensch). Nach einer echten Übermittlung geht jede weitere als
    Korrektur mit Verweis auf die vorige Kennung (KmId)."""
    require_human(ctx)
    _require(ctx)
    from ..elster import lohn as el
    b, w, korrektur = _lstb_satz(ctx, mid, jahr)
    ag = _elster_ag(ctx)
    xml = el.lstb(b, ag, _hersteller(ctx, True), test, _datenlieferant(ctx), korrektur=korrektur)
    werte = {k: v for k, v in w.items() if k.startswith("zeile")} | {"km_id": el.km_id(b, ag, korrektur)}
    return _senden(ctx, "lstb", jahr, "", mid, xml, el.lstb_datenart(jahr), werte, pin, test,
                   f"Lohnsteuerbescheinigung {jahr} {w['name']}")


def meldungen(ctx: Ctx, jahr: int | None = None) -> list[dict]:
    sql, args = ("SELECT g.*, m.vorname || ' ' || m.nachname AS name FROM lohn_meldungen g LEFT JOIN lohn_mitarbeiter m "
                 "ON m.id = g.mitarbeiter_id"), []
    if jahr is not None:
        sql += " WHERE g.jahr = ?"
        args.append(jahr)
    out = []
    for r in ctx.conn.execute(sql + " ORDER BY g.id DESC", args):
        d = dict(r)
        d["bezug"] = zeitraum_name(d["zeitraum"]) if d["art"] == "lsta" else d["name"]
        out.append(d)
    return out


def lohnjournal_csv(ctx: Ctx, jahr: int) -> str:
    """Lohnjournal als CSV (Semikolon, Beträge mit Komma), eine Zeile je festgeschriebener Abrechnung."""
    cols = ("brutto", "steuerbrutto", "steuerbrutto_einmal", "lst", "soli", "kist", "kist_ehegatte", "kv_an", "rv_an",
            "av_an", "pv_an", "kv_ag", "rv_ag", "av_ag", "pv_ag", "u1", "u2", "insolvenz", "kv_pauschal", "rv_pauschal",
            "pauschsteuer", "pauschal_lst", "pauschal_soli", "pauschal_kist", "pkv_zuschuss", "pv_zuschuss",
            "sachbezug", "steuerfrei", "abzug", "netto", "auszahlung", "ag_kosten")
    lines = [";".join(("monat", "personalnr", "name", *cols, "sv_tage"))]
    for r in lohnjournal(ctx, jahr)["zeilen"]:
        s = r["summen"]
        lines.append(";".join((r["monat"], r["personalnr"], r["name"],
                               *(f"{s[c] / 100:.2f}".replace(".", ",") for c in cols), str(s["sv_tage"]))))
    return "\n".join(lines) + "\n"


def lohnzettel(ctx: Ctx, mid: int, jahr: int) -> bytes:
    """Ausdruck der Lohnsteuerbescheinigung für den Arbeitnehmer, mit Datum und Kennung der letzten echten
    Übermittlung, falls es eine gibt."""
    from ..render.lohn import lohnsteuerbescheinigung
    b = lstb_werte(ctx, mid, jahr)
    echt = [g for g in b["gesendet"] if g["ok"] and not g["test"]]
    km = json.loads(echt[0]["werte"]).get("km_id", "") if echt else ""
    return lohnsteuerbescheinigung(b, arbeitgeber(ctx), echt[0] if echt else None, km)


# -- ELStAM ---------------------------------------------------------------------------------------------------------
# Was das Bundeszentralamt über ELSTER liefert, landet unverändert in lohn_elstam. Übernehmen schreibt eine neue
# Fassung der Merkmale ab dem Gültigkeitsmonat; bis dahin zeigt der Abgleich jede Abweichung als Auffälligkeit.
ELSTAM_FELDER = ("steuerklasse", "faktor", "kinderfreibetraege", "konfession", "konfession_ehegatte", "freibetrag",
                 "hinzurechnung")


def _elstam_merkmale(e: dict) -> dict:
    out = {k: e[k] for k in ELSTAM_FELDER if k in e and e[k] is not None}
    if "kinderfreibetraege" in out:
        k = Decimal(str(out["kinderfreibetraege"]))
        out["kinderfreibetraege"] = str(k.normalize()) if k else "0"
    if out.get("faktor"):
        out["faktor"] = str(Decimal(str(out["faktor"])).quantize(Decimal("0.001")))
    return out


def elstam_speichern(ctx: Ctx, vorgang_id: int | None, eintraege: list[dict]) -> int:
    """Empfangene Einträge (aus elster.elstam.aenderungsliste_lesen) ablegen; doppelt Geliefertes zählt einmal."""
    neu = 0
    with tx(ctx.conn):
        for e in eintraege:
            idnr = str(e.get("idnr", ""))
            row = ctx.conn.execute("SELECT id FROM lohn_mitarbeiter WHERE steuer_id = ? AND steuer_id <> ''",
                                   (idnr,)).fetchone()
            ab = e["gueltig_ab"].isoformat() if hasattr(e.get("gueltig_ab"), "isoformat") else str(e.get("gueltig_ab") or "")
            daten = json.dumps({k: v for k, v in e.items() if k != "gueltig_ab"}, default=str, sort_keys=True)
            schluessel = f"{idnr}|{e.get('art', '')}|{ab}|{daten}"
            cur = ctx.conn.execute("INSERT OR IGNORE INTO lohn_elstam (vorgang_id, mitarbeiter_id, idnr, art, gueltig_ab, "
                                   "daten, schluessel) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   (vorgang_id, row["id"] if row else None, idnr, str(e.get("art", "")), ab, daten,
                                    schluessel))
            neu += cur.rowcount
        audit.log(ctx.conn, ctx.actor, "elstam_empfangen", "lohn_elstam", vorgang_id, {"eintraege": len(eintraege),
                                                                                       "neu": neu})
    return neu


def elstam(ctx: Ctx, offen: bool = False) -> list[dict]:
    sql = ("SELECT e.*, m.vorname || ' ' || m.nachname AS name FROM lohn_elstam e LEFT JOIN lohn_mitarbeiter m "
           "ON m.id = e.mitarbeiter_id")
    if offen:
        sql += " WHERE e.uebernommen_am IS NULL AND e.verworfen_grund = ''"
    out = []
    for r in ctx.conn.execute(sql + " ORDER BY e.gueltig_ab DESC, e.id DESC"):
        d = {**dict(r), "daten": json.loads(r["daten"])}
        d["merkmale"] = _elstam_merkmale(d["daten"])
        d["abweichung"] = _elstam_abweichung(ctx, d)
        out.append(d)
    return out


def _elstam_abweichung(ctx: Ctx, e: dict) -> dict:
    """Felder, in denen die empfangenen Merkmale von denen in Kontor für denselben Monat abweichen."""
    if not e["mitarbeiter_id"] or not e["gueltig_ab"] or not e["merkmale"]:
        return {}
    try:
        mk = merkmale_im(ctx, e["mitarbeiter_id"], e["gueltig_ab"][:7])
    except KontorError:
        return dict(e["merkmale"])
    return {k: v for k, v in e["merkmale"].items() if str(mk.get(k, "")) != str(v)}


def elstam_uebernehmen(ctx: Ctx, eid: int) -> dict:
    """Empfangene Merkmale ab ihrem Gültigkeitsmonat in den Mitarbeiter übernehmen (frühestens ab Eintritt)."""
    _require(ctx)
    e = next((x for x in elstam(ctx) if x["id"] == eid), None)
    if not e:
        raise KontorError(f"ELStAM-Eintrag {eid} gibt es nicht.")
    if not e["mitarbeiter_id"]:
        raise KontorError(f"Zur Identifikationsnummer {e['idnr']} gibt es keinen Mitarbeiter (Steuer-ID eintragen).")
    if not e["merkmale"] or not e["gueltig_ab"]:
        raise KontorError("Der Eintrag enthält keine Merkmale zum Übernehmen.")
    m = _get_row(ctx, e["mitarbeiter_id"])
    ab = max(e["gueltig_ab"][:7], m["eintritt"][:7])
    neu = merkmale_setzen(ctx, e["mitarbeiter_id"], ab, e["merkmale"])
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE lohn_elstam SET uebernommen_am = strftime('%Y-%m-%dT%H:%M:%S','now','localtime') "
                         "WHERE id = ?", (eid,))
        audit.log(ctx.conn, ctx.actor, "elstam_uebernehmen", "lohn_elstam", eid, {"ab": ab, **e["merkmale"]})
    return neu


def elstam_verwerfen(ctx: Ctx, eid: int, grund: str) -> None:
    _require(ctx)
    if not (grund or "").strip():
        raise KontorError("Grund angeben.")
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE lohn_elstam SET verworfen_grund = ? WHERE id = ?", (grund.strip(), eid))
        audit.log(ctx.conn, ctx.actor, "elstam_verwerfen", "lohn_elstam", eid, {"grund": grund.strip()})


ELSTAM_ARTEN = {"anmelden": "Anmeldung", "wechsel": "Wechsel des Datenübermittlers", "abmelden": "Abmeldung"}


def _elstam_an(ctx: Ctx, art: str, mids: list[int], referenzdatum: str | None) -> list:
    from ..elster import elstam as es
    if art not in ELSTAM_ARTEN:
        raise KontorError(f"ELStAM-Vorgang {art!r} (erlaubt: {', '.join(ELSTAM_ARTEN)}).")
    if not mids:
        raise KontorError("Mindestens einen Mitarbeiter wählen.")
    out = []
    for mid in mids:
        m = _get_row(ctx, mid)
        name = f"{m['vorname']} {m['nachname']}"
        if not m["steuer_id"] or not m["geburtsdatum"]:
            raise KontorError(f"{name}: Steuer-ID und Geburtsdatum fehlen in den Stammdaten.")
        ref = date.fromisoformat(_tag(referenzdatum, "Referenzdatum")) if referenzdatum else \
            date.fromisoformat(m["eintritt"])
        if art == "abmelden":
            if not m["austritt"]:
                raise KontorError(f"{name}: erst den Austritt eintragen.")
            out.append(es.ElstamAN(m["steuer_id"], date.fromisoformat(m["geburtsdatum"]), referenzdatum=ref,
                                   ende=date.fromisoformat(m["austritt"])))
        else:
            out.append(es.ElstamAN(m["steuer_id"], date.fromisoformat(m["geburtsdatum"]),
                                   beginn=date.fromisoformat(m["eintritt"]), referenzdatum=ref))
    return out


def elstam_satz(ctx: Ctx, art: str, mids: list[int], referenzdatum: str | None, test: bool, senden: bool = False) -> str:
    """ELStAM-Anfrage als Datensatz: Anmeldung (ab Referenzdatum, sonst ab Eintritt), Wechsel des
    Datenübermittlers (ab Referenzdatum, Pflicht) oder Abmeldung (zum Austritt)."""
    _require(ctx)
    from ..elster import elstam as es
    an = _elstam_an(ctx, art, mids, referenzdatum)
    ag, hid, dl = _elster_ag(ctx), _hersteller(ctx, senden), _datenlieferant(ctx)
    if art == "anmelden":
        return es.anmelden(ag, an, hid, test, dl)
    if art == "abmelden":
        return es.abmelden(ag, an, hid, test, dl)
    if not referenzdatum:
        raise KontorError("Für den Wechsel des Datenübermittlers das Referenzdatum angeben (ab wann Kontor übernimmt).")
    return es.datenuebermittler_wechsel(ag, an, date.fromisoformat(_tag(referenzdatum, "Referenzdatum")), hid, test, dl)


def _elstam_datenart(art: str) -> str:
    from ..elster import elstam as es
    return es.elstam_datenart({"wechsel": "ummelden"}.get(art, art))


def elstam_pruefen(ctx: Ctx, art: str, mids: list[int], referenzdatum: str | None = None) -> dict:
    """Lokale Prüfung der ELStAM-Anfrage durch ERiC, ohne Versand."""
    from ..elster import eric
    xml = elstam_satz(ctx, art, mids, referenzdatum, test=True)
    r = _plausibel(eric.pruefen(xml, _elstam_datenart(art)))
    return {"titel": f"ELStAM {ELSTAM_ARTEN[art]}", **{k: r[k] for k in ("ok", "code", "text", "meldungen")}}


def _elstam_vorgang(ctx: Ctx, art: str, mids: list[int], test: bool, result: dict, datensatz: str) -> int:
    with tx(ctx.conn):
        cur = ctx.conn.execute("INSERT INTO lohn_elstam_vorgaenge (art, mitarbeiter_ids, test, ok, code, transferticket, "
                               "text, datensatz) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (art, json.dumps(mids), int(test), int(bool(result.get("ok"))), result.get("code", 0),
                                result.get("transferticket", ""), result.get("text", ""), datensatz))
        audit.log(ctx.conn, ctx.actor, "elstam_senden", "lohn_elstam_vorgaenge", cur.lastrowid,
                  {"art": art, "mitarbeiter": mids, "test": test, "ok": bool(result.get("ok")),
                   "code": result.get("code", 0), "transferticket": result.get("transferticket", "")})
    return cur.lastrowid


def elstam_senden(ctx: Ctx, art: str, mids: list[int], pin: str, test: bool, referenzdatum: str | None = None) -> dict:
    """ELStAM-Anfrage an ELSTER (nur der Mensch). Die Antwort kommt nicht sofort, sondern als Liste im Postfach:
    danach `elstam_abholen`."""
    require_human(ctx)
    from ..elster import eric
    s = md.get_settings(ctx)
    if not s["elster_zertifikat"]:
        raise KontorError("Pfad zum ELSTER-Zertifikat (.pfx) fehlt (Einstellungen → ELSTER).")
    if not pin:
        raise KontorError("PIN des Zertifikats eingeben.")
    xml = elstam_satz(ctx, art, mids, referenzdatum, test, senden=True)
    folder = ctx.data / "archiv" / "elster"
    folder.mkdir(parents=True, exist_ok=True)
    base = folder / f"elstam-{art}-{'test' if test else 'echt'}-{datetime_stamp()}"
    result = eric.senden(xml, _elstam_datenart(art), Path(s["elster_zertifikat"]), pin, base.with_suffix(".pdf"))
    base.with_suffix(".xml").write_text(xml, encoding="utf-8")
    for f in (base.with_suffix(".xml"), base.with_suffix(".pdf")):
        if f.is_file():
            os.chmod(f, 0o444)
    _elstam_vorgang(ctx, art, mids, test, result, base.with_suffix(".xml").name)
    return {"titel": f"ELStAM {ELSTAM_ARTEN[art]}", "test": test,
            **{k: result[k] for k in ("ok", "code", "text", "meldungen", "transferticket", "protokoll")}}


def elstam_abholen(ctx: Ctx, pin: str, test: bool = False) -> dict:
    """Bereitliegende ELStAM-Listen aus dem Postfach holen (nur der Mensch): abholen, entschlüsseln, unverändert
    schreibgeschützt ins Archiv legen, lesen, ablegen, erst dann bestätigen. Scheitert das Ablegen, bleibt alles im
    Postfach."""
    require_human(ctx)
    _require(ctx)
    from ..elster import elstam as es
    from ..elster import eric
    s = md.get_settings(ctx)
    if not s["elster_zertifikat"]:
        raise KontorError("Pfad zum ELSTER-Zertifikat (.pfx) fehlt (Einstellungen → ELSTER).")
    if not pin:
        raise KontorError("PIN des Zertifikats eingeben.")
    ag, hid, dl = _elster_ag(ctx), _hersteller(ctx, True), _datenlieferant(ctx)
    folder = ctx.data / "archiv" / "elster" / "elstam"
    folder.mkdir(parents=True, exist_ok=True)
    stamp, neu = datetime_stamp(), {"eintraege": 0, "neu": 0}

    def ablegen(pakete: list[dict]) -> None:
        eintraege = []
        for i, p in enumerate(pakete):
            f = folder / f"{stamp}-{_slug(p['referenz_id'] or str(i))}.xml"
            f.write_text(p["xml"], encoding="utf-8")
            os.chmod(f, 0o444)
            eintraege += es.aenderungsliste_lesen(p["xml"])
        vid = _elstam_vorgang(ctx, "abholen", [], test, {"ok": True, "text": f"{len(pakete)} Listen"}, stamp)
        neu["eintraege"], neu["neu"] = len(eintraege), elstam_speichern(ctx, vid, eintraege)

    anfrage = es.postfach_anfrage(hid, test, dl, ag_steuernummer=ag.steuernummer13)
    r = eric.abholen(anfrage, _elstam_datenart("anfrage"), Path(s["elster_zertifikat"]), pin, hid,
                     bestaetigung=lambda ids: es.postfach_bestaetigung(ids, hid, test, dl), ablegen=ablegen)
    a = r["anfrage"] or {}
    fehler = [f"{p['referenz_id']}: {p['fehler']}" for p in r["pakete"] if "fehler" in p]
    return {"titel": "ELStAM aus dem Postfach", "test": test, "ok": r["ok"] and bool(a.get("ok")),
            "code": a.get("code", 0), "text": a.get("text", ""), "transferticket": a.get("transferticket", ""),
            "meldungen": a.get("meldungen", []) + fehler + [f"{len(r['pakete'])} Listen, {neu['eintraege']} Einträge, "
                                                             f"{neu['neu']} neu, {len(r['bestaetigt'])} bestätigt"]}


def elstam_vorgaenge(ctx: Ctx) -> list[dict]:
    return [dict(r) for r in ctx.conn.execute("SELECT * FROM lohn_elstam_vorgaenge ORDER BY id DESC LIMIT 50")]


def lsta_extern(ctx: Ctx, jahr: int, zeitraum: str, notiz: str) -> dict:
    """Vermerken, dass die Anmeldung des Zeitraums ein anderes Programm schon gesendet hat (Wechsel im Jahr). Kontor
    sendet sie dann nicht noch einmal, vergleicht aber weiter: weichen die eigenen Werte ab, ist das eine Auffälligkeit."""
    _require(ctx)
    if not (notiz or "").strip():
        raise KontorError("Notiz angeben (welches Programm, wann gesendet).")
    # Werte, wie Kontor sie für den Zeitraum rechnet (offene Monate aus den Entwürfen): stimmen sie nach dem
    # Festschreiben nicht mehr, meldet der Abgleich die Abweichung
    w = lsta_werte(ctx, jahr, zeitraum, entwuerfe=True)
    with tx(ctx.conn):
        cur = ctx.conn.execute("INSERT INTO lohn_meldungen (art, jahr, zeitraum, test, ok, transferticket, werte) "
                               "VALUES ('lsta', ?, ?, 0, 1, ?, ?)",
                               (jahr, zeitraum, f"extern: {notiz.strip()}", json.dumps({"kz": w["kz"], "extern": True})))
        audit.log(ctx.conn, ctx.actor, "lsta_extern", "lohn_meldungen", cur.lastrowid,
                  {"jahr": jahr, "zeitraum": zeitraum, "notiz": notiz.strip()})
    return {"jahr": jahr, "zeitraum": zeitraum, "kz": w["kz"]}
