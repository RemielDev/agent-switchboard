#!/usr/bin/env python3
"""Playbooks: short lessons per project that the planner reads, kept honest by the owner's reactions.

After ACE (arXiv 2510.04618): lessons are small separate entries with helpful and harmful counts, merged by this script
and never rewritten wholesale by a model, so the list does not collapse into vague summaries. An entry that hurt three
times and more often than it helped drops out. No model call here.

  playbook.py add PROJECT "one sentence"     record a lesson (duplicates are ignored)
  playbook.py show PROJECT                   what the planner sees
  playbook.py vote PROJECT good|bad          credit or blame the entries last shown for PROJECT
"""
import json, re, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet
from common import queue_root

KEEP, SHOW = 60, 8
def _p(project, shown=False): return queue_root() / "playbooks" / (re.sub(r"[^\w.-]+", "_", str(project or "general")).lower() + (".shown.json" if shown else ".json"))
def load(project):
    try: return json.loads(_p(project).read_text(encoding="utf-8"))
    except (OSError, ValueError): return []
def save(project, rows):
    _p(project).parent.mkdir(parents=True, exist_ok=True); fleet._write(_p(project), rows)
def _norm(t): return re.sub(r"[^a-z0-9 ]", "", str(t).lower()).strip()

def add(project, text):
    text = " ".join(str(text or "").split())[:300]
    if len(text) < 12: return False
    rows = load(project)
    if any(_norm(r["text"]) == _norm(text) for r in rows): return False
    rows.append({"id": f"{int(time.time())}-{len(rows)}", "text": text, "helpful": 0, "harmful": 0, "added": time.time()})
    live = [r for r in rows if not (r["harmful"] >= 3 and r["harmful"] > r["helpful"])]
    save(project, sorted(live, key=lambda r: (r["helpful"] - r["harmful"], r["added"]))[-KEEP:]); return True

def top(project, n=SHOW):
    rows = [r for r in load(project) if not (r["harmful"] >= 3 and r["harmful"] > r["helpful"])]
    return sorted(rows, key=lambda r: (r["helpful"] - r["harmful"], r["added"]), reverse=True)[:n]

def render(project, remember=True):
    """The entries for a prompt, and a note of which ones were shown so a later reaction can be credited to them."""
    rows = top(project)
    if remember and rows:
        try: fleet._write(_p(project, True), {"at": time.time(), "ids": [r["id"] for r in rows]})
        except OSError: pass
    return "\n".join(f"- {r['text']}" + (f" (helped {r['helpful']}, hurt {r['harmful']})" if r["helpful"] or r["harmful"] else "") for r in rows)

def vote(project, good):
    try: ids = set(json.loads(_p(project, True).read_text(encoding="utf-8")).get("ids") or [])
    except (OSError, ValueError): ids = set()
    rows = load(project); n = 0
    for r in rows:
        if r["id"] in ids: r["helpful" if good else "harmful"] += 1; n += 1
    if n: save(project, rows)
    return n

if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 3 and a[0] == "add": print("added" if add(a[1], a[2]) else "not added (too short or already there)")
    elif len(a) == 2 and a[0] == "show": print(render(a[1], remember=False) or "(empty)")
    elif len(a) == 3 and a[0] == "vote" and a[2] in ("good", "bad"): print(f"{vote(a[1], a[2] == 'good')} entries updated")
    else: sys.exit(__doc__)
