"""Cross checks between the runner, the bridge and fleet: one liveness rule and per-device plan readings."""
import json, time

from _load import load, TK
import fleet

mr = load("mail_runner_int", "mail-runner.py")


# ---- one liveness shape and one set of thresholds ----
def reading(path, read_ago=2, relay_up=3600, **hosts):
    path.write_text(json.dumps({"read_at": time.time() - read_ago, "relay_up": relay_up, "peers": hosts}), encoding="utf-8")


def test_the_bridge_asks_fleet_and_both_agree(tmp_path, monkeypatch):
    src = (TK / "discord-bridge.py").read_text(encoding="utf-8")
    assert "fleet.confirmed_down(host, PEERS, missing=False)" in src and "PEER_GONE" not in src, "the bridge has no rule of its own"
    p = tmp_path / "peers.json"
    cases = [  # (reading kwargs, PC down?)
        (dict(PC={"push_age": 5, "beat_age": 20}), False),
        (dict(PC={"push_age": 119, "beat_age": 20}), True),  # 119 s at read time plus the 2 s the reading has been held
        (dict(PC={"push_age": 5, "beat_age": 151}), True),   # pushing, but its runner heartbeat is stale (fleet.FRESH)
        (dict(PC={"push_age": 5, "beat_age": None}), False),
        (dict(MAC={"push_age": 1, "beat_age": 1}), True),     # a relay up an hour never heard from it
        (dict(read_ago=61, PC={"push_age": 900}), False),     # an old reading proves nothing
        (dict(read_ago=-30, PC={"push_age": 900}), False),    # read in the future: a clock jump, proves nothing
        (dict(relay_up=30, PC={"push_age": 900}), False),     # a relay that just restarted has not heard from everyone
    ]
    for kw, down in cases:
        reading(p, **kw)
        assert fleet.confirmed_down("pc", p) is down and fleet.confirmed_down("PC", p, missing=False) is down, kw
    p.write_text("[1]", encoding="utf-8"); assert fleet.confirmed_down("PC", p) is False
    p.unlink()
    assert fleet.confirmed_down("PC", p) is True and fleet.confirmed_down("PC", p, missing=False) is False, "no file: legacy rule for runners, no proof for the bridge"


# ---- per-device plan readings ----
def rl(util, kind="seven_day"):
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {kind: {"utilization": util, "resetsAt": int(time.time()) + 4 * 86400}}}})


def test_a_runner_run_writes_this_devices_reading_and_week_pace_reads_the_newest(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_ROOT", str(tmp_path)); monkeypatch.setattr(mr, "hostname", lambda: "MAC-1")
    (tmp_path / "Queue/discord").mkdir(parents=True); (tmp_path / "Queue/discord/config.json").write_text(json.dumps({"hosts": ["PC-1", "MAC-1"]}))
    u = tmp_path / "Queue/usage"; u.mkdir(parents=True)
    (u / "claude.json").write_text(json.dumps({"windows": {"seven_day": {"utilization": 0.1, "resetsAt": time.time() + 4 * 86400}}, "at": time.time() - 3 * 3600}))
    mr.save_reading("noise\n" + rl(0.42) + "\n")
    r = json.loads((u / "claude-MAC-1.json").read_text())
    assert r["host"] == "MAC-1" and r["windows"]["seven_day"]["utilization"] == 0.42
    assert json.loads((u / "claude.json").read_text())["windows"]["seven_day"]["utilization"] == 0.1, "only the bridge's primary writes the legacy file"
    assert round(fleet.week_pace()["used"]) == 42, "week_pace reads the newest per-device reading"
    (u / "claude-w-MAC-1.json").write_text(json.dumps({"start": 1, "w": 9, "at": time.time() + 99}))
    assert round(fleet.week_pace()["used"]) == 42, "scan totals are not readings"
    mr.save_reading("no rate limit lines\n"); assert json.loads((u / "claude-MAC-1.json").read_text())["at"] == r["at"], "no reading, no write"
    monkeypatch.setattr(mr, "hostname", lambda: "pc-1"); mr.save_reading(rl(0.5))
    assert json.loads((u / "claude.json").read_text())["windows"]["seven_day"]["utilization"] == 0.5, "the primary mirrors the newest"


def test_execute_saves_the_reading_only_for_claude_runs():
    src = (TK / "mail-runner.py").read_text(encoding="utf-8")
    i = src.index("try: save_reading(out)"); assert 'if used == "claude":' in src[i - 60:i]
