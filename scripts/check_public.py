"""Scan tracked text and Git history for common accidental disclosure patterns.
This is a heuristic check, not a guarantee of privacy or a replacement for review.
"""
import re
import subprocess
from pathlib import Path

PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}"),
    re.compile(r"(?i)[A-Z]:[\\/]Users[\\/](?!Public|Example)[^\s/\\]+"),
    re.compile(r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3})\b"),
]

def main():
    root=Path(__file__).resolve().parents[1]
    names=subprocess.check_output(['git','ls-files','-z'],cwd=root).decode().split('\0')
    failures=[]
    for name in filter(None,names):
        path=root/name
        if path.suffix in ('.db','.log','.tar','.zip') or name.startswith(('data/','logs/','backups/','.private-audit/')):
            failures.append(name+': prohibited artifact')
        text=path.read_text(encoding='utf-8',errors='replace')
        if any(p.search(text) for p in PATTERNS): failures.append(name+': sensitive pattern')
    history=subprocess.check_output(['git','log','--all','-p','--format=fuller'],cwd=root).decode('utf-8',errors='replace')
    if any(p.search(history) for p in PATTERNS): failures.append('Git history: sensitive pattern')
    for failure in failures: print(failure)
    print('Public scan: '+('FAIL' if failures else 'PASS')+f' ({sum(bool(n) for n in names)} tracked files)')
    return bool(failures)

if __name__=='__main__': raise SystemExit(main())
