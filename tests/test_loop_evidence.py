"""Cycle prompts start from evidence, point at the rejected-ideas doc, and ship through the entry's deploy route."""
import json
import pytest
from _load import load
gl = load("improve_loop_ev", "improve-loop.py")

@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_ROOT", str(tmp_path)); (tmp_path / "Queue" / "loop").mkdir(parents=True)
    return tmp_path

def repo_body(**k):
    return gl.BODY.format(**{"repo": "Kepler", "gate": "G", "judge": "", "needs": gl.NEEDS, "ship": gl.SHIP_UNSET, **k})

def test_evidence_comes_before_the_pick_in_both_prompts():
    body = repo_body()
    assert "Start from evidence" in body and "Name the one metric it should move" in body
    assert body.index("Start from evidence") < body.index("3. Pick")
    assert "docs/rejected.md" in body and "docs/rejected.md" in gl.PROJECT_BODY

def test_repo_cycle_ships_through_its_deploy_route(root, monkeypatch):
    (root / "Queue" / "loop" / "config.json").write_text(json.dumps({"repos": {"Kepler": {"mode": "auto", "deploy": "merge the PR into main."}}}))
    sent = []
    class Mail:
        def send(self, *a, **k): sent.append((a, k)); return {"id": "m1", "thread": "t1"}
    monkeypatch.setattr(gl.fleet, "event", lambda *a, **k: None)
    gl.start(Mail(), "Kepler", "auto", say=lambda *a: None)
    body = sent[0][0][3]
    assert "deploy it yourself: merge the PR into main." in body and "Go ahead without asking" in body and "{" not in body.split("When to pull")[0]

def test_repo_without_a_route_leaves_the_change_ready_and_asks():
    assert "no deploy route" in repo_body() and "Deploy it" in repo_body()
