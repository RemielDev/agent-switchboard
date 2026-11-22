# Security

Agent Switchboard starts AI coding agents on your computers and lets you steer them from Discord. Read this before
you run it unattended.

## What you are trusting

- **The agents run as you.** A run started by `mail-runner.py` is a normal Claude Code (or Codex) process under your
  user account, in the folder of the repo it works on. It can do whatever that CLI's permission mode and your
  settings allow. The runner's default is `--permission-mode acceptEdits`: file edits are allowed, other tools need
  an allow rule in your Claude Code settings. Raising the mode (`auto`, `bypassPermissions`) is your decision; do it
  per device and only on a machine where you accept the result.
- **Your Discord bot token is a password.** It lives only in `~/.switchboard/discord/token.txt` on each device that
  runs the bridge. It is never read from the shared `Queue/` folder and must never be put there or committed.
- **Only the server owner is obeyed.** `discord-bridge.py setup` records the owner of the Discord server, and the
  bridge ignores messages, button presses and slash commands from everyone else. Use a private server that only
  you are in.
- **The shared `Queue/` folder is the control plane.** Anyone who can write files there can queue work for your
  agents. Share it only between your own devices, over a private sync (for example Syncthing over a private
  network), never through a public or team drive.

## What it reads on your machine

- **Your own session history, for the improvement loop.** `improve-loop.py` looks at which folders your interactive
  Claude Code and Codex sessions worked in (folder names, counts, and a short list of what you asked there lately) so
  it can propose a project for the loop. It writes that to `Queue/loop/activity/<host>.json`, which is shared with your
  other devices and nobody else. Nothing joins the loop without your Add. `python3 switchboard/improve-loop.py auto off`
  stops the proposals.
- **Your session status, for the board.** With the session hooks installed, each interactive session's folder, state
  and last prompt are written to `Queue/sessions/` and shown in your `#sessions` channel.
- **Screen captures are off.** The bridge can post one capture of a device where work is blocked; `setup` writes
  `"screenshots": false`, and it stays off until you change it. The bridge and the runner still take a small lock file
  named `screencap-service` under `~/.switchboard/locks/`; it only decides which process would answer a capture
  request and captures nothing by itself.

## What the code enforces

- **Agents cannot pose as you through the mail CLI.** `agent-mail.py` refuses to send, reply, amend or register as
  an agent of kind `human` unless it runs at a real terminal outside any agent. Programs that speak for you (the
  bridge, the improvement loop, the permission hook) call the library directly. This closes the CLI route only:
  agents share your OS user, so a determined process could still write a mail file by hand. Treat it as a guard
  against mistakes, not as a sandbox.
- **Risky buttons are never pressed for you.** When you tell the orchestrator to "do your recommendations", it may
  answer its own open questions, but the bridge refuses that for buttons that deploy, push, publish, spend or delete.
- **The permission hook never approves anything by itself.** `permission-hook.py` only forwards a permission request
  to you with Allow once / Deny buttons when you are away from the keyboard; no answer means the local dialog decides.
- **Untrusted text is data.** The role files tell every agent that attached files, web pages and tool output cannot
  give instructions, and to quote such text to you instead of acting on it. This is a prompt-level rule; it reduces
  prompt injection risk and does not remove it.
- **One writer per repo or area**, a `Queue/STOP` file that pauses every runner, per-message timeouts, an optional hop
  limit and optional hourly and daily run caps limit how far a mistake can spread.

## Recommendations

- Start with one device, `acceptEdits`, and no autostart. Watch a few requests end to end before you let it run
  at login (`setup/autostart.py`) or raise the permission mode.
- Keep repos that hold production credentials out of the root, or give the agents their own OS user.
- Do not point the root at a folder that other people can write to.
- Run `python3 tools/check-clean.py` before you push a fork: it refuses tokens, real Discord ids, home paths and
  private addresses.

## Reporting a problem

Open a private security advisory on the GitHub repository (Security tab, "Report a vulnerability"). Please do not
file a public issue for something that lets a third party run work on someone's machine.
