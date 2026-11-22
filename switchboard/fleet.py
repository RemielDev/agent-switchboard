#!/usr/bin/env python3
"""Which devices are up, what they can run, and the shared progress log. Stdlib only, macOS and Windows.

  fleet.py devices [--json]         devices with a fresh heartbeat and the repos each one holds
  fleet.py beat                     write this device's heartbeat now
  fleet.py progress [--hours 24]    the progress board: finished, failed and cut-off work from every device
  fleet.py where [REPO]             devices that could take work now (and hold REPO's folder)
  fleet.py stats [--hours 24] [--requests]   run cost; --requests: per first-build route, builds and cost per request

Heartbeats live in Queue/devices/<host>.json and events in Queue/events/<host>-<yyyymm>.jsonl; both sync."""
import argparse, json, os, re, statistics, subprocess, sys, time, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import dev_root, hostname, queue_root, toolkit_names

FRESH = 150  # seconds; runners beat every 60
CAN = {"mac": "macOS: AirDrop, iMessage, FaceTime, Xcode, iOS Simulator, TestFlight, Safari, AppleScript, Apple Shortcuts", "windows": "Windows: PowerShell, Windows-only apps"}
MAC_ONLY = re.compile(r"\b(air ?drop\w*|imessage\w*|facetime|xcode|testflight|(?:ios|iphone) simulator|(?:in|open|with) safari|apple ?script|osascript|apple shortcuts|mac ?os|(?:on|use|with|from|via) (?:the |my )?mac(?:book)?)\b", re.I)
PC_ONLY = re.compile(r"\b(powershell|(?:on|use|with|from|via) (?:the |my )?(?:pc|windows))\b", re.I)

def wants_platform(text):
    m, p = bool(MAC_ONLY.search(text or "")), bool(PC_ONLY.search(text or ""))
    return "mac" if m and not p else "windows" if p and not m else None

def devices_text(me):
    lines = []
    for d in devices():
        if not d.get("available"): continue
        host = str(d.get("host", ""))
        lines.append(f"- {d.get('label')} ({host}, {CAN.get(d.get('platform'), d.get('platform'))})" + (" -- this run is here" if host.lower() == str(me).lower() else ""))
    return "\n".join(lines)

def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for _ in range(20):
        try: os.replace(tmp, path); return
        except OSError: time.sleep(0.1)

# ---- one writer per synced file ----
# Syncthing copies files and cannot merge: any file two devices write while apart becomes a .sync-conflict copy.
# So each device writes only its own copy, <stem>.<host><ext> beside the shared name, and readers merge every copy.
# The old single file (the legacy file) stays as a read-only fallback with the lowest priority; it is never deleted.
LOOP_MAX = ("last", "due_checked", "maintained", "retro")  # Queue/loop/state.json: timestamps, the newest one counts
HOST_PART = re.compile(r"^[A-Za-z0-9_-]+$")
META = ("saved", "stamps", "merged_from")  # a host copy's own bookkeeping, never data; merged_from was written by older versions and is only dropped
TOMB_KEEP = 30 * 86400  # a deletion's stamp is kept this long. shortcut: a device offline longer brings its deleted entries back
_VIEW = {}  # (str(path), host) -> (entries, stamps) this host last saved; host None: the merged view load_merged last returned

def _flat(data):
    """DATA as {entry: its JSON}. An entry is a top-level key, or "key<TAB>inner" for each key of a dict value."""
    out = {}
    for k, v in data.items():
        if k in META: continue
        if isinstance(v, dict):
            out[k] = "{}"
            for kk, vv in v.items(): out[f"{k}\t{kk}"] = json.dumps(vv, sort_keys=True)
        else: out[k] = json.dumps(v, sort_keys=True)
    return out

def _stamps(d):
    """A copy's valid stamps: {entry: (time, present)}. Each says when that entry was last changed (present) or deleted."""
    out = {}
    for e, s in (d.get("stamps") or {}).items() if isinstance(d.get("stamps"), dict) else ():
        try: out[e] = (float(s[0]), bool(s[1]))
        except (TypeError, ValueError, IndexError, KeyError): pass
    return out

def host_path(path, host=None):
    """PATH's copy for HOST (default this device): Queue/discord/state.json -> Queue/discord/state.<host>.json."""
    p = Path(path); return p.with_name(f"{p.stem}.{host or hostname()}{p.suffix}")

def host_files(path):
    """Every host's copy of PATH as [(host, Path)]; conflict copies and the legacy file are not in it."""
    import glob
    p = Path(path); out = []
    for f in p.parent.glob(glob.escape(p.stem) + ".*" + glob.escape(p.suffix)):
        mid = f.name[len(p.stem) + 1:len(f.name) - len(p.suffix)]
        if HOST_PART.match(mid) and "sync-conflict" not in mid: out.append((mid, f))
    return sorted(out)

def save_host(path, data, host=None):
    """Write DATA (a dict) to this device's own copy of PATH, atomically, with its "saved" time and "stamps":
    {entry: [time, present]}, a last-writer-wins map. Each entry (see _flat) that differs from the view this process
    last merged or saved is stamped now, an entry gone from it gets a tombstone (present false), and every other
    stamp is carried forward, so merge() can tell a deletion here from an entry written elsewhere. Returns saved."""
    now, key, h = time.time(), str(Path(path)), host or hostname()
    view = _VIEW.get((key, h)) or _VIEW.get((key, None))
    if view is None:  # saved without a load in this process: this device's own copy is what it last knew
        try: own = json.loads(host_path(path, host).read_text(encoding="utf-8"))
        except (OSError, ValueError): own = {}
        own = own if isinstance(own, dict) else {}
        view = (_flat(own), _stamps(own))
    flat = _flat(data); stamps = {e: [t, p] for e, (t, p) in view[1].items() if p or now - t < TOMB_KEEP}
    stamps.update({e: [now, True] for e, j in flat.items() if view[0].get(e) != j})
    stamps.update({e: [now, False] for e in view[0] if e not in flat})
    rec = {k: v for k, v in data.items() if k not in META}; rec["saved"] = now
    if stamps: rec["stamps"] = stamps
    _write(host_path(path, host), rec); _VIEW[(key, h)] = (flat, {e: tuple(x) for e, x in stamps.items()}); return now

def saved_at(path):
    """The saved time of one host copy, 0 when it is missing or unreadable."""
    try: return float(json.loads(Path(path).read_text(encoding="utf-8")).get("saved") or 0)
    except (OSError, ValueError, TypeError, AttributeError): return 0.0

def copies(path):
    """Every readable copy of PATH, lowest priority first: [(host, saved, data)]. The legacy file (host "") comes
    first whatever its age; host copies follow by their saved time. A host copy is a dict with a "saved" time
    (so state.v1-backup.json and the like are not taken for a device)."""
    p, out, rows = Path(path), [], []
    try: out.append(("", p.stat().st_mtime, json.loads(p.read_text(encoding="utf-8"))))
    except (OSError, ValueError): pass
    for host, f in host_files(p):
        try: d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError): continue
        if isinstance(d, dict) and isinstance(d.get("saved"), (int, float)): rows.append((host, float(d["saved"]), d))
    return out + sorted(rows, key=lambda r: r[1])

def merge(rows, maxkeys=(), stamps=None):
    """Merge [(host, saved, dict)] given lowest priority first. Keys in MAXKEYS take the largest number across every
    copy (per key for a map) and no stamp touches them. Everything else is per entry (a top-level key, or one key of a
    dict value): the copy with the newest stamp wins, and a winning tombstone drops the entry. An entry without a
    stamp (the legacy file, a copy from before stamps) counts as the oldest; among those the later copy wins and dicts
    take the union. STAMPS, when given, is filled with the merged stamps."""
    rows = [r for r in rows if isinstance(r[2], dict)]
    out, best, tombs = {}, {}, {}  # best: entry -> stamp time of the value now in out (0 without a stamp)
    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
    for _, _, d in rows:
        st = _stamps(d)
        for e, (t, present) in st.items():
            if not present and t > tombs.get(e, 0): tombs[e] = t
        at = lambda e: st[e][0] if e in st and st[e][1] else 0.0
        for k, v in d.items():
            if k in META: continue
            old = out.get(k)
            if k in maxkeys:
                if isinstance(v, dict) and isinstance(old, dict):
                    out[k] = {**old, **{kk: max(vv, old[kk]) if num(vv) and num(old.get(kk)) else vv for kk, vv in v.items()}}
                elif num(v) and num(old): out[k] = max(old, v)
                else: out[k] = dict(v) if isinstance(v, dict) else v
                continue
            if k in out and not (isinstance(v, dict) and isinstance(old, dict)) and at(k) < best.get(k, 0): continue
            best[k] = max(at(k), best.get(k, 0))
            if not isinstance(v, dict): out[k] = v; continue
            if not isinstance(old, dict): old = out[k] = {}
            for kk, vv in v.items():
                e = f"{k}\t{kk}"
                if kk not in old or at(e) >= best.get(e, 0): old[kk] = vv; best[e] = at(e)
    for e, t in sorted(tombs.items(), key=lambda x: "\t" not in x[0]):  # inner entries first, then whole keys
        k, _, kk = e.partition("\t")
        if k in maxkeys: continue
        if kk:
            if isinstance(out.get(k), dict) and kk in out[k] and t > best.get(e, 0): del out[k][kk]
        elif k in out and t > best.get(k, 0) and not (isinstance(out[k], dict) and any(best.get(f"{k}\t{x}", 0) >= t for x in out[k])): del out[k]
    if stamps is not None:
        flat = _flat(out)
        stamps.update({e: [t, False] for e, t in tombs.items() if e not in flat and e.partition("\t")[0] not in maxkeys})
        stamps.update({e: [t, True] for e, t in best.items() if t and e in flat})
    return out

def load_merged(path, default=None, maxkeys=()):
    """PATH as every device wrote it: all host copies and the legacy file merged (see merge). DEFAULT when none can be read.
    The merged view is remembered for PATH, so the next save_host stamps only what this process changed."""
    rows = [r for r in copies(path) if isinstance(r[2], dict)]; st, key = {}, str(Path(path))
    out = merge(rows, maxkeys, st) if rows else None
    for k in [k for k in _VIEW if k[0] == key]: del _VIEW[k]
    _VIEW[(key, None)] = (_flat(out or {}), {e: tuple(x) for e, x in st.items()})
    return out if rows else ({} if default is None else default)

def load_newest(path, default=None):
    """The newest whole copy of PATH (a snapshot is one pull: two pulls are never mixed); the legacy file only when no host copy exists."""
    rows = copies(path)
    if not rows: return default
    d = rows[-1][2]
    return {k: v for k, v in d.items() if k not in META} if isinstance(d, dict) else d

def read_lines(path, key=None):
    """The lines of every copy of a log (the legacy file first, then each host's), each distinct line once, in order;
    KEY (line -> sortable) orders them across copies, stably."""
    p, seen, out = Path(path), set(), []
    for f in [p] + [f for _, f in host_files(p)]:
        try: lines = f.read_text(encoding="utf-8").splitlines()
        except OSError: continue
        for l in lines:
            if l.strip() and l not in seen: seen.add(l); out.append(l)
    return sorted(out, key=key) if key else out

def json_at(line):
    """Sort key for a JSON log line: its "at" time, 0 when it has none."""
    try: return float(json.loads(line).get("at") or 0)
    except (ValueError, TypeError, AttributeError): return 0.0

CONFLICT_RE = re.compile(r"^(?P<stem>.*)\.sync-conflict-\d{8}-\d{6}-[A-Z0-9]+(?P<ext>\.[^.]*)?$")

def union_lines(a, b):
    """The lines of A and B in order: B's own lines go where they sit in B; nothing is lost or doubled."""
    import difflib
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        out += a[i1:i2]
        if op in ("insert", "replace"): out += b[j1:j2]
    return out

def resolve_conflicts(root=None, dry=False, now=None, busy=600):
    """Clear the Syncthing conflict copies under ROOT (default Queue). Returns (done, left): what was merged or
    archived, and the copies left for the owner. Never deletes: each handled copy moves under ROOT/archive/sync-conflicts.
    - a copy inside attachments/*/before (an undo snapshot) is archived as it is, and so is an empty or zeroed copy;
    - JSON objects merge by the rules of merge(), stamps included: into the legacy file when that is what conflicted, into
      this device's own copy (with a new saved time) when its own copy conflicted. Another device's copy (a stem ending
      in .<host> of a known host: Queue/devices, lease-<host>.json) is that device's file, whatever its kind, and is left. A legacy file
      changed in the last BUSY seconds is still being written by an old writer: it waits for a later pass;
    - .md and .jsonl take the union of both copies' lines, in order;
    - anything else is left for the owner."""
    import shutil
    root = Path(root or queue_root()); now = now or time.time(); arch = root / "archive" / "sync-conflicts"
    done, left = [], []
    known = {p.stem.lower() for p in (root / "devices").glob("*.json")} | {p.stem[6:].lower() for p in (root / "discord").glob("lease-*.json")} | {hostname().lower()}
    def stash(f):
        d = arch / f.relative_to(root); n = 1
        while d.exists(): d = d.with_name(f"{f.name}.{n}"); n += 1
        if not dry: d.parent.mkdir(parents=True, exist_ok=True); shutil.move(str(f), str(d))
    for f in sorted(root.rglob("*.sync-conflict-*")):
        if arch in f.parents or not f.is_file(): continue
        parts = f.relative_to(root).parts; rel = "/".join(parts); m = CONFLICT_RE.match(f.name)
        if parts[0] == "attachments" and "before" in parts:
            stash(f); done.append(f"archived {rel} (a copy inside an undo snapshot)"); continue
        try: blank = not f.read_bytes().strip(b"\0 \t\r\n")
        except OSError: blank = False
        if blank: stash(f); done.append(f"archived {rel} (empty or all zero bytes: nothing to merge)"); continue
        orig = f.with_name(m["stem"] + (m["ext"] or "")) if m else None
        if orig is None or not orig.is_file(): left.append(f"{rel} (no original file beside it)"); continue
        ext, into = (m["ext"] or "").lower(), "/".join(orig.relative_to(root).parts)
        part = orig.stem.rsplit(".", 1)[1] if "." in orig.stem else ""
        if ext in (".md", ".jsonl") and part.lower() in known - {hostname().lower()}: left.append(f"{rel} ({part}'s own file: that device merges it)"); continue
        try:
            if ext == ".json":
                a, b = json.loads(orig.read_text(encoding="utf-8")), json.loads(f.read_text(encoding="utf-8"))
                if not (isinstance(a, dict) and isinstance(b, dict)): left.append(f"{rel} (not a JSON object)"); continue
                host = part if part and (isinstance(a.get("saved"), (int, float)) or part.lower() in known) else ""
                if host and host.lower() != hostname().lower(): left.append(f"{rel} ({host}'s own file: that device merges it)"); continue
                if not host and now - orig.stat().st_mtime < busy: left.append(f"{rel} ({into} is still being written; a later pass merges it)"); continue
                rows = sorted([("", float(a.get("saved") or orig.stat().st_mtime), a), ("", float(b.get("saved") or f.stat().st_mtime), b)], key=lambda r: r[1])
                st = {}; merged = merge(rows, LOOP_MAX if orig.parent.name == "loop" else (), st)
                if host: merged["saved"] = time.time(); merged.update({"stamps": st} if st else {})  # a new saved time: peers that read the old revision read this one
                if not dry: _write(orig, merged)
            elif ext in (".md", ".jsonl"):
                a, b = orig.read_text(encoding="utf-8").splitlines(), f.read_text(encoding="utf-8").splitlines()
                if not dry:
                    tmp = orig.with_name("." + orig.name + ".tmp"); tmp.write_text("\n".join(union_lines(a, b)) + "\n", encoding="utf-8"); os.replace(tmp, orig)
            else: left.append(f"{rel} (a {ext or 'plain'} file: compare it by hand)"); continue
        except (OSError, ValueError) as e: left.append(f"{rel} (could not merge: {str(e)[:80]})"); continue
        stash(f); done.append(f"merged {rel} into {into}")
    return done, left

def repos_here():
    """Repo folders on this device: every plain folder directly under the root, except the Queue and this checkout."""
    skip = toolkit_names()
    try: return sorted(p.name for p in dev_root().iterdir() if p.is_dir() and not p.name.startswith((".", "_")) and p.name not in skip)
    except OSError: return []

def codex_exe():
    """Full path of the Codex CLI, or None. launchd's PATH lacks ~/.npm-global/bin, where npm installs it on the Mac."""
    import shutil
    for n in (("codex.exe", "codex.cmd") if os.name == "nt" else ("codex",)):  # never the extensionless npm shim on Windows
        if shutil.which(n): return shutil.which(n)
    extra = [Path(os.environ["APPDATA"]) / "npm" / "codex.cmd"] if os.name == "nt" and os.environ.get("APPDATA") else [Path.home() / ".npm-global/bin/codex"]
    return next((str(p) for p in extra if p.is_file()), None)

LIMIT_RE = re.compile(r'"used_percent"\s*:\s*([\d.]+)\s*,\s*"window_minutes"\s*:\s*(\d+)\s*,\s*"resets_at"\s*:\s*(\d+)')

def codex_quota():
    """Codex's remaining quota from its own session logs (never prompts Codex): "left" is the weekly window (10080 min,
    the "secondary" limit), "left_5h" the 5-hour one ("primary"). None when no weekly reading is found."""
    if not codex_exe(): return None
    files = sorted((Path.home() / ".codex/sessions").glob("*/*/*/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime)[-6:]
    week = five = None
    for f in reversed(files):
        try: hits = LIMIT_RE.findall(f.read_text(encoding="utf-8", errors="replace"))
        except OSError: continue
        for used, mins, reset in reversed(hits):
            if int(mins) > 300 and week is None: week = (float(used), int(reset), f.stat().st_mtime)
            if int(mins) <= 300 and five is None: five = (float(used), int(reset))
        if week and five: break
    if not week: return None
    left = lambda used, reset: 100.0 if time.time() > reset else round(100 - used, 1)
    q = {"left": left(week[0], week[1]), "resets_at": week[1], "seen": week[2]}
    if five: q.update(left_5h=left(*five), resets_5h=five[1])
    return q

def codex_usable(q, pol):
    """Codex has quota for one more run: the weekly share above the policy's floor and some of the 5-hour window left.
    A reading from an older device (no left_5h) counts on the weekly figure alone."""
    return bool(q and q["left"] >= pol["codex_min_left"] and q.get("left_5h", 100.0) >= 5)

def codex_best():
    """The newest quota reading any device has: the quota is per account, so an old local log must not win."""
    seen = [d.get("codex") for d in devices() if d.get("codex")] + [codex_quota()]
    seen = [q for q in seen if q]
    if not seen: return None
    q = dict(max(seen, key=lambda q: q.get("seen", 0)))
    if time.time() > q.get("resets_at", 0): q["left"] = 100.0
    if "left_5h" in q and time.time() > q.get("resets_5h", 0): q["left_5h"] = 100.0
    return q

def engines_policy():
    """Which engine runs each agent, in order of preference. Queue/mail/engines.json overrides the default."""
    d = {"*": ["claude", "codex"], "reviewer": ["codex", "claude"], "codex_min_left": 15}
    try: d.update(json.loads((queue_root() / "mail" / "engines.json").read_text(encoding="utf-8")))
    except (OSError, ValueError): pass
    return d

def engines_text():
    q = codex_best(); pol = engines_policy()
    rows = ["Codex " + (f"{q['left']:g}% of the week left, resets {time.strftime('%b %d %H:%M', time.localtime(q['resets_at']))}" + (f", {q['left_5h']:g}% of the 5-hour window" if "left_5h" in q else "") if q else "quota unknown, so not used")]
    return ("Engines: " + "; ".join(rows) + f". Codex is used only above {pol['codex_min_left']}% left; otherwise Claude takes its work. "
            f"Order: reviewer {' then '.join(pol['reviewer'])}, others {' then '.join(pol['*'])}.")

def beat(extra=None):
    data = {"host": hostname(), "platform": "windows" if os.name == "nt" else "mac", "at": time.time(),
            "repos": repos_here(), "root": str(dev_root()), "codex": codex_quota()}
    data.update(extra or {})
    _write(queue_root() / "devices" / f"{hostname()}.json", data)
    return data

def start_heartbeat(extra=None, every=45):
    """Beat from a background thread so a long agent run never makes this device look offline."""
    import threading
    def loop():
        while True:
            try: beat(extra() if callable(extra) else extra)
            except Exception: pass
            time.sleep(every)
    t = threading.Thread(target=loop, daemon=True); t.start(); return t

_locks = []
def single_instance(name):
    """True if this process now owns the named device-local lock; False if another process holds it."""
    p = Path.home() / ".switchboard" / "locks" / (re.sub(r"[^\w.-]", "_", name) + ".lock"); p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "a+")
    try:
        if os.name == "nt":
            import msvcrt; f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl; fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close(); return False
    _locks.append(f); return True

def resources(host, fresh=FRESH):
    """What housekeeper.py last measured on a device (memory, CPU, runs), or {} without a fresh reading."""
    try: r = json.loads((queue_root() / "resources" / f"{host}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}
    return r if time.time() - float(r.get("at", 0)) < fresh else {}

def devices(fresh=FRESH):
    out, me, now = [], hostname().lower(), time.time()
    d = queue_root() / "devices"
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try: x = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError): continue
        x["age"] = int(now - float(x.get("at", 0)))
        x["available"] = x["age"] < fresh or str(x.get("host", "")).lower() == me
        x["label"] = "PC" if x.get("platform") == "windows" else "Mac"
        x["res"] = resources(x.get("host", ""), fresh); x["pressure"] = bool(x["res"].get("pressure"))
        out.append(x)
    return out

def available(host):
    return any(d["available"] and d["host"].lower() == str(host).lower() for d in devices())

# Optional second opinion on liveness, the one shape every reader uses (runner, loop, bridge). Heartbeats travel through
# the synced Queue, so a device whose sync is behind can look offline while it is working. If you run a small relay that
# every device reports to, have it write this device-local, never synced file after each report:
# {"read_at": epoch on this device, "relay_up": s the relay has run, "peers": {"<hostname>": {"push_age": s on the
# relay clock at read_at, "beat_age": s on that host's clock or null}}}. Without the file, heartbeats alone decide.
PEERS = Path.home() / ".switchboard" / "peers.json"
PEERS_FRESH, PUSH_DOWN = 60, 120  # a reading older than this proves nothing; a host silent at the relay this long is down

def confirmed_down(host, path=None, missing=True):
    """True when the liveness relay confirms HOST is down: a reading under PEERS_FRESH s old, from a relay that has run
    PUSH_DOWN s, that has stopped hearing from it (no push for PUSH_DOWN s, a heartbeat older than FRESH, or not
    listed). A stale or broken reading confirms nothing, since then this device may be the one offline. No file at all
    returns MISSING: the runner and loop pass True (no relay is deployed here, so the heartbeat rule decides),
    the bridge passes False (no proof, no takeover)."""
    try: d = json.loads(Path(path or PEERS).read_text(encoding="utf-8")); held = time.time() - float(d.get("read_at") or 0)
    except FileNotFoundError: return missing
    except (OSError, ValueError, TypeError, AttributeError): return False
    try:
        if not -5 <= held < PEERS_FRESH or float(d.get("relay_up") or 0) + held < PUSH_DOWN: return False  # a relay that just restarted has not heard from everyone
        p = {str(k).lower(): v for k, v in (d.get("peers") or {}).items()}.get(str(host).lower())
        if not isinstance(p, dict): return True
        return float(p.get("push_age") or 0) + held >= PUSH_DOWN or float(p.get("beat_age") or 0) >= FRESH
    except (TypeError, ValueError, AttributeError): return False

def leader(hosts):
    """The host of a priority list that should act now: the first one up, but a host whose heartbeat only looks stale
    (not confirmed down by the liveness relay) keeps its place, so two devices never both decide they are first."""
    me = hostname().lower()
    for h in ([hosts] if isinstance(hosts, str) else list(hosts or [])):  # this device too needs a fresh heartbeat, as in pick_host
        if available(h) or (str(h).lower() != me and not confirmed_down(h)): return h
    return None

def pick_host(hosts, fresh=FRESH):
    """First available host of a priority list (a plain string is a one-item list)."""
    hosts = [hosts] if isinstance(hosts, str) else list(hosts or [])
    up = {d["host"].lower() for d in devices(fresh) if d["available"]}
    return next((h for h in hosts if h.lower() in up), None)

def where(repo=None):
    """Devices that can take work now. With a repo: only those that hold its folder (when any device does)."""
    out = []
    known = bool(repo) and any(repo in (d.get("repos") or []) for d in devices() if d["available"])
    for d in devices():
        if not d["available"]: continue
        item = {"host": d["host"], "label": d["label"], "ok": True, "why": ""}
        item["platform"] = d.get("platform", ""); item["can"] = CAN.get(d.get("platform", ""), "")
        if repo and known and repo not in (d.get("repos") or []): item.update(ok=False, why="this repo's folder is not on this device")
        if item["ok"] and d.get("pressure"): item.update(ok=False, why="low on memory: " + d["res"].get("why", ""))
        out.append(item)
    return out

def event(kind, title, detail="", agent="", thread="", repo="", ref="", **more):
    """Append to the progress log. kind: started, done, failed, cutoff, paused, waiting, loop. MORE adds fields (session, cwd)."""
    e = {**more, "id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6], "ts": time.time(), "host": hostname(),
         "kind": kind, "title": str(title)[:200], "detail": str(detail)[:1500], "agent": agent, "thread": thread, "repo": repo, "ref": ref}
    p = queue_root() / "events" / f"{hostname()}-{time.strftime('%Y%m', time.gmtime())}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f: f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return e

def events(since=0.0):
    out, d = [], queue_root() / "events"
    for p in sorted(d.glob("*.jsonl")) if d.is_dir() else []:
        try: lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError: continue
        for l in lines:
            try: e = json.loads(l)
            except ValueError: continue
            if e.get("ts", 0) > since: out.append(e)
    return sorted(out, key=lambda e: e["ts"])

AREA_RE = re.compile(r"(?mi)^AREA:\s*([\w.-]+)\s*$")

def area_of(msg):
    """What a piece of work writes: its repo folder, else the AREA line in its body, else ''."""
    if msg.get("repo"): return str(msg["repo"]).lower()
    hit = AREA_RE.search(str(msg.get("body") or ""))
    return hit.group(1).lower() if hit else ""

def active_work(skip="", limit=25, stale=3300):
    """Every mail thread with unfinished mail, newest first: who holds each message, where, and whether it runs now.
    Read from the mail folders alone, so it is the same on every device and with Discord down."""
    root, now, rows = queue_root() / "mail", time.time(), {}
    try:
        ag = json.loads((root / "agents.json").read_text(encoding="utf-8"))
        kinds = {k.lower(): (v or {}).get("kind") for k, v in ag.items()}
        autos = {k.lower(): bool((v or {}).get("auto")) for k, v in ag.items()}
    except Exception: kinds, autos = {}, {}
    for p in root.glob("*/inbox/*.json"):
        agent = p.parent.parent.name
        if (p.parent.parent / "done" / p.name).exists(): continue
        try: m = json.loads(p.read_text(encoding="utf-8"))
        except Exception: continue
        if not isinstance(m, dict) or not m.get("id") or m.get("control"): continue
        tid = m.get("thread") or m["id"]
        if tid == skip: continue
        if (not m.get("needs_reply") and kinds.get(agent.lower()) != "human" and not autos.get(agent.lower())
                and kinds.get(str(m.get("from") or "").lower()) != "human"): continue
        if kinds.get(agent.lower()) == "human":
            if not m.get("options"): continue
            state = "waiting for the owner's answer"
        else:
            state = "queued" + (f" for {m['device']}" if m.get("device") else "")
            for cp in [root / "claims" / f"{m['id']}.json", *root.glob(f"claims/*/{m['id']}.json")]:  # per-host folders, plus legacy flat files
                try:
                    c = json.loads(cp.read_text(encoding="utf-8"))
                    if now - float(c.get("at", 0)) < stale: state = f"running on {c.get('host')}"; break
                except Exception: pass
        r = rows.setdefault(tid, {"thread": tid, "title": "", "created": str(m.get("created") or ""), "items": []})
        r["created"] = max(r["created"], str(m.get("created") or ""))
        r["items"].append({"agent": agent, "subject": str(m.get("subject") or "")[:90], "area": area_of(m), "state": state, "id": m["id"]})
    out = sorted(rows.values(), key=lambda r: r["created"], reverse=True)[:limit]
    for r in out:
        for p in root.glob(f"*/*/{r['thread']}.json"):
            if p.parent.parent.name == "claims": continue  # a claim file named after the thread's first message has no subject
            try: r["title"] = str(json.loads(p.read_text(encoding="utf-8")).get("subject") or "")[:90]; break
            except Exception: pass
        r["title"] = r["title"] or r["items"][0]["subject"]
    return out

def work_text(rows):
    lines = []
    for r in rows:
        items = "; ".join(f"{i['agent']} {i['state']}" + (f" [area {i['area']}]" if i["area"] else "") + f": \"{i['subject']}\"" for i in r["items"][:4])
        lines.append(f"- thread {r['thread']} \"{r['title']}\": {items}")
    return "\n".join(lines)

def stat(row):
    """Append one agent run's cost and token counts to Queue/stats/<host>-<yyyymm>.jsonl."""
    row = {"ts": time.time(), "host": hostname(), **row}
    p = queue_root() / "stats" / f"{hostname()}-{time.strftime('%Y%m', time.gmtime())}.jsonl"; p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f: f.write(json.dumps(row, ensure_ascii=False) + "\n")

def job_summary(thread, since=0.0):
    """One request's work since a moment, summed from the run stats: what the ledger records when the owner reacts."""
    rows = [r for r in stats(24 * 14) if r.get("thread") == thread and float(r.get("ts", 0)) > since]
    b = [r for r in rows if r.get("agent") == "builder"]
    return {"runs": len(rows), "builds": len(b), "reviews": sum(r.get("agent") == "reviewer" for r in rows),
            "secs": int(sum(float(r.get("secs") or 0) for r in rows)), "cost": round(sum(float(r.get("cost") or 0) for r in rows), 2),
            "out_tokens": int(sum(int(r.get("output_tokens") or 0) for r in rows)),
            "build_models": [f"{r.get('model') or r.get('engine') or '?'} {r.get('effort') or ''}".strip() for r in b],
            "checks": sum(int(r.get("checks") or 0) for r in b), "checks_passed": sum(int(r.get("checks_passed") or 0) for r in b),
            "repo": next((r.get("repo") for r in reversed(rows) if r.get("repo")), "")}

MODEL_NAMES = (("fable", "Fable"), ("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"), ("astra", "Astra"), ("sol", "Sol"), ("gemini", "Gemini"), ("composer", "Composer"))

def model_label(row):
    m = str(row.get("model") or "").lower()
    return next((name for key, name in MODEL_NAMES if key in m), "") or str(row.get("engine") or "").capitalize() or "Other"

def share_text(rows, top=3):
    """Which models did the work, by cost (or run time when nothing was priced): 'Opus 62% · Sonnet 27% · Fable 11%'."""
    for field in ("cost", "secs"):
        w = {}
        for r in rows: w[model_label(r)] = w.get(model_label(r), 0.0) + float(r.get(field) or 0)
        total = sum(w.values())
        if total > 0: break
    else: return ""
    items = sorted(w.items(), key=lambda kv: -kv[1])
    if len(items) > top: items = items[:top] + [("other", sum(v for _, v in items[top:]))]
    out = [f"{k} {round(v * 100 / total)}%" for k, v in items if round(v * 100 / total) > 0]
    return " · ".join(out)

def week_rate(rows):
    """Percent of Claude's weekly limit per API-priced dollar, from runs that read the limit before and after.
    A single run moves the reading in whole percents, so the rate is pooled over the week. shortcut: the owner's own
    sessions and parallel runs move the same reading, so it errs high; per-run readings would need the API to report it."""
    d = c = 0.0
    for r in rows:
        if r.get("engine") == "claude" and r.get("week0") is not None and r.get("cost"):
            d += max(0.0, float(r["week1"]) - float(r["week0"])); c += float(r["cost"])
    return d / c if d > 0 and c > 0 else None

def week_used(rows):
    """{engine: percent of its weekly subscription limit} these runs used."""
    rate, out = week_rate(stats(168)), {}
    for r in rows:
        e = r.get("engine") or "claude"
        if e == "claude" and rate is not None and r.get("cost"): v = float(r["cost"]) * rate
        elif r.get("week0") is not None: v = max(0.0, float(r["week1"]) - float(r["week0"]))
        else: continue
        out[e] = out.get(e, 0.0) + v
    return out

def pct(v): return "<0.1%" if v < 0.1 else f"{v:.1f}%" if v < 10 else f"{v:.0f}%"

def week_now():
    """The newest weekly-limit reading of any Claude run: (percent used, when) or None."""
    r = max((r for r in stats(168) if r.get("engine") == "claude" and r.get("week1") is not None), key=lambda r: float(r.get("ts", 0)), default=None)
    return (float(r["week1"]), float(r["ts"])) if r else None

WEEK = 7 * 86400

# Plan readings: one file per writer, Queue/usage/<kind>-<host>.json, written by any process that reads the plan limits
# and by every runner run that carried rate_limit_event lines; readers take the newest. claude-w-<host>.json are scan totals.
def windows_from(out):
    """The plan windows in a stream-json run's rate_limit_event lines, or {}."""
    wins = {}
    for line in str(out or "").splitlines():
        if '"rate_limit_event"' not in line: continue
        try: info = json.loads(line).get("rate_limit_info") or {}
        except (ValueError, AttributeError): continue
        for k, v in (info.get("unifiedWindows") or {}).items(): wins[k] = {"utilization": float(v.get("utilization") or 0), "resetsAt": int(v.get("resetsAt") or 0)}
        if info.get("rateLimitType") and info["rateLimitType"] not in wins and info.get("resetsAt"):
            wins[info["rateLimitType"]] = {"utilization": float(info.get("utilization") or 0), "resetsAt": int(info["resetsAt"])}
    return wins

def save_usage(kind, r, host=None):
    """usage/<kind>-<host>.json. The bridge's primary (first of Queue/discord/config.json hosts) also mirrors the newest
    reading to the legacy <kind>.json, for code not yet reading per-host files."""
    host = host or hostname(); d = queue_root() / "usage"; _write(d / f"{kind}-{host}.json", r)
    try: hosts = json.loads((queue_root() / "discord" / "config.json").read_text(encoding="utf-8")).get("hosts") or []
    except (OSError, ValueError, AttributeError): hosts = []
    if hosts and str(hosts[0]).lower() == host.lower(): _write(d / f"{kind}.json", usage_newest(kind))

def usage_newest(kind):
    """The freshest reading of claude (by "at") or codex (by "seen") from any device, or {}."""
    key, rows = "at" if kind == "claude" else "seen", []
    for p in (queue_root() / "usage").glob(f"{kind}*.json"):
        if p.name == f"{kind}.json" or (p.name.startswith(f"{kind}-") and not p.name.startswith(f"{kind}-w-") and p.name != f"{kind}-blocked.json"):
            try: x = json.loads(p.read_text(encoding="utf-8")); rows.append((float(x.get(key) or 0), x))
            except (OSError, ValueError, TypeError, AttributeError): continue
    return max(rows, key=lambda r: r[0], default=(0, {}))[1]

def week_pace(now=None):
    """Claude's weekly limit from the newest Queue/usage/claude*.json reading: used, share of the week gone, reset and projected percent, or None."""
    now = time.time() if now is None else now
    try:
        d = usage_newest("claude")
        w = d["windows"]["seven_day"]; used, resets, at = float(w["utilization"]) * 100, float(w["resetsAt"]), float(d["at"])
    except (OSError, ValueError, KeyError, TypeError): return None
    if now - at > 6 * 3600 or resets <= now: return None
    elapsed = min(100.0, max(0.0, (now - (resets - WEEK)) / WEEK * 100))
    return {"used": used, "elapsed": elapsed, "resets": resets, "projected": used * 100 / elapsed if elapsed >= 5 else None}

def pace_text(p, discord=True):
    if p is None: return ""
    when = f"<t:{int(p['resets'])}:R>" if discord else time.strftime("%a %H:%M", time.gmtime(p["resets"])) + " UTC"
    out = f"Claude week {p['used']:.0f}% used · {p['elapsed']:.0f}% of the week gone · resets {when}"
    if p["projected"] is not None and p["projected"] >= 100: out += " · **on pace to run out**" if discord else " · on pace to run out"
    return out

def receipt(thread, since=0.0, hours=72.0):
    """One request's line since a moment: '1.2% of Claude's week · 5 runs · 9 min · Opus 71% · Sonnet 29%'. No money."""
    rows = [r for r in stats(hours) if r.get("thread") == thread and float(r.get("ts", 0)) > since]
    if not rows: return ""
    n, mins = len(rows), max(1, round(sum(float(r.get("secs") or 0) for r in rows) / 60))
    used = " · ".join(f"{pct(v)} of {e.capitalize()}'s week" for e, v in sorted(week_used(rows).items()))
    sh = share_text(rows)
    parts = [x for x in (used, f"{n} run{'s' if n != 1 else ''}", f"{mins} min", sh) if x]
    cost, per = sum(float(r.get("cost") or 0) for r in rows), {}
    for r in stats(168):
        if r.get("thread"): per[r["thread"]] = per.get(r["thread"], 0.0) + float(r.get("cost") or 0)
    costs = [c for c in per.values() if c > 0]
    if len(costs) >= 10:
        med = statistics.median(costs)
        if med > 0 and cost >= 3 * med and cost >= 3.0: parts.append(f"{round(cost / med)}x a usual request")
    return " · ".join(parts)

def ledger(row):
    """Append one job outcome (the owner's reaction and what the job took) to Queue/ledger/<host>-<yyyymm>.jsonl."""
    row = {"ts": time.time(), "host": hostname(), **row}
    p = queue_root() / "ledger" / f"{hostname()}-{time.strftime('%Y%m', time.gmtime())}.jsonl"; p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f: f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row

def ledger_rows(days=7.0):
    rows, d = [], queue_root() / "ledger"
    for p in sorted(d.glob("*.jsonl")) if d.is_dir() else []:
        for l in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try: r = json.loads(l)
            except ValueError: continue
            if r.get("ts", 0) > time.time() - days * 86400: rows.append(r)
    return sorted(rows, key=lambda r: r.get("ts", 0))

def stats(hours=24.0):
    rows, d = [], queue_root() / "stats"
    for p in sorted(d.glob("*.jsonl")) if d.is_dir() else []:
        for l in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try: r = json.loads(l)
            except ValueError: continue
            if r.get("ts", 0) > time.time() - hours * 3600: rows.append(r)
    return rows

def stats_text(hours=24.0):
    rows = stats(hours)
    if not rows: return f"No agent runs in the last {hours:g} hours."
    out = [f"{len(rows)} agent runs in the last {hours:g} h, ${sum(r.get('cost') or 0 for r in rows):.2f} at API prices, {sum(r.get('secs') or 0 for r in rows) // 60} min of run time."]
    pt = pace_text(week_pace(), discord=False)
    if pt: out.append(pt)
    sh = share_text(rows, top=5)
    if sh: out.append("By model: " + sh)
    cr = sum(int(r.get("cache_read_input_tokens") or 0) for r in rows)
    den = cr + sum(int(r.get("cache_creation_input_tokens") or 0) + int(r.get("input_tokens") or 0) for r in rows)
    if den: out.append(f"Cache reads: {round(cr * 100 / den)}% of input tokens")
    th = {}
    for r in sorted(rows, key=lambda r: float(r.get("ts", 0))): th.setdefault(r.get("thread") or "other", []).append(r)
    top = sorted(th.items(), key=lambda kv: -sum(float(r.get("cost") or 0) for r in kv[1]))[:3]
    out.append("Costliest requests: " + "; ".join(f"{('other' if k == 'other' else str(rs[0].get('subject') or k))[:40]} ${sum(float(r.get('cost') or 0) for r in rs):.2f}" for k, rs in top))
    by = {}
    for r in rows: by.setdefault((r.get("agent") or "?", r.get("host") or "?"), []).append(r)
    for (agent, host), rs in sorted(by.items()):
        c = [r.get("cost") or 0 for r in rs]; ctx = [(r.get("cache_creation_input_tokens") or 0) + (r.get("cache_read_input_tokens") or 0) + (r.get("input_tokens") or 0) for r in rs]
        out.append(f"- {agent} on {host}: {len(rs)} runs, ${sum(c):.2f} total, ${sum(c) / len(c):.2f} each, avg {sum(r.get('secs') or 0 for r in rs) // len(rs)} s, avg context {sum(ctx) // len(ctx) // 1000}k tokens, avg output {sum(r.get('output_tokens') or 0 for r in rs) // len(rs)} tokens")
    return "\n".join(out)

def requests_view(hours=168.0):
    """Cost per completed request, by the route of its first build. A request is a thread's runs
    between two of the owner's messages (the stats rows' "req"); rows from before that field existed are left out.
    {route key: {requests, builds_per_request, two_or_more, reviews_per_request, runs_per_request, cost_per_request, setup_share}}"""
    reqs = {}
    for r in stats(hours):
        if r.get("req") is None or not r.get("thread"): continue
        reqs.setdefault((r["thread"], float(r["req"])), []).append(r)
    by = {}
    for rows in reqs.values():
        builds = sorted((r for r in rows if r.get("agent") == "builder"), key=lambda r: float(r.get("ts", 0)))
        b = by.setdefault((builds[0].get("route") or "?") if builds else "(no build)", {"requests": 0, "builds": 0, "two": 0, "reviews": 0, "runs": 0, "cost": 0.0, "checks": 0, "setup": 0})
        b["requests"] += 1; b["builds"] += len(builds); b["two"] += len(builds) >= 2; b["runs"] += len(rows)
        b["reviews"] += sum(r.get("agent") == "reviewer" for r in rows); b["cost"] += sum(float(r.get("cost") or 0) for r in rows)
        b["checks"] += sum(int(r.get("checks") or 0) for r in builds); b["setup"] += sum(int(r.get("checks_setup") or 0) for r in builds)
    return {k: {"requests": v["requests"], "builds_per_request": round(v["builds"] / v["requests"], 2), "two_or_more": round(v["two"] / v["requests"], 2),
                "reviews_per_request": round(v["reviews"] / v["requests"], 2), "runs_per_request": round(v["runs"] / v["requests"], 1),
                "cost_per_request": round(v["cost"] / v["requests"], 2), "setup_share": round(v["setup"] / v["checks"], 2) if v["checks"] else None}
            for k, v in sorted(by.items(), key=lambda kv: -kv[1]["requests"])}

def requests_text(hours=168.0):
    v = requests_view(hours)
    if not v: return f"No requests with a route key in the last {hours:g} hours."
    rows = [f"Requests in the last {hours:g} h, by the route of the first build:", "| Route | Requests | Builds each | Two or more builds | Reviews each | Runs each | Cost each | SETUP share of checks |", "|---|---|---|---|---|---|---|---|"]
    for k, x in v.items():
        rows.append(f"| {k} | {x['requests']} | {x['builds_per_request']:g} | {x['two_or_more'] * 100:.0f}% | {x['reviews_per_request']:g} | {x['runs_per_request']:g} | ${x['cost_per_request']:.2f} | " + ("-" if x["setup_share"] is None else f"{x['setup_share'] * 100:.0f}%") + " |")
    return "\n".join(rows)

WORD = {"done": "DONE", "failed": "FAILED", "cutoff": "CUT OFF", "waiting": "WAITING", "started": "STARTED", "loop": "LOOP"}
def line(e):
    t = time.strftime("%m-%d %H:%M", time.localtime(e["ts"]))
    return f"{t}  {WORD.get(e['kind'], e['kind'].upper()):8} {e.get('agent') or '-':12} {e['host']:20} {e['title']}"

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("devices"); s.add_argument("--json", action="store_true")
    sub.add_parser("beat")
    s = sub.add_parser("progress"); s.add_argument("--hours", type=float, default=24); s.add_argument("--all", action="store_true")
    s = sub.add_parser("where"); s.add_argument("repo", nargs="?")
    s = sub.add_parser("stats"); s.add_argument("--hours", type=float, default=None); s.add_argument("--requests", action="store_true")
    a = ap.parse_args()
    if a.cmd == "beat": print(json.dumps(beat()))
    elif a.cmd == "devices":
        ds = devices()
        if a.json: print(json.dumps(ds))
        else:
            for d in ds: print(f"{d['label']:4} {d['host']:22} {'up' if d['available'] else 'OFFLINE':8} seen {d['age']}s ago  repos: {', '.join(d.get('repos') or []) or 'none'}" + (f"  mem: {d['res']['free_gb']}/{d['res']['total_gb']} GB free, commit {d['res']['commit_pct']}%, cpu {d['res']['cpu_pct']}%, runs {d['res']['agent_runs']}" + ("  LOW MEMORY" if d["pressure"] else "") if d.get("res") else ""))
    elif a.cmd == "where": print(json.dumps(where(a.repo), indent=1))
    elif a.cmd == "stats": print(requests_text(a.hours or 168) if a.requests else stats_text(a.hours or 24))
    else:
        for e in events(time.time() - a.hours * 3600):
            if a.all or e["kind"] != "started": print(line(e))

if __name__ == "__main__": main()
