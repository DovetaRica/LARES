# LARES optional application (0.3.0)

The optional application adds a Chinese local UI, persistent event storage, deterministic
segment preprocessing, evidence-based candidate memory, room sessions, six-hour/daily/weekly
report orchestration and optional model comparison. The existing `home-ai` CLI is unchanged.
Public-package checks use synthetic data; production deployment history is not validation of this release.

## Install and try offline

Use Python 3.12 for the application preview. From the reviewed source directory:

```sh
python -m venv .venv
# Activate the virtual environment for your platform, then:
python -m pip install -c requirements-app-lock.txt ".[app]"
python -m app.demo
lares-app --demo
```

Open <http://127.0.0.1:8080>. `--demo` creates a temporary database containing only synthetic
temperature and light events, disables networking and scheduling, and removes the temporary
files on normal exit. It does not generate model-authored reports. To use another local port,
pass `--port 8088`.

For a wheel installation, use `python -m pip install "./home_ai_exceptions-0.3.0-py3-none-any.whl[app]"`.
Templates, static files, generic prompts and example configuration are inside the wheel.
Installing dependencies requires network access; the demos themselves do not.
The lock file records tested transitive versions for the Python 3.12 preview. The build tools
are pinned separately in requirements-build.txt. Byte-identical artifacts across platforms
are not claimed.

## Persistent local workspace

Run `lares-app` from a new, dedicated directory to create `data/`, `logs/` and later backups.
The optional application reads that directory's `.env` and environment variables. This differs
from the original dependency-free CLI. Do not run it from an existing production workspace or
point `DATABASE_PATH` at an existing installation during a demo. Only the additive upgrade from
the public v0.2.0-alpha.1 and v0.2.1 schemas is regression tested with synthetic data; production upgrades
require a separate backup and review. See [v0.3.0 compatibility](release-v030.md).

The application binds to loopback; Host validation and same-origin write checks are enabled.
It is not a publicly hosted service. The UI supports optional Basic authentication via
`WEB_AUTH_ENABLED`, `WEB_USERNAME` and `WEB_PASSWORD`. Do not expose it to a LAN or the Internet
without a separate authentication, TLS and deployment review.

## Explicit observation and model access

Networking is off until `LARES_ENABLE_NETWORK=true`. Non-loopback HA and model URLs additionally
require `LARES_ALLOW_REMOTE=true` and HTTPS. Redirects and implicit environment proxies are
disabled. `HA_URL` and `HA_TOKEN` enable only HA observation after the network gate opens.
Set `CONFIG_PATH` to a local YAML containing your explicit entity allowlist, based on
`app/resources/entities.yaml`. Bundled identifiers are synthetic and collect no real devices.

`OLLAMA_URL` defaults to localhost. `OLLAMA_MODEL` selects a locally installed model; no model is
downloaded by this package. Automatic schedules and startup backfill additionally require
`LARES_ENABLE_SCHEDULE=true`. Segment AI is off (`SEGMENTS_AI_MODE=off`); use `hybrid` explicitly
only after validating the local model. Candidate learning defaults to observation mode
(`LEARNING_ONLY=true`) and retains the seven-day/five-sample defaults.

Report review is off (`REPORT_REVIEW_ENABLED=false`); enabling it adds one critic request and,
only if needed, one revision after a local draft (up to four HTTP requests including format retry).
Idle scouting is off (`PROACTIVE_ENABLED=false`); it additionally needs network and scheduling
opt-ins, shares the ordinary model lock, and uses at most `PROACTIVE_MAX_CALLS_PER_DAY=6` reserved
requests per day with bounded reads. Leads are unconfirmed and do not activate automations.

Third-party model configuration is empty/off. Enabling it deliberately sends supplied household
context to that provider; no private provider settings or credentials were extracted. The
provider key is stored locally in the runtime data directory. Generic prompts have been newly
authored and have not been benchmarked with live models. Manual model requests are subject to
the same network gate. No HA service calls or automation installation are supported.

## Compatibility and limits

- `home_ai` and `app` are separate pipelines and database formats. No automatic migration or
  shared state is provided. The generic `app` package name may conflict with another project;
  use an isolated environment.
- Room, sleep, arrival, noise and non-occupancy mappings are unset. Configure them for your own
  sensors; the original household topology is not a default.
- Layered memory, report comparison and candidate code are included; complete live HA/model
  behavior and all production regression tests have not been validated in this extraction.
- A source-project preprocessing benchmark exceeded its original two-second limit. This preview
  makes no throughput, GPU, energy, accuracy or production-scale claim.
- JSON repair can salvage partial segment output; it is not evidence that missing text is true.
  Model interpretation and inferred correction intent always require human review.

## Local checks

```sh
python -m pip install pytest==9.1.1
python -m unittest discover -s tests -q
python -m pytest -q runtime_tests
python -m app.demo
python -m scripts.smoke_app
```

The application smoke test uses a temporary database, disables network sockets, checks all
thirteen UI pages and health, and checks Host/cross-origin rejection. No test connects to an
existing HA, model server or production database.
