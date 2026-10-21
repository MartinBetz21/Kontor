# Localising Kontor for another country

Kontor's tax logic is German today. This guide shows how to add another country so that it works for your own books and can go back into the one shared code base. It is written for you and your AI agent together.

## Ground rules

- **You know your rules, the agent writes them down.** Your country's bookkeeping and tax rules come from you, your advisor or official public sources. The agent may look up official sources with you, but it gives no advice and does not decide what applies to your business.
- **A source for every rule.** Every rate, threshold, invoice requirement, deadline and return field gets a named source: the law or the tax authority's page, with the date you read it. You confirm each one. A rule without a source does not go in.
- **Germany keeps working.** Your country is a profile, switched on per company. Nothing German is changed in place, and all existing tests still pass.
- **Your own fork first, then a pull request.** Build it in your fork with tests, use it for your own books, and send it back as a pull request ([CONTRIBUTING.md](CONTRIBUTING.md)) so the next person in your country starts from your work.
- **No real data.** Examples and tests use invented companies and amounts.

## What a country needs

Go through this checklist with your agent. Each line becomes a value in the country profile, with its source.

| Area | Questions | Where it lives today (Germany) |
|---|---|---|
| Language | Which language for invoices and the interface? | `kontor/render/templates/`, `kontor/ui/templates/` |
| Number and date format | Decimal separator, thousands separator, date order | `kontor/core/money.py`, Jinja filters in `kontor/api/app.py` and `kontor/render/pdf.py` |
| VAT or sales tax | Rates, exempt supplies, reverse charge for cross-border services, small business exemption, cash or accrual basis | `kontor/core/tax_codes.py`, `TAX_RATES` in `kontor/services/masterdata.py`, `kontor/services/autojournal.py` |
| Invoice requirements | Mandatory fields, required notes for exempt or reverse-charge invoices, numbering rules, e-invoice format | `kontor/core/checks.py`, `TREATMENT_NOTES` in `kontor/core/tax_codes.py`, `kontor/render/zugferd.py` |
| VAT return | Periods, fields of the return form, deadlines, rounding | `KZ_LABELS` and deadlines in `kontor/core/tax_codes.py`, `kontor/reports/ustva.py` |
| Profit statement | Cash basis or accruals for small businesses, non-deductible expenses | `kontor/reports/euer.py`, categories in `migrations/0001_init.sql` |
| Chart of accounts | Standard chart for companies with a balance sheet, account numbers for bank, receivables, VAT | `kontor/core/bilanz.py`, `kontor/services/autojournal.py`, `kontor/services/journal.py` |
| Filing | Manual entry in the authority's portal, or an official API with its own registration | `kontor/elster/` as the German example |
| Record keeping | Retention period, archive requirements | documented only |

Start with what you need every month (invoices, VAT figures). Electronic filing comes last, and always under the authority's own registration in your name.

## Structure of a country profile

Kontor has no country layer yet; the first localisation introduces it. Proposed shape, so that every country looks the same:

```
kontor/countries/
  __init__.py            # registry: country code → module
  de/                    # the existing German rules, moved here step by step
  xx/                    # your country (ISO 3166 code)
    __init__.py          # rates, tax kinds, invoice notes, return fields, deadlines
    SOURCES.md           # one line per rule: rule, value, source, date read, confirmed by
tests/countries/
  test_xx.py             # one test per rule, with invented amounts
```

A profile switch `land` (default `de`) in `PROFILE_SWITCHES` (`kontor/services/masterdata.py`) selects the country per company. Code reads the country's values through the registry and never through `if land == "xx"` scattered across the code base.

## Example skeleton (invented values)

The country "Examplia" (`xx`) below is invented. None of its numbers are real; replace every value with one from your `SOURCES.md`.

```python
# kontor/countries/xx/__init__.py
"""Examplia. Every value has a line in SOURCES.md; nothing here is real."""

CODE = "xx"
NAME = "Examplia"
CURRENCY = "EUR"
DECIMAL_SEPARATOR = ","
THOUSANDS_SEPARATOR = "."

# VAT rates in percent, as strings like the German ones
VAT_RATES = ("21", "9", "0")                    # SOURCES.md: rule 1 to 3

# Tax kinds for receipts (incoming invoices): key → label shown in the UI
TAX_KINDS = {
    "standard": "Standard rate",
    "reduced": "Reduced rate",
    "reverse_charge_eu": "Reverse charge EU",  # SOURCES.md: rule 4
}

# Notes that must appear on invoices for special cases
INVOICE_NOTES = {
    "reverse_charge_eu": "VAT reverse charged (example wording, replace from SOURCES.md rule 5)",
}

# VAT return: periods and field labels
RETURN_PERIODS = ("quarter",)                   # SOURCES.md: rule 6
RETURN_FIELDS = {
    "1a": "Supplies at the standard rate",      # SOURCES.md: rule 7
}
RETURN_DEADLINE_DAYS_AFTER_PERIOD = 30          # SOURCES.md: rule 8
```

```markdown
<!-- kontor/countries/xx/SOURCES.md -->
| # | Rule | Value | Source | Read on | Confirmed by |
|---|---|---|---|---|---|
| 1 | Standard VAT rate | 21 % | (law, article, official URL) | 2026-10-21 | (your name) |
```

```python
# tests/countries/test_xx.py
from kontor.countries import xx

def test_standard_rate_matches_sources():
    assert "21" in xx.VAT_RATES                 # change together with SOURCES.md rule 1
```

## How the agent walks you through it

1. Confirm the country and your legal form, and that you only want this for your own books.
2. Go through the checklist above, one area at a time. For each rule: you or the agent find the official source, the agent writes the rule with its value and source into `SOURCES.md`, you confirm it.
3. The agent writes the country module and one test per rule, then runs `pytest -q`. All German tests must still pass. The first country also adds the registry and the `land` profile switch; that is a change to the core, so it belongs in the same pull request.
4. Try it with an invented demo company first (`KONTOR_DATA` pointing at a demo folder).
5. Only then set up your real books with `kontor init` and the `land` profile.
6. Open a pull request with the country module, `SOURCES.md` and the tests ([CONTRIBUTING.md](CONTRIBUTING.md)). Mention which parts you use yourself and which are untested.

## What stays your responsibility

Your country's rules can change every year. Keep `SOURCES.md` current, check every figure before it goes to a tax authority, and remember that Kontor and its contributors give no tax advice ([DISCLAIMER.md](DISCLAIMER.md), [TERMS.md](TERMS.md)).
