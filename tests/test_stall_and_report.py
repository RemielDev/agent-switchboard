"""No time limits: runs stop only when they stall; a clean build may report straight to the owner; the runner's block
under a builder's answer stays short; prompts over budget are logged."""
import json, subprocess, threading, time, types
from pathlib import Path

import pytest

from _load import load
import fleet

mr = load("mail_runner_stall", "mail-runner.py")
ME = mr.hostname()


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "dev"; root.mkdir()
    monkeypatch.setenv("SWITCHBOARD_ROOT", str(root)); monkeypatch.delenv("AGENT_MAIL_PARENT", raising=False)
    mail = mr.load_mail()
    for name, kind in (("builder", "claude"), ("orchestrator", "claude"), ("owner", "human")):
        mail.register(name, ME, kind, auto=kind == "claude")
    monkeypatch.setattr(mr, "find_claude", lambda: "/x/claude"); monkeypatch.setattr(mr, "choose", lambda *a, **k: None)
    monkeypatch.setattr(fleet, "codex_exe", lambda: None); monkeypatch.setattr(mr, "memory_text", lambda cwd, L=None: "")
    logs = []; monkeypatch.setattr(mr, "say", lambda s: logs.append(s))
    a = types.SimpleNamespace(only="", once=False, dry_run=False, state_file=str(tmp_path / "local" / "state.json"), max_per_hour=0, max_per_day=0,
                              timeout_minutes=20, max_concurrent=None, max_hops=0, prompt_via="auto", permission_mode="auto", mcp=False, always_reply=False, no_loop=False)
    return types.SimpleNamespace(r=mr.Runner(a, mail), mail=mail, q=root / "Queue", root=root, logs=logs)

def events(q, kind):
    return [json.loads(l) for f in (q / "events").glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines() if f'"{kind}"' in l]


# ---- 6. stall, not a wall clock ----
class Busy:
    def wait(self, timeout=None): time.sleep(min(timeout or 0.05, 0.05)); raise subprocess.TimeoutExpired("x", timeout)

def test_a_run_that_keeps_writing_is_never_cut(tmp_path):
    out = tmp_path / "out.txt"; out.write_text("")
    def writer():
        for _ in range(12): out.write_text(out.read_text() + "x\n"); time.sleep(0.15)
    threading.Thread(target=writer, daemon=True).start()
    t0 = time.monotonic(); rc = mr.wait_run(Busy(), 0.6, watch=(out,))
    assert rc is None and time.monotonic() - t0 > 1.8, "it ran past three stall windows while output grew, then stopped once quiet"

def test_a_quiet_run_is_stopped_after_the_stall_window(tmp_path):
    out = tmp_path / "out.txt"; out.write_text("start")
    t0 = time.monotonic(); assert mr.wait_run(Busy(), 0.5, watch=(out,)) is None and time.monotonic() - t0 < 2.5
    ev = threading.Event(); ev.set()
    with pytest.raises(mr.Cancelled): mr.wait_run(Busy(), 5, cancel=ev, watch=(out,))

def test_a_stalled_run_is_a_cutoff_and_keeps_its_session(env, monkeypatch):
    r, mail, q = env.r, env.mail, env.q
    def run(exe, prompt, mode, cwd, *a, parent="", **k):
        mr.SESSIONS[parent] = ("stalled-sid", str(cwd)); return None, "", ""
    monkeypatch.setattr(mr, "run_claude", run)
    m = mail.send("owner", "orchestrator", "Map", "Plan the map."); agents = mail.load_agents()
    r.execute(r.prepare("orchestrator", agents["orchestrator"], m, agents, "/x/claude"))
    cut = events(q, "cutoff")
    assert cut and "stalled: no output for 20 min" in cut[0]["detail"] and cut[0]["session"] == "stalled-sid" and not events(q, "failed")
    assert mr.thread_record(m["id"])["session"] == "stalled-sid", "the next round resumes it"


# ---- 7. REPORT: direct ----
def build(env, monkeypatch, checks, said="Built the bridge."):
    mail = env.mail
    m1 = mail.send("owner", "orchestrator", "Kepler: bridge", "Make a bridge.")
    task = mail.send("orchestrator", "builder", "Build the bridge", "Build it.\nREPORT: direct\n" + checks, reply_to=m1["id"], needs_reply=True)
    monkeypatch.setattr(mr, "run_claude", lambda *a, parent="", **k: (0, json.dumps({"type": "result", "result": said}), ""))
    agents = mail.load_agents()
    env.r.execute(env.r.prepare("builder", agents["builder"], task, agents, "/x/claude"))
    return m1, task

def inbox(mail, agent): return [m for m in mail.list_inbox(agent) if m.get("from") == "builder"]

def test_a_clean_build_reports_straight_to_the_starter(env, monkeypatch):
    m1, task = build(env, monkeypatch, 'CHECK: python -c "print(1)"')
    to_owner = [m for m in inbox(env.mail, "owner") if m.get("reply_to") == task["id"]]
    assert to_owner and to_owner[0]["from"] == "builder" and "Built the bridge." in to_owner[0]["body"] and not inbox(env.mail, "orchestrator")
    brief = (env.q / "threads" / f"{m1['id']}.md").read_text(encoding="utf-8")
    assert brief.splitlines()[-1].startswith("Latest result (") and "Built the bridge." in brief

@pytest.mark.parametrize("checks,said", [('CHECK: python -c "raise SystemExit(1)"', "Built."), ("", "Built.")])
def test_anything_short_of_clean_goes_to_the_orchestrator(env, monkeypatch, checks, said):
    build(env, monkeypatch, checks, said)
    assert inbox(env.mail, "orchestrator") and not inbox(env.mail, "owner")

def test_a_failed_check_blocks_a_direct_report():
    msg = {"body": "x\nREPORT: direct"}
    ok = "\nChecks from the plan, run by the runner: 1 of 1 passed.\n- PASS `python ok.py` (0 s)"
    assert mr.direct_report(msg, "Done.", ok)
    assert not mr.direct_report(msg, "Done.", "\nChecks from the plan, run by the runner: 1 of 2 passed.\n- PASS `python ok.py` (0 s)\n- FAIL `python bad.py` (exit 1, 0 s): x")
    assert not mr.direct_report({"body": "no line"}, "Done.", ok), "the orchestrator decides per task"


# ---- 8. a short runner block ----
def test_changed_files_are_capped(env):
    cwd = env.root / "Repo"; cwd.mkdir()
    before = mr.snapshot(cwd)
    for i in range(15): (cwd / f"f{i}.py").write_text("x")
    info = {}; t = mr.measured_changes(cwd, before, "m1", info)
    assert sum(1 for l in t.splitlines() if l.startswith("- f")) == 10 and "... and 5 more (all in the diff)" in t and len(info["files"]) == 15
    assert len(t) < 1200, len(t)


# ---- 9. prompt budgets are logged, never cut ----
def test_prompts_over_budget_are_logged(env):
    r, mail = env.r, env.mail; agents = mail.load_agents()
    m = mail.send("owner", "orchestrator", "Big", "y" * 9000)
    job = r.prepare("orchestrator", agents["orchestrator"], m, agents, "/x/claude")
    assert any("prompt budget: orchestrator prompt is" in l and "over its 8000" in l for l in env.logs) and "y" * 9000 in job["prompt"]
    r.drop(job["held"], job["shared"])
    t = mail.send("orchestrator", "builder", "Big task", "z" * 6500)
    job = r.prepare("builder", agents["builder"], t, agents, "/x/claude")
    assert any("prompt budget: builder task prompt is 6500 chars" in l for l in env.logs)
    r.drop(job["held"], job["shared"])
