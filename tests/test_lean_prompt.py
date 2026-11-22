"""Lean agent runs: no user settings or memory hooks, memory by reference, a thread
brief instead of the history, continuing the thread's last orchestrator session, and compact prompt sections."""
import importlib.util, json, os, time, types
from pathlib import Path

import pytest

from _load import load, TK
import fleet

mr = load("mail_runner_lean_prompt", "mail-runner.py")
ME = mr.hostname()
SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "dev"; (root / "Kepler").mkdir(parents=True)
    monkeypatch.setenv("SWITCHBOARD_ROOT", str(root)); monkeypatch.delenv("AGENT_MAIL_PARENT", raising=False)
    mail = mr.load_mail()
    for name, kind in (("builder", "claude"), ("orchestrator", "claude"), ("owner", "human")):
        mail.register(name, ME, kind, auto=kind == "claude")
    monkeypatch.setattr(mr, "find_claude", lambda: "/x/claude"); monkeypatch.setattr(mr, "choose", lambda *a, **k: None)
    monkeypatch.setattr(fleet, "codex_exe", lambda: None); monkeypatch.setattr(mr, "memory_text", lambda cwd, L=None: "")
    a = types.SimpleNamespace(only="", once=False, dry_run=False, state_file=str(tmp_path / "local" / "state.json"), max_per_hour=0, max_per_day=0,
                              timeout_minutes=20, max_concurrent=None, max_hops=0, prompt_via="auto", permission_mode="auto", mcp=False, always_reply=False, no_loop=False)
    return types.SimpleNamespace(r=mr.Runner(a, mail), mail=mail, q=root / "Queue", root=root, tmp=tmp_path)


# ---- 1. lean launch ----
USER = {"env": {"A": "1"}, "autoMode": {"allow": ["python is safe"]}, "skipAutoPermissionPrompt": True, "permissions": {"allow": ["Bash(ls *)"]},
        "outputStyle": "Concise", "effortLevel": "xhigh", "autoMemoryEnabled": False,
        "enabledPlugins": {"ponytail@ponytail": True, "lint-pack@x": True, "other@x": True},
        "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "python ~/.switchboard/tools/claude-memory-loader.py --chunk 0"}]}],
                  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python claude-memory-loader.py"}]}, {"hooks": [{"type": "command", "command": "python session-hook.py"}]}],
                  "PreToolUse": [{"matcher": "Read|Edit", "hooks": [{"type": "command", "command": "python claude-memory-loader.py"}]},
                                 {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "python ~/.switchboard/tools/source-guard-shim.py"}]}],
                  "PermissionRequest": [{"hooks": [{"type": "command", "command": f"python {mr.HERE / 'permission-hook.py'}"}]},
                                        {"hooks": [{"type": "command", "command": "python /some/other/tool/permission-hook.py"}]}],
                  "Stop": [{"hooks": [{"type": "command", "command": "python session-hook.py"}]}]}}

def launch(env, monkeypatch, user, agent="builder", folder="Kepler", pony="lite", **kw):
    home = env.tmp / "home"; (home / ".claude").mkdir(parents=True, exist_ok=True)
    if user is not None: (home / ".claude/settings.json").write_text(json.dumps(user), encoding="utf-8")
    monkeypatch.setattr(mr.Path, "home", lambda: home)
    seen = {}
    class P:
        pid = 1
        def __init__(self, cmd, **k): seen["cmd"] = list(cmd)
        def wait(self, timeout=None): return 0
    monkeypatch.setattr(mr.subprocess, "Popen", P)
    mr.run_claude("/x/claude", "hi", "auto", env.root / folder, 5, agent, False, True, parent="m1", ponytail=pony, **kw)
    return seen["cmd"]

def test_agent_runs_skip_user_settings_and_keep_only_this_checkouts_permission_hook(env, monkeypatch):
    cmd = launch(env, monkeypatch, USER)
    assert cmd[cmd.index("--setting-sources") + 1] == "project,local" and cmd.count("--settings") == 1
    s = json.loads(cmd[cmd.index("--settings") + 1])
    assert s["env"] == {"A": "1"} and s["autoMode"] == USER["autoMode"] and s["skipAutoPermissionPrompt"] is True and s["permissions"] == USER["permissions"]
    assert "outputStyle" not in s and "effortLevel" not in s and s["autoMemoryEnabled"] is False
    assert set(s["hooks"]) == {"PermissionRequest"} and len(s["hooks"]["PermissionRequest"]) == 1, "only the permission hook, and only this checkout's"
    assert "/some/other/tool/" not in json.dumps(s) and "source-guard" not in json.dumps(s), "a same-named hook another tool installed is not run"
    assert "memory-loader" not in json.dumps(s) and "session-hook" not in json.dumps(s), "no memory or session hooks"
    assert s["enabledPlugins"] == {"ponytail@ponytail": True, "lint-pack@x": False, "other@x": False}, "all off but the runner's opt-ins"
    assert "--session-id" in cmd and "--resume" not in cmd

def test_unreadable_user_settings_keep_todays_launch(env, monkeypatch):
    cmd = launch(env, monkeypatch, None)
    assert "--setting-sources" not in cmd and "--settings" not in cmd

def test_a_resumed_run_keeps_its_session_id(env, monkeypatch):
    cmd = launch(env, monkeypatch, USER, agent="orchestrator", folder="", pony="off", resume=SID)
    assert cmd[cmd.index("--resume") + 1] == SID and "--session-id" not in cmd and mr.SESSIONS.pop("m1") == (SID, str(env.root / ""))


# ---- 2. memory by reference ----
NOTES = ["Local/Device.md", "MEMORY.md", "Projects/MEMORY.md", "Projects/Kepler/MEMORY.md"]

@pytest.mark.parametrize("short", [True, False])
def test_memory_lists_the_notes_for_a_repo_folder(env, monkeypatch, tmp_path, short):
    import tempfile
    top = Path(tempfile.mkdtemp(dir="/tmp" if short and os.path.isdir("/tmp") else None)).resolve() if short else tmp_path / ("long" * 20)
    vault, base, root = top / "v", top / "ai", env.root.resolve()
    for rel in NOTES:
        (vault / rel).parent.mkdir(parents=True, exist_ok=True); (vault / rel).write_text("SECRET NOTE TEXT", encoding="utf-8")
    base.mkdir(); (base / "MEMORY.md").write_text("SECRET NOTE TEXT", encoding="utf-8")
    (base / "registry.json").write_text(json.dumps({"vault": str(vault), "device_note": "Local/Device.md", "scopes": [
        {"root": str(root), "canonical": str(vault / "Projects/MEMORY.md")}, {"root": str(root / "Kepler"), "canonical": str(vault / "Projects/Kepler/MEMORY.md")}]}))
    monkeypatch.setenv("AI_MEMORY_BASE", str(base))
    spec = importlib.util.spec_from_file_location("loader_t", TK / "claude-memory-loader.py"); L = importlib.util.module_from_spec(spec); spec.loader.exec_module(L)
    text = load("mail_runner_mem", "mail-runner.py").memory_text(root / "Kepler", L)
    rows = text.splitlines()
    assert rows[0] == "## Memory" and rows[1] == f"Vault: {vault.resolve()}" and len(text) <= 900, len(text)
    paths = [r[2:].split(": ")[0] for r in rows if r.startswith("- ")]
    assert paths == ([str(vault / n) for n in NOTES] if short else NOTES), paths
    assert "standing rules for work under Projects/Kepler" in text and "SECRET" not in text
    assert rows[-1] == "Read a note before work it governs; read MEMORY.md before writing to the vault."
    if short: import shutil; shutil.rmtree(top, ignore_errors=True)


# ---- 3. thread brief ----
def thread(env, first="R" * 900, task="T" * 1000):
    m1 = env.mail.send("owner", "orchestrator", "Kepler: big map", first)
    m2 = env.mail.send("orchestrator", "builder", "Build it", task, reply_to=m1["id"])
    m3 = env.mail.send("builder", "orchestrator", "Re: Build it", "Built.", reply_to=m2["id"])
    return m1, m3

def test_thread_brief_falls_back_to_the_request_and_the_plan(env):
    m1, m3 = thread(env)
    b = mr.thread_brief(env.mail, m3)
    assert f"The request (owner, {m1['created']}): " + "R" * 500 in b and "R" * 501 not in b
    assert "Latest builder task" in b and "T" * 800 in b and "T" * 801 not in b and "Built." not in b
    assert b.splitlines()[-1] == f"Full history: python {TK.resolve() / 'agent-mail.py'} thread {m1['id']}"
    p = mr.build_prompt(env.mail, "orchestrator", {}, m3, env.mail.load_agents(), ME)
    assert "## Thread so far" in p and "## Earlier messages" not in p
    assert f"Before ending, write {env.q / 'threads' / (m1['id'] + '.md')} (create it if missing, under 1,200 chars)" in p
    assert mr.thread_brief(env.mail, m1) == "", "a thread's first message has nothing before it"

def test_thread_brief_uses_the_orchestrators_file(env):
    m1, m3 = thread(env)
    f = env.q / "threads" / f"{m1['id']}.md"; f.parent.mkdir(parents=True); f.write_text("Goal: map. " + "x" * 2000, encoding="utf-8")
    b = mr.thread_brief(env.mail, m3)
    assert b.startswith("Goal: map.") and "The request" not in b and len(b.splitlines()[0]) <= 1506


# ---- 4. continuation ----
def rec(**k): return {"session": SID, "host": ME, "cwd": "/w", "ended_at": time.time() - 600, "context_tokens": 40000, **k}

def test_sessions_continue_with_no_time_or_size_gate():
    old = rec(ended_at=time.time() - 3 * 86400, context_tokens=500_000)
    assert mr.can_resume(old, "/w") and mr.can_resume(rec(host="OTHER-PC"), "/w"), "no time or size limit forces a new session"
    for bad in (rec(session=""), rec(busy="m0", busy_at=time.time() - 60), rec(fresh=True)):
        assert not mr.can_resume(bad, "/w"), bad
    assert not mr.can_resume(rec(), "/other"), "another folder: a fresh run"
    assert mr.can_resume(rec(busy="m0", busy_at=time.time() - 7200), "/w"), "a stale busy mark does not block"

def test_every_agent_run_auto_compacts(env, monkeypatch):
    for agent in ("builder", "orchestrator"):
        cmd = launch(env, monkeypatch, USER, agent=agent)
        assert cmd[cmd.index("--autocompact") + 1] == "auto", agent

def test_fresh_flag_from_the_command_or_owner_and_cleared_after(env, monkeypatch):
    r, mail, q = env.r, env.mail, env.q
    m1, m3 = thread(env)
    mr.write_record(mail, m1["id"], **rec(cwd=str(env.root)), builder={"session": "b1"})
    monkeypatch.setattr(mr.sys, "argv", ["mail-runner.py", "fresh", m1["id"]]); assert mr.main() == 0
    d = mr.thread_record(m1["id"]); assert d["fresh"] is True and "builder" not in d, "the builder starts over too"
    seen = []
    def run(exe, prompt, mode, cwd, *a, parent="", resume="", **k):
        seen.append(resume); mr.SESSIONS[parent] = ("fresh-sid", str(cwd)); return 0, json.dumps({"type": "result", "result": "ok"}), ""
    monkeypatch.setattr(mr, "run_claude", run)
    agents = mail.load_agents()
    r.execute(r.prepare("orchestrator", agents["orchestrator"], m3, agents, "/x/claude"))
    d = mr.thread_record(m1["id"]); assert seen == [""] and d["session"] == "fresh-sid" and "fresh" not in d, "one fresh run, then the flag is gone"
    m4 = mail.send("owner", "orchestrator", "Re: map", "This went wrong, start fresh please.", reply_to=m1["id"])
    job = r.prepare("orchestrator", agents["orchestrator"], m4, agents, "/x/claude")
    assert job["resume"] == "" and mr.thread_record(m1["id"]).get("fresh") is True and "## Message" in job["prompt"], "the owner's words set the flag"
    r.drop(job["held"], job["shared"])

def test_a_session_from_the_other_device_is_copied_in(env, monkeypatch):
    home = env.tmp / "home"; monkeypatch.setattr(mr.Path, "home", lambda: home)
    tid, cwd = "T9", env.root / "Kepler"
    r0 = rec(host="OTHER-PC", cwd="C:\\Dev\\Work\\Kepler", rel="Kepler")
    assert mr.can_resume(r0, cwd), "the same folder relative to each device's dev root"
    assert "has not reached this device" in mr.bring_session(r0, cwd, tid, "orchestrator")
    (env.q / "threads").mkdir(parents=True, exist_ok=True); (env.q / "threads" / "T9.orchestrator.jsonl").write_text('{"x": 1}\n')
    assert mr.bring_session(r0, cwd, tid, "orchestrator") == ""
    assert (home / ".claude/projects" / mr.re.sub(r"[^A-Za-z0-9]", "-", str(cwd)) / f"{SID}.jsonl").read_text() == '{"x": 1}\n'
    own = mr.session_file("s2", cwd); own.parent.mkdir(parents=True, exist_ok=True); own.write_text("mine")
    mr.share_session("s2", cwd, tid, "builder"); assert (env.q / "threads" / "T9.builder.jsonl").read_text() == "mine"

def test_the_session_host_takes_the_threads_next_orchestrator_run(env, monkeypatch):
    r, mail = env.r, env.mail
    m1, m3 = thread(env)
    mr.write_record(mail, m1["id"], **rec(host="OTHER-PC"))
    monkeypatch.setattr(fleet, "available", lambda h: True); monkeypatch.setattr(fleet, "resources", lambda h, *a: {})
    assert r.session_host("orchestrator", m3, [ME, "OTHER-PC"]) == "other-pc" and r.session_host("builder", m3, [ME, "OTHER-PC"]) == ""
    assert not r.mine("orchestrator", [ME, "OTHER-PC"], m3), "pinned to the device that holds the session"
    monkeypatch.setattr(fleet, "resources", lambda h, *a: {"pressure": True})
    assert r.session_host("orchestrator", m3, [ME, "OTHER-PC"]) == "", "a pressed host does not hold the thread hostage"

def test_builder_continues_on_the_same_model_and_starts_fresh_on_escalation(env, monkeypatch):
    r, mail, q = env.r, env.mail, env.q
    m1, m3 = thread(env)
    seen = []
    def run(exe, prompt, mode, cwd, *a, parent="", resume="", **k):
        seen.append((prompt, resume)); mr.SESSIONS[parent] = (resume or f"b-{len(seen)}", str(cwd)); return 0, json.dumps({"type": "result", "result": "Fixed."}), ""
    monkeypatch.setattr(mr, "run_claude", run)
    route = {"engine": "claude", "model": "claude-sonnet-5-5", "effort": "medium", "why": "x", "alt": None}
    monkeypatch.setattr(mr, "choose", lambda *a, **k: dict(route))
    agents = mail.load_agents()
    def task(body):
        m = mail.send("orchestrator", "builder", "Fix round", body, reply_to=m3["id"])
        r.execute(r.prepare("builder", agents["builder"], m, agents, "/x/claude")); return m
    first = task("Build the bridge.")
    assert seen[-1][1] == "" and mr.thread_record(m1["id"])["builder"]["model"] == "claude-sonnet-5-5"
    task("The bridge rail is missing; add it.")
    assert seen[-1][1] == "b-1" and seen[-1][0].startswith("Same work as your last run in this thread.") and "rail is missing" in seen[-1][0]
    (q / "attachments" / first["id"]).mkdir(parents=True, exist_ok=True); (q / "attachments" / first["id"] / "changes.diff").write_text("diff")
    mr.write_record(mail, m1["id"], builder={**mr.thread_record(m1["id"])["builder"], "mid": first["id"]})
    route["model"] = "claude-opus-5-5"
    task("Still wrong after two rounds: the rail floats.")
    prompt, resume = seen[-1]
    assert resume == "" and "## Thread so far" in prompt and str(q / "attachments" / first["id"] / "changes.diff") in prompt and "rail floats" in prompt, "an escalation starts fresh with the plan, the diff and what is wrong"

def test_orchestrator_continues_its_session_and_records_it(env, monkeypatch):
    r, mail, q = env.r, env.mail, env.q
    m1, m3 = thread(env)
    f = q / "threads" / f"{m1['id']}.json"; f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rec(cwd=str(env.root), work=["- thread old"])), encoding="utf-8")
    monkeypatch.setattr(mr, "other_work", lambda mail, msg: ["- thread old", "- thread new"])
    seen, logs = [], []
    monkeypatch.setattr(mr, "say", lambda s: logs.append(s))
    def run(exe, prompt, mode, cwd, *a, parent="", resume="", **k):
        seen.append((prompt, resume)); mr.SESSIONS[parent] = (resume or "fresh-sid", str(cwd))
        usage = {"input_tokens": 5, "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 2000}
        return 0, "\n".join([json.dumps({"type": "assistant", "message": {"usage": usage, "content": []}}), json.dumps({"type": "result", "result": "Reviewed."})]), ""
    monkeypatch.setattr(mr, "run_claude", run)
    agents = mail.load_agents()
    job = r.prepare("orchestrator", agents["orchestrator"], m3, agents, "/x/claude")
    assert job["resume"] == SID and json.loads(f.read_text())["busy"] == m3["id"]
    assert any(f"prompt={len(job['prompt'])} resume={SID[:8]}" in l for l in logs)
    r.execute(job)
    prompt, resume = seen[0]
    assert resume == SID and prompt.startswith("Same thread as your last run; your earlier context is above.")
    assert "- thread new" in prompt and "- thread old" not in prompt and "## Your role" not in prompt and len(prompt) < 1500
    d = json.loads(f.read_text())
    assert d["session"] == SID and d["host"] == ME and d["context_tokens"] == 32005 and "busy" not in d and d["work"] == ["- thread old", "- thread new"]
    row = [json.loads(l) for p in (q / "stats").glob("*.jsonl") for l in p.read_text().splitlines()][-1]
    assert row["resumed"] is True and row["prompt_chars"] == len(prompt) and row["session"] == SID
    done = [json.loads(l) for p in (q / "events").glob("*.jsonl") for l in p.read_text().splitlines() if '"done"' in l][-1]
    assert done["session"] == SID

def test_a_failed_resume_falls_back_to_a_fresh_run(env, monkeypatch):
    r, mail, q = env.r, env.mail, env.q
    m1, m3 = thread(env)
    f = q / "threads" / f"{m1['id']}.json"; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(json.dumps(rec(cwd=str(env.root))), encoding="utf-8")
    seen = []
    def run(exe, prompt, mode, cwd, *a, parent="", resume="", **k):
        seen.append((prompt, resume)); mr.SESSIONS[parent] = (resume or "fresh-sid", str(cwd))
        if resume: return 1, json.dumps({"type": "result", "is_error": True, "result": "No conversation found with session ID"}), ""
        return 0, json.dumps({"type": "result", "result": "ok"}), ""
    monkeypatch.setattr(mr, "run_claude", run)
    agents = mail.load_agents()
    r.execute(r.prepare("orchestrator", agents["orchestrator"], m3, agents, "/x/claude"))
    assert [x[1] for x in seen] == [SID, ""] and "## Thread so far" in seen[1][0] and json.loads(f.read_text())["session"] == "fresh-sid"

def test_builders_never_resume(env):
    m1, m3 = thread(env)
    f = env.q / "threads" / f"{m1['id']}.json"; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(json.dumps(rec(cwd=str(env.root))), encoding="utf-8")
    m = env.mail.send("orchestrator", "builder", "Again", "Do more.", reply_to=m3["id"]); agents = env.mail.load_agents()
    job = env.r.prepare("builder", agents["builder"], m, agents, "/x/claude")
    assert job["resume"] == "" and "## Thread so far" in job["prompt"] and "Before ending, write" not in job["prompt"]
    env.r.drop(job["held"], job["shared"])


# ---- 5. compact sections ----
def test_notepad_shows_new_lines_and_counts_the_rest(env):
    import notes
    t = "T1"
    for i in range(3): notes.add(t, f"d{i}", f"old note {i}", at=time.time() - 100)
    notes.mark_read(t, time.time() - 50); notes.add(t, "d9", "N" * 3000, at=time.time())
    text = mr.notepad_text(t)
    assert "[NEW] " + "N" * 3000 in text and "old note" not in text
    assert text.splitlines()[-1] == f"3 earlier note(s), already read; full notepad: python {TK.resolve() / 'notes.py'} show {t}"

def test_other_work_is_same_area_only_and_at_most_five(env, monkeypatch):
    row = lambda i, area: {"thread": f"t{i}", "title": f"job {i}", "items": [{"agent": "builder", "state": "queued", "area": area, "subject": f"job {i}"}]}
    monkeypatch.setattr(fleet, "active_work", lambda skip="": [row(i, "kepler") for i in range(7)] + [row(9, "birch")])
    lines = mr.other_work(env.mail, {"id": "m", "thread": "m", "repo": "Kepler"})
    assert len(lines) == 5 and all("kepler" in l for l in lines) and not any("job 9" in l for l in lines)
    assert mr.other_work(env.mail, {"id": "m", "thread": "m", "body": "no area"}) == [], "no repo or area: the section is left out"

def test_discord_rules_only_short_and_only_for_the_orchestrator(env):
    d = env.q / "discord"; d.mkdir(parents=True)
    m = env.mail.send("owner", "orchestrator", "Hi", "Do it."); agents = env.mail.load_agents()
    (d / "argon-memory.json").write_text(json.dumps([{"rule": "Short answers."}]))
    assert "Short answers." in mr.build_prompt(env.mail, "orchestrator", {}, m, agents, ME)
    assert "Short answers." not in mr.build_prompt(env.mail, "builder", {}, m, agents, ME)
    (d / "argon-memory.json").write_text(json.dumps([{"rule": "x" * 400}]))
    assert "standing rules for Discord" not in mr.build_prompt(env.mail, "orchestrator", {}, m, agents, ME)

def test_same_repo_playbook_and_devices_are_short(env):
    q = env.q; (q / "discord").mkdir(parents=True); now = time.time()
    (q / "discord/state.json").write_text(json.dumps({"threads": {f"t{i}": {"title": f"Kepler job {i}", "project": "kepler", "active_at": now - i, "answer": "a" * 400} for i in range(6)}}))
    import playbook
    for i in range(7): playbook.add("kepler", f"Lesson number {i} about the Kepler map " + "y" * 200)
    for host, plat in ((ME, "mac"), ("OTHER-PC", "windows")): fleet._write(q / "devices" / f"{host}.json", {"host": host, "platform": plat, "at": now})
    m = env.mail.send("owner", "orchestrator", "Kepler: more", "More.", repo="Kepler"); agents = env.mail.load_agents()
    same = mr.same_repo_text(env.mail, m)
    assert 0 < len(same.splitlines()) <= 4 and all(len(l) <= 150 for l in same.splitlines())
    p = mr.build_prompt(env.mail, "orchestrator", {}, m, agents, ME)
    book = p.split("## Playbook for kepler")[1].split("\n\n")[0].splitlines()[1:]
    assert len(book) == 5 and all(len(l) <= 162 for l in book)
    dev = [l for l in p.splitlines() if l.startswith("Devices up:")]
    assert len(dev) == 1 and "OTHER-PC" in dev[0] and "this run" in dev[0] and "## Devices up now" not in p
