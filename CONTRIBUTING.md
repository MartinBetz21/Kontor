# Contributing to Kontor

<!-- status:start -->
Thank you for helping. Please read this before you open a pull request.
<!-- status:end -->

Kontor has one code base, this repository. Adaptations for another language, country or bank should come back here as a module or profile, not live on in a separate fork. That way every improvement reaches everyone, and your own installation stays a plain copy of this repository plus your own data.

Kontor is used for real bookkeeping, so changes have to be careful and tested.

## Ground rules

1. **Neutral core only.** Build every feature for any installation. Behaviour that differs between users becomes a setting, a profile switch or an entry in the instance file, never an `if` on one company, person or bank. Country adaptations go behind a profile switch, and the German behaviour keeps working.
2. **No real data.** No real names, addresses, tax numbers, VAT IDs, IBANs, amounts, invoice numbers, file paths or host names in code, tests, fixtures, screenshots, issues or commit messages. Use invented examples such as "Example GmbH" and `DE89370400440532013000`. Never commit certificates, keys, PINs, ELSTER manufacturer IDs, databases or data folders.
3. **Tests first.** Add or change a test, then the code. `pytest -q` must pass. The tests are the specification.
4. **One feature per pull request.** Small and focused, with a short note on what changes for users.
5. **No tax advice in the product.** Do not add wording that presents Kontor as tax advice or as a service for keeping other people's books (see [TERMS.md](TERMS.md)).
6. **Schema changes** are new numbered files in `migrations/`. Never edit an applied migration.

## How to contribute

1. Fork the repository and create a branch for one feature: `git checkout -b feature/english-invoice`.
2. Switch on the local check once per clone: `git config core.hooksPath .githooks`. It blocks certificates, keys, databases, data folders and real-looking data before they reach a commit.
3. Write the test, then the code. Run `pytest -q` and `python3 tools/check_contribution.py --all`.
<!-- signoff:start -->
4. Sign off every commit: `git commit -s`. The sign-off certifies the Developer Certificate of Origin 1.1 (https://developercertificate.org): you wrote the change or have the right to submit it under the project licence. Forgot it? `git rebase --signoff main` and push again.
<!-- signoff:end -->
5. Open a pull request against `main`. Use the template and tick the checklist.

## What happens with your pull request

Every pull request runs the same checks: the tests on Python 3.12 and 3.13, the contribution check (no real data, no secrets, no certificates) and the sign-off check. When all checks are green and the maintainer has approved, GitHub merges it into `main` automatically (squash merge). For pull requests from forks, GitHub does not let the workflow switch on auto-merge, so the maintainer does it with one click when approving. Nothing is merged without that approval; for bookkeeping software a human look at outside code is part of the safety.

## Where to extend Kontor

Please build adaptations at these points, so they stay switchable and other users keep their behaviour:

| What | Where | How |
|---|---|---|
| A language for screens or invoices | `kontor/ui/templates/`, `kontor/render/templates/` | Translate displayed text only. Stored values (status names, CLI areas) stay as they are. |
| A chart of accounts | `kontor/core/bilanz.py`, `kontor/services/autojournal.py` | A new value of the profile switch `kontenrahmen`, with its own account mapping. |
| VAT rules of a country | `kontor/core/tax_codes.py`, `kontor/services/masterdata.py`, `kontor/reports/ustva.py` | A profile switch for the country; rates, invoice notes and return fields per country. |
| A bank export format | `kontor/importers/` | A new importer next to `datev.py` and a new value of `bankimport`. Bank names never appear in the code. |
| Filing with a tax authority | `kontor/elster/` as the example | Produce the figures for manual entry first. Electronic filing needs the authority's own registration, which every user does for themselves. |

The full map of Germany-specific modules is in [README.md](README.md#adapting-kontor-to-another-language-or-country).

## Licensing of contributions

<!-- licensing:start -->
Contributions are licensed under the Apache License 2.0: what you submit is under the same licence (section 5 of the licence). The sign-off above is all we ask for; no separate agreement is needed.
<!-- licensing:end -->

## Name

"Kontor" is the name of this project. Forks are welcome. Please give a modified distribution its own name and do not suggest that it comes from the original author.
