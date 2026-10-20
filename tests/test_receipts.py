import pytest

from kontor.core.errors import HumanOnly, KontorError
from kontor.core.tax_codes import complete_amounts
from kontor.services import masterdata as md
from kontor.services import receipts as rc
from tests.test_render import SETTINGS, build_snapshot, build_xml, doc

JPG = b"\xff\xd8\xff\xe0" + b"beleg-foto" * 10


def cat(ctx, name="Software und Abos"):
    return next(c["id"] for c in md.list_categories(ctx) if c["name"] == name)


def test_complete_amounts():
    assert complete_amounts("inland_19", None, None, 11900) == (10000, 1900, 11900)
    assert complete_amounts("inland_7", None, None, 10700) == (10000, 700, 10700)
    assert complete_amounts("inland_19", 10000, 1900, 11900) == (10000, 1900, 11900)
    assert complete_amounts("rc_eu", None, None, 2000) == (2000, 380, 2000)
    assert complete_amounts("ohne_vorsteuer", None, None, 500) == (500, 0, 500)
    with pytest.raises(KontorError):
        complete_amounts("inland_19", 10000, 1900, 12000)


def test_photo_lands_in_inbox_and_duplicate_is_detected(ctx):
    first = rc.add_file(ctx, JPG, "IMG_0001.HEIC.jpg", "telegram")
    again = rc.add_file(ctx, JPG, "anderer-name.jpg", "upload")
    assert first["duplicate"] is False and again == {"id": first["id"], "duplicate": True, "parsed": False}
    r = rc.get_receipt(ctx, first["id"])
    assert r["status"] == "eingang" and (ctx.data / r["file_path"]).exists()
    assert r["file_path"].startswith("eingang/")


def test_unsupported_type_refused(ctx):
    with pytest.raises(KontorError, match="Dateityp"):
        rc.add_file(ctx, b"MZ...", "virus.exe", "upload")
    with pytest.raises(KontorError):
        rc.add_file(ctx, b"", "leer.pdf", "upload")


def test_zugferd_receipt_is_prefilled(ctx):
    xml = build_xml(build_snapshot(doc(), SETTINGS, "RE-2026-001", [], None))
    r = rc.get_receipt(ctx, rc.add_file(ctx, xml, "rechnung.xml", "upload")["id"])
    assert (r["supplier"], r["receipt_number"], r["gross"], r["tax_kind"]) == \
        ("Erika Mustermann · Example Consulting", "RE-2026-001", 119000, "inland_19")


def test_date_from_foreign_xml_never_becomes_a_path(ctx, ui):
    """Das Belegdatum wird später zum Ordner belege/JJJJ/MM. Aus einer fremden E-Rechnung darf kein „../“ kommen."""
    xml = build_xml(build_snapshot(doc(), SETTINGS, "RE-2026-001", [], None))
    xml = xml.replace(b'<udt:DateTimeString format="102">20261002<', b'<udt:DateTimeString format="102">../.....<', 1)
    rid = rc.add_file(ctx, xml, "rechnung.xml", "telegram")["id"]
    assert rc.get_receipt(ctx, rid)["receipt_date"] is None
    rc.update(ctx, rid, {"category_id": cat(ctx)})
    with pytest.raises(KontorError, match="Belegdatum"):
        rc.confirm(ui, rid)


def test_broken_xml_is_kept_as_plain_file(ctx):
    r = rc.add_file(ctx, b"<kaputt>", "kaputt.xml", "upload")
    assert r["parsed"] is False and rc.get_receipt(ctx, r["id"])["status"] == "eingang"


def test_agent_suggests_human_confirms(ctx, ui):
    rid = rc.add_file(ctx, JPG, "anthropic.jpg", "telegram")["id"]
    rc.update(ctx, rid, {"receipt_date": "2026-10-01", "supplier": "Anthropic Ireland", "gross": "20,00",
                         "tax_kind": "rc_eu", "category_id": cat(ctx, "KI-Nutzung (API)"), "paid_on": "2026-10-01"})
    with pytest.raises(HumanOnly):
        rc.confirm(ctx, rid)
    r = rc.confirm(ui, rid)
    assert (r["status"], r["net"], r["tax"], r["gross"]) == ("geprueft", 2000, 380, 2000)
    assert r["file_path"].startswith("belege/2026/10/") and (ui.data / r["file_path"]).exists()
    pay = ctx.conn.execute("SELECT amount, kind FROM payments WHERE receipt_id = ?", (rid,)).fetchone()
    assert tuple(pay) == (-2000, "ausgabe")
    with pytest.raises(HumanOnly):
        rc.update(ctx, rid, {"note": "Claude darf geprüfte Belege nicht ändern"})


def test_confirm_needs_required_fields(ui):
    rid = rc.add_file(ui, JPG, "x.jpg", "upload")["id"]
    with pytest.raises(KontorError, match="Lieferant"):
        rc.confirm(ui, rid)


def test_delete_inbox_receipt_human_only(ctx, ui):
    rid = rc.add_file(ctx, JPG, "privat.jpg", "upload")["id"]
    path = ctx.data / rc.get_receipt(ctx, rid)["file_path"]
    with pytest.raises(HumanOnly):
        rc.delete(ctx, rid)
    rc.delete(ui, rid)
    assert not path.exists() and rc.list_receipts(ui) == []


def test_receipt_note_line_endings_normalized(ctx):
    rid = rc.add_file(ctx, JPG + b"crlf", "n.jpg", "upload")["id"]
    rc.update(ctx, rid, {"note": "Zeile 1\r\nZeile 2"})
    assert rc.get_receipt(ctx, rid)["note"] == "Zeile 1\nZeile 2"
