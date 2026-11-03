# Kontor specification

The original design document is private and not part of this repository. Code comments that cite "Spec 6.2", "Spec 18.7a" and similar refer to its sections.

In this repository the test suite in `tests/` is the specification. Rule: change behaviour in a test first, then in the code. If you write a new specification section for a feature or a country adaptation, add it below in English and reference it from the tests.

## Bitcoin holdings (profile switch `bitcoin`)

Tests: `tests/test_bitcoin.py`, `tests/test_check_contribution.py`. With the switch off there is no page, no `kontor btc` command and no booking.

- **No addresses, no xpub, no transaction IDs.** Movements are entered by hand or imported from a CSV export (own format, a wallet app export, an exchange order export; see `kontor/importers/bitcoin.py`). Importers drop address, transaction ID and Lightning invoice columns before anything reaches the database. Duplicates are recognised by a hash of wallet, timestamp, kind and amount; importing the same file again adds nothing.
- **Draft first.** Every movement starts as a draft, is classified (purchase, sale, customer payment, payment of a supplier invoice, transfer between own wallets, loss) and confirmed. Only confirmed movements book (journal kind `auto`). Confirming a customer payment records the payment on the invoice on the transfer date; resetting the movement to draft removes it.
- **Date** of a disposal is the transfer of ownership (block confirmation, or the trade at an exchange). A sale also carries its contract date and the agreed EUR amount; that amount is the revenue.
- **Valuation per year:** periodic weighted average over all wallets, with two pools, acquisition cost and book value. Network fees in sats are separate disposals at the average. Cumulative rounding: disposal *i* carries round(pool × cumulative_sats_i / pool_sats) − round(pool × cumulative_sats_(i−1) / pool_sats), so totals match to the cent. At 31 December: holding × min(average acquisition cost, closing rate); a write-down below book value, a write-up at most back to acquisition cost. A sale contracted on or before 31 December and transferred after it stays in the holding and is additionally written down to its contract price if that is lower. Without a closing rate nothing is booked and a finding is shown. The closing pool carries into the next year; an explicit opening pool per year can be entered.
- **Rates:** daily candle close (00:00–24:00 UTC) from public exchange OHLC endpoints, stored with source and retrieval time. Fixed order per day: Kraken (primary), Bitstamp only for days Kraken does not deliver (history before Kraken's ~720-day window, outages), a manual rate last. The source of the closing rate is shown with the valuation; it is the only rate that changes a booking. The request names only the trading pair. Network access happens only in `kontor btc kurse --abrufen`.
- **Years whose journal comes from an import** get no Bitcoin bookings; their movements still count for holding, average and book value.
- **Findings:** open drafts, wallet balance differences, missing rates, customer payments whose market value differs from the invoice amount by more than 3 %, book value in the sub-ledger ≠ balance of the holding account in the journal.

| Purpose | Setting | Default (SKR04) |
|---|---|---|
| Holding | `btc_konto_bestand` | 1300 |
| Clearing with exchange or buyer | `btc_konto_verrechnung` | 1370 |
| Sale revenue (gross) | `btc_konto_erloes` | 4830 |
| Book value of disposals | `btc_konto_buchwert` | 6969 |
| Fees | `btc_konto_gebuehr` | 6855 |
| Year-end write-down | `btc_konto_abschreibung` | 6910 |
| Write-up | `btc_konto_zuschreibung` | 4915 |
| Loss | `btc_konto_verlust` | 6969 |

| Event | Booking |
|---|---|
| Purchase | holding / clearing (amount paid, fees included) |
| Customer payment | holding / receivables 1200 (invoice amount); difference to a market value set as acquisition cost against revenue or loss |
| Sale | clearing / revenue (contract amount, gross); book value / holding; fee / clearing |
| Supplier invoice paid | like a receipt assigned in the bank: expense account (net) and input VAT (or § 13b pair) / holding (book value); difference to the invoice amount on revenue or book-value account; input VAT counts in the VAT return |
| Network fee | fee / holding (book value of the fee sats) |
| Transfer between own wallets | nothing except the network fee |
| Loss | loss / holding (book value) |
| 31 December | write-down / holding, or holding / write-up |

## Payroll (profile switch `lohn`)

Tests: `tests/test_lohn.py` (the service), `tests/test_lohnsteuer.py` (wage tax against the BMF), `tests/test_sozialversicherung.py` (social insurance), `tests/test_elster_lohn.py` and `tests/test_elstam.py` (ELSTER records). With the switch off there is no page, no `kontor lohn` command and no booking.

- **Master data.** An employee has fixed data (personnel number, name, birth date, address, IBAN, tax ID, social insurance number, entry and exit date) and **attributes valid from a month** (`lohn_merkmale`): tax class, factor, child allowances, religion of employee and spouse, monthly allowance and addition amount from ELStAM, flat-tax minijob, person group, contribution group (four digits KV/RV/AV/PV), health insurer, its additional contribution and U1/U2 rates, children under 25 and proof of parenthood for long-term care insurance, Saxony, private health and care premiums, expense account. A month uses the newest version starting on or before it. ELStAM are entered by hand or taken over from what ELSTER delivers (see ELStAM below).
- **Pay items** (`lohn_bezuege`): salary (current, taxable, contributory), one-off payment (other income, `SONSTB`), benefit in kind (taxable and contributory, deducted from net), tax-free item, flat-taxed item under § 40 EStG with its rate, deduction from net. Recurring from–to, or one month only.
- **Calculation.** A draft is recalculated on every request and stores nothing. Wage tax, solidarity surcharge and the church-tax base come from the BMF's official Programmablaufplan, executed from the published XML (`kontor/core/pap/Lohnsteuer<year>.xml`); a new year is a new file. Church tax = base × 8 or 9 % (setting `lohn_kist_satz`), rounded down to the cent; for spouses of different religion it is split in half. One-off payments: annual wage `JRE4` = taxable wage so far this year + current monthly wage × remaining months including the current one. Partial months use the daily table (LZZ 4) × calendar days. Social insurance from `kontor/core/sozialversicherung.py`: regular, midijob (transition range), minijob, none; contribution ceilings with social-insurance days, pro-rata annual ceiling for one-off payments. Minijob with flat tax: 2 % of the contributory pay, paid to the Minijob-Zentrale, not part of the wage-tax return.
- **Net**: gross (all items except deductions) − wage tax − solidarity surcharge − church tax − employee contributions. **Payout**: net − benefits in kind − deductions + employer subsidy to private health and care insurance. **Employer cost**: gross + employer contributions + U1, U2, insolvency levy + minijob flat contributions and flat tax + § 40 flat taxes + subsidies.
- **Finalizing** can be done by the human (UI with confirm token) or the agent (`kontor lohn festschreiben`, also `--bis` for all open months); the audit log names the actor. It stores the calculation with every input (`lohn_abrechnungen.daten`) and the payslip PDF/A-3b read-only under `archiv/lohn/<year>/`. Months of an employee are finalized in order (from entry, January or after a carry-over from another program); a finalized payslip cannot change (trigger). A correction (UI or `kontor lohn stornieren`) cancels the newest finalized month with a reason and finalizes it again; earlier months can only be cancelled after the later ones. Pay items and attribute versions cannot be added for months that are already finalized.
- **Carry-over** (`lohn_vortrag`): year-to-date values up to a month from another payroll program, so that a change within the year keeps annual values, wage account and certificate complete.
- **Bookings** (journal kind `auto`, ref `LOHN<id>`, date = last day of the month): expense account (attribute, else `lohn_konto_gehalt`) or the item's own account for salary, one-off, benefit in kind and § 40 items; tax-free items on `lohn_konto_steuerfrei`; benefit in kind credited to `lohn_konto_sachbezug` (no VAT on it); deductions credited to `lohn_konto_abzug`; payout to `lohn_konto_verb_lohn`, employee taxes to `lohn_konto_verb_steuer`, employee contributions to `lohn_konto_verb_sv`; employer contributions, levies and subsidies on `lohn_konto_sv_ag` against `lohn_konto_verb_sv` (subsidies against the payout); minijob flat tax on `lohn_konto_pauschsteuer` against `lohn_konto_verb_sv`; § 40 flat taxes on `lohn_konto_pauschal_lst` against `lohn_konto_verb_steuer`. Every payslip balances to zero. Payments clear the liabilities through the bank assignment.
- **Wage-tax return (LStA)**: period by setting `lohn_anmeldezeitraum` (months 01–12, quarters 41–44, year 19), due on the 10th after the period. Values from finalized payslips of the period, minijobs with flat tax excluded: Kz86 number of employees, Kz42 wage tax, Kz41 § 40 flat wage tax, Kz49 solidarity surcharge, Kz47 flat church tax, church tax per religion (ev, lt, rf, fr → Kz61; rk → Kz62; ak → Kz63; Jewish communities → Kz64; others refused because the number depends on the state). It goes out only when every employed month of the period is finalized and the period has ended; after a real transmission the next one for the same period is a corrected return (Kz10). A difference between current values and the last transmission is a finding.
- **Wage-tax certificate (LStB)** per employee and year: lines 3 to 7, 22a, 23a, 24b (subsidies to private health and care insurance together), 25 to 28, periods of the tax attributes over every employed month of the year (carried-over months use the attributes Kontor knows for them). Wage year 2025: datenart `ElsterLohn`, schema 202501. From 2026: `LSTMitteilung` (Kontrollmitteilung 7, LST detail version 2, procedure `ElsterKMV`, schema from esteuer.de, validated in tests when `KONTOR_LST_SCHEMA` points to it); line 28 no longer exists, a 2026 certificate with private insurance premiums is refused until those ELStAM values are mapped. After a real transmission every further one is a correction referring to the previous KmId. ERiC checks only the frame of a certificate. The employee printout ("Lohnzettel", § 41b (1) sentence 3 EStG) is rendered by Kontor with date, transfer ticket and KmId of the last real transmission.
- **Returns sent by another program** (change within the year): `lsta_extern` records them (transfer ticket `extern: …`) so Kontor does not send them again; differences to Kontor's own values remain a finding.
- **ELStAM** (procedure `ElsterLohn2`): registration (`DUeAnmelden_3`, from a reference date, default entry date), change of data transmitter (`DUeUmmelden_3`, reference date required; the switch from another payroll program), deregistration (`DUeAbmelden_3`, at exit). Replies arrive in the ELSTER mailbox (`ElsterDatenabholung`, `PostfachAnfrage_32`): fetching downloads each package (Otto), decrypts it with the certificate, stores it unchanged and read-only under `archiv/elster/elstam/`, parses the change list into entries (`lohn_elstam`, duplicates once) and only then confirms (`PostfachBestaetigung_32`); if storing fails nothing is confirmed. Entries are matched to employees by tax ID; differences to the attributes valid in that month are a finding until the entry is taken over (new attribute version from its month, not before entry) or discarded with a reason. Rejections are findings. Sending and fetching are human-only (PIN). Requests can be checked locally (`kontor lohn elstam --pruefen`).
- **Sending** (LStA, LStB, ELStAM) goes through approval jobs (see below) or the payroll page, human-only, with the data-protection confirmation; record and protocol go read-only to `archiv/elster/`, each transmission into `lohn_meldungen` or `lohn_elstam_vorgaenge`. ERiC code 610001003 (hints only) counts as plausible.
- **Not in Kontor**: social-insurance reports (DEÜV, contribution statements, eAU) — they need a program certified by the ITSG or sv.net; Kontor shows the values per insurer. Company pension, short-time work, garnishments, multiple employments, minimum church tax, Märzklausel unless the core supports it, ELStAM test replies (they need ELSTER's mock system).
- **Findings**: past months not finalized, missing tax ID, returns due within ten days and not sent, sent returns whose values changed, missing tax number.

| Purpose | Setting | Default (SKR04) |
|---|---|---|
| Salaries | `lohn_konto_gehalt` | 6020 |
| Employer contributions, levies, subsidies | `lohn_konto_sv_ag` | 6110 |
| Minijob flat tax | `lohn_konto_pauschsteuer` | 6040 |
| § 40 flat taxes | `lohn_konto_pauschal_lst` | 6069 |
| Benefits in kind (credit) | `lohn_konto_sachbezug` | 4947 |
| Tax-free items | `lohn_konto_steuerfrei` | 6130 |
| Deductions (credit) | `lohn_konto_abzug` | 1340 |
| Payroll liabilities | `lohn_konto_verb_lohn` | 3720 |
| Wage and church tax liabilities | `lohn_konto_verb_steuer` | 3730 |
| Social security liabilities | `lohn_konto_verb_sv` | 3740 |

## Approval jobs (Aufträge zur Freigabe)

Tests: `tests/test_auftraege.py`. Kontor is operated by an agent; transmissions to authorities need the human.

- The agent (or the human) prepares a job with `kontor auftrag lsta|lstb|elstam|abholen|ebilanz|ustva …` (`--test` for a test transmission). Kontor computes the values, lets ERiC check the record (jobs without a check: fetching the ELStAM mailbox) and stores the values plus a short plain-language explanation (what goes where, amounts, due date, whether it replaces an earlier transmission). An identical open job is refused.
- The page „Freigaben“ (with a badge in the navigation) lists open jobs. The human selects one or several, confirms the data-protection notice and enters the certificate PIN once; Kontor sends them one after the other with the same PIN, which is never stored. Each result (accepted with transfer ticket, or rejected with reason) is stored on the job and listed under „Erledigt“.
- Before sending, the values are recomputed. If they differ from the prepared ones, the job is not sent and becomes `veraltet`; prepare it again.
- **VAT pre-return** (`kontor auftrag ustva --zeitraum JJJJ-MM|JJJJ-Qn`, profile switch `ustva_eric`, schema `UStVA_<year>`, 2025 and 2026): the fields are those of `reports/ustva.py` (Kz81, 86, 21, 45, 46/47, 84/85, 66, 67), Kz83 is recomputed the way ELSTER does it and must equal Kontor's balance. Refused while the period is running, while a receipt dated in the period (or undated) is unconfirmed, and for companies with a yearly period. After a real transmission of the period every further one is a correction (Kz10). Each transmission is in the audit log (`elster_senden`, object `ustva`); record and protocol go read-only to `archiv/elster/`. The W-IdNr is sent when the VAT ID is set; without it ERiC only gives a hint.
- Open jobs can be discarded with a reason. Statuses: `offen`, `gesendet`, `abgelehnt`, `veraltet`, `verworfen`.
