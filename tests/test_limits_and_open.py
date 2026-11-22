"""Claude's usage limit (detect, share, park, fall back to Codex, resume), requeue, session ids, open requests, the 3 s
inbox cadence. Every subprocess is faked; nothing here starts Claude or Codex."""
import json, os, time, types, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from _load import load, TK
import fleet

mr = load("mail_runner_limits", "mail-runner.py")
ME = mr.hostname()
NOW = datetime(2027, 1, 13, 20, 0, tzinfo=timezone.utc).timestamp()  # Wed Jan 13 2027, 12:00 PST
JAN16_9AM_PT = datetime(2027, 1, 16, 17, 0, tzinfo=timezone.utc).timestamp()


# ---- 1. detection and reset parsing ----
@pytest.mark.parametrize("text", [
    "You've hit your weekly limit · resets Jan 16, 9am (America/Los_Angeles)",
    "You've hit your session limit · resets 3pm (America/Los_Angeles)",
    "5-hour limit reached ∙ resets 3pm",
    "Claude AI usage limit reached|1794848400",
    "usage limit reached",
    "Opus limit · resets 9am",
])
def test_every_limit_message_is_seen(text):
    assert mr.LIMIT_RE.search(text), text

def test_ordinary_failures_are_not_limits():
    for text in ("Error: tool failed", "All 12 checks passed", "claude exit code 1: permission denied"):
        assert not mr.LIMIT_RE.search(text), text

def test_reset_times():
    weekly = "You've hit your weekly limit · resets Jan 16, 9am (America/Los_Angeles)"
    assert mr.limit_reset(weekly, now=NOW) == JAN16_9AM_PT
    three_pm = datetime(2027, 1, 13, 23, 0, tzinfo=timezone.utc).timestamp()
    assert mr.limit_reset("You've hit your session limit · resets 3pm (America/Los_Angeles)", now=NOW) == three_pm, "later today"
    assert mr.limit_reset("You've hit your session limit · resets 3pm (America/Los_Angeles)", now=three_pm + 60) == three_pm + 86400, "the next 3pm"
    local = datetime.fromtimestamp(NOW).astimezone().replace(hour=15, minute=0, second=0, microsecond=0)
    local = local if local.timestamp() > NOW else local + timedelta(days=1)
    assert mr.limit_reset("5-hour limit reached ∙ resets 3pm", now=NOW) == local.timestamp(), "no zone named: this machine's local time"
    assert mr.limit_reset("Claude AI usage limit reached|1800043200", now=NOW) == 1800043200
    assert mr.limit_reset("usage limit reached", now=NOW) == NOW + 3600, "unparseable: an hour"
    assert mr.limit_reset("Claude AI usage limit reached|1000", now=NOW) == NOW + 3600, "a reset in the past is not believed"

def test_reset_from_the_rate_limit_event():
    line = json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": int(NOW + 5000), "rateLimitType": "five_hour"}})
    ok = json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": int(NOW + 99999)}})
    assert mr.limit_reset("usage limit reached", f"{ok}\n{line}\n", now=NOW) == NOW + 5000

def test_an_unknown_zone_is_the_local_zone():
    t = datetime.now()
    assert t.replace(tzinfo=mr.zone("No/Such_Zone")).utcoffset() == t.astimezone().utcoffset() == t.replace(tzinfo=mr.zone()).utcoffset()

def test_paused_text(monkeypatch):
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(mr, "zone", lambda name=None: ZoneInfo("America/Los_Angeles"))  # as on a machine set to Pacific time
    assert mr.paused_text("You've hit your weekly limit · resets Jan 16, 9am", JAN16_9AM_PT) == \
        "Claude's weekly limit is reached. Resumes on its own Sat Jan 16, 9:00 AM PST, or sooner on Codex when it has quota."


# ---- a runner on a throwaway dev root ----
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_ROOT", str(tmp_path)); monkeypatch.delenv("AGENT_MAIL_PARENT", raising=False)
    mail = mr.load_mail()
    for name, kind in (("builder", "claude"), ("orchestrator", "claude"), ("owner", "human")):
        mail.register(name, ME, kind, auto=kind == "claude")
    monkeypatch.setattr(mr, "find_claude", lambda: "/x/claude")
    monkeypatch.setattr(mr, "choose", lambda *a, **k: None)
    monkeypatch.setattr(fleet, "codex_exe", lambda: None)
    a = types.SimpleNamespace(only="", once=False, dry_run=False, state_file=str(tmp_path / "local" / "state.json"), max_per_hour=0, max_per_day=0,
                              timeout_minutes=20, max_concurrent=None, max_hops=0, prompt_via="auto", permission_mode="auto", mcp=False, always_reply=False, no_loop=False)
    r = mr.Runner(a, mail)
    return types.SimpleNamespace(r=r, mail=mail, q=tmp_path / "Queue", root=tmp_path)

def events(q, kind=None):
    rows = [json.loads(l) for f in (q / "events").glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [e for e in rows if kind is None or e["kind"] == kind]

def limit_run(text):
    calls = []
    def run(exe, prompt, mode, cwd, *a, parent="", **k):
        calls.append(parent); mr.SESSIONS[parent] = ("11111111-2222-3333-4444-555555555555", str(cwd))
        return 1, json.dumps({"type": "result", "is_error": True, "result": text}), ""
    return run, calls

def codex_usable(monkeypatch):
    monkeypatch.setattr(fleet, "codex_exe", lambda: "/x/codex")
    monkeypatch.setattr(fleet, "codex_best", lambda: {"left": 90.0, "left_5h": 90.0, "resets_at": time.time() + 9e5})


# ---- 2 and 4: park instead of fail, then resume by itself ----
def test_a_limit_parks_the_message_and_it_resumes_after_the_reset(env, monkeypatch):
    r, mail, q = env.r, env.mail, env.q
    until = int(time.time()) + 7200
    run, calls = limit_run(f"Claude AI usage limit reached|{until}"); monkeypatch.setattr(mr, "run_claude", run)
    msg = mail.send("orchestrator", "builder", "Fix the pool", "Make the pool deeper.")
    agents = mail.load_agents()
    job = r.prepare("builder", agents["builder"], msg, agents, "/x/claude")
    assert isinstance(job, dict)
    r.execute(job)
    assert [m["id"] for m in mail.list_inbox("builder")] == [msg["id"]], "the message stays in the inbox"
    assert msg["id"] not in r.spent and r.failed == 0 and not events(q, "failed")
    paused = events(q, "paused")
    assert len(paused) == 1 and paused[0]["ref"] == msg["id"] and paused[0]["session"] == "11111111-2222-3333-4444-555555555555"
    assert "usage limit is reached. Resumes on its own" in paused[0]["detail"]
    block = json.loads((q / "usage" / "claude-blocked.json").read_text(encoding="utf-8"))
    assert block["until"] == until and block["host"] == ME and "usage limit reached" in block["reason"]
    assert not list(r.claims.glob("*.claim")), "its claims are released"
    # later passes: skipped, not started, and no second card update
    assert r.prepare("builder", agents["builder"], msg, agents, "/x/claude") is None
    assert len(calls) == 1 and len(events(q, "paused")) == 1 and len(events(q, "started")) == 1
    assert fleet.usage_newest("claude") == {}, "the block file is not a plan reading"
    # the reset passes: it starts again with no human action
    real = time.time; monkeypatch.setattr(mr.time, "time", lambda: real() + 3 * 3600)
    job = r.prepare("builder", agents["builder"], msg, agents, "/x/claude")
    assert isinstance(job, dict) and len(events(q, "started")) == 2
    r.drop(job["held"], job["shared"])

def test_a_newer_reading_under_the_limit_lifts_the_block(env):
    q = env.q; (q / "usage").mkdir(parents=True, exist_ok=True); t = time.time()
    fleet._write(q / "usage" / "claude-blocked.json", {"until": t + 86400, "at": t - 60, "reason": "x", "host": "PC"})
    fleet._write(q / "usage" / f"claude-{ME}.json", {"windows": {"seven_day": {"utilization": 1.0, "resetsAt": 1}}, "at": t})
    assert mr.claude_blocked() == t + 86400, "a reading at the limit keeps it"
    fleet._write(q / "usage" / f"claude-{ME}.json", {"windows": {"seven_day": {"utilization": 0.02, "resetsAt": 1}}, "at": t})
    assert mr.claude_blocked() == 0, "the limit lifted early"


# ---- 3: Codex for every agent while Claude is blocked ----
def test_codex_takes_builder_and_orchestrator_while_claude_is_blocked(env, monkeypatch):
    r, q = env.r, env.q
    codex_usable(monkeypatch)
    assert "codex" not in r.engines("builder"), "unblocked: codex_builds still decides"
    fleet._write(q / "usage" / "claude-blocked.json", {"until": time.time() + 3600, "at": time.time(), "reason": "weekly"})
    assert r.engines("builder") == ["codex"] and r.engines("orchestrator") == ["codex"] and r.engines("reviewer") == ["codex"]
    monkeypatch.setattr(fleet, "codex_best", lambda: {"left": 5.0, "left_5h": 90.0, "resets_at": time.time() + 9e5})
    assert r.engines("builder") == [], "no engine: park"

def test_blocked_orchestrator_runs_the_whole_request_on_codex(env, monkeypatch):
    r, mail, q = env.r, env.mail, env.q
    codex_usable(monkeypatch)
    fleet._write(q / "usage" / "claude-blocked.json", {"until": time.time() + 3600, "at": time.time(), "reason": "weekly"})
    seen = []
    monkeypatch.setattr(mr, "run_claude", lambda *a, **k: pytest.fail("Claude must not start"))
    monkeypatch.setattr(mr, "run_codex", lambda prompt, cwd, t, writes, **k: seen.append((prompt, writes)) or (0, "Done: the pool is deeper, checked with the tests.", ""))
    msg = mail.send("owner", "orchestrator", "Pool", "Make the pool deeper.")
    agents = mail.load_agents()
    job = r.prepare("orchestrator", agents["orchestrator"], msg, agents, "/x/claude")
    r.execute(job)
    assert seen and "Do the whole request yourself" in seen[0][0] and seen[0][1] is True
    assert not mail.list_inbox("orchestrator") and events(q, "done")[0]["detail"].startswith("Done: the pool")

def test_a_limit_mid_run_hands_the_build_to_codex(env, monkeypatch):
    r, mail, q = env.r, env.mail, env.q
    run, calls = limit_run("You've hit your weekly limit · resets Jan 16, 9am (America/Los_Angeles)"); monkeypatch.setattr(mr, "run_claude", run)
    codex_usable(monkeypatch)
    monkeypatch.setattr(mr, "run_codex", lambda prompt, *a, **k: (0, "Built it.", ""))
    msg = mail.send("orchestrator", "builder", "Fix", "Fix it.")
    agents = mail.load_agents()
    r.execute(r.prepare("builder", agents["builder"], msg, agents, "/x/claude"))
    assert calls and not mail.list_inbox("builder") and events(q, "done") and not events(q, "paused")


# ---- 5: requeue ----
def test_requeue_moves_a_failed_message_back(env, capsys):
    mail = env.mail
    msg = mail.send("orchestrator", "builder", "Fix", "Fix it.")
    mail.ack("builder", msg["id"], result="FAILED: claude exit code 1: You've hit your weekly limit", by="mail-runner@x")
    assert mr.requeue(mail, "builder", msg["id"]) == 0
    back = json.loads((env.q / "mail/builder/inbox" / f"{msg['id']}.json").read_text(encoding="utf-8"))
    assert not {"acked_at", "acked_by", "result"} & set(back) and back["body"] == "Fix it."
    assert not (env.q / "mail/builder/done" / f"{msg['id']}.json").exists()
    assert mr.requeue(mail, "builder", msg["id"]) == 1 and "already in" in capsys.readouterr().out
    assert mr.requeue(mail, "builder", "20261113T000000Z-00000000") == 1 and mr.requeue(mail, "builder", "../x") == 2


# ---- 6: session id ----
def test_every_claude_run_gets_a_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_ROOT", str(tmp_path)); seen, tails = [], []
    class P:
        pid = 1
        def __init__(self, cmd, **kw): seen.append(cmd)
        def wait(self, timeout=None): return 0
    class Tail:
        def __init__(self, *a): tails.append(a); self.stop = types.SimpleNamespace(set=lambda: None)
        def start(self): pass
        def join(self, t=None): pass
    monkeypatch.setattr(mr.subprocess, "Popen", P); monkeypatch.setattr(mr, "LiveTail", Tail)
    mr.run_claude("claude", "hi", "auto", tmp_path, 5, "builder", False, False, parent="m1")
    cmd = seen[0]; sid = cmd[cmd.index("--session-id") + 1]
    assert str(uuid.UUID(sid)) == sid and mr.SESSIONS.pop("m1") == (sid, str(tmp_path)) and tails[0][3:] == (sid, tmp_path)

def test_the_live_file_names_the_session(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_ROOT", str(tmp_path))
    out = tmp_path / "out.txt"
    out.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x/a.py"}}]}}) + "\n", encoding="utf-8")
    t = mr.LiveTail(out, "m-live", "builder", "sid-9", "/x/cwd"); t.start()
    try:
        end = time.time() + 8
        while not t.dest.exists() and time.time() < end: time.sleep(0.2)
        d = json.loads(t.dest.read_text(encoding="utf-8"))
        assert d["session"] == "sid-9" and d["cwd"] == "/x/cwd" and d["doing"] == "Reading a.py"
    finally: t.stop.set(); t.join(5)


# ---- 7: open requests ----
URL = "https://claude.ai/code/session_01NxSuwbDhyrgkQwyEKuQQzC"
SID = "f27998fa-fd99-4c31-ad4d-49937c557fc5"

def test_open_session_resumes_in_remote_control(tmp_path, monkeypatch):
    started, logs = [], []
    class P:
        pid = 1
        def __init__(self, cmd, stdout=None, **kw):
            started.append((cmd, kw)); stdout.write(b"backgrounded \xc2\xb7 f27998fa (idle)\n  claude logs f27998fa      show recent output\n"); stdout.flush()
        def wait(self, timeout=None): return 0
    def run(cmd, **kw):
        logs.append(cmd)
        text = "\x1b[2mstarting\x1b[0m" if len(logs) < 2 else f"/remote-control is active \x1b[1m·\x1b[0m Continue here, on your phone, or at \x1b[4m{URL}\x1b[0m"
        return types.SimpleNamespace(stdout=text, stderr="", returncode=0)
    monkeypatch.setattr(mr.subprocess, "Popen", P); monkeypatch.setattr(mr.subprocess, "run", run)
    monkeypatch.setattr(mr.time, "sleep", lambda s: None); monkeypatch.setattr(mr, "find_claude", lambda: "/x/claude")
    res = mr.open_session({"kind": "session", "session": SID, "cwd": str(tmp_path / "gone"), "name": "Kepler  pool fix"}, "auto", tmp_path)
    assert res == {"url": URL, "bg": "f27998fa"}
    cmd, kw = started[0]
    assert cmd == ["/x/claude", "--bg", "--remote-control", "Kepler pool fix", "--resume", SID, "--permission-mode", "auto"]
    assert kw["cwd"] == str(tmp_path) and kw["stdin"] == mr.subprocess.DEVNULL and kw["env"]["SESSION_HOOK_SKIP"] == "1", "a missing cwd falls back to the dev root"
    assert logs[0] == ["/x/claude", "logs", "f27998fa"]
    assert "error" in mr.open_session({"session": "not-a-uuid"}, "auto", tmp_path)

def test_runner_serves_open_requests_once_and_tracks_the_session(env, monkeypatch):
    r, q = env.r, env.q; d = q / "open" / ME; d.mkdir(parents=True)
    got = []
    monkeypatch.setattr(mr, "open_session", lambda req, mode, root: got.append((req, mode)) or {"url": URL, "bg": "f27998fa"})
    fleet._write(d / "r1.json", {"kind": "session", "session": SID, "cwd": str(env.root), "name": "x", "by": "owner", "at": time.time()})
    fleet._write(d / "old.json", {"kind": "file", "path": "a.md"}); os.utime(d / "old.json", (time.time() - 2 * 86400,) * 2)
    r.serve_open()
    end = time.time() + 5
    while not (d / "r1.done.json").exists() and time.time() < end: time.sleep(0.05)
    done = json.loads((d / "r1.done.json").read_text(encoding="utf-8"))
    assert done["url"] == URL and done["bg"] == "f27998fa" and done["at"] and got[0][1] == "auto"
    assert not (d / "old.json").exists(), "requests older than a day are deleted"
    assert json.loads((r.state.parent / "mail-runner-open.json").read_text(encoding="utf-8"))["bg"]["f27998fa"]["session"] == SID
    r.open_at = 0; r.serve_open(); time.sleep(0.2); assert len(got) == 1, "a done request is not run again"
    r.a.no_loop = True; fleet._write(d / "r2.json", {"kind": "session", "session": SID}); r.open_at = 0; r.serve_open(); time.sleep(0.2)
    assert not (d / "r2.done.json").exists(), "only the process that runs the loop serves them"

def test_open_file_rules(tmp_path, monkeypatch):
    root = tmp_path / "Dev" / "Root"; (root / "notes").mkdir(parents=True); other = tmp_path / "Dev" / "Stack.io" / "marketing"; other.mkdir(parents=True)
    for p in (root / "notes/a.md", other / "page.html", other / "run.sh", other / "x.command", tmp_path / "out.html"): p.write_text("x")
    mem = tmp_path / "Dev" / ".switchboard"; mem.mkdir(); (mem / "Device.md").write_text("x")
    monkeypatch.setattr(mr.Path, "home", lambda: tmp_path / "Dev")
    opened = []
    monkeypatch.setattr(mr.subprocess, "run", lambda cmd, **kw: opened.append(cmd))
    monkeypatch.setattr(mr.os, "startfile", lambda p: opened.append(["startfile", p]), raising=False)
    assert mr.open_file({"path": "notes/a.md"}, root) == {"opened": str((root / "notes/a.md").resolve())}, "relative to the dev root"
    assert mr.open_file({"path": str(other / "page.html")}, root)["opened"].endswith("page.html"), "another project beside the dev root"
    for path in (str(other / "run.sh"), str(other / "x.command"), str(tmp_path / "out.html"), "../../out.html", "~/.switchboard/Device.md", str(mem / "Device.md"), "notes/missing.md", ""):
        assert "error" in mr.open_file({"path": path}, root), path
    assert len(opened) == 2

def test_idle_background_sessions_are_stopped_after_12_hours(env, monkeypatch):
    r = env.r; old = time.time() - 13 * 3600
    fleet._write(r.state.parent / "mail-runner-open.json", {"bg": {"aaaa1111": {"busy": old}, "bbbb2222": {"busy": old}, "cccc3333": {"busy": old}, "dddd4444": {"busy": time.time()}}})
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        rows = [{"id": "aaaa1111", "status": "idle"}, {"id": "bbbb2222", "status": "busy"}, {"id": "dddd4444", "status": "idle"}]
        return types.SimpleNamespace(stdout=json.dumps(rows), stderr="", returncode=0)
    monkeypatch.setattr(mr.subprocess, "run", run)
    r.sweep_bg()
    assert ["/x/claude", "stop", "aaaa1111"] in calls and not any(c[1] == "stop" and c[2] != "aaaa1111" for c in calls)
    bg = json.loads((r.state.parent / "mail-runner-open.json").read_text(encoding="utf-8"))["bg"]
    assert set(bg) == {"bbbb2222", "dddd4444"} and bg["bbbb2222"]["busy"] > old, "stopped and gone ones are dropped; a busy one is fresh again"


# ---- 8: hop latency ----
def test_the_loop_looks_at_the_inboxes_every_3_seconds(env, monkeypatch):
    r, mail = env.r, env.mail; sleeps, served = [], []
    monkeypatch.setattr(r, "serve_open", lambda: served.append(1))
    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 4: mail.send("owner", "orchestrator", "Hi", "New mail.")
    r.idle(20, sleep=sleep)
    assert len(sleeps) == 4 and max(sleeps) <= 3 and len(served) == 4, sleeps
    assert "r.idle(a.poll)" in (TK / "mail-runner.py").read_text(encoding="utf-8")

