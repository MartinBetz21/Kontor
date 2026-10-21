"""Pflichtangaben vor dem Abschließen (§ 14 UStG für Rechnungen, § 35a GmbHG, Steuerart im Ausgang)."""
from __future__ import annotations

from .tax_codes import EU_COUNTRIES

SELLER_REQUIRED = (("name", "Name"), ("street", "Straße"), ("postcode", "PLZ"), ("city", "Ort"))
BUYER_REQUIRED = (("street", "Straße"), ("postcode", "PLZ"), ("city", "Ort"))


def finalize_problems(doc: dict, settings: dict, customer: dict | None, profile: dict | None = None) -> list[str]:
    problems = []
    if (profile or {}).get("gewinnermittlung") == "bilanz":
        missing = [label for key, label in (("register_court", "Registergericht"), ("register_number", "Handelsregisternummer"),
                                            ("managing_directors", "Geschäftsführer"), ("seat", "Sitz"))
                   if not settings.get(key)]
        if missing:
            problems.append(f"Einstellungen: {', '.join(missing)} fehlt (Handelsregister-Angaben, § 35a GmbHG)")
    if not doc["lines"]:
        problems.append("keine Position")
    if not customer:
        problems.append("kein Kunde gewählt")
    for key, label in SELLER_REQUIRED:
        if not settings.get(key):
            problems.append(f"Einstellungen: {label} fehlt")
    if doc["kind"] == "angebot":
        return problems
    if not (settings.get("tax_number") or settings.get("vat_id") or settings.get("tax_number_pending")):
        problems.append("Steuernummer oder USt-IdNr. fehlt in den Einstellungen "
                        "(oder „Steuernummer wird nachgeliefert“ setzen)")
    for key, label in BUYER_REQUIRED:
        if customer is not None and not customer.get(key):
            problems.append(f"Kundenanschrift: {label} fehlt")
    # Abschlag: Leistung noch nicht erbracht, § 14 Abs. 4 Nr. 6 UStG verlangt kein Leistungsdatum (Spec 6.4)
    if doc["kind"] != "abschlag" and not (doc.get("service_date") or (doc.get("service_from") and doc.get("service_to"))):
        problems.append("Leistungsdatum oder -zeitraum fehlt")
    if doc["gross"] == 0:
        problems.append("Summe ist 0")
    problems += _treatment_problems(doc.get("tax_treatment") or "inland", settings, customer or {})
    return problems


def _treatment_problems(treatment: str, settings: dict, customer: dict) -> list[str]:
    country = (customer.get("country") or "DE").upper()
    if treatment == "drittland_b2b":
        out = []
        if country in EU_COUNTRIES:
            out.append(f"Steuerart Drittland: Der Kunde sitzt in {country}, also nicht außerhalb der EU")
        if not customer.get("company"):
            out.append("Steuerart Drittland: nur an Unternehmen (Firmenname fehlt)")
        return out
    if treatment == "eu_b2b_rc":
        out = []
        if country == "DE" or country not in EU_COUNTRIES:
            out.append(f"Steuerart EU Reverse Charge: Der Kunde sitzt in {country}, nicht in einem anderen EU-Land")
        if not customer.get("vat_id"):
            out.append("Steuerart EU Reverse Charge: USt-IdNr. des Kunden fehlt")
        if not settings.get("vat_id"):
            out.append("Steuerart EU Reverse Charge: eigene USt-IdNr. fehlt in den Einstellungen")
        return out
    return []


def finalize_warnings(doc: dict, settings: dict) -> list[str]:
    warnings = []
    if doc["kind"] != "angebot" and not (settings.get("tax_number") or settings.get("vat_id")):
        warnings.append("Ohne Steuernummer ist die Rechnung nach § 14 UStG unvollständig: Der Kunde kann die Vorsteuer "
                        "erst mit einer berichtigten Rechnung ziehen, und eine E-Rechnungs-Datei entfällt. Sobald die "
                        "Nummer da ist: stornieren und neu stellen.")
    if doc["kind"] != "angebot" and not settings.get("iban"):
        warnings.append("Keine Bankverbindung in den Einstellungen: die Rechnung nennt kein Konto.")
    return warnings
