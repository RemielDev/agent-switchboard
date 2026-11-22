import time
import fleet

def test_requests_view(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "queue_root", lambda: tmp_path)
    t = time.time()
    rows = [  # thread A: request 1 one cheap build, request 2 two builds and a review; thread B: big build; an old row without req
        {"thread": "A", "req": 100.0, "agent": "orchestrator", "cost": 0.5},
        {"thread": "A", "req": 100.0, "agent": "builder", "route": "build_cheap:repo", "cost": 1.0, "checks": 2, "checks_setup": 1},
        {"thread": "A", "req": 200.0, "agent": "builder", "route": "build_cheap:repo", "cost": 1.0, "checks": 2, "checks_setup": 0},
        {"thread": "A", "req": 200.0, "agent": "builder", "route": "build_big:repo", "cost": 2.0},
        {"thread": "A", "req": 200.0, "agent": "reviewer", "cost": 1.5},
        {"thread": "B", "req": 0.0, "agent": "builder", "route": "build_big:repo", "cost": 3.0},
        {"thread": "C", "agent": "builder", "route": "build_big:repo", "cost": 9.0},
    ]
    for i, r in enumerate(rows): fleet.stat(r)
    v = fleet.requests_view(24)
    c, b = v["build_cheap:repo"], v["build_big:repo"]
    assert c["requests"] == 2 and c["builds_per_request"] == 1.5 and c["two_or_more"] == 0.5 and c["reviews_per_request"] == 0.5
    assert c["cost_per_request"] == 3.0 and c["setup_share"] == 0.25
    assert b["requests"] == 1 and b["cost_per_request"] == 3.0 and b["setup_share"] is None
    assert "| build_cheap:repo | 2 |" in fleet.requests_text(24)
