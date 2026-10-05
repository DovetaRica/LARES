# v0.2.1

This patch updates the optional application; `home-ai` CLI behavior and entry points remain
compatible. It includes report coverage/budget repairs, deterministic room evidence,
content-free model-call accounting, restart-safe doorway direction/short-trip confirmation,
generic spatial candidates and the local Rhine theme.

Networking, scheduled reports, segment AI and automatic distillation remain off by default.
Topology is a three-node synthetic example. Replace it only with your own explicitly authored
local configuration. A spatial candidate establishes neither personal identity nor a confirmed
complete journey. Short-trip and direction checks use ordered synthetic sensor edges.

## Installation

The dependency-free CLI supports Python 3.11+. The optional application is tested on Python 3.12.
For a new isolated source environment:

```sh
python -m pip install -c requirements-app-lock.txt ".[app]"
python -m app.demo
lares-app --demo
```

For the wheel: `python -m pip install "./home_ai_exceptions-0.2.1-py3-none-any.whl[app]"`.
The demo is temporary, offline and synthetic. The application remains bound to loopback.

## Database compatibility

The optional application adds a `model_calls` accounting table and `ai_questions.options_json`.
Its initialization performs additive, idempotent schema updates. Existing event, guidance and
question rows must be retained; synthetic v0.2 schema compatibility is regression tested.
Back up a persistent local database using SQLite's online backup API before an intentional
upgrade. Do not substitute a copy of a live WAL database. Keep the original database and old
package available for rollback; never overwrite current data with an old backup as a routine
code rollback. `home_ai` and `app` still have separate database formats.

No in-place upgrade of a maintainer's production database, deployment or config is performed
by this release. New accounting contains hashes, token/call counters and error classes, not
prompts, events or response content. It is not a quality or energy measurement.

## Verification scope

Use this release's own regression results and CI, not the source project's test count. Checks
use temporary databases, stub models and synthetic signals. Fresh wheel/sdist installation
and all local UI routes are checked outside the source tree with networking disabled.
Real HA/model integration, physical movement accuracy and production-scale performance remain
environment-specific checks. The version number does not imply those measurements were made.

See [application setup](application.md) and [extraction provenance](extraction-v02.md).
