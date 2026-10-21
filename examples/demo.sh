#!/usr/bin/env bash
# Fills an empty data folder with invented demo data through the CLI, then prints a draft invoice PDF path.
# Usage: KONTOR_DATA=~/kontor-demo examples/demo.sh   (the folder must be outside this repo)
set -euo pipefail
: "${KONTOR_DATA:?Set KONTOR_DATA to an empty folder outside the repo, e.g. ~/kontor-demo}"
K="${KONTOR:-kontor}"
"$K" einstellungen setzen --json - > /dev/null <<'JSON'
{"name": "Erika Mustermann", "trade_name": "Example Consulting", "street": "Musterstraße 1", "postcode": "10115",
 "city": "Berlin", "email": "info@example.com", "tax_number": "123/456/78901", "iban": "DE89370400440532013000",
 "ustva_period": "quartal"}
JSON
"$K" kunde anlegen --json - > /dev/null <<'JSON'
{"company": "Beispiel Technik GmbH", "contact": "Max Beispiel", "street": "Lindenallee 5", "postcode": "50667", "city": "Köln"}
JSON
"$K" rechnung entwurf --json - > /dev/null <<'JSON'
{"customer_id": 1, "service_date": "2026-09-30", "subject": "Workshop",
 "lines": [{"title": "Workshop Prozessanalyse", "quantity": "1", "unit": "psch.", "unit_price": "1.000,00"}]}
JSON
"$K" dokument vorschau 1 --out "$KONTOR_DATA/entwurf-1.pdf" > /dev/null
echo "Demo data ready. Draft PDF: $KONTOR_DATA/entwurf-1.pdf"
echo "Start the UI with: $K server   (then open http://127.0.0.1:8792/m/lokal/)"
