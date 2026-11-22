"""One writer key for the switchboard folder, per-host claim files, no takeover the liveness relay does not confirm, the build change log."""
import json, time, types
import pytest
from _load import load, TK
import fleet
mr = load("mail_runner_coord", "mail-runner.py")
ME = mr.hostname()

class Mail:
    class MailError(Exception): pass
    def __init__(self): self.sent = []
    def load_agents(self): return {}
    def thread(self, tid): return []
    def send(self, *a, **k): self.sent.append((a, k)); return {"id": "q1"}
    def write_json_atomic(self, path, data): fleet._write(path, data)

@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_ROOT", str(tmp_path))
    a = types.SimpleNamespace(only="", once=False, dry_run=False, state_file=str(tmp_path / "state.json"), max_per_hour=0, max_per_day=0, timeout_minutes=20, max_concurrent=None)
    r = mr.Runner(a, Mail()); r.claims = tmp_path / "local-claims"
    return r

def other(r, name, host="OTHER-PC", flat=False, **extra):
    d = r.q / "mail" / "claims" / ("" if flat else host)
    fleet._write(d / f"{name}.json", {"host": host, "at": time.time(), "mid": "m-other", **extra})
    return d / f"{name}.json"

# ---- one area key ----
def test_toolkit_areas_share_one_key(runner, tmp_path):
    key = lambda body, cwd=tmp_path: runner.repo_key({"body": body}, cwd, "builder")
    assert key("x\nAREA: discord-bridge") == key("x\nAREA: orchestration") == key("x\nAREA: mail-runner") == "switchboard"
    assert key("x\nAREA: field-notebook") == "field-notebook"
    assert runner.repo_key({"repo": "Fleet"}, tmp_path / "Fleet", "builder") == "fleet", "a repo is never aliased"
    assert runner.repo_key({"body": "no area"}, TK, "builder") == "switchboard", "work started in the switchboard folder"
    (runner.q / "mail").mkdir(parents=True, exist_ok=True); (runner.q / "mail" / "runner.json").write_text(json.dumps({"area_alias": {"docs-site": "switchboard"}}))
    assert key("x\nAREA: docs-site") == "switchboard"

def test_bridge_and_loop_areas_hold_each_other(runner):
    a = runner.take("m1", "t1", runner.repo_key({"body": "AREA: discord-bridge"}, runner.root, "builder"), 600, loose=True)
    b = runner.take("m2", "t2", runner.repo_key({"body": "AREA: orchestration"}, runner.root, "builder"), 600, loose=True)
    assert a and b is None
    runner.release(a)
    assert runner.take("m2", "t2", "switchboard", 600, loose=True)

# ---- per-host claim files ----
def test_own_claims_live_in_own_folder(runner):
    p = runner.own_claim("area-switchboard")
    assert p.parent.name == mr.re.sub(r"[^\w.-]", "_", ME) and p.parent.parent == runner.q / "mail" / "claims"

def test_other_hosts_claim_holds_in_its_folder_and_flat(runner):
    other(runner, "area-kepler")
    assert runner.take("m1", "t1", "kepler", 600) is None
    (runner.q / "mail/claims/OTHER-PC/area-kepler.json").unlink(); other(runner, "area-kepler", flat=True)
    assert runner.take("m1", "t1", "kepler", 600) is None, "legacy flat files from older code still hold"

def test_drop_never_deletes_the_other_devices_claim(runner):
    theirs = other(runner, "thread-t1"); legacy = other(runner, "m9", flat=True)
    mine = [runner.own_claim("thread-t1"), runner.own_claim("m1")]
    for f in mine: fleet._write(f, {"host": ME, "at": time.time()})
    runner.drop([], mine)
    assert theirs.exists() and legacy.exists() and not any(f.exists() for f in mine)

def test_active_work_reads_per_host_claims(runner, monkeypatch):
    mid = "20261112T000000Z-aaaaaaaa"
    box = runner.q / "mail" / "builder" / "inbox"; box.mkdir(parents=True)
    (runner.q / "mail" / "agents.json").write_text(json.dumps({"builder": {"kind": "claude", "auto": True}}))
    (box / f"{mid}.json").write_text(json.dumps({"id": mid, "thread": mid, "subject": "Kepler: x", "needs_reply": True}))
    other(runner, mid)
    rows = fleet.active_work()
    assert rows[0]["items"][0]["state"] == "running on OTHER-PC" and rows[0]["title"] == "Kepler: x"

# ---- liveness from the relay ----
@pytest.fixture
def stale_pc(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(fleet, "PEERS", tmp_path / "peers.json")
    monkeypatch.setattr(fleet, "resources", lambda *a, **k: {})
    monkeypatch.setattr(fleet, "available", lambda h: str(h).lower() == ME.lower())
    monkeypatch.setattr(fleet, "devices", lambda *a, **k: [{"host": "PC", "platform": "windows", "available": False, "repos": []}, {"host": ME, "platform": "mac", "available": True, "repos": []}])
    monkeypatch.setattr(fleet, "stats", lambda *a, **k: [])
    return runner

def peers(path, read_ago, push_age=None, beat_age=None, relay_up=3600):
    d = {"read_at": time.time() - read_ago, "relay_up": relay_up, "peers": {ME: {"push_age": 1, "beat_age": 5}}}
    if push_age is not None: d["peers"]["PC"] = {"push_age": push_age, "beat_age": beat_age}
    path.write_text(json.dumps(d))

def test_mac_holds_while_the_pc_still_pushes(stale_pc, tmp_path):
    peers(tmp_path / "peers.json", 2, push_age=5, beat_age=20)
    assert stale_pc.mine("builder", ["PC", ME], {"id": "m1", "subject": "x"}) is False

def test_mac_takes_over_once_the_relay_confirms_the_pc_is_down(stale_pc, tmp_path):
    peers(tmp_path / "peers.json", 2, push_age=300, beat_age=None)
    assert stale_pc.mine("builder", ["PC", ME], {"id": "m1", "subject": "x"}) is True

def test_mac_holds_without_a_fresh_reading(stale_pc, tmp_path):
    peers(tmp_path / "peers.json", 120, push_age=300)
    assert stale_pc.mine("builder", ["PC", ME], {"id": "m1", "subject": "x"}) is False, "an old reading may mean this device is the one offline"
    (tmp_path / "peers.json").write_text("{bad")
    assert stale_pc.mine("builder", ["PC", ME], {"id": "m1", "subject": "x"}) is False

def test_a_relay_that_just_restarted_confirms_nothing(stale_pc, tmp_path):
    peers(tmp_path / "peers.json", 2, push_age=300, relay_up=30)
    assert stale_pc.mine("builder", ["PC", ME], {"id": "m1", "subject": "x"}) is False, "push times reloaded from the relay's state.json are old until each host pushes again"

def test_no_peers_file_keeps_the_old_heartbeat_rule(stale_pc):
    assert stale_pc.mine("builder", ["PC", ME], {"id": "m1", "subject": "x"}) is True

def test_a_long_unconfirmed_hold_asks_owner_once(stale_pc, tmp_path):
    peers(tmp_path / "peers.json", 2, push_age=5)
    msg = {"id": "m1", "subject": "x"}
    stale_pc.age0[("unconfirmed", "m1")] = time.time() - 700
    assert stale_pc.mine("builder", ["PC", ME], msg) is False and stale_pc.mine("builder", ["PC", ME], msg) is False
    assert len(stale_pc.mail.sent) == 1 and stale_pc.mail.sent[0][1]["options"][0]["action"]["redirect"]["device"] == ME

def test_leader_follows_the_same_rule(stale_pc, tmp_path):
    peers(tmp_path / "peers.json", 2, push_age=5)
    assert fleet.leader(["PC", ME]) == "PC"
    peers(tmp_path / "peers.json", 2, push_age=300)
    assert fleet.leader(["PC", ME]) == ME
    peers(tmp_path / "peers.json", 120, push_age=300)
    assert fleet.leader(["PC", ME]) == "PC"
    assert fleet.leader([ME, "PC"]) == ME

# ---- change log ----
def test_change_log_rows_merge_and_reach_the_prompt(runner, monkeypatch):
    info = {"files": [["src/a.py", 40, 12], ["src/b.py", 1, 0]], "undo": "m1"}
    mr.log_change("kepler", {"id": "m1", "thread": "t1", "subject": "Kepler: page size"}, "builder", "claude-sonnet-5-5", "**Done.** Fixed the aim. Also more.", info)
    d = mr.queue_root() / "changes" / "kepler"
    (d / "DESK-PC.jsonl").write_text(json.dumps({"ts": time.time() - 3600, "host": "DESK-PC", "agent": "builder", "subject": "Kepler: config only", "files": [], "summary": "Changed a setting in the web console."}) + "\n")
    rows = mr.recent_changes("kepler")
    assert len(rows) == 2 and "DESK-PC builder Kepler: config only: no files; Changed a setting" in rows[0]
    assert "2 files (src/a.py, ..., +41 -12), undo m1" in rows[1]
    row = json.loads((d / f"{ME}.jsonl").read_text().splitlines()[0])
    assert row["summary"] == "Done." and row["files"][0] == ["src/a.py", 40, 12]
    same = mr.same_repo_text(runner.mail, {"id": "t2", "thread": "t2", "repo": "Kepler"})
    assert "- build: " in same and "page size" in same and all(len(l) <= 150 for l in same.splitlines()), same

def test_snapshot_skips_sync_conflict_copies(tmp_path):
    (tmp_path / "a.py").write_text("x"); (tmp_path / "a.sync-conflict-20261112-1-AAA.py").write_text("y"); (tmp_path / "~syncthing~b.py.tmp.py").write_text("z")
    assert set(mr.snapshot(tmp_path)) == {"a.py"}

# ---- review fixes ----
def test_leader_needs_this_devices_own_heartbeat_like_pick_host(stale_pc, monkeypatch):
    monkeypatch.setattr(fleet, "available", lambda h: False)
    assert fleet.leader([ME, "PC"]) is None, "no peers file: the old pick_host rule, so a stale own heartbeat does not lead"

def test_each_claim_is_also_written_flat_for_older_code(runner, monkeypatch):
    monkeypatch.setattr(mr, "LEGACY_FLAT_CLAIMS", True)  # the changeover setting, whatever the live value is
    files = runner.shared_claims("m1", "t1", "kepler")
    own = [f for f in files if f.parent.name != "claims"]; flat = [f for f in files if f.parent.name == "claims"]
    assert [f.name for f in own] == [f.name for f in flat] == ["m1.json", "thread-t1.json", "area-kepler.json"]
    for f in files: fleet._write(f, {"host": ME, "at": time.time(), "mid": "m1"})
    assert runner.take("m2", "t2", "kepler", 600) is not None, "this device's own flat copy never holds it"
    theirs = other(runner, "thread-t1", host="OTHER-PC", flat=True)  # an older runner on the other device rewrote the flat file
    runner.drop([], files)
    assert theirs.exists() and not any(f.exists() for f in files if f != theirs)

def test_no_flat_copies_once_the_changeover_flag_is_off(runner, monkeypatch):
    monkeypatch.setattr(mr, "LEGACY_FLAT_CLAIMS", False)
    files = runner.shared_claims("m1", "t1", "kepler")
    assert [f.name for f in files] == ["m1.json", "thread-t1.json", "area-kepler.json"]
    assert all(f.parent.name != "claims" for f in files), "only this device's own folder is written"

def test_codex_builds_stay_off_without_the_flag(runner, monkeypatch):
    monkeypatch.setattr(fleet, "codex_best", lambda: {"left": 90.0, "left_5h": 90.0, "resets_at": time.time() + 9e5})
    monkeypatch.setattr(fleet, "codex_exe", lambda: "/x/codex"); monkeypatch.setattr(mr, "find_claude", lambda: "/x/claude")
    cfgfile = runner.q / "mail" / "runner.json"; cfgfile.parent.mkdir(parents=True, exist_ok=True)
    R = mr.load_route()
    for raw in (None, "{bad", json.dumps({"routing": {}}), json.dumps({"routing": {"codex_builds": False}})):
        if raw is None: cfgfile.unlink(missing_ok=True)
        else: cfgfile.write_text(raw)
        assert "codex" not in runner.engines("builder") and "codex" in runner.engines("reviewer"), raw
        assert R.pick("builder", "add a popup", codex_ok=True, cfg=mr.routing_cfg())["engine"] == "claude", raw
    cfgfile.write_text(json.dumps({"routing": {"codex_builds": True}}))
    assert "codex" in runner.engines("builder")
