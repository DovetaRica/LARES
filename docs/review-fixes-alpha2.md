# Alpha.1 review resolution — alpha.2

Review baseline: `v0.1.0-alpha.1`, commit `15ba9ec`. Runtime/package fixes: `86003ad`; subsequent release commits contain documentation and verification additions. The final exact source commit is recorded in each release manifest. Original review reports remain unchanged in local version archives.

| Finding | Resolution | Evidence / limitation |
| --- | --- | --- |
| S-1 | HA now requires explicit remote opt-in and HTTPS outside loopback; redirects disabled | Shared endpoint-policy tests; direct observe cannot bypass validation. No real HA contacted. |
| S-2 | HA URL types, credentials, ports and URL syntax validated in load_config/doctor/validate-config | Safe fixed ConfigError messages do not echo supplied values. |
| S-3 | Malformed frames are skipped before queueing; valid frames continue | Mixed malformed/valid mocked stream regression with counters. |
| S-4 | Added common cloud/model token formats, literal assignments, additional private networks, forbidden paths, staged/history blobs and explicit artifact scanning | Synthetic credential, staged-only secret and ZIP tests. Scan remains heuristic, with explicit byte/file limits. |
| S-5 | Non-loopback model HTTP is rejected even when remote opt-in is enabled | Same policy tests as HA; no insecure override. |
| R-1 | Release builder retains wheel, sdist, source ZIP and checksum/commit/tool manifest; CI builds and installs distributions | Clean-commit build plus separate clean wheel/sdist installs. Prior wheel-build success and public artifact availability were distinct claims; this release makes artifacts available locally. |
| R-2 | Bundled package examples and resource-based default lookup | Both installed module and console Demo pass from unrelated directories; no explicit examples path needed. |
| R-3 | Exact git archive prefix is documented and recorded | Original source ZIP was directly produced by git archive with a prefix; the report's inference from a wrapper directory was incorrect. Content-level reproduction retained; byte-identical wheels not claimed. |
| R-4 | Read-only container now has a bounded writable /tmp tmpfs; license files included in build context | Static recipe correction plus a dedicated CI container job. Local Docker was not invoked; runtime result remains pending. |
| R-5 | Distribution, module and executable names explained; Python matrix expanded | Local 3.12 and 3.14 tests pass; 3.11/3.13 and Linux depend on future CI runs. |
| E-1 | Replay uses one SQLite connection/transaction; atomic batch API provided | Same schema/data benchmark and connection-count/rollback regression. Individual standalone inserts still commit individually by design. |
| E-2 | Full serialized model request counted; newest-suffix selection is linear | Escaping/Unicode budget comparison against a brute-force reference; provider checks size again before HTTP. Output has separate limits. |
| E-3 | Independent ingress producer and single model consumer, bounded queue, drop-newest and expiry policies | Controlled blocked-model test proves ingress continues and queue stays capped. No reconnect/durable queue claim. |
| E-4 | budget_skipped, cooldown_skipped and context_skipped metrics | Budget saturation, expiry and cooldown regressions. |
| E-5 | Evict/flush only oldest environment entity at capacity | Entity-eviction regression. Idle cooldown records remain bounded and are pruned on next event; no background timer is needed solely to release this bounded memory. |
| E-6 | Preserve ordinary-event no-inference behavior and bounded structures | 10,000 accepted synthetic device events: zero provider calls, buffer 100. |

Additional regression discovered while testing: SQLite backup target connections were not explicitly closed, causing Windows file locks. They are now closed deterministically and covered by backup/cleanup tests. Backups during uncommitted batches are rejected.

## Benchmark (local evidence, not a general performance guarantee)

Windows, Python 3.12.10, 12 logical CPUs; three runs of 1,000 identical device-event inserts, same schema and temporary storage. Database construction is outside timing in both cases.

| Path | Runs, seconds | Median |
| --- | --- | --- |
| alpha.1 per-operation connection/commit | 3.7408163, 3.7040832, 3.5168572 | 3.7040832 |
| alpha.2 batch transaction | 0.0178602, 0.0156101, 0.0159360 | 0.0159360 |

Observed median speedup: about 232x for this isolated write workload. This does not measure production HA throughput or end-to-end model performance. The 10,000-event pipeline run took 0.712 seconds with every event accepted and no model calls. Reproduce with scripts/benchmark_review.py and an explicit old database-module snapshot; the script only writes synthetic temporary databases.

## Remaining verification boundaries

Real HA/Ollama connectivity, TLS on actual deployments, Docker runtime, Linux CI and GPU performance remain unverified locally. The tests use fixtures or mocks; they do not establish model accuracy. Inference has a socket I/O timeout rather than an absolute whole-request deadline. In-flight thread cancellation can wait for the blocking request to return. Longer-running reliability, hierarchical summaries and persistent live learning remain future features, not review fixes claimed complete.
