# Third-party files in this repository

| File | Origin | License |
|---|---|---|
| `kontor/ui/static/htmx.min.js` | htmx 2.0.4, https://github.com/bigskysoftware/htmx | Zero-Clause BSD (0BSD) |
| `kontor/render/assets/fonts/archivo-400.ttf`, `archivo-600.ttf` | Archivo by The Archivo Project Authors, https://github.com/Omnibus-Type/Archivo | SIL Open Font License 1.1, see `kontor/render/assets/fonts/OFL.txt` |
| `kontor/elster/taxonomie_6_8.json` | Derived with `tools/ebilanz_taxonomie.py` from the official HGB taxonomy 6.8 (Kerntaxonomie, MicroBilG view) published by the German tax administration on esteuer.de | Official publication of the German tax administration (amtlich vorgeschriebener Datensatz, published with BMF letter of 27 May 2024 on esteuer.de). Converted to JSON without changing names or labels. Valid for fiscal years starting after 31 December 2024; fiscal years starting after 31 December 2025 need version 6.9. |

Not included, download yourself if you need them:

- ELSTER ERiC (German tax administration), under the ERiC licence for software manufacturers.
- Mustang-CLI (https://github.com/ZUGFeRD/mustangproject), Apache-2.0, optional e-invoice validator for the tests.

Python dependencies are installed from PyPI (`requirements.txt`) under their own licences. Note that the binary wheels of `pillow-heif` bundle GPL-licensed codecs; this matters only if you redistribute a bundled installer.
