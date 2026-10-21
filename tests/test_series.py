"""Serienrechnungen: z. B. Einführung über drei Monate."""
import pytest

from kontor.core.errors import KontorError
from kontor.services import documents as docs
from kontor.services import series
from tests.test_finalize import setup  # noqa: F401

LINE = [{"title": "Einführung", "description": "Monatlich", "quantity": "1", "unit_price": 50000}]


def test_three_monthly_invoices_then_stop(ui, setup):
    sid = series.create(ui, {"customer_id": setup, "subject": "Einführung", "lines": LINE,
                             "start_date": "2026-11-16", "count": 3})
    assert series.generate_due(ui, today="2026-11-15") == []
    first = series.generate_due(ui, today="2026-11-16")
    assert len(first) == 1
    d = docs.get_document(ui, first[0])
    assert (d["kind"], d["status"], d["net"]) == ("rechnung", "entwurf", 50000)
    assert (d["service_from"], d["service_to"]) == ("2026-11-16", "2026-12-15")
    assert "November 2026" in d["subject"]
    assert series.generate_due(ui, today="2026-11-30") == []
    assert len(series.generate_due(ui, today="2027-02-20")) == 2            # Dezember und Januar nachgeholt
    assert series.generate_due(ui, today="2027-06-01") == []
    s = series.get(ui, sid)
    assert (s["issued"], s["active"]) == (3, 0)


def test_open_ended_series_and_pause(ui, setup):
    sid = series.create(ui, {"customer_id": setup, "subject": "Betrieb", "lines": LINE, "start_date": "2027-02-16"})
    assert len(series.generate_due(ui, today="2027-04-16")) == 3
    series.set_active(ui, sid, False)
    assert series.generate_due(ui, today="2027-12-01") == []


def test_series_validation(ui, setup):
    with pytest.raises(KontorError):
        series.create(ui, {"customer_id": setup, "subject": "x", "lines": [], "start_date": "2026-11-16"})
    with pytest.raises(KontorError):
        series.create(ui, {"customer_id": setup, "subject": "x", "lines": LINE, "start_date": "16.11.2026"})
