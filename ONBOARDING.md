# Onboarding: setting up Kontor for your own business with an AI agent

This file is the script for an AI agent (Claude Code: `/onboard`) that sets up Kontor for **one person's own business**. The agent interviews the user, then writes the company profile through the Kontor CLI. It never edits the database directly and never edits code.

## Rules for the agent

1. **Never invent a value.** Every field comes from the user's answer or from a document the user points to (tax office letter, commercial register extract, bank details). If an answer is unclear, ask again. If the user does not know, leave the field empty and list it as open at the end.
2. **No tax advice.** When the user asks what to choose (profit method, VAT period, cash or accrual VAT, small business rule), say that their tax advisor or the letter from the tax office decides, and explain only what each option means inside Kontor.
3. **Own books only.** If it turns out the user is setting Kontor up to keep books for a client, stop and point to the disclaimer in README.md. Kontor is for your own books.
4. **Show before writing.** Before each write, show the JSON you are about to send and wait for a yes.
5. **Say what Kontor cannot do.** If the user's situation is not supported (see the table below), say so plainly and do not work around it.
6. Ask one topic at a time, in the user's language. Keep stored values as listed below (they are German keys and values).

## Not supported today

| Situation | What to tell the user |
|---|---|
| Small business rule (Kleinunternehmer, § 19 UStG) for outgoing invoices | Kontor writes invoices with VAT only. Not suitable without adapting the code. |
| Accrual VAT (Sollversteuerung) | The VAT figures are computed on a cash basis (Istversteuerung). The switch `besteuerung` exists but changes nothing yet. |
| Fiscal year that is not the calendar year | Not supported. |
| Chart of accounts SKR03 | Only SKR04 (balance sheet profile) or simple categories (EÜR profile). |
| Partnerships (GbR, OHG, KG) with a balance sheet | The balance sheet logic is built for small limited companies (GmbH, UG). Not tested for partnerships. |
| A country other than Germany | The tax logic is German. See README.md, "Adapting Kontor". |

## Steps

### 1. Prepare

- Check that the project runs: `pytest -q` in the repo.
- Ask where the books should live. The folder must be **outside** the code repository (Kontor refuses otherwise), for example `~/Kontor/<company>`.
- Show the user the core of [DISCLAIMER.md](DISCLAIMER.md) and [TERMS.md](TERMS.md) in their language (own books only, no tax advice, no warranty, they stay responsible for bookkeeping and filings, bookkeeping for others needs the legal qualification) and ask them to read both files. Continue only after they say yes.
- Create it (the flag records their yes on their own computer):

```sh
kontor init --daten ~/Kontor/meine-firma --id meine-firma --name "Meine Firma" --bedingungen-gelesen
```

  This creates the database and folders and adds the company to the register (`~/.config/kontor/mandanten.json`, or the file in `KONTOR_REGISTER`). The output lists what is still missing. For all later commands use `--mandant meine-firma` or `export KONTOR_MANDANT=meine-firma`.

  For a single company without a register, `export KONTOR_DATA=~/Kontor/meine-firma` and `kontor init --daten ~/Kontor/meine-firma` also work.

### 2. Interview

Ask in this order. The right column is where the answer goes.

| Topic | Question | Stored as |
|---|---|---|
| Company | Legal name (or your own name as a sole proprietor), trading name, street, postcode, city, country, e-mail, phone | settings `name`, `trade_name`, `street`, `postcode`, `city`, `country` (2 letters), `email`, `phone` |
| Legal form | Sole proprietor or freelancer, or a limited company (GmbH, UG)? | profile `gewinnermittlung`: `euer` for sole proprietors and freelancers, `bilanz` for GmbH/UG |
| Company register (only GmbH/UG) | Seat, register court, register number, managing directors, share capital, outstanding share capital | settings `seat`, `register_court`, `register_number`, `managing_directors`, `share_capital`, `share_capital_outstanding` (amounts like `25000,00`) |
| Chart of accounts | Derived, not asked: EÜR uses categories, GmbH/UG uses SKR04 | profile `kontenrahmen`: `kategorien` or `skr04` |
| VAT status | Do you charge VAT? (If small business rule: stop, see above.) How often do you file the VAT pre-return (monthly, quarterly, yearly)? | settings `ustva_period`: `monat`, `quartal` or `jahr` |
| Tax numbers | Tax number from the tax office letter (or "not yet assigned"), VAT ID if you have one | settings `tax_number` or `tax_number_pending: "1"`, `vat_id` |
| Bank for invoices | IBAN, BIC, bank name printed on invoices | settings `iban`, `bic`, `bank_name` |
| Bank import (only GmbH/UG) | Can your bank or banking software export transactions as a DATEV-format CSV? Which accounts? | profile `bankimport: "datev"`; per account `kontor bank konto --iban … --name … --konto 18xx` (SKR04 bank account number) |
| Own transfers | Under which names do transfers between your own accounts appear (company name, your own name)? | settings `own_names` (company names, comma separated), `owner_names` (owner's personal name) |
| Foreign vendors | Which software vendors abroad bill you without German VAT? | settings `foreign_providers` (upper case, comma separated) |
| Invoices | Payment term in days, offer validity in days, payment note text, digits in invoice numbers (3 to 6), last invoice number if you switch from another program | settings `payment_days`, `offer_valid_days`, `payment_note_template` (placeholders `{betrag}`, `{faellig_am}`, `{iban}`, `{kontoinhaber}`); profile `nummernstellen`; `kontor nummernkreis start --kreis RE --letzte N` |
| Logo | Path to an SVG, PNG or JPG | `kontor einstellungen logo <file>` |
| Fiscal year | Is your fiscal year the calendar year? (If not: stop, see above.) | nothing to store |
| Telegram receipt inbox | Leave off unless the user runs their own bot | profile `telegram_eingang: "nein"` |
| E-Bilanz (only GmbH/UG, optional) | Do you want to validate and send the E-Bilanz from Kontor? This needs ERiC and an own manufacturer ID from ELSTER, and can be set up later. | profile `e_bilanz: "ja"`, settings `finanzamt_nr`, `elster_hersteller_id`, `eric_pfad` |

### 3. Write

Send the answers in two calls (show each JSON first):

```sh
kontor --mandant meine-firma einstellungen setzen --json - <<'JSON'
{"name": "…", "street": "…", "postcode": "…", "city": "…", "email": "…", "iban": "…", "ustva_period": "quartal"}
JSON
kontor --mandant meine-firma profil setzen --json - <<'JSON'
{"gewinnermittlung": "euer", "kontenrahmen": "kategorien"}
JSON
```

Then the logo, bank accounts and number ranges with their own commands from the table.

### 4. Check

- `kontor --mandant meine-firma einrichtung` shows `"fertig": true` when nothing required is missing. Repeat the interview for every item under `fehlt`.
- Show the user a sample PDF without writing into their books: run `examples/demo.sh` with `KONTOR_DATA` pointing at a separate, empty demo folder. If the user prefers a draft in their own books, create it and delete it afterwards with `kontor dokument loeschen <id>` (drafts only; the test customer stays in the list). Never finalize anything: finalizing is a human action in the UI.
- Start the UI (`kontor server`) and let the user check the settings page.

### 5. Hand over

Tell the user in a short list: what was stored, what is still open, what Kontor does not support for their situation, and where the data folder is (back it up). Remind them that Kontor is no tax advice and that every figure must be checked before it goes to a tax office.
