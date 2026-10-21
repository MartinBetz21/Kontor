import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kontor.api.app import create_app
from pathlib import Path


LOGO = Path(__file__).parent / "fixtures" / "logo-hell.svg"
UI = Path(__file__).resolve().parent.parent / "kontor" / "ui"

STAMM = {"name": "Erika Mustermann", "trade_name": "Example Consulting", "street": "Musterstraße 1", "postcode": "10115",
         "city": "Berlin", "country": "DE", "email": "info@example.com", "phone": "+49 30 1234567",
         "tax_number": "123/456/78901", "vat_id": "", "iban": "DE89370400440532013000", "bic": "", "bank_name": "",
         "ustva_period": "monat", "payment_days": "14", "offer_valid_days": "30"}


class LokalClient(TestClient):
    """Alle Seiten liegen unter /m/lokal (Spec 18.2). Pfade aus Redirects tragen das Präfix schon."""

    def request(self, method, url, *args, **kwargs):
        if isinstance(url, str) and url.startswith("/m/lokal"):
            url = url[len("/m/lokal"):] or "/"
        return super().request(method, url, *args, **kwargs)


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "daten")
    with LokalClient(app, base_url="http://127.0.0.1/m/lokal") as c:
        c.confirm = app.state.confirm["lokal"]
        yield c


def setup_master(client):
    r = client.post("/einstellungen", data=STAMM, files={"logo": ("logo-hell.svg", LOGO.read_bytes(), "image/svg+xml")},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/kunden", data={"company": "Beispiel Technik GmbH & Co. KG", "contact": "Max Beispiel",
                                     "street": "Musterweg 1", "postcode": "10115", "city": "Berlin"},
                    follow_redirects=False)
    assert r.status_code == 303
    return int(re.search(r"/kunden/(\d+)", r.headers["location"]).group(1))


@pytest.mark.parametrize("path", ["/", "/kunden", "/positionen", "/einstellungen", "/protokoll", "/pruefen",
                                  "/api/uebersicht", "/umsatzsteuer"])
def test_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_settings_customer_catalog_template(client):
    cid = setup_master(client)
    assert "Beispiel Technik" in client.get(f"/kunden/{cid}").text
    assert client.post(f"/kunden/{cid}", data={"company": "Beispiel Technik GmbH & Co. KG", "street": "Neuer Weg 2",
                                               "postcode": "10115", "city": "Berlin"},
                       follow_redirects=False).status_code == 303
    assert "Neuer Weg 2" in client.get(f"/kunden/{cid}").text
    r = client.post("/positionen", data={"title": "Wartung", "description": "Nachschärfen im Alltag",
                                         "unit": "Monate", "unit_price": "640,00", "tax_rate": "19"},
                    follow_redirects=False)
    assert r.status_code == 303 and "640,00" in client.get("/positionen").text
    r = client.post("/texte", data={"kind": "angebot", "slot": "schluss", "title": "Standard",
                                    "body": "Zur Annahme genügt eine kurze Antwort per Mail.", "is_default": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "Musterstraße 1" in client.get("/einstellungen").text


def test_umsatzsteuer_page_names_the_figures(client):
    html = client.get("/umsatzsteuer?jahr=2026").text
    for label in ("Einbehaltene Umsatzsteuer", "Gezahlte Vorsteuer", "Voraussichtliche Zahllast", "Noch offen"):
        assert label in html


def test_errors_are_shown_not_crashing(client):
    r = client.post("/positionen", data={"title": "X", "unit_price": "abc", "tax_rate": "19"})
    assert r.status_code == 400 and "Kein Betrag" in r.text


def test_no_external_resources_in_templates():
    for f in (UI / "templates").glob("*.html"):
        assert not re.search(r"(src|href)=\"https?://", f.read_text(encoding="utf-8")), f.name


def test_cross_site_posts_are_refused(client):
    r = client.post("/kunden", data={"company": "Evil"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/kunden", data={"company": "Evil"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = client.post("/kunden", data={"company": "Gut"}, headers={"Sec-Fetch-Site": "same-origin"},
                    follow_redirects=False)
    assert r.status_code == 303


def test_foreign_host_is_refused(client):
    """DNS-Rebinding: evil.example zeigt auf 127.0.0.1, der Browser hält Lesen und Posten für same-origin."""
    evil = {"Host": "evil.example:8792", "Origin": "http://evil.example:8792", "Sec-Fetch-Site": "same-origin"}
    assert client.get("/api/dokumente", headers=evil).status_code == 400
    assert client.get("/kunden", headers=evil).status_code == 400
    assert client.post("/kunden", data={"company": "Evil"}, headers=evil).status_code == 400
    assert "Evil" not in client.get("/kunden").text
    assert client.get("/kunden", headers={"Host": "localhost:8792"}).status_code == 200


def test_other_unsafe_methods_are_checked_too(client):
    assert client.put("/kunden", data={"company": "Evil"}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_pages_cannot_be_framed_by_other_sites(client):
    rid = client.post("/belege/upload", files={"dateien": ("b.jpg", b"\xff\xd8\xff\xe0 foto", "image/jpeg")},
                      follow_redirects=False)
    assert rid.status_code == 303
    for path in ("/", "/kunden", "/belege/1/datei"):
        h = client.get(path).headers
        assert h["x-frame-options"] == "SAMEORIGIN" and h["content-security-policy"] == "frame-ancestors 'self'"
        assert h["x-content-type-options"] == "nosniff"


def test_tax_number_pending_checkbox(client):
    setup_master(client)
    client.post("/einstellungen", data={**STAMM, "tax_number": "", "tax_number_pending": "1"})
    assert 'name="tax_number_pending" value="1" checked' in client.get("/einstellungen").text
    client.post("/einstellungen", data={**STAMM, "tax_number": ""})          # Haken entfernt
    assert 'name="tax_number_pending" value="1" checked' not in client.get("/einstellungen").text


def test_drag_and_drop_is_wired_on_every_page(client):
    for path in ("/", "/belege", "/dokumente?gruppe=rechnungen"):
        assert 'id="drop-overlay"' in client.get(path).text
    js = client.get("/static/app.js").text
    assert '"drop"' in js and "/belege/upload" in js


def test_boot_id_for_live_reload(client):
    a = client.get("/api/boot").json()["boot"]
    assert a and a == client.get("/api/boot").json()["boot"]
    assert "/api/boot" in client.get("/static/app.js").text
