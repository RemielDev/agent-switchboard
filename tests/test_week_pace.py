import json, sys, time, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import fleet

def usage(tmp, util, resets, at):
    d = tmp / "usage"; d.mkdir(parents=True, exist_ok=True)
    (d / "claude.json").write_text(json.dumps({"windows": {"seven_day": {"utilization": util, "resetsAt": resets}}, "at": at, "host": "x"}), encoding="utf-8")

def test_week_pace(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "queue_root", lambda: tmp_path)
    now = time.time(); usage(tmp_path, 0.25, now + 4.5 * 86400, now)
    p = fleet.week_pace(now)
    assert round(p["used"]) == 25 and abs(p["elapsed"] - 35.7) < 0.1 and abs(p["projected"] - 70) < 0.1
    t = fleet.pace_text(p)
    assert "25% used" in t and "36% of the week gone" in t and "<t:" in t and "run out" not in t
    t = fleet.pace_text(p, discord=False)
    assert "UTC" in t and "<t:" not in t
    usage(tmp_path, 0.6, now + 4.5 * 86400, now)
    assert "on pace to run out" in fleet.pace_text(fleet.week_pace(now))

def test_week_pace_edges(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "queue_root", lambda: tmp_path)
    now = time.time()
    assert fleet.week_pace(now) is None, "missing file"
    usage(tmp_path, 0.25, now + 4.5 * 86400, now - 7 * 3600)
    assert fleet.week_pace(now) is None, "older than 6 h"
    usage(tmp_path, 0.02, now + 6.9 * 86400, now)
    p = fleet.week_pace(now)
    assert p["elapsed"] < 5 and p["projected"] is None

def test_receipt_outlier(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "queue_root", lambda: tmp_path)
    for i in range(10): fleet.stat({"engine": "claude", "thread": f"t{i}", "secs": 60, "cost": 1.0})
    fleet.stat({"engine": "claude", "thread": "big", "secs": 60, "cost": 6.0})
    assert fleet.receipt("big").endswith("6x a usual request")

def test_receipt_no_outlier_with_few_threads(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "queue_root", lambda: tmp_path)
    for i in range(5): fleet.stat({"engine": "claude", "thread": f"t{i}", "secs": 60, "cost": 1.0})
    fleet.stat({"engine": "claude", "thread": "big", "secs": 60, "cost": 6.0})
    assert "x a usual request" not in fleet.receipt("big")
