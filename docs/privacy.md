# Privacy

This public distribution was assembled from reviewed source modules and newly written synthetic examples. No source .env, entities configuration, prompts, databases, logs, browser profiles, deployment scripts, image archives or original Git history were imported.

The local `.private-audit/` directory is ignored and excluded from container inputs. It contains extraction verification metadata and is not part of the public project. Share `git archive` output, not a ZIP of the entire working directory.

Demo/replay does not use a network provider unless replay explicitly enables one. The demo always forces fixtures. Live event projection drops arbitrary attributes and uses aliases, but timestamps, states, alias groups and context identifiers remain sensitive metadata. State values are not automatically anonymized. Do not assume hashes or aliases make household activity anonymous.

Remote inference is disabled by default. When enabled, the selected endpoint receives the bounded normalized event payload and prompt. There is no telemetry or external notification implementation. Output may contain model-generated private information if the input is private; treat redirected terminal output as private too.

`python scripts/check_public.py` scans tracked text and Git history for common sensitive patterns. It is heuristic and cannot prove the absence of personal information. Review the actual archive contents before publishing. Never attach raw household logs to public issues. Report bugs using a synthetic reproduction.
