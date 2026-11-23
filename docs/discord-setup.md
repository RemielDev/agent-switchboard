# Connecting Discord

Discord is the front end: you type a request in a channel, it becomes a thread, and the agents answer there with
status, questions as buttons, and files. The bridge is one small process (`switchboard/discord-bridge.py`) that
turns agent mail into Discord messages and back. Agents never see Discord; they only read and write mail.

Setting it up takes about ten minutes, once.

## 1. A private server

Use a server that only you are in. The bridge obeys exactly one person, the server's owner, and ignores everyone
else, but the channels show your work, file names and answers. Create one in Discord with the + button if you do
not have one.

## 2. The bot

1. Open https://discord.com/developers/applications and choose **New Application**. Name it anything.
2. **Bot** tab: under **Privileged Gateway Intents** turn on **Message Content Intent**. The bridge has to read what
   you type. The other two privileged intents stay off.
3. Still on the Bot tab, press **Reset Token** and copy the token. Save it on your computer in the file
   `~/.switchboard/discord/token.txt` (on Windows: `%USERPROFILE%\.switchboard\discord\token.txt`), as one line and
   nothing else. This token is a password: do not paste it into a chat, a repository or the shared `Queue/` folder.
   With several devices, save it on each one that will run the bridge.
4. Invite the bot to your server. In the **OAuth2** tab copy the **Client ID**, put it into this link and open it:

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot+applications.commands&permissions=395137100880
   ```

   The number asks for exactly these permissions: View Channels, Send Messages, Send Messages in Threads, Create
   Public Threads, Create Private Threads, Manage Threads, Manage Messages, Read Message History, Add Reactions,
   Attach Files and Manage Channels. Manage Channels is used once by `setup` to create the channels and afterwards to keep their
   topics current; Manage Messages and Manage Threads are what let the bridge tidy its own cards, remove Discord's
   automatic notices and archive finished threads.

## 3. Set up the channels

Turn on Developer Mode in Discord (Settings, Advanced), right-click your server and choose **Copy Server ID**. Then:

```
python3 switchboard/discord-bridge.py setup --guild YOUR_SERVER_ID
```

`setup` looks for these channels by name, creates the ones that are missing, records the server's owner as the
person the bridge obeys, registers the slash commands, creates the `owner` mailbox that agents write to, and makes one small thread
under `#alerts` named "bridge lock" (private when the bot may create private threads): leave it alone, it is how two devices agree on which one serves. It is safe to run again; channels you moved or renamed
stay where you put them.

| Channel | What it is for |
|---|---|
| `#orchestrator` | You type here. Each request gets its own thread; keep talking inside the thread. |
| `#overview` | Everything in flight, one card per topic, updated in place. |
| `#progress` | One card per piece of work: working, done, cut off, failed, waiting. |
| `#sessions` | Your own Claude Code and Codex sessions on every device (needs the session hooks). |
| `#loops` | One card per improvement loop. Type here to add or stop a loop. |
| `#alerts` | Failures and anything that needs you. |
| `#reports` | Full reports from queue cards. |

The result is written to `<root>/Queue/discord/config.json`, which is shared between your devices. The token is not.

## 4. Run it

```
python3 switchboard/discord-bridge.py run
python3 switchboard/mail-runner.py --mcp        # in a second terminal: the agents
```

Type a request in `#orchestrator`. To start both at login, see `setup/autostart.py` (and read `SECURITY.md` first).

## Using it

- **A request** is any message in `#orchestrator`. It goes to the orchestrator agent. Start a message with an agent's
  name and a colon (`builder: ...`) to address that agent directly.
- **Inside a thread**, anything you type continues that piece of work. While a question with buttons is open, typed
  text answers it.
- **Questions** arrive as buttons. A question is always the last message in its thread. The same questions also show in
  your own Claude Code sessions with your next message there; answer at either end and the other closes.
- **"Do your recommendations"**, typed in `#orchestrator` or in a thread, lets the orchestrator answer its own open
  questions with the option it recommends. Buttons that deploy, push, publish, spend or delete are never pressed
  for you.
- **Slash commands:** `/status`, `/devices`, `/progress`, `/agents`, `/usage`, `/stop` (pause every agent),
  `/go` (resume), `/loop` (the improvement loop) and `/run` (give the orchestrator a task, optionally on one device).
- **Files:** attach a file to your message and the agents get it. Agents send files back into the thread.

## Options

`<root>/Queue/discord/config.json` holds the ids `setup` wrote plus switches you can add. See
`docs/configuration.md` for every key. The ones most people touch:

| Key | Effect |
|---|---|
| `hosts` | The devices that may run the bridge, in priority order. The first one that is up serves; the rest stand by. |
| `one_answer_per_turn` | One answer message per message of yours, edited in place, instead of a stream of messages. |
| `progress_boards` | Group `#progress` into a few category messages. |
| `notepad` | Messages you send in quick succession are read together as one note. |
| `triage`, `janitor` | Optional small-model helpers (need the `agy` CLI). Off unless you turn them on. |

## Two devices, one bot

Run the bridge on both and list both under `hosts`. The serving bridge keeps a timestamp current in the "bridge
lock" thread; the other device reads it straight from Discord and serves only when it has gone stale. You never get
two answers. See `docs/multi-device.md`.

## Without Discord

Nothing depends on Discord. From a terminal, `python3 switchboard/agent-mail.py inbox owner` shows what the agents
sent you, `reply` answers, and `python3 switchboard/fleet.py progress` shows the same board as `#progress`.
