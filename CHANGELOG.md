# Changelog

## 0.1.0-alpha.2

- Enforce explicit remote opt-in and TLS for non-loopback HA/model endpoints; validate URLs before connection and reject redirects.
- Skip malformed HA messages; separate bounded ingestion and model processing with overflow, expiry and skipped-budget metrics.
- Bundle Demo data in wheels and verify wheel/sdist installation outside the source tree.
- Budget complete serialized model requests and select context in linear time.
- Batch replay SQLite operations in one transaction; close backup connections and flush only the oldest environment entity at capacity.
- Expand privacy patterns, scan staged/history blobs and explicit release artifacts, and bound scanner resource usage.
- Add pinned build tools, commit-based release manifests, source ZIP/wheel/sdist output and expanded CI checks.
- Add bounded writable /tmp for the read-only container recipe (runtime remains unverified).

## 0.1.0-alpha.1

- Independent public extraction with fresh history and public commit identity.
- Four reused core modules, bounded aggregation and a new observation-only pipeline.
- Three synthetic scenarios, optional HA subscription and opt-in Ollama inference.
- Structured response validation, cooldown, context bounds and model failure abstention.
- Offline tests, bilingual README, agent instructions, privacy scan and container recipe.
- No production service migration or GitHub publication.
