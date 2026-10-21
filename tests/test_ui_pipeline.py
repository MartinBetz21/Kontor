import re

from tests.test_ui import STAMM, client, setup_master  # noqa: F401
from tests.test_ui_documents import doc_id, invoice_form


def offer_form(cid, **kw):
    f = {**invoice_form(cid, "8.000,00"), "kind": "angebot", "line_title": ["Entwicklung"], "payment_mode": "50_50",
         "valid_until": "2026-12-31"}
    f.update(kw)
    return f


def test_pipeline_accept_and_first_invoice(client):
    cid = setup_master(client)
    did = doc_id(client.post("/dokumente", data=offer_form(cid), follow_redirects=False))
    client.post(f"/dokumente/{did}/abschliessen", data={**offer_form(cid), "confirm": client.confirm})
    page = client.get("/angebote").text
    assert "Verschickt" in page and "AN-2026-001" in page and "50 % bei Auftrag" in page
    client.post(f"/dokumente/{did}/status", data={"status": "angenommen"})
    r = client.post(f"/dokumente/{did}/erste-rechnung", follow_redirects=False)
    assert "Abschlag 50 %" in client.get(r.headers["location"]).text


def test_expired_offer_shows_followup_on_overview(client):
    cid = setup_master(client)
    did = doc_id(client.post("/dokumente", data=offer_form(cid, valid_until="2026-01-31", doc_date="2026-01-02"),
                             follow_redirects=False))
    client.post(f"/dokumente/{did}/abschliessen", data={**offer_form(cid, valid_until="2026-01-31",
                                                                     doc_date="2026-01-02"), "confirm": client.confirm})
    page = client.get("/").text
    assert "Nachfassen" in page and "AN-2026-001" in page
    fid = int(re.search(r"/aufgaben/(\d+)/erledigt", page).group(1))
    client.post(f"/aufgaben/{fid}/erledigt", data={"notiz": "angerufen"})
    assert "/aufgaben/" not in client.get("/").text


def test_editor_new_customer_and_payment_mode(client):
    setup_master(client)
    f = offer_form("", new_company="Neu GmbH", new_contact="Anna Muster", new_street="Weg 1", new_postcode="50667",
                   new_city="Köln")
    did = doc_id(client.post("/dokumente", data=f, follow_redirects=False))
    page = client.get(f"/dokumente/{did}").text
    assert 'selected>Neu GmbH' in page.replace('" selected', ' selected').replace('"  selected', ' selected') \
        or "Neu GmbH</option>" in page
    assert 'value="50_50" selected' in page


def test_series_page(client):
    cid = setup_master(client)
    r = client.post("/serien", data={"customer_id": str(cid), "subject": "Einführung", "line_title": "Einführung",
                                     "line_description": "", "line_price": "500,00", "start_date": "2026-11-16",
                                     "count": "3"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Einführung" in client.get("/serien").text and "3" in client.get("/serien").text


def test_catalog_suggestions_and_reorder(client):
    setup_master(client)
    for t in ("Alpha", "Beta", "Gamma"):
        client.post("/positionen", data={"title": t, "unit_price": "1,00", "tax_rate": "19"})
    page = client.get("/dokumente/neu?art=angebot").text
    assert 'list="katalog"' in page and '<option value="Gamma"' in page
    ids = re.findall(r'data-item="(\d+)"', client.get("/positionen").text)
    client.post("/positionen/reihenfolge", data={"ids": ",".join(reversed(ids))})
    order = re.findall(r'<option value="(Alpha|Beta|Gamma)"', client.get("/dokumente/neu?art=angebot").text)
    assert order == ["Gamma", "Beta", "Alpha"]
