#!/usr/bin/env python3
"""Undo one build: put back the files it changed or deleted and remove the files it added (stdlib only).

The runner saves what this needs beside every builder reply (Queue/attachments/<mail id>/undo.json and before/), because
a repo folder need not be a git repository. Run it on the device that did the build. A file edited again after the
build is still overwritten with its pre-build content, so undo the newest build first.

  undo-build.py MAIL_ID [--dry-run]
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import hostname, queue_root

def main():
    if len(sys.argv) < 2: sys.exit(__doc__)
    mid, dry = sys.argv[1], "--dry-run" in sys.argv
    d = queue_root() / "attachments" / mid
    try: u = json.loads((d / "undo.json").read_text(encoding="utf-8"))
    except (OSError, ValueError): sys.exit(f"no undo record for {mid}")
    if u.get("host", "").lower() != hostname().lower(): sys.exit(f"that build ran on {u.get('host')}; run this there")
    cwd = Path(u["cwd"])
    for rel in u.get("restore", []):
        print(("would restore " if dry else "restored ") + rel)
        if not dry:
            (cwd / rel).parent.mkdir(parents=True, exist_ok=True); (cwd / rel).write_text((d / "before" / rel).read_text(encoding="utf-8"), encoding="utf-8")
    for rel in u.get("delete", []):
        print(("would remove " if dry else "removed ") + rel)
        if not dry: (cwd / rel).unlink(missing_ok=True)

if __name__ == "__main__": main()
