# Public extraction 0.2

This local candidate extends the alpha.3 public tree with generic runtime code selected from
an immutable source commit. Only application Python/static/template files and one explicitly
synthetic statistics regression module were selected. The original Git history is not imported.

The private source commit and per-file SHA-256 provenance are recorded in the maintainer's
ignored audit folder. They are not installed or uploaded. No private configuration, entity
inventory, raw event fixture, database, log, deployment script or operational documentation
is part of this extraction. Previous local version snapshots remain immutable.

Extraction adaptations:

- Remove source comments/docstrings that contain deployment anecdotes and household examples;
  retain executable algorithms with AST-based reformatting.
- Replace device-specific identifiers with synthetic identifiers; clear all household-specific
  configuration defaults. Generic room vocabulary used by identity validation is retained.
- Author new generic allowlist and prompts instead of copying persistent configuration.
- Bundle resources and templates so installation works outside the source checkout.
- Keep the original CLI intact; provide an optional loopback application with a temporary demo.
- Gate HTTP/WebSocket egress, remote TLS and scheduling explicitly, block redirects/proxies,
  and add local Host/same-origin write checks.
- Close backup SQLite handles explicitly.

The source project's 466-test result is not claimed for this public version. Public tests,
fresh-install results and remaining limitations belong to this candidate's own review record.
No production files, configuration, services or databases are changed by extraction.

Local tags and version folders do not publish to GitHub. Uploading the candidate is a separate
release step after reviewing its source, checksums and limitations.
