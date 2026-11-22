# Design decisions

Why Agent Switchboard is built the way it is. Each entry names the decision, the alternative that was tried or
considered and rejected, and the reason. Research is cited by arXiv number where it shaped a choice. How the parts work
is in [how-it-works.md](how-it-works.md).

## Transport and process layout

### A synced folder of JSON mail

- **Decision:** agents talk through one JSON file per message in a shared folder, with an explicit acknowledgement that
  moves the file from `inbox/` to `done/`.
- **Rejected:** a message broker or a hosted queue.
- **Why:** files survive restarts and devices that are offline for hours, you can read and fix them by hand, and there
  is no server to run or pay for. Any file sync tool becomes the network.

### One process owns Discord

- **Decision:** agents only read and write mail; `discord-bridge.py` is the only Discord-aware process.
- **Rejected:** agents posting to Discord themselves, including through a Discord channel plugin.
- **Why:** agent context never carries Discord traffic, nothing is posted twice, and the whole system still runs from
  a terminal when Discord is down or not set up.

### A separate runner process for the orchestrator is possible

- **Decision:** a runner can serve a subset of agents (`--only orchestrator`), so planning and replies have their own
  process.
- **Rejected:** forcing one process to serve every agent.
- **Why:** a long build then never delays a reply to you.

### Heartbeats from a background thread

- **Decision:** each process writes its device heartbeat from its own thread every 45 seconds.
- **Rejected:** beating from the main loop.
- **Why:** a long agent run once blocked the main loop long enough for the device to look offline; the standby took
  over and cards and mail were doubled.

### Codex reads its prompt from stdin

- **Decision:** the runner starts Codex as `codex exec ... -` and writes the prompt to its stdin.
- **Rejected:** a temporary prompt file passed by path.
- **Why:** a sandbox that runs commands as another user could not read the temp folder, so every job failed.

## Multi-device state

### Per-host copies of every synced state file, merged on read

- **Decision:** each device writes only its own copy (`<name>.<host>.json`); readers merge all copies per entry, newest
  stamp wins, with tombstones for removals.
- **Rejected:** one shared file that every device writes.
- **Why:** file sync copies, it cannot merge. A device back from a long offline spell saved its old state over the
  newer one and left conflict copies behind.

### One serving bridge, with a lock read live from Discord

- **Decision:** the serving bridge renews a plain message `host|epoch` in a private Discord thread; the standby reads it
  from Discord and serves only once it is stale. Without that lock, the standby also needs a liveness relay to confirm
  the primary is down.
- **Rejected:** leases and heartbeats in synced files only.
- **Why:** over a slow sync relay both arrived minutes late, so two bridges served at once and reposted and deleted each
  other's messages. Failing closed is safer than answering twice.

### Hot reload only onto code with a full test pass

- **Decision:** the bridge restarts onto new code only when a `tested-<hash>.ok` marker from a full passing test run
  exists for it.
- **Rejected:** reloading whenever the new code compiles.
- **Why:** code that compiles can still crash at start, and with synced code it crashes on every device at once.

### Catch-up on reconnect

- **Decision:** after every (re)connect the bridge fetches your messages newer than the last one it handled.
- **Rejected:** trusting the gateway to deliver everything.
- **Why:** Discord does not replay messages a bot missed while it was restarting.

## Context and cost per run

### Lean agent runs

- **Decision:** agent runs load only the mail MCP server (`--strict-mcp-config`), use `--setting-sources project,local`
  so user hooks and plugins stay out, and the orchestrator, builder and reviewer get short tool lists.
- **Rejected:** inheriting the user's full Claude Code setup in every run.
- **Why:** measured with a one-word prompt, a run with every MCP server or every plugin loaded cost about 1.6x to 2.4x
  the lean run, and user hooks and `CLAUDE.md` added tens of thousands of characters per run. Unused tool schemas cost
  the same way.

### Lean prompts

- **Decision:** short role cores with reference files read on demand, a thread brief instead of the full history, and
  memory passed as note paths.
- **Rejected:** full context in every run.
- **Why:** measured prompt size fell from about 37K to 16K characters, and first-call tokens from about 39K to 18K.

### Continue the thread's session by default

- **Decision:** a run resumes the thread's earlier session when it ran in the same folder; a fresh start happens on a
  model change or when you ask.
- **Rejected:** a cold start for every run.
- **Why:** resuming saves tokens and keeps context. A saving that also keeps context is a gain, not a trade.

### On a model change, start fresh from the plan and the diff

- **Decision:** when a build escalates to another model, the new run starts from the plan and the measured diff.
- **Rejected:** handing the long history to the stronger model.
- **Why:** arXiv 2608.24358 found that handing history to a stronger model recovered under half the gap and cost more.

## Planning, building and review

### Plan strong, build cheap, one mini review

- **Decision:** a strong model writes a full plan, a cheaper model builds it, and one separate look at the measured
  changes decides the next step.
- **Rejected:** verifying every step, or telling the builder that a reviewer follows.
- **Why:** arXiv 2610.30284 and 2610.34577: per-step verification cost 2x to 4x for no gain, one separate look at the
  changes helped, and telling the agent a verifier follows lowered success.

### Full review only for big jobs, by the other model family

- **Decision:** a full review runs only when the job is big or risky (or you ask), and goes to the other model family
  from the builder.
- **Rejected:** reviewing everything.
- **Why:** cost, and a different family catches different errors and never repeats the mini review's model
  (arXiv 2610.01471).

### The planner states the tier, with a reason

- **Decision:** the plan may carry `TIER:` or `MODEL:` lines with a written reason, which the router follows.
- **Rejected:** rules alone.
- **Why:** arXiv 2609.32917: choosing a model per subtask at plan time was 44 percent cheaper for a 2.9-point loss,
  with a written reason per choice.

### Plan checks are evidence, not verdicts

- **Decision:** the planner writes `CHECK:` commands; the runner runs them after the build and reports PASS, FAIL or
  SETUP to the mini review.
- **Rejected:** no checks, or checks as automatic pass/fail gates.
- **Why:** VeriMAP (arXiv 2510.17109) and MAST (arXiv 2503.13657): checks help, but planner-written checks wrongly fail
  about a fifth of correct work.

### Short efficiency rules in the builder role

- **Decision:** the builder role carries a few rules such as "if an approach fails twice, stop and report".
- **Rejected:** no such rules.
- **Why:** arXiv 2609.30725 found re-reads and re-runs cost 7 to 23 percent, and arXiv 2611.07238 found about 18
  percent of output tokens go to dead-end detours.

### Routing by rules, no model call

- **Decision:** `route.py` picks model and effort from a table and word lists.
- **Rejected:** an LLM router.
- **Why:** free, instant and testable (`route-test.py`), and routing errors are lopsided enough that simple rules do
  well (arXiv 2603.04445, RouteLLM arXiv 2406.18665).

## Learning from past runs

### Playbook entries with counts, merged by script

- **Decision:** per-project lessons are small separate entries with helpful and harmful counts, merged by a script.
- **Rejected:** a model rewriting the playbook.
- **Why:** ACE (arXiv 2510.04618): wholesale rewrites collapse into vague summaries.

### A weekly retro that asks first

- **Decision:** a script counts a week of run stats; the orchestrator may propose at most three changes and must ask
  you before making any.
- **Rejected:** automatic rewriting of rules or skills.
- **Why:** in arXiv 2609.12742 a 4.9-point gain from self-rewriting could not be separated from noise.

## Concurrency and safety

### One writer per repo or area

- **Decision:** with `locks: "all"`, only one writing run works in a repo or named area at a time, across devices.
- **Rejected:** parallel writers in one repo.
- **Why:** several builders working on the same files broke the test suite within an hour.

### Human slots, and no thread hold for your own messages

- **Decision:** each device keeps extra run slots that only your messages may use, and your message never waits behind
  a run in its own thread.
- **Rejected:** one queue for everything.
- **Why:** you should never wait behind agent-to-agent mail.

### No hop limit by default

- **Decision:** `--max-hops` defaults to 0; a plain answer is never answered again, and `turn_runs` plus `Queue/STOP`
  are the brakes.
- **Rejected:** a hard hop cap.
- **Why:** the cap cut real work short; the no-reply rule stops ping-pong without it.

### A commit-only memory reading must repeat

- **Decision:** a device is marked under memory pressure on low free memory at once, but on high commit alone only
  when the next reading agrees.
- **Rejected:** holding runs on the first high reading.
- **Why:** single high-commit readings held runs dozens of times a day while plenty of memory was free.

## Discord behaviour

### A scripted acknowledgement first

- **Decision:** with triage on, the bridge posts a fixed acknowledgement at once and edits it when the model answers.
- **Rejected:** waiting for the model before showing anything.
- **Why:** small models took 6 to 30 seconds, and no model answers instantly.

### Thread state out of names when wanted

- **Decision:** a new install keeps state out of thread names (`thread_state_names` false, `calm_names` true).
- **Rejected:** renaming threads freely as the state changes.
- **Why:** Discord allows 2 renames per 10 minutes, and each rename leaves a notice nobody can delete.

### Show the share of the weekly limit, not only dollars

- **Decision:** where usage readings exist, the bridge shows the share of each subscription's weekly limit used.
- **Rejected:** dollars only.
- **Why:** subscription users run into the limit, not a bill.

### Small helpers on a separate, optional small-model CLI

- **Decision:** triage, the janitor, topic labels and the housekeeper's judge use a small Gemini model through the
  optional `agy` CLI.
- **Rejected:** Claude for everything.
- **Why:** they spend none of the main subscription, and without the CLI the system falls back to scripted behaviour.

## Other setups that were rejected

- **One long-lived lead agent per project with subagents:** sessions grew without bound, fan-outs burned most of a
  day's tokens, and there was little control from outside.
- **A rewrite on the Agent SDK:** the same design at the cost of a rewrite.
- **Claude Code agent teams:** experimental, no teammates in `-p` mode, no isolation, and about 7x the tokens.
- **A council of three or more reviewers:** in arXiv 2607.27030 four passes of the strongest single model matched four
  leading models together.
- **Automatic memory layers such as Mem0:** no gain over no memory in a benchmark.
- **Git branches as the unit of isolation:** installers and hooks get in the way; the atomic-claim idea was copied
  instead.

## Known limits and what is not included

- **Only Claude Code agents are started.** The runner runs agents of kind `claude`. Codex is used as an engine for those
  runs (reviews by default, builds when `routing.codex_builds` is true), not as an agent kind of its own. Cursor and
  Codex kinds can be registered as mailboxes but are never run.
- **The housekeeper is Windows only.** On other systems it exits, except `housekeeper.py --conflicts`. So on a
  macOS-only or Linux-only setup, memory pressure is not measured and sync-conflict copies are resolved only when you
  run that command.
- **The small-model helpers need the optional `agy` CLI.** Without it there is no triage, janitor, smart buttons,
  assistant chat or housekeeper judge; topic labels fall back to Cursor's CLI or Claude Sonnet.
- **Linux is not a first-class platform.** The scripts run, `setup/autostart.py` only prints a systemd unit, and the
  device heartbeat records any non-Windows device as a Mac.
- **There is no web dashboard.** Discord, the mail CLI and `fleet.py` are the views. Other programs can still drop a
  request into `Queue/discord/web/`.
- **There is no remote desktop control tool** and no remote shell. Devices cooperate only through the synced folder.
- **The liveness relay is not included.** The bridge reads `~/.switchboard/peers.json` if something writes it, but
  nothing in this repository does; without it a standby bridge takes over only through the Discord lock.
- **Undo is per build and per device.** `undo-build.py MAIL_ID` puts back the files one build changed, only on the
  device that ran it, and overwrites later edits to those files; undo the newest build first.
- **Agents share your OS user.** The mail CLI refuses to speak as a person, but an agent could still write a mail file
  directly. Give the agents their own OS user if that matters to you (see [SECURITY.md](../SECURITY.md)).
- **Clocks must roughly agree** between devices, since merged state compares timestamps.
