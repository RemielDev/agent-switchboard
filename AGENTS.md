# Setting up Agent Switchboard: instructions for an AI agent

You are an AI coding agent (Claude Code, Codex, Cursor or similar) and the person you work for, **the owner**, asked
you to set this repository up on their computer. Work through this file in order. It is written so you can do
almost everything yourself; the steps only the owner can do are marked **OWNER STEP** and are few.

## Ground rules

- **Ask before anything that persists or widens access.** Starting programs at login (`setup/autostart.py`),
  raising the runner's permission mode above `acceptEdits`, changing `~/.claude/settings.json` (the hooks
  installer) and pairing devices are the owner's decisions. Explain what the step does in one or two sentences,
  then wait for a yes.
- **Never handle the Discord bot token.** Do not ask the owner to paste it into the chat, do not print it, do not put
  it in the shared `Queue/` folder or in any repository. The owner saves it into a file themselves (step 5).
- **Do not install software silently.** Tell the owner what is missing and the command you will run.
- **Change nothing outside this checkout, the root's `Queue/` folder and `~/.switchboard/`** unless a step says so.
- `setup/doctor.py` is the source of truth. Run it after every step; it is safe to run as often as you like.

## Step 0. Ask the owner four things

1. **Root folder.** Which folder holds their project folders (their "repos")? Example: `~/Developer`. The shared
   state folder `Queue/` will be created inside it, and every folder directly under it becomes a repo that agents can
   be sent to. If they have no such folder, propose `~/switchboard-root`.
2. **One device or several?** With several, the root must be a folder they sync between the devices (step 9).
3. **Discord?** It is the comfortable way to use this (phone and desktop), and optional: everything also works from
   a terminal. Discord needs about ten minutes of the owner's time once.
4. **Start at login?** Whether the runner and the bridge should start by themselves on this device.

## Step 1. Check what is installed

| Needed | Check | If missing |
|---|---|---|
| Python 3.11 or newer | `python3 --version` | python.org, Homebrew (`brew install python`) or winget (`winget install Python.Python.3.12`) |
| Claude Code CLI, signed in | `claude --version`, then `claude -p "say ready"` | https://docs.claude.com/en/docs/claude-code/setup (signing in is an **OWNER STEP**) |
| git | `git --version` | the platform's usual installer |

Optional, only if the owner wants them: the Codex CLI (a second engine for builds and reviews), Syncthing (shares
the root between devices), Tailscale (lets devices reach each other away from home), and a small-model CLI named
`agy` that the Discord bridge can use for triage and tidying. Everything degrades to the plain path without them.

No Python packages are needed. Every script uses the standard library only. One optional exception on Windows, which ships no
time zone database: `python -m pip install tzdata` lets the runner read a usage-limit reset time that names another
time zone; without it such times are read as local time.

## Step 2. Put the checkout inside the root

```
cd <root>
git clone https://github.com/RemielDev/agent-switchboard.git
cd agent-switchboard
```

A checkout that sits directly inside the root needs no configuration: the scripts find the root as the folder that
contains the checkout, and on a second device the same layout works again. If the checkout must live elsewhere, pass
`--root <root>` to the doctor in the next step; it remembers the path in `~/.switchboard/root`. The environment
variable `SWITCHBOARD_ROOT` overrides both.

## Step 3. Let the doctor set up the basics

```
python3 setup/doctor.py          # look first
python3 setup/doctor.py --fix    # then let it fix what it can
```

`--fix` creates `<root>/Queue/`, installs the role files (`templates/roles` to `Queue/mail/roles`), writes default
config files, and registers the mail agents: `owner` (the human), `orchestrator`, `builder`, `reviewer` (run
automatically on this device), `sessions` (used by the hooks) and one named after this device for the owner's own
interactive sessions. It never overwrites a file that exists. Lines marked TODO are the steps below.

## Step 4. Prove the core works, without Discord

Send the orchestrator one message as this device's interactive agent (use the agent name the doctor printed for
"your interactive sessions"; it is the lower-case host name), run the runner once, and read the answer:

```
python3 switchboard/agent-mail.py send --from <device-agent> --to orchestrator --subject "Hello" --body "Reply with the single word: ready." --needs-reply
python3 switchboard/mail-runner.py --once --mcp
python3 switchboard/agent-mail.py inbox <device-agent>
```

The second command starts one headless Claude Code run and takes a minute or two. An answer in the inbox means
mail, the runner and Claude Code all work. Show the owner the answer. If the run fails, read the runner's output:
the usual causes are Claude Code not being signed in, or a usage limit.

You cannot send as `owner` from here, by design: the mail CLI refuses to speak as a person unless a person is
typing at a real terminal. That is what stops agents from approving their own questions.

## Step 5. Connect Discord (skip if the owner said no)

Follow `docs/discord-setup.md`. In short:

1. **OWNER STEP**: create a private Discord server (or pick one only they are in), create an application and bot at
   https://discord.com/developers/applications, turn on the **Message Content Intent**, invite the bot with the
   link from `docs/discord-setup.md`, and save the bot token into `~/.switchboard/discord/token.txt` themselves.
2. **OWNER STEP**: give you the server id (Discord: Settings, Advanced, Developer Mode; then right-click the server,
   Copy Server ID). The id is not a secret.
3. You: `python3 switchboard/discord-bridge.py setup --guild <server id>`. It creates the channels, records the
   server owner as the only person the bridge obeys, and registers the slash commands.
4. You: `python3 switchboard/discord-bridge.py run` in a terminal the owner can see, and ask the owner to type
   a request in `#orchestrator`. With the runner also running (`python3 switchboard/mail-runner.py --mcp`), the
   request gets its own thread, a status message and then the answer.

## Step 6. Session hooks (optional, ask first)

`python3 switchboard/install-session-hooks.py --dry-run` shows the change; without `--dry-run` it adds hook entries
to `~/.claude/settings.json` (and a `notify` line for Codex when it has none), after copying each file to a backup.
With the hooks, the owner's own interactive sessions appear in Discord, and a permission request that arrives
while they are away from the keyboard is forwarded to them with Allow once and Deny buttons. `--remove` undoes it.

## Step 7. Mail tools inside the owner's own sessions (optional)

```
claude mcp add --scope user agent_mail -- python3 <root>/agent-switchboard/switchboard/agent-mail-mcp.py --agent <device-agent>
```

After that an interactive Claude Code session can list agents, send work to the orchestrator and read replies.

## Step 8. Start at login (only after a yes)

```
python3 setup/autostart.py show       # prints exactly what would be installed
python3 setup/autostart.py install    # the mail runner and the Discord bridge
python3 setup/autostart.py remove     # undo
```

Until then the owner starts the two processes by hand when they want agents available.

## Step 9. A second device (optional)

Read `docs/multi-device.md`. The short version: share the root folder between the devices (Syncthing works well),
keep each repo's heavy or generated folders out of the sync, then repeat steps 1 to 8 on the second device. The
doctor adds that device's host to the automatic agents, and the device that runs the bridge second stays on standby
and takes over only when the first stops. The bot token is saved separately on each device.

## Step 10. Shared memory and skills (optional)

`docs/memory-and-skills.md` explains the two small pieces: a loader that feeds scoped `MEMORY.md` rule files to
Claude Code sessions and runner turns, and `setup/skills-sync.sh`, which keeps one shared folder of agent skills
linked into every tool.

## When you are done

Run `python3 setup/doctor.py` once more and give the owner a short report: what is set up, what is still TODO and
why, how to start and stop things (`Queue/STOP` pauses every runner), and where the docs are. Tell them to read
`SECURITY.md` before raising the permission mode or enabling start at login.

## If something is wrong

- **The runner starts nothing.** Is the message addressed to an agent with `auto` true on this host
  (`python3 switchboard/agent-mail.py agents`)? Does `Queue/STOP` exist? Is the device listed
  (`python3 switchboard/fleet.py devices`)?
- **The bridge runs but ignores messages.** Only the server owner is obeyed; the Message Content Intent must be on;
  the bot needs the channel permissions listed in `docs/discord-setup.md`.
- **Two devices both answer.** Check both list the same `hosts` in `Queue/discord/config.json` and that the root
  really syncs (`python3 switchboard/fleet.py devices` on each shows both).
- **Tests.** `python3 switchboard/discord-bridge-test.py <part of a test name>` runs against a fake Discord and a
  temporary root, so it is always safe to run.
