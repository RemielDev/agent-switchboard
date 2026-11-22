#!/usr/bin/env python3
"""The improvement loop: on a schedule, hands the orchestrator one improvement cycle per enabled repo.
Runs inside mail-runner on whichever listed device is up first; needs no Discord.

  improve-loop.py status | on | off
  improve-loop.py add REPO [--mode ask|auto] | remove NAME
  improve-loop.py add NAME --path FOLDER --host DEVICE --brief "what to look at" [--every HOURS] [--mode ask|auto]
  improve-loop.py run NAME           start a cycle now
  improve-loop.py discover           scan this device's own Claude and Codex sessions now, then add and update entries
  improve-loop.py ignore NAME | auto on|off
  improve-loop.py outcome NAME --change TEXT --metric TEXT --direction up|down --baseline TEXT [--days N] [--ref TEXT]
  improve-loop.py verdict NAME ID improved|flat|worse|wait [--now TEXT] [--days N] [--note TEXT]
  improve-loop.py outcomes NAME      every recorded change and whether its metric moved

Two kinds of entry. A repo is a folder directly under the root: its cycle runs on any device that holds that folder.
A project (added with --path and --host) is a folder anywhere on one named device: an app, a site, ad campaigns. Its
cycle runs only there and is skipped while that device is off. Either kind may name a deploy route (--deploy): with one,
reviewed work that passes its checks goes live through it; without one, the cycle leaves the change ready and asks.

Outcomes, so the loop's changes really improve things: a cycle that ships records the one metric the change should
move and its value then. Once the change has been live for its N days, the next cycle is told to measure the metric
again and record a verdict; a worse change is undone, or put to the owner when the cycle cannot undo it. "wait"
pushes the check N more days when the numbers are still too small. Lines go to Queue/loop/<NAME>/outcomes.<host>.jsonl
and cycle starts to cycles.<host>.jsonl, one writer per file, read merged. Three cycles in a row with nothing shipped
make the next cycle find out why and, if only the owner can clear it, ask them.

Auto-maintenance: every device records which folders the owner worked in (their interactive Claude and Codex sessions,
never agent runs) in Queue/loop/activity/<host>.json. A folder with 5 or more of their sessions in 7 days is proposed
once, with Add, Not now and Never; nothing joins before they answer. Every entry keeps a "recent" list of what they
asked there lately, which its next cycle weighs toward. An entry that was added automatically pauses after 14 days
without a session and resumes when they come back; entries added by hand are never paused or removed.

Config: Queue/loop/config.json {enabled, interval_hours, hosts, repos: {REPO: {mode, deploy}}, projects: {NAME: {mode, path,
host, brief, every, deploy}}}. State: Queue/loop/state.<host>.json, one per device, read merged with the old state.json (fleet.load_merged).
mode auto (the default): it builds, reviews and ships, and asks the owner only for what NEEDS lists; an open question
does not stop its next cycle.
mode ask: it proposes one change and builds only after the owner picks Build it, and waits while that question is open."""
import argparse, importlib.util, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import hostname, queue_root
import fleet

def _p(n): return queue_root() / "loop" / n
def load(n, d):
    if n == "state.json": return fleet.load_merged(_p(n), d, fleet.LOOP_MAX)  # every device's copy: the newest time per repo counts
    try: return json.loads(_p(n).read_text(encoding="utf-8"))
    except (OSError, ValueError): return d
def save(n, v):
    if n == "state.json": return fleet.save_host(_p(n), v)  # only this device's copy: two writers of one synced file made conflict copies
    fleet._write(_p(n), v)
def config(): return {"enabled": False, "interval_hours": 8, "hosts": [], "repos": {}, "projects": {}, "auto_add": True, "min_sessions": 5, "daily_budget": 75, "ignore": [], "pending": {}, "snoozed": {}, **load("config.json", {})}

# ---- what the owner is working on ----
import os
SKIP_PATH = ("scratch-workspaces", "/tmp", "/var/folders", "\\temp\\", "/.claude", "\\.claude", "application support", "appdata", "/.codex", "\\.codex", "/.switchboard")
SKIP_NAME = {"queue", "backups", "desktop", "documents", "downloads"}
AGENT_PROMPT = ("You are the mail agent", "Group work items by topic", "Read the file ")
SCAN_EVERY, STALE_DAYS = 3 * 3600, 14
DEFAULT_BRIEF = "Whatever the evidence shows is most worth improving: errors, rough edges, performance, usage and conversion, design and UX polish, unfinished work."

def project_of(cwd):
    """(root folder, name, kind) of the project a working folder belongs to, or None for scratch, temp and home folders."""
    from common import dev_root
    low = str(cwd or "").replace("\\", "/").lower()
    if not low or any(m.replace("\\", "/") in low for m in SKIP_PATH): return None
    p, home, root = Path(cwd), Path.home(), dev_root()
    try:
        rel = p.resolve().relative_to(root.resolve())
        if not rel.parts: return None
        top = root / rel.parts[0]  # every folder directly under the root is a repo
        return str(top), top.name, "repo"
    except (ValueError, OSError): pass
    try: p.resolve().relative_to(home.resolve())
    except (ValueError, OSError): return None
    top = p
    for q in [p, *p.parents]:
        if q == home or q == q.parent: break
        if (q / ".git").exists(): top = q  # the outermost repository
    if top == home or len(top.parts) - len(home.parts) < 2 or not top.is_dir(): return None
    return str(top), top.name, "project"

def _claude_session(f):
    """(cwd, first thing the owner typed) of one Claude Code transcript; (None, '') for an agent run."""
    cwd, first = None, ""
    try:
        with open(f, encoding="utf-8", errors="replace") as h:
            for i, line in enumerate(h):
                if i > 80 or (cwd and first): break
                try: d = json.loads(line)
                except ValueError: continue
                cwd = cwd or d.get("cwd")
                if d.get("type") == "user" and not first:
                    c = (d.get("message") or {}).get("content")
                    texts = [c] if isinstance(c, str) else [x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"] if isinstance(c, list) else []
                    for t in texts:
                        t = str(t).strip()
                        if t.startswith(AGENT_PROMPT): return None, ""
                        if t and not t.startswith("<"): first = " ".join(t.split())[:120]; break
    except OSError: pass
    return cwd, first

def scan_activity(force=False):
    """Write this device's activity file: the folders the owner's own sessions ran in over the last 14 days."""
    out = _p("activity") / f"{hostname()}.json"; now = time.time()
    try:
        if not force and now - out.stat().st_mtime < SCAN_EVERY: return None
    except OSError: pass
    cut, found = now - STALE_DAYS * 86400, {}
    def note(cwd, ts, prompt, tool):
        pr = project_of(cwd)
        if not pr or pr[1].lower() in SKIP_NAME: return
        r = found.setdefault(pr[0], {"name": pr[1], "path": pr[0], "kind": pr[2], "host": hostname(), "sessions": 0, "week": 0, "last": 0, "recent": [], "tools": []})
        r["sessions"] += 1; r["week"] += ts >= now - 7 * 86400; r["last"] = max(r["last"], ts)
        if prompt: r["recent"].append((ts, prompt))
        if tool not in r["tools"]: r["tools"].append(tool)
    d = Path.home() / ".claude" / "projects"
    for f in d.glob("*/*.jsonl") if d.is_dir() else []:
        try: ts = f.stat().st_mtime
        except OSError: continue
        if ts < cut: continue
        cwd, first = _claude_session(f)
        if cwd: note(cwd, ts, first, "claude")
    d = Path.home() / ".codex" / "sessions"
    for f in d.rglob("*.jsonl") if d.is_dir() else []:
        try:
            ts = f.stat().st_mtime
            if ts < cut: continue
            with open(f, encoding="utf-8", errors="replace") as h: meta = (json.loads(h.readline()).get("payload") or {})
        except (OSError, ValueError): continue
        if meta.get("cwd") and meta.get("thread_source", "user") == "user": note(meta["cwd"], ts, "", "codex")
    items = sorted(found.values(), key=lambda r: -r["last"])
    for r in items: r["recent"] = [t for _, t in sorted(r["recent"], reverse=True)[:5]]
    save(f"activity/{hostname()}.json", {"host": hostname(), "at": now, "items": items}); return items

def activity():
    """Every device's activity, one row per project name: the device where the owner works on it most."""
    best, d = {}, _p("activity")
    for f in d.glob("*.json") if d.is_dir() else []:
        try: a = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError): continue
        for r in a.get("items", []):
            k = r["name"].lower()
            if k not in best or (r["week"], r["last"]) > (best[k]["week"], best[k]["last"]): best[k] = r
    return best

def maintain(mail=None, say=print):
    """Propose what the owner is actively working on, refresh every entry's recent focus, pause auto entries they have left."""
    c, act, now, before = config(), activity(), time.time(), None
    before = json.dumps(c, sort_keys=True); added = []
    ignore = {str(x).lower() for x in c["ignore"]} | SKIP_NAME
    known = {k.lower(): ("repos", k) for k in c["repos"]} | {k.lower(): ("projects", k) for k in c["projects"]}
    for k, r in act.items():
        if k in known:
            e = c[known[k][0]][known[k][1]] = dict(c[known[k][0]][known[k][1]] or {})
            if r["recent"]: e["recent"] = r["recent"]
            e["worked"] = r["last"]
            if e.get("auto") and known[k][0] == "projects": e["path"], e["host"] = r["path"], r["host"]
        elif (c["auto_add"] and k not in ignore and r["week"] >= int(c["min_sessions"]) and k not in {x.lower() for x in c["pending"]}
              and now - float(c["snoozed"].get(r["name"], 0)) > STALE_DAYS * 86400):
            e = {"mode": "auto", "auto": True, "recent": r["recent"], "worked": r["last"], "section": "repos" if r["kind"] == "repo" else "projects", "asked": now}
            if r["kind"] != "repo": e.update(path=r["path"], host=r["host"], every=24, brief=DEFAULT_BRIEF)
            c["pending"][r["name"]] = e  # nothing joins or runs until the owner says so
            added.append(r)
    for sec in ("repos", "projects"):
        for n, e in c[sec].items():
            if (e or {}).get("auto"):
                idle = now - float(e.get("worked") or 0) > STALE_DAYS * 86400
                if idle != bool(e.get("paused")): e["paused"] = idle
    if json.dumps(c, sort_keys=True) != before:
        first = str((c["hosts"] or [hostname()])[0])
        if first.lower() != hostname().lower():  # one writer per synced file: config.json is saved by the first host only (CLI edits aside)
            say(f"loop: automatic config edit skipped on {hostname()}: only {first} saves them"); added = []
        else: save("config.json", c)
    for r in added:
        say(f"loop: proposed {r['name']} ({r['week']} sessions in 7 days on {r['host']})")
        if mail: propose(mail, r["name"], c["pending"][r["name"]], r, say)
    return added

def propose(mail, name, e, r=None, say=print):
    """Ask the owner before anything joins the loop. They tap a button, or type what to change and the orchestrator applies it."""
    what = "the repo" if e.get("section") == "repos" else f"the project in {e.get('path')}"
    plan = (f"**Proposed: {what}, on {fleet_label(e.get('host') or (r or {}).get('host') or '')}, "
            + ("every cycle of the loop" if e.get("section") == "repos" else f"every {e.get('every', 24):g} h")
            + f", {e.get('mode', 'auto')} mode** ("
            + ("each cycle proposes one change and builds only after you tap Build it)." if e.get("mode") == "ask"
               else "each cycle builds, checks and ships one change, then measures whether it worked, and asks you only when it needs you).")
            + (f"\n**Focus: {e['brief']}**" if e.get("brief") else ""))
    why = f"You opened {r['week']} sessions there in the last 7 days. " if r else ""
    body = (f"Add **{name}** to the improvement loop? {why}**Nothing runs until you answer.**\n{plan}\n"
            "To change it, reply here in your own words (for example: focus on monetization, every 12 hours, build without asking).\n\n"
            f"*(For the orchestrator, when the owner answers; run these in the switchboard folder. Add: `improve-loop.py confirm \"{name}\"`. "
            f"Not now: `improve-loop.py dismiss \"{name}\"`. Never: `improve-loop.py ignore \"{name}\"`. "
            f"Anything they type instead is an edit: `improve-loop.py confirm \"{name}\"` with --brief, --every or --mode set from their words, "
            "then tell them in one sentence what was set.)*")
    try: mail.send("orchestrator", "owner", f"Add {name} to the improvement loop?", body, needs_reply=True, options=[{"label": "Add"}, {"label": "Not now"}, {"label": "Never"}])
    except Exception as x: say(f"loop: could not ask the owner about {name}: {x}")

def focus(e):
    r = (e or {}).get("recent") or []
    return ("\n\nWhat the owner has been asking for here lately in their own sessions, newest first. Weigh this cycle toward it, and do not redo what they already did:\n"
            + "\n".join(f"- {t}" for t in r[:5])) if r else ""

BODY = """Improvement cycle for the repo in folder "{repo}". This was started by the scheduled loop, not typed by the owner.
{judge}
1. Read this repo's memory notes, its own docs (a docs/rejected.md of ideas already turned down, if it has one, first), and the backlog at Queue/loop/{repo}/backlog.md under the switchboard root (create it with a short ranked list if it is missing). Read Queue/loop/{repo}/log.md for what earlier cycles did.
2. Start from evidence, not guesses: error logs, test results, analytics the repo has, user feedback or issues, whatever this device can read without new sign-ins. Cite each number with its date and denominator. Too small a sample is not evidence: say so and do not act on it. Say plainly what you could not reach and what access would unlock it.
3. Pick the single most valuable improvement that one builder turn can build and verify. Prefer fixing something broken or rough over adding features. Name the one metric it should move, which way, its value now (with date and denominator), and how many days live it needs before it can be judged.
4. {gate}
5. Call list_devices with this repo. Delegate to builder on a device that is ok, pinning it with the device argument. If none is ok, stop and report that.
6. When builder answers, send the result to reviewer. If the reviewer wants changes, send them to builder once more, then stop either way.
7. {ship}
8. Once it is live, record it so a later cycle checks it worked: `python improve-loop.py outcome "{repo}" --change "<under 12 words>" --metric "<metric>" --direction up|down --baseline "<value, date, denominator>" --days N --ref "<PR, commit or release id>"` in the switchboard folder. Nothing live: record nothing.
9. Append the outcome to Queue/loop/{repo}/log.md (date, change, verdict, what is unverified) and update the backlog.
10. Send owner one short message: what the evidence showed, what shipped, whether earlier changes worked, and what is left.
{needs}"""
PROJECT_BODY = """Improvement cycle for the project "{name}" in the folder {path} on {host}. This was started by the scheduled loop, not typed by the owner.

Focus: {brief}
{judge}
1. Read this project's memory notes, its own docs (a docs/rejected.md of ideas already turned down, if it has one, first), and the backlog at Queue/loop/{name}/backlog.md under the switchboard root (create it with a short ranked list if it is missing). Read Queue/loop/{name}/log.md for what earlier cycles did.
2. Start from evidence, not guesses: analytics, logs, campaign results, store or user feedback, whatever this device can read without new sign-ins. Cite each number with its date and denominator. Say plainly what you could not reach and what access would unlock it.
3. Pick the single most valuable improvement that one builder turn can build and verify. Prefer fixing what the numbers show is broken over adding features. Name the one metric it should move, which way, its value now, and how many days live it needs before it can be judged.
   {campaigns}
4. {gate}
5. The folder exists only on {host}. Delegate to builder pinned to {host} with the device argument, no repo argument, area "{area}", and the folder path in the task.
6. When builder answers, send the result to reviewer (same device). If the reviewer wants changes, send them to builder once more, then stop either way.
7. {ship}
8. Once it is live, record it so a later cycle checks it worked: `python improve-loop.py outcome "{name}" --change "<under 12 words>" --metric "<metric>" --direction up|down --baseline "<value, date, denominator>" --days N --ref "<PR, commit or release id>"` in the switchboard folder. Nothing live: record nothing.
9. Append the outcome to Queue/loop/{name}/log.md (date, change, verdict, what is unverified) and update the backlog.
10. Send owner one short message: what the evidence showed, what shipped, whether earlier changes worked, and what is left.
{needs}"""
CAMPAIGNS = ("Campaigns count as improvements. At the same spend, and under this project's own campaign rules where it has them, you may switch ads on or off, add ads made from real footage and the real product, "
             "and refresh ad copy, store listing text and screenshots, posts and emails. Log the before state so it can be undone. More spend, a budget, bid or audience change, a new paid campaign or any purchase goes to the owner.")
SHIP_SET = "Once the reviewer approves and every check passes, deploy it yourself: {deploy} Then look at the live result as a user sees it (desktop and phone width for a site) and fix what that shows."
SHIP_UNSET = ("This entry has no deploy route set in the loop config. Leave the change ready (a branch or PR) and ask the owner once with ask_owner, options \"Deploy it\" and \"Not yet\", "
              "naming the route you would use, so it can be saved with `improve-loop.py add NAME --deploy \"<route>\"` (a project also passes its --path and --host).")
NEEDS = """
When to pull the owner in. Decide and do everything else yourself. Ask with ask_owner and buttons, the exact place to click in the first line, only when:
- a step needs their account: a sign-in, a password or code, a purchase, or access no tool here reaches (say what it would unlock);
- money: more spend, a budget, bid or price change, a new paid campaign;
- a store release, or a pricing, caps or auth change;
- a change shipped earlier made its metric worse and you cannot undo it yourself;
- the same blocker or missing access shows up in a second cycle's log, or two build attempts failed;
- their instructions conflict, or the change would alter the repo's or product's direction.
Ask once. Write it in the backlog under "Needs the owner" with the date and question id. While it is open, do not ask it again and work on the next item that does not depend on it. If it is still open after 48 hours, replace it once with a fresh ask_owner (delete the old card with update_owner). When they answer, act on it in this cycle."""
GATES = {"ask": "Do not build yet. Use ask_owner with options \"Build it\", \"Skip\", \"Something else\", describing the change in two plain sentences. End your turn. Build only if they answer Build it. On Skip, log it and mark it skipped in the backlog, then pick the next most valuable improvement (design, UX and polish count, not only bugs, and so does upkeep: verifying projects work, checking they are consistent with each other, finding projects that are missing or were never added) and ask again the same way: a skip means look for another thing, not stop. Your final result is then one line: \"Not built yet: asked the owner whether to <the change in under 12 words>.\" The question card carries the details, so do not repeat them in the result.",
         "auto": "Go ahead without asking: build it, check it and ship it. Ask first (ask_owner, \"Build it\", \"Skip\", \"Something else\") only when the change is large, hard to undo, or alters the repo's or product's direction."}

# ---- outcomes: did a shipped change move its metric? ----
VERDICTS = ("improved", "flat", "worse", "wait")
def _dir(name): return queue_root() / "loop" / name
def _lines(name, stem):
    out = []
    for f in sorted(_dir(name).glob(f"{stem}.*.jsonl")) if _dir(name).is_dir() else []:
        try: text = f.read_text(encoding="utf-8")
        except OSError: continue
        for line in text.splitlines():
            try: d = json.loads(line)
            except ValueError: continue
            if isinstance(d, dict): out.append(d)
    return sorted(out, key=lambda d: float(d.get("t") or 0))
def _append(name, stem, d):
    f = _dir(name) / f"{stem}.{hostname()}.jsonl"; f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a", encoding="utf-8") as h: h.write(json.dumps(d, ensure_ascii=False) + "\n")

def outcomes(name, now=None):
    """Every recorded change for NAME, merged across devices, with its latest verdict, live time and due time."""
    now, rows = now or time.time(), {}
    for d in _lines(name, "outcomes"):
        if d.get("kind") == "outcome" and d.get("id"): rows[d["id"]] = {**d, "verdict": None, "checks": []}
        elif d.get("kind") == "verdict" and d.get("id") in rows:
            r = rows[d["id"]]; r["checks"].append(d)
            r["verdict"] = d.get("verdict"); r["due"] = float(d["t"]) + float(d.get("days") or 0) * 86400 if d.get("verdict") == "wait" else None
    for r in rows.values():
        live = r["live_at"] = float(r["t"])  # a change is recorded once it is live
        if r["verdict"] is None: r["due"] = live + float(r.get("days") or 7) * 86400
        if r["verdict"] in ("improved", "flat", "worse"): r["due"] = None
    return sorted(rows.values(), key=lambda r: float(r["t"]))

def _day(t): return time.strftime("%Y-%m-%d", time.gmtime(float(t))) if t else "?"
def judge_block(name, now=None):
    """Step 0 of a cycle: the shipped changes whose check is due."""
    now = now or time.time(); rs = outcomes(name, now)
    due = [r for r in rs if r.get("due") and r["due"] <= now]
    if not due: return ""
    return (f"\n0. Judge earlier changes first. Each has been live long enough. Measure its metric now, the same way as its baseline, and record the result with "
                f"`python improve-loop.py verdict \"{name}\" <id> improved|flat|worse|wait --now \"<value, date, denominator>\"` (wait: add --days N when the numbers are still too small to call). "
                "A worse change is undone the way it shipped (revert the PR, flip the flag back, restore the logged before values) when you can, and the owner is told; one you cannot undo yourself goes to them with ask_owner. Say what you found in your message.\n"
            + "\n".join(f"   - {r['id']}: {r.get('change')}. {r.get('metric')} should go {r.get('direction')}; was {r.get('baseline')}; live since {_day(r.get('live_at'))}; ref {r.get('ref') or '-'}." for r in due) + "\n")

def dry_block(name, now=None):
    """Three or more cycles since the last recorded change: find out why, and pull the owner in if only they can clear it."""
    rs, starts = outcomes(name, now), [float(d.get("t") or 0) for d in _lines(name, "cycles")]
    last = max([float(r["t"]) for r in rs] or [0]); n = sum(1 for t in starts if t > last)
    if n < 3: return ""
    return (f"\n\nThe last {n} cycles here shipped nothing. Before picking, find out why from the log (waiting on the owner, missing access, picks too big for one turn, nothing found). "
            "Fix what you can yourself. If only the owner can clear it, ask them once with ask_owner what to do, with the exact thing they would click or approve.")

def asks_block(asks):
    if not asks: return ""
    return ("\n\nStill waiting on the owner from earlier cycles: " + "; ".join(f"{q['subject'][:80]} (id {q['id']}, asked {_day(q['at'])})" for q in asks)
            + ". Do not ask these again. A \"Build it\" question an earlier cycle left open is yours to decide now under this cycle's rules: build it or drop it, then delete the card with update_owner.")

def ship_step(e):
    """The ship step for an entry: deploy through its route when it has one, else leave the change ready and ask."""
    route = (e or {}).get("deploy")
    return SHIP_SET.format(deploy=route) if route else SHIP_UNSET

def start(mail, repo, mode, say=print, asks=()):
    g = config()["repos"].get(repo)
    body = BODY.format(repo=repo, gate=GATES.get(mode, GATES["ask"]), judge=judge_block(repo), needs=NEEDS, ship=ship_step(g))
    m = mail.send("owner", "orchestrator", f"[loop] Improvement cycle: {repo}", body + focus(g) + dry_block(repo) + asks_block(asks), repo=None, needs_reply=True)
    try: _append(repo, "cycles", {"t": time.time(), "id": m["id"]})  # best effort: a failed write must not stop the state save below, or the cycle would start again
    except OSError as x: say(f"loop: could not log the cycle start: {x}")
    fleet.event("loop", f"{repo}: improvement cycle ({mode})", thread=m["thread"], repo=repo, ref=m["id"])
    st = load("state.json", {}); st.setdefault("last", {})[repo] = time.time(); st["by"] = hostname(); save("state.json", st)
    say(f"loop: started cycle for {repo} as {m['id']}"); return m

def start_project(mail, name, p, say=print, asks=()):
    area = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")
    body = PROJECT_BODY.format(name=name, path=p.get("path"), host=p.get("host"), brief=p.get("brief") or "anything that makes it better", area=area,
                               gate=GATES.get(p.get("mode", "auto"), GATES["auto"]), judge=judge_block(name), needs=NEEDS,
                               campaigns=p.get("campaigns") or CAMPAIGNS, ship=ship_step(p)) + focus(p) + dry_block(name) + asks_block(asks)
    m = mail.send("owner", "orchestrator", f"[loop] Improvement cycle: {name}", body, repo=None, needs_reply=True, device=p.get("host"))
    try: _append(name, "cycles", {"t": time.time(), "id": m["id"]})  # best effort: a failed write must not stop the state save below, or the cycle would start again
    except OSError as x: say(f"loop: could not log the cycle start: {x}")
    fleet.event("loop", f"{name}: improvement cycle ({p.get('mode', 'auto')})", thread=m["thread"], ref=m["id"])
    st = load("state.json", {}); st.setdefault("last", {})[name] = time.time(); st["by"] = hostname(); save("state.json", st)
    say(f"loop: started cycle for {name} as {m['id']}"); return m

def open_asks(name):
    """The questions earlier cycles for NAME still have waiting on the owner in Discord: [{id, subject, at}]."""
    s = fleet.load_merged(queue_root() / "discord/state.json", {})
    th, heads = s.get("threads") or {}, (f"{name}: improvement cycle".lower(), f"improvement cycle started for {name} (".lower())
    return [{"id": k, "subject": str(q.get("subject") or ""), "at": float(q.get("at") or 0)} for k, q in (s.get("q") or {}).items()
            if str((th.get(q.get("thread")) or {}).get("title", "")).lower().startswith(heads)]
def open_ask(name):
    """True while an earlier cycle for NAME still has a question waiting on the owner in Discord."""
    return bool(open_asks(name))

def lock_until(d):
    """The epoch a lock frees: its "until", else "since" + 12 hours, else None."""
    from datetime import datetime, timezone
    def at(k):
        try: return datetime.strptime(str(d.get(k)), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError: return None
    return at("until") or (at("since") + 12 * 3600 if at("since") else None)

def held(repo):
    """The repo's lock while it holds: until its "until", else 12 hours after "since". An expired lock file is left by a
    holder that never released it; repo-handoff.py treats it as free, so the loop does too."""
    try: d = json.loads((queue_root() / "locks" / f"{repo}.json").read_text(encoding="utf-8"))
    except FileNotFoundError: return None
    except (OSError, ValueError): return {}  # unreadable: held
    return d if time.time() <= (lock_until(d) or time.time() + 12 * 3600) else None

def tick(mail, say=print):
    try: scanned = scan_activity()  # every device reports its own sessions, a few times a day
    except Exception as e: scanned = None; say(f"loop: activity scan failed: {e}")
    c = config()
    if (fleet.leader(c["hosts"]) or "").lower() != hostname().lower(): return  # only the leader starts cycles; a stale heartbeat keeps its turn until the device is known to be down
    st0 = load("state.json", {})
    if scanned is not None or time.time() - st0.get("maintained", 0) > 1800:
        try: maintain(mail, say)
        except Exception as e: say(f"loop: maintenance failed: {e}")
        st0 = load("state.json", {}); st0["maintained"] = time.time(); save("state.json", st0); c = config()
    if c["enabled"] and time.time() - st0.get("retro", 0) > 7 * 86400:  # once a week, a script counts and the planner reads the counts
        st0["retro"] = time.time(); save("state.json", st0)
        try:
            import retro
            if len(fleet.stats(168)) >= 20: say(f"loop: weekly retro sent as {retro.send()['id']}")
        except Exception as e: say(f"loop: retro failed: {e}")
    if not c["enabled"] or not (c["repos"] or c["projects"]): return
    if budget_used(c) >= 1: return  # self-started work waits while agent runs are over the day's budget; the owner's own requests never do
    st = load("state.json", {}); last = st.get("last", {}); now = time.time()
    if now - max(last.values(), default=0) < 1800: return  # one cycle at a time, half an hour apart at least
    for repo, g in sorted(c["repos"].items(), key=lambda kv: last.get(kv[0], 0)):
        if (g or {}).get("paused"): continue
        if now - last.get(repo, 0) < float(c["interval_hours"]) * 3600: continue
        if not any(d["ok"] for d in fleet.where(repo)):
            if now - st.get("skipped", {}).get(repo, 0) > 6 * 3600:
                fleet.event("waiting", f"Loop skipped {repo}: " + "; ".join(f"{d['label']} {d['why']}" for d in fleet.where(repo)), repo=repo)
                st.setdefault("skipped", {})[repo] = now; save("state.json", st)
            continue
        mode, asks = (g or {}).get("mode", "auto"), open_asks(repo)
        if held(repo) is not None or (asks and mode == "ask"): continue  # ask mode waits on its question; auto mode works on around it
        start(mail, repo, mode, say, asks); return
    for name, p in sorted(c["projects"].items(), key=lambda kv: last.get(kv[0], 0)):
        if (p or {}).get("paused"): continue
        if now - last.get(name, 0) < float((p or {}).get("every") or c["interval_hours"]) * 3600: continue
        asks = open_asks(name)
        if not fleet.available((p or {}).get("host") or "") or (asks and (p or {}).get("mode", "auto") == "ask"): continue  # its only device is off, or (ask mode) its question is unanswered
        start_project(mail, name, p, say, asks); return

def budget_used(c=None):
    """Share of the day's agent budget used: agent runs of the last 24 hours, priced at API rates, over "daily_budget".
    0 when no budget is set."""
    c = c or config()
    try: return sum(float(r.get("cost") or 0) for r in fleet.stats(24)) / float(c.get("daily_budget") or 0)
    except ZeroDivisionError: return 0.0

def fleet_label(h): return "PC" if "pc" in str(h).lower() else "Mac" if "mac" in str(h).lower() else str(h)

def _mail():
    spec = importlib.util.spec_from_file_location("agent_mail", Path(__file__).resolve().parent / "agent-mail.py")
    m = importlib.util.module_from_spec(spec); sys.modules["agent_mail"] = m; spec.loader.exec_module(m); return m

def status():
    c, st = config(), load("state.json", {})
    rows = [f"Loop is {'ON' if c['enabled'] else 'OFF'}, every {c['interval_hours']} h, runs on {fleet.pick_host(c['hosts']) or 'no device up'} (order: {', '.join(c['hosts']) or 'none set'})."]
    for n, v in c["pending"].items():
        rows.append(f"- {n}: WAITING FOR YOUR OK (asked {time.strftime('%m-%d %H:%M', time.localtime(v.get('asked') or 0))}); not running")
    for g, v in c["repos"].items():
        t = st.get("last", {}).get(g); ok = [d["label"] for d in fleet.where(g) if d["ok"]]
        tag = (" [added automatically]" if (v or {}).get("auto") else "") + (" [paused: no session of yours in 14 days]" if (v or {}).get("paused") else "")
        rows.append(f"- {g}{tag}: {(v or {}).get('mode', 'auto')}, last {time.strftime('%m-%d %H:%M', time.localtime(t)) if t else 'never'}, can run now on {', '.join(ok) or 'nothing'}")
    for n, v in c["projects"].items():
        t = st.get("last", {}).get(n); up = fleet.available((v or {}).get("host") or "")
        tag = (" [added automatically]" if (v or {}).get("auto") else "") + (" [paused: no session of yours in 14 days]" if (v or {}).get("paused") else "")
        rows.append(f"- {n}{tag} (project on {(v or {}).get('host')}, {'up' if up else 'off now'}): {(v or {}).get('mode', 'auto')}, every {(v or {}).get('every') or c['interval_hours']} h, "
                    f"last {time.strftime('%m-%d %H:%M', time.localtime(t)) if t else 'never'}. Focus: {(v or {}).get('brief') or 'general'}")
    for n in list(c["repos"]) + list(c["projects"]):
        rs = outcomes(n)
        if rs:
            v = [r["verdict"] for r in rs]; due = sum(1 for r in rs if r.get("due") and r["due"] <= time.time())
            rows.append(f"  {n} outcomes: {v.count('improved')} improved, {v.count('flat')} flat, {v.count('worse')} worse, {due} due for a check")
    known = {k.lower() for k in list(c["repos"]) + list(c["projects"]) + list(c["pending"])}
    near = [f"{r['name']} ({r['week']} on {fleet_label(r['host'])})" for k, r in sorted(activity().items(), key=lambda kv: -kv[1]["week"]) if k not in known and r["week"] and k not in {str(x).lower() for x in c["ignore"]} | SKIP_NAME]
    u = budget_used(c)
    rows.append(f"Agent budget, last 24 h: {u * 100:.0f}% used." + (" Loop cycles are paused until it drops under 100%; your own requests still run." if u >= 1 else ""))
    rows.append(f"Auto-add is {'ON' if c['auto_add'] else 'OFF'}: a folder is proposed after {c['min_sessions']} of your own sessions in 7 days. Seen but below that: {', '.join(near[:8]) or 'none'}. Ignored: {', '.join(c['ignore']) or 'none'}.")
    rows.append(fleet.engines_text())
    return "\n".join(rows)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("status", "on", "off"): sub.add_parser(n)
    s = sub.add_parser("add"); s.add_argument("repo"); s.add_argument("--mode", choices=("ask", "auto"), default="auto")
    s.add_argument("--path"); s.add_argument("--host"); s.add_argument("--brief"); s.add_argument("--every", type=float)
    s.add_argument("--deploy", help="the entry's deploy route, in words the builder follows"); s.add_argument("--campaigns", help="a project's own campaign rules, replacing the default")
    s = sub.add_parser("remove"); s.add_argument("repo")
    s = sub.add_parser("confirm"); s.add_argument("repo"); s.add_argument("--mode", choices=("ask", "auto")); s.add_argument("--brief"); s.add_argument("--every", type=float)
    s = sub.add_parser("dismiss"); s.add_argument("repo")
    sub.add_parser("discover"); s = sub.add_parser("ignore"); s.add_argument("repo"); s = sub.add_parser("auto"); s.add_argument("state", choices=("on", "off"))
    s = sub.add_parser("run"); s.add_argument("repo")
    s = sub.add_parser("outcome"); s.add_argument("repo"); s.add_argument("--change", required=True); s.add_argument("--metric", required=True)
    s.add_argument("--direction", choices=("up", "down"), required=True); s.add_argument("--baseline", required=True)
    s.add_argument("--days", type=float, default=7); s.add_argument("--ref", default="")
    s = sub.add_parser("verdict"); s.add_argument("repo"); s.add_argument("id"); s.add_argument("verdict", choices=VERDICTS)
    s.add_argument("--now", default=""); s.add_argument("--days", type=float); s.add_argument("--note", default="")
    s = sub.add_parser("outcomes"); s.add_argument("repo")
    a = ap.parse_args(); c = config()
    if a.cmd == "outcome":
        oid = time.strftime("%m%d-", time.gmtime()) + os.urandom(2).hex()
        _append(a.repo, "outcomes", {"kind": "outcome", "id": oid, "t": time.time(), "host": hostname(), "change": a.change, "metric": a.metric,
                                     "direction": a.direction, "baseline": a.baseline, "days": a.days, "ref": a.ref})
        print(f"recorded {oid}: check {a.metric} ({a.direction}) {a.days:g} days after it is live"); return
    if a.cmd == "verdict":
        if a.id not in {r["id"] for r in outcomes(a.repo)}: sys.exit(f"no outcome {a.id} for {a.repo}; see improve-loop.py outcomes \"{a.repo}\"")
        if a.verdict == "wait" and not a.days: sys.exit("wait needs --days N")
        _append(a.repo, "outcomes", {"kind": "verdict", "id": a.id, "t": time.time(), "host": hostname(), "verdict": a.verdict, "now": a.now, "note": a.note, "days": a.days})
        print(f"{a.id}: {a.verdict}"); return
    if a.cmd == "outcomes":
        rs = outcomes(a.repo)
        for r in rs:
            last = (r["checks"] or [{}])[-1]
            state = r["verdict"] or f"check due {_day(r.get('due'))}"
            print(f"{r['id']}  {_day(r['t'])}  {r.get('change')}  | {r.get('metric')} {r.get('direction')}: {r.get('baseline')} -> {last.get('now') or '?'}  | {state}")
        if not rs: print(f"no outcomes recorded for {a.repo}")
        return
    if a.cmd == "on": c["enabled"] = True
    elif a.cmd == "off": c["enabled"] = False
    elif a.cmd == "add" and a.path:
        if not a.host: sys.exit("a project needs --host: the device that holds the folder")
        c["projects"][a.repo] = {**(c["projects"].get(a.repo) or {}), **{k: v for k, v in {"mode": a.mode, "path": a.path, "host": a.host, "brief": a.brief, "every": a.every, "deploy": a.deploy, "campaigns": a.campaigns}.items() if v is not None}}
    elif a.cmd == "add": c["repos"][a.repo] = {**(c["repos"].get(a.repo) or {}), **{k: v for k, v in {"mode": a.mode, "deploy": a.deploy}.items() if v is not None}}
    elif a.cmd == "remove": c["repos"].pop(a.repo, None); c["projects"].pop(a.repo, None)
    elif a.cmd == "confirm":
        e = c["pending"].pop(a.repo, None)
        if e is None: sys.exit(f"{a.repo} is not waiting for confirmation")
        sec = e.pop("section", "projects"); e.pop("asked", None)
        e.update({k: v for k, v in {"mode": a.mode, "brief": a.brief, "every": a.every}.items() if v is not None})
        c[sec][a.repo] = e
    elif a.cmd == "dismiss": c["pending"].pop(a.repo, None); c["snoozed"][a.repo] = time.time()
    elif a.cmd == "ignore": c["pending"].pop(a.repo, None); c["ignore"] = sorted(set(c["ignore"]) | {a.repo}); c["repos"].pop(a.repo, None) if (c["repos"].get(a.repo) or {}).get("auto") else None; c["projects"].pop(a.repo, None) if (c["projects"].get(a.repo) or {}).get("auto") else None
    elif a.cmd == "auto": c["auto_add"] = a.state == "on"
    elif a.cmd == "discover":
        scan_activity(force=True); maintain(_mail()); print(status()); return
    elif a.cmd == "run" and a.repo in c["projects"]:
        p = c["projects"][a.repo]
        if not fleet.available(p.get("host") or ""): sys.exit(f"{p.get('host')} is off, and {a.repo} exists only there")
        start_project(_mail(), a.repo, p, asks=open_asks(a.repo)); return
    elif a.cmd == "run":
        if not any(d["ok"] for d in fleet.where(a.repo)): sys.exit("no device can open that repo now: " + json.dumps(fleet.where(a.repo)))
        start(_mail(), a.repo, (c["repos"].get(a.repo) or {}).get("mode", "auto"), asks=open_asks(a.repo)); return
    if a.cmd != "status": save("config.json", c)
    if a.cmd == "status":
        try: scan_activity()
        except Exception: pass
    print(status())

if __name__ == "__main__": main()
