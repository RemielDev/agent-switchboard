#!/usr/bin/env python3
"""Which model and effort one agent run gets. Rules only, no model call.

The default menu: Opus 5.5, Fable 5.2 (never above high), Sonnet 5.5, Codex Sol and Astra; never Haiku, never max.
Fable costs about twice Opus per token and has its own weekly limit.

  route.py "subject and body text" [--agent builder] [--relay] [--prior N]     show the choice for a piece of work
  route.py --table                                                             print PLAN as a Markdown table

PLAN below is this install's default policy, and the whole of it: for each kind of job, the models that fit it, best
first. Edit it (or override rows in runner.json, below) to suit your own models and budget. The check takes the best
one available and always gives a backup. In words:
1. The owner named a model in the thread ("use fable high", "with astra high"): that wins, with the rules' pick as backup.
2. The planner named one for a build (`MODEL: sol high, <why>`): that is the choice, the rules' pick is the backup.
3. Orchestrator steps (Claude only, they need the mail tools): Sonnet medium for hand-offs, Opus high to write a
   plan, Fable medium to plan complex, analysis or focus work, Opus medium or high for the mini review.
4. Builds, ranked by fit. Routine: Sol medium, else Sonnet medium. Big or high risk: Sol xhigh, else Opus medium.
   Analysis: Astra high or Fable medium. Focus: Astra xhigh or Fable high. A build the mini review sends back keeps
   its model, so the builder's session continues; after two failed fix rounds on it, the fourth build goes to the top
   model of the other family.
5. Reviews happen only for big jobs (needs_review) and go to the other family from whoever built; the Claude stand-in
   is Fable, so a full review never repeats the mini review's Opus.

Queue/mail/runner.json "routing" can override: {"fable": "free"|"off", "fable_per_day": 0, "codex": true,
"codex_builds": true (Codex may also build; mail-runner treats a missing key as false, reviews only), "plan": {"build_big": [["claude", "claude-opus-5-5", "high"]]}}.
"""
import json, re, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

OPUS, FABLE, SONNET = "claude-opus-5-5", "claude-fable-5-2", "claude-sonnet-5-5"
# Output tokens per task by effort (published tokens-per-task measurements, slopalytics.com), times list output price:
#   Opus 5.5   L 19k  M 44k  H 60k  X 103k  Mx 173k   at $20 per M  ->  M $0.9, H $1.2, X $2.1
#   Fable 5.2  L 36k  M 43k  H 98k  X 162k  Mx 274k   at $40 per M  ->  L $1.4, M $1.7, H $3.9
# So: Fable medium costs less than Opus xhigh, Fable low saves little over medium, Fable high is 2.3 times medium,
# and nothing here uses Fable xhigh or any max. Opus xhigh is only the stand-in when Fable is unavailable.
ANALYSIS = re.compile(r"\banaly[sz]|analytics|\bmetrics\b|funnel|retention (rate|data|curve|drop)|conversion rate|cohort|telemetry|investigat|\baudit\b|root cause|benchmark|find out why|look into why", re.I)  # not "data" or "stats" alone: "user data" and "stats panel" are ordinary builds
COMPLEX = re.compile(r"architect|new (sub)?system|whole system|entire|overhaul|redesign|rewrite|from scratch|end[ -]to[ -]end|\bframework\b(?![\\/])|pipeline|(?<![\\/\w])multi[- ]?(?:step|part|system|device)|complex|major|big (task|feature|change|update)|\blarge\b|full (rework|pass)|\borchestrat(?!or|ion:)\w*", re.I)
FOCUS = re.compile(r"\bfocus|careful|important|critical|best quality|high quality|top quality|think hard|take your time|priority|must be (right|perfect)|no mistakes|do (it|this) properly", re.I)
HIGH = re.compile(r"data ?store|save data|session lock|purchase|payment|billing|marketplace|receipt|\bcurrenc(?:y|ies)\b|economy|exploit|security|migrat|\breplicat\w*|race condition|memory leak|architecture|refactor|rewrite", re.I)
DEVICE_ACTION = re.compile(r"\b(air ?drop\w*|imessage\w*|text me|send (?:me|it|this)|screenshot|open (?:the |my )?app|photo)\b", re.I)
LOW = re.compile(r"\btypo|wording|\btext\b|colou?r|rename|\blabel|tooltip|spacing|padding|\bconstant|\bcomment|readme|\bdocs?\b|\bicon\b", re.I)
NAMED = re.compile(r"\b(?:use|with|on|run (?:it )?on)\s+(fable|opus|sonnet)(?:\s+5(?:\.\d)?)?(?:\s+(?:at\s+)?(low|medium|high|extra[ -]?high|xhigh))?\b", re.I)
EFFORTS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh"}

def risk(text):
    text = str(text or "")
    if HIGH.search(text): return "high"
    if LOW.search(text) and len(text) < 700: return "low"
    return "medium"

def named(text):
    """(model, effort or '') when the owner asked for a model by name, else None."""
    hit = None
    for hit in NAMED.finditer(str(text or "")): pass  # the last mention wins
    if not hit: return None
    model = {"fable": FABLE, "opus": OPUS, "sonnet": SONNET}[hit.group(1).lower()]
    eff = re.sub(r"extra[ -]?high", "xhigh", (hit.group(2) or "").lower())
    return model, EFFORTS.get(eff, "")

SOL, ASTRA = "gpt-6.1-sol", "gpt-6-astra"   # Codex: Sol is its workhorse, Astra its frontier model
C, X = "claude", "codex"
# For each kind of job, the models that fit it, best first. The check takes the best one that is available and always
# names a backup: the next one down. It is a ranking by fit for the job, not "one vendor, else the other".
# Codex leads builds when it is available and allowed to build (codex_builds); Claude takes them otherwise.
# The orchestrator's own steps are Claude only, because they need the mail tools.
PLAN = {
    "chat":             [(C, SONNET, "medium"), (C, OPUS, "medium")],
    "plan_light":       [(C, SONNET, "medium"), (C, OPUS, "medium")],  # a small question in chat: Sonnet is enough
    "plan":             [(C, OPUS, "high"), (C, FABLE, "medium")],
    "fable_plan":       [(C, FABLE, "medium"), (C, OPUS, "xhigh")],
    "mini_review":      [(C, OPUS, "medium"), (C, FABLE, "low")],
    "mini_review_hard": [(C, OPUS, "high"), (C, FABLE, "medium")],
    "build_cheap":      [(X, SOL, "medium"), (C, SONNET, "medium"), (C, OPUS, "medium")],
    "build_big":        [(X, SOL, "xhigh"), (C, OPUS, "medium"), (X, ASTRA, "high")],
    "frontier":         [(X, ASTRA, "high"), (C, FABLE, "medium"), (C, OPUS, "xhigh")],
    "focus":            [(X, ASTRA, "xhigh"), (C, FABLE, "high"), (C, OPUS, "xhigh")],
    # A review goes to the other family from the builder when it can; the stand-in is Fable, so the full review never
    # repeats the mini review's model (Song, arXiv 2610.01471). Opus is the last backup when Fable is off or blocked.
    "review_of_claude":      [(X, SOL, "xhigh"), (C, FABLE, "medium"), (C, OPUS, "high")],
    "review_of_claude_hard": [(X, ASTRA, "high"), (C, FABLE, "high"), (C, OPUS, "high")],
    "review_of_codex":       [(C, FABLE, "medium"), (C, OPUS, "high")],
    "review_of_codex_hard":  [(C, FABLE, "high"), (C, OPUS, "high")],
}
LAST_RESORT = [(C, OPUS, "high"), (C, SONNET, "medium")]  # so there is always a backup
# Output tokens per task (slopalytics.com, tokens per task), by effort L M H X Mx:
#   Opus 5.5 19k 44k 60k 103k 173k   Fable 5.2 36k 43k 98k 162k 274k   Sol 6k 14k 23k 30k 68k   Astra 9k 18k 23k 27k 52k
NAMES = {"fable": (C, FABLE), "opus": (C, OPUS), "sonnet": (C, SONNET), "sol": (X, SOL), "astra": (X, ASTRA)}
MODEL = re.compile(r"(?mi)^MODEL:\s*(fable|opus|sonnet|sol|astra)\b[ ,]*(low|medium|high|extra[ -]?high|xhigh)?")  # the planner names the builder's model, with its reason after
TOP = {FABLE: "high"}  # the most effort a model may be given; others stop at xhigh

REVIEW_YES = re.compile(r"\b(review|double[- ]check|second opinion|verify) (this|it|that|the)\b|\bwith (a )?review\b|\bget it reviewed\b", re.I)
REVIEW_NO = re.compile(r"\b(no|skip|without|don'?t) (the |a )?review", re.I)

def needs_review(text, owner_text="", build_secs=0, build_out_tokens=0, prior_builds=0, cfg=None):
    """(bool, why). Only big jobs get a full review; simple work goes straight back to the owner. Big means: the owner
    asked for a review, the task is complex or flagged for focus, it touches a high-risk area (saves, purchases, security),
    the build itself was large (25 minutes or 80k output tokens), or it is already on its third build."""
    cfg = cfg or {}; both = f"{text}\n{owner_text}"
    if REVIEW_NO.search(str(owner_text)): return False, "the owner said no review"
    if FULL.search(str(text)): return True, "the mini review asked for a full review"
    if REVIEW_YES.search(str(owner_text)): return True, "the owner asked for a review"
    if cfg.get("review") == "always": return True, "reviews are set to always"
    if cfg.get("review") == "never": return False, "reviews are switched off"
    if COMPLEX.search(both) or len(str(owner_text)) > 1500: return True, "a big or complex job"
    if FOCUS.search(str(owner_text)): return True, "the owner asked for focus"
    if risk(both) == "high": return True, "a high-risk area"
    if build_secs >= float(cfg.get("review_min_minutes", 25)) * 60: return True, f"the build ran {build_secs / 60:.0f} minutes"
    if build_out_tokens >= int(cfg.get("review_min_tokens", 80000)): return True, f"the build wrote {build_out_tokens // 1000}k tokens"
    if prior_builds >= 3: return True, "the thread is on its third build or later"
    return False, "a small job: the builder's own check is enough"

TIER = re.compile(r"(?mi)^TIER:\s*(cheap|big|frontier)\b")  # the planner may set the build tier itself; it knows more than these word lists
FULL = re.compile(r"(?mi)^FULL REVIEW:")  # the mini review asks for a full review with this line in its mail to the reviewer
RUNNER_BLOCK = re.compile(r"\n-{3,}\nMeasured by the runner\b.*", re.S)  # the runner's own file list and diff path: paths there are not the builder's words

RESEARCH = re.compile(r"\b(what|why|how|which|list|chang\w*|since|history|explain|summar\w*|compare|everything|differen\w*)\b", re.I)

def pick(agent, text, relay=False, prior_builds=0, owner_text="", cfg=None, fable_today=0, codex_ok=False, built_by="", sender="", domain=None):
    """{'engine', 'model', 'effort', 'why', 'alt', 'key'} for one run: the best available model for the job and a backup that
    is never missing; KEY names the PLAN row (or named, planner_model, escalate) for the stats. TEXT is the mail's subject and body, OWNER_TEXT what the owner last wrote in the thread, PRIOR_BUILDS
    the builder runs this request already had, CODEX_OK whether Codex may be used, BUILT_BY the engine of the last
    build, SENDER who wrote the mail. DOMAIN is accepted for older callers and ignored: every build is ranked the same way."""
    cfg = cfg or {}
    plan = {**PLAN, **{k: [tuple(x) for x in v] for k, v in (cfg.get("plan") or {}).items()}}
    fable = cfg.get("fable", "free"); cap = int(cfg.get("fable_per_day", 0)); use_codex = codex_ok and cfg.get("codex", True)
    def usable(c):
        if c[0] == X: return use_codex and (agent == "reviewer" or cfg.get("codex_builds", True) is not False)
        if c[1] == FABLE: return fable != "off" and not (cap and fable_today >= cap)
        return True
    mk = lambda c: {"engine": c[0], "model": c[1], "effort": c[2]}
    def ranked(cands, why, key):
        ok = [c for c in cands if usable(c)]
        for c in LAST_RESORT:
            if len({(x[0], x[1]) for x in ok}) >= 2: break
            if (c[0], c[1]) not in {(x[0], x[1]) for x in ok}: ok.append(c)
        best = ok[0]; backup = next(c for c in ok[1:] if (c[0], c[1]) != (best[0], best[1]))
        if cands and best != cands[0]: why += f"; the best fit, {cands[0][1]}, is not available now"
        return {**mk(best), "why": why, "alt": mk(backup), "key": key}
    def out(key, why): return ranked(plan[key], why, key)
    asked = named(owner_text)
    if asked and not (asked[0] == FABLE and fable == "off"):
        eng = X if asked[0] in (SOL, ASTRA) else C
        base = "medium" if asked[0] != OPUS else "high"
        return ranked([(eng, asked[0], asked[1] or base)], "the owner named the model in this thread", "named")
    r = risk(text); both = f"{text}\n{owner_text}"; loop = str(text).lstrip().startswith("[loop]")
    kind = ("analysis" if loop or ANALYSIS.search(both) else "") or ("focus" if FOCUS.search(str(owner_text)) else "") or ("complex" if COMPLEX.search(both) or len(str(owner_text)) > 1500 else "")
    label = {"analysis": "analysis work", "focus": "the owner asked for focus", "complex": "a big or complex task"}.get(kind, "")
    if agent == "orchestrator":
        if relay and not loop:
            if str(sender).lower() in ("builder", "reviewer", "janitor"):  # their verdicts decide fixes and ships: never a cheap chat turn
                own = RUNNER_BLOCK.sub("", str(text)); mine = f"{own}\n{owner_text}"
                hard = bool(ANALYSIS.search(mine) or FOCUS.search(str(owner_text)) or COMPLEX.search(mine) or len(str(owner_text)) > 1500) or risk(own) == "high" or "CHANGES NEEDED" in own
                return out("mini_review_hard" if hard else "mini_review", f"mini review of the {str(sender).lower()}'s answer: a better model looks it over and decides the next step")
            return out("chat", "messaging turn: passes another agent's mail along")
        if loop: return out("plan", "planning a loop cycle: self-started work stays off Fable, which is kept for the owner's own requests")
        if kind: return out("fable_plan", f"writing the full plan for {label}")
        research = bool(RESEARCH.search(str(text)))  # what changed, why, lists: needs records read this run, not a cheap reply
        light = not research and (r == "low" or (len(str(text)) < 300 and (str(text).rstrip().endswith("?") or bool(DEVICE_ACTION.search(str(text))))))
        return out("plan_light" if light else "plan", "answering a small request" if light else "writing the full plan a cheap builder can follow")
    if agent == "reviewer":
        hard = bool(kind) or r == "high"
        if built_by == X: return out("review_of_codex_hard" if hard else "review_of_codex", f"review of Codex's {'hard' if hard else 'routine'} build, by the other family")
        return out("review_of_claude_hard" if hard else "review_of_claude", f"review of {label or r + '-risk work'}, by the other family from the builder")
    chosen = MODEL.search(str(text))
    if chosen:  # the planner knows the job: its named model is the choice, the rules' pick is the backup
        eng, model = NAMES[chosen.group(1).lower()]
        eff = re.sub(r"extra[ -]?high", "xhigh", (chosen.group(2) or "medium").lower())
        if TOP.get(model) == "high" and eff == "xhigh": eff = "high"
        rule = pick(agent, MODEL.sub("", str(text)), relay, prior_builds, owner_text, cfg, fable_today, codex_ok, built_by, sender)
        return ranked([(eng, model, eff), (rule["engine"], rule["model"], rule["effort"])], "the plan named this model for the build", "planner_model")
    if prior_builds >= 3:
        top = [(C, FABLE, "high"), (X, ASTRA, "xhigh"), (C, OPUS, "xhigh")]
        if built_by == C: top = [top[1], top[0], top[2]]  # fresh eyes: the other family first
        return ranked(top, f"this request already had {prior_builds} builds", "escalate")
    set_tier = TIER.search(str(text))
    if set_tier:
        return out({"cheap": "build_cheap", "big": "build_big", "frontier": "frontier"}[set_tier.group(1).lower()], f"the plan set the tier to {set_tier.group(1).lower()}")
    if kind in ("analysis", "focus"):  # here the work itself is the thinking, so it gets a frontier model
        return out("frontier" if kind == "analysis" else "focus", label)
    steps = ["build_cheap", "build_big", "frontier"]  # fix rounds keep the model, so the builder's session continues; prior_builds >= 3 escalates above
    i = 1 if kind == "complex" or r == "high" else 0
    what = "a big or high-risk build" if (kind == "complex" or r == "high") else "a routine build"
    return out(steps[i], f"{what} from a finished plan" + (f", fix round {prior_builds} on the same model" if prior_builds else ""))

# Ponytail is an optional Claude Code plugin that keeps changes minimal. It is expected off by default
# (~/.config/ponytail/config.json defaultMode off); the runner turns it on per run from this check. lite: build what was asked and name the smaller option; full: smallest
# complete change; ultra: also push back on the request, which suits nothing unattended, so only the owner can ask for it.
PONY_LEVELS = ("off", "lite", "full", "ultra")
PONY_ASK = re.compile(r"\b(?:(no|skip|without|stop) ponytail|ponytail(?:\s+(off|lite|full|ultra))?)\b", re.I)
PONY_ANSWER = re.compile(r"(?mi)^\W*PONYTAIL:\W*(off|lite|full|ultra)\b\W*(.*)")

def ponytail(agent, text, owner_text="", prior_builds=0, cfg=None):
    """(level, why, ask): the Ponytail level for one run. ASK is True when the job is complex enough that the runner
    should ask a side agent (ponytail_prompt / ponytail_answer) and use its level, with LEVEL as the fallback."""
    cfg = cfg or {}; both = f"{text}\n{owner_text}"
    hit = None
    for hit in PONY_ASK.finditer(str(owner_text or "")): pass  # the owner's last mention wins
    if hit: return ("off" if hit.group(1) else (hit.group(2) or "full").lower()), "the owner asked for it in this thread", False
    if cfg.get("ponytail") in PONY_LEVELS: return cfg["ponytail"], "set in runner.json", False
    if agent != "builder": return "off", "only builds write code", False
    if str(text).lstrip().startswith("[loop]") or ANALYSIS.search(both): return "off", "analysis work, no code to keep small", False
    if risk(text) == "low": return "off", "a small tweak, nothing to over-build", False
    if COMPLEX.search(both) or FOCUS.search(str(owner_text)) or risk(both) == "high" or prior_builds or len(str(owner_text)) > 1500:
        return "lite", "a complex job: the side check decides", True
    return "full", "a routine build: smallest complete change", False

def ponytail_prompt(text, owner_text=""):
    return ("Decide whether a coding agent should run with Ponytail for the task below. Ponytail makes it take the smallest "
            "complete change: reuse existing code, no unrequested abstractions or options.\n"
            "- off: the task needs broad new structure, careful design or wide coverage, where minimalism would under-build it.\n"
            "- lite: build what was asked; only name a smaller option. For big jobs where scope is set by a plan.\n"
            "- full: smallest complete change. For fixes and features whose scope could bloat.\n"
            "Never answer ultra. Do not use tools. Reply with exactly one line: PONYTAIL: <off|lite|full> - <reason in under 15 words>\n\n"
            f"Task:\n{str(text)[:6000]}\n\nWhat the owner last wrote:\n{str(owner_text)[:2000]}")

def ponytail_answer(reply, fallback):
    """(level, why) from the side agent's reply; FALLBACK when it gave no usable line. Ultra is never taken from it."""
    m = PONY_ANSWER.search(str(reply or ""))
    if not m or m.group(1).lower() == "ultra": return fallback, "side check gave no usable answer"
    return m.group(1).lower(), "side check: " + (m.group(2).strip()[:120] or "no reason given")

def table(plan=None):
    """PLAN as a Markdown table, one row per kind of job: first choice, then backups. Docs paste this, never restate it."""
    plan = plan or PLAN
    cell = lambda c: f"{c[1]} {c[2]}"
    return "\n".join(["| Job | First choice | Backups |", "|---|---|---|"] + [f"| {k} | {cell(v[0])} | {', '.join(cell(c) for c in v[1:]) or '-'} |" for k, v in plan.items()])

def main():
    import argparse
    if sys.argv[1:] == ["--table"]: print(table()); return
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text"); ap.add_argument("--agent", default="builder"); ap.add_argument("--relay", action="store_true"); ap.add_argument("--prior", type=int, default=0)
    ap.add_argument("--codex", action="store_true", help="assume Codex has quota"); ap.add_argument("--built-by", default="")
    ap.add_argument("--from", dest="sender", default="")
    a = ap.parse_args(); d = pick(a.agent, a.text, a.relay, a.prior, owner_text=a.text, codex_ok=a.codex, built_by=a.built_by, sender=a.sender)
    d["review"] = needs_review(a.text, a.text, prior_builds=a.prior); d["ponytail"] = ponytail(a.agent, a.text, a.text, a.prior); print(json.dumps(d))

if __name__ == "__main__": main()
