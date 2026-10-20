"""Erzeugt kontor/elster/taxonomie_<version>.json aus der amtlichen Excel-Sicht der Kerntaxonomie (esteuer.de,
de-gaap-ci-…-shell-fiscal-microbilg.xlsx). Läuft einmal je Taxonomie-Version, braucht openpyxl (nicht in Kontor).

    python tools/ebilanz_taxonomie.py <microbilg.xlsx> 6.8

Je Position: Elternposition der Rechenregel, Vorzeichen, Pflichtkennzeichen, Zeitbezug, ob für Kapitalgesellschaften
vorgesehen, und ob sie steuerlich unzulässig ist. Bilanz und GuV nach MicroBilG werden gefüllt; aus den übrigen
Teilen verlangt ERiC die Pflichtfelder leer (Spec 18.9)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import openpyxl

PARTS = {"Bilanz": "bilanz", "Gewinn- und Verlustrechnung nach MicroBilG": "guv", "Steuerliche Gewinnermittlung": "sge",
         "Steuerliche Gewinnermittlung bei Feststellungsverfahren": "sgep", "Steuerlicher Betriebsvermögensvergleich": "bvv",
         "Ergebnisverwendung": "ev", "Kapitalkontenentwicklung für Personenhandelsgesellschaften": "kke"}
REQ = {"Mussfeld": "M", "Mussfeld, Kontennachweis erwünscht": "MK", "Summenmussfeld": "SM",
       "Rechnerisch notwendig, soweit vorhanden": "RN"}


def main(xlsx: str, version: str) -> None:
    rows = list(openpyxl.load_workbook(xlsx, read_only=True).worksheets[0].iter_rows(values_only=True))
    hdr = list(rows[6])
    col = {h: hdr.index(h) for h in ("name", "Berichtsteil", "periodType", "+/-", "Rechnerisch verknüpft mit",
                                      "fiscalRequirement", "notPermittedFor", "legalFormKSt", "standard de", "abstract",
                                      "type")}
    since, through = hdr.index("validSince"), hdr.index("validThrough")
    out, current = {}, None
    for r in rows[7:]:
        if r[0] == "original_reference" and current:        # Gültigkeit steht in den Referenzzeilen der Position
            current["gueltig_ab"] = current["gueltig_ab"] or r[since] or ""
            current["gueltig_bis"] = current["gueltig_bis"] or r[through] or ""
            continue
        current = None
        if r[0] != "original_position" or r[col["Berichtsteil"]] not in PARTS:
            continue
        parent = (r[col["Rechnerisch verknüpft mit"]] or "").removeprefix("Oberposition (").removesuffix(")")
        out[r[col["name"]]] = {
            "teil": PARTS[r[col["Berichtsteil"]]], "eltern": parent or None,
            "vz": -1 if r[col["+/-"]] == "-" else 1, "pflicht": REQ.get(r[col["fiscalRequirement"]] or "", ""),
            "stichtag": r[col["periodType"]] == "instant", "kst": r[col["legalFormKSt"]] == "true",
            "steuerlich_unzulaessig": "steuerlich" in (r[col["notPermittedFor"]] or ""),
            "abstrakt": r[col["abstract"]] == "true", "geld": r[col["type"]] == "xbrli:monetaryItemType",
            "text": r[col["standard de"]] or "", "gueltig_ab": "", "gueltig_bis": ""}
        current = out[r[col["name"]]]
    target = Path(__file__).resolve().parent.parent / "kontor" / "elster" / f"taxonomie_{version.replace('.', '_')}.json"
    target.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    print(target, len(out))


if __name__ == "__main__":
    main(*sys.argv[1:3])
