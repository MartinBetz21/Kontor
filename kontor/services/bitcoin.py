"""Bitcoin-Nebenbuch (Spec 18.14, Profilschalter `bitcoin`): Wallets, Bewegungen als Entwurf, Zuordnen und Bestätigen,
Wallet-Saldo, Tageskurse, Bewertung je Jahr, Zuordnungsvorschläge, Auffälligkeiten.

Bewertung: periodischer gewogener Durchschnitt über alle Wallets, zwei Größen je Bestand (Anschaffungskosten und
Buchwert). Abgänge in der Reihenfolge Datum, Bewegung; die Netzwerkgebühr in sats ist ein eigener Abgang direkt nach
ihrer Bewegung. Kumulative Rundung: Der i-te Abgang trägt round(Pool × kumuliert_i / Pool_sats) −
round(Pool × kumuliert_{i−1} / Pool_sats), so stimmen die Summen auf den Cent. Stichtag: Bestand ×
min(Durchschnitt der Anschaffungskosten, Schlusskurs 31.12.), Abschreibung oder Zuschreibung höchstens bis zu den
Anschaffungskosten; schwebende Verkäufe zusätzlich höchstens zum Vertragspreis. Die Buchungen dazu leitet
autojournal ab."""
from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

from ..context import Ctx
from ..core.errors import KontorError
from ..core.money import fmt_eur, round_cents
from ..importers import bitcoin as imp
from ..store import audit
from ..store.db import tx
from . import documents as docs
from . import masterdata as md
from . import payments as pay

ZUGANG = ("kauf", "kundenzahlung")
ABGANG = ("verkauf", "zahlung", "verlust")
SENDET = ("verkauf", "zahlung", "ausgang", "verlust", "umbuchung")      # hier kann eine Netzwerkgebühr anfallen
KONTEN = ("bestand", "verrechnung", "erloes", "buchwert", "gebuehr", "abschreibung", "zuschreibung", "verlust")
LABEL = {"kauf": "Kauf", "verkauf": "Verkauf", "kundenzahlung": "Kundenzahlung", "zahlung": "Zahlung Eingangsrechnung",
         "umbuchung": "Umbuchung", "eingang": "Eingang", "ausgang": "Ausgang", "verlust": "Verlust"}
NUMMER = re.compile(r"\b[A-Z]{2}-\d{4}-\d{3,6}\b")
NULL = {"sats": 0, "ak": 0, "buchwert": 0}
STEUER = ("19", "7", "0", "rc_eu", "rc_drittland")      # wie bei der Bankzuordnung (18.7a)


def _require(ctx: Ctx) -> None:
    if not md.has(ctx, "bitcoin"):
        raise KontorError("Bitcoin ist für diesen Mandanten aus (Profilschalter bitcoin).")


def konten(ctx: Ctx) -> dict[str, str]:
    s = md.get_settings(ctx)
    return {k: s[f"btc_konto_{k}"] for k in KONTEN}


def _tag(value: str, feld: str = "Datum") -> str:
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        raise KontorError(f"{feld} als JJJJ-MM-TT ({value!r}).") from None


def _anteil(betrag: int, teil: int, ganz: int) -> int:
    """round(betrag × teil / ganz), kaufmännisch und exakt."""
    f = Fraction(betrag * teil, ganz)
    n = math.floor(abs(f) + Fraction(1, 2))
    return n if f >= 0 else -n


def wert(sats: int, kurs: Decimal) -> int:
    """sats zum Kurs (EUR je BTC) in Cent."""
    return round_cents(Decimal(sats) * kurs / Decimal(1_000_000))


def _locked(ctx: Ctx, day: str) -> bool:
    return ctx.conn.execute("SELECT 1 FROM journal_locks WHERE year = ?", (int(day[:4]),)).fetchone() is not None


# -- Wallets --------------------------------------------------------------------------------------------------------
def wallet(ctx: Ctx, name: str, art: str = "wallet", aktiv: bool = True) -> int:
    """Wallet oder Börsenkonto anlegen bzw. ändern. Kontor kennt nur den Namen, keine Adressen."""
    _require(ctx)
    name = (name or "").strip()
    if not name or art not in ("wallet", "boerse"):
        raise KontorError("Name und Art (wallet oder boerse) angeben.")
    with tx(ctx.conn):
        ctx.conn.execute("INSERT INTO btc_wallets (name, art, aktiv) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                         "art = excluded.art, aktiv = excluded.aktiv", (name, art, int(bool(aktiv))))
        wid = ctx.conn.execute("SELECT id FROM btc_wallets WHERE name = ?", (name,)).fetchone()[0]
        audit.log(ctx.conn, ctx.actor, "btc_wallet", "btc_wallets", wid, {"name": name, "art": art, "aktiv": bool(aktiv)})
    return wid


def wallets(ctx: Ctx) -> list[dict]:
    return [dict(r) for r in ctx.conn.execute("SELECT * FROM btc_wallets ORDER BY aktiv DESC, name")]


def _wallet_id(ctx: Ctx, w: int | str) -> int:
    row = ctx.conn.execute("SELECT id FROM btc_wallets WHERE id = ? OR name = ?",
                           (w if isinstance(w, int) else -1, str(w).strip())).fetchone()
    if not row:
        raise KontorError(f"Wallet {w!r} gibt es nicht (erst `kontor btc wallet --name …`).")
    return row[0]


# -- Bewegungen -----------------------------------------------------------------------------------------------------
def _felder(ctx: Ctx, data: dict) -> dict:
    out: dict = {}
    if data.get("wallet") not in (None, ""):
        out["wallet_id"] = _wallet_id(ctx, data["wallet"])
    if data.get("ziel") not in (None, ""):
        out["ziel_wallet_id"] = _wallet_id(ctx, data["ziel"])
    if data.get("art") not in (None, ""):
        if data["art"] not in imp.ARTEN:
            raise KontorError(f"Art {data['art']!r} (erlaubt: {', '.join(imp.ARTEN)}).")
        out["art"] = data["art"]
    for k in ("sats", "gebuehr_sats"):
        if data.get(k) not in (None, ""):
            v = int(data[k])
            if v < 0 or (k == "sats" and v == 0):
                raise KontorError(f"{k} muss eine ganze Zahl {'> 0' if k == 'sats' else '≥ 0'} sein.")
            out[k] = v
    for k in ("eur", "gebuehr_eur", "ak"):
        if data.get(k) not in (None, ""):
            out[k] = md.to_cents(data[k])
            if out[k] < 0:
                raise KontorError(f"{k}: Beträge positiv angeben.")
    for k in ("datum", "vertragsdatum"):
        if data.get(k) not in (None, ""):
            out[k] = _tag(data[k], k)
    nummer = data.get("rechnung")
    if nummer not in (None, ""):
        row = ctx.conn.execute("SELECT id FROM documents WHERE number = ? OR (id = ? AND number IS NOT NULL)",
                               (str(nummer), nummer if isinstance(nummer, int) else -1)).fetchone()
        if not row:
            raise KontorError(f"Rechnung {nummer} gibt es nicht.")
        out["document_id"] = row[0]
    if data.get("konto") not in (None, ""):
        if not re.fullmatch(r"\d{3,5}", str(data["konto"]).strip()):
            raise KontorError(f"Konto {data['konto']!r} ist keine Kontonummer.")
        out["konto"] = str(data["konto"]).strip()
    if data.get("steuer") not in (None, ""):
        if data["steuer"] not in STEUER:
            raise KontorError(f"Steuerschlüssel {data['steuer']!r} (erlaubt: {', '.join(STEUER)}).")
        out["steuer"] = data["steuer"]
    if data.get("beleg") not in (None, ""):
        if not ctx.conn.execute("SELECT 1 FROM receipts WHERE id = ?", (int(data["beleg"]),)).fetchone():
            raise KontorError(f"Beleg {data['beleg']} gibt es nicht.")
        out["receipt_id"] = int(data["beleg"])
    if "notiz" in data and data["notiz"] is not None:
        out["notiz"] = str(data["notiz"]).strip()
    return out


def _vorgaben(ctx: Ctx, m: dict) -> dict:
    """Verkauf: Vertragsdatum = Übergangsdatum, wenn keins genannt ist. Kundenzahlung: Nennwert = offener Rest."""
    if m.get("art") == "verkauf" and not m.get("vertragsdatum"):
        m["vertragsdatum"] = m.get("datum")
    if m.get("art") == "kundenzahlung" and m.get("document_id") and m.get("eur") is None:
        m["eur"] = docs.get_document(ctx, m["document_id"])["open"]
    return m


COLUMNS = ("wallet_id", "ziel_wallet_id", "art", "sats", "gebuehr_sats", "eur", "gebuehr_eur", "ak", "vertragsdatum",
           "datum", "document_id", "receipt_id", "konto", "steuer", "notiz")


def erfassen(ctx: Ctx, data: dict) -> int:
    """Bewegung von Hand erfassen (wallet, art, datum, sats, …). Sie ist ein Entwurf, bis sie bestätigt wird."""
    _require(ctx)
    m = _vorgaben(ctx, {"gebuehr_sats": 0, "gebuehr_eur": 0, "konto": "", "steuer": "", "notiz": "", **_felder(ctx, data)})
    if not all(m.get(k) for k in ("wallet_id", "art", "sats", "datum")):
        raise KontorError("wallet, art, datum und sats angeben.")
    with tx(ctx.conn):
        cur = ctx.conn.execute(f"INSERT INTO btc_movements ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                               [m.get(c) for c in COLUMNS])
        audit.log(ctx.conn, ctx.actor, "btc_erfassen", "btc_movements", cur.lastrowid,
                  {"art": m["art"], "datum": m["datum"], "sats": m["sats"]})
    return cur.lastrowid


def get(ctx: Ctx, mid: int) -> dict:
    row = ctx.conn.execute(
        "SELECT m.*, w.name AS wallet, z.name AS ziel, d.number AS nummer FROM btc_movements m "
        "JOIN btc_wallets w ON w.id = m.wallet_id LEFT JOIN btc_wallets z ON z.id = m.ziel_wallet_id "
        "LEFT JOIN documents d ON d.id = m.document_id WHERE m.id = ?", (mid,)).fetchone()
    if not row:
        raise KontorError(f"Bitcoin-Bewegung {mid} gibt es nicht.")
    return dict(row)


def bewegungen(ctx: Ctx, jahr: int | None = None, status: str | None = None) -> list[dict]:
    sql = ("SELECT m.*, w.name AS wallet, z.name AS ziel, d.number AS nummer FROM btc_movements m "
           "JOIN btc_wallets w ON w.id = m.wallet_id LEFT JOIN btc_wallets z ON z.id = m.ziel_wallet_id "
           "LEFT JOIN documents d ON d.id = m.document_id WHERE 1=1")
    args: list = []
    if jahr:
        sql += " AND substr(m.datum, 1, 4) = ?"
        args.append(str(jahr))
    if status:
        sql += " AND m.status = ?"
        args.append(status)
    return [dict(r) for r in ctx.conn.execute(sql + " ORDER BY m.datum, m.id", args)]


def zuordnen(ctx: Ctx, mid: int, **data) -> None:
    """Einen Entwurf klassifizieren und ergänzen: art, rechnung, beleg, konto, steuer, ziel, eur, gebuehr_eur,
    vertragsdatum, ak, notiz."""
    _require(ctx)
    m = get(ctx, mid)
    if m["status"] != "entwurf":
        raise KontorError(f"Bewegung {mid} ist {m['status']}; nur Entwürfe werden zugeordnet.")
    f = _felder(ctx, data)
    merged = _vorgaben(ctx, {**{c: m[c] for c in COLUMNS}, **f})
    with tx(ctx.conn):
        ctx.conn.execute(f"UPDATE btc_movements SET {', '.join(c + ' = ?' for c in COLUMNS)} WHERE id = ?",
                         [merged[c] for c in COLUMNS] + [mid])
        audit.log(ctx.conn, ctx.actor, "btc_zuordnen", "btc_movements", mid,
                  {k: v for k, v in merged.items() if k in f or k in ("eur", "vertragsdatum")})


def verwerfen(ctx: Ctx, mid: int, grund: str) -> None:
    """Entwurf verwerfen (z. B. Gegenstück einer Umbuchung). Er zählt nirgends mehr; ein erneuter Import legt ihn nicht an."""
    _require(ctx)
    if not (grund or "").strip():
        raise KontorError("Begründung fehlt.")
    m = get(ctx, mid)
    if m["status"] != "entwurf":
        raise KontorError(f"Bewegung {mid} ist kein Entwurf.")
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE btc_movements SET status = 'verworfen', notiz = trim(notiz || ' · verworfen: ' || ?) "
                         "WHERE id = ?", (grund.strip(), mid))
        audit.log(ctx.conn, ctx.actor, "btc_verwerfen", "btc_movements", mid, {"grund": grund})


def _unvollstaendig(m: dict) -> str:
    art = m["art"]
    if art in ("eingang", "ausgang"):
        return "Erst zuordnen: Kauf, Verkauf, Kundenzahlung, Zahlung, Umbuchung oder Verlust."
    if art in ZUGANG and m["gebuehr_sats"]:
        return "Eine Netzwerkgebühr gibt es nur bei ausgehenden Vorgängen."
    if art in ("kauf", "verkauf", "kundenzahlung", "zahlung") and not m["eur"]:
        return f"{LABEL[art]}: EUR-Betrag fehlt."
    if art == "verkauf" and not (m["vertragsdatum"] and m["vertragsdatum"] <= m["datum"]):
        return "Verkauf: Vertragsdatum fehlt oder liegt nach dem Übergang."
    if art == "kundenzahlung" and not m["document_id"]:
        return "Kundenzahlung: Rechnung fehlt."
    if art == "zahlung" and not (m["konto"] and m["steuer"]):
        return "Zahlung: Konto und Steuerschlüssel angeben (wie bei einem Beleg über die Bank)."
    if art == "umbuchung" and not m["ziel_wallet_id"]:
        return "Umbuchung: Ziel-Wallet fehlt."
    if art == "verlust" and not m["notiz"]:
        return "Verlust: Begründung (Notiz) fehlt."
    return ""


def bestaetigen(ctx: Ctx, mid: int) -> None:
    """Erst eine bestätigte Bewegung bucht. Eine Kundenzahlung erzeugt die Zahlung auf die Rechnung am Übergangsdatum."""
    _require(ctx)
    m = get(ctx, mid)
    if m["status"] != "entwurf":
        raise KontorError(f"Bewegung {mid} ist {m['status']}, kein Entwurf.")
    if _locked(ctx, m["datum"]):
        raise KontorError(f"Das Jahr {m['datum'][:4]} ist gesperrt (Abschluss aufgestellt).")
    problem = _unvollstaendig(m)
    if problem:
        raise KontorError(problem)
    with tx(ctx.conn):
        pid = None
        if m["art"] == "kundenzahlung":
            pid = pay.record_payment(ctx, m["document_id"], m["datum"], m["eur"],
                                     f"Bitcoin {m['sats']} sats (Bewegung {mid})")
        ctx.conn.execute("UPDATE btc_movements SET status = 'bestaetigt', payment_id = ? WHERE id = ?", (pid, mid))
        audit.log(ctx.conn, ctx.actor, "btc_bestaetigen", "btc_movements", mid,
                  {"art": m["art"], "datum": m["datum"], "sats": m["sats"], "eur": m["eur"], "zahlung": pid})


def entbestaetigen(ctx: Ctx, mid: int, grund: str) -> None:
    """Zurück zum Entwurf; die Zahlung einer Kundenzahlung verschwindet mit."""
    _require(ctx)
    if not (grund or "").strip():
        raise KontorError("Begründung fehlt.")
    m = get(ctx, mid)
    if m["status"] != "bestaetigt":
        raise KontorError(f"Bewegung {mid} ist nicht bestätigt.")
    if _locked(ctx, m["datum"]):
        raise KontorError(f"Das Jahr {m['datum'][:4]} ist gesperrt (Abschluss aufgestellt).")
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE btc_movements SET status = 'entwurf' WHERE id = ?", (mid,))
        if m["payment_id"]:
            pay.delete_payment(ctx, m["payment_id"], f"Bitcoin-Bewegung {mid} zurück zum Entwurf: {grund}")
        audit.log(ctx.conn, ctx.actor, "btc_entbestaetigen", "btc_movements", mid, {"grund": grund})


# -- Wallet-Saldo ---------------------------------------------------------------------------------------------------
def wallet_bestand(ctx: Ctx, w: int | str, bis: str, nur_bestaetigt: bool = False) -> int:
    """sats in der Wallet bis einschließlich `bis`: Anfangsbestand des letzten Vortragsjahres plus Bewegungen danach.
    Ohne `nur_bestaetigt` zählen auch Entwürfe mit (es sind echte Vorgänge, nur noch nicht zugeordnet)."""
    wid = _wallet_id(ctx, w)
    v = ctx.conn.execute("SELECT MAX(jahr) FROM btc_vortrag WHERE jahr <= ?", (int(bis[:4]),)).fetchone()[0]
    total = 0
    if v:
        row = ctx.conn.execute("SELECT sats FROM btc_vortrag WHERE jahr = ? AND wallet_id = ?", (v, wid)).fetchone()
        total = row[0] if row else 0
    status = ("bestaetigt",) if nur_bestaetigt else ("entwurf", "bestaetigt")
    for m in ctx.conn.execute(
            f"SELECT * FROM btc_movements WHERE status IN ({','.join('?' * len(status))}) AND datum <= ? AND datum >= ? "
            "AND (wallet_id = ? OR ziel_wallet_id = ?)", (*status, bis, f"{v or 0:04d}-01-01", wid, wid)):
        if m["wallet_id"] == wid:
            total += m["sats"] if m["art"] in ("kauf", "kundenzahlung", "eingang") else -(m["sats"] + m["gebuehr_sats"])
        if m["art"] == "umbuchung" and m["ziel_wallet_id"] == wid:
            total += m["sats"]
    return total


def saldo_eintragen(ctx: Ctx, w: int | str, datum: str, sats: int, notiz: str = "") -> None:
    _require(ctx)
    wid, day = _wallet_id(ctx, w), _tag(datum)
    with tx(ctx.conn):
        ctx.conn.execute("INSERT INTO btc_wallet_salden (wallet_id, datum, sats, notiz) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(wallet_id, datum) DO UPDATE SET sats = excluded.sats, notiz = excluded.notiz",
                         (wid, day, int(sats), notiz))
        audit.log(ctx.conn, ctx.actor, "btc_saldo", "btc_wallet_salden", wid, {"datum": day, "sats": int(sats)})


def salden(ctx: Ctx) -> list[dict]:
    out = []
    for r in ctx.conn.execute("SELECT s.*, w.name AS wallet FROM btc_wallet_salden s JOIN btc_wallets w "
                              "ON w.id = s.wallet_id ORDER BY s.datum DESC, w.name"):
        kontor = wallet_bestand(ctx, r["wallet_id"], r["datum"])
        out.append({**dict(r), "kontor": kontor, "differenz": r["sats"] - kontor})
    return out


# -- Kurse ----------------------------------------------------------------------------------------------------------
def _kurs_wert(raw) -> Decimal:
    s = str(raw).strip().replace("€", "").strip()
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", s):          # 12.345,67
        s = s.replace(".", "").replace(",", ".")
    v = imp._dezimal(s, "Kurs")
    if v <= 0:
        raise KontorError("Kurs in EUR je BTC, größer als 0.")
    return v


def kurs_speichern(ctx: Ctx, datum: str, eur, quelle: str = "manuell") -> None:
    """Tageskurs von Hand (EUR je BTC). Ein Kurs der Kraken-Abfrage hat Vorrang vor einem von Hand."""
    _require(ctx)
    day, value = _tag(datum), _kurs_wert(eur)
    with tx(ctx.conn):
        ctx.conn.execute("INSERT INTO btc_kurse (datum, quelle, eur, abgerufen_am) VALUES (?, ?, ?, ?) ON CONFLICT(datum, "
                         "quelle) DO UPDATE SET eur = excluded.eur, abgerufen_am = excluded.abgerufen_am",
                         (day, quelle, str(value), datetime.now().isoformat(timespec="seconds")))
        audit.log(ctx.conn, ctx.actor, "btc_kurs", "btc_kurse", None, {"datum": day, "eur": str(value), "quelle": quelle})


# Rangfolge je Tag: Kraken ist maßgeblich, Bitstamp füllt nur Tage ohne Kraken-Kurs (Historie vor dem Abruffenster,
# Ausfall), ein Kurs von Hand gilt zuletzt. Die Quelle steht an jedem Kurs (BMF 06.03.2025 Rn. 103).
RANG = "CASE quelle WHEN 'kraken' THEN 0 WHEN 'bitstamp' THEN 1 ELSE 2 END"
PARSER = {"kraken": imp.parse_kraken_ohlc, "bitstamp": imp.parse_bitstamp_ohlc}


def abruf_speichern(ctx: Ctx, quelle: str, text: str, heute: date | None = None) -> int:
    """Antwort einer Kursabfrage speichern. Gespeicherte Kurse bleiben, wie sie sind (einmal abgerufen)."""
    _require(ctx)
    heute = heute or datetime.now(timezone.utc).date()
    rows = PARSER[quelle](text, heute)
    at = datetime.now().isoformat(timespec="seconds")
    with tx(ctx.conn):
        neu = sum(ctx.conn.execute("INSERT OR IGNORE INTO btc_kurse (datum, quelle, eur, abgerufen_am) VALUES "
                                   "(?, ?, ?, ?)", (d, quelle, str(v), at)).rowcount for d, v in rows)
        if neu:
            audit.log(ctx.conn, ctx.actor, "btc_kurse_abruf", "btc_kurse", None,
                      {"quelle": quelle, "neu": neu, "bis": rows[-1][0]})
    return neu


def kraken_speichern(ctx: Ctx, text: str, heute: date | None = None) -> int:
    return abruf_speichern(ctx, "kraken", text, heute)


def bitstamp_ab(ctx: Ctx, ab: str | None = None) -> int:
    """Parameter start für den nächsten Bitstamp-Abruf: nach dem letzten gespeicherten Bitstamp-Tag, sonst `ab`, sonst
    der 01.01. des ersten Jahres mit Bewegung oder Vortrag (sonst des Vorjahres)."""
    last = ctx.conn.execute("SELECT MAX(datum) FROM btc_kurse WHERE quelle = 'bitstamp'").fetchone()[0]
    if last and not ab:
        return imp.kraken_since(last) + 86400
    if not ab:
        first = ctx.conn.execute("SELECT MIN(y) FROM (SELECT MIN(CAST(substr(datum, 1, 4) AS INTEGER)) AS y FROM "
                                 "btc_movements UNION ALL SELECT MIN(jahr) FROM btc_vortrag)").fetchone()[0]
        ab = f"{first or ctx.now_date().year - 1}-01-01"
    return imp.kraken_since(_tag(ab))


def kraken_seit(ctx: Ctx) -> int:
    """Parameter since für den nächsten Abruf: der letzte gespeicherte Kraken-Tag (0 = ganze verfügbare Historie)."""
    last = ctx.conn.execute("SELECT MAX(datum) FROM btc_kurse WHERE quelle = 'kraken'").fetchone()[0]
    return imp.kraken_since(last) if last else 0


def kurs(ctx: Ctx, datum: str) -> Decimal | None:
    row = kurs_zeile(ctx, datum)
    return row["eur"] if row else None


def kurs_zeile(ctx: Ctx, datum: str) -> dict | None:
    row = ctx.conn.execute(f"SELECT * FROM btc_kurse WHERE datum = ? ORDER BY {RANG} LIMIT 1", (datum,)).fetchone()
    return {**dict(row), "eur": Decimal(row["eur"])} if row else None


def letzter_kurs(ctx: Ctx, bis: str) -> dict | None:
    row = ctx.conn.execute(f"SELECT * FROM btc_kurse WHERE datum <= ? ORDER BY datum DESC, {RANG} LIMIT 1",
                           (bis,)).fetchone()
    return {**dict(row), "eur": Decimal(row["eur"])} if row else None


def kurse_stand(ctx: Ctx) -> dict:
    days = [r[0] for r in ctx.conn.execute("SELECT DISTINCT datum FROM btc_kurse ORDER BY datum")]
    luecken: list[str] = []
    if days:
        have, d, end = set(days), date.fromisoformat(days[0]), date.fromisoformat(days[-1])
        while d < end:
            d += timedelta(days=1)
            if d.isoformat() not in have:
                luecken.append(d.isoformat())
    return {"anzahl": len(days), "erster": days[0] if days else None,
            "letzter": letzter_kurs(ctx, days[-1]) if days else None, "luecken": luecken}


# -- Bewertung ------------------------------------------------------------------------------------------------------
def vortrag_setzen(ctx: Ctx, jahr: int, w: int | str, sats: int, ak, buchwert, notiz: str = "") -> None:
    """Anfangsbestand zum 01.01. (Bestand, Anschaffungskosten, Buchwert zum 31.12. des Vorjahres), wenn die Bewegungen
    davor nicht im Kontor stehen. Je Wallet; Anschaffungskosten und Buchwert zählen als Summe über alle Wallets."""
    _require(ctx)
    wid = _wallet_id(ctx, w)
    with tx(ctx.conn):
        ctx.conn.execute("INSERT INTO btc_vortrag (jahr, wallet_id, sats, ak, buchwert, notiz) VALUES (?, ?, ?, ?, ?, ?) "
                         "ON CONFLICT(jahr, wallet_id) DO UPDATE SET sats = excluded.sats, ak = excluded.ak, "
                         "buchwert = excluded.buchwert, notiz = excluded.notiz",
                         (int(jahr), wid, int(sats), md.to_cents(ak), md.to_cents(buchwert), notiz))
        audit.log(ctx.conn, ctx.actor, "btc_vortrag", "btc_vortrag", wid,
                  {"jahr": int(jahr), "sats": int(sats), "ak": str(ak), "buchwert": str(buchwert)})


def _vortrag(ctx: Ctx, jahr: int) -> dict | None:
    r = ctx.conn.execute("SELECT COUNT(*), SUM(sats), SUM(ak), SUM(buchwert) FROM btc_vortrag WHERE jahr = ?",
                         (jahr,)).fetchone()
    return {"sats": r[1], "ak": r[2], "buchwert": r[3]} if r[0] else None


def _ak(m: dict) -> int:
    return m["ak"] if m["art"] == "kundenzahlung" and m["ak"] is not None else m["eur"]


def bewertung(ctx: Ctx, jahr: int) -> dict:
    """Bewertung des Jahres. Der Anfangspool ist der Vortrag dieses Jahres, sonst das Ergebnis des Vorjahres."""
    first = [r[0] for r in ctx.conn.execute(
        "SELECT MIN(CAST(substr(datum, 1, 4) AS INTEGER)) FROM btc_movements WHERE status = 'bestaetigt' "
        "UNION ALL SELECT MIN(jahr) FROM btc_vortrag") if r[0] is not None]
    anfang, b = dict(NULL), None
    for y in range(min([jahr, *first]), jahr + 1):
        v = _vortrag(ctx, y)
        if v is not None:
            anfang = v
        elif b is not None:
            anfang = {"sats": b["bestand_sats"], "ak": b["ak"], "buchwert": b["buchwert"]}
        b = _jahr(ctx, y, anfang)
        if b["fehler"] and y < jahr:
            return {**_jahr(ctx, jahr, dict(NULL)), "fehler": f"Bewertung {y}: {b['fehler']}"}
    return b


def _jahr(ctx: Ctx, jahr: int, anfang: dict) -> dict:
    ms = bewegungen(ctx, jahr, "bestaetigt")
    zug = [m for m in ms if m["art"] in ZUGANG]
    pool = {"sats": anfang["sats"] + sum(m["sats"] for m in zug), "ak": anfang["ak"] + sum(_ak(m) for m in zug),
            "buchwert": anfang["buchwert"] + sum(_ak(m) for m in zug)}
    teile = []
    for m in ms:
        if m["art"] in ABGANG:
            teile.append((m, "betrag", m["sats"]))
        if m["art"] in SENDET and m["gebuehr_sats"]:
            teile.append((m, "gebuehr", m["gebuehr_sats"]))
    out = {"jahr": jahr, "anfang": anfang, "pool": pool,
           "zugaenge": [{"bewegung": m["id"], "art": m["art"], "datum": m["datum"], "sats": m["sats"], "ak": _ak(m)}
                        for m in zug],
           "abgaenge": [], "bestand_sats": pool["sats"], "ak": pool["ak"], "buchwert_vor_stichtag": pool["buchwert"],
           "kurs_stichtag": None, "kurs_quelle": None, "marktwert": None, "wertansatz": None, "schwebend": [], "abschreibung": 0,
           "zuschreibung": 0, "buchwert": pool["buchwert"], "fehler": ""}
    total = sum(s for _, _, s in teile)
    if total > pool["sats"]:
        out["fehler"] = f"Abgänge {total} sats übersteigen den Bestand {pool['sats']} sats (fehlt ein Zugang?)"
        return out
    cum = prev_ak = prev_bw = 0
    for m, teil, s in teile:
        cum += s
        ak, bw = _anteil(pool["ak"], cum, pool["sats"]), _anteil(pool["buchwert"], cum, pool["sats"])
        out["abgaenge"].append({"bewegung": m["id"], "art": m["art"], "teil": teil, "datum": m["datum"], "sats": s,
                                "ak": ak - prev_ak, "buchwert": bw - prev_bw})
        prev_ak, prev_bw = ak, bw
    bestand, ak_end, bw_vor = pool["sats"] - total, pool["ak"] - prev_ak, pool["buchwert"] - prev_bw
    out.update(bestand_sats=bestand, ak=ak_end, buchwert_vor_stichtag=bw_vor, buchwert=bw_vor)
    stichtag = f"{jahr}-12-31"
    kz = kurs_zeile(ctx, stichtag)
    if kz is None:
        return out
    k = kz["eur"]
    out["kurs_quelle"] = kz["quelle"]
    marktwert = wert(bestand, k)
    ansatz = min(ak_end, marktwert)
    ziel = ansatz
    for v in ctx.conn.execute("SELECT * FROM btc_movements WHERE status = 'bestaetigt' AND art = 'verkauf' AND "
                              "vertragsdatum <= ? AND datum > ? ORDER BY datum, id", (stichtag, stichtag)):
        anteil = _anteil(ansatz, min(v["sats"], bestand), bestand) if bestand else 0
        extra = max(anteil - v["eur"], 0)                  # Imparität: Vertragspreis unter dem Wertansatz
        out["schwebend"].append({"bewegung": v["id"], "sats": v["sats"], "eur": v["eur"], "abschreibung": extra})
        ziel -= extra
    diff = ziel - bw_vor
    out.update(kurs_stichtag=str(k), marktwert=marktwert, wertansatz=ansatz, abschreibung=max(-diff, 0),
               zuschreibung=max(diff, 0), buchwert=ziel)
    return out


def stand(ctx: Ctx, jahr: int | None = None) -> dict:
    """Bestand je Wallet und gesamt, Anschaffungskosten, Buchwert, Marktwert zum letzten Kurs, stille Reserve/Last."""
    jahr = jahr or ctx.now_date().year
    b = bewertung(ctx, jahr)
    ende = f"{jahr}-12-31"
    lk = letzter_kurs(ctx, min(ende, ctx.now_date().isoformat()))
    mw = wert(b["bestand_sats"], lk["eur"]) if lk else None
    return {"jahr": jahr, "sats": b["bestand_sats"], "ak": b["ak"], "buchwert": b["buchwert"], "kurs": lk,
            "marktwert": mw, "reserve": None if mw is None else mw - b["buchwert"], "fehler": b["fehler"],
            "wallets": [{**w, "sats": wallet_bestand(ctx, w["id"], ende, nur_bestaetigt=True)} for w in wallets(ctx)]}


# -- Import ---------------------------------------------------------------------------------------------------------
def importieren(ctx: Ctx, fmt: str, text: str, wallet: str | None = None) -> dict:
    """Export als Entwürfe übernehmen. Dubletten über einen Schlüssel aus Wallet, Zeitpunkt, Art und Betrag."""
    _require(ctx)
    if fmt not in imp.FORMATE:
        raise KontorError(f"Format {fmt!r} (erlaubt: {', '.join(imp.FORMATE)}).")
    if fmt != "kontor" and not wallet:
        raise KontorError("Wallet angeben, zu der der Export gehört.")
    rows = imp.FORMATE[fmt](text)
    wid = _wallet_id(ctx, wallet) if wallet else None
    neu = 0
    with tx(ctx.conn):
        for r in rows:
            w = wid if wid is not None else _wallet_id(ctx, r["wallet"])
            ziel = _wallet_id(ctx, r["ziel"]) if r["ziel"] else (w if r["art"] == "umbuchung" else None)
            key = hashlib.sha256(f"{w}|{r['zeit']}|{r['art']}|{r['sats']}".encode()).hexdigest()
            cur = ctx.conn.execute(
                "INSERT OR IGNORE INTO btc_movements (wallet_id, ziel_wallet_id, art, sats, gebuehr_sats, eur, gebuehr_eur, "
                "vertragsdatum, datum, notiz, quelle, import_schluessel) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (w, ziel, r["art"], r["sats"], r["gebuehr_sats"], r["eur"], r["gebuehr_eur"], r["vertragsdatum"],
                 r["datum"], r["notiz"], fmt, key))
            neu += cur.rowcount
        audit.log(ctx.conn, ctx.actor, "btc_import", "btc_movements", None, {"format": fmt, "zeilen": len(rows), "neu": neu})
    return {"zeilen": len(rows), "neu": neu, "vorhanden": len(rows) - neu}


# -- Zuordnungsvorschläge (ein Vorschlag ist kein Buchen) -----------------------------------------------------------
def vorschlag(ctx: Ctx, m: dict) -> dict | None:
    """Eingang ↔ offene Rechnung: Rechnungsnummer in der Notiz, sonst sats × Tagesschlusskurs innerhalb ±3 % des
    offenen Rests und höchstens 60 Tage nach dem Rechnungsdatum, genau ein Kandidat. Zahlung einer Eingangsrechnung
    in sats ohne EUR-Betrag: Wert zum Tagesschlusskurs des Belegdatums (sonst des Übergangs)."""
    if m["status"] != "entwurf":
        return None
    if m["art"] == "zahlung" and not m["eur"]:
        r = ctx.conn.execute("SELECT receipt_date FROM receipts WHERE id = ?", (m["receipt_id"],)).fetchone() \
            if m["receipt_id"] else None
        day = (r["receipt_date"] if r and r["receipt_date"] else None) or m["datum"]
        k = kurs(ctx, day)
        return {"art": "zahlung", "document_id": None, "nummer": None, "eur": wert(m["sats"], k),
                "regel": f"Tagesschlusskurs {day}"} if k is not None else None
    if m["art"] != "eingang":
        return None
    for num in NUMMER.findall((m["notiz"] or "").upper()):
        d = ctx.conn.execute("SELECT id FROM documents WHERE number = ?", (num,)).fetchone()
        if d and docs.get_document(ctx, d["id"])["open"] > 0:
            return {"art": "kundenzahlung", "document_id": d["id"], "nummer": num,
                    "eur": docs.get_document(ctx, d["id"])["open"], "regel": f"Nummer {num} in der Notiz"}
    k = kurs(ctx, m["datum"])
    if k is None:
        return None
    w, day = wert(m["sats"], k), date.fromisoformat(m["datum"])
    hits = [dict(r) for r in ctx.conn.execute(
        "SELECT d.id, d.number, d.doc_date, d.gross - d.prepaid_gross - COALESCE((SELECT SUM(amount) FROM payments "
        "WHERE document_id = d.id), 0) AS rest FROM documents d WHERE d.kind <> 'angebot' AND d.status IN "
        "('abgeschlossen', 'teilbezahlt') ORDER BY d.doc_date, d.number")]
    hits = [h for h in hits if h["rest"] > 0 and 0 <= (day - date.fromisoformat(h["doc_date"])).days <= 60
            and abs(w - h["rest"]) * 100 <= 3 * h["rest"]]
    if len(hits) != 1:
        return None
    h = hits[0]
    return {"art": "kundenzahlung", "document_id": h["id"], "nummer": h["number"], "eur": h["rest"],
            "regel": f"Wert {fmt_eur(w)} ≈ offen {fmt_eur(h['rest'])}"}


def bank_vorschlag(ctx: Ctx, m: dict) -> dict | None:
    """Verkauf bzw. Kauf ↔ Bankumsatz: Auszahlung (Erlös minus Gebühr) bzw. Kaufbetrag auf den Cent, Umsatz höchstens
    10 Tage nach (Verkauf) bzw. vor (Kauf) dem Übergangsdatum. Vorschlag: Konto Verrechnung ohne Steuer."""
    if m["status"] == "verworfen" or m["art"] not in ("kauf", "verkauf") or not m["eur"]:
        return None
    day = date.fromisoformat(m["datum"])
    betrag, von, bis = ((m["eur"] - m["gebuehr_eur"], day, day + timedelta(days=10)) if m["art"] == "verkauf"
                        else (-m["eur"], day - timedelta(days=10), day))
    hits = ctx.conn.execute(
        "SELECT t.id, t.booked_on, t.amount FROM bank_transactions t LEFT JOIN bank_assignments a "
        "ON a.bank_transaction_id = t.id WHERE a.bank_transaction_id IS NULL AND t.duplicate = 0 AND t.amount = ? "
        "AND t.booked_on BETWEEN ? AND ?", (betrag, von.isoformat(), bis.isoformat())).fetchall()
    if len(hits) != 1:
        return None
    return {"bank_transaction_id": hits[0]["id"], "datum": hits[0]["booked_on"], "betrag": betrag,
            "account": konten(ctx)["verrechnung"], "tax_key": "0"}


def bank_zuordnen(ctx: Ctx, mid: int) -> int:
    from . import bank
    _require(ctx)
    m = get(ctx, mid)
    s = bank_vorschlag(ctx, m)
    if not s:
        raise KontorError(f"Zu Bewegung {mid} passt kein offener Bankumsatz.")
    bank.assign(ctx, s["bank_transaction_id"], "konto", account=s["account"], tax_key="0",
                note=f"Bitcoin {LABEL[m['art']]} #{mid}")
    return s["bank_transaction_id"]


# -- Auffälligkeiten ------------------------------------------------------------------------------------------------
def _zeile(m: dict, konto: str) -> dict:
    return {"date": m["datum"], "account": konto, "contra": "", "beleg": f"BTC{m['id']}", "amount": m["eur"] or 0,
            "text": f"{LABEL[m['art']]} {m['sats']} sats {m['wallet']} {m['notiz']}".strip()}


def auffaelligkeiten(ctx: Ctx, jahr: int) -> list[dict]:
    from . import journal as jr
    k = konten(ctx)
    found: list[dict] = []
    drafts = bewegungen(ctx, jahr, "entwurf")
    if drafts:
        found.append({"art": "btc_entwurf", "konto": k["bestand"], "betrag": len(drafts),
                      "text": f"{len(drafts)} Bitcoin-Bewegungen sind noch Entwurf: zuordnen und bestätigen",
                      "buchungen": [_zeile(m, k["bestand"]) for m in drafts]})
    for s in salden(ctx):
        if s["datum"][:4] == str(jahr) and s["differenz"]:
            found.append({"art": "btc_wallet_differenz", "konto": k["bestand"], "betrag": s["differenz"],
                          "text": f"{s['wallet']} am {s['datum']}: Wallet zeigt {s['sats']} sats, Kontor {s['kontor']} sats, "
                                  f"Differenz {s['differenz']} sats", "buchungen": []})
    b = bewertung(ctx, jahr)
    if b["fehler"]:
        found.append({"art": "btc_bewertung", "konto": k["bestand"], "betrag": 0, "text": b["fehler"], "buchungen": []})
    stichtag = f"{jahr}-12-31"
    if b["kurs_stichtag"] is None and ctx.now_date().isoformat() > stichtag and (b["bestand_sats"] or b["schwebend"]):
        found.append({"art": "btc_kurs_fehlt", "konto": k["bestand"], "betrag": 0,
                      "text": f"Kurs zum {stichtag} fehlt: keine Bewertung zum Stichtag gebucht", "buchungen": []})
    for m in bewegungen(ctx, jahr, "bestaetigt"):
        if m["art"] != "kundenzahlung":
            continue
        kt = kurs(ctx, m["datum"])
        if kt is None:
            found.append({"art": "btc_kurs_fehlt", "konto": k["bestand"], "betrag": 0,
                          "text": f"Kurs vom {m['datum']} fehlt: Marktwert der Kundenzahlung {m['nummer']} nicht geprüft",
                          "buchungen": [_zeile(m, k["bestand"])]})
        elif m["ak"] is None and abs(wert(m["sats"], kt) - m["eur"]) * 100 > 3 * m["eur"]:
            mw = wert(m["sats"], kt)
            found.append({"art": "btc_marktwert", "konto": k["bestand"], "betrag": mw - m["eur"],
                          "text": f"{m['datum']} Kundenzahlung {m['nummer']}: Marktwert {fmt_eur(mw)}, Nennwert "
                                  f"{fmt_eur(m['eur'])} (über 3 % Abweichung). Anschaffungskosten auf den Marktwert setzen?",
                          "buchungen": [_zeile(m, k["bestand"])]})
    if md.get_profile(ctx)["gewinnermittlung"] == "bilanz" and not b["fehler"]:
        saldo = jr.balances(ctx, jahr).get(k["bestand"], 0)
        if saldo != b["buchwert"]:
            found.append({"art": "btc_buchwert_journal", "konto": k["bestand"], "betrag": saldo - b["buchwert"],
                          "text": f"{k['bestand']} steht im Journal auf {fmt_eur(saldo)}, das Bitcoin-Nebenbuch auf "
                                  f"{fmt_eur(b['buchwert'])}", "buchungen": jr.lines(ctx, jahr, k["bestand"])})
    return found
