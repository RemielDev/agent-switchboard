"""Outcomes: a shipped change records its metric, the next cycle judges it once it has been live long enough,
an open question no longer stops an auto-mode cycle, and dry spells pull the owner in."""
import json, sys, time
import pytest
from _load import load
gl = load("improve_loop_out", "improve-loop.py")

@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_ROOT", str(tmp_path)); (tmp_path / "Queue" / "loop").mkdir(parents=True)
    return tmp_path

def cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["improve-loop.py", *argv]); gl.main()

def test_record_judge_and_wait(root, monkeypatch, capsys):
    cli(monkeypatch, "outcome", "Stack.io", "--change", "fix join link", "--metric", "creator signups", "--direction", "up", "--baseline", "0 of 12, day 1", "--days", "3")
    rs = gl.outcomes("Stack.io"); assert len(rs) == 1 and rs[0]["live_at"] and rs[0]["verdict"] is None
    oid = rs[0]["id"]
    assert gl.judge_block("Stack.io") == ""  # not due yet
    later = time.time() + 4 * 86400
    b = gl.judge_block("Stack.io", now=later); assert oid in b and "creator signups should go up" in b and "verdict" in b
    cli(monkeypatch, "verdict", "Stack.io", oid, "wait", "--days", "5", "--now", "1 of 14")
    assert gl.judge_block("Stack.io", now=later) == ""
    assert oid in gl.judge_block("Stack.io", now=later + 6 * 86400)
    cli(monkeypatch, "verdict", "Stack.io", oid, "improved", "--now", "4 of 20")
    assert gl.judge_block("Stack.io", now=later + 30 * 86400) == "" and gl.outcomes("Stack.io")[0]["verdict"] == "improved"
    assert list((root / "Queue" / "loop" / "Stack.io").glob("outcomes.*.jsonl"))  # one file per device

def test_three_dry_cycles_ask_why(root):
    for i in range(3): gl._append("Kepler", "cycles", {"t": time.time() - 100 + i})
    assert "shipped nothing" in gl.dry_block("Kepler")
    gl._append("Kepler", "outcomes", {"kind": "outcome", "id": "x", "t": time.time(), "change": "c", "metric": "m", "direction": "up", "baseline": "b"})
    assert gl.dry_block("Kepler") == ""

def test_open_question_blocks_ask_mode_only(root, monkeypatch):
    sent = []
    class Mail:
        def send(self, *a, **k): sent.append((a, k)); return {"id": "m1", "thread": "t1"}
    cfg = {"enabled": True, "hosts": [gl.hostname()], "repos": {"Kepler": {"mode": "ask"}}, "projects": {}, "auto_add": False}
    for name, v in {"leader": lambda h: gl.hostname(), "where": lambda g: [{"ok": True, "label": "Mac", "why": ""}], "stats": lambda h: [], "event": lambda *a, **k: None}.items():
        monkeypatch.setattr(gl.fleet, name, v)
    monkeypatch.setattr(gl, "config", lambda: {**{"interval_hours": 8, "repos": {}, "projects": {}, "daily_budget": 0, "ignore": [], "pending": {}, "snoozed": {}, "min_sessions": 5}, **cfg})
    for n in ("scan_activity", "maintain"): monkeypatch.setattr(gl, n, lambda *a, **k: None)
    monkeypatch.setattr(gl, "open_asks", lambda n: [{"id": "q1", "subject": "Kepler: build the thing?", "at": time.time() - 3 * 86400}])
    st = {"maintained": time.time(), "retro": time.time()}
    monkeypatch.setattr(gl, "load", lambda n, d: st if n == "state.json" else d); monkeypatch.setattr(gl, "save", lambda n, v: None)
    gl.tick(Mail(), say=lambda *a: None); assert sent == []  # ask mode waits on its question
    cfg["repos"]["Kepler"]["mode"] = "auto"
    gl.tick(Mail(), say=lambda *a: None); assert len(sent) == 1
    body = sent[0][0][3]
    assert "Do not ask these again" in body and "q1" in body and "When to pull the owner in" in body and "Go ahead without asking" in body

def test_project_prompt_ships_with_route_and_campaign_rules():
    p = {"path": "/x", "host": "Mac", "mode": "auto", "deploy": "merge the PR into production."}
    body = gl.PROJECT_BODY.format(name="Stack.io", path="/x", host="Mac", brief="b", area="stack-io", gate=gl.GATES["auto"], judge="", needs=gl.NEEDS,
                                  campaigns=gl.CAMPAIGNS, ship=gl.SHIP_SET.format(deploy=p["deploy"]))
    assert "deploy it yourself: merge the PR into production." in body and "At the same spend" in body and "Never publish, deploy" not in body
