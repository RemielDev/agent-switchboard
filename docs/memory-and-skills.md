# Shared memory and skills

Two small, optional pieces keep every agent on every device working from the same rules. Neither is needed for the
mail, runner and Discord parts.

## Memory: one vault, scoped rule files

The memory is one folder of Markdown notes, the **vault**. Any folder works; many people use an Obsidian vault
because it syncs and links well, but nothing here needs Obsidian. Rules live in files named `MEMORY.md`:

```
vault/
  MEMORY.md                    rules for every task, everywhere
  Local/This Device.md         facts about one device; keep Local/ out of your sync
  Projects/
    shop-app/MEMORY.md         rules for one project
```

`switchboard/claude-memory-loader.py` is a Claude Code hook. When a session starts in a folder it sends, widest
first: the device note, the vault-root `MEMORY.md`, and the `MEMORY.md` of every scope the folder belongs to.
Narrower files win. After that it re-sends a file only when it changed, and when a session starts editing in a
different project it sends that project's rules before the first edit goes through. Runner turns are told in their prompt which
rule files apply to the folder they work in and read them first, so the orchestrator, builder and reviewer follow
the rules you gave your interactive sessions.

Why one vault with links instead of a `CLAUDE.md` in every repo: one home per fact. A rule stated once reaches every
tool and device, and a correction does not have to be repeated in five places.

### Set it up

```
python3 setup/install-memory.py --vault ~/memory-vault             # starter vault + ~/.switchboard/registry.json
python3 setup/install-memory.py --vault ~/memory-vault --hooks     # also add the loader to ~/.claude/settings.json
python3 switchboard/memoryctl.py init ~/Developer/shop-app         # give one project its own MEMORY.md in the vault
python3 switchboard/memoryctl.py check                             # verify every route
python3 switchboard/memoryctl.py lint                              # broken links, device paths in shared notes, tags
```

`--hooks` changes `~/.claude/settings.json` (a backup is written first, and `--remove-hooks` undoes it), so an AI
agent doing the setup should ask before running it. `--dry-run` shows the change.

### The registry

`~/.switchboard/registry.json` tells the loader where things are:

```json
{
  "vault": "/Users/you/memory-vault",
  "device_note": "Local/This Device.md",
  "scopes": [
    {"root": "/Users/you/Developer/shop-app",
     "canonical": "/Users/you/memory-vault/Projects/shop-app/MEMORY.md",
     "kind": "project",
     "recall": "/Users/you/memory-vault/Projects/shop-app"}
  ]
}
```

- `vault`: the folder of notes. Its root `MEMORY.md` applies to every task.
- `device_note` (optional): this device's own note, absolute or relative to the vault. It is sent first on every
  session here. Keep its folder out of sync so each device has its own.
- `scopes`: `root` is a workspace folder and `canonical` is the vault `MEMORY.md` that governs it and everything
  under it. `memoryctl.py init` adds these for you and links `<root>/MEMORY.md` to the vault file so other
  tools find the same rules. It also writes `<root>/.claude/settings.local.json` there, so Claude Code's own auto memory for that project lands in
  the vault. It refuses a folder that already has its own `CLAUDE.md` or `AGENTS.md`: move those
  rules into the vault first, so there is one home for them.

The registry is per device (paths differ between machines); the vault is shared.

## Skills: one folder, linked into every tool

Claude Code, Codex and Cursor each look for skills in their own folder. `setup/skills-sync.sh` (macOS and Linux)
makes one shared folder the home of all of them:

```
setup/skills-sync.sh                 # uses <root>/agent-skills
setup/skills-sync.sh ~/my-skills     # or any folder you name
```

- `~/.agents/skills` becomes a link to the shared folder, which Codex and Cursor read directly.
- Claude Code gets one link per skill in `~/.claude/skills`.
- A skill you create inside one tool's own folder is moved into the shared folder once it has stopped changing, so
  it reaches the other tools, and through your file sync, your other devices.

Put the shared folder inside the root you already sync and a skill edited on one device is on the others a moment
later. Run the script after adding a skill, or from a timer. Skills with their own dependencies (`node_modules`,
`.venv`) need their install step once per device; keep those folders out of the sync.
