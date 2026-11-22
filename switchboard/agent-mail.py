#!/usr/bin/env python3
"""Agent-to-agent mail over the shared <root>/Queue/mail folder (stdlib only, macOS and Windows).

Layout:
  Queue/mail/agents.json              registry {name: {host, kind, auto, cwd, description}}
  Queue/mail/<agent>/inbox/<id>.json  unread messages, one JSON file each
  Queue/mail/<agent>/done/<id>.json   acked messages (adds acked_at, acked_by, result)
  Queue/mail/roles/<agent>.md         optional role text read by mail-runner.py

Message id: <UTC yyyymmddThhmmssZ>-<8 hex>. Fields: id, from, to, subject, body, repo, reply_to, thread,
hops, needs_reply, created. Exit codes: 0 ok, 1 error, 2 wait timed out (or bad arguments).

  agent-mail.py agents [--json]
  agent-mail.py register NAME --host H --kind claude|codex|cursor|human [--auto] [--cwd P] [--description T]
  agent-mail.py send --from A --to B --subject S (--body T | --body-file F) [--repo R] [--reply-to ID] [--needs-reply]
  agent-mail.py inbox AGENT [--json]
  agent-mail.py read AGENT ID [--json]
  agent-mail.py ack AGENT ID [--result T]
  agent-mail.py reply AGENT ID (--body T | --body-file F) [--needs-reply]
  agent-mail.py wait AGENT --reply-to ID [--timeout S] [--interval S]
  agent-mail.py thread ID [--json]

Mail from a person (kind human) needs a person: the CLI refuses send, reply, amend or register as one unless it runs
at a real terminal outside any agent. Programs that speak for the owner (discord-bridge.py, improve-loop.py,
permission-hook.py) call send() directly.
"""
import argparse, json, os, re, secrets, sys, time
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import dev_root, queue_root, repo_dir

KINDS = ("claude", "codex", "cursor", "human")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
# Present in an agent's shell; see _person_at_terminal.
AGENT_ENV = ("AGENT_MAIL_AGENT", "AGENT_MAIL_PARENT", "SESSION_HOOK_SKIP", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
             "CODEX_SANDBOX", "CODEX_THREAD_ID", "CURSOR_AGENT")
# Folder names that cannot be agents: our own, plus Windows device names.
RESERVED = {"roles", "agents", "con", "prn", "aux", "nul"} | {f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)}


class MailError(Exception):
    """A user-facing mail error (unknown agent, missing message, bad input)."""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def new_id():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)

def mail_root():
    r = queue_root() / "mail"
    r.mkdir(parents=True, exist_ok=True)
    return r

def _retry(fn, tries=20, delay=0.1):
    """Windows refuses replace/unlink while a scanner or Syncthing holds the file; retry briefly."""
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1: raise
            time.sleep(delay)

def write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{secrets.token_hex(4)}.tmp"
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n"); f.flush(); os.fsync(f.fileno())
        _retry(lambda: os.replace(tmp, path))
    finally:
        try: tmp.unlink()
        except OSError: pass

def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---- registry ----

def load_agents():
    p = mail_root() / "agents.json"
    if not p.is_file(): return {}
    try:
        data = _load(p)
    except (OSError, ValueError) as e:
        raise MailError(f"cannot read registry {p}: {e}")
    if not isinstance(data, dict): raise MailError(f"registry {p} is not a JSON object")
    return data

def resolve_agent(name, agents=None):
    """Return the registered spelling of NAME (case-insensitive), or raise MailError."""
    agents = load_agents() if agents is None else agents
    name = str(name or "").strip()
    if name in agents: return name
    for k in agents:
        if k.lower() == name.lower(): return k
    known = ", ".join(sorted(agents)) or "none"
    raise MailError(f"unknown agent {name!r} (registered: {known})")

def agent_dir(name):
    d = mail_root() / name
    (d / "inbox").mkdir(parents=True, exist_ok=True)
    (d / "done").mkdir(parents=True, exist_ok=True)
    return d

def register(name, host, kind, auto=False, cwd=None, description=""):
    name = str(name or "").strip()
    if not NAME_RE.match(name) or name.lower() in RESERVED:
        raise MailError(f"bad agent name {name!r}: use letters, digits, - and _ (max 64), not a reserved name")
    if kind not in KINDS: raise MailError(f"bad kind {kind!r}: one of {', '.join(KINDS)}")
    if not str(host or "").strip(): raise MailError("host is required")
    agents = load_agents()
    for k in list(agents):
        if k.lower() == name.lower() and k != name: name = k  # keep the first spelling
    hosts = [h.strip() for h in str(host).split(",") if h.strip()]
    agents[name] = {"host": hosts[0] if len(hosts) == 1 else hosts, "kind": kind, "auto": bool(auto),
                    "cwd": str(cwd) if cwd else None, "description": description or ""}
    write_json_atomic(mail_root() / "agents.json", dict(sorted(agents.items())))
    agent_dir(name)
    return name, agents[name]


# ---- messages ----

def _check_id(mid):
    mid = str(mid or "").strip()
    if not ID_RE.match(mid): raise MailError(f"bad message id {mid!r}")
    return mid

def _boxes():
    """Every <agent>/<box> folder that exists, registered or not; done before inbox."""
    for d in sorted(mail_root().iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name == "roles": continue
        for box in ("done", "inbox"):
            if (d / box).is_dir(): yield d / box

def find_anywhere(mid):
    """Return the message with this id from any agent's done or inbox, or None."""
    mid = _check_id(mid)
    for box in _boxes():
        p = box / f"{mid}.json"
        if p.is_file():
            try: return _load(p)
            except (OSError, ValueError): continue
    return None

def _sort_key(m):
    return (str(m.get("created", "")), int(m.get("hops") or 0), str(m.get("id", "")))

def send(sender, to, subject, body, repo=None, reply_to=None, needs_reply=False, check_repo=True, device=None, options=None, control=None, new_thread=False):
    agents = load_agents()
    sender, to = resolve_agent(sender, agents), resolve_agent(to, agents)
    if sender == to: raise MailError("sender and recipient are the same agent")
    subject = " ".join(str(subject or "").split())
    if not subject: raise MailError("subject is empty")
    body = str(body or "")
    if not body.strip(): raise MailError("body is empty")
    repo = str(repo).strip() if repo else None
    if repo:
        if repo in (".", "..") or Path(repo).name != repo or "/" in repo or "\\" in repo:
            raise MailError(f"bad repo {repo!r}: give a folder name under the dev root, not a path")
        if check_repo and not repo_dir(repo).is_dir():
            raise MailError(f"unknown repo folder {repo!r} under {dev_root()}")
    human, running = agents.get(sender, {}).get("kind") == "human", os.environ.get("AGENT_MAIL_PARENT")
    thread, hops, from_thread = None, 0, None
    if reply_to:
        reply_to = _check_id(reply_to)
        parent = find_anywhere(reply_to)
        if not parent: raise MailError(f"reply_to message {reply_to} not found")
        thread, hops = parent.get("thread") or parent["id"], int(parent.get("hops") or 0) + 1
    elif running and new_thread and not human:
        # Sent from inside a runner turn as separate work: a thread of its own that remembers where it came from.
        parent = find_anywhere(running)
        if parent: hops, from_thread = int(parent.get("hops") or 0) + 1, parent.get("thread") or parent["id"]
    elif running and not human:
        # Sent from inside a runner turn: stay in that message's thread so history and the hop limit carry over.
        # Not for mail from a person: improve-loop.py runs in the runner process, where this value is left over from the last turn.
        parent = find_anywhere(running)
        if parent: thread, hops = parent.get("thread") or parent["id"], int(parent.get("hops") or 0) + 1
    if human and not running: hops = 0  # the hop limit stops agents looping, not a person's conversation
    inbox_dir = agent_dir(to) / "inbox"
    mid = new_id()
    while (inbox_dir / f"{mid}.json").exists(): mid = new_id()
    msg = {"id": mid, "from": sender, "to": to, "subject": subject, "body": body, "repo": repo,
           "reply_to": reply_to or None, "thread": thread or mid, "hops": hops, "needs_reply": bool(needs_reply),
           "created": now_iso()}
    if from_thread: msg["from_thread"] = from_thread  # split off from that thread; the bridge links the two
    if device: msg["device"] = str(device).strip()  # run on this device only
    if options:  # choices for a human: [{"label": ..., "device"?: host, "action"?: {...}}]
        msg["options"] = [o if isinstance(o, dict) else {"label": str(o)} for o in options][:5]
    if control: msg["control"] = control  # changes an earlier message instead of being shown itself
    write_json_atomic(inbox_dir / f"{mid}.json", msg)
    return msg

def amend(sender, target, text=None):
    """Replace the text of an earlier message to the owner, or delete it when TEXT is empty. Only its sender can."""
    target = _check_id(target)
    return send(sender, "owner", f"[amend] {target}", text or "(deleted)", check_repo=False,
                control={"target": target, "op": "update" if text else "delete"})

def answer(sender, target, option, why=""):
    """Answer one of the owner's open questions with option number OPTION, as their recommendation stand-in. The bridge carries it
    out only while the owner's own "do your recommendations" go-ahead covers that question, and never for a risky button."""
    target = _check_id(target)
    return send(sender, "owner", f"[answer] {target}", why or "(no reason given)", check_repo=False,
                control={"target": target, "op": "answer", "option": int(option)})

def go_ahead(sender, text="Do your recommendations"):
    """The owner, typing in an interactive agent session, tells the orchestrator to use its recommendations. The bridge
    takes this only from an interactive agent, never from a runner."""
    return send(sender, "owner", "[go-ahead]", text, check_repo=False, control={"target": "-", "op": "go_ahead"})

def redirect(agent, mid, device):
    """Move a waiting message to another device (or clear the pin with device='')."""
    m, box = get(agent, mid)
    if box != "inbox": raise MailError(f"message {mid} is already handled")
    if device: m["device"] = device
    else: m.pop("device", None)
    write_json_atomic(agent_dir(resolve_agent(agent)) / "inbox" / f"{m['id']}.json", m)
    return m

def list_inbox(agent, strict=False):
    """Unacked messages for AGENT, oldest first. An unreadable file is skipped with a warning, or raises MailError when
    STRICT, for callers that must not mistake "could not read" for "no mail"."""
    agent = resolve_agent(agent)
    d = agent_dir(agent)
    out = []
    for p in (d / "inbox").glob("*.json"):
        if not ID_RE.match(p.stem) or (d / "done" / p.name).exists(): continue  # skips temp, conflict, half-acked
        try:
            m = _load(p)
        except FileNotFoundError:
            continue  # acked between the listing and the read
        except (OSError, ValueError) as e:
            if strict: raise MailError(f"cannot read {p}: {e}")
            print(f"agent-mail: skipping unreadable {p}: {e}", file=sys.stderr); continue
        if isinstance(m, dict): out.append(m)
        elif strict: raise MailError(f"{p} is not a message")
    return sorted(out, key=_sort_key)

def get(agent, mid):
    """Return (message, 'inbox'|'done') for a message addressed to AGENT."""
    agent, mid = resolve_agent(agent), _check_id(mid)
    d = agent_dir(agent)
    for box in ("done", "inbox"):
        p = d / box / f"{mid}.json"
        if p.is_file():
            try: return _load(p), box
            except (OSError, ValueError) as e: raise MailError(f"cannot read {p}: {e}")
    raise MailError(f"message {mid} not found for agent {agent}")

def ack(agent, mid, result="", by=None):
    """Move a message from AGENT's inbox to done, recording acked_at, acked_by and result."""
    agent, mid = resolve_agent(agent), _check_id(mid)
    d = agent_dir(agent)
    src, dst = d / "inbox" / f"{mid}.json", d / "done" / f"{mid}.json"
    if dst.is_file():
        if src.is_file(): _retry(src.unlink)  # finish an interrupted ack
        raise MailError(f"message {mid} is already acked")
    if not src.is_file(): raise MailError(f"message {mid} not found in {agent}'s inbox")
    try: msg = _load(src)
    except (OSError, ValueError) as e: raise MailError(f"cannot read {src}: {e}")
    msg.update({"acked_at": now_iso(), "acked_by": by or agent, "result": result or ""})
    write_json_atomic(dst, msg)
    _retry(src.unlink)
    return msg

def reply(agent, mid, body, needs_reply=False, by=None):
    """Send BODY to the original sender, then ack the original (if it is still in the inbox)."""
    agent = resolve_agent(agent)
    orig, box = get(agent, mid)
    subject = str(orig.get("subject") or "")
    if not subject.lower().startswith("re:"): subject = "Re: " + subject
    new = send(agent, orig["from"], subject, body, repo=orig.get("repo"), reply_to=orig["id"],
               needs_reply=needs_reply, check_repo=False)
    if box == "inbox": ack(agent, orig["id"], result=f"replied {new['id']}", by=by)
    return new

def wait(agent, reply_to, timeout=300.0, interval=1.0):
    """Block until AGENT's inbox holds a message whose reply_to is REPLY_TO; return it, or None on timeout."""
    agent, reply_to = resolve_agent(agent), _check_id(reply_to)
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        for m in list_inbox(agent):
            if m.get("reply_to") == reply_to: return m
        left = deadline - time.monotonic()
        if left <= 0: return None
        time.sleep(min(max(0.1, float(interval)), left))

def thread(mid):
    """All messages of the thread, across every agent's inbox and done, ordered by created."""
    mid = _check_id(mid)
    seed = find_anywhere(mid)
    tid = (seed.get("thread") or seed["id"]) if seed else mid
    found = {}
    for box in _boxes():  # done is listed before inbox, so the acked copy wins
        for p in box.glob("*.json"):
            if not ID_RE.match(p.stem) or p.stem in found: continue
            try: m = _load(p)
            except (OSError, ValueError): continue
            if isinstance(m, dict) and (m.get("thread") == tid or m.get("id") == tid): found[p.stem] = m
    if not found: raise MailError(f"no messages found for thread {tid}")
    return sorted(found.values(), key=_sort_key)


# ---- CLI ----

def _dump(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))

def _line(m):
    tags = (" [needs-reply]" if m.get("needs_reply") else "") + (f" [repo={m['repo']}]" if m.get("repo") else "")
    tags += f" [re {m['reply_to']}]" if m.get("reply_to") else ""
    return f"{m.get('id')}  {m.get('from')} -> {m.get('to')}  hops={m.get('hops', 0)}{tags}  {m.get('subject')}"

def _show(m):
    for k in ("id", "from", "to", "subject", "repo", "reply_to", "thread", "hops", "needs_reply", "created",
              "acked_at", "acked_by", "result"):
        if k in m and m[k] is not None: print(f"{k}: {m[k]}")
    print(); print(m.get("body", ""))

def _person_at_terminal():
    """True only for someone typing at a real terminal. Agent shells carry AGENT_ENV or have no terminal attached."""
    if any(os.environ.get(v) for v in AGENT_ENV): return False
    try: return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError): return False

def _require_person(name, agents=None):
    """Refuse to let the CLI speak as a person from anywhere but that person's own terminal."""
    agents = load_agents() if agents is None else agents
    name = resolve_agent(name, agents)
    if agents[name].get("kind") == "human" and not _person_at_terminal():
        raise MailError(f"refused: {name} is a person, and this is not their terminal. Send as yourself.")

def _body(a):
    if a.body is not None: return a.body
    if a.body_file == "-": return sys.stdin.read()
    try: return Path(a.body_file).read_text(encoding="utf-8-sig")
    except (OSError, ValueError) as e: raise MailError(f"cannot read body file: {e}")

def _add_body(sp):
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--body"); g.add_argument("--body-file", help="UTF-8 file, or - for stdin")

def main(argv=None):
    for s in (sys.stdout, sys.stderr, sys.stdin):
        try: s.reconfigure(encoding="utf-8", errors="replace")
        except Exception: pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("agents"); sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("register"); sp.add_argument("name"); sp.add_argument("--host", required=True)
    sp.add_argument("--kind", required=True, choices=KINDS); sp.add_argument("--auto", action="store_true")
    sp.add_argument("--cwd"); sp.add_argument("--description", default="")
    sp = sub.add_parser("send"); sp.add_argument("--from", dest="sender", required=True)
    sp.add_argument("--to", required=True); sp.add_argument("--subject", required=True); _add_body(sp)
    sp.add_argument("--repo"); sp.add_argument("--reply-to"); sp.add_argument("--needs-reply", action="store_true")
    sp.add_argument("--device", help="run on this device only"); sp.add_argument("--option", action="append", help="a choice for a human; repeatable")
    sp = sub.add_parser("redirect"); sp.add_argument("agent"); sp.add_argument("id"); sp.add_argument("--device", default="")
    sp = sub.add_parser("amend", help="update or delete an earlier message to owner"); sp.add_argument("--from", dest="sender", required=True)
    sp.add_argument("id"); sp.add_argument("--text", help="new text; leave out to delete")
    sp = sub.add_parser("answer", help="answer the owner's open question with your recommendation, once they said to"); sp.add_argument("--from", dest="sender", required=True)
    sp.add_argument("id"); sp.add_argument("option", type=int); sp.add_argument("--why", default="")
    sp = sub.add_parser("go-ahead", help="from an interactive session the owner drives: let the orchestrator use its recommendations"); sp.add_argument("--from", dest="sender", required=True)
    sp.add_argument("--text", default="Do your recommendations")
    sp = sub.add_parser("inbox"); sp.add_argument("agent"); sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("read"); sp.add_argument("agent"); sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("ack"); sp.add_argument("agent"); sp.add_argument("id"); sp.add_argument("--result", default="")
    sp = sub.add_parser("reply"); sp.add_argument("agent"); sp.add_argument("id"); _add_body(sp)
    sp.add_argument("--needs-reply", action="store_true")
    sp = sub.add_parser("wait"); sp.add_argument("agent"); sp.add_argument("--reply-to", required=True)
    sp.add_argument("--timeout", type=float, default=300.0); sp.add_argument("--interval", type=float, default=1.0)
    sp = sub.add_parser("thread"); sp.add_argument("id"); sp.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "agents":
            agents = load_agents()
            if a.json: _dump(agents); return 0
            if not agents: print("no agents registered"); return 0
            for name, i in sorted(agents.items()):
                n = len(list_inbox(name))
                print(f"{name}  kind={i.get('kind')}  host={i.get('host')}  auto={'yes' if i.get('auto') else 'no'}"
                      f"  inbox={n}" + (f"  cwd={i['cwd']}" if i.get("cwd") else "")
                      + (f"  {i['description']}" if i.get("description") else ""))
        elif a.cmd == "register":
            agents = load_agents(); known = next((v for k, v in agents.items() if k.lower() == a.name.strip().lower()), {})
            if "human" in (a.kind, known.get("kind")) and not _person_at_terminal():
                raise MailError("refused: only a person at a real terminal can register or change a human mailbox")
            name, info = register(a.name, a.host, a.kind, a.auto, a.cwd, a.description)
            if info["auto"] and info["kind"] != "claude":
                print("warning: mail-runner only runs agents of kind claude; auto has no effect", file=sys.stderr)
            print(f"registered {name}: " + json.dumps(info, ensure_ascii=False))
        elif a.cmd == "send":
            _require_person(a.sender)
            print(send(a.sender, a.to, a.subject, _body(a), a.repo, a.reply_to, a.needs_reply, device=a.device, options=a.option)["id"])
        elif a.cmd == "redirect":
            m = redirect(a.agent, a.id, a.device); print(f"{m['id']} now runs on {m.get('device') or 'any device'}")
        elif a.cmd == "amend":
            _require_person(a.sender)
            print(amend(a.sender, a.id, a.text)["id"])
        elif a.cmd == "go-ahead":
            _require_person(a.sender)
            print(go_ahead(a.sender, a.text)["id"])
        elif a.cmd == "answer":
            _require_person(a.sender)
            print(answer(a.sender, a.id, a.option, a.why)["id"])
        elif a.cmd == "inbox":
            msgs = list_inbox(a.agent)
            if a.json: _dump(msgs)
            elif not msgs: print("inbox empty")
            else:
                for m in msgs: print(_line(m))
                print(f"(open one with: {Path(sys.argv[0]).name} read {a.agent} <id>)", file=sys.stderr)
        elif a.cmd == "read":
            m, box = get(a.agent, a.id)
            if a.json: _dump(m)
            else: print(f"box: {box}"); _show(m)
        elif a.cmd == "ack":
            m = ack(a.agent, a.id, a.result)
            print(f"acked {m['id']}")
        elif a.cmd == "reply":
            _require_person(a.agent)
            print(reply(a.agent, a.id, _body(a), a.needs_reply)["id"])
        elif a.cmd == "wait":
            m = wait(a.agent, a.reply_to, a.timeout, a.interval)
            if m is None:
                print(f"timeout: no reply to {a.reply_to} within {a.timeout:g}s", file=sys.stderr); return 2
            _dump(m)
        elif a.cmd == "thread":
            msgs = thread(a.id)
            if a.json: _dump(msgs)
            else:
                for m in msgs:
                    print(("done   " if m.get("acked_at") else "inbox  ") + _line(m))
        return 0
    except MailError as e:
        print(f"error: {e}", file=sys.stderr); return 1

if __name__ == "__main__": sys.exit(main())
