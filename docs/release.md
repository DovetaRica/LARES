# Release checklist

Current local milestone: `v0.1.0-alpha.1`.

Before a public GitHub release:

- Review the public README, feature limitations, code provenance and chosen license.
- Run offline tests, demos, configuration validation and tracked-file/history scan.
- Build an archive from the release tag and inspect its file list; do not archive the whole working directory.
- Test the container in a separate nonproduction environment; CI matrix results are not yet available locally.
- Test explicit HA observation and Ollama inference in an isolated test environment before advertising integration compatibility.
- Add reproducible model/backend/OS/driver measurements before claiming Arc A310 performance.
- Select the target GitHub owner/repository and publish only the public history.

Version policy: alpha tags capture reviewable checkpoints. Use scoped commits on codex/ branches. Never import private repository history. Do not amend a published tag; issue a new version for fixes.
