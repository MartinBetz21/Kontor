# Changelog

Versions count deliveries: a version rises once per published release, by the largest kind of change since the last one (patch for fixes only, minor for something new you notice including migrations that run by themselves, major when you have to set something up again or relearn something).

## Unreleased

- VAT pre-return (UStVA 2025 and 2026) sent through ERiC as an approval job (`kontor auftrag ustva`, profile switch `ustva_eric`). Refused while the period runs, while receipts of the period are unconfirmed, and for yearly filers. A second real transmission of a period goes as a correction.
- Approval jobs (page "Freigaben"): the agent prepares ELSTER transmissions, ERiC checks them and Kontor explains the values in plain language. The human checks each job on a detail page and sends one or several with the certificate PIN. A wrong PIN keeps the job open and stops the batch; values that changed since preparing block the send. Each transmission gets a protocol PDF. Migration 0017 runs by itself.
- Payroll behind the profile switch `lohn`: employees, monthly payslips with wage tax from the BMF's official Programmablaufplan (2025, 2026) and social-insurance contributions, payslip PDF, journal lines, wage account, ELStAM via ERiC, wage-tax return (LStA) and annual certificate (LStB) via ERiC, returns sent by another program can be marked. Social-insurance reports are not sent. Migrations 0015 and 0016 run by themselves.
- Month-end page: five steps with their status, a plain-language derivation of the amount due, payment and month lock in place.
- Payments received before the invoice (`kontor zahlung ohne-rechnung`, `kontor zahlung verknuepfen`) count for VAT and EÜR at once; a finalized invoice takes them over later if amount and tax split match.
- Receipts: confirm from the inbox list (single or selected) with what is missing shown; confirmed files get a readable name (date, supplier, receipt number).
- UI: the footer no longer squeezes the content column.
- Month-end page (`/abschluss`, from the VAT page or an approval): five steps with status (receipts, income, return sent and unchanged, payment to the tax office, lock) and in plain words how the amount comes about, with every invoice and receipt; record the payment and lock the period right there. Protocol, wage account and certificate PDFs open in their own window.
- Approvals: detail page per job with labelled values and „Jetzt prüfen“ (recomputes, compares with the prepared values, ERiC checks locally, nothing is sent); after sending the result and the ELSTER protocol PDF.
- Approval jobs: a wrong or locked certificate PIN no longer closes the job as rejected; it stays open and the remaining selected jobs are not tried with the same PIN.
- Receipts: prepared receipts can be confirmed from the inbox list, one by one or several at once; incomplete ones show what is missing.
- Payment received before the invoice (`kontor zahlung ohne-rechnung`): counts for VAT and EÜR at once, shows on the overview until a finalized invoice takes it over (`kontor zahlung verknuepfen`), without counting twice.
- VAT pre-return via ERiC: `kontor auftrag ustva --zeitraum 2026-09` prepares it from the figures of the VAT page, checks it with ERiC and explains it; the human sends it under „Freigaben“. Kontor refuses while receipts of the period are unconfirmed. Later transmissions of the same period go as corrections.
- Approval jobs: the agent prepares transmissions (wage-tax return, certificate, ELStAM, E-Bilanz), checked by ERiC and explained in plain language; the human approves one or several under „Freigaben“ with one PIN entry. Payroll can be finalized and cancelled from the CLI. Footer no longer squeezes the content column. Migration 0017 runs by itself.
- Payroll: ELStAM registration, change of data transmitter, deregistration and mailbox fetching via ERiC/Otto, received attributes compared and taken over; wage-tax certificate 2026 (LSTMitteilung); employee printout of the certificate; returns sent by another program can be recorded. Migration 0016 runs by itself.
- Payroll behind the profile switch `lohn`: employees with attributes valid from a month, pay items, monthly calculation (wage tax from the BMF Programmablaufplan XML, social insurance incl. midijob and minijob), finalizing with payslip PDF, cancellation, carry-over from another program, automatic journal lines, payroll journal, wage account, employer costs, contributions per insurer, wage-tax return (check and send via ERiC) and wage-tax certificate 2025. Page "Lohn" and `kontor lohn …`. Migration 0015 runs by itself.
- Bitcoin holdings behind the profile switch `bitcoin`: wallets, movements as drafts with classification and confirmation, CSV import (own format, wallet app export, exchange order export) without addresses or transaction IDs, daily exchange rates, wallet balance check, year-end valuation (periodic weighted average, write-down and write-up), automatic journal lines, findings, page "Bitcoin" and `kontor btc …`. Migration 0014 runs by itself.
- Balance sheet: account 6910 counts as write-down on current assets; E-Bilanz maps 6910 and 4915.
- Contribution check also blocks extended public keys, Bitcoin addresses and transaction IDs.

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
