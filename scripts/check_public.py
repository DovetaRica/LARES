"""Heuristic privacy checks for tracked files, staged blobs, history and explicit artifacts.
No finding prints matched values. This is not proof of anonymity or secret absence.
"""
import argparse
import io
import re
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

MAX_MEMBER = 8 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
MAX_FILES = 10000
PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}"),
    re.compile(r"(?i)[A-Z]:[\\/]Users[\\/](?!Public|Example)[^\s/\\]+"),
    re.compile(r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3})\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
]
ASSIGNMENT = re.compile(r"(?im)\b(?:password|secret|api_key|access_token|[a-z0-9_]+_(?:token|password|secret|api_key))[\"']?\s*[:=]\s*(?:[\"']([^\"'\r\n]{8,})[\"']|([a-zA-Z0-9_!@#$%+=:/-]{8,})(?=\s|$))")


def scan_text(text):
    findings = ["sensitive pattern" for p in PATTERNS if p.search(text)]
    for match in ASSIGNMENT.finditer(text):
        value = match.group(1) or match.group(2)
        # Explicit placeholders only; no blanket file/test allowlist.
        if value.startswith(("synthetic-", "example-", "replace_", "${", "<")):
            continue
        if value in ("os.environ.get(",):
            continue
        findings.append("credential assignment")
    return sorted(set(findings))


def forbidden_name(name):
    parts = PurePosixPath(name.replace("\\", "/")).parts
    return any(p in (".git", ".private-audit", ".venv", "versions", "data", "logs", "backups")
               or (p.startswith(".env") and p not in (".env.example", ".env.template")) for p in parts) or name.endswith((".db", ".db-wal", ".db-shm", ".log"))


def inspect_member(name, data):
    problems = ["prohibited artifact"] if forbidden_name(name) else []
    if len(data) > MAX_MEMBER:
        return problems + ["file exceeds scan budget"]
    for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
        problems.extend(scan_text(data.decode(encoding, errors="replace")))
    return sorted(set(problems))


def scan_artifact(path, budget=None):
    path = Path(path)
    failures = []
    budget = budget if budget is not None else {"total": 0, "count": 0}
    def check(name, data):
        budget["total"] += len(data); budget["count"] += 1
        if budget["count"] > MAX_FILES or budget["total"] > MAX_TOTAL:
            raise ValueError("Artifact exceeds scan budget")
        failures.extend(name + ": " + issue for issue in inspect_member(name, data))
    def safe_name(name):
        p = PurePosixPath(name.replace("\\", "/"))
        return not p.is_absolute() and ".." not in p.parts and ":" not in name
    if path.is_dir():
        for member in path.rglob("*"):
            if member.is_symlink():
                failures.append("symlink in artifact directory")
            elif member.is_file():
                if member.stat().st_size > MAX_MEMBER:
                    failures.append(member.name + ": file exceeds scan budget")
                elif member.name.endswith((".whl", ".zip", ".tar.gz", ".tgz")):
                    budget["count"] += 1
                    if budget["count"] > MAX_FILES:
                        raise ValueError("Artifact exceeds scan budget")
                    failures.extend(scan_artifact(member, budget))
                else:
                    check(str(member.relative_to(path)), member.read_bytes())
    elif path.name.endswith((".zip", ".whl")):
        with zipfile.ZipFile(path) as archive:
            if len(archive.infolist()) > MAX_FILES:
                raise ValueError("Artifact exceeds scan budget")
            for member in archive.infolist():
                if not safe_name(member.filename) or stat.S_ISLNK(member.external_attr >> 16):
                    failures.append("unsafe archive member"); continue
                if member.is_dir(): continue
                if member.file_size > MAX_MEMBER or budget["total"] + member.file_size > MAX_TOTAL:
                    raise ValueError("Artifact exceeds scan budget")
                if member.filename.endswith((".zip", ".whl", ".tar.gz", ".tgz")):
                    failures.append("nested archive requires separate scan"); continue
                check(member.filename, archive.read(member))
    elif path.name.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(path, "r|*") as archive:
            for member in archive:
                if not safe_name(member.name) or not (member.isfile() or member.isdir()):
                    failures.append("unsafe archive member"); continue
                if member.isdir(): continue
                if member.size > MAX_MEMBER or budget["total"] + member.size > MAX_TOTAL:
                    raise ValueError("Artifact exceeds scan budget")
                if member.name.endswith((".zip", ".whl", ".tar.gz", ".tgz")):
                    failures.append("nested archive requires separate scan"); continue
                check(member.name, archive.extractfile(member).read(MAX_MEMBER + 1))
    else:
        if path.stat().st_size > MAX_MEMBER:
            raise ValueError("Artifact exceeds scan budget")
        check(path.name, path.read_bytes())
    return failures


def scan_repository(root):
    def git(*args, **kwargs):
        return subprocess.check_output(["git", *args], cwd=root, **kwargs)
    staged = git("ls-files", "--stage", "-z").decode().split("\0")
    failures = []
    objects = set()
    names = []
    for entry in filter(None, staged):
        meta, name = entry.split("\t", 1)
        objects.add(meta.split()[1]); names.append(name)
        path = root / name
        if path.is_symlink():
            failures.append(name + ": symlink")
        elif path.exists():
            if path.stat().st_size > MAX_MEMBER:
                failures.append(name + ": file exceeds scan budget")
            else:
                failures.extend(name + ": " + issue for issue in inspect_member(name, path.read_bytes()))
        if forbidden_name(name): failures.append(name + ": prohibited path")
    history = git("rev-list", "--objects", "--all").decode().splitlines()
    for line in history:
        oid, _, name = line.partition(" ")
        objects.add(oid)
        if name and forbidden_name(name): failures.append("history: prohibited path")
    if len(objects) > MAX_FILES:
        raise ValueError("History exceeds scan budget")
    metadata = git("cat-file", "--batch-check", input=("\n".join(objects)+"\n").encode()).decode().splitlines()
    blobs = []
    total = 0
    for line in metadata:
        oid, kind, size = line.split()
        if kind != "blob": continue
        size = int(size); total += size
        if size > MAX_MEMBER or total > MAX_TOTAL:
            raise ValueError("History exceeds scan budget")
        blobs.append(oid)
    data = io.BytesIO(git("cat-file", "--batch", input=("\n".join(blobs)+"\n").encode()))
    for _ in blobs:
        oid, kind, size = data.readline().split()
        contents = data.read(int(size)); data.read(1)
        if inspect_member("history-blob", contents):
            failures.append("staged/history blob " + oid.decode()[:12] + ": sensitive pattern")
    identity = git("log", "--all", "--format=fuller").decode("utf-8", errors="replace")
    if scan_text(identity): failures.append("commit metadata: sensitive pattern")
    return sorted(set(failures)), len(names)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", action="append", type=Path, default=[])
    parser.add_argument("--artifacts-only", action="store_true")
    args = parser.parse_args(argv)
    if args.artifacts_only and not args.artifact:
        parser.error("--artifacts-only requires --artifact")
    root = Path(__file__).resolve().parents[1]
    try:
        failures, count = ([], 0) if args.artifacts_only else scan_repository(root)
        for path in args.artifact:
            failures.extend(scan_artifact(path))
        for failure in failures: print(failure)
        print("Public scan: " + ("FAIL" if failures else "PASS") + f" ({count} tracked files, {len(args.artifact)} artifact inputs)")
        return int(bool(failures))
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile, subprocess.CalledProcessError):
        print("Public scan: FAIL (input unavailable, invalid, or scan budget exceeded)")
        return 2

if __name__ == "__main__": raise SystemExit(main())
