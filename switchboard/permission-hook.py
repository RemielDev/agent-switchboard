#!/usr/bin/env python3
"""Remote permission approval for Claude Code: a PermissionRequest hook (stdlib only, macOS and Windows).

Claude Code runs this when it is about to show a permission dialog. If the owner has used this device's keyboard or
mouse in the last 180 seconds it exits at once with no output, so the normal dialog is the only thing that
happens. If they have been away longer it mails them the request (agent `sessions` -> `owner`, which the Discord
bridge shows with the buttons Allow once / Deny) and waits up to 8 minutes for the answer:

  Allow once -> {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
  Deny       -> the same with {"behavior": "deny", "message": ...}
  no answer, any other answer, any error -> no output, exit 0: the local dialog decides

It never allows anything by itself. In the main interactive session the local dialog stays on screen while this
waits, and whichever answers first wins. While it waits the session file (session-hook.py) says needs_you.
Unattended runner turns (AGENT_MAIL_AGENT or SESSION_HOOK_SKIP set) and tools that need an answer typed at the
device (AskUserQuestion, ExitPlanMode) are left alone.

Environment: SESSION_HOOK_FORCE_IDLE=1 skips the idle check (testing), SESSION_HOOK_IDLE_SECONDS (180),
SESSION_HOOK_WAIT_SECONDS (480)."""
import importlib.util, json, os, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SENDER, HUMAN = "sessions", "owner"
ALLOW, DENY = "Allow once", "Deny"
LOCAL_ONLY = {"AskUserQuestion", "ExitPlanMode"}  # allowing these needs an answer in updatedInput
SUMMARY_MAX = 400
KEYS = ("command", "file_path", "notebook_path", "path", "url", "pattern", "query", "skill", "prompt")
SECRETS = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ***"),
    (re.compile(r"(?i)([\w-]*(?:token|secret|password|passwd|api[_-]?key|authorization)[\w-]*[\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"'&;]+)"), r"\1***"),
    (re.compile(r"(?i)(--?[\w-]*(?:token|secret|password|passwd|api[_-]?key)[\w-]*\s+)(\"[^\"]*\"|'[^']*'|\S+)"), r"\1***"),
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})"), "***"),
)


def env_seconds(name, default):
    try: return max(0.0, float(os.environ.get(name, default)))
    except ValueError: return float(default)

def load(name, filename):
    """Import a sibling script whose file name is not a module name (as mail-runner.py does for agent-mail.py)."""
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

def idle_seconds():
    """Seconds since the last keyboard or mouse input on this device, or None when that cannot be read."""
    try:
        if os.name == "nt":
            import ctypes
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
            info = LASTINPUTINFO(); info.cbSize = ctypes.sizeof(info)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)): return None
            return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
        if sys.platform == "darwin":
            out = subprocess.run(["/usr/sbin/ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=5).stdout
            m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
            return int(m.group(1)) / 1e9 if m else None
    except Exception:
        pass
    return None

def redact(text):
    for rx, to in SECRETS: text = rx.sub(to, text)
    return text

def summarize(tool_input):
    """The command, file path or URL of a tool call on one line, secrets masked, at most SUMMARY_MAX characters."""
    text = ""
    if isinstance(tool_input, dict):
        text = next((str(tool_input[k]) for k in KEYS if tool_input.get(k)), "")
        if not text and tool_input: text = json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))
    elif tool_input: text = str(tool_input)
    text = redact(" ".join(text.split())) or "(no input)"
    if len(text) > SUMMARY_MAX:
        tail = f" ... [cut, {len(text)} characters in all]"
        text = text[:SUMMARY_MAX - len(tail)] + tail
    return text

def sweep(mail):
    """Ack answers nobody is waiting for any more (a button pressed after the wait ended)."""
    for m in mail.list_inbox(SENDER):
        try: age = time.time() - datetime.strptime(m["id"][:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
        except (KeyError, ValueError): continue
        if age > 3600:
            try: mail.ack(SENDER, m["id"], result="expired: no session was waiting")
            except mail.MailError: pass

def decide(d):
    """Return the decision object for this PermissionRequest payload, or None to leave it to the local dialog."""
    tool = str(d.get("tool_name") or "")
    if d.get("hook_event_name") != "PermissionRequest" or not tool or tool in LOCAL_ONLY: return None
    if os.environ.get("AGENT_MAIL_AGENT") or os.environ.get("SESSION_HOOK_SKIP"): return None
    if os.environ.get("SESSION_HOOK_FORCE_IDLE") != "1":
        idle = idle_seconds()
        if idle is None or idle < env_seconds("SESSION_HOOK_IDLE_SECONDS", 180): return None

    from common import hostname
    mail, sess = load("agent_mail", "agent-mail.py"), load("session_hook", "session-hook.py")
    host, sid, cwd = hostname(), d.get("session_id"), str(d.get("cwd") or "")
    project = sess.project_name(os.environ.get("CLAUDE_PROJECT_DIR") or cwd) or "?"
    summary, wait = summarize(d.get("tool_input")), env_seconds("SESSION_HOOK_WAIT_SECONDS", 480)

    def note(state, text):
        if not sid: return
        try: sess.update(sid, cwd, state=state, last_note=sess.clip(text), note_type="remote_permission")
        except Exception: pass

    if SENDER not in {k.lower() for k in mail.load_agents()}:
        mail.register(SENDER, "any", "human", auto=False,
                      description="Interactive session hooks. Sends the owner permission requests; not a mailbox to write to.")
    sweep(mail)
    body = (f"Host: {host}\nProject: {project}\nTool: {tool}\nRequest: {summary}\n\n"
            f"Answer within {max(1, round(wait / 60))} minutes. After that, or if you answer at the device first, "
            f"the buttons do nothing.")
    msg = mail.send(SENDER, HUMAN, f"Permission: {tool} in {project} on {host}", body, needs_reply=True,
                    options=[{"label": ALLOW}, {"label": DENY}])
    note("needs_you", f"Asked the owner remotely: {tool}: {summary}")

    deadline = time.monotonic() + wait
    while True:
        left = deadline - time.monotonic()
        if left <= 0: break
        answer = mail.wait(SENDER, reply_to=msg["id"], timeout=min(30.0, left), interval=2.0)
        if answer is None: continue
        said = " ".join(str(answer.get("body") or "").split()).lower()
        try: mail.ack(SENDER, answer["id"], result=f"permission answer: {said[:40]}")
        except mail.MailError: pass
        if str(answer.get("from", "")).lower() != HUMAN: continue  # only the owner's own answer counts
        if said.startswith(ALLOW.lower()):
            note("working", f"The owner allowed remotely: {tool}: {summary}")
            return {"behavior": "allow"}
        if said.startswith(DENY.lower()):
            note("working", f"The owner denied remotely: {tool}: {summary}")
            return {"behavior": "deny", "message": "The owner denied this request remotely."}
        break  # the owner answered something else: leave it to the local dialog
    try: mail.ack(HUMAN, msg["id"], result="expired: no remote decision", by=SENDER)  # only if never shown to the owner
    except mail.MailError: pass
    return None

def main():
    decision = None
    try:
        raw = b"" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read()
        d = json.loads(raw.decode("utf-8", "replace") or "{}")
        if isinstance(d, dict): decision = decide(d)
    except BaseException:  # any failure leaves the decision to the local dialog
        decision = None
    if decision:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}))
        sys.stdout.flush()

if __name__ == "__main__":
    main()
    sys.exit(0)
