"""Lohnsteuer, Solidaritätszuschlag und Kirchensteuer-Bemessungsgrundlage nach dem amtlichen Programmablaufplan (PAP)
des BMF für den maschinellen Lohnsteuerabzug. Kein nachgebauter Steuercode: das Modul liest den PAP, den das BMF
jedes Jahr als Pseudocode-XML veröffentlicht (`pap/Lohnsteuer<Jahr>.xml`, unverändert), und führt ihn mit den
Rechenregeln von Java-BigDecimal aus. Neues Jahr = neue XML-Datei in `pap/`, keine Codeänderung.

Aufbau: Der Ausdruck-Parser kennt nur die Grammatik, die im PAP vorkommt (BigDecimal-Aufrufe, int-Rechnung, Vergleiche,
`&&`, `||`, Feldzugriff). Alles andere bricht beim Laden mit einem Fehler ab. Aus jeder Methode des PAP entsteht einmal
Python-Quelltext aus dieser Whitelist; er wird je Jahr übersetzt und zwischengespeichert.

Beträge im PAP sind Cent (Eingaben und Ausgaben). Kirchensteuer selbst (8 % / 9 % von BK) rechnet dieses Modul nicht.

Die wichtigsten Ausgaben von `lohnsteuer()` (Name → PAP-Feld):
    lst               LSTLZZ    einzubehaltende Lohnsteuer des Lohnzahlungszeitraums, Cent
    soli              SOLZLZZ   Solidaritätszuschlag dazu, Cent
    kist_basis        BK        Bemessungsgrundlage Kirchenlohnsteuer, Cent (nur wenn R > 0)
    lst_sonst         STS       Lohnsteuer auf den sonstigen Bezug (SONSTB), Cent
    soli_sonst        SOLZS     Solidaritätszuschlag auf den sonstigen Bezug, Cent
    kist_basis_sonst  BKS       Bemessungsgrundlage Kirchenlohnsteuer auf den sonstigen Bezug, Cent
    vkv               VKVLZZ    nur PAP 2025: berücksichtigte Beiträge zur privaten Basis-KV/Pflege (laufend), Cent
    vkv_sonst         VKVSONST  nur PAP 2025: dasselbe beim sonstigen Bezug, Cent (kann negativ sein)
Fehlt ein Feld im PAP des Jahres (vkv in 2026), ist der Wert 0. `berechne()` liefert alle Ausgaben mit PAP-Namen."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from decimal import (ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP,
                     ROUND_UP, Context, Decimal, DivisionByZero, Inexact, InvalidOperation, Overflow)
from fractions import Fraction
from functools import cache
from pathlib import Path

from .errors import KontorError

PAP_DIR = Path(__file__).parent / "pap"

# ---- BigDecimal-Semantik -------------------------------------------------------------------------------------------
# add/subtract/multiply sind bei BigDecimal exakt; hoher Kontext + Inexact-Falle statt stillem Runden.
_EXAKT = Context(prec=300, rounding=ROUND_HALF_EVEN, Emin=-9999, Emax=9999,
                 traps=[InvalidOperation, DivisionByZero, Overflow, Inexact])
_RUNDEN = Context(prec=300, rounding=ROUND_HALF_EVEN, Emin=-9999, Emax=9999,
                  traps=[InvalidOperation, DivisionByZero, Overflow])
_MODI = {"UP": ROUND_UP, "DOWN": ROUND_DOWN, "CEILING": ROUND_CEILING, "FLOOR": ROUND_FLOOR,
         "HALF_UP": ROUND_HALF_UP, "HALF_DOWN": ROUND_HALF_DOWN, "HALF_EVEN": ROUND_HALF_EVEN}
_D = Decimal
_NULL = Decimal(0)


def _valueof(x):
    if isinstance(x, bool):
        raise TypeError("BigDecimal.valueOf(bool)")
    return Decimal(x) if isinstance(x, int) else x


def _add(a, b):
    return _EXAKT.add(a, b)


def _sub(a, b):
    return _EXAKT.subtract(a, b)


def _mul(a, b):
    return _EXAKT.multiply(a, b)


def _divx(a, b):
    """divide(b) ohne Skala: exakt oder ArithmeticException wie in Java."""
    return _EXAKT.divide(a, b)


def _divs(a, b, skala, modus):
    """divide(b, skala, modus) über Bruchrechnung, ohne Zwischenrundung."""
    q = Fraction(a) / Fraction(b) * 10 ** skala
    n, d = q.numerator, q.denominator
    fl, rest = divmod(n, d)                    # floor und Rest (0 <= rest < d)
    if rest == 0:
        r = fl
    elif modus == "DOWN":
        r = fl if n >= 0 else fl + 1
    elif modus == "UP":
        r = fl + 1 if n >= 0 else fl
    elif modus == "FLOOR":
        r = fl
    elif modus == "CEILING":
        r = fl + 1
    else:
        zwei = 2 * rest
        if zwei < d:
            r = fl
        elif zwei > d:
            r = fl + 1
        elif modus == "HALF_UP":
            r = fl + 1 if n >= 0 else fl
        elif modus == "HALF_DOWN":
            r = fl if n >= 0 else fl + 1
        else:                                  # HALF_EVEN
            r = fl if fl % 2 == 0 else fl + 1
    return Decimal(r).scaleb(-skala, context=_RUNDEN)


def _setscale(a, skala, modus):
    return a.quantize(Decimal(1).scaleb(-skala, context=_RUNDEN), rounding=_MODI[modus], context=_RUNDEN)


def _cmp(a, b):
    return (a > b) - (a < b)


def _long(a):
    return int(a)                              # BigDecimal.longValue(): Richtung null abgeschnitten


def _bd(x):
    return Decimal(x) if isinstance(x, int) else x


def _out(d):
    if d == 0:
        return Decimal(0)                      # kein „-0“ (Java kennt keine negative Null)
    if d.as_tuple().exponent > 0:
        return d.quantize(Decimal(1), context=_RUNDEN)
    return d


_HELFER = {"_D": _D, "_valueof": _valueof, "_add": _add, "_sub": _sub, "_mul": _mul, "_divx": _divx, "_divs": _divs,
           "_setscale": _setscale, "_cmp": _cmp, "_long": _long, "_bd": _bd, "_int": int, "_NULL": _NULL, "_ONE": Decimal(1),
           "_TEN": Decimal(10)}

# ---- Ausdrücke: Tokenizer, Parser, Quelltext ------------------------------------------------------------------------
_TOKEN = re.compile(r"\s*(?:(?P<num>\d+\.\d+|\d+)|(?P<id>[A-Za-z_][A-Za-z_0-9]*)|(?P<op>&&|\|\||==|!=|<=|>=|[-+<>!=().,\[\]{}]))")
_VORRANG = {"||": 1, "&&": 2, "==": 3, "!=": 3, "<": 4, ">": 4, "<=": 4, ">=": 4, "+": 5, "-": 5}
_PYOP = {"||": "or", "&&": "and"}


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise KontorError(f"PAP: Zeichen nicht erlaubt in {text!r} ab {text[pos:pos + 12]!r}")
        pos = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.t = _tokens(text)
        self.i = 0

    def _fehler(self, was: str):
        raise KontorError(f"PAP: {was} in {self.text!r}")

    def _peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def _next(self):
        tok = self._peek()
        self.i += 1
        return tok

    def _expect(self, op: str):
        if self._next() != ("op", op):
            self._fehler(f"„{op}“ erwartet")

    def fertig(self):
        if self.i != len(self.t):
            self._fehler("unerwartetes Ende")

    def ausdruck(self, minimum: int = 1):
        links = self._unaer()
        while True:
            kind, val = self._peek()
            if kind != "op" or val not in _VORRANG or _VORRANG[val] < minimum:
                return links
            self.i += 1
            rechts = self.ausdruck(_VORRANG[val] + 1)
            links = ("bin", val, links, rechts)

    def _unaer(self):
        kind, val = self._peek()
        if (kind, val) == ("op", "!"):
            self.i += 1
            return ("not", self._unaer())
        if (kind, val) == ("op", "-"):
            self.i += 1
            return ("neg", self._unaer())
        return self._postfix(self._primaer())

    def _primaer(self):
        kind, val = self._next()
        if kind == "num":
            return ("dez", val) if "." in val else ("int", int(val))
        if kind == "id":
            if val == "new":
                k2, v2 = self._next()
                if (k2, v2) != ("id", "BigDecimal"):
                    self._fehler("nur new BigDecimal erlaubt")
                self._expect("(")
                arg = self.ausdruck()
                self._expect(")")
                return ("new", arg)
            return ("name", val)
        if (kind, val) == ("op", "("):
            e = self.ausdruck()
            self._expect(")")
            return e
        if (kind, val) == ("op", "{"):
            elems = []
            while True:
                elems.append(self.ausdruck())
                k2, v2 = self._next()
                if (k2, v2) == ("op", "}"):
                    return ("liste", elems)
                if (k2, v2) != ("op", ","):
                    self._fehler("„,“ oder „}“ erwartet")
        self._fehler(f"unerwartetes Token {val!r}")

    def _postfix(self, e):
        while True:
            kind, val = self._peek()
            if (kind, val) == ("op", "."):
                self.i += 1
                k2, name = self._next()
                if k2 != "id":
                    self._fehler("Name nach „.“ erwartet")
                if self._peek() == ("op", "("):
                    self.i += 1
                    args = []
                    if self._peek() != ("op", ")"):
                        while True:
                            args.append(self.ausdruck())
                            if self._peek() == ("op", ","):
                                self.i += 1
                                continue
                            break
                    self._expect(")")
                    e = ("call", e, name, args)
                else:
                    e = ("feld", e, name)
            elif (kind, val) == ("op", "["):
                self.i += 1
                idx = self.ausdruck()
                self._expect("]")
                e = ("idx", e, idx)
            else:
                return e

    def zuweisung(self):
        k, name = self._next()
        if k != "id" or self._next() != ("op", "="):
            self._fehler("Zuweisung „NAME = Ausdruck“ erwartet")
        rhs = self.ausdruck()
        self.fertig()
        return name, rhs


class _Uebersetzer:
    """AST → Python-Quelltext. Nur Bezeichner aus VARIABLES/CONSTANTS, nur die Methoden, die der PAP braucht."""

    def __init__(self, typen: dict[str, str], konstanten: set[str]):
        self.typen = typen
        self.konst = konstanten

    def e(self, n) -> str:
        art = n[0]
        if art == "int":
            return str(n[1])
        if art == "dez":
            return f"_D('{n[1]}')"
        if art == "name":
            if n[1] in self.typen:
                return f"S['{n[1]}']"
            if n[1] in self.konst:
                return f"C['{n[1]}']"
            raise KontorError(f"PAP: unbekannter Bezeichner {n[1]!r}")
        if art == "not":
            return f"(not {self.e(n[1])})"
        if art == "neg":
            return f"(-{self.e(n[1])})"
        if art == "bin":
            _, op, a, b = n
            return f"({self.e(a)} {_PYOP.get(op, op)} {self.e(b)})"
        if art == "new":
            if n[1][0] != "int":
                raise KontorError("PAP: new BigDecimal nur mit ganzer Zahl (Dezimalzahl wäre binär ungenau)")
            return f"_D({n[1][1]})"
        if art == "idx":
            if n[1][0] != "name" or n[1][1] not in self.konst:
                raise KontorError("PAP: Index nur auf Konstanten-Tabellen")
            return f"C['{n[1][1]}'][{self.e(n[2])}]"
        if art == "feld":
            return self._feld(n)
        if art == "call":
            return self._aufruf(n)
        raise KontorError(f"PAP: Ausdruck {art!r} nicht unterstützt")

    @staticmethod
    def _feld(n) -> str:
        if n[1] != ("name", "BigDecimal"):
            raise KontorError(f"PAP: Feldzugriff .{n[2]} nicht erlaubt")
        if n[2] in ("ZERO", "ONE", "TEN"):
            return "_" + {"ZERO": "NULL", "ONE": "ONE", "TEN": "TEN"}[n[2]]
        raise KontorError(f"PAP: BigDecimal.{n[2]} nicht erlaubt")

    @staticmethod
    def _modus(n) -> str:
        if n[0] == "feld" and n[1] == ("name", "BigDecimal") and n[2].startswith("ROUND_") and n[2][6:] in _MODI:
            return repr(n[2][6:])
        raise KontorError(f"PAP: Rundungsmodus erwartet, nicht {n!r}")

    @staticmethod
    def _skala(n) -> str:
        if n[0] != "int":
            raise KontorError("PAP: Skala muss eine ganze Zahl sein")
        return str(n[1])

    def _aufruf(self, n) -> str:
        _, obj, name, args = n
        if obj == ("name", "BigDecimal"):
            if name == "valueOf" and len(args) == 1:
                return f"_valueof({self.e(args[0])})"
            raise KontorError(f"PAP: BigDecimal.{name}() nicht erlaubt")
        o = self.e(obj)
        if name in ("add", "subtract", "multiply", "compareTo") and len(args) == 1:
            fn = {"add": "_add", "subtract": "_sub", "multiply": "_mul", "compareTo": "_cmp"}[name]
            return f"{fn}({o}, {self.e(args[0])})"
        if name == "divide" and len(args) == 1:
            return f"_divx({o}, {self.e(args[0])})"
        if name == "divide" and len(args) == 3:
            return f"_divs({o}, {self.e(args[0])}, {self._skala(args[1])}, {self._modus(args[2])})"
        if name == "setScale" and len(args) == 2:
            return f"_setscale({o}, {self._skala(args[0])}, {self._modus(args[1])})"
        if name == "longValue" and not args:
            return f"_long({o})"
        raise KontorError(f"PAP: Aufruf .{name}({len(args)} Argumente) nicht erlaubt")


_COERCE = {"int": "_int", "double": "_bd", "BigDecimal": "_bd"}


def _zeilen(knoten, ue: _Uebersetzer, methoden: set[str], tiefe: int) -> list[str]:
    """Anweisungen einer Methode oder eines THEN/ELSE-Zweigs als Python-Zeilen."""
    ein = "    " * tiefe
    out: list[str] = []
    for k in knoten:
        if k.tag == "EVAL":
            name, rhs = _Parser(k.attrib["exec"]).zuweisung()
            if name not in ue.typen:
                raise KontorError(f"PAP: Zuweisung an unbekanntes Feld {name!r}")
            out.append(f"{ein}S['{name}'] = {_COERCE[ue.typen[name]]}({ue.e(rhs)})")
        elif k.tag == "EXECUTE":
            m = k.attrib["method"]
            if m not in methoden:
                raise KontorError(f"PAP: unbekannte Methode {m!r}")
            out.append(f"{ein}m_{m}(S, C)")
        elif k.tag == "IF":
            p = _Parser(k.attrib["expr"])
            cond = p.ausdruck()
            p.fertig()
            out.append(f"{ein}if {ue.e(cond)}:")
            dann = k.find("THEN")
            sonst = k.find("ELSE")
            out += (_zeilen(list(dann), ue, methoden, tiefe + 1) if dann is not None else []) or [f"{ein}    pass"]
            if sonst is not None:
                rumpf = _zeilen(list(sonst), ue, methoden, tiefe + 1)
                if rumpf:
                    out.append(f"{ein}else:")
                    out += rumpf
        else:
            raise KontorError(f"PAP: Element <{k.tag}> nicht unterstützt")
    return out


class _Pap:
    def __init__(self, jahr: int, xml: bytes):
        wurzel = ET.fromstring(xml)
        self.jahr = jahr
        self.eingabe: dict[str, str] = {}
        self.ausgabe: list[str] = []
        typen: dict[str, str] = {}
        self._defaults: dict[str, str | None] = {}
        for gruppe, ziel in (("INPUTS", "i"), ("OUTPUTS", "o"), ("INTERNALS", "x")):
            for g in wurzel.find("VARIABLES").iter(gruppe):
                for v in g:
                    name, typ = v.attrib["name"], v.attrib["type"]
                    if typ not in _COERCE:
                        raise KontorError(f"PAP: Typ {typ!r} von {name} nicht unterstützt")
                    typen[name] = typ
                    self._defaults[name] = v.attrib.get("default")
                    if ziel == "i":
                        self.eingabe[name] = typ
                    elif ziel == "o":
                        self.ausgabe.append(name)
        self.typen = typen
        # Konstanten (Tabellen und Zahlen) mit dem Parser lesen, nie roh auswerten
        namen = {c.attrib["name"] for c in wurzel.find("CONSTANTS")}
        ue = _Uebersetzer(typen, namen)
        self.konst: dict = {}
        for c in wurzel.find("CONSTANTS"):
            p = _Parser(c.attrib["value"])
            ast = p.ausdruck()
            p.fertig()
            if ast[0] == "liste":
                src = "[" + ", ".join(ue.e(x) for x in ast[1]) + "]"
            else:
                src = ue.e(ast)
            self.konst[c.attrib["name"]] = eval(src, {"__builtins__": {}, **_HELFER}, {})  # noqa: S307 (Whitelist-Quelltext)
        # Methoden
        knoten = {"MAIN": wurzel.find("METHODS").find("MAIN")}
        for m in wurzel.find("METHODS").findall("METHOD"):
            knoten[m.attrib["name"]] = m
        quelle = []
        for name, k in knoten.items():
            rumpf = _zeilen(list(k), ue, set(knoten), 1) or ["    pass"]
            quelle.append(f"def m_{name}(S, C):\n" + "\n".join(rumpf))
        ns: dict = {"__builtins__": {}, **_HELFER}
        exec(compile("\n\n".join(quelle), f"<Lohnsteuer{jahr}.xml>", "exec"), ns)   # noqa: S102 (Whitelist-Quelltext)
        self.main = ns["m_MAIN"]

    def start(self) -> dict:
        s: dict = {}
        for name, typ in self.typen.items():
            d = self._defaults.get(name)
            if d is None:
                s[name] = 0 if typ == "int" else Decimal(0)
                continue
            p = _Parser(d)
            ast = p.ausdruck()
            p.fertig()
            s[name] = eval(_Uebersetzer(self.typen, set()).e(ast), {"__builtins__": {}, **_HELFER}, {})  # noqa: S307
            s[name] = int(s[name]) if typ == "int" else _bd(s[name])
        return s


def _pap_dateien() -> dict[int, Path]:
    out = {}
    for p in PAP_DIR.glob("Lohnsteuer*.xml"):
        m = re.fullmatch(r"Lohnsteuer(\d{4})\.xml", p.name)
        if m:
            out[int(m.group(1))] = p
    return out


def jahre() -> list[int]:
    """Jahre, für die ein Programmablaufplan ausgeliefert wird."""
    return sorted(_pap_dateien())


@cache
def _lade(jahr: int) -> _Pap:
    datei = _pap_dateien().get(jahr)
    if datei is None:
        raise KontorError(f"Für {jahr} liegt kein Programmablaufplan vor (vorhanden: "
                          f"{', '.join(map(str, jahre())) or 'keiner'}). Die XML des BMF kommt nach "
                          f"kontor/core/pap/Lohnsteuer{jahr}.xml.")
    return _Pap(jahr, datei.read_bytes())


def eingaben(jahr: int) -> dict[str, str]:
    """Eingabefelder des PAP mit Typ (int, double, BigDecimal)."""
    return dict(_lade(jahr).eingabe)


def _wert(name: str, typ: str, v):
    if isinstance(v, bool):
        raise KontorError(f"Lohnsteuer: {name} ist kein Wahrheitswert")
    try:
        if isinstance(v, float):
            v = repr(v)
        d = Decimal(v) if not isinstance(v, Decimal) else v
        d = d.scaleb(0, context=_RUNDEN)
        if not d.is_finite():
            raise ValueError
    except (ValueError, ArithmeticError, TypeError):
        raise KontorError(f"Lohnsteuer: {name}={v!r} ist keine Zahl") from None
    if typ == "int":
        if d != d.to_integral_value():
            raise KontorError(f"Lohnsteuer: {name} muss eine ganze Zahl sein, nicht {v!r}")
        return int(d)
    return d


def berechne(jahr: int, werte: dict[str, int | str | Decimal]) -> dict[str, Decimal]:
    """Alle PAP-Ausgaben des Jahres (Cent). Unbekannte Eingaben sind ein Fehler, fehlende nehmen den PAP-Vorgabewert;
    LZZ und STKL ohne Vorgabe im PAP (2025) müssen angegeben werden."""
    pap = _lade(jahr)
    unbekannt = sorted(set(werte) - set(pap.eingabe))
    if unbekannt:
        raise KontorError(f"Lohnsteuer {jahr}: unbekannte Eingabe {', '.join(unbekannt)}. "
                          f"Erlaubt: {', '.join(sorted(pap.eingabe))}")
    for pflicht in ("LZZ", "STKL"):
        if pflicht in pap.eingabe and pflicht not in werte and pap._defaults.get(pflicht) is None:
            raise KontorError(f"Lohnsteuer {jahr}: {pflicht} fehlt (im PAP ohne Vorgabewert)")
    s = pap.start()
    for name, v in werte.items():
        s[name] = _wert(name, pap.eingabe[name], v)
    if s.get("STKL", 1) not in range(1, 7):
        raise KontorError(f"Lohnsteuer: Steuerklasse {s['STKL']} gibt es nicht (1 bis 6)")
    if s.get("LZZ", 1) not in range(1, 5):
        raise KontorError(f"Lohnsteuer: Lohnzahlungszeitraum {s['LZZ']} gibt es nicht (1 Jahr, 2 Monat, 3 Woche, 4 Tag)")
    try:
        pap.main(s, pap.konst)
    except (ArithmeticError, ZeroDivisionError) as e:
        raise KontorError(f"Lohnsteuer {jahr}: Rechenfehler im PAP ({type(e).__name__}) bei {werte}") from e
    return {name: _out(s[name]) for name in pap.ausgabe}


_HAUPT = {"lst": "LSTLZZ", "soli": "SOLZLZZ", "kist_basis": "BK", "lst_sonst": "STS", "soli_sonst": "SOLZS",
          "kist_basis_sonst": "BKS", "vkv": "VKVLZZ", "vkv_sonst": "VKVSONST"}


def lohnsteuer(jahr: int, **werte) -> dict[str, int]:
    """Hauptausgaben als ganze Cent, siehe Modul-Docstring. Eingaben mit PAP-Namen, z. B.
    `lohnsteuer(2026, LZZ=2, STKL=1, RE4=450000, R=1)`."""
    voll = berechne(jahr, werte)
    out = {}
    for kurz, pap in _HAUPT.items():
        d = voll.get(pap, Decimal(0))
        if d != d.to_integral_value():
            raise KontorError(f"Lohnsteuer: {pap}={d} ist nicht ganzzahlig")
        out[kurz] = int(d)
    return out
