import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import fleet

def dev(host, repos, pressure=False):
    return {"host": host, "label": host, "available": True, "repos": repos,
            "pressure": pressure, "res": {"why": "test"}}

def setup(monkeypatch, b_pressure=False):
    monkeypatch.setattr(fleet, "devices", lambda: [dev("a", ["Old"]), dev("b", [], b_pressure)])

def test_new_repo_all_ok(monkeypatch):
    setup(monkeypatch)
    assert [i["ok"] for i in fleet.where("Brand New")] == [True, True]

def test_known_repo_only_holder(monkeypatch):
    setup(monkeypatch)
    r = fleet.where("Old")
    assert [i["ok"] for i in r] == [True, False]
    assert r[1]["why"] == "this repo's folder is not on this device"

def test_pressure_still_blocks(monkeypatch):
    setup(monkeypatch, True)
    r = fleet.where("Brand New")
    assert r[0]["ok"] and not r[1]["ok"]
