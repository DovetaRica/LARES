# Validation report — 0.1.0-alpha.1

Performed locally on Windows with Python 3.12:

| Check | Result |
| --- | --- |
| Standard-library offline test suite | 16 tests passed |
| Three synthetic CLI demos | Passed; zero executed actions |
| Example config validation | Passed |
| Local doctor JSON | Passed; reports optional dependency availability without network probes |
| Isolated editable installation with HA extra | Passed in repository-local virtual environment |
| Python wheel build | Passed; public package contents inspected |
| Public tracked-file/history heuristic scan | Passed |
| Selected private credential/endpoint exact-match comparison | No matches in public files |
| Source file SHA-256 comparison | 55 snapshotted source files unchanged |
| Git diff whitespace check | Passed |

Tests cover network-blocked demos, ordinary-event inference avoidance, bounded context, provider failure, evidence forgery, action rejection, duplicate handling, event ordering, stale/stopped robot evidence, configuration validation, explicit connection/model opt-ins, HA field projection, a mocked read-only HA protocol, streaming aggregation and rejected-candidate preservation.

No running HA, NAS, model server or device was contacted for validation. No source deployment, restart, configuration write or source Git mutation was performed. File hashes verify the snapshotted local files, not independent uptime of remote services.

Not tested: real HA/Ollama integration, GPU inference, Docker build/run, non-Windows runtime and remote CI matrix. Mocked protocol tests do not establish full integration compatibility. No latency, accuracy, power or inference-savings claims are made.

Privacy checks are heuristic plus review of selected source values; they do not constitute a guarantee that arbitrary future contributions are safe to publish.
