"""Monatsabschluss eines Voranmeldezeitraums: fünf Schritte mit Stand (ok, offen, warn) und in Klartext, woraus sich
die Zahllast ergibt. Nur lesend; die Zahlen kommen aus `reports.ustva`, die Übermittlungen aus dem Protokoll."""
from __future__ import annotations

import json
import re

from ..context import Ctx
from ..core.money import fmt_eur
from ..core.tax_codes import period_bounds
from ..services import masterdata as md
from ..services import periods as per
from ..services import voranmeldung as va
from .ustva import ustva


def _kunde(row) -> str:
    if not row["customer_json"]:
        note = (row["note"] or "ohne Rechnung").strip()
        return note if len(note) <= 60 else note[:57].rsplit(" ", 1)[0] + " …"
    c = json.loads(row["customer_json"])
    return c.get("company") or c.get("contact") or ""


def _zahlungen(ctx: Ctx, spec: str) -> list[dict]:
    """Zahlungen an das Finanzamt, deren Notiz diesen Zeitraum nennt (z. B. „USt 2026-09 und 2026-10“)."""
    out = []
    for r in ctx.conn.execute("SELECT * FROM payments WHERE kind = 'ust' ORDER BY pay_date, id"):
        genannt = re.findall(r"\d{4}-(?:0[1-9]|1[0-2]|Q[1-4])", r["note"] or "")
        if spec in genannt:
            out.append({**dict(r), "zeitraeume": genannt})
    return out


def monatsabschluss(ctx: Ctx, spec: str) -> dict:
    u = ustva(ctx, spec)
    a, b = (d.isoformat() for d in period_bounds(spec))
    f = u["fields"]

    # -- Herleitung -----------------------------------------------------------------------------------------------
    einnahmen = [{"datum": r["pay_date"], "nummer": r["number"] or "ohne Rechnung", "kunde": _kunde(r),
                  "brutto": r["amount"], "steuer": r["tax"] or 0} for r in ctx.conn.execute(
        "SELECT p.pay_date, p.amount, p.note, d.number, d.customer_json, "
        "(SELECT SUM(tax) FROM payment_parts WHERE payment_id = p.id) AS tax FROM payments p "
        "LEFT JOIN documents d ON d.id = p.document_id WHERE p.kind = 'einnahme' AND p.origin = 'ledger' "
        "AND p.pay_date BETWEEN ? AND ? ORDER BY p.pay_date, p.id", (a, b))]
    ust_exakt = sum(e["steuer"] for e in einnahmen)
    ust_elster = f["81"]["tax"] + f["86"]["tax"]
    vorsteuer = [{"datum": v["receipt_date"], "id": v["id"], "lieferant": v["supplier"], "netto": v["net"],
                  "steuer": v["tax"]} for v in u["details"]["vorsteuer"]]
    rc = [{"datum": v["receipt_date"], "id": v["id"], "lieferant": v["supplier"], "netto": v["net"],
           "steuer": v["tax"]} for v in u["details"]["reverse_charge"]]
    herleitung = {
        "umsatzsteuer": {"summe": ust_elster, "posten": einnahmen},
        "vorsteuer": {"summe": f["66"]["tax"], "posten": vorsteuer},
        "reverse_charge": {"summe": f["67"]["tax"], "posten": rc},
        "zahllast": u["zahllast"],
        "rundung": ust_elster - ust_exakt,     # ELSTER rechnet die Steuer aus vollen Euro der Bemessungsgrundlage
    }

    # -- Schritte -------------------------------------------------------------------------------------------------
    schritte = []
    eingang = ctx.conn.execute("SELECT id, supplier, original_name, receipt_date FROM receipts WHERE status = 'eingang' "
                               "AND (receipt_date IS NULL OR receipt_date BETWEEN ? AND ?) ORDER BY id", (a, b)).fetchall()
    n_belege = ctx.conn.execute("SELECT COUNT(*) FROM receipts WHERE status = 'geprueft' AND receipt_date BETWEEN ? AND ?",
                                (a, b)).fetchone()[0]
    schritte.append({"schluessel": "belege", "titel": "Belege bestätigt", "stand": "offen" if eingang else "ok",
                     "text": (f"{len(eingang)} Beleg{'e' if len(eingang) != 1 else ''} im Eingang: "
                              + ", ".join(r["supplier"] or r["original_name"] for r in eingang)) if eingang
                     else f"{n_belege} Beleg{'e' if n_belege != 1 else ''} im Zeitraum, alle bestätigt.",
                     "link": "/belege?status=eingang" if eingang else ""})

    ohne = [e for e in einnahmen if e["nummer"] == "ohne Rechnung"]
    text = (f"{len(einnahmen)} Zahlungseingang{'' if len(einnahmen) == 1 else 'e'}, zusammen "
            f"{fmt_eur(sum(e['brutto'] for e in einnahmen))}." if einnahmen else "Keine Zahlungseingänge im Zeitraum.")
    if ohne:
        text += f" Davon {len(ohne)} ohne Rechnung: zählt schon, die Rechnung fehlt noch."
    schritte.append({"schluessel": "einnahmen", "titel": "Einnahmen erfasst", "stand": "ok", "text": text,
                     "hinweis": bool(ohne)})

    kz = {n: f[n][teil] for n, teil in va.FELDER if f[n][teil]}
    echt = [m for m in va.uebermittlungen(ctx, spec) if m["ok"] and not m["test"]]
    v = {"schluessel": "voranmeldung", "titel": "Voranmeldung an ELSTER", "stand": "offen",
         "text": "Noch nicht über Kontor gesendet."}
    if echt:
        g = echt[0]
        job = next((j for j in ctx.conn.execute(
            "SELECT id, ergebnis FROM auftraege WHERE art = 'ustva' AND status = 'gesendet' AND test = 0 "
            "ORDER BY id DESC") if json.loads(j["ergebnis"]).get("transferticket") == g["transferticket"]), None)
        gleich = g["kz"] == kz
        v.update({"stand": "ok" if gleich else "warn", "transferticket": g["transferticket"],
                  "auftrag": job["id"] if job else None, "am": g["am"],
                  "text": f"Gesendet am {g['am'][8:10]}.{g['am'][5:7]}. um {g['am'][11:16]} Uhr, "
                          f"{fmt_eur(g['kz83'])}{' (Berichtigung)' if g.get('berichtigt') else ''}. "
                          + ("Werte seitdem unverändert." if gleich else
                             f"Seitdem geändert, jetzt {fmt_eur(u['zahllast'])}: Berichtigung senden.")})
    schritte.append(v)

    zahlungen = _zahlungen(ctx, spec)
    soll = u["zahllast"]
    z = {"schluessel": "zahlung", "titel": "Zahlung an das Finanzamt", "stand": "offen",
         "text": (f"{fmt_eur(soll)} bis {u['deadline'][8:10]}.{u['deadline'][5:7]}.{u['deadline'][:4]} überweisen, "
                  f"dann hier erfassen (Notiz mit „{spec}“)." if soll > 0 else
                  f"Erstattung {fmt_eur(-soll)} kommt vom Finanzamt; bei Eingang erfassen (Notiz mit „{spec}“)."
                  if soll < 0 else "Nichts zu zahlen.")}
    if soll == 0 and not zahlungen:
        z["stand"] = "ok"
    if zahlungen:
        gezahlt = -sum(p["amount"] for p in zahlungen)
        mit = sorted({s for p in zahlungen for s in p["zeitraeume"]})
        soll_mit = sum(ustva(ctx, s)["zahllast"] for s in mit)
        z["stand"] = "ok" if gezahlt == soll_mit else "warn"
        z["text"] = (f"{fmt_eur(gezahlt)} am " + ", ".join(f"{p['pay_date'][8:10]}.{p['pay_date'][5:7]}." for p in zahlungen)
                     + (f" für {', '.join(mit)}" if len(mit) > 1 else "")
                     + ("." if gezahlt == soll_mit else f", erwartet {fmt_eur(soll_mit)}."))
    schritte.append(z)

    monate = {f"{a[:4]}-{m:02d}" for m in range(int(a[5:7]), int(b[5:7]) + 1)}
    gesperrt = monate <= {l["month"] for l in per.locked_months(ctx)}
    schritte.append({"schluessel": "gesperrt", "titel": "Monat gesperrt", "stand": "ok" if gesperrt else "offen",
                     "text": "Belege und Zahlungen sind fest." if gesperrt
                     else "Zum Schluss sperren: danach ändert sich in diesem Zeitraum nichts mehr."})

    return {"zeitraum": spec, "name": va.name(spec) if spec[5:] else spec, "frist": u["deadline"],
            "ueberfaellig": z["stand"] != "ok" and soll > 0 and u["deadline"] < ctx.now_date().isoformat(),
            "monate": sorted(monate), "heute": ctx.now_date().isoformat(),
            "schritte": schritte, "offen": sum(s["stand"] != "ok" for s in schritte), "herleitung": herleitung,
            "fields": f, "ustva_eric": md.has(ctx, "ustva_eric")}
