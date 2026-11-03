"""Bitcoin (Spec 18.14, Profil `bitcoin`). Alle Bewegungen, Beträge, Kurse und Namen sind erfunden."""
import re
from datetime import date

import pytest

from kontor.core.errors import KontorError
from kontor.importers import bitcoin as imp
from kontor.reports.abschluss import auffaelligkeiten, bilanz_guv
from kontor.services import bank
from kontor.services import bitcoin as btc
from kontor.services import documents as docs
from kontor.services import finalize as fin
from kontor.services import journal as jr
from kontor.services import masterdata as md

HAUPT = "DE47123456782222222222"
# Erfundene Werte aus der Erlaubnisliste in tools/check_contribution.py (keine echte Adresse, keine echte Transaktion)
ADRESSE = "bc1qtestwertnurfuertests0000000000000000"
TXID = "0123456789abcdef" * 4
SALDEN = """Konto;Bezeichnung;Saldo;S/H
1813;Hauptkonto;600,00;S
1809;Steuerkonto;2000,00;S
2900;Gezeichnetes Kapital;12500,00;H
2978;Verlustvortrag;9900,00;S
"""


@pytest.fixture
def gmbh(ctx):
    md.set_profile(ctx, {"gewinnermittlung": "bilanz", "kontenrahmen": "skr04", "nummernstellen": "4", "bitcoin": "ja"})
    md.set_settings(ctx, {"share_capital": "25000,00", "share_capital_outstanding": "12500,00"})
    btc.wallet(ctx, "Hardware")
    btc.wallet(ctx, "Handelsplatz", "boerse")
    jr.opening_from_saldenliste(ctx, 2026, SALDEN)
    return ctx


@pytest.fixture
def invoice(gmbh, ui):
    """Inlandsrechnung über 300,00 netto + 19 % = 357,00, Leistung im März."""
    md.set_settings(ui, {"name": "Muster GmbH", "street": "Weg 1", "postcode": "50667", "city": "Köln",
                         "tax_number": "214/0000/0000", "seat": "Köln", "register_court": "Amtsgericht Köln",
                         "register_number": "HRB 1", "managing_directors": "Erika Muster"})
    md.set_counter_start(ui, "RE", 500)
    cid = md.create_customer(ui, {"company": "Beispiel AG", "street": "Gasse 1", "postcode": "50667", "city": "Köln"})
    did = docs.save_draft(ui, {"kind": "rechnung", "customer_id": cid, "doc_date": "2026-03-02",
                               "service_date": "2026-03-02",
                               "lines": [{"title": "Beratung", "quantity": "1", "unit_price": 30000}]})
    return fin.finalize(ui, did)


def neu(ctx, **f) -> int:
    """Bewegung erfassen und bestätigen."""
    mid = btc.erfassen(ctx, {"wallet": "Hardware", **f})
    btc.bestaetigen(ctx, mid)
    return mid


def btc_lines(ctx, year):
    return [r for r in jr.lines(ctx, year) if r["ref"].startswith("BTC")]


# -- Schalter ---------------------------------------------------------------------------------------------------------
def test_switch_off_means_no_service_and_no_bookings(gmbh):
    neu(gmbh, art="kauf", datum="2026-02-01", sats=1_000_000, eur="600,00")
    assert btc_lines(gmbh, 2026)
    md.set_profile(gmbh, {"bitcoin": "nein"})
    assert btc_lines(gmbh, 2026) == []
    with pytest.raises(KontorError, match="Profilschalter bitcoin"):
        btc.erfassen(gmbh, {"wallet": "Hardware", "art": "kauf", "datum": "2026-02-02", "sats": 1, "eur": "1,00"})


def test_default_accounts_come_from_settings(gmbh):
    k = btc.konten(gmbh)
    assert (k["bestand"], k["verrechnung"], k["erloes"], k["buchwert"], k["gebuehr"], k["abschreibung"],
            k["zuschreibung"], k["verlust"]) == ("1300", "1370", "4830", "6969", "6855", "6910", "4915", "6969")
    md.set_settings(gmbh, {"btc_konto_bestand": "1440", "btc_konto_abschreibung": "6960"})
    assert (btc.konten(gmbh)["bestand"], btc.konten(gmbh)["abschreibung"]) == ("1440", "6960")
    with pytest.raises(KontorError, match="Konto"):
        md.set_settings(gmbh, {"btc_konto_bestand": "Bitcoin"})


# -- Entwurf, Bestätigen ----------------------------------------------------------------------------------------------
def test_draft_books_nothing_until_confirmed(gmbh):
    mid = btc.erfassen(gmbh, {"wallet": "Hardware", "art": "kauf", "datum": "2026-02-01", "sats": 1_000_000,
                              "eur": "600,00"})
    assert btc.get(gmbh, mid)["status"] == "entwurf" and btc_lines(gmbh, 2026) == []
    btc.bestaetigen(gmbh, mid)
    bal = jr.balances(gmbh, 2026)
    assert bal["1300"] == 60000 and bal["1370"] == -60000                     # Bestand an Verrechnung
    btc.entbestaetigen(gmbh, mid, "falscher Betrag")
    assert btc_lines(gmbh, 2026) == []


def test_unclassified_movements_cannot_be_confirmed(gmbh):
    mid = btc.erfassen(gmbh, {"wallet": "Hardware", "art": "eingang", "datum": "2026-02-01", "sats": 5000})
    with pytest.raises(KontorError, match="zuordnen"):
        btc.bestaetigen(gmbh, mid)
    verlust = btc.erfassen(gmbh, {"wallet": "Hardware", "art": "verlust", "datum": "2026-02-01", "sats": 5000})
    with pytest.raises(KontorError, match="Begründung"):
        btc.bestaetigen(gmbh, verlust)


# -- Bewertung und Buchungstabelle ------------------------------------------------------------------------------------
@pytest.fixture
def jahr(gmbh, invoice):
    """Ein erfundenes Jahr mit jeder Zeile der Buchungstabelle."""
    btc.kurs_speichern(gmbh, "2026-03-10", "71400")                            # Marktwert = Nennwert der Rechnung
    btc.kurs_speichern(gmbh, "2026-12-31", "50000")
    ids = {
        "kauf": neu(gmbh, art="kauf", datum="2026-02-01", sats=1_000_000, eur="600,00", gebuehr_eur="6,00"),
        "kunde": neu(gmbh, art="kundenzahlung", datum="2026-03-10", sats=500_000, rechnung=invoice["number"]),
        "verkauf": neu(gmbh, art="verkauf", datum="2026-04-01", vertragsdatum="2026-04-01", sats=400_000,
                       gebuehr_sats=1_000, eur="300,00", gebuehr_eur="3,00"),
        "zahlung": neu(gmbh, art="zahlung", datum="2026-05-02", sats=100_000, gebuehr_sats=500, eur="50,00",
                       konto="6815", steuer="19"),
        "umbuchung": neu(gmbh, art="umbuchung", datum="2026-06-01", sats=200_000, gebuehr_sats=300,
                         ziel="Handelsplatz"),
        "verlust": btc.erfassen(gmbh, {"wallet": "Handelsplatz", "art": "verlust", "datum": "2026-07-01",
                                       "sats": 50_000, "notiz": "Handelsplatz zahlt nicht aus (erfunden)"}),
    }
    btc.bestaetigen(gmbh, ids["verlust"])
    return ids


def test_periodic_average_cumulative_rounding_and_writedown(jahr, gmbh):
    b = btc.bewertung(gmbh, 2026)
    assert (b["pool"]["sats"], b["pool"]["ak"]) == (1_500_000, 95700)
    # kumulative Rundung: 25520, Gebühr 64, 6380, Gebühr 32, Umbuchungsgebühr 19, Verlust 3190
    assert [a["buchwert"] for a in b["abgaenge"]] == [25520, 64, 6380, 32, 19, 3190]
    assert sum(a["buchwert"] for a in b["abgaenge"]) + b["ak"] == b["pool"]["ak"]
    assert (b["bestand_sats"], b["ak"], b["wertansatz"], b["abschreibung"], b["buchwert"]) == \
        (948_200, 60495, 47410, 13085, 47410)                                  # 948.200 sats × 50.000 €


def test_every_line_of_the_booking_table(jahr, gmbh, invoice):
    bal = jr.balances(gmbh, 2026)
    assert bal["1300"] == 47410                                                # Bestand = Buchwert laut Nebenbuch
    assert bal["1370"] == -60000 + 30000 - 300                                 # Kauf, Erlös brutto, Servicegebühr
    assert bal["4830"] == -30000                                               # Vertragsbetrag brutto
    assert bal["6969"] == 25520 + 1380 + 3190                                  # Buchwert Verkauf, Differenz Zahlung, Verlust
    assert bal["6855"] == 300 + 64 + 32 + 19                                   # Servicegebühr und Netzwerkgebühren
    assert "3300" not in bal                                                   # Zahlung bucht wie ein Beleg über die Bank
    assert (bal["6815"], bal["1406"]) == (4202, 798)                           # 50,00 brutto, 19 % Vorsteuer
    assert bal["6910"] == 13085                                                # Abschreibung zum Stichtag
    assert "1200" not in bal and bal["3806"] == -5700                          # Rechnung bezahlt, USt fällig
    assert docs.get_document(gmbh, invoice["id"])["status"] == "bezahlt"
    by_ref = {}
    for r in btc_lines(gmbh, 2026):
        by_ref.setdefault(r["ref"], []).append(r)
    assert all(sum(r["amount"] for r in rows) == 0 for rows in by_ref.values())
    assert {r["account"] for r in by_ref[f"BTC{jahr['umbuchung']}"]} == {"6855", "1300"}   # nur die Netzwerkgebühr
    assert not [f for f in auffaelligkeiten(gmbh, 2026) if f["art"].startswith("btc_")]
    p = bilanz_guv(gmbh, 2026)["positionen"]
    assert p["abUV"] == 13085 and p["aktiva"] == p["passiva"]


def test_later_purchase_changes_earlier_disposal_in_the_same_year(jahr, gmbh):
    before = btc.bewertung(gmbh, 2026)["abgaenge"][0]["buchwert"]
    neu(gmbh, art="kauf", datum="2026-09-01", sats=500_000, eur="200,00")
    after = btc.bewertung(gmbh, 2026)["abgaenge"][0]["buchwert"]
    assert (before, after) == (25520, 23140)                                   # 115.700 × 400.000 / 2.000.000


def test_cumulative_rounding_adds_up_to_the_cent(gmbh):
    neu(gmbh, art="kauf", datum="2026-01-05", sats=3, eur="10,00")
    for d in ("2026-02-01", "2026-03-01", "2026-04-01"):
        neu(gmbh, art="verlust", datum=d, sats=1, notiz="erfundener Verlust")
    assert [a["buchwert"] for a in btc.bewertung(gmbh, 2026)["abgaenge"]] == [333, 334, 333]


def test_writeup_only_up_to_acquisition_cost(gmbh):
    neu(gmbh, art="kauf", datum="2026-02-01", sats=1_000_000, eur="600,00")
    btc.kurs_speichern(gmbh, "2026-12-31", "40000")
    btc.kurs_speichern(gmbh, "2027-12-31", "90000")
    b26, b27 = btc.bewertung(gmbh, 2026), btc.bewertung(gmbh, 2027)
    assert (b26["abschreibung"], b26["buchwert"]) == (20000, 40000)
    assert b27["anfang"] == {"sats": 1_000_000, "ak": 60000, "buchwert": 40000}
    assert (b27["zuschreibung"], b27["buchwert"]) == (20000, 60000)            # nicht bis 900,00
    assert jr.balances(gmbh, 2027)["4915"] == -20000


def test_pending_sale_is_written_down_to_the_contract_price(gmbh):
    neu(gmbh, art="kauf", datum="2026-02-01", sats=1_000_000, eur="600,00")
    btc.kurs_speichern(gmbh, "2026-12-31", "55000")
    sale = neu(gmbh, art="verkauf", vertragsdatum="2026-12-20", datum="2027-01-05", sats=1_000_000, eur="450,00")
    b = btc.bewertung(gmbh, 2026)
    assert b["bestand_sats"] == 1_000_000                                      # bleibt aktiviert, kein Erlös 2026
    assert (b["wertansatz"], b["buchwert"], b["abschreibung"]) == (55000, 45000, 15000)
    assert "4830" not in jr.balances(gmbh, 2026)
    b27 = btc.bewertung(gmbh, 2027)
    assert b27["abgaenge"][0]["buchwert"] == 45000 and jr.balances(gmbh, 2027)["4830"] == -45000
    btc.entbestaetigen(gmbh, sale, "Test")
    btc.zuordnen(gmbh, sale, eur="580,00")
    btc.bestaetigen(gmbh, sale)
    assert btc.bewertung(gmbh, 2026)["abschreibung"] == 5000                    # nur der normale Stichtagswert


def test_missing_closing_rate_books_nothing_and_is_flagged(gmbh):
    neu(gmbh, art="kauf", datum="2026-02-01", sats=1_000_000, eur="600,00")
    gmbh.today = date(2027, 2, 1)
    b = btc.bewertung(gmbh, 2026)
    assert b["kurs_stichtag"] is None and b["abschreibung"] == b["zuschreibung"] == 0
    assert "6910" not in jr.balances(gmbh, 2026)
    assert any(f["art"] == "btc_kurs_fehlt" for f in auffaelligkeiten(gmbh, 2026))


def test_customer_payment_creates_and_removes_the_invoice_payment(gmbh, invoice):
    btc.kurs_speichern(gmbh, "2026-03-10", "60000")                            # 500.000 sats = 300,00 statt 357,00
    mid = neu(gmbh, art="kundenzahlung", datum="2026-03-10", sats=500_000, rechnung=invoice["number"])
    m = btc.get(gmbh, mid)
    assert m["payment_id"] and m["eur"] == 35700
    assert docs.get_document(gmbh, invoice["id"])["status"] == "bezahlt"
    assert any(f["art"] == "btc_marktwert" for f in auffaelligkeiten(gmbh, 2026))
    assert not any(f["art"] == "zahlung_ohne_bank" for f in auffaelligkeiten(gmbh, 2026))
    btc.entbestaetigen(gmbh, mid, "Marktwert ansetzen")
    assert docs.get_document(gmbh, invoice["id"])["status"] == "abgeschlossen"
    btc.zuordnen(gmbh, mid, ak="300,00")
    btc.bestaetigen(gmbh, mid)
    bal = jr.balances(gmbh, 2026)
    assert bal["1300"] == 30000 and bal["6969"] == 5700 and "1200" not in bal  # Differenz zur Forderung als Aufwand
    assert not any(f["art"] == "btc_marktwert" for f in auffaelligkeiten(gmbh, 2026))


def test_lexware_year_books_nothing_but_carries_the_pool(gmbh):
    md.set_settings(gmbh, {"lexware_journal_years": "2025"})
    neu(gmbh, art="kauf", datum="2025-02-01", sats=1_000_000, eur="600,00")
    btc.kurs_speichern(gmbh, "2025-12-31", "50000")
    assert btc_lines(gmbh, 2025) == []
    assert btc.bewertung(gmbh, 2025)["buchwert"] == 50000
    assert btc.bewertung(gmbh, 2026)["anfang"] == {"sats": 1_000_000, "ak": 60000, "buchwert": 50000}


def test_opening_pool_from_the_prior_year(gmbh):
    btc.vortrag_setzen(gmbh, 2026, "Hardware", 2_000_000, "1.000,00", "900,00")
    assert btc.bewertung(gmbh, 2026)["anfang"] == {"sats": 2_000_000, "ak": 100000, "buchwert": 90000}
    assert btc.wallet_bestand(gmbh, "Hardware", "2026-12-31") == 2_000_000


def test_confirmed_movements_in_a_locked_year_stay(gmbh, ui):
    mid = neu(gmbh, art="kauf", datum="2026-02-01", sats=1_000_000, eur="600,00")
    jr.lock_year(ui, 2026, "Abschluss aufgestellt")
    with pytest.raises(KontorError, match="gesperrt"):
        btc.entbestaetigen(gmbh, mid, "zu spät")


# -- Wallet-Saldo, Auffälligkeiten ------------------------------------------------------------------------------------
def test_wallet_balance_shows_the_difference(jahr, gmbh):
    assert btc.wallet_bestand(gmbh, "Hardware", "2026-12-31") == 1_500_000 - 401_000 - 100_500 - 200_300
    assert btc.wallet_bestand(gmbh, "Handelsplatz", "2026-12-31") == 200_000 - 50_000
    btc.saldo_eintragen(gmbh, "Hardware", "2026-12-31", 798_300, "laut Wallet")
    s = next(x for x in btc.salden(gmbh) if x["wallet"] == "Hardware")
    assert s["differenz"] == 100
    f = next(f for f in auffaelligkeiten(gmbh, 2026) if f["art"] == "btc_wallet_differenz")
    assert f["betrag"] == 100


def test_book_value_must_match_the_journal(jahr, gmbh):
    jr.book(gmbh, {"ref": "K1", "date": "2026-12-31", "text": "erfundene Korrektur", "lines": {"1300": 100, "6969": -100}})
    f = next(f for f in auffaelligkeiten(gmbh, 2026) if f["art"] == "btc_buchwert_journal")
    assert f["betrag"] == 100


def test_open_drafts_are_flagged(gmbh):
    btc.erfassen(gmbh, {"wallet": "Hardware", "art": "eingang", "datum": "2026-02-01", "sats": 5000})
    assert any(f["art"] == "btc_entwurf" for f in auffaelligkeiten(gmbh, 2026))


# -- Kurse ------------------------------------------------------------------------------------------------------------
KRAKEN = ('{"error":[],"result":{"XXBTZEUR":['
          '[1767052800,"49000.0","51000.0","48000.0","50500.1","50000.0","12.5",100],'
          '[1767139200,"50500.1","52000.0","50000.0","51234.5","51000.0","10.0",90],'
          '[1767225600,"51234.5","51300.0","51000.0","51250.0","51200.0","1.0",9]],"last":1767139200}}')


def test_kraken_daily_candle_close_is_the_rate_of_the_utc_day(gmbh):
    rows = imp.parse_kraken_ohlc(KRAKEN, date(2026, 1, 1))
    assert rows == [("2025-12-30", btc.Decimal("50500.1")), ("2025-12-31", btc.Decimal("51234.5"))]  # laufender Tag fehlt
    assert btc.kraken_speichern(gmbh, KRAKEN, date(2026, 1, 1)) == 2
    assert btc.kurs(gmbh, "2025-12-31") == btc.Decimal("51234.5")
    r = gmbh.conn.execute("SELECT * FROM btc_kurse WHERE datum = '2025-12-31'").fetchone()
    assert r["quelle"] == "kraken" and r["abgerufen_am"]
    assert btc.kraken_speichern(gmbh, KRAKEN, date(2026, 1, 1)) == 0
    assert btc.kraken_seit(gmbh) == 1767139200


BITSTAMP = ('{"data":{"pair":"BTC/EUR","ohlc":['
            '{"timestamp":"1767052800","open":"1","high":"1","low":"1","close":"50400.0","volume":"1"},'
            '{"timestamp":"1767139200","open":"1","high":"1","low":"1","close":"51200.0","volume":"1"},'
            '{"timestamp":"1767225600","open":"1","high":"1","low":"1","close":"51300.0","volume":"1"}]}}')


def test_bitstamp_fills_only_days_without_a_kraken_close(gmbh):
    assert imp.parse_bitstamp_ohlc(BITSTAMP, date(2026, 1, 1))[-1] == ("2025-12-31", btc.Decimal("51200.0"))
    assert btc.abruf_speichern(gmbh, "bitstamp", BITSTAMP, date(2026, 1, 2)) == 3
    assert btc.kurs_zeile(gmbh, "2025-12-31")["quelle"] == "bitstamp"
    btc.kurs_speichern(gmbh, "2026-01-01", "1")                                 # von Hand gilt zuletzt
    assert btc.kurs(gmbh, "2026-01-01") == btc.Decimal("51300.0")
    btc.kraken_speichern(gmbh, KRAKEN, date(2026, 1, 1))                         # Kraken liefert 30. und 31.12.
    assert btc.kurs_zeile(gmbh, "2025-12-31") | {"abgerufen_am": ""} == \
        {"datum": "2025-12-31", "quelle": "kraken", "eur": btc.Decimal("51234.5"), "abgerufen_am": ""}
    assert btc.bewertung(gmbh, 2025)["kurs_quelle"] == "kraken"                  # Quelle des Stichtagskurses steht dabei
    assert btc.bitstamp_ab(gmbh) == 1767312000                                   # nach dem letzten Bitstamp-Tag
    assert btc.bitstamp_ab(gmbh, "2024-01-01") == 1704067200


def test_rate_gaps_are_listed(gmbh):
    btc.kurs_speichern(gmbh, "2026-01-01", "50000")
    btc.kurs_speichern(gmbh, "2026-01-04", "51000")
    stand = btc.kurse_stand(gmbh)
    assert stand["letzter"]["datum"] == "2026-01-04" and stand["luecken"] == ["2026-01-02", "2026-01-03"]


# -- Import -----------------------------------------------------------------------------------------------------------
BITBOX = "\n".join([
    "Time,Type,Amount,Unit,Fee,Fee Unit,Address,Transaction ID,Note",
    f"2026-03-10T14:22:05Z,received,500000,satoshi,,,{ADRESSE},{TXID},RE-2026-0501",
    f"2026-04-01T09:00:00Z,sent,0.004,BTC,0.00001,BTC,{ADRESSE},{TXID},",
    f"2026-06-01T10:00:00Z,sent_to_yourself,200000,satoshi,300,satoshi,{ADRESSE},{TXID},neue Adresse",
])


def test_bitbox_import_drops_addresses_and_transaction_ids(gmbh):
    rows = imp.parse_bitbox(BITBOX)
    assert [(r["art"], r["sats"], r["gebuehr_sats"]) for r in rows] == \
        [("eingang", 500_000, 0), ("ausgang", 400_000, 1_000), ("umbuchung", 200_000, 300)]
    assert all(ADRESSE not in str(r) and TXID not in str(r) for r in rows)
    res = btc.importieren(gmbh, "bitbox", BITBOX, wallet="Hardware")
    assert res["neu"] == 3 and btc.importieren(gmbh, "bitbox", BITBOX, wallet="Hardware")["neu"] == 0
    assert all(m["status"] == "entwurf" for m in btc.bewegungen(gmbh))
    umb = next(m for m in btc.bewegungen(gmbh) if m["art"] == "umbuchung")
    assert umb["ziel_wallet_id"] == umb["wallet_id"]
    for (table,) in gmbh.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        for row in gmbh.conn.execute(f"SELECT * FROM {table}"):
            text = " ".join(str(v) for v in tuple(row))
            assert ADRESSE not in text and TXID not in text, table


def test_coinfinity_import_gross_sale_and_fees(gmbh):
    text = "\n".join([
        "Order ID;Type;Date;Amount EUR;Amount Crypto;Crypto;Rate EUR;Mining Fee Crypto;Mining Fee EUR;"
        "Service Fee EUR;Total Fee EUR;;;LN Invoice;Transaction type",
        "ORD-1;buy;2026-02-01 10:00:00;600.00;0.01000000;BTC;59000.00;0;1.00;5.00;6.00;;;;onchain",
        "ORD-2;sell;2026-04-01 09:00:00;295.50;0.00500000;BTC;60000.00;0.00001000;0.60;4.50;4.50;;;lnbc-erfunden;onchain",
    ])
    rows = imp.parse_coinfinity(text)
    buy, sell = rows
    assert (buy["art"], buy["sats"], buy["eur"], buy["gebuehr_eur"]) == ("kauf", 1_000_000, 60000, 600)
    assert (sell["art"], sell["sats"], sell["eur"], sell["gebuehr_eur"], sell["gebuehr_sats"]) == \
        ("verkauf", 500_000, 30000, 450, 1_000)                                  # brutto = 0,005 × 60.000
    assert sell["vertragsdatum"] == sell["datum"] == "2026-04-01" and "ORD-2" in sell["notiz"]
    assert "lnbc" not in str(rows)
    assert btc.importieren(gmbh, "coinfinity", text, wallet="Hardware")["neu"] == 2
    assert btc.importieren(gmbh, "coinfinity", text, wallet="Hardware")["neu"] == 0


def test_own_csv_import(gmbh):
    text = ("Datum;Wallet;Art;sats;Gebühr sats;EUR;Vertragsdatum;Notiz\n"
            "01.02.2026;Hardware;kauf;1000000;0;600,00;;Kauf erfunden\n"
            "2026-04-01;Hardware;verkauf;400000;1000;300,00;2026-03-30;\n")
    assert btc.importieren(gmbh, "kontor", text)["neu"] == 2
    assert btc.importieren(gmbh, "kontor", text)["neu"] == 0
    v = next(m for m in btc.bewegungen(gmbh) if m["art"] == "verkauf")
    assert (v["vertragsdatum"], v["gebuehr_sats"], v["eur"]) == ("2026-03-30", 1000, 30000)
    with pytest.raises(KontorError, match="Wallet"):
        btc.importieren(gmbh, "kontor", text.replace("Hardware", "Unbekannt"))


# -- Zuordnungsvorschläge ---------------------------------------------------------------------------------------------
def test_incoming_payment_suggests_the_invoice(gmbh, invoice):
    btc.importieren(gmbh, "bitbox", BITBOX, wallet="Hardware")
    eingang = next(m for m in btc.bewegungen(gmbh) if m["art"] == "eingang")
    s = btc.vorschlag(gmbh, eingang)
    assert (s["art"], s["document_id"], s["eur"]) == ("kundenzahlung", invoice["id"], 35700)
    btc.zuordnen(gmbh, eingang["id"], art="kundenzahlung", rechnung=invoice["number"])
    btc.bestaetigen(gmbh, eingang["id"])
    assert docs.get_document(gmbh, invoice["id"])["status"] == "bezahlt"


def test_incoming_payment_matches_by_value_only_with_one_candidate(gmbh, invoice):
    btc.kurs_speichern(gmbh, "2026-03-20", "70000")                            # 500.000 sats = 350,00 (−2 %)
    mid = btc.erfassen(gmbh, {"wallet": "Hardware", "art": "eingang", "datum": "2026-03-20", "sats": 500_000})
    assert btc.vorschlag(gmbh, btc.get(gmbh, mid))["document_id"] == invoice["id"]
    late = btc.erfassen(gmbh, {"wallet": "Hardware", "art": "eingang", "datum": "2026-06-01", "sats": 500_000})
    btc.kurs_speichern(gmbh, "2026-06-01", "70000")
    assert btc.vorschlag(gmbh, btc.get(gmbh, late)) is None                     # mehr als 60 Tage nach Rechnungsdatum


def test_payment_of_an_invoice_in_sats_suggests_eur_at_the_daily_close(gmbh):
    btc.kurs_speichern(gmbh, "2026-05-02", "50000")
    mid = btc.erfassen(gmbh, {"wallet": "Hardware", "art": "zahlung", "datum": "2026-05-02", "sats": 100_000})
    s = btc.vorschlag(gmbh, btc.get(gmbh, mid))
    assert (s["art"], s["eur"]) == ("zahlung", 5000)                           # bestätigt wird von Hand


def test_sale_payout_suggests_the_bank_transaction_on_the_clearing_account(gmbh):
    md.set_profile(gmbh, {"bankimport": "datev"})
    bank.add_account(gmbh, HAUPT, "Hauptkonto", "1813")
    bank.import_datev(gmbh, '"TESTDEFFXXX";"' + HAUPT + '";;;03.04.2026;03.04.2026;297,00;"Handelsplatz Beispiel";;;"";'
                            '"Auszahlung";"";"";"";;"EUR";"x";"x";')
    neu(gmbh, art="kauf", datum="2026-02-01", sats=1_000_000, eur="600,00")
    sale = neu(gmbh, art="verkauf", datum="2026-04-01", vertragsdatum="2026-04-01", sats=400_000, eur="300,00",
               gebuehr_eur="3,00")
    s = btc.bank_vorschlag(gmbh, btc.get(gmbh, sale))
    assert s and s["account"] == "1370"
    btc.bank_zuordnen(gmbh, sale)
    t = bank.transactions(gmbh, 2026)[0]
    assert (t["kind"], t["target_account"], t["tax_key"]) == ("konto", "1370", "0")
    assert jr.balances(gmbh, 2026)["1370"] == -60000 + 30000 - 300 - 29700


def test_transfer_needs_a_target_and_discarded_drafts_stay_discarded(gmbh):
    btc.importieren(gmbh, "bitbox", BITBOX, wallet="Hardware")
    ausgang = next(m for m in btc.bewegungen(gmbh) if m["art"] == "ausgang")
    btc.zuordnen(gmbh, ausgang["id"], art="umbuchung", ziel="Handelsplatz")
    assert btc.get(gmbh, ausgang["id"])["ziel_wallet_id"] != ausgang["wallet_id"]
    eingang = next(m for m in btc.bewegungen(gmbh) if m["art"] == "eingang")
    btc.verwerfen(gmbh, eingang["id"], "Gegenstück einer Umbuchung")
    assert btc.importieren(gmbh, "bitbox", BITBOX, wallet="Hardware")["neu"] == 0
    assert btc.get(gmbh, eingang["id"])["status"] == "verworfen"


# -- Kommandozeile ----------------------------------------------------------------------------------------------------
def test_cli_refuses_without_switch_and_works_with_it(tmp_path, monkeypatch, capsys):
    import json

    from kontor.cli.main import main
    monkeypatch.setenv("KONTOR_DATA", str(tmp_path / "daten"))
    assert main(["btc", "wallet"]) == 1
    assert "Profilschalter bitcoin" in json.loads(capsys.readouterr().err)["fehler"]
    f = tmp_path / "p.json"
    f.write_text('{"bitcoin": "ja", "gewinnermittlung": "bilanz"}', encoding="utf-8")
    assert main(["profil", "setzen", "--json", str(f)]) == 0
    assert main(["btc", "wallet", "--name", "Hardware"]) == 0
    capsys.readouterr()
    csv = tmp_path / "b.csv"
    csv.write_text(BITBOX, encoding="utf-8")
    assert main(["btc", "import", "--format", "bitbox", "--datei", str(csv), "--wallet", "Hardware"]) == 0
    assert json.loads(capsys.readouterr().out)["neu"] == 3
    assert main(["btc", "kurse", "--datum", "2026-03-10", "--eur", "71400"]) == 0
    capsys.readouterr()
    assert main(["btc", "liste", "--status", "entwurf"]) == 0
    liste = json.loads(capsys.readouterr().out)["eintraege"]
    kauf = liste[0]["id"]
    assert main(["btc", "zuordnen", str(kauf), "--art", "kauf", "--eur", "357,00"]) == 0
    assert main(["btc", "bestaetigen", str(kauf)]) == 0
    capsys.readouterr()
    assert main(["btc", "bewertung", "--jahr", "2026"]) == 0
    assert json.loads(capsys.readouterr().out)["pool"]["ak"] == 35700
    assert main(["btc", "saldo", "--wallet", "Hardware", "--datum", "2026-12-31", "--sats", "1"]) == 0
    assert "differenz" in capsys.readouterr().out
    assert main(["btc", "bestaetigen", str(kauf), "--zurueck", "--grund", "Test"]) == 0


# -- Oberfläche -------------------------------------------------------------------------------------------------------
def test_page_only_with_switch_and_confirm_in_the_ui(tmp_path):
    from fastapi.testclient import TestClient

    from kontor.api.app import create_app
    from kontor.context import open_ctx
    ctx = open_ctx("cli", tmp_path / "d")
    md.set_profile(ctx, {"gewinnermittlung": "bilanz"})
    ctx.conn.close()
    with TestClient(create_app(tmp_path / "d"), base_url="http://127.0.0.1/m/lokal") as c:
        assert "/bitcoin" not in c.get("/").text and c.get("/bitcoin").status_code == 404
        assert "btc_konto_bestand" not in c.get("/einstellungen").text
    ctx = open_ctx("cli", tmp_path / "d")
    md.set_profile(ctx, {"bitcoin": "ja"})
    btc.wallet(ctx, "Hardware")
    mid = btc.erfassen(ctx, {"wallet": "Hardware", "art": "kauf", "datum": "2026-02-01", "sats": 1_000_000,
                             "eur": "600,00"})
    ctx.conn.close()
    with TestClient(create_app(tmp_path / "d"), base_url="http://127.0.0.1/m/lokal") as c:
        home = c.get("/").text
        assert "/m/lokal/bitcoin" in home and "Bitcoin-Bestand" in home
        page = c.get("/bitcoin?jahr=2026").text
        assert "Hardware" in page and "Entwürfe" in page
        settings = c.get("/einstellungen").text
        assert "btc_konto_bestand" in settings and "Hardware" in settings
        r = c.post(f"/bitcoin/{mid}/bestaetigen", data={"jahr": "2026"}, follow_redirects=False)
        assert r.status_code == 303
        assert "1.000.000" in c.get("/bitcoin?jahr=2026").text
        r = c.post("/bitcoin/wallet", data={"name": "Zweite", "art": "boerse"}, follow_redirects=False)
        assert r.status_code == 303 and "Zweite" in c.get("/einstellungen").text
    ctx = open_ctx("cli", tmp_path / "d")
    assert btc.get(ctx, mid)["status"] == "bestaetigt"
    assert re.search(r"BTC\d+", "".join(r["ref"] for r in jr.lines(ctx, 2026)))
    ctx.conn.close()


def test_page_renders_a_full_year(jahr, gmbh, invoice):
    from fastapi.testclient import TestClient

    from kontor.api.app import create_app
    btc.importieren(gmbh, "bitbox", BITBOX, wallet="Hardware")
    btc.saldo_eintragen(gmbh, "Hardware", "2026-12-31", 798_300)
    sale = neu(gmbh, art="verkauf", vertragsdatum="2026-12-20", datum="2027-01-05", sats=100_000, eur="10,00")
    with TestClient(create_app(gmbh.data), base_url="http://127.0.0.1/m/lokal") as c:
        page = c.get("/bitcoin?jahr=2026").text
        assert "Abschreibung 170,85 €" in page and "auf den Vertragspreis 40,00 €" in page    # 130,85 + 40,00
        assert "Vorschlag: RE-2026-0501" not in page                           # Rechnung ist schon bezahlt
        assert "948.200" in page and "100" in page
        assert c.get("/journal?jahr=2026").status_code == 200
        assert c.post(f"/bitcoin/{sale}/zurueck", data={"jahr": "2027", "grund": "Test"},
                      follow_redirects=False).status_code == 303
    assert btc.get(gmbh, sale)["status"] == "entwurf"


def test_ebilanz_maps_the_bitcoin_accounts():
    from kontor.elster import ebilanz as eb
    leaves, _ = eb.zuordnung({"1300": 9000, "6910": 1000, "4915": -500, "2900": -9500})
    assert leaves["is.netIncome.regular.operatingTC.deprAmort.currAss.misc"] == 1000
    assert leaves["is.netIncome.regular.operatingTC.otherOpRevenue.revalCurrAss.misc"] == 500
    assert leaves["bs.ass.currAss.receiv.other.other"] == 9000
    eb.hochrechnen(leaves)                                                     # beide Positionen sind zulässig


def test_payment_of_an_invoice_needs_account_and_tax_key_and_counts_as_input_vat(gmbh):
    from kontor.reports.ustva import ustva
    neu(gmbh, art="kauf", datum="2026-02-01", sats=1_000_000, eur="600,00")
    mid = btc.erfassen(gmbh, {"wallet": "Hardware", "art": "zahlung", "datum": "2026-05-02", "sats": 100_000,
                              "eur": "119,00"})
    with pytest.raises(KontorError, match="Konto und Steuerschlüssel"):
        btc.bestaetigen(gmbh, mid)
    btc.zuordnen(gmbh, mid, konto="6815", steuer="19")
    btc.bestaetigen(gmbh, mid)
    bal = jr.balances(gmbh, 2026)
    assert (bal["6815"], bal["1406"], bal["4830"]) == (10000, 1900, -11900 + 6000)   # Nennwert 119,00, Buchwert 60,00
    assert ustva(gmbh, "2026-05")["fields"]["66"]["tax"] == 1900
