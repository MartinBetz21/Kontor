import re

from tests.test_ui import client, setup_master  # noqa: F401

JPG = b"\xff\xd8\xff\xe0" + b"tankbeleg" * 20


def test_upload_check_confirm(client):
    setup_master(client)
    r = client.post("/belege/upload", files=[("dateien", ("tank.jpg", JPG, "image/jpeg")),
                                             ("dateien", ("tank-kopie.jpg", JPG, "image/jpeg"))],
                    follow_redirects=False)
    assert r.status_code == 303 and "1%20neu" in r.headers["location"]
    page = client.get("/belege").text
    rid = int(re.search(r"/belege/(\d+)", page).group(1))
    assert client.get(f"/belege/{rid}").status_code == 200
    assert client.get(f"/belege/{rid}/datei").content == JPG
    cat = re.search(r'<option value="(\d+)"\s*>Software und Abos', client.get(f"/belege/{rid}").text).group(1)
    r = client.post(f"/belege/{rid}/bestaetigen", data={"receipt_date": "2026-10-01", "supplier": "Aral",
                                                        "receipt_number": "", "gross": "59,50", "tax_kind": "inland_19",
                                                        "category_id": cat, "paid_on": "2026-10-01", "note": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "Aral" in client.get("/belege?status=geprueft").text
    assert client.post(f"/belege/{rid}/loeschen", data={}).status_code == 403


def test_reports_pages_and_actions(client):
    setup_master(client)
    for path in ("/auswertungen", "/auswertungen?zeitraum=2026-Q4", "/auswertungen?zeitraum=2026-10&jahr=2026"):
        page = client.get(path)
        assert page.status_code == 200 and "Kennzahl" in page.text
    client.post("/auswertungen/ust-zahlung", data={"datum": "2026-10-10", "betrag": "-12,00", "notiz": "UStVA 09"})
    assert client.post("/auswertungen/sperren", data={"monat": "2026-09"}).status_code == 403
    client.post("/auswertungen/sperren", data={"monat": "2026-09", "notiz": "abgegeben", "confirm": client.confirm})
    assert "2026-09" in client.get("/auswertungen").text
    client.post("/auswertungen/entsperren", data={"monat": "2026-09", "begruendung": "Korrektur",
                                                  "confirm": client.confirm})
    csv = client.get("/export/journal/2026.csv")
    assert csv.headers["content-type"].startswith("text/csv") and csv.text.startswith("Datum;Art")


def test_receipt_with_two_tax_rates_in_ui(client):
    setup_master(client)
    client.post("/belege/upload", files=[("dateien", ("linde.jpg", b"\xff\xd8 linde", "image/jpeg"))])
    rid = int(re.search(r"/belege/(\d+)", client.get("/belege").text).group(1))
    page = client.get(f"/belege/{rid}").text
    assert 'name="part_gross"' in page
    cat = re.search(r'<option value="(\d+)"\s*>Bewirtung', page).group(1)
    r = client.post(f"/belege/{rid}/bestaetigen", data={
        "receipt_date": "2026-09-24", "supplier": "Gasthaus Zur Linde", "receipt_number": "", "gross": "",
        "tax_kind": "", "category_id": cat, "paid_on": "2026-09-24",
        "note": "", "bewirtung_anlass": "Projektbesprechung", "bewirtung_teilnehmer": "Erika Mustermann, N. N.",
        "part_kind": ["inland_7", "inland_19", "inland_19"], "part_gross": ["32,10", "23,80", ""]},
        follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/belege/{rid}").text
    assert "55,90" in page and "gemischt" in page
