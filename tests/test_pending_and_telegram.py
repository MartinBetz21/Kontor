"""Steuernummer folgt; Telegram-Begleittext und Nachträge; Bewirtungsangaben."""
import json

import pytest

from kontor.core.errors import KontorError
from kontor.importers.telegram import LocalTransport, pull
from kontor.render.zugferd import extract_xml
from kontor.services import finalize as fin
from kontor.services import masterdata as md
from kontor.services import receipts as rc
from tests.test_finalize import invoice, setup  # noqa: F401


def test_invoice_with_tax_number_pending(ui, setup):
    md.set_settings(ui, {"tax_number": "", "tax_number_pending": "1"})
    did = invoice(ui, setup)
    assert any("§ 14" in w for w in fin.warnings(ui, did))
    d = fin.finalize(ui, did)
    pdf = (ui.data / d["pdf_path"]).read_bytes()
    assert extract_xml(pdf) is None
    import io, pypdf
    text = " ".join(" ".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(pdf)).pages).split())
    assert "Steuernummer wird nachgeliefert" in text


def test_without_pending_still_refused(ui, setup):
    md.set_settings(ui, {"tax_number": "", "tax_number_pending": ""})
    with pytest.raises(KontorError, match="Steuernummer"):
        fin.finalize(ui, invoice(ui, setup))


def drop(folder, name, content, meta):
    (folder / name).write_bytes(content)
    (folder / f"{name}.json").write_text(json.dumps({"datei": name, **meta}), encoding="utf-8")


def test_caption_and_reply_note_are_captured(ctx, tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    drop(server, "20261002-1015-foto.jpg", b"\xff\xd8 essen",
         {"originalname": "foto.jpg", "begleittext": "Geschäftsessen mit Max", "message_id": 501})
    (server / "20261002-1017-nachtrag-503.json").write_text(json.dumps(
        {"art": "nachtrag", "text": "Anlass: Projektstart Bestellportal", "reply_to": 501, "message_id": 503}),
        encoding="utf-8")
    result = pull(ctx, LocalTransport(server))
    assert result["neu"] == ["foto.jpg"] and len(result["notizen"]) == 1 and result["fehler"] == []
    r = rc.list_receipts(ctx)[0]
    assert r["note"] == "Geschäftsessen mit Max\nAnlass: Projektstart Bestellportal"
    assert r["source_ref"] == "telegram:501"
    assert not list(server.iterdir())


def test_note_for_unknown_photo_stays_on_server(ctx, tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    (server / "x-nachtrag-9.json").write_text(json.dumps({"art": "nachtrag", "text": "t", "reply_to": 8}),
                                              encoding="utf-8")
    result = pull(ctx, LocalTransport(server))
    assert len(result["fehler"]) == 1 and (server / "x-nachtrag-9.json").exists()


def test_bewirtung_needs_occasion_and_guests(ctx, ui):
    rid = rc.add_file(ctx, b"\xff\xd8 linde2", "g.jpg", "telegram")["id"]
    cat = next(c["id"] for c in md.list_categories(ctx) if c["name"] == "Bewirtung")
    rc.update(ctx, rid, {"receipt_date": "2026-09-24", "supplier": "Zur Linde", "category_id": cat,
                         "parts": [{"tax_kind": "inland_7", "gross": "32,10"}, {"tax_kind": "inland_19", "gross": "23,80"}],
                         "bewirtung_anlass": "Projektbesprechung Bestellportal"})
    with pytest.raises(KontorError, match="Bewirtete Personen"):
        rc.confirm(ui, rid)
    rc.update(ctx, rid, {"bewirtung_teilnehmer": "Erika Mustermann; Max Beispiel (Beispiel Technik)"})
    assert rc.confirm(ui, rid)["status"] == "geprueft"
