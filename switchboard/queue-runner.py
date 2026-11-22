#!/usr/bin/env python3
"""Run queued Claude Code tasks one at a time from <root>/Queue/todo.

Card: a Markdown file with frontmatter `repo`, optional `machine` (any|<hostname>), `budget_minutes`,
`permission_mode` (auto|acceptEdits|plan). The body is the prompt. The runner claims the repo lock, runs
`claude -p` in <root>/<repo>, writes <name>.report.md next to the card in done/ or failed/, releases the lock.
A file named STOP in Queue/ pauses the loop. Codex is never invoked.

  queue-runner.py [--once] [--poll 60] [--dry-run]
"""
import argparse, json, os, shutil, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import hostname, dev_root, queue_root, read_frontmatter, run
import fleet

def now(): return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def pick(q, me):
    for card in sorted(q.joinpath("todo").glob("*.md")):
        meta, _ = read_frontmatter(card.read_text(encoding="utf-8"))
        machine = meta.get("machine", "any")
        if machine not in ("any", me): continue
        return card
    return None

def claude_cmd():
    return "claude.cmd" if os.name == "nt" and shutil.which("claude.cmd") else "claude"

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true"); ap.add_argument("--poll", type=int, default=60)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    me, root, q = hostname(), dev_root(), queue_root()
    handoff = Path(__file__).with_name("repo-handoff.py")
    log = q / f"runner-{me}.log"
    def say(msg):
        line = f"{now()} {msg}"; print(line, flush=True); log.open("a", encoding="utf-8").write(line + "\n")
    say(f"runner start on {me}, root={root}")
    while True:
        if (q / "STOP").exists():
            say("STOP present, idle")
            if a.once: return 0
            time.sleep(a.poll); continue
        card = pick(q, me)
        if not card:
            if a.once: say("queue empty"); return 0
            time.sleep(a.poll); continue
        meta, body = read_frontmatter(card.read_text(encoding="utf-8"))
        repo = meta.get("repo", "").strip()
        rdir = root / repo
        budget = int(meta.get("budget_minutes", "120") or 120)
        mode = meta.get("permission_mode", "auto") or "auto"
        running = q / "running" / f"{me}--{card.name}"
        shutil.move(str(card), str(running))
        if not repo or not rdir.is_dir():
            shutil.move(str(running), str(q / "failed" / card.name))
            (q / "failed" / (card.stem + ".report.md")).write_text(f"# Failed\n\nUnknown repo folder: {rdir}\n", encoding="utf-8")
            say(f"failed {card.name}: unknown repo {repo!r}"); continue
        claim = run([sys.executable, str(handoff), "claim", repo, "--note", f"queue {card.name}"])
        if claim.returncode != 0:
            shutil.move(str(running), str(card)); say(f"deferred {card.name}: {claim.stdout.strip()}")
            if a.once: return 0
            time.sleep(a.poll); continue
        prompt = (f"You are running unattended on {me} from a queue card named {card.name}. Work only inside this "
                  f"repo folder. Never publish, never run Git commands, never call Codex or Cursor. If you cannot finish, "
                  f"leave a precise checkpoint of where the work stands. Finish with a short report: changes, "
                  f"checks run, not tested, next step.\n\n{body}")
        cmd = [claude_cmd(), "-p", prompt, "--permission-mode", mode, "--output-format", "json"]
        say(f"start {card.name} repo={repo} budget={budget}m mode={mode}")
        if a.dry_run:
            say("dry-run: " + " ".join(cmd[:4]) + " ..."); shutil.move(str(running), str(card))
            run([sys.executable, str(handoff), "release", repo, "--note", "dry-run", "--force"]); return 0
        started = time.time(); ok = False; out = ""
        fleet.event("started", card.stem, agent="queue", thread="card-" + card.stem, repo=repo, ref="card-" + card.stem)
        try:
            p = subprocess.run(cmd, cwd=str(rdir), capture_output=True, text=True, timeout=budget * 60,
                               env=dict(os.environ, SESSION_HOOK_SKIP="1"))  # session-hook.py: do not list this run
            out = p.stdout or p.stderr; ok = p.returncode == 0
        except subprocess.TimeoutExpired as e:
            out = f"timeout after {budget} minutes\n" + (e.stdout or "")
        result = ""
        try: result = json.loads(out).get("result", out)
        except Exception: result = out
        dest = q / ("done" if ok else "failed")
        shutil.move(str(running), str(dest / card.name))
        (dest / (card.stem + ".report.md")).write_text(
            f"# {'Done' if ok else 'Failed'}: {card.stem}\n\n- machine: {me}\n- repo: {repo}\n- started: "
            f"{datetime.fromtimestamp(started, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n- finished: {now()}\n\n"
            f"## Claude report\n\n{result}\n", encoding="utf-8")
        run([sys.executable, str(handoff), "release", repo, "--note", f"queue {card.name}", "--force"])
        say(f"{'done' if ok else 'failed'} {card.name} in {int(time.time()-started)}s")
        fleet.event("done" if ok else ("cutoff" if str(out).startswith("timeout") else "failed"), card.stem, detail=str(result), agent="queue", thread="card-" + card.stem, repo=repo, ref="card-" + card.stem)
        if a.once: return 0 if ok else 1

if __name__ == "__main__": sys.exit(main())
