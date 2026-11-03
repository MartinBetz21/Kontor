"""Inhaltsregeln von tools/check_contribution.py für Bitcoin (Spec 18.14). Die Muster werden hier zur Laufzeit
zusammengesetzt, damit diese Datei sich nicht selbst meldet."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "tools" / "check_contribution.py"
spec = importlib.util.spec_from_file_location("check_contribution", TOOL)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def base58check(version: int, payload: bytes) -> str:
    raw = bytes([version]) + payload
    raw += hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    n, out = int.from_bytes(raw, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def funde(text: str) -> list[str]:
    return cc.pruefe([("beispiel.py", text.encode())])


def test_extended_public_key_is_reported():
    assert any("öffentlicher Schlüssel" in f for f in funde("k = '" + "z" + "pub" + "6" * 107 + "'"))


def test_bech32_address_is_reported():
    assert any("Bitcoin-Adresse" in f for f in funde("an " + "bc" + "1q" + "x" * 38))


def test_base58_address_with_valid_checksum_is_reported():
    address = base58check(0x05, bytes(range(1, 21)))
    assert address[0] == "3" and any("Bitcoin-Adresse" in f for f in funde(f"Adresse: {address}"))


def test_base58_lookalike_without_checksum_is_not_reported():
    address = base58check(0x00, bytes(range(1, 21)))
    broken = address[:-1] + ("2" if address[-1] != "2" else "3")
    assert funde(f"Kennung {broken}") == []


def test_transaction_id_is_reported_only_near_tx_words():
    tx_hex = "9f" * 32
    assert any("Transaktions-ID" in f for f in funde(f"txid = '{tx_hex}'"))
    assert any("Transaktions-ID" in f for f in funde(f"Transaction ID,{tx_hex}"))
    assert funde(f"sha256 der Datei: {tx_hex}") == []                          # Hash in anderem Zusammenhang


@pytest.mark.parametrize("value", sorted(cc.BTC_ERLAUBT))
def test_invented_test_values_are_allowed(value):
    assert funde(f"Address,Transaction ID\n{value},{value}") == []
