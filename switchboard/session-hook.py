#!/usr/bin/env python3
"""Session status for interactive Claude Code and Codex sessions (stdlib only, macOS and Windows).

A Claude Code hook command for UserPromptSubmit, Stop, Notification and SessionEnd. It reads the hook JSON on
stdin and keeps one small file per session, Queue/sessions/<host>/<session_id>.json, with the fields

  session_id, host, tool, cwd, project, state, last_prompt, last_note, note_type, updated, started

state is working (a prompt was submitted), idle (the turn finished), needs_you (a permission prompt, or Claude
has been waiting for input) or ended. It always exits 0. Runs started by mail-runner.py or queue-runner.py are not
listed: they have AGENT_MAIL_AGENT or SESSION_HOOK_SKIP in their environment.

On UserPromptSubmit it also shows the owner the questions their agents are waiting on (Queue/asks, written by the Discord
bridge): each one new to this session, and each one it showed that has since been settled elsewhere, as context for the
session, which lists them for the owner and closes them with agent-mail-mcp.py answer_question or settle_question. So a
question is asked both in Discord and in the session the owner is using, and an answer at either end closes the other. Config
"asks_in_sessions" in Queue/discord/config.json: "all" (default), "project" (only this session's project) or "off".
That is the only output; everything else prints nothing.

  session-hook.py                     Claude Code hook, JSON on stdin
  session-hook.py --codex '<json>'    Codex `notify` program (agent-turn-complete): tool codex, state idle
  session-hook.py --codex-hook        Codex hook (UserPromptSubmit, JSON on stdin): as the Claude hook, tool codex
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
    for k in ("prompt_at", "last_reply", "prompts", "transcript", "asks_seen"):  # kept across events: the bridge and answer_question read them
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

def handle(d, tool="claude"):
    """One Claude Code hook payload (or a Codex hook's, which has the same shape)."""
    ev, sid = str(d.get("hook_event_name") or ""), d.get("session_id")
    if not sid: return
    fields = {}
    if ev in EVENT_STATE: fields["state"] = EVENT_STATE[ev]
    if d.get("transcript_path"): fields["transcript"] = str(d["transcript_path"])  # answer_question reads the owner's answers to the app's question dialog from it
    if ev == "UserPromptSubmit" and real_prompt(d.get("prompt")):
        fields["last_prompt"], fields["prompt_at"] = clip(d.get("prompt")), time.time()  # the bridge settles a question this session asked once the owner types here
        try: old = json.loads(session_path(sid).read_text(encoding="utf-8")).get("prompts") or []
        except (OSError, ValueError, AttributeError): old = []
        fields["prompts"] = (old + [[round(time.time(), 1), clip(d.get("prompt"), 600)]])[-8:]  # answer_question checks the owner's quoted words against these
    elif ev == "Stop":
        r = last_reply(d.get("transcript_path"))
        if r: fields["last_reply"] = r
    elif ev == "Notification":
        msg, kind = clip(d.get("message")), str(d.get("notification_type") or "")
        fields.update(last_note=msg, note_type=kind)
        if kind in NEEDS_TYPES or (not kind and NEEDS_RE.search(msg)): fields["state"] = "needs_you"
    # A session that never took a prompt has no file; its end, or a stray event, is not worth creating one.
    rec = update(sid, d.get("cwd"), tool=tool, create="last_prompt" in fields or fields.get("state") == "needs_you", **fields)  # listed once the owner typed or it needs them
    _log(f"{ev} {sid} -> {rec.get('state') if rec else 'no file'}")
    if ev == "UserPromptSubmit" and rec and "last_prompt" in fields:
        text, seen = asks_context(rec)
        if text:
            update(sid, d.get("cwd"), tool=tool, create=False, asks_seen=seen)
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}))

ASKS_SHOWN = 6  # at most this many new questions per prompt; the rest come on the next one
def ago(t):
    s = max(0, time.time() - float(t or 0))
    return f"{int(s // 60)} min" if s < 3600 else f"{s / 3600:.0f} h" if s < 86400 else f"{s / 86400:.0f} days"

def asked_here(q, rec):
    """True when this session asked the question itself (agent-mail-mcp.py ask_owner records where): it is already in this chat."""
    from common import queue_root
    try:
        reg = json.loads((queue_root() / "mail" / "agents.json").read_text(encoding="utf-8")); info = (reg.get("agents", reg) if isinstance(reg, dict) else {}).get(q.get("from")) or {}
        if info.get("auto"): return False  # a runner's question: no session of the owner's asked it, whatever folder the run was in
        o = json.loads((queue_root() / "mail" / "origins" / f"{q['id']}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, AttributeError): return False
    norm = lambda x: os.path.normcase(os.path.normpath(x))
    return o.get("host") == rec.get("host") and bool({norm(x) for x in (o.get("cwd"), o.get("root")) if x} & {norm(x) for x in (rec.get("cwd"), rec.get("root")) if x})

def asks_context(rec):
    """(text for the session, updated asks_seen) or (None, seen): the open questions this session has not shown yet, this
    project's first, and the ones it showed that were settled since, with how."""
    from common import open_questions, project_key, queue_root
    seen = dict(rec.get("asks_seen") or {})
    try: mode = str(json.loads((queue_root() / "discord" / "config.json").read_text(encoding="utf-8")).get("asks_in_sessions") or "all")
    except (OSError, ValueError, AttributeError): mode = "all"
    if mode == "off": return None, seen
    doc, here = open_questions(), project_key(rec.get("project"))
    mine = lambda q: project_key(q.get("project")) == here
    mark = lambda q: f"open@{int(q.get('rev') or 0)}"  # an amended question is shown again
    new = [q for q in doc.get("open") or [] if isinstance(q, dict) and q.get("id") and seen.get(q["id"]) != mark(q) and (mode != "project" or mine(q))]
    new.sort(key=lambda q: (not mine(q), -float(q.get("at") or 0)))
    closed = {c.get("id"): c for c in doc.get("closed") or [] if isinstance(c, dict)}
    gone = [closed[k] for k, v in seen.items() if str(v).startswith("open") and k in closed]
    if not new and not gone: return None, seen
    lines = []
    if new:
        lines.append("Questions the owner's agents are waiting on. Each is also a card in Discord; an answer at either end closes the other.")
        for n, q in enumerate(new[:ASKS_SHOWN], 1):
            opts = " | ".join(f"{i}: {o}" for i, o in enumerate(q.get("options") or []))
            again = " (CHANGED since this session showed it: answer the new wording)" if str(seen.get(q["id"], "")).startswith("open") else ""
            mine_too = " (asked from this folder: if this session asked it, it is already in this chat)" if asked_here(q, rec) else ""
            lines.append(f"{n}. [{q['id']}] {q.get('project') or 'no project'}, asked {ago(q.get('at'))} ago by {q.get('from')}{again}{mine_too}: {clip(' '.join(str(q.get('question') or q.get('subject') or '').split()), 320)}")
            lines.append(f"   Options: {opts or '(free answer)'}" + (" (moves work between devices: answer this one in Discord)" if q.get("device_choice") else "") + (f"  Discord: {q['link']}" if q.get("link") else ""))
            seen[q["id"]] = mark(q)
        if len(new) > ASKS_SHOWN: lines.append(f"({len(new) - ASKS_SHOWN} more come with their next message, or call list_questions.)")
    if gone:
        lines.append("Settled elsewhere since this session showed them; do not ask the owner about them again: " + "; ".join(f"[{c['id']}] {clip(c.get('subject') or '', 80)}: {clip(c.get('how') or '', 160)}" for c in gone))
        for c in gone: seen[c["id"]] = "closed"
    if new:
        lines.append("Do what the owner asked first. Then end this reply with these under \"Waiting on you\", one line each with their options, "
                     "and put the one for this project in your question box if you have one. When they answer one, here or later, call the agent_mail "
                     "tool answer_question with its id, their exact words and the option number their words pick. If your work here already "
                     "did what one asks or made it moot, call settle_question with one line on how, instead of asking them.")
    for k in [k for k, v in seen.items() if v == "closed"][:-200]: seen.pop(k)  # the file stays small
    return "\n".join(lines), seen

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
        if isinstance(d, dict): handle(d, tool="codex" if argv[:1] == ["--codex-hook"] else "claude")
    cleanup()

if __name__ == "__main__":
    try: main(sys.argv[1:])
    except BaseException as e:  # a hook must never fail the session: no output, exit 0
        _log(f"error {type(e).__name__}: {e}")
    sys.exit(0)
