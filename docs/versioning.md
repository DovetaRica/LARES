# Version folders and continuous reviews

The root is the active Git working tree. Each released version has an independent local folder:

```text
versions/
  README.md
  v0.1.0-alpha.1/
    README.md
    manifest.json
    source/
    artifacts/
    reviews/
      round-001/
        REVIEW.md
        review_repros/
      round-002/
    SUGGESTIONS.md
  v0.1.0-alpha.2/  (created only when that release exists)
```

`source/` is extracted from the exact release tag, with Git export-ignore rules applied. It is not a copy of the active directory, virtual environment, databases or original installation. Preserve each snapshot; make fixes in the root working tree, then commit and create a new version tag.

Create a folder after tagging a release:

```sh
python scripts/snapshot_version.py v0.1.0-alpha.1 --review-dir /path/to/local-review
```

Omit `--review-dir` if no report is available yet. The command rejects existing version folders and only copies Markdown/Python review files, never executes them. Initial snapshot/report hashes are recorded in `manifest.json`.

Add later reports under a new `reviews/round-NNN/` folder. Every round must identify the actual commit it reviewed; do not attach a review of newer code to an older version as if it reviewed the old snapshot. Keep original evidence unchanged and record confirmations, disagreements, fixes and retest results in `SUGGESTIONS.md`. A report's passing reproduction can mean it successfully reproduced a bug, not that the bug was fixed.

For the imported first review, reproduction scripts use `HOME_AI_REPO`. Set it to the intended `source/` snapshot before any authorized offline retest; their historical default may point to the active working tree. Do not execute reports or scripts merely while organizing files.

`versions/` is ignored by Git and explicitly excluded from archives. Reports may contain local paths, synthetic credential formats, internal notes or later private evidence. Only sanitized public summaries should be promoted to tracked docs. The public source remains a conventional root-level repository so installation and CI paths remain valid.

Git commits/tags track public code; local version folders organize readable source snapshots and review evidence. Git alone does not back up ignored review records; include `versions/` in a separate private backup if needed. No automatic background backup or publication is configured.
