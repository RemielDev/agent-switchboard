"""Shared helpers for Agent Switchboard (Python 3.10+, no third-party packages)."""
import json, os, platform, socket, subprocess, sys, urllib.request
from pathlib import Path

def hostname():
    return socket.gethostname().split('.')[0]

def dev_root():
    """The root: the folder that holds your repo folders and the shared Queue/. In order: the SWITCHBOARD_ROOT
    environment variable, the path written in ~/.switchboard/root, else the folder this checkout sits in
    (<root>/agent-switchboard/switchboard/common.py), so a checkout placed in a synced root needs no setup."""
    env = os.environ.get("SWITCHBOARD_ROOT")
    if env: return Path(env)
    try:
        saved = (Path.home() / ".switchboard" / "root").read_text(encoding="utf-8").strip()
        if saved: return Path(saved)
    except OSError: pass
    return Path(__file__).resolve().parents[2]

def repo_dir(name, root=None):
    """The folder of repo NAME under the root. Names match without regard to case, so a repo written as "kepler" finds
    the folder "Kepler" on a case-sensitive file system as it does on macOS and Windows."""
    root = Path(root) if root else dev_root(); p = root / str(name)
    if p.is_dir(): return p
    try: return next((d for d in root.iterdir() if d.is_dir() and d.name.lower() == str(name).lower()), p)
    except OSError: return p

def toolkit_names():
    """Folder names under the root that are never a repo: the shared Queue and this checkout itself."""
    return {"Queue", Path(__file__).resolve().parents[1].name}

def queue_root():
    q = dev_root() / "Queue"
    for d in ("todo", "running", "done", "failed", "locks"):
        (q / d).mkdir(parents=True, exist_ok=True)
    return q

def syncthing_api():
    """Return (base_url, api_key) for this user's Syncthing, or (None, None)."""
    base = os.environ.get("SYNCTHING_URL")
    key = os.environ.get("SYNCTHING_API_KEY")
    candidates = []
    if platform.system() == "Darwin":
        candidates.append(Path.home() / "Library/Application Support/Syncthing/config.xml")
        candidates.append(Path.home() / ".switchboard/syncthing-apikey")
    elif platform.system() == "Windows":
        candidates.append(Path.home() / ".switchboard/syncthing/config.xml")
        candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Syncthing/config.xml")
    else:
        candidates.append(Path.home() / ".local/state/syncthing/config.xml")
        candidates.append(Path.home() / ".config/syncthing/config.xml")
    import re
    for c in candidates:
        if not c.is_file(): continue
        txt = c.read_text(errors="ignore")
        if c.suffix == ".xml":
            m = re.search(r"<apikey>([^<]+)</apikey>", txt)
            a = re.search(r'<gui[^>]*>\s*<address>([^<]+)</address>', txt, re.S)
            if m and not key: key = m.group(1)
            if a and not base: base = "http://" + a.group(1)
        else:
            if not key: key = txt.strip()
    if not base: base = "http://127.0.0.1:8384"
    return (base, key) if key else (None, None)

def st_get(path):
    base, key = syncthing_api()
    if not key: return None
    req = urllib.request.Request(base + path, headers={"X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}

def read_frontmatter(text):
    meta, body = {}, text
    if text.startswith("---"):
        parts = text.split("\n---", 1)
        if len(parts) == 2:
            for line in parts[0].splitlines()[1:]:
                if ":" in line and not line.strip().startswith("#"):
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.split("#", 1)[0].strip()
            body = parts[1].lstrip("\n")
    return meta, body

def run(cmd, cwd=None, timeout=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
