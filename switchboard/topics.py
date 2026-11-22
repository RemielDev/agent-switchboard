#!/usr/bin/env python3
"""Topics: everything in flight on every device, grouped by what it is about.

Items are the owner's own Claude and Codex sessions (Queue/sessions, written by the session hooks on each device) and the
agent mail threads with unfinished work (fleet.active_work). Each item gets one topic, kept in Queue/topics/labels.json:
a repo folder names its own topic; anything else is labelled once by a small model in a separate process, which reuses
an existing topic when the item fits. Nothing here talks to Discord; the bridge and a terminal read the same view.

  topics.py show [--json]    the grouped view
  topics.py label            label new items now (one small model call for all of them)
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet
from common import dev_root, hostname, queue_root

SESSION_FRESH = 12 * 3600   # a session untouched this long leaves the view
ENDED_KEEP = 3600           # an ended session stays an hour
RETRY = 300                 # seconds between labelling attempts after a failure
MODEL = "claude-sonnet-5-5"   # last fallback only. Gemini through agy (optional) labels first, then Cursor's Composer where installed.
KEEP = 600                  # labels kept

def _dir(): return queue_root() / "topics"
def load_labels():
    try: return json.loads((_dir() / "labels.json").read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}
def save_labels(labels):
    if len(labels) > KEEP: labels = dict(sorted(labels.items(), key=lambda kv: kv[1].get("at", 0))[-KEEP:])
    _dir().mkdir(parents=True, exist_ok=True); fleet._write(_dir() / "labels.json", labels)

def label_host(h):
    return "PC" if "pc" in str(h).lower() else "Mac" if "mac" in str(h).lower() else str(h or "")

def real_folder(name):
    name = str(name or "").strip()
    return "" if name.lower() in ("workspace", "queue") or name.lower().startswith("scratch") else name

def repo_topic(name):
    """A real project folder under the dev root is its own topic; scratch and unknown folders are not."""
    name = str(name or "").strip()
    if not real_folder(name): return ""
    for p in dev_root().iterdir() if dev_root().is_dir() else []:
        if p.is_dir() and p.name.lower() == name.lower(): return p.name
    return ""

def items(now=None):
    now, out = now or time.time(), []
    d = queue_root() / "sessions"
    up, me = {str(v["host"]).lower() for v in fleet.devices() if v.get("available")}, hostname().lower()
    for p in d.glob("*/*.json") if d.is_dir() else []:
        try: s = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError): continue
        if not isinstance(s, dict) or not s.get("session_id"): continue
        age, state = now - float(s.get("updated") or 0), str(s.get("state") or "idle")
        if state == "working" and age > 3600: state = "idle"
        host = str(s.get("host") or p.parent.name); offline = state == "working" and host.lower() != me and host.lower() not in up
        if offline: state = "idle"
        if age > SESSION_FRESH or (state == "ended" and age > ENDED_KEEP): continue
        fp = str(s.get("first_prompt") or "").strip()
        if fp.startswith("/") and len(re.sub(r"^(/[\w:.-]+(?=\s|$)\s*)+", "", fp).split()) < 2: fp = ""
        title = (fp or str(s.get("last_prompt") or "")).strip()
        if not title or title.startswith("<"): title = str(s.get("project") or "session")
        out.append({"key": "s:" + str(s["session_id"]), "kind": "session", "tool": s.get("tool") or "claude", "host": s.get("host") or p.parent.name,
                    "where": label_host(s.get("host") or p.parent.name), "state": state, "offline": offline, "title": " ".join(title.split())[:140],
                    "now": " ".join(str(s.get("last_prompt") or "").split())[:140] if not str(s.get("last_prompt") or "").lstrip().startswith("<") else "",
                    "project": repo_topic(s.get("project")), "folder": real_folder(s.get("project")), "updated": float(s.get("updated") or 0)})
    for r in fleet.active_work(limit=60):
        states = [i["state"] for i in r["items"]]
        state = "needs_you" if any("the owner" in x.lower() for x in states) else "working" if any(x.startswith("running") for x in states) else "queued"
        hosts = sorted({label_host(x.split(" on ", 1)[1]) for x in states if " on " in x})
        area = next((i["area"] for i in r["items"] if i["area"]), "")
        out.append({"key": "t:" + r["thread"], "kind": "agents", "tool": "agents", "host": "", "where": ", ".join(hosts), "state": state,
                    "title": r["title"], "now": "; ".join(f"{i['agent']} {i['state']}" for i in r["items"][:3]),
                    "project": repo_topic(area), "folder": "", "area": area, "updated": 0.0})
    return out

def ask_model(todo, known):
    """One small model call for every unlabelled item: {key: topic}. Runs outside any agent's context."""
    exe = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    rows = "\n".join(f"{i['key']} | {i['title']}" + (f" | area {i['area']}" if i.get("area") else "") + (f" | folder {i['folder']}" if i.get("folder") else "") for i in todo)
    prompt = ("Group work items by topic for a status board. A topic is the product or system the work is about, "
              "2 to 4 words in Title Case, never the action taken. Reuse one of the existing topics whenever an item "
              "fits it; make a new topic only when none fits. Items about the same system must share one topic. "
              "An item's folder is the project folder it runs in: prefer an existing topic named after that folder.\n\n"
              f"Existing topics: {json.dumps(sorted(known)) if known else 'none yet'}\n\nItems (key | what was asked):\n{rows}\n\n"
              "Answer with one JSON object mapping every key to its topic, and nothing else.")
    agy = next((str(x) for x in (shutil.which("agy"), Path.home() / ".local/bin/agy", Path(os.environ.get("LOCALAPPDATA", "")) / "agy/bin/agy.exe") if x and Path(x).is_file()), "")
    if agy:  # optional: Gemini through the Antigravity CLI, so labelling spends no Claude usage
        try:
            with tempfile.TemporaryDirectory() as td:
                r = subprocess.run([agy, "-p", prompt, "--model", "gemini-4-argon-low", "--output-format", "json", "--disable-slash-commands", "--print-timeout", "90s"], cwd=td,
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            env_ = json.loads((r.stdout or "").strip().splitlines()[-1])
            hit = re.search(r"\{.*\}", env_.get("response") or "", re.S) if env_.get("status") == "SUCCESS" else None
            if hit:
                got = json.loads(hit.group(0))
                return {k: " ".join(str(v).split())[:40] for k, v in got.items() if isinstance(v, str) and v.strip()}
        except Exception: pass  # fall through to Composer, then Claude
    cur = shutil.which("cursor-agent") or (str(Path.home() / ".local/bin/cursor-agent") if (Path.home() / ".local/bin/cursor-agent").exists() else "")
    if cur:  # quick messaging work goes to Composer: no Claude usage at all
        try:
            with tempfile.TemporaryDirectory() as td:
                r = subprocess.run([cur, "-p", "--trust", "--output-format", "text", "--model", "composer-2.5-fast", prompt], cwd=td, stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
            hit = re.search(r"\{.*\}", r.stdout or "", re.S)
            if hit:
                got = json.loads(hit.group(0))
                return {k: " ".join(str(v).split())[:40] for k, v in got.items() if isinstance(v, str) and v.strip()}
        except Exception: pass  # fall through to Claude
    env = {**os.environ, "SESSION_HOOK_SKIP": "1"}
    for k in ("AGENT_MAIL_PARENT", "CLAUDECODE"): env.pop(k, None)
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run([exe, "-p", prompt, "--model", MODEL, "--effort", "low", "--strict-mcp-config"], cwd=td, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    hit = re.search(r"\{.*\}", r.stdout or "", re.S)
    if not hit: raise RuntimeError(f"no JSON from the model (exit {r.returncode}): {(r.stdout or r.stderr or '')[:200]}")
    got = json.loads(hit.group(0))
    return {k: " ".join(str(v).split())[:40] for k, v in got.items() if isinstance(v, str) and v.strip()}

LOOP_TITLES = (r"^(.+?): improvement cycle \((?:ask|auto)\)$", r"^Improvement cycle started for (.+?) \(", r"^\[loop\] Improvement cycle: (.+)$")

def label(force=False):
    """Give every new item a topic. Repo folders label themselves; the rest go to the model in one call."""
    labels, its, now = load_labels(), items(), time.time()
    new = [i for i in its if i["key"] not in labels]
    for i in [i for i in new if i["project"]]: labels[i["key"]] = {"topic": i["project"], "at": now}
    todo = [i for i in new if not i["project"]]
    for i in list(todo):  # a loop cycle names its project: "SloopApp" -> existing "Sloop App"; an unknown project is its own topic
        m = next((m for m in (re.match(r, i["title"]) for r in LOOP_TITLES) if m), None)
        if not m: continue
        name, topics, norm = m.group(1).strip(), {v["topic"] for v in labels.values()}, lambda x: re.sub(r"[\W_]+", "", x.lower())
        hits = {t for t in topics if norm(t) == norm(name)} or {t for t in topics if t.lower().startswith(name.lower() + " ")} or ({name} if not any(t.lower().startswith(name.lower()) for t in topics) else set())
        if len(hits) == 1: labels[i["key"]] = {"topic": hits.pop(), "at": now}; todo.remove(i)
    for i in list(todo):  # a session's folder names its topic: "Birch" -> existing "Birch Website"
        f = (i.get("folder") or "").lower()
        hits = {v["topic"] for v in labels.values() if f and (v["topic"].lower() == f or v["topic"].lower().startswith(f + " "))}
        if len(hits) == 1: labels[i["key"]] = {"topic": hits.pop(), "at": now}; todo.remove(i)
    note = ""
    if todo:
        mark = _dir() / "last-try"
        try: recent = now - mark.stat().st_mtime < RETRY
        except OSError: recent = False
        if recent and not force: note = f"{len(todo)} item(s) wait for the next labelling attempt"
        else:
            _dir().mkdir(parents=True, exist_ok=True); mark.write_text(hostname(), encoding="utf-8")
            try:
                got = ask_model(todo, {v["topic"] for v in labels.values()})
                for i in todo:
                    if got.get(i["key"]): labels[i["key"]] = {"topic": got[i["key"]], "at": now}
                mark.unlink(missing_ok=True)
            except Exception as e: note = f"labelling failed, retry in {RETRY // 60} min: {str(e)[:160]}"
    if new: save_labels(labels)
    return {"new": len(new), "labelled": len([i for i in new if i["key"] in labels]), "note": note}

def overview():
    labels, groups, its = load_labels(), {}, items()
    first = {}  # project -> its earliest label: one project, one topic card
    for i in sorted((i for i in its if (i.get("folder") or i["project"]) and (labels.get(i["key"]) or {}).get("topic")), key=lambda i: labels[i["key"]].get("at", 0)):
        first.setdefault(i.get("folder") or i["project"], labels[i["key"]]["topic"])
    for i in its:
        topic = first.get(i.get("folder") or i["project"]) or (labels.get(i["key"]) or {}).get("topic") or i["project"] or "Unsorted"
        g = groups.setdefault(topic, {"topic": topic, "items": []}); g["items"].append(i)
    rank = {"needs_you": 0, "working": 1, "queued": 2, "idle": 3, "ended": 4}
    for g in groups.values():
        g["items"].sort(key=lambda i: (rank.get(i["state"], 5), -i["updated"]))
        g["needs_you"] = sum(i["state"] == "needs_you" for i in g["items"]); g["working"] = sum(i["state"] == "working" for i in g["items"])
        g["unlabelled"] = any(i["key"] not in labels for i in g["items"])
    return sorted(groups.values(), key=lambda g: (-g["needs_you"], -g["working"], g["topic"].lower()))

def text(groups):
    out = []
    for g in groups:
        head = g["topic"] + (f"  ({g['needs_you']} need you)" if g["needs_you"] else "") + (f"  ({g['working']} working)" if g["working"] else "")
        out.append(head)
        for i in g["items"]:
            who = f"{i['tool']} on {i['where']}" if i["where"] else i["tool"]
            out.append(f"  {i['state']:<9} {who:<16} {i['title'][:90]}")
    return "\n".join(out) or "nothing in flight"

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show"); s.add_argument("--json", action="store_true")
    l = sub.add_parser("label"); l.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.cmd == "label": print(json.dumps(label(a.force))); return 0
    g = overview(); print(json.dumps(g, indent=1) if a.json else text(g)); return 0

if __name__ == "__main__":
    sys.exit(main())
