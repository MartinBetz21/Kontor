"""Schnappschuss → HTML → PDF/A-3b. Rechnungen bekommen die factur-x.xml eingebettet.
Entwürfe sind normale PDFs mit Wasserzeichen und ohne XML."""
from __future__ import annotations

from pathlib import Path

from drafthorse.pdf import attach_xml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

from ..core.errors import KontorError
from ..core.money import fmt_eur, fmt_qty
from ..core.snapshot import date_de
from ..core.status import KIND_LABELS
from .zugferd import build_xml

HERE = Path(__file__).parent
ASSETS = HERE / "assets"
_env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
# Ländername in der Anschrift; unbekannte Codes bleiben stehen.
COUNTRIES_DE = {"AT": "Österreich", "BE": "Belgien", "CH": "Schweiz", "CZ": "Tschechien", "DK": "Dänemark",
                "ES": "Spanien", "FI": "Finnland", "FR": "Frankreich", "GB": "Vereinigtes Königreich", "IE": "Irland",
                "IT": "Italien", "LI": "Liechtenstein", "LU": "Luxemburg", "NL": "Niederlande", "NO": "Norwegen",
                "PL": "Polen", "PT": "Portugal", "SE": "Schweden", "US": "USA"}
_env.filters.update(eur=fmt_eur, qty=fmt_qty, date_de=date_de, country_de=lambda c: COUNTRIES_DE.get(c, c))


def render_html(snap: dict, logo: Path | None, draft: bool) -> str:
    if logo is not None and not Path(logo).is_file():
        raise KontorError(f"Logo nicht gefunden: {logo} (Einstellungen → Logo)")
    return _env.get_template("document.html").render(
        s=snap, draft=draft, label=KIND_LABELS[snap["kind"]],
        logo_uri=Path(logo).resolve().as_uri() if logo else None,
        css_uri=(ASSETS / "document.css").as_uri())


def render_own_receipt(data: dict, seller: dict, tax_label: str, created: str) -> bytes:
    """Eigenbeleg für eine Ausgabe ohne Rechnung (PDF/A-3b)."""
    html = _env.get_template("eigenbeleg.html").render(d=data, seller=seller, tax_label=tax_label, created=created,
                                                       css_uri=(ASSETS / "document.css").as_uri())
    return HTML(string=html, base_url=str(ASSETS)).write_pdf(pdf_variant="pdf/a-3b")


def render_pdf(snap: dict, logo: Path | None = None, draft: bool = False) -> bytes:
    html = HTML(string=render_html(snap, logo, draft), base_url=str(ASSETS))
    if draft:
        return html.write_pdf()
    pdf = html.write_pdf(pdf_variant="pdf/a-3b")
    seller = snap["seller"]
    if snap["kind"] != "angebot" and (seller.get("tax_number") or seller.get("vat_id")):
        pdf = attach_xml(pdf, build_xml(snap), level="EN 16931")   # ohne Kennung wäre das XML ungültig (BR-CO-26)
    return pdf
