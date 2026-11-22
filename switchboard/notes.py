#!/usr/bin/env python3
"""The owner's notepad: everything they wrote in a request's thread, kept in order, with what each message replied to and
whether an agent has read it yet. One JSON file per thread in Queue/notes. The Discord bridge writes it; every agent
run gets it in its prompt, so a step always works from all of their words, not only the message that started it.

A note is unread until an agent run starts in its thread after it was written. A message the owner deletes is marked
withdrawn (treat as unsaid); one they edit keeps its newest text. Nothing here talks to Discord or to a model.

  notes.py show THREAD      print a thread's notepad as the agents see it
"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet
from common import queue_root

def _dir(): return queue_root() / "notes"
def _path(thread): return _dir() / (str(thread).replace("/", "_").replace("\\", "_") + ".json")
def load(thread):
    try: return json.loads(_path(thread).read_text(encoding="utf-8"))
    except (OSError, ValueError): return []
def save(thread, notes):
    _dir().mkdir(parents=True, exist_ok=True); fleet._write(_path(thread), notes[-400:])

def add(thread, mid, text, cid="", reply=None, files=0, at=None):
    notes = load(thread)
    if any(n["id"] == mid for n in notes): return
    notes.append({"id": str(mid), "cid": str(cid), "at": at or time.time(), "text": str(text or ""), "reply": reply or None, "files": int(files or 0), "state": "unread"})
    save(thread, notes)

def find(mid):
    """(thread, note) for a Discord message id, newest files first, or (None, None)."""
    d = _dir()
    for p in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:200] if d.is_dir() else []:
        try: notes = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError): continue
        for n in notes:
            if n.get("id") == str(mid): return p.stem, n
    return None, None

def change(mid, **fields):
    thread, _ = find(mid)
    if not thread: return None
    notes = load(thread)
    for n in notes:
        if n["id"] == str(mid): n.update(fields)
    save(thread, notes); return thread

def move(old, new):
    """A new request has no thread until its first mail exists: carry its notes over."""
    notes = load(old)
    if not notes: return
    save(new, load(new) + notes)
    try: _path(old).unlink()
    except OSError: pass

def unread(thread): return [n for n in load(thread) if n.get("state") == "unread"]

def mark_read(thread, upto, by=""):
    """Notes written before UPTO are read: an agent run started after them. Returns the notes that changed."""
    notes, out = load(thread), []
    for n in notes:
        if n.get("state") == "unread" and float(n.get("at", 0)) <= upto:
            n["state"], n["read_at"], n["read_by"] = "read", upto, by; out.append(n)
    if out: save(thread, notes)
    return out

def render(thread, limit=15, clip=500):
    """The notepad as text for an agent's prompt; '' when the owner wrote nothing here."""
    every = load(thread)
    if not every: return ""
    notes = every if len(every) <= limit else [every[0]] + every[-(limit - 1):]  # the first message (often the spec) never drops out
    rows = []
    for n in notes:
        cut = 3000 if n is every[0] or n.get("state") == "unread" else clip  # the spec and what no agent has read yet stay whole
        tag = {"unread": "[NEW] ", "withdrawn": "[WITHDRAWN, the owner deleted this: treat it as unsaid] "}.get(n.get("state"), "")
        if n.get("edited"): tag += "[edited, this is the newest wording] "
        re_ = f"(replying to {n['reply'].get('who')}: \"{n['reply'].get('text', '')[:160]}\") " if n.get("reply") else ""
        files = f" [{n['files']} file(s) attached]" if n.get("files") else ""
        rows.append(f"- {time.strftime('%H:%M', time.localtime(float(n.get('at', 0))))} {tag}{re_}{' '.join(str(n.get('text', '')).split())[:cut]}{files}")
    return "\n".join(rows)

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "show": print(render(sys.argv[2], limit=100) or "(empty)")
    else: sys.exit(__doc__)
