#!/usr/bin/env python3
"""Codex `notify` wrapper: record the session for the status board, then run the notify program that was
configured before. Usage in config.toml: notify = [python, this file, <original program>, <original args...>].
`codex-notify.py --install` wraps whatever notify is set now (idempotent, backs up config.toml)."""
import json, re, shutil, subprocess, sys, tomllib
from pathlib import Path
HERE = Path(__file__).resolve().parent

def install():
    cfg = Path.home() / ".codex/config.toml"; s = cfg.read_text(encoding="utf-8")
    cur = tomllib.loads(s).get("notify") or []
    if any("codex-notify.py" in str(x) for x in cur): print("already wrapped"); return
    new = [sys.executable.replace("\\", "/"), "-X", "utf8", str(HERE / "codex-notify.py").replace("\\", "/"), *cur]
    line = "notify = " + json.dumps(new)
    shutil.copy2(cfg, str(cfg) + ".pre-codex-notify")
    if cur:
        out, n = re.subn(r"(?ms)^notify\s*=\s*\[.*?\]\s*$", lambda m: line, s, count=1)
        if n != 1: sys.exit("could not find the notify line to replace; left untouched")
    else:
        first = re.search(r"(?m)^\[", s); i = first.start() if first else len(s)
        out = s[:i] + line + "\n" + s[i:]
    tomllib.loads(out); cfg.write_text(out, encoding="utf-8"); print("wrapped:", cur or "(no previous notify)")

def main():
    if sys.argv[1:2] == ["--install"]: return install()
    if len(sys.argv) < 2: return
    orig, payload = sys.argv[1:-1], sys.argv[-1]
    try: subprocess.run([sys.executable, "-X", "utf8", str(HERE / "session-hook.py"), "--codex", payload], timeout=5, capture_output=True)
    except Exception: pass
    if orig:
        try: subprocess.run(orig + [payload], timeout=60)
        except Exception: pass

if __name__ == "__main__": main()
