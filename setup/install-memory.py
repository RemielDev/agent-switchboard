#!/usr/bin/env python3
"""Set up shared memory for Claude Code on this device (stdlib only). Optional. Safe to run again.

  install-memory.py --vault FOLDER             create the vault from templates/memory/vault (only files that are
                                               missing) and write ~/.switchboard/registry.json
  install-memory.py --vault FOLDER --hooks     also add the memory loader to ~/.claude/settings.json
  install-memory.py --remove-hooks             take the loader's hook entries out again (other hooks stay)
  ... --dry-run                                show what would change, write nothing

The vault is a plain folder of Markdown notes: sync it between your devices however you like (keep its Local/
folder out of the sync). The loader feeds each session the MEMORY.md files that apply to the folder it works in;
see docs/memory-and-skills.md. Route a project to its own rules with `switchboard/memoryctl.py init <folder>`.
~/.claude/settings.json is copied to settings.json.pre-switchboard-memory before its first change."""
import argparse, json, shutil, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOADER = REPO / "switchboard" / "claude-memory-loader.py"
TEMPLATE = REPO / "templates" / "memory" / "vault"
BASE = Path.home() / ".switchboard"
SETTINGS = Path.home() / ".claude" / "settings.json"
CHUNKS = 8
MARK = "claude-memory-loader.py"

def command(chunk=None):
    exe = f'"{sys.executable}"' + (" -X utf8" if sys.platform == "win32" else "")
    return f'{exe} "{LOADER.as_posix()}"' + ("" if chunk is None else f" --chunk {chunk} --chunks {CHUNKS}")

def wanted():
    slots = [{"type": "command", "command": command(n), "timeout": 10} for n in range(CHUNKS)]
    return {"SessionStart": {"matcher": "startup|resume|clear|compact", "hooks": slots},
            "UserPromptSubmit": {"hooks": slots},
            "PreToolUse": {"matcher": "Read|Edit|Write|MultiEdit", "hooks": [{"type": "command", "command": command(), "timeout": 10}]}}

def without_ours(groups):
    out = []
    for g in groups or []:
        hooks = [h for h in g.get("hooks", []) if MARK not in str(h.get("command", ""))]
        if hooks: out.append({**g, "hooks": hooks})
    return out

def set_hooks(add, dry):
    try: data = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.is_file() else {}
    except ValueError: sys.exit(f"{SETTINGS} is not valid JSON; fix it first")
    before = json.dumps(data, sort_keys=True)
    hooks = data.setdefault("hooks", {})
    for event, group in wanted().items():
        rest = without_ours(hooks.get(event))
        hooks[event] = rest + [group] if add else rest
        if not hooks[event]: del hooks[event]
    if not hooks: del data["hooks"]
    if json.dumps(data, sort_keys=True) == before: return print("hooks: already as wanted")
    print(f"hooks: {'add' if add else 'remove'} the memory loader in {SETTINGS}" + (" (dry run)" if dry else ""))
    if dry: return
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    backup = SETTINGS.with_name(SETTINGS.name + ".pre-switchboard-memory")
    if SETTINGS.is_file() and not backup.exists(): shutil.copyfile(SETTINGS, backup)
    SETTINGS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

def set_vault(vault, dry):
    vault = Path(vault).expanduser().resolve()
    for src in sorted(TEMPLATE.rglob("*")):
        if not src.is_file(): continue
        dest = vault / src.relative_to(TEMPLATE)
        if dest.exists(): continue
        print(f"vault: create {dest}" + (" (dry run)" if dry else ""))
        if not dry: dest.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(src, dest)
    reg_path = BASE / "registry.json"
    try: reg = json.loads(reg_path.read_text(encoding="utf-8")) if reg_path.is_file() else {}
    except ValueError: sys.exit(f"{reg_path} is not valid JSON; fix it first")
    new = {**reg, "vault": str(vault)}
    new.setdefault("device_note", "Local/This Device.md"); new.setdefault("scopes", []); new.setdefault("stores", [])
    if new == reg: return print(f"registry: already points at {vault}")
    print(f"registry: write {reg_path}" + (" (dry run)" if dry else ""))
    if not dry: BASE.mkdir(parents=True, exist_ok=True); reg_path.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault"); ap.add_argument("--hooks", action="store_true")
    ap.add_argument("--remove-hooks", action="store_true"); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not (a.vault or a.hooks or a.remove_hooks): ap.error("give --vault FOLDER, --hooks or --remove-hooks")
    if a.vault: set_vault(a.vault, a.dry_run)
    if a.remove_hooks: set_hooks(False, a.dry_run)
    elif a.hooks: set_hooks(True, a.dry_run)

if __name__ == "__main__": main()
