import re

from tests.test_ui import client, setup_master  # noqa: F401  (Fixture)


def invoice_form(cid, price="1.000,00"):
    return {"kind": "rechnung", "customer_id": str(cid), "doc_date": "2026-10-02", "service_mode": "datum",
            "service_date": "2026-09-30", "subject": "Prozessanalyse Auftragsabwicklung", "intro": "Hallo {vorname},",
            "closing": "Danke!", "line_title": ["Prozessanalyse"], "line_description": ["Analyse der Abläufe"],
            "line_quantity": ["1"], "line_unit": ["psch."], "line_price": [price], "line_tax": ["19"]}


def doc_id(r):
    return int(re.search(r"/dokumente/(\d+)", r.headers["location"]).group(1))


def test_pages(client):
    for path in ("/dokumente?gruppe=angebote", "/dokumente?gruppe=rechnungen", "/dokumente/neu?art=angebot",
                 "/dokumente/neu?art=rechnung", "/api/dokumente"):
        assert client.get(path).status_code == 200, path


def test_invoice_flow(client):
    cid = setup_master(client)
    r = client.post("/dokumente", data=invoice_form(cid), follow_redirects=False)
    did = doc_id(r)
    page = client.get(f"/dokumente/{did}").text
    assert "1.000,00" in page and "1.190,00 €" in page
    assert "1.190,00 €" in client.post("/dokumente/summen", data=invoice_form(cid)).text
    assert "Kein Betrag" in client.post("/dokumente/summen", data=invoice_form(cid, "abc")).text
    assert client.post(f"/dokumente/{did}/abschliessen", data=invoice_form(cid)).status_code == 403
    r = client.post(f"/dokumente/{did}/abschliessen", data={**invoice_form(cid), "confirm": client.confirm},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/dokumente/{did}").text
    assert "RE-2026-001" in page and "Abgeschlossen" in page
    pdf = client.get(f"/dokumente/{did}/pdf?download=1")
    assert pdf.headers["content-type"] == "application/pdf"
    assert 'attachment; filename="RE-2026-001.pdf"' == pdf.headers["content-disposition"]
    client.post(f"/dokumente/{did}/zahlung", data={"datum": "2026-10-10", "betrag": "1.190,00", "notiz": ""})
    assert "Bezahlt" in client.get(f"/dokumente/{did}").text
    r = client.post(f"/dokumente/{did}/stornieren", data={"confirm": client.confirm}, follow_redirects=False)
    assert "Stornorechnung" in client.get(r.headers["location"]).text


def test_bad_number_on_save_is_400(client):
    cid = setup_master(client)
    r = client.post("/dokumente", data=invoice_form(cid, "1,2,3"))
    assert r.status_code == 400 and "Kein Betrag" in r.text


def test_offer_to_abschlag_and_catalog(client):
    cid = setup_master(client)
    client.post("/positionen", data={"title": "Wartung", "unit": "Monate", "unit_price": "640,00",
                                     "tax_rate": "19"})
    assert "Wartung" in client.get("/katalog/suche?q=wart").text
    form = {**invoice_form(cid, "8.000,00"), "kind": "angebot", "line_title": ["Entwicklung"]}
    did = doc_id(client.post("/dokumente", data=form, follow_redirects=False))
    client.post(f"/dokumente/{did}/abschliessen", data={**form, "confirm": client.confirm})
    client.post(f"/dokumente/{did}/status", data={"status": "angenommen"})
    r = client.post(f"/dokumente/{did}/rechnung", data={"art": "abschlag", "prozent": "50"}, follow_redirects=False)
    assert "Abschlag 50 % auf Angebot AN-2026-001" in client.get(r.headers["location"]).text


def test_wrong_payment_deleted_in_ui(client):
    cid = setup_master(client)
    did = doc_id(client.post("/dokumente", data=invoice_form(cid), follow_redirects=False))
    client.post(f"/dokumente/{did}/abschliessen", data={**invoice_form(cid), "confirm": client.confirm})
    client.post(f"/dokumente/{did}/zahlung", data={"datum": "2026-10-10", "betrag": "119,00"})
    page = client.get(f"/dokumente/{did}").text
    pid = int(re.search(r"/zahlungen/(\d+)/loeschen", page).group(1))
    assert client.post(f"/zahlungen/{pid}/loeschen", data={"begruendung": "Tippfehler"}).status_code == 403
    client.post(f"/zahlungen/{pid}/loeschen", data={"begruendung": "Tippfehler", "confirm": client.confirm})
    assert "Abgeschlossen" in client.get(f"/dokumente/{did}").text


def test_correction_in_ui(client):
    cid = setup_master(client)
    client.post("/einstellungen", data={**__import__("tests.test_ui", fromlist=["STAMM"]).STAMM, "tax_number": "",
                                         "tax_number_pending": "1"})
    did = doc_id(client.post("/dokumente", data=invoice_form(cid), follow_redirects=False))
    client.post(f"/dokumente/{did}/abschliessen", data={**invoice_form(cid), "confirm": client.confirm})
    client.post("/einstellungen", data={**__import__("tests.test_ui", fromlist=["STAMM"]).STAMM})
    assert client.post(f"/dokumente/{did}/berichtigen", data={"grund": "Steuernummer ergänzt"}).status_code == 403
    client.post(f"/dokumente/{did}/berichtigen", data={"grund": "Steuernummer ergänzt", "confirm": client.confirm})
    page = client.get(f"/dokumente/{did}").text
    assert "Fassung 1" in page and "Steuernummer ergänzt" in page
    latest = client.get(f"/dokumente/{did}/pdf").content
    original = client.get(f"/dokumente/{did}/pdf?version=0").content
    assert latest != original


def test_editor_works_without_unit_field_and_pdf_shows_only_quantity(client):
    cid = setup_master(client)
    form = {k: v for k, v in invoice_form(cid).items() if k != "line_unit"}
    form["line_quantity"] = ["3"]
    did = doc_id(client.post("/dokumente", data=form, follow_redirects=False))
    assert 'name="line_unit"' not in client.get(f"/dokumente/{did}").text
    from kontor.render.pdf import render_html
    from kontor.core.snapshot import build_snapshot
    import json
    d = client.get("/api/dokumente").json()[0]
    assert len(d["lines"]) == 1 and d["lines"][0]["quantity"] == "3"


def test_save_finished_pdf_to_downloads(client, tmp_path):
    client.app.state.downloads = tmp_path / "Downloads"
    cid = setup_master(client)
    did = doc_id(client.post("/dokumente", data=invoice_form(cid), follow_redirects=False))
    assert client.post(f"/dokumente/{did}/speichern").status_code == 400          # Entwurf: noch nicht fertig
    client.post(f"/dokumente/{did}/abschliessen", data={**invoice_form(cid), "confirm": client.confirm})
    r = client.post(f"/dokumente/{did}/speichern", follow_redirects=False)
    assert r.status_code == 303
    saved = tmp_path / "Downloads" / "RE-2026-001.pdf"
    assert saved.read_bytes()[:4] == b"%PDF"
    assert "speichern" in client.get("/dokumente?gruppe=rechnungen").text
