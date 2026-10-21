**What changes for users**

**Tests** (added or changed; `pytest -q` passes)

**Checklist**
- [ ] No real data anywhere: code, tests, fixtures, docs, screenshots, commit messages (names, addresses, tax numbers, IBANs, amounts, paths, host names).
- [ ] Built neutrally in the core: differences between users are settings, profile switches or instance-file entries, never a special case for one company, person or bank.
- [ ] German behaviour still works (new countries go behind a profile switch, see LOCALIZATION.md).
- [ ] Every tax rule I added names its official source (SOURCES.md of the country), and no wording presents Kontor as tax advice, certified or legally reviewed.
- [ ] Every commit is signed off (`git commit -s`, see CONTRIBUTING.md).
