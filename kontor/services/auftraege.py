"""Aufträge zur Freigabe: Übermittlungen an Behörden, die ein Agent über die Kommandozeile vorbereitet und der Mensch
in der Oberfläche freigibt, einzeln oder gesammelt, mit einmal Zertifikats-PIN.

Beim Anlegen prüft ERiC den Datensatz; nur Plausibles kommt in die Liste. `werte` hält fest, was gesendet würde,
`erklaerung` sagt es in einfacher Sprache. Beim Freigeben rechnet Kontor die Werte neu: weichen sie ab (z. B. eine
Abrechnung wurde storniert), geht der Auftrag nicht raus, sondern wird `veraltet`. Gesendet wird nur durch den
Menschen; die PIN wird nie gespeichert."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Callable

from ..context import Ctx, require_human
from ..core.errors import KontorError
from ..core.money import fmt_eur
from ..store import audit
from ..store.db import tx
from . import masterdata as md


@dataclass
class Art:
    name: str
    werte: Callable[[Ctx, dict], dict]
    titel: Callable[[Ctx, dict, dict], str]
    erklaerung: Callable[[Ctx, dict, dict], str]
    pruefen: Callable[[Ctx, dict], dict] | None
    senden: Callable[[Ctx, dict, str, bool], dict]
    zeilen: Callable[[dict], list[tuple[str, str]]] | None = None      # Werte beschriftet für die Detailansicht


def _datum(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d.%m.%Y")


def _finanzamt(ctx: Ctx) -> str:
    return md.get_settings(ctx)["lohn_finanzamt_name"] or "das Finanzamt"


# -- Lohn -----------------------------------------------------------------------------------------------------------
def _lohn():
    from . import lohn
    return lohn


def _lsta_werte(ctx: Ctx, p: dict) -> dict:
    lohn = _lohn()
    satz, w = lohn.lsta_satz(ctx, int(p["jahr"]), p["zeitraum"])
    return {"kz": w["kz"], "berichtigt": satz.berichtigt}


def _lsta_erklaerung(ctx: Ctx, p: dict, w: dict) -> str:
    lohn, kz = _lohn(), w["kz"]
    z = next(x for x in lohn.zeitraeume(ctx, int(p["jahr"])) if x["zeitraum"] == p["zeitraum"])
    kist = sum(kz["kist"].values()) + kz["pauschal_kist"]
    teile = [f"Meldet {_finanzamt(ctx)} die Lohnsteuer für {z['name']} {p['jahr']}: {kz['arbeitnehmer']} "
             f"Arbeitnehmer, Lohnsteuer {fmt_eur(kz['lst'])}, Solidaritätszuschlag {fmt_eur(kz['soli'])}, "
             f"Kirchensteuer {fmt_eur(kist)}.",
             f"Zu zahlen: {fmt_eur(kz['gesamt'])} bis {_datum(z['faellig'])}."]
    if w["berichtigt"]:
        teile.append("Ersetzt die schon gesendete Anmeldung für diesen Zeitraum (berichtigte Anmeldung).")
    return " ".join(teile)


def _lsta_zeilen(w: dict) -> list[tuple[str, str]]:
    kz = w["kz"]
    out = [("Kz86 · Zahl der Arbeitnehmer", str(kz["arbeitnehmer"])), ("Kz42 · Lohnsteuer", fmt_eur(kz["lst"])),
           ("Kz49 · Solidaritätszuschlag", fmt_eur(kz["soli"]))]
    out += [(f"Kirchensteuer {konf}", fmt_eur(v)) for konf, v in sorted(kz["kist"].items())]
    if kz["pauschal_kist"]:
        out.append(("Kz47 · Pauschale Kirchensteuer", fmt_eur(kz["pauschal_kist"])))
    out.append(("Kz83 · Zu zahlen", fmt_eur(kz["gesamt"])))
    if w["berichtigt"]:
        out.insert(0, ("Kz10 · Berichtigte Anmeldung", "ja"))
    return out


def _lstb_werte(ctx: Ctx, p: dict) -> dict:
    b = _lohn().lstb_werte(ctx, int(p["mitarbeiter"]), int(p["jahr"]))
    return {k: v for k, v in b.items() if k.startswith("zeile")} | {"name": b["name"], "von": b["von"], "bis": b["bis"],
                                                                    "korrektur": any(g["ok"] and not g["test"]
                                                                                     for g in b["gesendet"])}


def _lstb_erklaerung(ctx: Ctx, p: dict, w: dict) -> str:
    text = (f"Schickt die Lohnsteuerbescheinigung {p['jahr']} von {w['name']} ({_datum(w['von'])} bis "
            f"{_datum(w['bis'])}) an die Finanzverwaltung: Bruttoarbeitslohn {fmt_eur(w['zeile3_brutto'])}, "
            f"Lohnsteuer {fmt_eur(w['zeile4_lst'])}, Solidaritätszuschlag {fmt_eur(w['zeile5_soli'])}, Kirchensteuer "
            f"{fmt_eur(w['zeile6_kist'] + w['zeile7_kist_ehegatte'])}.")
    if w["korrektur"]:
        text += " Ersetzt die schon gesendete Bescheinigung (Korrektur)."
    return text


ELSTAM_TEXT = {
    "anmelden": "Meldet {name} bei ELSTER als Arbeitnehmer der Firma an, ab {ref}. Danach liefert ELSTER die "
                "Lohnsteuermerkmale (Steuerklasse usw.) ins Postfach; Abholen ist ein eigener Auftrag.",
    "wechsel": "Teilt ELSTER mit, dass ab {ref} Kontor statt des bisherigen Lohnprogramms die Lohnsteuermerkmale von "
               "{name} bekommt. Danach kommen sie ins Postfach; Abholen ist ein eigener Auftrag.",
    "abmelden": "Meldet {name} bei ELSTER als Arbeitnehmer der Firma ab (Austritt). Danach kommen keine "
                "Lohnsteuermerkmale mehr.",
}


def _elstam_werte(ctx: Ctx, p: dict) -> dict:
    m = _lohn().get(ctx, int(p["mitarbeiter"]))
    return {"vorgang": p["vorgang"], "name": m["name"], "referenzdatum": p.get("referenzdatum") or m["eintritt"],
            "austritt": m["austritt"]}


# -- E-Bilanz -------------------------------------------------------------------------------------------------------
def _ebilanz_werte(ctx: Ctx, p: dict) -> dict:
    from . import elster
    if not md.has(ctx, "e_bilanz"):
        raise KontorError("E-Bilanz ist für diesen Mandanten aus (Profilschalter e_bilanz).")
    return elster.kennzahlen(ctx, int(p["jahr"]))


def _ebilanz_pruefen(ctx: Ctx, p: dict) -> dict:
    from . import elster
    return elster.pruefen(ctx, int(p["jahr"]))


def _ebilanz_senden(ctx: Ctx, p: dict, pin: str, test: bool) -> dict:
    from . import elster
    return elster.senden(ctx, int(p["jahr"]), pin, test)


# -- Umsatzsteuer-Voranmeldung --------------------------------------------------------------------------------------
def _ustva():
    from . import voranmeldung
    return voranmeldung


def _ustva_erklaerung(ctx: Ctx, p: dict, w: dict) -> str:
    kz = w["kz"]
    fa = md.get_settings(ctx)["lohn_finanzamt_name"] or "Finanzamt"

    def euro(n: int) -> str:
        return f"{n:,} €".replace(",", ".")
    teile = [f"Meldet dem {fa} die Umsatzsteuer für {_ustva().name(p['zeitraum'])}:"]
    if kz.get("81"):
        teile.append(f"Umsätze zu 19 % {euro(kz['81'])} (Steuer {fmt_eur(kz['81'] * 19)}).")
    if kz.get("86"):
        teile.append(f"Umsätze zu 7 % {euro(kz['86'])} (Steuer {fmt_eur(kz['86'] * 7)}).")
    if kz.get("21") or kz.get("45"):
        teile.append(f"Leistungen ins Ausland ohne deutsche Steuer {euro(kz.get('21', 0) + kz.get('45', 0))}.")
    if kz.get("46") or kz.get("84"):
        teile.append(f"Leistungen ausländischer Anbieter (Reverse Charge) {euro(kz.get('46', 0) + kz.get('84', 0))}, Steuer "
                     f"{fmt_eur(kz.get('47', 0) + kz.get('85', 0))}, im selben Zug als Vorsteuer abgezogen.")
    teile.append(f"Vorsteuer aus Rechnungen {fmt_eur(kz.get('66', 0))}.")
    if not any(kz.get(n) for n in ("81", "86", "21", "45", "46", "84", "66")):
        teile.append("Nullmeldung.")
    faellig = _datum(w["faellig"]) + (" (schon fällig)" if w["faellig"] < ctx.now_date().isoformat() else "")
    teile.append(f"Zu zahlen: {fmt_eur(w['kz83'])} bis {faellig}." if w["kz83"] >= 0
                 else f"Erstattung: {fmt_eur(-w['kz83'])}.")
    if w["berichtigt"]:
        teile.append("Ersetzt die schon gesendete Voranmeldung für diesen Zeitraum (berichtigte Anmeldung).")
    return " ".join(teile)


def _ustva_zeilen(w: dict) -> list[tuple[str, str]]:
    from ..core.tax_codes import KZ_LABELS
    from ..elster.ustva import KENNZAHLEN
    out = [(f"Kz{n} · {KZ_LABELS[n]}", f"{w['kz'][n]:,} €".replace(",", ".") if ganz else fmt_eur(w["kz"][n]))
           for n, ganz in KENNZAHLEN.items() if n in w["kz"]]
    out.append((f"Kz83 · {KZ_LABELS['83']}", fmt_eur(w["kz83"])))
    if w["berichtigt"]:
        out.insert(0, ("Kz10 · Berichtigte Anmeldung", "ja"))
    return out


def _flach(w, prefix: str = "") -> list[tuple[str, str]]:
    out = []
    for k, v in w.items():
        if isinstance(v, dict):
            out += _flach(v, f"{prefix}{k} · ")
        else:
            out.append((f"{prefix}{k}", "ja" if v is True else "nein" if v is False else str(v)))
    return out


def zeilen(a: dict) -> list[tuple[str, str]]:
    """Die gespeicherten Werte eines Auftrags, beschriftet, für die Detailansicht."""
    art = ARTEN.get(a["art"])
    return art.zeilen(a["werte"]) if art and art.zeilen else _flach(a["werte"])


ARTEN: dict[str, Art] = {
    "lsta": Art("Lohnsteuer-Anmeldung", _lsta_werte,
                lambda ctx, p, w: f"Lohnsteuer-Anmeldung {_lohn().zeitraum_name(p['zeitraum'])} {p['jahr']}",
                _lsta_erklaerung, lambda ctx, p: _lohn().lsta_pruefen(ctx, int(p["jahr"]), p["zeitraum"]),
                lambda ctx, p, pin, test: _lohn().lsta_senden(ctx, int(p["jahr"]), p["zeitraum"], pin, test),
                _lsta_zeilen),
    "lstb": Art("Lohnsteuerbescheinigung", _lstb_werte,
                lambda ctx, p, w: f"Lohnsteuerbescheinigung {p['jahr']} {w['name']}", _lstb_erklaerung,
                lambda ctx, p: _lohn().lstb_pruefen(ctx, int(p["mitarbeiter"]), int(p["jahr"])),
                lambda ctx, p, pin, test: _lohn().lstb_senden(ctx, int(p["mitarbeiter"]), int(p["jahr"]), pin, test)),
    "elstam": Art("ELStAM", _elstam_werte,
                  lambda ctx, p, w: f"ELStAM {_lohn().ELSTAM_ARTEN[p['vorgang']]} {w['name']}",
                  lambda ctx, p, w: ELSTAM_TEXT[p["vorgang"]].format(name=w["name"], ref=_datum(w["referenzdatum"])),
                  lambda ctx, p: _lohn().elstam_pruefen(ctx, p["vorgang"], [int(p["mitarbeiter"])],
                                                        p.get("referenzdatum")),
                  lambda ctx, p, pin, test: _lohn().elstam_senden(ctx, p["vorgang"], [int(p["mitarbeiter"])], pin,
                                                                  test, p.get("referenzdatum"))),
    "elstam_abholen": Art("ELStAM abholen", lambda ctx, p: {},
                          lambda ctx, p, w: "ELStAM aus dem ELSTER-Postfach abholen",
                          lambda ctx, p, w: "Holt bereitliegende Lohnsteuermerkmale aus dem ELSTER-Postfach der Firma "
                                            "und legt sie in Kontor ab. Abweichungen zu den Merkmalen in Kontor zeigt "
                                            "Kontor danach als Auffälligkeit, bis sie übernommen sind.",
                          None, lambda ctx, p, pin, test: _lohn().elstam_abholen(ctx, pin, test)),
    "ustva": Art("Umsatzsteuer-Voranmeldung", lambda ctx, p: _ustva().werte(ctx, p["zeitraum"]),
                 lambda ctx, p, w: f"Umsatzsteuer-Voranmeldung {_ustva().name(p['zeitraum'])}", _ustva_erklaerung,
                 lambda ctx, p: _ustva().pruefen(ctx, p["zeitraum"]),
                 lambda ctx, p, pin, test: _ustva().senden(ctx, p["zeitraum"], pin, test), _ustva_zeilen),
    "ebilanz": Art("E-Bilanz", _ebilanz_werte, lambda ctx, p, w: f"E-Bilanz {p['jahr']}",
                   lambda ctx, p, w: f"Schickt die E-Bilanz {p['jahr']} an das Finanzamt: Bilanzsumme "
                                     f"{fmt_eur(w['bilanzsumme'])}, Eigenkapital {fmt_eur(w['eigenkapital'])}, "
                                     f"Jahresergebnis {fmt_eur(w['jahresergebnis'])}.",
                   _ebilanz_pruefen, _ebilanz_senden),
}


def _row(r) -> dict:
    return {**dict(r), "parameter": json.loads(r["parameter"]), "werte": json.loads(r["werte"]),
            "ergebnis": json.loads(r["ergebnis"])}


def anlegen(ctx: Ctx, art: str, parameter: dict, test: bool = False, notiz: str = "") -> dict:
    """Auftrag vorbereiten. ERiC prüft den Datensatz; nur Plausibles wird angelegt. Ein gleicher offener Auftrag
    (Art, Parameter, Test) wird nicht doppelt angelegt."""
    if art not in ARTEN:
        raise KontorError(f"Auftragsart {art!r} (erlaubt: {', '.join(ARTEN)}).")
    a = ARTEN[art]
    p = {k: v for k, v in parameter.items() if v not in (None, "")}
    w = a.werte(ctx, p)
    if a.pruefen:
        r = a.pruefen(ctx, p)
        if not r["ok"]:
            raise KontorError(f"ERiC lehnt den Datensatz ab: {r['text']} {' · '.join(r['meldungen'])}".strip())
    key = json.dumps(p, sort_keys=True)
    if ctx.conn.execute("SELECT 1 FROM auftraege WHERE art = ? AND parameter = ? AND test = ? AND status = 'offen'",
                        (art, key, int(test))).fetchone():
        raise KontorError("Diesen Auftrag gibt es schon offen (Freigaben).")
    titel = ("Testversand: " if test else "") + a.titel(ctx, p, w)
    erklaerung = a.erklaerung(ctx, p, w)
    if test:
        erklaerung += " Testversand: ELSTER nimmt ihn an und löscht ihn wieder, er zählt nicht."
    with tx(ctx.conn):
        cur = ctx.conn.execute("INSERT INTO auftraege (art, parameter, titel, erklaerung, werte, notiz, test, "
                               "erstellt_von) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (art, key, titel, erklaerung, json.dumps(w, default=str, sort_keys=True),
                                (notiz or "").strip(), int(test), ctx.actor))
        audit.log(ctx.conn, ctx.actor, "auftrag_anlegen", "auftraege", cur.lastrowid,
                  {"art": art, "parameter": p, "test": test})
    return get(ctx, cur.lastrowid)


def get(ctx: Ctx, aid: int) -> dict:
    row = ctx.conn.execute("SELECT * FROM auftraege WHERE id = ?", (aid,)).fetchone()
    if not row:
        raise KontorError(f"Auftrag {aid} gibt es nicht.")
    return _row(row)


def liste(ctx: Ctx, status: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM auftraege", []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    return [_row(r) for r in ctx.conn.execute(sql + " ORDER BY id DESC LIMIT 200", args)]


def offen_anzahl(ctx: Ctx) -> int:
    return ctx.conn.execute("SELECT COUNT(*) FROM auftraege WHERE status = 'offen'").fetchone()[0]


def verwerfen(ctx: Ctx, aid: int, grund: str) -> None:
    if not (grund or "").strip():
        raise KontorError("Grund angeben.")
    if get(ctx, aid)["status"] != "offen":
        raise KontorError("Nur offene Aufträge lassen sich verwerfen.")
    _erledigt(ctx, aid, "verworfen", {"grund": grund.strip()})


def _erledigt(ctx: Ctx, aid: int, status: str, ergebnis: dict) -> None:
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE auftraege SET status = ?, ergebnis = ?, erledigt_am = "
                         "strftime('%Y-%m-%dT%H:%M:%S','now','localtime') WHERE id = ?",
                         (status, json.dumps(ergebnis, default=str), aid))
        audit.log(ctx.conn, ctx.actor, f"auftrag_{status}", "auftraege", aid, ergebnis)


ESIGNER = (610201100, 610201199)      # ERiC: eSigner-Fehler (falsche oder gesperrte PIN, Zertifikat), vor dem Versand


def pruefen(ctx: Ctx, aid: int) -> dict:
    """Vor dem Senden nachsehen: Werte neu rechnen und mit dem Angelegten vergleichen, ERiC lokal prüfen lassen.
    Sendet nichts und ändert den Auftrag nicht."""
    a = get(ctx, aid)
    if a["status"] != "offen":
        raise KontorError("Nur offene Aufträge lassen sich prüfen.")
    art = ARTEN[a["art"]]
    try:
        jetzt = json.loads(json.dumps(art.werte(ctx, a["parameter"]), default=str, sort_keys=True))
    except KontorError as e:
        return {"ok": False, "code": 0, "text": str(e), "meldungen": [], "geaendert": True}
    out = {"ok": True, "code": 0, "text": "Ohne ERiC-Prüfung (diese Auftragsart hat keine).", "meldungen": [],
           "geaendert": jetzt != a["werte"], "jetzt": zeilen({**a, "werte": jetzt})}
    if art.pruefen:
        try:
            r = art.pruefen(ctx, a["parameter"])
        except KontorError as e:
            r = {"ok": False, "code": 0, "text": str(e), "meldungen": []}
        if r.get("code") == 610001003:
            r = {**r, "ok": True, "text": "Plausibel, mit Hinweisen."}
        out.update({k: r.get(k) for k in ("ok", "code", "text", "meldungen")})
    return out


def freigeben(ctx: Ctx, ids: list[int], pin: str) -> list[dict]:
    """Ausgewählte offene Aufträge der Reihe nach senden (nur der Mensch), alle mit derselben PIN. Ein Auftrag, dessen
    Werte sich seit dem Anlegen geändert haben, geht nicht raus."""
    require_human(ctx)
    if not ids:
        raise KontorError("Mindestens einen Auftrag auswählen.")
    if not pin:
        raise KontorError("PIN des Zertifikats eingeben.")
    out = []
    for aid in sorted(ids):
        a = get(ctx, aid)
        if a["status"] != "offen":
            out.append({"id": aid, "titel": a["titel"], "ok": False, "text": f"nicht offen ({a['status']})"})
            continue
        art = ARTEN[a["art"]]
        try:
            jetzt = json.loads(json.dumps(art.werte(ctx, a["parameter"]), default=str, sort_keys=True))
        except KontorError as e:
            jetzt = {"fehler": str(e)}
        if jetzt != a["werte"]:
            _erledigt(ctx, aid, "veraltet", {"vorher": a["werte"], "jetzt": jetzt})
            out.append({"id": aid, "titel": a["titel"], "ok": False,
                        "text": "Werte haben sich seit dem Anlegen geändert, nicht gesendet. Neu anlegen."})
            continue
        try:
            r = art.senden(ctx, a["parameter"], pin, bool(a["test"]))
        except KontorError as e:
            r = {"ok": False, "code": 0, "text": str(e), "meldungen": []}
        if not r.get("ok") and ESIGNER[0] <= (r.get("code") or 0) <= ESIGNER[1]:
            # Zertifikat/PIN scheitert vor dem Versand: Auftrag bleibt offen, und keine weitere Fehleingabe am Zertifikat
            out.append({"id": aid, "titel": a["titel"], "ok": False, "code": r["code"],
                        "text": f"Nicht gesendet, Auftrag bleibt offen: {r['text']} PIN prüfen und erneut freigeben."})
            break
        ergebnis = {k: r.get(k) for k in ("ok", "code", "text", "meldungen", "transferticket", "protokoll")}
        _erledigt(ctx, aid, "gesendet" if r.get("ok") else "abgelehnt", ergebnis)
        out.append({"id": aid, "titel": a["titel"], **ergebnis})
    return out
