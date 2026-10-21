# Security

Kontor runs only on your own computer and keeps your books in folders you choose. Please read the security notes in the [README](README.md#security).

## Reporting a vulnerability

Please report security problems privately through GitHub: on the repository page, open **Security → Report a vulnerability**. Do not open a public issue for it, and never attach real bookkeeping data, receipts, certificates or PINs. Describe the problem with invented data.

Kontor is shared free of charge and without support (see [TERMS.md](TERMS.md)). Reports are read and fixed on a best-effort basis; there is no guaranteed response time.

## What never belongs in a report or a contribution

Certificates (`.pfx`, `.p12`), PINs, ELSTER manufacturer IDs, `.env` files, databases, real names, addresses, tax numbers, IBANs or amounts. The contribution check (`tools/check_contribution.py`) blocks most of these automatically.
