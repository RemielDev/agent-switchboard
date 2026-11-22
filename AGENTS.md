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

## Commands on your platform

- This guide writes `python3`. On Windows type `python` (or `py -3`) instead. The doctor prints every follow-up
  command the way it must be typed on this machine, with full paths, so copy its `->` lines rather than retyping.
- `~` is the home folder (`%USERPROFILE%` on Windows).
- Run the commands from the checkout folder unless a full path is given.
- macOS and Windows are the supported platforms. On Linux the mail, runner and bridge work, `setup/autostart.py`
  prints a systemd user unit for you to install by hand, and screen captures and the housekeeper are not available.

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
| Python 3.11 or newer | `python3 --version` (Windows: `python --version`) | python.org, Homebrew (`brew install python`), winget (`winget install Python.Python.3.12`), or on Linux your distribution's package or `uv python install 3.12` |
| Claude Code CLI, signed in | `claude --version` (step 4 proves it is signed in, so no extra test run is needed) | https://docs.claude.com/en/docs/claude-code/setup (signing in is an **OWNER STEP**) |
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

**The folder you clone into is the root.** A checkout that sits directly inside the root needs no configuration:
the scripts take the folder that contains the checkout as the root, and on a second device the same layout works
again. After the next step, check that the doctor's `root` line names the folder the owner gave you. If the checkout must live elsewhere, pass
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

Send the orchestrator one message as this device's interactive agent, run the runner once, and read the answer.
`<device-agent>` is the name the doctor printed on its line "your interactive sessions on this device" (the
lower-case host name).

```
python3 switchboard/agent-mail.py send --from <device-agent> --to orchestrator --subject "Hello" --body "Reply with the single word: ready." --needs-reply
python3 switchboard/mail-runner.py --once --mcp
python3 switchboard/agent-mail.py inbox <device-agent>
python3 switchboard/agent-mail.py read <device-agent> <id from the inbox line>
```

What each one prints when it works:

1. `send` prints the new message's id, for example `20270105T101500Z-3fa9c2d1`.
2. The runner prints a `start ...` line and, 10 seconds to 2 minutes later, `done ... replied to <device-agent>`
   (in its first line, `caps=0/h 0/day` means no run caps are set). It
   starts one real headless Claude Code run, so it uses a little of the owner's Claude usage (a few cents at API
   prices). If it prints `nothing to do`, the message did not reach an automatic agent: check the `--to` name.
3. `inbox` lists one line per waiting message: id, sender, subject.
4. `read` prints the message; its last lines are the orchestrator's answer. Show the owner that answer.

Tell the owner before you run it: the run happens in the root folder with permission mode `acceptEdits`, so the
orchestrator may read anything under the root and edit files there if a request asks it to. This test request only
asks for one word. If the run fails, read the runner's output: the usual causes are Claude Code not being signed in,
or a usage limit.

You cannot send as `owner` from here, by design: the mail CLI refuses to speak as a person unless a person is
typing at a real terminal. That is what stops agents from approving their own questions.

## Step 5. Connect Discord (skip if the owner said no)

Two things here only the owner can do. If they are not at the keyboard, finish the other steps and hand them this
message, filled in (the doctor's `discord token` line has the exact token path on this machine; `doctor --fix`
already created that folder, so the owner only saves one file):

> To connect Discord I need two things from you, about ten minutes in total. The full steps are in
> `docs/discord-setup.md` in the checkout.
> 1. Create a bot: https://discord.com/developers/applications, New Application, Bot tab, turn on **Message Content
>    Intent**, press **Reset Token**, and save the token as a single line in the file `<token path>`. Do not paste
>    the token to me or anywhere else.
> 2. Invite the bot to a private server that only you are in, using the invite link in `docs/discord-setup.md`
>    with your application's Client ID, then send me the **server id** (Discord Settings, Advanced, turn on Developer
>    Mode; right-click the server, Copy Server ID). The id is not a secret.

When you have the server id and the token file exists:

1. `python3 switchboard/discord-bridge.py setup --guild <server id>`. It creates the channels, records the server
   owner as the only person the bridge obeys, registers the slash commands and creates the `owner` mailbox. It ends
   with a line that starts `setup ok:`. A missing token or a wrong id stops with a plain message saying which.
2. Start the bridge and the runner, each in its own terminal the owner can see, and leave both running:
   `python3 switchboard/discord-bridge.py run` and `python3 switchboard/mail-runner.py --mcp`. The bridge prints
   `gateway connected`.
3. Ask the owner to type a request in `#orchestrator`. It gets its own thread, a status message and then the answer.

## Step 6. Session hooks (optional, ask first)

`python3 switchboard/install-session-hooks.py --dry-run` shows the change and writes nothing. Without `--dry-run` it
adds hook entries to `~/.claude/settings.json` (and a `notify` line for Codex when it has none), after copying each
file to a backup. With the hooks, the owner's own interactive sessions appear in Discord, and a permission request
that arrives while they are away from the keyboard is forwarded to them with Allow once and Deny buttons. `--remove`
undoes it.

If the doctor says the hooks are "installed from ANOTHER checkout", this machine already has a copy of this project
somewhere else. Installing would point the hooks at this checkout instead; the dry run lists each one with what it
replaces. Ask the owner which checkout should own them before you change anything.

## Step 7. Mail tools inside the owner's own sessions (optional)

The doctor prints the exact command on its line "mail tools for your own sessions": a `claude mcp add ...` with this
machine's Python and the full path to `agent-mail-mcp.py`. Use that line as printed (it is correct on Windows too).
It registers an MCP server in the owner's Claude Code user settings, so ask first. After that an interactive Claude
Code session can list agents, send work to the orchestrator and read replies.

## Step 8. Start at login (only after a yes)

```
python3 setup/autostart.py show       # installs nothing; prints each login item: the command, when it runs, its log, how to undo
python3 setup/autostart.py install    # adds them
python3 setup/autostart.py remove     # undo
```

Show the owner the `show` output and wait for a yes. The mail runner is always included; the Discord bridge is added
only once Discord is set up on this device, because before that it would only exit at every login. On Linux `install`
prints a systemd user unit and the two commands to enable it instead of installing anything. Until the owner says
yes, they start the processes by hand when they want agents available.

## Step 9. A second device (optional)

Read `docs/multi-device.md`. The short version: share the root folder between the devices (Syncthing works well),
keep each repo's heavy or generated folders out of the sync, then repeat steps 1 to 8 on the second device. The
doctor adds that device's host to the automatic agents, and the device that runs the bridge second stays on standby
and takes over only when the first stops. The bot token is saved separately on each device.

## Step 10. Shared memory and skills (optional)

`docs/memory-and-skills.md` explains the two small pieces: a loader that feeds scoped `MEMORY.md` rule files to
Claude Code sessions and runner turns, and `setup/skills-sync.sh` (bash: macOS and Linux), which keeps one shared
folder of agent skills linked into every tool (create the shared folder first, for example `<root>/agent-skills`;
without it the script says so and does nothing). Reading the doc, `memoryctl.py check` and `memoryctl.py lint` change
nothing and need no permission. Two things to tell the owner before you run them, because they write
outside this checkout: `setup/install-memory.py --hooks` changes `~/.claude/settings.json` (it has a `--dry-run`), and
`switchboard/memoryctl.py init <project>` adds a `MEMORY.md` link and a `.claude/settings.local.json` inside that
project folder.

## When you are done

Run the doctor once more and give the owner a short report in this shape:

> **Set up:** Agent Switchboard in `<root>/agent-switchboard`, root `<root>`. Agents registered: orchestrator,
> builder, reviewer. Test message answered: "<the answer>".
> **Not done yet, and why:** (for example) Discord: needs your bot token and server id, see my message above.
> Start at login: not installed, you asked to see it first; here is what it would add: ...
> **How to use it now:** start `python3 switchboard/mail-runner.py --mcp` (and `discord-bridge.py run` once Discord
> is set up), then type in `#orchestrator`. To pause every agent, create the file `<root>/Queue/STOP`; delete it
> to resume.
> **What it can touch:** agents run as you, in permission mode `acceptEdits`, inside `<root>`. Read `SECURITY.md`
> before raising that mode or turning on start at login.
> **What I changed on this machine:** the checkout, `<root>/Queue/`, and `~/.switchboard/` (list anything else).

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
