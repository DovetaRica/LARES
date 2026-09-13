# Contributing to LARES

Start with README.md and AGENTS.md. Use synthetic reproductions and isolated environments. Keep normal HA automation independent and preserve shadow-only behavior unless a separately reviewed design changes that boundary.

```sh
python -m unittest discover -s tests -v
python -m home_ai demo
python -m home_ai validate-config --config config/example.json
python scripts/check_public.py
```

Before changing providers, detectors or event budgets, add a regression that demonstrates the actual failure mode. Package changes must also pass fresh wheel/sdist installation outside the repository using scripts/smoke_distributions.py.

Open a focused pull request explaining the problem, resulting behavior and validation. Never include tokens, household event histories, device mappings or private infrastructure details in issues, commits or attachments. Review evidence should be synthetic or sanitized. Do not run tests against someone else's live home.
