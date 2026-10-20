# Changelog

Versions count deliveries: a version rises once per published release, by the largest kind of change since the last one (patch: fixes only; minor: something new you notice, including migrations that run by themselves; major: you have to set something up again or relearn something).

## 0.2.0 (2026-10-20)

First public release of the neutral core.

- Instance file: companies, data folders and installation-wide defaults live outside the code (`KONTOR_REGISTER`).
- Onboarding with an AI agent (`/onboard`, ONBOARDING.md), `kontor init` with a one-time confirmation of TERMS.md and DISCLAIMER.md, `kontor einrichtung`.
- Neutral bank import profile `datev` (DATEV-format CSV).
- Payment note and invoice footer name the account holder (banks check the payee name).
- Security: host check against DNS rebinding, frame protection, all unsafe methods checked against cross-site requests, nosniff, database 600 and data folder 700, receipt dates from foreign e-invoices validated.
- Contribution structure: CI (tests on Python 3.12 and 3.13, contribution check, DCO), auto-merge after approval, Dependabot, pre-commit hook.
