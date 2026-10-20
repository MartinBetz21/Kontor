import pytest

from kontor.core.errors import KontorError
from kontor.services import masterdata as md


def test_settings_defaults_and_update(ctx):
    s = md.get_settings(ctx)
    assert s["payment_days"] == "14" and s["ustva_period"] == "monat" and s["tax_number"] == ""
    md.set_settings(ctx, {"name": "Erika Mustermann", "trade_name": "Example Consulting", "ustva_period": "quartal"})
    s = md.get_settings(ctx)
    assert (s["name"], s["trade_name"], s["ustva_period"]) == ("Erika Mustermann", "Example Consulting", "quartal")


def test_settings_reject_unknown_and_invalid(ctx):
    with pytest.raises(KontorError):
        md.set_settings(ctx, {"farbe": "blau"})
    with pytest.raises(KontorError):
        md.set_settings(ctx, {"ustva_period": "halbjahr"})   # „jahr“ gibt es seit Spec 18.3
    with pytest.raises(KontorError):
        md.set_settings(ctx, {"payment_days": "vierzehn"})


def test_customer_crud(ctx):
    cid = md.create_customer(ctx, {"company": "Beispiel Technik GmbH & Co. KG", "contact": "Max Beispiel",
                                   "postcode": "10115", "city": "Berlin"})
    md.update_customer(ctx, cid, {"street": "Musterweg 1"})
    c = md.get_customer(ctx, cid)
    assert (c["company"], c["street"], c["country"]) == ("Beispiel Technik GmbH & Co. KG", "Musterweg 1", "DE")
    assert md.customer_label(c) == "Beispiel Technik GmbH & Co. KG"
    assert [x["id"] for x in md.list_customers(ctx)] == [cid]


def test_customer_needs_a_name(ctx):
    with pytest.raises(KontorError, match="Firma oder Ansprechpartner"):
        md.create_customer(ctx, {"city": "Berlin"})
    with pytest.raises(KontorError):
        md.get_customer(ctx, 999)


def test_catalog_with_german_price_and_search(ctx):
    iid = md.create_catalog_item(ctx, {"title": "Wartung", "unit": "Monate", "unit_price": "640,00"})
    md.create_catalog_item(ctx, {"title": "Prozessanalyse", "unit_price": 100000})
    assert md.get_catalog_item(ctx, iid)["unit_price"] == 64000
    assert [i["title"] for i in md.list_catalog(ctx, "wart")] == ["Wartung"]
    with pytest.raises(KontorError):
        md.create_catalog_item(ctx, {"title": "X", "unit_price": 1, "tax_rate": "16"})


def test_templates_default(ctx):
    md.create_template(ctx, {"kind": "angebot", "slot": "schluss", "title": "Standard",
                             "body": "Zur Annahme genügt eine kurze Antwort per Mail.", "is_default": True})
    md.create_template(ctx, {"kind": "angebot", "slot": "schluss", "title": "Neu", "body": "B", "is_default": True})
    assert md.default_text(ctx, "angebot", "schluss") == "B"
    assert md.default_text(ctx, "schluss", "einleitung") == ""   # Rechnungsarten nutzen 'rechnung'


def test_categories(ctx):
    names = [c["name"] for c in md.list_categories(ctx)]
    assert "Software und Abos" in names
