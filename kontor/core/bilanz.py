"""Bilanz- und GuV-Posten der Kleinstkapitalgesellschaft aus Kontensalden (SKR04).
Salden in Cent, Soll positiv. Rein, ohne IO."""
from __future__ import annotations

from .errors import KontorError

UST_ACCOUNTS = ("3801", "3806", "3811", "3816", "3837", "3841", "1401", "1406", "1407")

# Bezeichnungen der Konten, die Kontor selbst bebucht oder vorschlägt (Anzeige, wenn kein Export sie liefert)
SKR04_NAMES = {
    "1200": "Forderungen aus Lieferungen und Leistungen", "1401": "Abziehbare Vorsteuer 7 %",
    "1406": "Abziehbare Vorsteuer 19 %", "1407": "Abziehbare Vorsteuer nach § 13b UStG 19 %",
    "1460": "Geldtransit", "3300": "Verbindlichkeiten aus Lieferungen und Leistungen",
    "3500": "Noch nicht zugeordnete Bankvorgänge", "3510": "Verbindlichkeiten gegenüber Gesellschaftern",
    "3720": "Verbindlichkeiten aus Lohn und Gehalt", "3801": "Umsatzsteuer 7 %", "3806": "Umsatzsteuer 19 %",
    "3811": "Umsatzsteuer nicht fällig 7 %", "3816": "Umsatzsteuer nicht fällig 19 %",
    "3837": "Umsatzsteuer nach § 13b UStG 19 %", "3841": "Umsatzsteuer Vorjahr",
    "4200": "Erlöse", "4300": "Erlöse 7 % USt", "4336": "Erlöse aus im anderen EU-Land steuerpflichtigen Leistungen (§ 13b)",
    "4338": "Erlöse aus im Drittland steuerbaren Leistungen", "4400": "Erlöse 19 % USt",
    "6120": "Beiträge zur Berufsgenossenschaft", "6420": "Beiträge", "6673": "Reisekosten Unternehmer Fahrtkosten",
    "6825": "Rechts- und Beratungskosten", "6827": "Abschluss- und Prüfungskosten", "6830": "Buchführungskosten",
    "6837": "Aufwendungen für die zeitlich befristete Überlassung von Rechten (Lizenzen)",
    "6840": "Mietleasing (bewegliche Wirtschaftsgüter)", "6969": "Sonstige Aufwendungen unregelmäßig",
}
EQUITY_ACCOUNTS = ("2900", "2970", "2978")     # gezeichnetes Kapital, Gewinn- und Verlustvortrag


def is_pl(account: str) -> bool:
    return len(account) == 4 and account[0] in "4567"


def positions(bal: dict[str, int], share_capital: int, outstanding: int) -> dict[str, int]:
    """Kapital und ausstehende Einlage kommen aus den Stammdaten; 2900 trägt nur die Differenz.
    Umsatzsteuer und Vorsteuer sind ein Anspruch des Finanzamts und werden saldiert ausgewiesen."""
    p = dict.fromkeys(("ford", "sonstVG", "bank", "rueck", "anz", "lul", "sonstV", "steuern", "lohn", "umsatz", "sbe",
                       "material", "personal", "abAV", "abUV", "sba", "zinsE", "zinsA"), 0)
    ust = 0
    for k, v in bal.items():
        if v == 0 or k in EQUITY_ACCOUNTS:
            continue
        x = int(k) if k.isdigit() else -1
        if is_pl(k):
            if x < 4800: p["umsatz"] -= v
            elif x < 5000: p["sbe"] -= v
            elif x < 6000: p["material"] += v
            elif x < 6200: p["personal"] += v
            elif x < 6300: p["abAV"] += v
            elif k == "6960": p["abUV"] += v
            elif x < 7000: p["sba"] += v
            elif x < 7300: p["zinsE"] -= v
            else: p["zinsA"] += v
            continue
        if k in UST_ACCOUNTS:
            ust += v
            continue
        if v > 0:
            if 1200 <= x < 1300: p["ford"] += v
            elif 1800 <= x < 1900: p["bank"] += v
            else: p["sonstVG"] += v
            continue
        if 3000 <= x < 3100: p["rueck"] -= v
        elif 3250 <= x < 3300: p["anz"] -= v
        elif 3300 <= x < 3400: p["lul"] -= v
        else:
            p["sonstV"] -= v
            if k == "3730": p["steuern"] -= v
            if k == "3720": p["lohn"] -= v
    if ust < 0:
        p["sonstV"] -= ust
        p["steuern"] -= ust
    else:
        p["sonstVG"] += ust
    p["erg"] = p["umsatz"] + p["sbe"] - p["material"] - p["personal"] - p["abAV"] - p["abUV"] - p["sba"] \
        + p["zinsE"] - p["zinsA"]
    p["gez"], p["ausst"] = share_capital, outstanding
    p["vv"] = -(bal.get("2978", 0) + bal.get("2970", 0))
    p["ek"] = p["gez"] - p["ausst"] + p["vv"] + p["erg"]
    p["verb"] = p["lul"] + p["anz"] + p["sonstV"]
    p["aktiva"] = p["ford"] + p["sonstVG"] + p["bank"]
    p["passiva"] = p["ek"] + p["rueck"] + p["verb"]
    if p["aktiva"] != p["passiva"]:
        raise KontorError(f"Bilanz geht nicht auf: Aktiva {p['aktiva']} / Passiva {p['passiva']} Cent "
                          "(Stammkapital und ausstehende Einlage in den Stammdaten prüfen).")
    return p
