#!/usr/bin/env python3
"""Per-repo ownership lock shared through the synced Queue/locks folder.

  repo-handoff.py claim <Repo> [--note TEXT] [--force]
  repo-handoff.py release <Repo> [--note TEXT]
  repo-handoff.py status [<Repo>]

Claim before editing a repo on this device; release when the work is finished and saved. The claim is refused while
another device holds the lock or, when Syncthing runs here, while it reports the shared folder is still syncing,
unless --force. Without Syncthing (no API key found) the sync check is skipped.

Several agent runs share one device, so the lock also names the run that holds it. The check and the write happen
under a device-local file lock (never synced), so two runs here cannot both be told "claimed". A lock from something
with no run id (a queue card, an interactive session, older code) is held against agent runs until its own expiry: its
"until" field, else LOCK_HOURS after "since". A run's lock is free once that run's claim file is gone or past its deadline.
"""
import argparse, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import hostname, queue_root, st_get

def now(): return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def lock_path(repo): return queue_root() / "locks" / (repo.replace("/", "_") + ".json")

def read_lock(repo):
    p = lock_path(repo)
    try: return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError: return None

LOCK_HOURS = 12      # how long a lock with no run id holds against agent runs on its own device
RUN_STALE = 25 * 60  # a run claim file that states no deadline: the runner's default timeout plus 5 minutes
GUARD_WAIT = 30      # seconds to wait for another claim or release of the same repo on this device

def this_run(): return os.environ.get("AGENT_MAIL_PARENT", "")

def claims_dir(): return Path(os.environ.get("AGENT_MAIL_CLAIMS") or Path.home() / ".switchboard" / "mail-runner-claims")

def epoch(stamp):
    try: return datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError: return None

def run_live(run):
    """True while that mail-runner run's claim file exists and is not past the deadline its holder wrote in it."""
    p = claims_dir() / f"{run}.claim"
    try:
        try: until = float(p.read_text(encoding="utf-8").split()[2])
        except (IndexError, ValueError): until = p.stat().st_mtime + RUN_STALE
    except OSError: return False
    return time.time() <= until

def other_run(cur):
    """Who else on this device holds this lock right now, or ''. The lock names a device, and one device runs several
    agents at once, so the device alone is not one writer. A lock with no run id cannot be told apart from another
    caller with no run id (two terminals), so it is held only against agent runs, until it expires."""
    if not cur: return ""
    run = cur.get("run", "") or ""
    if run == this_run(): return ""
    if run: return run if run_live(run) else ""
    until = epoch(cur.get("until"))
    if until is None:
        since = epoch(cur.get("since"))
        until = since + LOCK_HOURS * 3600 if since is not None else time.time() + 1  # unreadable dates: treat as held
    return "a session or queue card with no run id" if time.time() <= until else ""

class Guard:
    """One claim or release of a repo at a time on this device. An OS file lock in a device-local folder: it is
    never synced, and the OS drops it if the process dies, so nothing is left behind to go stale."""
    def __init__(self, repo):
        d = claims_dir().parent / "locks"; d.mkdir(parents=True, exist_ok=True)
        self.path = d / ("handoff-" + "".join(c if c.isalnum() or c in ".-_" else "_" for c in repo.lower()) + ".lock")
    def __enter__(self):
        self.f = open(self.path, "a+"); end = time.time() + GUARD_WAIT
        while True:
            try:
                if os.name == "nt":
                    import msvcrt; self.f.seek(0); msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl; fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() > end: self.f.close(); raise TimeoutError(str(self.path))
                time.sleep(0.05)
    def __exit__(self, *a):
        try:
            if os.name == "nt":
                import msvcrt; self.f.seek(0); msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError: pass
        self.f.close(); return False

def write_lock(repo, data):
    """Whole file or nothing: written under a per-process temp name beside the lock, then renamed over it."""
    p = lock_path(repo); t = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        t.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8"); os.replace(t, p)
    finally:
        try: t.unlink()
        except OSError: pass

def sync_state():
    s = st_get("/rest/db/status?folder=switchboard-workspace")
    if not s: return "unknown (no Syncthing API)"
    if s.get("error"): return "unknown (" + s["error"] + ")"
    need = s.get("needFiles", 0) + s.get("needDeletes", 0)
    return s.get("state", "?") + (" with %d pending items" % need if need else "")

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["claim", "release", "status"])
    ap.add_argument("repo", nargs="?")
    ap.add_argument("--note", default="")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    me = hostname()
    if a.action == "status":
        locks = sorted(queue_root().joinpath("locks").glob("*.json"))
        if a.repo: locks = [l for l in locks if l.stem == a.repo]
        for l in locks:
            d = json.loads(l.read_text()); print(f"{l.stem}: {d['owner']} since {d['since']} {d.get('note','')}")
        if not locks: print("no locks held")
        print("syncthing:", sync_state()); return 0
    if not a.repo: ap.error("repo is required")
    try:
        with Guard(a.repo): return change(a, me)
    except TimeoutError:
        print(f"REFUSED: another claim or release of {a.repo} is in progress on this device; try again"); return 7

def change(a, me):
    """Claim or release, with the device-local guard held from the check to the write."""
    cur = read_lock(a.repo)
    if a.action == "claim":
        if cur and cur["owner"] != me and not a.force:
            print(f"REFUSED: {a.repo} is held by {cur['owner']} since {cur['since']}. {cur.get('note','')}"); return 2
        if cur and cur["owner"] == me and other_run(cur) and not a.force:
            print(f"REFUSED: {a.repo} is held by another run on this device ({other_run(cur)}). Wait for it to finish."); return 6
        state = sync_state()
        if state == "unknown (no Syncthing API)": pass  # Syncthing is optional: nothing to wait for
        elif state.startswith("unknown"):
            print(f"WARNING: Syncthing state is {state}; claiming anyway. Verify the other device is not editing this repo.")
        elif not state.startswith("idle") and not a.force:
            print(f"REFUSED: Syncthing is {state}; wait for idle or use --force"); return 3
        until = datetime.fromtimestamp(time.time() + LOCK_HOURS * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_lock(a.repo, {"repo": a.repo, "owner": me, "since": now(), "until": until, "note": a.note, "run": this_run()})
        print(f"claimed {a.repo} on {me}"); return 0
    if cur is None: print(f"{a.repo} is not locked"); return 0
    if cur["owner"] != me and not a.force:
        print(f"REFUSED: {a.repo} is held by {cur['owner']}; release it there or use --force"); return 2
    if other_run(cur) and not a.force:
        print(f"REFUSED: {a.repo} is held by another run on this device ({other_run(cur)})."); return 6
    lock_path(a.repo).unlink()
    with (queue_root() / "locks" / "history.log").open("a") as f: f.write(f"{now()} {me} released {a.repo} {a.note}\n")
    print(f"released {a.repo}"); return 0

if __name__ == "__main__": sys.exit(main())
