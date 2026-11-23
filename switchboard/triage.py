#!/usr/bin/env python3
"""Small-model triage: the first reader of every message the owner sends in Discord (stdlib only).

The bridge writes a job file (the owner's words, the thread's history, what is running now, the other open threads) and starts
this in its own process; the answer lands next to it as <job>.out.json, which the bridge acts on:
  answer   a live-status question the thread's status already answers (running, which step, done): the model replies, no
           agent run starts. The exchange is logged so the orchestrator sees it on its next turn.
  forward  more for work already open: it goes into that thread (THREAD), the same as typing there.
  new      a new request: a new thread for the orchestrator.
Every action comes with REPLY, one or two short sentences posted at once, so the owner knows what happened to their message.
The same runner also handles the bridge's other small jobs (kind "buttons", "chat", "settled"; see run()).

Optional: it calls a small, fast model through the `agy` command-line tool (Antigravity CLI, Gemini models); no Claude
or Codex usage. Without `agy` on PATH every job returns {"error": "agy not installed"} and the bridge falls back to its
plain path: messages are relayed to the orchestrator unchanged, answers keep the default buttons.

  triage.py JOB.json        run one job (the bridge does this)
  triage.py --selfcheck"""
import json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

MODELS = ["gemini-4-carbon-medium", "gemini-4-carbon-low"]  # Medium first (about 30 s; the instant acknowledgement covers the wait): judging what needs an agent matters more than 5 s. Low if it fails
SCHEMA = {"type": "object", "properties": {"action": {"type": "string", "enum": ["answer", "forward", "new"]},
          "thread": {"type": "string"}, "reply": {"type": "string"}, "answers_question": {"type": "boolean"}}, "required": ["action", "thread", "reply", "answers_question"]}
RULES = """You are the first reader of every Discord message the owner sends to their AI agents. The agents (an orchestrator on
Claude Opus, builders and reviewers) are slow and costly; you are fast. Decide what happens to the message, then tell
the owner in one or two short sentences. Return JSON only: {"action", "thread", "reply", "answers_question"}.

action:
- "answer": ONLY a live-status question that THIS THREAD's status line or last message answers word for word: is it
  running, which step is it on, did it finish, deploy or fail, where is the file it named. Put the answer in reply.
  Everything else is "forward", even when you think you know: what changed or was done, since when, lists, history,
  summaries, explanations, why something happened, opinions, plans, and anything asking for work, a change, a decision
  or a check. The context is a few clipped messages with no tools and no logs; the orchestrator reads the real records.
  If your answer would only repeat a message already in the thread, it is "forward". When unsure, "forward".
- "forward": more for work already open (a follow-up, a correction, an answer, "also do X", "stop", "do it"). thread =
  the id of the open thread it belongs to: usually THIS THREAD, or one listed under OTHER OPEN THREADS when it is
  plainly about that work.
- "new": a new piece of work that belongs to no open thread. thread = "".

answers_question: true only when there is an OPEN QUESTION and the owner's message answers it: picks one of its options,
says yes, no, go, deploy or not now, or gives exactly what it asked for. A complaint, a problem they found, a side question
or new work is false, even when they replied to the question's message: the question is then put on hold and asked
again once their message is handled, so say so in reply (e.g. "Fixing that first; the deploy question comes back after.").

reply: one short sentence (answer: at most three), plain and direct, no filler, no em dashes, no model or agent names
("the orchestrator" at most), never restating their words. For forward/new say what happens next using the context, e.g.
"Added to the login fix; the builder is mid-build, so it goes in at the next step." or "New request: Opus is picking it
up now." For answer, the answer itself in at most three short sentences. Never claim something the context does not say."""

BTN_SCHEMA = {"type": "object", "properties": {"buttons": {"type": "array", "items": {"type": "object", "properties": {"label": {"type": "string"}, "say": {"type": "string"}},
              "required": ["label", "say"]}}, "obsolete": {"type": "array", "items": {"type": "string"}},
              "updates": {"type": "array", "items": {"type": "object", "properties": {"thread": {"type": "string"}, "note": {"type": "string"}, "drop_question": {"type": "boolean"}},
              "required": ["thread", "note", "drop_question"]}}}, "required": ["buttons", "obsolete", "updates"]}
CHAT_MODELS = ["gemini-4-carbon-medium", "gemini-4-carbon-low"]  # the assistant chat: Medium first, Low if it runs out of time
BTN_MODELS = ["gemini-4-carbon-low", "gemini-4-carbon-medium"]  # Carbon High and Medium took 70 to 90 s on a full thread; Low 7 s with the same picks  # judgement over speed: this runs after the answer is already posted
BTN_RULES = """You keep one Discord thread clean right after the orchestrator posts a new answer in it, and you pick the buttons
under that answer. The owner reads these threads on a phone: every stale, repeated or untrue message costs them attention, and
a button they do not need is noise. Return JSON only: {"buttons": [{"label", "say"}], "obsolete": [ids], "updates": [...]}.

1. obsolete: ids of bot messages to delete, chosen ONLY from the ids marked [deletable] in THE THREAD. Delete one when:
   - what it says about the state is no longer true. Typical: "Nothing is built yet", "Not built yet", "Not fixed yet",
     "waiting on your tap / answer", "the question is still open", "queued", "not posted yet", "working on it", a plan or
     proposal for work that has since been built, published, posted, answered or dropped (by the new answer or any later message);
   - the new answer, or a later message, repeats it or replaces it: an older report on the same work, an interim reply,
     an earlier draft that was later posted;
   - it is a status line ("🟡 Working", "🔴 Needs you", "✅ Done") or a routing notice (starts with ✦) older than the newest answer.
   Keep a bot message that answered one of the owner's own questions or requests (an owner message just before it) when its
   answer is still true, even if its opening status line is now stale: they asked for it, and deleting it leaves their
   question hanging. Also keep anything still true and useful that no later message says: a draft they may reuse, a list
   of ideas, numbers they asked for. Files and question cards are never deletable.
   Be thorough: a thread should read as the owner's requests plus the CURRENT state, not a history of superseded statuses.

2. buttons: 0 to 3 next steps that only the owner can take, taken from what the new answer offers, asks or leaves open
   ("Tell me if you want it pushed" gives "Push to followers"). label: at most 24 characters, starts with a verb, specific.
   say: one plain sentence sent as the owner's words when they tap, naming the thing. Never a button for: work the agents do on
   their own (a queued or next step, the improvement loop), something already done, an acknowledgement (Accept, OK,
   Looks good, Thanks), Undo, Close, or anything an open question card in the thread already asks. Never spend money or
   change ads; a publish, deploy or post only when the answer itself offers it. Most finished answers need no button.

3. updates: the other open threads of the same repo or project below are separate work the owner follows. For each one whose
   state THE NEW ANSWER changes (it published, deployed, restarted, posted or fixed something that thread is waiting on,
   asking about or reporting as not done), give its id, a note of one short sentence saying what happened, and
   drop_question true when its open question is now answered or pointless. Skip threads it does not affect. Only what
   THE NEW ANSWER states as done; never guess."""

CHAT_SCHEMA = {"type": "object", "properties": {"reply": {"type": "string"}, "remember": {"type": "array", "items": {"type": "string"}},
               "forget": {"type": "array", "items": {"type": "integer"}}, "escalate": {"type": "string"},
               "actions": {"type": "array", "items": {"type": "object", "properties": {"op": {"type": "string", "enum": ["delete", "unbutton", "edit", "rename", "archive", "refresh"]},
                           "target": {"type": "string"}, "text": {"type": "string"}}, "required": ["op", "target", "text"]}}},
               "required": ["reply", "remember", "forget", "escalate", "actions"]}
CHAT_RULES = """You are the assistant that runs the owner's Discord server for their AI agents: you read their
messages first (triage), pick the buttons under answers, tidy threads, and tell sibling threads what changed. This is
the owner's private chat with you about how you and the Discord flow should behave. Return JSON only:
{"reply", "remember", "forget", "escalate"}.

reply: answer the owner directly in one to three short sentences, plain, no filler, no em dashes. Say what you will do
differently from now on. If they ask what you remember, list the rules briefly.
remember: new standing rules from their message, each one short imperative sentence about Discord handling ("Never put
Accept or Undo under finished work."). Only what they state or clearly want; nothing already in YOUR MEMORY.
forget: the numbers of rules in YOUR MEMORY that their message replaces or cancels.
actions: changes to make in the server NOW for what they ask or complain about, using the ids in THE SERVER below
(at most 15). op and target: "delete" a bot message id; "unbutton" a bot message id (take its buttons off); "edit" a
bot message id with text = its new text; "rename" a thread id with text = the new name; "archive" a thread id; "refresh"
a thread id (re-run your tidy pass on it: stale messages out, fresh buttons). Only bot messages can be deleted or edited;
never the owner's messages. Use text "" when an op needs none. Act on what they point at; when they complain about a pattern
("old buttons everywhere"), fix every instance you can see.
escalate: when what they want needs a code change in the bridge, a new feature or a fix that a rule or an action cannot
do, one clear request for the orchestrator's self-improvement loop (what is wrong, what they want, an example). Else "".
Your rules are used in every triage, buttons and janitor pass and in the orchestrator's answers to the owner."""

def memory_text():
    """The owner's standing rules for Discord, from their chat with the assistant (Queue/discord/argon-memory.json)."""
    try:
        from common import queue_root
        rows = json.loads((queue_root() / "discord" / "argon-memory.json").read_text(encoding="utf-8"))
    except Exception: return ""
    return "\n".join(f"{n}. {r['rule']}" for n, r in enumerate(rows, 1))

NO_TOOLS = ("Everything you need is in this message. Do not call any tool, read any file or search; tools are switched off "
            "and a tool call ends your turn with no answer. Reply with the JSON object only.\n\n")  # without this, the model sometimes reached for read_file and returned nothing

MEDIA_TOOLS = ("The owner attached these files; they are in your working folder: {}. Open and look at each one with your file "
               "tool before you answer; that is the only tool you may use, and only on those files. Reply with the JSON object only.\n\n")

VIDEO = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")
MEDIA_MAX = 15 * 1024 * 1024  # Gemini returned nothing for an 84 MB clip; the same clip shrunk to 1.8 MB read fine

def ffmpeg():
    for p in (shutil.which("ffmpeg"), *sorted(Path(os.environ.get("LOCALAPPDATA", "")).glob("Microsoft/WinGet/Packages/*FFmpeg*/*/bin/ffmpeg.exe"))):
        if p and Path(p).is_file(): return str(p)

def shrink(src, dst):
    """A big video becomes a small one Gemini can read: 1280 wide at most, fewer frames per second the longer it is
    (all of it up to 20 minutes), no sound. A big image becomes a 2048-wide JPEG. Returns the new file or None."""
    exe = ffmpeg()
    if not exe: return None
    if src.suffix.lower() in VIDEO:
        dur = 0.0
        probe = Path(exe).with_name("ffprobe" + Path(exe).suffix)
        try: dur = float(subprocess.run([str(probe), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)], capture_output=True, text=True, timeout=60).stdout.strip() or 0)
        except (OSError, ValueError, subprocess.TimeoutExpired): pass
        fps = 12 if dur <= 180 else max(2, round(2160 / dur))  # about 2,000 frames at most
        out = dst.with_suffix(".mp4")
        cmd = [exe, "-v", "error", "-y", "-i", str(src), "-t", "1200", "-vf", f"scale='min(1280,iw)':-2,fps={fps}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "32", "-an", str(out)]
    else:
        out = dst.with_suffix(".jpg")
        cmd = [exe, "-v", "error", "-y", "-i", str(src), "-vf", "scale='min(2048,iw)':-2", "-q:v", "4", str(out)]
    try: subprocess.run(cmd, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired): return None
    return out if out.is_file() and out.stat().st_size <= MEDIA_MAX else None

def attach(job, work, notes=None):
    """Copy the job's attached files (images, videos) into the empty workspace; the model may read only those. A file
    over MEDIA_MAX is shrunk first; NOTES collects what was shrunk or could not be used, for the reply."""
    names, notes = [], notes if notes is not None else []
    for f in (job.get("files") or [])[:4]:
        src = Path(f)
        if not src.is_file(): continue
        mb = src.stat().st_size / 1048576
        if src.stat().st_size <= MEDIA_MAX:
            dst = Path(work) / src.name; shutil.copyfile(src, dst); names.append(src.name); continue
        small = shrink(src, Path(work) / src.stem)
        if small: names.append(small.name); notes.append(f"shrank {src.name} ({mb:.0f} MB) to {small.stat().st_size / 1048576:.1f} MB to look at it")
        else: notes.append(f"could not use {src.name} ({mb:.0f} MB): too big to shrink here")
    return names

def scoped_reads(work):
    """read_file allowed inside this job's folder only, in every spelling the path may take on this machine."""
    out = set()
    for p in {str(Path(work)), os.path.realpath(work)}:
        out |= {f"read_file({p}/**)", f"read_file({p}\\**)", f"read_file({p.replace(chr(92), '/')}/**)"}
    return sorted(out)

def last_json(text):
    """agy's result line: the last line of its output that is a JSON object (warnings may follow it on some machines)."""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try: return json.loads(line)
            except ValueError: continue
    return {}

def agy():
    for p in (shutil.which("agy"), Path.home() / ".local/bin/agy", Path(os.environ.get("LOCALAPPDATA", "")) / "agy/bin/agy.exe"):
        if p and Path(p).is_file(): return str(p)

def prompt(job):
    mem = memory_text()
    parts = [RULES, ""] + (["OWNER'S STANDING RULES FOR DISCORD (follow them)", mem, ""] if mem else []) + [ "OWNER'S MESSAGE", job["text"], "", "THIS THREAD (id " + (job.get("thread") or "none: they wrote in the main channel") + ")"]
    parts += [job.get("status") or "no thread", "", "ITS HISTORY (oldest first)", job.get("history") or "nothing yet", ""]
    if job.get("question"): parts += ["OPEN QUESTION TO THE OWNER IN THIS THREAD", job["question"], ""]
    parts += ["OTHER OPEN THREADS (id | title | state)", job.get("others") or "none"]
    return "\n".join(parts)[:24000]  # Windows caps a command line near 32k characters

def chat_prompt(job):
    return "\n".join([CHAT_RULES, "", "YOUR MEMORY (numbered)", memory_text() or "nothing yet", "", "THIS CHAT SO FAR (oldest first)", job.get("history") or "nothing",
                      "", "OWNER'S MESSAGE", job["text"], "", "THE SERVER NOW (threads newest first; message lines: id | who | marks | text)", job.get("server") or "not available"])[:30000]

def btn_prompt(job):
    mem = memory_text()
    return "\n".join([BTN_RULES, ""] + (["OWNER'S STANDING RULES FOR DISCORD (follow them)", mem, ""] if mem else []) + [ "THE NEW ANSWER", job["answer"][:5000], "", "THE THREAD (oldest first; id | who | marks | text)", job.get("thread_text") or job.get("earlier") or "nothing",
                      "", "OTHER OPEN THREADS OF THIS REPO (id | title | state | open question | last answer)", job.get("others") or "none"])[:24000]

SETTLED_RULES = """An agent asked the owner a question and is still waiting in Discord. Since then the owner worked in one of their own Claude or
Codex sessions on the same repo or project. Decide whether that session did, settled or made moot exactly what the question
asks (for example it already deployed the version the question asks to deploy, built or rejected the proposed change,
or answered the choice). Unrelated work on the same project is NOT enough: say done false. When unsure, say done false.
why: one short plain sentence naming what the session did that settles it (or why it does not)."""
SETTLED_SCHEMA = {"type": "object", "properties": {"done": {"type": "boolean"}, "why": {"type": "string"}, "session": {"type": "string"}}, "required": ["done", "why", "session"]}

def settled_prompt(job):
    rows = [f"- session {s.get('id')} ({s.get('tool')}, {s.get('project')}): first prompt: {s.get('first_prompt') or '-'} | last prompt: {s.get('last_prompt') or '-'} | its last reply: {s.get('last_reply') or '-'}" for s in job.get("sessions") or []]
    return "\n".join([SETTLED_RULES, "", "THE QUESTION", job.get("question") or "", "", "ITS BUTTONS", ", ".join(job.get("options") or []),
                      "", "THE OWNER'S SESSIONS SINCE (session is the id of the one that settles it, or empty)", *rows])[:16000]

def run_settled(job, exe, work):
    fails = []
    for m in BTN_MODELS:
        try:
            r = subprocess.run([exe, "-p", NO_TOOLS + settled_prompt(job), "--model", m, "--output-format", "json", "--json-schema", json.dumps(SETTLED_SCHEMA),
                                "--disable-slash-commands", "--print-timeout", "75s"], cwd=work, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=85)
            env = last_json(r.stdout)
            if env.get("status") != "SUCCESS": fails.append(f"{m}: {str(env.get('error') or r.stderr or 'no output')[-160:]}"); continue
            body = str(env.get("response") or ""); out = json.JSONDecoder().raw_decode(body[body.find("{"):])[0]
            if isinstance(out.get("done"), bool): return {"done": out["done"], "why": str(out.get("why") or "").strip()[:300], "session": str(out.get("session") or ""), "model": m}
            fails.append(f"{m}: answer did not fit the schema")
        except (subprocess.TimeoutExpired, ValueError, KeyError, IndexError, OSError) as e: fails.append(f"{m}: {type(e).__name__}")
    return {"error": "no Gemini model answered", "fails": fails}

SUPERSEDE_RULES = """The owner has several open questions from their agents about one repo or project, each waiting for their tap in Discord and in their
Claude sessions. Find the OLDER questions that a NEWER one makes unnecessary, so they answer once instead of several times:
the newer one asks the same decision again, or its answer settles the older one too (a newer pull request that already
holds the older one's changes, a newer deploy or publish that includes what the older one would ship, a newer plan that
replaces the older plan). Two different decisions on the same project are NOT a match: leave both open. Only ever name
an older question as replaced by a newer one (by its asked time). When unsure, leave it open.
why: one short plain sentence the owner reads on the closed card, naming what the newer question covers."""
SUPERSEDE_SCHEMA = {"type": "object", "properties": {"replaced": {"type": "array", "items": {"type": "object", "properties": {"old": {"type": "string"}, "by": {"type": "string"}, "why": {"type": "string"}}, "required": ["old", "by", "why"]}}}, "required": ["replaced"]}

def supersede_prompt(job):
    rows = [f"- id {q.get('id')} | asked {q.get('asked')} by {q.get('from')} | thread: {q.get('thread') or '-'} | buttons: {', '.join(q.get('options') or [])}\n  {' '.join(str(q.get('question') or '').split())}" for q in job.get("questions") or []]
    return "\n".join([SUPERSEDE_RULES, "", f"PROJECT: {job.get('project') or '-'}", "", "OPEN QUESTIONS (oldest first)", *rows])[:16000]

def run_supersede(job, exe, work):
    fails, ids = [], {str(q.get("id")) for q in job.get("questions") or []}
    for m in BTN_MODELS:
        try:
            r = subprocess.run([exe, "-p", NO_TOOLS + supersede_prompt(job), "--model", m, "--output-format", "json", "--json-schema", json.dumps(SUPERSEDE_SCHEMA),
                                "--disable-slash-commands", "--print-timeout", "75s"], cwd=work, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=85)
            env = last_json(r.stdout)
            if env.get("status") != "SUCCESS": fails.append(f"{m}: {str(env.get('error') or r.stderr or 'no output')[-160:]}"); continue
            body = str(env.get("response") or ""); out = json.JSONDecoder().raw_decode(body[body.find("{"):])[0]
            if isinstance(out.get("replaced"), list):
                keep = [{"old": str(x["old"]), "by": str(x["by"]), "why": str(x.get("why") or "").strip()[:300]} for x in out["replaced"]
                        if isinstance(x, dict) and str(x.get("old")) in ids and str(x.get("by")) in ids and x.get("old") != x.get("by")]
                return {"replaced": keep, "model": m}
            fails.append(f"{m}: answer did not fit the schema")
        except (subprocess.TimeoutExpired, ValueError, KeyError, IndexError, OSError) as e: fails.append(f"{m}: {type(e).__name__}")
    return {"error": "no Gemini model answered", "fails": fails}

def run(job, models=None):
    models = models or {"buttons": BTN_MODELS, "chat": CHAT_MODELS}.get(job.get("kind"), MODELS)  # the tidy pass stays on Low: Medium took over 200 s on a full thread, High never answered
    exe = agy()
    if not exe: return {"error": "agy not installed"}
    work = Path(tempfile.mkdtemp(prefix="triage-"))  # an empty workspace: the model has nothing to read or change
    fails = []
    if job.get("kind") == "settled":
        try: return run_settled(job, exe, work)
        finally: shutil.rmtree(work, ignore_errors=True)
    if job.get("kind") == "supersede":
        try: return run_supersede(job, exe, work)
        finally: shutil.rmtree(work, ignore_errors=True)
    try:
        for m in models:
            try:
                kind = job.get("kind") == "buttons"; chat = job.get("kind") == "chat"
                media = []; names = attach(job, work, media)
                lead = (MEDIA_TOOLS.format(", ".join(names)) if names else NO_TOOLS)
                text_in = lead + (chat_prompt(job) if chat else btn_prompt(job) if kind else prompt(job))
                schema = CHAT_SCHEMA if chat else BTN_SCHEMA if kind else SCHEMA
                extra = ["--settings", json.dumps({"permissions": {"allow": scoped_reads(work)}})] if names else []
                r = subprocess.run([exe, "-p", text_in, "--model", m, "--output-format", "json", "--json-schema", json.dumps(schema), *extra,
                                    "--disable-slash-commands", "--print-timeout", "100s" if kind or chat else "75s"], cwd=work, stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=110 if kind or chat else 85)
                env = last_json(r.stdout)
                if env.get("status") != "SUCCESS": fails.append(f"{m}: {str(env.get('error') or r.stderr or 'no output')[-160:]}"); continue
                body = str(env.get("response") or "").strip()
                if "{" not in body: fails.append(f"{m}: no JSON in the answer"); continue
                out = json.JSONDecoder().raw_decode(body[body.find("{"):])[0]  # the first JSON object: some models print it twice (fenced, then raw)
                if chat and str(out.get("reply") or "").strip():
                    return {"reply": str(out["reply"]).strip(), "remember": [str(x).strip() for x in out.get("remember") or [] if str(x).strip()][:5],
                            "actions": [a for a in out.get("actions") or [] if isinstance(a, dict) and a.get("op") and str(a.get("target") or "").isdigit()][:15],
                            "forget": [int(x) for x in out.get("forget") or [] if str(x).lstrip("-").isdigit()], "escalate": str(out.get("escalate") or "").strip(), "model": m, "media": media}
                if kind and isinstance(out.get("buttons"), list):
                    out["buttons"] = [{"label": str(b["label"]).strip()[:40], "say": str(b["say"]).strip()[:500]} for b in out["buttons"][:3]
                                      if isinstance(b, dict) and str(b.get("label") or "").strip() and str(b.get("say") or "").strip()]
                    out["obsolete"] = [str(x) for x in out.get("obsolete") or []]
                    out["updates"] = [u for u in out.get("updates") or [] if isinstance(u, dict) and u.get("thread") and str(u.get("note") or "").strip()][:6]
                    return {**out, "model": m, "secs": round(env.get("duration_seconds") or 0, 1), "fails": fails}
                if out.get("action") in ("answer", "forward", "new") and str(out.get("reply") or "").strip():
                    return {**out, "model": m, "secs": round(env.get("duration_seconds") or 0, 1)}
                fails.append(f"{m}: answer did not fit the schema")
            except (subprocess.TimeoutExpired, ValueError, KeyError, IndexError, OSError) as e: fails.append(f"{m}: {type(e).__name__}"); continue
        return {"error": "no Gemini model answered", "fails": fails}
    finally: shutil.rmtree(work, ignore_errors=True)

def main(path):
    p = Path(path); job = json.loads(p.read_text(encoding="utf-8"))
    out = run(job); out["at"] = time.time()
    tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p.with_name(p.stem + ".out.json"))

def selfcheck():
    t = prompt({"text": "did it deploy?", "thread": "T1", "status": "Done", "history": "owner: deploy it\norchestrator: deployed", "others": "T2 | Kepler shop | Working"})
    assert "OWNER'S MESSAGE\ndid it deploy?" in t and "T2 | Kepler shop" in t and "id T1" in t
    b = btn_prompt({"answer": "Posted. Tell me if you want it pushed.", "thread_text": "55 | bot | [deletable] | Not posted yet"})
    assert "THE NEW ANSWER\nPosted." in b and "55 | bot | [deletable] | Not posted yet" in b
    c = chat_prompt({"text": "never show Accept", "history": "owner: hi"})
    assert "OWNER'S MESSAGE\nnever show Accept" in c and "YOUR MEMORY" in c
    import tempfile as _t
    w = _t.mkdtemp(); f = Path(w) / "src.png"; f.write_bytes(b"x"); w2 = _t.mkdtemp()
    assert attach({"files": [str(f), "/nope.png"]}, w2) == ["src.png"] and (Path(w2) / "src.png").exists()
    assert all(r.startswith("read_file(") for r in scoped_reads(w2)) and not any(r == "read_file(*)" for r in scoped_reads(w2))
    assert last_json('{"status": "SUCCESS"}\nwarning: x') == {"status": "SUCCESS"}
    twice = '```json\n{"a": 1}\n```\n{"a": 1}'
    assert json.JSONDecoder().raw_decode(twice[twice.find("{"):])[0] == {"a": 1}
    print("triage self-check OK")

if __name__ == "__main__":
    if "--selfcheck" in sys.argv: selfcheck()
    else: main(sys.argv[1])
