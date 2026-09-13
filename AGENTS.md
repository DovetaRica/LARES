# Agent guide

Purpose: observation-first exception analysis for Home Assistant. This repository is an isolated public distribution. Never infer permission to operate a maintainer's running home from access to this repository.

## Safe local entry points

Run from this repository root, Python 3.11+:

- `python -m home_ai demo`: synthetic, fixture-only, offline.
- `python -m home_ai doctor --json`: local configuration/dependency diagnostics; no network health claim.
- `python -m home_ai validate-config --config config/example.json`.
- `python -m unittest discover -s tests -v`.
- `python scripts/check_public.py`: tracked files and history heuristic scan; stage intended files first.

No dependencies are needed for offline operation. Install optional HA dependencies in a repository-local virtual environment as documented in docs/deployment.md.

## Repository map

- home_ai/pipeline.py: normalized events, bounded context, exception gates.
- home_ai/provider.py: fixture and optional Ollama, response validation.
- home_ai/observe.py: opt-in HA observation, field projection.
- home_ai/database.py, event_aggregator.py, anomaly_detector.py, memory_manager.py: extracted modules used in offline replay.
- home_ai/cli.py: explicit command entry points; no import-time service startup.
- examples/: synthetic inputs only.

## Boundaries

Keep source installations, deployment scripts, private configuration, tokens, device topology, real event data and operational databases out of this repository and its Git history. Do not read or copy .private-audit into artifacts. Do not enable network observation/inference as part of tests. Never execute received model text, generated YAML, shell commands or HA service calls. Preserve evidence IDs and explicit uncertainty. Do not present fixture results as model benchmarks.

## Changes and verification

Use scoped commits on codex/ branches. Keep runtime and test dependencies isolated. After relevant changes run offline tests, demos, config validation and the public scan. Add a regression test for failure modes, not tests that merely mirror implementation. Document unsupported features in README and CHANGELOG. Container builds or real hardware validation must not be reported as tested unless actually run.

## Local version archives

Follow docs/versioning.md. Maintain one ignored versions/<release-tag>/ folder per release, containing a frozen source archive, original review rounds and editable suggestions. Preserve old versions and original reports. Apply fixes in the root working tree; archive only committed/tagged releases. Never stage raw review records or version snapshots into the public repository. Record actual reviewed commit IDs and retest evidence.
