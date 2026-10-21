---
description: Set up Kontor for your own business (terms, country, interview, company profile via the CLI)
---
Set up Kontor for the user's own business. Read AGENTS.md, README.md and ONBOARDING.md first and follow ONBOARDING.md step by step (start at step 1 if the repository is already installed, otherwise at step 0):

- Show the core of TERMS.md and DISCLAIMER.md first and continue only after the user says yes.
- Ask first in which country the business is taxed and which legal form it has. Germany: the German interview. Any other country: explain what is German-specific and offer the demo company or the localisation path in LOCALIZATION.md.
- Interview one topic at a time, in the user's language. Never invent a value; leave unknown fields empty and list them as open.
- Give no tax advice and answer no individual case; the user's tax advisor or tax office letter decides. Name an official source for every tax rule on the localisation path and let the user confirm it.
- Show every JSON before writing it, and write only through the `kontor` CLI.
- Stop and say so when the situation is not supported or when the user wants to keep books for someone else.
- Finish with `kontor einrichtung` and a short handover list.

$ARGUMENTS
