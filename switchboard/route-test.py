#!/usr/bin/env python3
"""Checks that the model check still follows its routing rules. Run after any edit to route.py: python3 route-test.py"""
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("route", Path(__file__).resolve().parent / "route.py"); R = importlib.util.module_from_spec(spec); spec.loader.exec_module(R)
def pick(agent, text, **k): return R.pick(agent, text, owner_text=k.pop("owner", ""), **k)
def is_(d, model, effort): return d["model"] == model and d["effort"] == effort
def readme():
    """The README that holds the routing table pasted from route.py --table (beside this file or one folder up), else None."""
    for d in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent):
        try: text = (d / "README.md").read_text(encoding="utf-8")
        except OSError: continue
        if "| Job | First choice | Backups |" in text: return text
    return None
def readme_models():
    """Models named in the README's routing table rows."""
    import re
    rows = [l for l in (readme() or "").splitlines() if l.startswith("| ") and not l.startswith("| Job")]
    return {m for l in rows for m in re.findall(r"\b(claude-[\w.-]+|gpt-[\w.-]+)", l)}
CASES = [
    ("a quick device action is light", lambda: is_(pick("orchestrator", "airdrop this device a random photo"), R.SONNET, "medium")),
    ("ordinary build without Codex is cheap", lambda: is_(pick("builder", "Field Notebook: add a daily reward popup"), R.SONNET, "medium")),
    ("ordinary build with Codex is Sol medium", lambda: is_(pick("builder", "add a daily reward popup", codex_ok=True), R.SOL, "medium")),
    ("user data is not analysis", lambda: is_(pick("builder", "fix the bug where user data does not load in the shop"), R.SONNET, "medium")),
    ("stats panel is not analysis", lambda: is_(pick("builder", "Kepler: add stats panel to the HUD"), R.SONNET, "medium")),
    ("a why question is not analysis", lambda: is_(pick("builder", "why does the sprint button not work on mobile"), R.SONNET, "medium")),
    ("high risk builds one tier up", lambda: is_(pick("builder", "DataStore session lock loses coins"), R.OPUS, "medium")),
    ("analysis keeps a frontier model", lambda: is_(pick("builder", "analyze the retention data"), R.FABLE, "medium")),
    ("analysis goes to Astra when Codex is healthy", lambda: is_(pick("builder", "analyze the retention data", codex_ok=True), R.ASTRA, "high")),
    ("focus goes to Fable high", lambda: is_(pick("builder", "polish the HUD", owner="make it nicer, this is important, focus on quality"), R.FABLE, "high")),
    ("fix rounds keep the model", lambda: pick("builder", "popup fixes", prior_builds=1)["model"] == pick("builder", "popup fixes", prior_builds=2)["model"] == pick("builder", "popup fixes")["model"] != pick("builder", "popup fixes", prior_builds=3)["model"]),
    ("the fourth build escalates", lambda: pick("builder", "popup fixes", prior_builds=3)["model"] == R.FABLE),
    ("the plan can set the tier", lambda: is_(pick("builder", "tune the bike speed\nTIER: big"), R.OPUS, "medium")),
    ("planning is Opus high", lambda: is_(pick("orchestrator", "Field Notebook: add a daily reward popup with streaks and a calendar"), R.OPUS, "high")),
    ("loop cycles stay off Fable", lambda: is_(pick("orchestrator", "[loop] Improvement cycle: Field Notebook"), R.OPUS, "high")),
    ("mini review is Opus, a better model than the builder", lambda: is_(pick("orchestrator", "Re: popup built", relay=True, sender="builder"), R.OPUS, "medium")),
    ("keywords: concurrency-test is not currency", lambda: R.risk("concurrency-test.py passed 15/15") != "high"),
    ("keywords: real replication and currency stay high", lambda: R.risk("fix replication of the boat to clients") == "high" and R.risk("add a currency shop") == "high"),
    ("keywords: largest, orchestrator, framework path are not complex", lambda: not R.COMPLEX.search("the largest step was 1.07%") and not R.COMPLEX.search("the orchestrator's video") and not R.COMPLEX.search("in Framework/lib") and not R.COMPLEX.search(r"in Framework\lib")),
    ("keywords: large, orchestration, framework stay complex", lambda: R.COMPLEX.search("a large refactor") and R.COMPLEX.search("orchestration overhaul") and R.COMPLEX.search("port it to the new framework")),
    ("keywords: the switchboard folder and a project prefix are not complex", lambda: not R.COMPLEX.search("see switchboard/discord-bridge.py") and not R.COMPLEX.search(r"C:\Projects\switchboard\route.py") and not R.COMPLEX.search("Re: Orchestration: CHECKDIR accepts root paths")),
    ("keywords: multi device feature, multi-step, multi-system stay complex", lambda: R.COMPLEX.search("a multi device feature") and R.COMPLEX.search("a multi-step change") and R.COMPLEX.search("a multi-system rollout") and R.COMPLEX.search("orchestration overhaul")),
    ("a janitor mail naming the switchboard folder gets the medium mini review", lambda: is_(pick("orchestrator", "Discord flow: 8 finding(s) from Gemini for your review\nCheck the evidence yourself (switchboard/discord-bridge.py, its tests)", relay=True, sender="janitor"), R.OPUS, "medium")),
    ("keywords: a routine builder answer gets the medium mini review", lambda: is_(pick("orchestrator", "Re: popup built\nDone. concurrency-test.py passed 15/15; the largest gap is 2 px; edited src/ui/panel.ts", relay=True, sender="builder"), R.OPUS, "medium")),
    ("a reviewer's verdict gets a mini review, not a cheap turn", lambda: is_(pick("orchestrator", "Re: review passed", relay=True, sender="reviewer"), R.OPUS, "medium")),
    ("the runner's file list does not make a mini review hard", lambda: is_(pick("orchestrator", "Re: popup built\nDone, the popup opens.\n\n---\nMeasured by the runner, not reported by the builder: 1 file(s) changed.\n- switchboard/route.py (+2 -1)\nFull diff: C:\\Projects\\Queue\\attachments\\x\\changes.diff", relay=True, sender="builder"), R.OPUS, "medium")),
    ("the builder's own words still make a mini review hard", lambda: is_(pick("orchestrator", "Re: save pipeline rewrite done\n\n---\nMeasured by the runner: no source file changed in this run.", relay=True, sender="builder"), R.OPUS, "high")),
    ("other hand-offs are Sonnet", lambda: is_(pick("orchestrator", "Re: noted", relay=True, sender="mac"), R.SONNET, "medium")),
    ("the owner's research question is planned, not a cheap reply", lambda: pick("orchestrator", "what has changed since the last release?", owner="what has changed since the last release?")["model"] != R.SONNET),
    ("a plain status question stays light", lambda: is_(pick("orchestrator", "is it done?", owner="is it done?"), R.SONNET, "medium")),
    ("the owner naming a model wins", lambda: is_(pick("builder", "x", owner="plan the pet system, use fable low"), R.FABLE, "low")),
    ("Codex reviews Claude's work", lambda: pick("reviewer", "Review the quest system, end to end", codex_ok=True)["engine"] == "codex"),
    ("Claude reviews Codex's work, on Fable", lambda: (lambda d: d["engine"] == "claude" and d["model"] == R.FABLE)(pick("reviewer", "Review the popup", codex_ok=True, built_by="codex"))),
    ("full review differs from the mini review without Codex", lambda: pick("reviewer", "Review the quest system, end to end")["model"] != pick("orchestrator", "Re: built", relay=True, sender="builder")["model"]),
    ("hard full review differs from the hard mini review without Codex", lambda: pick("reviewer", "Review the data store session lock rewrite")["model"] != pick("orchestrator", "Re: DataStore session lock rewrite built", relay=True, sender="builder")["model"]),
    ("Fable is never above high in a review", lambda: all(c[2] in ("low", "medium", "high") for k, v in R.PLAN.items() if k.startswith("review") for c in v if c[1] == R.FABLE)),
    ("with Fable off the review still has a backup", lambda: all((lambda d: d["model"] != R.FABLE and d["alt"] and d["alt"]["model"] != d["model"])(pick("reviewer", t, cfg={"fable": "off"}, **k)) for t, k in [("Review the quest system, end to end", {}), ("Review the popup", {"built_by": "codex"})])),
    ("codex_builds false keeps Codex to reviews", lambda: pick("builder", "add a popup", codex_ok=True, cfg={"codex_builds": False})["engine"] == "claude" and pick("builder", "analyze the retention data", codex_ok=True, cfg={"codex_builds": False})["engine"] == "claude" and pick("builder", "popup fixes", prior_builds=3, built_by="claude", codex_ok=True, cfg={"codex_builds": False})["engine"] == "claude" and pick("reviewer", "Review the quest system, end to end", codex_ok=True, cfg={"codex_builds": False})["engine"] == "codex"),
    ("the stats key of a routine build is build_cheap", lambda: pick("builder", "add a daily reward popup")["key"] == "build_cheap" and pick("builder", "add a daily reward popup", domain="repo")["key"] == "build_cheap"),
    ("early returns carry a key too", lambda: pick("builder", "x", owner="use fable low")["key"] == "named" and pick("builder", "x\nMODEL: opus high, y")["key"] == "planner_model" and pick("builder", "popup fixes", prior_builds=3)["key"] == "escalate"),
    ("every model in the README table is in PLAN (when a README carries it)", lambda: readme() is None or (readme_models() and readme_models() <= {c[1] for v in R.PLAN.values() for c in v})),
    ("the README table matches route.py --table (when a README carries it)", lambda: readme() is None or R.table() in readme()),
    ("every choice has a backup", lambda: all(pick(ag, t, **k)["alt"] for ag, t, k in [("builder", "add a popup", {}), ("orchestrator", "plan the popup feature for the shop", {}), ("orchestrator", "Re: built", {"relay": True, "sender": "builder"}), ("reviewer", "Review the quest system, end to end", {}), ("builder", "x", {"owner": "use fable high"})])),
    ("the backup is a different model", lambda: all(d["alt"]["model"] != d["model"] for d in [pick("builder", "add a popup"), pick("builder", "add a popup", codex_ok=True), pick("orchestrator", "plan the popup feature for the shop")])),
    ("big work with Codex is Sol xhigh, backed up by Opus", lambda: (lambda d: is_(d, R.SOL, "xhigh") and d["alt"]["model"] == R.OPUS)(pick("builder", "DataStore session lock loses coins", codex_ok=True))),
    ("the planner can name the model", lambda: (lambda d: is_(d, R.SOL, "high") and d["alt"])(pick("builder", "add a popup\nMODEL: sol high, plain UI work", codex_ok=True))),
    ("a named model that is unavailable falls to the backup", lambda: pick("builder", "add a popup\nMODEL: astra high, needs reasoning")["engine"] == "claude"),
    ("Fable is never asked for above high", lambda: pick("builder", "x\nMODEL: fable extra high, hard")["effort"] == "high"),
    ("never Haiku, never max", lambda: all("haiku" not in c[1] and c[2] not in ("max", "ultra") for v in R.PLAN.values() for c in v if c)),
    ("small jobs get no review", lambda: R.needs_review("Review the daily reward popup")[0] is False),
    ("big jobs get a review", lambda: R.needs_review("Review the new quest system, end to end")[0] is True),
    ("high-risk areas get a review", lambda: R.needs_review("Review the DataStore session lock fix")[0] is True),
    ("the mini review can ask for a full review", lambda: R.needs_review("FULL REVIEW: touches three systems\nReview the popup")[0] is True),
    ("the owner's no review wins", lambda: R.needs_review("Review the new quest system, end to end", "build it, no review")[0] is False),
    ("ponytail: routine build is full", lambda: R.ponytail("builder", "add a daily reward popup")[:2][0] == "full"),
    ("ponytail: orchestrator and reviewer stay off", lambda: R.ponytail("orchestrator", "add a popup")[0] == R.ponytail("reviewer", "add a popup")[0] == "off"),
    ("ponytail: analysis is off", lambda: R.ponytail("builder", "analyze the retention data")[0] == "off"),
    ("ponytail: a small tweak is off", lambda: R.ponytail("builder", "fix the typo in the shop label")[0] == "off"),
    ("ponytail: complex work asks the side check", lambda: R.ponytail("builder", "rewrite the quest system end to end")[2] is True),
    ("ponytail: a build sent back asks the side check", lambda: R.ponytail("builder", "popup fixes", prior_builds=1)[2] is True),
    ("ponytail: the owner naming a level wins", lambda: R.ponytail("builder", "rewrite it all", "use ponytail ultra")[:3:2] == ("ultra", False)),
    ("ponytail: the owner can turn it off", lambda: R.ponytail("builder", "add a popup", "no ponytail on this one")[0] == "off"),
    ("ponytail: runner.json can force a level", lambda: R.ponytail("builder", "add a popup", cfg={"ponytail": "off"})[0] == "off"),
    ("ponytail: side answer is parsed", lambda: R.ponytail_answer("PONYTAIL: off - needs a new system", "lite") == ("off", "side check: needs a new system")),
    ("ponytail: side ultra or junk falls back", lambda: R.ponytail_answer("PONYTAIL: ultra", "lite")[0] == R.ponytail_answer("not sure", "lite")[0] == "lite"),
]
bad = [n for n, f in CASES if not f()]
for n in bad: print("FAIL", n)
print(f"{len(CASES) - len(bad)}/{len(CASES)} passed"); sys.exit(1 if bad else 0)
