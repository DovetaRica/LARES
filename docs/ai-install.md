# Installing LARES with an AI agent

Read README.md and AGENTS.md first. This alpha provides observation and review decisions only; there is no action-execution mode to enable.

1. Identify the target Python environment (3.11+) and create an isolated environment if installing dependencies.
2. Use the repository or a versioned wheel from Releases. Run `python -m home_ai demo`; installed wheels include the examples and work outside the checkout.
3. Run `python -m home_ai doctor --json`. It checks local configuration, not live connectivity.
4. For real observation, get the user's target HA URL and explicit entity mapping. Store mappings in ignored `config/private.json`, and receive the token through the local `HOME_AI_HA_TOKEN` environment variable. Never echo it or commit it.
5. Validate configuration. Non-loopback HA and model endpoints each need their remote opt-in and HTTPS. Do not weaken TLS to complete setup.
6. Only within the user's authorized target environment run `observe --connect`; enabling a real model additionally requires `--enable-model` and a configured model name. No implicit model downloads occur.
7. Inspect structured decisions and overflow/expiry/failure metrics. Explain limitations: no device actions, persistent live memory or reconnect recovery.
8. Stop the foreground observer with Ctrl+C. Do not restart or change an existing household service as a side effect of this setup.

Quickstart commands and exact configuration fields are in docs/deployment.md and schemas/config.schema.json. Do not turn roadmap statements into promises of implemented capabilities. Fixtures are demonstrations, not model-accuracy evidence.
