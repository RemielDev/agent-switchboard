#!/usr/bin/env python3
"""Weekly retro: one compact summary of what the agents did, handed to the planner once a week.

The numbers come from the run stats: cost and rebuilds per request by the route of its first build
(fleet.requests_view), runs per agent and model, and plan checks. Reaction buttons no longer feed a ledger, so nothing
here reads one. The script does the counting; a model reads only the summary, never transcripts.
It proposes at most three changes and asks the owner before any is made.

  retro.py            print the summary
  retro.py --send     also mail it to the orchestrator as this week's retro
"""
import importlib.util, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet

def summary(days=7.0):
    runs = fleet.stats(days * 24)
    agents = {}
    for r in runs:
        a = agents.setdefault(f"{r.get('agent')} on {r.get('model') or r.get('engine') or '?'}", {"runs": 0, "hours": 0.0, "out_tokens": 0})
        a["runs"] += 1; a["hours"] = round(a["hours"] + float(r.get("secs") or 0) / 3600, 2); a["out_tokens"] += int(r.get("output_tokens") or 0)
    checks = sum(int(r.get("checks") or 0) for r in runs); passed = sum(int(r.get("checks_passed") or 0) for r in runs); setup = sum(int(r.get("checks_setup") or 0) for r in runs)
    return {"days": days, "requests_by_first_build_route": fleet.requests_view(days * 24), "runs_by_agent_and_model": agents,
            "plan_checks": {"run": checks, "passed": passed, "could_not_run": setup},
            "runs": len(runs), "run_hours": round(sum(float(r.get("secs") or 0) for r in runs) / 3600, 1)}

BODY = """Weekly retro for the orchestration. This was started by the schedule, not typed by the owner.

The numbers for the last {days:g} days, counted by a script:
{data}

1. Read the numbers. Name the weakest one: the route with the most rebuilds or the highest cost per request, the step that takes the most hours, or checks that fail often.
2. Search alphaxiv.org or arxiv.org once for a published technique that addresses it. Prefer proven and simple; name the paper.
3. Propose at most three changes, each one line with the number it should move. A change to which model does what is a `routing.plan` entry in Queue/mail/runner.json; a change to instructions is a role file edit; anything in code is a builder task with `CHECK:` lines.
4. Ask the owner once with ask_owner (Apply these, Skip). Change nothing before they answer. A route with fewer than ten requests this week is too small a sample: propose nothing about it.
5. Write the proposals and their answer to Queue/ledger/retro-log.md."""

def send(days=7.0):
    spec = importlib.util.spec_from_file_location("agent_mail", Path(__file__).resolve().parent / "agent-mail.py")
    mail = importlib.util.module_from_spec(spec); sys.modules["agent_mail"] = mail; spec.loader.exec_module(mail)
    return mail.send("owner", "orchestrator", "Orchestration: weekly retro", BODY.format(days=days, data=json.dumps(summary(days), indent=1)), needs_reply=True)

if __name__ == "__main__":
    print(json.dumps(summary(), indent=1))
    if "--send" in sys.argv: print("sent", send()["id"])
