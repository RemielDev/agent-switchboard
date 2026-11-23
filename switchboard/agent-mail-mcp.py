#!/usr/bin/env python3
"""MCP stdio server for agent mail (newline-delimited JSON-RPC 2.0, protocol 2024-11-05, stdlib only).

  agent-mail-mcp.py --agent NAME

NAME is the caller's fixed identity: every tool sends, reads and acks as that agent. If --agent is omitted the
AGENT_MAIL_AGENT environment variable is used (mail-runner.py sets it for the Claude run it starts).
Example Claude Code entry:  claude mcp add agent_mail -- python3 /path/agent-mail-mcp.py --agent mac-claude
"""
import argparse, importlib.util, json, os, re, sys, time
from pathlib import Path

PROTOCOL = "2024-11-05"
MAX_WAIT = 45

def load_mail():
    spec = importlib.util.spec_from_file_location("agent_mail", Path(__file__).resolve().with_name("agent-mail.py"))
    mod = importlib.util.module_from_spec(spec); sys.modules["agent_mail"] = mod
    spec.loader.exec_module(mod)
    return mod

def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}

_ID = {"type": "string", "description": "Message id, for example 20250301T181500Z-3fa9c2d1."}

TOOLS = [
    {"name": "list_agents",
     "description": "List every agent in the mail registry: name, kind (claude, codex, cursor or human), host, "
                    "whether a runner answers its mail automatically (auto), working folder and description. "
                    "Use it to find who to write to. Your own entry is marked with is_you.",
     "inputSchema": _schema({})},
    {"name": "send_message",
     "description": "Send a new message from you to another registered agent and return its id. Set needs_reply "
                    "to true when you expect an answer, then call wait_for_reply with the returned id. Set repo "
                    "to a repo folder name under the dev root when the work belongs to one repo; an automatic "
                    "recipient then runs in that folder. An automatic agent answers within minutes; a human or "
                    "a non-automatic agent may take much longer.",
     "inputSchema": _schema({
         "to": {"type": "string", "description": "Recipient agent name from list_agents."},
         "subject": {"type": "string", "description": "Short one-line subject."},
         "body": {"type": "string", "description": "Full message. Include everything the recipient needs; "
                                                   "it does not see your conversation."},
         "repo": {"type": "string", "description": "Optional repo folder name under the dev root."},
         "reply_to": {"type": "string", "description": "Optional message id: the message continues THAT message's thread "
                                                       "(for example another open thread this work belongs in) instead of yours."},
         "new_thread": {"type": "boolean", "description": "True to start separate work in a thread of its own (its own Discord "
                                                          "thread, linked to yours), for a piece that should not run inside this one. Default false."},
         "needs_reply": {"type": "boolean", "description": "True if you expect an answer. Default false."},
         "device": {"type": "string", "description": "Optional host name from list_devices. The recipient then runs on "
                                                     "that device only. Leave out to use the first available device."},
         "area": {"type": "string", "description": "For work that writes files outside a repo: a short slug for what it "
                                                   "changes, such as discord-bridge or mail-runner. Work in the same "
                                                   "area or repo runs one at a time across all devices; different "
                                                   "areas run side by side. Leave out for read-only work."}},
         ["to", "subject", "body"])},
    {"name": "list_active_work",
     "description": "Every thread with unfinished work on any device: which agent holds each message, its repo or "
                    "area, and whether it is running, queued or waiting for the owner. Check it before starting work that "
                    "could overlap or contradict something already in flight.",
     "inputSchema": _schema({})},
    {"name": "report_conflict",
     "description": "Tell the owner, once, that two or more threads overlap or contradict each other. They get one message "
                    "that follows all of them and updates as they move. Give the thread ids, a summary of the clash "
                    "in a few plain sentences with your recommendation, and 2 to 5 options. A pair already reported in "
                    "the last day is not sent again. End your turn after calling it.",
     "inputSchema": _schema({
         "threads": {"type": "array", "items": {"type": "string"}, "description": "Thread ids from list_active_work, two or more."},
         "summary": {"type": "string", "description": "What clashes and what you recommend."},
         "options": {"type": "array", "items": {"type": "string"}, "description": "Button labels, at most 5."}},
         ["threads", "summary", "options"])},
    {"name": "list_devices",
     "description": "Devices that are up right now, with their platform and what each can run, and whether each can "
                    "take work (ok, or why not, such as low memory). Pass repo to learn which devices hold that repo's "
                    "folder. Never plan work on a device that is missing from this list or not ok.",
     "inputSchema": _schema({"repo": {"type": "string", "description": "Optional repo folder name."}})},
    {"name": "ask_owner",
     "description": "Ask the owner a question they answer with one tap. It is asked at both ends: a card with buttons in "
                    "Discord, and in their own Claude sessions, which show it with their next message. An answer at either end "
                    "closes the other. In an interactive session they are typing in, ALSO ask them here in the chat (your "
                    "question box), and when they answer here call answer_question so the Discord card closes with their answer. "
                    "Before it asks, it checks the questions already waiting on them for the same repo or thread: if there "
                    "are any, nothing is sent and you get them back. Then ask once more with replaces: the ids of the ones "
                    "this question makes unnecessary (the newer PR already holds the older one's changes, the same decision "
                    "asked again, a plan that replaces the older plan); fold anything still needed from them into this "
                    "question, so they answer once. Pass replaces [] to keep them all. Replaced questions close in Discord "
                    "and point at yours, and their threads are archived. End your turn after asking: you are started again "
                    "with their answer in this thread. Give 2 to 5 short option labels. For a choice of device, pass devices "
                    "true and the options are built from the devices that are available and ok right now; if only "
                    "one qualifies, no question is sent and that device is returned. To recommend a device, pass "
                    "recommended: its button then reads like \"<device label> (recommended)\". Do not state the recommendation "
                    "or its reason in the question.",
     "inputSchema": _schema({
         "question": {"type": "string", "description": "The question, with the context they need, in a few plain sentences."},
         "options": {"type": "array", "items": {"type": "string"}, "description": "Button labels, at most 5."},
         "devices": {"type": "boolean", "description": "True to ask which device should run the work."},
         "recommended": {"type": "string", "description": "With devices true: host name or label of the device you recommend."},
         "repo": {"type": "string", "description": "Repo or project folder the question is about. Give it: it groups their questions per repo."},
         "replaces": {"type": "array", "items": {"type": "string"}, "description": "Ids of open questions this one makes unnecessary, "
                      "from the list a first call returned. [] keeps them all open."}},
         ["question"])},
    {"name": "list_questions",
     "description": "The questions waiting on the owner right now, from every agent and device, with their ids, options, project, "
                    "age and Discord link, and the ones closed in the last two days with how. Use it when they ask what is "
                    "waiting on them, before asking them something, or to answer or settle one.",
     "inputSchema": _schema({"repo": {"type": "string", "description": "Optional: only this repo or project."}})},
    {"name": "answer_question",
     "description": "The owner answered one of their open questions here in this session (in the chat or in your question box): pass "
                    "it on, so the agent that asked gets their answer and the Discord card closes saying they answered here. Give "
                    "the question's id, words: their exact words from this chat (or the label they picked), and option (0 = first "
                    "button) when their words pick a button. It is refused unless those words are in what they typed or picked in "
                    "a session on this device after the question was asked. The agent that asked gets their words as they are, "
                    "and the button only when their words name it. Never answer for them.",
     "inputSchema": _schema({"id": _ID, "option": {"type": "integer", "description": "0 for the first button."},
                             "words": {"type": "string", "description": "Their exact words in this chat (or the label they picked), quoted."}}, ["id", "words"])},
    {"name": "settle_question",
     "description": "Close one of the owner's open questions that no longer needs them: the work in this session already did what it "
                    "asks, or newer work made it moot. No answer is sent; its Discord card closes and says how, in one line. "
                    "For a question you asked yourself, or as the orchestrator, or from one of their own interactive sessions. "
                    "Do not use it to decide for them: if the choice is still theirs, ask or answer_question.",
     "inputSchema": _schema({"id": _ID, "how": {"type": "string", "description": "One line: what settled it."}}, ["id", "how"])},
    {"name": "update_owner",
     "description": "Change or delete a message or question you already sent the owner, once it is out of date: the "
                    "situation moved on, the answer is no longer needed, or what you said was wrong. Give the id "
                    "returned when you sent it. Pass text to replace it (buttons stay); leave text out to delete it.",
     "inputSchema": _schema({"id": _ID, "text": {"type": "string", "description": "New text. Leave out to delete the message."}}, ["id"])},
    {"name": "answer_for_owner",
     "description": "Only when the owner said to use your recommendations: answer one of their open questions with the option "
                    "you recommend. Give the question's id, the option's number (0 = first button) and one line on why. "
                    "The bridge refuses it without their go-ahead, and for buttons that deploy, push, publish, spend or delete.",
     "inputSchema": _schema({"id": _ID, "option": {"type": "integer", "description": "0 for the first button."},
                             "why": {"type": "string", "description": "One line: why this option."}}, ["id", "option", "why"])},
    {"name": "check_inbox",
     "description": "List the unread messages in your inbox, oldest first, with a body preview. A message stays "
                    "here until you ack it or reply to it.",
     "inputSchema": _schema({})},
    {"name": "read_message",
     "description": "Return one of your messages in full by id, from your inbox or your done folder.",
     "inputSchema": _schema({"id": _ID}, ["id"])},
    {"name": "ack_message",
     "description": "Mark a message in your inbox as handled without replying: it moves to done with an optional "
                    "result note. Use reply_message instead when the sender expects an answer.",
     "inputSchema": _schema({"id": _ID, "result": {"type": "string",
                             "description": "Optional short note on what you did with the message."}}, ["id"])},
    {"name": "reply_message",
     "description": "Answer one of your messages: sends body to the original sender in the same thread and acks "
                    "the original. Returns the id of the reply.",
     "inputSchema": _schema({"id": _ID, "body": {"type": "string", "description": "Your answer."}}, ["id", "body"])},
    {"name": "wait_for_reply",
     "description": "Block until a reply to the message with this id (one you sent) arrives in your inbox, or "
                    f"until timeout_seconds pass (maximum {MAX_WAIT}, default 30). Returns the reply in full with "
                    "status \"reply\", or status \"timeout\" when nothing has arrived yet; call it again to keep "
                    "waiting. The reply stays in your inbox until you ack it.",
     "inputSchema": _schema({"id": _ID, "timeout_seconds": {"type": "number", "minimum": 0, "maximum": MAX_WAIT,
                             "description": f"Seconds to wait, at most {MAX_WAIT}. Default 30."}}, ["id"])},
]

class ToolError(Exception): pass

def _str(args, key, required=True):
    v = args.get(key)
    if v is None or v == "":
        if required: raise ToolError(f"missing required argument: {key}")
        return None
    if not isinstance(v, str): raise ToolError(f"argument {key} must be a string")
    return v

_fleet = None
def fleet():
    global _fleet
    if _fleet is None:
        spec = importlib.util.spec_from_file_location("fleet", Path(__file__).resolve().parent / "fleet.py")
        _fleet = importlib.util.module_from_spec(spec); spec.loader.exec_module(_fleet)
    return _fleet

def _origin(mail, mid):
    """Record where a question was asked (host and folder), so the bridge can close its Discord card once the owner types in
    that session instead of tapping a button. Best effort: a question without it just waits for a button or a reply."""
    try:
        from common import hostname
        d = mail.queue_root() / "mail" / "origins"; d.mkdir(parents=True, exist_ok=True)
        rec = {"host": hostname(), "cwd": os.getcwd(), "root": os.environ.get("CLAUDE_PROJECT_DIR") or "", "at": time.time()}
        (d / f"{mid}.json").write_text(json.dumps(rec), encoding="utf-8")
    except Exception: pass

def _norm(text): return " ".join(re.sub(r"[\"'`“”‘’.,!?;:()*_]+", " ", str(text or "").lower()).split())

def _iso(ts):
    import datetime
    try: return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError): return 0.0

def _picked(path, since):
    """the owner's answers to the app's question dialog (AskUserQuestion) in a Claude transcript after SINCE, as plain text."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 600_000)); tail = f.read().decode("utf-8", "replace")
    except (OSError, TypeError, ValueError): return []
    out, asked = [], set()
    for line in tail.splitlines():
        try: e = json.loads(line)
        except ValueError: continue
        if not isinstance(e, dict): continue
        content = (e.get("message") or {}).get("content")
        if not isinstance(content, list): continue
        if e.get("type") == "assistant":  # the dialog the session opened; only its own result counts, never text a tool printed
            asked |= {c.get("id") for c in content if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "AskUserQuestion"}
        elif e.get("type") == "user" and _iso(e.get("timestamp")) >= since:
            for c in content:
                if not isinstance(c, dict) or c.get("type") != "tool_result" or c.get("tool_use_id") not in asked or c.get("is_error"): continue
                body = c.get("content"); body = body if isinstance(body, str) else " ".join(x.get("text", "") for x in body or [] if isinstance(x, dict))
                out += re.findall(r'"[^"]*"="([^"]*)"', body)  # only the picked answers ("question"="answer"), never the question text
    return out

def his_words(mail, words, since):
    """The session on this device where the owner typed or picked WORDS after SINCE, or None. Only their prompts (session-hook.py
    keeps the last few) and their picks in the app's question dialog count, never text an agent wrote. Returns (session, the
    text the words were found in) or (None, None)."""
    from common import hostname
    w = _norm(words)
    if not w: return None
    for f in (mail.queue_root() / "sessions" / hostname()).glob("*.json"):
        try: s = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError): continue
        if not isinstance(s, dict) or float(s.get("updated") or 0) < since: continue
        texts = [t for at, t in (s.get("prompts") or []) if float(at or 0) >= since - 5]
        if not texts and float(s.get("prompt_at") or 0) >= since - 5: texts = [s.get("last_prompt") or ""]
        hit = next((t for t in texts + _picked(s.get("transcript"), since - 5) if w in _norm(t)), None)
        if hit is not None: return s, hit
    return None, None

def open_question(qid, include_closed=False):
    from common import open_questions
    doc = open_questions()
    q = next((x for x in doc.get("open") or [] if x.get("id") == qid), None)
    if q or not include_closed: return q, doc
    return next((dict(x, closed=True) for x in doc.get("closed") or [] if x.get("id") == qid), None), doc

def ask_project(mail, repo, question):
    """(project key, mail thread) for a question: its repo, else the repo of the mail this run answers, else a 'Project:' prefix."""
    from common import project_key
    parent = os.environ.get("AGENT_MAIL_PARENT"); pm = mail.find_anywhere(parent) if parent else None
    proj = repo or (pm or {}).get("repo") or ""
    if not proj:
        for text in (question, (pm or {}).get("subject")):
            head = str(text or "").split(": ", 1)
            if len(head) == 2 and 0 < len(head[0]) <= 30: proj = re.sub(r"^(re:\s*)+", "", head[0], flags=re.I); break
    return project_key(proj), ((pm or {}).get("thread") or (pm or {}).get("id"))

def call_tool(mail, me, name, args):
    """Run one tool as agent ME and return a JSON-serialisable result."""
    if not me: raise ToolError("this server was started without --agent NAME, so it has no identity")
    me = mail.resolve_agent(me)
    if name == "list_agents":
        return [{"name": n, **info, "is_you": n == me} for n, info in sorted(mail.load_agents().items())]
    if name == "send_message":
        nr = args.get("needs_reply", False)
        if not isinstance(nr, bool): raise ToolError("argument needs_reply must be true or false")
        dev = _str(args, "device", False)
        if dev and not fleet().available(dev): raise ToolError(f"device {dev} is not available right now; call list_devices")
        area = re.sub(r"[^\w.-]+", "-", (_str(args, "area", False) or "").strip().lower()).strip("-")
        nt = args.get("new_thread", False)
        if not isinstance(nt, bool): raise ToolError("argument new_thread must be true or false")
        rt = _str(args, "reply_to", False)
        if rt and nt: raise ToolError("give reply_to or new_thread, not both")
        m = mail.send(me, _str(args, "to"), _str(args, "subject"), _str(args, "body") + (f"\n\nAREA: {area}" if area else ""),
                      repo=_str(args, "repo", False), needs_reply=nr, device=dev, reply_to=rt or None, new_thread=nt)
        return {"id": m["id"], "to": m["to"], "thread": m["thread"], "needs_reply": m["needs_reply"], "device": m.get("device")}
    if name == "list_devices":
        return fleet().where(_str(args, "repo", False))
    if name == "list_active_work":
        return fleet().active_work()
    if name == "report_conflict":
        ids = sorted({str(t).strip() for t in (args.get("threads") or []) if str(t).strip()})
        if len(ids) < 2: raise ToolError("give at least two thread ids")
        opts = [{"label": str(o)[:80]} for o in (args.get("options") or [])][:5]
        if len(opts) < 2: raise ToolError("give at least two options")
        import hashlib
        mark = mail.mail_root() / "conflicts" / (hashlib.sha1(" ".join(ids).encode()).hexdigest()[:16] + ".json")
        try:
            old = json.loads(mark.read_text(encoding="utf-8"))
            if time.time() - float(old.get("at", 0)) < 86400: return {"asked": False, "id": old.get("id"), "note": "this conflict was already reported to the owner; do not report it again, wait for their answer"}
        except (OSError, ValueError): pass
        q = _str(args, "summary")
        m = mail.send(me, "owner", ("Conflict: " + q.splitlines()[0])[:80], q + "\n\nLINKED: " + " ".join(ids), needs_reply=True, options=opts)
        mark.parent.mkdir(parents=True, exist_ok=True); mail.write_json_atomic(mark, {"id": m["id"], "threads": ids, "at": time.time()})
        return {"asked": True, "id": m["id"], "note": "End your turn now. You are started again with their answer in this thread."}
    if name == "ask_owner":
        q = _str(args, "question"); repo = _str(args, "repo", False); note = ""
        if args.get("devices"):
            ok = [d for d in fleet().where(repo) if d["ok"]]
            if not ok: raise ToolError("no device can take this work right now: " + json.dumps(fleet().where(repo)))
            if len(ok) == 1: return {"asked": False, "device": ok[0]["host"], "note": "only one device qualifies; use it"}
            want = (_str(args, "recommended", False) or "").strip().lower()
            top = next((d for d in ok if want in (d["host"].lower(), d["label"].lower())), None)
            if want and not top: note = f" {args['recommended']} is not available for this work, so no device was marked as recommended."
            ok.sort(key=lambda d: d is not top)  # the first button is the highlighted one
            opts = [{"label": d["label"] + " (recommended)" if d is top else d["label"] if top else "Run on " + d["label"], "device": d["host"]} for d in ok]
        else:
            opts = [{"label": str(o)[:80]} for o in (args.get("options") or [])][:5]
            if len(opts) < 2: raise ToolError("give at least two options, or set devices true")
        from common import open_questions, project_key
        rep = args.get("replaces")
        if rep is not None and (not isinstance(rep, list) or not all(isinstance(x, str) for x in rep)): raise ToolError("replaces must be a list of question ids")
        proj, tid = ask_project(mail, repo, q); waiting = [x for x in open_questions().get("open") or []
                                                           if (proj and project_key(x.get("project")) == proj) or (tid and x.get("tid") == tid)]
        if waiting and rep is None:
            return {"asked": False, "open_on_same_project": [{k: x.get(k) for k in ("id", "from", "project", "subject", "question", "options", "thread")} | {"asked_hours_ago": round((time.time() - float(x.get("at") or 0)) / 3600, 1)} for x in waiting],
                    "note": "Nothing was sent. the owner already has these questions open on the same project or thread. Ask once more "
                            "with replaces: the ids your question makes unnecessary (fold anything still needed from them into your "
                            "question so they answer once), or replaces [] to keep them all open."}
        known = {x.get("id") for x in waiting}
        rep = [x for x in rep or [] if x in known]
        if rep: q = q.rstrip() + "\n\nREPLACES: " + " ".join(rep)
        m = mail.send(me, "owner", q.splitlines()[0][:80], q, repo=repo, needs_reply=True, options=opts)
        _origin(mail, m["id"])
        auto = (mail.load_agents().get(me) or {}).get("auto")
        return {"asked": True, "id": m["id"], "options": [o["label"] for o in opts], "replaces": rep,
                "note": ("End your turn now. You are started again with their answer in this thread." if auto else
                         "Ask them the same question here in the chat now (your question box), then end your turn. When they answer "
                         "here, call answer_question with this id and their words, so the Discord card closes with their answer. If "
                         "they answer in Discord first, their next message here carries that answer. If the question stops "
                         "mattering, call settle_question.") + note}
    if name == "list_questions":
        from common import open_questions, project_key
        doc, want = open_questions(), project_key(_str(args, "repo", False))
        keep = lambda x: not want or project_key(x.get("project")) == want
        return {"open": [x | {"asked_hours_ago": round((time.time() - float(x.get("at") or 0)) / 3600, 1)} for x in doc.get("open") or [] if keep(x)],
                "closed_recently": [x for x in doc.get("closed") or [] if keep(x)][:20],
                "as_of": doc.get("at"), "note": "" if doc.get("at") else "No Discord bridge has published questions lately, so this list may be empty for that reason."}
    if name == "answer_question":
        info = mail.load_agents().get(me) or {}
        if info.get("auto") or info.get("host") in (None, "any") or isinstance(info.get("host"), list):
            raise ToolError("only the owner's own interactive sessions (an agent bound to one device) pass on their answers; an automatic agent never answers for them")
        qid = _str(args, "id"); q, _ = open_question(qid, include_closed=True)
        if not q: raise ToolError(f"{qid} is not an open question; call list_questions")
        if q.get("closed") and not q.get("late_ok"): return {"sent": False, "note": f"Already closed: {q.get('how')}. Tell the owner; nothing more to do."}
        opt, words = args.get("option"), _str(args, "words")
        if opt is not None and (isinstance(opt, bool) or not isinstance(opt, int) or not 0 <= opt < len(q.get("options") or [])): raise ToolError(f"option must be 0 to {len(q.get('options') or []) - 1}")
        since = max(float(q.get("asked_at") or q.get("at") or 0) if q.get("closed") else float(q.get("at") or 0), float(q.get("rev_at") or 0))  # words from before the question, or before it was last reworded, do not count
        s, said = his_words(mail, words, since)
        if not s: raise ToolError("those words are not in anything the owner typed or picked in a session on this device since the question was asked. "
                                  "Quote them exactly, or ask them first; never answer for them.")
        if opt is not None and _norm(q["options"][opt]) not in _norm(words): opt = None  # their words do not name that button: they go to the asker as written
        text = None if opt is not None else words
        where = f"a {s.get('tool') or 'Claude'} session on the {'Mac' if 'mac' in str(s.get('host') or '').lower() else 'PC'}" + (f" ({s.get('project')})" if s.get("project") else "")
        m = mail.send(me, "owner", f"[relay] {qid}", text or words, check_repo=False,
                      control={"target": qid, "op": "relay", "option": opt, "words": words, "where": where, "rev": int(q.get("rev") or 0)})
        return {"sent": True, "id": m["id"], "answer": (q.get("options") or [])[opt] if opt is not None else text,
                "note": "The Discord bridge passes it to the agent that asked and closes the card within a minute."}
    if name == "settle_question":
        qid = _str(args, "id"); q, _ = open_question(qid, include_closed=True)
        if not q: raise ToolError(f"{qid} is not an open question; call list_questions")
        if q.get("closed"): return {"sent": False, "note": f"Already closed: {q.get('how')}."}
        m = mail.send(me, "owner", f"[settle] {qid}", _str(args, "how"), check_repo=False, control={"target": qid, "op": "settle"})
        return {"sent": True, "id": m["id"], "note": "Its Discord card closes within a minute, saying how."}
    if name == "update_owner":
        text = _str(args, "text", False); m = mail.amend(me, _str(args, "id"), text)
        return {"id": m["id"], "done": "updated" if text else "deleted"}
    if name == "answer_for_owner":
        m = mail.answer(me, _str(args, "id"), int(args.get("option", -1)), _str(args, "why", False))
        return {"id": m["id"], "sent": True, "note": "The bridge answers it only if the owner's go-ahead covers this question; check their thread for the result."}
    if name == "check_inbox":
        out = []
        for m in mail.list_inbox(me):
            body = str(m.get("body", ""))
            item = {k: m.get(k) for k in ("id", "from", "subject", "repo", "reply_to", "thread", "hops",
                                          "needs_reply", "created")}
            item["body_preview"] = body[:300] + ("..." if len(body) > 300 else "")
            out.append(item)
        return {"agent": me, "count": len(out), "messages": out}
    if name == "read_message":
        m, box = mail.get(me, _str(args, "id"))
        return {"box": box, **m}
    if name == "ack_message":
        m = mail.ack(me, _str(args, "id"), result=_str(args, "result", False) or "")
        return {"acked": m["id"], "acked_at": m["acked_at"]}
    if name == "reply_message":
        m = mail.reply(me, _str(args, "id"), _str(args, "body"))
        return {"id": m["id"], "to": m["to"], "reply_to": m["reply_to"], "thread": m["thread"]}
    if name == "wait_for_reply":
        t = args.get("timeout_seconds", 30)
        if isinstance(t, bool) or not isinstance(t, (int, float)): raise ToolError("timeout_seconds must be a number")
        t = max(0.0, min(float(t), float(MAX_WAIT)))
        mid = _str(args, "id")
        m = mail.wait(me, mid, timeout=t, interval=1.0)
        if m is None: return {"status": "timeout", "waited_seconds": t, "reply_to": mid}
        return {"status": "reply", "message": m}
    raise ToolError(f"unknown tool: {name}")

def handle(mail, me, req):
    """Return the response object for one request, or None for a notification."""
    if not isinstance(req, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    rid, method, has_id = req.get("id"), req.get("method"), "id" in req
    def ok(result): return {"jsonrpc": "2.0", "id": rid, "result": result} if has_id else None
    def err(code, message): return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}} if has_id else None
    if not isinstance(method, str): return err(-32600, "Invalid Request")
    if method == "initialize":
        return ok({"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                   "serverInfo": {"name": "agent_mail", "version": "1.0.0"},
                   "instructions": f"Mail between AI agents and the owner across devices. You are agent {me or '(unset)'}. "
                                   "check_inbox shows your unread mail; reply_message or ack_message clears it."})
    if method.startswith("notifications/"): return None
    if method == "ping": return ok({})
    if method == "tools/list": return ok({"tools": TOOLS})
    if method == "tools/call":
        params = req.get("params") if isinstance(req.get("params"), dict) else {}
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in {t["name"] for t in TOOLS}: return err(-32602, f"Unknown tool: {name}")
        try:
            if not isinstance(args, dict): raise ToolError("arguments must be an object")
            result = call_tool(mail, me, name, args)
            text, is_error = json.dumps(result, ensure_ascii=False, indent=2), False
        except (ToolError, mail.MailError) as e:
            text, is_error = f"Error: {e}", True
        except Exception as e:  # never let one bad call take the server down
            text, is_error = f"Error: {type(e).__name__}: {e}", True
        return ok({"content": [{"type": "text", "text": text}], "isError": is_error})
    return err(-32601, f"Method not found: {method}")

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", default=os.environ.get("AGENT_MAIL_AGENT", ""))
    a = ap.parse_args()
    mail = load_mail()
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer  # bytes: no newline or code page translation on Windows
    def emit(obj):
        try:
            stdout.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"); stdout.flush()
        except (BrokenPipeError, OSError):
            os._exit(0)  # the client went away
    for raw in iter(stdin.readline, b""):
        line = raw.decode("utf-8", errors="replace").strip()
        if not line: continue
        try:
            req = json.loads(line)
        except ValueError:
            emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}); continue
        try:
            if isinstance(req, list):
                out = [r for r in (handle(mail, a.agent, x) for x in req) if r is not None]
                if out: emit(out)
            else:
                resp = handle(mail, a.agent, req)
                if resp is not None: emit(resp)
        except Exception as e:
            rid = req.get("id") if isinstance(req, dict) else None
            emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": f"Internal error: {e}"}})
    return 0

if __name__ == "__main__": sys.exit(main())
