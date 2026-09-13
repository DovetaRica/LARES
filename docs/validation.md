# Validation report — 0.1.0-alpha.2

## Executed locally

| Check | Result |
| --- | --- |
| Python 3.12.10 offline suite | 30 tests passed |
| Python 3.14.7 offline suite | 30 tests passed |
| Fixture Demo, replay and example configuration | Passed |
| Commit-based source ZIP, wheel and sdist build | Passed, using requirements-build.txt pins |
| Fresh wheel installation outside repository | Module Demo with socket guard, console Demo, doctor and validate-config passed |
| Fresh sdist installation outside repository | Same checks passed in a separate temporary environment |
| Public working/staged/history scanner and built-artifact scanner | Passed |
| Database benchmark against alpha.1 snapshot | Same schema, three runs; see review-fixes-alpha2.md |
| Git whitespace check | Passed |

Package tests are performed on actual built distributions, not inferred from editable installation. Release outputs and checksums are retained in the version-specific dist folder and local version archive. Final commit and build-tool versions are in release-manifest.json.

Regressions cover HA URL/TLS/opt-in policy, safe error reporting, malformed-frame continuation, blocked-model ingestion, bounded queues and expiry, full request budgeting with escaping, mocked HTTP request construction, response schema rejection, cooldown/budget metrics, atomic transactions, backup connection cleanup, oldest-entity eviction, package-resource discovery, expanded scanner patterns and staged-blob scanning.

## Not executed locally

No real Home Assistant, NAS, Ollama, GPU or device was contacted. No existing service was started, stopped, restarted or reconfigured. No Docker engine was accessed. Container configuration was corrected statically and a dedicated CI job added, but no runtime pass is claimed. Python 3.11/3.13 and Linux CI results are pending; matrix configuration alone is not a test result.

No model accuracy, real-service latency, power consumption or byte-identical wheel reproducibility is claimed. Privacy scanning is heuristic and does not certify anonymity. Original alpha.1 code snapshots and review originals are retained unchanged; current work is isolated to the public project.
