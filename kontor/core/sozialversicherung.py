"""Sozialversicherungsbeiträge einer Lohnabrechnung: KV, RV, AV, PV, Umlagen, Minijob, Übergangsbereich.

Alle Beträge sind ganze Cent, alle Sätze in Prozent (Decimal). Die Zahlen je Jahr stehen mit Quelle in
`sv_werte.json`; hier steht nur die Rechenregel, jeweils mit ihrer Rechtsgrundlage am Ort der Anwendung.

Unterstützt (alles andere wirft KontorError, nie ein stilles Falschrechnen):

Personengruppe (DEÜV-Schlüssel)
  101  Beschäftigte ohne Besonderheit; Übergangsbereich möglich
  102  Auszubildende; kein Übergangsbereich (§ 20 Abs. 2a Satz 8 SGB IV), Geringverdienergrenze § 20 Abs. 3 SGB IV
  106  Werkstudenten (Beitragsgruppe meist 0100); Übergangsbereich wie 101
  109  geringfügig entlohnt (Minijob): nur Pauschalbeiträge und Umlagen
  110  kurzfristig beschäftigt: keine Beiträge, nur Umlagen; art = "keine"
  190  Studenten im vorgeschriebenen Zwischenpraktikum: alles null, auch keine Umlagen

Beitragsgruppe (4 Ziffern KV/RV/AV/PV; Schlüsselverzeichnis nach DEÜV, lohn-info.de/beitragsgruppenschluessel.html)
  KV  0 kein Beitrag (privat oder Selbstzahler; mit pkv_beitrag gibt es den Zuschuss § 257 Abs. 2 SGB V)
      1 allgemeiner Beitrag (14,6 % + Zusatzbeitrag, § 241 SGB V)   3 ermäßigter Beitrag (14,0 %, § 243 SGB V)
      6 Pauschalbeitrag 13 % (nur 109, § 249b SGB V)
      9 Firmenzahler, freiwillig versichert: gerechnet wie 1; kv_ag ist dann der Beitragszuschuss (§ 257 Abs. 1 SGB V)
  RV  0 kein Beitrag   1 voller Beitrag (bei 109: Aufstockung)   3 halber Beitrag, nur Arbeitgeberanteil (§ 172 Abs. 1 SGB VI)
      5 Pauschalbeitrag 15 % (nur 109, § 172 Abs. 3 SGB VI)
  AV  0 kein Beitrag   1 voller Beitrag   2 halber Beitrag, nur Arbeitgeberanteil (§ 346 Abs. 3 SGB III)
  PV  0 kein Beitrag   1 voller Beitrag   2 halber Beitragssatz (Beihilfeberechtigte, § 55 Abs. 1 Satz 3 SGB XI)

Nicht unterstützt, mit KontorError: andere Personengruppen und Ziffern (Altersteilzeit, Kurzarbeit, Knappschaft,
Landwirtschaft, Privathaushalt-Minijob), Märzklausel (§ 23a Abs. 4 SGB IV; wird nur erkannt, wenn `monat` übergeben
wird), PKV-Zuschuss im Übergangsbereich, Mehrfachbeschäftigung (der Aufrufer muss Entgelte vorher zusammenrechnen).
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from .errors import KontorError
from .money import percent_of, round_cents

_DATEI = Path(__file__).with_name("sv_werte.json")

_REGULAER = {"101", "102", "106"}
_UEBERGANG_OK = {"101", "106"}       # Auszubildende ausgenommen: § 20 Abs. 2a Satz 8 SGB IV
_KV, _RV, _AV, _PV = {"0", "1", "3", "6", "9"}, {"0", "1", "3", "5"}, {"0", "1", "2"}, {"0", "1", "2"}

_SCHLUESSEL = ("kv_an", "kv_ag", "rv_an", "rv_ag", "av_an", "av_ag", "pv_an", "pv_ag", "u1", "u2", "insolvenz",
               "kv_pauschal", "rv_pauschal", "pauschsteuer_basis", "pkv_zuschuss", "pv_zuschuss")


@lru_cache(maxsize=1)
def _alle() -> dict:
    return json.loads(_DATEI.read_text(encoding="utf-8"))


def werte(jahr: int) -> dict:
    """Der Jahresblock aus sv_werte.json: je Schlüssel {"wert": …, "quelle": …}."""
    block = _alle().get(str(jahr))
    if not isinstance(block, dict):
        raise KontorError(f"Für {jahr} liegen keine Sozialversicherungswerte vor (sv_werte.json hat "
                          f"{', '.join(k for k in _alle() if k.isdigit())}). Jeden Wert mit Quelle ergänzen.")
    return block


def _p(w: dict, key: str) -> Decimal:
    return Decimal(str(w[key]["wert"]))


def _c(w: dict, key: str) -> int:
    return int(w[key]["wert"])


def _halb(bem: int, satz: Decimal) -> int:
    """Halber Satz auf die Bemessung, gerundet (BVV § 2 Abs. 1 Satz 1)."""
    return percent_of(bem, satz / 2)


def _anteil(betrag: int | Decimal, tage: int) -> int:
    """Monatsbetrag für `tage` von 30 SV-Tagen (§ 1 Abs. 1 BVV: Monat = 30 Tage)."""
    return round_cents(Decimal(betrag) * tage / 30)


def _bem(lauf: int, einmal: int, bbg_m: int, bbg_j: int, tage: int, bisher: int, bisher_tage: int) -> tuple[int, int, bool]:
    """Beitragspflichtiges laufendes und einmaliges Entgelt gegen die Beitragsbemessungsgrenze.

    Laufend: Monats-BBG, bei Teilmonaten tagesanteilig (§ 1 Abs. 1 BVV, § 341 Abs. 3 SGB III).
    Einmalig: nur soweit das bisherige beitragspflichtige Entgelt die anteilige Jahres-BBG nicht erreicht
    (§ 23a Abs. 3 SGB IV); anteilige BBG = Jahres-BBG × SV-Tage bis einschließlich dieses Monats / 360.
    `bisher` wird je Zweig auf die anteilige BBG der bisherigen Tage gekappt; wer in früheren Monaten über der
    Monats-BBG lag, bekommt hier eine zu hohe Vorlast und damit ein zu kleines Einmal-Entgelt. Dafür die
    kappten Werte übergeben. Letztes Element: wurde das Einmal-Entgelt abgeschnitten?
    """
    l = min(lauf, _anteil(bbg_m, tage))
    if einmal == 0:
        return l, 0, False
    anteil = round_cents(Decimal(bbg_j) * (bisher_tage + tage) / 360)
    vorlast = min(bisher, round_cents(Decimal(bbg_j) * bisher_tage / 360))
    e = min(einmal, max(0, anteil - vorlast - l))
    return l, e, e < einmal


def _uebergang_be(ae: Decimal, g: int, og: int, f: Decimal) -> tuple[int, int]:
    """BE für den Gesamtbeitrag (§ 20 Abs. 2a Satz 1 SGB IV) und für den Arbeitnehmeranteil (Satz 6), in Cent.

    BE    = F·G + (OG/(OG−G) − G/(OG−G)·F) · (AE − G)      für G < AE ≤ OG
    BE_AN = OG/(OG−G) · (AE − G)
    Bis zur Geringfügigkeitsgrenze: BE = F · AE, der Arbeitgeber trägt allein (§ 2 Abs. 2 Satz 2 und 5 BVV).
    Auf Cent gerundet wie in den Tabellen des Gemeinsamen Rundschreibens (Ziffer 4.3.2.2/4.3.2.3).
    """
    if ae <= g:
        return round_cents(f * ae), 0
    d = Decimal(og - g)
    be = f * g + (Decimal(og) / d - Decimal(g) / d * f) * (ae - g)
    be_an = Decimal(og) / d * (ae - g)
    return round_cents(be), round_cents(be_an)


def _pv_saetze(w: dict, digit: str, sachsen: bool) -> tuple[Decimal, Decimal, Decimal]:
    """(Gesamtsatz, Arbeitnehmersatz ohne Zuschlag/Abschlag, Arbeitgebersatz) der PV in Prozent.

    Beschäftigte in Sachsen tragen 1 Punkt allein (§ 58 Abs. 3 Satz 1 SGB XI), bei halbem Satz 0,5 Punkte
    (Satz 2); der Rest wird hälftig geteilt. 3,6 % → AN 2,3 / AG 1,3 (BMG, lohn-info 2025/2026).
    """
    r = _p(w, "pv") if digit == "1" else _p(w, "pv") / 2
    s = (_p(w, "pv_sachsen_an_allein") if digit == "1" else _p(w, "pv_sachsen_an_allein_halber_satz")) if sachsen else Decimal(0)
    ag = (r - s) / 2
    return r, ag + s, ag


def _pv(w, digit, sachsen, be, be_an, zuschlag, abschlag, uebergang) -> tuple[int, int]:
    """PV-Anteile (AN, AG) nach BVV § 2: ungleich getragene Beiträge werden je Anteil gerundet.

    Regulär: AN = AN-Satz·E, AG = AG-Satz·E, je gerundet; Kinderlosenzuschlag und Kinderabschlag (§ 55 Abs. 3
    SGB XI) nur beim Arbeitnehmer, je eigens gerundet (BVV § 2 Abs. 1 Satz 4).
    Übergangsbereich: Gesamtbeitrag = 2 · gerundeter halber Satz auf BE; AN = AN-Satz auf BE_AN; AG = Rest;
    Zuschlag auf BE (Satz 1), Abschlag auf BE_AN (Satz 6), nicht in AG (BVV § 2 Abs. 2 Satz 1, 3, 4, 6, 7;
    Rundschreiben Ziffer 4.3.3.1 und Beispiele 8 und 9).
    """
    r, an_satz, ag_satz = _pv_saetze(w, digit, sachsen)
    if uebergang:
        gesamt = 2 * percent_of(be, r / 2)
        an = percent_of(be_an, an_satz)
        ag = gesamt - an
    else:
        an, ag = percent_of(be, an_satz), percent_of(be, ag_satz)
    an += (percent_of(be, zuschlag) if zuschlag else 0) - (percent_of(be_an, abschlag) if abschlag else 0)
    return an, ag


def _zweig(satz: Decimal, digit_halb_ag: bool, be: int, be_an: int) -> tuple[int, int]:
    """(AN, AG) für KV/RV/AV: halber Satz, gerundet und verdoppelt (BVV § 2 Abs. 1 Satz 1, Abs. 2 Satz 1/3/4).

    Regulär ist be_an = be, dann gleiche Hälften. Im Übergangsbereich AN auf BE_AN, AG = Gesamtbeitrag − AN.
    `digit_halb_ag`: nur Arbeitgeberanteil (RV 3, AV 2): halber Satz auf BE (§ 172 Abs. 1 SGB VI, Rundschreiben 4.3.3.1).
    """
    if digit_halb_ag:
        return 0, _halb(be, satz)
    an = _halb(be_an, satz)
    return an, 2 * _halb(be, satz) - an


def _pruefe(entgelt, einmal, sv_tage, beitragsgruppe, personengruppe, kv_zusatz, pv_kinder, pv_kinderlos, u1, u2,
            bisher_entgelt, bisher_sv_tage, bisher_einmal, pkv_beitrag, pkv_pv_beitrag, monat):
    for name, v in (("entgelt", entgelt), ("einmal", einmal), ("bisher_entgelt", bisher_entgelt),
                    ("bisher_einmal", bisher_einmal), ("pkv_beitrag", pkv_beitrag), ("pkv_pv_beitrag", pkv_pv_beitrag),
                    ("pv_kinder", pv_kinder), ("bisher_sv_tage", bisher_sv_tage), ("sv_tage", sv_tage)):
        if isinstance(v, bool) or not isinstance(v, int):
            raise KontorError(f"{name} muss eine ganze Zahl sein (Cent bzw. Tage), nicht {v!r}")
        if v < 0:
            raise KontorError(f"{name} darf nicht negativ sein: {v}")
    if not 1 <= sv_tage <= 30:
        raise KontorError(f"sv_tage muss zwischen 1 und 30 liegen, nicht {sv_tage}")
    if bisher_sv_tage + sv_tage > 360:
        raise KontorError("Mehr als 360 SV-Tage im Jahr")
    if (bisher_entgelt or bisher_einmal) and not bisher_sv_tage:
        raise KontorError("bisher_entgelt ohne bisher_sv_tage: die anteilige Jahres-BBG braucht die Tage")
    if not re.fullmatch(r"\d{4}", str(beitragsgruppe)):
        raise KontorError(f"Beitragsgruppe muss vier Ziffern haben (KV RV AV PV), nicht {beitragsgruppe!r}")
    for z, erlaubt, name in zip(beitragsgruppe, (_KV, _RV, _AV, _PV), ("KV", "RV", "AV", "PV")):
        if z not in erlaubt:
            raise KontorError(f"Beitragsgruppe {beitragsgruppe}: Ziffer {z} für {name} wird nicht unterstützt "
                              f"(möglich: {', '.join(sorted(erlaubt))})")
    for name, v in (("kv_zusatz", kv_zusatz), ("u1", u1), ("u2", u2)):
        if not isinstance(v, Decimal) or not Decimal(0) <= v <= Decimal(20):
            raise KontorError(f"{name} muss ein Decimal-Prozentsatz zwischen 0 und 20 sein, nicht {v!r}")
    if pv_kinderlos and pv_kinder > 0:
        raise KontorError("Kinderlosenzuschlag und Kinder schließen sich aus (§ 55 Abs. 3 SGB XI)")
    if monat is not None and monat not in range(1, 13):
        raise KontorError(f"monat muss 1 bis 12 sein, nicht {monat!r}")
    if personengruppe not in _REGULAER | {"109", "110", "190"}:
        raise KontorError(f"Personengruppe {personengruppe!r} wird nicht unterstützt "
                          "(möglich: 101, 102, 106, 109, 110, 190)")


def _rechne(w: dict, *, entgelt: int, einmal: int = 0, beitragsgruppe: str = "1111", personengruppe: str = "101",
            kv_zusatz: Decimal = Decimal(0), pv_kinder: int = 0, pv_kinderlos: bool = False, sachsen: bool = False,
            u1: Decimal = Decimal(0), u2: Decimal = Decimal(0), sv_tage: int = 30,
            bisher_entgelt: int = 0, bisher_sv_tage: int = 0, bisher_einmal: int = 0,
            pkv_beitrag: int = 0, pkv_pv_beitrag: int = 0, rv_aufstockung: bool = True,
            monat: int | None = None, uebergangsbereich: bool | None = None) -> dict:
    _pruefe(entgelt, einmal, sv_tage, beitragsgruppe, personengruppe, kv_zusatz, pv_kinder, pv_kinderlos, u1, u2,
            bisher_entgelt, bisher_sv_tage, bisher_einmal, pkv_beitrag, pkv_pv_beitrag, monat)
    kv_d, rv_d, av_d, pv_d = beitragsgruppe
    r = dict.fromkeys(_SCHLUESSEL, 0)
    r.update(an_summe=0, ag_summe=0, art="keine", bemessung_kv=0, bemessung_rv=0, bemessung_an_kv=0, bemessung_an_rv=0,
             bemessung_umlage=0, bemessung_insolvenz=0)
    gesamt = entgelt + einmal

    if personengruppe == "190":
        return r

    # --- Kurzfristig: keine Beiträge, nur Umlagen vom Entgelt (Minijob-Zentrale, Beitragsübersicht) ---
    if personengruppe == "110":
        if beitragsgruppe != "0000":
            raise KontorError("Kurzfristig Beschäftigte (110) haben die Beitragsgruppe 0000")
        _umlagen(w, r, entgelt, gesamt, u1, u2)
        return _summen(r)

    # --- Minijob: Pauschalbeiträge auf das Arbeitsentgelt, Aufstockung RV ---
    if personengruppe == "109":
        if kv_d not in "06" or av_d != "0" or pv_d != "0" or rv_d not in "015":
            raise KontorError(f"Beitragsgruppe {beitragsgruppe} passt nicht zu einem Minijob (109); "
                              "erwartet 6500, 6100, 0500 oder 0100")
        if pkv_beitrag or pkv_pv_beitrag:
            raise KontorError("Beitragszuschuss zur privaten KV/PV gibt es im Minijob nicht")
        r["art"] = "minijob"
        r["bemessung_kv"] = r["bemessung_an_kv"] = gesamt
        r["pauschsteuer_basis"] = gesamt      # 2 % (§ 40a Abs. 2 EStG) rechnet der Aufrufer
        if kv_d == "6":
            r["kv_pauschal"] = percent_of(gesamt, _p(w, "minijob_kv_pauschal"))        # § 249b SGB V
        if rv_d != "0":
            r["rv_pauschal"] = pausch = percent_of(gesamt, _p(w, "minijob_rv_pauschal"))   # § 168 Abs. 1 Nr. 1b SGB VI
            basis = max(gesamt, _c(w, "minijob_rv_mindestbemessung")) if rv_d == "1" and rv_aufstockung else gesamt
            r["bemessung_rv"] = r["bemessung_an_rv"] = basis
            if rv_d == "1" and rv_aufstockung:
                # BVV § 2 Abs. 1 Satz 5: voller Satz auf max(Entgelt, 175 €), gerundet; AN = Beitrag − AG-Anteil.
                # Nicht halbiert: Arbeitgeber und Beschäftigter tragen hier nicht je zur Hälfte (Satz 3).
                r["rv_an"] = percent_of(basis, _p(w, "rv")) - pausch                       # § 163 Abs. 8 SGB VI
        _umlagen(w, r, entgelt, gesamt, u1, u2)
        return _summen(r)

    # --- Regulär beschäftigt (101, 102, 106) ---
    f, g, og = _p(w, "faktor_f"), _c(w, "geringfuegigkeitsgrenze"), _c(w, "uebergang_obergrenze")
    ae_lauf_m = Decimal(entgelt) * 30 / sv_tage        # Teilmonat auf einen Monat hochrechnen (Rundschreiben 4.3.2.5)
    in_uebergang = personengruppe in _UEBERGANG_OK and (
        uebergangsbereich is True or (uebergangsbereich is None and g < ae_lauf_m <= og))
    if uebergangsbereich is True and personengruppe not in _UEBERGANG_OK:
        raise KontorError(f"Personengruppe {personengruppe} kennt keinen Übergangsbereich")
    ae_m = ae_lauf_m + einmal
    if in_uebergang and ae_m > og:
        in_uebergang = False       # Einmalzahlung sprengt die Obergrenze: reguläre Berechnung (Rundschreiben 4.3.3.3, Bsp. 27)

    bbg_km, bbg_kj, bbg_rm, bbg_rj = (_c(w, k) for k in ("bbg_kv_monat", "bbg_kv_jahr", "bbg_rv_monat", "bbg_rv_jahr"))
    if in_uebergang:
        if pkv_beitrag or pkv_pv_beitrag:
            raise KontorError("Zuschuss zur privaten KV/PV im Übergangsbereich wird nicht unterstützt")
        r["art"] = "midijob"
        be_m, ban_m = _uebergang_be(ae_m, g, og, f)
        be_l_m, _ = _uebergang_be(ae_lauf_m, g, og, f)
        be, be_an = _anteil(be_m, sv_tage), _anteil(ban_m, sv_tage)
        be_lauf = _anteil(be_l_m, sv_tage)        # U1/U2 ohne Einmal-Entgelt (§ 7 Abs. 2 AAG, Rundschreiben 4.3.8, Bsp. 26)
        r["bemessung_kv"] = r["bemessung_rv"] = be
        r["bemessung_an_kv"] = r["bemessung_an_rv"] = be_an
        u_basis, i_basis = be_lauf, be
    else:
        r["art"] = "regulaer"
        kl, ke, kcut = _bem(entgelt, einmal, bbg_km, bbg_kj, sv_tage, bisher_entgelt + bisher_einmal, bisher_sv_tage)
        rl, re_, rcut = _bem(entgelt, einmal, bbg_rm, bbg_rj, sv_tage, bisher_entgelt + bisher_einmal, bisher_sv_tage)
        if monat in (1, 2, 3) and (kcut or rcut):
            raise KontorError("Märzklausel (§ 23a Abs. 4 SGB IV): das Einmal-Entgelt überschreitet die anteilige "
                              "Beitragsbemessungsgrenze und gehört womöglich ins Vorjahr; wird nicht gerechnet")
        be = be_an = kl + ke
        r["bemessung_kv"] = r["bemessung_an_kv"] = kl + ke
        r["bemessung_rv"] = r["bemessung_an_rv"] = rl + re_
        u_basis, i_basis = rl, rl + re_
    be_kv, be_kv_an, be_rv, be_rv_an = r["bemessung_kv"], r["bemessung_an_kv"], r["bemessung_rv"], r["bemessung_an_rv"]

    if kv_d in "139":
        satz = _p(w, "kv_allgemein" if kv_d != "3" else "kv_ermaessigt")
        # Zusatzbeitrag als eigener Beitrag, getrennt halbiert und gerundet (Rundschreiben 4.3.3.1, Bsp. 8)
        a1, g1 = _zweig(satz, False, be_kv, be_kv_an)
        a2, g2 = _zweig(kv_zusatz, False, be_kv, be_kv_an)
        r["kv_an"], r["kv_ag"] = a1 + a2, g1 + g2
    elif kv_d == "6":
        raise KontorError("Beitragsgruppe KV 6 (Pauschalbeitrag) gibt es nur im Minijob (Personengruppe 109)")
    if rv_d in "13":
        r["rv_an"], r["rv_ag"] = _zweig(_p(w, "rv"), rv_d == "3", be_rv, be_rv_an)
    elif rv_d == "5":
        raise KontorError("Beitragsgruppe RV 5 (Pauschalbeitrag) gibt es nur im Minijob (Personengruppe 109)")
    if av_d in "12":
        r["av_an"], r["av_ag"] = _zweig(_p(w, "av"), av_d == "2", be_rv, be_rv_an)
    if pv_d in "12":
        zuschlag = _p(w, "pv_kinderlosenzuschlag") if pv_kinderlos else Decimal(0)
        abschlag = _p(w, "pv_abschlag_je_kind") * min(max(pv_kinder - 1, 0), _c(w, "pv_abschlag_max_kinder"))
        r["pv_an"], r["pv_ag"] = _pv(w, pv_d, sachsen, be_kv, be_kv_an, zuschlag, abschlag, in_uebergang)

    # Auszubildende bis zur Geringverdienergrenze: Arbeitgeber trägt den Gesamtbeitrag allein (§ 20 Abs. 3 SGB IV);
    # der Kinderlosenzuschlag bleibt beim Beschäftigten (§ 58 Abs. 1 Satz 3 SGB XI).
    if personengruppe == "102" and entgelt <= _c(w, "azubi_geringverdiener") < gesamt:
        raise KontorError("Einmal-Entgelt hebt den Auszubildenden über die Geringverdienergrenze (§ 20 Abs. 3 Satz 2 "
                          "SGB IV): wird nicht gerechnet")
    if personengruppe == "102" and gesamt <= _c(w, "azubi_geringverdiener"):
        zuschlag_an = percent_of(be_kv, _p(w, "pv_kinderlosenzuschlag")) if pv_kinderlos and pv_d in "12" else 0
        for z in ("kv", "rv", "av"):
            r[z + "_ag"] += r[z + "_an"]
            r[z + "_an"] = 0
        basis_an = r["pv_an"] - zuschlag_an
        r["pv_ag"] += basis_an
        r["pv_an"] = zuschlag_an

    # Zuschuss zur privaten KV/PV (§ 257 Abs. 2 SGB V, § 61 Abs. 2 SGB XI): höchstens der Arbeitgeberanteil, den es
    # bei gesetzlicher Versicherung gäbe (halber Satz + halber durchschnittlicher Zusatzbeitrag), höchstens die
    # Hälfte des Beitrags. Abrunden, damit die Hälfte nie überschritten wird.
    if pkv_beitrag:
        if kv_d != "0":
            raise KontorError("pkv_beitrag nur mit Beitragsgruppe KV 0 (nicht gesetzlich versichert)")
        hoechst = round_cents(Decimal(be_kv) * (_p(w, "kv_allgemein") + _p(w, "kv_zusatz_durchschnitt")) / 200)
        r["pkv_zuschuss"] = min(pkv_beitrag // 2, hoechst)
    if pkv_pv_beitrag:
        if pv_d != "0":
            raise KontorError("pkv_pv_beitrag nur mit Beitragsgruppe PV 0 (nicht in der sozialen Pflegeversicherung)")
        _, _, ag_satz = _pv_saetze(w, "1", sachsen)
        r["pv_zuschuss"] = min(pkv_pv_beitrag // 2, percent_of(be_kv, ag_satz))

    _umlagen(w, r, u_basis, i_basis, u1, u2)
    return _summen(r)


def _umlagen(w: dict, r: dict, u_basis: int, i_basis: int, u1: Decimal, u2: Decimal) -> None:
    """U1/U2 vom laufenden Entgelt bis zur RV-BBG ohne Einmal-Entgelt (§ 7 Abs. 2 AAG), Insolvenzgeldumlage vom
    RV-Entgelt mit Einmal-Entgelt (§ 358 Abs. 2 SGB III); Gemeinsames Rundschreiben Übergangsbereich 4.3.8/4.3.9, Bsp. 26–28."""
    r["bemessung_umlage"], r["bemessung_insolvenz"] = u_basis, i_basis
    r["u1"], r["u2"] = percent_of(u_basis, u1), percent_of(u_basis, u2)
    r["insolvenz"] = percent_of(i_basis, _p(w, "insolvenzgeldumlage"))


def _summen(r: dict) -> dict:
    r["an_summe"] = r["kv_an"] + r["rv_an"] + r["av_an"] + r["pv_an"]
    # Arbeitgeber: Beitragsanteile, Pauschalbeiträge und Umlagen; ohne Zuschüsse zur PKV/PV und ohne Pauschsteuer
    r["ag_summe"] = (r["kv_ag"] + r["rv_ag"] + r["av_ag"] + r["pv_ag"] + r["kv_pauschal"] + r["rv_pauschal"]
                     + r["u1"] + r["u2"] + r["insolvenz"])
    return r


def beitraege(jahr: int, *, entgelt: int, einmal: int = 0, beitragsgruppe: str = "1111", personengruppe: str = "101",
              kv_zusatz: Decimal = Decimal(0), pv_kinder: int = 0, pv_kinderlos: bool = False, sachsen: bool = False,
              u1: Decimal = Decimal(0), u2: Decimal = Decimal(0), sv_tage: int = 30,
              bisher_entgelt: int = 0, bisher_sv_tage: int = 0, bisher_einmal: int = 0,
              pkv_beitrag: int = 0, pkv_pv_beitrag: int = 0, rv_aufstockung: bool = True,
              monat: int | None = None, uebergangsbereich: bool | None = None) -> dict:
    """Beiträge eines Abrechnungsmonats; Cent, flaches Dict (Schlüssel siehe `_SCHLUESSEL` plus Summen und Bemessungen).

    entgelt/einmal: laufendes und einmalig gezahltes SV-pflichtiges Entgelt des Monats. kv_zusatz: Zusatzbeitrag der
    Kasse in Prozent. pv_kinder: Zahl der Kinder unter 25 (Abschlag 0,25 Punkte je Kind ab dem 2., höchstens 4 Kinder);
    pv_kinderlos: Zuschlag 0,6 Punkte (Aufrufer prüft Alter ab 23 und Elterneigenschaft). u1/u2: Umlagesätze in Prozent
    (Minijob: Sätze der Minijob-Zentrale aus `werte(jahr)["umlage_u1_minijob"]`). sv_tage: SV-Tage (30 = voller Monat).
    bisher_*: bisheriges SV-Entgelt des Jahres bei diesem Arbeitgeber vor diesem Monat (nur für Einmal-Entgelt).
    monat: Abrechnungsmonat, nur zum Erkennen der Märzklausel. uebergangsbereich: None = nach dem laufenden Entgelt
    entscheiden; True/False erzwingen (der Aufrufer kennt das regelmäßige Entgelt, § 20 Abs. 2 SGB IV).

    Rückgabe: Beträge in Cent. Bei Minijobs stehen die Arbeitgeber-Pauschalen in kv_pauschal/rv_pauschal, nicht in
    kv_ag/rv_ag. an_summe = Summe der Arbeitnehmeranteile; ag_summe = Arbeitgeberanteile + Pauschalen + Umlagen (ohne
    pkv_zuschuss/pv_zuschuss und ohne Pauschsteuer). bemessung_*: beitragspflichtige Einnahmen nach BBG bzw.
    Übergangsbereich; bemessung_an_*: Grundlage des Arbeitnehmeranteils; bemessung_umlage/_insolvenz: Grundlagen der
    Umlagen. art: "regulaer" | "midijob" | "minijob" | "keine" (110 und 190).
    """
    return _rechne(werte(jahr), entgelt=entgelt, einmal=einmal, beitragsgruppe=beitragsgruppe,
                   personengruppe=personengruppe, kv_zusatz=kv_zusatz, pv_kinder=pv_kinder, pv_kinderlos=pv_kinderlos,
                   sachsen=sachsen, u1=u1, u2=u2, sv_tage=sv_tage, bisher_entgelt=bisher_entgelt,
                   bisher_sv_tage=bisher_sv_tage, bisher_einmal=bisher_einmal, pkv_beitrag=pkv_beitrag,
                   pkv_pv_beitrag=pkv_pv_beitrag, rv_aufstockung=rv_aufstockung, monat=monat,
                   uebergangsbereich=uebergangsbereich)
