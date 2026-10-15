# v0.3.0

This release extends the optional local application. The `home-ai` CLI and package names
remain compatible. Home Assistant access remains observation-only.

## Changes

- Optional local draft → review → necessary revision, with complete evidence, shared context
  budgets and at most four HTTP requests including a draft format retry.
- Evidence-grounded local questions and allowlisted read-only queries for known identities,
  exact-window statistics and scoped owner guidance. No arbitrary SQL, URL or shell tools.
- Optional idle scouting with daily request reservations, ordinary-work priority and an
  inbox of unconfirmed leads. It does not create confirmed habits or control devices.
- Persistent manual jobs: queued jobs are restored in order; jobs that had started become
  interrupted and are not automatically replayed by the manual queue. Report catch-up is a
  separate, explicitly scheduled policy. Single-process operation remains required.
- A fresh runtime per application lifespan, and content-free request-stage hashes, budget,
  token/call counters, failure classes and optional Ollama engine timings.

## Defaults and installation

Networking, scheduling, segment AI, automatic distillation, report review and idle scouting
are off by default. In a dedicated Python 3.12 environment:

```sh
python -m pip install -c requirements-app-lock.txt ".[app]"
python -m app.demo
lares-app --demo
```

The UI binds to loopback. The demo supplies temporary synthetic data and explicit isolated
settings; it does not inherit the working directory's database or entity configuration.
For the wheel, install `"./home_ai_exceptions-0.3.0-py3-none-any.whl[app]"`.

`REPORT_REVIEW_ENABLED=true` enables local report review after deliberate model access.
`PROACTIVE_ENABLED=true` additionally needs `LARES_ENABLE_NETWORK=true` and
`LARES_ENABLE_SCHEDULE=true`; its default daily reservation limit is six HTTP requests.
Ordinary local review generally uses two or three requests, so it may cost more time than
one draft. No speed, GPU, energy saving or general accuracy improvement is claimed.

## Database compatibility

The application adds `manual_jobs` and `model_calls.stages_json`; older call rows retain
empty stage metadata. Additive initialization is idempotent. Synthetic databases built from
the public v0.2.1 schema retain their existing event, guidance, question and accounting rows
after two initializations, with SQLite integrity and foreign-key checks.

Back up persistent databases through SQLite's online backup API before an intentional upgrade.
Keep the prior package, database and configuration for recovery. Do not copy a live WAL file
or overwrite current data as a routine code rollback. No production migration or full recovery
drill is performed by this public release. The `home_ai` and `app` database formats remain separate.

Accepted queued manual requests resume independently of the schedule switch. The network gate
still blocks egress when disabled. Task completion indicates workflow completion, not model
truth or exactly-once execution of external requests.

## Verification and limits

Public tests use temporary databases, synthetic inputs and stub models; live socket connections
are blocked in runtime tests. Installation checks exercise wheel/sdist outside the source tree,
the original CLI, all thirteen local UI pages and health, Host/origin rejection and offline startup.
The source ZIP includes frozen historical schema fixtures, so upgrade tests do not need Git.

Read-only query rows, deadlines and returned tokens are limited, but individual stored text
fields have no separate byte cap before materialization. Use trusted local data; this is not
a general untrusted-database query service. Returned facts cannot prove personal identity,
complete occupancy, causality or physical movement accuracy. Alias ambiguity, long-term
semantic quality, production contention and model/hardware performance remain unverified.

Only selected committed generic code was extracted. Private prompts, household configuration,
raw events, databases, deployment scripts and internal operational documents are excluded.
See [application setup](application.md) and [extraction provenance](extraction-v02.md).
