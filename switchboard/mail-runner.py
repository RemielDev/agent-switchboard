#!/usr/bin/env python3
"""Answer agent mail with headless Claude Code, several messages at once.

For every registry agent with auto true, kind claude and host equal to this host, takes inbox messages oldest
first and runs `claude -p <prompt> --permission-mode <mode> --output-format stream-json --verbose` in the message's repo folder
(else the agent's cwd, else the dev root). The JSON `result` text is mailed back to the sender, which acks the
message. A failure or timeout acks it with "FAILED: ..." and tells agent `owner` (if registered).

Guards: a file named STOP in Queue/ pauses everything; a per-message timeout; hourly and daily run caps kept in
a device-local state file; an optional hop limit (--max-hops, default 0 = none; a message at the limit is acked "hop limit", never run);
agents of any other kind are never run; a plain answer (reply_to set, needs_reply false) is run but not answered
again, so two automatic agents cannot ping-pong; the owner's answers are always replied to, since they wait in Discord (--always-reply restores a reply to every message).

Concurrency: each runner process takes up to --max-concurrent messages at once (else "max_concurrent" in
Queue/mail/runner.json, read every pass, else 4; never more than MAX_CONCURRENT, and 0 means that ceiling). A message
the owner wrote themselves may also use one of HUMAN_EXTRA slots shared by every runner process on the device, and no device
holds more than DEVICE_MAX runs. Claim files on this device keep one run per message, one per mail thread and one per
repo across all runner processes here. --once and --dry-run stay one at a time.

Claude's usage limit: a run that ends on it writes Queue/usage/claude-blocked.json (until the reset it names). While it
holds, Codex takes any agent's run when it has quota; otherwise the message stays in the inbox ("paused" event, not
failed) and starts again by itself once the limit resets. Open requests in Queue/open/<host>/ resume a run's session in
Remote Control or open a file (served by the process that runs the loop). Inboxes are looked at every 3 s.

Lean runs: --setting-sources project,local and one --settings built from the user file (agent_settings), so no
memory-loader or session hooks; the prompt lists the memory notes by path, carries a thread brief (Queue/threads/<thread>.md,
kept by the orchestrator) instead of the history, and an orchestrator run continues the thread's last session on this
host when it can (--resume, Queue/threads/<thread>.json).

  mail-runner.py [--poll 20] [--once] [--dry-run] [--timeout-minutes 20] [--max-per-hour 0] [--max-per-day 0]
                 [--max-hops 0] [--max-concurrent N] [--permission-mode acceptEdits] [--always-reply] [--mcp]
                 [--prompt-via auto|arg|stdin] [--state-file PATH]
  mail-runner.py requeue <agent> <mail id>     put an acked (e.g. FAILED) message back in the inbox
"""
import argparse, importlib.util, json, os, shutil, signal, subprocess, sys, tempfile, threading, time, re, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import hostname, dev_root, queue_root
import fleet, screencap

HERE = Path(__file__).resolve().parent
NOTIFY = "owner"
READERS = {"orchestrator", "reviewer"}  # agents that delegate or only read; every other agent writes files and takes an area
DEFAULT_LOCKS = "all"  # the live setting is Queue/mail/runner.json
HUMAN_EXTRA = 6  # extra slots only a person's message may use, so the owner never waits behind agent-to-agent mail; per device, not per process
DEFAULT_MAX_CONCURRENT = 4  # when Queue/mail/runner.json sets none, or sets something unusable
MAX_CONCURRENT = 16  # ceiling for one runner process, whatever the flag or runner.json says; 0 there = this ceiling
DEVICE_MAX = 24  # ceiling for one device: every runner process and the human slots together
NOT_A_PERSON = {"sessions"}  # kind human in the registry, but written by programs (session hooks), not typed by the owner

class Cancelled(BaseException):
    """The runner is shutting down: the run was stopped and its message stays in the inbox."""

def load_mail():
    spec = importlib.util.spec_from_file_location("agent_mail", HERE / "agent-mail.py")
    mod = importlib.util.module_from_spec(spec); sys.modules["agent_mail"] = mod
    spec.loader.exec_module(mod)
    return mod

_loop = None
def load_loop():
    global _loop
    if _loop is None:
        spec = importlib.util.spec_from_file_location("improve_loop", HERE / "improve-loop.py")
        _loop = importlib.util.module_from_spec(spec); spec.loader.exec_module(_loop)
    return _loop

def now(): return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def say(msg): print(f"{now()} {msg}", flush=True)

def find_claude():
    """Full path of the Claude Code executable, or None. On Windows this may be claude.cmd or claude.exe."""
    exe = shutil.which("claude")
    if os.name == "nt" and not (exe or "").lower().endswith((".exe", ".cmd", ".bat", ".com")):
        exe = shutil.which("claude.exe") or shutil.which("claude.cmd")  # never the extensionless npm shell shim
    return exe


# ---- device-local state: run timestamps for the caps, and in-flight claims ----

def load_runs(path):
    try:
        runs = json.loads(Path(path).read_text(encoding="utf-8")).get("runs", [])
        return [float(t) for t in runs if isinstance(t, (int, float))]
    except (OSError, ValueError, AttributeError):
        return []

def counts(runs, t=None):
    t = time.time() if t is None else t
    return sum(1 for r in runs if t - r < 3600), sum(1 for r in runs if t - r < 86400)

def record_run(mail, path):
    t = time.time()
    runs = [r for r in load_runs(path) if t - r < 86400] + [t]
    mail.write_json_atomic(path, {"host": hostname(), "updated": now(), "runs": runs})

def claim_until(p, stale_seconds):
    """When a claim file expires: the holder wrote its own deadline, since runner processes have different timeouts."""
    try: return float(p.read_text(encoding="utf-8").split()[2])
    except (IndexError, ValueError): return p.stat().st_mtime + stale_seconds

def claim(claims_dir, name, stale_seconds):
    """Take the device-local claim on one message, thread or repo so two runs here never share it. None if held."""
    claims_dir.mkdir(parents=True, exist_ok=True)
    p = claims_dir / (re.sub(r"[^\w.-]", "_", name) + ".claim")
    try:
        if time.time() > claim_until(p, stale_seconds): p.unlink()
    except OSError:
        pass
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    os.write(fd, f"{os.getpid()} {now()} {time.time() + stale_seconds:.0f}\n".encode("utf-8")); os.close(fd)
    return p

def live_claims(claims_dir, stale_seconds, prefix=None):
    """Claim files on this device that have not expired. prefix None = message claims only: the runs in flight here."""
    out = []
    for p in Path(claims_dir).glob("*.claim"):
        own = p.stem.startswith(("thread-", "repo-", "area-", "human-slot-"))
        if (own if prefix is None else not p.stem.startswith(prefix)): continue
        try:
            if time.time() <= claim_until(p, stale_seconds): out.append(p)
        except OSError: pass
    return out

def clean_limit(n):
    """A usable max_concurrent: a whole number from 1 to MAX_CONCURRENT, 0 for "the ceiling". Anything else (a bool,
    text, a fraction, a negative, NaN, Infinity or 1e999 from JSON) gives the default. Never raises."""
    try:
        if isinstance(n, bool) or n is None: return DEFAULT_MAX_CONCURRENT
        if isinstance(n, str): n = int(n.strip())
        if isinstance(n, float):
            if n != n or n in (float("inf"), float("-inf")) or n != int(n): return DEFAULT_MAX_CONCURRENT
            n = int(n)
        if not isinstance(n, int) or n < 0: return DEFAULT_MAX_CONCURRENT
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_MAX_CONCURRENT
    return MAX_CONCURRENT if n == 0 else min(n, MAX_CONCURRENT)

def is_person(msg, agents):
    """True for mail the owner wrote themselves. Session-hook mail and the improvement loop's own cycle mail are sent under a
    human-kind name by programs, so they queue like any agent mail."""
    sender = str(msg.get("from") or "").lower()
    if sender in NOT_A_PERSON or str(msg.get("subject") or "").startswith("[loop]"): return False
    return (agents.get(sender) or {}).get("kind") == "human"


# ---- running Claude ----

def kill_tree(p):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(p.pid, signal.SIGTERM)
            try: p.wait(5)
            except subprocess.TimeoutExpired: os.killpg(p.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try: p.wait(10)
    except Exception: pass

def wait_run(p, stall_s, cancel=None, watch=()):
    """Exit code, or None when the run stalls: its output files (WATCH) did not grow for STALL_S seconds. Work is
    never stopped on a time limit, so a run that keeps working is never cut; without WATCH, STALL_S is a plain limit.
    Polls so a shutdown can reach a run on a worker thread."""
    size, end = -1, time.monotonic() + stall_s
    while True:
        if cancel is not None and cancel.is_set(): raise Cancelled()
        try: return p.wait(timeout=max(0.05, min(1.0, end - time.monotonic())))
        except subprocess.TimeoutExpired:
            now_size = 0
            for f in watch:
                try: now_size += Path(f).stat().st_size
                except OSError: pass
            if watch and now_size != size: size, end = now_size, time.monotonic() + stall_s
            elif time.monotonic() >= end: return None

def use_stdin(exe, prompt, choice):
    """A .cmd shim goes through cmd.exe, which cuts an argument at a newline; long prompts overflow argv."""
    if choice != "auto": return choice == "stdin"
    if os.name == "nt": return exe.lower().endswith((".cmd", ".bat")) or len(prompt) > 30000
    return len(prompt.encode("utf-8")) > 200000

TURN_RUNS = 4  # build and review runs one request may use between two of the owner's messages (Queue/mail/runner.json "turn_runs"; 0 = no limit)

def turn_limit():
    try: return int(json.loads((queue_root() / "mail" / "runner.json").read_text(encoding="utf-8")).get("turn_runs", TURN_RUNS))
    except (OSError, ValueError, TypeError, AttributeError): return TURN_RUNS

def turn_spend(mail, thread, agents):
    """Build and review runs in this thread since the owner last wrote in it: (runs, minutes). Read from the stats files."""
    since = 0.0
    try:
        for m in mail.thread(thread):
            if is_person(m, agents):
                try: since = max(since, datetime.strptime(str(m.get("created"))[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
                except ValueError: pass
    except Exception: pass
    rows = [r for r in fleet.stats(72) if r.get("thread") == thread and r.get("agent") != "orchestrator" and float(r.get("ts", 0)) > since]
    return len(rows), sum(float(r.get("secs") or 0) for r in rows) / 60

def side_chat(thread, n=6, hours=24):
    """The owner's questions in this thread that the Gemini triage answered itself (Queue/discord/sidechat/<thread>.jsonl)."""
    try: rows = [json.loads(l) for l in (queue_root() / "discord" / "sidechat" / f"{thread}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, ValueError): return ""
    rows = [r for r in rows if time.time() - float(r.get("at") or 0) < hours * 3600][-n:]
    return "\n".join(f"- the owner: {str(r.get('q'))[:300]}\n  Gemini: {str(r.get('a'))[:400]}" for r in rows)

def build_cmd(exe, prompt, mode, via_stdin, extra=()):
    return [exe, "-p"] + ([] if via_stdin else [prompt]) + ["--permission-mode", mode, "--output-format", "stream-json", "--verbose"] + list(extra)  # the stream carries the weekly limit readings

def run_env(parent, claims, **more):
    """A run's environment. The message id goes here, per run, never in os.environ, which all worker threads share."""
    env = dict(os.environ, AGENT_MAIL_PARENT=parent or "", AGENT_MAIL_CLAIMS=str(claims or ""), **more)
    try: model = str(json.loads((queue_root() / "mail" / "runner.json").read_text(encoding="utf-8")).get("model") or "")
    except (OSError, ValueError, AttributeError): model = ""
    if model: env["ANTHROPIC_MODEL"] = model  # agent runs use the model the owner chose for them, not whatever the app's default has become
    return env

# Agents that only route mail get a lean launch. Measured on orchestrator runs: the skill listing was about 10.5k of
# the 26k tokens written fresh every run, the unused tool schemas (Agent, Workflow, ScheduleWakeup, Skill and others;
# none called in 90 runs) about 9k re-read every turn, and deferred mail tools cost one ToolSearch turn a run.
LEAN_AGENTS = {"orchestrator"}
LEAN_TOOLS = "Bash,PowerShell,Read,Edit,Write,Glob,Grep"

# Builder and reviewer: tools actually called in 76 runs over 48 h were these plus Skill in repo folders; Agent only by
# the reviewer. Everything else was loaded and never used.
WORKER_TOOLS = LEAN_TOOLS + ",WebFetch,WebSearch,TaskStop,Monitor"
LEAN_WORKERS = {"builder": WORKER_TOOLS, "reviewer": WORKER_TOOLS + ",Agent"}

def lean_launch(agent, in_repo=False):
    """(extra flags, extra environment) for a lean run; empty for every other agent, whose launch is unchanged.
    Builder and reviewer get their own tool set; in a repo folder they also keep Skill (and the skill listing)."""
    if agent in LEAN_AGENTS:
        return ["--disable-slash-commands", "--tools", LEAN_TOOLS], {"ENABLE_TOOL_SEARCH": "false"}
    if agent in LEAN_WORKERS:
        tools = LEAN_WORKERS[agent] + (",Skill" if in_repo else "")
        return ([] if in_repo else ["--disable-slash-commands"]) + ["--tools", tools], {"ENABLE_TOOL_SEARCH": "false"}
    return [], {}

def pick_model(agent, relay):
    """Model for one run, from Queue/mail/runner.json: "models" may name one per agent, and "<agent>:relay" one for turns
    that only handle another agent's mail (the owner's own messages keep the agent's main model). Else "model"."""
    try: cfg = json.loads((queue_root() / "mail" / "runner.json").read_text(encoding="utf-8"))
    except (OSError, ValueError): return "", ""
    m = cfg.get("models") or {}
    return str((m.get(f"{agent}:relay") if relay else None) or m.get(agent) or cfg.get("model") or ""), str(cfg.get("subagent_model") or "")

ROUTE = {}  # message id -> {"model", "effort", "why"} from route.py, set by the runner just before the run
_route = None
def load_route():
    global _route
    if _route is None:
        spec = importlib.util.spec_from_file_location("route", HERE / "route.py")
        _route = importlib.util.module_from_spec(spec); spec.loader.exec_module(_route)
    return _route

def last_word(mail, thread, agents):
    """(text, time) of the last thing the owner themselves wrote in a thread; ("", 0.0) when they wrote nothing."""
    said, since = "", 0.0
    try:
        for m in mail.thread(thread):
            if is_person(m, agents):
                said = f"{m.get('subject', '')}\n{m.get('body', '')}"
                try: since = max(since, datetime.strptime(str(m.get("created"))[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
                except ValueError: pass
    except Exception: pass
    return said, since

SNAP_EXT = (".lua", ".json", ".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".toml", ".ps1", ".sh", ".css", ".html", ".sql", ".yaml", ".yml")
SNAP_SKIP = {"node_modules", "Queue", "__pycache__", "dist", "build", "Pods", "DerivedData"}

def snapshot(cwd, max_files=25000, max_bytes=80_000_000):
    """The source files a build may touch, before it runs: {relative path: (mtime, size, text or None)}."""
    out, kept = {}, 0
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in SNAP_SKIP and not d.startswith(".")]
        for f in files:
            if not f.endswith(SNAP_EXT) or f.endswith(".bak") or ".sync-conflict-" in f or f.startswith((".syncthing.", "~syncthing~")): continue
            p = Path(root) / f
            try: st = p.stat()
            except OSError: continue
            text = None
            if st.st_size <= 200_000 and kept + st.st_size <= max_bytes:
                try: text = p.read_text(encoding="utf-8", errors="replace"); kept += st.st_size
                except OSError: pass
            out[str(p.relative_to(cwd))] = (st.st_mtime, st.st_size, text)
            if len(out) >= max_files: return out
    return out

def measured_changes(cwd, before, mid, info=None):
    """What a build really changed, measured by the runner and not taken from the builder's own account: a short list
    for the mini review and a unified diff saved beside the mail. '' when nothing changed or the snapshot is missing.
    INFO, a dict, receives "files" ([relative path, plus, minus] per changed file) and "undo" (the mail id when an undo
    record was written), for the plan checks and the change log."""
    import difflib
    info = {} if info is None else info
    info.setdefault("files", []); info.setdefault("undo", "")
    if before is None: return ""
    after = snapshot(cwd); rows, diff = [], []
    undo = {"cwd": str(cwd), "restore": [], "delete": []}  # a repo folder need not be a git repository, so the runner keeps what an undo needs
    for rel, (mt, size, text) in sorted(after.items()):
        old = before.get(rel)
        if old and old[0] == mt and old[1] == size: continue
        a = (old[2] or "").splitlines() if old else []; b = (text or "").splitlines()
        d = list(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}", lineterm="", n=2)) if text is not None and (old is None or old[2] is not None) else []
        plus = sum(1 for l in d if l.startswith("+") and not l.startswith("+++")); minus = sum(1 for l in d if l.startswith("-") and not l.startswith("---"))
        if old and d == [] and text is not None and old[2] is not None: continue  # touched, same content
        rows.append(f"- {rel} ({'new, ' if not old else ''}+{plus} -{minus})"); diff += d; info["files"].append([rel.replace("\\", "/"), plus, minus])
        if not old: undo["delete"].append(rel)
        elif old[2] is not None: undo["restore"].append((rel, old[2]))
    for rel in sorted(set(before) - set(after)):
        rows.append(f"- {rel} (deleted)"); info["files"].append([rel.replace("\\", "/"), 0, 0])
        if before[rel][2] is not None: undo["restore"].append((rel, before[rel][2]))
    if not rows: return "\n\n---\nMeasured by the runner: no source file changed in this run."
    where = ""
    try:
        d = queue_root() / "attachments" / mid; d.mkdir(parents=True, exist_ok=True)
        (d / "changes.diff").write_text("\n".join(diff[:4000]) + ("\n... diff cut at 4000 lines" if len(diff) > 4000 else ""), encoding="utf-8")
        where = f"\nFull diff: {d / 'changes.diff'}"
        for rel, text in undo["restore"]:
            q = d / "before" / rel; q.parent.mkdir(parents=True, exist_ok=True); q.write_text(text, encoding="utf-8")
        (d / "undo.json").write_text(json.dumps({"cwd": undo["cwd"], "host": hostname(), "restore": [r for r, _ in undo["restore"]], "delete": undo["delete"]}), encoding="utf-8")
        where += f"\nUndo: python undo-build.py {mid} (in the switchboard folder, on {hostname()})"; info["undo"] = mid
    except OSError: pass
    return f"\n\n---\nMeasured by the runner, not reported by the builder: {len(rows)} file(s) changed.\n" + "\n".join(rows[:10]) + (f"\n... and {len(rows) - 10} more (all in the diff)" if len(rows) > 10 else "") + where

CHECK_RE = re.compile(r"(?m)^CHECK:\s*(.+?)\s*$")
CHECKDIR_RE = re.compile(r"(?m)^CHECKDIR:\s*(.+?)\s*$")
CHECK_CMDS = {"python", "python3", "py", "pytest", "ruff", "mypy", "pyright", "uv", "node", "npm", "npx", "pnpm", "yarn", "bun", "deno", "tsc", "eslint", "jest", "vitest",
              "go", "cargo", "dotnet", "swift", "ruby", "bundle", "rspec", "php", "phpunit", "mvn", "gradle"}  # test and lint runners; add your stack's here
CHECK_BAD = re.compile(r"[;&|<>`$\n\r]|\.\.[\\/]")  # one plain command, no shell, no leaving the folder

def task_folders(cwd, text):
    """Existing folders inside cwd that the task text names (absolute or relative paths, or the folder of a named file), in order."""
    root = Path(cwd).resolve(); out = []
    pat = re.compile(r"""[A-Za-z]:[\\/][^\s"'`<>|)]+|(?<![\w.:-])/[^\s"'`<>|)]+|(?:[\w.-]+[\\/])+[\w.-]*""")
    for cand in pat.findall(str(text or "")):
        cand = cand.rstrip(".,;:)")
        if not cand: continue
        try:
            p = Path(cand.replace("\\", "/"))
            p = (p if p.is_absolute() else root / p).resolve()
            if p.is_file(): p = p.parent
            if p.is_dir() and (p == root or root in p.parents) and p not in out: out.append(p)
        except (OSError, ValueError): continue
        if len(out) >= 20: break
    return out

SCRIPT_EXT = (".py", ".js", ".mjs", ".cjs", ".ts")
NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}

def _rel(p, root):
    try: return Path(p).resolve().relative_to(root).as_posix() or "."
    except ValueError: return str(p)

def run_plan_checks(cwd, text, limit=6, timeout=300, changed=(), dev_root=None):
    """Run the plan's CHECK lines after a build and report each result (VeriMAP, arXiv 2510.17109: the planner states
    how a subtask is verified). Returns (text for the reply, checks run, checks passed, checks that could not run). A
    check is one plain command from a short list of tools, run without a shell inside the build's folder: the plan is
    written by a model that also reads untrusted text, so nothing else is executed. A failed check is evidence for the
    mini review, not a verdict; planner-written checks are sometimes too strict.
    SETUP marks the harness, not the build, and only for: the script it names is not a file where it ran (also pytest's
    exit 4 "file or directory not found"), the time limit, a tool not installed, or a `python -m <module>` whose module
    is not installed for this runner's Python. Any other non-zero exit is FAIL.
    CHANGED (paths relative to cwd) are this build's files: their folders are searched for a named script first, then
    the folders the task text names; CHECKDIR switches both off."""
    import shlex
    cmds = CHECK_RE.findall(str(text or ""))[:limit]
    if not cmds: return "", 0, 0, 0
    rows, ok, setup, run_dir, root = [], 0, 0, cwd, Path(cwd).resolve()
    prose = CHECKDIR_RE.sub("", CHECK_RE.sub("", str(text or ""))).lower()  # the task without its CHECK lines
    m = CHECKDIR_RE.search(str(text or ""))
    if m:
        val = m.group(1).strip().strip('"')
        v = val.replace("\\", "/"); parts = v.split("/")
        cands = [(root / v, root, False)]  # (path, folder it must stay inside, was it re-read?)
        if parts[0].lower() == root.name.lower(): cands.append((root / "/".join(parts[1:]), root, True))
        if dev_root: cands.append((Path(dev_root).resolve() / v, Path(dev_root).resolve(), True))
        hit = next(((d, lim, alt) for c, lim, alt in cands for d in [c.resolve()] if d.is_dir() and (d == lim or lim in d.parents)), None)
        if hit:
            run_dir = hit[0]
            if hit[2]: rows.append(f"- NOTE CHECKDIR `{val[:100]}` read as {_rel(hit[0], hit[1])} (write it relative to the build folder)")
        else: rows.append(f"- NOTE CHECKDIR `{val[:100]}` ignored: not a folder inside {cwd}")
    folders = []
    if not m:
        for rel in changed or ():
            try: f = (root / str(rel).replace("\\", "/")).resolve().parent
            except (OSError, ValueError): continue
            if f.is_dir() and (f == root or root in f.parents) and f not in [x for x, _ in folders]: folders.append((f, "the folder of a file this build changed"))
        folders += [(f, "named in the task") for f in task_folders(cwd, text) if f not in [x for x, _ in folders]]
    for c in cmds:
        try: parts = shlex.split(c, posix=os.name != "nt")
        except ValueError: parts = []
        tool = Path(parts[0]).name.lower().removesuffix(".exe") if parts else ""
        if not parts or CHECK_BAD.search(c) or tool not in CHECK_CMDS or Path(parts[0]).name != parts[0]:
            rows.append(f"- NOT RUN `{c[:100]}`: only a single command from this list is allowed, without pipes, redirects or paths: {', '.join(sorted(CHECK_CMDS))}"); continue
        exe = sys.executable if tool in ("python", "python3", "py") else (shutil.which(parts[0]) or shutil.which(parts[0] + ".cmd"))
        if not exe and tool == "pytest" and importlib.util.find_spec("pytest") is not None:
            exe, parts = sys.executable, [parts[0], "-m", "pytest", *parts[1:]]
        if not exe: rows.append(f"- SETUP `{c[:100]}`: {parts[0]} is not installed on this device"); setup += 1; continue
        if tool in ("python", "python3", "py") and len(parts) > 2 and parts[1] == "-m":
            try: have = importlib.util.find_spec(parts[2]) is not None
            except (ImportError, ValueError): have = False
            if not have: rows.append(f"- SETUP `{c[:100]}`: {parts[2]} is not installed for this runner's Python"); setup += 1; continue
        use_dir = run_dir
        script = None if len(parts) > 1 and parts[1] in ("-m", "-c") else next((a for a in parts[1:] if not a.startswith("-") and a.lower().endswith(SCRIPT_EXT)), None)
        if script and not (Path(run_dir) / script).is_file():
            hit = next(((f, why) for f, why in folders if (f / script).is_file()), None)
            if not hit:
                if Path(script).name.lower() in prose:  # the task itself names the file, so the build was to write or keep it: a real failure
                    rows.append(f"- FAIL `{c[:100]}`: {script} is not a file in {_rel(run_dir, root)}, though the task names it"); continue
                rows.append(f"- SETUP `{c[:100]}`: {script} is not a file in {_rel(run_dir, root)}"); setup += 1; continue
            use_dir = hit[0]; rows.append(f"- NOTE `{c[:100]}` ran in {_rel(hit[0], root)} ({hit[1]}; add CHECKDIR to be explicit)")
        t0 = time.monotonic()
        try:
            r = subprocess.run([exe, *parts[1:]], cwd=str(use_dir), stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **NO_WINDOW)
            secs, both = time.monotonic() - t0, r.stdout + "\n" + r.stderr
            tail = " | ".join(l.strip() for l in both.splitlines() if l.strip())[-300:]
            is_pytest = tool == "pytest" or parts[1:3] == ["-m", "pytest"]
            if r.returncode == 0: ok += 1; rows.append(f"- PASS `{c[:100]}` ({secs:.0f} s)")
            elif is_pytest and r.returncode == 4 and "file or directory not found" in both: setup += 1; rows.append(f"- SETUP `{c[:100]}`: pytest found no such file or folder in {_rel(use_dir, root)}: {tail[-150:]}")
            else: rows.append(f"- FAIL `{c[:100]}` (exit {r.returncode}, {secs:.0f} s): {tail}")
        except subprocess.TimeoutExpired: setup += 1; rows.append(f"- SETUP `{c[:100]}`: no result in {timeout} s (the time limit)")
        except OSError as e: setup += 1; rows.append(f"- SETUP `{c[:100]}`: could not start: {e}")
    head = f"\nChecks from the plan, run by the runner: {ok} of {len(cmds)} passed" + (f", {setup} could not run (SETUP lines are the harness, not the build)" if setup else "")
    return head + ".\n" + "\n".join(x[:160] for x in rows), len(cmds), ok, setup

def runner_cfg():
    try: d = json.loads((queue_root() / "mail" / "runner.json").read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}
    return d if isinstance(d, dict) else {}

def routing_cfg():
    """runner.json "routing". Codex builds stay off until it says "codex_builds": true, so a missing key or an
    unreadable file never routes a build to Codex (it has never built here); reviews are unaffected."""
    r = runner_cfg().get("routing")
    return {"codex_builds": False, **(r if isinstance(r, dict) else {})}

# One writer key for the switchboard folder itself (HERE): these areas all edit its files, which hot-reload on every
# device, so they must hold each other. runner.json "area_alias" adds to or overrides this.
TOOLKIT = "switchboard"
# Per-host claim folders are invisible to older code, which reads only Queue/mail/claims/<name>.json. True also writes
# each claim flat, for a fleet where some devices still run that older code.
LEGACY_FLAT_CLAIMS = False
AREA_ALIAS = {"discord-bridge": TOOLKIT, "mail-runner": TOOLKIT, "orchestration": TOOLKIT, "route": TOOLKIT, "fleet": TOOLKIT}

def area_key(area):
    a = str(area or "").lower()
    extra = runner_cfg().get("area_alias")
    alias = {**AREA_ALIAS, **({str(k).lower(): str(v).lower() for k, v in extra.items()} if isinstance(extra, dict) else {})}
    return alias.get(a, a)

def choose(mail, agent, msg, relay, agents):
    """Model and effort for one run, from route.py: the task's words, the thread's history and what the owner asked for.
    Also returns "prior" (builds this request already had) and "req" (when the owner last wrote: the request's id)."""
    cfg = routing_cfg()
    if cfg.get("enabled") is False: return None
    try:  # Fable's own limit was hit recently: stay off it until the mark expires
        if float(json.loads((queue_root() / "mail" / "fable-blocked.json").read_text(encoding="utf-8")).get("until", 0)) > time.time(): cfg = {**cfg, "fable": "off"}
    except (OSError, ValueError, AttributeError): pass
    thread = msg.get("thread") or msg.get("id"); rows = fleet.stats(72)
    since = last_word(mail, thread, agents)[1]  # a long thread holds many requests: count only builds since the owner last wrote
    prior = sum(1 for r in rows if r.get("thread") == thread and r.get("agent") == "builder" and float(r.get("ts", 0)) > since)
    fable_today = sum(1 for r in rows if str(r.get("model") or "").startswith("claude-fable") and float(r.get("ts", 0)) > time.time() - 86400)
    said = last_word(mail, thread, agents)[0]
    pol = fleet.engines_policy(); q = fleet.codex_best()
    codex_ok = fleet.codex_usable(q, pol) and bool(fleet.codex_exe())
    builds = sorted((r for r in rows if r.get("thread") == thread and r.get("agent") == "builder" and float(r.get("ts", 0)) > since), key=lambda r: r.get("ts", 0))
    R = load_route(); text = f"{msg.get('subject', '')}\n{msg.get('body', '')}"
    d = R.pick(agent, text, relay=relay, prior_builds=prior if agent == "builder" else 0,
               owner_text=said, cfg=cfg, fable_today=fable_today, codex_ok=codex_ok, built_by=str(builds[0 if agent == "builder" else -1].get("engine") or "") if builds else "",
               sender=str(msg.get("from") or ""))
    level, why, ask = R.ponytail(agent, text, said, prior if agent == "builder" else 0, cfg)
    return {**d, "prior": prior, "req": since, "ponytail": level, "ponytail_why": why, "ponytail_ask": ask, "ponytail_text": (text, said) if ask else None}

def ask_ponytail(exe, text, owner_text, fallback, timeout_s=120):
    """(level, why) for a complex build, from a short Sonnet side run with no tools, plugins or MCP. Any failure keeps FALLBACK."""
    R = load_route()
    try: off = {k: False for k in (json.loads((Path.home() / ".claude/settings.json").read_text(encoding="utf-8")).get("enabledPlugins") or {})}
    except (OSError, ValueError): off = {}
    cmd = [exe, "-p", "--model", R.SONNET, "--effort", "low", "--output-format", "json", "--tools", "", "--strict-mcp-config",
           "--disable-slash-commands", "--no-session-persistence", "--setting-sources", "project,local", "--settings", json.dumps({"enabledPlugins": off})]  # no user hooks or CLAUDE.md in a one-word side check
    try:
        r = subprocess.run(cmd, input=R.ponytail_prompt(text, owner_text), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout_s, env=dict(os.environ, SESSION_HOOK_SKIP="1", PONYTAIL_DEFAULT_MODE="off"))
        return R.ponytail_answer(parse_output(r.stdout)[1], fallback)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return fallback, f"side check failed ({type(e).__name__})"

def ponytail_banned(cwd):
    """True when the project's own Claude settings switch the plugin off (a project that bans Git must, since Ponytail's hooks run it)."""
    for d in [Path(cwd).resolve(), *Path(cwd).resolve().parents]:
        for f in ("settings.local.json", "settings.json"):
            try: v = (json.loads((d / ".claude" / f).read_text(encoding="utf-8")).get("enabledPlugins") or {}).get("ponytail@ponytail")
            except (OSError, ValueError, AttributeError): continue
            if v is not None and d != Path.home(): return v is False
    return False

def review_wanted(mail, msg, agents):
    """(bool, why) from route.needs_review: the owner wants reviews only on big jobs."""
    try: cfg = (json.loads((queue_root() / "mail" / "runner.json").read_text(encoding="utf-8")).get("routing")) or {}
    except (OSError, ValueError, AttributeError): cfg = {}
    thread = msg.get("thread") or msg.get("id")
    said, since = last_word(mail, thread, agents)
    builds = sorted((r for r in fleet.stats(72) if r.get("thread") == thread and r.get("agent") == "builder" and float(r.get("ts", 0)) > since), key=lambda r: r.get("ts", 0))
    last = builds[-1] if builds else {}
    return load_route().needs_review(f"{msg.get('subject', '')}\n{msg.get('body', '')}", said, build_secs=float(last.get("secs") or 0),
                                     build_out_tokens=int(last.get("output_tokens") or 0), prior_builds=len(builds), cfg=cfg)

RELAY = set()  # message ids whose run only handles another agent's mail (set by the runner just before the run)

def tool_phrase(name, inp):
    """A few words for what a tool call is doing, for the live card in Discord. None for bookkeeping tools."""
    n, inp = str(name or ""), inp if isinstance(inp, dict) else {}
    base = lambda k: Path(str(inp.get(k) or "")).name
    if n in ("Edit", "MultiEdit", "Write", "NotebookEdit"): return f"Editing {base('file_path') or base('notebook_path') or 'a file'}"
    if n == "Read": return f"Reading {base('file_path') or 'a file'}"
    if n in ("Grep", "Glob"): return "Searching the code"
    if n == "Bash": return str(inp.get("description") or "").strip() or ("Running " + (re.findall(r"[\w.-]+", str(inp.get("command") or "")) or ["a command"])[0])
    if n in ("WebFetch", "WebSearch"): return "Looking it up online"
    if n in ("Task", "Agent"): return "Running a helper agent"
    if n.startswith("mcp__agent_mail__"):
        return {"send_message": f"Messaging {inp.get('to') or 'an agent'}", "ask_owner": "Asking the owner", "update_owner": "Updating the owner", "reply_message": "Replying"}.get(n.rsplit("__", 1)[1], "Checking mail")
    if n in ("TodoWrite", "ToolSearch", "TodoRead"): return None
    return n.replace("mcp__", "").replace("__", ": ").replace("_", " ")

class LiveTail(threading.Thread):
    """Reads a running Claude's stream-json output as it is written and keeps Queue/live/<message id>.json saying what it
    is doing right now, so the Discord card can show it. The file goes away when the run ends."""
    def __init__(self, path, mid, agent, session="", cwd=""):
        super().__init__(daemon=True); self.path, self.agent, self.stop = Path(path), agent, threading.Event()
        self.session, self.cwd = session, str(cwd)
        self.dest = queue_root() / "live" / f"{re.sub(r'[^A-Za-z0-9_-]', '_', str(mid or 'x'))}.json"
    def run(self):
        pos, buf, last, n, trail, says, t0 = 0, b"", None, 0, [], "", time.time()
        while not self.stop.wait(3):
            try:
                with self.path.open("rb") as f: f.seek(pos); chunk = f.read(); pos += len(chunk)
            except OSError: continue
            lines = (buf + chunk).split(b"\n"); buf = lines.pop()
            new, changed = None, False
            for raw in lines:
                if b'"assistant"' not in raw: continue
                try: d = json.loads(raw)
                except ValueError: continue
                self.session = str(d.get("session_id") or self.session)  # the id the run really uses (a resumed run keeps its own)
                for b in ((d.get("message") or {}).get("content") or []):
                    if not isinstance(b, dict): continue
                    if b.get("type") == "tool_use":
                        n += 1; ph = tool_phrase(b.get("name"), b.get("input"))
                        if ph and (not trail or trail[-1][1] != ph[:90]): trail.append([round(time.time()), ph[:90]]); changed = True
                        new = ph or new
                    elif b.get("type") == "text" and str(b.get("text") or "").strip():
                        says = " ".join(str(b["text"]).split())[:400]; changed = True  # its latest words, for the live card
            if new: last = new
            if changed and last:
                try: fleet._write(self.dest, {"agent": self.agent, "host": hostname(), "doing": last[:80], "tools": n, "at": time.time(),
                                              "t0": t0, "trail": trail[-15:], "says": says, "session": self.session, "cwd": self.cwd})
                except OSError: pass
        try: self.dest.unlink()
        except OSError: pass

SESSIONS = {}  # message id -> (session id, cwd) of its latest Claude run, for the stats row and the end event
KEEP_SETTINGS = ("env", "autoMode", "skipAutoPermissionPrompt", "permissions", "autoMemoryEnabled")
KEEP_HOOKS = (("PermissionRequest", "permission-hook"), ("PreToolUse", "source-guard"))

def agent_settings(pony="off"):
    """The one --settings JSON of an agent run, which also gets --setting-sources project,local, so the user settings
    file and its memory-loader and session hooks are not loaded (measured: 27K chars of hooks and a 15.6K user
    CLAUDE.md in a run that saved one note). Built from the device's own ~/.claude/settings.json, read each
    run: KEEP_SETTINGS and only the permission and source-guard hooks; every plugin off but the runner's opt-ins.
    None when the file cannot be read: the run then keeps the user settings, as before."""
    try: user = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError): return None
    if not isinstance(user, dict): return None
    out, hooks = {k: user[k] for k in KEEP_SETTINGS if k in user}, {}
    for event, name in KEEP_HOOKS:
        for m in (user.get("hooks") or {}).get(event) or []:
            hs = [h for h in (m.get("hooks") or []) if isinstance(h, dict) and name in str(h.get("command") or "")] if isinstance(m, dict) else []
            if hs: hooks.setdefault(event, []).append({**m, "hooks": hs})
    if hooks: out["hooks"] = hooks
    plugins = {k: False for k in (user.get("enabledPlugins") or {})}
    if pony != "off" and "ponytail@ponytail" in plugins: plugins["ponytail@ponytail"] = True
    out["enabledPlugins"] = plugins
    return out

def stream_session(out):
    """The session id a stream-json run reports, or ''."""
    for line in str(out or "").splitlines()[:20]:
        if '"session_id"' not in line: continue
        try: return str(json.loads(line).get("session_id") or "")
        except (ValueError, AttributeError): continue
    return ""

def context_tokens(out):
    """The context of a run's last assistant turn: input + cache read + cache creation tokens, from its stream; 0 if absent."""
    n = 0
    for line in str(out or "").splitlines():
        if '"assistant"' not in line or '"usage"' not in line: continue
        try: u = (json.loads(line).get("message") or {}).get("usage") or {}
        except (ValueError, AttributeError): continue
        t = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if t: n = t
    return n

def run_claude(exe, prompt, mode, cwd, timeout_s, agent, via_stdin, with_mcp, parent="", claims=None, cancel=None, ponytail="off", resume=""):
    """Run Claude once. Returns (returncode or None on timeout, stdout, stderr). Output goes to files, not pipes.
    PONYTAIL is the plugin level for this run (off, lite, full, ultra); the plugin's own default is off.
    Every run gets its own --session-id, recorded in SESSIONS, so the bridge can offer to open it in Claude; RESUME
    continues that session instead (it keeps its id)."""
    sid = resume or str(uuid.uuid4()); SESSIONS[parent] = (sid, str(cwd))
    pony = ponytail if ponytail in ("lite", "full", "ultra") and not ponytail_banned(cwd) else "off"
    env = run_env(parent, claims, AGENT_MAIL_AGENT=agent, PONYTAIL_DEFAULT_MODE=pony)
    model, sub = pick_model(agent, parent in RELAY); RELAY.discard(parent)
    routed = ROUTE.pop(parent, None)  # route.py's choice for this run: model and effort
    if routed: model = routed.get("model") or model
    if model: env["ANTHROPIC_MODEL"] = model
    if sub: env["CLAUDE_CODE_SUBAGENT_MODEL"] = sub
    with tempfile.TemporaryDirectory(prefix="mail-runner-", ignore_cleanup_errors=True) as td:
        td = Path(td)
        extra = []
        try: in_repo = agent != "orchestrator" and bool(Path(cwd).resolve().relative_to(dev_root().resolve()).parts)
        except (ValueError, OSError): in_repo = False
        if with_mcp:
            py = [sys.executable] + (["-X", "utf8"] if os.name == "nt" else [])
            cfg = {"mcpServers": {"agent_mail": {"command": py[0], "args": py[1:] + [
                str(HERE / "agent-mail-mcp.py"), "--agent", agent], "env": {"SWITCHBOARD_ROOT": str(dev_root()), "AGENT_MAIL_PARENT": parent or ""}}}}
            allowed = ["mcp__agent_mail"]  # nothing else is loaded
            (td / "mcp.json").write_text(json.dumps(cfg), encoding="utf-8")
            extra = ["--mcp-config", str(td / "mcp.json"), "--strict-mcp-config", "--exclude-dynamic-system-prompt-sections", "--allowedTools", *allowed]
        # Measured: unused MCP servers and plugins were 37 to 60 percent of a run's cost; user hooks and CLAUDE.md another 40K chars.
        lean = agent_settings(pony)
        if lean is not None: extra += ["--setting-sources", "project,local", "--settings", json.dumps(lean)]
        if routed and routed.get("effort"): extra = list(extra) + ["--effort", routed["effort"]]
        (td / "prompt.txt").write_text(prompt, encoding="utf-8", newline="\n")
        lean_flags, lean_env = lean_launch(agent, in_repo=in_repo)
        env.update(lean_env)
        cmd = build_cmd(exe, prompt, mode, via_stdin, extra + lean_flags + ["--autocompact", "auto"] + (["--resume", sid] if resume else ["--session-id", sid]))  # heavy context compacts; it never ends the session
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        with (td / "out.txt").open("wb") as fo, (td / "err.txt").open("wb") as fe, (td / "prompt.txt").open("rb") as fi:
            p = subprocess.Popen(cmd, cwd=str(cwd), stdin=fi if via_stdin else subprocess.DEVNULL,
                                 stdout=fo, stderr=fe, env=env, **kw)
            tail = LiveTail(td / "out.txt", parent, agent, sid, cwd); tail.start()
            try:
                rc = wait_run(p, timeout_s, cancel, watch=(td / "out.txt",))
                if rc is None: kill_tree(p)
            except BaseException:
                kill_tree(p); raise
            finally: tail.stop.set(); tail.join(5)
        out = (td / "out.txt").read_text(encoding="utf-8", errors="replace")
        err = (td / "err.txt").read_text(encoding="utf-8", errors="replace")
    if stream_session(out): SESSIONS[parent] = (stream_session(out), str(cwd))
    return rc, out, err

def run_codex(prompt, cwd, timeout_s, writes, parent="", claims=None, cancel=None, model="", effort=""):
    """Run Codex once, non-interactively. Returns (returncode or None on timeout, last message text, stderr)."""
    exe = fleet.codex_exe()
    with tempfile.TemporaryDirectory(prefix="mail-runner-", ignore_cleanup_errors=True) as td:
        td = Path(td); (td / "prompt.md").write_text(prompt, encoding="utf-8", newline="\n")
        pick = (["-m", model] if model else []) + (["-c", f'model_reasoning_effort="{effort}"'] if effort else [])
        cmd = [exe, "exec", *pick, "--skip-git-repo-check", "-C", str(cwd), "-s", "workspace-write" if writes else "read-only", "-o", str(td / "last.txt"),
               "-"]  # the prompt goes in on stdin: Windows' elevated sandbox runs Codex's commands as CodexSandboxOffline, which cannot read this temp folder
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        with (td / "out.txt").open("wb") as fo, (td / "err.txt").open("wb") as fe, (td / "prompt.md").open("rb") as fi:
            p = subprocess.Popen(cmd, cwd=str(cwd), stdin=fi, stdout=fo, stderr=fe, env=run_env(parent, claims, SESSION_HOOK_SKIP="1"), **kw)
            try:
                rc = wait_run(p, timeout_s, cancel, watch=(td / "out.txt", td / "err.txt"))
                if rc is None: kill_tree(p)
            except BaseException: kill_tree(p); raise
        last = (td / "last.txt").read_text(encoding="utf-8", errors="replace") if (td / "last.txt").is_file() else ""
        return rc, last, (td / "err.txt").read_text(encoding="utf-8", errors="replace")[-1500:]

LIMIT_RE = re.compile(r"usage limit|limit reached|rate limit|quota|out of extra usage|credit balance|hit your [\w -]*limit|5-hour limit|limit\s*[\u00b7\u2022\u2219|-]\s*resets", re.I)
EPOCH_RE = re.compile(r"limit reached\|(\d{9,11})")
RESET_RE = re.compile(r"resets\s+(?:at\s+)?(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?:at\s+)?)?(?P<h>\d{1,2})(?::(?P<mi>\d\d))?\s*(?P<ap>[ap]\.?m\.?)?\s*(?:\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_+-]+)*)\))?", re.I)
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

def zone(name=None):
    """The time zone NAME (an IANA name such as a limit message carries), else this machine's local zone. An unknown
    name, or Windows Python without tzdata, also gives the local zone."""
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception: pass
    return datetime.now().astimezone().tzinfo

def _reset_at(m, now):
    """The epoch a RESET_RE match names: a date and time, or a time alone (its next occurrence). 0 when unusable."""
    h, mi, ap = int(m["h"]), int(m["mi"] or 0), (m["ap"] or "").lower().replace(".", "")
    if not ap and not m["mi"]: return 0
    if ap: h = h % 12 + (12 if ap == "pm" else 0)
    if h > 23 or mi > 59: return 0
    base = datetime.fromtimestamp(now, zone(m["tz"]))  # no zone named: this machine's local time
    try:
        if m["mon"]:
            t = base.replace(month=MONTHS.index(m["mon"][:3].lower()) + 1, day=int(m["day"]), hour=h, minute=mi, second=0, microsecond=0)
            if t.timestamp() < now - 86400: t = t.replace(year=t.year + 1)
        else:
            t = base.replace(hour=h, minute=mi, second=0, microsecond=0)
            if t.timestamp() <= now: t += timedelta(days=1)
    except ValueError: return 0
    return t.timestamp()

def limit_reset(text, out="", now=None):
    """When a Claude usage limit ends, as an epoch: a rejected rate_limit_event in the run's stream-json, else the
    "limit reached|<epoch>" form, else "resets Jan 16, 9am (America/Los_Angeles)" or "resets 3pm (...)"; else an hour."""
    now = time.time() if now is None else now
    hits = []
    for line in str(out or "").splitlines():
        if '"rate_limit_event"' not in line: continue
        try: info = json.loads(line).get("rate_limit_info") or {}
        except (ValueError, AttributeError): continue
        rows = [info] + [v for v in (info.get("unifiedWindows") or {}).values() if isinstance(v, dict)]
        hits += [float(r["resetsAt"]) for r in rows if r.get("status") == "rejected" and r.get("resetsAt")]
    m = EPOCH_RE.search(str(text or ""))
    if m: hits.append(float(m.group(1)))
    if not hits:
        m = RESET_RE.search(str(text or ""))
        if m: hits.append(_reset_at(m, now))
    t = max(hits, default=0)
    return t if now < t < now + 8 * 86400 else now + 3600

def block_file(): return queue_root() / "usage" / "claude-blocked.json"

def claude_blocked(local=None):
    """When Claude's usage limit ends (epoch), or 0 when Claude is usable: the shared Queue/usage/claude-blocked.json and
    LOCAL, this process's own copy. A plan reading from any device newer than a block, with every window under 100
    percent, means the limit lifted early, so that block no longer counts."""
    try: f = json.loads(block_file().read_text(encoding="utf-8"))
    except (OSError, ValueError): f = None
    r = fleet.usage_newest("claude"); r = r if isinstance(r, dict) else {}
    w = [v for v in (r.get("windows") or {}).values() if isinstance(v, dict)] if isinstance(r.get("windows"), dict) else []
    under = bool(w) and all(float(v.get("utilization") or 0) < 1 for v in w)
    out = 0.0
    for d in (f, local):
        if not isinstance(d, dict): continue
        until = float(d.get("until") or 0)
        if until > time.time() and not (under and float(r.get("at") or 0) > float(d.get("at") or 0)): out = max(out, until)
    return out

def paused_text(reason, until):
    """The paused card's line: which limit, and when the work resumes by itself (this machine's local time)."""
    kind = re.search(r"weekly|session|5-hour|five-hour|daily", str(reason or ""), re.I)
    t = datetime.fromtimestamp(until, zone())
    when = f"{t:%a %b} {t.day}, {t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'}" + (f" {t:%Z}" if t.strftime("%Z") else "")
    return f"Claude's {kind.group(0).lower() if kind else 'usage'} limit is reached. Resumes on its own {when}, or sooner on Codex when it has quota."

# ---- open requests: Queue/open/<host>/<id>.json, written by the bridge, served by the runner on that device ----

TICK = 3  # seconds between cheap looks at the inboxes and open requests, whatever --poll says
SESSION_URL = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9]+")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
OPEN_EXT = {".html", ".htm", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov", ".md", ".txt", ".csv", ".json"}

def open_session(req, mode, root, wait=45):
    """Resume a finished run's session as a Remote Control background session: {"url", "bg"} or {"error"}."""
    sid, exe = str(req.get("session") or ""), find_claude()
    if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", sid): return {"error": "no valid session id"}
    if not exe: return {"error": f"Claude Code is not installed on {hostname()}"}
    cwd = Path(str(req.get("cwd") or "")).expanduser() if req.get("cwd") else Path(root)
    if not cwd.is_dir(): cwd = Path(root)  # a folder from the other device
    name = " ".join(str(req.get("name") or "Agent run").split())[:80] or "Agent run"
    with tempfile.TemporaryDirectory(prefix="mail-runner-open-", ignore_cleanup_errors=True) as td:
        o, e = Path(td) / "out.txt", Path(td) / "err.txt"
        with o.open("wb") as fo, e.open("wb") as fe:  # files, not pipes: the background service it starts may keep them open
            p = subprocess.Popen([exe, "--bg", "--remote-control", name, "--resume", sid, "--permission-mode", mode], cwd=str(cwd), stdin=subprocess.DEVNULL,
                                 stdout=fo, stderr=fe, env=dict(os.environ, SESSION_HOOK_SKIP="1"), **(NO_WINDOW if os.name == "nt" else {"start_new_session": True}))
            try: rc = p.wait(60)
            except subprocess.TimeoutExpired: kill_tree(p); return {"error": "claude --bg gave no answer in 60 s"}
        text = ANSI.sub("", o.read_text(encoding="utf-8", errors="replace") + "\n" + e.read_text(encoding="utf-8", errors="replace"))
    m = re.search(r"claude logs (\S+)", text) or re.search(r"backgrounded\W+(\w+)", text)
    if rc != 0 or not m: return {"error": f"claude --bg failed (exit {rc}): {' '.join(text.split())[-300:] or 'no output'}"}
    bg, end = m.group(1), time.monotonic() + wait
    while True:
        try:
            r = subprocess.run([exe, "logs", bg], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, stdin=subprocess.DEVNULL, **NO_WINDOW)
            u = SESSION_URL.search(ANSI.sub("", (r.stdout or "") + (r.stderr or "")))
            if u: return {"url": u.group(0), "bg": bg}
        except (OSError, subprocess.TimeoutExpired): pass
        if time.monotonic() >= end: return {"error": f"no Remote Control link in `claude logs {bg}` after {wait} s", "bg": bg}
        time.sleep(3)

def open_file(req, root):
    """Open one file with the OS default app: {"opened": path} or {"error"}. Only files of OPEN_EXT inside the folder
    that holds the dev root (relative paths are from the dev root), never under ~/.switchboard."""
    raw = str(req.get("path") or "").strip()
    if not raw: return {"error": "no path"}
    base, mem = Path(root).resolve().parent, (Path.home() / ".switchboard").resolve()
    p = Path(raw).expanduser(); p = (p if p.is_absolute() else Path(root) / p).resolve()
    if base not in p.parents: return {"error": f"refused: {p} is outside {base}"}
    if p == mem or mem in p.parents: return {"error": "refused: ~/.switchboard is never opened"}
    if p.suffix.lower() not in OPEN_EXT: return {"error": f"refused: {p.suffix or 'no extension'} is not a file type that may be opened"}
    if not p.is_file(): return {"error": f"not a file: {p}"}
    if os.name == "nt": os.startfile(str(p))
    else: subprocess.run(["open", str(p)], check=True, capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    return {"opened": str(p)}

def requeue(mail, agent, mid):
    """mail-runner.py requeue <agent> <mail id>: put an acked message back in the inbox without its ack fields, so a
    runner takes it again. Refuses one that is already in the inbox. Returns the exit code."""
    try: agent, mid = mail.resolve_agent(agent), mail._check_id(mid)
    except mail.MailError as e: print(e); return 2
    d = mail.agent_dir(agent); src, dst = d / "done" / f"{mid}.json", d / "inbox" / f"{mid}.json"
    if dst.exists(): print(f"{mid} is already in {agent}'s inbox"); return 1
    if not src.is_file(): print(f"{mid} is not in {agent}'s done folder"); return 1
    msg = json.loads(src.read_text(encoding="utf-8"))
    for k in ("acked_at", "acked_by", "result"): msg.pop(k, None)
    mail.write_json_atomic(dst, msg); mail._retry(src.unlink)
    print(f"requeued {mid} for {agent}"); return 0

WEEK_RE = re.compile(r'"seven_day"\s*:\s*\{\s*"utilization"\s*:\s*([\d.]+)')

def save_reading(out):
    """This run's plan reading from its rate_limit_event lines, as Queue/usage/claude-<host>.json: the
    pusher then needs its own probe turn only when no device has run Claude for an hour."""
    w = fleet.windows_from(out)
    if w: fleet.save_usage("claude", {"windows": w, "at": time.time(), "host": hostname()}, hostname())

def week_used(out):
    """(first, last) share of the Claude subscription's weekly limit, 0..1, from the run's rate_limit_event lines; (None, None) if absent."""
    hits = [float(x) for x in WEEK_RE.findall(out or "")]
    return (hits[0], hits[-1]) if hits else (None, None)

def parse_output(out):
    """Return (data dict or None, result text) from Claude's output (json, or stream-json: the last result line)."""
    text = out.strip()
    cands = [text] + [l for l in reversed(text.splitlines()) if l.strip().startswith(("{", "["))]
    for c in cands:
        try: data = json.loads(c)
        except ValueError: continue
        if isinstance(data, list):
            data = next((x for x in reversed(data) if isinstance(x, dict) and x.get("type") == "result"), None)
        if isinstance(data, dict) and "result" in data:
            r = data.get("result")
            return data, r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
    return None, text


def week_fields(engine, out, cq0):
    """What this run's engine reported of its weekly subscription limit, before and after (percent used)."""
    if engine == "claude":
        w0, w1 = week_used(out)
        return {"week0": w0 * 100, "week1": w1 * 100} if w0 is not None else {}
    cq1 = fleet.codex_quota()
    return {"week0": 100 - cq0["left"], "week1": 100 - cq1["left"]} if cq0 and cq1 else {}


# ---- prompt ----

def _clip(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[:n] + f"\n[... {len(s) - n} more characters, read the message for the rest]"

def discord_rules():
    """What the owner told the triage assistant about how answers in Discord should look and behave (Queue/discord/argon-memory.json)."""
    try: rows = json.loads((queue_root() / "discord" / "argon-memory.json").read_text(encoding="utf-8"))
    except (OSError, ValueError): return ""
    return "\n".join(f"- {r['rule']}" for r in rows)

def told_owner(mail, agent, msg, since):
    """This run already mailed the owner an answer in this thread (send_message to owner): the runner's reply would be a second report."""
    iso = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    try: rows = mail.thread(msg.get("thread") or msg["id"])
    except Exception: return False
    return any(m.get("from") == agent and m.get("to") == NOTIFY and str(m.get("created") or "") >= iso and not m.get("options") and not m.get("control") for m in rows)

def first_sentence(text, n=160):
    t = " ".join(re.sub(r"[*_`#>]+", "", str(text or "")).split())
    return re.split(r"(?<=[.!?])\s", t, maxsplit=1)[0][:n]

def change_dir(key): return queue_root() / "changes" / re.sub(r"[^\w.-]", "_", str(key or "workspace").lower())

def log_change(key, msg, agent, model, result, info):
    """One line per build in Queue/changes/<repo or area>/<host>.jsonl (one writer per file), from what the runner
    measured, not what the builder said. A build that changed no file still gets a line, with no files."""
    row = {"ts": time.time(), "host": hostname(), "mid": msg.get("id"), "thread": msg.get("thread") or msg.get("id"), "agent": agent, "model": model,
           "subject": str(msg.get("subject") or "")[:100], "files": (info.get("files") or [])[:15], "nfiles": len(info.get("files") or []),
           "undo": info.get("undo") or "", "summary": first_sentence(result)}
    f = change_dir(key) / f"{hostname()}.jsonl"; f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as h: h.write(json.dumps(row, ensure_ascii=False) + "\n")

def recent_changes(key, n=10):
    """The last N change-log lines for a repo or area, every device's file merged by time, one prompt line each."""
    rows = []
    for f in change_dir(key).glob("*.jsonl"):
        try: rows += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        except (OSError, ValueError): continue
    out = []
    for r in sorted((r for r in rows if isinstance(r, dict)), key=lambda r: float(r.get("ts") or 0))[-n:]:
        label = str(r.get("host") or "?")
        fs = r.get("files") or []; k = int(r.get("nfiles") or len(fs))
        what = (f"{k} file{'s' if k != 1 else ''} ({fs[0][0]}{', ...' if k > 1 else ''}, +{sum(int(x[1]) for x in fs)} -{sum(int(x[2]) for x in fs)})" if fs else f"no files; {str(r.get('summary') or '')[:100]}")
        out.append(f"- {time.strftime('%m-%d %H:%M', time.gmtime(float(r.get('ts') or 0)))} {label} {r.get('agent')} {str(r.get('subject') or '')[:70]}: {what}" + (f", undo {r['undo']}" if r.get("undo") else ""))
    return out

def same_repo_text(mail, msg):
    """The other Discord threads of this message's repo in the last 48 hours, from the bridge's synced state, plus the
    repo's newest builds. Finished ones too: a thread that already did the work is exactly what a sibling must know."""
    tid = msg.get("thread") or msg.get("id")
    try: repo = msg.get("repo") or next((fleet.area_of(m) for m in mail.thread(tid) if fleet.area_of(m)), "")
    except Exception: repo = msg.get("repo") or ""
    g = str(repo or "").lower()
    if not g: return ""
    st = fleet.load_merged(queue_root() / "discord" / "state.json", {})  # each bridge writes its own copy
    rows, now = [], time.time()
    for k, t in (st.get("threads") or {}).items():
        title = str(t.get("title") or "")
        if k == tid or now - float(t.get("active_at") or t.get("t0") or 0) > 48 * 3600: continue
        if str(t.get("project") or "").lower() != g and not title.lower().startswith(g): continue
        rows.append((float(t.get("active_at") or t.get("t0") or 0), f"- {k} | {title[:80]} | {t.get('shown') or '?'} | last answer: {' '.join(str(t.get('answer') or '').split())[:220]}"))
    out = [r for _, r in sorted(rows, reverse=True)[:2]]
    out += [f"- build: {l.lstrip('- ')}" for l in recent_changes(g if msg.get("repo") else area_key(g), n=2)]
    return "\n".join(l[:150] for l in out[:4])  # threads, builds (UTC, measured by the runner): four short lines at most

def thread_file(tid, ext): return queue_root() / "threads" / (re.sub(r"[^\w.-]", "_", str(tid or "x")) + ext)

def thread_brief(mail, msg):
    """## Thread so far: the brief the orchestrator keeps in Queue/threads/<thread>.md, else the request and the latest
    builder task (the plan), and how to read the whole history. '' for a thread's first message with no brief."""
    tid = msg.get("thread") or msg.get("id")
    try: brief = thread_file(tid, ".md").read_text(encoding="utf-8").strip()
    except OSError: brief = ""
    out = [brief[:1500] + (" [...]" if len(brief) > 1500 else "")] if brief else []
    if not brief:
        try: rows = mail.thread(tid)
        except Exception: rows = []
        earlier = rows[:next((i for i, m in enumerate(rows) if m.get("id") == msg.get("id")), len(rows))]  # what came before this message
        if not earlier: return ""
        task = next((m for m in reversed(earlier) if m.get("from") == "orchestrator" and m.get("to") == "builder"), None)
        out.append(f"The request ({earlier[0].get('from')}, {earlier[0].get('created')}): {_clip(earlier[0].get('body'), 500)}")
        if task and task is not earlier[0]: out.append(f"Latest builder task ({task.get('created')}): {_clip(task.get('body'), 800)}")
    return "\n".join(out + [f"Full history: python {HERE / 'agent-mail.py'} thread {tid}"])

def note_line(n):
    tag = {"unread": "[NEW] ", "withdrawn": "[WITHDRAWN, the owner deleted this: treat it as unsaid] "}.get(n.get("state"), "")
    if n.get("edited"): tag += "[edited, this is the newest wording] "
    re_ = f"(replying to {n['reply'].get('who')}: \"{n['reply'].get('text', '')[:160]}\") " if isinstance(n.get("reply"), dict) else ""
    files = f" [{n['files']} file(s) attached]" if n.get("files") else ""
    return f"- {time.strftime('%H:%M', time.localtime(float(n.get('at', 0))))} {tag}{re_}{' '.join(str(n.get('text', '')).split())[:3000]}{files}"

def notepad_text(thread):
    """The owner's notepad, compact: what no agent has read yet in full, the rest as a count and where to read it."""
    try:
        import notes as _notes
        every = _notes.load(thread)
    except Exception: return ""
    new = [n for n in every if isinstance(n, dict) and n.get("state") == "unread"]
    rows = [note_line(n) for n in new]
    if len(every) > len(new): rows.append(f"{len(every) - len(new)} earlier note(s), already read; full notepad: python {HERE / 'notes.py'} show {thread}")
    return "\n".join(rows)

def other_work(mail, msg):
    """Other unfinished threads in this message's repo or area (both devices), one line each, at most five."""
    tid = msg.get("thread") or msg.get("id")
    try: key = area_key(msg.get("repo") or fleet.area_of(msg) or next((fleet.area_of(m) for m in mail.thread(tid) if fleet.area_of(m)), ""))
    except Exception: key = area_key(msg.get("repo") or fleet.area_of(msg))
    if not key: return []
    try: rows = [r for r in fleet.active_work(skip=tid) if any(area_key(i.get("area")) == key for i in r.get("items") or [])]
    except Exception: return []
    return [l[:200] for l in fleet.work_text(rows[:5]).splitlines() if l.strip()]

_memory = None
def memory_loader():
    """The device's session memory loader (claude-memory-loader.py) as a module, for its scope rules; None if missing."""
    global _memory
    if _memory is None:
        _memory = False
        for f in (Path.home() / ".switchboard" / "tools" / "claude-memory-loader.py", HERE / "claude-memory-loader.py"):
            try:
                spec = importlib.util.spec_from_file_location("claude_memory_loader", f); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); _memory = m; break
            except Exception: continue
    return _memory or None

def memory_text(cwd, L=None):
    """## Memory: the memory notes that govern a run in CWD, as paths with a hint, not their text (agent runs no longer
    get the memory-loader hooks). Resolved like the loader: the vault from ~/.switchboard/registry.json, the device note,
    the global MEMORY.md and every scoped MEMORY.md from the vault down to CWD. '' when the registry cannot be read."""
    L = L or memory_loader()
    try: vault, scoped, device = L.vault_root(), L.scope_files(cwd), L.device_file()
    except Exception: return ""
    if not vault: return ""
    glob = vault / "MEMORY.md" if (vault / "MEMORY.md").is_file() else L.GLOBAL
    notes = ([(device, "this device's paths, tools, hosts and limits")] if device and device.is_file() else []) + [(glob, "the owner's rules for every task: replies, autonomy, memory")]
    for p in scoped:
        if (device and p == device) or Path(p).resolve() == Path(glob).resolve(): continue
        try: where = Path(p).resolve().parent.relative_to(vault).as_posix(); p = Path(p).resolve()  # the vault file, not the workspace link to it
        except ValueError: where = str(Path(p).parent)
        notes.append((p, f"standing rules for work under {where or 'the vault'}"))
    def text(short):
        def name(p):
            try: return Path(p).resolve().relative_to(vault).as_posix() if short else str(p)
            except ValueError: return str(p)
        return "\n".join(["## Memory", f"Vault: {vault}", *[f"- {name(p)}: {hint}" for p, hint in notes],
                          "Read a note before work it governs; read MEMORY.md before writing to the vault."])
    return text(False) if len(text(False)) <= 900 else text(True)[:900]  # long paths: relative to the vault line

WORK = {}  # message id -> the other-work lines its prompt showed, for the thread record

def build_prompt(mail, agent, info, msg, agents, me, cwd=None):
    role_file = mail.mail_root() / "roles" / f"{agent}.md"
    role = role_file.read_text(encoding="utf-8-sig").strip() if role_file.is_file() else ""
    sender = msg.get("from", "?")
    sinfo = agents.get(sender, {})
    tid = msg.get("thread") or msg.get("id")
    cwd = cwd or (dev_root() / str(msg["repo"]) if msg.get("repo") else dev_root())
    lines = [f"You are the mail agent \"{agent}\" on {me}" + (f" ({info['description']})" if info.get("description") else "")
             + f". You are running unattended to handle one message from {'the owner (a person)' if sinfo.get('kind') == 'human' else 'another agent'}; nobody can answer questions "
             "during this run. Never publish, release or deploy unless your role or the project's own rules say to, and never start Codex or Cursor yourself.", ""]
    if role: lines += ["## Your role", role, ""]
    lines += ["## Message",
              f"From: {sender}" + (f" ({sinfo.get('kind', '?')}" + (f", {sinfo['description']}" if sinfo.get("description") else "") + ")" if sinfo else ""),
              f"Subject: {msg.get('subject', '')}",
              f"Thread id: {msg.get('thread')}",
              f"Message id: {msg.get('id')}"]
    if msg.get("repo"): lines.append(f"Repo folder: {msg['repo']}")
    lines.append("Reply expected: " + ("yes" if msg.get("needs_reply") else "no"))
    lines += ["", str(msg.get("body", "")), ""]
    brief = thread_brief(mail, msg)
    last = (thread_record(tid).get("builder") or {}).get("mid") if agent == "builder" else None
    diff = queue_root() / "attachments" / str(last) / "changes.diff" if last else None
    if diff and diff.is_file(): brief = (brief + "\n" if brief else "") + f"Last build's measured diff in this thread: {diff}"
    if brief: lines += ["## Thread so far", brief, ""]
    pad = notepad_text(tid)
    if pad: lines += ["## The owner's notepad for this thread", "NEW = no agent had read it before this run; WITHDRAWN = treat it as unsaid. If a NEW note changes your task, follow the note, or stop and say what it changes.", pad, ""]
    if agent == "orchestrator":
        try:
            import playbook as _pb
            proj = next((fleet.area_of(m) for m in mail.thread(tid) if fleet.area_of(m)), "") or "general"
            top = _pb.top(proj, 5)
            if top:
                fleet._write(_pb._p(proj, True), {"at": time.time(), "ids": [r["id"] for r in top]})  # so the owner's reaction credits what was shown
                lines += [f"## Playbook for {proj} (best first; follow unless this request says otherwise)", *[f"- {' '.join(str(r['text']).split())[:160]}" for r in top], ""]
        except Exception: pass
        try:
            used, mins = turn_spend(mail, tid, agents); cap = turn_limit()
            if used and cap: lines += ["## Spent on this request since the owner last wrote",
                               f"{used} build and review runs, {mins:.0f} minutes. " + ("There is no hard limit, but every further round costs the owner usage: finish, or report what is left." if not cap else "The limit per request is reached, so delegate nothing more here; report and ask the owner." if used >= cap else f"The limit per request is {cap}: {cap - used} left, so plan to finish inside it."), ""]
        except Exception: pass
        other = WORK[msg.get("id")] = other_work(mail, msg)
        if other: lines += ["## Other unfinished work in this repo or area (both devices)", *other, ""]
        rules = discord_rules()
        if rules and sinfo.get("kind") == "human" and len(rules) < 400: lines += ["## The owner's standing rules for Discord", rules, ""]
        same = same_repo_text(mail, msg)
        if same: lines += ["## Same repo, last 48 hours (if one already did what this asks, say so instead of doing it again)", same, ""]
        side = side_chat(tid)
        if side: lines += ["## Asked and answered without you", "Gemini answered these questions from the owner in this thread from what it could see; no agent ran. Correct anything wrong.", side, ""]
    try:
        up = [f"{d.get('label') or d.get('host')} ({d.get('host')}{', this run' if str(d.get('host', '')).lower() == str(me).lower() else ''})" for d in fleet.devices() if d.get("available")]
        if up: lines += [f"Devices up: {', '.join(up)}. A job this device cannot do goes to one that can (send_message with device set to its host).", ""]
    except Exception: pass
    mem = memory_text(cwd)
    if mem: lines += [mem, ""]
    end = ("Do what the owner asks. Your final answer is posted to them in Discord: its first sentence answers them directly. At most 3 short "
           "sentences or bullets unless they asked for detail or this reports a finished build. No recap of your steps, no restating their words, "
           "no 'tell me if you want X' (open next steps become buttons). Discord markdown: **bold** the key fact, a `##` heading only to "
           "separate sections of a long report, `-#` for a small footnote, `>` for a quote." if sinfo.get("kind") == "human" else
           "Do the work this message asks for. End with a concise result: what you did or found, what you checked, "
           f"and anything left open. Your final answer is mailed to {sender} as the reply, so make it stand on its own.")
    lines += ["## What to do", end,
              "You can message other agents with the agent_mail MCP tools (list_agents, send_message, check_inbox, "
              "read_message, ack_message, reply_message, list_devices, ask_owner) when they are available. Never wait for another automatic agent inside your turn: delegate and end, you are started again with the answer. Do not reply to "
              "or ack this message yourself: the runner does both when you finish."]
    if agent == "orchestrator":
        lines.append(f"Before ending, write {thread_file(tid, '.md')} (create it if missing, under 1,200 chars): goal, decisions the owner made, current state, open items, key file paths. Replace it, do not append history.")
        lines.append(f"This thread keeps one session. If your context is confused or you repeated a wrong claim, run `python {HERE / 'mail-runner.py'} fresh {tid}`: the next run here starts fresh from the brief.")
    return "\n".join(lines)

def continuation_prompt(mail, msg, agents, rec):
    """The short prompt of an orchestrator run that resumes the thread's last session: the new message, the owner's NEW
    notes, and the other-work lines that changed since that run."""
    tid, sender = msg.get("thread") or msg.get("id"), msg.get("from", "?")
    lines = ["Same thread as your last run; your earlier context is above.", "", "## New message",
             f"From: {sender}" + (f" ({(agents.get(sender) or {}).get('kind')})" if agents.get(sender) else ""), f"Subject: {msg.get('subject', '')}", f"Message id: {msg.get('id')}", "", str(msg.get("body", "")), ""]
    try:
        import notes as _notes
        new = [note_line(n) for n in _notes.unread(tid)]
    except Exception: new = []
    if new: lines += ["## NEW notes from the owner in this thread", *new, ""]
    other = WORK[msg.get("id")] = other_work(mail, msg)
    changed = [l for l in other if l not in (rec.get("work") or [])]
    if changed: lines += ["## Other unfinished work that changed since your last run", *changed, ""]
    lines.append(f"Handle it as before: the runner replies for you when you finish. Before ending, replace {thread_file(tid, '.md')} with the updated brief.")
    return "\n".join(lines)

def thread_record(tid):
    try: d = json.loads(thread_file(tid, ".json").read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}
    return d if isinstance(d, dict) else {}

def rel_cwd(cwd):
    """A run's folder relative to the dev root (the same on every device), else the absolute path."""
    try: return Path(cwd).resolve().relative_to(dev_root().resolve()).as_posix()
    except (ValueError, OSError): return str(cwd)

def can_resume(rec, cwd, now=None, busy_for=3600):
    """Sessions continue: no time or size limit starts a new one (auto-compact handles heavy context). Resume the session on record when it ran in the same folder, the thread is not flagged fresh, and no run
    of it is in flight."""
    now = time.time() if now is None else now
    try:
        same = rec.get("rel") == rel_cwd(cwd) if rec.get("rel") is not None else os.path.normcase(str(rec.get("cwd") or "")) == os.path.normcase(str(cwd))
        return bool(rec.get("session")) and same and not rec.get("fresh") and not (rec.get("busy") and now - float(rec.get("busy_at") or 0) < busy_for)
    except (TypeError, ValueError): return False

def write_record(mail, tid, **fields):
    """Merge FIELDS into Queue/threads/<thread>.json; a None value removes that key."""
    rec = thread_record(tid); rec.update(fields)
    mail.write_json_atomic(thread_file(tid, ".json"), {k: v for k, v in rec.items() if v is not None})

FRESH_RE = re.compile(r"\b(start (?:fresh|over|a new session)|fresh (?:start|session))\b", re.I)

def set_fresh(mail, tid):
    """The next run in this thread starts a fresh session from the thread brief (its context is confused, or the owner asked)."""
    write_record(mail, tid, fresh=True, builder=None)

def session_file(sid, cwd): return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd)) / f"{sid}.jsonl"

def share_session(sid, cwd, tid, agent):
    """Copy this device's session file to Queue/threads/<thread>.<agent>.jsonl, so the other device can continue it."""
    src = session_file(sid, cwd)
    if not src.is_file(): src = next((Path.home() / ".claude" / "projects").glob(f"*/{sid}.jsonl"), None)
    if not src: return
    dst = thread_file(tid, f".{agent}.jsonl"); tmp = dst.with_name("." + dst.name + ".tmp")
    dst.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(src, tmp); os.replace(tmp, dst)

def bring_session(rec, cwd, tid, agent):
    """Make the recorded session resumable here. On its own host it is local; on the other device the synced copy goes
    into this device's project folder for CWD (a copied session resumes on macOS and Windows alike).
    Returns '' when it can be resumed, else why not."""
    if str(rec.get("host") or "").lower() == hostname().lower(): return ""
    src = thread_file(tid, f".{agent}.jsonl")
    if not src.is_file(): return f"its session file from {rec.get('host')} has not reached this device"
    try: dst = session_file(rec["session"], cwd); dst.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(src, dst); return ""
    except OSError as e: return f"the session file could not be copied here ({e})"

REPORT_RE = re.compile(r"(?mi)^REPORT:\s*direct\s*$")

def direct_report(msg, said, block):
    """True when the builder's result may go straight to the thread's starter: the task says `REPORT: direct`, and at
    least one plan check ran and none failed (SETUP is allowed). Anything else goes to the orchestrator as before."""
    m = re.search(r"Checks from the plan, run by the runner: (\d+) of (\d+) passed", block)
    return bool(REPORT_RE.search(str(msg.get("body") or "")) and m and int(m.group(2)) > 0 and "\n- FAIL" not in block)

def note_latest(tid, said):
    """Put the direct result into the thread brief as its "Latest result" line, replacing an older one."""
    f = thread_file(tid, ".md")
    try: old = f.read_text(encoding="utf-8")
    except OSError: old = ""
    keep = [l for l in old.splitlines() if not l.startswith("Latest result")]
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(keep + [f"Latest result ({time.strftime('%m-%d %H:%M', time.gmtime())} UTC, builder, reported directly): {first_sentence(said, 300)}"]) + "\n", encoding="utf-8")

PROMPT_BUDGET = {"continuation": 2000, "orchestrator": 8000, "builder task": 6000}
def budget_check(kind, n):
    """Log, never cut, a prompt over its budget."""
    if n > PROMPT_BUDGET.get(kind, 1e9): say(f"prompt budget: {kind} prompt is {n} chars, over its {PROMPT_BUDGET[kind]}")

def builder_continuation(msg):
    """The prompt of a builder run that continues its last session in this thread: the new task alone."""
    return "\n".join(["Same work as your last run in this thread.", "", "## New task", f"From: {msg.get('from', '?')}", f"Subject: {msg.get('subject', '')}",
                      f"Message id: {msg.get('id')}", "", str(msg.get("body", "")), "",
                      "End with a concise result: what you did or found, what you checked, and anything left open. The runner replies for you; do not reply or ack yourself."])


# ---- one message ----

_ENDED = {}  # events file -> ((mtime, size), refs whose run ended)
def run_ended(q, host, mid):
    """True when host's event log in Queue/events says run mid finished (done, failed or cutoff): its shared claim files may still be syncing."""
    try:
        files = sorted(Path(q, "events").glob(f"{host}-*.jsonl"), key=lambda p: p.name)[-2:]
        for f in files:
            st = f.stat(); key = (st.st_mtime, st.st_size); hit = _ENDED.get(str(f))
            if not hit or hit[0] != key:
                refs = set()
                for line in f.read_text(encoding="utf-8").splitlines():
                    try: e = json.loads(line)
                    except ValueError: continue
                    if isinstance(e, dict) and e.get("kind") in ("done", "failed", "cutoff") and e.get("ref"): refs.add(e["ref"])
                hit = _ENDED[str(f)] = (key, refs)
            if mid in hit[1]: return True
    except (OSError, ValueError): pass
    return False

class Runner:
    def __init__(self, a, mail):
        self.a, self.mail = a, mail
        self.me, self.root, self.q = hostname(), dev_root(), queue_root()
        self.state = Path(a.state_file).expanduser() if a.state_file else Path.home() / ".switchboard" / "mail-runner-state.json"
        self.claims = self.state.parent / "mail-runner-claims"
        self.by = f"mail-runner@{self.me}"
        self.noted, self.spent, self.sim, self.failed = set(), set(), 0, 0
        self.age0 = {}
        self.only = {x.strip().lower() for x in (a.only or "").split(",") if x.strip()}
        self.running, self.cancel, self.serial = {}, threading.Event(), bool(a.once or a.dry_run)
        self.draining = False  # new code is on disk: finish what runs, start only the owner's own messages, then exit so the service restarts
        self.claude_mark = None  # this process's own copy of the last Claude block it wrote (the shared file is the main one)
        self.opening, self.open_lock, self.open_at, self.sweep_at = set(), threading.Lock(), 0.0, 0.0

    def limit(self):
        """Runs this process may hold at once: the flag, else Queue/mail/runner.json (read live), else the default."""
        n = self.a.max_concurrent
        if n is None:
            try: n = json.loads((self.q / "mail" / "runner.json").read_text(encoding="utf-8")).get("max_concurrent")
            except (OSError, ValueError, AttributeError): n = None
            except Exception: n = None  # e.g. the file holds a list
        return clean_limit(n)

    def locks(self):
        """What makes a message wait, from Queue/mail/runner.json "locks" (read live): "all" = one run per thread and one
        writer per repo or area; "repo" = only one run per repo on a device and never two
        devices writing the same repo or named area at once (the file sync would drop one side's edits); "none" = nothing."""
        try: v = json.loads((self.q / "mail" / "runner.json").read_text(encoding="utf-8")).get("locks")
        except (OSError, ValueError, AttributeError): v = None
        return v if v in ("all", "repo", "none") else DEFAULT_LOCKS

    def engines(self, agent):
        """Engines to try for this agent, best first: only ones installed here, Codex only with quota left. While
        Claude's usage limit is reached (claude_blocked) Claude is skipped and Codex may take any agent's run, whatever
        routing.codex_builds says; an empty list then means park the message until the limit resets."""
        pol = fleet.engines_policy(); want = list(pol.get(agent) or pol["*"]); q = fleet.codex_best(); out = []
        blocked = claude_blocked(self.claude_mark) > 0
        reviews_only = agent != "reviewer" and routing_cfg().get("codex_builds") is not True and not blocked  # until runner.json turns Codex builds on: read-only runs only
        for e in want + (["codex"] if blocked and "codex" not in want else []):
            if e == "codex" and (reviews_only or not (fleet.codex_usable(q, pol) and fleet.codex_exe())): continue
            if e == "claude" and (not find_claude() or blocked): continue
            out.append(e)
        if blocked: return out
        return out or (["claude"] if find_claude() else want[:1])

    def block_claude(self, text, out=""):
        """A Claude run ended on a usage limit: mark Claude unavailable for every runner on both devices until it resets
        (Queue/usage/claude-blocked.json). Returns (the limit's line, until)."""
        line = next((l.strip() for l in str(text or "").splitlines() if LIMIT_RE.search(l)), str(text or "").strip())[:300]
        until, t = limit_reset(text, out), time.time()
        self.claude_mark = {"until": until, "reason": line, "host": self.me, "at": t}
        try: self.mail.write_json_atomic(block_file(), self.claude_mark)
        except Exception as e: say(f"claude block not shared: {e}")
        say(f"claude reports a usage limit ({line[:120]}); not starting Claude runs until {time.strftime('%Y-%m-%d %H:%M', time.localtime(until))}")
        return line, until

    def park(self, agent, msg, reason="", until=0.0, **more):
        """Leave a message in the inbox while Claude's limit is reached and no other engine can take it: no ack, no
        failure, not spent. One "paused" event per message and block; it starts again by itself once the limit resets."""
        mid = msg["id"]; until = until or claude_blocked(self.claude_mark) or time.time() + 3600
        if not reason:
            try: reason = str(json.loads(block_file().read_text(encoding="utf-8")).get("reason") or "")
            except (OSError, ValueError, AttributeError): reason = str((self.claude_mark or {}).get("reason") or "")
        mark = self.claims / (re.sub(r"[^\w.-]", "_", mid) + ".parked")
        try: seen = mark.read_text(encoding="utf-8").strip()
        except OSError: seen = ""
        if seen == f"{until:.0f}": return
        detail = paused_text(reason, until)
        say(f"paused {mid} for {agent}: {detail} It stays in the inbox.")
        fleet.event("paused", msg.get("subject", ""), detail=detail, agent=agent, thread=msg.get("thread", ""), repo=msg.get("repo") or "", ref=mid, **more)
        try: self.claims.mkdir(parents=True, exist_ok=True); mark.write_text(f"{until:.0f}", encoding="utf-8")
        except OSError: pass

    def beat_extra(self):
        hour, day = counts(load_runs(self.state))
        return {"runs_hour": hour, "runs_day": day, "cap_hour": self.a.max_per_hour, "cap_day": self.a.max_per_day, "stopped": (self.q / "STOP").exists()}

    def once(self, key, msg):
        """Log a repeating idle condition only when it first appears."""
        if key not in self.noted: self.noted.add(key); say(msg)

    def stopped(self):
        if (self.q / "STOP").exists():
            self.once("stop", "STOP present, idle"); return True
        self.noted.discard("stop"); return False

    def notify(self, agent, msg, what, detail):
        """Tell agent owner about a message that was not handled normally."""
        try:
            agents = self.mail.load_agents()
            to = self.mail.resolve_agent(NOTIFY, agents)
            if to == agent: return
            self.mail.send(agent, to, f"[mail-runner] {what}: {msg.get('subject', '')}",
                           f"Runner on {self.me} did not complete message {msg.get('id')} for agent {agent}.\n"
                           f"From: {msg.get('from')}\nSubject: {msg.get('subject')}\nThread: {msg.get('thread')}\n"
                           f"Hops: {msg.get('hops')}\n\n{detail}", reply_to=msg.get("id"))
        except self.mail.MailError as e:
            say(f"could not notify {NOTIFY}: {e}")

    def fail(self, agent, msg, reason, **more):
        timed = reason.strip().startswith(("timeout", "stalled"))  # a stopped run is not a failed build: the edits made so far are kept
        text = ("STALLED: " if timed else "FAILED: ") + _clip(reason.strip() or "unknown error", 1500) + ("; nothing failed, the run went quiet and was stopped; the work so far and its session are kept, so the next round continues it" if timed else "")
        self.failed += 1
        say(f"failed {msg['id']} for {agent}: {text.splitlines()[0][:200]}")
        fleet.event("cutoff" if timed else "failed", msg.get("subject", ""), detail=text, agent=agent, thread=msg.get("thread", ""), repo=msg.get("repo") or "", ref=msg["id"], **more)
        try: self.mail.ack(agent, msg["id"], result=text, by=self.by)
        except self.mail.MailError as e: say(f"could not ack {msg['id']}: {e}")
        self.notify(agent, msg, "STALLED" if timed else "FAILED", text)
        sender = msg.get("from", "")
        if msg.get("needs_reply") and sender.lower() != NOTIFY:  # the sender is waiting; owner already got the notice
            try: self.mail.send(agent, sender, "Re: " + str(msg.get("subject", "")), text, reply_to=msg["id"])
            except self.mail.MailError as e: say(f"could not tell {sender}: {e}")

    def repo_key(self, msg, cwd, agent=""):
        """What one writer at a time is keyed on: the mail's repo, else its AREA line, else the folder an agent is bound
        to, else the whole workspace for an agent that writes files. '' for agents that only read or delegate."""
        if msg.get("repo"): return fleet.area_of(msg)
        if fleet.area_of(msg): return area_key(fleet.area_of(msg))
        try:
            Path(cwd).resolve().relative_to(HERE.resolve()); return TOOLKIT  # started inside the toolkit folder: the same writer key as its areas
        except (ValueError, OSError): pass
        if agent and agent not in READERS and not self.bound(cwd): return "workspace"
        try: rel = Path(cwd).resolve().relative_to(self.root.resolve())
        except ValueError: return ""
        return rel.as_posix().lower() if rel.parts else ""

    def bound(self, cwd):
        try: return bool(Path(cwd).resolve().relative_to(self.root.resolve()).parts)
        except ValueError: return True

    def own_claim(self, name):
        """This device's shared claim file for NAME, in its own folder: only this device writes or deletes it."""
        return self.q / "mail" / "claims" / re.sub(r"[^\w.-]", "_", self.me) / (re.sub(r"[^\w.-]", "_", name) + ".json")

    def shared_claims(self, mid, thread, area):
        """The synced claim files one run writes: this device's own folder, plus the flat copies older code reads."""
        own = [self.own_claim(mid), self.own_claim(f"thread-{thread}")] + ([self.own_claim(f"area-{area}")] if area else [])
        return own + ([f.parent.parent / f.name for f in own] if LEGACY_FLAT_CLAIMS else [])

    def other_claims(self, pattern):
        """Shared claim files matching PATTERN that other devices wrote: their own folders, plus flat files from older code."""
        base, mine = self.q / "mail" / "claims", re.sub(r"[^\w.-]", "_", self.me).lower()
        try: dirs = [d for d in base.iterdir() if d.is_dir() and d.name.lower() != mine]
        except OSError: dirs = []
        return [p for d in [base, *dirs] for p in d.glob(pattern)]

    def release(self, held):
        for p in held:
            try: p.unlink()
            except OSError: pass

    def drop(self, held, shared):
        """Give back everything a run held: the claims on this device and the notes that tell the other device."""
        self.release(held)
        for f in shared:
            try:
                if f.parent.name == "claims" and str(json.loads(f.read_text(encoding="utf-8")).get("host", "")).lower() != self.me.lower(): continue  # a flat copy another device rewrote is theirs
                f.unlink()
            except (OSError, ValueError): pass

    def loose_busy(self, repo, stale):
        """Work outside a repo names an area, or takes the whole workspace when it names none. The workspace and a named
        area never run together, on this device or the other, since unnamed work may touch any of those files."""
        mine = re.sub(r"[^\w.-]", "_", f"area-{repo}")
        clash = lambda stem: stem != mine and (repo == "workspace" or stem == "area-workspace")
        for p in self.claims.glob("area-*.claim"):
            try:
                if clash(p.stem) and time.time() <= claim_until(p, stale): return p.stem
            except OSError: pass
        for p in self.other_claims("area-*.json"):
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
                if clash(p.stem) and c.get("loose") and c.get("host", "").lower() != self.me.lower() and time.time() - float(c.get("at", 0)) < stale and not (c.get("mid") and run_ended(self.q, c["host"], c["mid"])): return f"{p.stem} on {c.get('host')}"
            except (OSError, ValueError): pass
        return ""

    def take(self, mid, thread, repo, stale, direct=False, loose=False, extra=False):
        """The claims a run needs, all or none, across every runner process on this device. None when one is held; the
        message then simply stays in the inbox for a later pass, so nothing about it changes."""
        mode = self.locks()
        wanted = [(mid, "another runner on this device holds it")]
        # What the owner types never waits, not even behind a run in its own thread. Do not remove this exemption in a
        # review fix; a second orchestrator run in a thread is the accepted cost.
        if mode == "all" and not direct: wanted.append((f"thread-{thread}", f"thread {thread} already has a run"))
        if mode == "all" and repo and loose:
            other = self.loose_busy(repo, stale)
            if other: self.once(("busy", mid, "loose"), f"hold {mid}: {other} is writing in the shared workspace"); return None
        if repo and (mode == "all" or (mode == "repo" and not loose)):
            wanted.append((f"area-{repo}" if loose else f"repo-{repo}", f"{'area' if loose else 'repo'} {repo} already has a run"))
        held = []
        for name, why in wanted:
            p = claim(self.claims, name, stale)
            if not p:
                self.release(held); self.once(("busy", mid, name), f"hold {mid}: {why}"); return None
            held.append(p)
        if extra:  # past this process's own limit: only on one of the device's human slots
            slot = next((p for p in (claim(self.claims, f"human-slot-{i}", stale) for i in range(HUMAN_EXTRA)) if p), None)
            if not slot:
                self.release(held); self.once(("busy", mid, "slot"), f"hold {mid}: all {HUMAN_EXTRA} extra slots for the owner's own messages are busy on this device"); return None
            held.append(slot)
        if len(live_claims(self.claims, stale)) > DEVICE_MAX:  # counted after taking ours, so two processes cannot both squeeze in
            self.release(held); self.once(("busy", mid, "device"), f"hold {mid}: this device already has {DEVICE_MAX} runs"); return None
        remote = ([f"thread-{thread}"] if mode == "all" and not direct else []) + ([f"area-{repo}"] if repo and (mode == "all" or (mode == "repo" and repo != "workspace")) else [])
        for name in remote:  # the same claims, held by the other device
            for p in self.other_claims(re.sub(r"[^\w.-]", "_", name) + ".json"):
                try:
                    c = json.loads(p.read_text(encoding="utf-8"))
                    if c.get("host", "").lower() != self.me.lower() and time.time() - float(c.get("at", 0)) < stale and not (c.get("mid") and run_ended(self.q, c["host"], c["mid"])):
                        self.release(held); self.once(("busy", mid, name), f"hold {mid}: {name} has a run on {c.get('host')}"); return None
                except (OSError, ValueError): pass
        return held

    def prepare(self, agent, info, msg, agents, exe, extra=False):
        """Everything that decides whether a message starts, always on the main thread, so the caps and claims are
        checked one message at a time. Returns a job to run, 'capped' when the run caps stop the pass, or None."""
        a, mail, mid = self.a, self.mail, msg["id"]
        hops = int(msg.get("hops") or 0)
        if a.max_hops and hops >= a.max_hops:
            if a.dry_run: say(f"dry-run: would ack {mid} for {agent} with 'hop limit' (hops={hops}) and notify {NOTIFY}"); return
            say(f"hop limit: {mid} for {agent} has hops={hops}, not run")
            mail.ack(agent, mid, result="hop limit", by=self.by)
            fleet.event("cutoff", msg.get("subject", ""), detail=f"stopped at the {a.max_hops}-hop limit", agent=agent, thread=msg.get("thread", ""), repo=msg.get("repo") or "", ref=mid)
            self.notify(agent, msg, "hop limit", f"hops={hops} reached the limit of {a.max_hops}; the message was acked without running.")
            return
        hour, day = counts(load_runs(self.state))
        hour, day = hour + self.sim, day + self.sim
        if (a.max_per_hour and hour >= a.max_per_hour) or (a.max_per_day and day >= a.max_per_day):
            which = f"hourly cap {hour}/{a.max_per_hour}" if hour >= a.max_per_hour else f"daily cap {day}/{a.max_per_day}"
            if "cap" not in self.noted and not a.dry_run:
                fleet.event("waiting", f"{which} reached on {self.me}; waiting for it to clear or for another device", agent=agent, thread=msg.get("thread", ""))
            self.once("cap", f"{which} reached, leaving {mid} for {agent} and later mail in the inbox")
            return "capped"
        self.noted.discard("cap")
        if not a.dry_run and claude_blocked(self.claude_mark) and not self.engines(agent):
            return self.park(agent, msg)  # Claude's limit is reached and Codex cannot take it: wait in the inbox, start nothing
        if msg.get("repo"): cwd = self.root / str(msg["repo"])
        elif info.get("cwd"):
            cwd = Path(str(info["cwd"])).expanduser()
            if not cwd.is_absolute(): cwd = self.root / cwd
        else: cwd = self.root
        tid0 = msg.get("thread") or mid
        if agent == "orchestrator" and is_person(msg, agents) and FRESH_RE.search(f"{msg.get('subject', '')}\n{msg.get('body', '')}") and not a.dry_run:
            try: set_fresh(mail, tid0); say(f"{mid}: the owner asked for a fresh start; thread {tid0} starts a new session")
            except Exception as e: say(f"fresh flag not written for {mid}: {e}")
        rec = thread_record(tid0) if agent == "orchestrator" else {}
        resume = ""
        if rec and can_resume(rec, cwd):  # sessions continue: the thread's own session, here or copied from the other device
            why = bring_session(rec, cwd, tid0, "orchestrator")
            if why: say(f"fresh run for {mid}: session {str(rec.get('session'))[:8]} cannot continue here: {why}")
            else: resume = str(rec.get("session"))
        elif rec.get("session"): say(f"fresh run for {mid}: " + ("the thread is flagged fresh" if rec.get("fresh") else "a run of its session is in flight" if rec.get("busy") else "its session ran in another folder"))
        prompt = continuation_prompt(mail, msg, agents, rec) if resume else build_prompt(mail, agent, info, msg, agents, self.me, cwd)
        if resume: budget_check("continuation", len(prompt))
        elif agent == "orchestrator": budget_check("orchestrator", len(prompt))
        elif agent == "builder": budget_check("builder task", len(str(msg.get("body") or "")))
        via_stdin = use_stdin(exe or "claude", prompt, a.prompt_via)
        shown = build_cmd(exe or "claude", f"<prompt, {len(prompt)} chars>", a.permission_mode, via_stdin,
                          (["--mcp-config", "<temp mcp.json>", "--allowedTools", "mcp__agent_mail"] if a.mcp else []) + lean_launch(agent, in_repo=bool(msg.get("repo")))[0])
        if a.dry_run:
            self.sim += 1
            problem = "" if cwd.is_dir() else f"  (would FAIL: folder missing)"
            say(f"dry-run: {agent} <- {msg.get('from')} {mid} \"{msg.get('subject')}\" cwd={cwd}{problem}")
            say("dry-run: " + " ".join(shown) + ("  (prompt on stdin)" if via_stdin else ""))
            print("\n".join("    | " + l for l in prompt.splitlines()), flush=True)
            return
        if not cwd.is_dir():
            return self.fail(agent, msg, f"working folder does not exist on {self.me}: {cwd}")
        timeout_s = a.timeout_minutes * 60
        thread = str(msg.get("thread") or mid)
        direct = is_person(msg, agents)
        if agent == "reviewer" and not direct:
            try: want, why = review_wanted(mail, msg, agents)
            except Exception as e: want, why = True, f"review rule failed ({e})"
            if not want:  # only big jobs get a review
                say(f"review skipped: {mid}, {why}")
                try: mail.reply(agent, mid, f"Review skipped: {why}. The owner's rule is that only big jobs get a review. Treat the build as done: tell the owner the result now, and do not send this for review again.", by=self.by)
                except Exception as e: say(f"review skip: could not reply for {mid}: {e}")
                return
        cap = turn_limit()
        if cap and not direct and agent != "orchestrator":
            used, mins = turn_spend(mail, thread, agents)
            if used >= cap:  # the loop this stops: build, review, fix, review again, with no end
                say(f"turn budget: {mid} for {agent} not run, thread {thread} has used {used} build and review runs ({mins:.0f} min) since the owner last wrote")
                try: mail.reply(agent, mid, f"Not run. This request has used {used} build and review runs ({mins:.0f} minutes) since the owner last wrote in it, and the limit per request is {cap}. "
                                "Stop delegating in this thread. Tell the owner in a few plain sentences what is finished and saved, what is still open, and ask them with ask_owner (Keep going / Stop here) whether to spend more. Their answer renews the budget.", by=self.by)
                except Exception as e: say(f"turn budget: could not reply for {mid}: {e}")
                return
        area = self.repo_key(msg, cwd, agent)
        loose = bool(area) and not msg.get("repo") and (not self.bound(cwd) or area == TOOLKIT)
        held = self.take(mid, thread, area, timeout_s + 300, direct, loose, extra)
        if not held: return
        shared = []
        try:  # from here the claims are ours: whatever goes wrong before the job is handed over, give them back
            try: _, box = mail.get(agent, mid)
            except mail.MailError: box = None
            if box != "inbox":
                self.release(held); say(f"skip {mid} for {agent}: no longer in the inbox"); return
            record_run(mail, self.state)
            shared = self.shared_claims(mid, thread, area)
            for f in shared:
                try: mail.write_json_atomic(f, {"host": self.me, "at": time.time(), "mid": mid, **({"loose": True} if loose and f.name.startswith("area-") else {})})
                except Exception: pass
            fleet.event("started", msg.get("subject", ""), agent=agent, thread=msg.get("thread", ""), repo=msg.get("repo") or "", ref=mid)
            try: (self.claims / (re.sub(r"[^\w.-]", "_", mid) + ".parked")).unlink(missing_ok=True)  # a later pause is news again
            except OSError: pass
            if resume:  # no second run may resume this session while it runs
                try: write_record(mail, thread, busy=mid, busy_at=time.time())
                except Exception: pass
        except BaseException:
            self.drop(held, shared); raise
        say(f"start {mid} for {agent} from {msg.get('from')} \"{msg.get('subject')}\" cwd={cwd} "
            f"mode={a.permission_mode} timeout={a.timeout_minutes:g}m run {hour + 1}/{a.max_per_hour} this hour prompt={len(prompt)}" + (f" resume={resume[:8]}" if resume else ""))
        return {"relay": not direct, "agent": agent, "msg": msg, "exe": exe, "cwd": cwd, "prompt": prompt, "via_stdin": via_stdin, "timeout_s": timeout_s, "held": held, "shared": shared,
                "resume": resume, "info": info, "agents": agents, "work": WORK.pop(mid, []), "stale": timeout_s + 300}

    def execute(self, job):
        """Run one prepared message to its end, usually on a worker thread. Claims are released however it ends."""
        a, mail = self.a, self.mail
        agent, msg, exe, cwd, prompt, via_stdin, timeout_s = (job[k] for k in ("agent", "msg", "exe", "cwd", "prompt", "via_stdin", "timeout_s"))
        mid = msg["id"]
        alive = threading.Event()
        def keep_claims():  # with no time limit, a long run's claims must not look expired to other runners
            while not alive.wait(60):
                t = time.time()
                for p in job["held"]:
                    try: p.write_text(f"{os.getpid()} {now()} {t + job.get('stale', timeout_s + 300):.0f}\n", encoding="utf-8")
                    except OSError: pass
                for f in job["shared"]:
                    try: mail.write_json_atomic(f, {**json.loads(f.read_text(encoding="utf-8")), "at": t})
                    except Exception: pass
        threading.Thread(target=keep_claims, name=f"claims-{mid}", daemon=True).start()
        try:
            started = time.time(); cq0 = fleet.codex_quota()
            before = None
            if agent == "builder":
                try: before = snapshot(cwd)
                except Exception as e: say(f"snapshot failed for {mid}: {e}")
            engines = self.engines(agent)
            if not engines: return self.park(agent, msg)  # the limit was reached after this message was picked
            try: job["route"] = choose(mail, agent, msg, bool(job.get("relay")), mail.load_agents())
            except Exception as e: job["route"] = None; say(f"model check failed for {mid}, using the defaults: {e}")
            rt = job.get("route") or {}
            req = {"route": rt.get("key"), "prior": rt.get("prior"), "req": rt.get("req")}  # kept before a fallback rewrites the route
            if rt.get("engine") in engines: engines = [rt["engine"]] + [e for e in engines if e != rt["engine"]]
            elif rt.get("engine") == "codex" and (rt.get("alt") or {}).get("engine") == "claude": rt = job["route"] = {**rt, **rt["alt"], "why": rt["why"] + "; Codex is not available here", "alt": None}
            pony, pwhy = rt.get("ponytail") or "off", rt.get("ponytail_why") or "routing is off"
            if rt.get("ponytail_ask") and "claude" in engines: pony, pwhy = ask_ponytail(exe, *rt["ponytail_text"], pony)
            say(f"ponytail for {mid} ({agent}): {pony} ({pwhy})")
            pony_line = f"\n\nUse the ponytail skill at level {pony} for this work." if pony != "off" else ""
            def setting(engine):  # the model and effort this engine runs with: the first choice, or the stand-in for the other family
                if rt.get("engine") == engine: return rt
                return rt.get("alt") if (rt.get("alt") or {}).get("engine") == engine else {}
            rc = out = err = None; used = None
            alone = ("\n\nYou have no mail tools in this run and no other agent can take this work now. Do the whole request yourself in this "
                     "one run: plan it, make the change, check that it works. Your final message is posted to the owner as the answer." if agent == "orchestrator" else
                     "\n\nYou have no mail tools in this run. If the work needs another agent, say so in your result.")
            def fresh():  # every run but the first Claude one gets the full prompt: a continuation only works inside its own session
                nonlocal prompt, via_stdin
                if job.get("resume"):
                    job["resume"] = ""; prompt = build_prompt(mail, agent, job.get("info") or {}, msg, job.get("agents") or {}, self.me, cwd); via_stdin = use_stdin(exe or "claude", prompt, a.prompt_via)
            tid = msg.get("thread") or mid
            if agent == "builder" and "claude" in engines:  # the same work in this thread continues its session while the route keeps the model
                trec = thread_record(tid); b = trec.get("builder") or {}; want = (setting("claude") or {}).get("model", "")
                if b.get("session") and not trec.get("fresh") and can_resume(b, cwd) and b.get("area") == self.repo_key(msg, cwd, agent) and b.get("model") == want:
                    why = bring_session(b, cwd, tid, "builder")
                    if why: say(f"fresh builder run for {mid}: {why}")
                    else: job["resume"] = b["session"]; prompt = builder_continuation(msg); budget_check("continuation", len(prompt)); via_stdin = use_stdin(exe or "claude", prompt, a.prompt_via); say(f"builder {mid} continues session {b['session'][:8]} prompt={len(prompt)}")
                elif b.get("session") and b.get("model") != want: say(f"fresh builder run for {mid}: the route moved from {b.get('model') or 'the default'} to {want or 'the default'} (an escalation starts from the plan and the last diff)")
            for engine in engines:  # may grow by "codex" when Claude reports its limit mid-way
                used, limited = engine, None
                try:
                    if engine == "codex":
                        cs = setting("codex") or {}; fresh()
                        say(f"engine codex for {mid} ({agent}): {cs.get('model') or 'default model'} at {cs.get('effort') or 'default effort'}" + (f" ({rt.get('why')})" if rt.get("engine") == "codex" else " (stand-in)"))
                        rc, text, err = run_codex(prompt + alone + pony_line, cwd, timeout_s, writes=agent != "reviewer", model=cs.get("model", ""), effort=cs.get("effort", ""), parent=mid, claims=self.claims, cancel=self.cancel)
                        out = json.dumps({"result": text, "is_error": rc != 0 or not text.strip()})
                    else:
                        if job.get("relay"): RELAY.add(mid)
                        cl = setting("claude")
                        if cl: ROUTE[mid] = cl; say(f"model for {mid}: {cl['model']} at {cl['effort']} ({rt.get('why') if rt.get('engine') == 'claude' else 'stand-in for Codex'})")
                        rc, out, err = run_claude(exe, prompt, a.permission_mode, cwd, timeout_s, agent, via_stdin, a.mcp, parent=mid, claims=self.claims, cancel=self.cancel, ponytail=pony, **({"resume": job["resume"]} if job.get("resume") else {}))
                        d1, r1 = parse_output(out or "")
                        if job.get("resume") and rc is not None and (rc != 0 or (d1 or {}).get("is_error") or not (r1 or "").strip()):
                            say(f"resume of {job['resume'][:8]} did not finish {mid} ({(r1 or err or 'no output').strip()[:120]}); a fresh run takes it"); fresh()
                            if not LIMIT_RE.search((r1 or "") + (err or "")):
                                rc, out, err = run_claude(exe, prompt, a.permission_mode, cwd, timeout_s, agent, via_stdin, a.mcp, parent=mid, claims=self.claims, cancel=self.cancel, ponytail=pony)
                                d1, r1 = parse_output(out or "")
                        if (str((cl or {}).get("model", "")).startswith("claude-fable") and rc is not None
                                and (rc != 0 or (d1 or {}).get("is_error")) and LIMIT_RE.search((r1 or "") + (err or ""))):
                            # Fable has its own weekly limit. Reaching it must not stop the work or block Opus.
                            try: mail.write_json_atomic(self.q / "mail" / "fable-blocked.json", {"until": time.time() + 3 * 3600, "host": self.me})
                            except Exception: pass
                            rt = job["route"] = {"engine": "claude", "model": "claude-opus-5-5", "effort": "xhigh", "why": "Fable's limit is reached; rerun on Opus xhigh", "alt": rt.get("alt") if (rt.get("alt") or {}).get("engine") == "codex" else None}
                            ROUTE[mid] = rt; say(f"model for {mid}: Fable's limit is reached, rerunning on Opus xhigh and avoiding Fable for 3 hours")
                            rc, out, err = run_claude(exe, prompt, a.permission_mode, cwd, timeout_s, agent, via_stdin, a.mcp, parent=mid, claims=self.claims, cancel=self.cancel, ponytail=pony)
                        d2, r2 = parse_output(out or "")
                        bk = rt.get("alt") or {}
                        if (rc is not None and (rc != 0 or (d2 or {}).get("is_error") or not (r2 or "").strip()) and bk.get("engine") == "claude"
                                and rt.get("engine") == "claude" and bk.get("model") != (ROUTE.get(mid) or cl or {}).get("model") and not job.get("backup_used")
                                and not LIMIT_RE.search((r2 or "") + (err or ""))):  # the account's limit stops every model alike
                            # the chosen model did not finish: the backup from the same check takes the run once
                            job["backup_used"] = True; rt = job["route"] = {**bk, "why": f"backup after {rt.get('model')} did not finish", "alt": None}
                            ROUTE[mid] = rt; say(f"model for {mid}: {rt['model']} at {rt['effort']} (backup, the first choice did not finish)")
                            rc, out, err = run_claude(exe, prompt, a.permission_mode, cwd, timeout_s, agent, via_stdin, a.mcp, parent=mid, claims=self.claims, cancel=self.cancel, ponytail=pony)
                except OSError as e:
                    rc, out, err = 1, "", f"could not start {engine}: {e}"
                d0, r0 = parse_output(out or "")
                bad = rc is not None and (rc != 0 or (d0 or {}).get("is_error") or not (r0 or "").strip())
                if bad and engine == "claude" and LIMIT_RE.search((r0 or "") + (err or "")):
                    limited = self.block_claude(f"{r0 or ''}\n{err or ''}", out)
                    if "codex" not in engines and "codex" in self.engines(agent): engines.append("codex")
                if not bad or engine == engines[-1]: break
                say(f"{engine} did not complete {mid} ({(r0 or err or 'no output').strip()[:120]}); trying {engines[engines.index(engine) + 1]}")
            secs = int(time.time() - started)
            s = SESSIONS.pop(mid, None); ses = {"session": s[0], "cwd": s[1]} if s and used == "claude" else {}
            if agent in ("orchestrator", "builder") and ses:  # what the thread's next run continues, on either device; a stalled run's too
                try:
                    here = {"session": ses["session"], "host": self.me, "cwd": str(cwd), "rel": rel_cwd(cwd), "ended_at": time.time(), "context_tokens": context_tokens(out)}
                    if agent == "orchestrator": write_record(mail, tid, **here, work=job.get("work") or [], busy=None, busy_at=None, fresh=None)
                    else: write_record(mail, tid, builder={**here, "model": (setting("claude") or {}).get("model", ""), "area": self.repo_key(msg, cwd, agent), "mid": mid})
                    share_session(ses["session"], cwd, tid, agent)
                except Exception as e: say(f"thread record not written for {mid}: {e}")
            if limited:  # Claude's limit and nothing else could take it: not a failure, and not spent
                return self.park(agent, msg, *limited, **ses)
            if rc is None:
                return self.fail(agent, msg, f"stalled: no output for {a.timeout_minutes:g} min", **ses)
            self.spent.add(mid)  # paid for: never run this id again in this process, whatever happens below
            data, result = parse_output(out)
            if rc != 0 or (data or {}).get("is_error"):
                what = f"claude exit code {rc}" if rc != 0 else "claude reported an error"
                return self.fail(agent, msg, f"{what}: " + (result or err.strip() or "no output"), **ses)
            if not result.strip():
                return self.fail(agent, msg, "claude returned no result text" + (f": {err.strip()}" if err.strip() else ""), **ses)
            cost = (data or {}).get("total_cost_usd")
            checks, info, direct = (0, 0, 0), {}, False
            if agent == "builder":
                said = result; result = ""  # RESULT collects the runner's block; the builder's text goes in front of it below
                try: result = measured_changes(cwd, before, mid, info)
                except Exception as e: say(f"change report failed for {mid}: {e}")
                try: log_change(self.repo_key(msg, cwd, agent), msg, agent, (setting(used) or {}).get("model", "") or used, said, info)
                except Exception as e: say(f"change log not written for {mid}: {e}")
                try:
                    ctext, n, ok, su = run_plan_checks(cwd, msg.get("body"), dev_root=self.root, changed=[f[0] for f in info.get("files") or []])
                    if n: result += ctext; checks = (n, ok, su); say(f"plan checks for {mid}: {ok} of {n} passed, {su} could not run")
                except Exception as e: say(f"plan checks failed to run for {mid}: {e}")
                block = "\n".join(l for l in result.split("\n") if not (l.strip() and l.strip() in said))  # nothing the builder already said
                if len(block) > 1200: say(f"result block for {mid} is {len(block)} chars, over its 1,200 budget")
                direct = direct_report(msg, said, block)
                result = said.rstrip() + block
            try: fleet.stat({"engine": used, "agent": agent, "thread": msg.get("thread", ""), "repo": msg.get("repo") or "", "subject": str(msg.get("subject", ""))[:80], "secs": secs, "cost": cost, "checks": checks[0], "checks_passed": checks[1], "checks_setup": checks[2], "model": (setting(used) or {}).get("model", ""), "effort": (setting(used) or {}).get("effort", ""), "ponytail": pony,
                             "session": ses.get("session", ""), "cwd": str(cwd), "prompt_chars": len(prompt), "resumed": bool(job.get("resume")), "turns": (data or {}).get("num_turns"), **req, **week_fields(used, out, cq0), **{k: ((data or {}).get("usage") or {}).get(k) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}})
            except Exception: pass
            if used == "claude":
                try: save_reading(out)
                except Exception as e: say(f"plan reading not saved for {mid}: {e}")
            fleet.event("done", msg.get("subject", ""), detail=result, agent=agent, thread=msg.get("thread", ""), repo=msg.get("repo") or "", ref=mid, **ses)
            tail = f" in {secs}s" + (f" cost=${cost}" if cost is not None else "")
            if msg.get("reply_to") and not msg.get("needs_reply") and not a.always_reply and str(msg.get("from", "")).lower() != NOTIFY:  # the owner's answers always get the reply: they are waiting in Discord
                mail.ack(agent, mid, result=result, by=self.by)
                say(f"done {mid} for {agent}{tail}: plain answer, acked without a reply")
                return
            if str(msg.get("from", "")).lower() == NOTIFY and len(result.strip()) < 400 and told_owner(mail, agent, msg, started):  # a short "reported to the owner" summary; a real answer still goes
                mail.ack(agent, mid, result=result, by=self.by)
                say(f"done {mid} for {agent}{tail}: it already wrote to the owner in this thread, so no second report")
                return
            if agent == "builder" and direct:  # the orchestrator asked for it and every check is clean: no relay hop
                try:
                    first = mail.thread(msg.get("thread") or mid)[0]; to = str(first.get("from") or NOTIFY)
                    new = mail.send(agent, to, "Re: " + str(first.get("subject") or msg.get("subject", "")), result, reply_to=mid)
                    mail.ack(agent, mid, result=f"reported directly to {to} with {new['id']}", by=self.by)
                    note_latest(msg.get("thread") or mid, said)
                    say(f"done {mid} for {agent}{tail}: REPORT direct, sent to {to} with {new['id']}"); return
                except Exception as e: say(f"direct report for {mid} failed ({e}); it goes to the orchestrator")
            try:
                new = mail.reply(agent, mid, result, by=self.by)
                say(f"done {mid} for {agent}{tail}: replied to {new['to']} with {new['id']}")
            except mail.MailError as e:
                say(f"done {mid} for {agent}{tail}, but the reply was not sent: {e}")
                try: mail.ack(agent, mid, result=f"{result}\n\n(reply not sent: {e})", by=self.by)
                except mail.MailError as e2: say(f"could not ack {mid}: {e2}")
        finally:
            alive.set(); self.drop(job["held"], job["shared"])

    def handle(self, agent, info, msg, agents, exe):
        """Process one message on this thread, start to finish. Returns 'capped' when the run caps stop the pass."""
        job = self.prepare(agent, info, msg, agents, exe)
        if isinstance(job, dict): return self.execute(job)
        return job

    def start(self, job):
        """Hand a prepared message to a worker thread."""
        mid = job["msg"]["id"]
        def work():
            try: self.execute(job)
            except Cancelled: say(f"stopped {mid}: the runner is shutting down, it stays in the inbox")
            except Exception as e: say(f"run error {mid}: {type(e).__name__}: {e}")  # stays in the inbox; a later pass retries
            finally: self.running.pop(mid, None)
        t = threading.Thread(target=work, name=f"run-{mid}", daemon=True); self.running[mid] = t
        try: t.start()
        except BaseException:  # no thread, so nothing would ever release the claims
            self.running.pop(mid, None); self.drop(job["held"], job["shared"]); raise

    def shutdown(self):
        """Stop every run and wait for its claims to be released."""
        self.cancel.set()
        for t in list(self.running.values()): t.join(45)

    def inbox_sig(self):
        """What the cheap look between full passes sees: the files in the inboxes this process serves here, and the runs in flight."""
        try: agents = self.mail.load_agents()
        except Exception: agents = {}
        seen = []
        for name, info in sorted(agents.items()):
            if not isinstance(info, dict) or not info.get("auto") or (self.only and name.lower() not in self.only): continue
            hosts = [info.get("host")] if isinstance(info.get("host"), str) else list(info.get("host") or [])
            if self.me.lower() not in [str(h).lower() for h in hosts]: continue
            try: seen += sorted(f"{name}/{p.name}" for p in (self.q / "mail" / name / "inbox").iterdir())
            except OSError: pass
        return seen, sorted(self.running)

    def idle(self, poll, sleep=time.sleep):
        """Wait for the next full pass: at most POLL seconds, looking at the inboxes and open requests every TICK seconds.
        New mail or a finished run ends the wait at once, so a hop between agents never waits out --poll."""
        sig, end = self.inbox_sig(), time.monotonic() + poll
        while time.monotonic() < end:
            sleep(min(TICK, max(0.0, end - time.monotonic())))
            try: self.serve_open()
            except Exception as e: self.once(("open", type(e).__name__), f"open requests: {type(e).__name__}: {e}")
            if self.inbox_sig() != sig: return

    def serve_open(self):
        """Open requests for this device, Queue/open/<host>/<id>.json: {"kind": "session", ...} resumes a run's session as a
        Remote Control background session, {"kind": "file", "path"} opens a file. Each gets <id>.done.json. Only the
        process that runs the loop serves them (one per device); files older than a day are deleted."""
        if getattr(self.a, "no_loop", True) or self.a.dry_run or self.a.once or time.monotonic() - self.open_at < TICK: return
        self.open_at, d = time.monotonic(), self.q / "open" / self.me
        try: files = list(d.glob("*.json"))
        except OSError: files = []
        for f in files:
            rid = f.name[:-5]
            try:
                if time.time() - f.stat().st_mtime > 86400: f.unlink(); continue
                if rid.endswith(".done") or rid in self.opening or (d / f"{rid}.done.json").exists(): continue
                req = json.loads(f.read_text(encoding="utf-8"))
            except FileNotFoundError: continue
            except (OSError, ValueError) as e:
                if time.time() - f.stat().st_mtime > 30: req = {"kind": f"unreadable ({e})"}  # still syncing, or broken
                else: continue
            self.opening.add(rid)
            threading.Thread(target=self.open_one, args=(d, rid, req if isinstance(req, dict) else {}), name=f"open-{rid}", daemon=True).start()
        if time.time() - self.sweep_at > 600:
            self.sweep_at = time.time(); threading.Thread(target=self.sweep_bg, name="open-sweep", daemon=True).start()

    def open_one(self, d, rid, req):
        try:
            kind = str(req.get("kind") or "")
            res = open_session(req, self.a.permission_mode, self.root) if kind == "session" else open_file(req, self.root) if kind == "file" else {"error": f"unknown request kind {kind!r}"}
            if res.get("bg"):
                with self.open_lock:
                    st = self.open_state(); st.setdefault("bg", {})[res["bg"]] = {"session": req.get("session"), "opened": time.time(), "busy": time.time()}
                    self.mail.write_json_atomic(self.state.parent / "mail-runner-open.json", st)
        except Exception as e: res = {"error": f"{type(e).__name__}: {e}"}
        try: self.mail.write_json_atomic(d / f"{rid}.done.json", {**res, "at": time.time()})
        except Exception as e: say(f"open {rid}: done file not written: {e}")
        say(f"open {rid} ({req.get('kind')}): {res.get('url') or res.get('opened') or res.get('error')}")
        self.opening.discard(rid)

    def open_state(self):
        try: st = json.loads((self.state.parent / "mail-runner-open.json").read_text(encoding="utf-8"))
        except (OSError, ValueError): st = {}
        return st if isinstance(st, dict) else {}

    def sweep_bg(self):
        """Stop the background sessions opened here that sat idle over 12 hours (their conversations are kept), and drop
        pause marks older than 8 days."""
        for p in self.claims.glob("*.parked"):
            try:
                if time.time() - p.stat().st_mtime > 8 * 86400: p.unlink()
            except OSError: pass
        exe = find_claude()
        with self.open_lock:
            st = self.open_state(); bg = st.get("bg") or {}
            if not bg or not exe: return
            try:
                rows = json.loads(subprocess.run([exe, "agents", "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, stdin=subprocess.DEVNULL, **NO_WINDOW).stdout)
                live = {str(r.get("id")): r for r in rows if isinstance(r, dict)}
            except (OSError, ValueError, AttributeError, TypeError, subprocess.TimeoutExpired): return
            for k, v in list(bg.items()):
                if k not in live: bg.pop(k); continue
                if live[k].get("status") != "idle": v["busy"] = time.time()
                elif time.time() - float(v.get("busy") or 0) > 12 * 3600:
                    try: subprocess.run([exe, "stop", k], capture_output=True, timeout=60, stdin=subprocess.DEVNULL, **NO_WINDOW); say(f"stopped background session {k}: idle over 12 hours")
                    except (OSError, subprocess.TimeoutExpired): continue
                    bg.pop(k)
            st["bg"] = bg
            try: self.mail.write_json_atomic(self.state.parent / "mail-runner-open.json", st)
            except Exception: pass

    def mine(self, agent, hosts, msg):
        """True when this device should run the message now: its pinned device, else the first available
        host that holds the repo's source. A message pinned to an offline device waits, and the owner is asked once."""
        me, mid = self.me.lower(), msg["id"]
        if fleet.resources(self.me).get("pressure") and not is_person(msg, self.mail.load_agents()):  # housekeeper.py: no new agent runs until memory recovers; the owner's own messages still run
            self.once(("pressure", mid), f"hold {mid} for {agent}: this device is low on memory"); return False
        pin = str(msg.get("device") or "").lower() or self.session_host(agent, msg, hosts)
        if pin:
            if pin == me: return True
            if fleet.available(pin):  # back up: the bridge removes the question, so ask again if it drops off again
                self.age0.pop(mid, None); (self.q / "mail" / "claims" / f"{mid}.waiting").unlink(missing_ok=True)
                return False
            if not fleet.available(pin) and time.time() - self.age0.setdefault(mid, time.time()) > 240:
                self.ask_redirect(agent, msg, msg.get("device"), "is pinned to {d}, which is offline", "which is offline")
            return False
        capped = {d["host"].lower() for d in fleet.devices() if d.get("pressure") or d.get("cap_hour") and (d.get("runs_hour", 0) >= d["cap_hour"] or d.get("runs_day", 0) >= d.get("cap_day", 1e9))}
        hour, day = counts(load_runs(self.state))
        (capped.add if (self.a.max_per_hour and hour >= self.a.max_per_hour) or (self.a.max_per_day and day >= self.a.max_per_day) else capped.discard)(me)
        # A host ranked before this one whose heartbeat only looks old keeps its place until the liveness relay confirms
        # it is down: over a slow sync both devices could otherwise decide they were first and run the same mail.
        ghost = {str(h).lower() for h in hosts if str(h).lower() != me and not fleet.available(h) and not fleet.confirmed_down(h)}
        beats = {str(d.get("host", "")).lower(): d for d in fleet.devices()}
        up = [h for h in hosts if fleet.available(h) or str(h).lower() in ghost]
        up = [h for h in up if str(h).lower() not in capped] or up  # a device at its run cap passes work to the next one
        want = fleet.wants_platform(f"{msg.get('subject', '')}\n{msg.get('body', '')}") if is_person(msg, self.mail.load_agents()) else None
        plat = {str(d.get("host", "")).lower(): d.get("platform") for d in fleet.devices()}
        if want: up = sorted(up, key=lambda h: plat.get(str(h).lower()) != want)  # stable: keeps the usual order otherwise
        repo = msg.get("repo")
        if repo:
            ok = {d["host"].lower() for d in fleet.where(repo) if d["ok"]} | {h for h in ghost if repo in ((beats.get(h) or {}).get("repos") or [])}
            good = [h for h in up if str(h).lower() in ok]
            if not good:
                self.once(("norepo", mid), f"hold {mid} for {agent}: no available device holds {repo} right now")
                return False
            # Be where the work already is: the device that did the last build of this request, then the usual order.
            # No model spends tokens choosing a device.
            thread = msg.get("thread") or mid
            built = sorted((r for r in fleet.stats(72) if r.get("thread") == thread and r.get("agent") == "builder"), key=lambda r: r.get("ts", 0))
            last = str(built[-1].get("host") or "").lower() if built else ""
            up = sorted(good, key=lambda h: ((plat.get(str(h).lower()) != want) if want else False, str(h).lower() != last))
        if up and str(up[0]).lower() in ghost:
            self.once(("unconfirmed", mid), f"hold {mid} for {agent}: {up[0]} looks offline, but the liveness relay does not confirm it is down")
            if time.time() - self.age0.setdefault(("unconfirmed", mid), time.time()) > 600:
                self.ask_redirect(agent, msg, up[0], "is waiting for {d}, which looks offline but is not confirmed down", "which looks offline")
            return False
        if self.age0.pop(("unconfirmed", mid), None): (self.q / "mail" / "claims" / f"{mid}.waiting").unlink(missing_ok=True)
        if not up or str(up[0]).lower() != me: return False
        for other in self.other_claims(f"{mid}.json"):
            try:
                c = json.loads(other.read_text(encoding="utf-8"))
                if c.get("host", "").lower() != me and time.time() - float(c.get("at", 0)) < self.a.timeout_minutes * 60 + 300: return False
            except (OSError, ValueError): pass
        return True

    def session_host(self, agent, msg, hosts):
        """The device holding the thread's orchestrator session, when it is up, unpressed and serves this agent: the run goes
        there so the session continues in place. '' otherwise (then any device may take it and copy the session over)."""
        if agent != "orchestrator": return ""
        h = str(thread_record(msg.get("thread") or msg["id"]).get("host") or "").lower()
        ok = h and h in [str(x).lower() for x in hosts] and (h == self.me.lower() or (fleet.available(h) and not fleet.resources(h).get("pressure")))
        return h if ok else ""

    def ask_redirect(self, agent, msg, device, sentence, state):
        """Ask the owner once whether work waiting for DEVICE should run here instead; their tap pins it to this device."""
        mid = msg["id"]; mark = self.q / "mail" / "claims" / f"{mid}.waiting"
        if mark.exists() or self.a.dry_run: return
        mark.parent.mkdir(parents=True, exist_ok=True); mark.write_text(self.me, encoding="utf-8")
        label = self.me
        fleet.event("waiting", f"{msg.get('subject')}: waiting for {device}, {state}", agent=agent, thread=msg.get("thread", ""), repo=msg.get("repo") or "")
        try:
            self.mail.send(agent, NOTIFY, f"Waiting for {device}", f"\"{msg.get('subject')}\" {sentence.format(d=device)}. Run it on {label} instead?",
                           reply_to=mid, options=[{"label": f"Run on {label}", "action": {"redirect": {"agent": agent, "id": mid, "device": self.me}}},
                                                  {"label": "Keep waiting", "action": {"none": True}}])
        except self.mail.MailError as e: say(f"could not ask about {mid}: {e}")

    def one_pass(self):
        a, mail = self.a, self.mail
        if self.stopped(): return
        agents = mail.load_agents()
        exe = find_claude()
        batch = []
        for name in sorted(agents):
            info = agents[name] if isinstance(agents[name], dict) else {}
            hosts = [info.get("host")] if isinstance(info.get("host"), str) else list(info.get("host") or [])
            if not info.get("auto") or self.me.lower() not in [str(h).lower() for h in hosts]: continue
            if self.only and name.lower() not in self.only: continue
            if info.get("kind") != "claude":
                self.once(("kind", name), f"skip agent {name}: kind {info.get('kind')!r} is not claude, never run"); continue
            batch += [(name, info, hosts, m) for m in mail.list_inbox(name)]
        batch.sort(key=lambda row: (str(row[3].get("created", "")), str(row[3].get("id", ""))))  # oldest first across agents, so one busy agent cannot take every slot
        for name, info, hosts, msg in batch:
            if self.stopped(): return
            if not exe and not a.dry_run:
                self.once("exe", "claude not found on PATH, leaving mail untouched"); return
            self.noted.discard("exe")
            if msg.get("id") in self.running: continue
            if msg.get("id") in self.spent:
                self.once(("spent", msg.get("id")), f"skip {msg.get('id')} for {name}: already run here but still in the inbox"); continue
            over = not self.serial and len(self.running) >= self.limit()
            if over and not (is_person(msg, agents) and len(self.running) < self.limit() + HUMAN_EXTRA):
                self.once("full", f"all {self.limit()} run slots busy; later agent mail waits"); continue
            if not over: self.noted.discard("full")
            if self.draining and not is_person(msg, agents): continue
            if not self.mine(name, hosts, msg): continue
            if self.serial:
                if self.handle(name, info, msg, agents, exe) == "capped": return
                continue
            job = self.prepare(name, info, msg, agents, exe, extra=over)
            if job == "capped": return
            if job: self.start(job)

def main():
    if sys.argv[1:2] == ["fresh"]:
        if len(sys.argv) != 3: print("usage: mail-runner.py fresh <thread id>"); return 2
        set_fresh(load_mail(), sys.argv[2]); print(f"thread {sys.argv[2]}: the next run starts a fresh session"); return 0
    if sys.argv[1:2] == ["requeue"]:
        if len(sys.argv) != 4: print("usage: mail-runner.py requeue <agent> <mail id>"); return 2
        return requeue(load_mail(), sys.argv[2], sys.argv[3])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--poll", type=float, default=20, help="seconds between full passes and loop ticks; inboxes and open requests are looked at every 3 s")
    ap.add_argument("--once", action="store_true", help="one pass over the inboxes, then exit")
    ap.add_argument("--dry-run", action="store_true", help="print what would run; no claude, no mail changes")
    ap.add_argument("--timeout-minutes", type=float, default=20)
    ap.add_argument("--max-per-hour", type=int, default=0, help="0 = no cap")
    ap.add_argument("--max-per-day", type=int, default=0, help="0 = no cap")
    ap.add_argument("--max-hops", type=int, default=0, help="0 = no limit")
    ap.add_argument("--max-concurrent", type=int, default=None, help=f"messages this process runs at once; default Queue/mail/runner.json, else {DEFAULT_MAX_CONCURRENT}; at most {MAX_CONCURRENT}, 0 = that ceiling")
    ap.add_argument("--permission-mode", default="acceptEdits")
    ap.add_argument("--always-reply", action="store_true", help="also reply to plain answers (can ping-pong without end unless --max-hops is set)")
    ap.add_argument("--mcp", action="store_true", help="give each run its own agent_mail MCP server and allow its tools")
    ap.add_argument("--prompt-via", choices=("auto", "arg", "stdin"), default="auto",
                    help="auto passes the prompt as an argument, or on stdin for a Windows .cmd shim or a very long prompt")
    ap.add_argument("--only", help="comma-separated agent names this process serves; default all")
    ap.add_argument("--no-loop", action="store_true", help="do not run the improvement loop scheduler in this process")
    ap.add_argument("--state-file", help="default ~/.switchboard/mail-runner-state.json (device-local, never synced)")
    a = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        try: s.reconfigure(encoding="utf-8", errors="replace")
        except Exception: pass
    def _term(signum, frame): raise KeyboardInterrupt  # stop like Ctrl+C: kill the Claude run, release the claim
    for name in ("SIGTERM", "SIGBREAK"):
        try: signal.signal(getattr(signal, name), _term)
        except (AttributeError, ValueError, OSError): pass
    if not a.dry_run and not a.once and not fleet.single_instance("mail-runner-" + (a.only or "all")):
        print("another mail-runner with the same --only is already running here; exiting"); time.sleep(60); return 0
    r = Runner(a, load_mail())
    if not a.dry_run: fleet.start_heartbeat(r.beat_extra)
    if not a.dry_run and not a.once: screencap.start_service(say)
    say(f"mail-runner start on {r.me}, DevRoot={r.root}, mode={a.permission_mode}, caps={a.max_per_hour}/h {a.max_per_day}/day, "
        f"timeout={a.timeout_minutes:g}m, max concurrent={r.limit()}, state={r.state}, claude={find_claude() or 'not found'}" + (", dry-run" if a.dry_run else ""))
    watched = [HERE / n for n in ("mail-runner.py", "fleet.py", "agent-mail.py", "agent-mail-mcp.py", "improve-loop.py", "route.py")]
    def stamp():
        out = []
        for f in watched:
            try: out.append(f.stat().st_mtime)
            except OSError: out.append(0)
        return tuple(out)
    loaded, ticked = stamp(), -1e9
    while True:
        if not (a.once or a.dry_run):
            if not r.draining and stamp() != loaded:
                r.draining = True; say("code changed on disk: finishing the runs in flight, then restarting; only the owner's own messages start meanwhile")
            if r.draining and not r.running:
                say("restarting on the new code"); return 3
        try:
            if not a.dry_run and not a.no_loop and not r.stopped() and time.monotonic() - ticked >= a.poll:  # new mail wakes the pass early, not the loop
                ticked = time.monotonic()
                try: load_loop().tick(r.mail, say)
                except Exception as e: say(f"loop error: {type(e).__name__}: {e}")
            r.one_pass()
        except KeyboardInterrupt:
            say("interrupted"); r.shutdown(); return 130
        except Exception as e:  # keep the loop alive; the next pass retries
            say(f"pass error: {type(e).__name__}: {e}")
            if a.once or a.dry_run: return 1
        if a.once or a.dry_run:
            return 1 if r.failed else 0
        try: r.idle(a.poll)  # --poll paces the full pass and the loop; new mail and open requests are seen within TICK seconds
        except KeyboardInterrupt: say("interrupted"); r.shutdown(); return 130

if __name__ == "__main__": sys.exit(main())
