"""Abo-Übersicht: Kosten pro Monat und Jahr, nächste Abbuchungen."""
import pytest

from kontor.core.errors import KontorError
from kontor.services import subscriptions as subs
from tests.test_ui import client, setup_master  # noqa: F401


def test_overview_sums_and_upcoming(ctx):
    subs.create(ctx, {"name": "Google Workspace", "provider": "Google Cloud EMEA", "amount": "8,10",
                      "interval": "monat", "next_due": "2026-10-05", "tax_kind": "rc_eu"})
    subs.create(ctx, {"name": "Domain example.com", "provider": "Example Hosting", "amount": "12,00", "interval": "jahr",
                      "next_due": "2027-09-01", "tax_kind": "inland_19"})
    subs.create(ctx, {"name": "Webinar-Tool", "provider": "Webinar-Tool", "interval": "monat"})            # Betrag noch offen
    o = subs.overview(ctx)                                                                  # ctx.today = 2026-10-02
    assert o["per_month"] == 810 + 100 and o["per_year"] == 810 * 12 + 1200
    assert [s["name"] for s in o["upcoming"]] == ["Google Workspace"]
    assert [s["name"] for s in o["missing_amount"]] == ["Webinar-Tool"]


def test_past_due_date_rolls_forward_for_display(ctx):
    subs.create(ctx, {"name": "Abo", "amount": "10,00", "interval": "monat", "next_due": "2026-08-15"})
    assert subs.list_all(ctx)[0]["next_display"] == "2026-10-15"


def test_cancelled_not_in_sums_and_validation(ctx):
    sid = subs.create(ctx, {"name": "Altes Tool", "amount": "20,00", "interval": "monat"})
    subs.update(ctx, sid, {"status": "gekuendigt", "ends_on": "2026-10-31"})
    assert subs.overview(ctx)["per_month"] == 0
    with pytest.raises(KontorError):
        subs.create(ctx, {"name": "", "interval": "monat"})
    with pytest.raises(KontorError):
        subs.create(ctx, {"name": "x", "interval": "woche"})


def test_abo_page(client):
    setup_master(client)
    r = client.post("/abos", data={"name": "Webinar-Tool", "provider": "Webinar-Tool", "amount": "", "interval": "monat",
                                   "next_due": "", "tax_kind": "rc_drittland", "payment_via": "Karte"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/abos").text
    assert "Webinar-Tool" in page and "Betrag fehlt" in page
