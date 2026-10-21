# Kontor

Kontor is a local bookkeeping and invoicing app for small businesses. It was originally built by Martin Betz ([AI Traction](https://ai-traction.de) and UTXO Solutions GmbH) to keep the books of his own companies, and is shared as-is so that others can run it for their own books and adapt it to other languages and countries. See [About this project](#about-this-project).

It runs on your own computer (FastAPI web UI on 127.0.0.1, SQLite, PDFs on disk). There is no cloud service, no account and no telemetry. A command line interface that speaks JSON lets an AI coding agent such as Claude Code prepare drafts, while every irreversible step stays with a human in the UI.

## Start here

You need an AI coding agent that can run commands on your computer (Kontor was built with [Claude Code](https://claude.com/claude-code), but others work too), `git`, and Python 3.12 or newer. Kontor runs on macOS and Linux. Windows is not tested; WSL2 is the most likely way. Everything stays on your computer.

Copy this into your agent:

```text
I want to use Kontor, an open-source bookkeeping app for my own business: https://github.com/MartinBetz21/Kontor
Clone it into a folder of my choice, read AGENTS.md and ONBOARDING.md, and walk me through the onboarding step by step. Ask me before you install anything.
```

The agent checks the prerequisites, installs Kontor, shows you the terms, asks which country your business is taxed in and then sets up your company or, outside Germany, explains what is missing and walks you through [LOCALIZATION.md](LOCALIZATION.md). Already inside the repository in Claude Code? Type `/onboard`.

Other agents (Codex CLI, Gemini CLI, Cursor, Aider and the like) work with the same prompt as long as they can run shell commands in a folder you choose. A chat without access to your computer (Claude in the browser or desktop app without a terminal) can explain Kontor, but it cannot install or run it. You would type every command yourself. There is no hosted version, and the tax logic is German.

> **Read this first.** Kontor is free software for keeping **your own** books. It is not tax, legal or accounting advice and does not replace a tax advisor. There is no warranty. Check every figure and every filing yourself. If you keep books or prepare tax filings for other people, you need the qualification your country's law requires (in Germany: Steuerberatungsgesetz, §§ 2, 3, 5 and 6 (1) no. 1 StBerG). The full texts are [DISCLAIMER.md](DISCLAIMER.md) and the terms of use [TERMS.md](TERMS.md); `kontor init` asks you to confirm them once.

## What it does today

- Offers and invoices with line items and discounts. An accepted offer becomes a partial or final invoice. Invoices can be cancelled (Storno) or corrected. Finalized invoices become PDF/A-3b with an embedded ZUGFeRD / Factur-X XML (profile EN 16931).
- Sequential, gap-free number series (`RE-2026-001`, `AN-2026-001`), configurable width, and a starting number for migrating from another program.
- Receipts (Belege): upload or import, pre-filled by an agent, confirmed by a human, then filed under `belege/YYYY/MM/`. Receipts with several VAT rates, business meals (70/30 split), self-issued receipts (Eigenbeleg).
- Payments, cash-basis VAT (Istversteuerung), month locks after the VAT return.
- VAT pre-return figures (UStVA, by ELSTER field number) to type into Mein ELSTER, EÜR (cash-basis profit statement), a yearly VAT overview.
- For companies that keep double-entry books: journal on SKR04 accounts, opening balances from a trial balance, bank statement import (DATEV CSV format) with matching rules, balance sheet and P&L for micro companies, a list of known booking traps, year lock.
- E-Bilanz (electronic balance sheet, XBRL taxonomy 6.8) generated, validated locally with ELSTER's ERiC library and, with your own manufacturer ID, sent. ERiC is not included.
- Offer pipeline with expiry and follow-up tasks, recurring invoices (series), a subscription overview.
- Several separate companies (Mandanten) in one install, each with its own data folder, colour and profile.
- An append-only audit log with a hash chain, archive checksums and an integrity check (`kontor pruefen`).

The UI, the CLI commands, the invoice layout and the error messages are in **German**. The tax logic is **German** (UStG, EStG, HGB). See [Adapting Kontor](#adapting-kontor-to-another-language-or-country).

## Quick start

Requirements: Python 3.12 or newer, and the system libraries WeasyPrint needs for PDFs (on macOS `brew install pango`, on Debian/Ubuntu `apt install libpango-1.0-0 libpangoft2-1.0-0`). Tested locally on macOS with Python 3.14. The CI runs the tests on Python 3.12 and 3.13 on Linux.

On a Mac, `python3 --version` often shows Apple's older Python 3.9, which is too old. Install a current one first (`brew install python@3.12` or from python.org) and use that binary, for example `python3.12`, in the first command below.

```sh
git clone <repository-url> kontor && cd kontor
python3 -m venv .venv                    # python3 must be 3.12 or newer
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
.venv/bin/pytest -q                      # about 300 tests; 8 skip without the optional e-invoice validator
```

Set up your own company with an AI agent: open the repo in Claude Code and run `/onboard`. The agent follows [ONBOARDING.md](ONBOARDING.md), interviews you and writes your company profile through the CLI. It is instructed never to invent values and to leave open what you don't know. Without an agent, the same steps by hand:

```sh
.venv/bin/kontor init --daten ~/Kontor/meine-firma --id meine-firma --name "Meine Firma"   # outside this repo; asks you to confirm TERMS.md once
export KONTOR_MANDANT=meine-firma
.venv/bin/kontor einstellungen setzen --json company.json    # name, address, tax number, IBAN, VAT period …
.venv/bin/kontor profil setzen --json profile.json           # EÜR or balance sheet, chart of accounts …
.venv/bin/kontor einrichtung                                 # lists what is still missing
.venv/bin/kontor server                                      # http://127.0.0.1:8792
```

`kontor init` creates the data folder and adds the company to the instance file `~/.config/kontor/mandanten.json` (set `KONTOR_REGISTER` first to keep it somewhere else). The first time, it refuses until you have read [TERMS.md](TERMS.md) and [DISCLAIMER.md](DISCLAIMER.md) and add `--bedingungen-gelesen`. Your confirmation is stored in `~/.config/kontor/bedingungen.json` on your computer and nowhere else. The two JSON files contain only the keys you want to set. All keys and allowed values are listed in [ONBOARDING.md](ONBOARDING.md). A minimal example for a sole proprietor:

```json
{"name": "Erika Mustermann", "street": "Musterstraße 1", "postcode": "10115", "city": "Berlin",
 "email": "info@example.com", "iban": "DE89370400440532013000", "tax_number_pending": "1", "ustva_period": "quartal"}
```

```json
{"gewinnermittlung": "euer", "kontenrahmen": "kategorien"}
```

Just looking around (invented data, no confirmation needed): `export KONTOR_DATA=~/kontor-demo` (any folder outside this repo), `KONTOR=.venv/bin/kontor examples/demo.sh` for an invented company with a draft invoice, then `.venv/bin/kontor server` (the tenant id is `lokal`).

### The instance file

Code and data are separate. One code base (this repo) serves every installation. Everything that belongs to one installation lives outside the code:

- each company's **data folder** (database, archive, receipts),
- the **instance file** (register): which companies exist, where their folders are, and installation-wide defaults. Default location `~/.config/kontor/mandanten.json`, or set `KONTOR_REGISTER`. Example: [`examples/mandanten.example.json`](examples/mandanten.example.json).

The instance file may set `basis` (root for relative data paths), `eingang` (host and folder of the Telegram receipt inbox), and `einstellungen` / `profil` defaults, either for all companies or per company. Order of precedence: code default < instance file (top) < instance file (company) < value stored in the company database. Unknown keys are refused, so a typo does not silently do nothing.

On macOS, `app/build.sh` builds `~/Applications/Kontor.app`, a small pywebview window around the same server.

### Environment variables

| Variable | Purpose |
|---|---|
| `KONTOR_DATA` | One data folder, used as tenant `lokal`. Overrides the register. The tests use it. |
| `KONTOR_REGISTER` | Path to the tenant register JSON. Default `~/.config/kontor/mandanten.json`. |
| `KONTOR_MANDANT` | Default tenant id for the CLI. |
| `KONTOR_STATE` | Where the UI remembers the last tenant. Default `~/.config/kontor`. |
| `KONTOR_ERIC` | Platform folder of the ERiC library (also settable per tenant in the UI). |
| `KONTOR_EINGANG_HOST`, `KONTOR_EINGANG_ORDNER` | SSH host and folder for the optional Telegram receipt inbox (profile switch `telegram_eingang`, off by default). Override `eingang` in the instance file. |

### Optional tools

- `tools/Mustang-CLI.jar` (Mustang project, Apache-2.0) plus a Java runtime validates generated e-invoices. Tests that need it skip when it is missing. Download it from the Mustang project releases and place it in `tools/` (it is gitignored).
- ELSTER ERiC is only needed for E-Bilanz validation and sending. Get it from the ELSTER developer area under its own licence, register as a manufacturer to receive your own Hersteller-ID, and set `KONTOR_ERIC`. Never commit ERiC, certificates or your Hersteller-ID.

## Architecture

```
kontor/
  config.py        tenant register, data folder rules (data never inside the code repo)
  context.py       Ctx = DB connection + data folder + actor ('ui' human, 'cli' agent, 'system')
  core/            pure logic, no I/O: money (integer cents), calc (EN 16931 rounding), tax_codes,
                   checks (mandatory invoice fields), status, snapshot, allocation, bilanz (SKR04 to balance sheet)
  services/        the only layer the UI and CLI call: documents, finalize, payments, receipts, periods,
                   journal, autojournal, bank, pipeline, series, subscriptions, masterdata, integrity, elster
  reports/         read-only: overview, ustva, euer, abschluss (balance sheet, traps), export (CSV)
  render/          pdf.py (HTML to PDF/A-3b via WeasyPrint), zugferd.py (CII XML), templates/, assets/ (CSS, fonts)
  importers/       datev.py (bank CSV), lexware.py (migration from Lexware API JSON), telegram.py (receipt inbox), base.py
  elster/          ebilanz.py (XBRL dataset), eric.py (ctypes binding to ERiC), taxonomie_6_8.json
  store/           db.py (SQLite, migrations, tx()), audit.py (hash-chained log)
  api/             app.py (FastAPI factory, per-tenant routing, confirm tokens), pages.py (all routes)
  ui/              Jinja templates + htmx, app.css, app.js
  cli/main.py      the `kontor` command: JSON in, JSON out, errors as JSON on stderr with exit code 1
migrations/        numbered SQL files, applied in order on every connect
app/               macOS window wrapper and app bundle builder
tests/             pytest suite, the executable specification
tools/             check_contribution.py (no real data, no secrets), check_dco.py (sign-off), ebilanz_taxonomie.py
.githooks/         pre-commit hook that runs check_contribution.py (git config core.hooksPath .githooks)
.github/           CI (tests, contribution check, DCO), auto-merge after approval, Dependabot, CODEOWNERS, templates
examples/          instance file example, demo data script
ONBOARDING.md      script for the AI onboarding (`/onboard`, .claude/commands/onboard.md)
```

A data folder contains `kontor.db` (SQLite), `archiv/YYYY/` (finalized PDFs and snapshots, read-only), `belege/YYYY/MM/` (confirmed receipts), `eingang/` (unconfirmed receipts) and `abgaben/` (E-Bilanz files and trial balances). Back it up. It is your bookkeeping.

### Rules the code enforces

- Amounts are integers in cents everywhere. Parse with `core.money.parse_eur`, format with `fmt_eur`.
- A finalized document is immutable. Its snapshot (`core/snapshot.py`) is the truth for PDF, XML and later display, never the current master data. Corrections create a new version with a reason.
- Human-only actions raise `HumanOnly` unless the actor is `ui`. Only a human finalizes or cancels a document and confirms or deletes a receipt. Locking a month or year and sending to ELSTER are human actions too. The CLI deliberately has no commands for them. Irreversible UI actions also need the per-tenant confirm token from the form.
- All writes go through `store.db.tx()` and are logged by `store.audit.log()`.
- Code never branches on a tenant id. Differences between companies are profile switches (`services/masterdata.PROFILE_SWITCHES`: profit method EÜR or balance sheet, VAT on cash basis (an accrual switch exists but is not implemented), chart of accounts, number width, bank import, E-Bilanz, Telegram inbox). A test checks this.
- Schema changes are new files in `migrations/` (`NNNN_name.sql`). Never edit an applied migration.

Comments mention "Spec 6.2", "Spec 18.7a" and so on. They refer to the author's private design document, which is not published. The tests describe the same behaviour and are the reference (see [SPEC.md](SPEC.md)).

## Working on Kontor with an AI agent

This README is written so that an agent can orient itself. Good first prompts:

- "Read README.md and AGENTS.md, run the tests, then explain how an invoice goes from draft to finalized PDF."
- "Add English as a second language for the invoice PDF. Propose the approach first."
- "Adapt the VAT logic for Austria (20 %, 13 %, 10 %). List every file that needs to change before editing."

Conventions for changes:

0. **One neutral core, no instance data in it.** Every feature is built here, neutrally, for any installation. Your own installation is this code plus your instance file and data folders, never a modified copy of the code. Real names, addresses, tax numbers, IBANs, amounts, paths or host names never go into code, tests, fixtures or docs. Anything an installation needs becomes a setting, a profile switch or an entry in the instance file.
1. Write or change a test first (`tests/test_<area>.py`), then the code. The suite runs in about 20 seconds.
2. Keep business logic in `core/` (pure) or `services/`. `api/pages.py` and `cli/main.py` only translate input and output.
3. Use `KONTOR_DATA` pointing at a temporary folder for experiments. Never point it at real books while developing.
4. Run `pytest -q` before every commit.

### Bank import

The profile switch `bankimport` selects the import format. Today there is one neutral profile:

| Value | Format |
|---|---|
| `datev` | Bank transactions in DATEV format as CSV (semicolon separated, quoted, Latin-1), as exported by many German banking programs. Columns used (0-based): 1 own IBAN, 4 booking date, 5 value date (used when present), 6 amount (`-1.234,56`), 7 counterparty name, 11 and 12 purpose, 8 to 10 and 13 to 14 searched for the counterparty IBAN. |

Each bank account is registered once with its SKR04 account number (`kontor bank konto --iban … --name Hauptkonto --konto 1800`). Transfers between your own accounts are recognised by your own IBANs, by `own_names` (company names) and by pairing. A new format is a new importer next to `importers/datev.py` plus a new value of `bankimport`. Bank names never appear in the code.

### Using the CLI as an agent

```sh
kontor kunde anlegen --json customer.json
kontor rechnung entwurf --json invoice.json          # creates a draft; a human finalizes it in the UI
kontor beleg erfassen receipt.pdf                    # lands in the inbox
kontor beleg vorschlag 12 --json fields.json         # agent pre-fills; a human confirms
kontor auswertung ustva 2026-Q3
kontor --help                                        # all areas; `kontor <area> --help` for actions
```

Areas: `einstellungen`, `profil`, `kunde`, `position`, `textvorlage`, `angebot`, `rechnung`, `dokument`, `zahlung`, `beleg`, `eingang`, `auswertung`, `bank`, `journal`, `export`, `pipeline`, `serie`, `abo`, `elster`, `nummernkreis`, `altbestand`, plus `init`, `einrichtung`, `mandanten`, `pruefen`, `backup` and `server`. `examples/demo.sh` shows the JSON shapes for settings, customers and invoices.

## Adapting Kontor to another language or country

### What is Germany-specific

| Concern | Where it lives |
|---|---|
| Number and date formats (`1.234,50`, `DD.MM.YYYY`) | `core/money.py`, Jinja filters in `api/app.py` and `render/pdf.py` |
| VAT rates 19 / 7 / 0 % | `services/masterdata.TAX_RATES`, `core/tax_codes.py`, `services/autojournal.py`, `services/bank.TAX_KEYS` |
| Reverse charge, EU and non-EU B2B rules, invoice notes | `core/tax_codes.py` (`TAX_KINDS`, `TAX_TREATMENTS`, `TREATMENT_NOTES`, `EU_COUNTRIES`) |
| Mandatory invoice fields (§ 14 UStG, § 35a GmbHG) | `core/checks.py` |
| VAT return fields and deadlines (UStVA Kennzahlen) | `core/tax_codes.py` (`KZ_LABELS`, deadlines), `reports/ustva.py` |
| Cash-basis profit statement (EÜR), meal deduction 70/30 | `reports/euer.py`, categories seeded in `migrations/0001_init.sql` |
| Chart of accounts SKR04 and balance sheet layout | `core/bilanz.py`, `services/autojournal.py`, `services/journal.py`, `services/bank.py`, `reports/abschluss.py` |
| E-Bilanz (XBRL) and ELSTER transmission | `elster/`, `services/elster.py`, `tools/ebilanz_taxonomie.py` |
| Bank and migration formats (DATEV CSV, Lexware) | `importers/datev.py`, `importers/lexware.py` |
| E-invoice ZUGFeRD / Factur-X (EN 16931) | `render/zugferd.py` (usable across the EU, not only in Germany) |

The EN 16931 rounding in `core/calc.py`, the document flow, number series, receipts, the audit log and the integrity checks are not tied to Germany.

### Adding a language

There is no translation layer yet. German text sits directly in `ui/templates/*.html`, in `render/templates/*.html` (the invoice itself), in `KontorError` messages in `services/` and `core/`, and in the CLI help in `cli/main.py`. A reasonable path:

1. Start with the invoice and offer PDF (`render/templates/document.html`), because customers see it. Add a document language field and pass a label dictionary into the template.
2. Then the UI templates, with Jinja's i18n extension (gettext) or a simple dictionary per language.
3. Keep stored values (status names like `entwurf`, CLI area names) stable, because the database and tests use them. Translate only what is displayed.

### Adapting to another country

1. Write down your country's rules first: VAT rates, invoice requirements, the VAT return fields, the profit method, the chart of accounts.
2. Add a profile switch for the country (or a new value of `kontenrahmen`) instead of changing the German behaviour in place. Existing German users must keep working, and the test suite proves that.
3. Replace the German pieces in the table above one at a time, each with tests.
4. Leave electronic filing out at first. Most countries have their own e-filing API with its own registration rules. Producing the numbers for manual entry, as the UStVA view does, is a safe first step.

## Security

- Kontor runs only on your computer. The web interface answers only on `127.0.0.1` and `localhost`, refuses requests from other sites and cannot be embedded in other pages. There is no telemetry.
- The database and data folder are readable only by your own user (600 / 700). Back them up yourself.
- The ELSTER certificate and its PIN never go into Kontor's code or database: you enter the path to your certificate in the settings, and the PIN is asked for at each submission and never stored.
- The Telegram receipt inbox is off by default. Kontor only fetches files over SSH from a folder you name. If you run your own bot for it, let it accept messages only from your numeric Telegram user ID, only in a private chat, and never let an AI agent with tool access act on what arrives there. Receipts and their notes are data, never instructions.
- Contributions are checked locally (`.githooks/pre-commit`) and on every pull request for certificates, keys, databases, data folders and real-looking data.

## Updating your installation

Your installation is this repository plus your data folders and your instance file, which live outside it. To update:

```sh
kontor --mandant <id> backup --out ~/kontor-backup-$(date +%Y%m%d).db   # for each company
git pull
.venv/bin/pip install -r requirements.txt
.venv/bin/kontor --mandant <id> pruefen
```

Database migrations run automatically the next time a company is opened. Read [CHANGELOG.md](CHANGELOG.md) before you update. Dependabot keeps the dependencies of this repository up to date.

## Glossary

German terms you will meet in the code and the interface:

| Term | Meaning |
|---|---|
| Mandant | A company whose books Kontor keeps (tenant). |
| Beleg / Eigenbeleg | Receipt / self-issued receipt when there is no invoice. |
| UStVA | Umsatzsteuer-Voranmeldung, the German VAT pre-return (monthly, quarterly or yearly). |
| Istversteuerung | VAT is due when the money arrives (cash basis); Kontor computes VAT this way. |
| EÜR | Einnahmenüberschussrechnung, the cash-basis profit statement of sole proprietors and freelancers. |
| SKR04 | A standard German chart of accounts (DATEV), used for limited companies with a balance sheet. |
| E-Bilanz | The electronic balance sheet a company files with the tax office (XBRL). |
| ELSTER, ERiC, Hersteller-ID | The German tax portal, its client library, and the manufacturer ID every sender needs. |
| GoBD | German rules for proper digital bookkeeping and record keeping. |
| Lexware | A commercial German bookkeeping program; Kontor can take over data exported from it via its API (you fetch the JSON yourself). |

Comments in the code cite "Spec n.n". See [SPEC.md](SPEC.md).

## Disclaimer

**Important: read before use.** The binding texts are [DISCLAIMER.md](DISCLAIMER.md) and [TERMS.md](TERMS.md). Kontor is free software for keeping **your own** books. It is not tax, legal or accounting advice, and it does not replace a tax advisor or accountant. Neither the author nor the contributors answer questions about how to book or tax a particular transaction, in issues, courses, communities or direct messages. Such questions belong with your own tax advisor.

Kontor is provided without warranty of any kind, to the extent permitted by applicable law. Liability is limited as described in [TERMS.md](TERMS.md), section 7 (under German law: only for intent and gross negligence while Kontor is given free of charge). You use it at your own risk. Check every figure, every booking and every filing yourself before you rely on it or send it to a tax authority. You alone are responsible for your books and your tax returns.

The Germany-specific parts (SKR04 accounts, tax codes, VAT pre-returns (UStVA), ELSTER submission, EÜR, E-Bilanz) reflect one user's setup at one point in time. Tax rules, forms and data formats change every year. These parts may be outdated, incomplete or wrong for your situation. The bundled E-Bilanz taxonomy is version 6.8, which does not apply to fiscal years starting after 31 December 2025. Other countries are not covered unless you adapt the software yourself.

If you keep books or prepare tax filings for other people, you need the qualification and registration your country's law requires. In Germany that means you must be authorised under the Tax Advisory Act (Steuerberatungsgesetz, §§ 2, 3, 5 and 6 (1) no. 1 StBerG). Outside Germany, check your local rules (for example anti-money-laundering supervision for bookkeepers in the UK, or a PTIN for paid US tax preparers).

Kontor does not include the ELSTER ERiC library. If you want to submit returns electronically, obtain ERiC from the official ELSTER developer area under its own licence terms and use your own manufacturer ID (Hersteller-ID). Whoever sends through ERiC is the software manufacturer under the ERiC licence and must show the tax administration's data protection notices; Kontor asks you to confirm that you have read the current wording before each submission. Kontor is not affiliated with or endorsed by the German tax administration, ELSTER, DATEV or Lexware.

## About this project

Kontor was originally built by Martin Betz (AI Traction, https://ai-traction.de, and UTXO Solutions GmbH) to keep the books of his own companies. It is shared as-is for anyone who wants to use it for their own bookkeeping. Neither the author nor his companies keep books, prepare filings or give tax advice for people who use Kontor. There is no support, no service and no warranty. If Kontor is useful to you, a mention of where it came from is welcome. See also [NOTICE](NOTICE).

## License

<!-- license:start -->
Kontor is licensed under the Apache License 2.0, see [LICENSE](LICENSE). Copies and derivatives must keep the [NOTICE](NOTICE) file (Apache-2.0, section 4). The licence covers the code, not the name "Kontor".
<!-- license:end -->

Bundled third-party files are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: fork, one feature per branch, tests first, sign off every commit (`git commit -s`), keep real data out, and build adaptations for other countries and languages as switchable modules here instead of in a separate fork. Pull requests are merged automatically once all checks are green and the maintainer has approved. For pull requests from forks the maintainer switches on auto-merge when approving.
