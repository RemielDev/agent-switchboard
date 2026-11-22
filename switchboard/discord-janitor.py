#!/usr/bin/env python3
"""Discord janitor: a Gemini model looks over the server and the script carries out the safe part of what it says.

The bridge keeps Discord tidy by rules; this catches what the rules miss (repeated notices, stale questions, threads
nobody needs, layout that stopped making sense). Runs on the device whose bridge is active, started by the bridge
every JANITOR_EVERY seconds when Queue/discord/config.json has "janitor": true. Optional: it needs the Antigravity CLI
(agy) signed in to a Google account with Gemini access, and spends no Claude or Codex usage. Without agy a pass only
removes stale buttons and logs that no model is available.

What the model may do, enforced here, not trusted to the model:
- delete a message only if the bot wrote it, the bridge is not tracking it (state.json), and it is not the newest
  message of a thread that ends in an open question;
- close (archive) a thread only if the bot owns it and nothing happened in it for ARCHIVE_IDLE hours. A reply reopens it;
- everything else it notices goes to Queue/discord/janitor-issues.jsonl: mess a script fix should prevent, and ways the
  Discord flow could work better for the owner. New findings go to the orchestrator (Opus) as critic at most every
  CRITIC_EVERY seconds, when config "janitor_critic" is true: it builds, tests and ships the ones it agrees with and says
  why it rejects the rest. Its verdicts come back to agent `janitor` and are shown to the model with KNOWN ISSUES.
The owner's messages are never touched. At most MAX_ACTIONS per run. Every action is logged to janitor-log.jsonl.
Every file it writes is this device's own copy (janitor-log.<host>.jsonl and so on, fleet.host_path); it reads all
copies merged, so two devices never write one synced file (a file sync tool makes conflict copies of those).

  python discord-janitor.py            one pass (does nothing if the server has not changed since the last pass)
  python discord-janitor.py --dry      ask the model, print the plan, change nothing
"""
import hashlib, importlib.util, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

TK = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("bridge", TK / "discord-bridge.py"); br = importlib.util.module_from_spec(spec); spec.loader.exec_module(br)

MODELS = ["gemini-4-carbon-low", "gemini-4-carbon-medium"]  # first that answers; config "janitor_models" overrides
MAX_ACTIONS, ARCHIVE_IDLE, PROMPT_CHARS, CRITIC_EVERY = 40, 6, 22000, 2 * 3600  # Windows caps a command line near 32k characters
CRITIC_KEEP_CHARS = 4500  # critic verdict lines in the prompt; the prompt rides on that same command line
SCHEMA = {"type": "object", "properties": {
    "delete": {"type": "array", "items": {"type": "object", "properties": {"channel": {"type": "string"}, "message": {"type": "string"}, "why": {"type": "string"}}, "required": ["channel", "message", "why"]}},
    "archive": {"type": "array", "items": {"type": "object", "properties": {"thread": {"type": "string"}, "why": {"type": "string"}}, "required": ["thread", "why"]}},
    "issues": {"type": "array", "items": {"type": "string"}}}, "required": ["delete", "archive", "issues"]}

RULES = """You are the janitor of a Discord server where AI agents report to the person who runs them, the owner. Keep it tidy and easy to read.
Below is a snapshot: channels and open threads, each with its newest messages (oldest first). A message line is
  <id> | <who> | <age> | <flags> | <text>
who is bot or owner. Flags: Q = an open question with buttons, waiting on the owner; B = other buttons (Close, next-step suggestions, an answered question): not a question and not Needs you; T = tracked by the bridge (cannot be deleted), N = system notice.
The text may start with [...]: the bot's reactions on that message (its state marks: ✅ done, 🔴 needs you, 🟡 working, ❌ failed) and whether a thread hangs off it (thread open or thread archived). A message of the owner's with ✅ and an archived thread was answered and is finished. Only the newest 15 messages per channel and 10 per thread are shown: never report a card, thread or message as missing because it is not in the snapshot. Session mirror cards in #threads show Working while the session runs a new prompt, which can come after an earlier Finished ping, and Idle once that turn ends: both are correct. Two Q questions in one thread are two different choices, each counted in Needs you.

Return JSON only, matching the schema:
- delete: bot messages that are clutter: repeats of the same notice (keep the newest), notices about work that is
  plainly over and superseded by a later message, questions with buttons whose thread later shows the work finished or
  moved on. Never the owner's messages, never T messages, never the only record of a result the owner has not answered.
- archive: bot threads that are finished and idle (no open question, nothing working) so they leave the sidebar.
- issues: up to 5 short sentences, each one concrete improvement: mess the bot keeps creating that a code fix should
  prevent (name the pattern and where), or a way the flow could work better for the owner (how their requests, follow-ups,
  answers, buttons and threads move; what they have to do twice; what is hard to find). Not one-off clutter you listed.
Rename notices ("changed the channel name") cannot be deleted by anyone; do not list them, and do not report old ones
as an issue: only a rename newer than 2 hours shows the bot still renaming.
Issues already reported are listed under KNOWN ISSUES; do not report them again in other words, only what is new.
Buttons left on old messages are removed by script; do not list them. Threads the owner started stay open until they tap Close;
do not propose archiving them. For issues, look hardest at what the owner sees: a reply that did not answer what they asked,
the same message twice, a question asked again, a card or status that says something no longer true, work that stalled.
A bot message whose stated state is no longer true ("nothing built yet", "waiting on your tap", "the question is still
open", "not posted yet") while later messages show the work done is clutter: delete it unless it is tracked (T) or is a
real answer to one of the owner's questions that is still true. When unsure, leave it. Empty lists are a fine answer."""

def cfg(): return br.load("config.json", {})
def lines(name): return br.fleet.read_lines(br.dpath(name), key=br.fleet.json_at)  # every device's copy of a log, by time
def jload(name): return br.fleet.load_merged(br.dpath(name), {})
def jsave(name, v): br.fleet.save_host(br.dpath(name), v)
def agy():
    for p in (shutil.which("agy"), Path.home() / ".local/bin/agy", Path(os.environ.get("LOCALAPPDATA", "")) / "agy/bin/agy.exe"):
        if p and Path(p).is_file(): return str(p)

def tracked():
    """Every Discord id the bridge's state mentions: cards, boards, questions, pings. Those are the bridge's to manage."""
    return set(re.findall(r"\d{17,20}", json.dumps(br.shared_state())))

def age(snowflake):
    s = time.time() - ((int(snowflake) >> 22) + 1420070400000) / 1000
    return f"{s / 3600:.0f}h" if s >= 3600 else f"{s / 60:.0f}m"

def message_flags(mid, btn, mtype, keep, open_q):
    return ("Q" if btn and mid in open_q else "B" if btn else "") + ("T" if mid in keep else "") + ("N" if mtype not in (0, 19, 20) else "")

def msg_extras(m):
    """The bot's own reactions on a message and the thread hanging off it, so the model can see what was answered and what exists."""
    parts = [r["emoji"]["name"] for r in m.get("reactions") or [] if r.get("me")]
    if m.get("thread"): parts.append("thread archived" if m["thread"].get("thread_metadata", {}).get("archived") else "thread open")
    return "[" + " ".join(parts) + "] " if parts else ""

def snapshot(c, bot, keep, open_q=frozenset()):
    """The server as compact text, plus the facts the guards need (who wrote what, which threads are whose)."""
    g, ch = c["guild_id"], c.get("channels", {})
    facts = {"msgs": {}, "threads": {}}
    threads = (br.api("GET", f"/guilds/{g}/threads/active") or {}).get("threads", [])
    lock = (c.get("lease_msg") or [None])[0]  # the private bridge lock thread is not clutter
    threads = [t for t in threads if t.get("id") != lock]
    out = []
    def lines(cid, n):
        try: ms = br.api("GET", f"/channels/{cid}/messages?limit={n}") or []
        except br.DiscordError: return []
        rows = []
        for m in reversed(ms):
            who = "bot" if m["author"]["id"] == bot else "owner" if m["author"]["id"] == str(c.get("owner_id")) else "other"
            card = bool((m.get("flags") or 0) & 32768)  # a card: its buttons and text sit inside, and only the bridge can rebuild it
            btn = (not card and bool(m.get("components"))) or (card and any(c.get("type") == 2 for c in br.walk_components(m.get("components"))))
            flags = message_flags(m["id"], btn, m.get("type"), keep, open_q)  # every bot message is a card now: deletable unless tracked, its rows never stripped here
            body = br.msg_text(m) or (m.get("embeds") or [{}])[0].get("title", "") or ""
            text = re.sub(r"\s+", " ", body)[:150]
            facts["msgs"][m["id"]] = {"channel": cid, "who": who, "q": btn, "type": m.get("type"), "card": card}
            facts.setdefault("order", {}).setdefault(cid, []).append({"id": m["id"], "who": who, "q": btn and not card, "type": m.get("type"), "text": bool(body.strip())})
            rows.append(f"  {m['id']} | {who} | {age(m['id'])} | {flags or '-'} | {msg_extras(m)}{text}")
        if ms: facts["msgs"][ms[0]["id"]]["newest"] = True
        return rows
    for name, cid in ch.items():
        out.append(f"#{name} ({cid})"); out += lines(cid, 15)
    for t in sorted(threads, key=lambda t: int(t.get("last_message_id") or t["id"]), reverse=True)[:40]:
        last = t.get("last_message_id") or t["id"]
        facts["threads"][t["id"]] = {"owner": t.get("owner_id"), "idle_h": (time.time() - ((int(last) >> 22) + 1420070400000) / 1000) / 3600, "parent": t.get("parent_id")}
        out.append(f"thread {t['id']} in {t.get('parent_id')} \"{t.get('name')}\" last activity {age(last)} ago"); out += lines(t["id"], 10)
    text = "\n".join(out)
    return text[:PROMPT_CHARS], facts

def known(n=12):
    """The latest issues already on file, so the model reports only new ones."""
    rows = [json.loads(l).get("issue", "") for l in lines("janitor-issues.jsonl")]
    out, cutoff = [], time.time() - 48 * 3600
    for l in lines("janitor-critic.jsonl"):
        try:
            r = json.loads(l)
            if not isinstance(r, dict) or not isinstance(r.get("at"), (int, float)) or r["at"] < cutoff: continue
            for x in str(r.get("verdict", "")).splitlines():
                x = x.strip()
                if re.search(r"\b(AGREED|REJECTED)\b", x): out.append(x[:300])
        except ValueError: continue
    seen, uniq = set(), []
    for x in reversed(out):  # newest first; keep the newest copy of a duplicate
        if x not in seen: seen.add(x); uniq.append(x)
    keep, total = [], 0
    for x in uniq:
        total += len(x) + (1 if keep else 0)
        if total > CRITIC_KEEP_CHARS: break
        keep.append(x)
    keep.reverse()
    return ("\n".join(f"- {r}" for r in rows[-n:]) or "- none") + ("\n\nTHE CRITIC'S VERDICTS FROM THE LAST 48 H (do not propose again what it rejected or already fixed)\n" + "\n".join(keep) if keep else "")

def gallery_ids():
    """The card gallery thread (card-gallery.py) and its samples, and the owner's thread with the assistant: never stale, never tidied."""
    out = set()
    try: g = json.loads(br.dpath("gallery.json").read_text(encoding="utf-8")); out |= {g["thread"], *g["msgs"]}
    except (OSError, ValueError, KeyError): pass
    try: out.add(br.shared_state()["argon_thread"])
    except KeyError: pass
    try: out |= set(json.loads(br.dpath("config.json").read_text(encoding="utf-8"))["lease_msg"])  # the bridge lock: never archived or tidied
    except (OSError, ValueError, KeyError, TypeError): pass
    return out

def _json_ok(line):
    try: json.loads(line); return True
    except ValueError: return False

def memory():
    """The owner's standing rules for Discord, from their thread with the assistant."""
    try: rows = json.loads(br.dpath("argon-memory.json").read_text(encoding="utf-8"))
    except (OSError, ValueError): return ""
    return ("\n\nOWNER'S STANDING RULES FOR DISCORD (follow them)\n" + "\n".join(f"- {r['rule']}" for r in rows)) if rows else ""

def stale_rows(facts, open_q):
    """Buttons left on a message with newer talk under it: the owner wrote since, or the bot posted text since (files and
    notices do not count). Open question cards are the bridge's and stay. Decided by script, not by the model."""
    out = []
    for cid, rows in (facts.get("order") or {}).items():
        if cid not in facts["threads"] or cid in facts.get("gallery", ()): continue  # the gallery's buttons are demos
        for i, r in enumerate(rows):
            if not r["q"] or r["who"] != "bot" or r["id"] in open_q: continue
            if any(x["who"] == "owner" or (x["who"] == "bot" and x["type"] in (0, 19) and x["text"]) for x in rows[i + 1:]): out.append({"channel": cid, "message": r["id"]})
    return out

def kept_threads():
    """Discord threads the owner started and has not closed: they stay in sight until the owner taps Close."""
    st = br.shared_state()
    return {t["dthread"] for t in (st.get("threads") or {}).values() if t.get("dthread") and t.get("origin") and not t.get("closed") and not t.get("gone")}

def open_questions():
    return set(re.findall(r"\d{17,20}", json.dumps(br.shared_state().get("q") or {})))

def ask(text, models):
    exe = agy()
    if not exe: raise RuntimeError("agy not installed")
    work = Path(tempfile.mkdtemp(prefix="janitor-"))  # an empty workspace: the model has nothing to read or change
    try:
        for m in models:
            try:
                r = subprocess.run([exe, "-p", "Everything you need is in this message. Do not call any tool or read any file; reply with the JSON object only.\n\n" + RULES + memory() + "\n\nKNOWN ISSUES\n" + known() + "\n\nSNAPSHOT\n" + text, "--model", m, "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
                                    "--disable-slash-commands", "--print-timeout", "240s"], cwd=work, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=300)
                env = next((json.loads(l) for l in reversed(r.stdout.strip().splitlines()) if l.strip().startswith("{") and _json_ok(l)), {})
                if env.get("status") != "SUCCESS": br.log("janitor model failed", m, (r.stderr or r.stdout)[-200:]); continue
                body = env["response"].strip(); body = json.dumps(json.JSONDecoder().raw_decode(body[body.find("{"):])[0])  # the first object: some models print it twice
                return m, json.loads(body), env.get("usage", {})
            except (subprocess.TimeoutExpired, ValueError, KeyError, IndexError) as e:
                out = getattr(e, "stdout", None) or (r.stdout if "r" in locals() else "") or ""
                br.log("janitor model failed", m, str(e)[:120], "| out:", re.sub(r"\s+", " ", str(out))[-300:], "| err:", re.sub(r"\s+", " ", (r.stderr if "r" in locals() else "") or "")[-200:])
        raise RuntimeError("no Gemini model answered")
    finally: shutil.rmtree(work, ignore_errors=True)

def allowed(plan, facts, keep):
    """The guards. Returns (deletes, archives) the script will carry out."""
    dels, arcs = [], []
    for d in plan.get("delete", []):
        f = facts["msgs"].get(str(d.get("message")))
        if not f or f["who"] != "bot" or d["message"] in keep or f.get("type") == 4: continue  # type 4, a rename notice, cannot be deleted by anyone
        if f.get("newest") and f["q"]: continue  # an open question at the end of its thread stays
        dels.append({**d, "channel": f["channel"]})
    for a in plan.get("archive", []):
        t = facts["threads"].get(str(a.get("thread")))
        if t and t["owner"] == facts.get("bot") and t["idle_h"] >= ARCHIVE_IDLE and str(a.get("thread")) not in facts.get("kept", ()): arcs.append(a)  # never one of the owner's unclosed threads
    return dels[:MAX_ACTIONS], arcs[:MAX_ACTIONS - len(dels[:MAX_ACTIONS])]

def record(name, rows):
    if not rows: return
    with open(br.fleet.host_path(br.dpath(name)), "a", encoding="utf-8") as f:  # this device's own copy
        for r in rows: f.write(json.dumps({"at": time.time(), **r}, ensure_ascii=False) + "\n")

CRITIC = """Gemini, the Discord janitor, found these patterns in the Discord flow. You are the critic. For each one, check
the evidence yourself (the switchboard folder: discord-bridge.py, its tests, the bridge log, Queue/discord/state.<host>.json)
and decide. Agree only when it is real, would keep happening, and a script change in that toolkit fixes it without new risk;
reject vague, one-off, already-fixed or risky ones. For the ones you agree with, hand the builder one batched fix with tests
(discord-bridge-test.py must pass), have the reviewer check it, then restart the bridge as the toolkit README says. This is
standing work the owner turned on: do not ask them. Stay inside the toolkit: no repos, no publishing, no money.
Reply with one line per finding, AGREED (what changed) or REJECTED (why), and send the owner the same lines as one short
message (send_message to owner).

FINDINGS
"""

def critic(dry=False):
    """Collect the critic's verdicts, then hand it the findings it has not seen, at most every CRITIC_EVERY seconds.
    Off unless Queue/discord/config.json sets "janitor_critic": true: each batch costs an Opus run plus a bridge build,
    which can crowd out the owner's own work and use up the week's usage."""
    if not cfg().get("janitor_critic"): return None
    AM = br.AM
    if "janitor" not in AM.load_agents(): AM.register("janitor", "any", "claude", auto=False, description="Gemini Discord janitor: findings for the orchestrator to judge")
    for m in AM.list_inbox("janitor"):
        record("janitor-critic.jsonl", [{"verdict": str(m.get("body") or "")[:3000], "reply_to": m.get("reply_to")}])
        try: AM.ack("janitor", m["id"])
        except AM.MailError: pass
    st = jload("janitor-critic-state.json")
    rows = [json.loads(l) for l in lines("janitor-issues.jsonl")]
    new = [r for r in rows if float(r.get("at") or 0) > float(st.get("upto") or 0)]
    if not new or time.time() - float(st.get("at") or 0) < CRITIC_EVERY: return None
    body = CRITIC + "\n".join(f"{i}. {r['issue']}" for i, r in enumerate(new[-8:], 1))
    if dry: return body
    m = AM.send("janitor", "orchestrator", f"Discord flow: {len(new[-8:])} finding(s) from Gemini for your review", body, needs_reply=True)
    jsave("janitor-critic-state.json", {"at": time.time(), "upto": max(float(r.get("at") or 0) for r in new), "last": m["id"]})
    br.log("janitor: findings sent to the critic", m["id"], len(new[-8:]))
    return body

def main(dry=False):
    c = cfg(); bot = str(c.get("bot_id") or (br.api("GET", "/users/@me") or {}).get("id"))
    keep = tracked() | gallery_ids(); oq = open_questions(); text, facts = snapshot(c, bot, keep, oq); facts["bot"] = bot; facts["kept"] = kept_threads() | gallery_ids(); facts["gallery"] = gallery_ids()
    if not dry:
        cleared = []
        for r in stale_rows(facts, oq)[:20]:
            try: br.api("PATCH", f"/channels/{r['channel']}/messages/{r['message']}", {"components": []}); cleared.append({"op": "unbutton", **r})
            except br.RateLimited: break
            except br.DiscordError as e: br.log("janitor: stale buttons not removed", r["message"], str(e)[:100])
        if cleared: record("janitor-log.jsonl", cleared); br.log("janitor: removed stale buttons from", len(cleared), "message(s)")
    digest = hashlib.sha256(re.sub(r"\| \d+[hm] \|", "|", text).encode()).hexdigest()  # ages tick every minute; only content counts
    st = jload("janitor.json")
    if not dry and st.get("digest") == digest: critic(); return br.log("janitor: nothing changed since the last pass")
    try: model, plan, usage = ask(text, c.get("janitor_models") or MODELS)
    except RuntimeError as e: return br.log("janitor: no model pass,", str(e))  # agy missing or no model answered: nothing else changes
    dels, arcs = allowed(plan, facts, keep)
    br.log(f"janitor ({model}, {usage.get('total_tokens', '?')} tokens): {len(dels)} deletes, {len(arcs)} closes, {len(plan.get('issues', []))} issues"
           + f" ({len(plan.get('delete', [])) - len(dels)} deletes and {len(plan.get('archive', [])) - len(arcs)} closes refused by the guards)")
    if dry: return print(json.dumps({"model": model, "delete": dels, "archive": arcs, "issues": plan.get("issues", [])}, indent=1, ensure_ascii=False))
    done = []
    for d in dels:
        try: br.api("DELETE", f"/channels/{d['channel']}/messages/{d['message']}"); done.append({"op": "delete", **d})
        except br.RateLimited: break
        except br.DiscordError as e: br.log("janitor delete refused", d["message"], str(e)[:100])
    for a in arcs:
        try: br.api("PATCH", f"/channels/{a['thread']}", {"archived": True}); done.append({"op": "archive", **a})
        except br.RateLimited: break
        except br.DiscordError as e: br.log("janitor close refused", a["thread"], str(e)[:100])
    record("janitor-log.jsonl", [{**x, "model": model} for x in done])
    seen = {json.loads(l).get("issue") for l in lines("janitor-issues.jsonl")}
    record("janitor-issues.jsonl", [{"issue": i, "model": model} for i in plan.get("issues", []) if i not in seen])
    jsave("janitor.json", {"digest": digest, "at": time.time(), "model": model, "done": len(done)})
    critic()

def _selfcheck():
    facts = {"bot": "B", "msgs": {"1": {"channel": "c", "who": "bot", "q": False}, "2": {"channel": "c", "who": "owner", "q": False},
                                  "3": {"channel": "c", "who": "bot", "q": True, "newest": True}, "4": {"channel": "c", "who": "bot", "q": False}},
             "threads": {"t1": {"owner": "B", "idle_h": 9}, "t2": {"owner": "B", "idle_h": 1}, "t3": {"owner": "R", "idle_h": 99}}}
    plan = {"delete": [{"channel": "x", "message": m, "why": ""} for m in ("1", "2", "3", "4", "9")], "archive": [{"thread": t, "why": ""} for t in ("t1", "t2", "t3")]}
    d, a = allowed(plan, facts, keep={"4"})
    assert [x["message"] for x in d] == ["1"] and d[0]["channel"] == "c", d  # not the owner's, not an open question, not tracked, not unknown
    assert [x["thread"] for x in a] == ["t1"], a  # only the bot's own threads, idle long enough
    f2 = {"threads": {"t": {}}, "order": {"t": [{"id": "a", "who": "bot", "q": True, "type": 0, "text": True}, {"id": "b", "who": "bot", "q": False, "type": 0, "text": False},
                                                 {"id": "c", "who": "bot", "q": True, "type": 0, "text": True}, {"id": "d", "who": "owner", "q": False, "type": 0, "text": True},
                                                 {"id": "e", "who": "bot", "q": True, "type": 0, "text": True}, {"id": "f", "who": "bot", "q": False, "type": 4, "text": True}]}}
    assert [r["message"] for r in stale_rows(f2, open_q={"c"})] == ["a"], stale_rows(f2, {"c"})  # a file or rename under buttons is not newer talk; an open question stays
    facts["kept"] = {"t1"}; assert not allowed(plan, facts, keep={"4"})[1], "the owner's unclosed thread is never archived"
    print("janitor self-check OK")

if __name__ == "__main__":
    if "--selfcheck" in sys.argv: _selfcheck()
    else: main(dry="--dry" in sys.argv)
