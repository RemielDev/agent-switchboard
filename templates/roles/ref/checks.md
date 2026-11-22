# Writing a builder task: checks, tier, model

Read this before you send `builder` a task.

## The task itself
- Plan in full, then hand to a cheaper builder. The task leaves nothing to work out: the goal in one sentence, the exact files and functions, what to change in each, what must not change, and the checks that mean done. If you cannot write that yet, read the code until you can.
- A rebuild on a stronger model starts fresh: give it the plan, the path of the measured diff and what is still wrong. Never paste the earlier run's steps (arXiv 2608.24358: inherited history cost 2.2x more per step).
- Batch related fixes into one task. Pass the repo, or an `area` slug for work outside a repo folder: one writer per repo or area runs at a time.

## Keep it short
- A builder task stays under about 300 words unless the plan truly needs more. Point to files, mail ids, diff paths and notes instead of pasting them. The builder's session continues across fix rounds, so a fix is the change and its reason in a few lines, not the plan again.
- Add `REPORT: direct` when the task needs no review from you (a clear change whose checks prove it): if every check passes, the result goes straight to the owner and you are not run. Leave it off for big, risky or visual work you must judge.

## Match the reference, not just the budget
- When the owner asks for new work to match existing work ("same detail", "match the theme", "same quality"), the task must measure the existing reference first and add CHECK lines that compare the new work against it. Being under a budget is not a pass.

## CHECK lines (VeriMAP, arXiv 2510.17109)
- End the task with up to six `CHECK: <command>` lines, one assertion each: structure, the behavior asked for, an edge case. The runner runs them after the build and reports each result.
- Only a short list of commands is allowed (`CHECK_CMDS` in `mail-runner.py`: python, pytest, ruff, node, npm, npx, tsc, eslint, go, cargo and other test and lint runners). No pipes, redirects, `;`, `python -c` or paths outside the folder; have the builder write a small test file instead.
- A CHECK line is the command alone: no note or expectation after it (write that as a `CRITERION:` line).
- Checks run in the build's folder (the repo, else the root). For a subfolder add one `CHECKDIR: <path under that folder>`. Do not also prefix the check's paths with that folder.
- `CRITERION: <sentence>` for what only a reader can judge; you judge it at the mini review.
- A failed check is evidence, not a verdict: read the failure. A `SETUP` line means the runner could not run the check; never send work back for SETUP lines alone.

## Tier and model
- `TIER: cheap, <why>` (clear, small), `TIER: big, <why>` (many files, risky area, merging results) or `TIER: frontier, <why>` (real reasoning while building). The reason matters (arXiv 2609.32917). A follow-up fix is usually cheap.
- `MODEL: <model> <low|medium|high|extra high>, <why>` overrides the model check when you know better; its pick becomes the backup. `python route.py --table` prints the models this install routes to.
