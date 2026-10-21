"""E-Bilanz (§ 5b EStG) als ELSTER-Datensatz mit XBRL, Kerntaxonomie 6.8, Kleinstkapitalgesellschaft mit GuV nach
MicroBilG, Steuerbilanz ohne Abweichung zur Handelsbilanz (Spec 18.9). Rein, ohne IO: Salden und Stammdaten rein,
XML raus. Prüfen und Senden macht `eric.py`.

Konten werden je SKR04-Bereich einer Blattposition der Taxonomie zugeordnet; Summenpositionen rechnet der Generator
über die Rechenregeln der Taxonomie hoch. Jede Pflichtposition für Kapitalgesellschaften, zu der es keinen Wert gibt,
geht leer (`xsi:nil`) mit. Ab Wirtschaftsjahr 2025 kommt der Kontennachweis dazu."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from functools import cache
from pathlib import Path
from xml.sax.saxutils import escape

from ..core.bilanz import EQUITY_ACCOUNTS, UST_ACCOUNTS, is_pl, positions
from ..core.errors import KontorError

TAXONOMIE = "6.8"
GAAP_NS = "http://www.xbrl.de/taxonomies/de-gaap-ci-2024-04-01"
GCD_NS = "http://www.xbrl.de/taxonomies/de-gcd-2024-04-01"
TESTMERKER = "700000004"           # ERiC-Entwicklerhandbuch 6.5.5: Testfall, wird am Annahmeserver gelöscht


@cache
def taxonomie() -> dict[str, dict]:
    return json.loads((Path(__file__).parent / "taxonomie_6_8.json").read_text(encoding="utf-8"))


@dataclass
class Stammdaten:
    name: str
    strasse: str
    hausnummer: str
    plz: str
    ort: str
    steuernummer13: str            # ELSTER-Format, z. B. 5214012345670 (EricMakeElsterStnr)
    stammkapital: int              # Cent
    ausstehend: int                # nicht eingeforderte ausstehende Einlagen, Cent
    land: str = "Deutschland"


@dataclass
class Bericht:
    jahr: int
    salden: dict[str, int]                         # Konto → Saldo in Cent, Soll positiv (Schlusssalden des Jahres)
    namen: dict[str, str] = field(default_factory=dict)
    erstellt: date | None = None
    einlagen: int = 0                              # Einlagen und Ausschüttungen im Jahr (Betriebsvermögensvergleich)
    ausschuettungen: int = 0


# -- Zuordnung SKR04 → Blattposition -------------------------------------------------------------------------------
# Bereiche (von, bis) inklusive; die erste passende Zeile gewinnt. Einzelkonten stehen vor ihren Bereichen.

GUV = [
    ("4300", "4309", "is.netIncome.regular.operatingTC.grossTradingProfit.totalOutput.netSales.grossSales.reducedRateVAT"),
    ("4100", "4199", "is.netIncome.regular.operatingTC.grossTradingProfit.totalOutput.netSales.grossSales.taxExemptOther"),
    ("4310", "4399", "is.netIncome.regular.operatingTC.grossTradingProfit.totalOutput.netSales.grossSales.untaxable"),
    ("4000", "4799", "is.netIncome.regular.operatingTC.grossTradingProfit.totalOutput.netSales.grossSales.generalRateVAT"),
    ("4840", "4849", "is.netIncome.regular.operatingTC.otherOpRevenue.currGains"),
    ("4930", "4939", "is.netIncome.regular.operatingTC.otherOpRevenue.releaseProv"),
    ("4800", "4999", "is.netIncome.regular.operatingTC.otherOpRevenue.miscellaneous"),
    ("5920", "5959", "is.netIncome.regular.operatingTC.grossTradingProfit.materialServices.services.UStG13bDedInputTax"),
    ("5900", "5999", "is.netIncome.regular.operatingTC.grossTradingProfit.materialServices.services.OtherDedInputTax"),
    ("5000", "5899", "is.netIncome.regular.operatingTC.grossTradingProfit.materialServices.misc"),
    ("6024", "6024", "is.netIncome.regular.operatingTC.staff.salaries.managerPartner"),
    ("6000", "6099", "is.netIncome.regular.operatingTC.staff.salaries.misc"),
    ("6130", "6130", "is.netIncome.regular.operatingTC.staff.social.welfare"),
    ("6100", "6129", "is.netIncome.regular.operatingTC.staff.social.socExp.other"),
    ("6100", "6199", "is.netIncome.regular.operatingTC.staff.social.other"),
    ("6260", "6269", "is.netIncome.regular.operatingTC.deprAmort.fixAss.tan.lowValueAs"),
    ("6200", "6299", "is.netIncome.regular.operatingTC.deprAmort.fixAss.tan.other"),
    ("6960", "6960", "is.netIncome.regular.operatingTC.deprAmort.currAss.misc"),   # Bitcoin auf Niederstwert
    ("6310", "6319", "is.netIncome.regular.operatingTC.otherCost.leaseFix.other"),
    ("6400", "6449", "is.netIncome.regular.operatingTC.otherCost.insurance"),
    ("6500", "6599", "is.netIncome.regular.operatingTC.otherCost.vehicles"),
    ("6610", "6619", "is.netIncome.regular.operatingTC.otherCost.limitedDeductible.gifts.deductible"),
    ("6620", "6629", "is.netIncome.regular.operatingTC.otherCost.limitedDeductible.gifts.nondeductible"),
    ("6600", "6639", "is.netIncome.regular.operatingTC.otherCost.marketing"),
    ("6640", "6641", "is.netIncome.regular.operatingTC.otherCost.limitedDeductible.entertainment.deductible"),
    ("6644", "6644", "is.netIncome.regular.operatingTC.otherCost.limitedDeductible.entertainment.nonDeductible"),
    ("6650", "6689", "is.netIncome.regular.operatingTC.otherCost.employee"),   # GmbH: auch der Geschäftsführer
    ("6700", "6799", "is.netIncome.regular.operatingTC.otherCost.freight"),
    ("6800", "6814", "is.netIncome.regular.operatingTC.otherCost.communication"),
    ("6821", "6821", "is.netIncome.regular.operatingTC.otherCost.training"),
    ("6815", "6824", "is.netIncome.regular.operatingTC.otherCost.administration"),
    ("6825", "6834", "is.netIncome.regular.operatingTC.otherCost.legalConsulting"),
    ("6837", "6837", "is.netIncome.regular.operatingTC.otherCost.concessLicenses"),
    ("6835", "6839", "is.netIncome.regular.operatingTC.otherCost.leaseMoveable.other"),
    ("6840", "6849", "is.netIncome.regular.operatingTC.otherCost.leasingAll.moveable"),
    ("6880", "6889", "is.netIncome.regular.operatingTC.otherCost.currLoss"),
    ("6930", "6939", "is.netIncome.regular.operatingTC.otherCost.regAllowance"),
    ("6300", "6999", "is.netIncome.regular.operatingTC.otherCost.miscellaneous"),
    ("7000", "7299", "is.netIncome.regular.fin.netInterest.income.other"),
    ("7300", "7599", "is.netIncome.regular.fin.netInterest.expenses.other"),
    ("7680", "7699", "is.netIncome.otherTaxes"),
    ("7600", "7679", "is.netIncome.tax"),
]

AKTIV = [
    ("1200", "1299", "bs.ass.currAss.receiv.trade.other"),
    ("1800", "1899", "bs.ass.currAss.cashEquiv.bank"),
    ("0000", "9999", "bs.ass.currAss.receiv.other.other"),       # Bitcoin 1440 und übrige Sollsalden
]

PASSIV = [
    ("3000", "3099", "bs.eqLiab.accruals.other"),
    ("3250", "3299", "bs.eqLiab.liab.advPaym"),
    ("3300", "3399", "bs.eqLiab.liab.trade.genOther"),
    ("3510", "3519", "bs.eqLiab.liab.other.shareholders"),
    ("3720", "3729", "bs.eqLiab.liab.other.employees"),
    ("3730", "3739", "bs.eqLiab.liab.other.theroffTax"),
    ("3740", "3799", "bs.eqLiab.liab.other.thereoffSocSec"),
    ("0000", "9999", "bs.eqLiab.liab.other.other"),
]

UST_FORDERUNG = "bs.ass.currAss.receiv.other.vat"
UST_SCHULD = "bs.eqLiab.liab.other.theroffTax"
KAPITAL = "bs.eqLiab.equity.subscribed.corp"
AUSSTEHEND = "bs.eqLiab.equity.subscribed.unpaidCap"
VORTRAG = "bs.eqLiab.equity.retainedEarnings"
ERGEBNIS_BILANZ = "bs.eqLiab.equity.netIncome"
ERGEBNIS_GUV = "ismi.netIncome"


def _lookup(table: list[tuple[str, str, str]], konto: str) -> str:
    for lo, hi, concept in table:
        if len(konto) == 4 and lo <= konto <= hi:
            return concept
    raise KontorError(f"Konto {konto}: keine Position in der E-Bilanz-Zuordnung.")


def zuordnung(salden: dict[str, int]) -> tuple[dict[str, int], list[tuple[str, str, int]]]:
    """Blattpositionen mit Wert (Cent, im Vorzeichen der Taxonomie) und der Kontennachweis (Position, Konto, Betrag).
    Dieselbe Saldierung wie `core.bilanz.positions`: Umsatzsteuer und Vorsteuer netto, Sollsalden auf
    Verbindlichkeitskonten als sonstige Vermögensgegenstände."""
    leaves: dict[str, int] = {}
    nachweis: list[tuple[str, str, int]] = []

    def add(concept: str, konto: str, cents: int) -> None:
        leaves[concept] = leaves.get(concept, 0) + cents
        nachweis.append((concept, konto, cents))

    ust = 0
    for konto, v in sorted(salden.items()):
        if v == 0 or konto in EQUITY_ACCOUNTS:
            continue
        if is_pl(konto):
            concept = _lookup(GUV, konto)
            # Erträge stehen im Haben (negativ), Aufwendungen im Soll; die Taxonomie will beide positiv
            add(concept, konto, -v if konto < "5000" or "7000" <= konto < "7300" else v)
        elif konto in UST_ACCOUNTS:
            ust += v
            nachweis.append(("ust", konto, v))
        elif v > 0:
            add(_lookup(AKTIV, konto), konto, v)
        else:
            add(_lookup(PASSIV, konto), konto, -v)
    ust_konto = UST_FORDERUNG if ust > 0 else UST_SCHULD
    nachweis = [(ust_konto, k, v if ust > 0 else -v) if c == "ust" else (c, k, v) for c, k, v in nachweis]
    if ust:
        leaves[ust_konto] = leaves.get(ust_konto, 0) + abs(ust)
    return leaves, nachweis


def hochrechnen(leaves: dict[str, int]) -> dict[str, int]:
    """Summenpositionen über die Rechenregeln: Eltern = Σ Vorzeichen × Kind, bis zur Wurzel."""
    tax = taxonomie()
    for c in leaves:
        if c not in tax:
            raise KontorError(f"Position {c} fehlt in der Taxonomie {TAXONOMIE}.")
        if tax[c]["steuerlich_unzulaessig"] or not tax[c]["kst"]:
            raise KontorError(f"Position {c} ist in der Steuerbilanz einer Kapitalgesellschaft nicht zulässig.")
    total = dict(leaves)
    for c, v in leaves.items():
        node = c
        while tax[node]["eltern"]:
            v = v * tax[node]["vz"]
            node = tax[node]["eltern"]
            total[node] = total.get(node, 0) + v
    return total


def werte(b: Bericht, s: Stammdaten) -> tuple[dict[str, int], list[tuple[str, str, int]]]:
    leaves, nachweis = zuordnung(b.salden)
    p = positions(b.salden, s.stammkapital, s.ausstehend)
    erg = p["erg"]
    leaves[KAPITAL] = s.stammkapital
    if s.ausstehend:
        leaves[AUSSTEHEND] = s.ausstehend
    leaves[VORTRAG] = p["vv"]
    leaves[ERGEBNIS_BILANZ] = erg
    # Betriebsvermögensvergleich: Eigenkapital jetzt − Vorjahr − Einlagen + Ausschüttungen = Jahresergebnis.
    # Das Vorjahr ist das Eigenkapital vor dem Ergebnis des Jahres (Stammkapital unverändert, Vortrag aus 2978).
    leaves["BVV.profitLoss.assetsCurrentYear"] = p["ek"]
    leaves["BVV.profitLoss.assetsPreviousYear.assets"] = p["ek"] - erg - b.einlagen + b.ausschuettungen
    if b.einlagen:
        leaves["BVV.profitLoss.contribution"] = b.einlagen
    if b.ausschuettungen:
        leaves["BVV.profitLoss.withdrawalDistrib"] = b.ausschuettungen
    total = hochrechnen(leaves)
    checks = {"Bilanzsumme Aktiva": (total.get("bs.ass", 0), p["aktiva"]),
              "Bilanzsumme Passiva": (total.get("bs.eqLiab", 0), p["passiva"]),
              "Eigenkapital": (total.get("bs.eqLiab.equity", 0), p["ek"]),
              "Jahresergebnis GuV": (total.get(ERGEBNIS_GUV, 0), erg),
              "Betriebsvermögensvergleich": (total.get("BVV.profitLoss", 0), erg)}
    for label, (got, want) in checks.items():
        if got != want:
            raise KontorError(f"E-Bilanz {b.jahr}: {label} {got} ≠ Abschluss {want} Cent.")
    return total, nachweis


# -- XML ------------------------------------------------------------------------------------------------------------

def _eur(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def _gcd(b: Bericht, s: Stammdaten, kontennachweis: bool) -> str:
    y = b.jahr
    d = 'contextRef="D-AKTJAHR"'
    nil = lambda n: f'<de-gcd:{n} xsi:nil="true" {d}/>'
    val = lambda n, v: f"<de-gcd:{n} {d}>{escape(str(v))}</de-gcd:{n}>"
    flag = lambda n: f"<de-gcd:{n} {d}/>"
    elements = ["B", "GuVMicroBilG", "BVV"] + (["KS"] if kontennachweis else [])
    nil_elements = ["BAL", "EB", "EV", "GuV", "KKE", "SGE", "SGEP", "STU"]
    return "\n".join([
        val("genInfo.doc.id.generationDate", (b.erstellt or date.today()).isoformat()),
        "<de-gcd:genInfo.report.id.reportType>", flag("genInfo.report.id.reportType.reportType.JA"),
        "</de-gcd:genInfo.report.id.reportType>",
        "<de-gcd:genInfo.report.id.reportStatus>", flag("genInfo.report.id.reportStatus.reportStatus.E"),
        "</de-gcd:genInfo.report.id.reportStatus>",
        "<de-gcd:genInfo.report.id.revisionStatus>", flag("genInfo.report.id.revisionStatus.revisionStatus.E"),
        "</de-gcd:genInfo.report.id.revisionStatus>",
        "<de-gcd:genInfo.report.id.reportElement>",
        *[flag(f"genInfo.report.id.reportElement.reportElements.{e}") for e in elements],
        *[nil(f"genInfo.report.id.reportElement.reportElements.{e}") for e in nil_elements],
        "</de-gcd:genInfo.report.id.reportElement>",
        "<de-gcd:genInfo.report.id.statementType>", flag("genInfo.report.id.statementType.statementType.E"),
        "</de-gcd:genInfo.report.id.statementType>",
        "<de-gcd:genInfo.report.id.statementType.tax>", nil("genInfo.report.id.statementType.tax.statementTypeTax.GHB"),
        "</de-gcd:genInfo.report.id.statementType.tax>",
        val("genInfo.report.id.incomeStatementendswithBalProfit", "false"),
        "<de-gcd:genInfo.report.id.accountingStandard>", flag("genInfo.report.id.accountingStandard.accountingStandard.AO"),
        "</de-gcd:genInfo.report.id.accountingStandard>",
        "<de-gcd:genInfo.report.id.specialAccountingStandard>", flag("genInfo.report.id.specialAccountingStandard.K"),
        "</de-gcd:genInfo.report.id.specialAccountingStandard>",
        "<de-gcd:genInfo.report.id.incomeStatementFormat>",
        flag("genInfo.report.id.incomeStatementFormat.incomeStatementFormat.GKV"),
        "</de-gcd:genInfo.report.id.incomeStatementFormat>",
        "<de-gcd:genInfo.report.id.consolidationRange>", flag("genInfo.report.id.consolidationRange.consolidationRange.EA"),
        "</de-gcd:genInfo.report.id.consolidationRange>",
        val("genInfo.report.period.fiscalYearBegin", f"{y}-01-01"),
        val("genInfo.report.period.fiscalYearEnd", f"{y}-12-31"),
        val("genInfo.report.period.balSheetClosingDate", f"{y}-12-31"),
        val("genInfo.report.period.fiscalPreviousYearBegin", f"{y - 1}-01-01"),
        val("genInfo.report.period.fiscalPreviousYearEnd", f"{y - 1}-12-31"),
        val("genInfo.report.period.balSheetClosingDatePreviousYear", f"{y - 1}-12-31"),
        nil("genInfo.company.id.business"),
        "<de-gcd:genInfo.company.id.domesticBranchOfForeignCompany>",
        nil("genInfo.company.id.domesticBranchOfForeignCompany.other"),
        "</de-gcd:genInfo.company.id.domesticBranchOfForeignCompany>",
        "<de-gcd:genInfo.company.id.entityWithTaxablePurposeBusiness>",
        nil("genInfo.company.id.entityWithTaxablePurposeBusiness.normal"),
        "</de-gcd:genInfo.company.id.entityWithTaxablePurposeBusiness>",
        "<de-gcd:genInfo.company.id.incomeClassification>", flag("genInfo.company.id.incomeClassification.trade"),
        "</de-gcd:genInfo.company.id.incomeClassification>",
        "<de-gcd:genInfo.company.id.legalStatus>", val("genInfo.company.id.legalStatus.legalStatus.GMBH", "GmbH"),
        "</de-gcd:genInfo.company.id.legalStatus>",
        val("genInfo.company.id.name", s.name),
        val("genInfo.company.id.location", "Firmensitz"),
        val("genInfo.company.id.location.street", s.strasse),
        val("genInfo.company.id.location.houseNo", s.hausnummer),
        val("genInfo.company.id.location.zipCode", s.plz),
        val("genInfo.company.id.location.city", s.ort),
        val("genInfo.company.id.location.country", s.land),
        "<de-gcd:genInfo.company.id.idNo>", val("genInfo.company.id.idNo.type.companyId.ST13", s.steuernummer13),
        nil("genInfo.company.id.idNo.type.companyId.STID"), nil("genInfo.company.id.idNo.type.companyId.STWID"),
        val("genInfo.company.id.idNo.type.companyId.BF4", s.steuernummer13[:4]),
        "</de-gcd:genInfo.company.id.idNo>",
        nil("genInfo.company.id.optionKStGs1a"),
        "<de-gcd:genInfo.report.id.accordingTo>", nil("genInfo.report.id.accordingTo.name"),
        "<de-gcd:genInfo.report.id.accordingTo.idNo>", nil("genInfo.company.id.idNo.type.companyId.ST13"),
        nil("genInfo.company.id.idNo.type.companyId.BF4"), "</de-gcd:genInfo.report.id.accordingTo.idNo>",
        nil("genInfo.report.id.accordingTo.yearEnd"), "</de-gcd:genInfo.report.id.accordingTo>",
        # Gesellschafterangaben sind nur bei Personengesellschaften gefüllt; das Tupel muss trotzdem leer mit
        "<de-gcd:genInfo.company.id.shareholder>", nil("genInfo.company.id.shareholder.name"),
        nil("genInfo.company.id.shareholder.id"), "<de-gcd:genInfo.company.id.shareholder.group>",
        nil("genInfo.company.id.shareholder.group.limPartner"), "</de-gcd:genInfo.company.id.shareholder.group>",
        f'<de-gcd:genInfo.company.id.shareholder.currentnumber xsi:nil="true" {d} unitRef="PURE"/>',
        nil("genInfo.company.id.shareholder.taxnumber"), nil("genInfo.company.id.shareholder.taxid"),
        nil("genInfo.company.id.shareholder.WID"), "<de-gcd:genInfo.company.id.shareholder.legalStatus>",
        nil("genInfo.company.id.shareholder.legalStatus.legalStatus.KOER"),
        "</de-gcd:genInfo.company.id.shareholder.legalStatus>", "<de-gcd:genInfo.company.id.shareholder.ShareDivideKey>",
        nil("genInfo.company.id.shareholder.ShareDivideKey.numerator"),
        nil("genInfo.company.id.shareholder.ShareDivideKey.denominator"),
        "</de-gcd:genInfo.company.id.shareholder.ShareDivideKey>",
        nil("genInfo.company.id.shareholder.SpecialBalanceRequired"),
        nil("genInfo.company.id.shareholder.extensionRequired"), "</de-gcd:genInfo.company.id.shareholder>",
    ])


def _gaap(total: dict[str, int], nachweis: list[tuple[str, str, int]], namen: dict[str, str], jahr: int,
          kontennachweis: bool) -> str:
    tax = taxonomie()
    lines = []
    beginn, ende = f"{jahr}-01-01", f"{jahr}-12-31"
    gilt = lambda t: (not t["gueltig_ab"] or t["gueltig_ab"] <= beginn) and (not t["gueltig_bis"] or t["gueltig_bis"] >= ende)
    # ERiC verlangt jede Pflichtposition, auch die rechtsformfremden (dann leer). Tabellen mit Dimensionen
    # (table.*) bleiben draußen: Sie gehören zu Personengesellschaften und bräuchten eigene Kontexte.
    pflicht = {c for c, t in tax.items()
               if t["pflicht"] in ("M", "MK", "SM") and not c.startswith("table.") and gilt(t)}
    for c in sorted(set(total) | pflicht):
        t = tax[c]
        if t["abstrakt"]:
            continue
        ctx = "I-AKTJAHR" if t["stichtag"] else "D-AKTJAHR"
        unit = ' unitRef="EUR"' if t["geld"] else ""
        if c in total:
            lines.append(f'<de-gaap-ci:{c} decimals="2" contextRef="{ctx}"{unit}>{_eur(total[c])}</de-gaap-ci:{c}>')
        else:
            lines.append(f'<de-gaap-ci:{c} xsi:nil="true" contextRef="{ctx}"{unit}/>')
    # Anlagenspiegel: Die GmbH hat kein Anlagevermögen; ERiC verlangt trotzdem eine Angabe (leer genügt)
    lines.append('<de-gaap-ci:all xsi:nil="true" contextRef="D-BAL-AKTJAHR" unitRef="EUR"/>')
    if kontennachweis:
        d = 'contextRef="D-AKTJAHR"'
        for concept, konto, cents in nachweis:
            lines.append("<de-gaap-ci:detailedInformation.accountBalances>"
                         f"<de-gaap-ci:detailedInformation.accountbalances.positionName {d}>{concept}"
                         "</de-gaap-ci:detailedInformation.accountbalances.positionName>"
                         f"<de-gaap-ci:detailedInformation.accountbalances.accountNumber {d}>{konto}"
                         "</de-gaap-ci:detailedInformation.accountbalances.accountNumber>"
                         f"<de-gaap-ci:detailedInformation.accountbalances.accountDescription {d}>"
                         f"{escape(namen.get(konto) or konto)}</de-gaap-ci:detailedInformation.accountbalances.accountDescription>"
                         f'<de-gaap-ci:detailedInformation.accountbalances.amount decimals="2" {d} unitRef="EUR">'
                         f"{_eur(cents)}</de-gaap-ci:detailedInformation.accountbalances.amount>"
                         "</de-gaap-ci:detailedInformation.accountBalances>")
    return "\n".join(lines)


def datensatz(b: Bericht, s: Stammdaten, hersteller_id: str, test: bool, datenlieferant: str,
              version: str = "0.1") -> str:
    """Der vollständige ELSTER-Datensatz (Datenart Bilanz, Vorgang send-Auth). Mit `test` trägt er den Testmerker."""
    total, nachweis = werte(b, s)
    kn = b.jahr >= 2025                    # Kontennachweis ist ab Wirtschaftsjahr 2025 Pflicht
    y, st = b.jahr, s.steuernummer13
    ent = f'<xbrli:entity><xbrli:identifier scheme="http://www.rzf-nrw.de/Steuernummer">{st}</xbrli:identifier></xbrli:entity>'
    testmerker = f"<Testmerker>{TESTMERKER}</Testmerker>" if test else ""
    lieferant = escape(datenlieferant)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Elster xmlns="http://www.elster.de/elsterxml/schema/v11">
<TransferHeader version="11">
<Verfahren>ElsterBilanz</Verfahren><DatenArt>Bilanz</DatenArt><Vorgang>send-Auth</Vorgang>{testmerker}
<HerstellerID>{escape(hersteller_id)}</HerstellerID><DatenLieferant>{lieferant}</DatenLieferant>
<Datei><Verschluesselung>CMSEncryptedData</Verschluesselung><Kompression>GZIP</Kompression><TransportSchluessel/></Datei>
<VersionClient>Kontor {escape(version)}</VersionClient>
</TransferHeader>
<DatenTeil><Nutzdatenblock>
<NutzdatenHeader version="11"><NutzdatenTicket>{y}0001</NutzdatenTicket><Empfaenger id="F">{st[:4]}</Empfaenger>
<Hersteller><ProduktName>Kontor</ProduktName><ProduktVersion>{escape(version)}</ProduktVersion></Hersteller>
<DatenLieferant>{lieferant}</DatenLieferant></NutzdatenHeader>
<Nutzdaten>
<ebilanz:EBilanz version="000002" xmlns:ebilanz="http://rzf.fin-nrw.de/RMS/EBilanz/2016/XMLSchema">
<ebilanz:stichtag>{y}1231</ebilanz:stichtag>
<xbrli:xbrl xmlns:de-gcd="{GCD_NS}" xmlns:de-gaap-ci="{GAAP_NS}" xmlns:link="http://www.xbrl.org/2003/linkbase"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:xlink="http://www.w3.org/1999/xlink"
 xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
<link:schemaRef xlink:type="simple" xlink:href="{GCD_NS}/de-gcd-2024-04-01-shell.xsd"/>
<link:schemaRef xlink:type="simple" xlink:href="{GAAP_NS}/de-gaap-ci-2024-04-01-shell-fiscal-microbilg.xsd"/>
<xbrli:context id="I-AKTJAHR">{ent}<xbrli:period><xbrli:instant>{y}-12-31</xbrli:instant></xbrli:period></xbrli:context>
<xbrli:context id="D-AKTJAHR">{ent}<xbrli:period><xbrli:startDate>{y}-01-01</xbrli:startDate><xbrli:endDate>{y}-12-31</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:context id="D-BAL-AKTJAHR">{ent}<xbrli:period><xbrli:startDate>{y}-01-01</xbrli:startDate><xbrli:endDate>{y}-12-31</xbrli:endDate></xbrli:period>
<xbrli:scenario><xbrldi:explicitMember dimension="de-gaap-ci:dim_changes.nt.ass.gross">de-gaap-ci:bs.ass.fixAss</xbrldi:explicitMember>
<xbrldi:explicitMember dimension="de-gaap-ci:dim_taxTrans">de-gaap-ci:dim_taxBal</xbrldi:explicitMember></xbrli:scenario></xbrli:context>
<xbrli:unit id="EUR"><xbrli:measure>iso4217:EUR</xbrli:measure></xbrli:unit>
<xbrli:unit id="PURE"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>
{_gcd(b, s, kn)}
{_gaap(total, nachweis, b.namen, y, kn)}
</xbrli:xbrl>
</ebilanz:EBilanz>
</Nutzdaten>
</Nutzdatenblock></DatenTeil>
</Elster>
"""
