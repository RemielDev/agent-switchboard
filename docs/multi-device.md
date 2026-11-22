# Running on two or more computers

One computer is enough. Add a second when you want an always-on worker that does the long runs while your laptop
sleeps, or when some work needs a tool only one machine has.

## Do you need this at all?

- **One worker, steered from anywhere:** run the runner and the bridge on one always-on computer and talk to it
  from Discord on your phone or laptop. No file sync, nothing to set up on the other devices. Start here.
- **Two workers:** both computers can run agents. They share state through one synced folder and coordinate
  through files in it. That is what the rest of this page covers.

## The one rule: share the root

Every device sees the same **root** folder: your project folders plus `Queue/`, the shared state. The scripts never
talk to each other over the network. They read and write files under `Queue/`, and your file sync carries them:

- mail (`Queue/mail`), so a message sent on one device can be answered on another;
- heartbeats (`Queue/devices`), so each device knows which others are up and what repos they hold;
- claims and locks, so one writer works in a repo or area at a time, whichever device it runs on;
- the progress log, Discord state and notes, so either device can serve Discord.

Any sync that keeps a folder identical on your devices works. [Syncthing](https://syncthing.net) is a good fit:
it is direct between your machines, with no cloud in the middle.

1. Install Syncthing on each device and share the root folder between them.
2. Copy [`templates/stignore.example`](../templates/stignore.example) to `<root>/.stignore` on every device and
   adjust it. Keep heavy, generated and machine-specific content out of the sync.
3. On each device run `python3 setup/doctor.py --fix` from the checkout inside the root. It adds that device to the
   automatic agents' host lists.
4. Save the Discord bot token on each device that will run the bridge (`~/.switchboard/discord/token.txt`). The
   token is never synced.
5. List the devices that may serve Discord, in priority order, under `hosts` in `<root>/Queue/discord/config.json`.

Away from home, devices still have to reach each other for the sync to flow. A private network such as
[Tailscale](https://tailscale.com) does that without opening ports; Syncthing can also relay by itself.

## Source code and file sync

File sync and git do not mix well: never let two tools both think they own a `.git` folder. Pick one of these:

- **Sync the working tree, keep git on one device.** The example ignore file excludes `.git`. Your main device
  commits and pushes; the other device edits files that sync back. Simple, and what the "one writer per repo"
  rule is designed around.
- **Clone each repo on each device with git, and do not sync the repo folders at all.** Then ignore those folders
  in `.stignore` and share only `Queue/`, the checkout and your skills. Work then has to be pulled and pushed by
  the agents, and a file an agent attaches from inside a repo can only be uploaded by a bridge running on the
  device that holds it. Ask agents to put deliverables under `Queue/` in that setup.

## How the devices coordinate

- **Which device runs a message.** An agent's `host` in `Queue/mail/agents.json` can be a list. A message runs on
  the device it is pinned to (`device`), else on the first available listed host that holds the repo and is not
  under memory pressure. The orchestrator can pin work to a device that has the tool it needs.
- **One writer per repo or area.** Before a writing run starts, the runner takes a claim under `Queue/mail/claims`
  that every device honours. Read-only work and work in different repos runs side by side.
- **One serving bridge.** The first host in `hosts` serves. It keeps a timestamp current in a small "bridge lock"
  thread that `setup` created; the standby reads that straight from Discord, so it is never fooled by a slow file
  sync, and serves only once the timestamp is 150 seconds stale. When the first host returns it waits for the
  standby's state to sync, then takes the bridge back. You never get two answers.
- **One writer per synced file.** File sync copies files; it cannot merge them. State that both devices write
  (Discord state, loop state) is kept as one copy per host and merged on read with last-writer-wins per entry, so
  two devices never fight over one file.
- **Conflict copies.** If your sync still produces `*.sync-conflict-*` files under `Queue/`, `fleet.py` merges the
  JSON state and line-based logs and moves every handled copy to `Queue/archive/sync-conflicts`. Nothing is deleted.
  `python3 switchboard/housekeeper.py --conflicts` does a pass by hand.
- **Taking a repo over by hand.** `python3 switchboard/repo-handoff.py claim <Repo>` on the device you sit at keeps
  agents on other devices out of it until you `release` it.

## Checking

```
python3 switchboard/fleet.py devices     # every device with a fresh heartbeat, and the repos each holds
python3 switchboard/fleet.py where REPO  # which devices could take work in REPO right now
python3 setup/doctor.py                  # this device's setup
```

If a device is missing from `fleet.py devices` on the other one, the sync is not flowing: fix that first.
