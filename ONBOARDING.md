# Onboarding: setting up Kontor for your own business with an AI agent

This file is the script for an AI agent that sets up Kontor for **one person's own business**. It works in two situations:

- The user gave you only the starter prompt from the README ("I want to use Kontor … clone it … walk me through the onboarding"). Start at step 0.
- The user already has the repository open in Claude Code and typed `/onboard`. Start at step 1.

The agent interviews the user, then writes the company profile through the Kontor CLI. It never edits the database directly and never edits code, except on the localisation path for another country (step 4), and only in the user's own copy.

## Rules for the agent

1. **Terms first.** Before anything is set up for real, show the user the core of [DISCLAIMER.md](DISCLAIMER.md) and [TERMS.md](TERMS.md) and get their yes (step 1).
2. **Never invent a value.** Every field comes from the user's answer or from a document the user points to (tax office letter, commercial register extract, bank details). If an answer is unclear, ask again. If the user does not know, leave the field empty and list it as open at the end.
3. **No tax advice, no individual cases.** When the user asks what to choose or how to book something (profit method, VAT period, cash or accrual VAT, small business rule, a particular receipt), say that their tax advisor or the letter from the tax office decides, and explain only what each option means inside Kontor and where it is set.
4. **A source for every tax rule.** On the localisation path you may research public official sources, but every rule you write down needs a named source, and the user confirms each one. You do not decide whether a rule is right for them.
5. **Own books only.** If the user wants to keep books for someone else (a client, a relative), stop and point to DISCLAIMER.md. Kontor is for your own books.
6. **Show before writing.** Before each write, show the JSON you are about to send and wait for a yes. Ask before you install anything.
7. **Say what Kontor cannot do.** If the user's situation is not supported (table below), say so plainly and do not work around it.
8. **Try with the demo company.** Anything the user only wants to try goes into a separate demo folder (`examples/demo.sh`), never into their real books.
9. Ask one topic at a time, in the user's language. Keep stored values as listed below (they are German keys and values).

## Not supported today

| Situation | What to tell the user |
|---|---|
| A country other than Germany | The tax logic is German. See step 4 and [LOCALIZATION.md](LOCALIZATION.md). |
| Small business rule (Kleinunternehmer, § 19 UStG) for outgoing invoices | Kontor writes invoices with VAT only. Not suitable without adapting the code. |
| Accrual VAT (Sollversteuerung) | The VAT figures are computed on a cash basis (Istversteuerung). The switch `besteuerung` exists but changes nothing yet. |
| Fiscal year that is not the calendar year | Not supported. |
| Chart of accounts SKR03 | Only SKR04 (balance sheet profile) or simple categories (EÜR profile). |
| Partnerships (GbR, OHG, KG) with a balance sheet | The balance sheet logic is built for small limited companies (GmbH, UG). Not tested for partnerships. |
| Keeping books for other people | Not what Kontor is for (DISCLAIMER.md). |

## Steps

### 0. Get the code (only if you started from the starter prompt)

1. Ask the user where Kontor should live, for example `~/Kontor/app`. The code folder and the books folder are different folders. The books never go inside the code.
2. Check the prerequisites and tell the user what is missing: `git --version`, a Python of 3.12 or newer (`python3 --version`, which on a Mac is often too old, then `python3.12 --version` or Homebrew), and on Linux the Pango libraries for PDFs. Windows is not tested. Suggest WSL2. Do not install system software without asking.
3. Clone and install (ask first):

```sh
git clone https://github.com/MartinBetz21/Kontor.git <folder>
cd <folder>
python3 -m venv .venv            # with a Python of 3.12 or newer
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
.venv/bin/pytest -q              # about 300 tests should pass; a few skip without the optional validator
```

   Then read README.md and AGENTS.md. In all later commands the CLI is `.venv/bin/kontor` (or `kontor` once the venv is active).

### 1. Show the terms and get a yes

Summarise [DISCLAIMER.md](DISCLAIMER.md) and [TERMS.md](TERMS.md) in the user's language: Kontor is for their own books only. It gives no tax advice and examines no individual case. There is no warranty and no support. They stay responsible for bookkeeping, deadlines and filings. Keeping books for others needs the legal qualification of their country. Ask them to read both files. Continue only after a clear yes. Their yes is recorded later by `kontor init --bedingungen-gelesen`, on their own computer only.

### 2. Country and legal form (always the first question)

Ask: in which country is the business taxed, and what legal form does it have?

- **Germany:** continue with step 3.
- **Any other country:** continue with step 4.

### 3. Germany: create the books folder

- Ask where the books should live, outside the code folder, for example `~/Kontor/<company>`.
- Create it:

```sh
.venv/bin/kontor init --daten ~/Kontor/meine-firma --id meine-firma --name "Meine Firma" --bedingungen-gelesen
```

  This creates the database and folders and adds the company to the register (`~/.config/kontor/mandanten.json`, or the file in `KONTOR_REGISTER`). The output lists what is still missing. For all later commands use `--mandant meine-firma` or `export KONTOR_MANDANT=meine-firma`. Then continue with step 5.

### 4. Another country: explain, then offer the localisation path

Tell the user plainly:

- Kontor's tax logic is German: VAT rates and rules, the German VAT pre-return (UStVA), the cash-basis profit statement (EÜR), the German chart of accounts SKR04, ELSTER and the E-Bilanz. None of that is right for their country as it is.
- What works anywhere: offers and invoices as documents (the invoice texts are German), customers, receipts as files, payments, the audit log, backups. The VAT figures, the profit statement and the filing views would be wrong.
- They must know their local bookkeeping and tax rules themselves or get them from their own advisor. You can look up public official sources with them, but you do not give advice.

Then offer three ways and let them choose:

1. **Look around with the demo company** (`examples/demo.sh` in a separate folder). Nothing is set up for real.
2. **Localise Kontor for their country** following [LOCALIZATION.md](LOCALIZATION.md): a country profile in their own fork, with tests, one source per rule, each confirmed by them, and the invitation to send it back as a pull request so it reaches everyone. Do not set up their real books until the country profile exists and its tests pass.
3. **Stop here.**

### 5. Interview (Germany)


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
| Bank import (only GmbH/UG) | Can your bank or banking software export transactions as a DATEV-format CSV? Which accounts? | profile `bankimport: "datev"`. Per account `kontor bank konto --iban … --name … --konto 18xx` (SKR04 bank account number) |
| Own transfers | Under which names do transfers between your own accounts appear (company name, your own name)? | settings `own_names` (company names, comma separated), `owner_names` (owner's personal name) |
| Foreign vendors | Which software vendors abroad bill you without German VAT? | settings `foreign_providers` (upper case, comma separated) |
| Invoices | Payment term in days, offer validity in days, payment note text, digits in invoice numbers (3 to 6), last invoice number if you switch from another program | settings `payment_days`, `offer_valid_days`, `payment_note_template` (placeholders `{betrag}`, `{faellig_am}`, `{iban}`, `{kontoinhaber}`). Profile `nummernstellen`; `kontor nummernkreis start --kreis RE --letzte N` |
| Logo | Path to an SVG, PNG or JPG | `kontor einstellungen logo <file>` |
| Fiscal year | Is your fiscal year the calendar year? (If not: stop, see above.) | nothing to store |
| Telegram receipt inbox | Leave off unless the user runs their own bot | profile `telegram_eingang: "nein"` |
| E-Bilanz (only GmbH/UG, optional) | Do you want to validate and send the E-Bilanz from Kontor? This needs ERiC and an own manufacturer ID from ELSTER, and can be set up later. | profile `e_bilanz: "ja"`, settings `finanzamt_nr`, `elster_hersteller_id`, `eric_pfad` |

### 6. Write

Send the answers in two calls (show each JSON first):

```sh
.venv/bin/kontor --mandant meine-firma einstellungen setzen --json - <<'JSON'
{"name": "…", "street": "…", "postcode": "…", "city": "…", "email": "…", "iban": "…", "ustva_period": "quartal"}
JSON
.venv/bin/kontor --mandant meine-firma profil setzen --json - <<'JSON'
{"gewinnermittlung": "euer", "kontenrahmen": "kategorien"}
JSON
```

Then the logo, bank accounts and number ranges with their own commands from the table.

### 7. Check

- `.venv/bin/kontor --mandant meine-firma einrichtung` shows `"fertig": true` when nothing required is missing. Repeat the interview for every item under `fehlt`.
- Show the user a sample PDF without writing into their books: run `examples/demo.sh` with `KONTOR_DATA` pointing at a separate, empty demo folder. If the user prefers a draft in their own books, create it and delete it afterwards with `kontor dokument loeschen <id>` (drafts only, the test customer stays in the list). Never finalize anything: finalizing is a human action in the UI.
- Start the UI (`kontor server`) and let the user check the settings page.

### 8. Hand over

Tell the user in a short list: what was stored, what is still open, what Kontor does not support for their situation, and where the data folder is (back it up). Remind them that Kontor is no tax advice and that every figure must be checked before it goes to a tax office.
