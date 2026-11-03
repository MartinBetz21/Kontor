"""Lohn-PDFs: Entgeltbescheinigung je festgeschriebener Abrechnung (Entgeltbescheinigungsverordnung, § 108 GewO)
und Lohnkonto je Mitarbeiter und Jahr (§ 41 EStG, § 4 LStDV). PDF/A-3b wie die Rechnungen."""
from __future__ import annotations

from weasyprint import HTML

from ..core.money import fmt_eur
from ..core.snapshot import date_de
from .pdf import ASSETS, _env

MONATE = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November",
          "Dezember")


def monat_de(monat: str) -> str:
    return f"{MONATE[int(monat[5:]) - 1]} {monat[:4]}"


_env.filters.update(monat_de=monat_de, eur0=lambda c: fmt_eur(c) if c else "–")


def _pdf(template: str, **kw) -> bytes:
    html = _env.get_template(template).render(css_uri=(ASSETS / "document.css").as_uri(), **kw)
    return HTML(string=html, base_url=str(ASSETS)).write_pdf(pdf_variant="pdf/a-3b")


def entgeltbescheinigung(a: dict, arbeitgeber: dict) -> bytes:
    return _pdf("entgeltbescheinigung.html", a=a, ag=arbeitgeber)


def lohnkonto(k: dict) -> bytes:
    return _pdf("lohnkonto.html", k=k)


def lohnsteuerbescheinigung(b: dict, arbeitgeber: dict, uebermittelt: dict | None = None, km_id: str = "") -> bytes:
    """Ausdruck für den Arbeitnehmer (§ 41b Abs. 1 Satz 3 EStG); ERiC druckt die Bescheinigung nicht."""
    return _pdf("lohnsteuerbescheinigung.html", b=b, ag=arbeitgeber, uebermittelt=uebermittelt, km_id=km_id)
