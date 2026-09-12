# Deployment

## Offline baseline

Python 3.11+, repository root:

```sh
python -m home_ai demo
python -m home_ai replay examples/automation_correction.jsonl
python -m home_ai validate-config --config config/example.json
python -m home_ai doctor --json
```

No `.env` is loaded. These commands do not connect to HA or a model. Replay uses temporary storage and removes it on exit. Exit codes: 0 success, 2 configuration/runtime failure, 130 interruption. `doctor` checks local configuration and optional dependency availability; it does not certify service health. Error output deliberately omits arbitrary exception text to avoid leaking secrets.

## Optional isolated installation

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[ha]'
.\.venv\Scripts\python -m home_ai doctor --json
```

Linux/macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[ha]'
.venv/bin/python -m home_ai doctor --json
```

`home-ai` is installed as an equivalent console command in that environment. Installing downloads packaging tools and the optional websockets dependency. It does not start services.

## Explicit HA observation

Copy `config/example.json` to ignored `config/private.json`. Set `ha_url` for your own environment and an explicit `entity_mapping`, for example `{"light.example": "example_room.light", "binary_sensor.example": "example_room.presence"}`. Use the same alias prefix only for sensors intentionally compared in one area. Unmapped entities are discarded before reasoning; the HA subscription still receives state-change messages before filtering.

Supply your token locally using the process environment variable `HOME_AI_HA_TOKEN`; do not put it in tracked files or paste it into an issue. Then run:

```sh
python -m home_ai observe --config config/private.json --connect --limit 100
```

The limit counts accepted mapped messages, not seconds. `--limit 0` runs until interrupted. Ctrl+C exits. This version exits on connection loss; automatic reconnect and persistent live history are future work. It does not fetch initial states, so only new observed changes are available as evidence. The connector only authenticates and subscribes to `state_changed`; it has no service call or configuration write path.

Read-only behavior is enforced by this implementation, not by an assumption that the provided HA token lacks write privileges.

## Explicit model inference

In private configuration set `provider` to `ollama`, `model` to a model you have already installed, and `model_url` to its API base. Non-loopback endpoints require `allow_remote_model: true`, including LAN/container host addresses. This opt-in is a network boundary, not an automatic anonymization guarantee.

```sh
python -m home_ai replay examples/sensor_conflict.jsonl --config config/private.json --enable-model
python -m home_ai observe --config config/private.json --connect --enable-model
```

The adapter never downloads models. Ambient HTTP proxy settings and HTTP redirects are disabled. Schema-invalid responses, unavailable providers and timeouts produce abstention. Runtime metadata and evidence can still reveal activity; review docs/privacy.md before enabling a remote endpoint.

## Containers and removal

`docker compose run --rm demo` builds a separate demo image and runs without network, host mounts or exposed ports. The image includes optional HA dependencies, but the default Compose service cannot reach HA or a model. This recipe is not runtime-validated yet.

No production deployment is installed by the Python quickstart. Stop a foreground command with Ctrl+C. Local installation can be removed by deleting only this repository's virtual environment; do not run cleanup or Compose commands against an existing home deployment. Keep private data separate when upgrading; use release tags to select code versions.
