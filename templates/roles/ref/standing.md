# The owner's standing decisions about the system

This file is yours to edit: it is where decisions that should outlive one conversation go. Agents do not reverse these or report them as defects. Proposed changes go to the owner as a question first. The lines below are sensible starting points.

- Devices: leave work unpinned unless it needs something only one device has. The runner sends work to the device that did the request's last build, else the first listed host that is up. For a need only one platform meets (Xcode, the iOS Simulator, PowerShell and so on) or a device the owner names: pin the task (send_message device). Never answer that something cannot be done when an up device can; never ask the owner which device.
- The only queue is one writer per repo or named area. What the owner types never waits behind agent-to-agent mail.
- `route.py` picks model and effort per run (`python route.py --table`).
- Discord stays tidy: one answer per turn of the owner's, edited in place; a question with buttons is always the last message in its thread.
- Nothing joins the improvement loop without the owner's Add.
- Improvement loop memory: `Queue/loop/<name>/backlog.md` and `log.md`. When the owner states a priority, complaint, idea or decision about a looped project, or accepts or rejects a result, add one dated line and re-rank. Start, stop or refocus looping with `improve-loop.py`. Do it quietly.
- Playbook: when a job ends with a lesson that changes how the next one is planned or built, `python playbook.py add "<project>" "<lesson>"` (one per job).
