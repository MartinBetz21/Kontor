"""Speichern in einen gewählten Ordner (merkt sich den Ordner je Kunde) und „In Rechnung umwandeln“."""
import re

from kontor.services import masterdata as md
from tests.test_ui import client, setup_master  # noqa: F401
from tests.test_ui_documents import doc_id, invoice_form
from tests.test_ui_pipeline import offer_form


def finalized_invoice(client):
    cid = setup_master(client)
    did = doc_id(client.post("/dokumente", data=invoice_form(cid), follow_redirects=False))
    client.post(f"/dokumente/{did}/abschliessen", data={**invoice_form(cid), "confirm": client.confirm})
    return cid, did


def test_save_into_chosen_folder_and_remember_per_customer(client, tmp_path):
    cid, did = finalized_invoice(client)
    kunde = tmp_path / "Kunden" / "Beispiel Technik"
    kunde.mkdir(parents=True)
    r = client.post(f"/dokumente/{did}/speichern", data={"ziel": str(kunde)}, follow_redirects=False)
    assert r.status_code == 303 and (kunde / "RE-2026-001.pdf").exists()
    (kunde / "RE-2026-001.pdf").unlink()
    client.post(f"/dokumente/{did}/speichern", data={})                      # ohne Wahl: Ordner des Kunden
    assert (kunde / "RE-2026-001.pdf").exists()
    assert str(kunde) in client.get(f"/kunden/{cid}").text


def test_missing_folder_is_a_message(client, tmp_path):
    _, did = finalized_invoice(client)
    r = client.post(f"/dokumente/{did}/speichern", data={"ziel": str(tmp_path / "gibt-es-nicht")})
    assert r.status_code == 400 and "Ordner" in r.text


def test_convert_accepted_offer_into_invoice_draft(client):
    cid = setup_master(client)
    did = doc_id(client.post("/dokumente", data=offer_form(cid, payment_mode="vorab"), follow_redirects=False))
    client.post(f"/dokumente/{did}/abschliessen", data={**offer_form(cid, payment_mode="vorab"), "confirm": client.confirm})
    assert "In Rechnung umwandeln" not in client.get(f"/dokumente/{did}").text      # erst nach Annahme
    client.post(f"/dokumente/{did}/status", data={"status": "angenommen"})
    page = client.get(f"/dokumente/{did}").text
    assert "In Rechnung umwandeln" in page
    r = client.post(f"/dokumente/{did}/erste-rechnung", follow_redirects=False)
    new = client.get(r.headers["location"]).text
    assert "Rechnung" in new and "Entwurf" in new
    assert "In Rechnung umwandeln" in client.get("/angebote").text


def test_folder_chooser_endpoint_uses_native_dialog(client, tmp_path):
    seen = {}

    def chooser(start):
        seen["start"] = start
        return str(tmp_path)
    client.app.state.folder_chooser = chooser
    r = client.post("/ordner-waehlen", data={"start": "/irgendwo"})
    assert r.json() == {"path": str(tmp_path)} and seen["start"] == "/irgendwo"
    client.app.state.folder_chooser = lambda start: None                       # abgebrochen
    assert client.post("/ordner-waehlen", data={"start": ""}).json() == {"path": None}
    assert "prompt(" not in client.get("/static/app.js").text
