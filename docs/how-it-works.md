# How it works

This page is for a developer deciding whether to trust and run Agent Switchboard. It describes what each part does,
what happens to one request from start to finish, what lives in the shared folder, and the rules that keep the agents
from stepping on each other. Exact keys, flags and defaults are in [configuration.md](configuration.md); the reasons
behind the design are in [design-decisions.md](design-decisions.md).

## The idea in five sentences

1. Every agent has an inbox: a folder of JSON files under `Queue/mail/` in one shared folder, the **root**.
2. A runner process on each computer watches the inboxes of the automatic agents and starts one headless Claude Code
   run per message, in the right repo folder, and mails the result back.
3. An orchestrator agent plans each request and hands one task to a builder; the runner measures what the build
   really changed and runs the plan's checks, and the orchestrator decides what happens next.
4. A separate bridge process is the only thing that talks to Discord: it turns your messages into mail and the
   agents' mail into threads, cards and buttons.
5. Several computers cooperate by syncing the root with a file sync tool, with claims and per-device files so two
   devices never write the same thing.

Words used below: the **root** is the folder that holds your repos and `Queue/`; a **repo** is a folder directly under
the root; an **area** is a short name for writing work that is not in a repo; `~/.switchboard/` is device-local and is
never synced. The code is in `switchboard/`.

## The components

The letters match the README and its diagram.

### A. Discord bridge

`switchboard/discord-bridge.py` is one process with its own small WebSocket client for Discord's gateway. It obeys only
the server owner. A message in `#orchestrator` becomes mail from `owner` to `orchestrator` and gets its own Discord
thread with one status card edited in place. Mail addressed to `owner` and the shared progress log become messages,
cards, question buttons and file uploads. Agents never see Discord. The bridge keeps its state in
`Queue/discord/state.<host>.json`. Two devices can run it; one serves and the other stands by (see the safety rules).
It restarts itself onto new code only when the full test suite has passed on that code.

Two optional helpers use a small Gemini model through the `agy` CLI, so they spend no Claude or Codex usage.
`triage.py` is the first reader of your message: it can answer a status question at once, route a follow-up into the
thread it belongs to, or mark it as new work. `discord-janitor.py` looks over the server every 5 minutes and may delete
bot messages the bridge no longer tracks or archive idle bot threads; the script enforces those limits, not the model.
Without `agy` both report an error and the bridge uses its plain path.

### B. Mail and runner

`switchboard/agent-mail.py` is the mail library and CLI: one JSON file per message, explicit acknowledgement (the file
moves from `inbox/` to `done/` with `acked_at`, `acked_by` and `result`), threads and hop counts.
`switchboard/agent-mail-mcp.py` exposes the same library as an MCP server over stdio, with a fixed identity, so an
agent can call `send_message`, `ask_owner`, `check_inbox` and the rest as tools.

`switchboard/mail-runner.py` is the worker. For each registered agent with `auto: true`, kind `claude` and this device
in its `host` list, it takes inbox mail oldest first and runs `claude -p ... --output-format stream-json` in the
message's repo folder (else the agent's `cwd`, else the root). Codex can stand in as the engine for a run when the
engine policy and quota allow it. The result text is mailed to the sender and the message is acked. A failure or a
stall acks it `FAILED: ...` and tells `owner`. After a build the runner measures the change, runs the plan's checks,
writes run stats, and keeps a live tail of the run for the Discord card. It also hosts the improvement loop scheduler.
When one of its own files changes on disk it finishes the runs in flight, then exits with code 3 so the service
manager starts it again on the new code.

`switchboard/route.py` picks the model and effort for each run from a rules table, with no model call.

### C. Fleet

`switchboard/fleet.py` is a library and CLI for "which devices are up and what is in flight". Each runner and bridge
writes a heartbeat to `Queue/devices/<host>.json` every 45 seconds from a background thread; a heartbeat under 150
seconds old counts as fresh. It picks a device for a repo, lists unfinished work, reads Codex quota, and holds the
helpers that let two devices share state through file sync (`host_path`, `save_host`, `load_merged`,
`resolve_conflicts`).

`switchboard/repo-handoff.py` is a per-repo lock for a person moving work between devices, or for a queue card.
`switchboard/housekeeper.py` is optional and does its work on Windows only: it measures memory and CPU, marks the
device under memory pressure so it takes no new agent runs, cleans up orphaned helper processes and old temp folders,
and once an hour resolves file sync conflict copies under `Queue/`.

### D. Hooks

`switchboard/session-hook.py` is a Claude Code hook (UserPromptSubmit, Stop, Notification, SessionEnd) and a Codex
`notify` program. It records each of your interactive sessions in `Queue/sessions/<host>/<session id>.json` with a
state of `working`, `idle`, `needs_you` or `ended`, so your own sessions show beside the agents' work. It skips runner
jobs. `switchboard/permission-hook.py` is a PermissionRequest hook, described under the safety rules.
`switchboard/install-session-hooks.py` adds both to `~/.claude/settings.json`, after a backup.

### E. Memory and skills

`switchboard/claude-memory-loader.py` feeds the scoped `MEMORY.md` files from one Markdown vault into Claude Code
sessions through command hooks. `switchboard/memoryctl.py` checks and lints the routes from folders to rule files.
`setup/skills-sync.sh` keeps one shared folder of agent skills linked into Claude Code, Codex and Cursor. Runner jobs
do not load the memory hooks; the runner lists the paths of the memory notes that govern the run in its prompt.
Details: [memory-and-skills.md](memory-and-skills.md).

### F. Setup

`setup/doctor.py` checks a device and, with `--fix`, creates `Queue/`, installs the role files from `templates/roles`,
writes default config files and registers the mail agents. It never overwrites an existing file.
`setup/autostart.py` installs login items (launchd on macOS, a per-user Run entry on Windows, a printed systemd unit
on Linux) only when you ask. `setup/install-memory.py` creates a starter vault and the memory registry.
`tools/check-clean.py` refuses tokens, real Discord ids, home paths and private addresses in the repository.
[AGENTS.md](../AGENTS.md) walks a coding agent through all of it.

### Also in the box

- **Improvement loop** (`improve-loop.py`). Runs inside the runner on the first available device of its `hosts`
  list. At most one cycle per pass and at least 30 minutes apart, it mails the orchestrator a cycle for one repo or
  project you added. In `auto` mode a cycle builds, reviews and ships through the entry's deploy route, or leaves the
  change ready and asks when there is none. In `ask` mode it proposes one change with Build it / Skip / Something
  else. A shipped change records the metric it should move; a later cycle checks it and records a verdict. It can
  propose folders you work in often, and adds nothing before you answer.
- **Queue runner** (`queue-runner.py`). An older, simpler path: Markdown task cards in `Queue/todo/`, run one at a
  time with `claude -p` in the card's repo, with a report written beside the card. It never runs Codex.
- **Notepad** (`notes.py`). Everything you wrote in a thread, in order, with what each line replied to and whether an
  agent has read it. Edits keep the newest text; a deleted message is marked withdrawn. Every run in the thread gets
  it in its prompt.
- **Playbook** (`playbook.py`). Short per-project lessons with helpful and harmful counts, merged by the script and
  never rewritten by a model. The planner sees the top entries.
- **Retro** (`retro.py`). Counts a week of run stats into one summary. The loop sends it to the orchestrator once a
  week when there were 20 or more runs; the orchestrator may propose at most three changes and must ask first.
- **Topics** (`topics.py`). Groups your sessions and the agent threads with unfinished work by subject for the
  `#overview` channel. A repo folder is its own topic; anything else gets one label from a small model.

## The life of a request

1. **You type in `#orchestrator`.** The bridge acts only on messages from the server owner. With `quick_status` on,
   a bare status question (`?`, `status`) is answered from files without starting an agent.
2. **Batching.** With `notepad` on, a new request is sent at once. Text inside a thread, or a Discord reply to a bot
   message, is held until you have been quiet for 10 seconds (12 after a typing signal) and sent as one message, each
   line marked with what it replied to. With `triage` on, the bridge first posts a short scripted acknowledgement and
   asks `triage.py` what to do; no answer within 120 seconds means the plain relay.
3. **Relay.** The bridge writes mail from `owner` to `orchestrator` (or to the agent named by a `builder: ...`
   prefix) with `needs_reply` set. Attachments are saved under `Queue/attachments/<discord message id>/` and listed in
   the body. A new request gets its own Discord thread and one status card. If the mail cannot be written, your
   message gets a warning reaction and is retried for a day.
4. **A runner picks it up.** The device is chosen by rules, not a model: a pinned `device`, else the device that holds
   the thread's orchestrator session, else the agent's `host` list filtered by fresh heartbeat, memory pressure, run
   caps and platform words in your text ("on the Mac"), and for a repo only devices that hold its folder. If a pinned
   device stays offline for 4 minutes, the runner asks you with Run on buttons. The runner takes its claims, asks
   `route.py` for model and effort, and builds the prompt: the role file `Queue/mail/roles/<agent>.md`, the thread
   brief `Queue/threads/<thread>.md`, new notepad lines, other work in flight in the same repo or area, playbook
   entries, memory note paths and a line about the devices. The orchestrator resumes the thread's earlier session when
   it can; "start fresh" in your message, or `mail-runner.py fresh <thread>`, forces a new one.
5. **Plan.** The orchestrator answers small questions itself. For real work it reads the code, writes a plan a
   cheaper builder can follow, sends it to `builder` and posts a short version in the thread. Plan lines the runner
   and router read: `CHECK:` (a command to run after the build, up to 6), `CHECKDIR:`, `TIER:`, `MODEL:`, `AREA:` (the
   writer key for work outside a repo) and `REPORT: direct`.
6. **Build.** The runner starts the builder under a claim on the repo or area. Afterwards the runner, not the builder,
   appends a "Measured by the runner" block: files changed with line counts, a diff at
   `Queue/attachments/<mail id>/changes.diff`, copies of the files before the build and an `undo.json` record (which
   `undo-build.py` applies), one line in `Queue/changes/<repo or area>/<host>.jsonl`, and each check's result as
   PASS, FAIL or SETUP. With `REPORT: direct` and every check passing, the result goes straight to whoever started
   the thread.
7. **Mini review.** The builder's answer goes back to the orchestrator, routed to a stronger model. It decides: done,
   one more step for the builder (same model, same session), a new plan, or a full review by the other model family
   (a mail to `reviewer` whose first line is `FULL REVIEW: <reason>`). A review request for a small job is answered by
   the runner with "Review skipped". `turn_runs` caps build and review runs between two of your messages.
8. **Questions, at both ends.** An agent calls `ask_owner`. The bridge posts a card with up to 5 buttons and **Done
   elsewhere**, as the last message in the thread, and lists every open question in `Queue/asks/open.<host>.json`.
   Your own Claude Code (and Codex) sessions show each new one with your next message there, through the session hook,
   so you see it wherever you look first. One answer closes both ends: a tap, a typed message or a Discord reply to
   the card answers it in Discord, and the session hears it was settled; an answer you give in a session is passed on
   by that session with `answer_question`, which checks your quoted words against what you really typed or picked
   there, and the card closes saying where you answered. A session whose own work did what a question asks closes it
   with `settle_question`. Before asking, `ask_owner` shows the agent the questions already open on the same repo or
   thread, and the agent names the ones its question replaces, so you answer once (a newer pull request that holds the
   older one's changes replaces the older ask). A small model (`triage.py`, kind `supersede`) catches the overlaps
   agents miss. Replaced cards point at the newer question, and a thread left with nothing to wait for is archived.
   Cards are also closed when another mail answered them, when a session of yours took over, or when their thread
   stayed closed for 6 hours.
9. **"Do your recommendations".** Typed in `#orchestrator` (all open questions) or a thread (that thread's), it records
   a go-ahead for the questions open now, valid for 2 hours, and mails the orchestrator the list. The orchestrator
   answers each with its recommended option. The bridge accepts those answers only from `orchestrator`, only for
   questions the go-ahead covers, never for a device or action button, and never for a label that matches deploy,
   push, publish, release, merge, git, spend, buy, pay, purchase, delete, production, live or "something else".
10. **Answer.** The orchestrator replies to `owner`. The bridge posts it, split at paragraphs (a long answer goes up
    as a file), uploads up to 4 files named on `ATTACH: <path>` lines (under the root, 10 MB at most), removes the
    status card and puts a Close button on the answer.
11. **Afterwards.** A finished request thread with nothing open is archived after 20 minutes without new posts (24
    hours when it failed or was cut off). A thread you wrote in stays while it is recent. Any new message reopens it.

The same flow works without Discord: send mail with `agent-mail.py send` from your device's own mailbox and read the
reply with `agent-mail.py inbox`.

## The `Queue/` folder

`Queue/` sits in the root and is shared between devices. Files named `<host>` belong to one device; every other device
only reads them.

```
Queue/
  STOP                          pauses every runner, the queue runner and the loop; /stop and /go toggle it
  todo/ running/ done/ failed/  queue cards; running/<host>--<card>; <card>.report.md beside the card
  locks/<repo>.json             repo-handoff.py lock; locks/history.log keeps the history
  mail/
    agents.json                 the agent registry
    <agent>/inbox/<id>.json     unread messages
    <agent>/done/<id>.json      acked messages
    roles/<agent>.md            role prompts; roles/ref/*.md are read on demand
    runner.json                 runner settings, read live
    engines.json                engine order per agent and the Codex quota floor
    claims/<host>/<name>.json   each device's claims on messages, threads, repos and areas
    claims/<mail id>.waiting    a message held while it waits for a device
    origins/<id>.json           which interactive session asked a question
    conflicts/<hash>.json       one-day dedupe for report_conflict
    fable-blocked.json          Fable's own limit was hit: routing stays off Fable for 3 hours
  devices/<host>.json           heartbeat: liveness, repos held, Codex quota, run caps
  resources/<host>.json         housekeeper reading: memory, CPU, pressure
  housekeeper/<host>-control.json  pause the housekeeper or ask for a clean-up now
  events/<host>-<yyyymm>.jsonl  progress log
  stats/<host>-<yyyymm>.jsonl   one row per run: engine, model, effort, route, tokens, cost, seconds, checks
  ledger/<host>-<yyyymm>.jsonl  per-request rows from feedback buttons (only with config feedback)
  usage/                        weekly-limit readings; claude-blocked.json while Claude's usage limit holds
  live/<run>.json               what a run is doing now, for the status card
  threads/<thread>.md           the thread brief the orchestrator keeps
  threads/<thread>.json         session record used to resume a thread
  threads/<thread>.<agent>.jsonl  a copy of a session so another device can continue it
  notes/<thread>.json           your notepad for that thread
  attachments/<discord id>/     files you attached
  attachments/<mail id>/        changes.diff, before/ and undo.json for one build
  changes/<repo or area>/<host>.jsonl  one line per build
  open/<host>/<id>.json         "Open in Claude" and open-a-file requests for that device; <id>.done.json answers
  captures/                     screen capture requests in transit, removed after upload or 15 minutes
  sessions/<host>/<id>.json     your interactive sessions (recent prompts, for answer_question's check)
  asks/open.<host>.json         open questions and the ones closed in the last two days, from the serving bridge
  topics/labels.json            topic labels
  playbooks/                    per-project lessons
  loop/
    config.json                 loop settings
    state.<host>.json           last cycle times
    activity/<host>.json        the folders you worked in on that device
    <name>/                     outcomes.<host>.jsonl, cycles.<host>.jsonl, backlog.md, log.md
  discord/
    config.json                 bridge settings, written by setup and by you
    state.<host>.json           threads, cards and questions
    lease-<host>.json           file lease, the fallback for the Discord lock
    status-<host>.json          serving or standby, code hash, recent log lines
    tested-<hash>.ok            written by a full passing test run; allows hot reload onto that code
    refresh.<host>.json         asks the serving bridge to go over open threads once
    triage/ smart/ settle/ argon/ health/  small-model job files and answers
    sidechat/<thread>.jsonl     status questions triage answered, shown to the orchestrator
    argon-memory.json           rules you gave the assistant chat
    janitor*.<host>.json(l)     janitor state, log and findings
    gallery.json                ids of the card gallery thread
    web/web-<id>.json           drop box: {"text", "thread"} from any program becomes a request
  archive/sync-conflicts/       every conflict copy that was handled; nothing is deleted
```

## Rules that keep it safe and orderly

**One writer per repo or area.** Before a writing run starts, the runner takes a claim. Device-wide claim files in
`~/.switchboard/mail-runner-claims/` give one run per message, per mail thread and per repo or area across every runner
process on the device. The synced `Queue/mail/claims/<host>/` folder extends the thread and area holds to the other
devices, within the sync delay. Writing work in a repo takes `repo-<name>`; outside a repo it takes its `AREA:`; work
that names neither takes `workspace` and never runs beside a named area. The orchestrator and reviewer only read and
take no writer key. A claim outlives a killed runner only until its timeout plus 5 minutes. `locks` in `runner.json`
can loosen this.

**Human slots.** Each device has 6 extra run slots that only messages you wrote may use, and your message never waits
behind a run in its own thread. So a long agent-to-agent exchange cannot keep you waiting. A device holds at most 24
runs in all, and one runner process at most 16.

**Hop limit.** Every message carries a hop count that grows along a thread; your own messages reset it to 0. The
default `--max-hops 0` sets no hard limit. Ping-pong is stopped another way: a plain answer (a reply that does not ask
for one) is never answered again, unless you start the runner with `--always-reply`. `turn_runs` and the STOP file are
the other brakes.

**STOP file.** If `Queue/STOP` exists, the runners, the queue runner and the loop start nothing new. `/stop` in Discord
creates it and `/go` removes it.

**The CLI cannot speak as a person.** `agent-mail.py` refuses `send`, `reply`, `amend`, `answer`, `go-ahead` and
`register` as an agent of kind `human` unless stdin and stdout are a real terminal and none of the agent marker
variables is set (`AGENT_MAIL_AGENT`, `CLAUDECODE`, `CODEX_SANDBOX` and others). Programs that speak for you, such as
the bridge, the loop and the permission hook, call the library. This closes the CLI route only: agents run as your OS
user and could still write a mail file by hand. See [SECURITY.md](../SECURITY.md).

**Per-host state copies, merged on read.** File sync copies files; it cannot merge them. So every state file that more
than one device writes is kept as one copy per device (`<name>.<host>.json`), and readers merge all copies: per entry,
the copy with the newest stamp wins, removals are kept as tombstones for 30 days, and timestamp fields take the
maximum. Clocks must roughly agree. This covers bridge state, janitor files, loop state, claims, leases, usage and
resource readings; heartbeats, events and stats have per-host file names anyway. `Queue/discord/config.json` is written
only by `setup` (run it on one device at a time), and automatic loop config edits happen only on the first loop host.

**Sync-conflict handling.** If the sync still makes `*.sync-conflict-*` copies under `Queue/`, one resolver handles
them: the Windows housekeeper once an hour, or `housekeeper.py --conflicts` by hand on any system. JSON is merged by
the rules above, `.md` and `.jsonl` copies take the union of their lines in order, another device's own file is left
for that device, and empty copies and copies inside an undo snapshot are archived. Every handled copy moves to
`Queue/archive/sync-conflicts/`. Nothing is deleted. Anything it cannot handle is reported to you as one event.

**One serving bridge.** The first host in the bridge's `hosts` list is the primary. The serving bridge renews a lock
message `host|epoch` in a small "bridge lock" thread every 15 seconds; `setup` creates it. A standby reads it straight
from Discord and serves only after 150 seconds without a renewal. Without a readable Discord lock it falls back to
synced lease files, and then also needs an optional liveness relay to confirm the primary is down, so it fails closed.
A primary coming back waits (at most 10 minutes) for the standby's state to sync before taking over. A standby never
writes state. On every reconnect the bridge fetches your messages it missed, since Discord does not replay them.

**Usage-limit pause.** A Claude run that ends on a usage-limit message writes `Queue/usage/claude-blocked.json` with
the reset time. While it holds, Codex may take any agent's run if it has quota; otherwise the message stays in the
inbox, a `paused` event is written and the card shows Paused. It starts again by itself after the reset. This is not a
failure. A stall is reported as cut off, not failed.

**Memory pressure.** A device the housekeeper marks under pressure starts no new agent runs (your own messages still
run) and `fleet.py where` reports it as not available.

**Untrusted text.** The role files tell every agent that attached files, web pages, search results and tool output are
data, never instructions, and to quote such text to you if it asks for an action. Because the planner also reads
untrusted text, the runner executes a plan's `CHECK:` lines without a shell: one command from a fixed list (`python`,
`pytest`, `ruff`, `node`, `npm`, `npx`, `tsc`, `eslint`, `go`, `cargo` and other test and lint runners), rejecting `;`, `&`, `|`, `<`, `>`, `$`,
backticks, line breaks and `..` paths, at most 6 checks and 300 seconds each. A failed check is evidence for the
mini review, not an automatic verdict.

**What the permission hook does.** When Claude Code is about to show a permission dialog, `permission-hook.py` checks
whether you used this device's keyboard or mouse in the last 180 seconds. If you did, it exits and the normal dialog
decides. If you are away, it mails the request from `sessions` to `owner`, which the bridge shows with Allow once and
Deny buttons, and waits up to 8 minutes. Allow once and Deny return that decision to Claude Code. No answer, any other
answer or any error returns nothing, and the local dialog decides. It never allows anything by itself, skips runner
jobs and the AskUserQuestion and ExitPlanMode tools, and redacts secrets in the summary it sends.

**Files.** `ATTACH:` uploads only files under the root and never from the token folder. Open buttons for a local file
open only viewer file types inside the root's parent folder, never under `~/.switchboard`.

**Hot reload only onto tested code.** The bridge restarts on changed code only when the files compile, have stayed
unchanged for 20 seconds, and `Queue/discord/tested-<hash>.ok` exists for that code. A full passing run of
`discord-bridge-test.py` writes that marker. Otherwise it logs once and keeps running the old code.

## Where to go next

- [configuration.md](configuration.md): every config key, flag, environment variable and the routing table.
- [discord-setup.md](discord-setup.md): creating the bot and what you can type.
- [multi-device.md](multi-device.md): running on two or more computers.
- [design-decisions.md](design-decisions.md): why it is built this way, and what was rejected.
- [SECURITY.md](../SECURITY.md): what you are trusting.
