import _load  # noqa: F401  puts the switchboard folder on sys.path
import fleet

def dev(host, platform):
    return {"host": host, "label": "Mac" if platform == "mac" else "PC", "platform": platform, "available": True,
            "repos": [], "pressure": False, "res": {}}

def test_wants_platform():
    w = fleet.wants_platform
    assert w("airdrop this device a random photo") == "mac"
    assert w("send it via iMessage") == "mac"
    assert w("run it on the PC") == "windows"
    assert w("Kepler: fix the page size") is None
    assert w("Field Notebook: add a MacGuffin item") is None
    assert w("airdrop it from the pc") is None

def test_devices_text_and_where(monkeypatch):
    monkeypatch.setattr(fleet, "devices", lambda *a, **k: [dev("m", "mac"), dev("w", "windows")])
    t = fleet.devices_text("w")
    assert "AirDrop" in t and t.count("this run is here") == 1
    assert [l for l in t.splitlines() if "this run is here" in l][0].startswith("- PC (w")
    for i in fleet.where():
        assert i["platform"] and i["can"]
