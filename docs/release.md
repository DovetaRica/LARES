# Release workflow

Current target: `v0.1.0-alpha.2` (Python package version `0.1.0a2`).

1. Review changes, run offline tests, Demo, replay, configuration validation and privacy scan. Commit the reviewed public files.
2. Create a new immutable release tag; never move the alpha.1 tag.
3. In an isolated environment install `requirements-build.txt` (pinned tool versions).
4. Run:

```sh
python scripts/build_release.py --ref v0.1.0-alpha.2 --out dist/v0.1.0-alpha.2
python scripts/smoke_distributions.py dist/v0.1.0-alpha.2
python scripts/check_public.py --artifact dist/v0.1.0-alpha.2
python scripts/snapshot_version.py v0.1.0-alpha.2
```

The build command extracts the selected commit into a clean temporary directory, builds sdist and wheel (wheel from the sdist), scans artifacts, and writes a manifest with commit, package/tool versions and SHA-256 checksums. It never reads .private-audit or invokes Docker. Smoke checks install wheel and sdist into separate clean environments and run both module and console Demo entry points away from the repository; the module Demo also has a socket guard.

Source ZIP reproduction (including its original directory prefix):

```sh
git archive --format=zip --prefix=home-ai-0.1.0a2/ --output=rebuilt-source.zip v0.1.0-alpha.2
```

The archive prefix and source commit are recorded in `release-manifest.json`. The alpha.1 source ZIP was also produced directly by git archive, using the prefix `home-ai-0.1.0-alpha.1/`; a prefix alone is not evidence of manual repackaging. Source contents are reproducible; byte-identical wheels across machines/toolchains are not claimed.

Keep versioned artifacts and raw review evidence in the local version folder. Publish only selected sanitized source/distribution artifacts. Git ignores dist/ and versions/; these are local records until explicitly uploaded. No GitHub publishing is performed by these scripts.

Before claiming production compatibility, test real HA/Ollama, Docker and target hardware in a separate authorized environment. A configured CI matrix is not evidence that all jobs ran. Container tmpfs fixes are static until runtime-validated.
