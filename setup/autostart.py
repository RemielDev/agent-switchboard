#!/usr/bin/env python3
"""Start the switchboard processes at login on this device (stdlib only, macOS and Windows). Safe to run again.

  autostart.py show                          print what would be installed, change nothing
  autostart.py install [--runner] [--bridge] [--queue] [--house] [--permission-mode MODE]
  autostart.py remove                        take every switchboard login item out again

With no component flags, `install` sets up the mail runner and the Discord bridge. This makes programs start by
themselves at every login, so run it only after the owner has agreed to that; an AI agent setting this up should
ask first.

macOS: one launchd agent per component in ~/Library/LaunchAgents (templates/autostart/*.plist), logs in
~/Library/Logs. Windows: one HKCU Run entry per component that starts templates/autostart/worker.ps1 hidden;
no administrator rights. Linux: prints a systemd user unit for you to install by hand.

--permission-mode is passed to the mail runner (default acceptEdits). Unattended runs cannot answer a permission
dialog, so anything outside that mode is refused unless you allow it in your Claude Code settings."""
import argparse, json, os, shutil, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SWITCHBOARD = REPO / "switchboard"
TEMPLATES = REPO / "templates" / "autostart"
sys.path.insert(0, str(SWITCHBOARD))
from common import dev_root  # noqa: E402

MAC = {"runner": "com.switchboard.mail-runner", "bridge": "com.switchboard.discord-bridge"}
WIN = {"runner": ("Switchboard-MailRunner", "mail"), "bridge": ("Switchboard-DiscordBridge", "discord"),
       "queue": ("Switchboard-QueueRunner", "queue"), "house": ("Switchboard-Housekeeper", "house")}
SCRIPT = {"runner": "mail-runner.py", "bridge": "discord-bridge.py", "queue": "queue-runner.py", "house": "housekeeper.py"}

def discord_ready():
    """True once this device has a bot token and the root has a Discord config: before that the bridge would only exit."""
    try:
        token = (Path.home() / ".switchboard" / "discord" / "token.txt").read_text(encoding="utf-8").strip()
        cfg = json.loads((dev_root() / "Queue" / "discord" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError): return False
    return bool(token and cfg.get("guild_id"))

def chosen(a):
    picked = [k for k in ("runner", "bridge", "queue", "house") if getattr(a, k, False)]
    if picked: return picked
    if discord_ready(): return ["runner", "bridge"]
    print("bridge: left out, Discord is not set up on this device yet. Run this again after `discord-bridge.py setup`, or pass --bridge to add it anyway.")
    return ["runner"]

ARGS = {"runner": "--poll 10 --permission-mode {mode} --mcp", "bridge": "run", "queue": "--poll 60", "house": ""}
def describe(name, mode):
    """What a login item does, in words: shown by `show` and before `install` writes anything."""
    return (f"  runs:   {sys.executable} -u {SWITCHBOARD / SCRIPT[name]} {ARGS[name].format(mode=mode)}".rstrip() + "\n"
            f"  root:   {dev_root()}\n"
            "  when:   at every login of this user; started again if it exits (after a STOP file, it waits)\n"
            "  undo:   autostart.py remove")

def mac_plist(name, mode):
    text = (TEMPLATES / f"{MAC[name]}.plist").read_text(encoding="utf-8")
    path = os.pathsep.join(dict.fromkeys([str(Path(sys.executable).parent), str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]))
    for k, v in {"PYTHON": sys.executable, "SWITCHBOARD": str(SWITCHBOARD), "ROOT": str(dev_root()), "HOME": str(Path.home()),
                 "PATH": path, "PERMISSION_MODE": mode}.items():
        text = text.replace("{{" + k + "}}", v)
    return text

def win_command(name, mode):
    worker = TEMPLATES / "worker.ps1"
    shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    extra = f" -PermissionMode {mode}" if name == "runner" else ""
    return f'"{shell}" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{worker}" -Mode {WIN[name][1]} -Root "{dev_root()}"{extra}'

def linux_unit(name, mode):
    args = f"--poll 10 --permission-mode {mode} --mcp" if name == "runner" else "run" if name == "bridge" else "--poll 60" if name == "queue" else ""
    return (f"# ~/.config/systemd/user/switchboard-{name}.service, then: systemctl --user enable --now switchboard-{name}\n"
            f"[Unit]\nDescription=Agent Switchboard {name}\n\n[Service]\nEnvironment=SWITCHBOARD_ROOT={dev_root()}\n"
            f"ExecStart={sys.executable} -u {SWITCHBOARD / SCRIPT[name]} {args}\nRestart=always\nRestartSec=30\n\n[Install]\nWantedBy=default.target\n")

def install(a, dry):
    if dry: print("Nothing is installed by `show`. These login items would be added by `install`:\n")
    for name in chosen(a):
        if sys.platform == "darwin":
            if name not in MAC: print(f"{name}: no login item on macOS (the housekeeper is Windows only; run queue-runner.py by hand)"); continue
            dest = Path.home() / "Library" / "LaunchAgents" / f"{MAC[name]}.plist"
            print(f"{name}: launchd agent {dest}\n{describe(name, a.permission_mode)}\n  log:    ~/Library/Logs/switchboard-{'mail-runner' if name == 'runner' else 'discord-bridge'}.log")
            if dry: continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["launchctl", "unload", str(dest)], capture_output=True)
            dest.write_text(mac_plist(name, a.permission_mode), encoding="utf-8")
            r = subprocess.run(["launchctl", "load", str(dest)], capture_output=True, text=True)
            print("  loaded" if r.returncode == 0 else f"  launchctl load failed: {r.stderr.strip()}")
        elif os.name == "nt":
            key, cmd = WIN[name][0], win_command(name, a.permission_mode)
            print(f"{name}: HKCU Run entry {key}\n{describe(name, a.permission_mode)}\n  entry:  {cmd}\n  log:    <root>\\Queue\\worker-{WIN[name][1]}-<computer name>.log")
            if dry: continue
            r = subprocess.run(["reg", "add", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run", "/v", key, "/t", "REG_SZ", "/d", cmd, "/f"], capture_output=True, text=True)
            print("  added (starts at next logon)" if r.returncode == 0 else f"  reg add failed: {r.stderr.strip()}")
        else:
            print(linux_unit(name, a.permission_mode))

def remove():
    if sys.platform == "darwin":
        for label in MAC.values():
            dest = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
            if dest.is_file():
                subprocess.run(["launchctl", "unload", str(dest)], capture_output=True); dest.unlink(); print("removed", dest)
    elif os.name == "nt":
        for key, _ in WIN.values():
            r = subprocess.run(["reg", "delete", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run", "/v", key, "/f"], capture_output=True, text=True)
            if r.returncode == 0: print("removed", key)
    else:
        print("Disable and delete the switchboard-*.service units you installed under ~/.config/systemd/user.")

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("show", "install"):
        s = sub.add_parser(name)
        for flag in ("runner", "bridge", "queue", "house"): s.add_argument(f"--{flag}", action="store_true")
        s.add_argument("--permission-mode", default="acceptEdits")
    sub.add_parser("remove")
    a = ap.parse_args()
    if a.cmd == "remove": return remove()
    install(a, dry=a.cmd == "show")

if __name__ == "__main__": main()
