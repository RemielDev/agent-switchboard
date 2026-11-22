#!/usr/bin/env python3
"""Refuse secrets and machine-specific details in the repository (stdlib only). CI runs it on every push.

  python3 tools/check-clean.py          scan every text file under the repository root
  python3 tools/check-clean.py PATH...  scan only these files or folders

It looks for things that must never be committed here: access tokens and private keys, real Discord ids, home
folder paths of a real account, private network addresses, e-mail addresses, and live state (a Queue/ folder).
A line may opt out with the marker `check-clean: ok` when the match is a deliberate example."""
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT = {".py", ".md", ".json", ".ps1", ".sh", ".yml", ".yaml", ".toml", ".txt", ".plist", ".svg", ".excalidraw", ".html", ".cfg", ""}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".prep", "node_modules"}
EXAMPLE_USERS = {"alex", "you", "me", "name", "user", "username", "runner", "shared"}

RULES = [
    ("Discord bot token", re.compile(r"\b[MNO][A-Za-z\d_-]{23,25}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}\b")),
    ("API key", re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,}|nvapi-[A-Za-z0-9_-]{20,})\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Discord id (snowflake)", re.compile(r"(?<![\d.])\d{17,20}(?![\d.])")),
    ("home folder path", re.compile(r"(?i)(?:/Users/|/home/|[A-Z]:\\+Users\\+)([A-Za-z][\w.-]*)")),
    ("private network address", re.compile(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))\.\d{1,3}\.\d{1,3}\b")),
    ("tailnet name", re.compile(r"\b[\w-]+\.ts\.net\b")),
    ("e-mail address", re.compile(r"\b[\w.+-]+@(?!example\.|users\.noreply\.|anthropic\.com)[\w-]+\.[a-z]{2,}\b")),
]

def files(args):
    roots = [Path(a) for a in args] or [ROOT]
    for r in roots:
        if r.is_file(): yield r; continue
        for p in sorted(r.rglob("*")):
            if p.is_file() and not (set(p.relative_to(r).parts) & SKIP_DIRS) and p.suffix.lower() in TEXT: yield p

def main():
    bad = 0
    for p in files(sys.argv[1:]):
        if p.resolve() == Path(__file__).resolve(): continue
        if "Queue" in p.parts and p.suffix != ".md":
            print(f"{p}: live state: a Queue/ folder must not be committed"); bad += 1; continue
        try: lines = p.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError): continue
        for n, line in enumerate(lines, 1):
            if "check-clean: ok" in line: continue
            for what, rx in RULES:
                for m in rx.finditer(line):
                    if what == "home folder path" and m.group(1).lower() in EXAMPLE_USERS: continue
                    print(f"{p}:{n}: {what}: {m.group(0)[:60]}"); bad += 1
    print("clean" if not bad else f"{bad} problem(s)")
    return 1 if bad else 0

if __name__ == "__main__": sys.exit(main())
