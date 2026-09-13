"""Create a local, ignored release folder from an immutable Git tag.
Raw review files are local records and must not enter the public history.
"""
import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root)


def snapshot(root, version, review_dir=None):
    root = Path(root).resolve()
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", version):
        raise ValueError("Use a release tag such as v0.1.0-alpha.1")
    ref = "refs/tags/" + version
    commit = git(root, "rev-parse", "--verify", ref + "^{commit}").decode().strip()
    target = root / "versions" / version
    if target.exists():
        raise FileExistsError("Version folder already exists; preserve it and add a new review round")
    review_files = []
    if review_dir:
        review_dir = Path(review_dir).resolve()
        if not (review_dir / "REVIEW.md").is_file():
            raise ValueError("Review directory must contain REVIEW.md")
        review_files = [p for p in review_dir.rglob("*") if p.is_file() and not p.is_symlink()
                        and p.suffix in (".md", ".py") and not any(part.startswith(".") or part == "__pycache__" for part in p.relative_to(review_dir).parts)]
    archive = git(root, "archive", "--format=zip", ref)
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        # A source snapshot contains only the tagged public archive, never local files.
        for member in z.infolist():
            resolved = (target / "source" / member.filename).resolve()
            if not resolved.is_relative_to((target / "source").resolve()):
                raise ValueError("Unsafe archive path")
        (target / "source").mkdir(parents=True)
        z.extractall(target / "source")
    (target / "reviews").mkdir()
    (target / "artifacts").mkdir()
    (target / "artifacts" / (version + "-source.zip")).write_bytes(archive)
    if review_files:
        round_dir = target / "reviews" / "round-001"
        for file in review_files:
            dest = round_dir / file.relative_to(review_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, dest)
    manifest = {"version": version, "source_commit": commit,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_format": "git archive of release tag; export-ignore rules apply",
                "public_upload": False,
                "immutable_files": {str(p.relative_to(target)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                                    for folder in ("source", "reviews", "artifacts") for p in (target / folder).rglob("*") if p.is_file()}}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (target / "SUGGESTIONS.md").write_text("# Suggestions and review tracking\n\nKeep original reports unchanged. Record new rounds under reviews/round-002, round-003, etc.\n\n| ID | Finding | Status | Fix commit | Retest evidence |\n| --- | --- | --- | --- | --- |\n", encoding="utf-8")
    (target / "README.md").write_text(f"# {version}\n\nSource commit: `{commit}`.\n\n- `source/`: frozen public release source; edit the root working tree for fixes.\n- `reviews/`: original reports and reproductions by round.\n- `SUGGESTIONS.md`: editable findings, decisions and retest tracking.\n- `artifacts/`: tagged source archive.\n- `manifest.json`: SHA-256 inventory of original snapshot and initial review files.\n\nThis entire directory is local and ignored by Git. Do not upload it without a privacy review.\n", encoding="utf-8")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--review-dir", type=Path)
    args = parser.parse_args()
    target = snapshot(Path(__file__).resolve().parents[1], args.version, args.review_dir)
    print(target)


if __name__ == "__main__":
    main()
