# Extraction inventory

## Reused and adapted

| Module | Role in the public alpha | Changes |
| --- | --- | --- |
| database.py | Temporary replay storage | Original general SQLite schema retained |
| event_aggregator.py | Environment statistics and manual correction detection | Streaming statistics, bounded entity caches, skip identical manual state/attributes |
| anomaly_detector.py | Repeated correction and stale-event heuristics | Retained; limitations documented |
| memory_manager.py | Review candidate storage and feedback | Retained; scores documented as heuristics |

## Rebuilt public entry points

Configuration, normalized event gates, CLI, HA projection/subscription and Ollama response validation are new public-specific implementations. They do not import the original application entry point or use its settings. There is no import-time database creation, scheduler or collector startup.

## Not imported

Original service entry point, scheduler, web UI, personal prompts, device mappings, notifier, backup/deployment scripts, live test scripts, local browser data, operational databases and old Git history. These require separate review before any future port.

## Feature gaps

Hierarchical summarization, production UI, long-term live memory, execution and generated automation validation are not part of this alpha. This is a runnable public extraction baseline, not a complete migration of every feature in the private service.
