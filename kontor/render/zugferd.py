"""ZUGFeRD 2.x, Profil EN 16931: XML aus dem Schnappschuss bauen; Eingangs-XML (CII) lesen."""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pypdf
from drafthorse.models.accounting import ApplicableTradeTax, CategoryTradeTax, TradeAllowanceCharge
from drafthorse.models.document import Document
from drafthorse.models.note import IncludedNote
from drafthorse.models.party import TaxRegistration
from drafthorse.models.payment import PaymentMeans, PaymentTerms
from drafthorse.models.tradelines import LineItem
from lxml import etree

PROFILE = "urn:cen.eu:en16931:2017"
TYPE_CODES = {"rechnung": "380", "abschlag": "380", "schluss": "380", "storno": "381"}
UNIT_CODES = {"psch.": "LS", "pauschal": "LS", "monat": "MON", "monate": "MON", "std.": "HUR",
              "stunde": "HUR", "stunden": "HUR", "tag": "DAY", "tage": "DAY", "stück": "H87"}
NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}


def unit_code(unit: str) -> str:
    return UNIT_CODES.get((unit or "").strip().lower(), "C62")


def _eur(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


def _d(iso: str) -> date:
    return date.fromisoformat(iso)


# Steuerart im Ausgang → Kategorie (BT-151) und Befreiungsgrund (BT-120/121), Spec 18.4.
CATEGORY = {"inland": "S", "drittland_b2b": "O", "eu_b2b_rc": "AE"}
EXEMPTION = {"O": ("VATEX-EU-O", "Nicht im Inland steuerbare Leistung (§ 3a Abs. 2 UStG)"),
             "AE": ("VATEX-EU-AE", "Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge)")}


def _set_category(tax, category: str, rate: str) -> None:
    tax.type_code, tax.category_code = "VAT", category
    if category != "O":                     # „nicht steuerbar“ hat keinen Steuersatz (BR-O-5 ff.)
        tax.rate_applicable_percent = Decimal(rate)


def build_xml(snap: dict) -> bytes:
    """Storno: alle Beträge positiv, Typ 381, Verweis auf die Originalrechnung (BG-3)."""
    sign = -1 if snap["kind"] == "storno" else 1
    category = CATEGORY[snap.get("tax_treatment") or "inland"]
    doc = Document()
    doc.context.guideline_parameter.id = PROFILE
    doc.header.id = snap["number"]
    doc.header.type_code = TYPE_CODES[snap["kind"]]
    doc.header.issue_date_time = _d(snap["doc_date"])
    if snap.get("subject"):
        note = IncludedNote()
        note.content = snap["subject"]
        doc.header.notes.add(note)

    seller, buyer, t = snap["seller"], snap["buyer"], doc.trade
    s = t.agreement.seller
    # „nicht steuerbar“ (O) darf keine USt-IdNr. tragen, weder Verkäufer noch Käufer (BR-O-02); auf dem PDF bleibt sie.
    vat_ids = category != "O"
    if (not seller.get("vat_id") or not vat_ids) and seller.get("tax_number"):
        s.id = seller["tax_number"]   # BT-29: ohne USt-IdNr. verlangt BR-CO-26 eine Verkäuferkennung
    s.name =seller["name"] + (f" · {seller['trade_name']}" if seller.get("trade_name") else "")
    s.address.line_one = seller["street"]
    s.address.postcode = seller["postcode"]
    s.address.city_name = seller["city"]
    s.address.country_id = seller.get("country") or "DE"
    if seller.get("email"):
        s.electronic_address.uri_ID = ("EM", seller["email"])
    if seller.get("tax_number"):
        s.tax_registrations.add(TaxRegistration(id=("FC", seller["tax_number"])))
    if seller.get("vat_id") and vat_ids:
        s.tax_registrations.add(TaxRegistration(id=("VA", seller["vat_id"])))

    b = t.agreement.buyer
    b.name = buyer.get("company") or buyer.get("contact")
    b.address.line_one = buyer.get("street", "")
    b.address.postcode = buyer.get("postcode", "")
    b.address.city_name = buyer.get("city", "")
    b.address.country_id = buyer.get("country") or "DE"
    if buyer.get("vat_id") and vat_ids:
        b.tax_registrations.add(TaxRegistration(id=("VA", buyer["vat_id"])))
    if buyer.get("leitweg_id"):
        t.agreement.buyer_reference = buyer["leitweg_id"]

    if snap["kind"] == "abschlag":
        pass        # Anzahlung: kein Liefer- oder Leistungsdatum, auch kein voraussichtliches (Spec 6.4)
    elif snap.get("service_from") and snap.get("service_to"):
        t.settlement.period.start = _d(snap["service_from"])
        t.settlement.period.end = _d(snap["service_to"])
    else:
        t.delivery.event.occurrence = _d(snap.get("service_date") or snap["doc_date"])

    for line in snap["lines"]:
        li = LineItem()
        li.document.line_id = str(line["position"])
        li.product.name = line["title"]
        if line.get("description"):
            li.product.description = line["description"]
        li.agreement.net.amount = _eur(sign * line["unit_price"])
        li.delivery.billed_quantity = (Decimal(line["quantity"]), unit_code(line.get("unit", "")))
        _set_category(li.settlement.trade_tax, category, line["tax_rate"])
        li.settlement.monetary_summation.total_amount = _eur(sign * line["total"])
        t.items.add(li)

    allowance_total = charge_total = 0
    for a in snap["allowances"]:
        amount = sign * a["amount"]
        ac = TradeAllowanceCharge()
        ac.indicator = amount > 0          # True = Zuschlag, False = Abzug
        ac.actual_amount = _eur(abs(amount))
        ac.reason = a["label"]
        tax = CategoryTradeTax()
        _set_category(tax, category, a["tax_rate"])
        ac.trade_tax.add(tax)
        t.settlement.allowance_charge.add(ac)
        if amount > 0:
            charge_total += amount
        else:
            allowance_total += -amount

    t.settlement.currency_code = "EUR"
    if snap["kind"] == "storno" and snap.get("source_number"):
        t.settlement.invoice_referenced_document.issuer_assigned_id = snap["source_number"]
    if seller.get("iban"):
        pm = PaymentMeans()
        pm.type_code = "58"  # SEPA-Überweisung
        pm.payee_account.iban = seller["iban"].replace(" ", "")
        pm.payee_account.account_name = seller["name"]
        t.settlement.payment_means.add(pm)
    for g in snap["totals"]["groups"]:
        tax = ApplicableTradeTax()
        _set_category(tax, category, g["rate"])
        if category in EXEMPTION:
            tax.exemption_reason_code, tax.exemption_reason = EXEMPTION[category]
        tax.basis_amount = _eur(sign * g["basis"])
        tax.calculated_amount = _eur(sign * g["tax"])
        t.settlement.trade_tax.add(tax)
    if snap.get("due_date"):
        terms = PaymentTerms()
        terms.due = _d(snap["due_date"])
        t.settlement.terms.add(terms)

    tot = snap["totals"]
    prepaid = sign * sum(p["gross"] for p in snap.get("prepaid", []))
    m = t.settlement.monetary_summation
    m.line_total = _eur(sign * tot["lines_net"])
    m.charge_total = _eur(charge_total)
    m.allowance_total = _eur(allowance_total)
    m.tax_basis_total = _eur(sign * tot["net"])
    m.tax_total = (_eur(sign * tot["tax"]), "EUR")
    m.grand_total = _eur(sign * tot["gross"])
    m.prepaid_total = _eur(prepaid)
    m.due_amount = _eur(sign * tot["gross"] - prepaid)
    return doc.serialize(schema="FACTUR-X_EN16931")


def read_xml(xml: bytes) -> dict:
    """ZUGFeRD/Factur-X (CII) → Belegvorschlag in Cent. Gutschriften (381) negativ. UBL-XRechnung: ValueError."""
    root = etree.fromstring(xml)
    if root.tag != f"{{{NS['rsm']}}}CrossIndustryInvoice":
        raise ValueError("Keine ZUGFeRD/Factur-X-Rechnung (CII). UBL-XRechnung bitte als PDF erfassen.")

    def text(path: str) -> str:
        found = root.xpath(path, namespaces=NS)
        return found[0].text.strip() if found and found[0].text else ""

    def cents(path: str) -> int:
        raw = text(path)
        return int((Decimal(raw) * 100).quantize(Decimal("1"))) if raw else 0

    sign = -1 if text("rsm:ExchangedDocument/ram:TypeCode") == "381" else 1
    raw_date = text("rsm:ExchangedDocument/ram:IssueDateTime/udt:DateTimeString")
    summary = "//ram:SpecifiedTradeSettlementHeaderMonetarySummation/"
    rates = root.xpath("//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax/ram:RateApplicablePercent",
                       namespaces=NS)
    return {
        "supplier": text("//ram:SellerTradeParty/ram:Name"),
        "receipt_number": text("rsm:ExchangedDocument/ram:ID"),
        "receipt_date": f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) == 8 else None,
        "net": sign * cents(summary + "ram:TaxBasisTotalAmount"),
        "tax": sign * cents(summary + "ram:TaxTotalAmount"),
        "gross": sign * cents(summary + "ram:GrandTotalAmount"),
        "tax_rates": sorted({format(Decimal(r.text).normalize(), "f") for r in rates}),
    }


def extract_xml(pdf: bytes) -> bytes | None:
    reader = pypdf.PdfReader(io.BytesIO(pdf))
    for name, blobs in reader.attachments.items():
        if name.lower() in ("factur-x.xml", "zugferd-invoice.xml", "xrechnung.xml"):
            return blobs[0]
    return None
