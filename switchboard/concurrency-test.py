#!/usr/bin/env python3
"""Offline tests for the concurrency rules in mail-runner.py and repo-handoff.py.

  python3 concurrency-test.py          run everything
  python3 concurrency-test.py race     run tests whose name contains "race"

Uses the harness in discord-bridge-test.py (temp dev root, fake Discord, real agent-mail), so nothing live is touched.
Kept in its own file because several builds edit discord-bridge-test.py at the same time."""
import importlib.util, json, os, socket, subprocess, sys, time, traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("bridge_harness", HERE / "discord-bridge-test.py")
H = importlib.util.module_from_spec(spec); sys.modules["bridge_harness"] = H; spec.loader.exec_module(H)
ME = socket.gethostname().split(".")[0]

def runner(limit=2):
    r = H.make_runner(limit); return r, H.runner_mod
def from_owner(subject, sender="owner", **k):
    return H.agent_mod.send(sender, "builder", subject, f"Do it\n\nAREA: p{next(H._area)}", needs_reply=True, **k)
def local_claims(r): return sorted(p.name for p in r.claims.glob("*.claim"))
def shared_claims(): return sorted(p.name for p in (H.tmp / "Queue/mail/claims").glob("*.json"))

# ---- finding 1: human mail keeps its extra slots, but claims its thread, and a device has a real ceiling ----
def test_human_mail_never_waits_for_the_run_in_its_thread():
    r, M = runner(4); runs = H.FakeRuns(); M.run_claude = runs
    first = H.mail_to_builder("First"); second = from_owner("The owner adds something", reply_to=first["id"])
    r.one_pass(); assert H.until(lambda: len(runs.seen) == 2), "what the owner types starts beside the run already in its thread"
    assert set(runs.seen) == {first["id"], second["id"]} and len(r.running) == 2, runs.seen
    H.finish(r, runs)

def test_human_extra_slots_are_per_device():
    r, M = runner(1); other = H.make_runner(1); runs = H.FakeRuns(); M.run_claude = runs  # two runner processes on one device share the claims folder
    agents = [H.mail_to_builder(f"A{i}")["id"] for i in range(3)]; people = [from_owner(f"R{i}")["id"] for i in range(10)]
    r.one_pass(); other.one_pass(); assert H.until(lambda: len(runs.seen) == 2 + M.HUMAN_EXTRA)
    r.one_pass(); other.one_pass(); time.sleep(0.1)
    assert runs.peak == 2 + M.HUMAN_EXTRA == 8, f"one slot per process plus {M.HUMAN_EXTRA} for the whole device, not per process: {runs.peak}"
    assert sorted(runs.seen) == sorted(agents[:2] + people[:M.HUMAN_EXTRA]), "agent mail never uses an extra slot"
    assert len([n for n in local_claims(r) if n.startswith("human-slot-")]) == M.HUMAN_EXTRA
    runs.go.set(); assert H.until(lambda: not r.running and not other.running); assert not local_claims(r), "slots are released with the run"

def test_device_ceiling():
    r, M = runner(4); other = H.make_runner(4); runs = H.FakeRuns(); M.run_claude = runs; keep = M.DEVICE_MAX; M.DEVICE_MAX = 5
    try:
        for i in range(9): H.mail_to_builder(f"T{i}")
        r.one_pass(); other.one_pass(); assert H.until(lambda: len(runs.seen) == 5); r.one_pass(); other.one_pass(); time.sleep(0.1)
        assert runs.peak == 5 and len(M.live_claims(r.claims, 60)) == 5, "no more than DEVICE_MAX runs on a device, whatever each process allows"
        runs.go.set(); assert H.until(lambda: not r.running and not other.running)
    finally: M.DEVICE_MAX = keep
    assert M.DEVICE_MAX >= 2 * M.DEFAULT_MAX_CONCURRENT + M.HUMAN_EXTRA, "the default setup fits under the ceiling"

def test_programs_sending_as_a_person_get_no_extra_slot():
    r, M = runner(1); runs = H.FakeRuns(); M.run_claude = runs
    busy = H.mail_to_builder("Busy"); hook = from_owner("Session finished", sender="sessions"); loop = from_owner("[loop] Improvement cycle: Kepler"); real = from_owner("Real")
    agents = H.agent_mod.load_agents()
    assert [M.is_person(m, agents) for m in (busy, hook, loop, real)] == [False, False, False, True]
    assert not M.is_person({"from": "nobody"}, agents) and not M.is_person({}, agents)
    r.one_pass(); assert H.until(lambda: len(runs.seen) == 2); r.one_pass(); time.sleep(0.05)
    assert runs.seen == [busy["id"], real["id"]], "session-hook and loop mail wait like agent mail"
    H.finish(r, runs)

# ---- finding 3: claims are released on every failure between taking them and starting the run ----
def test_claims_released_when_start_fails():
    r, M = runner(2); runs = H.FakeRuns(go=True); M.run_claude = runs; m = H.mail_to_builder("Task", area="bridge")
    def boom(*a, **k): raise OSError("disk full")
    for where in ("record_run", "event", "thread"):
        keep = (M.record_run, M.fleet.event, M.threading.Thread)
        class NoThread:
            def __init__(self, *a, **k): pass
            def start(self): raise RuntimeError("can't start new thread")
        if where == "record_run": M.record_run = boom
        elif where == "event": M.fleet.event = boom
        else: M.threading.Thread = NoThread
        try:
            try: r.one_pass(); raise AssertionError("the failure should surface")
            except (OSError, RuntimeError): pass
        finally: M.record_run, M.fleet.event, M.threading.Thread = keep
        assert not local_claims(r) and not shared_claims() and not r.running, f"{where}: nothing left held: {local_claims(r)} {shared_claims()}"
        assert [x["id"] for x in H.inbox("builder")] == [m["id"]], "the message is untouched"
    r.one_pass(); assert H.until(lambda: not r.running and not H.inbox("builder")), "and the very next pass runs it"
    assert runs.seen == [m["id"]]

# ---- finding 5: max_concurrent is validated, clamped, and never crashes ----
def test_max_concurrent_is_clamped_and_never_raises():
    r, M = runner(None); f = H.tmp / "Queue/mail/runner.json"; top, dflt = M.MAX_CONCURRENT, M.DEFAULT_MAX_CONCURRENT
    for text, want in (('{"max_concurrent": 99999}', top), ('{"max_concurrent": Infinity}', dflt), ('{"max_concurrent": 1e999}', dflt), ('{"max_concurrent": -Infinity}', dflt),
                       ('{"max_concurrent": NaN}', dflt), ('{"max_concurrent": -3}', dflt), ('{"max_concurrent": true}', dflt), ('{"max_concurrent": "abc"}', dflt),
                       ('{"max_concurrent": 2.5}', dflt), ('{"max_concurrent": [1]}', dflt), ('{"max_concurrent": null}', dflt), ('[1, 2]', dflt), ('{}', dflt), ("", dflt),
                       ('{"max_concurrent": 0}', top), ('{"max_concurrent": 3.0}', 3), ('{"max_concurrent": "7"}', 7), ('{"max_concurrent": 16}', 16), ('{"max_concurrent": 1}', 1)):
        f.write_text(text, encoding="utf-8"); assert r.limit() == want, (text, r.limit(), want)
    for flag, want in ((99999, top), (0, top), (-1, dflt), (float("inf"), dflt), (5, 5)):
        r.a.max_concurrent = flag; assert r.limit() == want, (flag, r.limit())
    assert 1 <= M.DEFAULT_MAX_CONCURRENT <= M.MAX_CONCURRENT <= M.DEVICE_MAX < 100, "sane, bounded numbers"
    r.a.max_concurrent = None; f.write_text('{"max_concurrent": 1e999}', encoding="utf-8"); r.one_pass()  # a pass with a bad file runs

# ---- the 23:53 and 23:54 edits of 2026-11-09 that had no tests ----
def test_orchestrator_prompt_lists_other_unfinished_work():
    r, M = runner(); m = H.mail_to_builder("Mine", area="kepler"); agents = H.agent_mod.load_agents(); keep = (M.fleet.active_work, M.fleet.work_text)
    seen = {}
    row = lambda t, area: {"thread": t, "title": t, "items": [{"agent": "builder", "state": "running", "area": area, "subject": t}]}
    def active(skip=None): seen["skip"] = skip; return [row("Kepler pool fix", "kepler"), row("Birch bait", "birch")]
    M.fleet.active_work = active
    try:
        o = M.build_prompt(H.agent_mod, "orchestrator", {}, m, agents, "TEST-PC"); bld = M.build_prompt(H.agent_mod, "builder", {}, m, agents, "TEST-PC")
        assert "## Other unfinished work in this repo or area" in o and "Kepler pool fix" in o and seen["skip"] == m["thread"], "its own thread is left out"
        assert "Birch bait" not in o, "only work in the same repo or area (the full list was prompt bloat)"
        assert "Other unfinished work" not in bld, "only the orchestrator gets the list"
        M.fleet.active_work = boom = lambda skip=None: (_ for _ in ()).throw(OSError("x"))
        assert "Other unfinished work" not in M.build_prompt(H.agent_mod, "orchestrator", {}, m, agents, "TEST-PC"), "a failure there never breaks the prompt"
    finally: M.fleet.active_work, M.fleet.work_text = keep

def test_writer_with_no_area_takes_the_workspace_and_readers_take_nothing():
    r, M = runner(); plain = {"body": "no area line"}
    assert r.repo_key(plain, r.root, "builder") == "workspace" and r.repo_key(plain, r.root, "orchestrator") == "" and r.repo_key(plain, r.root, "reviewer") == ""
    assert r.repo_key({"body": "x\n\nAREA: discord-bridge"}, r.root, "orchestrator") == "switchboard", "a named area always counts (switchboard areas share one key)"
    (H.tmp / "Kepler").mkdir(); assert r.repo_key(plain, H.tmp / "Kepler", "builder") == "kepler" and r.bound(H.tmp / "Kepler") and not r.bound(r.root)

def test_queued_line_shows_position_and_what_is_running():
    b, t0 = H.b, time.time()
    for i in range(3): H.mail_to_builder(f"Q{i}")
    box = [m["id"] for m in H.inbox("builder")]
    t = {"queued": t0, "to": "builder", "last_mail": box[2]}
    assert "2 ahead of it" in b.queue_line(t) and f"<t:{int(t0)}:R>" in b.queue_line(t)
    b.st["threads"]["x"] = {"runs": {box[0]: {"t0": H.CLOCK.time(), "agent": "builder"}}}
    line = b.queue_line(t); assert "1 ahead of it" in line and "`builder` is running 1 now" in line, line
    assert "next up" in b.queue_line({**t, "last_mail": box[1]})
    assert "picked up, starting" in b.queue_line({**t, "last_mail": "not-in-the-inbox"})
    del b.st["threads"]["x"]

# ---- the repo lock ----
def env(run, claims): return {"AGENT_MAIL_PARENT": run, "AGENT_MAIL_CLAIMS": str(claims)}
def lock_file(repo="Kepler"): return H.tmp / "Queue/locks" / f"{repo}.json"
def stamp(offset): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))
def put_lock(run="", since=0, until=None, repo="Kepler"):
    lock_file(repo).parent.mkdir(parents=True, exist_ok=True)
    lock_file(repo).write_text(json.dumps({"repo": repo, "owner": ME, "since": stamp(since), "note": "", "run": run, **({"until": stamp(until)} if until is not None else {})}), encoding="utf-8")

def bind_repo(repo="Kepler"):
    """Give the temp repo a folder."""
    (H.tmp / repo).mkdir(parents=True, exist_ok=True)

def test_repo_claim_race_has_one_winner():
    claims = H.tmp / "claims"; claims.mkdir(); n, rounds = 6, 12
    for i in range(n): (claims / f"run-{i}.claim").write_text(f"1 t {time.time() + 3600:.0f}\n", encoding="utf-8")
    base = {**os.environ, "SWITCHBOARD_ROOT": str(H.tmp), "SYNCTHING_URL": "http://127.0.0.1:1", "SYNCTHING_API_KEY": "x"}
    for rnd in range(rounds):
        ps = [subprocess.Popen([sys.executable, "-X", "utf8", str(HERE / "repo-handoff.py"), "claim", "Kepler"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env={**base, **env(f"run-{i}", claims)}) for i in range(n)]
        outs = [(p.communicate(timeout=120), p.returncode) for p in ps]
        codes = sorted(rc for _, rc in outs)
        assert codes == [0] + [6] * (n - 1), f"round {rnd}: exactly one run is told 'claimed': {codes} {[o[0][1][-200:] for o in outs if o[1] not in (0, 6)]}"
        winner = next(i for i, (_, rc) in enumerate(outs) if rc == 0)
        assert json.loads(lock_file().read_text(encoding="utf-8"))["run"] == f"run-{winner}", "and the lock names that run"
        assert not [p.name for p in lock_file().parent.iterdir() if p.name.endswith(".tmp")], "no temp files left for Syncthing to carry"
        lock_file().unlink()

def test_lock_without_run_id_holds_until_its_expiry():
    claims = H.tmp / "claims"; claims.mkdir(); (claims / "run-b.claim").write_text("x", encoding="utf-8"); B = env("run-b", claims); none = env("", claims); bind_repo()
    put_lock(run="", since=-60)
    r = H.handoff("claim", "Kepler", env=B); assert r.returncode == 6 and "no run id" in r.stdout, r.stdout + r.stderr
    assert H.handoff("release", "Kepler", env=B).returncode == 6 and lock_file().is_file(), "a run cannot release it either"
    put_lock(run="", since=-60, until=3600); assert H.handoff("claim", "Kepler", env=B).returncode == 6, "its own expiry is honoured"
    put_lock(run="", since=-13 * 3600); assert H.handoff("claim", "Kepler", env=B).returncode == 0, "an old-format lock expires 12 hours after it was taken"
    put_lock(run="", since=-7200, until=-60); assert H.handoff("claim", "Kepler", env=B).returncode == 0, "and one past its 'until' is free"
    d = json.loads(lock_file().read_text(encoding="utf-8")); assert d["run"] == "run-b" and d["until"] > d["since"], "new locks carry their expiry"
    put_lock(run="", since=-60); assert H.handoff("claim", "Kepler", env=none).returncode == 0, "the person or queue card that holds it can claim again"
    assert H.handoff("release", "Kepler", env=none).returncode == 0 and not lock_file().exists()
    put_lock(run="", since=-60); lock_file().write_text(lock_file().read_text(encoding="utf-8").replace(stamp(-60)[:10], "garbage"), encoding="utf-8")
    assert H.handoff("claim", "Kepler", env=B).returncode == 6, "an unreadable date counts as held, never as free"

def test_expired_run_claim_file_frees_the_lock():
    claims = H.tmp / "claims"; claims.mkdir(); B = env("run-b", claims); (claims / "run-b.claim").write_text("x", encoding="utf-8"); bind_repo()
    put_lock(run="run-a"); (claims / "run-a.claim").write_text(f"1 t {time.time() + 600:.0f}\n", encoding="utf-8")
    assert H.handoff("claim", "Kepler", env=B).returncode == 6, "a run inside its deadline holds the lock"
    (claims / "run-a.claim").write_text(f"1 t {time.time() - 5:.0f}\n", encoding="utf-8")
    assert H.handoff("claim", "Kepler", env=B).returncode == 0, "a leftover claim file past its deadline no longer holds it"
    put_lock(run="run-a"); (claims / "run-a.claim").write_text("x", encoding="utf-8"); old = time.time() - 26 * 60; os.utime(claims / "run-a.claim", (old, old))
    assert H.handoff("claim", "Kepler", env=B).returncode == 0, "a claim file with no deadline expires 25 minutes after it was written"
    put_lock(run="run-a"); (claims / "run-a.claim").unlink(); assert H.handoff("release", "Kepler", env=B).returncode == 0, "and a finished run's lock can be released"

def test_remote_claim_of_a_finished_run_is_free():
    r, M = runner(2); X = "20261112T000000Z-aaaaaaaa"; cdir = H.tmp / "Queue/mail/claims"; cdir.mkdir(parents=True, exist_ok=True)
    f = cdir / "thread-T1.json"; f.write_text(json.dumps({"host": "OtherMac", "at": time.time(), "mid": X}), encoding="utf-8")
    assert r.take("m1", "T1", None, 3600) is None, "a live remote claim holds"
    ev = H.tmp / "Queue/events"; ev.mkdir(parents=True, exist_ok=True)
    (ev / "OtherMac-202611.jsonl").write_text(json.dumps({"kind": "done", "ref": X, "host": "OtherMac", "ts": time.time()}) + "\n", encoding="utf-8")
    held = r.take("m1", "T1", None, 3600); assert held is not None, "the other host's done event frees it"; r.release(held)
    assert f.exists(), "the claim file is left alone"
    f.write_text(json.dumps({"host": "OtherMac", "at": time.time()}), encoding="utf-8")
    assert r.take("m1", "T1", None, 3600) is None, "a claim without mid holds as before"


def test_lean_launch_per_agent():
    M = H.load_module("mail_runner_lean", "mail-runner.py"); seen = {}  # a fresh copy: earlier tests replace run_claude on the shared one
    class FakePopen:
        pid = 0
        def __init__(self, cmd, **kw): seen["cmd"], seen["env"] = list(cmd), kw["env"]
        def wait(self, timeout=None): return 0
    keep = M.subprocess.Popen; M.subprocess.Popen = FakePopen
    try:
        for with_mcp in (True, False):
            M.run_claude("claude", "hi", "auto", H.tmp, 5, "orchestrator", False, with_mcp)
            cmd, env = seen["cmd"], seen["env"]
            assert "--disable-slash-commands" in cmd, "no skill listing"
            tools = cmd[cmd.index("--tools") + 1].split(",")
            assert set(tools) == {"Bash", "PowerShell", "Read", "Edit", "Write", "Glob", "Grep"}, "file and shell tools stay, nothing else"
            assert env["ENABLE_TOOL_SEARCH"] == "false" and env["AGENT_MAIL_AGENT"] == "orchestrator", "mail tools load up front"
            if with_mcp: assert "mcp__agent_mail" in cmd and "--strict-mcp-config" in cmd, "agent_mail stays"
        repo = H.tmp / "LeanRepo"; repo.mkdir(exist_ok=True)
        (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"repo_server": {"command": "x"}}}), encoding="utf-8")
        M.run_claude("claude", "hi", "auto", repo, 5, "orchestrator", False, True)
        cmd, env = seen["cmd"], seen["env"]
        assert set(cmd[cmd.index("--tools") + 1].split(",")) == {"Bash", "PowerShell", "Read", "Edit", "Write", "Glob", "Grep"}, "a repo folder changes nothing for the orchestrator"
        assert "mcp__repo_server" not in cmd and env["ENABLE_TOOL_SEARCH"] == "false", "a repo's own MCP servers are not loaded"
        base = {"Bash", "PowerShell", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "WebSearch", "TaskStop", "Monitor"}
        for agent in ("builder", "reviewer"):
            want = base | ({"Agent"} if agent == "reviewer" else set())
            M.run_claude("claude", "hi", "auto", H.tmp, 5, agent, False, True)
            cmd, env = seen["cmd"], seen["env"]
            assert "--disable-slash-commands" in cmd, f"{agent} outside a repo: no skill listing"
            assert set(cmd[cmd.index("--tools") + 1].split(",")) == want, f"{agent} tool set"
            assert env["ENABLE_TOOL_SEARCH"] == "false" and "mcp__agent_mail" in cmd, "mail tools load up front"
            assert cmd[:6] == ["claude", "-p", "hi", "--permission-mode", "auto", "--output-format"], "same base command"
            M.run_claude("claude", "hi", "auto", repo, 5, agent, False, True)
            cmd, env = seen["cmd"], seen["env"]
            tools = cmd[cmd.index("--tools") + 1].split(",")
            assert "--disable-slash-commands" not in cmd, f"{agent} in a repo keeps the skill listing"
            assert set(tools) == want | {"Skill"}, f"{agent} in a repo keeps Skill"
            assert "mcp__repo_server" not in cmd and env["ENABLE_TOOL_SEARCH"] == "false", "only the mail tools, loaded up front"
        assert M.lean_launch("pc") == ([], {}), "other agents unchanged"
        M.run_claude("claude", "hi", "auto", H.tmp, 5, "pc", False, True)
        assert "--tools" not in seen["cmd"] and "--disable-slash-commands" not in seen["cmd"], "pc launch must not change"
    finally: M.subprocess.Popen = keep

def main():
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and want in n]
    failed = 0
    for name, fn in tests:
        H.setup()
        try: fn(); print("PASS", name)
        except BaseException:
            failed += 1; print("FAIL", name); traceback.print_exc()
        finally: H.teardown()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)

if __name__ == "__main__": main()
