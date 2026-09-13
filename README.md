# Home AI

**Automation handles the routine. AI handles the exceptions.**

An observation-first exception intelligence layer for Home Assistant. Keep existing automation independent, inspect ambiguous events, and turn repeated manual corrections into review candidates.

**Status: v0.1.0-alpha.2 — experimental, shadow mode only.**

[中文](README.zh-CN.md) · [Architecture](docs/architecture.md) · [Deployment](docs/deployment.md) · [Privacy](docs/privacy.md) · [Agent guide](AGENTS.md)

## Try without a home, token, model, or network

Python 3.11+; run from the repository root, or use these commands after installing the wheel:

```sh
python -m home_ai demo
python -m home_ai doctor --json
python -m unittest discover -s tests -v
```

The demo uses explicitly labelled **deterministic fixtures**, not actual AI inference. It never connects to Home Assistant or changes a device. Output is JSON.

| Synthetic scenario | Expected result |
| --- | --- |
| Robot activity and presence, with corroborating presence | Review uncertain cause; never assume the room is empty |
| Two presence sensors disagree | Sensor-conflict review |
| Three manual reversals of automation | Three reviews plus one candidate for human investigation |

All scenarios report `executed_actions: 0`. Provider-call counts in fixture demos are not real LLM benchmarks.

## What works today

- Bounded event context by count, age and full serialized model-request size (including prompt and JSON envelope).
- Duplicate filtering, recent-state conflict signals and per-entity/reason cooldown.
- Offline replay with extracted SQLite storage, streaming environment statistics, repeated-correction detection and candidate memory.
- Explicitly enabled HA observation, HTTPS required for non-loopback endpoints, malformed-message counters and a bounded ingestion queue; no service-call implementation.
- Explicitly enabled Ollama inference, structured output validation and abstention on failure.
- Diagnostic/configuration commands, synthetic examples and offline tests.

HA observation currently streams decisions; persistent live history and candidate aggregation are not yet wired into that command. The replay command exercises the extracted storage/memory modules.

## Model and HA access are opt-in

Follow [deployment](docs/deployment.md) for isolated installation and configuration. The default configuration uses fixtures. Real inference requires `provider: ollama`, an explicit model name and `--enable-model`. HA requires `observe --connect`, an explicit mapping and an environment token. Nothing loads an existing `.env` automatically.

## Roadmap, not current claims

Hierarchical summaries, semantic rule synthesis, counterexample evaluation, persistent live learning, reconnect recovery, execution policy, HA Add-on/HACS packaging, and hardware benchmarks remain future work. Current context handling truncates old events; it does not prove that all important evidence survives.

Intel Arc A310 is a planned validation target. No GPU support, speed, power, minimum hardware, or percentage of avoided inference is claimed as measured here.

## Containers

```sh
docker compose run --rm demo
```

The provided demo service has no network, host ports or host volumes. Its read-only root has a 64 MiB temporary filesystem for replay SQLite data. Building downloads dependencies. The container recipe has not yet been runtime-validated; the Python path is the tested baseline.

## Naming and installation artifacts

The Python distribution is `home-ai-exceptions`, the import module is `home_ai`, and the command is `home-ai`; the GitHub repository name may be chosen independently. Examples are bundled in wheels and do not depend on the current directory. Each release includes a source ZIP, wheel, sdist and a commit/checksum manifest.

## Development and release

[Extraction inventory](docs/extraction.md), [validation report](docs/validation.md), [changelog](CHANGELOG.md), [release checklist](docs/release.md).

Project code: Apache-2.0; see [LICENSE](LICENSE). No model weights are included. Third-party dependencies retain their own licenses; see [provenance](docs/provenance.md).

The alpha.1 review fixes and remaining verification limits are documented in [review fixes](docs/review-fixes-alpha2.md).

Local release folders and continuous review records: [versioning workflow](docs/versioning.md).
