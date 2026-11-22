#!/usr/bin/env python3
"""Check this device's Agent Switchboard setup, and fix the safe parts (stdlib only). Safe to run again.

  doctor.py                 check only: print OK, FIX (doctor --fix can do it) and TODO (needs you) lines
  doctor.py --fix           also create folders, install role files and config defaults, register the mail agents
  doctor.py --fix --root P  first remember P as the root on this device (~/.switchboard/root)
  doctor.py --json          the same checks as one JSON object, for an AI agent doing the setup

--fix never overwrites a file you already have, never touches your Claude or Codex settings, never installs
anything and never makes a program start at login. Those steps are listed as TODO with the exact command.
Exit code 0 when nothing required is missing."""
import argparse, importlib.util, json, os, platform, shutil, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SWITCHBOARD = REPO / "switchboard"
TEMPLATES = REPO / "templates"
sys.path.insert(0, str(SWITCHBOARD))

AGENTS = {  # name -> (kind, auto, hosts: "any" or "here", description)
    "owner": ("human", False, "any", "The owner. Mail here is posted to Discord."),
    "sessions": ("human", False, "any", "Interactive session hooks. Sends the owner permission requests; not a mailbox to write to."),
    "orchestrator": ("claude", True, "here", "Takes the owner's instructions, delegates, reports back."),
    "builder": ("claude", True, "here", "Builds one task per message."),
    "reviewer": ("claude", True, "here", "Reviews work it did not write. Read-only."),
}

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SWITCHBOARD / filename)
    mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod; spec.loader.exec_module(mod)
    return mod

class Report:
    def __init__(self): self.rows = []
    def add(self, status, what, detail="", cmd=""): self.rows.append({"status": status, "check": what, "detail": detail, "command": cmd})
    def ok(self, what, detail=""): self.add("OK", what, detail)
    def fix(self, what, detail=""): self.add("FIX", what, detail, "python3 setup/doctor.py --fix")
    def todo(self, what, detail="", cmd=""): self.add("TODO", what, detail, cmd)
    def note(self, what, detail="", cmd=""): self.add("NOTE", what, detail, cmd)

def hosts_of(info):
    h = info.get("host")
    return [h] if isinstance(h, str) else list(h or [])

def check(fix, root_arg, name_arg):
    r = Report()
    v = sys.version_info
    (r.ok if v >= (3, 11) else r.todo)("python", f"{v.major}.{v.minor} (3.11 or newer needed)")

    if root_arg:
        p = Path(root_arg).expanduser().resolve()
        if fix:
            p.mkdir(parents=True, exist_ok=True)
            (Path.home() / ".switchboard").mkdir(parents=True, exist_ok=True)
            (Path.home() / ".switchboard" / "root").write_text(str(p) + "\n", encoding="utf-8")
        else: os.environ["SWITCHBOARD_ROOT"] = str(p)
    import common
    root = common.dev_root()
    how = "SWITCHBOARD_ROOT" if os.environ.get("SWITCHBOARD_ROOT") else "~/.switchboard/root" if (Path.home() / ".switchboard" / "root").is_file() else "the folder this checkout sits in"
    if not root.is_dir(): r.todo("root", f"{root} does not exist (from {how})", "python3 setup/doctor.py --fix --root <folder that holds your repos>"); return r
    r.ok("root", f"{root} (from {how})")
    queue = root / "Queue"
    if not queue.is_dir() and not fix: r.fix("queue", f"{queue} is missing")
    else: common.queue_root(); r.ok("queue", str(queue))
    if (queue / "STOP").exists(): r.note("stop file", f"{queue / 'STOP'} exists: every runner is paused until you delete it")

    for exe, need, why in (("claude", True, "Claude Code CLI: the runner starts it for every message"),
                           ("codex", False, "Codex CLI: optional second engine for builds and reviews"),
                           ("agy", False, "optional small-model CLI used for Discord triage and tidying"),
                           ("syncthing", False, "optional: shares the root between your devices")):
        path = shutil.which(exe)
        if path: r.ok(exe, path)
        elif need: r.todo(exe, f"not on PATH. {why}", "https://docs.claude.com/en/docs/claude-code/setup")
        else: r.note(exe, f"not installed. {why}")

    if not queue.is_dir(): return r
    mail = load("agent_mail", "agent-mail.py")
    me = common.hostname()
    agents = mail.load_agents()
    for name, (kind, auto, where, text) in AGENTS.items():
        have = agents.get(name)
        want_host = "any" if where == "any" else me
        if have and (where == "any" or me.lower() in [h.lower() for h in hosts_of(have)] or "any" in hosts_of(have)):
            r.ok(f"agent {name}", f"kind {have.get('kind')}, host {have.get('host')}"); continue
        if not fix: r.fix(f"agent {name}", "not registered" if not have else f"registered for {have.get('host')}, not for this device ({me})"); continue
        hosts = want_host if not have else ",".join(hosts_of(have) + [me])
        mail.register(name, hosts, kind, auto, None, text); r.ok(f"agent {name}", f"registered for {hosts}")
    mine = name_arg or "".join(c if c.isalnum() else "-" for c in me.lower()).strip("-") or "here"
    if mine in mail.load_agents(): r.ok(f"agent {mine}", "your interactive sessions on this device")
    elif fix: mail.register(mine, me, "claude", False, None, f"Interactive sessions on {me}."); r.ok(f"agent {mine}", "registered for your interactive sessions on this device")
    else: r.fix(f"agent {mine}", "the mailbox for your own interactive sessions on this device is not registered")

    roles = queue / "mail" / "roles"
    missing = [str(p.relative_to(TEMPLATES / "roles")) for p in sorted((TEMPLATES / "roles").rglob("*.md")) if not (roles / p.relative_to(TEMPLATES / "roles")).is_file()]
    if not missing: r.ok("roles", str(roles))
    elif fix:
        for rel in missing:
            (roles / rel).parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(TEMPLATES / "roles" / rel, roles / rel)
        r.ok("roles", f"installed {', '.join(missing)}")
    else: r.fix("roles", f"missing in {roles}: {', '.join(missing)}")

    for rel, src in (("mail/runner.json", "runner.json"), ("loop/config.json", "loop.config.json")):
        dest = queue / rel
        if dest.is_file(): r.ok(rel, str(dest))
        elif fix: dest.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(TEMPLATES / "config" / src, dest); r.ok(rel, "installed the default")
        else: r.fix(rel, "missing; the built-in defaults apply until it exists")

    token = Path.home() / ".switchboard" / "discord" / "token.txt"
    try: cfg = json.loads((queue / "discord" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError): cfg = {}
    if token.is_file() and token.read_text(encoding="utf-8").strip(): r.ok("discord token", str(token))
    else: r.todo("discord token", f"create a bot at https://discord.com/developers/applications and save its token in {token} (docs/discord-setup.md)")
    if cfg.get("guild_id") and cfg.get("channels"): r.ok("discord server", f"configured, {len(cfg['channels'])} channels")
    else: r.todo("discord server", "not set up", f"python3 {SWITCHBOARD / 'discord-bridge.py'} setup --guild <your server id>")

    try: settings = (Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8")
    except OSError: settings = ""
    if "session-hook.py" in settings: r.ok("session hooks", "installed in ~/.claude/settings.json")
    else: r.todo("session hooks", "optional: shows your own sessions in Discord and forwards permission requests when you are away", f"python3 {SWITCHBOARD / 'install-session-hooks.py'}")
    r.note("mail tools for your own sessions", "optional: lets an interactive Claude Code session send and read agent mail",
           f"claude mcp add --scope user agent_mail -- {sys.executable} {SWITCHBOARD / 'agent-mail-mcp.py'} --agent {mine}")
    r.note("start at login", "optional, ask the owner first", "python3 setup/autostart.py show")

    try:
        fleet = load("fleet", "fleet.py"); fleet.beat()
        r.ok("heartbeat", f"{me} ({platform.system()}), repos here: {', '.join(fleet.repos_here()) or 'none yet'}")
    except Exception as e: r.todo("heartbeat", f"fleet.py could not write a heartbeat: {e}")
    return r

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fix", action="store_true"); ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", help="the folder that holds your repos and the shared Queue/")
    ap.add_argument("--name", help="mail agent name for your own sessions on this device (default: the host name)")
    a = ap.parse_args()
    r = check(a.fix, a.root, a.name)
    need = [x for x in r.rows if x["status"] in ("FIX", "TODO") and x["check"] in ("python", "root", "queue", "claude") or (x["status"] == "FIX")]
    if a.json: print(json.dumps({"ok": not need, "checks": r.rows}, indent=2))
    else:
        for x in r.rows:
            print(f"{x['status']:5} {x['check']:34} {x['detail']}" + (f"\n      -> {x['command']}" if x["command"] and x["status"] != "OK" else ""))
        print("\nCore ready: mail, agents and the runner. TODO lines are optional extras (Discord, hooks)." if not need else "\nNot ready: run `python3 setup/doctor.py --fix`, then handle the TODO lines." if any(x["status"] == "FIX" for x in need) else "\nNot ready: handle the TODO lines above.")
    return 0 if not need else 1

if __name__ == "__main__": sys.exit(main())
