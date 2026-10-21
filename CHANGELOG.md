# Changelog

Versions count deliveries: a version rises once per published release, by the largest kind of change since the last one (patch for fixes only, minor for something new you notice including migrations that run by themselves, major when you have to set something up again or relearn something).

## 0.2.1 (2026-10-21)

- Start here: a starter prompt in the README lets an AI agent clone, install and onboard from the repository link alone.
- Onboarding asks for the country first. Outside Germany it explains what is German-specific and offers the demo company or the localisation path (LOCALIZATION.md).
- Terms of use revised (TERMS.md, version 2026-10-21): liability clause, no answers to individual cases, intended for business use. `kontor init` asks for the confirmation again.
- Before sending to ELSTER, Kontor asks you to confirm the tax administration's data protection notices (current wording from the ELSTER developer area).
- NOTICE carries only copyright and origin; SECURITY.md added.

## 0.2.0 (2026-10-20)

First public release of the neutral core.

- Instance file: companies, data folders and installation-wide defaults live outside the code (`KONTOR_REGISTER`).
- Onboarding with an AI agent (`/onboard`, ONBOARDING.md), `kontor init` with a one-time confirmation of TERMS.md and DISCLAIMER.md, `kontor einrichtung`.
- Neutral bank import profile `datev` (DATEV-format CSV).
- Payment note and invoice footer name the account holder (banks check the payee name).
- Security: host check against DNS rebinding, frame protection, all unsafe methods checked against cross-site requests, nosniff, database 600 and data folder 700, receipt dates from foreign e-invoices validated.
- Contribution structure: CI (tests on Python 3.12 and 3.13, contribution check, DCO), auto-merge after approval, Dependabot, pre-commit hook.
