"""One writer per synced file: per-host copies, the merge rules, and the conflict resolver."""
import json, os, time
from pathlib import Path
import pytest
from _load import load
import fleet

ME = fleet.hostname()

def put(path, data): path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(data), encoding="utf-8")
def read(path): return json.loads(Path(path).read_text(encoding="utf-8"))

def test_fleet_host_path_and_files(tmp_path):
    s = tmp_path / "state.json"
    assert fleet.host_path(s, "PC") == tmp_path / "state.PC.json"
    for n in ("state.PC.json", "state.Mac-Book.json", "state.json", "state.sync-conflict-20250301-102546-YUTVQYM.json", "state.json.pre-x.bak", "statex.PC.json"): (tmp_path / n).write_text("{}")
    assert [h for h, _ in fleet.host_files(s)] == ["Mac-Book", "PC"], "conflict copies, backups and other names are no host's copy"

def test_fleet_save_host_writes_only_its_own_copy_atomically(tmp_path):
    s = tmp_path / "state.json"; put(s, {"a": 1}); before = s.read_bytes()
    t = fleet.save_host(s, {"a": 2, "saved": 1, "merged_from": {"x": 1}, "seen": [1]}, "PC")
    assert s.read_bytes() == before, "the legacy file is never written"
    own = read(tmp_path / "state.PC.json"); assert own["a"] == 2 and own["saved"] == t and own["saved"] > 1 and own["seen"] == [1], "a key of its own named seen is kept"
    assert not list(tmp_path.glob(".*.tmp")), "written through a temp file and renamed"

def test_merge_timestamp_maps_take_the_max_per_key(tmp_path):
    s = tmp_path / "state.json"
    put(tmp_path / "state.PC.json", {"last": {"Kepler": 300, "Birch": 100}, "maintained": 50, "retro": 900, "saved": 20})
    put(tmp_path / "state.Mac.json", {"last": {"Kepler": 200, "Fish": 400}, "maintained": 70, "retro": 10, "saved": 10})
    m = fleet.load_merged(s, {}, fleet.LOOP_MAX)
    assert m["last"] == {"Kepler": 300, "Birch": 100, "Fish": 400} and m["maintained"] == 70 and m["retro"] == 900
    assert "saved" not in m and "merged_from" not in m

def test_merge_dicts_union_newer_wins_per_key_scalars_from_newest(tmp_path):
    s = tmp_path / "state.json"
    put(s, {"threads": {"t0": "legacy", "t1": "legacy"}, "mode": "legacy", "only_legacy": 1})
    put(tmp_path / "state.PC.json", {"threads": {"t1": "pc", "t2": "pc"}, "mode": "pc", "list": [1], "saved": 100})
    put(tmp_path / "state.Mac.json", {"threads": {"t2": "mac", "t3": "mac"}, "mode": "mac", "list": [2], "saved": 50})
    m = fleet.load_merged(s)
    assert m["threads"] == {"t0": "legacy", "t1": "pc", "t2": "pc", "t3": "mac"}, "union; the newer copy (by saved) wins per key; legacy is the oldest"
    assert m["mode"] == "pc" and m["list"] == [1] and m["only_legacy"] == 1

def test_merge_legacy_is_the_lowest_priority_whatever_its_age(tmp_path):
    s = tmp_path / "state.json"; put(s, {"x": "legacy"}); os.utime(s, (time.time() + 999, time.time() + 999))
    put(tmp_path / "state.PC.json", {"x": "pc", "saved": 1})
    assert fleet.load_merged(s)["x"] == "pc"
    assert fleet.load_merged(tmp_path / "none.json", {"d": 1}) == {"d": 1}

def test_merge_a_delete_is_a_tombstone_and_holds_against_older_copies(tmp_path):
    s = tmp_path / "state.json"
    put(tmp_path / "state.Mac.json", {"threads": {"a": 1, "gone": 1}, "saved": 10})
    m = fleet.load_merged(s); m["threads"].pop("gone"); t = fleet.save_host(s, m, "PC")
    own = read(tmp_path / "state.PC.json"); assert own["stamps"] == {"threads\tgone": [t, False]} and "merged_from" not in own, own
    assert fleet.load_merged(s)["threads"] == {"a": 1}, "the PC removed 'gone'"
    put(tmp_path / "state.Mac.json", {"threads": {"a": 1, "gone": 1, "new": 1}, "saved": t + 5})
    assert fleet.load_merged(s)["threads"] == {"a": 1, "new": 1}, "a later Mac save without a stamp for 'gone' does not bring it back; its new entry counts"
    put(tmp_path / "state.Mac.json", {"threads": {"a": 1, "gone": 2}, "saved": t + 6, "stamps": {"threads\tgone": [t + 6, True]}})
    assert fleet.load_merged(s)["threads"] == {"a": 1, "gone": 2}, "written again after the deletion: the newer stamp wins"
    m = fleet.load_merged(s); fleet.save_host(s, m, "PC")
    assert read(tmp_path / "state.PC.json")["stamps"] == {"threads\tgone": [t + 6, True]}, "the merged stamp is carried forward, the beaten tombstone is gone"

def test_merge_an_offline_save_does_not_bring_back_a_deleted_question(tmp_path):
    """The PC goes offline holding q.m1; the Mac answers m1 (deletes it); the PC saves something unrelated."""
    s = tmp_path / "state.json"
    put(tmp_path / "state.Mac.json", {"q": {"m1": {"thread": "T"}, "m2": {"thread": "U"}}, "alias": {}, "saved": 10})
    pc = fleet.load_merged(s)                                    # the PC reads, then loses the network
    mac = fleet.load_merged(s); del mac["q"]["m1"]; fleet.save_host(s, mac, "Mac")
    pc["alias"]["x"] = "unrelated"; fleet.save_host(s, pc, "PC")  # offline: it never saw the Mac's newer copy
    assert "m1" in read(tmp_path / "state.PC.json")["q"], "the PC's own copy still holds it"
    m = fleet.load_merged(s); assert sorted(m["q"]) == ["m2"] and m["alias"] == {"x": "unrelated"}, m
    fleet.save_host(s, m, "PC"); assert sorted(fleet.load_merged(s)["q"]) == ["m2"], "and it stays answered once the PC has merged"

def test_merge_max_keys_ignore_what_a_copy_claims_to_have_read(tmp_path):
    """A slower host's copy named the faster host's revision as read and replaced maintained 1000 with 700."""
    s = tmp_path / "state.json"
    put(tmp_path / "state.Mac.json", {"maintained": 1000, "last": {"Kepler": 900}, "saved": 10})
    put(tmp_path / "state.PC.json", {"maintained": 700, "last": {"Kepler": 600}, "saved": 20, "merged_from": {"Mac": 10}})
    m = fleet.load_merged(s, {}, fleet.LOOP_MAX); assert m == {"maintained": 1000, "last": {"Kepler": 900}}, m
    put(tmp_path / "state.PC.json", {"maintained": 700, "last": {}, "saved": 30, "stamps": {"maintained": [30, True], "last\tKepler": [30, False]}})
    m = fleet.load_merged(s, {}, fleet.LOOP_MAX); assert m == {"maintained": 1000, "last": {"Kepler": 900}}, ("no stamp or tombstone lowers a max key", m)

def test_merge_old_tombstones_are_pruned_after_30_days(tmp_path):
    s = tmp_path / "state.json"; now = time.time()
    put(tmp_path / "state.PC.json", {"q": {}, "saved": 5, "stamps": {"q\told": [now - 31 * 86400, False], "q\trecent": [now - 86400, False]}})
    fleet.save_host(s, fleet.load_merged(s), "PC")
    assert list(read(tmp_path / "state.PC.json")["stamps"]) == ["q\trecent"]

def test_fleet_load_newest_takes_one_whole_copy(tmp_path):
    s = tmp_path / "Kepler.json"; put(s, {"pulled_at": "old", "metrics": [0]})
    assert fleet.load_newest(s)["pulled_at"] == "old", "the legacy file when no host copy exists"
    put(tmp_path / "Kepler.PC.json", {"pulled_at": "a", "metrics": [1], "health": {"x": 1}, "saved": 5})
    put(tmp_path / "Kepler.Mac.json", {"pulled_at": "b", "metrics": [2], "saved": 9})
    assert fleet.load_newest(s) == {"pulled_at": "b", "metrics": [2]}, "two pulls are never mixed"

def test_fleet_read_lines_unions_every_copy_in_order(tmp_path):
    f = tmp_path / "janitor-issues.jsonl"
    f.write_text('{"at": 1, "issue": "a"}\n{"at": 3, "issue": "c"}\n')
    (tmp_path / "janitor-issues.PC.jsonl").write_text('{"at": 2, "issue": "b"}\n{"at": 3, "issue": "c"}\n')
    assert [json.loads(l)["issue"] for l in fleet.read_lines(f, key=fleet.json_at)] == ["a", "b", "c"]

def test_fleet_union_lines_keeps_both_sides_in_place():
    a = ["# Log", "- one", "- two", "- four"]; b = ["# Log", "- one", "- three", "- four", "- five"]
    assert fleet.union_lines(a, b) == ["# Log", "- one", "- two", "- three", "- four", "- five"]

# ---- the conflict resolver ----
CF = "sync-conflict-20250301-102546-YUTVQYM"

def test_conflict_resolver_handles_each_kind_and_never_deletes(tmp_path):
    q = tmp_path / "Queue"; old = time.time() - 3600
    put(q / "discord/state.json", {"threads": {"a": "pc"}, "mode": "pc"}); os.utime(q / "discord/state.json", (old, old))
    put(q / f"discord/state.{CF}.json", {"threads": {"b": "mac"}, "mode": "mac"}); os.utime(q / f"discord/state.{CF}.json", (old - 60, old - 60))
    put(q / "loop/state.json", {"last": {"K": 100}}); os.utime(q / "loop/state.json", (old, old))
    put(q / f"loop/state.{CF}.json", {"last": {"K": 50, "F": 7}}); os.utime(q / f"loop/state.{CF}.json", (old + 5, old + 5))
    put(q / f"loop/state.{ME}.json", {"last": {"K": 1}, "saved": 10}); put(q / f"loop/state.{ME}.{CF}.json", {"last": {"Z": 2}, "saved": 5})
    put(q / "loop/state.OTHER-HOST.json", {"saved": 10}); put(q / f"loop/state.OTHER-HOST.{CF}.json", {"saved": 5})
    (q / "loop/K").mkdir(parents=True); (q / "loop/K/log.md").write_text("# K\n- a\n- c\n"); (q / f"loop/K/log.{CF}.md").write_text("# K\n- a\n- b\n- c\n")
    (q / "discord/janitor-issues.jsonl").write_text('{"i": 1}\n'); (q / f"discord/janitor-issues.{CF}.jsonl").write_text('{"i": 2}\n{"i": 1}\n')
    (q / "discord/janitor.out").write_text("x"); (q / f"discord/janitor.{CF}.out").write_text("y")
    snap = q / "attachments/20250301T164950Z-19b34d9d/before/switchboard"; snap.mkdir(parents=True); (snap / f"fleet.{CF}.py").write_text("old")
    (q / f"discord/zero.{CF}.json").write_bytes(b"\0" * 64); put(q / "discord/zero.json", {})
    (q / f"tmp/state.{CF}.json").parent.mkdir(parents=True); (q / f"tmp/state.{CF}.json").write_text("{}")

    done, left = fleet.resolve_conflicts(q, dry=True)
    assert len(list(q.rglob("*sync-conflict*"))) == 10, "a dry run changes nothing"
    done, left = fleet.resolve_conflicts(q)
    assert read(q / "discord/state.json") == {"threads": {"a": "pc", "b": "mac"}, "mode": "pc"}, "legacy JSON: merged into the original name, the newer file wins scalars"
    assert read(q / "loop/state.json")["last"] == {"K": 100, "F": 7}, "loop timestamps take the max"
    own = read(q / f"loop/state.{ME}.json"); assert own["last"] == {"K": 1, "Z": 2} and own["saved"] > 10, "this device's own copy: merged into it, with a new saved time"
    assert (q / "loop/K/log.md").read_text().splitlines() == ["# K", "- a", "- b", "- c"]
    assert [json.loads(l)["i"] for l in (q / "discord/janitor-issues.jsonl").read_text().splitlines()] == [2, 1], "each line where it sat"
    assert (q / "discord/janitor.out").read_text() == "x"
    arch = q / "archive/sync-conflicts"
    assert (arch / "attachments/20250301T164950Z-19b34d9d/before/switchboard" / f"fleet.{CF}.py").read_text() == "old", "snapshot copies archived, not deleted"
    assert (arch / f"discord/state.{CF}.json").exists() and (arch / f"discord/zero.{CF}.json").exists()
    rest = sorted(p.relative_to(q).as_posix() for p in q.rglob("*sync-conflict*") if p.is_file() and arch not in p.parents)
    assert rest == [f"discord/janitor.{CF}.out", f"loop/state.OTHER-HOST.{CF}.json", f"tmp/state.{CF}.json"], rest
    assert len(left) == 3 and any("OTHER-HOST's own file" in x for x in left) and any("compare it by hand" in x for x in left) and any("no original" in x for x in left), left
    assert len(done) == 7, done
    assert fleet.resolve_conflicts(q) == ([], left), "a second pass finds only what it left"

def test_conflict_resolver_waits_while_an_old_writer_still_writes_the_legacy_file(tmp_path):
    q = tmp_path / "Queue"; put(q / "discord/state.json", {"a": 1}); put(q / f"discord/state.{CF}.json", {"b": 2})
    done, left = fleet.resolve_conflicts(q)
    assert not done and "still being written" in left[0] and (q / f"discord/state.{CF}.json").exists()
    assert fleet.resolve_conflicts(q, now=time.time() + 700)[0], "a later pass merges it"
    assert read(q / "discord/state.json") == {"a": 1, "b": 2}

def test_conflict_resolver_recovered_entries_reach_a_peer_that_read_the_old_revision(tmp_path):
    """The merged own copy must get a new saved time: with its old one a peer that had read that revision would skip it."""
    q = tmp_path / "Queue"; d = q / "discord"; t = time.time()
    put(d / f"janitor.{ME}.json", {"items": {"a": 1, "x": 1}, "saved": 10})
    put(d / f"janitor.{ME}.{CF}.json", {"items": {"z": 2, "x": 1}, "saved": 5, "stamps": {"items\tz": [5, True]}})
    put(d / "janitor.OTHER-HOST.json", {"items": {"a": 1}, "saved": 20, "merged_from": {ME: 10}, "stamps": {"items\tx": [20, False]}})
    done, left = fleet.resolve_conflicts(q); assert len(done) == 1 and not left, (done, left)
    own = read(d / f"janitor.{ME}.json"); assert own["saved"] >= t and own["stamps"] == {"items\tz": [5, True]}, ("a new saved time, the stamps kept", own)
    assert fleet.load_merged(d / "janitor.json")["items"] == {"a": 1, "z": 2}, "the peer sees the recovered entry; what it deleted stays deleted"

def test_conflict_resolver_leaves_another_hosts_log_alone(tmp_path):
    """The cleanup must never rewrite another device's own log (janitor-log.<other host>.jsonl)."""
    q = tmp_path / "Queue"; d = q / "discord"; put(d / "lease-OTHER-HOST.json", {"host": "OTHER-HOST", "at": 1}); put(q / "devices/Third-Host.json", {"host": "Third-Host"})
    for stem, ext in (("janitor-log.other-host", ".jsonl"), ("notes.Third-Host", ".md"), (f"janitor-log.{ME}", ".jsonl"), ("log.v2", ".md")):
        (d / f"{stem}{ext}").write_text("a\n"); (d / f"{stem}.{CF}{ext}").write_text("b\n")
    done, left = fleet.resolve_conflicts(q)
    assert (d / "janitor-log.other-host.jsonl").read_text() == "a\n" and (d / "notes.Third-Host.md").read_text() == "a\n", "another known host's file is not rewritten (any case)"
    assert (d / f"janitor-log.other-host.{CF}.jsonl").exists() and (d / f"notes.Third-Host.{CF}.md").exists()
    assert sorted(x.split(" (")[1] for x in left) == ["Third-Host's own file: that device merges it)", "other-host's own file: that device merges it)"], left
    assert (d / f"janitor-log.{ME}.jsonl").read_text() == "a\nb\n" and (d / "log.v2.md").read_text() == "a\nb\n" and len(done) == 2, "its own log and a name that is no host still merge"

# ---- the writers moved to the helper ----
@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_ROOT", str(tmp_path)); (tmp_path / "Queue/loop").mkdir(parents=True); return tmp_path

def test_merge_improve_loop_state_is_per_host_and_read_merged(root):
    gl = load("improve_loop_merge", "improve-loop.py"); q = root / "Queue/loop"
    put(q / "state.json", {"last": {"Kepler": 5}, "retro": 1})
    put(q / "state.OTHER-HOST.json", {"last": {"Kepler": 50, "Fish": 9}, "due_checked": 40, "saved": 1})
    st = gl.load("state.json", {}); assert st["last"] == {"Kepler": 50, "Fish": 9} and st["retro"] == 1
    st["last"]["Birch"] = 77; st["due_checked"] = 45; gl.save("state.json", st)
    assert read(q / "state.json") == {"last": {"Kepler": 5}, "retro": 1}, "the legacy file is left as it was"
    put(q / "state.OTHER-HOST.json", {"last": {"Kepler": 60}, "due_checked": 20, "saved": time.time() + 100})  # it saved again without reading ours
    m = gl.load("state.json", {}); assert m["due_checked"] == 45 and m["last"] == {"Kepler": 60, "Fish": 9, "Birch": 77}, m
    assert gl.load("config.json", {"x": 1}) == {"x": 1}, "other loop files are untouched"

def test_merge_open_ask_reads_every_bridge_copy(root):
    gl = load("improve_loop_ask", "improve-loop.py"); d = root / "Queue/discord"
    put(d / "state.OTHER-HOST.json", {"threads": {"T": {"title": "Kepler: improvement cycle (ask)"}}, "q": {"m": {"thread": "T"}}, "saved": 1})
    assert gl.open_ask("Kepler") and not gl.open_ask("Birch")

def test_merge_loop_maintain_saves_the_config_on_the_first_host_only(root, monkeypatch):
    """Only the first host's loop may rewrite Queue/loop/config.json."""
    gl = load("improve_loop_maintain", "improve-loop.py"); f = root / "Queue/loop/config.json"; said = []
    monkeypatch.setattr(gl, "activity", lambda: {"kepler": {"name": "Kepler", "recent": "pool", "last": 5, "week": 9, "kind": "repo", "path": "", "host": ME}})
    put(f, {"hosts": ["OTHER-HOST", ME], "repos": {"Kepler": {"mode": "ask"}}}); before = f.read_bytes()
    gl.maintain(say=said.append); assert f.read_bytes() == before and len(said) == 1 and "only OTHER-HOST saves" in said[0], said
    put(f, {"hosts": [ME.upper(), "OTHER-HOST"], "repos": {"Kepler": {"mode": "ask"}}})
    gl.maintain(say=said.append); assert read(f)["repos"]["Kepler"]["recent"] == "pool" and len(said) == 1, "the first host saves the edit"
