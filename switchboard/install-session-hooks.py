#!/usr/bin/env python3
"""Install the session hooks on this device (stdlib only, macOS and Windows). Safe to run again.

  install-session-hooks.py              merge the hooks into ~/.claude/settings.json and set Codex notify
  install-session-hooks.py --dry-run    show what would change, write nothing
  install-session-hooks.py --remove     take our entries out again (other hooks stay)
  install-session-hooks.py --python P   use this interpreter in the hook commands instead of sys.executable

What it does:
- ~/.claude/settings.json: adds session-hook.py to UserPromptSubmit, Stop, Notification and SessionEnd
  (timeout 5 s) and permission-hook.py to PermissionRequest (timeout 600 s, the hook waits up to 8 minutes).
  Existing hooks are kept as they are; our entries are found by script name and updated in place.
- ~/.codex/config.toml: adds `notify = [python, session-hook.py, "--codex"]` only when the file exists and has
  no `notify` key. An existing one is reported and left alone.
- Registers the mail agent `sessions` (the sender of permission requests) if it is missing.
Each file is copied to <name>.pre-session-hooks before its first change."""
import argparse, importlib.util, json, os, shutil, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent

SESSION_EVENTS = ("UserPromptSubmit", "Stop", "Notification", "SessionEnd")
SCRIPTS = {"session-hook.py": (SESSION_EVENTS, 5), "permission-hook.py": (("PermissionRequest",), 600)}
BACKUP = ".pre-session-hooks"


def quote(p, always):
    p = str(p).replace("\\", "/")  # forward slashes survive Git Bash, PowerShell and sh alike
    return f'"{p}"' if always or " " in p else p

def command(python, script):
    win = os.name == "nt"
    return quote(python, win) + (" -X utf8 " if win else " ") + quote(HERE / script, win)

def backup(path, dry):
    b = path.with_name(path.name + BACKUP)
    if b.exists(): return f"backup kept: {b}"
    if not dry: shutil.copy2(path, b)
    return f"backup: {b}"

def write_text(path, text, dry):
    if dry: return
    tmp = path.with_name("." + path.name + ".session-hooks.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    try: shutil.copymode(path, tmp)
    except OSError: pass
    os.replace(tmp, path)

def is_ours(hook, script):
    return isinstance(hook, dict) and script in str(hook.get("command", "")) + " ".join(map(str, hook.get("args") or []))

def claude_settings(python, remove, dry):
    path = (Path.home() / ".claude" / "settings.json")
    path = path.resolve() if path.exists() else path
    out = []
    if path.exists():
        before = path.read_text(encoding="utf-8")
        data = json.loads(before)
        if not isinstance(data, dict): raise SystemExit(f"{path} is not a JSON object; nothing changed")
    elif remove: return [f"{path}: absent, nothing to remove"]
    else: before, data = None, {}
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict): raise SystemExit(f"{path}: \"hooks\" is not an object; nothing changed")
    for script, (events, timeout) in SCRIPTS.items():
        want = {"type": "command", "command": command(python, script), "timeout": timeout}
        for ev in events:
            groups = hooks.get(ev)
            if not isinstance(groups, list): groups = []
            mine = [h for g in groups if isinstance(g, dict) for h in (g.get("hooks") or []) if is_ours(h, script)]
            if not remove and mine == [want]: continue  # already installed as wanted
            kept = []  # every group without our entries; a group that held only ours goes
            for g in groups:
                if isinstance(g, dict) and isinstance(g.get("hooks"), list) and any(is_ours(h, script) for h in g["hooks"]):
                    g = dict(g, hooks=[h for h in g["hooks"] if not is_ours(h, script)])
                    if not g["hooks"]: continue
                kept.append(g)
            if not remove: kept.append({"hooks": [want]})
            if mine or not remove:
                was = "; ".join(str(h.get("command", ""))[:140] for h in mine)
                out.append(f"{ev}: " + ("removed " + script if remove else f"{script} now points at this checkout (was: {was})" if mine else "added " + script))
            if kept: hooks[ev] = kept
            else: hooks.pop(ev, None)
    if not hooks: data.pop("hooks", None)
    after = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if before is not None and json.loads(before) == data: return [f"{path}: already up to date"]
    if before is None:
        if not dry: path.parent.mkdir(parents=True, exist_ok=True); path.write_text(after, encoding="utf-8", newline="\n")
        return [f"{path}: created"] + out
    note = backup(path, dry)
    write_text(path, after, dry)
    return [f"{path}: {'would change' if dry else 'changed'} ({note})"] + out

def codex_config(python, remove, dry):
    path = Path.home() / ".codex" / "config.toml"
    if not path.exists(): return [f"{path}: absent, Codex notify not set"]
    path = path.resolve()
    text = path.read_text(encoding="utf-8")
    line = "notify = " + json.dumps([str(python).replace("\\", "/")] + (["-X", "utf8"] if os.name == "nt" else [])
                                    + [str(HERE / "session-hook.py").replace("\\", "/"), "--codex"])
    try:
        import tomllib
        current = tomllib.loads(text).get("notify")
    except Exception as e:
        return [f"{path}: could not be parsed ({type(e).__name__}); left alone. Add by hand: {line}"]
    ours = isinstance(current, list) and any("session-hook.py" in str(x) for x in current)
    if remove:
        if not ours: return [f"{path}: no session-hook notify to remove"]
        kept = [l for l in text.splitlines(keepends=True) if not (l.lstrip().startswith("notify") and "session-hook.py" in l)]
        note = backup(path, dry); write_text(path, "".join(kept), dry)
        return [f"{path}: notify removed ({note})"]
    if ours:
        if line in text: return [f"{path}: notify already set"]
        new = "".join(line + "\n" if (l.lstrip().startswith("notify") and "session-hook.py" in l) else l for l in text.splitlines(keepends=True))
        note = backup(path, dry); write_text(path, new, dry)
        return [f"{path}: notify updated ({note})"]
    if current is not None:
        return [f"{path}: notify is already set to something else and was left alone: {json.dumps(current)}",
                f"  to list Codex sessions too, run both from one wrapper, or replace it with: {line}"]
    note = backup(path, dry)
    write_text(path, line + "\n" + text, dry)  # top of the file: a top-level key must come before any [table]
    return [f"{path}: {'would add' if dry else 'added'} {line} ({note})"]

def mail_agent(dry):
    spec = importlib.util.spec_from_file_location("agent_mail", HERE / "agent-mail.py")
    mail = importlib.util.module_from_spec(spec); sys.modules["agent_mail"] = mail
    spec.loader.exec_module(mail)
    if "sessions" in {k.lower() for k in mail.load_agents()}: return ["mail agent sessions: already registered"]
    if not dry:
        mail.register("sessions", "any", "human", auto=False,
                      description="Interactive session hooks. Sends the owner permission requests; not a mailbox to write to.")
    return [f"mail agent sessions: {'would register' if dry else 'registered'}"]

def main():
    for s in (sys.stdout, sys.stderr):
        try: s.reconfigure(encoding="utf-8", errors="replace")
        except Exception: pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--remove", action="store_true")
    ap.add_argument("--python", default=sys.executable); ap.add_argument("--no-codex", action="store_true")
    a = ap.parse_args()
    lines = claude_settings(a.python, a.remove, a.dry_run)
    if not a.no_codex: lines += codex_config(a.python, a.remove, a.dry_run)
    if not a.remove:
        try: lines += mail_agent(a.dry_run)
        except Exception as e: lines.append(f"mail agent sessions: not registered ({type(e).__name__}: {e}); the hook registers it on first use")
    if a.dry_run: print("Dry run: nothing was written. Without --dry-run these changes would be made:")
    print("\n".join(lines))
    if not a.dry_run and not a.remove: print("New Claude Code sessions use the hooks; restart running ones.")
    return 0

if __name__ == "__main__": sys.exit(main())
