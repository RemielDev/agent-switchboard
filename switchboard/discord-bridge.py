#!/usr/bin/env python3
"""Discord front end for the agent mail layer. Stdlib only (own minimal WebSocket client), macOS and Windows.

  discord-bridge.py setup --guild ID     create channels, record the owner, register slash commands
  discord-bridge.py run                  connect to Discord's gateway and relay

Everything Discord-specific lives here. Agents only read and write mail; this process turns mail and the progress
log into Discord messages, so agent context never carries Discord traffic and the system works without Discord.

- A message from the server owner in #orchestrator becomes mail to the orchestrator (or `agent: text`) and gets its
  own Discord thread, started on that message, so the channel stays a list of requests.
- Inside a thread: one status message edited in place, each agent reply, questions with buttons, attachments.
- Anything the owner types in a thread continues that mail thread. Text typed while a question is open answers it.
- Finished agent threads archive themselves; a thread of the owner's stays until they close it, and hidden threads with unfinished work are reopened by the audit. A new reply reopens any. A thread deleted by hand comes back only when
  there is something new to say. A finished run with an answer leaves only that answer: the status message is removed and, once nothing is left open, the Close button sits on the answer.
- A Discord reply to a question card answers that question.
- If the owner's words cannot be written to mail, their message gets a warning reaction and a notice, and is retried for a day.
- Thread names carry the state, changed rarely because Discord allows 2 renames per 10 minutes.
- #sessions (config key "threads") has one card per interactive session with a real thread on it, named from its first
  prompt. The card shows state; the thread keeps the prompts. #progress holds one card per piece of work, edited in place.
- A report_conflict message ends with a LINKED line: the line is stripped and its thread gets a card following every linked thread.
- Slash commands: /status /devices /progress /agents /stop /go /loop /run. /run inside a thread continues it.

Token: ~/.switchboard/discord/token.txt (device-local). Config and state: Queue/discord/*.json (synced, no secrets).
Config "thread_state_names": false keeps state out of thread names; "add_owner_to_threads": false stops adding the owner to
new threads; "delete_system_notices": false and "screenshots": false turn those features off.
Config "receipt": true adds a cost line under each orchestrator answer; "burn_alert" (default 20, 0 off) alerts when a request passes that many dollars since the owner's last message; "pace_alert" (percent projected at reset, default 100, 0 turns it off) alerts once a week when Claude's weekly limit is on pace for it, only when it would reach the limit at least an hour before the reset.
- Discord's automatic notices are deleted where Discord allows it (see SYSTEM_TYPES). A notice type it refuses is not tried again.
- A replaceable bot notice has a slot (notice/clear_notice) and is edited or removed in place. Only those are ever cleaned up.
- Blocked work gets one screenshot of the device where it is blocked, under the card; rate limited, text note if impossible.
- #overview has one card per topic (topics.py groups sessions and mail threads), edited in place. A topic with nothing
  queued, working or waiting on the owner for an hour loses its card. Pings go in the card's own thread: an item starts needing
  them, one that worked 2 minutes or more finishes, a run fails or is cut off. Never for queued or working.
- Board channels (overview, progress, alerts, reports, sessions) are read-only by convention: what the owner types there is
  mailed to the orchestrator with a thread in #orchestrator, and their message is removed (needs Manage Messages).
- Old bot messages are swept every 10 minutes, a bounded number per pass: answered question cards after 1 hour, resolved
  or duplicate alerts after 6, progress cards of finished work after 24, and bot messages in a board channel the state
  no longer tracks. Nobody else's messages are deleted, except the owner's redirected board messages.
Only the first available host in config "hosts" is active, so a second device can take over when the first is down."""
import argparse, base64, contextlib, datetime, hashlib, importlib.util, io, json, os, re, socket, ssl, struct, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import dev_root, hostname, queue_root
import fleet, screencap, topics
import notes as NOTES
QUIET = 10        # seconds of silence after the owner's last message before a batch goes to the agents
QUIET_TYPING = 12  # the owner is still typing: wait this long after the last typing signal
CHAT_WAIT = 300  # seconds the assistant's chat may take: medium effort with an image and the whole-server view can take over 2 minutes, and low effort is the fallback after it
SMART_WAIT = 150  # seconds the assistant's tidy-and-buttons pass may take (a stronger model; the answer is already showing)
TRIAGE_WAIT = 120  # seconds the Gemini triage may take before the owner's message goes to the orchestrator the old way
STAGES = (("plan", "Plan"), ("build", "Build"), ("review", "Review"), ("report", "Report"))
QWORDS = set("why what how when where who which whats is are was were did does do can could would should will has have any".split())
ACKS = {"thanks","thank you","thx","ty","ok","okay","k","cool","nice","great","perfect","got it","sounds good","lgtm","yes","no","yep","nope"}
def head_line(text):
    """The first line of what the owner said, for a subject or title: a batch's wrapper line and a reply's '(replying to ...)' prefix name nothing."""
    lines = [l for l in str(text or "").strip().splitlines() if l.strip()]
    if not lines: return ""
    orig, first = lines[0].strip(), lines[0]
    if re.match(r"^The owner sent \d+ messages in a row\.", first) and len(lines) > 1: first = re.sub(r"^\d+\.\s+", "", lines[1].strip())
    out = re.sub(r'^\(replying to [^:]+: ".*?"\)\s*', "", first.strip(), count=1).strip()
    return out or orig
def asks_owner(text):
    """An agent's answer ends on a question to the owner: only its own last line counts, not quotes, headings or the runner's measured block."""
    s = str(text or "").split("\n---\nMeasured by the runner")[0]
    keep, fence = [], False
    for l in s.splitlines():
        if l.strip().startswith("```"): fence = not fence; continue
        if not fence and l.strip() and l.strip() != "---": keep.append(l.strip())
    if not keep: return False
    ls = [re.sub(r"^(-#|>|\*|-)+\s*", "", x).strip().strip("*").strip() for x in keep[-3:]]
    if ls[-1][:1] in "\"'“‘`": return False
    # the "?" rule is for the last line only; the ask phrases also count on the 2 lines before it, unless those quote something
    return re.sub(r"[)*_\"'”’\s]+$", "", ls[-1]).endswith("?") or any(ASKS_OWNER.search(x) for x in ls[-1:] + [y for y in ls[:-1] if y[:1] not in "\"'“‘`" and not re.search("[\"“]", y)])
def new_work(said):
    """False for a question, or a short acknowledgement: neither is new work in a finished thread."""
    s = said.strip()
    if s.endswith("?"): return False
    words = re.sub(r"[^\w\s']", "", s.lower()).split()
    if not words: return False  # only emoji or punctuation
    if words[0].replace("'", "") in QWORDS: return False
    return not (len(words) <= 4 and " ".join(words) in ACKS)
HISTORY = re.compile(r"\b(since|so far|today|yesterday|this week|last week|history|changelog|everything|all (the )?(changes|work)|chang\w*|did (you|we)|why|what'?s new|differen\w*|publish\w*|live|list|summar\w*)\b", re.I)  # needs the logs, not the last few thread messages
DOING = re.compile(r"^\s*(please\s+)?(fix|make|add|change|remove|build|deploy|publish|ship|do|run|stop|start|create|update|delete|revert|implement|try|use|set|move|rename|merge|push|redo|undo|go|write|test|check)\b", re.I)  # work, never just answered

TOKEN = Path.home() / ".switchboard/discord/token.txt"
API = "https://discord.com/api/v10"
TK = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("agent_mail", TK / "agent-mail.py")
AM = importlib.util.module_from_spec(_spec); sys.modules["agent_mail"] = AM; _spec.loader.exec_module(AM)  # the owner's words go through this: the CLI refuses to speak as a person
_gspec = importlib.util.spec_from_file_location("improve_loop", TK / "improve-loop.py")
GL = importlib.util.module_from_spec(_gspec); _gspec.loader.exec_module(GL)  # the #loops board reads the loop's own config and state
RUN_LIMIT = 50 * 60  # a run with no end event after this long counts as cut off (runner timeout is 45 min for builds)
MAX_SLEEP = 3            # longest rate-limit wait served by sleeping; longer ones defer the work so the gateway stays alive
AUTO_ARCHIVE = 10080     # minutes; Discord's own timer stays long so a thread waiting on the owner never disappears
KEEP_UNREAD = 24 * 3600   # the owner's own unclosed threads get a send-and-delete ping this often, so they show unread and never vanish
KEEP_RECENT = 24 * 3600  # a thread stays in sight this long after the owner last wrote or tapped in it, or after its work last moved
KEEP_MAX = 5              # pings per sweep pass, to stay far from rate limits
AUDIT_MAX = 5             # hidden threads with unfinished work reopened per sweep pass
JANITOR_TITLE = re.compile(r"^Discord flow: \d+ finding\(s\) from Gemini for your review")
LOOP_TITLE = re.compile(r"^(?:\[loop\] Improvement cycle: .+|.+: improvement cycle \((?:ask|auto)\)|Improvement cycle started for .+)$")  # one card per loop cycle, all with the same title
ONE_OFF_TITLE = re.compile(r"^Permission: ")  # cards that are answered once and then only clutter #orchestrator
QUIET_TITLE = re.compile(JANITOR_TITLE.pattern + r"|^\[loop\] Improvement cycle: Orchestration\b")  # machine work that never gets a thread or card of its own (a question still does)
NOTICE_TITLE = re.compile(r"\bis low on memory\b|\bmemory recovered\b|^Loop skipped ")  # thread-less device notices: one line on the #progress usage line, never a thread
NOTICE_KEEP = 6 * 3600   # shortcut: a notice line goes after this even if no "recovered" came; read device pressure instead if that misleads
LIMIT_HIT = re.compile(r"hit your (weekly|session) limit|usage limit", re.I)  # a Claude usage limit: paused until it resets, not failed
LIVE_EVERY = 20          # seconds between live-card edits of one thread, as the running agent's Queue/live file changes
OPEN_WAIT = 60           # seconds a tapped Open waits for the device's runner to answer in Queue/open/<host>/<id>.done.json
ARCHIVE_AFTER = 2 * 3600 # a finished thread is archived after this long without activity
TIDY_REQUEST, TIDY_UNKNOWN, TIDY_SESSION = 20 * 60, 3600, 3600  # the 10-minute sweep archives a finished request thread, a thread under #orchestrator the bridge does not know, and a session thread after this long with nothing posted
TIDY_WAIT = {"DONE": TIDY_REQUEST, "FAILED": 24 * 3600, "CUT OFF": 24 * 3600}  # how long a request thread showing this state waits; any other state stays open
SWEEP = 20               # seconds between re-checks of live threads
MOVE_RETRY_MAX = 600     # a card move whose delete keeps failing is retried after 20 s, doubling to this
ANSWER_WAIT = 90         # a run that ended well gets this long for its answer to arrive before a Done card is shown: the runner
                         # reports done before it mails the reply, and a Mac's reply comes a file sync (about 10 s) later
RENAME_SETTLE, RENAME_WINDOW, RENAMES_MAX = 30, 600, 2  # a state must hold 30 s to reach the name; 2 renames per 10 min
QUEUED_STALE = 2 * 3600  # the owner's words that no runner took (sent to a session agent, or nowhere) stop showing Queued after this
TITLE_GEN = re.compile(r"\s*(?:You are a helpful assistant\. You will be presented with a user prompt|You are in a fork of an existing Codex thread)")  # Codex's own title-naming turn
GRANT_TTL = 2 * 3600  # the owner's "do your recommendations" covers the questions open then, for this long
RISKY_LABEL = re.compile(r"deploy|push|publish|release|merge|\bgit\b|\bota\b|spend|buy|pay|purchase|delete|production|\blive\b|something else", re.I)  # never answered for the owner
RECO_HOWTO = """For each question: read its mail thread if you need the history, then pick the option you would recommend and
answer it with the answer_for_owner tool (or: python agent-mail.py answer --from orchestrator <question id> <option number> --why "<one line>").
Skip, and leave for the owner, any question whose answer would deploy, push, publish, release, merge, use Git where it is not
allowed, touch production data, send an OTA, spend money, change ads or budgets, delete data, or cannot be undone.
Never pick "Something else". If a question is yours and stale or superseded, withdraw it with update_owner instead.
Then reply to the owner with one line per question: what you chose and why, or that you skipped it and why."""
DONE_GRACE = 10         # a run that ended well is not shown as Done for this long: mail it handed on can sync a moment after its done event
HANDOFF_NAME_WAIT = 300  # mail handed to another agent is usually picked up soon: a thread named Working keeps that name this long before it is named Queued
BAD_MAIL_ALERT = 300     # an inbox file that stays unreadable this long raises one alert
BAD_MAIL_HOLD = 60       # an unreadable inbox file of unknown thread is most likely mail still syncing: no thread is closed for this long, and no state changes
SESSION_GRACE = 3600     # a session file must stay missing this long before its thread is archived
UNSENT_KEEP = 86400      # how long a message of the owner's that could not be mailed keeps being retried
SEEN_EMOJI, FAIL_EMOJI = "%F0%9F%91%80", "%E2%9A%A0%EF%B8%8F"
WORK_AGENTS = {"orchestrator", "builder", "reviewer"}  # a thread with a run of one of these is work, even with no Discord origin
STATE_REACT = {"WORKING": "🟡", "QUEUED": "🟡", "WAITING": "🟡", "PAUSED": "⏸️", "NEEDS YOU": "🔴", "DONE": "✅", "FAILED": "❌", "CUT OFF": "❌"}  # the state on the owner's own thread-starting message
TITLE_CLIPS = (80, 120)  # lengths the mail subject and stored title are cut to, usually mid-word
LIMIT, INLINE_PARTS = 1900, 2  # message size used when splitting; longer answers become one message plus a file
UNKNOWN_CHANNEL, UNKNOWN_MESSAGE, THREAD_ARCHIVED, THREAD_EXISTS, THREAD_LOCKED = 10003, 10008, 50083, 160004, 160005
MISSING_PERMISSIONS, SYSTEM_MESSAGE = 50013, 50021  # 50021: Discord will not act on this kind of system message
# Automatic notices to delete. Discord's docs mark 1, 2 and 5 (person added or removed, icon) as not deletable; they are
# tried once in case that differs in practice. A rename notice (4) can be deleted by nobody, the bot with Manage Messages
# included, so it is never tried, queued or logged. Commands (20, 23), the thread starter (21) and text (0, 19) are never touched.
NEVER_DELETABLE = {4}
SYSTEM_TYPES = {1, 2, 5, 6, 7, 8, 9, 10, 11, 12, 14, 15, 16, 17, 18, 22}
SHOT_COOLDOWN, SHOTS_PER_HOUR, SHOT_WAIT, NOTICES_MAX = 120, 20, 180, 300  # per device; fleet-wide; seconds for a capture to arrive; slots kept
STATES = {"WORKING": ("🟡", "Working"), "QUEUED": ("⚪", "Queued"), "NEEDS YOU": ("🔴", "Needs you"), "WAITING": ("⏳", "Waiting"),
          "DONE": ("✅", "Done"), "FAILED": ("❌", "Failed"), "CUT OFF": ("⚠️", "Cut off"), "IDLE": ("⚪", "Idle"), "ENDED": ("⬛", "Ended"), "PAUSED": ("⏸️", "Paused")}
CHANNELS = {"overview": "Everything in flight, grouped by topic: one card per topic, updated in place. Pings are inside each card's thread.",
            "orchestrator": "Talk to the agents. Each request gets its own thread; keep talking inside it. 'builder: ...' addresses one agent. Slash commands: /status /run /loop /devices /stop /go",
            "progress": "One card per piece of work, updated in place: WORKING, DONE, CUT OFF, FAILED, WAITING.",
            "threads": "Your own Claude and Codex sessions on every device: one card and one thread each. The card shows the state, the thread keeps the prompts.",
            "reports": "Full reports from queue cards.", "alerts": "Failures and anything that needs the owner.",
            "loops": "Every improvement loop, one card each, updated in place. Only the owner types here: what you send (\"loop Birch, check onboarding daily\", \"stop looping Kepler\") goes to the orchestrator, which sets it up, and your message is removed."}
CHANNEL_NAMES = {"threads": "sessions"}  # config key -> Discord name, where they differ
CATEGORIES = {"overview": "Agents", "orchestrator": "Agents", "progress": "Agents", "alerts": "Agents", "reports": "Agents", "threads": "My sessions", "loops": "Agents"}  # where setup creates a missing channel
BOARDS = ("overview", "progress", "alerts", "reports", "threads", "loops")  # the bot posts here; the owner's messages are redirected
READ_ONLY = "Read-only: the bot posts here. Type in #orchestrator."
def board_topic(key): return ("" if key == "loops" else READ_ONLY + " " if key in BOARDS else "") + CHANNELS[key]
LOOP_EVERY = 60  # seconds between #loops refreshes; the relative times on its cards tick in Discord itself
LOOP_WORD = re.compile(r"^\s*/?loop\b[:,\s]*", re.I)
LOOP_HOWTO = ("\n\nThe owner typed this in #loops, so it is their Add (or their remove). Set it up with improve-loop.py in the switchboard folder: "
              "`add REPO` for a repo folder, `add NAME --path FOLDER --host DEVICE --brief \"...\" [--every HOURS]` for anything else, "
              "`add` again with a new --brief or --every to change one, `remove NAME` to stop one. Use --mode ask unless they say auto. "
              "If what to loop or where it lives is unclear, ask them one question. Reply in one line: what is now looped, how often, on which device.")
OV_EVERY, OV_KEEP, GONE_AFTER = 10, 3600, 20   # seconds between overview passes; a topic with nothing live keeps its card this long; a session must stay missing this long to count as gone
                                               # (a mail thread gets ANSWER_WAIT: the next hop's mail may come a file sync later)
OV_GAP, OV_MARK = 30, 60                       # a few missed overview passes is a break (restart, standby, a stall): open working spans end where they were last seen; how often that moment is saved
LEASE_RENEW, LEASE_STALE = 15, 90               # the serving bridge renews its lease this often; the primary takes back a standby's lease once it is this old
DISCORD_TAKEOVER = 150                          # with the lock message in Discord there is no sync lag: a standby takes over after this
RECLAIM_WAIT = 600                              # the primary back from offline waits at most this long for the serving host's state to sync (peer_synced)
TAKEOVER_STALE = 600                            # a standby takes over only after the primary's lease AND heartbeat are this old: over a Syncthing relay both arrive minutes late, and two bridges at once repost and delete each other's messages
PEERS = fleet.PEERS                             # device-local: an optional liveness relay's last reading of each host (fleet.confirmed_down)
SKEW = 300                                      # a lock up to this far "in the future" is fresh: the epoch is rounded to whole seconds and the two clocks differ a little
JANITOR_EVERY = 300                            # seconds between Gemini janitor passes (config "janitor"); a pass with no change since the last asks no model
LABEL_EVERY, LABEL_WAIT = 120, 300             # seconds between labelling runs; an unlabelled item waits this long before it shows under Unsorted
WORKED_MIN, TYPING, PING_KEEP, PINGS_SHOWN = 120, 60, 900, 5  # work shorter than this ends silently; the owner typed there this recently; an undelivered ping expires; pings left in a card's thread
LIVE = ("needs_you", "working", "queued")
OV_ICON = {"needs_you": "🔴", "working": "🟡", "queued": "⚪", "idle": "💤", "ended": "⬛", "done": "✅", "failed": "❌", "cutoff": "⚠️"}
OV_RANK = {"needs_you": 0, "working": 1, "queued": 2, "idle": 3}
PING_WORDS = {"needs": ("🔴", "Needs you"), "done": ("✅", "Finished"), "failed": ("❌", "Failed"), "cutoff": ("⚠️", "Cut off")}
SWEEP_EVERY, SWEEP_MAX, SWEEP_SCANS = 600, 20, 5  # seconds between cleaning passes; deletions per pass; old threads read per pass for answered cards
Q_KEEP, ALERT_KEEP, PROG_KEEP, UNTRACKED_GRACE, REDIRECT_KEEP = 3600, 6 * 3600, 86400, 3600, 7 * 86400
def snow_time(mid): return (int(mid) >> 22) / 1000 + 1420070400  # when Discord made this id
PROJECT_PACE = 60        # seconds between renames that only add the project to a thread name, so open threads are not all renamed at once
NAME_BATCH = 5           # session threads named per pass, so a backlog cannot hold up the gateway
CLOSE_REFUSED_WAIT, CLOSE_REFUSED_TRIES = 3600, 6  # a thread opened for a rename that Discord refuses to close again: hourly, then given up
COMMANDS = [
    {"name": "status", "description": "Queue, agents, devices and the loop at a glance"},
    {"name": "devices", "description": "Which devices are up and what each can run"},
    {"name": "progress", "description": "Finished, failed and cut-off work from the last 24 hours"},
    {"name": "agents", "description": "List the agents"},
    {"name": "usage", "description": "What the agent runs cost in the last 24 hours, by agent and device"},
    {"name": "stop", "description": "Pause every agent, the queue and the loop"},
    {"name": "go", "description": "Resume after /stop"},
    {"name": "loop", "description": "The improvement loop", "options": [
        {"type": 3, "name": "action", "description": "What to do", "required": True, "choices": [{"name": n, "value": n} for n in ("status", "on", "off", "run", "add")]},
        {"type": 3, "name": "repo", "description": "Repo folder, for run", "required": False},
        {"type": 3, "name": "what", "description": "For add: what to loop, in your words; the orchestrator sets it up", "required": False}]},
    {"name": "run", "description": "Give the orchestrator a task", "options": [
        {"type": 3, "name": "task", "description": "What to do", "required": True},
        {"type": 3, "name": "where", "description": "Which device", "required": False, "choices": [{"name": "Let it decide", "value": "auto"}, {"name": "PC", "value": "windows"}, {"name": "Mac", "value": "mac"}]}]}]

LOG_TAIL = []  # the last log lines, published in status-<host>.json so the other device can read them without remote control
def log(*a):
    line = " ".join(str(x) for x in (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),) + a)
    print(line, flush=True); LOG_TAIL.append(line[:300]); del LOG_TAIL[:-40]
def dpath(n): return queue_root() / "discord" / n
def code_hash(d=None):
    """sha1[:10] over every file the bridge loads, as Queue/discord/tested-<hash>.ok names it after a full test pass."""
    h = hashlib.sha1()
    for f in RELOAD_FILES:
        p = Path(d or TK) / f
        if p.exists(): h.update(f.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()[:10]
def load(n, d):
    try: return json.loads(dpath(n).read_text(encoding="utf-8"))
    except (OSError, ValueError): return d
def save(n, v): fleet._write(dpath(n), v)
def shared_state():
    """The bridge state as every device saved it: each bridge writes only Queue/discord/state.<host>.json, readers merge
    all of them with the old state.json (fleet.load_merged). One writer per synced file: Syncthing cannot merge."""
    return fleet.load_merged(dpath("state.json"), {})

STATUS_LINE = re.compile(r"^\S+ \*\*(Working|Queued|Needs you|Waiting|Paused|Done|Failed|Cut off)\*\*")
def walk_components(items):
    """Every component in a message, cards included (a card nests its rows and texts inside a container)."""
    for c in items or []:
        yield c
        yield from walk_components(c.get("components"))
def msg_text(m):
    """A message's text: its content, or the text blocks of a card."""
    return m.get("content") or "\n".join(c.get("content", "") for c in walk_components(m.get("components")) if c.get("type") == 10)
def card_kind(md):
    """Card colour from what the message says: blue working, yellow needs the owner, green done, red failed, light blue an
    update from elsewhere, grey information or an answered question."""
    text = (md or "").strip(); first = text.splitlines()[0] if text else ""
    m = STATUS_LINE.match(first)
    if m: return {"Working": "working", "Queued": "info", "Waiting": "info", "Paused": "info", "Needs you": "ask", "Done": "done", "Failed": "fail", "Cut off": "fail"}[m[1]]
    if "**Answer:**" in text or re.search(r"-# (On hold|No longer needed|Replaced)", text): return "info"
    if " asks:" in first: return "ask"
    if first.startswith(("✦", "**FAILED", "**CUT OFF")): return "fail" if first.startswith("**") else "update"
    low = first.lower()
    if re.search(r"\b(failed|error|crash\w*|broke|could ?n[o']t|can'?t|refused|cut off)\b", low): return "fail"
    if re.search(r"\bneeds you\b|\basks\b", low): return "ask"
    if re.search(r"\bworking\b|\bstarted\b", low) and not re.search(r"\bworking: 0\b", low): return "working"
    if re.search(r"\b(not (built|fixed|published|live|posted|done)|nothing (is )?(built|published|fixed)|not yet|waiting on|still open|is queued)\b", low): return "ask"
    if DONE_WORDS.search(low) or re.search(r"\b(built|done|live|ready|passed|saved|finished|complete\w*)\b", low): return "done"
    return "info"
INTERNAL_TAG = re.compile(r"^\s*(AREA|TIER|CHECKDIR|CHECK|ROUTE)\s*:\s*\S", re.I)
MAX_FILES = 4  # per answer, the newest by mtime (the current progress), one message each so the owner can reply to each file; more are listed by name
FILE_EXT = r"\.(?:html?|pdf|png|jpe?g|gif|webp|mp4|mov|md|txt|csv|json)"
FILE_PATH = re.compile(r"(?<![\w/\\:.-])(?:/Users/|[A-Za-z]:\\|[\w.-]+/)[^\s`'\"<>()\[\]]*?" + FILE_EXT + r"(?![\w/\\-])", re.I)  # a bare path an answer names; code spans may hold spaces
class _Roots:
    """The synced root as each device spells it, lower case with / and a trailing /: this device's own (dev_root) and
    each peer's from its heartbeat's "root" field. A heartbeat without that field adds nothing, so a path under that
    peer's root counts only by its platform form. Read on every use, so a device that joins later counts."""
    @staticmethod
    def norm(p): return str(p).replace("\\", "/").rstrip("/").lower() + "/"
    def values(self):
        out = [self.norm(dev_root())]
        try: out += [self.norm(d["root"]) for d in fleet.devices() if d.get("root")]
        except Exception: pass
        return list(dict.fromkeys(out))
ROOTS = _Roots()
Q_ROW_V = 2  # question button layout; 2 added Done elsewhere. Raising it re-edits every open card once
ELSEWHERE_EVERY = 60  # seconds between looks for questions settled outside Discord
PERMISSION_GRACE = 120  # past a permission hook's own wait, its card is closed
RELOAD_FILES = ("discord-bridge.py", "cards.py", "fleet.py", "notes.py", "agent-mail.py", "topics.py", "screencap.py", "common.py")  # what the running bridge has loaded
ASKS_OWNER = re.compile(r"\b(your (choice|call|go-ahead)|give me (a|the) go-ahead|let me know|tell me (which|whether|if)|should i|do you want|want me to)\b", re.I)
DONE_WORDS = re.compile(r"\b(publish\w*|deploy\w*|restart\w*|posted|announced|is live|went live|shipped|merged|fixed|released)\b", re.I)
def norm_name(x): return re.sub(r"[^a-z0-9]", "", str(x or "").lower())  # "Stack.io" and "stack.io" are one project
def same_question(a, b):
    """The same decision asked again: the same buttons and mostly the same words (not two different choices in one thread)."""
    if [o["label"] for o in a["options"]] != [o["label"] for o in b["options"]]: return False
    w = lambda q: set(re.findall(r"[a-z0-9]{4,}", str(q.get("body") or "")[:400].lower()))
    x, y = w(a), w(b)
    return bool(x and y) and len(x & y) / len(x | y) >= (0.4 if len(a["options"]) > 2 else 0.55)  # a plain yes/no needs more overlap to be the same decision

class DiscordError(RuntimeError):
    def __init__(self, method, path, status, raw):
        self.status = status
        try: self.code = (json.loads(raw) or {}).get("code")
        except (ValueError, AttributeError): self.code = None
        super().__init__(f"Discord {method} {path.split('?')[0]} -> {status} {raw[:300].decode(errors='replace')}")
class RateLimited(RuntimeError):
    def __init__(self, wait):
        self.wait = wait; super().__init__(f"Discord rate limit, {wait:.0f} s left")

BLOCKED = {}  # route -> time it opens again, for limits too long to sleep through
def route(method, path): return method + " " + re.sub(r"/messages/\d+", "/messages/#", path.split("?")[0])

def api(method, path, body=None, auth=True, files=None):
    """One Discord call. Short rate limits are slept through; long ones raise RateLimited at once so the caller can
    retry later, because sleeping here would stop the gateway heartbeat."""
    key = route(method, path) + (" name" if method == "PATCH" and isinstance(body, dict) and "name" in body else "")  # a blocked rename must not block closing the thread
    for _ in range(5):
        wait = max(BLOCKED.get(key, 0), BLOCKED.get("*", 0)) - time.time()
        if wait > 0: raise RateLimited(wait)
        h = {"User-Agent": "DiscordBot (https://localhost, 2.0)"}
        if auth: h["Authorization"] = "Bot " + TOKEN.read_text(encoding="utf-8").strip()
        if files:
            b = "----bridge" + os.urandom(8).hex(); h["Content-Type"] = f"multipart/form-data; boundary={b}"
            data = f"--{b}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\nContent-Type: application/json\r\n\r\n{json.dumps(body or {})}\r\n".encode()
            for n, (name, blob) in enumerate(files):
                safe = re.sub(r"[^\w.\- ]", "_", name)[:120] or "file"
                data += f"--{b}\r\nContent-Disposition: form-data; name=\"files[{n}]\"; filename=\"{safe}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode() + blob + b"\r\n"
            data += f"--{b}--\r\n".encode()
        else:
            h["Content-Type"] = "application/json"; data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, method=method, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=120 if files else 30) as r:
                raw = r.read(); return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read() or b"{}"
            if e.code == 429:
                info = json.loads(raw); after = float(info.get("retry_after", 2)) + 0.2
                if after <= MAX_SLEEP: time.sleep(after); continue
                BLOCKED["*" if info.get("global") else key] = time.time() + after
                raise RateLimited(after)
            raise DiscordError(method, path, e.code, raw)
    raise RateLimited(MAX_SLEEP)

def mail(*args, check=True):
    r = subprocess.run([sys.executable, "-X", "utf8", str(TK / "agent-mail.py"), *args], capture_output=True, text=True, encoding="utf-8", timeout=60)
    if check and r.returncode: raise RuntimeError("agent-mail " + " ".join(args[:2]) + ": " + (r.stderr or r.stdout)[-300:])
    return r.stdout.strip()

def tool(script, *args):
    r = subprocess.run([sys.executable, "-X", "utf8", str(TK / script), *args], capture_output=True, text=True, encoding="utf-8", timeout=60)
    return (r.stdout + r.stderr).strip()

# ---- message text: Discord counts UTF-16 units, 2000 per message, 100 per thread name ----
def dlen(s): return len(s.encode("utf-16-le")) // 2
def fit(s, limit):
    """Index of the longest prefix of s within limit UTF-16 units."""
    if len(s) * 2 <= limit: return len(s)
    n = 0
    for i, c in enumerate(s):
        n += 2 if ord(c) > 0xFFFF else 1
        if n > limit: return i
    return len(s)
def clip(s, limit=2000):
    s = s or "(empty)"
    return s if dlen(s) <= limit else s[:fit(s, limit - 1)] + "…"
def fenced(s):
    """A clipped text that cut a code block open gets it closed, so whatever follows is not swallowed into the block."""
    return s + "\n```" if s.count("```") % 2 else s
def quoted(text):
    """The owner's words quoted in another thread: a > quote, or as they are when they hold a code block a quote would break."""
    return text if "```" in text else "> " + text.replace("\n", "\n> ")
def split_message(text, limit=LIMIT):
    """Split at paragraph, line or word boundaries; a code block cut in two is closed and reopened."""
    text, out, fence = (text or "").strip() or "(empty)", [], ""
    while True:
        lead = fence + "\n" if fence else ""
        if dlen(lead + text) <= limit: out.append(lead + text); return out
        room = limit - dlen(lead) - 8
        end = fit(text, room); cut = text.rfind("\n#", 0, end)  # a heading starts the next part, never ends this one
        if cut < end // 2: cut = text.rfind("\n\n", 0, end)
        if cut < end // 2: cut = text.rfind("\n", 0, end)
        if cut < end // 2: cut = text.rfind(" ", 0, end)
        if cut < end // 2: cut = end
        part, text = text[:cut].rstrip(), text[cut:].strip("\n")  # leading spaces stay: nested lists and code keep their indent
        for line in part.splitlines():
            if line.lstrip().startswith("```"): fence = "" if fence else line.strip()
        out.append(lead + part + ("\n```" if fence else ""))
        if not text: return out
def word_cut(s, n):
    """s within n UTF-16 units, ended at a sentence if one is close enough, else at a word."""
    if dlen(s) <= n: return s
    s = s[:fit(s, n - 1)]; ends = list(re.finditer(r"[.!?](?=\s)", s))
    if ends and ends[-1].end() > n // 3: return s[:ends[-1].end()]
    head = s.rsplit(" ", 1)[0]
    return (head if len(head) > n // 2 else s).rstrip(" .,:;-") + "…"
def clean_title(s, n=90, whole=False):
    raw = re.sub(r"^(\s*re:\s*)+", "", str(s or ""), flags=re.I)
    s = re.sub(r"<(@[!&]?|#|a?:\w+:)\d+>|<t:\d+(:\w)?>", " ", raw)
    s = re.sub(r"[*`~|]+", "", s); s = " ".join(s.split())
    if not s: return "Request"
    if s.startswith("/"):  # a slash command names nothing: the rest of the prompt does, and its one-word argument ("off") is not a title
        s = re.sub(r"^(/[\w:.-]+(?=\s|$)\s*)+", "", s)
        if len(s.split()) < 2: return "Session"
    if not whole and len(raw) in TITLE_CLIPS and s[-1].isalnum(): n = min(n, dlen(s) - 1)  # already clipped by length, so its last word is partial
    return word_cut(s, n)
def slash_only(s): return str(s or "").lstrip().startswith("/") and clean_title(s) == "Session"
def project_label(repo="", area=""):
    """A project as thread names show it: a repo folder under its own name, an area slug in plain words (discord-bridge -> Discord bridge)."""
    if repo:
        try: return next((p.name for p in dev_root().iterdir() if p.is_dir() and p.name.lower() == str(repo).lower()), str(repo))
        except OSError: return str(repo)
    s = " ".join(x for x in re.split(r"[-_.\s]+", str(area or "")) if x)
    return s[:1].upper() + s[1:]
def project_of(msg):
    """The project a mail message or progress event belongs to: its repo folder, else the AREA line in its body, else ''."""
    if msg.get("repo"): return project_label(repo=msg["repo"])
    hit = fleet.AREA_RE.search(str(msg.get("body") or ""))
    return project_label(area=hit.group(1)) if hit else ""
NO_PROJECT = re.compile(r"\?|proj|project|temp|untitled|tmp.*|scratch(?:[-_].*)?", re.I)  # throwaway folders a session can run in: they name no project
def project_key(s): return re.sub(r"[\W_]+", " ", str(s)).strip().lower()
def with_project(project, title, n=90, whole=False):
    """'Project: what it is about' within n. Only the description is shortened, never the project, and a title that
    already starts with the project (in any spelling of it) does not get it twice. No project: the plain title."""
    name = clean_title(title, n, whole)
    if not project: return name
    head, sep, rest = name.partition(":")
    if sep and project_key(head) == project_key(project): name = rest.strip()
    return f"{project}: " + word_cut(name, max(n - dlen(project) - 2, 8)) if name else str(project)
def strip_linked(body):
    """(text, thread ids) for a body whose last line is 'LINKED: id id ...'; ids always hold a digit, so a sentence is left alone."""
    lines = (body or "").rstrip().splitlines()
    hit = re.fullmatch(r"\s*LINKED:\s*(\S*\d\S*(?:\s+\S*\d\S*)*)\s*", lines[-1]) if lines else None
    return ("\n".join(lines[:-1]).rstrip(), hit[1].split()) if hit else (body, [])

# ---- minimal WebSocket client (RFC 6455, text frames, client masking) ----
class WS:
    def __init__(self, url):
        host = re.match(r"wss://([^/]+)", url).group(1); path = url[len("wss://" + host):] or "/"
        self.s = ssl.create_default_context().wrap_socket(socket.create_connection((host, 443), timeout=30), server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            c = self.s.recv(4096)
            if not c: raise ConnectionError("closed during handshake")
            self.buf += c
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]: raise ConnectionError("handshake refused: " + head[:80].decode(errors="replace"))
    def _frame(self, op, data):
        n, mask = len(data), os.urandom(4)
        head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
    def send(self, text): self._frame(1, text.encode("utf-8"))
    def _read(self, n):
        while len(self.buf) < n:
            c = self.s.recv(65536)
            if not c: raise ConnectionError("socket closed")
            self.buf += c
        out, self.buf = self.buf[:n], self.buf[n:]; return out
    def recv(self, timeout):
        """One text message, or None on timeout. Raises ConnectionError when the socket closes."""
        parts = []
        while True:
            self.s.settimeout(timeout if not parts and not self.buf else 30)
            try: b1, b2 = self._read(2)
            except (socket.timeout, TimeoutError): return None
            self.s.settimeout(30)
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126: n = struct.unpack(">H", self._read(2))[0]
            elif n == 127: n = struct.unpack(">Q", self._read(8))[0]
            data = self._read(n)
            if op == 8: raise ConnectionError("gateway closed: " + (str(struct.unpack(">H", data[:2])[0]) if len(data) >= 2 else "no code"))
            if op == 9: self._frame(10, data); continue
            if op == 10: continue
            parts.append(data)
            if b1 & 0x80: return b"".join(parts).decode("utf-8", errors="replace")
    def close(self):
        try: self.s.close()
        except OSError: pass

# ---- setup ----
# What setup writes into a new config (keys already there are kept). The quieter layout, and nothing that needs an
# extra tool or shares more than text: screen captures of a blocked device and the small-model helpers stay off until
# the owner turns them on. The code's own defaults, used when a key is absent, are the older behaviour.
NEW_INSTALL = {"one_answer_per_turn": True, "notepad": True, "quick_status": True, "progress_boards": True, "calm_names": True,
               "thread_state_names": False, "add_owner_to_threads": False, "screenshots": False, "triage": False, "janitor": False,
               "argon_chat": False}
def setup(guild):
    """Channels are found by their configured id, then by name; only what is missing is created, in its category. Moved or renamed channels stay put."""
    g, rows, cfg = api("GET", f"/guilds/{guild}"), api("GET", f"/guilds/{guild}/channels"), load("config.json", {})
    known, text, ch = cfg.get("channels") or {}, [c for c in rows if c["type"] == 0], {}
    ids, names, cats = {c["id"] for c in text}, {c["name"]: c["id"] for c in text}, {c["name"].lower(): c["id"] for c in rows if c["type"] == 4}
    def category(name):
        if name.lower() not in cats: cats[name.lower()] = api("POST", f"/guilds/{guild}/channels", {"name": name, "type": 4})["id"]
        return cats[name.lower()]
    made = []
    for key in CHANNELS:
        name = CHANNEL_NAMES.get(key, key); cid = known.get(key) if known.get(key) in ids else names.get(name)
        if not cid: cid = api("POST", f"/guilds/{guild}/channels", {"name": name, "type": 0, "topic": board_topic(key), "parent_id": category(CATEGORIES[key])})["id"]; made.append(key)
        ch[key] = cid
    for n in CHANNELS: api("PATCH", f"/channels/{ch[n]}", {"topic": board_topic(n)})
    if "overview" in made:  # a new overview channel goes to the top of its category; one that exists stays where the owner put it
        try:
            parent = cats[CATEGORIES["overview"].lower()]
            rest = sorted((c for c in text if c.get("parent_id") == parent), key=lambda c: (c.get("position") or 0, int(c["id"])))
            api("PATCH", f"/guilds/{guild}/channels", [{"id": ch["overview"], "position": 0}] + [{"id": c["id"], "position": n + 1} for n, c in enumerate(rest)])
        except DiscordError as e: print("overview channel created but not moved to the top:", str(e)[:160])
    me = api("GET", "/users/@me")
    cfg = {**cfg, "guild_id": guild, "owner_id": g["owner_id"], "bot_id": me["id"], "channels": {**known, **ch}}
    cfg.setdefault("hosts", [hostname()])
    for k, v in NEW_INSTALL.items(): cfg.setdefault(k, v)
    save("config.json", cfg)
    api("PUT", f"/applications/{me['id']}/guilds/{guild}/commands", COMMANDS)
    print("setup ok:", g["name"], "channels", list(ch), "commands", [c["name"] for c in COMMANDS], "hosts", cfg["hosts"])

# ---- text helpers ----
def status_text():
    q = queue_root(); n = lambda d: len([p for p in (q / d).glob("*.md") if not p.name.endswith(".report.md")]) if (q / d).is_dir() else 0
    devs = ", ".join(f"{d['label']} {'up' if d['available'] else 'offline'}" for d in fleet.devices()) or "none seen"
    caps = "; ".join(f"{d['label']} {d.get('runs_hour', 0)}/{d.get('cap_hour', '?')} this hour, {d.get('runs_day', 0)}/{d.get('cap_day', '?')} today" for d in fleet.devices() if d["available"])
    waiting = {a: len(list((q / "mail" / a / "inbox").glob("*.json"))) for a in load_agents() if a != "owner"}
    locks = [p.stem for p in (q / "locks").glob("*.json")] if (q / "locks").is_dir() else []
    return (f"{'**PAUSED** (use /go). ' if (q / 'STOP').exists() else ''}Devices: {devs}.\nAgent runs: {caps or 'no runner up'}.\n"
            f"Mail waiting: {', '.join(f'{a} {c}' for a, c in waiting.items() if c) or 'none'}. Queue cards: {n('todo')} waiting, {n('running')} running.\n"
            f"Repos held: {', '.join(locks) or 'none'}.\n" + tool("improve-loop.py", "status"))
def load_agents():
    try: return json.loads((queue_root() / "mail/agents.json").read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}
def devices_text():
    rows = []
    for d in fleet.devices():
        rows.append(f"**{d['label']}** ({d['host']}): {'up' if d['available'] else f'offline, last seen {d['age'] // 60} min ago'}. Repos: {', '.join(d.get('repos') or []) or 'none'}.")
    return "\n".join(rows) or "No device has reported yet."
def progress_text():
    ev = [e for e in fleet.events(time.time() - 86400) if e["kind"] != "started"][-18:]
    return "```\n" + ("\n".join(fleet.line(e) for e in ev) or "nothing in the last 24 hours")[:1850] + "\n```"
def clock(s): s = int(max(0, s)); return f"{s // 60}:{s % 60:02d}"

class Bridge:
    def __init__(self):
        self.cfg = load("config.json", None) or sys.exit(f"This root has no Discord setup yet ({dpath('config.json')} is missing). Run first:\n  "
                                                         f"{Path(sys.executable).name} {Path(__file__).name} setup --guild <your server id>\ndocs/discord-setup.md explains where the id comes from.")
        self.st = self.load_state(); self.state_saved = fleet.saved_at(fleet.host_path(dpath("state.json"), hostname()))  # named in each lease renewal, see peer_synced
        self.ch, self.owner = self.cfg["channels"], self.cfg["owner_id"]
        self.dirty, self.last_sweep, self.last_tick, self.last_q, self.saved = set(), 0.0, 0.0, 0.0, ""
        self.sysq = []  # automatic notices waiting to be deleted
        self.open_cache = None  # mail threads with undelivered agent mail, rebuilt each tick
        self.blind = False      # the agent inboxes could not be read on the last tick, so no thread newly claims Done
        self.bad_mail = {}      # inbox files that cannot be read: {path: {"t0", "agent", "thread", "why"}}; each holds only its own thread
        self.mail_seen = {}     # {message id: thread} of the waiting mail as last read, so a file that turns unreadable still has a thread
        self.mail_err = None    # why the whole check last failed, so the log gets a line when that changes and not on every tick
        self.name_budget = 0
        self.project_t = 0.0    # when a thread name last gained its project, see PROJECT_PACE
        self.lock_check = None  # threads whose name lock is still to be checked against Discord, set on the first tick
        self.last_ov, self.last_label, self.label_proc = 0.0, 0.0, None
        self.last_clean, self.budget = time.time(), 0  # the first cleaning pass comes 10 minutes after a start

    def save_state(self): self.state_saved = fleet.save_host(dpath("state.json"), self.st, hostname())  # this device's own copy only
    def load_state(self):
        """Older state files had no thread fields; everything they hold stays valid, so upgrading only adds keys."""
        raw = shared_state()
        if raw and raw.get("v") != 2 and not dpath("state.v1-backup.json").exists(): save("state.v1-backup.json", raw)
        raw.pop("sys_undeletable", None)  # each start retries every notice type once: a refusal may have been a missing permission since granted
        return {"threads": {}, "q": {}, "pending": {}, "ev_ts": time.time(), "reports": [], "dthreads": {}, "alias": {}, **raw, "v": 2}

    # ---- Discord primitives ----
    def send_to(self, cid, content, reply_to=None, components=None, ping=False, card=None):
        if card is None: card = self.cfg.get("cards", True)  # every bot message is a card unless a caller says otherwise
        if card and self.cfg.get("cards", True):  # a coloured card (cards.py): answers, questions, status, updates
            body = self.card_body(content, components, ping)
        else:
            body = {"content": clip(content), "allowed_mentions": {"users": [self.owner]} if ping else {"parse": []}}
            if components is not None: body["components"] = components
        if reply_to: body["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
        mid = api("POST", f"/channels/{cid}/messages", body)["id"]
        if "flags" in body: self.remember_card(mid, content, components)
        return mid
    def card_body(self, md, rows=None, ping=False):
        """One card: the colour follows what the text says (card_kind), the buttons sit inside it."""
        import cards
        return {"content": "", "flags": cards.V2, "components": [cards.card(card_kind(md), cards.text(clip(md, 3900).strip() or "\u200b"), *(rows or []))],
                "allowed_mentions": {"users": [self.owner]} if ping else {"parse": []}}
    def remember_card(self, mid, md, rows):
        """A card's text and buttons, kept so a later button change can rebuild it (a card has no content to keep them)."""
        c = self.st.setdefault("cards", {}); c[str(mid)] = {"md": md, "rows": rows or []}
        if len(c) > 600: self.st["cards"] = dict(list(c.items())[-500:])
    def card_of(self, mid): return (self.st.get("cards") or {}).get(str(mid))
    def md_of(self, m):
        """The text of a message as the owner sees it: a card's own text, else its content."""
        rec = self.card_of(m.get("id")); return rec["md"] if rec else (m.get("content") or "")
    def send(self, channel, content, reply_to=None, components=None, ping=False, card=None):
        return self.send_to(self.ch[channel], content, reply_to, components, ping, card)
    def edit_to(self, cid, mid, content, components=None, card=None):
        rec = self.card_of(mid)
        if card is None: card = self.cfg.get("cards", True)  # an older plain message becomes a card the next time it changes
        if rec or (card and self.cfg.get("cards", True)):  # a card stays a card; card=True turns a plain message into one
            rows = components if components is not None else (rec or {}).get("rows")
            body = self.card_body(content, rows); body.pop("content", None) if rec else None
            r = self.guard(cid, lambda: api("PATCH", f"/channels/{cid}/messages/{mid}", body))
            self.remember_card(mid, content, rows); return r
        body = {"content": clip(content), "allowed_mentions": {"parse": []}}
        if components is not None: body["components"] = components
        return self.guard(cid, lambda: api("PATCH", f"/channels/{cid}/messages/{mid}", body))
    def edit(self, channel, mid, content, components=None):
        """True when edited, None when the message no longer exists, False on any other failure."""
        try: self.edit_to(self.ch[channel], mid, content, components); return True
        except DiscordError as e:
            if e.code == UNKNOWN_MESSAGE: return None
            log("edit failed", str(e)[:120]); return False
        except RateLimited: return False
    def delete_to(self, cid, mid): self.guard(cid, lambda: api("DELETE", f"/channels/{cid}/messages/{mid}"))
    def upload_to(self, cid, name, data, caption="", reply_to=None):
        payload = {"content": caption[:1900], "allowed_mentions": {"parse": []}}
        if reply_to: payload["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
        return api("POST", f"/channels/{cid}/messages", payload, files=[(name, data)])["id"]

    # ---- replaceable notices: the only bot messages that cleanup may edit away or delete ----
    def notice(self, slot, cid, text, reply_to=None):
        """Post or update the one message this slot owns, so a thread shows the current notice and not its history.
        Only short bot text goes through here, which is why the owner's messages, open cards, answers and files can never be cleaned up."""
        slots = self.st.setdefault("notices", {}); cur = slots.get(slot)
        if cur and cur["ch"] != cid: self.clear_notice(slot); cur = slots.get(slot)
        if cur:
            if cur["text"] == text: return cur["id"]
            try: self.edit_to(cid, cur["id"], text); cur["text"] = text; return cur["id"]
            except DiscordError as e:
                if e.code != UNKNOWN_MESSAGE: raise
        slots[slot] = {"ch": cid, "id": self.guard(cid, lambda: self.send_to(cid, text, reply_to=reply_to)), "text": text, "at": time.time()}
        if len(slots) > NOTICES_MAX:
            for k in sorted(slots, key=lambda k: slots[k]["at"])[:len(slots) - NOTICES_MAX]: del slots[k]  # forgotten, not deleted
        return slots[slot]["id"]
    def clear_notice(self, slot):
        """Delete the slot's message. No guard: removing a stale line must not reopen an archived thread."""
        cur = self.st.setdefault("notices", {}).get(slot)
        if not cur: return
        try: api("DELETE", f"/channels/{cur['ch']}/messages/{cur['id']}")
        except RateLimited: return  # the slot stays, so the next pass tries again
        except DiscordError as e:
            if e.code not in (UNKNOWN_MESSAGE, UNKNOWN_CHANNEL): log("notice not removed", slot[:60], str(e)[:120])
        self.st["notices"].pop(slot, None)

    # ---- Discord's automatic notices ----
    def on_system(self, d):
        if self.cfg.get("delete_system_notices") is False or d.get("guild_id") != self.cfg.get("guild_id"): return
        if int(d["type"]) in NEVER_DELETABLE or int(d["type"]) in self.st.get("sys_undeletable", []): return
        if len(self.sysq) < 200: self.sysq.append({"ch": d["channel_id"], "id": d["id"], "type": int(d["type"])})
        self.drain_system()
    def drain_system(self):
        """Delete queued notices. A rate limit keeps the rest for the next tick."""
        while self.sysq:
            x = self.sysq[0]
            try: api("DELETE", f"/channels/{x['ch']}/messages/{x['id']}")
            except RateLimited: return
            except DiscordError as e:
                if e.code == SYSTEM_MESSAGE:
                    self.st.setdefault("sys_undeletable", []).append(x["type"]); log(f"Discord refuses to delete system message type {x['type']}; not trying that type again")
                    self.sysq = [y for y in self.sysq if y["type"] != x["type"]]; continue
                if e.code == MISSING_PERMISSIONS:
                    try: self.alert("sys-notice-permission", "Could not delete an automatic Discord notice. Give the bot **Manage Messages** in this server.")
                    except Exception as a: log("alert failed", str(a)[:100])
                elif e.code not in (UNKNOWN_MESSAGE, UNKNOWN_CHANNEL): log("system notice not deleted", str(e)[:120])
            except Exception as e: log("system notice delete failed, will retry", str(e)[:100]); return
            self.sysq.pop(0)

    # ---- screenshots of blocked work ----
    def spawn(self, fn): threading.Thread(target=fn, daemon=True).start()  # a capture takes seconds; the gateway must keep beating
    def shots(self): return self.st.setdefault("shots", {"seen": {}, "log": [], "hosts": {}, "wait": {}})
    def want_shot(self, key, cid, host, reply_to=None):
        """One screenshot of HOST for one blocked thing, posted under its card. Never raises: the card matters more."""
        try:
            if self.cfg.get("screenshots") is False or not host: return
            s, now = self.shots(), time.time()
            if key in s["seen"]: return
            s["seen"][key] = now  # decided once, whatever happens to this capture, so a stuck loop cannot repeat it
            if len(s["seen"]) > 400: s["seen"] = dict(sorted(s["seen"].items(), key=lambda kv: kv[1])[-300:])
            s["log"] = [t for t in s["log"] if now - t < 3600]
            who = str(host).lower()
            if now - s["hosts"].get(who, 0) < SHOT_COOLDOWN or len(s["log"]) >= SHOTS_PER_HOUR: return log("screenshot skipped by rate limit", key[:80])
            local = who == hostname().lower()
            if not local and not fleet.available(host): return self.shot_note(cid, host, "that device is offline", reply_to)
            s["log"].append(now); s["hosts"][who] = now
            sid = uuid.uuid4().hex; s["wait"][sid] = {"ch": cid, "host": host, "reply": reply_to, "at": now}
            if local: self.spawn(lambda: screencap.fulfil(sid, screencap.LOCAL))
            else: screencap.ask(sid, host)
        except Exception as e: log("screenshot request failed", str(e)[:160])
    def shot_label(self, host): return next((d["label"] for d in fleet.devices() if d["host"].lower() == str(host).lower()), host)
    def shot_note(self, cid, host, why, reply_to=None):
        """The text instead of a picture. One per channel, replaced in place, so failures cannot pile up either."""
        try: self.notice(f"shot:{cid}", cid, f"-# No screenshot from {self.shot_label(host)}: {why}.", reply_to=reply_to)
        except Exception as e: log("screenshot note failed", str(e)[:120])
    def pump_shots(self, now):
        """Upload captures that have arrived. Files are removed once handled; a capture that never arrives becomes a note."""
        s = self.shots()
        for sid, w in list(s["wait"].items()):
            path, why = screencap.result(sid)
            if path:
                try:
                    data = path.read_bytes()
                    if len(data) > 10 * 1024 * 1024: raise ValueError("capture over 10 MB")
                    self.upload_to(w["ch"], f"screen-{self.shot_label(w['host'])}.jpg", data, f"-# Screen of {self.shot_label(w['host'])} <t:{int(w['at'])}:R>", w.get("reply"))
                    self.clear_notice(f"shot:{w['ch']}")
                except (RateLimited, urllib.error.URLError, OSError):  # try again next tick, until it expires
                    if now - w["at"] < SHOT_WAIT: continue
                    self.shot_note(w["ch"], w["host"], "the upload kept failing", w.get("reply"))
                except Exception as e:
                    log("screenshot upload refused", str(e)[:160]); self.shot_note(w["ch"], w["host"], "Discord refused the image", w.get("reply"))
            elif why: self.shot_note(w["ch"], w["host"], why, w.get("reply"))
            elif now - w["at"] < SHOT_WAIT: continue
            else: self.shot_note(w["ch"], w["host"], "no capture arrived in time", w.get("reply"))
            del s["wait"][sid]; screencap.forget(sid)
    def blocked_host(self, t, m=None):
        """The device where this work is blocked: a permission request names it, otherwise the run that is going."""
        if m and m.get("from") == "sessions":
            hit = re.search(r"^Host: *(\S+)", str(m.get("body", "")), re.M)
            if hit: return hit[1]
        runs = sorted((r for r in t["runs"].values() if r.get("host")), key=lambda r: r["t0"])
        live = [r for r in runs if not r.get("end")]
        return (live or runs)[-1]["host"] if runs else hostname()
    def check_stuck(self, tid, t):
        """A run that went silent past the limit, recently. Runs from before a restart are left alone."""
        now = time.time()
        for ref, r in t["runs"].items():
            if not r.get("end") and r.get("host") and RUN_LIMIT <= now - r["t0"] < RUN_LIMIT + 3600:
                self.want_shot(f"stuck:{tid}:{ref}", t.get("dthread") or self.ch["alerts"], r["host"])

    # ---- real Discord threads ----
    def guard(self, cid, fn):
        """Run one call against a channel: reopen an archived or locked thread once; forget one that was deleted."""
        for attempt in (0, 1):
            try: return fn()
            except DiscordError as e:
                if attempt == 0 and e.code in (THREAD_ARCHIVED, THREAD_LOCKED) and cid in self.st["dthreads"]:
                    self.reopen(cid, e.code == THREAD_LOCKED); continue
                if e.code == UNKNOWN_CHANNEL: self.forget(cid)
                raise
    def reopen(self, cid, unlock=False):
        api("PATCH", f"/channels/{cid}", {"archived": False, **({"locked": False} if unlock else {})})
        t = self.st["threads"].get(self.st["dthreads"].get(cid, ""))
        if t: t.pop("archived", None)
    def forget(self, cid):
        """The Discord thread is gone. A session stays without one; mail work gets a new one on its next reply."""
        tid = self.st["dthreads"].pop(cid, None)
        if not tid: return
        if tid.startswith("session:"):
            c = self.st.get("sessions", {}).get(tid[8:])
            if c: c.pop("thread", None); c["no_thread"] = True
            return
        t = self.st["threads"].get(tid)
        if not t: return
        for k in ("dthread", "status", "status_ch", "status_text", "status_close", "anchor_text", "archived", "name_want", "name_t", "links_msg", "links_ch", "links_text",
                  "close_msg", "answer_msg", "answered", "answers"): t.pop(k, None)  # the answer and its button went with the thread
        t["gone"] = True; log("thread deleted", tid)
    def touch(self, t): t["active_at"] = time.time()
    def thread_name(self, t):
        """A hand-off (the last run ended, its mail waits for another agent) is usually short, so a thread already named
        Working keeps that name for HANDOFF_NAME_WAIT instead of spending a rename on Queued: the rename is then still
        there for Done. A name with any other state, the checkmark above all, is not kept."""
        name = self.title_of(t)
        if LOOP_TITLE.match(str(t.get("title") or "")) and t.get("t0"):  # two loop cycles of one project must not share a name
            d = datetime.datetime.fromtimestamp(float(t["t0"])); tag = f" · {d.strftime('%b')} {d.day}"
            if self.cfg.get("thread_state_names") is False: return name[:100 - len(tag)] + tag
            name = name[:90 - len(tag)] + tag
        if self.cfg.get("thread_state_names") is False: return name[:100]
        word = self.state_word(t); working = f"{STATES['WORKING'][0]} {name}"[:100]
        if self.cfg.get("calm_names") and word in ("QUEUED", "WAITING"): word = "WORKING"  # every rename leaves a line nobody can delete, so the name only changes for needs-you and the end
        if self.handoff(t, word) and t.get("name") == working and time.time() - max(t.get("last_end", 0), t.get("owed_at", 0)) < HANDOFF_NAME_WAIT: return working
        return f"{STATES[word][0]} {name}"[:100]
    def title_of(self, t):
        """The name without its state emoji: 'Project: title' once the project is known, the plain title until then and
        for work that never gets one (a general question)."""
        return with_project(t.get("project"), t.get("title"))
    def note_project(self, tid, m, dirty=True):
        """A thread's project is the repo or area of its mail or events, the first one seen. It stays, so the name gains it once."""
        t = self.st["threads"].get(tid)
        if not t or t.get("project"): return
        p = project_of(m)
        if not p: return
        t["project"] = p
        if dirty: self.dirty.add(tid)
    def backfill_projects(self):
        """Once: threads from before names carried the project learn it from their finished mail. Nothing is marked dirty,
        so each open thread is renamed by the regular sweep at the paced rate and an archived one is not opened for it."""
        if self.st.get("projects_backfilled"): return
        for a in AM.load_agents():
            try: files = sorted((AM.agent_dir(AM.resolve_agent(a)) / "done").glob("*.json"))
            except Exception: continue
            for p in files:
                try: m = AM._load(p)
                except (OSError, ValueError): continue
                if isinstance(m, dict): self.note_project(self.resolve(m.get("thread") or m.get("id") or ""), m, dirty=False)
        self.st["projects_backfilled"] = True
    def handoff(self, t, word=None):
        """Queued only because mail of this work waits for an agent after its last run ended."""
        return (word or self.state_word(t)) == "QUEUED" and not t.get("queued") and bool(self.owed(t))
    @staticmethod
    def bare_name(name):
        """A thread name without its leading state emoji."""
        return next((name[len(v[0]) + 1:] for v in STATES.values() if name.startswith(v[0] + " ")), name)
    def own_name(self, t, live):
        """Whether a live name is one the bridge gave, with or without its state emoji. A stripped emoji is not a rename by hand."""
        return self.bare_name(live) in (self.bare_name(t.get("name") or ""), clean_title(t.get("title"))[:100], self.title_of(t)[:100])
    def unlock_names(self):
        """Once per start: a name lock stays only where the live name is one the owner typed. A lock left behind when the state
        emoji was stripped is cleared, so the thread gets its emoji back. Reading a thread does not open an archived one."""
        if self.cfg.get("thread_state_names") is False: return
        if self.lock_check is None: self.lock_check = {tid for tid, t in self.st["threads"].items() if t.get("name_lock") and t.get("dthread")}
        for tid in sorted(self.lock_check):
            t = self.st["threads"].get(tid)
            if not t or not t.get("name_lock") or not t.get("dthread"): self.lock_check.discard(tid); continue
            try: c = api("GET", f"/channels/{t['dthread']}")
            except RateLimited: return  # the rest wait for the next pass
            except DiscordError as e:
                self.lock_check.discard(tid)
                if e.code == UNKNOWN_CHANNEL: self.forget(t["dthread"])
                else: log("name lock not checked", tid, str(e)[:120])
                continue
            self.lock_check.discard(tid)
            if (c.get("thread_metadata") or {}).get("archived"): t["archived"] = True
            if c.get("name") and self.own_name(t, c["name"]): t["name"] = c["name"]; t.pop("name_lock"); log("name lock cleared", tid, repr(c["name"]))
    def open_thread(self, parent, start, name):
        try: return api("POST", f"/channels/{parent}/messages/{start}/threads", {"name": name, "auto_archive_duration": AUTO_ARCHIVE})["id"]
        except DiscordError as e:
            if e.code == THREAD_EXISTS: return str(start)  # a message's thread has that message's id
            raise
    def join_owner(self, cid):
        if self.cfg.get("add_owner_to_threads") is False: return
        try: api("PUT", f"/channels/{cid}/thread-members/{self.owner}")
        except Exception as e: log("could not add owner to thread", str(e)[:100])
    def anchor_text(self, t):
        return f"{STATES[self.shown_word(t)][0]} **{clean_title(t.get('title'))}**" + (f"\n-# from {t['who']}" if t.get("who") else "")
    def ensure_thread(self, tid, t):
        """The Discord thread for this work, created on the owner's message or on a one-line card in the channel. None if
        it cannot be had right now; callers then post in the channel, so nothing is lost for lack of a permission."""
        if t.get("dthread"): return t["dthread"]
        if time.time() < t.get("thread_retry", 0): return None
        parent, name = self.ch["orchestrator"], self.thread_name(t)
        for _ in range(2):
            start = t.get("origin")
            if not start:
                if not t.get("anchor"): t["anchor"] = self.send_to(parent, self.anchor_text(t)); t["anchor_text"] = self.anchor_text(t)
                start = t["anchor"]
            try: cid = self.open_thread(parent, start, name); break
            except DiscordError as e:
                if e.code == UNKNOWN_MESSAGE:  # the message it hung from was deleted
                    t.pop("anchor" if not t.get("origin") else "origin", None); continue
                t["thread_retry"] = time.time() + 300; log("thread create failed", str(e)[:200])
                self.alert("thread-create", f"Could not create a Discord thread, so replies are going in the channel. Check the bot has Create Public Threads, Send Messages in Threads and Manage Threads in #orchestrator.\n-# {str(e)[:300]}")
                return None
        else: t["thread_retry"] = time.time() + 300; return None
        t["dthread"], t["name"] = cid, name; self.st["dthreads"][cid] = tid
        t.pop("thread_retry", None); self.join_owner(cid); self.retire_card(t, cid); log("thread opened", tid, cid)
        self.link_split(tid, t, cid)
        return cid
    def link_split(self, tid, t, cid):
        """Work an agent split off into a thread of its own (send_message new_thread): each thread links the other."""
        try: first = AM.find_anywhere(tid) or {}
        except Exception: return
        src = self.st["threads"].get(self.resolve(first.get("from_thread") or ""))
        if not src or not src.get("dthread") or src.get("dthread") == cid: return
        t["from_thread"] = first["from_thread"]
        try:
            self.send_to(cid, f"-# ↪️ Split off from <#{src['dthread']}>")
            if not src.get("archived"): self.send_to(src["dthread"], f"-# ↪️ Continues separately in <#{cid}>: {clean_title(t.get('title') or '', 80)}")
        except Exception as e: log("split link not posted", str(e)[:100])
    def retire_card(self, t, cid):
        """A status card left in the channel by an older bridge, or while threads were unavailable.
        A failed delete or edit keeps the card tracked; render_status calls this again, backed off, until it works or is gone."""
        if not t.get("card") or t.get("card_moved"): return
        now = time.time()
        if now < t.get("card_retry", 0): return
        try:
            text = t.get("card_text", "")
            if text.startswith("Working on it") or any(text.startswith(v[0]) for v in STATES.values()): self.delete_to(self.ch["orchestrator"], t["card"])  # status only
            else: self.edit_to(self.ch["orchestrator"], t["card"], clip(text, 1900) + f"\n-# Continued in <#{cid}>")
        except DiscordError as e:
            if e.code != UNKNOWN_MESSAGE: self.move_later(t, now, "card"); log("old card not moved, next try in", t["card_wait"], "s", str(e)[:100]); return
        except Exception as e: self.move_later(t, now, "card"); log("old card not moved, next try in", t["card_wait"], "s", type(e).__name__, str(e)[:100]); return
        t["card_moved"] = True; t.pop("card_retry", None); t.pop("card_wait", None)
    def deliver(self, tid, t, fn):
        """fn(channel, reply_to) inside this work's thread, or in the channel when no thread can be had. Used for real
        content, so a thread deleted by hand comes back here. Returns (result, channel)."""
        t.pop("gone", None); self.touch(t)
        for _ in range(2):
            cid = self.ensure_thread(tid, t)
            if not cid: break
            try:
                r = self.guard(cid, lambda: fn(cid, None)); t.pop("archived", None)
                t.pop("gone", None); return r, cid  # a thread found deleted during this call was marked gone again; the new one is live
            except DiscordError as e:
                if e.code == UNKNOWN_CHANNEL: continue
                if e.status not in (400, 403): raise
                log("thread post failed, using the channel", str(e)[:160]); t["thread_retry"] = time.time() + 300; break
        cid = self.ch["orchestrator"]
        return fn(cid, t.get("origin")), cid
    def say(self, tid, t, content, components=None, ping=False, card=None):
        return self.deliver(tid, t, lambda c, r: self.send_to(c, content, reply_to=r, components=components, ping=ping, card=card))
    def sync_name(self, tid, t, now):
        """Thread names follow the state, but only once it has settled and within Discord's 2 renames per 10 minutes.
        Each rename also leaves a line in the thread, so a quick working-then-done never renames twice.
        With state names off nothing is renamed, except that `project_renames` lets a plain name gain its project, once."""
        off = self.cfg.get("thread_state_names") is False
        retitle = bool(t.get("retitle")) and self.state_word(t) in ("DONE", "FAILED", "CUT OFF")  # a new title reaches the name when the work ends, even with state names off
        if t.get("name_lock") or (off and not self.cfg.get("project_renames") and not retitle): return
        if t.get("archived"):
            if t.get("name_late"): self.late_name(t, now)
            return
        want = self.thread_name(t)
        if want == t.get("name"): t.pop("name_want", None); t.pop("name_t", None); t.pop("name_late", None); return
        name = t.get("name") or ""; mark = lambda n: n[:len(n) - len(self.bare_name(n))]
        gain = bool(t.get("project")) and mark(name) == mark(want) and self.bare_name(name) == clean_title(t.get("title"))[:100]  # same state, the plain name only gains its project
        if off and not gain and not retitle: return
        if t.get("name_want") != want: t["name_want"], t["name_t"] = want, now; return
        recent = [x for x in t.get("renames", []) if now - x < RENAME_WINDOW]
        if now - t["name_t"] < RENAME_SETTLE or len(recent) >= RENAMES_MAX: return
        if self.state_word(t) == "WORKING" and len(recent) >= RENAMES_MAX - 1: return  # keep a rename for leaving Working, so a yellow dot cannot outlast the work
        if gain and now - self.project_t < PROJECT_PACE: return
        if gain: self.project_t = now
        try: api("PATCH", f"/channels/{t['dthread']}", {"name": want})
        except RateLimited: t["name_t"] = now; return
        except DiscordError as e:
            if e.code == UNKNOWN_CHANNEL: self.forget(t["dthread"])
            elif e.code == THREAD_ARCHIVED: t["archived"] = True
            else: log("rename failed", str(e)[:120])
            return
        t["name"], t["renames"] = want, recent + [now]; t.pop("name_want", None); t.pop("name_t", None)
        if retitle: t.pop("retitle", None)
    def close_name(self, t, now):
        """The name a thread should be closed with, when it is not the one it has and a rename is allowed right now."""
        if not t or self.cfg.get("thread_state_names") is False or t.get("name_lock"): return None
        want = self.thread_name(t)
        if want == t.get("name"): return None
        t["name_want"] = want; t.setdefault("name_t", now); t["name_late"] = True  # stays pending until the rename is known to have landed
        return want if len([x for x in t.get("renames", []) if now - x < RENAME_WINDOW]) < RENAMES_MAX else None
    def named(self, t, want, now):
        t["name"], t["renames"] = want, [x for x in t.get("renames", []) if now - x < RENAME_WINDOW] + [now]
        for k in ("name_want", "name_t", "name_late"): t.pop(k, None)
    def late_name(self, t, now):
        """A thread closed before its final name could go out. Discord refuses to rename an archived thread, so it is
        opened for the rename and closed again, once the rename limit allows. Only close_thread asks for this."""
        cid, want = t["dthread"], self.thread_name(t)
        if want == t.get("name"): t.pop("name_late", None); t.pop("name_want", None); t.pop("name_t", None); return
        if len([x for x in t.get("renames", []) if now - x < RENAME_WINDOW]) >= RENAMES_MAX: return
        if BLOCKED.get(route("PATCH", f"/channels/{cid}") + " name", 0) > now: return  # still limited: do not open the thread for nothing
        try:
            api("PATCH", f"/channels/{cid}", {"archived": False})
            try: api("PATCH", f"/channels/{cid}", {"name": want}); self.named(t, want, now)
            finally: api("PATCH", f"/channels/{cid}", {"archived": True})
        except RateLimited: return
        except DiscordError as e:
            if e.code == UNKNOWN_CHANNEL: self.forget(cid)
            else: t.pop("name_late", None); log("late rename failed, name left as it is", str(e)[:120])
    def in_sight(self, tid, t, now):
        """A thread the owner needs to see: fresh (see fresh), or one they started, wrote or tapped in that they have not closed
        (config keep_until_closed, default on). Not an ended thread with only Close left (ended): the idle sweep archives it."""
        if self.ended(tid, t): return False
        if self.fresh(tid, t, now): return True
        return bool((t.get("origin") or t.get("owner_at")) and not t.get("closed") and self.cfg.get("keep_until_closed", True))
    def ended(self, tid, t):
        """Done, nothing open or pending, and Close (with Open in Claude, which is no follow-up) is the only button left."""
        return bool(self.state_word(t) == "DONE" and not self.asks(t) and not self.text_asks(t)
                    and not any(self.resolve(q["thread"]) == tid for q in self.st["q"].values() if q.get("thread"))
                    and not (t.get("suggest") or {}).get("items") and t.get("close_msg")
                    and t.get("row_sig") == json.dumps(self.close_row({"runs": t.get("runs") or {}}, True), sort_keys=True) and self.closeable(tid, t))
    def fresh(self, tid, t, now):
        """A question is open in the thread, its work is not Done, or the owner wrote or tapped in it, or its work moved, within
        KEEP_RECENT. kept_at (the keep-visible ping) does not count."""
        if t.get("asking") or any(self.resolve(q["thread"]) == tid for q in self.st["q"].values() if q.get("thread")): return True
        if self.state_word(t) != "DONE": return True
        return now - max(t.get("owner_at", 0), t.get("active_at", t.get("t0", 0))) < KEEP_RECENT
    def maybe_archive(self, tid, t, now):
        if t.get("archived") or ((t.get("origin") or t.get("owner_at")) and self.in_sight(tid, t, now)) or self.state_word(t) != "DONE" or now - t.get("active_at", t["t0"]) < ARCHIVE_AFTER: return  # the owner's threads stay while in sight
        if self.st.get("idle_refused", {}).get(t["dthread"], 0) >= CLOSE_REFUSED_TRIES: return  # the idle sweep gave up on this close
        try: api("PATCH", f"/channels/{t['dthread']}", {"archived": True}); t["archived"] = True
        except RateLimited: pass
        except DiscordError as e:
            if e.code == UNKNOWN_CHANNEL: self.forget(t["dthread"])
            else: log("archive failed", str(e)[:120])

    # ---- one model per piece of work, rendered as a status message in its thread and a card in #progress ----
    def thread(self, tid, title="", mail=None):
        t = self.st["threads"].setdefault(tid, {"title": title[:120], "runs": {}, "t0": time.time(), "answer": "", "who": ""})
        if title and not t.get("title"): t["title"] = title[:120]
        if mail: self.note_project(tid, mail)  # before the Discord thread opens, so its first name already carries the project
        return t
    def resolve(self, tid): return self.st["alias"].get(tid, tid)
    def active(self, t):
        return [r for r in t["runs"].values() if not r.get("end") and time.time() - r["t0"] < RUN_LIMIT]
    def state_word(self, t):
        """Done is claimed only with no mail waiting for an agent in this work (t["owed"], kept by sync_owed once per tick, so
        no inbox is read here). While the inboxes cannot be read, a thread not yet shown as Done keeps a state that is not Done.
        For DONE_GRACE after a run ends the thread stays Working: a done event can arrive a tick before the mail it handed on."""
        word = self.raw_word(t)
        if word != "DONE": return word
        if self.owed(t): return "QUEUED"
        if self.blind and t.get("shown") not in (None, "DONE"): return t["shown"] if t["shown"] in ("WORKING", "QUEUED") else "QUEUED"
        if time.time() - t.get("ended_at", 0) < DONE_GRACE: return "WORKING"  # just ended: mail it handed on may not be here yet
        return word
    def shown_word(self, t):
        """The state a starter card shows: an open question waits on the owner even while other work of the thread runs, as #progress shows."""
        word = self.state_word(t)
        return "NEEDS YOU" if word in ("WORKING", "QUEUED", "WAITING") and self.asks(t) else word
    def owed(self, t):
        """The agents with mail of this work still waiting, as last read. Mail whose own run has since ended is not waiting."""
        return {a: m for a, m in (t.get("owed") or {}).items() if not (t["runs"].get(m) or {}).get("end")}
    def asks(self, t):
        """A question card of this work is really open: the flag alone can outlive its question (st["q"] is trimmed in prune)."""
        return bool(t and t.get("asking")) and any(self.st["threads"].get(q.get("thread")) is t for q in self.st["q"].values())
    def text_asks(self, t):
        """An agent other than the orchestrator asked the owner in its answer's text, not with ask_owner: it waits on the owner until they speak."""
        at = (t or {}).get("text_ask")
        return bool(at) and not self.owed(t) and not (t.get("owner_at", 0) > at)
    def q_count(self, t):
        """Open question cards of this work: a thread can wait on the owner for two different choices at once."""
        return sum(1 for q in self.st["q"].values() if self.st["threads"].get(q.get("thread")) is t) if self.asks(t) else 0
    def cur_runs(self, t):
        """The runs of the current request, oldest first: a follow-up asking for new work in a finished thread starts a new set (t["epoch"])."""
        since = t.get("epoch", 0) - 5  # a run's start comes from its device's clock
        return sorted((r for r in t["runs"].values() if r["t0"] >= since), key=lambda r: r["t0"])
    def needs(self, t):
        """Needs you: a question is really open, no mail of this work waits for another agent (the asker's own mail is the
        turn that asked, acked when its run ends), and the owner has not spoken since it was asked."""
        if self.text_asks(t): return True
        if not self.asks(t): return False
        qs =[q for q in self.st["q"].values() if self.st["threads"].get(q.get("thread")) is t]
        if set(self.owed(t)) - {q.get("from") for q in qs}: return False
        at = max(float(q.get("at") or 0) for q in qs)
        return not (t.get("queued") and t["queued"] > at)
    def raw_word(self, t):
        if self.active(t): return "WORKING"
        if self.needs(t): return "NEEDS YOU"
        if t.get("queued") and (time.time() - t["queued"] < QUEUED_STALE or self.owed(t)): return "QUEUED"  # mail a runner holds is owed and stays Queued
        runs = self.cur_runs(t)
        if runs and not runs[-1].get("end"): return "CUT OFF"  # the newest run went silent past RUN_LIMIT (a live one is active above); an older end-less run is history
        if t.get("waiting") and float(t.get("waiting_at") or 0) >= max([float(r.get("t1") or 0) for r in runs if r.get("end")] or [0]): return "WAITING"  # a waiting note older than the last run end is stale
        ends = [r["end"] for r in runs if r.get("end")]; last = ends[-1] if ends else ""
        if last == "failed" and LIMIT_HIT.search(str(t.get("detail") or "")): last = "paused"  # an older runner reported the usage limit as a failure
        return {"done": "DONE", "failed": "FAILED", "cutoff": "CUT OFF", "paused": "PAUSED"}.get(last, "DONE" if t.get("mailed") else "QUEUED")
    def steps(self, t):
        """Time is shown with Discord's own live timestamps, so a working thread needs no repeated edits."""
        rows = []
        for r in self.cur_runs(t)[-6:]:
            if r.get("end"): state = f"{ {'done': 'done', 'failed': 'failed', 'cutoff': 'cut off'}.get(r['end'], r['end']) } ({clock((r.get('t1') or time.time()) - r['t0'])})"
            elif time.time() - r["t0"] >= RUN_LIMIT: state = "cut off, no result"
            else: state = f"working, started <t:{int(r['t0'])}:R>"
            rows.append(f"`{r['agent']}` on {r['label']}: {state}")
        return rows
    def queue_line(self, t, to=None, mid=None, at=None):
        """Where a queued message stands: a live timer, how many are ahead of it and what is running now."""
        since = f"queued <t:{int(at or t.get('queued') or t.get('t0') or time.time())}:R>"
        to, mid = to or t.get("to") or "orchestrator", mid or t.get("last_mail")
        try:
            with contextlib.redirect_stderr(io.StringIO()): box = [m["id"] for m in AM.list_inbox(to)]  # an unreadable file is logged by open_mail, once
        except Exception: return since
        busy = {ref: r for x in self.st["threads"].values() for ref, r in x["runs"].items() if not r.get("end") and time.time() - r["t0"] < RUN_LIMIT}
        if mid not in box: return f"{since} · picked up, starting"
        ahead = [i for i in box[:box.index(mid)] if i not in busy]
        run = [r for r in busy.values() if r.get("agent") == to]
        now = f" · `{to}` is running {len(run)} now, oldest started <t:{int(min(r['t0'] for r in run))}:R>" if run else ""
        return f"{since} · {'next up' if not ahead else f'{len(ahead)} ahead of it'} for `{to}`{now}"
    def owed_line(self, t):
        """Who the waiting mail is for, and where the first of it stands in that agent's queue."""
        owed = self.owed(t); (to, mid), rest = next(iter(owed.items())), list(owed)[1:]
        if any(Path(k).stem == mid for k in self.bad_mail): return f"queued <t:{int(t.get('owed_at') or time.time())}:R> · the mail file for `{to}` can't be read right now (`{mid}.json`)" + "".join(f" · also waiting for `{a}`" for a in rest)
        line = self.queue_line(t, to, mid, t.get("owed_at"))
        return line + ("" if f"`{to}`" in line else f" for `{to}`") + "".join(f" · also waiting for `{a}`" for a in rest)
    def read_inbox(self, a):
        """One agent's waiting mail, oldest first, and its files that cannot be read: ([message], {path: why}). A quiet
        tick costs the one strict read; only when that trips on a file are the files read one by one, so the rest still count."""
        if not any(x["agent"] == a for x in self.bad_mail.values()):
            try: return AM.list_inbox(a, strict=True), {}
            except AM.MailError: pass
        d = AM.agent_dir(AM.resolve_agent(a)); out, bad = [], {}
        for p in (d / "inbox").glob("*.json"):
            if not AM.ID_RE.match(p.stem) or (d / "done" / p.name).exists(): continue
            try: m = AM._load(p)
            except FileNotFoundError: continue
            except (OSError, ValueError) as e: bad[p] = str(e); continue
            if isinstance(m, dict): out.append(m)
            else: bad[p] = "not a message"
        return sorted(out, key=AM._sort_key), bad
    def bad_thread(self, p, auto):
        """The work an unreadable inbox file belongs to, if that can be told: as it was last read, else what its text still
        shows, else the thread it started. None when it cannot be told or would not count, and the file then holds nothing."""
        mid = p.stem
        if mid in self.mail_seen: return self.mail_seen[mid]
        try: raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError: raw = ""
        if not auto and not re.search(r'"needs_reply"\s*:\s*true', raw): return None
        m = re.search(r'"thread"\s*:\s*"([^"\\]+)"', raw)
        return m[1] if m else mid if self.resolve(mid) in self.st["threads"] else None
    def open_mail(self):
        """Work with mail still waiting for an agent, so delegated work counts as out: {thread: {agent: its first waiting
        message}}. Mail whose own run has ended is not waiting; the runner is acking it. Mail to an automatic agent always
        counts; mail to a non-automatic one only when it needs a reply; a person's mailbox never. One unreadable file
        (half-synced or corrupt) is skipped: it holds its own thread open when that thread can be told, and never any other.
        None if the registry or a whole inbox can't be read, and every caller then treats the thread as still open."""
        if self.open_cache is None:
            ended = {ref for x in self.st["threads"].values() for ref, r in x["runs"].items() if r.get("end")}
            try:
                out, seen, bad = {}, {}, {}
                agents = AM.load_agents()  # raises on a half-written registry; the bridge's own load_agents would read it as no agents
                if not agents: raise AM.MailError("agent registry is missing or empty")
                for a, info in agents.items():
                    if info.get("kind") == "human": continue  # the owner's questions are tracked in st["q"]; sessions is not a mailbox
                    box, broken = self.read_inbox(a)
                    for m in box:
                        # Nothing runs a non-automatic agent, so its mail holds a thread open only when it asks for a reply.
                        self.note_project(self.resolve(m.get("thread") or m["id"]), m)
                        if m["id"] not in ended and (info.get("auto") or m.get("needs_reply")):
                            seen[m["id"]] = m.get("thread") or m["id"]
                            out.setdefault(self.resolve(seen[m["id"]]), {}).setdefault(a, m["id"])
                    for p, why in broken.items():
                        th = None if p.stem in ended else self.bad_thread(p, info.get("auto"))
                        bad[str(p)] = {"agent": a, "thread": th, "why": why}
                        if th: seen[p.stem] = th; out.setdefault(self.resolve(th), {}).setdefault(a, p.stem)
                self.open_cache, self.mail_seen = out, seen; self.note_bad(bad)
                if self.mail_err: log("open mail check works again"); self.mail_err = None
            except Exception as e:
                why = str(e)[:120]
                if why != self.mail_err: log("open mail check failed", why); self.mail_err = why  # on change, not on every tick
                return None
        return self.open_cache
    def note_bad(self, bad):
        """Keep the list of unreadable inbox files, with a log line when one starts failing and when it clears."""
        now = time.time()
        for k in [k for k in self.bad_mail if k not in bad]:
            log("inbox file readable again or gone", k); self.dirty.add(self.resolve(self.bad_mail.pop(k).get("thread") or ""))  # its card says why it waits
        for k, x in bad.items():
            if k not in self.bad_mail:
                self.bad_mail[k] = {"t0": now}; self.dirty.add(self.resolve(x["thread"] or ""))
                log("inbox file unreadable, skipped", k, x["why"][:120], f"-- holds thread {x['thread']}" if x["thread"] else "-- its thread is not known, so it holds nothing")
            self.bad_mail[k].update(x)
    def alert_bad(self, now):
        """One alert for each inbox file that has stayed unreadable for BAD_MAIL_ALERT. A failed send is tried again a minute
        later. While the file stays unreadable the alert is marked as still seen, so the sweep removes it only once the file clears."""
        for k, x in self.bad_mail.items():
            p = Path(k); key = f"mail-file:{x['agent']}/{p.name}"
            if x.get("alerted"): self.st.setdefault("alert_last", {})[key] = now; continue
            if now - x["t0"] < BAD_MAIL_ALERT or now < x.get("alert_retry", 0): continue
            held = self.st["threads"].get(self.resolve(x["thread"])) if x.get("thread") else None
            text = (f"**Unreadable mail file** `{p.name}` in the `{x['agent']}` inbox has not been readable for {int((now - x['t0']) // 60)} minutes, so that message is not counted or run. "
                    + ("Its thread" + (f" <#{held['dthread']}>" if held and held.get("dthread") else "") + " stays Queued until the file is fixed or removed." if x.get("thread") else "Its thread is not known, so no thread is held for it.")
                    + f"\n-# {k}\n-# {x.get('why', '')[:200]}")
            try: self.alert(key, text); x["alerted"] = True
            except Exception as e:
                self.st.get("alerted", {}).pop(key, None); x["alert_retry"] = now + 60; log("unreadable mail file alert not sent, will retry", p.name, str(e)[:120])
    def sync_owed(self, now):
        """Once per tick: note on each thread which agents still have its mail waiting. Unreadable inboxes change nothing,
        so a thread keeps what it had and state_word holds back any new Done until they can be read again."""
        out, was = self.open_mail(), self.blind
        self.blind = out is None
        if out is None: return
        self.alert_bad(now)
        for tid, t in self.st["threads"].items():
            want = out.get(tid)
            if want != t.get("owed"):
                if want: t["owed"] = want; t.setdefault("owed_at", now)
                else: t.pop("owed", None); t.pop("owed_at", None)
                self.dirty.add(tid)
            elif was and t.get("shown") and t["shown"] != self.state_word(t): self.dirty.add(tid)  # held back while the inboxes were unreadable
    def closeable(self, tid, t):
        """Finished with most likely nothing more to do: final answer delivered, no question or reply pending, no agent running or owed mail."""
        if not t.get("dthread") or self.state_word(t) != "DONE" or not t.get("answer"): return False
        if any(q.get("thread") == tid for q in self.st["q"].values()): return False
        last = next((l for l in reversed(t["answer"].splitlines()) if l.strip()), "")
        if last.rstrip(" *_`").endswith("?"): return False  # the agent ended by asking the owner something
        out = self.open_mail()
        return out is not None and tid not in out and not self.unplaced()
    def unplaced(self):
        """An inbox file went unreadable moments ago and its thread cannot be told: it may be this thread's mail, half-synced.
        Close waits that out. A file that stays unreadable stops counting here, so one corrupt file never blocks closing for good."""
        return any(not x.get("thread") and time.time() - x["t0"] < BAD_MAIL_HOLD for x in self.bad_mail.values())
    def close_row(self, t=None, close=True):
        """The buttons under a finished answer: the assistant's next steps for this situation (smart_finish), then Open in
        Claude for a run with a session, and Close when nothing is left open."""
        t = t or {}; rows = []
        sug = (t.get("suggest") or {}).get("items") or []
        if sug: rows.append({"type": 1, "components": [{"type": 2, "style": 1 if n == 0 else 2, "label": clip(x["label"], 40), "custom_id": f"s:{t['suggest']['for']}:{n}"} for n, x in enumerate(sug[:3])]})
        last = self.open_btn(t) + ([{"type": 2, "style": 2, "label": "Close", "custom_id": "c:thread:0"}] if close else [])  # no Accept / Needs fixing / Undo: finished work is done, and the owner says "undo" when they want it
        if last: rows.append({"type": 1, "components": last})
        return rows
    def last_reaction(self, tid): return max([float(r.get("ts", 0)) for r in fleet.ledger_rows(30) if r.get("thread") == tid], default=0.0)
    def feedback(self, i, tid, kind):
        """Accept, Needs fixing or Undo on a result: one ledger row for the job, a vote on the project's playbook, and
        the buttons go away. Only Undo starts an agent, because something has to be put back."""
        t = self.st["threads"].get(tid or "")
        if not t: return self.respond(i, "This thread is not tracked any more.")
        job = fleet.job_summary(tid, self.last_reaction(tid))
        fleet.ledger({"thread": tid, "title": (t.get("title") or "")[:100], "reaction": kind, **job})
        try:
            import playbook
            playbook.vote(job.get("repo") or t.get("project") or "general", kind == "accept")
        except Exception as e: log("playbook vote failed", str(e)[:100])
        t.pop("fb", None); self.dirty.add(tid)
        said = {"accept": "You accepted this.", "fix": "You marked this as needing a fix. Say what to change here.", "undo": "You asked to undo this. The orchestrator is putting the files back."}[kind]
        try: self.respond(i, clip(self.md_of(i["message"]), 1850) + f"\n-# {said}", update=True, components=self.close_row(t))
        except Exception as e: log("feedback reply failed", str(e)[:120])
        if kind == "undo": self.continue_thread(tid, "Undo the last build of this request on the device that built it, then tell me in one sentence what was put back.")
    def suggested(self, i, tid, key, n):
        """A next-step button the assistant put under an answer: the same as the owner typing its words in the thread."""
        t = self.st["threads"].get(tid or ""); sug = (t or {}).get("suggest") or {}
        try: x = sug["items"][int(n)] if sug.get("for") == key else None
        except (KeyError, IndexError, ValueError, TypeError): x = None
        if not t or not x: return self.respond(i, clip(self.md_of(i["message"]), 1900), update=True, components=[])
        t["suggest"] = None; t["row_sig"] = None
        self.respond(i, clip(self.md_of(i["message"]), 1850) + f"\n-# You chose: {x['label']}", update=True, components=[])
        t.pop("close_msg", None); self.continue_thread(tid, x["say"]); log("suggested button", tid, x["label"])
    def answered(self, t):
        """The work finished and its answer reached the owner since the last run began, so the answer alone is the outcome."""
        return self.state_word(t) == "DONE" and bool(t.get("answered"))
    def awaiting(self, t):
        """The last run ended well moments ago and its answer is not here yet, so the card waits instead of showing Done."""
        at = t.get("done_at")
        if not at: return False
        word = self.state_word(t)  # Working with no run going is the short hold after a run ends: the card stays as it is then too
        if (word == "DONE" or (word == "WORKING" and not self.active(t))) and time.time() - at < ANSWER_WAIT: return True
        t.pop("done_at"); return False
    def shows_working(self, text): return (text or "").split("\n", 1)[0].split("  ·  ")[0] == "{} **{}**".format(*STATES["WORKING"])
    def drop_card(self, t):
        """Delete the no-thread channel card. A failure keeps it and flags the thread, so a later sweep tries again."""
        if not t.get("card"): return
        now = time.time()
        if now < t.get("card_retry", 0): t["retry"] = True; return  # backing off: the flag keeps the sweep coming back
        try: self.delete_to(self.ch["orchestrator"], t["card"])
        except DiscordError as e:
            if e.code != UNKNOWN_MESSAGE: t["retry"] = True; self.move_later(t, now, "card"); log("status card not removed, next try in", t["card_wait"], "s", str(e)[:120]); return
        except Exception as e:  # a long rate limit or a network error: the flag must outlive this pass, or no sweep comes back for the card
            t["retry"] = True; self.move_later(t, now, "card"); log("status card not removed, next try in", t["card_wait"], "s", type(e).__name__, str(e)[:120]); return
        for k in ("card", "card_text", "card_retry", "card_wait", "card_edit_retry", "card_edit_wait"): t.pop(k, None)
    def move_later(self, t, now, key="status_move"):
        """A card call that failed waits longer each time: 20 s doubling to MOVE_RETRY_MAX. Kept in the thread's state, so a
        restart does not start over. key "status_move" is the threaded move, "card" the channel card's delete (drop, retire), "card_edit" its edit."""
        t[key + "_wait"] = min(MOVE_RETRY_MAX, 2 * t.get(key + "_wait", 0) or SWEEP)
        t[key + "_retry"] = now + t[key + "_wait"]
    def set_row(self, t, spot, row):
        """Change only a message's buttons. False when the message, or an earlier thread it was in, no longer exists."""
        try:
            rec = self.card_of(spot[1])
            if rec: self.edit_to(spot[0], spot[1], rec["md"], row); return True  # a card is rebuilt with its text, or the text would go
            self.guard(spot[0], lambda: api("PATCH", f"/channels/{spot[0]}/messages/{spot[1]}", {"components": row})); return True
        except DiscordError as e:
            if e.code == UNKNOWN_MESSAGE or (e.code == UNKNOWN_CHANNEL and not t.get("gone")): return False  # gone: the thread in use was just found deleted
            raise
    def place_close(self, t, want, close=True):
        """The buttons ride on the last message of the answer. An archived thread stays as it is: an edit would reopen it.
        While the assistant is still choosing them (smart_wait) nothing is placed, so no button appears and then goes."""
        if t.get("archived") or time.time() < t.get("smart_wait", 0): return
        row = self.close_row(t, close) if want else []
        target, have = (t.get("answer_msg") if row else None), t.get("close_msg")
        sig = json.dumps(row, sort_keys=True)
        if have == target and (not target or t.get("row_sig") == sig): return
        if have and have != target: self.set_row(t, have, []); t.pop("close_msg", None)
        if target:
            if self.set_row(t, target, row): t["close_msg"] = target; t["row_sig"] = sig; self.strip_rows(target[0], keep=target[1])
            else: t.pop("answer_msg", None)
    def strip_rows(self, cid, keep):
        """Only the newest buttons in a thread are live: any older bot message there with buttons loses them, except open
        question cards. Done the moment new buttons are placed, so no stale row waits for the janitor."""
        if cid == self.ch.get("orchestrator"): return  # a fallback answer in the channel: other threads' cards live there
        cards = {m for q in self.st["q"].values() for m in [q.get("msg")] if m} | set(self.st.get("open_msgs", []))  # open buttons stay usable
        try: msgs = api("GET", f"/channels/{cid}/messages?limit=30")
        except Exception as e: return log("old buttons not checked", str(e)[:100])
        for m in msgs:
            rec = self.card_of(m["id"])
            if m["id"] == keep or m["id"] in cards or not (rec["rows"] if rec else m.get("components")) or str(m.get("author", {}).get("id")) != str(self.cfg.get("bot_id")): continue
            try:
                if rec: self.edit_to(cid, m["id"], rec["md"], [])
                else: self.guard(cid, lambda: api("PATCH", f"/channels/{cid}/messages/{m['id']}", {"components": []}))
                log("old buttons removed", cid, m["id"])
            except Exception as e: log("old buttons not removed", str(e)[:100])
    def drop_status(self, t, cid):
        """Delete the status card. It is forgotten only when Discord says it is gone; any other failure keeps it for the next sweep."""
        if not t.get("status"): return
        try: self.delete_to(t.get("status_ch") or cid, t["status"])
        except DiscordError as e:
            if e.code == UNKNOWN_CHANNEL and t.get("gone"): raise
            if e.code not in (UNKNOWN_MESSAGE, UNKNOWN_CHANNEL): log("status card not removed, will retry", str(e)[:120]); return
        for k in ("status", "status_ch", "status_text", "status_close"): t.pop(k, None)
    def close_thread(self, i, tid):
        """The Close button. Archives without locking, so a later message reopens the thread as usual."""
        cid, t = i["channel_id"], self.st["threads"].get(tid or "")
        if not tid: return self.respond(i, "This thread is not tracked any more, so it can't be closed from here.")
        self.respond(i, self.md_of(i["message"]), update=True, components=[])  # answer within Discord's 3 s first; reading the inboxes can take longer
        self.open_cache = None  # the button may be old: look at the inboxes as they are now
        if t and not self.closeable(tid, t):
            self.dirty.add(tid)  # the next render shows the button again only if it still applies
            if self.open_mail() is None or self.unplaced(): return self.followup(i, "Could not check the agent inboxes just now, so this thread stays open. Try again in a minute.")
            return self.followup(i, "Something is still open in this thread, so it stays open.")
        if t: t["status_close"] = False; t.pop("close_msg", None); t["closed"] = True  # the owner closed it: no more keep-visible pings
        try:
            now = time.time(); name = self.close_name(t, now)  # a rename still waiting to settle goes out with the close
            if name:
                try: api("PATCH", f"/channels/{cid}", {"name": name, "archived": True}); self.named(t, name, now)
                except (RateLimited, DiscordError) as e: log("close without the rename, name follows later", tid, str(e)[:120]); name = None
            if not name: api("PATCH", f"/channels/{cid}", {"archived": True})
            if t: t["archived"] = True
        except Exception as e:
            log("close failed", tid, str(e)[:120])
            if t: self.dirty.add(tid)  # brings the button back for another try
            self.send_to(cid, "Could not close this thread. Try again in a minute.")
    def live(self, ref):
        """A running agent's Queue/live/<run>.json (mail-runner LiveTail): doing, trail, says, at, session, cwd. {} when missing or stale."""
        try: d = json.loads((queue_root() / "live" / f"{ref}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError): return {}
        return d if isinstance(d, dict) and time.time() - float(d.get("at") or 0) < 900 else {}
    def session_run(self, t):
        """The newest run of this work with a Claude session (from its live file, or its end event): its ref, or None."""
        return next((ref for ref, r in sorted(t["runs"].items(), key=lambda kv: -kv[1]["t0"]) if r.get("session")), None)
    def open_btn(self, t):
        """Open in Claude for the newest run with a session: on the live card, and on the answer's row until a newer run's answer takes it."""
        ref = self.session_run(t) if t.get("runs") else None
        return [{"type": 2, "style": 2, "label": "Open in Claude", "custom_id": f"o:{ref}:0"}] if ref else []
    def open_row(self, t): return [{"type": 1, "components": b}] if (b := self.open_btn(t)) else []
    def track_line(self, t):
        """Plan ✓ → Build ✓ ×2 → Review ●: the steps this work went through, the running one marked."""
        seen, cur, built = {}, None, False
        for r in self.cur_runs(t):
            a = r.get("agent"); k = "build" if a == "builder" else "review" if a == "reviewer" else "report" if built else "plan"
            if k in ("build", "review"): seen.pop("report", None)  # an orchestrator step that a later build or review followed was a mini review, not the report
            built = built or a in ("builder", "reviewer"); seen[k] = seen.get(k, 0) + 1
            if not r.get("end") and time.time() - r["t0"] < RUN_LIMIT: cur = k
        return " → ".join(f"{name} {'●' if k == cur else '✓'}" + (f" ×{seen[k]}" if seen[k] > 1 else "") for k, name in STAGES if k in seen)
    def status_text(self, t):
        """The live card: state and the steps so far on one line, then for each running agent its device and start (a
        live Discord timestamp), its last 3 steps and its latest words, as last read from its live file (tick, LIVE_EVERY)."""
        word = self.state_word(t); icon, label = STATES[word]; track = self.track_line(t)
        lines = [f"{icon} **{label}**" + (f"  ·  {track}" if track else "")]
        live = sorted(((ref, r) for ref, r in t["runs"].items() if not r.get("end") and time.time() - r["t0"] < RUN_LIMIT), key=lambda kv: kv[1]["t0"])
        for ref, r in live[-2:]:
            lv = r.get("live") or {}; trail = " → ".join(str(x[1]) for x in (lv.get("trail") or [])[-3:] if isinstance(x, list) and len(x) > 1)
            lines.append(f"-# ▸ `{r['agent']}` on {r['label']}, started <t:{int(r['t0'])}:R>: {trail or lv.get('doing') or 'working'}")
            if lv.get("says"): lines.append("> " + clip(" ".join(str(lv["says"]).replace("```", "").split()), 200))  # one quoted line: a fence would swallow the card
        if not live: lines += ["-# " + s for s in self.steps(t)[-1:]]
        if word == "QUEUED":
            if not t.get("queued") and self.owed(t): lines.append("-# " + self.owed_line(t))
            elif not t.get("queued") and self.raw_word(t) == "DONE": lines.append("-# The agent inboxes can't be read right now, so this is not shown as done yet.")
            else: lines.append("-# " + self.queue_line(t))
        elif word == "WAITING": lines.append("-# " + (t.get("waiting") or "")[:300])
        elif word == "PAUSED" and t.get("detail"): lines.append("-# " + t["detail"][:300])
        if t.get("unread"): lines.append(f"-# {t['unread']} of your messages here not read by an agent yet")
        elif word == "NEEDS YOU": lines.append("-# Waiting for your answer below.")
        elif word in ("FAILED", "CUT OFF") and t.get("detail"): lines.append(t["detail"][:500])
        return "\n".join(lines)

    def linked_block(self, lid, room):
        """One linked thread in a conflict card: title, link, then the same lines as its own status."""
        lt = self.st["threads"].get(self.resolve(lid))
        if not lt: return clip(f"**{lid}**\n-# No activity seen here yet.", room)
        out = [f"**{clean_title(lt.get('title') or lid)}**" + (f" · <#{lt['dthread']}>" if lt.get("dthread") else "")]
        for line in self.status_text(lt).splitlines():
            if dlen("\n".join(out + [line])) > room: break
            out.append(line)
        return clip("\n".join(out), room)
    def links_text(self, t):
        ids = t["linked"]; room = max(80, 1860 // len(ids) - 2)
        return f"**Linked threads ({len(ids)})**\n\n" + "\n\n".join(self.linked_block(l, room) for l in ids)
    def render_links(self, tid, t, cid):
        """The conflict card, edited in place. A closed conflict is not reopened just because linked work moved."""
        if not t.get("linked") or t.get("archived"): return
        text = self.links_text(t)
        if t.get("links_text") == text and t.get("links_ch") == cid: return
        if t.get("links_msg") and t.get("links_ch") == cid:
            try: self.edit_to(cid, t["links_msg"], text); t["links_text"] = text; return
            except DiscordError as e:
                if e.code != UNKNOWN_MESSAGE: raise
        t["links_msg"] = self.guard(cid, lambda: self.send_to(cid, text)); t["links_ch"], t["links_text"] = cid, text
    def render(self, tid):
        t = self.st["threads"][tid]
        if self.quiet(t): t["shown"] = self.state_word(t); return  # machine work: no thread, status card or #progress card
        if (t.get("origin") or t.get("mailed")) and not t.get("gone"):
            try: self.render_status(tid, t)
            except DiscordError as e:
                if e.code != UNKNOWN_CHANNEL: raise
                log("status not rendered, channel gone", tid, str(e)[:120])
        self.render_progress(tid, t)
        t["shown"] = self.state_word(t)
    def render_status(self, tid, t):
        now, text, cid = time.time(), self.status_text(t), self.ensure_thread(tid, t)
        t.pop("retry", None)  # set again below only if a card delete fails in this pass
        if not cid:  # no thread available: the same status as a card under the owner's message
            if self.answered(t): self.drop_card(t); return
            if self.awaiting(t):
                if not self.shows_working(t.get("card_text")): self.drop_card(t)  # Queued or Failed is no longer true; Working stays until the answer
                return
            if t.get("card_text") != text and t.get("origin"):
                if t.get("card") and now < t.get("card_edit_retry", 0): t["retry"] = True; return  # a refused edit is tried less and less often
                try: done = self.edit("orchestrator", t["card"], text) if t.get("card") else None
                except Exception as e: done = False; log("status card edit failed", type(e).__name__, str(e)[:120])  # a network error
                if done is None: t.pop("card", None); t["card"] = self.send("orchestrator", text, reply_to=t["origin"]); done = True
                if done: t["card_text"] = text; t.pop("card_edit_retry", None); t.pop("card_edit_wait", None)
                else: t["retry"] = True; self.move_later(t, now, "card_edit")  # the card still shows the old text, so card_text keeps it and a sweep edits again
            return
        self.retire_card(t, cid)  # an old channel card whose removal failed when the thread opened: tried again, backed off
        try: self.render_links(tid, t, cid)
        except Exception as x: log("conflict card failed, status continues", tid, str(x)[:120])  # links_text is unchanged, so the next pass tries the card again
        if t.get("anchor") and t.get("anchor_text") != self.anchor_text(t):
            if self.edit("orchestrator", t["anchor"], self.anchor_text(t)): t["anchor_text"] = self.anchor_text(t)
        if t.get("origin") and t.get("dthread") == t.get("origin"):  # a thread on the owner's own message: its state as a reaction there, since the bot cannot edit it
            new = STATE_REACT.get(self.shown_word(t))
            if new and new != t.get("state_react"):
                if t.get("state_react"): self.react(self.ch["orchestrator"], t["origin"], urllib.parse.quote(t["state_react"]), "DELETE")
                self.react(self.ch["orchestrator"], t["origin"], urllib.parse.quote(new)); t["state_react"] = new  # best effort, not retried
        answered = self.answered(t)  # a finished run shows only its answer; every other state keeps its card
        for k, o in self.st["threads"].items():  # an older piece of work in this same Discord thread: its buttons are stale now
            if k != tid and (o.get("close_msg") or [None])[0] == cid and o.get("t0", 0) <= t.get("t0", 0):
                try: self.set_row(o, o["close_msg"], [])
                except DiscordError as e: log("old buttons not removed", k, str(e)[:100]); continue
                o.pop("close_msg", None)
        self.place_close(t, answered and not any(q.get("thread") == tid for q in self.st["q"].values()), answered and self.closeable(tid, t))
        asking = t.get("answers") and any(q.get("thread") == tid and q.get("msg") for q in self.st["q"].values())  # "Needs you, waiting below" repeats the card
        if answered or asking or (self.awaiting(t) and not self.shows_working(t.get("status_text"))): self.drop_status(t, cid)  # a card left on Queued or Failed is not true during the wait
        elif not self.awaiting(t):  # while the answer is on its way the card stays as it is: no Done card to post and take away again
            # An archived thread with nothing new stays as it is: editing the card would reopen it.
            close = bool(t.get("status_close")) if t.get("archived") and t.get("status_text") == text else self.closeable(tid, t)
            if close and not t.get("status_close"): t["status_move"] = True  # the button belongs right under the final answer
            if t.get("status_move") and t.get("status"):  # keep the status just under the owner's latest message
                if now >= t.get("status_move_retry", 0):  # a delete that keeps failing is tried less and less often
                    gone = True
                    try: self.delete_to(cid, t["status"])
                    except DiscordError as e:
                        if e.code == UNKNOWN_CHANNEL: raise
                        gone = e.code == UNKNOWN_MESSAGE
                        if not gone: self.move_later(t, now); log("status card not moved, next try in", t["status_move_wait"], "s", str(e)[:120])  # the card stays tracked and is edited in place; status_move stays
                    if gone:
                        for k in ("status", "status_text", "status_move", "status_move_retry", "status_move_wait"): t.pop(k, None)
            else:
                for k in ("status_move", "status_move_retry", "status_move_wait"): t.pop(k, None)
            opener = None if t.get("archived") and t.get("status_text") == text else self.session_run(t)  # as close: no edit that would reopen it
            if t.get("status_text") != text or bool(t.get("status_close")) != close or (opener and t.get("status_open") != opener):
                row = self.close_row(t) if close else self.open_row(t)
                if t.get("status") and t.get("status_ch") == cid:
                    try: self.edit_to(cid, t["status"], text, components=row)
                    except DiscordError as e:
                        if e.code != UNKNOWN_MESSAGE: raise
                        t.pop("status")  # deleted by hand
                if not (t.get("status") and t.get("status_ch") == cid):
                    t["status"] = self.guard(cid, lambda: self.send_to(cid, text, components=row, card=True)); t["status_ch"] = cid; t.pop("archived", None)  # posting reopens an archived thread
                t["status_text"], t["status_close"], t["status_open"] = text, close, self.session_run(t)
        self.sync_name(tid, t, now)
        if t.get("dthread"): self.maybe_archive(tid, t, now)
    def render_boards(self, now):
        """#progress as five messages, one per category, each edited in place: needs you, working, queued, trouble, done.
        On with config "progress_boards": true; the per-work cards are then no longer posted and the sweep removes old ones."""
        b = self.st.setdefault("boards", {"ids": [], "text": []})
        if now - b.get("at", 0) < 5: return
        b["at"] = now
        cats = [("NEEDS YOU", "Needs you", []), ("WORKING", "Working", []), ("QUEUED", "Queued and waiting", []), ("FAILED", "Failed or cut off, last 24 h", []), ("DONE", "Done, last 24 h", [])]
        slot = {"NEEDS YOU": 0, "WORKING": 1, "QUEUED": 2, "WAITING": 2, "PAUSED": 2, "FAILED": 3, "CUT OFF": 3, "DONE": 4}
        need_n = 0
        for tid, t in self.st["threads"].items():
            if not (t["runs"] or t.get("waiting") or t.get("asking") or t.get("queued")): continue
            if not (t.get("origin") or t.get("mailed") or any(r.get("agent") in WORK_AGENTS for r in t["runs"].values())): continue  # device notices (memory, caps) are not work
            word = self.state_word(t); i = slot.get(word)
            if self.asks(t) and word in ("WORKING", "QUEUED", "WAITING"): i = 0  # an open question card waits on the owner even while other work of the thread runs, as #overview shows
            last = max([t.get("active_at", t["t0"])] + [r.get("t1", r["t0"]) for r in t["runs"].values()])
            if i is None or (i >= 2 and now - last > 86400): continue
            live = [f"{r['agent']} on {r['label']}" for r in self.active(t)]
            who = ", ".join(live) or ", ".join(sorted({r["label"] for r in t["runs"].values()}))
            ttl = clean_title(t.get('title') or tid, 70)
            nq = self.q_count(t) if i == 0 else 0
            if i == 0: need_n += max(1, nq)
            cats[i][2].append((last, ttl, f"- {ttl}" + (f"  ·  {nq} questions" if nq > 1 else "") + (f"  ·  {who}" if who else "") + f"  ·  <t:{int(last)}:R>" + (f"  ·  <#{t['dthread']}>" if t.get("dthread") else "")))
        texts = []
        for n, (word, label, rows) in enumerate(cats):
            rows.sort(reverse=True)
            if n >= 3:  # finished work: one line per title (the janitor opens a same-titled thread every few hours)
                first, count = {}, {}
                for k, (_, ttl, _l) in enumerate(rows): first.setdefault(ttl, k); count[ttl] = count.get(ttl, 0) + 1
                rows = [(a, ttl, line + (f"  ·  ×{count[ttl]}" if count[ttl] > 1 else "")) for k, (a, ttl, line) in enumerate(rows) if first[ttl] == k]
            shown = [r for _, _t, r in rows[:12]]
            texts.append(f"{STATES[word][0]} **{label}** ({need_n if n == 0 else len(rows)})\n" + ("\n".join(shown) + (f"\n-# and {len(rows) - 12} more" if len(rows) > 12 else "") if rows else "-# nothing"))
        try:  # one line on top: what the agents cost today, so a fast burn is visible without asking
            rows24 = fleet.stats(24); n = len(rows24); mins = sum(float(r.get("secs") or 0) for r in rows24) / 60
            try: budget = float(json.loads((queue_root() / "loop" / "config.json").read_text(encoding="utf-8")).get("daily_budget") or 0)
            except (OSError, ValueError): budget = 0.0
            used = sum(float(r.get("cost") or 0) for r in rows24) / budget if budget else 0.0
            top = {}
            for r in rows24: top[r.get("thread") or ""] = top.get(r.get("thread") or "", 0) + 1
            busy = sorted(top.items(), key=lambda kv: -kv[1])[:3]
            names = ", ".join(f"{clean_title((self.st['threads'].get(t) or {}).get('title') or 'other', 40)} ({k} runs)" for t, k in busy)
            share, wk = fleet.share_text(rows24), fleet.week_used(rows24); now_w = fleet.week_now(); pace = fleet.pace_text(fleet.week_pace())
            used24 = "  ·  " + " · ".join(f"{fleet.pct(v)} of {e.capitalize()}'s week" for e, v in sorted(wk.items())) if wk else ""
            texts.insert(0, f"📊 **Agents, last 24 h**: {n} runs, {mins / 60:.1f} h{used24}" + ("  ·  " + pace if pace else f"  ·  Claude week {now_w[0]:.0f}% used <t:{int(now_w[1])}:R>" if now_w else "") + (f"  ·  day budget {used * 100:.0f}% used" + ("  ·  **loop paused**" if used >= 1 else "") if budget else "") + (f"\n-# by model: {share}" if share else "") + (f"\n-# busiest: {names}" if names else ""))
        except Exception as x: log("ERR usage line", str(x)[:120])
        notes = self.st.get("notices") or {}
        for k in [k for k, x in notes.items() if now - float(x.get("at") or 0) > NOTICE_KEEP]: del notes[k]
        if notes and texts: texts[0] += "\n-# ⚠️ " + clip("; ".join(x["text"] for x in notes.values()), 400)  # device notices, latest of each
        if self.post_cards("progress", b, texts):
            for t in self.st["threads"].values(): t.pop("prog", None)  # old per-work cards are no longer tracked, so the sweep removes them
    def render_progress(self, tid, t):
        if self.cfg.get("progress_boards"): return
        if not t["runs"] and not t.get("waiting"): return
        word = self.state_word(t); steps = self.steps(t); hosts = sorted({r["label"] for r in t["runs"].values()})
        text = f"{STATES[word][0]} **{word}**  {clean_title(t.get('title') or tid)}" + (f"  ·  {', '.join(hosts)}" if hosts else "") + (f"  ·  <#{t['dthread']}>" if t.get("dthread") else "")
        text += ("\n" + "\n".join("-# " + s for s in steps)) if steps else ""
        if t.get("waiting") and word == "WAITING": text += "\n-# " + t["waiting"][:200]
        if word != "WORKING" and (t.get("answer") or t.get("detail")): text += "\n" + (t.get("answer") or t.get("detail"))[:500]
        if t.get("prog_text") == text: return
        if t.get("prog"):
            done = self.edit("progress", t["prog"], text)
            if done is None: t.pop("prog")
            elif not done: return
        if not t.get("prog"): t["prog"] = self.send("progress", text)
        t["prog_text"] = text

    @staticmethod
    def clean_prompt(s):
        s = " ".join(str(s or "").split())
        return "" if s.startswith("<") or TITLE_GEN.match(s) else s  # task notifications and other machine turns are not the owner's words
    def sessions(self, now):
        """One card per interactive Claude or Codex session, from the files the session hooks write, with a thread on
        it. The name comes from the first prompt and is set once, because a session flips between working and idle every
        turn and each rename would leave a line in the thread; the card carries the state."""
        self.name_budget = NAME_BATCH
        cards = self.st.setdefault("sessions", {}); d = queue_root() / "sessions"; seen = set()
        if not d.is_dir(): return  # a missing folder does not mean every session ended
        up = {str(v["host"]).lower() for v in fleet.devices() if v.get("available")} | {hostname().lower()}
        for p in sorted(d.glob("*/*.json")):
            try: x = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError): continue
            key = f"{x.get('host')}/{x.get('session_id')}"; seen.add(key); c = cards.setdefault(key, {}); c.pop("missing", None)
            if c.get("thread") and not c.get("named") and not c.get("where0") and self.card_where(c.get("text")): c["where0"] = self.card_where(c["text"])  # what its thread was opened as, before the card changes
            state = x.get("state", "idle")
            if state == "working" and now - float(x.get("updated", 0)) > 3600: state = "idle"
            offline = state == "working" and str(x.get("host") or "").lower() not in up
            if offline: state = "idle"
            if state == "needs_you" and x.get("note_type") == "idle_prompt": state = "idle"  # finished and waiting for the next prompt
            if c.get("state") != state: c["state"], c["since"] = state, now
            label = next((v["label"] for v in fleet.devices() if v["host"] == x.get("host")), x.get("host"))
            icon, word = STATES.get({"working": "WORKING", "needs_you": "NEEDS YOU", "idle": "IDLE", "ended": "ENDED"}.get(state, ""), ("⚪", state.capitalize()))
            tail = f" · {label} · {x.get('tool', 'claude')}"; where = f"{x.get('project') or '?'}  ·  {label}  ·  {x.get('tool', 'claude')}"; prompt = self.clean_prompt(x.get("last_prompt"))
            note = str(x.get("last_note") or "")[:200] if state == "needs_you" else ""
            text = f"{icon} **{word}**  {self.session_label(x, prompt)}  ·  {label}  ·  {x.get('tool', 'claude')}" + (f"\n> {prompt[:200]}" if prompt else "") + (f"\n-# {note}" if note else "") + (f"\n-# {label} is offline" if offline else "") + f"\n-# since <t:{int(c['since'])}:R>"
            try:
                if not c.get("id"):
                    if state == "ended" or not (prompt or self.clean_prompt(x.get("first_prompt")) or note): continue  # no card until the owner has said something, or it needs them
                    c["id"] = self.send("threads", text); c["text"] = text
                elif c.get("text") != text:
                    done = self.edit("threads", c["id"], text)
                    if done is None:  # card deleted by hand: start over next pass
                        for k in ("id", "thread", "text"): c.pop(k, None)
                        continue
                    if done: c["text"] = text
                self.session_thread(key, c, where, prompt, note, state, now, None if x.get("note_type") == "remote_permission" else x.get("host"), tail)  # a remote permission request has its own card
            except Exception as e: log("ERR session card", str(e)[:100])
        self.flush_rearchive(now)  # after the files are read, so a session that just resumed keeps its thread open
        for key in [k for k in cards if k not in seen]:
            c = cards[key]; c.setdefault("missing", now)
            where = self.card_where(c.get("text"))
            if c.get("thread") and where: self.name_session(c, c["thread"], "", self.where_tail(where), now, where)  # its file is gone, and the sweep skips tracked threads
            if now - c["missing"] < SESSION_GRACE: continue
            if self.close_session(c): cards.pop(key); self.st["dthreads"].pop(c.get("thread"), None)
        self.sweep_session_names(now)
    @staticmethod
    def session_label(x, prompt=""):
        """What a #threads card calls a session: its project, or for a throwaway folder (a scratch workspace, a Codex dated
        folder named from the prompt's first letters) the session's first prompt as a title."""
        p = str(x.get("project") or "").strip()
        if p and not NO_PROJECT.fullmatch(p) and not re.search(r"[/\\]Codex[/\\]\d{4}-\d{2}-\d{2}[/\\][^/\\]+[/\\]?$", str(x.get("cwd") or "")): real = True
        else: real = False
        first = Bridge.clean_prompt(x.get("first_prompt")) or prompt
        if slash_only(first): first = prompt if prompt and not slash_only(prompt) else ""
        if real: return with_project(p, first, 60).replace("  ·  ", " · ") if first else p
        return clean_title(first, 60).replace("  ·  ", " · ") if first else "Session"
    def close_session(self, c):
        """The session's file is gone for good. Its thread is archived, never deleted, so the prompts stay readable, and
        its card says Ended. False means try again next pass."""
        try:
            if c.get("thread") and not c.get("archived"): api("PATCH", f"/channels/{c['thread']}", {"archived": True})
            text = c.get("text") or ""
            if c.get("id") and ("\n> " not in text or TITLE_GEN.search(text)): self.delete_to(self.ch["threads"], c["id"])  # never carried the owner's words: nothing to keep
            elif c.get("id") and text:
                ended = re.sub(r"^\S+ \*\*[^*]+\*\*", f"{STATES['ENDED'][0]} **{STATES['ENDED'][1]}**", c["text"], count=1)
                if ended != c["text"]: self.edit("threads", c["id"], ended)
        except DiscordError as e: log("session cleanup", str(e)[:100])  # deleted by hand or refused; Discord's own timer archives it
        except Exception as e: log("session cleanup, will retry", str(e)[:100]); return False  # rate limit or network
        return True
    @staticmethod
    def session_name(prompt, tail, project=""): return with_project(project, prompt, 100 - dlen(tail), whole=True) + tail  # "project: first prompt · device · tool"
    @staticmethod
    def where_project(where):
        """The project of a "project  ·  device  ·  tool" line, or "" when the session did not say."""
        p = where.rsplit("  ·  ", 2)[0].strip() if (where or "").count("  ·  ") >= 2 else ""
        p = p.split(": ", 1)[0].strip()
        return "" if NO_PROJECT.fullmatch(p) else p
    @staticmethod
    def old_name(where): return re.sub(r"\s+", " ", where)[:100]  # the name a session thread is opened with before it has a prompt
    @staticmethod
    def card_where(text):
        """The "project  ·  device  ·  tool" part of a session card's first line, or "" if the text is not such a card."""
        m = re.match(r"\S+ \*\*[^*]+\*\*  (.+)", (text or "").split("\n")[0])
        return m[1] if m and m[1].count("  ·  ") >= 2 else ""
    @staticmethod
    def where_tail(where): return "".join(" · " + x for x in where.rsplit("  ·  ", 2)[1:])
    def rearchive(self, cid, now):
        """Close a thread the bridge opened for a rename. Never raises: what cannot be done now stays queued in the state, across
        restarts too. A refused close is tried again hourly, a few times, then given up and logged; Discord's own timer archives it."""
        q, refused = self.st.setdefault("rearchive", {}), self.st.setdefault("rearchive_refused", {})
        try: api("PATCH", f"/channels/{cid}", {"archived": True})
        except RateLimited as e: q[cid] = now + max(5, e.wait); return
        except DiscordError as e:
            if e.status >= 500: q[cid] = now + 60; return
            if e.code not in (THREAD_ARCHIVED, UNKNOWN_CHANNEL):
                refused[cid] = refused.get(cid, 0) + 1
                if refused[cid] < CLOSE_REFUSED_TRIES:
                    q[cid] = now + CLOSE_REFUSED_WAIT; log("thread not closed again, will retry in an hour", cid, f"refusal {refused[cid]} of {CLOSE_REFUSED_TRIES}", str(e)[:120]); return
                log("gave up closing thread, left open", cid, f"refused {refused[cid]} times", str(e)[:120])
                if cid not in self.st["dthreads"] and not self.st.get("session_names_swept"): self.st["session_names_stop"] = True  # only the sweep opens untracked threads
        except Exception as e: q[cid] = now + 60; log("thread not closed yet, will retry", cid, str(e)[:120]); return
        q.pop(cid, None); refused.pop(cid, None)
    def keep_open(self, cid, why=""):
        """Forget a close still owed to this thread: its session is in use again."""
        if self.st.get("rearchive", {}).pop(cid, None) is None: return
        self.st.get("rearchive_refused", {}).pop(cid, None); log("queued close dropped", cid, why)
    def session_live(self, cid):
        key = self.st["dthreads"].get(cid) or ""
        c = self.st.get("sessions", {}).get(key[8:]) if key.startswith("session:") else None
        return bool(c) and not c.get("missing") and c.get("state") in ("working", "needs_you")
    def flush_rearchive(self, now):
        for cid, due in list(self.st.get("rearchive", {}).items()):
            if now < due: continue
            if self.session_live(cid): self.keep_open(cid, "session is active again")
            else: self.rearchive(cid, now)
    def rename_thread(self, cid, name):
        """Discord refuses to rename an archived thread, so one is opened for the rename and closed again. If the rename
        fails the error still reaches the caller, which retries, and the thread is closed whatever happened."""
        try: return api("PATCH", f"/channels/{cid}", {"name": name})
        except DiscordError as e:
            if e.code != THREAD_ARCHIVED: raise
        self.st.setdefault("rearchive", {})[cid] = 0  # written first, so no failure below can leave it open and forgotten
        try:
            api("PATCH", f"/channels/{cid}", {"archived": False})
            return api("PATCH", f"/channels/{cid}", {"name": name})
        finally: self.rearchive(cid, time.time())
    def first_mirrored(self, cid):
        """The session's first prompt as it was mirrored into its thread. Discord's order for after= is not relied on."""
        rows = [m for m in api("GET", f"/channels/{cid}/messages?limit=100&after=0") or [] if m.get("author", {}).get("id") == self.cfg.get("bot_id") and m.get("content", "").startswith("> ")]
        return min(rows, key=lambda m: int(m["id"]))["content"][2:].strip() if rows else ""
    def name_session(self, c, cid, prompt, tail, now, where=""):
        """Name a thread from its first prompt, once. This is also the migration for threads opened under the old name.
        Same rule as the sweep: only a name the bridge itself opened the thread with is changed; any other was set by hand and is kept."""
        old = bool(c.get("named")) and not c.get("proj")  # named before names started with the project: gains it once, if still open and project_renames allows it
        if (c.get("named") and not (old and self.cfg.get("project_renames"))) or not (prompt or c.get("prompt")) or slash_only(prompt or c.get("prompt")) or now < c.get("name_retry", 0) or self.name_budget <= 0: return
        self.name_budget -= 1; project = self.where_project(where)
        try:
            ch = api("GET", f"/channels/{cid}"); cur = ch["name"]; slashed = cur.startswith("/") or cur == self.session_name("/", tail, project)  # opened under a slash command's name
            if old:
                if project and not (ch.get("thread_metadata") or {}).get("archived"):  # an archived thread is not opened for this
                    first = self.first_mirrored(cid) or c.get("prompt") or prompt; want = self.session_name(first, tail, project)
                    if cur == self.session_name(first, tail) and want != cur: self.rename_thread(cid, want); log("session thread renamed", cid, repr(cur), "->", repr(want))
            elif cur in {self.old_name(w) for w in (where, c.get("where0")) if w} or slashed:
                first = prompt or c.get("prompt") if slashed else self.first_mirrored(cid) or c.get("prompt") or prompt
                want = self.session_name(first, tail, project)
                if want != cur: self.rename_thread(cid, want); log("session thread renamed", cid, repr(cur), "->", repr(want))
            else: log("session thread name kept", cid, repr(cur))
            c["named"] = c["proj"] = True; c.pop("name_retry", None); c.pop("where0", None)
        except RateLimited: c["name_retry"] = now + 60
        except DiscordError as e:
            c["name_retry"] = now + 300; log("session rename failed", str(e)[:120])
            if e.code == UNKNOWN_CHANNEL: self.forget(cid)
        except Exception as e: c["name_retry"] = now + 60; log("session rename failed, will retry", str(e)[:120])  # network
    def sweep_held(self, now):
        """True when the sweep must open no thread this pass. While Discord refuses to close any thread, the sweep waits for
        that close, so at most the one thread it was on is left open. If Discord never lets a thread the sweep opened be
        closed, the sweep ends there: the remaining old threads keep their names instead of each being reopened and left open."""
        if self.st.get("session_names_stop"):
            log("session name sweep stopped: Discord refuses to close threads, the remaining old threads keep their names")
            for k in ("session_names_stop", "session_names_seen", "session_names_retry"): self.st.pop(k, None)
            self.st["session_names_swept"] = True; return True
        q, refused = self.st.get("rearchive", {}), self.st.get("rearchive_refused", {})
        for cid in [c for c in refused if c not in q]: refused.pop(cid)  # no close is owed any more
        if not refused: return False
        self.st["session_names_retry"] = max(now + 60, min(q[cid] for cid in refused)); return True
    def sweep_session_names(self, now):
        """One-time rename of finished session threads the bridge no longer tracks. Only a name equal to the one the bridge
        opened the thread with is changed, proved against the card the thread hangs off (same id). Marked done only when
        the list was read, every thread was handled and none is still waiting to be closed; any other outcome retries later."""
        if self.st.get("session_names_swept") or now < self.st.get("session_names_retry", 0): return
        if self.sweep_held(now): return
        seen = self.st.setdefault("session_names_seen", [])
        try:
            rows = api("GET", f"/channels/{self.ch['threads']}/threads/archived/public?limit=100")["threads"]
            todo = [x for x in rows if x["id"] not in seen and x["id"] not in self.st["dthreads"]]
            for x in todo[:NAME_BATCH]:
                try:
                    where = self.card_where(api("GET", f"/channels/{self.ch['threads']}/messages/{x['id']}").get("content"))
                    if where and x["name"] == self.old_name(where):
                        first = self.first_mirrored(x["id"])
                        want = self.session_name(first, self.where_tail(where), self.where_project(where)) if first else x["name"]
                        if want != x["name"]: self.rename_thread(x["id"], want); log("old session thread renamed", x["id"], repr(x["name"]), "->", repr(want))
                    else: log("old session thread name kept", x["id"], repr(x["name"]))
                except DiscordError as e:
                    if e.status == 403 or e.status >= 500: raise  # not about this thread: stop and try the same one later
                    log("old session rename failed", x["id"], str(e)[:120])  # gone, or its card was deleted so the name cannot be proved
                seen.append(x["id"])
                if self.st.get("rearchive_refused") or self.st.get("session_names_stop"): break  # Discord refused to close one: open no more
        except RateLimited as e: self.st["session_names_retry"] = now + max(60, e.wait); return
        except DiscordError as e:
            log("session name sweep failed, will retry", str(e)[:120]); self.st["session_names_retry"] = now + (300 if e.status >= 500 else 3600); return
        except Exception as e:  # network; must not reach tick(), which would drop the gateway
            log("session name sweep failed, will retry", type(e).__name__, str(e)[:120]); self.st["session_names_retry"] = now + 300; return
        if self.sweep_held(now): return
        if self.st.get("rearchive"): self.st["session_names_retry"] = max(now + 60, min(self.st["rearchive"].values())); return  # that thread comes back on the list once closed
        if len(todo) <= NAME_BATCH: self.st["session_names_swept"] = True; self.st.pop("session_names_seen", None); self.st.pop("session_names_retry", None)
    def session_thread(self, key, c, name, prompt, note, state, now, host=None, tail=""):
        cid = c.get("thread"); real = prompt if prompt and not slash_only(prompt) else ""  # a slash command names nothing: wait for a real prompt
        if not cid and not c.get("no_thread") and state != "ended" and now >= c.get("retry", 0) and (real or not tail):  # wait for the prompt: a thread opened without it must be renamed, and Discord's rename notice cannot be deleted
            try:
                cid = self.open_thread(self.ch["threads"], c["id"], self.session_name(real, tail, self.where_project(name)) if real and tail else self.old_name(name))
                c["thread"] = cid; c["named"] = bool(real and tail); c["proj"] = True; c.pop("where0", None) if c["named"] else c.update(where0=name); self.st["dthreads"][cid] = "session:" + key; c.pop("retry", None); self.join_owner(cid)
                self.send_to(cid, "-# Read-only mirror of this session. To reach an agent, use #orchestrator.")
            except DiscordError as e:
                c["retry"] = now + 300; log("session thread failed", str(e)[:160]); return
        if not cid: return
        if prompt and c.get("prompt") != prompt:
            c["prompt"] = prompt
            loop = prompt.lstrip().startswith("/loop")  # a /loop re-sends the same text every pass: mirror each text once
            if not (loop and prompt[:1900] in c.get("loops", [])):
                if loop: c["loops"] = (c.get("loops", []) + [prompt[:1900]])[-10:]
                self.guard(cid, lambda: self.send_to(cid, "> " + prompt[:1900])); c.pop("archived", None); self.keep_open(cid, "new prompt in its session")
        if tail: self.name_session(c, cid, prompt, tail, now, name)
        if state != "needs_you": self.clear_notice(f"session:{key}"); c.pop("note", None)  # no longer true, so it no longer shows
        elif note and c.get("note") != note:
            mid = self.notice(f"session:{key}", cid, "**Needs you:** " + note); c["note"] = note; c.pop("archived", None); self.keep_open(cid, "its session needs the owner")
            if host: self.want_shot(f"session:{key}:{note[:60]}", cid, host, mid)
        if state == "ended" and not c.get("archived"): api("PATCH", f"/channels/{cid}", {"archived": True}); c["archived"] = True

    def on_event(self, e):
        if not e.get("thread") and NOTICE_TITLE.search(str(e.get("title") or "")): return self.device_notice(e)
        if e["kind"] == "failed" and LIMIT_HIT.search(str(e.get("detail") or "")): e = {**e, "kind": "paused"}  # an older runner reports the usage limit as a failure
        if e.get("repo"): self.st.get("notices", {}).pop(f"skip:{e['repo']}", None)  # the repo's loop moved again: its skip note is over
        tid = self.resolve(e.get("thread") or e.get("ref") or e["id"]); t = self.thread(tid); ref = e.get("ref") or e["id"]; self.touch(t)
        self.note_project(tid, e)
        if not (t.get("origin") or t.get("mailed") or t.get("split_seen")):  # work an agent split off is shown at once in a thread of its own
            t["split_seen"] = True
            try:
                if (AM.find_anywhere(tid) or {}).get("from_thread"): t["mailed"] = True
            except Exception: pass
        if e["kind"] in ("started", "loop", "done") and not t.get("titled") and not t.get("dthread") and not t.get("origin"): t["title"], t["titled"] = e.get("title", "")[:120], True
        elif not t.get("title"): t["title"] = e.get("title", "")[:120]
        label = next((d["label"] for d in fleet.devices() if d["host"] == e["host"]), e["host"])
        if e["kind"] == "started" and self.cfg.get("notepad"):
            try: self.notes_read(tid, e["ts"], e.get("agent") or "")
            except Exception as x: log("ERR notes read", str(x)[:120])
        if e["kind"] == "started": t["runs"][ref] = {"agent": e.get("agent") or "runner", "label": label, "host": e["host"], "t0": e["ts"]}; t.pop("waiting", None); t.pop("waiting_at", None); t.pop("queued", None); t.pop("answered", None); t.pop("answers", None); t.pop("done_at", None); t.pop("ended_at", None); t.pop("status_move_retry", None); t.pop("status_move_wait", None); t.pop("card_retry", None); t.pop("card_wait", None); t.pop("card_edit_retry", None); t.pop("card_edit_wait", None)  # a new run tries a pending card move at once
        elif e["kind"] in ("done", "failed", "cutoff", "paused"):
            r = t["runs"].setdefault(ref, {"agent": e.get("agent") or "runner", "label": label, "host": e["host"], "t0": e["ts"]})
            r["end"], r["t1"] = e["kind"], e["ts"]; t.pop("queued", None)
            if e.get("session"): r["session"] = e["session"]
            if e["kind"] == "done": t["done_at"] = t["ended_at"] = time.time()  # when the bridge saw it, so a late-syncing event still gets the whole wait
            t["last_end"] = time.time()
            if e["kind"] == "paused": t["detail"] = e.get("detail", "")[:500]  # a usage limit: shown on the card, no alert or ping; a later "started" resumes it
            elif e["kind"] != "done":
                t["detail"] = e.get("detail", "")[:500]
                word, who = 'CUT OFF' if e['kind'] == 'cutoff' else 'FAILED', e.get('agent') or 'runner'
                al = self.st.get("alerts", {}); prev = next((m for m, a in al.items() if a.get("tid") == tid and a.get("text") and time.time() - a["at"] < 900), None)
                if prev:  # one alert per failure burst of a thread: the next failure joins it
                    add = f"\n**{word}** {who} on {label}: {e.get('detail', '')[:300]}"; txt = al[prev]["text"][:1900 - len(add)] + add[:1900]
                    try: self.edit_to(self.ch["alerts"], prev, txt[:1900]); al[prev]["text"] = txt[:1900]
                    except Exception as x: log("alert merge failed", str(x)[:120])
                else:
                    head = clean_title(t.get("title") or e.get("title"), 70) + (f" -- {e['title']}" if e.get("title") and e.get("title") != (t.get("title") or e.get("title")) else "")
                    text = f"**{word}** {head} ({who} on {label})" + (f" <#{t['dthread']}>" if t.get("dthread") else "") + f"\n{e.get('detail', '')[:600]}"
                    mid = self.alert(f"{tid}:{e['kind']}:{e.get('agent')}", text, replace=False, tid=tid)
                    if mid and mid in self.st.get("alerts", {}): self.st["alerts"][mid]["text"] = text
                self.want_shot(f"run:{tid}:{ref}:{e['kind']}", t.get("dthread") or self.ch["alerts"], e["host"])
        elif e["kind"] == "waiting": t["waiting"] = e.get("title", ""); t["waiting_at"] = e["ts"]
        elif e["kind"] == "loop" and not t.get("dthread"): t["title"] = e.get("title", t.get("title"))
        self.dirty.add(tid)
    def device_notice(self, e):
        """Memory pressure and skipped loop passes are about a device, not work: the latest one of each sits on the
        #progress usage line (render_boards), and a recovery removes it. No thread or card is made for them."""
        n, title = self.st.setdefault("notices", {}), str(e.get("title") or "")
        if title.startswith("Loop skipped"): n[f"skip:{e.get('repo') or title}"] = {"text": title[:160], "at": e["ts"]}
        elif "memory recovered" in title: n.pop(f"mem:{e['host']}", None)
        else: n[f"mem:{e['host']}"] = {"text": title[:160], "at": e["ts"]}

    # ---- questions: messages that change or disappear as the situation changes ----
    def q_up(self, q): return [i for i, o in enumerate(q["options"]) if not o.get("device") or fleet.available(o["device"])]
    def q_row(self, key, q):
        """Buttons for the options that can be used right now; an option for an offline device is hidden until it returns."""
        btn = [{"type": 2, "style": 1 if n == 0 else 2, "label": q["options"][i]["label"][:80], "custom_id": f"a:{key}:{i}"} for n, i in enumerate(self.q_up(q))]
        if not btn: return []
        if not self.cfg.get("done_elsewhere_button", True): return [{"type": 1, "components": btn}]
        # The owner settled it somewhere the bridge cannot see (a call, another app): one tap closes the card, nothing is sent to the agents
        return [{"type": 1, "components": btn}, {"type": 1, "components": [{"type": 2, "style": 2, "label": "Done elsewhere", "custom_id": f"x:{key}:-"}]}]
    def q_text(self, q): return f"<@{self.owner}> **{q['from']}** asks:\n{fenced(clip(q.get('tail') or q.get('body') or q.get('subject', ''), 1700))}"
    def q_channel(self, q): return q.get("channel") or self.ch["orchestrator"]
    def q_stale(self, q):
        """True when a 'pinned to an offline device' question no longer applies: the work ran, moved, or its device is back."""
        for o in q["options"]:
            r = (o.get("action") or {}).get("redirect")
            if not r: continue
            try: m = json.loads((queue_root() / "mail" / r["agent"] / "inbox" / f"{r['id']}.json").read_text(encoding="utf-8"))
            except FileNotFoundError: return True
            except (OSError, ValueError): continue  # being rewritten or synced; look again next pass
            if m.get("device") and fleet.available(m["device"]): return True
        return False
    def find_card(self, key, q=None):
        """Message id of a question posted before cards were tracked."""
        try:
            for m in api("GET", f"/channels/{self.q_channel(q or {})}/messages?limit=100"):
                if any(str(c.get("custom_id", "")).startswith(f"a:{key}:") for c in walk_components(m.get("components"))): return m["id"]
        except Exception as e: log("find card failed", str(e)[:120])
    def q_clear(self, q):
        if q.get("thread") in self.st["threads"]:
            t = self.st["threads"][q["thread"]]
            rest = [x for x in self.st["q"].values() if x.get("thread") == q["thread"]]  # callers removed q already; the thread still asks while any other question is open
            if rest: t["asking"] = (max(rest, key=lambda x: float(x.get("at") or 0)).get("subject") or "")[:120]
            else: t.pop("asking", None)
            self.dirty.add(q["thread"])
    def answer_for_owner(self, m, ctl):
        """The orchestrator answers one of the owner's questions with its recommendation. Carried out only while their own
        go-ahead covers that question, and never for a button that deploys, pushes, publishes, spends or deletes."""
        key, g = ctl.get("target"), self.st.get("grant") or {}
        q = self.st["q"].get(key)
        if m.get("from") != "orchestrator" or not q or key not in g.get("qs", []) or time.time() - g.get("at", 0) > GRANT_TTL:
            return log("recommended answer refused, no go-ahead", m.get("from"), key)
        try: opt = q["options"][int(ctl.get("option"))]
        except (TypeError, ValueError, IndexError): return log("recommended answer refused, no such option", key)
        if opt.get("action") or opt.get("device") or RISKY_LABEL.search(opt["label"]): return log("recommended answer refused, risky button", key, opt["label"])
        why = " ".join(str(m.get("body") or "").split())[:300]
        AM.reply("owner", key, f"{opt['label']}\n\n(the owner told the orchestrator to use its recommendation. Its reason: {why})")
        self.st["q"].pop(key); self.q_clear(q); g["qs"].remove(key)
        t = self.st["threads"].get(q.get("thread"))
        if t and (load_agents().get(q.get("from")) or {}).get("auto"): self.touch(t); t["queued"] = time.time(); self.dirty.add(q["thread"])
        if q.get("msg"):
            try: self.edit_to(self.q_channel(q), q["msg"], clip(self.q_text(q), 1700) + f"\n**Answer:** {opt['label']} (recommended; you said go ahead)\n-# {clip(why, 200)}", components=[])
            except Exception as e: log("question card not updated", str(e)[:120])
        self.q_answered(self.q_channel(q), q.get("msg"), *q.get("extra", [])); self.q_done_old(q)
        log("recommended answer", key, opt["label"])
    def drop_q(self, key):
        q = self.st["q"].get(key)
        if not q: return
        cid = self.q_channel(q)
        for mid in [q.get("msg") or self.find_card(key, q)] + q.get("extra", []):  # only the card and its parts hold the question open
            if not mid: continue
            try: self.delete_to(cid, mid)
            except Exception as e:
                if " 404 " not in str(e): log("delete failed, will retry", key, str(e)[:120]); return
        del self.st["q"][key]; self.q_clear(q); self.q_done_old(q)  # an earlier copy that will not delete is parked in st["q_old"], with its backoff
        log("question removed", key)
    def old_gone(self, cid, rec, key=""):
        """Remove the earlier copies of a moved question card whose delete failed. rec["old"] holds the ids still to go;
        a 404 means the copy is gone. Tries back off from 30 s to 10 minutes and the failure is logged once. Never raises."""
        left = []
        for mid in rec.get("old") or []:
            try: self.delete_to(cid, mid)
            except Exception as e:
                if getattr(e, "status", None) == 404: continue
                left.append(mid)
                if not rec.get("old_n"): log("old question copy not removed, will retry", key, str(e)[:120])
        if left: rec["old"] = left; rec["old_n"] = rec.get("old_n", 0) + 1; rec["old_at"] = time.time() + min(30 * 2 ** (rec["old_n"] - 1), 600)
        else:
            for k in ("old", "old_n", "old_at"): rec.pop(k, None)
        return not left
    def q_done_old(self, q):
        """An answered question keeps its card as the record, but an earlier copy that would not delete must still go:
        what is left is parked in st["q_old"] and retried by refresh_questions."""
        if q.get("old") and not self.old_gone(self.q_channel(q), q):
            self.st.setdefault("q_old", []).append({"ch": self.q_channel(q), "old": q["old"], "old_n": q["old_n"], "old_at": q["old_at"]})
    def q_err(self, key, x):
        """A question that keeps failing the same way is logged once per 10 minutes, not on every 10 s pass."""
        seen = self.__dict__.setdefault("q_errs", {}); sig = type(x).__name__ + re.sub(r"\d+", "#", str(x))[:200]; was = seen.get(key)
        for k in [k for k in seen if k not in self.st["q"]]: del seen[k]
        if was and was[0] == sig and time.time() - was[1] < 600: return
        seen[key] = (sig, time.time()); log("ERR question", key, x)
    def refresh_questions(self):
        for rec in list(self.st.get("q_old") or []):  # copies left behind by questions that are answered since
            if time.time() < rec.get("old_at", 0): continue
            if self.old_gone(rec["ch"], rec): self.st["q_old"].remove(rec)
            elif rec.get("old_n", 0) >= 20:
                self.st["q_old"].remove(rec); live = []
                for mid in rec["old"]:  # it stays, so one last try takes its buttons off; never tried again
                    try:
                        if self.card_of(mid): self.edit_to(rec["ch"], mid, self.card_of(mid)["md"], [])
                        else: self.guard(rec["ch"], lambda: api("PATCH", f"/channels/{rec['ch']}/messages/{mid}", {"components": []}))
                    except Exception as e:
                        if getattr(e, "status", None) != 404: live.append(mid)
                log("old question copy given up on", rec["ch"], rec["old"], f"buttons still on {live}" if live else "buttons removed")
        for key, q in list(self.st["q"].items()):
            try:
                if self.q_stale(q): self.drop_q(key); continue
                if q.get("old") and time.time() >= q.get("old_at", 0): self.old_gone(self.q_channel(q), q, key)
                up = self.q_up(q)
                if q.setdefault("shown", up) != up or q.setdefault("row_v", 0) != Q_ROW_V:  # a card posted before the current button layout is edited once
                    q["shown"] = up; q["row_v"] = Q_ROW_V; mid = q.get("msg") or self.find_card(key, q); q["msg"] = mid
                    if mid: self.edit_to(self.q_channel(q), mid, self.q_text(q), components=self.q_row(key, q))
                self.q_bottom(key, q)
            except Exception as x: self.q_err(key, x)
        if time.time() - getattr(self, "last_elsewhere", 0) >= ELSEWHERE_EVERY:
            self.last_elsewhere = time.time()
            try: self.elsewhere()
            except Exception as x: self.q_err("elsewhere", x)
    def elsewhere(self):
        """Close the cards of questions the owner settled outside Discord, so no thread keeps waiting on them for nothing:
        (a) a reply to the question is in the mail (typed at a terminal, or relayed by a session they answered in);
        (b) a permission prompt whose hook has stopped waiting, so its buttons would do nothing;
        (c) an interactive Claude or Codex session asked it, and they have typed in that session since (they answered there).
        The card keeps its text, loses its buttons and says how it was settled. Nothing is closed while mail cannot be read."""
        qs = self.st["q"]
        if not qs: return
        root = queue_root() / "mail"; first = min(qs)[:16]; replies = {}
        try: boxes = [os.path.join(a.path, b) for a in os.scandir(root) if a.is_dir() for b in ("inbox", "done")]
        except OSError: return
        for box in boxes:
            try: names = [n for n in os.listdir(box) if n.endswith(".json") and n[:16] >= first]  # ids start with their UTC second, so older mail is skipped unread
            except FileNotFoundError: continue
            except OSError: return
            for n in names:
                try: m = json.loads(Path(box, n).read_text(encoding="utf-8"))
                except (OSError, ValueError): continue
                r = m.get("reply_to") if isinstance(m, dict) else None
                if r in qs and m.get("from") != qs[r].get("from"): replies.setdefault(r, m)
        agents, now, sessions = load_agents(), time.time(), self.session_files()
        checks = self.st.setdefault("settle_checks", {})
        for k in [k for k in checks if k not in qs]:
            checks.pop(k)
            for f in (dpath("settle") / f"{k}.json", dpath("settle") / f"{k}.out.json"):
                try: f.unlink()
                except OSError: pass
        for key, q in list(qs.items()):
            how = None
            if key in replies:
                m = replies[key]; said = " ".join(str(m.get("body") or "").split())
                how = f"answered{' at a terminal' if m.get('from') == 'owner' else ' through ' + str(m.get('from'))}: {clip(said, 160)}"
            elif q.get("from") == "sessions" and str(q.get("subject") or "").startswith("Permission:"):
                w = re.search(r"Answer within (\d+) minutes", str(q.get("body") or ""))
                if now - float(q.get("at") or now) > (int(w.group(1)) if w else 8) * 60 + PERMISSION_GRACE: how = "the session stopped waiting for this; answer it on the device"
            else:
                info = agents.get(q.get("from")) or {}
                interactive = not info.get("auto") and info.get("kind") in ("claude", "codex")
                if interactive: how = self.typed_since(key, q)
                cands = None if how else self.worked_since(key, q, sessions)
                if cands and interactive: how = self.worked_how(cands[0])  # a session asked the owner about this project and they went on with it in their sessions
                elif cands: how = self.judge_settled(key, q, cands)  # an agent's question: the same project alone is not proof, a small model reads what the session did
            if how: self.settle(key, how)
    def session_files(self):
        """Every session record on every host (Queue/sessions/<host>/<id>.json), read fresh; unreadable ones are skipped."""
        out = []
        for f in (queue_root() / "sessions").glob("*/*.json"):
            try: s = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError): continue
            if isinstance(s, dict): out.append(s)
        return out
    def q_project(self, key, q):
        """The repo or project a question is about: its mail's repo, else its thread's project, else a short 'Project:' title prefix."""
        if q.get("repo") is None or "repo" not in q:
            try: q["repo"] = str(json.loads((queue_root() / "mail" / "owner" / "done" / f"{key}.json").read_text(encoding="utf-8")).get("repo") or "")
            except (OSError, ValueError): q["repo"] = ""
        t = self.st["threads"].get(q.get("thread")) or {}
        if q["repo"] or t.get("project"): return q["repo"] or t["project"]
        head = str(t.get("title") or q.get("subject") or "").split(": ", 1)
        return head[0] if len(head) == 2 and 0 < len(head[0]) <= 30 else ""
    def worked_since(self, key, q, sessions):
        """The owner's own sessions on the question's repo or project (any device) that took a prompt after it was asked and have
        since finished their turn or ended, newest first. Agent runs never write session files, so these are all the owner's."""
        proj = norm_name(self.q_project(key, q))
        if not proj: return []
        at, out = float(q.get("at") or 0) + 5, []
        for s in sessions:
            if norm_name(s.get("project")) != proj or s.get("state") not in ("idle", "ended"): continue
            fresh = max(float(s.get("prompt_at") or 0), float(s.get("started") or 0))
            if fresh > at and float(s.get("updated") or 0) >= fresh: out.append(s)
        return sorted(out, key=lambda s: -float(s.get("updated") or 0))
    def worked_how(self, s, why=""):
        where = f"a {s.get('tool') or 'Claude'} session on the {'Mac' if 'mac' in str(s.get('host') or '').lower() else 'PC'}"
        return f"you finished it in {where}: {clip(why, 200)}" if why else f"you went on with {s.get('project')} in {where} ({clip(s.get('last_prompt') or '', 120)})"
    def judge_settled(self, key, q, cands):
        """The triage model (triage.py, kind settled) reads the question and what those sessions did, and says whether one of them settled
        it. Runs in its own process; its verdict is read on a later pass and kept until the sessions change. None until it says yes."""
        if not self.cfg.get("settle_check", True): return None
        checks, now = self.st.setdefault("settle_checks", {}), time.time()
        sig = "|".join(f"{s.get('session_id')}@{int(float(s.get('updated') or 0))}" for s in cands[:4])
        path = dpath("settle") / f"{key}.json"; out = dpath("settle") / f"{key}.out.json"; c = checks.get(key)
        if c and c.get("sig") == sig:
            if c.get("state") == "running":
                try: r = json.loads(out.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    if now - float(c.get("at") or 0) > 300: c.update(state="failed", retry=now + 1800); log("settle check timed out", key)
                    return None
                except (OSError, ValueError): return None
                for f in (path, out):
                    try: f.unlink()
                    except OSError: pass
                if not isinstance(r.get("done"), bool): c.update(state="failed", retry=now + 1800); log("settle check failed", key, str(r.get("error"))[:120]); return None
                c.update(state="yes" if r["done"] else "no", why=str(r.get("why") or ""), session=str(r.get("session") or ""))
                log("settle check", key, c["state"], clip(c["why"], 120))
            if c.get("state") == "yes":
                return self.worked_how(next((x for x in cands if str(x.get("session_id")) == c.get("session")), cands[0]), c.get("why") or "done there")
            if not (c.get("state") == "failed" and now >= float(c.get("retry") or 0)): return None
        if sum(1 for x in checks.values() if x.get("state") == "running") >= 2: return None  # at most two checks at once
        job = {"kind": "settled", "question": f"{q.get('subject') or ''}\n{q.get('body') or ''}"[:4000], "options": [o.get("label", "") for o in q.get("options") or []],
               "sessions": [{k: x.get(k) for k in ("session_id", "tool", "project", "first_prompt", "last_prompt", "last_reply")} | {"id": x.get("session_id")} for x in cands[:4]]}
        try:
            path.parent.mkdir(parents=True, exist_ok=True); out.unlink(missing_ok=True); path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            self.triage_spawn(path)
        except OSError as e: log("settle check not started", key, str(e)[:120]); return None
        checks[key] = {"sig": sig, "state": "running", "at": now}
        return None
    def typed_since(self, key, q):
        """'you answered in the <project> session' when a session in the folder that asked has had a prompt from the owner since."""
        try: o = json.loads((queue_root() / "mail" / "origins" / f"{key}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError): return None
        folders = {os.path.normcase(os.path.normpath(f)) for f in (o.get("cwd"), o.get("root")) if f}
        try: files = list((queue_root() / "sessions" / str(o.get("host") or "")).glob("*.json"))
        except OSError: return None
        for f in files:
            try: s = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError): continue
            mine = {os.path.normcase(os.path.normpath(x)) for x in (s.get("cwd"), s.get("root")) if x}
            if folders & mine and float(s.get("prompt_at") or 0) > float(q.get("at") or 0) + 5:
                return f"you answered in the {s.get('project') or 'same'} session ({clip(s.get('last_prompt') or '', 120)})"
        return None
    def settle(self, key, how, tapped=None):
        """Close an open question without sending any answer: its card keeps the text and shows HOW, without buttons."""
        q = self.st["q"].pop(key, None)
        if not q: return
        self.q_clear(q); (self.st.get("settle_checks") or {}).pop(key, None)
        if q.get("thread") in self.st["threads"]: self.st["threads"][q["thread"]].pop("turn_answer", None)
        mid = q.get("msg")
        if mid and mid != tapped:
            try: self.edit_to(self.q_channel(q), mid, clip(self.q_text(q), 1700) + f"\n**Settled elsewhere:** {clip(how, 250)}", components=[])
            except Exception as e: log("question card not updated", str(e)[:120])
        self.q_answered(self.q_channel(q), mid, tapped, *q.get("extra", [])); self.q_done_old(q)
        log("question settled elsewhere", key, clip(how, 100))
    def done_elsewhere(self, i, key):
        if key not in self.st["q"]:
            self.q_answered(i["channel_id"], i["message"]["id"])
            return self.respond(i, clip(self.md_of(i["message"]), 1900) + "\n-# Already answered or expired.", update=True, components=[])
        self.settle(key, "you marked it done elsewhere", tapped=i["message"]["id"])
        return self.respond(i, clip(self.md_of(i["message"]), 1800) + "\n**Settled elsewhere:** you marked it done elsewhere", update=True, components=[])
    def q_bottom(self, key, q):
        """A question with buttons stays the last message in its thread. When anything lands below it (a status card,
        an answer part, a notice), it is posted again at the bottom without a second ping and the old copy is removed."""
        cid, mid, now = q.get("channel"), q.get("msg"), time.time()
        if not cid or not mid or q.get("lost") or cid == self.ch["orchestrator"] or now - q.get("moved", 0) < 20: return
        if (self.st["threads"].get(q.get("thread")) or {}).get("archived"): return  # a post would reopen a thread the owner closed
        cards = {x["msg"] for x in self.st["q"].values() if x.get("channel") == cid and x.get("msg")}
        notes = {n[1] for x in self.st["threads"].values() for n in x.get("gemini_notes", [])}
        try: last = api("GET", f"/channels/{cid}/messages?limit={min(len(cards) + 10, 50)}")
        except DiscordError as e:
            if e.status != 404: raise
            q["lost"] = True; log("question thread is gone, its card is no longer moved", key); return  # deleted by hand: asking again every pass finds nothing
        if not last: return
        for m in last:  # several open questions share the bottom; they must not keep jumping over each other
            if m["id"] == mid: return
            if m.get("type", 0) not in (0, 19) or m["id"] in notes or (str(m.get("author", {}).get("id")) == str(self.cfg.get("bot_id")) and str(m.get("content") or "").startswith(("✦", "-# "))): continue  # a notice, a rename or a routing note is no reason to post the card again
            if m["id"] not in cards: break
        if last[0].get("author", {}).get("id") == str(self.owner) and now - ((int(last[0]["id"]) >> 22) + 1420070400000) / 1000 < 15: return  # the owner just typed; their words may be the answer
        q["moved"] = now
        new = self.send_to(cid, self.q_text(q), components=self.q_row(key, q), card=True)
        if not new: return
        q["msg"] = new; q.setdefault("old", []).append(mid)
        self.old_gone(cid, q, key)  # a copy that will not delete stays on the question, for the retry and for drop_q
    def on_question(self, m, tid):
        key, parts = m["id"], split_message(m.get("body", ""), 1700)
        q = self.st["q"].get(key)
        if not q:
            q = {"from": m["from"], "options": m["options"], "subject": m.get("subject", ""), "body": m.get("body", ""), "thread": tid, "tail": parts[-1], "repo": m.get("repo") or ""}
            q["shown"] = self.q_up(q); q["row_v"] = Q_ROW_V; q["at"] = time.time(); self.st["q"][key] = q
        t = self.thread(tid, m.get("subject", ""), m); t["mailed"] = True; t["asking"] = m.get("subject", "")[:120]; t["last_mail"] = key
        if not q.get("msg"):  # one open question per decision: no double tap starts the same work twice
            for ch, old in t.pop("held_cards", []):  # cards put on hold earlier: the new question replaces them
                try: self.delete_to(ch, old)
                except Exception: pass
            self.hold_qs(tid, "Replaced by the newer question below.", keep=key, asker=m["from"])
            for o, x in self.same_repo(tid, t): self.hold_qs(o, "Replaced by the same question in another thread" + (f": <#{t['dthread']}>" if t.get("dthread") else "."), keep=key, asker=m["from"])
            for ch, old in t.pop("held_cards", []):  # the older copy of this same question here goes at once
                try: self.delete_to(ch, old)
                except Exception: pass
        t["who"] = t.get("who") or m["from"]; self.dirty.add(tid)
        for part in parts[len(q.get("extra", [])):-1]:
            mid, q["channel"] = self.say(tid, t, part); q.setdefault("extra", []).append(mid)
        if not q.get("msg"):
            q["msg"], q["channel"] = self.say(tid, t, self.q_text(q), components=self.q_row(key, q), ping=True, card=True)
            self.drop_interim(t); self.drop_gemini_notes(t)
        if m.get("from") == "sessions" and not any(o.get("action") for o in q["options"]): self.want_shot(f"q:{key}", q["channel"], self.blocked_host(t, m), q["msg"])  # action buttons are about an offline device, which has no screen to show

    def drop_interim(self, t):
        """one_answer_per_turn: an interim answer of this turn ("Not sent yet ...") is superseded by the question card that follows it."""
        ta = t.pop("turn_answer", None) if self.cfg.get("one_answer_per_turn") else None
        if not ta: return
        if not re.match(r"^(\*\*\w+\*\*\n)?\W*Not sent yet", ta.get("head", ""), re.I): return  # a real report stays; the popped turn_answer still makes the next answer a new message
        for mid in ta["msgs"]:
            try: self.delete_to(ta["channel"], mid)
            except Exception as e: log("interim answer not removed", str(e)[:100])
        posted = self.st.get("posted", {})
        for a in t.get("answers", []):
            if a in posted: posted[a]["msgs"] = []
        t["answers"] = []
        if (t.get("answer_msg") or [0, 0])[1] in ta["msgs"]: t.pop("answer_msg", None)

    # ---- agent replies ----
    def layout(self, text, who):
        """(messages, whole text as a file or None): a long answer is its opening plus a file, not a wall of posts."""
        head = f"**{who}**\n" if who and who != "orchestrator" else ""
        parts = split_message(head + text)
        if len(parts) <= INLINE_PARTS: return parts, None
        return [parts[0] + "\n-# The rest is in the attached file."], text
    def receipt_line(self, tid, t):
        """A small line under the answer: what this request cost since the owner's last message, and which models did it."""
        if not self.cfg.get("receipt"): return ""
        try:
            r = fleet.receipt(self.resolve(tid), float(t.get("turn_at") or t.get("t0") or 0) - 5)
            return "-# " + r if r and not re.search(r"\b1 runs?\b", r) else ""  # a one-run answer needs no receipt
        except Exception as e: log("receipt failed", str(e)[:120]); return ""
    def strip_attach(self, text):
        """Lines like 'ATTACH: Queue/x/y.png' become files; returns (text, [paths])."""
        keep, files = [], []
        secret, root = TOKEN.resolve().parent, queue_root().parent.resolve()
        for line in (text or "").splitlines():
            if INTERNAL_TAG.match(line): continue  # AREA:, TIER:, CHECKDIR: lines are for the runner, not for the owner
            m = re.match(r"^\s*ATTACH:\s*(.+?)\s*$", line)
            if not m: keep.append(line); continue
            p = Path(m.group(1).strip().strip("`")); p = p if p.is_absolute() else queue_root().parent / p
            try:
                real = p.resolve()
                if real.is_relative_to(secret): keep.append(f"(refused to attach {p.name}: secrets folder)")
                elif not real.is_relative_to(root): keep.append(f"(refused to attach {p.name}: outside the dev root)")
                elif p.is_file() and p.stat().st_size <= 10 * 1024 * 1024: files.append(p)
                else: keep.append(f"(could not attach {p.name}: missing here or over 10 MB)")
            except OSError as e: keep.append(f"(could not attach {p.name}: {str(e)[:80]})")
        return "\n".join(keep), files
    def put_file(self, tid, t, name, data):
        """Upload into the thread; None if Discord refuses this file for good (too large, wrong type)."""
        try: return self.deliver(tid, t, lambda c, r: self.upload_to(c, name, data, reply_to=r))[0]
        except DiscordError as e:
            if e.status >= 500: raise
            log("upload refused", name, str(e)[:120]); return None
    def on_answer(self, m, tid):
        """Safe to repeat: posted[...] records every part delivered, so a failure part-way resumes without duplicates."""
        posted = self.st.setdefault("posted", {}); p = posted.setdefault(m["id"], {"from": m["from"], "thread": tid, "msgs": [], "files": 0})
        if p.get("done"): return
        t = self.thread(tid, m.get("subject", ""), m)
        if self.quiet(t): p["done"] = True; return log("machine thread, answer not posted", tid, clean_title(t.get("title"), 60))
        t["mailed"] = True; t["who"], t["last_mail"] = m["from"], m["id"]
        text, files = self.strip_attach(m.get("body", "")); t["answer"] = text; t.pop("queued", None); self.dirty.add(tid)
        parts, whole = self.layout(text, m["from"])
        if m.get("from") == "orchestrator" and parts:
            foot = self.receipt_line(tid, t)
            if foot:
                if dlen(parts[-1]) + dlen(foot) + 1 <= 2000: parts[-1] = parts[-1] + "\n" + foot
                else: parts.append(foot)
        # Config "one_answer_per_turn": between two of the owner's messages a thread holds one answer, edited in place. A later
        # update from the agents replaces it instead of adding a message; only their next message starts a new one.
        ta = t.get("turn_answer") if self.cfg.get("one_answer_per_turn") else None
        reuse = list(ta["msgs"]) if ta and not p["msgs"] else []
        if reuse and time.time() - ((int(reuse[0]) >> 22) + 1420070400000) / 1000 > 120:  # an edit does not mark the thread unread: a late result is posted anew
            for old in reuse:
                try: self.delete_to(ta["channel"], old)
                except Exception: pass
            reuse = []
        for part in parts[len(p["msgs"]):]:
            mid = None
            if reuse:
                old = reuse.pop(0)
                try: self.edit_to(ta["channel"], old, part); mid, p["channel"] = old, ta["channel"]
                except RateLimited: raise
                except Exception: mid = None  # the old message is gone: post this part
            if mid is None: mid, p["channel"] = self.say(tid, t, part, card=True)
            p["msgs"].append(mid)
        for old in reuse:  # the new answer is shorter: drop the parts left over
            try: self.delete_to(ta["channel"], old)
            except Exception: pass
        if ta and p["msgs"]:  # earlier answers of this turn no longer own those messages
            for a in t.get("answers", []):
                if a != m["id"] and a in posted: posted[a]["msgs"] = []
            t["answers"] = []
        if whole and not p.get("long"): p["long"] = p["tail"] = self.put_file(tid, t, "full-answer.md", whole.encode("utf-8"))
        sent = t.setdefault("files_sent", [])
        if "file_list" not in p:  # chosen once, so a resumed delivery picks up the same list
            fresh = [f for n, f in enumerate(files) if f.name not in [g.name for g in files[:n]]]  # the same file twice in one answer goes once; a file sent again later is sent (they may have asked to see it again)
            new = sorted(fresh, key=lambda f: f.stat().st_mtime if f.exists() else 0, reverse=True)[:MAX_FILES]  # the newest show where the work is now
            p["file_list"] = [f.name for f in fresh if f in new]  # in the answer's order
            if len(fresh) > MAX_FILES:
                try: self.say(tid, t, "-# " + clip(f"{len(fresh) - MAX_FILES} more file(s) not attached, older: " + ", ".join(f.name for f in fresh if f not in new), 500))
                except Exception: pass
        files = [next(f for f in files if f.name == n) for n in p["file_list"] if any(f.name == n for f in files)]
        for f in files[p["files"]:]:
            sent.append(f"{f.name}:{f.stat().st_size}"); del sent[:-60]
            fid = self.put_file(tid, t, f.name, f.read_bytes())
            if fid is None: self.say(tid, t, f"(could not attach {f.name}: Discord refused it)")
            else: p["tail"] = fid  # the buttons go under the last file, not above it
            p["files"] += 1
        if "opens" not in p:
            rows = self.file_rows(t, text, files)
            if rows:
                try: self.st["open_msgs"] = (self.st.get("open_msgs", []) + [self.say(tid, t, "-# Open " + ", ".join(f"`{x}`" for x in rows[1]), components=rows[0])[0]])[-200:]
                except DiscordError as e: log("open buttons not posted", str(e)[:120])
            p["opens"] = True
        p["done"] = True; p["at"] = time.time(); self.drop_gemini_notes(t)
        if self.cfg.get("one_answer_per_turn") and p["msgs"]: t["turn_answer"] = {"channel": p["channel"], "msgs": list(p["msgs"]), "head": parts[0][:80]}
        if self.cfg.get("feedback") and m.get("from") == "orchestrator":
            try:
                if fleet.job_summary(tid, self.last_reaction(tid))["builds"]: t["fb"] = m["id"]  # something was built since their last reaction: ask for one
            except Exception as e: log("feedback check failed", str(e)[:100])
        if text.strip() or files:  # recorded only once all of it is posted, so the status card outlives a part-way failure
            t["answered"] = True; self.dirty.add(tid); p["text"] = text
            if m["from"] != "orchestrator" and (load_agents().get(m["from"]) or {}).get("auto") and asks_owner(text): t["text_ask"] = time.time()
            else: t.pop("text_ask", None)
            t["answers"] = [a for a in t.get("answers", []) if a != m["id"]] + [m["id"]]  # this run's answers still showing
            if p["msgs"] or (p.get("tail") and p.get("channel")): t["answer_msg"] = [p["channel"], p.get("tail") or p["msgs"][-1]]
        if len(posted) > 120: self.st["posted"] = dict(list(posted.items())[-100:])
        if (m.get("from") == "orchestrator" or t.get("text_ask")) and p.get("channel") and self.cfg.get("smart_buttons", True) and (text.strip() or files):
            try: self.smart_start(tid, t, m["id"], text, p["channel"], set(p["msgs"]) | {p.get("tail")})
            except Exception as e: t.pop("smart_wait", None); log("smart buttons not started", str(e)[:120])
    def file_rows(self, t, text, attached):
        """([rows], [names]): Open on Mac / Open on PC for up to 3 local files the answer names (code spans, bare paths,
        and attached .html files, which Discord cannot show). A file under the synced dev root opens on either machine
        and is sent relative to it; one outside opens only on the machine its path form belongs to. Only devices up now."""
        root = dev_root().resolve(); found = []
        cands = [c for c in re.findall(r"`([^`\n]+)`", text) if re.search(FILE_EXT + r"$", c.strip(), re.I)] + FILE_PATH.findall(text)
        cands += [f.resolve().relative_to(root).as_posix() for f in attached if f.suffix.lower() in (".html", ".htm") and f.resolve().is_relative_to(root)]
        for c in cands:
            c = c.strip(); low = c.replace("\\", "/").lower()
            rel = next((c[len(r):] for r in ROOTS.values() if low.startswith(r)), None)
            if rel is None and low.startswith("/users/"): on = ["mac"]
            elif rel is None and re.match(r"[a-z]:/", low): on = ["pc"]
            else:
                c, on = (c if rel is None else rel).replace("\\", "/"), ["mac", "pc"]
                top = c.split("/")[0]
                if not top or ".." in c.split("/") or (rel is None and top != "Queue" and not (dev_root() / top).is_dir()): continue  # not a dev-root path
            if c not in [x["path"] for x in found]: found.append({"name": re.split(r"[/\\]", c)[-1], "path": c, "on": on})
        up = {d["platform"] for d in fleet.devices() if d["available"]}; of = t.setdefault("open_files", {}); rows, names = [], []
        for x in found[:3]:
            on = [s for s in x["on"] if {"mac": "mac", "pc": "windows"}[s] in up]
            if not on: continue
            n = str(max(map(int, of), default=-1) + 1); of[n] = {"name": x["name"], "path": x["path"]}; names.append(x["name"])
            rows.append({"type": 1, "components": [{"type": 2, "style": 2, "custom_id": f"of:{n}:{s}", "label": clip(f"Open {x['name'] + ' ' if len(found) > 1 else ''}on {'Mac' if s == 'mac' else 'PC'}", 80)} for s in on]})
        for k in sorted(of, key=int)[:-30]: del of[k]  # the newest 30 stay tappable
        return (rows, names) if rows else None
    def quiet(self, t):
        """Machine work (janitor critic mail, the improvement loop's cycles on the switchboard itself) that gets no thread or card unless the owner is in it or a question opened one."""
        return bool(QUIET_TITLE.match(re.sub(r"^(\s*re:\s*)+", "", str(t.get("title") or ""), flags=re.I))) and not t.get("dthread") and not (t.get("origin") or t.get("owner_at"))
    def same_repo(self, tid, t):
        """The other threads about the same repo or area: its project tag, else the title's 'Repo:' prefix."""
        key = lambda x: (x.get("project") or (clean_title(x.get("title") or "", 80).split(":")[0] if ":" in str(x.get("title") or "") else "")).strip().lower()
        k, now = key(t), time.time()
        if not k: return []
        return [(o, x) for o, x in self.st["threads"].items() if o != tid and x.get("dthread") and not x.get("gone") and key(x) == k
                and (any(q.get("thread") == o for q in self.st["q"].values()) or self.state_word(x) != "DONE" or now - x.get("active_at", x.get("t0", 0)) < 48 * 3600)]
    def smart_start(self, tid, t, mid, text, cid, mine):
        """The assistant (triage.py, kind buttons) reads the new answer, the thread and the repo's other open threads, and returns
        the buttons for this situation, the older bot messages it makes redundant, and one-line updates for the other threads."""
        prev = {x for a, pp in self.st.get("posted", {}).items() if pp.get("thread") == tid and a != mid for x in pp.get("msgs", [])}
        cards = set(re.findall(r"\d{17,20}", json.dumps(self.st))) - prev  # every id the bridge tracks (status cards, quotes, question cards), but older answers here may go
        try: msgs = api("GET", f"/channels/{cid}/messages?limit=40")
        except Exception: msgs = []
        bot, owner = str(self.cfg.get("bot_id")), str(self.owner)
        asks = lambda m: any(str(c.get("custom_id", "")).startswith("a:") for c in walk_components(m.get("components")))
        earlier = [m for m in msgs if str(m.get("author", {}).get("id")) == bot and m["id"] not in mine and m["id"] not in cards and not m.get("attachments") and not asks(m)]
        ok = {m["id"] for m in earlier}
        def row(m):
            who = "bot" if str(m.get("author", {}).get("id")) == bot else "owner" if str(m.get("author", {}).get("id")) == owner else "other"
            marks = [k for k, v in (("[deletable]", m["id"] in ok), ("[THE NEW ANSWER]", m["id"] in mine), ("[open question card]", asks(m)),
                                    ("[files: " + ", ".join(a.get("filename", "") for a in m.get("attachments") or []) + "]", bool(m.get("attachments")))) if v]
            return f"{m['id']} | {who} | {' '.join(marks) or '-'} | {clip(' '.join((self.md_of(m) or msg_text(m)).split()), 500) or '(no text)'}"
        others = self.same_repo(tid, t)
        def line(o, x):
            q = next((q for q in self.st["q"].values() if q.get("thread") == o), None)
            return " | ".join([o, clean_title(x.get("title") or o, 70), STATES[self.state_word(x)][1], clip(" ".join(str((q or {}).get("body") or "none").split()), 300), clip(" ".join(str(x.get("answer") or "").split()), 300)])
        job = {"kind": "buttons", "answer": text, "thread_text": "\n".join(row(m) for m in reversed(msgs) if m.get("type", 0) in (0, 19)),
               "others": "\n".join(line(o, x) for o, x in others[:10])}
        d = dpath("smart"); d.mkdir(parents=True, exist_ok=True); jid = "s" + mid
        (d / f"{jid}.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8"); self.triage_spawn(d / f"{jid}.json")
        self.st.setdefault("smart", {})[jid] = {"tid": tid, "for": mid, "ch": cid, "at": time.time(), "earlier": [m["id"] for m in earlier], "others": [o for o, _ in others], "answer": text[:3000]}
        t["smart_wait"] = time.time() + SMART_WAIT; t.pop("suggest", None)
    def post_news(self, o, x, line):
        """One news message per thread, edited to add lines, and removed with the routing notes when its next answer posts."""
        have = x.get("news")
        if have and have in [n[1] for n in x.get("gemini_notes", [])]:
            x["news_text"] = clip(x.get("news_text", "") + "\n" + line, 1900)
            try: self.edit_to(x["dthread"], have, x["news_text"]); return
            except Exception: pass
        try: mid = self.send_to(x["dthread"], line, card=True)
        except Exception as e: return log("thread update not posted", o, str(e)[:100])
        x["news"], x["news_text"] = mid, line; self.gemini_note(o, x["dthread"], mid)
    # ---- The owner's chat with the assistant ("argon" in state and config): rules it remembers, fixes it makes, bigger changes it hands to the orchestrator ----
    ARGON_HELLO = ("✦ **Talk to me here about this Discord server.** Tell me how it should look and behave, or point at anything wrong. "
                   "I remember your rules and use them in every thread, fix what I can see right away, and pass bigger changes to the orchestrator as fixes to build.\n"
                   "-# Say \"what do you remember\" to see my rules. I run on the optional `agy` CLI; without it I cannot answer.")
    def ensure_argon(self, now):
        chan = self.cfg.get("argon_channel")  # a channel of its own (#argon) beats a thread, which Discord hides once idle
        if chan and self.st.get("argon_thread") != chan:
            old = self.st.get("argon_thread"); self.st["argon_thread"] = chan
            try:
                self.send_to(chan, self.ARGON_HELLO)
                if old and old != chan: api("DELETE", f"/channels/{old}")  # the earlier thread had only the intro
            except Exception as e: log("argon channel intro failed", str(e)[:120])
            return log("argon chat moved to channel", chan)
        if self.st.get("argon_thread") and not self.st.get("argon_welcome") and not getattr(self, "argon_backfilled", False):
            self.argon_backfilled = True  # once per start: find the welcome post so the janitor keeps it
            try:
                ms = api("GET", f"/channels/{self.st['argon_thread']}/messages?limit=50&after=0") or []
                ms = [m for m in ms if str(m["author"]["id"]) == str(self.cfg.get("bot_id")) and self.md_of(m).startswith("✦")]  # bot posts are cards: their text is in state, not content
                if ms: self.st["argon_welcome"] = min(ms, key=lambda m: int(m["id"]))["id"]
            except Exception as e: log("argon welcome backfill failed", str(e)[:120])
        if self.st.get("argon_thread") or now < getattr(self, "argon_try", 0) or self.cfg.get("argon_chat") is False: return
        self.argon_try = now + 3600
        try:
            th = api("POST", f"/channels/{self.ch['orchestrator']}/threads", {"name": "✦ Talk to the assistant", "type": 11, "auto_archive_duration": 10080})["id"]
            self.st["argon_thread"] = th; self.join_owner(th)
            self.st["argon_welcome"] = self.send_to(th, self.ARGON_HELLO)
            log("argon thread opened", th)
        except Exception as e: log("argon thread not opened", str(e)[:120])
    def server_view(self):
        """The server as the assistant sees it in its chat: active threads, newest first, each with its last messages and ids."""
        bot, owner, skip = str(self.cfg.get("bot_id")), str(self.owner), {self.st.get("argon_thread"), (load("config.json", {}).get("lease_msg") or [None])[0]}
        try: skip.add(load("gallery.json", {}).get("thread"))
        except Exception: pass
        out, index = [], {}
        try: threads = (api("GET", f"/guilds/{self.cfg['guild_id']}/threads/active") or {}).get("threads", [])
        except Exception: threads = []
        threads = [x for x in threads if x["id"] not in skip]
        threads.sort(key=lambda x: int(x.get("last_message_id") or x["id"]), reverse=True)
        for th in [{"id": self.ch["orchestrator"], "name": "#orchestrator (channel)"}] + threads[:14]:
            try: ms = api("GET", f"/channels/{th['id']}/messages?limit=8") or []
            except Exception: continue
            index[th["id"]] = {"thread": True}
            out.append(f"THREAD {th['id']} \"{th.get('name')}\"")
            for m in reversed(ms):
                who = "bot" if str(m["author"]["id"]) == bot else "owner" if str(m["author"]["id"]) == owner else "other"
                btn = [c.get("label") for c in walk_components(m.get("components")) if c.get("type") == 2 and c.get("label")]
                marks = (["buttons: " + ", ".join(btn)] if btn else []) + (["files"] if m.get("attachments") else [])
                index[m["id"]] = {"ch": th["id"], "who": who}
                out.append(f"  {m['id']} | {who} | {'; '.join(marks) or '-'} | {clip(' '.join((self.md_of(m) or msg_text(m)).split()), 160) or '(no text)'}")
        return "\n".join(out)[:20000], index
    def argon_start(self, d):
        cid = d["channel_id"]
        try: rows = api("GET", f"/channels/{cid}/messages?limit=20") or []
        except Exception: rows = []
        hist = "\n".join(f"{'owner' if str(m['author']['id']) == str(self.owner) else 'argon'}: {clip(' '.join((self.md_of(m) or msg_text(m)).split()), 400)}" for m in reversed(rows) if m["id"] != d["id"])
        view, index = self.server_view()
        dd = dpath("argon"); dd.mkdir(parents=True, exist_ok=True); jid = "c" + d["id"]
        files = self.argon_files(d, dd / f"{jid}-files")
        (dd / f"{jid}.json").write_text(json.dumps({"kind": "chat", "text": d.get("content") or "(see the attached file)", "history": hist, "server": view, "files": files}, ensure_ascii=False), encoding="utf-8")
        self.triage_spawn(dd / f"{jid}.json")
        try: ack = self.send_to(cid, "✦ Looking at it…", reply_to=d["id"], card=False)  # at once: the answer replaces it
        except Exception: ack = None
        self.st.setdefault("argon_jobs", {})[jid] = {"at": time.time(), "d": {k: d.get(k) for k in ("id", "channel_id", "content")}, "ack": ack}
        self.__dict__.setdefault("argon_index", {})[jid] = index
        try: api("POST", f"/channels/{cid}/typing")
        except Exception: pass
    def argon_files(self, d, folder):
        """Up to 4 images or videos from the owner's message, downloaded for the model to look at (Discord's 500 MB at most;
        triage.py shrinks anything over 15 MB before the model sees it)."""
        out = []
        for a in (d.get("attachments") or [])[:4]:
            if int(a.get("size") or 0) > 500 * 1024 * 1024: log("argon attachment over 500 MB skipped", a.get("filename")); continue
            name = re.sub(r"[^\w.-]+", "_", str(a.get("filename") or "file"))[:80]
            try:
                folder.mkdir(parents=True, exist_ok=True); dst = folder / name
                with urllib.request.urlopen(urllib.request.Request(a["url"], headers={"User-Agent": "DiscordBot (https://localhost, 2.0)"}), timeout=300) as r, open(dst, "wb") as f:
                    import shutil; shutil.copyfileobj(r, f, 1 << 20)  # streamed: a big video never sits in memory
                out.append(str(dst))
            except Exception as e: log("argon attachment not downloaded", name, str(e)[:100])
        return out
    def argon_poll(self, now):
        for jid, j in list(self.st.get("argon_jobs", {}).items()):
            out, res = dpath("argon") / f"{jid}.out.json", None
            if out.exists():
                try: res = json.loads(out.read_text(encoding="utf-8"))
                except (OSError, ValueError): res = {}
            elif now - j["at"] < CHAT_WAIT: continue
            del self.st["argon_jobs"][jid]
            for f in (out, dpath("argon") / f"{jid}.json"): f.unlink(missing_ok=True)
            import shutil; shutil.rmtree(dpath("argon") / f"{jid}-files", ignore_errors=True)
            try: self.argon_finish(j, res or {}, self.__dict__.get("argon_index", {}).pop(jid, {}))
            except Exception as x: log("ERR argon chat", jid, str(x)[:160])
    def argon_finish(self, j, res, index):
        d = j["d"]; cid = d["channel_id"]
        def say(text):  # the "Looking at it…" message becomes the answer
            if j.get("ack"):
                try: return self.edit_to(cid, j["ack"], text, card=False)
                except Exception: pass
            return self.send_to(cid, text, reply_to=d["id"])
        if res.get("error") or not res.get("reply"):
            log("argon chat failed", d["id"], res.get("error") or "no result in time", *(res.get("fails") or []))
            if res.get("error") == "agy not installed": return say("✦ I cannot answer: the optional `agy` CLI that runs me is not installed on the machine running the bridge.")
            return say("✦ I could not work that out in time. Say it again and I will retry.\n-# " + clip("; ".join(res.get("fails") or [res.get("error") or f"no answer within {CHAT_WAIT} s"]), 300))
        mem = load("argon-memory.json", [])
        gone = {n - 1 for n in res.get("forget") or [] if 0 < n <= len(mem)}
        mem = [r for n, r in enumerate(mem) if n not in gone] + [{"rule": clip(r, 300), "at": time.time()} for r in res.get("remember") or []]
        save("argon-memory.json", mem[-80:])
        done = [x for x in (self.argon_act(a, index) for a in res.get("actions") or []) if x]
        esc = res.get("escalate") or ""
        if esc:
            try: AM.send("owner", "orchestrator", word_cut("Discord bridge: " + esc.splitlines()[0], 80),
                         f"{esc}\n\n(the owner told the Discord assistant this in its chat: {d.get('content')})\n\nAREA: discord-bridge", needs_reply=True)
            except Exception as e: esc = ""; log("argon escalation not sent", str(e)[:120])
        lines = ["✦ " + res["reply"]]
        if res.get("remember"): lines.append("-# Remembered: " + clip("; ".join(res["remember"]), 300))
        if gone: lines.append(f"-# Forgot {len(gone)} old rule(s)")
        if done: lines.append("-# Done: " + clip("; ".join(done), 400))
        if esc: lines.append("-# Sent to the orchestrator as a fix to build: " + clip(esc, 200))
        if res.get("media"): lines.append("-# " + clip("; ".join(res["media"]), 300))
        say("\n".join(lines))
        log("argon chat", d["id"], len(res.get("remember") or []), "rules,", len(done), "actions,", "escalated" if esc else "")
    def argon_act(self, a, index):
        """One change the assistant makes in the server. Only bot messages are deleted or edited; ids must come from what it was shown."""
        op, target, text = a.get("op"), str(a.get("target")), str(a.get("text") or "")
        info = index.get(target)
        if not info: return None
        try:
            if op in ("delete", "unbutton", "edit"):
                if info.get("thread") or info.get("who") != "bot": return None
                ch = info["ch"]; key = self.q_by_message(target)
                if op == "delete":
                    if key: self.drop_q(key)
                    else: self.delete_to(ch, target)
                    for pp in self.st.get("posted", {}).values():
                        if target in pp.get("msgs", []): pp["msgs"] = [x for x in pp["msgs"] if x != target]
                    return f"deleted {target}"
                if op == "unbutton":
                    rec = self.card_of(target)
                    if rec: self.edit_to(ch, target, rec["md"], [])
                    else: self.guard(ch, lambda: api("PATCH", f"/channels/{ch}/messages/{target}", {"components": []}))
                    return f"buttons off {target}"
                if text.strip(): self.edit_to(ch, target, text); return f"edited {target}"
                return None
            if not info.get("thread") or target == self.ch.get("orchestrator"): return None
            tid = self.st["dthreads"].get(target); t = self.st["threads"].get(tid) if tid else None
            if op == "rename" and text.strip():
                api("PATCH", f"/channels/{target}", {"name": clip(text.strip(), 95)})
                if t: t["name"] = clip(text.strip(), 95); t["name_lock"] = True  # the owner's choice: state renames leave it alone
                return f"renamed <#{target}>"
            if op == "archive":
                api("PATCH", f"/channels/{target}", {"archived": True})
                if t: t["archived"] = True; t["closed"] = True; t["status_close"] = False
                return f"closed <#{target}>"
            if op == "refresh" and tid:
                self.st.setdefault("refresh", []).append(tid); return f"re-tidying <#{target}>"
        except Exception as e: log("argon action failed", op, target, str(e)[:120])
        return None
    def card_sweep(self, now):
        """After a refresh: every bot message with text in the server's channels and active threads becomes a card, edited
        in place. One channel per pass; Discord's edit limit pauses it and it carries on next pass."""
        q = self.st.get("card_sweep")
        if not q or not self.cfg.get("cards", True) or now < getattr(self, "sweep_wait", 0): return
        cid = q[0]; bot = str(self.cfg.get("bot_id"))
        if cid == (load("config.json", {}).get("lease_msg") or [None])[0]: q.pop(0); return  # the lock message must stay plain text
        try: ms = api("GET", f"/channels/{cid}/messages?limit=50") or []
        except RateLimited as e: self.sweep_wait = now + 5; return
        except Exception: q.pop(0); return
        skip = set(self.st.setdefault("not_cards", []))
        for m in ms:
            if str(m.get("author", {}).get("id")) != bot or m.get("type", 0) not in (0, 19) or (m.get("flags") or 0) & 32768 or m["id"] in skip: continue
            if not (m.get("content") or "").strip() or self.card_of(m["id"]): continue
            try: self.edit_to(cid, m["id"], "\n".join(l for l in m["content"].splitlines() if not INTERNAL_TAG.match(l)), m.get("components") or [], card=True)
            except RateLimited: self.sweep_wait = now + 5; return
            except Exception as e: self.st["not_cards"] = (self.st["not_cards"] + [m["id"]])[-500:]; log("not turned into a card", m["id"], str(e)[:100])
        q.pop(0); log("cards: channel done", cid, len(q), "left")
    def refresh_step(self, now):
        """discord-bridge.py refresh: the assistant goes over every open thread's latest answer once, one thread at a time, as if
        it had just been posted: fresh buttons, redundant messages removed, sibling threads told."""
        go, seen = False, self.st.setdefault("refresh_seen", {})  # one request file per host (one writer per synced file): {host: the newest "at" acted on}
        for host, f in fleet.host_files(dpath("refresh.json")):
            try: at = float(json.loads(f.read_text(encoding="utf-8")).get("at") or 0)
            except (OSError, ValueError, TypeError, AttributeError): at = 0.0
            if at > seen.get(host, 0): seen[host] = at; go = True
            if host.lower() == hostname().lower(): f.unlink(missing_ok=True)  # only its own copy
        if go:
            self.st["refresh"] = [k for k, t in self.st["threads"].items() if t.get("dthread") and not t.get("gone") and not t.get("archived")
                                  and t.get("answers") and now - t.get("active_at", t.get("t0", 0)) < 72 * 3600]
            log("refresh queued", len(self.st["refresh"]), "threads")
            try:
                gal = load("gallery.json", {}).get("thread")
                act = [x["id"] for x in (api("GET", f"/guilds/{self.cfg['guild_id']}/threads/active") or {}).get("threads", []) if x["id"] != gal]
                self.st["card_sweep"] = [c for c in self.ch.values() if c] + act
            except Exception as e: log("card sweep not queued", str(e)[:100])
        q = self.st.get("refresh")
        if not q or self.st.get("smart") or now - getattr(self, "last_refresh", 0) < 15: return
        self.last_refresh = now; tid = q.pop(0); t = self.st["threads"].get(tid)
        if not t or not t.get("answers"): return
        if not self.cardify(tid, t):  # Discord's edit limit: this thread comes round again once it clears
            q.append(tid); return
        aid = t["answers"][-1]; p = self.st.get("posted", {}).get(aid) or {}
        ch = (t.get("answer_msg") or [p.get("channel")])[0] or t["dthread"]
        self.smart_start(tid, t, aid, t.get("answer") or p.get("text") or "", ch, set(p.get("msgs", [])) | {p.get("tail")})
        log("refresh", tid, len(q), "left")
    def cardify(self, tid, t):
        """The thread's answers, status card and open question cards from before cards existed become cards, edited in place."""
        if not self.cfg.get("cards", True): return True
        spots = [(pp.get("channel") or t.get("dthread"), x) for pp in self.st.get("posted", {}).values() if pp.get("thread") == tid for x in pp.get("msgs", [])]
        if t.get("status"): spots.append((t.get("status_ch") or t.get("dthread"), t["status"]))
        spots += [(self.q_channel(q), q["msg"]) for q in self.st["q"].values() if q.get("thread") == tid and q.get("msg")]
        clean = lambda md: "\n".join(l for l in md.splitlines() if not INTERNAL_TAG.match(l))  # routing tags an older bridge let through
        for cid, mid in spots:
            rec = self.card_of(mid)
            if rec and clean(rec["md"]) != rec["md"]:
                try: self.edit_to(cid, mid, clean(rec["md"]))
                except RateLimited: return False
                except Exception as e: log("card not cleaned", mid, str(e)[:100])
                continue
            if not cid or not mid or rec or mid in self.st.setdefault("not_cards", []): continue
            try:
                m = api("GET", f"/channels/{cid}/messages/{mid}")
                if (m.get("flags") or 0) & 32768 or not (m.get("content") or "").strip(): self.st["not_cards"] = (self.st["not_cards"] + [mid])[-500:]; continue  # a card already, or a file without text
                self.edit_to(cid, mid, clean(m["content"]), m.get("components") or [], card=True)
            except RateLimited: return False
            except Exception as e:
                log("not turned into a card", mid, str(e)[:100]); self.st["not_cards"] = (self.st["not_cards"] + [mid])[-500:]
        return True
    def smart_poll(self, now):
        for jid, j in list(self.st.get("smart", {}).items()):
            out, res = dpath("smart") / f"{jid}.out.json", None
            if out.exists():
                try: res = json.loads(out.read_text(encoding="utf-8"))
                except (OSError, ValueError): res = {}
            elif now - j["at"] < SMART_WAIT: continue
            del self.st["smart"][jid]
            for f in (out, dpath("smart") / f"{jid}.json"): f.unlink(missing_ok=True)
            try: self.smart_finish(j, res or {})
            except Exception as x: log("ERR smart buttons", jid, str(x)[:160])
    def smart_finish(self, j, res):
        t = self.st["threads"].get(j["tid"])
        if not t: return
        if t.get("answers") and t["answers"][-1] != j["for"]: return  # a newer answer has its own job and its own wait
        t.pop("smart_wait", None); self.dirty.add(j["tid"])
        if res.get("error") or "buttons" not in res: return log("smart buttons: the assistant gave nothing, default row", j["tid"], res.get("error", ""))
        t["suggest"] = {"for": j["for"], "items": res["buttons"]} if res["buttons"] else None
        for mid in [x for x in res.get("obsolete") or [] if x in j["earlier"]][:5]:  # only messages it was shown: never a card, a file or the owner's words
            try:
                self.delete_to(j["ch"], mid); log("redundant message removed", j["ch"], mid)
                for pp in self.st.get("posted", {}).values():
                    if mid in pp.get("msgs", []): pp["msgs"] = [x for x in pp["msgs"] if x != mid]
            except Exception as e: log("redundant message not removed", str(e)[:100])
        done = DONE_WORDS.search(j.get("answer") or "")
        for u in res.get("updates") or []:
            o = self.resolve(u["thread"]); x = self.st["threads"].get(o)
            if o not in j["others"] or not x: continue
            note = clip(" ".join(str(u["note"]).split()), 300); h = hashlib.sha1(note.lower().encode()).hexdigest()[:12]
            if h in x.get("news_seen", []): continue
            x["news_seen"] = (x.get("news_seen", []) + [h])[-20:]
            with open(self.sidechat_path(o), "a", encoding="utf-8") as f:  # its orchestrator reads this next run
                f.write(json.dumps({"at": time.time(), "q": f"(update from another thread, {j['tid']})", "a": note}, ensure_ascii=False) + "\n")
            dropped = self.hold_qs(o, f"No longer needed: {note}") if u.get("drop_question") and done else []
            if dropped:
                with open(self.sidechat_path(o), "a", encoding="utf-8") as f:
                    f.write(json.dumps({"at": time.time(), "q": "(question withdrawn by the bridge)", "a": "; ".join(f"{k}: {clip(q.get('subject') or '', 100)}" for k, q in dropped) + f". Reason: {note}"}, ensure_ascii=False) + "\n")
            if x.get("archived") and not dropped: continue  # a closed thread is not reopened just for news
            self.post_news(o, x, f"✦ {note}\n-# from <#{j['ch']}>")
            log("thread updated from another", o, "<-", j["tid"])
        log("smart buttons", j["tid"], [b["label"] for b in res["buttons"]], "removed", len(res.get("obsolete") or []), res.get("model"), res.get("secs"), *(["failed first:"] + res["fails"] if res.get("fails") else []))
    def amend(self, m, ctl):
        """An agent changes or withdraws something it told the owner. Only the original sender can."""
        if ctl.get("op") == "answer": return self.answer_for_owner(m, ctl)
        if ctl.get("op") == "go_ahead":  # the owner said it in a Claude or Codex session they are typing in: those agents never run unattended
            info = load_agents().get(m.get("from")) or {}
            if info.get("auto") or info.get("kind") not in ("claude", "codex"): return log("go-ahead refused", m.get("from"))
            return self.recommend_go({"id": None, "channel_id": self.ch["orchestrator"], "content": m.get("body") or "Do your recommendations"}, None)
        target, text = ctl.get("target"), m.get("body") if ctl.get("op") == "update" else None
        if text is not None: text = strip_linked(text)[0]
        q = self.st["q"].get(target)
        if q and q["from"] == m["from"]:
            if text is None: return self.drop_q(target)
            for mid in q.pop("extra", []):
                try: self.delete_to(self.q_channel(q), mid)
                except Exception as e: log("delete failed", str(e)[:120])
            q["body"] = q["tail"] = text; mid = q.get("msg") or self.find_card(target, q); q["msg"] = mid
            if mid: self.edit_to(self.q_channel(q), mid, self.q_text(q), components=self.q_row(target, q))
            return
        posted = self.st.setdefault("posted", {}); p = posted.get(target)
        if not p or p.get("from") != m["from"]: return log("amend ignored", m["from"], target)
        t = self.st["threads"].get(p.get("thread") or "")
        if "msgs" not in p:  # posted by an older bridge as a card in the channel
            if p.get("thread"):
                if t: t["answer"] = text or "-# Withdrawn."; t["card_text"] = t["answer"]; self.edit("orchestrator", t.get("card"), clip(t["answer"])) if t.get("card") else None
            elif text is not None: self.edit("orchestrator", p["msg"], f"**{m['from']}**: {text[:1800]}")
            else:
                try: api("DELETE", f"/channels/{self.ch['orchestrator']}/messages/{p['msg']}")
                except Exception as e: log("delete failed", str(e)[:120])
                posted.pop(target, None)
            return
        cid = p.get("channel") or self.ch["orchestrator"]
        if p.get("long"):
            try: self.delete_to(cid, p.pop("long"))
            except Exception as e: log("delete failed", str(e)[:120])
        if text is None:
            for mid in p["msgs"]:
                try: self.delete_to(cid, mid)
                except Exception as e: log("delete failed", str(e)[:120])
            posted.pop(target, None)
            if t:
                self.dirty.add(p["thread"]); left = [a for a in t.get("answers", []) if a != target and a in posted]
                if left:  # another answer of this run is still showing: it stays the outcome and keeps the Close button
                    shown = next((posted[a] for a in reversed(left) if posted[a].get("msgs")), None)
                    t["answers"], t["answer"] = left, posted[left[-1]].get("text", t.get("answer", ""))
                    if shown: t["answer_msg"] = [shown.get("channel") or cid, shown["msgs"][-1]]
                    else: t.pop("answer_msg", None)
                else:
                    t["answer"] = ""
                    for k in ("answered", "answers", "answer_msg"): t.pop(k, None)  # nothing left to show, so the status card comes back
            return
        clean, files = self.strip_attach(text); parts, whole = self.layout(clean, m["from"]); before = list(p["msgs"])
        for i, part in enumerate(parts):
            if i < len(p["msgs"]): self.edit_to(cid, p["msgs"][i], part)
            elif t: p["msgs"].append(self.say(p["thread"], t, part)[0])
        for mid in p["msgs"][len(parts):]:
            try: self.delete_to(cid, mid)
            except Exception as e: log("delete failed", str(e)[:120])
        del p["msgs"][len(parts):]
        if t:
            if whole: p["long"] = self.put_file(p["thread"], t, "full-answer.md", whole.encode("utf-8")) or p.get("long"); p["tail"] = p["long"] or p.get("tail")
            for f in files: p["tail"] = self.put_file(p["thread"], t, f.name, f.read_bytes()) or p.get("tail")
            p["text"] = clean; self.dirty.add(p["thread"])
            if (t.get("answers") or [target])[-1] == target: t["answer"] = clean  # an edit to an earlier answer leaves the latest one as the outcome
            if p["msgs"] and (t.get("answer_msg") or [0, 0])[1] in before + [p.get("tail")]: t["answer_msg"] = [cid, p.get("tail") or p["msgs"][-1]]  # the Close button follows the end of the answer

    def take_links(self, m, tid):
        """A conflict report: remove its LINKED line and put the card at the top of its thread, above the question."""
        m["body"], ids = strip_linked(m.get("body", ""))
        if not ids: return
        t = self.thread(tid, m.get("subject", ""), m); t.update(linked=ids, mailed=True); t["who"] = t.get("who") or m["from"]; self.dirty.add(tid)
        try:
            cid = None if t.get("gone") else self.ensure_thread(tid, t)
            if cid: self.render_links(tid, t, cid)
        except Exception as x: log("conflict card waits for the next render", tid, str(x)[:120])
    def on_mail(self, m):
        tid = self.resolve(m.get("thread") or m["id"]); self.note_project(tid, m)
        if not m.get("control"): self.take_links(m, tid)
        if m.get("control"):
            try: self.amend(m, m["control"])
            except DiscordError as x: log("amend failed", m["id"], str(x)[:160])  # a target that is gone must not block the inbox
        elif m.get("options"): self.on_question(m, tid)
        elif str(m.get("subject", "")).startswith("[mail-runner]"): pass  # the progress event already raised the alert
        else: self.on_answer(m, tid)
        mail("ack", "owner", m["id"], "--result", "shown in Discord")

    # ---- The owner's words ----
    def addressee(self, text):
        """('builder', rest) for 'builder: rest', else (None, text)."""
        hit = re.match(r"^@?([A-Za-z][\w-]*)[:,]\s+(.*)$", text, re.S); agents = load_agents()
        if hit and hit.group(1).lower() in agents and hit.group(1).lower() != "owner": return hit.group(1).lower(), hit.group(2)
        return None, text
    def device_note(self, device): return f"\n\n(the owner chose the device: run the work on {device}. Pin delegated work with the device argument.)" if device else ""
    def from_owner(self, text, origin, device=None, extra=""):
        to, text = self.addressee(text); to = to or "orchestrator"
        body = text + extra + self.device_note(device)
        mid = AM.send("owner", to, word_cut(head_line(text), 80), body, needs_reply=True)["id"]
        t = self.thread(mid, head_line(text)); t.update(origin=origin, to=to, queued=time.time(), last_mail=mid, owner_at=time.time()); self.touch(t)
        self.dirty.add(mid); log("discord -> mail", to, mid); return mid
    def waiting_q(self, tid, chosen=None):
        """The question the owner's words answer: the card they replied to, else the newest one open in this work. Questions
        whose buttons do something other than reply (moving work between devices) are never answered by text."""
        typed = lambda q: not any(o.get("action") for o in q["options"])
        if chosen in self.st["q"]: return chosen if typed(self.st["q"][chosen]) else None
        return next((k for k, q in reversed(list(self.st["q"].items())) if q.get("thread") == tid and typed(q)), None)
    def continue_thread(self, tid, text, device=None, extra="", chosen=None, answer=True):
        """The owner spoke inside an existing piece of work: same mail thread, and an open question takes it as the answer.
        The mail is written first, so a failure leaves this work's state untouched for the retry."""
        t = self.st["threads"].get(tid) or self.thread(tid, head_line(text)); t.pop("closed", None); t["owner_at"] = time.time()
        to, said = self.addressee(text); open_q = self.waiting_q(tid, chosen) if answer else None  # answer=False: the triage saw it does not answer the open question, which stays open
        if open_q and not chosen and said.rstrip().endswith("?") and (load_agents().get(self.st["q"][open_q].get("from")) or {}).get("auto"): open_q = None  # they asked an agent something back: the card and its buttons stay open
        def moved(): self.touch(t); self.dirty.add(tid); t["queued"] = time.time(); t.pop("waiting", None); t["status_move"] = True; t.pop("turn_answer", None); t["turn_at"] = time.time(); t.pop("suggest", None); t.pop("fb_ok", None)  # the owner spoke: the next answer is a new message, with buttons of its own
        if open_q and not to:
            mid = AM.reply("owner", open_q, said + extra + self.device_note(device))["id"]
            asker = self.st["q"][open_q].get("from")
            if not (load_agents().get(asker) or {}).get("auto"):  # a session's question: no runner reads that inbox, and the session may be gone
                mid = AM.send("owner", "orchestrator", word_cut(head_line(said), 80), f"The owner answered a question from `{asker}`, an interactive session that may be gone. "
                              f"Act on their words if that session has not.\n\nQuestion: {clip(self.st['q'][open_q].get('body', ''), 800)}\n\nThe owner: {said}{extra}{self.device_note(device)}",
                              needs_reply=True, reply_to=mid)["id"]; t["to"] = "orchestrator"
            q = self.st["q"].pop(open_q); self.q_clear(q); self.hold_qs(tid, "Closed: you answered the same question above.", keep=open_q, asker=q.get("from"), like=q) if tid else None; t["last_mail"] = mid; moved(); self.q_answered(self.q_channel(q), q.get("msg"), *q.get("extra", []))
            if q.get("msg"):
                try: self.edit_to(self.q_channel(q), q["msg"], clip(self.q_text(q), 1800) + f"\n**Answer:** {clip(head_line(said), 150)}", components=[])
                except Exception as e: log("question card not updated", str(e)[:120])
            self.q_done_old(q); log("discord -> answer", open_q, mid); return mid
        held = self.hold_qs(tid)  # not the answer (triage said so, they asked back, or wrote to another agent): asked again after
        if held: extra += ("\n\n(On hold: " + "; ".join(f"your question \"{clip(q.get('subject') or q.get('body', ''), 100)}\" ({k})" for k, q in held) +
                           ". The owner did not answer it; they asked for this first. Do this first and show the result. Then, if the decision is still needed, "
                           "ask it again as a new question with fresh buttons, updated for what changed.)")
        to = to or t.get("to") or "orchestrator"; body = said + extra + self.device_note(device); subject = word_cut(head_line(said), 80)
        fresh = said.strip() and not held and new_work(said) and self.state_word(t) in ("DONE", "FAILED", "CUT OFF")  # new work, not a question about the old
        try: mid = AM.send("owner", to, subject, body, needs_reply=True, reply_to=t.get("last_mail") or tid)["id"]
        except AM.MailError:  # the thread began as a queue card or loop event, with no mail to attach to
            mid = AM.send("owner", to, subject, body, needs_reply=True)["id"]; self.st["alias"][mid] = tid
            if len(self.st["alias"]) > 400: self.st["alias"] = dict(list(self.st["alias"].items())[-300:])
        if fresh:
            t["epoch"] = time.time(); t.pop("detail", None)
            if "improvement cycle" not in str(t.get("title", "")).lower(): nt = head_line(said)[:120]; t["retitle"] = t.get("retitle") or nt != t.get("title"); t["title"] = nt; t["titled"] = True  # loop threads keep the title #loops finds them by
        t["last_mail"] = mid; moved(); log("discord -> thread", to, tid, mid); return mid
    def hold_qs(self, tid, note="On hold: you asked for something else first. It is asked again once that is done.", keep=None, asker=None, like=None):
        """The owner wrote something other than the answer (a fix, a complaint): the thread's open questions go on hold. Their
        buttons come off and the card stops moving to the bottom; the orchestrator asks again after doing what they said.
        Also used when an agent asks again in the same thread (asker, keep = the new one): the older card is replaced."""
        held = []
        for key, q in list(self.st["q"].items()):
            if key == keep or q.get("thread") != tid or any(o.get("action") for o in q["options"]): continue  # device-move buttons are not the owner's decision to postpone
            if asker and (q.get("from") != asker or not same_question(q, like if like is not None else self.st["q"][keep])): continue
            self.st["q"].pop(key); self.q_clear(q); held.append((key, q))
            if q.get("msg") and q.get("thread") in self.st["threads"]: self.st["threads"][q["thread"]].setdefault("held_cards", []).append([self.q_channel(q), q["msg"]])
            if q.get("msg"):
                try: self.edit_to(self.q_channel(q), q["msg"], clip(self.q_text(q), 1700) + f"\n-# {note}", components=[])
                except Exception as e: log("question card not updated", str(e)[:120])
            self.q_answered(self.q_channel(q), q.get("msg"), *q.get("extra", [])); self.q_done_old(q); log("question on hold" if not asker else "question replaced", key)
        return held
    def q_by_message(self, mid):
        """The open question that this message is the card, or an earlier part, of."""
        return next((k for k, q in self.st["q"].items() if mid and (mid == q.get("msg") or mid in q.get("extra", []) or mid in q.get("old", []))), None)
    def by_message(self, mid):
        """The work a message in the channel belongs to, so a Discord reply to an old card continues it."""
        for tid, t in self.st["threads"].items():
            if mid in (t.get("origin"), t.get("card"), t.get("anchor")): return tid
        key = self.q_by_message(mid)
        return self.st["q"][key].get("thread") if key else None
    def react(self, cid, mid, emoji=SEEN_EMOJI, method="PUT"):
        """A reaction on the owner's message, right where they are looking. Best effort; False when Discord refused."""
        try: api(method, f"/channels/{cid}/messages/{mid}/reactions/{emoji}/@me"); return True
        except Exception: return False
    def relay(self, r):
        """Mail one of the owner's messages to the agents. True when it continued existing work. Raises if no mail was written."""
        d, device, cid = r["d"], r.get("device"), r["d"]["channel_id"]
        if "extra" not in r:
            lost = []; r["extra"] = self.attachments(d, lost)
            if lost: r["lost"] = lost
        text = (d.get("content") or "").strip() or "See the attached file(s)."
        if r.get("board"):
            if not d.get("attachments") and not re.sub(r"<(@[!&]?|#)\d+>", "", text).strip(): log("board message ignored, only links", r["board"], d["id"]); return False
            self.redirect(r, text); return False
        tid = self.st["dthreads"].get(cid); tid = None if tid and tid.startswith("session:") else tid
        ref = (d.get("message_reference") or {}).get("message_id"); chosen = self.q_by_message(ref) if ref else None
        if tid and tid not in self.st["threads"]: self.thread(tid, head_line(text)).update(dthread=cid, name=None, mailed=True)
        tid = tid or (self.by_message(ref) if ref else None)
        if not tid: self.from_owner(text, d["id"], device, r["extra"]); return False
        self.continue_thread(tid, text, device, r["extra"], chosen if chosen and self.st["q"][chosen].get("thread") == tid else None)
        return True
    def hand_over(self, r):
        """Relay now. If that fails the owner sees it on their own message, and the message is kept and retried."""
        try: continued = self.relay(r)
        except Exception as x: return self.relay_failed(r, x)
        if continued and not self.cfg.get("notepad"): self.react(r["d"]["channel_id"], r["d"]["id"])
    def relay_failed(self, r, x):
        d, now = r["d"], time.time(); log("ERR relay", d["id"], str(x)[:200])
        self.st.setdefault("unsent", {})[d["id"]] = r
        r.setdefault("first", now); r["tries"] = r.get("tries", 0) + 1; r["next"] = now + min(300, 30 * 2 ** (r["tries"] - 1))
        if not r.get("flagged"): r["flagged"] = self.react(d["channel_id"], d["id"], FAIL_EMOJI)
        if not r.get("notice"):
            try: r["notice"] = self.send_to(d["channel_id"], f"⚠️ **Not delivered to the agents yet.** Retrying for up to a day. You don't need to resend it.\n-# {str(x)[:200]}", reply_to=d["id"])
            except Exception as e: log("could not post the failure notice", str(e)[:120])
    def retry_unsent(self, now):
        for mid, r in list(self.st.get("unsent", {}).items()):
            if now < r.get("next", 0): continue
            d = r["d"]; cid = d["channel_id"]
            try: self.relay(r)
            except Exception as x:
                if now - r.get("first", now) < UNSENT_KEEP: self.relay_failed(r, x); continue
                del self.st["unsent"][mid]; log("gave up on relay", mid, str(x)[:200])
                if r.get("notice"):
                    try: self.edit_to(cid, r["notice"], "⚠️ **Could not deliver this to the agents after a day of retries. Please send it again.**")
                    except Exception as e: log("could not update the failure notice", str(e)[:120])
                continue
            del self.st["unsent"][mid]; log("relay recovered", mid)
            if r.get("flagged"): self.react(cid, mid, FAIL_EMOJI, "DELETE")
            self.react(cid, mid)
            if r.get("notice"):
                try: self.delete_to(cid, r["notice"])
                except Exception as e: log("could not remove the failure notice", str(e)[:120])
    def on_message(self, d):
        if int(d.get("type", 0)) in NEVER_DELETABLE: return  # a rename notice: nobody can delete it, so nothing is tried
        if int(d.get("type", 0)) in SYSTEM_TYPES: return self.on_system(d)
        if d["author"]["id"] != self.owner or d.get("type", 0) not in (0, 19) or not (d.get("content", "").strip() or d.get("attachments")): return
        if d["id"] in self.st.setdefault("seen", []): return
        if int(d["id"]) > int(self.st.get("last_owner_id") or 0): self.st["last_owner_id"] = d["id"]  # catch_up resumes from here after a restart
        cid, tid = d.get("channel_id"), self.st["dthreads"].get(d.get("channel_id"))
        board = self.board_of(cid) or ("overview" if not tid and self.ov_thread(cid) else None)  # a thread has its own channel id: only an #overview card's thread counts as its board
        if cid and cid == self.st.get("argon_thread"):  # the owner's chat with the assistant: never agent work
            self.st["seen"] = (self.st["seen"] + [d["id"]])[-200:]; return self.argon_start(d)
        if not tid and not board and cid != self.ch["orchestrator"]: return
        if tid and tid.startswith("session:"): return
        self.st["seen"] = (self.st["seen"] + [d["id"]])[-200:]
        if not board and self.RECO.match(d.get("content") or "") and self.recommend_go(d, tid): return
        if self.cfg.get("quick_status") and not board and self.quick_status(d, tid): return
        if self.cfg.get("notepad") and not board: return self.note_in(d, tid)
        self.hand_over({"d": {k: d.get(k) for k in ("id", "channel_id", "content", "attachments", "message_reference")}, **({"board": board} if board else {})})
    RECO = re.compile(r"^\s*(please\s+)?(do|go with|use|apply|take|act on)\s+(your|ur|the)\s+recommend", re.I)
    def recommend_go(self, d, tid):
        """The owner said "do your recommendations": the orchestrator gets every open question (this thread's only, inside one)
        with their go-ahead, picks its recommended option for each and skips the risky ones. The go-ahead (st["grant"]) lets
        the bridge carry out those answers; it covers only the questions open now, for GRANT_TTL."""
        typed = lambda q: not any(o.get("action") or o.get("device") for o in q["options"])
        qs = {k: q for k, q in self.st["q"].items() if typed(q) and (not tid or q.get("thread") == tid)}
        if not qs:
            said = self.send_to(d["channel_id"], "No open questions to answer.", reply_to=d["id"])
            if tid in self.st["threads"]: self.gemini_note(tid, d["channel_id"], said)
            return True
        self.st["grant"] = {"at": time.time(), "qs": list(qs), "by": d["id"]}
        rows = []
        for k, q in qs.items():
            t = self.st["threads"].get(q.get("thread")) or {}
            rows.append(f"- Question `{k}` from `{q.get('from')}`, mail thread `{q.get('thread')}` (\"{clean_title(t.get('title') or '', 80)}\"), asked {k[4:6]}-{k[6:8]} {k[9:11]}:{k[11:13]} UTC:\n  "
                        + clip(q.get("body", ""), 900).replace("\n", "\n  ") + "\n  Options: " + ", ".join(f"{i} = {o['label']}" for i, o in enumerate(q["options"])))
        extra = ("\n\nThe owner's go-ahead covers these open questions:\n" + "\n".join(rows) + "\n\n" + RECO_HOWTO)
        mid = self.from_owner(d.get("content") or "", d["id"], extra=extra)
        log("recommendations go-ahead", len(qs), "questions", mid); return True
    QUICK = re.compile(r"^\s*(\?+|status\??|update( me)?\??|progress\??|what'?s? (is )?(running|going on|queued|happening|left)\??|anything running\??)\s*$", re.I)
    def quick_status(self, d, tid):
        """A bare status question ("?", "update me", "whats running") is answered from the files, at once, with no agent
        run. Config "quick_status". Anything longer, or a question in a thread that is not working, queued or paused, goes to the orchestrator as before."""
        if d.get("attachments") or not self.QUICK.match(d.get("content") or ""): return False
        t0 = self.st["threads"].get(tid) if tid else None
        if t0 and self.state_word(t0) not in ("WORKING", "QUEUED", "PAUSED"): return False
        try:
            work, lines = fleet.active_work(limit=200), []
            t = self.st["threads"].get(tid) if tid else None
            if t:
                lines.append(self.status_text(t))
                here = [i for r in work if r["thread"] == tid for i in r["items"]]
                lines += [f"-# {i['agent']} {i['state']}: {i['subject'][:80]}" for i in here[:5]]
                if not here and not self.active(t): lines.append("-# Nothing is running or waiting in this thread.")
            else:
                items = [(r, i) for r in work for i in r["items"] if i["agent"] not in ("mac", "pc", "owner", "sessions")]
                run = [(r, i) for r, i in items if i["state"].startswith("running")]; wait = [(r, i) for r, i in items if not i["state"].startswith("running")]
                asks = [r for r in work if any("The owner" in i["state"] for i in r["items"])]
                lines.append(f"**Running ({len(run)})**" + "".join(f"\n- {i['agent']} on {i['state'].split(' on ')[-1]}: {i['subject'][:70]}" for r, i in run[:8]) + ("" if run else "\n-# nothing"))
                lines.append(f"**Waiting ({len(wait)})**" + "".join(f"\n- {i['agent']} {i['state']}: {i['subject'][:70]}" for r, i in wait[:8]) + ("" if wait else "\n-# nothing"))
                if asks: lines.append(f"**Needs you ({len(asks)})**" + "".join(f"\n- {r['title'][:80]}" for r in asks[:5]))
            said = self.send_to(d["channel_id"], clip("\n".join(lines), 1900), reply_to=d["id"])
            if tid in self.st["threads"]: self.gemini_note(tid, d["channel_id"], said)  # a status snapshot goes once the next answer posts
            log("quick status answered", d["id"]); return True
        except Exception as e:
            log("quick status failed, handing to the orchestrator", str(e)[:120]); return False
    # ---- The owner's notepad: consecutive messages are read together, with what they reply to ----
    def note_in(self, d, tid):
        """Hold one of the owner's messages for a moment so several sent in a row reach the agents as one, and write it in
        the thread's notepad. Nothing is mailed here; flush_notes does that once they have gone quiet."""
        cid = d["channel_id"]; ref = d.get("referenced_message")
        reply = None
        if isinstance(ref, dict):
            reply = {"who": "their own earlier message" if (ref.get("author") or {}).get("id") == self.owner else "the bot", "text": " ".join(str(ref.get("content") or "").split())[:220] or ("its attachment " + ", ".join(str(x.get("filename") or "") for x in (ref.get("attachments") or []))).strip() or "a message with no text"}
        r = {"d": {k: d.get(k) for k in ("id", "channel_id", "content", "attachments", "message_reference")}}
        if reply: r["reply"] = reply
        ref_id = (d.get("message_reference") or {}).get("message_id")
        answering = bool(tid or (ref_id and (self.by_message(ref_id) or self.q_by_message(ref_id))))  # inside a request, or a reply to the bot
        b = self.st.setdefault("batches", {}).setdefault(cid, {"rs": []})
        b["rs"].append(r); b["due"] = time.time() + ((4 if self.cfg.get("triage") else QUIET) if answering else 0)  # with triage the owner hears back fast, so only a short wait for more lines  # only answers are held; a new request starts at once
        NOTES.add(tid or f"new-{cid}", d["id"], d.get("content") or "", cid=cid, reply=reply, files=len(d.get("attachments") or []))
        if self.cfg.get("triage") and not str(tid or "").startswith("session:"): self.ack(cid, d["id"])
        elif not str(tid or "").startswith("session:"): self.react(cid, d["id"])  # no triage: the read mark at once, so the owner sees it was taken
        if tid in self.st["threads"]: self.note_count(tid)
        if not answering: self.flush_notes(time.time())
    def catch_up(self):
        """After any (re)connect: Discord does not replay what was sent while the bridge was down (a restart onto new code
        takes about 20 s), so the owner's messages newer than the last one handled, from the last 2 hours, are fetched from the
        channels and active threads and handled now, oldest first. st["seen"] keeps anything from being handled twice."""
        floor = str(int((time.time() - 7200) * 1000 - 1420070400000) << 22)
        if not self.st.get("last_owner_id"):  # first run with catch-up: start after the newest message already handled, never re-handle older ones
            self.st["last_owner_id"] = str(max([int(x) for x in self.st.get("seen", []) if str(x).isdigit()] or [int(floor)]))
        after = max(int(self.st.get("last_owner_id") or 0), int(floor))
        try: threads = [x["id"] for x in (api("GET", f"/guilds/{self.cfg['guild_id']}/threads/active") or {}).get("threads", []) if int(x.get("last_message_id") or 0) > after]
        except Exception: threads = []
        chans = [c for c in list(self.ch.values()) + [self.st.get("argon_thread")] if c] + threads
        found = []
        for cid in dict.fromkeys(chans):
            try: ms = api("GET", f"/channels/{cid}/messages?after={after}&limit=50") or []
            except Exception: continue
            found += [{**m, "channel_id": cid} for m in ms if str(m.get("author", {}).get("id")) == str(self.owner) and m["id"] not in self.st.get("seen", [])]
        for m in sorted(found, key=lambda m: int(m["id"])):
            log("caught up a message sent while the bridge was down", m["channel_id"], m["id"])
            try: self.on_message(m)
            except Exception as x: log("ERR caught-up message", m["id"], str(x)[:120])
    def ack(self, cid, mid):
        """At once, no model: '✦ Getting that in…' under the owner's first message, edited to 'Adding this too…' for each one
        that follows while it is still being read. The triage model's reply later replaces this same message (gemini_say)."""
        a = self.st.setdefault("acks", {}).get(cid); now = time.time()
        if a and now - a["at"] < 300:
            a["n"] += 1; a["at"] = now
            try: self.edit_to(cid, a["mid"], "✦ Adding this too…" if a["n"] == 2 else f"✦ Adding these {a['n']} together…", card=False)
            except Exception as e: log("ack not updated", str(e)[:100])
            return
        try: self.st["acks"][cid] = {"mid": self.send_to(cid, "✦ Getting that in…", reply_to=mid, card=False), "n": 1, "at": now}
        except Exception as e: log("ack not posted", str(e)[:100])
    def drop_ack(self, cid):
        """An acknowledgement nobody turned into the reply (the reply went into a new thread, or the triage gave up) goes."""
        a = (self.st.get("acks") or {}).pop(cid, None)
        if a:
            try: self.delete_to(cid, a["mid"])
            except Exception: pass
    def note_count(self, tid):
        t = self.st["threads"].get(tid)
        if not t: return
        n = len(NOTES.unread(tid))
        if t.get("unread", 0) != n:
            t["unread"] = n; self.dirty.add(tid)
            if not n: t.pop("unread", None)
    def on_typing(self, d):
        b = self.st.get("batches", {}).get(d.get("channel_id"))
        if b and d.get("user_id") == self.owner: b["due"] = max(b.get("due", 0), time.time() + QUIET_TYPING)
    def flush_notes(self, now):
        """Mail each finished batch as one message: the owner's words in order, each with what it replied to."""
        for cid, b in list(self.st.get("batches", {}).items()):
            if now < b.get("due", 0) or not b["rs"]:
                if not b["rs"]: self.st["batches"].pop(cid, None)
                continue
            if any(x["merged"]["d"]["channel_id"] == cid for x in self.st.get("triage", {}).values()): continue  # they added more while the last batch is being read: it goes next, in order
            rs = b["rs"]; first, last = rs[0], rs[-1]
            def line(r):
                re_ = f"(replying to {r['reply']['who']}: \"{r['reply']['text']}\") " if r.get("reply") else ""
                return re_ + ((r["d"].get("content") or "").strip() or "See the attached file(s).")
            text = line(first) if len(rs) == 1 else f"The owner sent {len(rs)} messages in a row. Read them together as one message:\n" + "\n".join(f"{i}. {line(r)}" for i, r in enumerate(rs, 1))
            refs = [(r["d"].get("message_reference") or {}) for r in rs]
            asked = next((x for x in refs if x.get("message_id") and self.q_by_message(x["message_id"])), None)
            merged = {"d": {"id": first["d"]["id"], "channel_id": cid, "content": text, "attachments": [a for r in rs for a in (r["d"].get("attachments") or [])],
                            "message_reference": asked or next((x for x in refs if x.get("message_id")), None)}}
            self.st["batches"].pop(cid, None)
            if self.cfg.get("triage") and not merged["d"]["attachments"] and not str(self.st["dthreads"].get(cid) or "").startswith("session:"):
                self.triage_start(merged, len(rs)); continue
            try: self.relay(merged)
            except Exception as x: self.relay_failed(merged, x); continue
            self.notes_sent(cid, first["d"]["id"], len(rs))
    def notes_sent(self, cid, first_id, n):
        tid = self.st["dthreads"].get(cid) or next((k for k, t in self.st["threads"].items() if t.get("origin") == first_id), None)
        if tid:
            NOTES.move(f"new-{cid}", tid); self.note_count(tid)
        log("notes sent", cid, n, "message(s) as one")
        return tid
    # ---- Triage (optional small model, triage.py): the first reader of each of the owner's messages ----
    def history_text(self, tid, n=10):
        try: rows = AM.thread(self.resolve(tid))[-n:]
        except Exception: return ""
        return "\n".join(f"{m.get('from')}: {' '.join(str(m.get('body') or '').split())[:500]}" for m in rows)
    def triage_job(self, merged):
        cid = merged["d"]["channel_id"]; tid = self.st["dthreads"].get(cid); t = self.st["threads"].get(tid) if tid else None
        q = self.waiting_q(tid) if tid else None; now = time.time()
        others = [(k, x) for k, x in self.st["threads"].items() if k != tid and not x.get("gone") and (x.get("origin") or x.get("mailed"))
                  and (self.state_word(x) in ("WORKING", "QUEUED", "NEEDS YOU", "WAITING") or now - x.get("active_at", x.get("t0", 0)) < 6 * 3600)]
        others.sort(key=lambda kx: -kx[1].get("active_at", kx[1].get("t0", 0)))
        return {"text": merged["d"]["content"], "thread": tid or "", "status": self.status_text(t) if t else "",
                "history": self.history_text(tid) if tid else "", "question": clip(self.st["q"][q].get("body", ""), 800) if q else "",
                "others": "\n".join(f"{k} | {clean_title(x.get('title') or k, 70)} | {STATES[self.state_word(x)][1]}" for k, x in others[:15])}
    def triage_spawn(self, path):
        how = {"creationflags": 0x08000000 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
        return subprocess.Popen([sys.executable, "-X", "utf8", str(TK / "triage.py"), str(path)], cwd=str(TK), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **how)
    def triage_start(self, merged, n):
        """Hand the owner's message to the triage model (triage.py) and show them it is being read. triage_poll acts on the answer.
        Without the optional `agy` CLI the job comes back as an error and triage_finish relays the message unchanged."""
        cid, jid = merged["d"]["channel_id"], "t" + str(merged["d"]["id"])
        d = dpath("triage"); d.mkdir(parents=True, exist_ok=True)
        try: (d / f"{jid}.json").write_text(json.dumps(self.triage_job(merged), ensure_ascii=False), encoding="utf-8"); self.triage_spawn(d / f"{jid}.json")
        except Exception as x:
            log("triage could not start, sending as before", str(x)[:120])
            try: self.relay(merged)
            except Exception as y: return self.relay_failed(merged, y)
            return self.notes_sent(cid, merged["d"]["id"], n)
        self.st.setdefault("triage", {})[jid] = {"merged": merged, "at": time.time(), "n": n}
        try: api("POST", f"/channels/{cid}/typing")
        except Exception: pass
    def triage_poll(self, now):
        for jid, j in list(self.st.get("triage", {}).items()):
            out, res = dpath("triage") / f"{jid}.out.json", None
            if out.exists():
                try: res = json.loads(out.read_text(encoding="utf-8"))
                except (OSError, ValueError): res = {}
            elif now - j["at"] < TRIAGE_WAIT: continue
            del self.st["triage"][jid]
            for f in (out, dpath("triage") / f"{jid}.json"): f.unlink(missing_ok=True)
            try: self.triage_finish(j, res or {})
            except Exception as x: log("ERR triage", jid, str(x)[:160])
            ch = j["merged"]["d"]["channel_id"]
            if not any(x["merged"]["d"]["channel_id"] == ch for x in self.st.get("triage", {}).values()):
                self.drop_ack(ch)
                nxt = (self.st.get("batches") or {}).get(ch)
                if nxt and nxt.get("rs"): self.ack(ch, nxt["rs"][0]["d"]["id"])  # what they added meanwhile is read next: it gets its own acknowledgement
    def gemini_say(self, cid, text, reply_to=None):
        a = (self.st.get("acks") or {}).pop(cid, None)
        if a:  # the instant acknowledgement becomes the reply, in place: one message, not two
            try: self.edit_to(cid, a["mid"], clip("✦ " + "\n".join(" ".join(l.split()) for l in text.strip().splitlines()), 1900), card=False); return a["mid"]
            except Exception as e: log("ack not turned into the reply", str(e)[:100])
        try: return self.send_to(cid, clip("✦ " + "\n".join(" ".join(l.split()) for l in text.strip().splitlines()), 1900), reply_to=reply_to)
        except Exception as e: log("triage reply not posted", str(e)[:120])
    def gemini_note(self, cur, cid, mid):
        """A routing notice inside a thread: kept so drop_gemini_notes removes it once the real answer posts."""
        if mid: self.st["threads"][cur].setdefault("gemini_notes", []).append([cid, mid])
        return mid
    def drop_gemini_notes(self, t):
        """one_answer_per_turn: the triage model's routing notices of this turn go once the real answer or question card is posted."""
        if not self.cfg.get("one_answer_per_turn"): return
        for ch, mid in t.pop("gemini_notes", []):
            try: self.delete_to(ch, mid)
            except Exception as e: log("gemini notice not removed", str(e)[:100])
    def triage_finish(self, j, res):
        """Answer it, send it into the thread it belongs to, or open it as new work, and tell the owner which in a line.
        A failed or missing result (no `agy`, a timeout) has no action and ends in the plain relay at the bottom."""
        merged = j["merged"]; d = merged["d"]; cid, text = d["channel_id"], d.get("content") or ""
        act, reply = res.get("action"), str(res.get("reply") or "").strip()
        cur = self.st["dthreads"].get(cid); cur = cur if cur in self.st["threads"] else None
        if act == "answer" and HISTORY.search(text):  # the triage model only sees the thread's last messages: an agent researches it
            act, reply = "forward", "Looking through the records for that; the orchestrator will list it here."
            res = {**res, "action": act, "thread": cur or "", "answers_question": False}
        if act == "answer" and reply and not DOING.match(text):
            self.gemini_say(cid, reply, reply_to=d["id"])  # an answer to their question stays; only routing notices go
            if cur:
                with open(self.sidechat_path(cur), "a", encoding="utf-8") as f: f.write(json.dumps({"at": time.time(), "q": text, "a": reply}, ensure_ascii=False) + "\n")
                self.notes_read(cur, time.time(), by="Gemini")
            else: NOTES.mark_read(f"new-{cid}", time.time(), "Gemini")  # answered: it must not ride along as NEW on their next request
            return log("triage answered", d["id"], res.get("model"), res.get("secs"))
        ref = (d.get("message_reference") or {}).get("message_id")
        if cur and act in ("forward", "new") and res.get("answers_question") is False and (self.waiting_q(cur) or (ref and self.q_by_message(ref))):
            self.continue_thread(cur, text, answer=False)  # a complaint or new problem, not the answer: the question goes on hold until it is handled
            self.notes_sent(cid, d["id"], j.get("n", 1))
            if reply: self.gemini_note(cur, cid, self.gemini_say(cid, reply, reply_to=d["id"]))
            return log("triage: not an answer to the open question", d["id"])
        target = self.resolve(res.get("thread") or "") if act == "forward" else None
        if cur and target and target != cur:  # typed inside a thread: it stays there; the orchestrator moves it if it belongs elsewhere (its overlap rule)
            self.continue_thread(cur, text); self.notes_sent(cid, d["id"], j.get("n", 1))
            if reply: self.gemini_note(cur, cid, self.gemini_say(cid, "Sent to this thread's orchestrator, which passes it on if it belongs to other work.", reply_to=d["id"]))
            return log("triage: kept in its own thread instead of moving it", d["id"], "->", cur)
        if target and target in self.st["threads"] and target != cur:
            self.continue_thread(target, text); th = self.st["threads"][target].get("dthread")
            if th:
                try: self.gemini_note(target, th, self.send_to(th, fenced(clip(f"From <#{cid}>:\n" + quoted(text), 1900))))  # goes once the answer there posts
                except Exception as e: log("triage forward not quoted", str(e)[:120])
            if reply: self.gemini_say(cid, reply + (f" <#{th}>" if th else ""), reply_to=d["id"])
            return log("triage moved it", d["id"], "->", target)
        try: self.relay(merged)
        except Exception as x: return self.relay_failed(merged, x)
        tid = self.notes_sent(cid, d["id"], j.get("n", 1))
        if not reply: return
        if cur: return self.gemini_note(cur, cid, self.gemini_say(cid, reply, reply_to=d["id"]))
        t = self.st["threads"].get(tid) if tid else None
        th = None
        if t:
            try: th = self.ensure_thread(tid, t)
            except Exception as e: log("triage: no thread yet for the reply", str(e)[:120])
        self.gemini_say(th or cid, reply, reply_to=None if th else d["id"])
    def sidechat_path(self, tid):
        d = dpath("sidechat"); d.mkdir(parents=True, exist_ok=True); return d / f"{tid}.jsonl"
    def notes_read(self, tid, ts, by=""):
        """An agent run started in this thread: everything the owner wrote before it has now been read. Mark those messages."""
        for n in NOTES.mark_read(tid, ts, by):
            if n.get("cid") and str(n.get("id", "")).isdigit(): self.react(n["cid"], n["id"])
        self.note_count(tid)
    def on_delete(self, d):
        """The owner deleted a message: before it was sent it simply never happened; after, the agents are told they took it back."""
        mid, cid = str(d.get("id")), d.get("channel_id")
        b = self.st.get("batches", {}).get(cid)
        if b and any(r["d"]["id"] == mid for r in b["rs"]):
            b["rs"] = [r for r in b["rs"] if r["d"]["id"] != mid]; NOTES.change(mid, state="withdrawn"); log("note withdrawn before it was sent", mid); return
        tid, n = NOTES.find(mid)
        if not n or n.get("state") == "withdrawn": return
        NOTES.change(mid, state="withdrawn"); log("note withdrawn", mid)
        cid = (self.st["threads"].get(tid) or {}).get("dthread") or (cid if self.st["dthreads"].get(cid) else None)
        if not cid: return  # no thread to tell: the notepad already shows it as withdrawn
        fake = {"d": {"id": f"del-{mid}", "channel_id": cid, "content": f"(I deleted an earlier message of mine. I take it back, ignore it: \"{' '.join(str(n.get('text', '')).split())[:300]}\")", "attachments": [], "message_reference": None}}
        nb = self.st.setdefault("batches", {}).setdefault(cid, {"rs": []}); nb["rs"].append(fake); nb["due"] = time.time() + QUIET
        if tid: self.note_count(tid)
    def on_edit(self, d):
        if (d.get("author") or {}).get("id") != self.owner or "content" not in d: return
        mid, cid, text = str(d.get("id")), d.get("channel_id"), d.get("content") or ""
        b = self.st.get("batches", {}).get(cid)
        for r in (b["rs"] if b else []):
            if r["d"]["id"] == mid: r["d"]["content"] = text; NOTES.change(mid, text=text, edited=True); return
        tid, n = NOTES.find(mid)
        if not n or n.get("text") == text or n.get("state") == "withdrawn": return
        NOTES.change(mid, text=text, edited=True)
        cid = (self.st["threads"].get(tid) or {}).get("dthread") or (cid if self.st["dthreads"].get(cid) else None)
        if not cid: return
        fake = {"d": {"id": f"edit-{mid}", "channel_id": cid, "content": f"(I edited an earlier message of mine. It now says: \"{' '.join(text.split())[:600]}\")", "attachments": [], "message_reference": None}}
        nb = self.st.setdefault("batches", {}).setdefault(cid, {"rs": []}); nb["rs"].append(fake); nb["due"] = time.time() + QUIET
    def web_prompts(self):
        """Prompts from outside Discord: any program (a web page, a script) may drop Queue/discord/web/web-<id>.json with
        {"text", "thread"}. The same as typing in #orchestrator, or inside a thread when the file names one. The owner's
        words are quoted in the Discord thread."""
        d = dpath("web")
        if not d.is_dir(): return
        seen = self.st.setdefault("web_seen", [])
        for p in sorted(d.glob("web-*.json")):
            wid = p.stem
            if wid not in seen:
                try: w = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError): continue  # still syncing
                text = str(w.get("text") or "").strip()
                if text:
                    tid = self.resolve(w["thread"]) if w.get("thread") and self.resolve(w["thread"]) in self.st["threads"] else None
                    if tid: self.continue_thread(tid, text); th = self.st["threads"][tid].get("dthread")
                    else:
                        mid = self.from_owner(text, None); t = self.st["threads"][mid]; t["mailed"] = True; t["who"] = "The owner, outside Discord"; th = None
                        try: th = self.ensure_thread(mid, t)
                        except Exception as e: log("web prompt has no thread yet", mid, str(e)[:120])
                    try: self.send_to(th or self.ch["orchestrator"], clip("From outside Discord:\n> " + text.replace("\n", "\n> "), 1900))
                    except Exception as e: log("web prompt not quoted", str(e)[:120])
                    log("web prompt -> mail", wid, tid or "new")
                self.st["web_seen"] = seen = (seen + [wid])[-200:]
            p.unlink(missing_ok=True)
    def ov_thread(self, cid):
        ov = self.st.get("overview") or {}
        return bool(cid) and (any(c.get("thread") == cid for c in (ov.get("cards") or {}).values()) or cid in (ov.get("orphans") or []))
    def board_of(self, cid): return next((k for k in BOARDS if self.ch.get(k) == cid), None)
    def loop_request(self, text, device=None, extra="", cid=None):
        """The owner asked, in #loops or with /loop add, to loop something or change a loop: the orchestrator sets it up with
        improve-loop.py. Returns (mail id, Discord thread id or None). Raises only if no mail was written."""
        said = LOOP_WORD.sub("", text.strip(), count=1) or text.strip()
        mid = self.from_owner("Loop: " + said, None, device, extra + LOOP_HOWTO)
        t = self.st["threads"][mid]; t["mailed"] = True; t["who"] = "The owner, in #loops"; th = None
        try:
            th = self.ensure_thread(mid, t)
            if cid: self.send_to(th or self.ch["orchestrator"], fenced(clip(f"From <#{cid}>:\n" + quoted(text), 1900)))
        except Exception as e: log("loop request not shown in its thread", mid, str(e)[:120])
        return mid, th
    def loop_cards(self, now):
        """The #loops board: a header, then one card per looped repo or project, in the loop's own order."""
        c, st = GL.config(), GL.load("state.json", {}); last = st.get("last", {}); used = GL.budget_used(c)
        threads, qs = self.st["threads"], self.st["q"].values()
        head = (f"🔁 **Improvement loop {'ON' if c['enabled'] else 'OFF'}**  ·  every {c['interval_hours']} h  ·  runs on {GL.fleet_label(fleet.pick_host(c['hosts']) or 'no device up')}"
                + ("  ·  **paused: day budget used up**" if used >= 1 else "")
                + "".join(f"\n-# {n}: added automatically, waiting for your Keep" for n in c["pending"])
                + "\n-# Type here to loop something new, refocus one or stop one. The orchestrator sets it up and your message is removed.")
        cards = [head]
        for kind, rows in (("repo", c["repos"]), ("project", c["projects"])):
            for name, v in rows.items():
                v = v or {}; heads = (f"{name}: improvement cycle".lower(), f"improvement cycle started for {name} (".lower())
                mine = sorted(((t.get("t0", 0), tid) for tid, t in threads.items() if str(t.get("title", "")).lower().startswith(heads)), reverse=True)
                asking = next((tid for t0, tid in mine if any(q.get("thread") == tid for q in qs) and not any(t1 > t0 and self.state_word(threads[x]) == "DONE" and (threads[x].get("last_end") or t1) >= max(q.get("at", 0) for q in qs if q.get("thread") == tid) for t1, x in mine)), None)
                every = float(v.get("every") or c["interval_hours"]) if kind == "project" else float(c["interval_hours"])
                t0 = last.get(name)
                if kind == "repo": ok = [d["label"] for d in fleet.where(name) if d["ok"]]; where = ", ".join(ok) or "no device can open it now"
                else: where = f"{GL.fleet_label(v.get('host'))}" + ("" if fleet.available(v.get("host") or "") else " (off now)")
                live = next((tid for _, tid in mine if self.state_word(threads[tid]) == "WORKING"), None)
                queued = None if live else next((tid for _, tid in mine if self.state_word(threads[tid]) == "QUEUED"), None)
                if live:
                    run = next((r for r in reversed(self.cur_runs(threads[live])) if not r.get("end")), None)
                    where = f"running on {run['label']}" if run and run.get("label") else "running"
                url = lambda cid: f"https://discord.com/channels/{self.cfg.get('guild_id')}/{cid}"  # a masked link: an archived thread's <#id> shows as "#unknown"
                if asking: icon, when = "🔴", f"needs your answer [here]({url(threads[asking].get('dthread') or self.ch['orchestrator'])})"
                elif live: icon, when = "🟡", "cycle running now"
                elif queued: icon, when = "⚪", ("cycle queued for its next step" if threads[queued].get("runs") else "cycle queued, waiting to start")
                elif v.get("paused"): icon, when = "⏸️", "paused: no session of yours in 14 days"
                elif not c["enabled"]: icon, when = "⏸️", "loop is off"
                elif t0 and t0 + every * 3600 > now: icon, when = "🟢", f"next <t:{int(t0 + every * 3600)}:R>"
                else:  # due: say what it waits for when the loop cannot start it now
                    lock = GL.held(name) if kind == "repo" else None
                    why = ("no device can open it now" if kind == "repo" and not ok else f"waiting: {name} is claimed" + (f" by {GL.fleet_label(lock['owner'])}" + (" (off now)" if not fleet.available(lock['owner']) else "") if lock and lock.get("owner") else "") + (f", frees <t:{int(u)}:R>" if lock and (u := GL.lock_until(lock)) is not None else "") if lock is not None
                           else f"waiting: {GL.fleet_label(v.get('host'))} is off" if kind == "project" and not fleet.available(v.get("host") or "") else "")
                    icon, when = ("⏳", "due, " + why) if why else ("🟢", "due now")
                link = next((threads[tid].get("dthread") for _, tid in mine if threads[tid].get("dthread")), None)
                cards.append(f"{icon} **{name}**  ·  {kind}  ·  {v.get('mode', 'ask')} mode  ·  every {every:g} h  ·  {where}"
                             + (" [added automatically]" if v.get("auto") else "")
                             + f"\n-# {when}  ·  last cycle " + (f"<t:{int(t0)}:R>" if t0 else "never") + (f"  ·  [latest cycle]({url(link)})" if link else "")
                             + (f"\n-# Focus: {clip(v['brief'], 300)}" if kind == "project" and v.get("brief") else ""))
        return cards
    def post_cards(self, key, b, texts):
        """Keep board KEY as these messages in order, each edited in place. True when they were all posted again
        (first run, a different count, or one deleted by hand: rebuilt on the next pass)."""
        if len(b["ids"]) != len(texts):
            for mid in b["ids"]: self.zap(self.ch[key], mid)
            b["ids"], b["text"] = [self.send(key, x) for x in texts], list(texts)
            return True
        for i, x in enumerate(texts):
            if b["text"][i] == x: continue
            done = self.edit(key, b["ids"][i], x)
            if done is None: b["ids"] = []; return False
            if done: b["text"][i] = x
        return False
    def render_loops(self, now):
        if "loops" not in self.ch: return
        b = self.st.setdefault("loops", {"ids": [], "text": []})
        if now - b.get("at", 0) < LOOP_EVERY: return
        b["at"] = now; self.post_cards("loops", b, self.loop_cards(now))
    def redirect(self, r, text):
        """The owner typed in a read-only board channel: it becomes a normal request with its thread in #orchestrator, and their
        message is removed. Without Manage Messages it stays, with one reply that links the thread; the sweep removes
        both once the permission is there. If one of its files could not be saved it stays for good, with the same
        kind of reply. Raises only if no mail was written, so a retry cannot mail it twice."""
        d = r["d"]; cid, board = d["channel_id"], r["board"]
        if board == "loops": mid, th = self.loop_request(text, r.get("device"), r["extra"], cid)
        else:
            mid = self.from_owner(text, None, r.get("device"), r["extra"])
            t = self.st["threads"][mid]; t["mailed"] = True; t["who"] = f"The owner, typed in #{CHANNEL_NAMES.get(board, board)}"; th = None
            try:
                th = self.ensure_thread(mid, t)
                self.send_to(th or self.ch["orchestrator"], clip(f"Moved here from <#{cid}>, which is read-only:\n> " + text.replace("\n", "\n> "), 1900))  # no mention: they just typed it, and join_owner adds them
            except Exception as e: log("redirect not shown in its thread", mid, str(e)[:120])
        rec = {"ch": cid, "at": time.time()}
        if r.get("lost"):  # a file of theirs was not saved: their message is the only copy, so it stays and the sweep leaves it
            rec["keep"] = True; log("board message kept, file not saved", board, mid, ", ".join(r["lost"])[:120])
            try: rec["reply"] = self.send_to(cid, f"{READ_ONLY} Your message went to the orchestrator: <#{th or self.ch['orchestrator']}>. It stays here because a file could not be saved: {clip(', '.join(r['lost']), 300)}", reply_to=d["id"])
            except Exception as x: log("redirect reply failed", str(x)[:120])
            self.st.setdefault("redirects", {})[d["id"]] = rec; return
        try: api("DELETE", f"/channels/{cid}/messages/{d['id']}"); return log("board message redirected", board, mid)
        except DiscordError as e:
            if e.code in (UNKNOWN_MESSAGE, UNKNOWN_CHANNEL): return
            if e.code == MISSING_PERMISSIONS:
                try: rec["reply"] = self.send_to(cid, f"{READ_ONLY} Your message went to the orchestrator: <#{th or self.ch['orchestrator']}>", reply_to=d["id"])
                except Exception as x: log("redirect reply failed", str(x)[:120])
            else: log("board message not removed yet", str(e)[:120])
        except Exception as e: log("board message not removed yet", str(e)[:120])  # rate limit or network: the sweep removes it
        self.st.setdefault("redirects", {})[d["id"]] = rec
    def on_thread_event(self, kind, d):
        cid = d.get("id"); tid = self.st["dthreads"].get(cid)
        if not tid: return
        if kind == "THREAD_DELETE": return self.forget(cid)
        if tid.startswith("session:"): return
        t = self.st["threads"].get(tid)
        if not t: return
        if (d.get("thread_metadata") or {}).get("archived"): t["archived"] = True
        else: t.pop("archived", None)
        if d.get("name") and t.get("name") and d["name"] != t["name"]:
            if self.own_name(t, d["name"]): t["name"] = d["name"]; t.pop("name_lock", None)  # only the state emoji differs: the next sync puts it right
            else: t["name_lock"] = True  # the owner renamed it by hand: keep their name

    def respond(self, i, content, ephemeral=True, components=None, update=False):
        rec = self.card_of((i.get("message") or {}).get("id")) if update else None
        if rec:  # the tapped message is a card: it is answered as a card, its text kept
            rows = components if components is not None else rec["rows"]
            data = self.card_body(content, rows); data.pop("content", None); self.remember_card(i["message"]["id"], content, rows)
            return api("POST", f"/interactions/{i['id']}/{i['token']}/callback", {"type": 7, "data": data}, auth=False)
        data = {"content": content[:2000], "allowed_mentions": {"parse": []}}
        if ephemeral and not update: data["flags"] = 64
        if components is not None: data["components"] = components
        api("POST", f"/interactions/{i['id']}/{i['token']}/callback", {"type": 7 if update else 4, "data": data}, auth=False)

    def demo(self, i, cid):
        """The gallery thread's buttons (cards.py): a private card, a form, or a note. Nothing real changes."""
        import cards
        _, what, n = (cid.split(":", 2) + ["", ""])[:3]
        cb = lambda body: api("POST", f"/interactions/{i['id']}/{i['token']}/callback", body, auth=False)
        if what == "open":
            if not getattr(self, "avatar", None):
                try: me = api("GET", "/users/@me"); self.avatar = f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=256" if me.get("avatar") else "https://cdn.discordapp.com/embed/avatars/0.png"
                except Exception: self.avatar = "https://cdn.discordapp.com/embed/avatars/0.png"
            return cb({"type": 4, "data": {"flags": 64 | cards.V2, "components": [cards.opened(n, self.avatar)], "allowed_mentions": {"parse": []}}})
        if what == "form" and n != "submit": return cb({"type": 9, "data": cards.form()})
        if what == "form":
            got = {c["custom_id"]: c.get("value", "") for r in i["data"].get("components", []) for c in r.get("components", [])}
            return self.respond(i, f"**Form received** (demo, nothing was sent)\n> {got.get('what', '')}\n-# Repo: {got.get('repo') or 'not given'}")
        if what == "pick": return self.respond(i, f"You picked option {int((i['data'].get('values') or ['0'])[0]) + 1}. In real use this would continue the thread with that choice.")
        return self.respond(i, f"Demo button `{what}` tapped. Nothing changed.")
    def followup(self, i, content):
        """A private note after the tap was already answered."""
        try: api("POST", f"/webhooks/{self.cfg['bot_id']}/{i['token']}", {"content": content[:2000], "flags": 64, "allowed_mentions": {"parse": []}}, auth=False)
        except Exception as e: log("follow-up not sent", str(e)[:120])

    def open_session(self, i, tid, ref):
        """Open in Claude: the runner on the run's device opens that Claude session and answers with its link."""
        t = self.st["threads"].get(tid or "") or {}; r = (t.get("runs") or {}).get(ref) or {}
        if not r.get("session"): return self.respond(i, "That Claude session is not known any more.")
        self.open_request(i, r["host"], {"kind": "session", "session": r["session"], "cwd": r.get("cwd") or "", "name": clean_title(t.get("title") or tid, 100)}, "Opened the Claude session on the {label}.", "Open in Claude")
    def open_file(self, i, tid, n, short):
        """Open on Mac / Open on PC under an answer that named a local file (file_rows)."""
        x = ((self.st["threads"].get(tid or "") or {}).get("open_files") or {}).get(n)
        host = next((d["host"] for d in fleet.devices() if d["platform"] == {"mac": "mac", "pc": "windows"}.get(short)), None)
        if not x or not host: return self.respond(i, "That file is not known any more.")
        self.open_request(i, host, {"kind": "file", "path": x["path"]}, f"Opened {x['name']} on the {{label}}.", "Open")
    def open_request(self, i, host, req, ok, link):
        """Queue/open/<host>/<id>.json for that device's runner. The tap gets a private "thinking" reply at once (Discord
        wants one within 3 s); pump_opens edits it once the runner writes <id>.done.json, or after OPEN_WAIT."""
        label = self.shot_label(host)
        if not fleet.available(host): return self.respond(i, f"The {label} is offline right now, so nothing can be opened there.")
        api("POST", f"/interactions/{i['id']}/{i['token']}/callback", {"type": 5, "data": {"flags": 64}}, auth=False)
        rid = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
        fleet._write(queue_root() / "open" / host / f"{rid}.json", {**req, "by": "bridge", "at": time.time()})
        self.st.setdefault("opens", {})[rid] = {"host": host, "token": i["token"], "at": time.time(), "ok": ok.format(label=label), "link": link}; log("open requested", host, req["kind"], rid)
    def pump_opens(self, now):
        """Each tapped Open whose device answered (or did not within OPEN_WAIT): its private reply becomes the result."""
        for rid, o in list((self.st.get("opens") or {}).items()):
            base = queue_root() / "open" / o["host"]; done = base / f"{rid}.done.json"
            try: res = json.loads(done.read_text(encoding="utf-8"))
            except (OSError, ValueError): res = None
            if res is not None and not isinstance(res, dict): res = {"error": f"unreadable answer {str(res)[:80]}"}
            if res is None and now - o["at"] < OPEN_WAIT: continue
            label, url = self.shot_label(o["host"]), str((res or {}).get("url") or "")
            text = (f"No answer from the {label} within a minute. Is its runner up?" if res is None else f"Could not open it on the {label}: {clip(str(res['error']), 300)}" if res.get("error") else o["ok"])
            web = url.startswith(("https://", "http://")) and not (res or {}).get("error")
            if url and not web and not (res or {}).get("error"): text += f"\n`{url}`"  # Discord link buttons take web links only
            body = {"content": text, "components": [{"type": 1, "components": [{"type": 2, "style": 5, "label": o["link"], "url": url}]}] if web else []}
            try: api("PATCH", f"/webhooks/{self.cfg['bot_id']}/{o['token']}/messages/@original", body, auth=False)
            except RateLimited:
                if now - o["at"] < 840: continue  # the tap's token lasts 15 minutes
            except Exception as e: log("open reply not edited", str(e)[:120])
            del self.st["opens"][rid]
            for f in (done, base / f"{rid}.json"): f.unlink(missing_ok=True)  # handled: a runner that comes up late does not open it then
    def on_interaction(self, i):
        user = (i.get("member") or {}).get("user", i.get("user") or {}).get("id")
        if user != self.owner: return self.respond(i, "Only the server owner can use this.")
        if str((i.get("data") or {}).get("custom_id", "")).startswith("demo:"): return self.demo(i, i["data"]["custom_id"])
        q = queue_root(); here = self.st["dthreads"].get(i.get("channel_id")); here = None if here and here.startswith("session:") else here
        if here and self.st["threads"].get(here, {}).get("archived"):  # Discord refuses interactions in an archived thread
            try: self.reopen(i["channel_id"])
            except Exception as e: log("reopen failed", str(e)[:120])
        if i["type"] == 2:
            name = i["data"]["name"]; o = {x["name"]: x.get("value") for x in i["data"].get("options", [])}
            if name == "status": return self.respond(i, status_text())
            if name == "devices": return self.respond(i, devices_text())
            if name == "progress": return self.respond(i, progress_text())
            if name == "agents": return self.respond(i, "```\n" + mail("agents")[:1900] + "\n```")
            if name == "usage": return self.respond(i, fleet.stats_text(24)[:1900])
            if name == "stop": (q / "STOP").write_text("stopped from Discord\n", encoding="utf-8"); return self.respond(i, "Stopped. Agents, queue and loop are paused until /go.", ephemeral=False)
            if name == "go": (q / "STOP").unlink(missing_ok=True); return self.respond(i, "Resumed.", ephemeral=False)
            if name == "loop":
                act = o.get("action", "status")
                if act == "run" and not o.get("repo"): return self.respond(i, "Name the repo folder, for example Field Notebook.")
                if act == "add":
                    if not (o.get("what") or "").strip(): return self.respond(i, "Say what to loop in `what`, for example: Birch, check onboarding daily.")
                    th = self.loop_request(o["what"])[1]
                    return self.respond(i, f"Sent to the orchestrator: <#{th or self.ch['orchestrator']}>")
                return self.respond(i, tool("improve-loop.py", *([act, o["repo"]] if act == "run" else [act]))[:1900] or "ok", ephemeral=act == "status")
            if name == "run":
                task, where = o["task"], o.get("where", "auto"); up = [d for d in fleet.devices() if d["available"]]
                want = next((d for d in up if d["platform"] == where), None)
                if where != "auto" and not want:
                    key = i["id"]; self.st["pending"][key] = task
                    label = "Mac" if where == "mac" else "PC"
                    row = [{"type": 1, "components": [{"type": 2, "style": 1, "label": f"Run on {d['label']}", "custom_id": f"r:{key}:{d['host']}"} for d in up] + [{"type": 2, "style": 2, "label": "Cancel", "custom_id": f"r:{key}:-"}]}]
                    return self.respond(i, f"The {label} is offline right now. Run it somewhere else?\n> {task[:300]}", ephemeral=False, components=row)
                self.respond(i, f"> {task[:1800]}", ephemeral=False)
                try: origin = api("GET", f"/webhooks/{self.cfg['bot_id']}/{i['token']}/messages/@original", auth=False)["id"]
                except Exception as e: origin = i["id"]; log("could not read the /run message", str(e)[:120])
                return self.hand_over({"d": {"id": origin, "channel_id": i["channel_id"], "content": task, "attachments": []}, "device": want["host"] if want else None})
        if i["type"] == 3:
            kind, key, val = i["data"]["custom_id"].split(":", 2)
            if kind == "c": return self.close_thread(i, here)
            if kind == "o": return self.open_session(i, here, key)
            if kind == "of": return self.open_file(i, here, key, val)
            if kind == "f": return self.feedback(i, here, val)
            if kind == "s": return self.suggested(i, here, key, val)
            if kind == "x": return self.done_elsewhere(i, key)
            if kind == "r":
                task = self.st["pending"].pop(key, None)
                if not task or val == "-": return self.respond(i, "Cancelled." if val == "-" else "That request expired.", update=True, components=[])
                if not fleet.available(val): return self.respond(i, "That device just went offline too.", update=True, components=[])
                self.respond(i, f"> {task[:1800]}", update=True, components=[])
                return self.hand_over({"d": {"id": i["message"]["id"], "channel_id": i["channel_id"], "content": task, "attachments": []}, "device": val})
            qd = self.st["q"].get(key)
            if not qd: self.q_answered(i["channel_id"], i["message"]["id"]); return self.respond(i, clip(self.md_of(i["message"]), 1900) + "\n-# Already answered or expired.", update=True, components=[])
            opt = qd["options"][int(val)]
            if opt.get("device") and not fleet.available(opt["device"]):
                return self.respond(i, f"{opt['label']}: that device is offline now. Pick another.")
            act = opt.get("action") or {}
            if act.get("redirect"): r = act["redirect"]; mail("redirect", r["agent"], r["id"], "--device", r["device"])
            elif not act.get("none"):
                AM.reply("owner", key, opt["label"] + (f" (device {opt['device']})" if opt.get("device") else ""))
            tapped, card = i["message"]["id"], qd.get("msg")
            self.st["q"].pop(key, None); self.q_clear(qd)
            if qd.get("thread"): self.hold_qs(qd["thread"], "Closed: you answered the same question above.", keep=key, asker=qd.get("from"), like=qd)
            self.q_answered(i["channel_id"], tapped, card, *qd.get("extra", []))
            if qd.get("thread") in self.st["threads"]: self.st["threads"][qd["thread"]].pop("turn_answer", None)  # they answered: the next answer is a new message below the question, not an edit of one above it
            if qd.get("thread") in self.st["threads"]: self.st["threads"][qd["thread"]]["owner_at"] = time.time()  # they tapped in it: kept in sight
            if qd.get("thread") in self.st["threads"] and not act.get("none") and (load_agents().get(qd.get("from")) or {}).get("auto"): self.touch(self.st["threads"][qd["thread"]]); self.st["threads"][qd["thread"]]["queued"] = time.time()
            if card and card != tapped:  # they tapped a copy the move had already replaced: the card that stays shows the answer too
                try: self.edit_to(self.q_channel(qd), card, clip(self.q_text(qd), 1800) + f"\n**Answer:** {opt['label']}", components=[])
                except Exception as e: log("question card not updated", str(e)[:120])
            try: return self.respond(i, clip(self.md_of(i["message"]), 1800) + f"\n**Answer:** {opt['label']}", update=True, components=[])
            finally: self.q_done_old(qd)

    # ---- #overview: one card per topic, and the pings in each card's thread ----
    def session_records(self):
        out, d = {}, queue_root() / "sessions"
        for p in d.glob("*/*.json") if d.is_dir() else []:
            try: x = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError): continue
            if isinstance(x, dict) and x.get("session_id"): out[str(x["session_id"])] = x
        return out
    def spawn_label(self):
        """topics.py label in its own process: the model call takes seconds and must never run inside this loop."""
        how = {"creationflags": 0x08000000 | 0x00000200} if os.name == "nt" else {"start_new_session": True}  # no window, own process group
        return subprocess.Popen([sys.executable, "-X", "utf8", str(TK / "topics.py"), "label"], cwd=str(TK), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **how)
    def maybe_janitor(self, now):
        """discord-janitor.py in its own process, like the labelling: a Gemini pass takes a minute."""
        if not hasattr(self, "last_janitor"): self.last_janitor = now - JANITOR_EVERY + 300  # first pass 5 minutes after start
        if not self.cfg.get("janitor") or now - self.last_janitor < JANITOR_EVERY: return
        p = getattr(self, "janitor_proc", None)
        if p is not None and p.poll() is None: return
        self.last_janitor = now
        try: self.janitor_proc = self.spawn_janitor()
        except Exception as e: log("janitor not started", str(e)[:120])
    def spawn_janitor(self):
        how = {"creationflags": 0x08000000 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
        out = open(fleet.host_path(dpath("janitor.out"), hostname()), "a", encoding="utf-8")  # one writer per synced file
        return subprocess.Popen([sys.executable, "-X", "utf8", str(TK / "discord-janitor.py")], cwd=str(TK), stdin=subprocess.DEVNULL, stdout=out, stderr=out, **how)
    def maybe_label(self, groups, now):
        if not any(g.get("unlabelled") for g in groups) or now - self.last_label < LABEL_EVERY: return
        if self.label_proc is not None and self.label_proc.poll() is None: return  # the last one is still going
        self.last_label = now
        try: self.label_proc = self.spawn_label()
        except Exception as e: log("labelling not started", str(e)[:120])
    def ov_link(self, key, r):
        if key.startswith("s:"): return (self.st.get("sessions", {}).get(f"{r.get('host')}/{key[2:]}") or {}).get("thread")
        return (self.st["threads"].get(self.resolve(key[2:])) or {}).get("dthread")
    def ov_who(self, r): return r.get("tool", "") + (f" · {r['where']}" if r.get("where") else "")
    def card_text(self, topic, rows):
        rows = sorted(rows, key=lambda kr: (OV_RANK.get(kr[1]["state"], 4), str(kr[1].get("title", "")).lower()))
        need, work, queued = (sum(r["state"] == s for _, r in rows) for s in ("needs_you", "working", "queued"))
        need = sum(max(1, self.q_count(self.ov_tt(k))) for k, r in rows if r["state"] == "needs_you")
        out = [f"**{clean_title(topic, 60)}**", f"-# 🔴 Need you: {need}  ·  🟡 Working: {work}" + (f"  ·  ⚪ Queued: {queued}" if queued else "")]
        for n, (key, r) in enumerate(rows):
            link = self.ov_link(key, r)
            line = f"{OV_ICON.get(r['state'], '⚪')} `{self.ov_who(r)}` {clean_title(r.get('title'), 70)}" + (f" · {nq} questions" if r["state"] == "needs_you" and (nq := self.q_count(self.ov_tt(key))) > 1 else "") + (f" · <#{link}>" if link else "")
            if dlen("\n".join(out + [line])) > 1850: out.append(f"-# and {len(rows) - n} more"); break
            out.append(line)
        return "\n".join(out)
    def ov_tt(self, key): return self.st["threads"].get(self.resolve(key[2:])) if key.startswith("t:") else None
    def finished(self, t):
        """A mail thread's ending is a finish the owner hears about: nothing asks them or is owed or running, and this turn was
        more than an orchestrator turn of its own (a reply or an idle check)."""
        if self.asks(t) or self.owed(t) or self.active(t) or self.state_word(t) in ("NEEDS YOU", "WORKING", "PAUSED"): return False
        turn = [r for r in self.cur_runs(t) if r["t0"] >= float(t.get("turn_at") or 0) - 5]
        return not turn or any(r.get("agent") != "orchestrator" for r in turn)
    def thread_end(self, key):
        t = self.st["threads"].get(self.resolve(key[2:]))
        return {"FAILED": "failed", "CUT OFF": "cutoff"}.get(self.state_word(t), "done") if t else "done"
    def ping(self, ov, key, r, what, extra=""):
        """Queue one ping for one event. Skipped when the owner is in that session: they typed its prompt within the last minute."""
        if what == "done":
            tt = self.ov_tt(key)
            if tt is not None and not self.finished(tt):
                due = r.get("ping_due"); r["ping_due"] = True
                return None if due else log("ping skipped, not finished", key)  # logged once, not every pass
            if r.get("last_ping") == "done" and not r.get("fresh"): return log("ping skipped, Finished already sent and nothing worked since", key)
            if r.get("kind") == "session" and r.get("loop_pass") and r.get("last_ping") == "done" and r.get("typed", 0) <= r.get("ping_at", 0):
                return log("ping skipped, loop pass with no new prompt", key)  # a /loop session pings once per prompt the owner types, not every pass
        r.pop("ping", None)  # a new event: the earlier mention is no longer the one to carry along
        if r.get("kind") == "session" and time.time() - r.get("typed", 0) < TYPING: return log("ping skipped, the owner is in that session", key, what)
        icon, word = PING_WORDS[what]; link = self.ov_link(key, r)
        text = f"<@{self.owner}> {icon} **{word}** · `{self.ov_who(r)}` {clean_title(r['prompt'] if r.get('kind') == 'session' and r.get('prompt') else r.get('title'), 90)}" + (f" · {extra}" if extra else "") + (f" · <#{link}>" if link else "")
        r.pop("ping_due", None); r["pinged"] = True; r["ping_at"] = time.time(); r["last_ping"] = what; r.pop("fresh", None); ov["pings"].append({"key": key, "topic": r["topic"], "text": text, "at": time.time(), "what": what})
    def overview(self, now):
        """Read the topics view, note what changed since the last pass, then bring the cards and pings in line with it.
        The first pass ever only records, so a start with work already in flight pings for nothing."""
        if "overview" not in self.ch or now - self.last_ov < OV_EVERY: return
        self.last_ov = now; boot = "overview" not in self.st
        ov = self.st.setdefault("overview", {"cards": {}, "items": {}, "pings": []}); items, seen = ov["items"], set()
        last = ov.get("at", now); prev = max(getattr(self, "ov_seen", 0), ov.get("at", 0)) or now; self.ov_seen = now  # the last pass, this process's or the saved moment
        stop = now if now - prev <= OV_GAP else prev  # after a break, a span that has ended since ends where it was last seen working
        def worked(r, end):
            """Close the open working span at END and return the seconds worked so far. Only time in the working state counts."""
            w = r.pop("work", None)
            if w is not None: r["worked"] = r.get("worked", 0) + max(0, min(end, now) - w)
            return r.get("worked", 0)
        groups = topics.overview(); self.maybe_label(groups, now); recs = self.session_records()
        for g in groups:
            for i in g["items"]:
                key, state = i["key"], i["state"]; seen.add(key); x = recs.get(key[2:]) if i["kind"] == "session" else None
                if x and state == "needs_you" and x.get("note_type") == "idle_prompt": state = "idle"  # finished and waiting for the next prompt, as on its session card
                tt = self.st["threads"].get(self.resolve(key[2:])) if i["kind"] != "session" else None
                if self.asks(tt) or self.text_asks(tt): state = "needs_you"  # its question card waits on the owner, whatever the mail's own state
                elif tt and state == "working" and self.state_word(tt) == "DONE": state = "done"  # finished here (Done in #progress) while the mail view still says running
                r = items.setdefault(key, {"seen": now, "state": None}); old = r["state"]
                r.update(topic=g["topic"], tool=i["tool"], where=i["where"], title=tt["title"] if tt and tt.get("epoch") and tt.get("title") else i["title"], host=i.get("host") or "", kind=i["kind"]); r.pop("gone", None); r.pop("miss", None); r.pop("upto", None); r.pop("ping_due", None)
                if x:
                    p = self.clean_prompt(x.get("last_prompt"))
                    if p and p != r.get("prompt"): r["typed"] = min(now, float(x.get("updated") or now))  # a new prompt from the owner; a machine turn leaves last_prompt as it was
                    if p: r["prompt"] = p
                    r["loop_pass"] = str(x.get("last_prompt") or "").lstrip().startswith("/loop")
                if state == old: continue
                r["state"], r["since"] = state, now
                if state == "working": r["work"] = now; r["fresh"] = True; continue  # a span opens; "worked" holds the spans before it, so work handed on or paused for a permission adds up
                total = worked(r, stop)  # waiting on the owner, queued or idle is not work
                if state == "needs_you":
                    if not boot and i["kind"] == "session" and not (x and x.get("note_type") == "remote_permission"): self.ping(ov, key, r, "needs")  # a mail thread's question card has pinged already, and so has a remote permission's
                elif state in ("idle", "ended"):
                    r.pop("worked", None)
                    if not boot and state == "idle" and total >= WORKED_MIN and not i.get("offline"): self.ping(ov, key, r, "done")
        # A missing folder does not mean everything ended. agent-mail makes an empty mail folder again on the next inbox read, so the registry inside it is what is looked for.
        there = (queue_root() / "sessions").is_dir() and (queue_root() / "mail/agents.json").is_file()
        for key, r in list(items.items()) if there else []:
            if key in seen: continue
            if self.asks(self.ov_tt(key)) or self.text_asks(self.ov_tt(key)):  # its question card waits on the owner, whether or not its mail is still in the view
                if r["state"] != "needs_you": worked(r, r.pop("upto", stop)); r["state"], r["since"] = "needs_you", now
                for k in ("gone", "miss", "upto"): r.pop(k, None)
                continue
            if "gone" not in r:
                # a file caught mid-write is not an ending, and neither is a mail thread between two hops: the next agent's mail may come a file sync later
                r.setdefault("upto", stop)  # the wait starts now, also after a break; the work ends where it was last seen
                if now - r.setdefault("miss", now) < (ANSWER_WAIT if key.startswith("t:") else GONE_AFTER): continue
                r["gone"] = now; r.pop("miss"); total = worked(r, r.pop("upto")); r.pop("worked", None)  # it worked until it was last seen, not through the wait
                end = "ended" if r.get("kind") == "session" else self.thread_end(key)
                if end == "done" and total >= WORKED_MIN: self.ping(ov, key, r, "done")
                r["state"] = end
            elif now - r["gone"] < OV_KEEP and r.get("ping_due"): self.ping(ov, key, r, "done")
            elif now - r["gone"] >= OV_KEEP: del items[key]
        for tid, t in self.st["threads"].items():  # failed and cut-off runs, each once
            for run in t["runs"].values():
                kind = run.get("end") if run.get("end") in ("failed", "cutoff") else "cutoff" if not run.get("end") and now - run["t0"] >= RUN_LIMIT else ""
                if not kind or run.get("pinged"): continue
                run["pinged"] = True
                if boot or now - (run.get("t1") or run["t0"] + RUN_LIMIT) > OV_KEEP: continue
                key = next((k for k in ["t:" + tid] + ["t:" + a for a, v in self.st["alias"].items() if v == tid] if k in items), "t:" + tid)
                r = items.get(key)
                if not r: r = items[key] = {"seen": now, "gone": now, "state": kind, "kind": "agents", "tool": "agents", "where": run.get("label", ""), "host": "", "title": t.get("title") or tid,
                                            "topic": (topics.load_labels().get(key) or {}).get("topic") or "Unsorted"}
                if "gone" in r: r["state"] = kind
                self.ping(ov, key, r, kind, f"`{run.get('agent')}` on {run.get('label')}")
        if not any("work" in r for r in items.values()): ov.pop("at", None)
        elif "at" not in ov or now - last >= OV_MARK or now < last: ov["at"] = now  # saved about once a minute, and only while something works, so an idle bridge does not rewrite its state
        by, cards = {}, ov["cards"]; self.move_pings(ov, now)
        for key, r in items.items():
            if r["topic"] == "Unsorted" and not r.get("pinged") and now - r["seen"] < LABEL_WAIT: continue  # give the labelling a moment before a card is made for it
            by.setdefault(r["topic"], []).append((key, r))
        owed = {(items.get(p["key"]) or p)["topic"] for p in ov["pings"] if p.get("what") != "done"}  # a Finished alone never brings a card (and a thread) back
        for topic in sorted(set(by) | set(cards)):
            rows, c = by.get(topic, []), cards.get(topic); live = any(r["state"] in LIVE for _, r in rows)
            if not c:
                if not rows or not (live or topic in owed): continue  # nothing going on: no card to make
                c = cards[topic] = {"live": now}
            if live: c["live"] = now
            if not rows or now - c["live"] >= OV_KEEP:
                if self.drop_topic(ov, c): del cards[topic]
                continue
            text = self.card_text(topic, rows)
            if c.get("id") and c.get("text") == text: continue
            if c.get("id"):
                done = self.edit("overview", c["id"], text)
                if done is False: continue
                if done is None:  # deleted by hand. Discord keeps a thread whose starter message is deleted, so the thread is removed here
                    for key, r in rows:  # an open Needs-you mention in that thread goes out again under the new card
                        p = r.get("ping")
                        if p and p["topic"] == topic and p.get("mid") in c.get("pings", []):
                            r.pop("ping"); ov["pings"].append({"key": key, "topic": topic, "text": p["text"], "at": now, "what": "needs"})
                    self.drop_thread(ov, c)
                    for k in ("id", "pings", "ping", "no_thread"): c.pop(k, None)
            if not c.get("id"): c["id"] = self.send("overview", text)
            c["text"] = text
        self.send_pings(ov, now)
        try: self.drop_orphans(ov, now)  # last, so a rate limit or a network error here holds back no card or ping
        except Exception as e: log("overview threads left behind not removed", type(e).__name__, str(e)[:120])
    def move_pings(self, ov, now):
        """A delivered Needs-you mention sits in its topic's thread. When the item moves to another topic while it still
        needs the owner (a label arrived, and the old topic's thread may go with its card), the mention is queued again for
        the new topic and taken out of the old thread. It is forgotten once the item no longer needs them. The record is
        in the state and is compared by topic, so nothing repeats when nothing moved, or after a restart."""
        items, cards = ov["items"], ov["cards"]
        for key, r in items.items():
            p = r.get("ping")
            if not p: continue
            if r["state"] != "needs_you": r.pop("ping"); continue
            if p["topic"] == r["topic"]: continue
            r.pop("ping"); ov["pings"].append({"key": key, "topic": r["topic"], "text": p["text"], "at": now, "what": "needs"})
            log("ping follows its item to another topic", key, p["topic"], "->", r["topic"])
            c = cards.get(p["topic"]) or {}; mid = p.get("mid")
            where = c.get("thread") if mid in c.get("pings", []) else self.ch["overview"] if mid and c.get("ping") == mid else None
            if not where or not any(x["topic"] == p["topic"] for x in items.values()): continue  # the old card goes anyway, and its thread with it
            try: api("DELETE", f"/channels/{where}/messages/{mid}")
            except Exception as e: log("moved ping not removed from its old topic", str(e)[:100])
            if mid in c.get("pings", []): c["pings"].remove(mid)
            else: c.pop("ping", None)
        for topic, c in cards.items():  # a delivered ping whose item has since moved to another topic is stranded in the old thread
            bk = c.get("by_key") or {}
            for key, mid in list(bk.items()):
                r = items.get(key)
                if not r or r.get("topic") == topic: continue  # unknown item or still in this topic: leave it
                del bk[key]
                if mid in c.get("pings", []):
                    if c.get("thread"):
                        try: api("DELETE", f"/channels/{c['thread']}/messages/{mid}")
                        except Exception as e: log("moved item's old ping not removed", str(e)[:100])
                    c["pings"].remove(mid)
                    log("old ping removed, its item moved topic", key, topic, "->", r["topic"])
    def drop_thread(self, ov, c):
        """Delete a card's ping thread, which needs Manage Threads in #overview. A refusal is logged once and the thread
        is remembered, to be tried again hourly, not on every pass. A rate limit is raised, with the thread still on the card."""
        th = c.get("thread")
        if not th: return
        try: api("DELETE", f"/channels/{th}")
        except DiscordError as e:
            if e.status == 403: self.thread_refused(ov, th, e)
            elif e.code != UNKNOWN_CHANNEL: log("overview thread not deleted", str(e)[:120])
        c.pop("thread")
    def thread_refused(self, ov, th, e):
        if th not in ov.setdefault("orphans", []): ov["orphans"] = (ov["orphans"] + [th])[-50:]
        ov["orphan_at"] = time.time()
        if not ov.get("orphan_logged"): ov["orphan_logged"] = True; log("overview thread not deleted: the bot needs Manage Threads in #overview. Left in place, tried again hourly", th, str(e)[:120])
    def drop_orphans(self, ov, now):
        """Threads whose delete was refused: one try an hour, and the rest only once that one goes through."""
        if not ov.get("orphans") or now - ov.get("orphan_at", 0) < CLOSE_REFUSED_WAIT: return
        for th in list(ov["orphans"]):
            try: api("DELETE", f"/channels/{th}")
            except DiscordError as e:
                if e.status == 403: return self.thread_refused(ov, th, e)
                if e.code != UNKNOWN_CHANNEL: log("overview thread not deleted", str(e)[:120])
            ov["orphans"].remove(th)
        for k in ("orphans", "orphan_at", "orphan_logged"): ov.pop(k, None)
        log("overview threads left behind are removed")
    def drop_topic(self, ov, c):
        """Remove a topic's card and its ping thread. False on a rate limit, so the next pass tries again."""
        try:
            self.drop_thread(ov, c)
            for k in ("ping", "id"):
                if not c.get(k): continue
                try: api("DELETE", f"/channels/{self.ch['overview']}/messages/{c[k]}")
                except DiscordError as e:
                    if e.code != UNKNOWN_MESSAGE: log("overview card not deleted", str(e)[:120])  # forgotten here, so the sweep takes it
                c.pop(k)
        except RateLimited: return False
        return True
    def drop_overview_threads(self, ov):
        """Once: topic threads under #overview are not used unless config "overview_threads" is true. Any left are deleted; pings go to the work's own thread."""
        if self.cfg.get("overview_threads") is True or ov.get("threads_dropped"): return
        for c in (ov.get("cards") or {}).values():
            th = c.pop("thread", None); c.pop("pings", None); c.pop("by_key", None); c["no_thread"] = True
            if th:
                try: api("DELETE", f"/channels/{th}"); log("overview thread deleted", th)
                except Exception as e: log("overview thread not deleted", th, str(e)[:100])
        for th in ov.pop("orphans", []) or []:
            try: api("DELETE", f"/channels/{th}")
            except Exception: pass
        ov["threads_dropped"] = True
    def send_pings(self, ov, now):
        self.drop_overview_threads(ov)
        keep = []
        for p in ov["pings"]:
            if now - p["at"] > PING_KEEP: continue  # too late to be news
            topic = (ov["items"].get(p["key"]) or p)["topic"]; c = ov["cards"].get(topic)
            if not c and p.get("what") == "done": continue  # its card is gone: old news, not worth a new card
            if not c or not c.get("id"): keep.append(p); continue
            try: mid = self.post_ping(topic, c, p["text"], p["key"])
            except RateLimited: keep.append(p); continue
            except Exception as e: log("ping not sent", p["key"], str(e)[:120]); continue
            r = ov["items"].get(p["key"])
            if r and p.get("what") == "needs" and r["state"] == "needs_you": r["ping"] = {"topic": topic, "text": p["text"], "mid": mid}  # so it can follow the item to another topic
        ov["pings"] = keep
    def post_ping(self, topic, c, text, key=None):
        """One new message per event, inside the card's thread, which keeps only the last few. If the thread cannot be
        had, one reply under the card that each new ping replaces. Never a fresh top-level message per event.
        Returns the new message's id."""
        cid = self.ch["overview"]
        if self.cfg.get("overview_threads") is not True:  # no topic threads: the ping goes into the work's own thread
            r = (self.st.get("overview") or {}).get("items", {}).get(key) or {}
            link = self.ov_link(key, r) if key else None
            if link:
                mid = self.send_to(link, text, ping=True)
                prev = c.setdefault("in_work", {}).get(key)
                c["in_work"][key] = [link, mid]
                if prev:  # one notice per item: its newest event replaces the one before
                    try: self.delete_to(prev[0], prev[1])
                    except Exception as e: log("replaced ping not removed", str(e)[:100])
                return mid
            c["no_thread"] = True
        for _ in range(2):
            if not c.get("thread") and not c.get("no_thread"):
                try: c["thread"] = self.open_thread(cid, c["id"], clean_title(topic, 90))
                except DiscordError as e: c["no_thread"] = True; log("overview thread not created, pinging under the card", str(e)[:120])
            if not c.get("thread"): break
            try: mid = self.send_to(c["thread"], text, ping=True)
            except DiscordError as e:
                if e.code != UNKNOWN_CHANNEL: raise
                c.pop("thread"); c.pop("pings", None); continue  # deleted by hand: open it again
            c["pings"] = c.get("pings", []) + [mid]
            last = c.setdefault("by_key", {}); prev = last.get(key) if key else None
            if key: last[key] = mid
            if prev in c["pings"]:  # one notice per item: its newest event replaces the one before
                c["pings"].remove(prev)
                try: api("DELETE", f"/channels/{c['thread']}/messages/{prev}")
                except Exception as e: log("replaced ping not removed", str(e)[:100])
            while len(c["pings"]) > PINGS_SHOWN:
                try: api("DELETE", f"/channels/{c['thread']}/messages/{c['pings'].pop(0)}")
                except Exception as e: log("old ping not removed", str(e)[:100])
            return mid
        old = c.get("ping"); c["ping"] = self.send_to(cid, text, reply_to=c["id"], ping=True)
        if old:
            try: api("DELETE", f"/channels/{cid}/messages/{old}")
            except Exception as e: log("old ping not removed", str(e)[:100])
        return c["ping"]

    # ---- cleaning up old bot messages ----
    def q_answered(self, cid, *mids):
        a = self.st.setdefault("answered", {}); now = time.time()
        for mid in mids:
            if mid: a[mid] = {"ch": cid, "at": now}
        for k in sorted(a, key=lambda k: a[k]["at"])[:max(0, len(a) - 300)]: del a[k]
    def zap(self, cid, mid):
        """Delete one message within this pass's budget. True when it is gone or Discord refuses for good, False when it
        should be tried again. A rate limit ends the whole pass."""
        if self.budget <= 0: return False
        self.budget -= 1
        try: api("DELETE", f"/channels/{cid}/messages/{mid}")
        except DiscordError as e:
            if e.status >= 500: return False
            if e.code not in (UNKNOWN_MESSAGE, UNKNOWN_CHANNEL): log("sweep could not delete", cid, mid, str(e)[:120])
        return True
    def sweep_old(self, now):
        """One bounded cleaning pass, at most every 10 minutes. Silent on rate limits: what is left waits for the next pass."""
        if now - self.last_clean < SWEEP_EVERY: return
        self.last_clean, self.budget = now, SWEEP_MAX
        try:
            for step in (self.sweep_redirects, self.sweep_questions, self.sweep_alerts, self.sweep_burn, self.sweep_pace, self.sweep_progress, self.sweep_untracked, self.sweep_janitor, self.sweep_backfill): step(now)
        except RateLimited: pass
        except Exception as e: log("ERR sweep", type(e).__name__, str(e)[:160])
        try: self.sweep_idle(now)  # on its own, so a rate-limited delete above never keeps finished threads open
        except RateLimited: pass
        except Exception as e: log("ERR idle sweep", type(e).__name__, str(e)[:160])
        try: self.sweep_keep(now)
        except RateLimited as e: log("keep-visible pings rate limited, next pass tries again", str(e)[:80])
        except Exception as e: log("ERR keep sweep", type(e).__name__, str(e)[:160])
        try: self.sweep_audit(now)
        except RateLimited: pass
        except Exception as e: log("ERR audit sweep", type(e).__name__, str(e)[:160])
    def sweep_keep(self, now):
        """Threads the owner started or wrote or tapped in, not closed and still in sight (in_sight: a question open, work not Done,
        or not closed), stay visible: one Discord hid (archived) gets a message sent and deleted at once, which reopens it.
        A fresh one that sat a day without a message gets the same ping to show unread. An old finished one is not re-pinged."""
        if not self.cfg.get("keep_unread", True): return
        gid = self.cfg.get("guild_id")
        if not gid: return
        if not self.st.get("keep_marked"):  # once: threads the owner closed before Close was recorded (only the owner's Close sets status_close False)
            for t in self.st["threads"].values():
                if t.get("archived") and t.get("status_close") is False: t["closed"] = True
            self.st["keep_marked"] = True
        mine = [(tid, t) for tid, t in self.st["threads"].items() if t.get("dthread") and (t.get("origin") or t.get("owner_at")) and not t.get("closed") and not t.get("gone")
                and (t.get("runs") or now - float(t.get("t0") or now) < 86400) and self.in_sight(tid, t, now)]  # never ran for a day: a stray duplicate, not work to keep in sight
        if not mine: return
        seen = getattr(self, "live_threads", None)
        live = seen[1] if seen and seen[0] == now else {th["id"] for th in (api("GET", f"/guilds/{gid}/threads/active") or {}).get("threads") or []}
        due = []
        for tid, t in mine:
            cid = t["dthread"]
            hidden = t.get("archived") or cid not in live
            last = max(t.get("active_at", t.get("t0", 0)), t.get("kept_at", 0))
            if hidden or (self.fresh(tid, t, now) and now - last >= KEEP_UNREAD): due.append((last, tid, t, cid))
        for _, tid, t, cid in sorted(due)[:KEEP_MAX]:
            try:
                mid = self.guard(cid, lambda: self.send_to(cid, "·"))
                t["kept_at"] = now; t.pop("archived", None)
                api("DELETE", f"/channels/{cid}/messages/{mid}")
            except DiscordError as e:
                if e.code == UNKNOWN_CHANNEL: self.forget(cid)
                else: t["kept_at"] = now; log("keep-visible ping failed", tid, str(e)[:120])
            else: log("kept visible", tid, clean_title(t.get("title") or tid, 60))
    def sweep_audit(self, now):
        """Reopen (no message) a tracked thread that is not the owner's, with unfinished work or an open question, that Discord
        shows archived or no longer active. A closed thread is never reopened. The owner's own threads are sweep_keep's."""
        gid = self.cfg.get("guild_id")
        if not gid or not self.st["threads"] or self.cfg.get("audit_hidden") is False: return
        out = self.open_mail()
        if out is None or self.unplaced(): return
        busy = set(out) | {self.resolve(r["thread"]) for r in fleet.active_work(limit=500)}
        asking = {self.resolve(q["thread"]) for q in self.st["q"].values() if q.get("thread")}
        cand = [(tid, t) for tid, t in self.st["threads"].items() if t.get("dthread") and not (t.get("gone") or t.get("closed") or t.get("origin") or t.get("owner_at"))
                and (tid in busy or tid in asking or t.get("asking"))]
        if not cand: return
        seen = getattr(self, "live_threads", None)
        live = seen[1] if seen and seen[0] == now else {th["id"] for th in (api("GET", f"/guilds/{gid}/threads/active") or {}).get("threads") or []}
        n = 0
        for tid, t in cand:
            cid = t["dthread"]
            if not (t.get("archived") or cid not in live): continue
            if n >= AUDIT_MAX: break
            n += 1
            try: self.reopen(cid)
            except DiscordError as e:
                if e.code == UNKNOWN_CHANNEL: self.forget(cid)
                else: log("audit reopen failed", tid, str(e)[:120])
            else: log("audit reopened", tid, clean_title(t.get("title") or tid, 60))
    def sweep_idle(self, now):
        """Close threads the owner no longer needs to see (an ended thread with only Close left is archived by the normal idle rule). A thread the owner started, wrote or tapped in stays until they close it
        (in_sight), and hidden threads with unfinished work are reopened by sweep_audit. Another request thread under #orchestrator is archived once nothing was
        posted in it for 20 minutes, its work shows Done, it has no unfinished mail and no question is open in it (24 hours
        for one showing Failed or Cut off, 60 minutes for a thread there the bridge does not know). A session thread is
        archived after 60 idle minutes. Archiving is reversible: any new message reopens the thread. Nothing is deleted here (sweep_janitor removes superseded janitor review cards and finished permission and device-approval cards).
        With no thread state, or while the inboxes cannot be read, nothing is closed. A close Discord refuses is tried a
        few times, one per pass, then left to Discord's own timer."""
        gid = self.cfg.get("guild_id")
        if not gid or not self.st["threads"]: return
        out = self.open_mail()
        if out is None or self.unplaced(): return  # cannot tell what is unfinished: close nothing
        busy = set(out) | {self.resolve(r["thread"]) for r in fleet.active_work(limit=500)}
        asking = {self.resolve(q["thread"]) for q in self.st["q"].values() if q.get("thread")}
        threads, refused = (api("GET", f"/guilds/{gid}/threads/active") or {}).get("threads") or [], self.st.setdefault("idle_refused", {})
        self.live_threads = (now, {th["id"] for th in threads})  # sweep_keep reuses it: one call per pass
        for gone in set(refused) - {th["id"] for th in threads}: del refused[gone]  # closed or deleted by now: nothing left to count
        for th in threads:
            cid = th["id"]; idle = now - snow_time(th.get("last_message_id") or cid); key = self.st["dthreads"].get(cid) or ""
            t = c = None
            if th.get("parent_id") == self.ch.get("orchestrator"):
                t = None if key.startswith("session:") else self.st["threads"].get(key)
                if not t:
                    if key or idle < TIDY_UNKNOWN: continue  # tracked but its record is gone or of another kind: left alone
                elif (self.in_sight(key, t, now) and (t.get("origin") or t.get("owner_at"))) or key in busy or key in asking or t.get("asking") or (wait := TIDY_WAIT.get(self.state_word(t))) is None: continue
                elif min(idle, now - t.get("active_at", t["t0"])) < wait: continue
            elif th.get("parent_id") == self.ch.get("threads"):
                if idle < TIDY_SESSION: continue
                c = self.st.get("sessions", {}).get(key[8:]) if key.startswith("session:") else None
            else: continue
            if refused.get(cid, 0) >= CLOSE_REFUSED_TRIES: continue
            try:
                name = self.close_name(t, now) if t else None  # a rename still waiting to settle goes out with the close
                if name:
                    try: api("PATCH", f"/channels/{cid}", {"name": name, "archived": True}); self.named(t, name, now)
                    except (RateLimited, DiscordError) as e: log("idle thread closed without the rename, name follows later", key, str(e)[:120]); name = None
                if not name: api("PATCH", f"/channels/{cid}", {"archived": True})
            except DiscordError as e:
                if e.code == UNKNOWN_CHANNEL: self.forget(cid)
                elif e.code != THREAD_ARCHIVED:
                    if e.status < 500: refused[cid] = refused.get(cid, 0) + 1  # a Discord outage is not a refusal
                    log("gave up closing idle thread, left open" if refused.get(cid, 0) >= CLOSE_REFUSED_TRIES else "idle thread not closed", cid, f"refusal {refused.get(cid, 0)} of {CLOSE_REFUSED_TRIES}", str(e)[:120])
                continue
            refused.pop(cid, None)
            if t: t["archived"] = True
            if c: c["archived"] = True
            log("idle thread closed", cid, str(th.get("name") or "")[:70], f"idle {idle / 60:.0f} min")
    def sweep_redirects(self, now):
        """The owner's board messages that could not be removed when they were redirected, and the reply that linked their thread."""
        red = self.st.get("redirects", {})
        for mid, r in list(red.items()):
            if now - r["at"] > REDIRECT_KEEP: del red[mid]; continue
            if r.get("keep"): continue  # a file of it was not saved: the original is never removed here
            if self.budget <= 0: return
            self.budget -= 1
            try: api("DELETE", f"/channels/{r['ch']}/messages/{mid}")
            except DiscordError as e:
                if e.code == MISSING_PERMISSIONS: return  # still no Manage Messages: the same answer for all of them
                if e.code not in (UNKNOWN_MESSAGE, UNKNOWN_CHANNEL): log("redirected message not removed", str(e)[:120]); continue
            if r.get("reply"): self.zap(r["ch"], r["reply"])  # if this fails the reply is untracked from here on, and goes with those
            del red[mid]
    def sweep_questions(self, now):
        a = self.st.get("answered", {})
        for mid, x in list(a.items()):
            if now - x["at"] >= Q_KEEP and self.zap(x["ch"], mid): del a[mid]
    def sweep_alerts(self, now):
        """A failed run's alert goes once it is 6 hours old and either a newer alert says the same or its work is no longer failed."""
        al, newest = self.st.get("alerts", {}), {}
        for a in al.values(): newest[a["key"]] = max(newest.get(a["key"], 0), a["at"])
        for mid, a in list(al.items()):
            if now - a["at"] < ALERT_KEEP: continue
            t = self.st["threads"].get(a.get("tid") or "")
            if (a["at"] < newest[a["key"]] or not t or self.state_word(t) not in ("FAILED", "CUT OFF")) and self.zap(self.ch["alerts"], mid): del al[mid]
        for slot, n in list(self.st.get("notices", {}).items()):  # a replaceable alert whose problem has not come up for 6 hours
            if slot.startswith("alert:") and now - self.st.get("alert_last", {}).get(slot[6:], n["at"]) >= ALERT_KEEP and self.budget > 0: self.budget -= 1; self.clear_notice(slot)
    def sweep_burn(self, now):
        """One alert when a request in progress passes config "burn_alert" dollars since the owner's last message there. A notice, not a cap."""
        try:
            limit = float(self.cfg.get("burn_alert", 20) or 0)
            if limit <= 0: return
            rows = fleet.stats(72)
            for tid, t in self.st["threads"].items():
                if not self.active(t): continue
                since, real = float(t.get("turn_at") or t.get("t0") or 0) - 5, self.resolve(tid)
                mine = [r for r in rows if r.get("thread") == real and float(r.get("ts", 0)) > since]
                cost = sum(float(r.get("cost") or 0) for r in mine)
                if cost >= limit:
                    wk = fleet.week_used(mine).get("claude")
                    what = f"{fleet.pct(wk)} of Claude's week" if wk is not None else f"${cost:.0f}"
                    self.alert("burn:" + tid, f"**{clean_title(t.get('title') or tid, 70)}** has used {what} in {len(mine)} runs since your last message there." + (f"  ·  <#{t['dthread']}>" if t.get("dthread") else ""), tid=tid)
        except Exception as e: log("ERR burn sweep", str(e)[:120])
    def sweep_pace(self, now):
        """One #alerts notice per week when Claude's weekly limit is on pace for config "pace_alert" percent at reset."""
        try:
            limit = float(self.cfg.get("pace_alert", 100) or 0)
            if limit <= 0: return
            p = fleet.week_pace(now)
            if not (p and p["projected"] is not None and p["projected"] >= limit and p["used"] >= 20): return
            week = fleet.WEEK; spent = p["elapsed"] / 100 * week  # seconds since the week began
            runout = now + (limit - p["used"]) * spent / p["used"]
            if runout > p["resets"] - 3600: return  # it would not reach the limit at least an hour before the reset
            k = int(p["resets"])
            if self.st.get("pace_alerted") == k: return
            self.alert("pace", f"Claude's weekly limit is on pace for {p['projected']:.0f}% at reset and would reach {limit:.0f}% <t:{int(runout)}:R>, before it resets <t:{k}:R>: {p['used']:.0f}% used with {p['elapsed']:.0f}% of the week gone.")
            self.st["pace_alerted"] = k
        except Exception as e: log("ERR pace sweep", str(e)[:120])
    def sweep_progress(self, now):
        for t in self.st["threads"].values():
            if not t.get("prog") or self.state_word(t) not in ("DONE", "FAILED", "CUT OFF"): continue
            last = max([t.get("active_at", t["t0"])] + [r.get("t1", r["t0"]) for r in t["runs"].values()])
            if now - last >= PROG_KEEP and self.zap(self.ch["progress"], t["prog"]): t.pop("prog")  # prog_text stays, so only new work posts a card again
    def tracked(self):
        """Every message id the state still points at. A bot message in a board channel that is not here is left over."""
        st, ids = self.st, set()
        for t in st["threads"].values(): ids |= {t.get(k) for k in ("prog", "card", "anchor", "status", "links_msg")}
        ids |= {c.get("id") for c in st.get("sessions", {}).values()} | {n["id"] for n in st.get("notices", {}).values()}
        ids |= set(st.get("alerts", {})) | set(st.get("report_msgs", [])) | set(st.get("answered", {}))
        for c in st.get("overview", {}).get("cards", {}).values(): ids |= {c.get("id"), c.get("ping")}
        ids |= set(st.get("boards", {}).get("ids", [])) | set(st.get("loops", {}).get("ids", []))
        ids |= {r.get("reply") for r in st.get("redirects", {}).values()} | {r.get("notice") for r in st.get("unsent", {}).values()}
        for q in st["q"].values(): ids |= {q.get("msg"), *q.get("extra", [])}
        for p in st.get("posted", {}).values(): ids |= {p.get("long"), p.get("msg"), *p.get("msgs", [])}
        return ids
    def sweep_untracked(self, now):
        """Walk each board channel a page per pass, newest first, and remove the bot's own messages the state no longer
        tracks. Only messages written by this bot, only ordinary ones, never one from the last hour. Reports posted
        before reports were tracked are adopted on the first walk instead of removed."""
        me = self.cfg.get("bot_id")
        if not me: return
        sw = self.st.setdefault("sweep", {}); cur = sw.setdefault("cursor", {}); tracked = self.tracked()
        for key in BOARDS:
            cid = self.ch.get(key)
            if not cid or self.budget <= 0: continue
            rows = api("GET", f"/channels/{cid}/messages?limit=100" + (f"&before={cur[key]}" if cur.get(key) else "")) or []
            done = True
            for m in rows:
                if (m.get("author") or {}).get("id") != me or int(m.get("type") or 0) not in (0, 19) or m["id"] in tracked: continue
                if now - snow_time(m["id"]) < UNTRACKED_GRACE: continue
                if key == "reports" and not sw.get("reports_adopted"): self.keep_report(m["id"]); continue
                if self.budget <= 0: done = False; break
                if not self.zap(cid, m["id"]): done = False
            if not done: continue  # an unfinished page is read again next pass
            if len(rows) >= 100: cur[key] = min((m["id"] for m in rows), key=int)
            else:  # the end of the channel: start again from the newest
                cur.pop(key, None)
                if key == "reports": sw["reports_adopted"] = True
    def remove_card(self, tid, t, what):
        """Delete a finished card's #orchestrator anchor and thread. False when the budget is spent or Discord refused (a 403 is logged once)."""
        if self.budget <= 0: return False
        self.budget -= 1
        try:
            if t.get("anchor"):
                try: api("DELETE", f"/channels/{self.ch['orchestrator']}/messages/{t['anchor']}")
                except DiscordError as e:
                    if e.code not in (UNKNOWN_MESSAGE, UNKNOWN_CHANNEL): raise
            try: api("DELETE", f"/channels/{t['dthread']}")
            except DiscordError as e:
                if e.code != UNKNOWN_CHANNEL: raise
        except DiscordError as e:
            if e.status == 403 and not t.get("janitor_403"): t["janitor_403"] = True; self.dirty.add(tid); log(f"{what} not removed (403)", tid)
            elif e.status != 403: log(f"{what} not removed", tid, str(e)[:120])
            return False
        self.forget(t["dthread"]); return True
    def sweep_janitor(self, now):
        """Janitor critic mails each open a card in #orchestrator. Keep the newest; remove older finished ones nobody touched.
        Also removes finished one-off cards (device approvals, permission prompts) idle for ARCHIVE_AFTER, unless a question
        is open, the owner spoke in them, or mail is still open. Finished loop-cycle cards (same title per cycle) keep one open or newest card
        per title; older ones go on the same terms."""
        cands = [(t.get("t0", 0), tid) for tid, t in self.st["threads"].items() if t.get("dthread") and not t.get("gone") and JANITOR_TITLE.match(str(t.get("title") or ""))]
        for _, tid in sorted(cands)[:-1]:
            t = self.st["threads"][tid]
            if not self.closeable(tid, t) or self.asks(t) or t.get("origin") or t.get("owner_at"): continue
            if now - max(t.get("last_end", 0), t.get("active_at", 0)) < ARCHIVE_AFTER: continue
            if self.budget <= 0: return
            if self.remove_card(tid, t, "janitor card"): log("old janitor card removed", tid)
        for tid, t in list(self.st["threads"].items()):
            if not t.get("dthread") or t.get("gone") or not ONE_OFF_TITLE.match(str(t.get("title") or "")): continue
            if self.state_word(t) != "DONE" or any(q.get("thread") == tid for q in self.st["q"].values()): continue
            if self.asks(t) or t.get("origin") or t.get("owner_at"): continue
            out = self.open_mail()
            if out is None or tid in out: continue
            if now - max(t.get("last_end") or 0, t.get("active_at") or 0, t.get("t0") or 0) < ARCHIVE_AFTER: continue
            if self.budget <= 0: return
            if self.remove_card(tid, t, "one-off card"): log("old one-off card removed", tid)
        groups = {}
        for tid, t in self.st["threads"].items():
            if t.get("dthread") and not t.get("gone") and LOOP_TITLE.match(str(t.get("title") or "")): groups.setdefault(t["title"], []).append(tid)
        for tids in groups.values():
            keep = {tid for tid in tids if self.asks(self.st["threads"][tid]) or self.state_word(self.st["threads"][tid]) != "DONE" or any(q.get("thread") == tid for q in self.st["q"].values())}
            if not keep: keep = {max(tids, key=lambda i: self.st["threads"][i].get("t0") or 0)}
            for tid in tids:
                t = self.st["threads"][tid]
                if tid in keep or not self.closeable(tid, t) or self.asks(t) or t.get("origin") or t.get("owner_at"): continue
                if now - max(t.get("last_end") or 0, t.get("active_at") or 0, t.get("t0") or 0) < ARCHIVE_AFTER: continue
                if self.budget <= 0: return
                if self.remove_card(tid, t, "loop cycle card"): log("old loop cycle card removed", tid)
    def sweep_backfill(self, now):
        """Once: find answered question cards from before they were remembered, a few threads per pass. Reading an archived thread does not open it."""
        sw = self.st.setdefault("sweep", {}); me = self.cfg.get("bot_id")
        if sw.get("backfilled") or not me: return
        seen = sw.setdefault("scanned", [])
        todo = [c for c in [self.ch["orchestrator"]] + [c for c, tid in self.st["dthreads"].items() if not tid.startswith("session:")] if c not in seen]
        for cid in todo[:SWEEP_SCANS]:
            try: rows = api("GET", f"/channels/{cid}/messages?limit=100") or []
            except DiscordError: rows = []
            for m in rows:
                text = m.get("content") or ""
                if (m.get("author") or {}).get("id") == me and not m.get("components") and re.match(r"<@\d+> \*\*[^*\n]+\*\* asks:", text) and ("\n**Answer:** " in text or "\n-# Already answered or expired." in text):
                    self.st.setdefault("answered", {}).setdefault(m["id"], {"ch": cid, "at": snow_time(m["id"])})
            seen.append(cid)
        if len(todo) <= SWEEP_SCANS: sw["backfilled"] = True; sw.pop("scanned", None)

    def tick(self):
        now = time.time()
        if now - self.last_tick < 2: return
        self.last_tick = now; self.open_cache = None
        # Two events can carry the same timestamp (a coarse clock, as on Windows, or two writers): read from the cursor
        # inclusively and remember which events at exactly that time were handled, so neither hides the other.
        for e in fleet.events(self.st["ev_ts"], inclusive=True):
            if e["ts"] == self.st["ev_ts"]:
                if e.get("id") in self.st.setdefault("ev_seen", []): continue
            else: self.st["ev_ts"] = e["ts"]; self.st["ev_seen"] = []
            self.st.setdefault("ev_seen", []).append(e.get("id"))
            try: self.on_event(e)
            except Exception as x: log("ERR event", x)
        for m in json.loads(mail("inbox", "owner", "--json", check=False) or "[]"):
            try: self.on_mail(m)
            except RateLimited as x: log("mail waits for rate limit", m.get("id"), x); break
            except Exception as x: log("ERR mail", m.get("id"), x)
        if now - self.last_q >= 10: self.last_q = now; self.refresh_questions()
        for kind in ("done", "failed"):
            d = queue_root() / kind
            for p in sorted(d.glob("*.report.md")) if d.is_dir() else []:
                if p.name in self.st["reports"] or p.stat().st_mtime < self.st.get("started", now): continue
                try: self.post_report(p)
                except Exception as x: log("ERR report", p.name, x); continue
                self.st["reports"] = (self.st["reports"] + [p.name])[-300:]
        self.retry_unsent(now)
        try: self.web_prompts()
        except Exception as x: log("ERR web prompts", str(x)[:160])
        self.maybe_janitor(now)
        self.drain_system()
        try: self.backfill_projects()
        except Exception as x: log("ERR project backfill", str(x)[:160])
        if self.lock_check != set():
            try: self.unlock_names()
            except Exception as x: log("ERR name locks", str(x)[:160])
        if self.cfg.get("progress_boards"):
            try: self.render_boards(now)
            except RateLimited: pass
            except Exception as x: log("ERR boards", str(x)[:160])
        try: self.render_loops(now)
        except RateLimited: pass
        except Exception as x: log("ERR loops board", str(x)[:160])
        try: self.ensure_argon(now); self.argon_poll(now)
        except Exception as x: log("ERR argon chat", str(x)[:160])
        try: self.card_sweep(now)
        except Exception as x: log("ERR card sweep", str(x)[:160])
        try: self.refresh_step(now)
        except Exception as x: log("ERR refresh", str(x)[:160])
        try: self.smart_poll(now)
        except Exception as x: log("ERR smart poll", str(x)[:160])
        try: self.triage_poll(now)
        except Exception as x: log("ERR triage poll", str(x)[:160])
        if now - getattr(self, "last_live", 0) >= 5:  # a running agent's live file changed: its card is edited, at most once per LIVE_EVERY per thread
            self.last_live = now
            for tid, t in self.st["threads"].items():
                if now - t.get("live_edit", 0) < LIVE_EVERY: continue
                for ref, r in t["runs"].items():
                    if r.get("end") or now - r["t0"] >= RUN_LIMIT: continue
                    d = self.live(ref)
                    if not d or d.get("at") == (r.get("live") or {}).get("at"): continue
                    r["live"] = {k: d.get(k) for k in ("doing", "trail", "says", "at")}; r.update({k: str(d[k]) for k in ("session", "cwd") if d.get(k)})
                    t["live_edit"] = now; self.dirty.add(tid)
        try: self.pump_opens(now)
        except Exception as x: log("ERR open requests", str(x)[:160])
        if self.cfg.get("notepad"):
            try: self.flush_notes(now)
            except Exception as x: log("ERR notes", str(x)[:160])
        try: self.pump_shots(now)
        except Exception as x: log("ERR screenshots", str(x)[:160])
        if "threads" in self.ch: self.sessions(now)
        try: self.overview(now)
        except RateLimited: pass  # what changed is already recorded; cards and pings go out on a later pass
        except Exception as x: log("ERR overview", type(x).__name__, str(x)[:160])
        self.sweep_old(now)
        try: self.sync_owed(now)
        except Exception as x: log("ERR owed mail", str(x)[:160])
        if now - getattr(self, "last_qline", 0) >= 10:  # queue position moves without any event for the waiting thread
            self.last_qline = now
            self.dirty |= {tid for tid, t in self.st["threads"].items() if not t.get("gone") and (t.get("origin") or t.get("mailed")) and self.state_word(t) == "QUEUED" and now - (t.get("queued") or t.get("owed_at") or t.get("t0") or 0) < 6 * 3600}
        sweep = now - self.last_sweep >= SWEEP
        if sweep: self.last_sweep = now
        for tid, t in self.st["threads"].items():
            if t.get("done_at") and now - t["done_at"] >= ANSWER_WAIT: t.pop("done_at"); self.dirty.add(tid)  # no answer came: the outcome is shown now
            if t.get("ended_at") and now - t["ended_at"] >= DONE_GRACE: t.pop("ended_at"); self.dirty.add(tid)  # nothing was handed on: Done and Close show now
        self.dirty |= {tid for tid, t in self.st["threads"].items() if any(self.resolve(l) in self.dirty for l in t.get("linked", []))}  # a conflict card follows its linked threads
        for tid, t in list(self.st["threads"].items()):
            if sweep: self.check_stuck(tid, t)
            if tid in self.dirty or (sweep and (t.get("dthread") or self.active(t) or t.get("retry"))):
                try: self.render(tid); self.dirty.discard(tid)
                except RateLimited: pass  # stays dirty, next pass tries again
                except Exception as x: log("ERR render", tid, x); self.dirty.discard(tid)
            if t.get("name_want") and t.get("dthread") and now - t.get("name_t", now) >= RENAME_SETTLE:  # a settled name goes out now, not at the next render
                try: self.sync_name(tid, t, now)
                except Exception as x: log("ERR rename", tid, x)
        self.dirty &= set(self.st["threads"])
        self.prune()
        snap = json.dumps(self.st, sort_keys=True)
        if snap != self.saved: self.save_state(); self.saved = snap
        self.publish_status(now, True); self.heal_lost(now); self.health_step(now)
        self.maybe_reload(now)
    def health_step(self, now):
        """Once per start: one tiny triage job through Gemini on this device. If it does not answer, an alert says so,
        with the reasons, instead of the assistant silently doing nothing (a model that reaches for a tool returns no answer)."""
        if getattr(self, "health", None) == "done" or not self.cfg.get("triage"): return
        d = dpath("health"); d.mkdir(parents=True, exist_ok=True); f = d / f"h-{hostname()}.json"; out = f.with_name(f.stem + ".out.json")
        if not getattr(self, "health", None):
            if now - self.st.get("started", now) < 60: return
            out.unlink(missing_ok=True)
            f.write_text(json.dumps({"text": "is it running?", "thread": "", "status": "Working", "history": "", "others": ""}), encoding="utf-8")
            try: self.triage_spawn(f); self.health = ("started", now)
            except Exception as e: self.health = "done"; log("Gemini health check not started", str(e)[:100])
            return
        if out.exists():
            try: res = json.loads(out.read_text(encoding="utf-8"))
            except (OSError, ValueError): res = {}
            self.health = "done"
            if res.get("action"): return log("Gemini health check OK", res.get("model"), res.get("secs"))
            return self.alert(f"gemini-{hostname()}", f"**The assistant (Gemini) is not answering on {hostname()}.** Triage, buttons and tidying fall back to defaults.\n-# " + clip("; ".join(res.get("fails") or [res.get("error") or "no result"]), 600))
        if now - self.health[1] > 180:
            self.health = "done"; self.alert(f"gemini-{hostname()}", f"**The assistant (Gemini) did not answer within 3 minutes on {hostname()}.**")
    def heal_lost(self, now):
        """'Shown in Discord' must stay true: every 10 minutes, the parts of answers from the last 2 hours are checked, and
        an answer whose messages are gone without the bridge having removed them (a second bridge, a stray delete) is
        posted again, once, with a note. Removals the bridge makes scrub the id from posted first, so they never come back."""
        if now - getattr(self, "heal_at", now - 300) < 600: return
        self.heal_at = now; checked = 0
        for aid, p in list(self.st.get("posted", {}).items()):
            if checked >= 12: break
            if not p.get("done") or not p.get("msgs") or p.get("healed") or p.get("thread") not in self.st["threads"]: continue
            age = now - float(p.get("at") or 0)
            if age > 7200 or age < 120: continue
            checked += 1
            try: api("GET", f"/channels/{p.get('channel')}/messages/{p['msgs'][-1]}"); continue
            except DiscordError as e:
                if e.code != UNKNOWN_MESSAGE: continue
            except Exception: continue
            m = AM.find_anywhere(aid)
            if not m: continue
            log("answer went missing in Discord, posting it again", aid)
            p.update(msgs=[], files=0, done=False, healed=True); p.pop("tail", None); p.pop("file_list", None)
            t = self.st["threads"][p["thread"]]; t.pop("turn_answer", None)
            self.on_answer({**m, "body": str(m.get("body") or "") + "\n-# Posted again: this answer had gone missing."}, p["thread"])
            self.st["posted"][aid]["healed"] = True
    def publish_status(self, now, active):
        """Queue/discord/status-<host>.json every minute: serving or standby, the code it runs, the last log lines. The
        other device (and the self-improvement loop) can see what this bridge is doing without remote control."""
        if now - getattr(self, "status_at", 0) < 60: return
        self.status_at = now
        try:
            code = hashlib.sha1((TK / "discord-bridge.py").read_bytes()).hexdigest()[:10]
            save(f"status-{hostname()}.json", {"host": hostname(), "at": now, "active": active, "code": code, "ran": getattr(self, "code_ran", None), "log": LOG_TAIL[-30:]})  # ran: code_hash() this process started on
        except Exception as e: print("status not written", e)
    def maybe_reload(self, now):
        """New code synced in: once it has stopped changing for 20 s and parses, save and exit, and the supervisor (a
        launchd agent, a scheduled task or any process manager) starts the bridge again on it. Deploys then need no remote
        control of the other device."""
        if self.cfg.get("self_reload") is False or now - getattr(self, "code_check", 0) < 15: return
        self.code_check = now
        sig = tuple((f, (TK / f).stat().st_mtime) for f in RELOAD_FILES if (TK / f).exists())
        if not hasattr(self, "code_start"): self.code_start, self.code_ran = sig, code_hash(); return
        if sig == self.code_start: return
        if getattr(self, "code_seen", None) != sig: self.code_seen, self.code_since = sig, now; return  # still syncing: wait until it settles
        if now - self.code_since < 20: return
        try:
            for f in RELOAD_FILES:
                if (TK / f).exists(): compile((TK / f).read_text(encoding="utf-8"), f, "exec")
        except SyntaxError as e:
            log("new code does not parse, staying on the old one", str(e)[:120]); self.code_start = sig; return
        h = code_hash()
        if h == self.code_ran: self.code_start = sig; return  # touched, not changed
        if not dpath(f"tested-{h}.ok").exists():  # code that only compiles can still crash at start, on both machines at once
            if getattr(self, "untested", None) != h: self.untested = h; log(f"new code {h} has no tested-{h}.ok marker (discord-bridge-test.py writes it after a full pass); staying on the old one")
            return
        if self.is_active(): self.save_state()  # a standby never writes state while another bridge serves
        log("code changed on disk: restarting on the new code"); sys.exit(0)

    def keep_report(self, mid):
        """Track a report message so the sweep leaves it. The newest 300 are kept, by the age in the id, whatever order they were added in."""
        self.st["report_msgs"] = sorted(set(self.st.get("report_msgs", [])) | {mid}, key=int)[-300:]
    def post_report(self, p):
        text = p.read_text(encoding="utf-8", errors="replace"); cid = self.ch["reports"]
        keep = self.keep_report
        if dlen(text) <= LIMIT: return keep(self.send_to(cid, text))
        keep(self.send_to(cid, split_message(text)[0] + "\n-# Full report attached.")); keep(self.upload_to(cid, p.name, text.encode("utf-8")))
    def prune(self):
        st = self.st
        if len(st["threads"]) > 150:
            linked = {self.resolve(l) for t in st["threads"].values() for l in t.get("linked", [])}  # a conflict card still shows these
            done = [k for k, t in st["threads"].items() if not self.active(t) and not t.get("asking") and not t.get("owed") and k not in linked]
            for k in sorted(done, key=lambda k: st["threads"][k].get("active_at", st["threads"][k]["t0"]))[:len(st["threads"]) - 100]: del st["threads"][k]
        if len(st["dthreads"]) > 1200: st["dthreads"] = dict(list(st["dthreads"].items())[-1000:])  # outlives threads, so an old thread can still be answered in
        if len(st["q"]) > 60: st["q"] = dict(list(st["q"].items())[-40:])

    def lease_get(self):
        """The bridge lock: a message 'host|epoch' in a private thread (config lease_msg), read straight from Discord, so
        both devices see the same thing at once. src "error" when that read fails, "file" when no lock message is set up;
        both carry the newest synced lease-<host>.json (or legacy lease.json) of another host, which arrives late."""
        spot = load("config.json", {}).get("lease_msg") or self.cfg.get("lease_msg")
        if spot:
            try:
                host, at = (api("GET", f"/channels/{spot[0]}/messages/{spot[1]}").get("content") or "|0").split("|", 1)
                return {"host": host, "at": float(at), "src": "discord"}
            except Exception: pass
        me, best = hostname().lower(), {}
        for p in [dpath("lease.json"), *sorted(dpath("").glob("lease-*.json"))]:
            try: x = json.loads(p.read_text(encoding="utf-8")); at = float(x.get("at") or 0)
            except (OSError, ValueError, TypeError, AttributeError): continue
            if str(x.get("host") or "").lower() != me and at > float(best.get("at") or 0): best = {"host": x.get("host"), "at": at}
        return {**best, "src": "error" if spot else "file"}
    def lease_put(self, now):
        spot = load("config.json", {}).get("lease_msg") or self.cfg.get("lease_msg")
        if spot:
            body = {"content": f"{hostname()}|{now:.0f}"}  # plain text on purpose: never a card
            try: api("PATCH", f"/channels/{spot[0]}/messages/{spot[1]}", body)
            except DiscordError as e:
                if e.code == 50083:  # Discord archived the quiet thread after 7 days: reopen it and write again
                    try: api("PATCH", f"/channels/{spot[0]}", {"archived": False}); api("PATCH", f"/channels/{spot[0]}/messages/{spot[1]}", body)
                    except Exception as e2: log("discord lease not renewed", str(e2)[:100])
                else: log("discord lease not renewed", str(e)[:100])
            except Exception as e: log("discord lease not renewed", str(e)[:100])
        self.lease_at = now
        try: save(f"lease-{hostname()}.json", {"host": hostname(), "at": now, "state": getattr(self, "state_saved", 0)})  # one file per host; "state": peer_synced: two writers of one synced file made conflict copies
        except Exception as e: log("lease not renewed", str(e)[:100])
    def peer_down(self, host):
        """True only when the optional liveness relay confirms HOST is down (fleet.confirmed_down, the rule the runners and
        the loop use): this device's own push reached the relay under a minute ago, the relay has run for 2 minutes, and HOST
        has not pushed for 2 minutes or reports a stale heartbeat. A missing, old or unreadable reading is False, so without
        a relay a standby never takes over on synced files alone."""
        return fleet.confirmed_down(host, PEERS, missing=False)
    def is_active(self, cached=False):
        """cached: the gateway loop asks every second, so it reuses an answer up to 10 s old; see decide_active."""
        now = time.time()
        if cached and now - getattr(self, "active_at", 0) < 10: return self.active_now
        was = getattr(self, "active_now", None)
        self.active_at = now; self.active_now = self.decide_active()
        if self.active_now and not was: self.became_active(was is False)
        return self.active_now
    def became_active(self, switched=True):
        """Taking over or taking the bridge back: every host's state is merged first, so what the other bridge saved
        while this one stood by or was offline is kept, not overwritten by this device's older copy."""
        self.reclaim_t = None; self.st = {**self.st, **self.load_state()}
        if switched: log("serving now: merged the state every device saved")
    def peer_synced(self, host, lease, live, now):
        """The primary takes the bridge back only once file sync has brought the serving host's state: its
        state.<host>.json is at least as new as the state its last renewal in lease-<host>.json names, and with the live
        lock that renewal is at most LEASE_STALE older than the lock. Otherwise a primary back after a long time offline
        would take the lock at once and save its old state before the standby's newer one had arrived. Waits RECLAIM_WAIT at most.
        Not synced either: a lease file without "state" (missing, or from an older bridge), and no state file
        for HOST while its lease is fresh (it is serving, so it has saved one)."""
        lf, sf = load(f"lease-{host}.json", {}), fleet.host_path(dpath("state.json"), host)
        try: file_at, want, at = float(lf.get("at") or 0), float(lf["state"]), float(lease.get("at") or 0)
        except (KeyError, TypeError, ValueError, AttributeError): want = None
        if want is not None and (not live or file_at >= at - LEASE_STALE) and fleet.saved_at(sf) >= want and (sf.exists() or now - at >= LEASE_STALE): return True
        if not getattr(self, "reclaim_t", None):
            self.reclaim_t = now; log(f"primary: waiting up to {RECLAIM_WAIT // 60} min for {host}'s state to sync before taking the bridge back")
        return now - self.reclaim_t >= RECLAIM_WAIT
    def decide_active(self):
        """Serve only while this is the first host of the list seen within 5 minutes AND no other host holds a fresh
        lease. The serving bridge renews Queue/discord/lease.json every LEASE_RENEW seconds, so a primary whose device
        heartbeat runs late keeps serving and the standby never starts a second copy (two bridges would post every ping
        twice and their synced state files would clash). Handing back: the standby stops renewing once the primary is
        seen again, and the primary takes over when that lease is LEASE_STALE old.
        Fails closed: a standby that cannot read the Discord lock never takes over on synced files alone (an offline device
        sees every file as old); it also needs the optional liveness relay to confirm the primary down (peer_down). The
        primary never asks the relay, so a relay outage changes nothing while Discord works."""
        me, now = hostname().lower(), time.time()
        hosts = self.cfg.get("hosts") or [hostname()]; primary = str(hosts[0]).lower()
        lease = self.lease_get(); holder = str(lease.get("host") or "").lower(); live = lease.get("src") == "discord"
        try: age = now - float(lease.get("at") or 0)
        except (TypeError, ValueError): age = TAKEOVER_STALE
        if me != primary:  # the standby serves only once the primary's lock has gone stale: 150 s read live from Discord, 10 minutes from a synced file
            if holder and holder != me and -SKEW <= age < (DISCORD_TAKEOVER if live else TAKEOVER_STALE): return False
            if not live and (fleet.pick_host(hosts, fresh=TAKEOVER_STALE) or "").lower() != me: return False
            if not live and not self.peer_down(primary):
                if getattr(self, "held_log", 0) < now - 600: self.held_log = now; log("standby: no live Discord lock and the relay does not confirm", primary, "down; not taking over")
                return False
        elif not live and holder and holder != me and -SKEW <= age < LEASE_STALE: return False  # without Discord, the primary waits out the standby's file lease
        if me == primary and holder and holder != me and not self.peer_synced(lease.get("host"), lease, live, now): return False  # its state first
        # the primary with Discord takes its lock straight back; the standby sees that within 10 s and stops
        if (holder != me or age >= LEASE_RENEW) if live else now - getattr(self, "lease_at", 0) >= LEASE_RENEW: self.lease_put(now)
        return True

    def alert(self, key, text, replace=True, tid=""):
        """A repeat of the same problem edits its earlier alert. A failed run's alert (replace=False) is its record until
        the sweep finds it resolved or repeated. alert_last is when the problem was last seen, raised or not."""
        seen = self.st.setdefault("alerted", {}); now = time.time()
        last = self.st.setdefault("alert_last", {}); last[key] = now
        for k in [k for k, v in last.items() if now - v > 86400 and "alert:" + k not in self.st.get("notices", {})]: del last[k]
        if now - seen.get(key, 0) < 6 * 3600: return
        seen[key] = now
        for k in [k for k, v in seen.items() if now - v > 86400]: del seen[k]
        if not replace:
            mid = self.send("alerts", text, card=True); al = self.st.setdefault("alerts", {}); al[mid] = {"key": key, "tid": tid, "at": now}
            for k in sorted(al, key=lambda k: al[k]["at"])[:max(0, len(al) - 200)]: del al[k]
            return mid
        slot = "alert:" + key
        self.notice(slot, self.ch["alerts"], text + (f"\n-# Repeated <t:{int(now)}:R>" if slot in self.st.setdefault("notices", {}) else ""))

    def attachments(self, d, lost=None):
        """Save a Discord message's files under Queue/attachments (synced) and describe them for the agent. The names
        of files that were not saved (too large, or the download failed) are added to LOST."""
        lost = [] if lost is None else lost
        rows = []
        for a in d.get("attachments") or []:
            name = re.sub(r"[^\w.\- ]", "_", a.get("filename") or "file")[:120]; size = int(a.get("size") or 0)
            if size > 100 * 1024 * 1024: rows.append(f"- {name}: skipped, larger than 100 MB"); lost.append(name); continue
            dest = queue_root() / "attachments" / str(d["id"]) / name; dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                req = urllib.request.Request(a["url"], headers={"User-Agent": "DiscordBot (https://localhost, 2.0)"})
                with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk: break
                        f.write(chunk)
                rows.append(f"- Queue/attachments/{d['id']}/{name} ({a.get('content_type') or 'unknown type'}, {max(1, size // 1024)} KB)")
            except Exception as e: rows.append(f"- {name}: download failed ({str(e)[:80]})"); lost.append(name)
        if not rows: return ""
        return ("\n\nThe owner attached these files. Paths are relative to the switchboard root (Queue/ is shared, so with file sync on they reach the other devices too). "
                "Open them with your file tools (images and PDFs can be read directly; for audio or video use a transcription tool or ffmpeg):\n" + "\n".join(rows))

    def dispatch(self, kind, d):
        if kind == "INTERACTION_CREATE": self.on_interaction(d)
        elif kind == "MESSAGE_CREATE": self.on_message(d)
        elif self.cfg.get("notepad") and kind == "MESSAGE_DELETE": self.on_delete(d)
        elif self.cfg.get("notepad") and kind == "MESSAGE_UPDATE": self.on_edit(d)
        elif self.cfg.get("notepad") and kind == "TYPING_START": self.on_typing(d)
        elif kind in ("THREAD_UPDATE", "THREAD_DELETE"): self.on_thread_event(kind, d)

    def run(self):
        self.st.setdefault("started", time.time())
        while True:
            if not self.is_active():
                time.sleep(20); self.st = {**self.st, **self.load_state()}; self.maybe_reload(time.time()); self.publish_status(time.time(), False); continue  # standby: another device is serving; new code is picked up here too
            ws = None
            try:
                ws = WS(api("GET", "/gateway/bot")["url"] + "/?v=10&encoding=json")
                hello = json.loads(ws.recv(30)); interval = hello["d"]["heartbeat_interval"] / 1000; seq = None; hb = time.time() + interval * 0.5
                ws.send(json.dumps({"op": 2, "d": {"token": TOKEN.read_text(encoding="utf-8").strip(), "intents": 1 | 512 | 2048 | 32768,
                                                    "properties": {"os": sys.platform, "browser": "bridge", "device": "bridge"}}}))
                log("gateway connected on", hostname())
                try: self.catch_up()
                except Exception as x: log("ERR catch up", str(x)[:160])
                if not self.is_active():  # uncached: the network may have come back between the check above and this connect
                    log("lock lost while connecting; standing by"); continue
                while True:
                    if not self.is_active(cached=True): break
                    raw = ws.recv(1.0)
                    if time.time() >= hb: ws.send(json.dumps({"op": 1, "d": seq})); hb = time.time() + interval
                    if raw:
                        p = json.loads(raw); seq = p.get("s") or seq
                        if p["op"] in (7, 9): raise ConnectionError(f"gateway asked to reconnect (op {p['op']})")
                        if p["op"] == 1: ws.send(json.dumps({"op": 1, "d": seq}))
                        if p["op"] == 0:
                            try: self.dispatch(p["t"], p["d"])
                            except Exception as x: log("ERR dispatch", p["t"], x)
                    self.tick()
            except Exception as e:
                log("gateway down:", type(e).__name__, str(e)[:160])
            finally:
                if ws: ws.close()
            time.sleep(6)

def ensure_lock():
    """The lock two bridges share (config lease_msg): one plain message 'host|epoch' in a thread of its own under
    #alerts, which lease_get reads live from Discord. Made once by setup. With it a second device can tell at once
    whether the first still serves; without it a standby only takes over when a liveness relay confirms the first down."""
    cfg = load("config.json", {})
    chan = (cfg.get("channels") or {}).get("alerts")
    if cfg.get("lease_msg") or not chan: return False
    name = {"name": "bridge lock (leave this thread alone)", "auto_archive_duration": 10080}
    try: th = api("POST", f"/channels/{chan}/threads", {**name, "type": 12, "invitable": False})["id"]  # private: nothing shows in the channel
    except DiscordError: th = api("POST", f"/channels/{chan}/threads", {**name, "type": 11})["id"]  # no permission for private threads
    mid = api("POST", f"/channels/{th}/messages", {"content": f"{hostname()}|0"})["id"]
    save("config.json", {**load("config.json", {}), "lease_msg": [th, mid]})
    return True

def need_token():
    """Stop with what to do, not a traceback, when this device has no bot token yet."""
    try: ok = bool(TOKEN.read_text(encoding="utf-8").strip())
    except OSError: ok = False
    if not ok:
        sys.exit(f"No Discord bot token on this device yet. The owner creates a bot and saves its token, as one line, in:\n  {TOKEN}\n"
                 "docs/discord-setup.md has the steps (about ten minutes, once). Never put the token in a chat, a repository or the shared Queue folder.")

def register_owner():
    """The owner's human mailbox, registered through the mail module: its CLI refuses to register a human unless a person
    runs it at a real terminal. An existing owner mailbox keeps its settings."""
    if "owner" in AM.load_agents(): return False
    hosts = load("config.json", {}).get("hosts") or [hostname()]
    AM.register("owner", ",".join(str(h) for h in hosts), "human", description="the human owner, through Discord")
    return True

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup"); s.add_argument("--guild", required=True)
    sub.add_parser("run")
    sub.add_parser("refresh", help="have the assistant go over every open thread once: buttons, tidying, updates from sibling threads")
    a = ap.parse_args()
    for s_ in (sys.stdout, sys.stderr):
        try: s_.reconfigure(encoding="utf-8", errors="replace")
        except Exception: pass
    if a.cmd in ("setup", "run"): need_token()
    if a.cmd == "setup":
        try: setup(a.guild)
        except DiscordError as e:
            sys.exit(f"Discord refused: {str(e)[:200]}\nCheck that --guild is your server's id, that the bot was invited to that server with the link in "
                     "docs/discord-setup.md, and that the token in the token file is the bot's current one.")
        print("registered the owner mailbox" if register_owner() else "owner mailbox already registered")
        return print("created the bridge lock" if ensure_lock() else "bridge lock already set up")
    if a.cmd == "refresh": save(f"refresh.{hostname()}.json", {"at": time.time()}); return print("asked the running bridge to refresh every open thread")
    screencap.start_service(log)
    if not fleet.single_instance("discord-bridge"):
        print("another discord-bridge is already running here; exiting"); time.sleep(60); return
    fleet.start_heartbeat(lambda: {k: v for k, v in next((d for d in fleet.devices() if d["host"] == hostname()), {}).items() if k.startswith(("runs_", "cap_"))})
    Bridge().run()

if __name__ == "__main__": main()
