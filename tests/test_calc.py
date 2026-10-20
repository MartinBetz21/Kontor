from kontor.core.calc import Allowance, Line, compute, line_total


def test_line_total_rounds_half_up():
    assert line_total("3", 75000) == 225000
    assert line_total("1.5", 3333) == 5000   # 4999,5 → 5000


def test_golden_offer_with_discount():
    t = compute([Line("1", 400000, "19"), Line("1", 250000, "19"), Line("3", 60000, "19")],
                [Allowance(-80000, "19")])
    assert (t.lines_net, t.allowances, t.charges) == (830000, 80000, 0)
    assert (t.net, t.tax, t.gross) == (750000, 142500, 892500)


def test_golden_prozessanalyse():
    t = compute([Line("1", 100000, "19")], [])
    assert (t.net, t.tax, t.gross) == (100000, 19000, 119000)


def test_golden_small_invoice():
    t = compute([Line("1", 25126, "19")], [])
    assert (t.net, t.tax, t.gross) == (25126, 4774, 29900)


def test_two_rates_grouped_and_rounded_once_per_rate():
    t = compute([Line("1", 1001, "19"), Line("1", 1001, "19"), Line("1", 1000, "7")], [])
    groups = {g.rate: (g.basis, g.tax) for g in t.groups}
    assert groups == {"19": (2002, 380), "7": (1000, 70)}
    assert t.gross == 2002 + 380 + 1000 + 70


def test_storno_negative_lines():
    t = compute([Line("1", -100000, "19")], [])
    assert (t.net, t.tax, t.gross) == (-100000, -19000, -119000)


def test_as_dict_roundtrip_keys():
    d = compute([Line("1", 100000, "19")], []).as_dict()
    assert d["groups"] == [{"rate": "19", "basis": 100000, "tax": 19000}]
