# Configuration reference

Every config file, key, flag and environment variable, with the default the code uses. Paths under `Queue/` are in the
shared root; paths under `~/.switchboard/` are on one device and never synced. Script paths are given as
`switchboard/<file>`. For what the parts do, see [how-it-works.md](how-it-works.md).

Config files are plain JSON. The runner and the bridge read their files live, so most changes apply on the next pass
without a restart.

## `Queue/mail/runner.json`

Runner settings, read on every pass. `setup/doctor.py --fix` installs `templates/config/runner.json`, which writes
`max_concurrent` 4, `locks` "all", `turn_runs` 4 and `routing` `{"enabled": true, "codex_builds": false}`. A missing
file or key falls back to the default below.

| Key | Default | Meaning |
|---|---|---|
| `max_concurrent` | 4 | Runs one runner process may hold at once. At most 16; `0` means 16; an unusable value means 4. The `--max-concurrent` flag wins over it. |
| `locks` | `"all"` | `"all"`: one run per mail thread and one writer per repo or area. `"repo"`: only one run per repo on a device, and never two devices on the same repo or named area. `"none"`: no holds. |
| `turn_runs` | 4 | Build and review runs one request may use between two of your messages. `0` = no limit. |
| `model` | none | Sets `ANTHROPIC_MODEL` for agent runs. |
| `models` | none | A model per agent, `{"builder": "..."}`; the key `"<agent>:relay"` sets one for turns that only handle another agent's mail. |
| `subagent_model` | none | Sets `CLAUDE_CODE_SUBAGENT_MODEL` for agent runs. |
| `area_alias` | built in | Extra `{area: writer key}` pairs. Areas that edit the same files should share one writer key. The built-in map sends the switchboard's own areas to one key. |
| `routing.enabled` | true | `false`: no `route.py` choice; runs use the configured or default model. |
| `routing.codex_builds` | false | `true` lets Codex build. Otherwise Codex only takes reviewer runs (and any run while Claude's usage limit holds). |
| `routing.codex` | true | `false` keeps Codex out of routing. |
| `routing.fable` | `"free"` | `"off"` keeps Fable out of routing. |
| `routing.fable_per_day` | 0 | Daily cap on Fable runs; `0` = no cap. |
| `routing.plan` | none | Replaces rows of the routing table (see [Model routing](#model-routing)). |
| `routing.review` | none | `"always"` or `"never"`: overrides the rule that only big jobs get a full review. |
| `routing.review_min_minutes` | 25 | A build that ran this long gets a full review. |
| `routing.review_min_tokens` | 80000 | A build that wrote this many output tokens gets a full review. |
| `routing.ponytail` | none | Forces a level of the optional Ponytail plugin for every run: `off`, `lite`, `full` or `ultra`. |

## `Queue/mail/engines.json`

Optional. Merged over this default:

```json
{"*": ["claude", "codex"], "reviewer": ["codex", "claude"], "codex_min_left": 15}
```

| Key | Default | Meaning |
|---|---|---|
| `*` | `["claude", "codex"]` | Engine order for any agent without its own entry. |
| `<agent>` | `reviewer`: `["codex", "claude"]` | Engine order for one agent. |
| `codex_min_left` | 15 | Codex is used only while at least this percent of its weekly quota is left. It also needs 5 percent of its 5-hour window. |

Only engines installed on the device are tried. While `Queue/usage/claude-blocked.json` holds, Codex may take any
agent's run whatever `routing.codex_builds` says.

## `Queue/mail/agents.json`

The registry. Write it with `agent-mail.py register` (or `setup/doctor.py --fix`) rather than by hand.

```json
{"builder": {"host": ["Desk-PC", "Laptop-Mac"], "kind": "claude", "auto": true, "cwd": null,
             "description": "Builds one task per message."}}
```

| Field | Meaning |
|---|---|
| `host` | One host name or a priority list. The runner runs an agent only on a host that is named here; `"any"` is used for human mailboxes and does not make an automatic agent run. |
| `kind` | `claude`, `codex`, `cursor` or `human`. The runner runs only kind `claude`; Codex is an engine for those runs, not a kind it starts. |
| `auto` | `true`: the runner answers this agent's mail. |
| `cwd` | Folder for runs whose mail names no repo. Default: the root. |
| `description` | One line, shown to the agent and in `agents` listings. |

Names use letters, digits, `-` and `_`, at most 64 characters. `roles`, `agents` and Windows device names (`con`,
`nul`, `com1` and so on) are reserved. `setup/doctor.py --fix` registers `owner` and `sessions` (kind `human`),
`orchestrator`, `builder` and `reviewer` (kind `claude`, `auto`, this host) and one mailbox named after this device
(kind `claude`, not `auto`) for your own interactive sessions. `discord-bridge.py setup` also registers `owner` if it is
missing.

## `Queue/discord/config.json`

`discord-bridge.py setup --guild ID` writes `guild_id`, `owner_id` (the server owner), `bot_id` and `channels`
(`overview`, `orchestrator`, `progress`, `threads`, `reports`, `alerts`, `loops`, each a channel id; `threads` is the
`#sessions` channel). It adds `hosts` and the defaults in the "setup writes" column only when the key is missing, then
creates the `owner` mailbox and the bridge lock (`lease_msg`) if they do not exist. It keeps every other key. Run setup
on one device at a time. `templates/config/discord.config.example.json` shows a filled-in file.

"Code default" is what the bridge does when the key is absent. "Setup writes" is what a new install gets.

| Key | Code default | Setup writes | Meaning |
|---|---|---|---|
| `hosts` | this host | `[setup host]` | Devices that may run the bridge, in priority order. The first is the primary. |
| `lease_msg` | none | `[thread id, message id]` | The bridge lock: a plain bot message `host\|epoch` in a private thread under `#alerts` named "bridge lock". Needed for a fast two-device takeover. Leave the thread alone. |
| `one_answer_per_turn` | off | true | One answer message per message of yours, edited in place. |
| `notepad` | off | true | Batch your messages, keep the notepad, track edits, deletions and read marks. |
| `quick_status` | off | true | Answer a bare status question from files without starting an agent. |
| `progress_boards` | off | true | A few category board messages in `#progress` instead of one card per piece of work. |
| `calm_names` | off | true | Rename threads only for needs-you and the end. |
| `thread_state_names` | on | false | Put the state in thread names. Discord allows 2 renames per 10 minutes and each leaves a notice. |
| `add_owner_to_threads` | on | false | Add you to each new thread. |
| `screenshots` | on | false | Post a screen capture of the device where work is blocked under its card. |
| `triage` | off | false | Small-model first reader and an instant scripted acknowledgement. Needs `agy`. |
| `janitor` | off | false | Run `discord-janitor.py` every 5 minutes on the serving bridge. Needs `agy`. |
| `argon_chat` | on | false | Open the assistant chat thread in `#orchestrator`. Needs `agy`. |
| `argon_channel` | none | not written | A channel id for the assistant chat instead of a thread. |
| `smart_buttons` | on | not written | After an answer, a small-model pass suggests next-step buttons and tidies. Needs `agy`; without it the default buttons stay. |
| `settle_check` | on | not written | A small-model check before an agent's question is closed as settled elsewhere. |
| `done_elsewhere_button` | on | not written | Show **Done elsewhere** on question cards. |
| `cards` | on | not written | Discord Components V2 cards instead of plain messages. |
| `receipt` | off | not written | A cost line under each orchestrator answer. |
| `burn_alert` | 20 | not written | Dollars one request may cost since your last message before one `#alerts` notice. `0` = off. |
| `pace_alert` | 100 | not written | Projected percent of the weekly limit at reset that raises one notice a week. `0` = off. |
| `feedback` | off | not written | Kept for older installs: records which answers would take a feedback reaction. The Accept / Needs fixing / Undo buttons are no longer shown. |
| `janitor_critic` | off | not written | Send new janitor findings to the orchestrator, at most every 2 hours, as mail from agent `janitor`. |
| `janitor_models` | built in | not written | Model list for the janitor, first that answers. |
| `project_renames` | off | not written | Allow one rename that adds the project to a thread name. |
| `delete_system_notices` | on | not written | Delete Discord's automatic notices where Discord allows it. |
| `keep_until_closed` | on | not written | Threads you started or wrote in stay until you close them. |
| `keep_unread` | on | not written | Nudge kept threads so they show as unread. |
| `audit_hidden` | on | not written | Reopen hidden threads that still have unfinished work. |
| `overview_threads` | off | not written | Topic ping threads in `#overview`. Off: pings go to the work's own thread. |
| `self_reload` | on | not written | Restart onto new code that has a passing test marker. |

## `Queue/loop/config.json`

Improvement loop settings, merged over the defaults below. `setup/doctor.py --fix` installs
`templates/config/loop.config.json` (the same values, with `enabled` false). Edit it with `improve-loop.py` or by hand;
automatic edits are saved only on the first host in `hosts`.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | false | Master switch. `improve-loop.py on` / `off`. |
| `interval_hours` | 8 | Default time between cycles of one entry. |
| `hosts` | `[]` | Devices that may run the loop, in priority order; the first available one does. |
| `repos` | `{}` | `{NAME: {mode, deploy, campaigns, auto, paused, recent}}`: entries for repo folders. |
| `projects` | `{}` | `{NAME: {mode, path, host, brief, every, deploy, ...}}`: a folder anywhere on one named device. |
| `auto_add` | true | Propose folders you work in often. |
| `min_sessions` | 5 | Your own sessions in 7 days before a folder is proposed. |
| `daily_budget` | 75 | Dollars of agent cost at API prices over 24 hours; self-started cycles wait above it. `0` = no budget. |
| `ignore` | `[]` | Folder names never proposed. |
| `pending` | `{}` | Proposals waiting for your answer. |
| `snoozed` | `{}` | Proposals you answered Not now. |

Entry fields: `mode` is `auto` (default: build, review and ship through `deploy`, or leave it ready and ask) or `ask`
(propose one change and wait); `deploy` is the deploy route in words the builder follows; `every` is hours between
cycles; `brief` says what to look at; `campaigns` replaces the default rules for campaign work. Entries the loop added
itself pause after 14 days without one of your sessions.

## `Queue/housekeeper/` and `switchboard/housekeeper-apps.json`

`Queue/housekeeper/<host>-control.json`: `{"paused": true}` pauses that device's housekeeper; `{"clean_at": <epoch>}`
asks for a judgment now.

`switchboard/housekeeper-apps.json` configures the optional memory judge (needs `agy`, Windows only):

| Key | Default | Meaning |
|---|---|---|
| `judge_every_min` | 10 | Minutes between judgments; every 3 minutes while free memory is low. |
| `pressure_free_gb` | 6 | Free memory under this counts as low for the judge. |
| `models` | Gemini low, then medium | Models tried in order. |
| `notes` | examples | Context the model weighs. There is no keep or close list. |

The pressure mark that stops new agent runs is separate and fixed: it starts under 2.5 GB free or over 97 percent
commit, and clears above 4 GB free and under 94 percent. A commit-only reading must repeat on the next pass.

## Queue cards

A queue card is a Markdown file in `Queue/todo/` for `queue-runner.py`. See `switchboard/queue-card.example.md`.

| Frontmatter | Default | Meaning |
|---|---|---|
| `repo` | required | Folder under the root to run in. |
| `machine` | `any` | `any` or one host name. |
| `budget_minutes` | 120 | Time budget for the run. |
| `permission_mode` | `auto` | `auto`, `acceptEdits` or `plan`. |

The body is the prompt. The report is written as `<card>.report.md` in `done/` or `failed/`.

## Environment variables

| Variable | Read by | Meaning |
|---|---|---|
| `SWITCHBOARD_ROOT` | every script, `skills-sync.sh` | The root. Wins over `~/.switchboard/root` and the checkout's parent folder. |
| `SYNCTHING_URL`, `SYNCTHING_API_KEY` | `common.py` (used by `repo-handoff.py`) | Syncthing REST address and key; otherwise read from Syncthing's own config. |
| `AGENT_MAIL_AGENT` | MCP server, hooks, memory loader | The agent identity of a runner job. Hooks skip such runs. |
| `AGENT_MAIL_PARENT` | `agent-mail.py`, `repo-handoff.py` | The message a run answers, for thread and hop continuity and the run id. |
| `AGENT_MAIL_CLAIMS` | `repo-handoff.py` | The runner's device claim folder. |
| `SESSION_HOOK_SKIP` | hooks | Skip this process. |
| `SESSION_HOOK_ALL` | `session-hook.py` | Also record sessions in temp and scratch folders. |
| `SESSION_HOOK_LOG` | `session-hook.py` | Append one debug line per event to this file. |
| `SESSION_HOOK_FORCE_IDLE` | `permission-hook.py` | `1` skips the keyboard idle check (testing). |
| `SESSION_HOOK_IDLE_SECONDS` | `permission-hook.py` | Idle time before a request is forwarded. Default 180. |
| `SESSION_HOOK_WAIT_SECONDS` | `permission-hook.py` | How long it waits for your answer. Default 480. |
| `CLAUDE_PROJECT_DIR` | MCP server, hooks | The session's folder, set by Claude Code. |
| `AI_MEMORY_BASE` | `claude-memory-loader.py` | Base folder for the registry and cache. Default `~/.switchboard`. |
| `SWITCHBOARD_SKILLS` | `skills-sync.sh` | The shared skills folder. Default `<root>/agent-skills`. |
| `SWITCHBOARD_SKILLS_LOG` | `skills-sync.sh` | Log file. Default `~/.switchboard/skills-sync.log`. |
| `BRIDGE_TESTED_DIR` | `discord-bridge-test.py` | Where a full pass writes `tested-<hash>.ok`. |
| `APPDATA`, `LOCALAPPDATA` | several | Windows locations of Codex logs, `agy` and Syncthing. |

The runner sets these for each run: `AGENT_MAIL_AGENT`, `AGENT_MAIL_PARENT`, `AGENT_MAIL_CLAIMS`, `ANTHROPIC_MODEL`
and `CLAUDE_CODE_SUBAGENT_MODEL` (from `runner.json`), `PONYTAIL_DEFAULT_MODE`, and `ENABLE_TOOL_SEARCH=false` for the
orchestrator, builder and reviewer.

`agent-mail.py` treats a process as an agent, not a person, when any of these is set: `AGENT_MAIL_AGENT`,
`AGENT_MAIL_PARENT`, `SESSION_HOOK_SKIP`, `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CODEX_SANDBOX`, `CODEX_THREAD_ID`,
`CURSOR_AGENT`.

## Device-local state

Everything under `~/.switchboard/` (on Windows `%USERPROFILE%\.switchboard\`). Keep it out of any sync.

| Path | What |
|---|---|
| `root` | The root's path, written by `setup/doctor.py --fix --root P`. |
| `discord/token.txt` | The Discord bot token, one line. Never synced, never uploaded. |
| `mail-runner-state.json` | Run counts for the hourly and daily caps (`--state-file` changes it). |
| `mail-runner-claims/` | Device-wide claim files shared by every runner process here. |
| `locks/` | Single-instance locks and the repo lock guard. |
| `captures/` | Screen captures waiting to be uploaded. |
| `peers.json` | Optional: the last reading of a liveness relay, used before a standby bridge takes over without a Discord lock. |
| `registry.json` | Memory registry: vault, device note, scopes. |
| `runner-memory.json` | Optional: device-note sections runner jobs may skip. |
| `MEMORY.md` | Optional link to the vault-root rules. |
| `.cache/claude-memory-loader/` | Memory loader session state and error log. |
| `backups/initializer/` | Backups `memoryctl.py init` writes before it changes a route. |
| `skills-sync.log` | `skills-sync.sh` log. |
| `tools/claude-memory-loader.py` | Optional: a device copy of the loader that the runner prefers for scope rules. |

## Commands and flags

### `switchboard/agent-mail.py`

Exit codes: 0 ok, 1 error, 2 wait timed out or bad arguments. Commands that speak as an agent of kind `human`
(`send`, `reply`, `amend`, `answer`, `go-ahead`, `register`) are refused unless a person runs them at a real terminal.

| Command | What it does |
|---|---|
| `agents [--json]` | List the registry with inbox counts. |
| `register NAME --host H[,H2] --kind claude\|codex\|cursor\|human [--auto] [--cwd P] [--description T]` | Add or update an agent. |
| `send --from A --to B --subject S (--body T \| --body-file F\|-) [--repo R] [--reply-to ID] [--needs-reply] [--device HOST] [--option LABEL]...` | Send a message. `--device` pins the run to one device; `--option` adds a button for a human. |
| `inbox AGENT [--json]` | Unread messages. |
| `read AGENT ID [--json]` | One message. |
| `ack AGENT ID [--result T]` | Move it to `done/`. |
| `reply AGENT ID (--body T \| --body-file F) [--needs-reply]` | Reply in the same thread. |
| `wait AGENT --reply-to ID [--timeout 300] [--interval 1]` | Wait for a reply; exit 2 on timeout. |
| `thread ID [--json]` | Every message of a thread. |
| `redirect AGENT ID [--device HOST]` | Move a waiting message to a device; an empty value clears the pin. |
| `amend --from A ID [--text T]` | Edit an earlier message to the owner, or delete it without `--text`. |
| `answer --from A ID OPTION [--why T]` | Answer an open question with option number OPTION, after the owner's go-ahead. |
| `go-ahead --from A [--text T]` | From a session the owner drives: let the orchestrator use its recommendations. |

### `switchboard/agent-mail-mcp.py`

`agent-mail-mcp.py [--agent NAME]`: an MCP server over stdio (JSON-RPC 2.0, protocol 2024-11-05). Without `--agent` it
uses `AGENT_MAIL_AGENT`. Tools: `list_agents`, `send_message`, `list_active_work`, `report_conflict`, `list_devices`,
`ask_owner` (at most 5 options), `update_owner`, `answer_for_owner`, `check_inbox`, `read_message`, `ack_message`,
`reply_message`, `wait_for_reply` (at most 45 seconds per call).

### `switchboard/mail-runner.py`

| Flag | Default | Meaning |
|---|---|---|
| `--poll S` | 20 | Seconds between full passes and loop ticks. Inboxes are looked at every 3 seconds. |
| `--once` | | One pass, then exit. |
| `--dry-run` | | Print what would run; no runs, no mail changes. |
| `--timeout-minutes M` | 20 | Stall limit: a run with no output for this long is stopped. Not a wall-clock limit. |
| `--max-per-hour N` | 0 | Runs per hour on this device; `0` = no cap. |
| `--max-per-day N` | 0 | Runs per day on this device; `0` = no cap. |
| `--max-hops N` | 0 | Hop limit per thread; `0` = none. A message at the limit is acked, not run. |
| `--max-concurrent N` | `runner.json`, else 4 | Runs this process holds at once; at most 16; `0` = 16. |
| `--permission-mode M` | `acceptEdits` | Passed to Claude Code. |
| `--always-reply` | | Also reply to plain answers. Can ping-pong without end unless `--max-hops` is set. |
| `--mcp` | | Give each run its own mail MCP server and allow its tools. |
| `--prompt-via auto\|arg\|stdin` | `auto` | How the prompt reaches Claude Code; `auto` uses stdin for a Windows `.cmd` shim or a very long prompt. |
| `--only a,b` | all | Agents this process serves. One process per `--only` value per device. |
| `--no-loop` | | Do not run the improvement loop scheduler in this process. |
| `--state-file P` | `~/.switchboard/mail-runner-state.json` | Run count file. |

Subcommands: `mail-runner.py requeue AGENT ID` puts an acked message back in the inbox; `mail-runner.py fresh THREAD`
makes the next run in that thread start a fresh session. The runner exits with code 3 to be restarted on new code.

`setup/autostart.py` starts one runner per device (`--poll 10 --mcp`). To keep a long build from delaying replies,
you can instead run the orchestrator in its own process: `mail-runner.py --mcp --only orchestrator` and
`mail-runner.py --mcp --only builder,reviewer --no-loop`.

### Other scripts in `switchboard/`

| Script | Commands and flags |
|---|---|
| `queue-runner.py` | `[--once] [--poll 60] [--dry-run]` |
| `repo-handoff.py` | `claim\|release\|status [REPO] [--note T] [--force]`. Exit codes: 0 ok, 2 held by another device, 3 Syncthing still syncing, 6 held by another run here, 7 another claim in progress here. |
| `discord-bridge.py` | `setup --guild ID`; `run`; `refresh` (have the serving bridge go over every open thread once). |
| `discord-janitor.py` | `[--dry]` (ask the model, print the plan, change nothing); `--selfcheck`. |
| `triage.py` | `JOB.json` (the bridge runs this); `--selfcheck`. |
| `cards.py` | `--selfcheck`. |
| `card-gallery.py` | `post` (a demo thread of every card); `--selfcheck`. |
| `fleet.py` | `devices [--json]`; `beat`; `progress [--hours 24] [--all]`; `where [REPO]`; `stats [--hours H] [--requests]`. |
| `housekeeper.py` | `[--poll 60] [--once] [--dry-run] [--conflicts]`. `--conflicts` works on any system. |
| `improve-loop.py` | `status\|on\|off`; `add NAME [--mode ask\|auto] [--path P --host H --brief T] [--every HOURS] [--deploy T] [--campaigns T]`; `remove NAME`; `confirm NAME [--mode M] [--brief T] [--every HOURS]`; `dismiss NAME`; `discover`; `ignore NAME`; `auto on\|off`; `run NAME`; `outcome NAME --change T --metric T --direction up\|down --baseline T [--days 7] [--ref T]`; `verdict NAME ID improved\|flat\|worse\|wait [--now T] [--days N] [--note T]`; `outcomes NAME`. |
| `route.py` | `"text" [--agent builder] [--relay] [--prior N] [--codex] [--built-by ENGINE] [--from SENDER]`; `--table`. |
| `route-test.py` | Checks the routing rules. Run it after editing `route.py`. |
| `notes.py` | `show THREAD`. |
| `playbook.py` | `add PROJECT "sentence"`; `show PROJECT`; `vote PROJECT good\|bad`. |
| `retro.py` | `[--send]`. |
| `topics.py` | `show [--json]`; `label [--force]`. |
| `session-hook.py` | Hook mode (JSON on stdin); `--codex '<json>'`; `--list`. |
| `permission-hook.py` | Hook mode only. |
| `install-session-hooks.py` | `[--dry-run] [--remove] [--python P] [--no-codex]`. |
| `codex-notify.py` | `--install` wraps the Codex `notify` program already set. |
| `claude-memory-loader.py` | `--chunk N --chunks 8` (hook mode). |
| `memoryctl.py` | `[--registry P] check \| lint \| init ROOT [--memory-dir D]`. |
| `screencap.py` | `test`. |
| `undo-build.py` | `MAIL_ID [--dry-run]`: put back the files one build changed and remove the files it added, from its `undo.json`. Run it on the device that did the build. |

### Setup and tools

| Script | Commands and flags |
|---|---|
| `setup/doctor.py` | `[--fix] [--root P] [--name NAME] [--json]`. `--name` sets the mailbox name for your own sessions (default: the host name). Exit 0 when nothing required is missing. |
| `setup/autostart.py` | `show \| install [--runner] [--bridge] [--queue] [--house] [--permission-mode M] \| remove`. With no component flag, `install` sets up the runner and the bridge. |
| `setup/install-memory.py` | `--vault FOLDER [--hooks]`; `--remove-hooks`; `--dry-run`. |
| `setup/skills-sync.sh` | `[STORE]`. macOS and Linux. |
| `tools/check-clean.py` | `[PATH...]`. Prints `clean` or the problems; a line may opt out with `check-clean: ok`. |

### Tests

| Command | What it covers |
|---|---|
| `python3 switchboard/discord-bridge-test.py [name-substring]` | Bridge and runner against a fake Discord and a temporary root. A full unfiltered pass writes the tested marker for hot reload. |
| `python3 switchboard/concurrency-test.py [substring]` | Runner slots, claims and lock races. |
| `python3 switchboard/route-test.py` | Routing rules. |
| `python -m pytest tests -q` | Unit tests. |

## Model routing

`route.py` picks the engine, model and effort for each run by rules, with no model call. `PLAN` in `route.py` is the
default policy: for each kind of job, the models that fit it, best first. The runner takes the first one that is
available (Codex installed and allowed, Fable not off or capped) and always names a backup. This is the output of
`python3 switchboard/route.py --table`:

| Job | First choice | Backups |
|---|---|---|
| chat | claude-sonnet-5-5 medium | claude-opus-5-5 medium |
| plan_light | claude-sonnet-5-5 medium | claude-opus-5-5 medium |
| plan | claude-opus-5-5 high | claude-fable-5-2 medium |
| fable_plan | claude-fable-5-2 medium | claude-opus-5-5 xhigh |
| mini_review | claude-opus-5-5 medium | claude-fable-5-2 low |
| mini_review_hard | claude-opus-5-5 high | claude-fable-5-2 medium |
| build_cheap | gpt-6.1-sol medium | claude-sonnet-5-5 medium, claude-opus-5-5 medium |
| build_big | gpt-6.1-sol xhigh | claude-opus-5-5 medium, gpt-6-astra high |
| frontier | gpt-6-astra high | claude-fable-5-2 medium, claude-opus-5-5 xhigh |
| focus | gpt-6-astra xhigh | claude-fable-5-2 high, claude-opus-5-5 xhigh |
| review_of_claude | gpt-6.1-sol xhigh | claude-fable-5-2 medium, claude-opus-5-5 high |
| review_of_claude_hard | gpt-6-astra high | claude-fable-5-2 high, claude-opus-5-5 high |
| review_of_codex | claude-fable-5-2 medium | claude-opus-5-5 high |
| review_of_codex_hard | claude-fable-5-2 high | claude-opus-5-5 high |

With `routing.codex_builds` false (the default), the Codex rows are skipped for builds, so builds go to the first
Claude entry.

How a row is chosen, in order:

1. A model you named in the thread ("use fable high", "with astra high") wins, with the rules' pick as backup.
2. A model the planner named for a build (`MODEL: sol high, <why>`) is the choice, with the rules' pick as backup.
3. Orchestrator steps: `chat` for hand-offs of another agent's mail, `mini_review` or `mini_review_hard` for a
   builder's or reviewer's answer, `plan_light` for a small question, `plan` for a full plan, `fable_plan` for
   complex, analysis or focus work.
4. A request on its fourth build or later escalates to the top model of the other family.
5. The planner's `TIER: cheap|big|frontier` picks `build_cheap`, `build_big` or `frontier`.
6. Analysis work goes to `frontier`, a request for focus to `focus`, a complex or high-risk build to `build_big`, and
   everything else to `build_cheap`. Fix rounds keep the model so the builder's session continues.
7. A full review uses a `review_of_*` row: the other family from whoever built.

To change the policy, edit `PLAN` in `route.py` and run `route-test.py`, or override rows without touching code with
`routing.plan` in `runner.json`. Each row is a list of `[engine, model, effort]`, best first; rows you leave out keep
their defaults:

```json
{"routing": {"plan": {"build_big": [["claude", "claude-opus-5-5", "high"], ["claude", "claude-sonnet-5-5", "medium"]],
                      "plan": [["claude", "claude-opus-5-5", "medium"]]}}}
```

`models` and `model` in `runner.json` set the model for agents when routing is off (`routing.enabled` false).

## Adding or changing an agent

1. **Register it** from a terminal (registering a `claude` agent needs no person check):

   ```
   python3 switchboard/agent-mail.py register docs-writer --host Desk-PC,Laptop-Mac --kind claude --auto \
     --description "Writes and updates documentation."
   ```

   `--host` must name every device whose runner should answer it. Without `--auto` the agent is a mailbox only.
2. **Write its role file** at `Queue/mail/roles/<name>.md`. Start from a file in `templates/roles/`: `builder.md` for
   an agent that writes files, `reviewer.md` for one that only reads. Keep the core short and put long reference
   material in `Queue/mail/roles/ref/*.md`, which the role tells the agent to read when relevant. Keep the
   untrusted-text rule from the templates. The runner reads the role file on every run, so edits apply to the next
   run.
3. **Know what it gets.** The runner gives every automatic agent its role, the thread brief, the notepad, other work
   in flight, playbook entries and memory note paths. Any agent other than `orchestrator` and `reviewer` counts as a
   writer: it takes the repo or area claim. The lean tool lists and the routing rows are written for `orchestrator`,
   `builder` and `reviewer`; another agent runs without a restricted tool list and is routed like a build.
4. **Address it.** From Discord, start a message with its name and a colon (`docs-writer: update the setup page`).
   From another agent, `send_message` to it. From a terminal, `agent-mail.py send --to docs-writer`.
5. **To change one**, run `register` again with the new values (the first spelling of the name is kept), or edit its
   role file. To stop a device answering it, register it again without that host. `agent-mail.py agents` shows the
   result.

Engine order per agent is set in `Queue/mail/engines.json`, and its model in `runner.json` `models` when you do not want
the routing table to choose.
