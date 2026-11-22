#!/usr/bin/env python3
"""Session status for interactive Claude Code and Codex sessions (stdlib only, macOS and Windows).

A Claude Code hook command for UserPromptSubmit, Stop, Notification and SessionEnd. It reads the hook JSON on
stdin and keeps one small file per session, Queue/sessions/<host>/<session_id>.json, with the fields

  session_id, host, tool, cwd, project, state, last_prompt, last_note, note_type, updated, started

state is working (a prompt was submitted), idle (the turn finished), needs_you (a permission prompt, or Claude
has been waiting for input) or ended. It prints nothing and always exits 0. Runs started by mail-runner.py or
queue-runner.py are not listed: they have AGENT_MAIL_AGENT or SESSION_HOOK_SKIP in their environment.

  session-hook.py                     Claude Code hook, JSON on stdin
  session-hook.py --codex '<json>'    Codex `notify` program (agent-turn-complete): tool codex, state idle
  session-hook.py --list              print every session file, one JSON object per line

Every run also deletes old files: state ended for 2 hours, or not updated for 3 days.
Set SESSION_HOOK_LOG to a file path to append one line per event (for debugging)."""
import json, os, re, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

STATES = ("working", "idle", "needs_you", "ended")
EVENT_STATE = {"UserPromptSubmit": "working", "Stop": "idle", "SessionEnd": "ended", "PostToolUse": "working"}
# Notification types that mean the session is blocked on the owner. Real messages (Claude Code 2.1.319):
# permission_prompt "Claude needs your permission to use <tool>", idle_prompt "Claude is waiting for your input".
NEEDS_TYPES = {"permission_prompt", "worker_permission_prompt", "idle_prompt", "elicitation_dialog",
               "elicitation_url_dialog", "agent_needs_input", "quota_auto_resume_stale"}
NEEDS_RE = re.compile(r"permission|approv|waiting for (your )?input|needs your", re.I)
ENDED_KEEP, STALE_KEEP = 7200, 3 * 86400  # an ended session leaves the board after 2 hours; resuming it lists it again
# Not the owner's sessions: runs in a temp or scratchpad folder (probes, tests) and Codex's own background jobs.
MACHINE_CWD = re.compile(r"[\\/]tmp\.[A-Za-z0-9]+|[\\/]scratchpad[\\/]|[\\/]OpenAI[\\/]Codex[\\/]bin[\\/]|[\\/]AppData[\\/]Local[\\/]Temp[\\/]|^/(private/)?tmp/|^/(private/)?var/folders/", re.I)
def machine(cwd): return bool(MACHINE_CWD.search(str(cwd or "")))


def skipped():
    """True inside an unattended runner turn, which must not be listed or ask the owner anything."""
    return bool(os.environ.get("AGENT_MAIL_AGENT") or os.environ.get("SESSION_HOOK_SKIP"))

def clip(text, n=200):
    return str(text or "").strip()[:n]

def slash_only(text):
    """True for a prompt that is only slash commands, the same test topics.py items() uses."""
    s = str(text or "").strip()
    return s.startswith("/") and len(re.sub(r"^(/[\w:.-]+(?=\s|$)\s*)+", "", s).split()) < 2

def real_prompt(text):
    """False for what the app injects as a prompt (task notifications, reminders): not the owner's words, so never a title."""
    s = str(text or "").lstrip()
    return bool(s) and not s.startswith("<") and not TITLE_GEN.match(s)

TITLE_GEN = re.compile(r"You are a helpful assistant\. You will be presented with a user prompt|You are in a fork of an existing Codex thread")  # Codex names a thread with a turn of its own

def project_name(path):
    return re.split(r"[\\/]+", str(path or "").rstrip("\\/"))[-1]

def sessions_root():
    from common import queue_root
    return queue_root() / "sessions"

def session_path(session_id, host=None):
    from common import hostname
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id))[:120] or "_"
    return sessions_root() / (host or hostname()) / f"{sid}.json"

def _log(text):
    p = os.environ.get("SESSION_HOOK_LOG")
    if not p: return
    try:
        with open(p, "a", encoding="utf-8") as f: f.write(f"{time.time():.3f} {text}\n")
    except OSError: pass

def update(session_id, cwd="", tool="claude", create=True, **fields):
    """Merge FIELDS into this host's file for the session and return the record (None if absent and not created)."""
    import fleet
    from common import hostname
    p, now = session_path(session_id), time.time()
    try: rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError): rec = None
    if not isinstance(rec, dict):
        if not create or (machine(cwd) and not os.environ.get("SESSION_HOOK_ALL")): return None
        rec = {}
    tool, cwd = rec.get("tool") or tool, str(cwd or rec.get("cwd") or "")
    # Claude Code gives hooks the project root, so a `cd` inside the session does not rename the project.
    root = os.environ.get("CLAUDE_PROJECT_DIR") if tool == "claude" else ""
    new = {"session_id": str(session_id), "host": hostname(), "tool": tool, "cwd": cwd,
           "project": project_name(root or cwd), "root": str(root or rec.get("root") or cwd),
           "state": rec.get("state") if rec.get("state") in STATES else "idle",
           "first_prompt": ("" if slash_only(rec.get("first_prompt")) else rec.get("first_prompt")) or (rec.get("last_prompt", "") if real_prompt(rec.get("last_prompt")) and not slash_only(rec.get("last_prompt")) else ""),
           "last_prompt": rec.get("last_prompt", ""), "last_note": rec.get("last_note", ""),
           "note_type": rec.get("note_type", ""), "updated": now, "started": rec.get("started") or now}
    for k in ("prompt_at", "last_reply"):  # kept across Stop and tool events: the bridge reads them to settle questions the owner handled here
        if rec.get(k): new[k] = rec[k]
    new.update({k: v for k, v in fields.items() if v is not None})
    if not new["first_prompt"] and real_prompt(new["last_prompt"]) and not slash_only(new["last_prompt"]): new["first_prompt"] = new["last_prompt"]
    fleet._write(p, new)
    return new

def last_reply(path, n=400):
    """The session's final assistant text this turn, from the end of its transcript (JSONL), clipped. The bridge shows it to
    a small model to judge whether the work settled one of the owner's open questions. Best effort: '' when unreadable."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 400_000)); tail = f.read().decode("utf-8", "replace")
    except (OSError, TypeError, ValueError): return ""
    for line in reversed(tail.splitlines()):
        try: e = json.loads(line)
        except ValueError: continue
        if not isinstance(e, dict) or e.get("type") != "assistant": continue
        c = (e.get("message") or {}).get("content")
        text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict) and x.get("type") == "text")
        if text.strip(): return clip(" ".join(text.split()), n)
    return ""

def handle(d):
    """One Claude Code hook payload."""
    ev, sid = str(d.get("hook_event_name") or ""), d.get("session_id")
    if not sid: return
    fields = {}
    if ev in EVENT_STATE: fields["state"] = EVENT_STATE[ev]
    if ev == "UserPromptSubmit" and real_prompt(d.get("prompt")): fields["last_prompt"], fields["prompt_at"] = clip(d.get("prompt")), time.time()  # the bridge settles a question this session asked once the owner types here
    elif ev == "Stop":
        r = last_reply(d.get("transcript_path"))
        if r: fields["last_reply"] = r
    elif ev == "Notification":
        msg, kind = clip(d.get("message")), str(d.get("notification_type") or "")
        fields.update(last_note=msg, note_type=kind)
        if kind in NEEDS_TYPES or (not kind and NEEDS_RE.search(msg)): fields["state"] = "needs_you"
    # A session that never took a prompt has no file; its end, or a stray event, is not worth creating one.
    rec = update(sid, d.get("cwd"), create="last_prompt" in fields or fields.get("state") == "needs_you", **fields)  # listed once the owner typed or it needs them
    _log(f"{ev} {sid} -> {rec.get('state') if rec else 'no file'}")

def handle_codex(arg):
    """Codex notify payload: type, thread-id, turn-id, cwd, input-messages, last-assistant-message."""
    d = json.loads(arg)
    if d.get("type") not in (None, "agent-turn-complete"): return
    sid = d.get("thread-id") or d.get("thread_id")
    if not sid: return
    asked = d.get("input-messages") or []
    prompt = clip(asked[-1] if isinstance(asked, list) and asked else "")
    # Codex runs its title naming and sub-agents as threads of their own: listed only once one carries the owner's words.
    return update(sid, d.get("cwd") or os.getcwd(), tool="codex", create=real_prompt(prompt), state="idle",
           last_prompt=prompt if real_prompt(prompt) else None,
           last_note=clip(d.get("last-assistant-message")), last_reply=clip(" ".join(str(d.get("last-assistant-message") or "").split()), 400) or None,
           note_type="agent-turn-complete", prompt_at=time.time() if real_prompt(prompt) else None)

def cleanup(now=None):
    """Delete ended files after 2 hours, any file after 3 days without an update, and leftover temp files."""
    now = now or time.time()
    for p in sessions_root().glob("*/*"):
        try:
            age = now - p.stat().st_mtime
            if p.suffix == ".tmp":
                if age > 3600: p.unlink()
                continue
            if p.suffix != ".json" or age < ENDED_KEEP: continue
            try: rec = json.loads(p.read_text(encoding="utf-8"))
            except ValueError: rec = {}
            if not isinstance(rec, dict): rec = {}
            try: age = now - float(rec.get("updated"))
            except (TypeError, ValueError): pass
            if age >= STALE_KEEP or (rec.get("state") == "ended" and age >= ENDED_KEEP): p.unlink()
        except OSError: continue

def main(argv):
    if argv[:1] == ["--list"]:
        for p in sorted(sessions_root().glob("*/*.json")):
            try: print(json.dumps(json.loads(p.read_text(encoding="utf-8")), ensure_ascii=False))
            except (OSError, ValueError): pass
        return
    if skipped(): return
    if argv[:1] == ["--codex"]:
        if len(argv) > 1: handle_codex(argv[-1])
    else:
        raw = b"" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read()
        d = json.loads(raw.decode("utf-8", "replace") or "{}")
        if isinstance(d, dict): handle(d)
    cleanup()

if __name__ == "__main__":
    try: main(sys.argv[1:])
    except BaseException as e:  # a hook must never fail the session: no output, exit 0
        _log(f"error {type(e).__name__}: {e}")
    sys.exit(0)
