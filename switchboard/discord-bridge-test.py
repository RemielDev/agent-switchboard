#!/usr/bin/env python3
"""Offline harness for discord-bridge.py: a fake Discord behind urllib, a real agent-mail in a temp Queue.

  python3 discord-bridge-test.py            run everything
  python3 discord-bridge-test.py archived   run tests whose name contains "archived"

No network, no bot token and no live state are touched: the dev root is a temp folder, urlopen is replaced, and the
token path points at a dummy file. The fake enforces Discord's rules that matter here: 2000-char messages, 100-char
thread names, thread id equal to the source message id, 50083 on editing in an archived thread, send unarchives,
10003 for a deleted thread, 2 renames per 10 minutes, 429 bodies, missing permissions."""
import argparse, importlib.util, io, json, os, re, shutil, subprocess, sys, tempfile, threading, time as real_time, traceback, urllib.error, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OWNER, BOT, GUILD, PARENT, PROGRESS, THREADS, REPORTS, ALERTS = "100", "200", "300", "1000", "1001", "1002", "1003", "1004"
LOOPS = "1006"  # like OVERVIEW: in the fake server, added to the bridge with loops_on()
OVERVIEW = "1005"  # exists in the fake server; a test adds it to the bridge's channels with ov_on()
PC, MAC = "TEST-PC", "TEST-MAC"

def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod; spec.loader.exec_module(mod); return mod

class Clock:
    """The bridge's clock, so a test can jump forward. Sleeping just advances it."""
    def __init__(self): self.offset = 0.0
    def time(self): return real_time.time() + self.offset
    def sleep(self, s): self.offset += s
    def strftime(self, *a): return real_time.strftime(*a)
    def gmtime(self, *a): return real_time.gmtime(*a)

class Err(Exception):
    def __init__(self, status, code, body=None): self.status, self.code, self.body = status, code, body

class Fake:
    def __init__(self):
        self.next, self.channels, self.calls, self.reactions, self.responses, self.members, self.renames = 5000, {}, [], [], [], [], {}
        for c in (PARENT, PROGRESS, THREADS, REPORTS, ALERTS, OVERVIEW, LOOPS): self.channels[c] = {"type": 0, "msgs": {}}
        self.deny_threads, self.down, self.queue429, self.react_log, self.deny_manage = False, False, [], [], False
        self.fail = None  # fn(method, path, body) -> an Err or URLError to raise for that call, or None
    def nid(self): self.next += 1; return str(self.next)
    def add_msg(self, cid, content, author=BOT, components=None, reference=None, mid=None, files=None, type=0):
        mid = mid or self.nid()
        self.channels[cid]["msgs"][mid] = {"id": mid, "channel_id": cid, "content": content, "author": {"id": author}, "components": components or [], "message_reference": reference, "files": files or [], "type": type}
        return self.channels[cid]["msgs"][mid]
    def msgs(self, cid): return list(self.channels[cid]["msgs"].values())
    def count(self, method, part): return sum(1 for m, p, _ in self.calls if m == method and part in p)
    def threads(self): return [k for k, c in self.channels.items() if c["type"] == 11]

    def urlopen(self, req, timeout=None):
        if "discord.com" not in req.full_url: raise AssertionError("harness refused a non-Discord URL: " + req.full_url)
        if self.down: raise urllib.error.URLError("network down")
        path = req.full_url.split("/api/v10", 1)[1]; method = req.get_method(); raw = req.data or b""
        files = []
        if "multipart" in (req.get_header("Content-type") or ""):
            body = json.loads(re.search(rb"payload_json.*?\r\n\r\n(.*?)\r\n--", raw, re.S).group(1))
            for name, blob in re.findall(rb'filename="([^"]+)".*?\r\n\r\n(.*?)\r\n--', raw, re.S): files.append((name.decode(), blob))
        else: body = json.loads(raw) if raw else None
        self.calls.append((method, path, body))
        try:
            if self.fail and (x := self.fail(method, path, body)): raise x
            if self.queue429:
                self.queue429.pop(0); raise Err(429, 0, {"retry_after": 0.5, "global": False, "message": "slow"})
            self.query = path.partition("?")[2]
            return Resp(self.route(method, path.split("?")[0], body, files))
        except Err as e:
            payload = e.body if e.body is not None else {"code": e.code, "message": "fake"}
            raise urllib.error.HTTPError(req.full_url, e.status, "fake", {}, io.BytesIO(json.dumps(payload).encode()))

    def channel(self, cid):
        if cid not in self.channels: raise Err(404, 10003)
        return self.channels[cid]
    def route(self, m, p, b, files):
        if (r := re.fullmatch(r"/channels/(\d+)/messages", p)):
            cid = r[1]; c = self.channel(cid)
            if m == "GET":
                rows = list(c["msgs"].values()); lim = re.search(r"(?:^|&)limit=(\d+)", getattr(self, "query", "")); bef = re.search(r"(?:^|&)before=(\d+)", getattr(self, "query", ""))
                if bef: rows = [x for x in rows if int(x["id"]) < int(bef[1])]  # a page further back
                return rows[::-1][:int(lim[1])] if lim and (int(lim[1]) < 100 or int(lim[1]) < len(rows)) else rows  # never more than asked for; a short read is the newest, newest first, as Discord gives it
            if m != "POST": raise AssertionError(p)
            if c["type"] == 11:
                if c.get("locked"): raise Err(403, 160005)
                c["archived"] = False; c["last_at"] = CLOCK.time()
            content = (b or {}).get("content", "")
            v2 = bool(((b or {}).get("flags") or 0) & 32768)
            if v2 and content: raise Err(400, 50035)  # a card carries no content
            if len(content.encode("utf-16-le")) // 2 > 2000: raise Err(400, 50035)
            if not content and not files and not v2: raise Err(400, 50006)
            ref = (b or {}).get("message_reference")
            msg = self.add_msg(cid, content, components=(b or {}).get("components"), reference=ref, files=files)
            if v2: msg["flags"] = 32768
            return msg
        if (r := re.fullmatch(r"/channels/(\d+)/messages/(\d+)", p)):
            c = self.channel(r[1])
            if r[2] not in c["msgs"]: raise Err(404, 10008)
            if m == "GET": return c["msgs"][r[2]]
            if m == "DELETE":
                if c["msgs"][r[2]]["type"] in (1, 2, 3, 4, 5, 21): raise Err(400, 50021)  # Discord's table: not deletable
                if self.deny_manage and c["msgs"][r[2]]["author"]["id"] != BOT: raise Err(403, 50013)
                self.__dict__.setdefault("gone", []).append(c["msgs"].pop(r[2])); return None
            if c.get("archived"): raise Err(400, 50083)
            if len((b or {}).get("content", "").encode("utf-16-le")) // 2 > 2000: raise Err(400, 50035)
            was = c["msgs"][r[2]].get("flags", 0) & 32768
            if (was or (b.get("flags") or 0) & 32768) and b.get("content"): raise Err(400, 50035)  # a card never takes content back
            c["msgs"][r[2]].update({k: v for k, v in b.items() if k in ("content", "components", "flags")}); return c["msgs"][r[2]]
        if (r := re.fullmatch(r"/channels/(\d+)/messages/(\d+)/threads", p)):
            c = self.channel(r[1])
            if self.deny_threads: raise Err(403, 50013)
            if r[2] not in c["msgs"]: raise Err(404, 10008)
            if r[2] in self.channels: raise Err(400, 160004)
            if not 1 <= len(b["name"]) <= 100: raise Err(400, 50035)
            self.channels[r[2]] = {"type": 11, "msgs": {}, "name": b["name"], "parent": r[1], "archived": False, "auto": b.get("auto_archive_duration"), "last_at": CLOCK.time()}
            return {"id": r[2], "name": b["name"]}
        if m == "POST" and (r := re.fullmatch(r"/channels/(\d+)/threads", p)):  # a thread with no starter message
            tid = self.nid(); self.channels[tid] = {"type": 11, "msgs": {}, "name": b["name"], "parent": r[1], "archived": False, "last_at": CLOCK.time()}
            return {"id": tid, "name": b["name"]}
        if (r := re.fullmatch(r"/channels/(\d+)/threads/archived/public", p)):
            return {"threads": [{"id": k, "name": c["name"], "parent_id": r[1]} for k, c in self.channels.items() if c["type"] == 11 and c.get("parent") == r[1] and c.get("archived")], "has_more": False}
        if re.fullmatch(r"/guilds/\d+/threads/active", p):  # last_message_id carries the time of the last post, as a real id does
            return {"threads": [{"id": k, "name": c["name"], "parent_id": c.get("parent"), "last_message_id": str(int((c.get("last_at", 0) - 1420070400) * 1000) << 22) if c.get("last_at") else None}
                                for k, c in self.channels.items() if c["type"] == 11 and not c.get("archived")], "members": []}
        if (r := re.fullmatch(r"/guilds/(\d+)", p)): return {"id": r[1], "name": "g", "owner_id": OWNER}
        if re.fullmatch(r"/guilds/\d+/channels", p):
            if m == "GET": return [{"id": k, "type": c["type"], "name": c["name"], "parent_id": c.get("parent"), "position": c.get("position", 0)} for k, c in self.channels.items() if c["type"] != 11 and c.get("name")]
            if m == "PATCH":  # bulk positions
                for x in b: self.channel(x["id"])["position"] = x["position"]
                return None
            cid = self.nid(); self.channels[cid] = {"type": b["type"], "msgs": {}, "name": b["name"], "parent": b.get("parent_id"), "topic": b.get("topic")}
            return {"id": cid, "name": b["name"]}
        if p == "/users/@me": return {"id": BOT}
        if p == "/gateway/bot": return {"url": "wss://gateway.test"}  # the socket itself is FakeWS in the one test that runs the loop
        if re.fullmatch(r"/applications/\d+/guilds/\d+/commands", p): return b
        if (r := re.fullmatch(r"/channels/(\d+)", p)):
            c = self.channel(r[1])
            if m == "GET": return {"id": r[1], "type": c["type"], "name": c.get("name"), "thread_metadata": {"archived": bool(c.get("archived"))}}
            if m == "DELETE": del self.channels[r[1]]; return None
            if "name" in b and c.get("archived"): raise Err(400, 50083)
            if "name" in b:
                now = CLOCK.time(); rec = [x for x in self.renames.get(r[1], []) if now - x < 600]
                if len(rec) >= 2: raise Err(429, 0, {"retry_after": 500.0, "global": False, "message": "rename limit"})
                self.renames[r[1]] = rec + [now]; c["name"] = b["name"]
            for k in ("archived", "locked", "topic"):
                if k in b: c[k] = b[k]
            return {"id": r[1]}
        if re.fullmatch(r"/channels/(\d+)/thread-members/\d+", p): self.members.append(p); self.channel(p.split("/")[2]); return None
        if (r := re.fullmatch(r"/channels/(\d+)/messages/(\d+)/reactions/(.+)/@me", p)):
            self.react_log.append((m, r[2], r[3]))
            if m == "PUT": self.reactions.append(r[2])
            return None
        if (r := re.fullmatch(r"/interactions/(\d+)/(.+)/callback", p)):
            self.responses.append(b)
            if b["type"] == 4: self.add_msg(PARENT, b["data"].get("content", ""), mid=self.nid())
            return None
        if re.fullmatch(r"/webhooks/\d+/.+/messages/@original", p): return self.msgs(PARENT)[-1]
        if m == "POST" and re.fullmatch(r"/webhooks/\d+/[^/]+", p): self.responses.append({"type": "followup", "data": b}); return {"id": self.nid()}
        raise AssertionError("no fake route: " + m + " " + p)

class Resp:
    def __init__(self, v): self.v = v
    def read(self): return json.dumps(self.v, default=lambda x: x.decode("latin-1")).encode() if self.v is not None else b""  # an upload's bytes
    def __enter__(self): return self
    def __exit__(self, *a): return False

CLOCK = Clock()
GRACE = None  # the bridge's real DONE_GRACE; setup() runs every test with 0 and grace_on() turns the real value back on
bridge_mod, agent_mod, fake, b, tmp, fleet_mod = None, None, None, None, None, None
runner_mod, shot_mod, real_capture, capture_fail = None, None, None, None

def setup():
    global fake, b, tmp, bridge_mod, agent_mod, fleet_mod, shot_mod, real_capture, capture_fail, GRACE
    tmp = Path(tempfile.mkdtemp(prefix="bridge-test-"))
    os.environ["SWITCHBOARD_ROOT"] = str(tmp); CLOCK.offset = 0
    os.environ.pop("AGENT_MAIL_PARENT", None)  # this harness may itself be running inside a runner turn
    agent_mod = agent_mod or load_module("agent_mail_test", "agent-mail.py")
    for n, k, auto in (("owner", "human", False), ("sessions", "human", False), ("orchestrator", "claude", True), ("builder", "claude", True), ("mac", "claude", False)):
        agent_mod.register(n, MAC if n == "mac" else "any", k, auto=auto)
    q = tmp / "Queue"; (q / "discord").mkdir(parents=True, exist_ok=True)
    (q / "discord/config.json").write_text(json.dumps({"guild_id": GUILD, "owner_id": OWNER, "bot_id": BOT, "cards": False, "smart_buttons": False, "argon_chat": False, "self_reload": False, "overview_threads": True, "hosts": [PC, MAC],
        "channels": {"orchestrator": PARENT, "progress": PROGRESS, "threads": THREADS, "reports": REPORTS, "alerts": ALERTS}}), encoding="utf-8")
    bridge_mod = bridge_mod or load_module("discord_bridge_test", "discord-bridge.py")
    fleet_mod = bridge_mod.fleet
    if GRACE is None: GRACE = getattr(bridge_mod, "DONE_GRACE", 10)
    bridge_mod.DONE_GRACE = 0  # most tests look at the tick a run ends on; the hold before Done has its own tests, which call grace_on()
    fleet_mod.time = CLOCK
    bridge_mod.time = CLOCK; (tmp / "secrets").mkdir(); bridge_mod.TOKEN = tmp / "secrets/dummy-token.txt"; bridge_mod.TOKEN.write_text("dummy", encoding="utf-8"); bridge_mod.BLOCKED.clear()
    fake = Fake(); urllib.request.urlopen = fake.urlopen; bridge_mod.urllib.request.urlopen = fake.urlopen
    for host, plat in ((PC, "windows"), (MAC, "mac")):
        fleet_mod._write(q / "devices" / f"{host}.json", {"host": host, "platform": plat, "root": {"windows": "C:/Dev/Work", "mac": "/Users/alex/Developer/Work"}[plat], "at": real_time.time(), "repos": []})
    shot_mod = bridge_mod.screencap; real_capture = real_capture or shot_mod.capture; capture_fail = None
    shot_mod.capture = fake_capture; shot_mod.LOCAL = tmp / "local-captures"; shot_mod.time = CLOCK
    bridge_mod.PEERS = tmp / "local-peers.json"  # never the real relay reading in ~/.switchboard
    b = bridge_mod.Bridge(); b.st["started"] = 0
    b.spawn = lambda fn: fn(); b.cfg["screenshots"] = False  # the screenshot tests turn it on; nothing here ever reads the real screen
    b.cfg["smart_buttons"] = False  # the Argon button tests turn it on with a stub; no test runs the real model
    b.cfg["cards"] = False  # the card tests turn cards on; the rest read plain content
    bridge_mod.topics.time = CLOCK; b.spawn_label = lambda: None  # no test ever starts the real labelling process

def teardown():
    if os.environ.get("SWITCHBOARD_ROOT") == str(tmp): shutil.rmtree(tmp, ignore_errors=True)

def fake_capture(dest):
    if capture_fail: raise shot_mod.CaptureError(capture_fail)
    Path(dest).parent.mkdir(parents=True, exist_ok=True); Path(dest).write_bytes(b"\xff\xd8fake-jpeg")
    return Path(dest)

def tick(n=1):
    for _ in range(n): b.last_tick = 0; b.tick()
def sweep_tick(): b.last_sweep = 0; b.last_q = 0; tick()
def owner_msg(content, cid=PARENT, reference=None, author=OWNER):
    m = fake.add_msg(cid, content, author=author, reference=reference)
    b.dispatch("MESSAGE_CREATE", {"id": m["id"], "channel_id": cid, "author": {"id": author}, "content": content, "attachments": [], "type": 0, "message_reference": reference})
    return m["id"]
def inbox(a): return agent_mod.list_inbox(a)
def request(text="Fix the pool in Kepler"):
    origin = owner_msg(text); tick()
    return origin, inbox("orchestrator")[-1]["id"]
def event(kind, tid, ref=None, agent="orchestrator", title="t", detail=""):
    return fleet_mod.event(kind, title, agent=agent, thread=tid, ref=ref or tid, detail=detail)
def answer(tid, body, sender="orchestrator"):
    return agent_mod.send(sender, "owner", "Re: Fix the pool in Kepler", body, reply_to=tid)
def question(tid, body="Which one?", options=("A", "B"), sender="orchestrator"):
    return agent_mod.send(sender, "owner", "Pick one", body, reply_to=tid, options=[{"label": o} if isinstance(o, str) else o for o in options])
def status_msgs(th): return [m for m in fake.msgs(th) if re.match(r"^\S+ \*\*(Working|Queued|Needs you|Waiting|Done|Failed|Cut off)\*\*", m["content"])]
def press(th_or_ch, key, i):
    msg = next(m for c in fake.channels.values() for m in c["msgs"].values() if any(x.get("custom_id") == f"a:{key}:{i}" for x in bridge_mod.walk_components(m["components"])))
    b.dispatch("INTERACTION_CREATE", {"type": 3, "id": fake.nid(), "token": "tok", "channel_id": msg["channel_id"], "member": {"user": {"id": OWNER}}, "data": {"custom_id": f"a:{key}:{i}"}, "message": msg})

# ---- required scenarios ----
def test_new_request():
    origin, mid = request()
    assert origin in fake.threads(), "thread is on the owner's own message"
    assert fake.channels[origin]["parent"] == PARENT and fake.channels[origin]["auto"] == 10080
    assert fake.channels[origin]["name"].endswith("Fix the pool in Kepler"), fake.channels[origin]["name"]
    assert [m["author"]["id"] for m in fake.msgs(PARENT)] == [OWNER], "channel holds only the owner's message"
    assert len(status_msgs(origin)) == 1 and "Queued" in status_msgs(origin)[0]["content"]
    assert any(OWNER in p for p in fake.members), "owner joined"
    assert inbox("orchestrator")[0]["subject"] == "Fix the pool in Kepler"

def test_reply_in_thread():
    origin, mid = request()
    a = answer(mid, "Which pool?"); tick()
    n = len(fake.threads())
    agent_mod.ack("orchestrator", mid)
    owner_msg("The big one", cid=origin); tick()
    assert len(fake.threads()) == n, "no second thread"
    m = inbox("orchestrator")[-1]
    assert m["thread"] == mid and m["reply_to"] == a["id"] and m["hops"] == 0, m
    assert m["body"] == "The big one"
    assert origin in str(b.st["dthreads"]) and fake.msgs(PARENT)[-1]["id"] == origin, "parent untouched"
    assert len([x for x in fake.react_log if x[0] == "PUT" and x[2] != urllib.parse.quote("🟡")]) == 1, fake.react_log  # the state reaction on their message is not counted
    assert len(status_msgs(origin)) == 1 and status_msgs(origin)[0]["content"].split()[1] == "**Queued**"
    assert fake.msgs(origin)[-1]["id"] == status_msgs(origin)[0]["id"], "status moved under the new message"
    # a long conversation never reaches the agent hop limit
    for i in range(9):
        last = inbox("orchestrator")[-1]; agent_mod.ack("orchestrator", last["id"]); answer(mid, f"a{i}"); tick()
        owner_msg(f"more {i}", cid=origin); tick()
    assert inbox("orchestrator")[-1]["hops"] == 0
    # builder addressing inside a thread
    owner_msg("builder: do it", cid=origin); tick()
    assert inbox("builder")[-1]["thread"] == mid and inbox("builder")[-1]["body"] == "do it"

def test_button_press():
    origin, mid = request()
    q = question(mid); tick()
    card = next(m for m in fake.msgs(origin) if m["components"])
    assert f"<@{OWNER}>" in card["content"] and fake.channels[origin]["name"]
    assert "Needs you" in status_msgs(origin)[0]["content"] and b.state_word(b.st["threads"][mid]) == "NEEDS YOU"
    assert not inbox("owner"), "question acked once shown"
    press(origin, q["id"], 1)
    r = fake.responses[-1]; assert r["type"] == 7 and "**Answer:** B" in r["data"]["content"] and r["data"]["components"] == []
    reply = next(m for m in inbox("orchestrator") if m.get("reply_to") == q["id"]); assert reply["body"] == "B" and reply["thread"] == mid
    assert q["id"] not in b.st["q"] and not b.st["threads"][mid].get("asking")
    tick(); assert "Queued" in status_msgs(origin)[0]["content"]

def test_typed_answer_to_question():
    origin, mid = request()
    q = question(mid); tick()
    owner_msg("Neither, use the small one", cid=origin); tick()
    reply = next(m for m in inbox("orchestrator") if m.get("reply_to") == q["id"]); assert reply["body"] == "Neither, use the small one"
    card = next(m for m in fake.msgs(origin) if "asks:" in m["content"])
    assert card["components"] == [] and "**Answer:** Neither" in card["content"] and q["id"] not in b.st["q"]

def test_a_question_back_puts_the_card_on_hold():
    origin, mid = request(); q = question(mid); tick()
    owner_msg("Is the mcp still off?", cid=origin); tick()
    got = inbox("orchestrator")[-1]; assert got["body"].startswith("Is the mcp still off?") and got.get("needs_reply"), "it goes on in the thread and is answered"
    card = next(m for m in fake.msgs(origin) if "asks:" in m["content"])
    assert not card["components"] and "**Answer:**" not in card["content"] and "On hold" in card["content"] and q["id"] not in b.st["q"], "asked again after their question is answered"
    assert "On hold" in got["body"] and q["id"] in got["body"]

def test_status_edit():
    origin, mid = request()
    st_id = status_msgs(origin)[0]["id"]; before = fake.count("POST", f"/channels/{origin}/messages")
    event("started", mid); tick()
    assert status_msgs(origin)[0]["id"] == st_id and "Working" in status_msgs(origin)[0]["content"]
    assert "<t:" in status_msgs(origin)[0]["content"], "live timestamp"
    edits = fake.count("PATCH", f"/channels/{origin}/messages/")
    for _ in range(4): CLOCK.offset += 25; sweep_tick()
    assert fake.count("PATCH", f"/channels/{origin}/messages/") == edits, "no edits while nothing changes"
    event("done", mid); answer(mid, "Pool fixed."); tick()
    assert not status_msgs(origin) and st_id not in [m["id"] for m in fake.msgs(origin)], "no Done card once answered"
    assert fake.count("POST", f"/channels/{origin}/messages") == before + 1, "only the answer was posted"
    assert any(m["content"] == "Pool fixed." for m in fake.msgs(origin))
    prog = fake.msgs(PROGRESS)[0]; assert "DONE" in prog["content"] and f"<#{origin}>" in prog["content"], prog["content"]
    assert len(fake.msgs(PROGRESS)) == 1
    # the name follows the settled state
    assert "✅" not in fake.channels[origin]["name"]
    sweep_tick(); CLOCK.offset += 31; sweep_tick()
    assert fake.channels[origin]["name"].startswith("✅"), fake.channels[origin]["name"]

def test_long_message():
    origin, mid = request()
    code = "\n".join(f"line {i} " + "x" * 70 for i in range(30))
    answer(mid, "Intro\n\n```py\n" + code + "\n```\n\nOutro " + "word " * 100); tick()
    parts = [m for m in fake.msgs(origin) if not status_msgs(origin) or m["id"] != status_msgs(origin)[0]["id"]]
    assert 1 < len(parts) <= 2 and all(len(p["content"]) <= 2000 for p in parts), [len(p["content"]) for p in parts]
    assert all(p["content"].count("```") % 2 == 0 for p in parts), "code fences balanced"
    assert "Outro" in parts[-1]["content"] and "```py" in parts[1]["content"]
    # beyond two messages: opening plus the whole text as a file
    origin2 = owner_msg("second"); tick(); mid2 = inbox("orchestrator")[-1]["id"]
    answer(mid2, "para\n\n" * 6000); tick()
    msgs = fake.msgs(origin2); file_msgs = [m for m in msgs if m["files"]]
    assert len(file_msgs) == 1 and file_msgs[0]["files"][0][0] == "full-answer.md" and len(msgs) <= 3, len(msgs)
    # emoji count double against the limit
    origin3 = owner_msg("third"); tick(); mid3 = inbox("orchestrator")[-1]["id"]
    answer(mid3, "😀" * 3000); tick()
    assert all(len(m["content"].encode("utf-16-le")) // 2 <= 2000 for m in fake.msgs(origin3))
    # a long question keeps its buttons on the final part
    origin4 = owner_msg("fourth"); tick(); mid4 = inbox("orchestrator")[-1]["id"]
    q = question(mid4, body="Long question. " * 400); tick()
    msgs = [m for m in fake.msgs(origin4) if "Long question" in m["content"]]
    assert len(msgs) >= 2 and msgs[-1]["components"] and not msgs[0]["components"]
    b.drop_q(q["id"]); assert not [m for m in fake.msgs(origin4) if "Long question" in m["content"]], "all parts removed"

def test_attach():
    origin, mid = request()
    f = tmp / "Queue/report.png"; f.write_bytes(b"\x89PNG")
    answer(mid, "Look\nATTACH: Queue/report.png\nATTACH: Queue/missing.png"); tick()
    msgs = fake.msgs(origin); up = [m for m in msgs if m["files"]]
    assert up and up[0]["files"][0] == ("report.png", b"\x89PNG"); assert any("could not attach missing.png" in m["content"] for m in msgs)
    assert "ATTACH:" not in "".join(m["content"] for m in msgs)
    secret = bridge_mod.TOKEN.parent / "secret.txt"; secret.write_text("no", encoding="utf-8")
    bridge_mod.TOKEN = secret
    answer(mid, f"ATTACH: {secret}"); tick()
    assert any("refused" in m["content"] for m in fake.msgs(origin)) and "secret.txt" not in [x[0] for m in fake.msgs(origin) for x in m["files"]]
    # finding 1: nothing outside the dev root, including through a link that lives inside it
    outside = Path(tempfile.mkdtemp(prefix="bridge-outside-"))
    try:
        key = outside / "id_ed25519"; key.write_text("private", encoding="utf-8")
        link = tmp / "Queue/innocent.png"
        try: link.symlink_to(key); linked = True
        except OSError: linked = False  # Windows without the symlink privilege: the link half cannot be set up here
        answer(mid, f"ATTACH: {key}" + ("\nATTACH: Queue/innocent.png" if linked else "")); tick()
        names = [x[0] for m in fake.msgs(origin) for x in m["files"]]
        assert "id_ed25519" not in names and "innocent.png" not in names, names
        assert sum("outside the dev root" in m["content"] for m in fake.msgs(origin)) == 1 and (not linked or any("innocent.png: outside" in m["content"] for m in fake.msgs(origin)))
    finally: shutil.rmtree(outside, ignore_errors=True)

def test_archived_thread():
    origin, mid = request(); b.st["threads"][mid].pop("origin", None); b.st["threads"][mid].pop("owner_at", None); b.cfg["keep_unread"] = False
    event("started", mid); event("done", mid); answer(mid, "Done."); tick(); sweep_tick(); CLOCK.offset += 31; sweep_tick()
    CLOCK.offset += 2 * 3600 + 10; sweep_tick()
    assert fake.channels[origin]["archived"] and b.st["threads"][mid].get("archived"), "finished thread archives itself"
    calls = len(fake.calls); sweep_tick(); assert len(fake.calls) == calls, "archived thread stays quiet"
    # new agent reply reopens by sending
    answer(mid, "One more thing."); tick()
    assert not fake.channels[origin]["archived"] and any("One more thing" in m["content"] for m in fake.msgs(origin))
    # editing inside an archived thread reopens and retries
    fake.channels[origin]["archived"] = True; b.st["threads"][mid]["archived"] = True
    event("started", mid, ref="r2"); tick()
    assert not fake.channels[origin]["archived"] and "Working" in status_msgs(origin)[0]["content"]
    assert fake.count("PATCH", f"/channels/{origin}") and "archived" not in b.st["threads"][mid]
    # The owner typing in an archived thread continues it
    fake.channels[origin]["archived"] = True; b.dispatch("THREAD_UPDATE", {"id": origin, "name": fake.channels[origin]["name"], "thread_metadata": {"archived": True}})
    fake.channels[origin]["archived"] = False  # Discord unarchives when they send
    owner_msg("again", cid=origin); tick()
    assert inbox("orchestrator")[-1]["body"] == "again"
    # a button in an archived thread is reopened first
    q = question(mid); tick(); fake.channels[origin]["archived"] = True; b.st["threads"][mid]["archived"] = True
    press(origin, q["id"], 0); assert not fake.channels[origin]["archived"]

def test_old_state_upgrade():
    q = tmp / "Queue/discord"; now = real_time.time()
    for mid, c in ((6001, PARENT), (6002, PARENT), (6003, PARENT), (6004, PARENT), (6005, THREADS)): fake.add_msg(c, "old", mid=str(mid))
    old = {"threads": {
        "20261110T000000Z-aaaaaaaa": {"title": "In flight", "runs": {"r": {"agent": "orchestrator", "label": "PC", "t0": now}}, "t0": now, "answer": "", "who": "", "origin": "6001", "card": "6002", "card_text": "Working on it . ."},
        "20261110T000000Z-bbbbbbbb": {"title": "Finished", "runs": {"r": {"agent": "orchestrator", "label": "PC", "t0": now - 900, "end": "done", "t1": now - 800}}, "t0": now - 900, "answer": "Old answer", "who": "orchestrator", "origin": "6003", "card": "6004", "card_text": "Old answer"}},
        "q": {"20261110T000001Z-cccccccc": {"from": "orchestrator", "options": [{"label": "Yes"}, {"label": "No"}], "subject": "s", "body": "b", "thread": "20261110T000000Z-aaaaaaaa", "msg": "6003", "shown": [0, 1]}},
        "pending": {}, "ev_ts": now, "reports": [], "started": 1.0, "seen": [],
        "sessions": {f"{MAC}/abc": {"id": "6005", "text": "**IDLE**  x  ·  Mac  ·  claude"}},
        "posted": {"20261110T000002Z-dddddddd": {"from": "orchestrator", "thread": "20261110T000000Z-bbbbbbbb"}}}
    fleet_mod._write(q / "state.json", old)
    (tmp / f"Queue/sessions/{MAC}").mkdir(parents=True); (tmp / f"Queue/sessions/{MAC}/abc.json").write_text(json.dumps({"session_id": "abc", "host": MAC, "tool": "claude", "project": "Kepler", "state": "idle", "last_prompt": "hello", "updated": now}), encoding="utf-8")
    agent_mod.send("orchestrator", "owner", "seed", "seed")  # keeps the mail folder real
    for m in inbox("owner"): agent_mod.ack("owner", m["id"])
    global b
    b = bridge_mod.Bridge(); assert b.st["v"] == 2 and (q / "state.v1-backup.json").exists()
    assert b.st["threads"]["20261110T000000Z-bbbbbbbb"]["answer"] == "Old answer"
    b.last_sweep = 0; tick()
    # in-flight work moved into a real thread, its parked 'Working' card gone, finished work left alone
    assert "6001" in fake.channels and "6001" in fake.threads()
    assert "6002" not in fake.channels[PARENT]["msgs"], "old working card removed"
    assert "6003" not in fake.threads(), "finished thread not disturbed"
    assert b.st["q"]["20261110T000001Z-cccccccc"]["msg"] == "6003"
    # legacy session card keeps its id and gains a thread
    assert "6005" in fake.threads() and b.st["sessions"][f"{MAC}/abc"]["id"] == "6005"
    # an old question card in the channel still works
    agent_mod.send("orchestrator", "owner", "q", "q")
    msg = fake.channels[PARENT]["msgs"]["6003"]; msg["components"] = [{"components": [{"custom_id": "a:20261110T000001Z-cccccccc:0"}]}]
    mail = agent_mod.send("orchestrator", "owner", "x", "x")  # a real id to reply to
    b.st["q"][mail["id"]] = b.st["q"].pop("20261110T000001Z-cccccccc"); msg["components"] = [{"components": [{"custom_id": f"a:{mail['id']}:0"}]}]
    press(PARENT, mail["id"], 0); assert fake.responses[-1]["type"] == 7 and "**Answer:** Yes" in fake.responses[-1]["data"]["content"]
    # a reply to the old origin continues its mail thread, and the old thread stays usable
    b.dispatch("MESSAGE_CREATE", {"id": fake.nid(), "channel_id": PARENT, "author": {"id": OWNER}, "content": "follow up on the finished one", "attachments": [], "message_reference": {"message_id": "6003"}})
    assert b.st["threads"]["20261110T000000Z-bbbbbbbb"].get("queued"), "continued by reply"
    # legacy amend (no message list recorded) does not crash
    b.amend({"from": "orchestrator", "body": "changed"}, {"target": "20261110T000002Z-dddddddd", "op": "update"})

def test_deleted_thread():
    origin, mid = request()
    answer(mid, "first"); tick()
    del fake.channels[origin]; b.dispatch("THREAD_DELETE", {"id": origin})
    assert b.st["threads"][mid].get("gone") and origin not in b.st["dthreads"]
    event("started", mid); tick()
    assert origin not in fake.channels, "status alone does not recreate a deleted thread"
    answer(mid, "second"); tick()
    assert origin in fake.channels and any(m["content"] == "second" for m in fake.msgs(origin)), "a new reply brings it back"
    assert not b.st["threads"][mid].get("gone")
    # deleted while the gateway was away: found through 10003
    del fake.channels[origin]
    answer(mid, "third"); tick()
    assert any(m["content"] == "third" for m in fake.msgs(origin)) and not inbox("owner")

def test_rate_limits():
    origin, mid = request()
    # a short limit is slept through and the post succeeds
    fake.queue429 = [1]; answer(mid, "short limit"); before = CLOCK.offset; tick()
    assert any("short limit" in m["content"] for m in fake.msgs(origin)) and CLOCK.offset > before
    # a long limit defers instead of blocking the loop, and the rest arrives once
    fake.queue429 = []
    route = "POST /channels/" + origin + "/messages"
    bridge_mod.BLOCKED[route] = CLOCK.time() + 60
    answer(mid, "after the limit"); started = real_time.time(); tick()
    assert real_time.time() - started < 5 and CLOCK.offset < 10, "did not sleep through a long limit"
    assert inbox("owner") and not any("after the limit" in m["content"] for m in fake.msgs(origin)), "still unread, nothing lost"
    CLOCK.offset += 61; tick()
    assert not inbox("owner") and sum("after the limit" in m["content"] for m in fake.msgs(origin)) == 1
    # renames stop at 2 per 10 minutes, and a third waits
    b.st["threads"][mid]["renames"] = []; fake.renames[origin] = []
    for word in ("A1", "A2", "A3"):
        b.st["threads"][mid]["title"] = word; b.st["threads"][mid].pop("name_want", None); sweep_tick(); CLOCK.offset += 31; sweep_tick(); CLOCK.offset += 5
    assert fake.channels[origin]["name"].endswith("A2"), fake.channels[origin]["name"]
    CLOCK.offset += 600; sweep_tick(); CLOCK.offset += 31; sweep_tick()
    assert fake.channels[origin]["name"].endswith("A3"), fake.channels[origin]["name"]
    # Discord refusing a rename does not block anything
    b.st["threads"][mid]["title"] = "A4"; b.st["threads"][mid]["renames"] = []; b.st["threads"][mid].pop("name_want", None)
    fake.renames[origin] = [CLOCK.time(), CLOCK.time()]; sweep_tick(); CLOCK.offset += 31; sweep_tick()
    assert b.st["threads"][mid].get("name_want", "").endswith("A4")
    # one rename is always kept for leaving Working, so a yellow dot cannot outlast the work
    CLOCK.offset += 700  # past the 429 the A4 refusal just caused
    t = b.st["threads"][mid]; t["title"] = "A5"; fake.renames[origin] = []; t.pop("name_want", None)
    t["runs"]["yellow"] = {"agent": "builder", "label": "PC", "t0": CLOCK.time()}; t["renames"] = [CLOCK.time()]
    sweep_tick(); CLOCK.offset += 31; sweep_tick()
    assert not fake.channels[origin]["name"].startswith("🟡"), fake.channels[origin]["name"]
    t["renames"] = []; fake.renames[origin] = []; t.pop("name_want", None); sweep_tick(); CLOCK.offset += 31; sweep_tick()
    assert fake.channels[origin]["name"].startswith("🟡"), fake.channels[origin]["name"]
    t["runs"]["yellow"]["end"], t["runs"]["yellow"]["t1"] = "done", CLOCK.time(); sweep_tick(); CLOCK.offset += 31; tick()
    assert not fake.channels[origin]["name"].startswith("🟡"), fake.channels[origin]["name"]

def test_missing_permissions():
    fake.deny_threads = True
    origin, mid = request()
    assert origin not in fake.threads()
    answer(mid, "Still reaches them."); tick()
    assert any(m["content"] == "Still reaches them." and (m["message_reference"] or {}).get("message_id") == origin for m in fake.msgs(PARENT))
    assert not inbox("owner"), "acked in degraded mode"
    assert any("Could not create a Discord thread" in m["content"] for m in fake.msgs(ALERTS))
    fake.deny_threads = False; b.st["threads"][mid].pop("thread_retry"); answer(mid, "Threads work now."); tick()
    assert origin in fake.threads() and any(m["content"] == "Threads work now." for m in fake.msgs(origin))
    assert not [m for m in fake.msgs(PARENT) if m["content"].startswith("⚪ **Queued")], "fallback status card retired"

def test_amend_and_stale_question():
    origin, mid = request()
    a = answer(mid, "Version one"); tick()
    agent_mod.amend("orchestrator", a["id"], "Version two"); tick()
    assert [m["content"] for m in fake.msgs(origin) if "Version" in m["content"]] == ["Version two"]
    long_a = answer(mid, "para " * 900); tick()
    agent_mod.amend("orchestrator", long_a["id"], "short now"); tick()
    assert sum(m["content"] == "short now" for m in fake.msgs(origin)) == 1 and not any("para para" in m["content"] for m in fake.msgs(origin))
    agent_mod.amend("orchestrator", a["id"]); tick()
    assert not [m for m in fake.msgs(origin) if "Version" in m["content"]]
    agent_mod.send("builder", "owner", "x", "not yours"); tick()
    # another agent cannot change it
    other = answer(mid, "Mine"); tick(); agent_mod.amend("builder", other["id"], "hacked"); tick()
    assert any(m["content"] == "Mine" for m in fake.msgs(origin))
    # an offline-device question disappears when the work has run, and buttons hide while offline
    work = agent_mod.send("orchestrator", "builder", "job", "job", device=MAC)
    q = question(mid, options=({"label": "Move to PC", "device": PC, "action": {"redirect": {"agent": "builder", "id": work["id"], "device": PC}}},)); tick()
    assert any(m["components"] for m in fake.msgs(origin))
    agent_mod.ack("builder", work["id"]); sweep_tick()
    assert q["id"] not in b.st["q"] and not any(m["components"] for m in fake.msgs(origin))
    q2 = question(mid, options=("Keep", {"label": "On Mac", "device": MAC})); tick()
    fleet_mod._write(tmp / f"Queue/devices/{MAC}.json", {"host": MAC, "platform": "mac", "at": 0, "repos": []})
    sweep_tick()
    card = next(m for m in fake.msgs(origin) if m["components"])
    assert [x["label"] for x in card["components"][0]["components"]] == ["Keep"]
    # a target thread deleted by hand does not jam the inbox
    a2 = answer(mid, "x"); tick(); del fake.channels[origin]; agent_mod.amend("orchestrator", a2["id"], "y"); tick()
    assert not inbox("owner")

def test_questions_settled_elsewhere():
    def cards(): return [m for c in fake.channels.values() for m in c["msgs"].values()]
    def look(): b.last_elsewhere = 0; sweep_tick()
    def settled(key): return key not in b.st["q"] and any("Settled elsewhere" in m["content"] and not m["components"] for m in cards())
    origin, mid = request()
    # (a) a reply that came from somewhere other than Discord closes the card; the asker replying to itself does not
    q = question(mid); tick()
    assert any(x.get("custom_id") == f"x:{q['id']}:-" for m in cards() for x in bridge_mod.walk_components(m["components"])), "Done elsewhere button"
    agent_mod.send("orchestrator", "builder", "note", "self", reply_to=q["id"]); look()
    assert q["id"] in b.st["q"]
    agent_mod.send("mac", "orchestrator", "The owner says go", "Go with A", reply_to=q["id"]); look()
    assert settled(q["id"]) and any("through mac: Go with A" in m["content"] for m in cards())
    # (b) a permission prompt closes once its hook has stopped waiting
    pq = agent_mod.send("sessions", "owner", "Permission: Bash in proj on host", "Answer within 8 minutes. After that the buttons do nothing.", options=[{"label": "Allow once"}, {"label": "Deny"}]); tick()
    CLOCK.offset += 8 * 60; look(); assert pq["id"] in b.st["q"]
    CLOCK.offset += 200; look(); assert settled(pq["id"])
    # (c) an interactive session asked, and the owner has typed in that session since
    mq = agent_mod.send("mac", "owner", "Unlock it?", "Unlock it?", options=[{"label": "Unlocked"}, {"label": "Later"}]); tick()
    (tmp / "Queue/mail/origins").mkdir(parents=True, exist_ok=True)
    (tmp / f"Queue/mail/origins/{mq['id']}.json").write_text(json.dumps({"host": MAC, "cwd": "/x/Elm", "root": "/x/Elm"}), encoding="utf-8")
    sd = tmp / f"Queue/sessions/{MAC}"; sd.mkdir(parents=True, exist_ok=True)
    rec = {"session_id": "s9", "host": MAC, "tool": "claude", "project": "Elm", "cwd": "/x/Elm/sub", "root": "/x/Elm", "state": "idle", "last_prompt": "done, unlocked it", "updated": CLOCK.time()}
    (sd / "s9.json").write_text(json.dumps({**rec, "prompt_at": CLOCK.time() - 50}), encoding="utf-8"); look()
    assert mq["id"] in b.st["q"], "a prompt from before the question does not count"
    (sd / "s9.json").write_text(json.dumps({**rec, "prompt_at": CLOCK.time() + 30}), encoding="utf-8"); look()
    assert settled(mq["id"]) and any("answered in the Elm session" in m["content"] for m in cards())
    # (d) the Done elsewhere button closes it and sends the agents nothing
    dq = question(mid, body="Second one?"); tick(); before = len(inbox("orchestrator"))
    msg = next(m for m in cards() if any(x.get("custom_id") == f"x:{dq['id']}:-" for x in bridge_mod.walk_components(m["components"])))
    b.dispatch("INTERACTION_CREATE", {"type": 3, "id": fake.nid(), "token": "tok", "channel_id": msg["channel_id"], "member": {"user": {"id": OWNER}}, "data": {"custom_id": f"x:{dq['id']}:-"}, "message": msg}); tick()
    assert dq["id"] not in b.st["q"] and len(inbox("orchestrator")) == before
    # (e) an agent's question about a repo: a later session on that repo is only a candidate; Gemini must say it settled it
    verdict = {"done": False, "why": "it fixed the camera, not the publish", "session": "s7"}; spawned = []
    def fake_spawn(path):
        spawned.append(json.loads(Path(path).read_text(encoding="utf-8")))
        Path(path).with_name(Path(path).stem + ".out.json").write_text(json.dumps(verdict), encoding="utf-8")
    b.triage_spawn = fake_spawn
    gq = agent_mod.send("orchestrator", "owner", "Kepler: publish v1?", "Publish v1?", repo="Kepler", options=[{"label": "Publish"}, {"label": "Not now"}], check_repo=False); tick()
    rec2 = {"session_id": "s7", "host": MAC, "tool": "claude", "project": "Kepler", "cwd": "/x/Kepler", "last_prompt": "fix the camera", "started": CLOCK.time() - 999}
    (sd / "s7.json").write_text(json.dumps({**rec2, "state": "working", "prompt_at": CLOCK.time() + 20, "updated": CLOCK.time() + 20}), encoding="utf-8"); look()
    assert gq["id"] in b.st["q"] and not spawned, "a session still working is not a candidate"
    (sd / "s7.json").write_text(json.dumps({**rec2, "state": "idle", "prompt_at": CLOCK.time() - 100, "updated": CLOCK.time() + 40}), encoding="utf-8"); look()
    assert gq["id"] in b.st["q"] and not spawned, "work from before the question is not a candidate"
    (sd / "s7.json").write_text(json.dumps({**rec2, "project": "kepler", "state": "idle", "prompt_at": CLOCK.time() + 20, "updated": CLOCK.time() + 40}), encoding="utf-8"); look(); look()
    assert gq["id"] in b.st["q"] and len(spawned) == 1 and spawned[0]["kind"] == "settled" and "Publish v1?" in spawned[0]["question"], "unrelated work leaves it open, asked once"
    verdict = {"done": True, "why": "it published v1", "session": "s7"}; CLOCK.offset += bridge_mod.SETTLE_RECHECK  # after a "no" the same question waits before it is judged again
    (sd / "s7.json").write_text(json.dumps({**rec2, "project": "kepler", "state": "idle", "last_prompt": "publish v1", "last_reply": "Published v1.", "prompt_at": CLOCK.time() + 50, "updated": CLOCK.time() + 60}), encoding="utf-8"); look(); look()
    assert len(spawned) == 2 and settled(gq["id"]) and any("finished it in a claude session on the Mac: it published v1" in m["content"] for m in cards())
    assert not b.st["settle_checks"], "checks of closed questions are dropped"
    # an interactive session's question settles on their later session in that project, no model needed
    eq = agent_mod.send("mac", "owner", "Echo Bay: unlock the Mac?", "Unlock the Mac", repo="Echo Bay", options=[{"label": "Unlocked"}, {"label": "Later"}], check_repo=False); tick()
    (sd / "s8.json").write_text(json.dumps({"session_id": "s8", "host": MAC, "tool": "claude", "project": "Echo Bay", "cwd": "/y/Echo Bay", "state": "ended", "last_prompt": "names", "started": CLOCK.time() + 30, "updated": CLOCK.time() + 90}), encoding="utf-8"); look()
    assert settled(eq["id"]) and len(spawned) == 2
    # an open card posted before the current button layout gets the new buttons once
    oq = question(mid, body="Old card?"); tick(); b.st["q"][oq["id"]]["row_v"] = 1; sweep_tick()
    assert b.st["q"][oq["id"]]["row_v"] == bridge_mod.Q_ROW_V and any(x.get("custom_id") == f"x:{oq['id']}:-" for m in cards() for x in bridge_mod.walk_components(m["components"]))

def test_sessions():
    d = tmp / f"Queue/sessions/{MAC}"; d.mkdir(parents=True); f = d / "s1.json"
    def write(**k):
        base = {"session_id": "s1", "host": MAC, "tool": "claude", "project": "Kepler", "state": "working", "last_prompt": "Do the thing", "updated": real_time.time()}
        f.write_text(json.dumps({**base, **k}), encoding="utf-8")
    write(); tick()
    card = fake.msgs(THREADS)[0]; th = card["id"]
    assert th in fake.threads() and fake.channels[th]["name"] == "Kepler: Do the thing · Mac · claude" and "Working" in card["content"]
    assert any(m["content"] == "> Do the thing" for m in fake.msgs(th))
    edits = fake.count("PATCH", f"/channels/{THREADS}/messages/")
    for _ in range(3): write(); tick()
    assert fake.count("PATCH", f"/channels/{THREADS}/messages/") == edits, "tool-use updates do not edit the card"
    write(state="needs_you", last_note="Needs permission", note_type="permission_prompt"); tick()
    assert "Needs you" in fake.msgs(THREADS)[0]["content"] and any("Needs permission" in m["content"] for m in fake.msgs(th))
    write(last_prompt="<task-notification>noise</task-notification>", state="idle"); tick()
    assert not any("noise" in m["content"] for m in fake.msgs(th) + fake.msgs(THREADS))
    write(last_prompt="Second", state="working"); tick(); write(last_prompt="Second", state="working"); tick()
    assert sum(m["content"] == "> Second" for m in fake.msgs(th)) == 1
    n = len(fake.threads()); owner_msg("hello?", cid=th); tick()
    assert len(fake.threads()) == n and not inbox("orchestrator"), "session threads are mirrors"
    write(state="ended"); tick()
    assert fake.channels[th]["archived"] and "Ended" in fake.msgs(THREADS)[0]["content"]
    # thread deleted by hand stays deleted; the card keeps working
    del fake.channels[th]; write(state="working", last_prompt="Third"); tick(); write(state="idle", last_prompt="Fourth"); tick()
    assert th not in fake.channels and b.st["sessions"][f"{MAC}/s1"].get("no_thread")
    # the session file vanishing removes the card only after the grace period
    f.unlink(); tick(); assert fake.msgs(THREADS)
    CLOCK.offset += 3601; b.last_clean = CLOCK.time(); tick()  # before the next cleaning pass, which removes the untracked card
    assert "Ended" in fake.msgs(THREADS)[0]["content"] and f"{MAC}/s1" not in b.st["sessions"], "card stays and says Ended"

def test_session_question_answer_reaches_the_orchestrator():
    """A question from a session agent (no runner reads its inbox): their typed words also go to the orchestrator, so they are acted on."""
    origin, mid = request()
    q = question(mid, options=("Done", "Later"), sender="mac"); tick()
    owner_msg("i thought i already did this, new mcp?", cid=origin); tick()
    assert any(m.get("reply_to") == q["id"] for m in inbox("mac")), "the asker still gets their words"
    fwd = [m for m in inbox("orchestrator") if "new mcp" in m["body"] and "`mac`" in m["body"]]
    assert fwd and fwd[0]["thread"] == mid, fwd
    t = b.st["threads"][mid]; assert t["to"] == "orchestrator" and t["last_mail"] == fwd[0]["id"]

def test_session_button_tap_does_not_leave_the_thread_queued():
    origin, mid = request(); agent_mod.ack("orchestrator", mid); answer(mid, "ok"); tick()
    q = question(mid, options=("Done", "Later"), sender="mac"); tick()
    press(origin, q["id"], 0); tick()
    assert b.state_word(b.st["threads"][mid]) != "QUEUED", b.state_word(b.st["threads"][mid])

def test_queued_with_no_runner_ages_out_but_owed_mail_stays_queued():
    origin, mid = request(); t = b.st["threads"][mid]
    tick()
    CLOCK.offset += bridge_mod.QUEUED_STALE + 60; tick()
    assert b.state_word(t) == "QUEUED", "mail still waiting for the orchestrator keeps the card Queued"
    agent_mod.ack("orchestrator", mid); t["mailed"] = True; tick()
    assert b.state_word(t) != "QUEUED", b.state_word(t)

def test_session_card_waits_for_a_real_prompt():
    d = tmp / f"Queue/sessions/{MAC}"; d.mkdir(parents=True)
    base = {"host": MAC, "tool": "codex", "project": "fini", "state": "idle", "updated": real_time.time()}
    (d / "t1.json").write_text(json.dumps({**base, "session_id": "t1", "last_prompt": "You are a helpful assistant. You will be presented with a user prompt, and your job"}), encoding="utf-8")
    (d / "t2.json").write_text(json.dumps({**base, "session_id": "t2", "last_prompt": ""}), encoding="utf-8")
    tick(); assert not fake.msgs(THREADS), [m["content"] for m in fake.msgs(THREADS)]
    (d / "t2.json").write_text(json.dumps({**base, "session_id": "t2", "last_prompt": "Find me flights"}), encoding="utf-8"); tick()
    assert len(fake.msgs(THREADS)) == 1 and "Find me flights" in fake.msgs(THREADS)[0]["content"]


def test_do_your_recommendations_answers_only_covered_safe_questions():
    origin, mid = request(); agent_mod.ack("orchestrator", mid)
    q1 = question(mid, "Build the fix?", ("Build it", "Skip", "Something else")); q2 = question(mid, "Deploy now?", ("Allow Git for this push", "Hold off")); tick()
    owner_msg("Do your recommendations"); tick()
    go = [m for m in inbox("orchestrator") if q1["id"] in m["body"] and q2["id"] in m["body"]]
    assert go and "answer_for_owner" in go[0]["body"] and set(b.st["grant"]["qs"]) == {q1["id"], q2["id"]}
    q3 = question(mid, "Later one?", ("Yes", "No")); tick()
    agent_mod.answer("orchestrator", q2["id"], 0, "ship it"); agent_mod.answer("orchestrator", q3["id"], 0, "not covered")
    agent_mod.answer("builder", q1["id"], 1, "not the orchestrator"); tick()
    assert all(k in b.st["q"] for k in (q1["id"], q2["id"], q3["id"])), "risky button, uncovered question and wrong agent are all refused"
    agent_mod.answer("orchestrator", q1["id"], 0, "small, reversible fix"); tick()
    assert q1["id"] not in b.st["q"] and q1["id"] not in b.st["grant"]["qs"]
    reply = next(m for m in inbox("orchestrator") if m.get("reply_to") == q1["id"]); assert reply["body"].startswith("Build it") and "small, reversible fix" in reply["body"]
    card = next(m for c in fake.channels.values() for m in c["msgs"].values() if "Build the fix?" in m["content"])
    assert card["components"] == [] and "**Answer:** Build it (recommended" in card["content"]
    CLOCK.offset += bridge_mod.GRANT_TTL + 1; agent_mod.answer("orchestrator", q2["id"], 1, "too late"); tick()
    assert q2["id"] in b.st["q"], "the go-ahead runs out"

def test_do_your_recommendations_with_nothing_open_says_so():
    owner_msg("do your recommendations"); tick()
    assert not inbox("orchestrator") and any("No open questions" in m["content"] for m in fake.msgs(PARENT))


def test_go_ahead_from_an_interactive_session_only():
    origin, mid = request(); agent_mod.ack("orchestrator", mid)
    q1 = question(mid, "Build the fix?", ("Build it", "Skip")); tick()
    agent_mod.go_ahead("builder"); tick(); assert not b.st.get("grant"), "a runner cannot give the go-ahead"
    agent_mod.go_ahead("mac", "Do your recommendations for the current threads"); tick()
    assert b.st["grant"]["qs"] == [q1["id"]] and any(q1["id"] in m["body"] for m in inbox("orchestrator"))


def test_agent_started_thread():
    a = agent_mod.send("orchestrator", "owner", "Loop finished Kepler", "All good."); tick()
    anchor = fake.msgs(PARENT)[0]; assert "Loop finished Kepler" in anchor["content"] and anchor["id"] in fake.threads()
    assert any(m["content"] == "All good." for m in fake.msgs(anchor["id"]))
    assert not status_msgs(anchor["id"]), "a message with no run is just the message"
    owner_msg("Thanks, do the next", cid=anchor["id"]); tick()
    assert inbox("orchestrator")[-1]["thread"] == a["id"]
    p = agent_mod.send("sessions", "owner", "Permission: Bash in Kepler on Mac", "Host: Mac", options=[{"label": "Allow once"}, {"label": "Deny"}]); tick()
    press(PARENT, p["id"], 0)
    reply = inbox("sessions")[-1]; assert reply["body"] == "Allow once" and reply["reply_to"] == p["id"] and reply["from"] == "owner"

def test_guards():
    owner_msg("Not the owner", author="999"); tick()
    assert not inbox("orchestrator") and not fake.threads(), "only the owner counts"
    origin, mid = request(); agent_mod.ack("orchestrator", mid)
    owner_msg("sneaky", cid=origin, author="999"); tick(); assert not inbox("orchestrator")
    # slash /run keeps working, and inside a thread it continues the thread
    b.dispatch("INTERACTION_CREATE", {"type": 2, "id": fake.nid(), "token": "tok", "channel_id": PARENT, "member": {"user": {"id": OWNER}}, "data": {"name": "run", "options": [{"name": "task", "value": "Slash job"}]}}); tick()
    assert inbox("orchestrator")[-1]["subject"] == "Slash job"
    assert any(c.get("name", "").endswith("Slash job") for c in fake.channels.values() if c["type"] == 11)
    agent_mod.ack("orchestrator", inbox("orchestrator")[-1]["id"])
    b.dispatch("INTERACTION_CREATE", {"type": 2, "id": fake.nid(), "token": "tok", "channel_id": origin, "member": {"user": {"id": OWNER}}, "data": {"name": "run", "options": [{"name": "task", "value": "Thread job"}]}}); tick()
    assert inbox("orchestrator")[-1]["thread"] == mid
    # a person renaming a thread stops state renames
    t = b.st["threads"][mid]
    b.dispatch("THREAD_UPDATE", {"id": origin, "name": "My name", "thread_metadata": {"archived": False}}); assert t.get("name_lock")
    # unknown thread ids and queue-card threads (no mail to attach to) do not lose the message
    b.st["threads"]["evt-1"] = {"title": "Card", "runs": {}, "t0": real_time.time(), "answer": "", "who": "", "origin": origin, "dthread": origin}
    b.st["dthreads"][origin] = "evt-1"
    agent_mod.ack("orchestrator", inbox("orchestrator")[-1]["id"])
    owner_msg("about this card", cid=origin); tick()
    m = inbox("orchestrator")[-1]; assert m["body"] == "about this card" and b.st["alias"][m["id"]] == "evt-1"

# ---- regressions from the review ----
def guarded_cli():
    """The agent-mail CLI, which refuses to speak as a person, and its list of agent environment names."""
    return HERE / "agent-mail.py", agent_mod.AGENT_ENV

def cli(*args, env=None):
    """agent-mail.py the way an agent runs it: no terminal, and with whatever environment is given."""
    script, agent_env = guarded_cli()
    clean = {k: v for k, v in os.environ.items() if k not in agent_env}
    return subprocess.run([sys.executable, "-X", "utf8", str(script), *args], capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL, env={**clean, **(env or {})})

def at_terminal(*args, env=None):
    """agent-mail.py with a real pseudo-terminal on stdin and stdout, as someone typing would have. POSIX only."""
    import pty
    master, slave = pty.openpty()
    script, agent_env = guarded_cli()
    clean = {k: v for k, v in os.environ.items() if k not in agent_env}
    try: return subprocess.run([sys.executable, "-X", "utf8", str(script), *args], stdin=slave, stdout=slave, stderr=subprocess.PIPE, env={**clean, **(env or {})}).returncode
    finally: os.close(slave); os.close(master)

def test_cli_cannot_speak_as_a_person():
    origin, mid = request()
    send = ["send", "--to", "builder", "--subject", "s", "--body", "b", "--reply-to", mid]
    for who in ("owner", "OWNER", "sessions"):
        for env in ({}, {"AGENT_MAIL_AGENT": "builder"}, {"AGENT_MAIL_PARENT": mid}):
            r = cli(*send[:0], "send", "--from", who, *send[1:], env=env)
            assert r.returncode == 1 and "refused" in r.stderr, (who, env, r.returncode, r.stderr)
    assert not inbox("builder"), "no forged mail"
    # the same forgery approving a permission prompt
    p = agent_mod.send("sessions", "owner", "Permission: Bash", "Host: x", needs_reply=True, options=[{"label": "Allow once"}, {"label": "Deny"}]); tick()
    r = cli("reply", "owner", p["id"], "--body", "Allow once"); assert r.returncode == 1 and "refused" in r.stderr, r.stderr
    r = cli("send", "--from", "owner", "--to", "sessions", "--subject", "s", "--body", "Allow once", "--reply-to", p["id"]); assert r.returncode == 1
    assert not inbox("sessions"), "the waiting session sees no answer"
    # the mailbox cannot be retyped to get round the check, and no new person can be made
    for args in (["register", "owner", "--host", "any", "--kind", "claude"], ["register", "OWNER", "--host", "any", "--kind", "claude"], ["register", "boss", "--host", "any", "--kind", "human"]):
        r = cli(*args); assert r.returncode == 1 and "refused" in r.stderr, (args, r.stderr)
    agents = agent_mod.load_agents(); assert agents["owner"]["kind"] == "human" and "boss" not in agents
    a = answer(mid, "first"); tick()
    r = cli("amend", "--from", "sessions", a["id"], "--text", "changed"); assert r.returncode == 1
    # agents still send as themselves, and their hops still count
    r = cli("send", "--from", "builder", "--to", "orchestrator", "--subject", "s", "--body", "b", "--reply-to", mid); assert r.returncode == 0, r.stderr
    m = next(x for x in inbox("orchestrator") if x["id"] == r.stdout.strip()); assert m["from"] == "builder" and m["hops"] == 1
    r = cli("reply", "orchestrator", m["id"], "--body", "ok"); assert r.returncode == 0, r.stderr
    # the bridge's own sends as owner still work with no terminal
    owner_msg("still me", cid=origin); tick()
    m = inbox("orchestrator")[-1]; assert m["from"] == "owner" and m["body"] == "still me" and m["hops"] == 0, m

def test_person_at_a_real_terminal():
    if os.name == "nt": return print("(no pty on Windows)")
    origin, mid = request()
    q = question(mid); tick()
    assert at_terminal("reply", "owner", q["id"], "--body", "From the terminal") == 0, "documented fallback when Discord is down"
    assert next(m for m in inbox("orchestrator") if m.get("reply_to") == q["id"])["from"] == "owner"
    assert at_terminal("send", "--from", "owner", "--to", "builder", "--subject", "s", "--body", "b", env={"CLAUDECODE": "1"}) == 1, "an agent with a pty is still an agent"
    assert not inbox("builder")

def test_hop_limit_with_runner_parent():
    # an in-process sender inside a runner turn does not get the human reset
    origin, mid = request()
    os.environ["AGENT_MAIL_PARENT"] = mid
    try:
        m = agent_mod.send("owner", "builder", "s", "b", reply_to=mid); assert m["hops"] == 1, m
        # improve-loop.py sends as owner inside the runner, where this value is stale: it must still start its own thread
        loop = agent_mod.send("owner", "orchestrator", "[loop] Improvement cycle: X", "b", needs_reply=True)
        assert loop["thread"] == loop["id"] and loop["hops"] == 0, loop
        # an agent's turn still inherits its thread and counts
        a = agent_mod.send("builder", "orchestrator", "s", "b"); assert a["thread"] == mid and a["hops"] == 1, a
    finally: os.environ.pop("AGENT_MAIL_PARENT", None)

def test_session_thread_archived_not_deleted():
    d = tmp / f"Queue/sessions/{MAC}"; d.mkdir(parents=True); f = d / "s2.json"
    f.write_text(json.dumps({"session_id": "s2", "host": MAC, "tool": "claude", "project": "Sloop", "state": "idle", "last_prompt": "Keep this prompt", "updated": real_time.time()}), encoding="utf-8"); tick()
    th = fake.msgs(THREADS)[0]["id"]
    f.unlink(); tick(); CLOCK.offset += 3601
    fake.queue429 = [1]; bridge_mod.BLOCKED["PATCH /channels/" + th] = CLOCK.time() + 60; tick()  # a rate limit defers it
    assert f"{MAC}/s2" in b.st["sessions"] and not fake.channels[th]["archived"], "tried again next pass, not dropped"
    fake.queue429 = []; CLOCK.offset += 61; tick()
    assert th in fake.channels and fake.channels[th]["archived"], "archived"
    assert any(m["content"] == "> Keep this prompt" for m in fake.msgs(th)), "prompts survive"
    assert not fake.count("DELETE", "/channels/"), "nothing deleted"
    assert "Ended" in fake.msgs(THREADS)[0]["content"] and f"{MAC}/s2" not in b.st["sessions"]

def test_failed_relay_is_visible_and_retried():
    global b
    real_send = bridge_mod.AM.send
    def broken(*a, **k): raise OSError("disk full")
    bridge_mod.AM.send = broken
    try:
        m = owner_msg("Do not lose me"); tick()
        assert not inbox("orchestrator") and m in b.st["unsent"], "kept"
        assert ("PUT", m, bridge_mod.FAIL_EMOJI) in fake.react_log, "warning reaction"
        notice = next(x for x in fake.msgs(PARENT) if "Not delivered" in x["content"]); assert "disk full" in notice["content"] and notice["message_reference"]["message_id"] == m
        # a failed thread message leaves the work's state alone, and repeats do not repost the notice
        CLOCK.offset += 31; tick(); assert sum("Not delivered" in x["content"] for x in fake.msgs(PARENT)) == 1
        assert not b.st["threads"]
    finally: bridge_mod.AM.send = real_send
    CLOCK.offset += 301; tick()
    msgs = inbox("orchestrator"); assert len(msgs) == 1 and msgs[0]["body"] == "Do not lose me", "delivered once"
    assert m not in b.st["unsent"] and ("DELETE", m, bridge_mod.FAIL_EMOJI) in fake.react_log, "warning removed"
    assert not any("Not delivered" in x["content"] for x in fake.msgs(PARENT)), "notice removed"
    assert m in fake.threads(), "it became a normal request"
    # kept across a restart, then given up on with a clear notice after a day
    bridge_mod.AM.send = broken
    try:
        m2 = owner_msg("Second"); tick()
        saved = bridge_mod.shared_state(); assert m2 in saved["unsent"]
        b = bridge_mod.Bridge(); b.st["started"] = 0; assert m2 in b.st["unsent"]
        CLOCK.offset += 86401; tick()
        assert m2 not in b.st["unsent"] and any("Please send it again" in x["content"] for x in fake.msgs(PARENT))
    finally: bridge_mod.AM.send = real_send
    # same for typing inside a thread, and for /run
    origin, mid = request(); agent_mod.ack("orchestrator", mid); before = b.st["threads"][mid].get("queued")
    bridge_mod.AM.send = broken
    try:
        m3 = owner_msg("in thread", cid=origin); tick()
        assert m3 in b.st["unsent"] and any("Not delivered" in x["content"] for x in fake.msgs(origin)) and b.st["threads"][mid].get("queued") == before, "state untouched"
        b.dispatch("INTERACTION_CREATE", {"type": 2, "id": fake.nid(), "token": "tok", "channel_id": PARENT, "member": {"user": {"id": OWNER}}, "data": {"name": "run", "options": [{"name": "task", "value": "Slash fails"}]}})
        assert any(r["d"]["content"] == "Slash fails" for r in b.st["unsent"].values())
    finally: bridge_mod.AM.send = real_send
    CLOCK.offset += 301; tick()
    assert {"Slash fails", "in thread"} <= {x["body"] for x in inbox("orchestrator")} and not b.st["unsent"]

def test_discord_reply_answers_question_card():
    origin, mid = request()
    # the shape of the card live in #orchestrator: posted in the channel, its work having no Discord thread
    q = agent_mod.send("orchestrator", "owner", "Pick", "Which one?", reply_to=mid, options=[{"label": "A"}, {"label": "B"}])
    card = fake.add_msg(PARENT, "<@100> **orchestrator** asks:\nWhich one?", components=[{"type": 1, "components": [{"type": 2, "custom_id": f"a:{q['id']}:0"}]}])
    agent_mod.ack("owner", q["id"])
    t = b.st["threads"][mid]; t["asking"] = "Pick"
    b.st["q"][q["id"]] = {"from": "orchestrator", "options": q["options"], "subject": "Pick", "body": "Which one?", "thread": mid, "msg": card["id"], "shown": [0, 1]}
    agent_mod.ack("orchestrator", mid); n = len(fake.threads())
    owner_msg("Neither, use C", reference={"message_id": card["id"]}); tick()
    reply = [m for m in inbox("orchestrator") if m.get("reply_to") == q["id"]]
    assert len(reply) == 1 and reply[0]["body"] == "Neither, use C" and reply[0]["thread"] == mid, inbox("orchestrator")
    assert len(inbox("orchestrator")) == 1, "no new request"
    assert len(fake.threads()) == n and q["id"] not in b.st["q"] and not t.get("asking")
    assert card["id"] and fake.channels[PARENT]["msgs"][card["id"]]["components"] == [] and "**Answer:** Neither" in fake.channels[PARENT]["msgs"][card["id"]]["content"]
    # replying to one card answers that one, not the newest
    q1 = question(mid, "First?", sender="orchestrator"); q2 = question(mid, "Second?"); tick()
    first = b.st["q"][q1["id"]]["msg"]
    owner_msg("about the first", cid=origin, reference={"message_id": first}); tick()
    assert next(m for m in inbox("orchestrator") if m["body"] == "about the first")["reply_to"] == q1["id"] and q2["id"] in b.st["q"]
    # a card that moves work between devices is not answered by text
    work = agent_mod.send("orchestrator", "builder", "job", "job", device=MAC)
    q3 = question(mid, options=({"label": "Move", "device": PC, "action": {"redirect": {"agent": "builder", "id": work["id"], "device": PC}}},)); tick()
    b.st["q"].pop(q2["id"]); last = b.st["threads"][mid]["last_mail"]
    owner_msg("hm", cid=origin, reference={"message_id": b.st["q"][q3["id"]]["msg"]}); tick()
    assert q3["id"] in b.st["q"] and next(m for m in inbox("orchestrator") if m["body"] == "hm")["reply_to"] == last, "still open, the text just continues the thread"

def test_titles():
    clipped = "Orchestrator discord needs to have better message handling and readability. I se"[:80]
    assert len(clipped) == 80 and bridge_mod.clean_title(clipped) == "Orchestrator discord needs to have better message handling and readability.", bridge_mod.clean_title(clipped)
    words = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar")[:80]
    assert len(words) == 80 and bridge_mod.clean_title(words).endswith("…") and not bridge_mod.clean_title(words).rstrip("…").endswith("nov"), bridge_mod.clean_title(words)
    assert bridge_mod.clean_title("A short natural title") == "A short natural title"
    assert bridge_mod.clean_title("Re: " + clipped).startswith("Orchestrator")
    long_line = "This is a very long single line request from the owner that goes on and on about what should happen next in the repo and more"
    assert bridge_mod.word_cut(long_line, 80).endswith("…") and " ".join(bridge_mod.word_cut(long_line, 80).rstrip("…").split()[-1:]) in long_line.split()
    # the new mail subject is cut at a word, and an event carrying it does not replace the owner's own title
    origin, mid = request(long_line)
    subject = inbox("orchestrator")[0]["subject"]; assert len(subject) <= 80 and subject.endswith("…") and subject.rstrip("…").split()[-1] in long_line.split(), subject
    title = b.st["threads"][mid]["title"]; assert title.startswith("This is a very long")
    b.st["threads"][mid].pop("dthread"); event("started", mid, title="Totally different mail subject"); tick()
    assert b.st["threads"][mid]["title"] == title, "event title did not overwrite"
    name = fake.channels[origin]["name"]; assert name.split(" ", 1)[1] in (bridge_mod.clean_title(title),) and len(name) <= 100, name
    # with state names off: no icon at all, and nothing renames the thread
    b.cfg["thread_state_names"] = False
    origin2, mid2 = request("Plain name please")
    assert fake.channels[origin2]["name"] == "Plain name please", fake.channels[origin2]["name"]
    event("started", mid2); event("done", mid2); tick(); sweep_tick(); CLOCK.offset += 31; sweep_tick()
    assert fake.channels[origin2]["name"] == "Plain name please" and origin2 not in fake.renames

def test_non_owner_button():
    origin, mid = request(); q = question(mid); tick(); agent_mod.ack("orchestrator", mid)
    msg = next(m for m in fake.msgs(origin) if m["components"])
    b.dispatch("INTERACTION_CREATE", {"type": 3, "id": fake.nid(), "token": "tok", "channel_id": origin, "member": {"user": {"id": "999"}}, "data": {"custom_id": f"a:{q['id']}:0"}, "message": msg})
    assert fake.responses[-1]["data"]["flags"] == 64 and "Only the server owner" in fake.responses[-1]["data"]["content"]
    assert q["id"] in b.st["q"] and not inbox("orchestrator") and next(m for m in fake.msgs(origin) if m["id"] == msg["id"])["components"], "question still open, no answer sent"

def test_discord_down_keeps_mail():
    origin, mid = request()
    fake.down = True; answer(mid, "while down")
    try: tick()
    except Exception: pass
    assert len(inbox("owner")) == 1, "mail stays unread for the terminal"
    fake.down = False; tick()
    assert not inbox("owner") and sum("while down" in m["content"] for m in fake.msgs(origin)) == 1

def test_text_helpers():
    assert bridge_mod.clean_title("Re: **Fix** <#1234567890123456> the  `pool`") == "Fix the pool"
    assert bridge_mod.clean_title("   ") == "Request" and len(bridge_mod.clean_title("word " * 80)) <= 90
    parts = bridge_mod.split_message("a" * 5000); assert all(len(p) <= 1900 for p in parts) and "".join(parts) == "a" * 5000
    assert bridge_mod.split_message("") == ["(empty)"]

# ---- concurrency ----
class FakeRuns:
    """Stands in for run_claude: blocks until released, records which message each run was given."""
    def __init__(self, go=False): self.go, self.seen, self.active, self.peak, self.lock = threading.Event(), [], 0, 0, threading.Lock(); self.go.set() if go else None
    def __call__(self, exe, prompt, mode, cwd, timeout_s, agent, via, mcp, parent="", claims=None, cancel=None, ponytail="off"):
        with self.lock: self.seen.append(parent); self.active += 1; self.peak = max(self.peak, self.active)
        try:
            while not self.go.is_set():
                if cancel is not None and cancel.is_set(): raise runner_mod.Cancelled()
                real_time.sleep(0.01)
        finally:
            with self.lock: self.active -= 1
        return 0, json.dumps({"result": "ok"}), ""
def until(fn, s=10):
    end = real_time.time() + s
    while real_time.time() < end:
        if fn(): return True
        real_time.sleep(0.01)
    return False
def make_runner(limit=2):
    global runner_mod
    runner_mod = runner_mod or load_module("mail_runner_test", "mail-runner.py")
    agent_mod.register("builder", bridge_mod.hostname(), "claude", auto=True)
    a = argparse.Namespace(state_file=str(tmp / "runner-state.json"), only=None, max_concurrent=limit, once=False, dry_run=False, max_hops=0, max_per_hour=0, max_per_day=0,
                           timeout_minutes=1, permission_mode="auto", always_reply=False, mcp=False, prompt_via="auto", no_loop=True)
    r = runner_mod.Runner(a, agent_mod); r.mine = lambda *x: True; r.engines = lambda n: ["claude"]; runner_mod.find_claude = lambda: "claude"
    runner_mod.ask_ponytail = lambda exe, text, owner_text, fallback, timeout_s=120: (fallback, "test: no side run")  # never the real claude CLI
    return r
_area = iter(range(10**6))
def mail_to_builder(subject="Task", area=None, **k):  # each task writes its own area unless a test names one
    return agent_mod.send("orchestrator", "builder", subject, "Do it" + ("" if area == "" else f"\n\nAREA: {area or 'a%d' % next(_area)}"), needs_reply=True, **k)
def finish(r, runs): runs.go.set(); assert until(lambda: not r.running)

def test_one_writer_per_area():
    r = make_runner(4); runs = FakeRuns(); runner_mod.run_claude = runs
    same = [mail_to_builder(f"S{i}", area="bridge")["id"] for i in range(2)]; loose = [mail_to_builder(f"W{i}", area="")["id"] for i in range(2)]
    r.one_pass(); assert until(lambda: len(runs.seen) == 1); r.one_pass()
    assert runs.seen == [same[0]], "one writer per named area, and unnamed work waits for every named area"
    finish(r, runs); runs.go.clear(); r.one_pass(); assert until(lambda: len(runs.seen) == 2); r.one_pass()
    assert runs.seen[1] == same[1] and len(r.running) == 1, "oldest first; unnamed work still waits"
    finish(r, runs); runs.go.clear(); r.one_pass(); assert until(lambda: len(runs.seen) == 3); r.one_pass()
    assert runs.seen[2] == loose[0] and len(r.running) == 1, "unnamed work takes the whole workspace, one at a time"
    finish(r, runs); runs.go.clear(); r.one_pass(); assert until(lambda: len(runs.seen) == 4); finish(r, runs)
    r.one_pass(); assert not inbox("builder"), "the held ones run once the area is free"

def test_owners_typed_answer_to_a_question_gets_a_reply():
    r = make_runner(2); runs = FakeRuns(go=True); runner_mod.run_claude = runs
    q = agent_mod.send("builder", "owner", "Which one?", "Which one?", needs_reply=True)
    agent_mod.reply("owner", q["id"], "Is the mcp still off?"); r.one_pass(); assert until(lambda: not r.running and not inbox("builder"))
    got = [m for m in inbox("owner") if m.get("reply_to") and m["from"] == "builder"]; assert got and got[-1]["body"].startswith("ok"), "their answer is replied to, not only filed"

def test_concurrency_limit_and_one_run_per_message():
    r = make_runner(2); runs = FakeRuns(); runner_mod.run_claude = runs
    ids = [mail_to_builder(f"T{i}")["id"] for i in range(3)]
    r.one_pass(); assert until(lambda: len(runs.seen) == 2); r.one_pass()
    assert sorted(runs.seen) == sorted(ids[:2]) and len(r.running) == 2, "the limit holds and the oldest go first"
    assert "AGENT_MAIL_PARENT" not in os.environ, "the message id never goes in the shared environment"
    runs.go.set(); assert until(lambda: not r.running)
    runs.go.clear(); r.one_pass(); assert until(lambda: len(runs.seen) == 3); finish(r, runs)
    r.one_pass(); assert sorted(runs.seen) == sorted(ids) and not inbox("builder"), "each message ran exactly once"

def test_concurrency_setting():
    r = make_runner(None); assert r.limit() == 4, "default"
    (tmp / "Queue/mail/runner.json").write_text('{"max_concurrent": 7}', encoding="utf-8"); assert r.limit() == 7, "read live"
    r.a.max_concurrent = 3; assert r.limit() == 3, "the flag wins"
    (tmp / "Queue/mail/runner.json").write_text("not json", encoding="utf-8"); r.a.max_concurrent = None; assert r.limit() == 4, "a bad file falls back"

def test_one_run_per_thread():
    r = make_runner(4); other = make_runner(4); runs = FakeRuns(); runner_mod.run_claude = runs
    first = mail_to_builder("First"); second = agent_mod.send("orchestrator", "builder", "Second", "Then this", needs_reply=True, reply_to=first["id"])
    r.one_pass(); assert until(lambda: len(runs.seen) == 1); r.one_pass(); other.one_pass()
    assert runs.seen == [first["id"]], "a second runner process on this device waits too"
    assert second["id"] in [m["id"] for m in inbox("builder")], "the waiting message is untouched"
    finish(r, runs); runs.go.clear(); r.one_pass(); assert until(lambda: len(runs.seen) == 2); assert runs.seen[1] == second["id"]; finish(r, runs)

def test_one_run_per_repo():
    for g in ("Kepler", "Fish"): (tmp / g).mkdir()
    r = make_runner(4); other = make_runner(4); runs = FakeRuns(); runner_mod.run_claude = runs
    a1 = mail_to_builder("A1", repo="Kepler"); a2 = mail_to_builder("A2", repo="kepler", check_repo=False); fish = mail_to_builder("F", repo="Fish")
    r.one_pass(); assert until(lambda: len(runs.seen) == 2); r.one_pass(); other.one_pass(); real_time.sleep(0.05)
    assert sorted(runs.seen) == sorted([a1["id"], fish["id"]]), "same repo waits (any case), a different repo runs"
    finish(r, runs); runs.go.clear(); r.one_pass(); assert until(lambda: len(runs.seen) == 3); finish(r, runs)
    assert not list(Path(r.claims).glob("*.claim")), "claims are released"

def test_claim_deadline_comes_from_its_holder():
    make_runner(); d = tmp / "c"; assert runner_mod.claim(d, "repo-x", 3000)
    assert runner_mod.claim(d, "repo-x", 1) is None, "a runner with a shorter timeout must not steal a longer run's claim"
    (d / "repo-x.claim").write_text("1 t 1\n", encoding="utf-8"); assert runner_mod.claim(d, "repo-x", 3000), "an expired one can be taken"
    (d / "old.claim").write_text("1 2026-11-09T00:00:00Z\n", encoding="utf-8"); assert runner_mod.claim(d, "old", 3000) is None, "claims from the old format still hold"

def test_shutdown_stops_runs_and_keeps_mail():
    r = make_runner(2); runs = FakeRuns(); runner_mod.run_claude = runs; m = mail_to_builder("Long")
    r.one_pass(); assert until(lambda: runs.seen); r.shutdown()
    assert not r.running and not list(r.claims.glob("*.claim")), "claims released"
    assert [x["id"] for x in inbox("builder")] == [m["id"]], "the message waits for the restart"

def test_wait_run_timeout_and_shutdown():
    make_runner(); slow = [sys.executable, "-c", "import time; time.sleep(30)"]
    p = subprocess.Popen(slow)
    try: assert runner_mod.wait_run(p, 0.2) is None, "a timeout returns None"
    finally: p.kill(); p.wait()
    p = subprocess.Popen(slow); stop = threading.Event(); stop.set()
    try:
        try: runner_mod.wait_run(p, 30, stop); raise AssertionError("should stop")
        except runner_mod.Cancelled: pass
    finally: p.kill(); p.wait()

def handoff(*args, env=None):
    e = {**os.environ, "SWITCHBOARD_ROOT": str(tmp), "SYNCTHING_URL": "http://127.0.0.1:1", "SYNCTHING_API_KEY": "x", **(env or {})}
    return subprocess.run([sys.executable, "-X", "utf8", str(HERE / "repo-handoff.py"), *args], capture_output=True, text=True, encoding="utf-8", env=e, timeout=60)
def test_repo_claim_separates_runs_on_one_device():
    claims = tmp / "claims"; claims.mkdir(); (tmp / "Kepler").mkdir(); (claims / "run-a.claim").write_text("x", encoding="utf-8")
    A, B = {"AGENT_MAIL_PARENT": "run-a", "AGENT_MAIL_CLAIMS": str(claims)}, {"AGENT_MAIL_PARENT": "run-b", "AGENT_MAIL_CLAIMS": str(claims)}
    assert handoff("claim", "Kepler", env=A).returncode == 0
    r = handoff("claim", "Kepler", env=B); assert r.returncode == 6 and "another run" in r.stdout, r.stdout
    assert handoff("release", "Kepler", env=B).returncode == 6, "and it cannot release another run's claim"
    assert handoff("claim", "Kepler", env=A).returncode == 0, "the holder can claim again"
    (claims / "run-a.claim").unlink(); assert handoff("claim", "Kepler", env=B).returncode == 0, "a finished run's claim passes on"

# ---- idle threads are closed by the 10-minute sweep ----
def finished_idle(text="Fix the pool in Kepler", reply="Done.", mine=False):
    origin, mid = request(text)
    if not mine: b.st["threads"][mid].pop("origin", None); b.st["threads"][mid].pop("owner_at", None)  # an agent-started thread: the idle sweep may close it
    event("started", mid); agent_mod.ack("orchestrator", mid); event("done", mid); answer(mid, reply); tick(); sweep_tick()
    assert b.state_word(b.st["threads"][mid]) == "DONE", b.state_word(b.st["threads"][mid]); return origin, mid
def idle(minutes): CLOCK.offset += minutes * 60; b.last_clean = 0; tick()  # jump ahead and let one cleaning pass run
def closed(th): return bool(fake.channels[th].get("archived"))

def test_idle_finished_request_thread_closes_after_20_minutes():
    origin, mid = finished_idle(); idle(19); assert not closed(origin), "19 idle minutes is too early"
    idle(2); assert closed(origin) and b.st["threads"][mid].get("archived"), "closed after 20 idle minutes"
    assert origin in fake.channels and fake.channels[origin]["msgs"] and not fake.channels[origin].get("locked"), "archived only: nothing deleted or locked"
    n = fake.count("PATCH", f"/channels/{origin}"); idle(30); assert fake.count("PATCH", f"/channels/{origin}") == n, "a closed thread is left alone"
    answer(mid, "One more thing."); tick(); assert not closed(origin), "a new reply reopens it"

def test_owner_thread_stays_until_close_and_is_kept_unread():
    origin, mid = finished_idle(mine=True); b.st["threads"][mid]["suggest"] = dict(SUG); idle(30); CLOCK.offset += 3 * 3600; idle(1)
    assert not closed(origin), "a thread the owner started is never closed for them"
    fake.channels[origin]["archived"] = True; b.st["threads"][mid]["archived"] = True; n = len(fake.msgs(origin)); idle(10)
    assert not closed(origin) and len(fake.msgs(origin)) == n, "hidden by Discord: reopened by a message sent and deleted at once"
    assert fake.count("DELETE", f"/channels/{origin}/messages/") >= 1
    k = fake.count("DELETE", f"/channels/{origin}/messages/"); idle(10); assert fake.count("DELETE", f"/channels/{origin}/messages/") == k, "not pinged again within the day"
    CLOCK.offset += 24 * 3600; idle(1); assert fake.count("DELETE", f"/channels/{origin}/messages/") == k, "finished and untouched for a day: visible and no longer fresh, not pinged"
    assert not closed(origin), "and the owner's thread is not archived by the idle sweep"

def test_idle_sweep_counts_from_the_last_message():
    origin, mid = finished_idle(); idle(15); owner_msg("thanks", cid=origin); tick()
    agent_mod.ack("orchestrator", inbox("orchestrator")[-1]["id"]); tick(); idle(10)
    assert not closed(origin), "The owner posted 10 minutes ago"
    o2, m2 = finished_idle("Second job"); at = b.st["threads"][m2].get("active_at"); idle(15); fake.channels[o2]["last_at"] = CLOCK.time(); idle(10)
    assert b.st["threads"][m2].get("active_at") == at and not closed(o2), "something was posted 10 minutes ago, even if the bridge never counted it as activity"
    idle(11); assert closed(o2), "and it closes once that post is 20 minutes old"

def test_idle_done_thread_stays_open_with_only_an_active_work_row_or_question_entry():
    origin, mid = finished_idle(); CLOCK.offset += 45 * 60; real = bridge_mod.fleet.active_work
    bridge_mod.fleet.active_work = lambda **k: [{"thread": mid}]
    try: b.sweep_idle(CLOCK.time()); assert not closed(origin), "an active-work row is unfinished work"
    finally: bridge_mod.fleet.active_work = real
    b.st["q"]["k1"] = {"thread": mid}; assert not b.st["threads"][mid].get("asking") and b.state_word(b.st["threads"][mid]) == "DONE"
    b.sweep_idle(CLOCK.time()); assert not closed(origin), "a question is still open in it, though the thread does not say so"
    del b.st["q"]["k1"]; b.sweep_idle(CLOCK.time()); assert closed(origin), "closed once neither is left"

def keep_pings(th): return fake.count("DELETE", f"/channels/{th}/messages/")
def test_owner_finished_thread_untouched_a_day_stays_open_without_pings():
    origin, mid = finished_idle(mine=True); b.st["threads"][mid]["suggest"] = dict(SUG); CLOCK.offset += 25 * 3600; now = CLOCK.time(); n = keep_pings(origin)
    b.sweep_idle(now); b.sweep_keep(now)
    assert keep_pings(origin) == n and not closed(origin), "no keep-visible ping, still open"
    b.st["threads"][mid]["closed"] = True; b.sweep_idle(now); assert closed(origin), "archived once the owner closed it"

def test_owner_old_finished_thread_hidden_is_restored():
    origin, mid = finished_idle(mine=True); b.st["threads"][mid]["suggest"] = dict(SUG); CLOCK.offset += 25 * 3600; now = CLOCK.time(); t = b.st["threads"][mid]
    fake.channels[origin]["archived"] = True; t["archived"] = True; n = keep_pings(origin)
    b.sweep_idle(now); b.sweep_keep(now)
    assert not closed(origin) and keep_pings(origin) == n + 1 and not t.get("archived"), "restored by one ping"
    b.sweep_idle(now); b.sweep_keep(now); assert keep_pings(origin) == n + 1, "no further ping"

def test_owner_closed_thread_hidden_stays_archived():
    origin, mid = finished_idle(mine=True); CLOCK.offset += 25 * 3600; now = CLOCK.time(); t = b.st["threads"][mid]
    fake.channels[origin]["archived"] = True; t["archived"] = True; t["closed"] = True; n = keep_pings(origin)
    b.sweep_idle(now); b.sweep_keep(now); assert closed(origin) and keep_pings(origin) == n, "closed by the owner: left archived"

def test_audit_reopens_agent_thread_with_unfinished_work():
    origin, mid = finished_idle(); t = b.st["threads"][mid]; now = CLOCK.time(); real = bridge_mod.fleet.active_work
    fake.channels[origin]["archived"] = True; t["archived"] = True; n = len(fake.msgs(origin))
    bridge_mod.fleet.active_work = lambda **k: [{"thread": mid}]
    try: b.sweep_audit(now)
    finally: bridge_mod.fleet.active_work = real
    assert not closed(origin) and len(fake.msgs(origin)) == n, "reopened without a message"

def test_audit_leaves_finished_or_closed_agent_thread_archived():
    origin, mid = finished_idle(); t = b.st["threads"][mid]; now = CLOCK.time()
    fake.channels[origin]["archived"] = True; t["archived"] = True; b.sweep_audit(now); assert closed(origin), "nothing unfinished: stays archived"
    o2, m2 = finished_idle("Second job"); t2 = b.st["threads"][m2]; real = bridge_mod.fleet.active_work
    fake.channels[o2]["archived"] = True; t2["archived"] = True; t2["closed"] = True
    bridge_mod.fleet.active_work = lambda **k: [{"thread": m2}]
    try: b.sweep_audit(now)
    finally: bridge_mod.fleet.active_work = real
    assert closed(o2), "closed: never reopened"

SUG = {"for": "x", "items": [{"label": "Next", "say": "next"}]}
def test_stale_waiting_flag_after_runs_end_shows_done():
    origin, mid = finished_idle(); t = b.st["threads"][mid]; t1 = max(r["t1"] for r in t["runs"].values())
    t["waiting"] = "x"; t.pop("waiting_at", None); assert b.state_word(t) == "DONE", "legacy flag without waiting_at"
    t["waiting_at"] = t1 - 60; assert b.state_word(t) == "DONE", "older than the last end"
    t["waiting_at"] = t1 + 60; assert b.state_word(t) == "WAITING", "newer than the last end"

def test_bare_question_in_finished_thread_goes_to_the_orchestrator():
    b.cfg["quick_status"] = True; origin, mid = finished_idle(); th = b.st["threads"][mid]["dthread"]; n = len(inbox("orchestrator"))
    owner_msg("?", cid=th); CLOCK.offset += 5; tick(); tick()
    box = inbox("orchestrator"); assert len(box) == n + 1 and box[-1]["body"].startswith("?"), "finished thread: the orchestrator gets it"
    o2, m2 = request("Second job"); t2 = b.st["threads"][m2]; event("started", m2); tick()
    k = len(inbox("orchestrator")); owner_msg("?", cid=t2["dthread"]); CLOCK.offset += 5; tick(); tick()
    assert len(inbox("orchestrator")) == k, "running thread: quick status, no new mail"
    assert any(m["content"] != "?" and m["id"] for m in fake.msgs(t2["dthread"])[-2:])

def test_owner_ended_thread_with_only_close_is_archived():
    origin, mid = finished_idle(mine=True); assert b.st["threads"][mid].get("close_msg"), "Close placed"
    o2, m2 = finished_idle("Second job", mine=True); b.st["threads"][m2]["suggest"] = dict(SUG); b.st["threads"][m2]["row_sig"] = None
    o3, m3 = finished_idle("Third job", mine=True); b.st["q"]["k1"] = {"thread": m3}
    idle(21); assert closed(origin) and b.st["threads"][mid].get("archived"), "ended Close-only: archived"
    assert not closed(o2), "a follow-up button keeps it"; assert not closed(o3), "an open question keeps it"
    del b.st["q"]["k1"]

def test_thread_the_owner_wrote_in_with_follow_ups_stays_open_until_closed():
    origin, mid = finished_idle(); t = b.st["threads"][mid]; assert not t.get("origin"); t["suggest"] = dict(SUG)
    CLOCK.offset += 3 * 3600; now = CLOCK.time(); t["owner_at"] = now - 3600
    b.sweep_idle(now); assert not closed(origin), "the owner wrote in it an hour ago: the idle sweep keeps it"
    b.maybe_archive(mid, t, now); assert not closed(origin) and not t.get("archived"), "and so does maybe_archive"
    CLOCK.offset += 24 * 3600; now = CLOCK.time(); t["owner_at"] = now - 25 * 3600; b.sweep_idle(now); b.maybe_archive(mid, t, now)
    assert not closed(origin) and not t.get("archived"), "25 hours since the owner wrote: still open"
    t["closed"] = True; b.sweep_idle(now); assert closed(origin) and t.get("archived"), "archived once closed"
    o2, m2 = finished_idle("Second job"); t2 = b.st["threads"][m2]; t2["suggest"] = dict(SUG); CLOCK.offset += 26 * 3600; now = CLOCK.time(); t2["owner_at"] = now - 25 * 3600
    b.maybe_archive(m2, t2, now); assert not closed(o2), "maybe_archive keeps it too"
    t2["closed"] = True; b.maybe_archive(m2, t2, now); assert closed(o2) and t2.get("archived"), "and archives it once closed"

def test_owner_old_finished_thread_with_an_open_question_stays_and_is_pinged():
    origin, mid = finished_idle(mine=True); b.st["q"]["k1"] = {"thread": mid}; CLOCK.offset += 25 * 3600; now = CLOCK.time()
    b.sweep_idle(now); assert not closed(origin), "a question is open in it"
    fake.channels[origin]["archived"] = True; b.st["threads"][mid]["archived"] = True; n = keep_pings(origin)
    b.sweep_idle(now); b.sweep_keep(now); assert keep_pings(origin) == n + 1 and not closed(origin), "hidden with a question open: reopened by a ping"
    del b.st["q"]["k1"]

def test_continue_thread_records_owner_at():
    origin, mid = finished_idle(); t = b.st["threads"][mid]; t.pop("owner_at", None)
    b.continue_thread(mid, "thanks"); assert t.get("owner_at") == CLOCK.time() or abs(t["owner_at"] - CLOCK.time()) < 5, t.get("owner_at")

def ended_badly(kind, text="Fix the pool in Kepler"):
    origin, mid = request(text); b.st["threads"][mid].pop("origin", None); b.st["threads"][mid].pop("owner_at", None)  # agent-started: the idle sweep may close it
    event("started", mid); agent_mod.ack("orchestrator", mid); event(kind, mid); tick(); sweep_tick()
    assert b.state_word(b.st["threads"][mid]) == {"failed": "FAILED", "cutoff": "CUT OFF"}[kind], b.state_word(b.st["threads"][mid]); return origin, mid

def test_idle_failed_or_cut_off_thread_closes_after_24_hours():
    for kind in ("failed", "cutoff"):
        origin, mid = ended_badly(kind, f"Job that {kind}"); idle(23 * 60 + 50); assert not closed(origin), "under 24 idle hours is too early"
        idle(11); assert closed(origin) and b.st["threads"][mid].get("archived"), "closed after 24 idle hours"

def test_idle_failed_thread_stays_open_with_unfinished_mail_or_a_question():
    origin, mid = ended_badly("failed"); sub = agent_mod.send("orchestrator", "builder", "Retry it", "b", reply_to=mid); tick()
    idle(25 * 60); assert not closed(origin), "mail still waits for the builder"
    o2, m2 = ended_badly("cutoff", "Second job"); question(m2); tick(); assert any(x.get("thread") == m2 for x in b.st["q"].values())
    idle(25 * 60); assert not closed(o2), "a question is open in it"

def test_idle_waiting_thread_stays_open():
    origin, mid = finished_idle(); t = b.st["threads"][mid]; t["waiting"] = True; t["waiting_at"] = CLOCK.time() + 60; assert b.state_word(t) == "WAITING", b.state_word(t)
    CLOCK.offset += 25 * 3600; b.sweep_idle(CLOCK.time()); assert not closed(origin), "waiting work is not closed at any age"

def close_attempts(th): return sum(1 for m, p, body in fake.calls if m == "PATCH" and p == f"/channels/{th}" and (body or {}).get("archived"))
def refuse_idle_close(th, status=403, code=50013): fake.fail = lambda m, p, body: Err(status, code, {"code": code, "message": "no"}) if m == "PATCH" and p == f"/channels/{th}" and (body or {}).get("archived") else None

def test_idle_refused_close_stops_after_a_few_tries():
    origin, mid = finished_idle(); out, restore = logged(); tries = bridge_mod.CLOSE_REFUSED_TRIES
    try:
        refuse_idle_close(origin); idle(25)
        for _ in range(tries + 4): idle(10)
        assert not closed(origin) and b.st["idle_refused"][origin] == tries, b.st.get("idle_refused")
        said = [x for x in out if "idle thread not closed" in x or "gave up closing" in x]; assert len(said) == tries and "gave up" in said[-1] and "gave up" not in said[0], said
        n = close_attempts(origin); idle(30); assert close_attempts(origin) == n, "not tried again"
    finally: restore(); fake.fail = None
    fake.channels[origin]["archived"] = True; idle(10); assert origin not in b.st["idle_refused"], "the count goes with the thread"

def test_idle_refusal_count_clears_on_success_and_ignores_outages():
    origin, mid = finished_idle(); refuse_idle_close(origin, 500, 0); idle(25); idle(10); assert not closed(origin) and not b.st["idle_refused"], "a 5xx is not a refusal"
    refuse_idle_close(origin); idle(10); assert b.st["idle_refused"][origin] == 1; fake.fail = None
    idle(10); assert closed(origin) and not b.st["idle_refused"], "closed: nothing left to count"

def test_idle_request_thread_stays_open_with_unfinished_mail():
    origin, mid = finished_idle(); sub = agent_mod.send("orchestrator", "builder", "Build it", "b", reply_to=mid); tick()
    idle(45); assert not closed(origin), "mail still waits for the builder"
    agent_mod.ack("builder", sub["id"]); tick(); sweep_tick(); idle(1); assert closed(origin), "closed once that mail is finished"

def test_idle_request_thread_stays_open_while_running_failed_or_unread():
    origin, mid = request(); event("started", mid); tick(); idle(45); assert not closed(origin), "a run is going"
    agent_mod.ack("orchestrator", mid); event("failed", mid); tick(); sweep_tick(); idle(45); assert not closed(origin), "a failed thread stays in view"
    o2, m2 = finished_idle("Second job"); b.open_mail = lambda: None; idle(45); assert not closed(o2), "inboxes unreadable: nothing is closed"

def test_idle_request_thread_stays_open_with_an_open_question():
    origin, mid = finished_idle(); q = question(mid); tick(); assert any(x.get("thread") == mid for x in b.st["q"].values())
    idle(45); assert not closed(origin), "a question is open in it"
    press(origin, next(k for k, x in b.st["q"].items() if x.get("thread") == mid), 0); tick()
    for m in inbox("orchestrator"): event("started", mid, ref=m["id"]); agent_mod.ack("orchestrator", m["id"]); event("done", mid, ref=m["id"])  # the answer's run
    tick(); sweep_tick(); idle(19); assert not closed(origin), "the answer counts as activity"
    idle(21); assert not closed(origin), "the owner tapped in it: kept in sight for a day"
    CLOCK.offset += 24 * 3600; idle(1); assert not closed(origin), "the owner's tap keeps it until the owner closes it"
    b.st["threads"][mid]["closed"] = True; idle(1); assert closed(origin), "closed once the owner closes it, answered and idle"

def test_idle_session_thread_closes_after_60_minutes():
    th = old_session("s1", "scratch · Mac · claude", ["Do it"]); session_file(); request(); fake.channels[th]["last_at"] = CLOCK.time()
    idle(59); assert not closed(th), "59 idle minutes is too early"
    idle(2); assert closed(th) and b.st["sessions"][f"{MAC}/s1"].get("archived") and th in fake.channels, "archived after 60, not deleted"

def test_idle_unknown_thread_waits_60_minutes_and_other_channels_are_left():
    finished_idle(); mine = fake.add_msg(PARENT, "by hand")["id"]; other = fake.add_msg(REPORTS, "elsewhere")["id"]
    for cid, parent in ((mine, PARENT), (other, REPORTS)): fake.channels[cid] = {"type": 11, "msgs": {}, "name": "x", "parent": parent, "archived": False, "last_at": CLOCK.time()}
    idle(25); assert not closed(mine), "a thread the bridge does not know gets 60 minutes, not 20"
    idle(40); assert closed(mine) and not closed(other), "only #orchestrator and #threads are tidied"

def test_idle_sweep_closes_nothing_without_thread_state():
    th = old_session("s1", "scratch · Mac · claude", ["Do it"]); fake.channels[th]["last_at"] = CLOCK.time(); assert not b.st["threads"]
    idle(120); assert not closed(th) and not fake.count("GET", "/threads/active"), "no state to judge by: nothing is asked or closed"

def test_idle_sweep_runs_with_the_ten_minute_pass_and_is_quiet_on_rate_limits():
    origin, mid = finished_idle(); CLOCK.offset += 25 * 60; n = fake.count("GET", "/threads/active"); out, restore = logged()
    try:
        fake.fail = lambda m, p, body: blocked429() if p.endswith("/threads/active") else None
        b.last_clean = 0; tick(); tick(3); assert fake.count("GET", "/threads/active") == n + 1 and not closed(origin), "one try per pass"
        assert not fake.msgs(ALERTS) and not any("idle" in x.lower() for x in out), out
    finally: restore(); fake.fail = None
    bridge_mod.BLOCKED.clear(); sweep(); assert closed(origin), "the next pass closes it"

# ---- Discord's automatic notices ----
def notice(kind, cid=PARENT, author=BOT, guild=GUILD):
    m = fake.add_msg(cid, "", author=author, type=kind)
    b.dispatch("MESSAGE_CREATE", {"id": m["id"], "channel_id": cid, "guild_id": guild, "author": {"id": author}, "content": "", "attachments": [], "type": kind})
    return m["id"]

def test_system_notices_deleted():
    origin, mid = request()
    created = notice(18); pinned = notice(6, author=OWNER)
    assert created not in fake.channels[PARENT]["msgs"] and pinned not in fake.channels[PARENT]["msgs"]
    assert origin in fake.channels[PARENT]["msgs"], "The owner's message stays"

def test_system_notices_leave_other_messages():
    n = len(fake.msgs(PARENT)); command = notice(20); starter = notice(21); text = notice(0, author=OWNER)
    assert len(fake.msgs(PARENT)) == n + 3 and not fake.count("DELETE", "/messages/"), "commands, thread starters and text are never touched"
    elsewhere = notice(18, guild="999"); assert elsewhere in fake.channels[PARENT]["msgs"], "only this server"

def test_undeletable_system_notice_is_tried_once():
    a = notice(1); assert fake.count("DELETE", "/messages/") == 1, "Discord refuses it"
    assert a in fake.channels[PARENT]["msgs"] and 1 in b.st["sys_undeletable"]
    notice(1); assert fake.count("DELETE", "/messages/") == 1, "not tried again"
    notice(18); assert fake.count("DELETE", "/messages/") == 2, "other types still go"

def test_rename_notice_type_4_is_never_tried_queued_or_logged():
    global b
    origin, mid = request(); out, restore = logged()
    try:
        four = [notice(4), notice(4, cid=origin, author=OWNER)]; tick(3)
        assert not fake.count("DELETE", "/messages/") and not b.sysq and 4 not in b.st.get("sys_undeletable", []), "no call, nothing queued, nothing learned"
        b.save_state(); b = bridge_mod.Bridge(); b.spawn = lambda fn: fn(); b.cfg["screenshots"] = False; b.spawn_label = lambda: None
        four.append(notice(4)); tick(); assert not fake.count("DELETE", "/messages/"), "a restart does not try it either"
        other = notice(18); assert other not in fake.channels[PARENT]["msgs"], "other notices still go"
        four.append(fake.add_msg(PROGRESS, "", type=4)["id"]); left = fake.add_msg(PROGRESS, "left over")["id"]; sweep(2)
        assert left not in fake.channels[PROGRESS]["msgs"], "the board sweep ran"
        assert not [p for p in deletes() if p.rsplit("/", 1)[1] in four], "no delete path touches a rename notice"
        assert all(any(m in c["msgs"] for c in fake.channels.values()) for m in four)
        assert not any("type 4" in x or "notice" in x.lower() for x in out), out
    finally: restore()

def test_undeletable_list_is_dropped_at_startup():
    global b
    notice(1); b.save_state()
    assert 1 in bridge_mod.shared_state().get("sys_undeletable", []), "the refusal was saved"
    b = bridge_mod.Bridge(); b.spawn = lambda fn: fn(); b.cfg["screenshots"] = False
    assert "sys_undeletable" not in b.st, "a restart forgets it"
    n = fake.count("DELETE", "/messages/"); notice(1); assert fake.count("DELETE", "/messages/") == n + 1, "so the type is tried again"
    assert 1 in b.st["sys_undeletable"]; notice(1); assert fake.count("DELETE", "/messages/") == n + 1, "and re-learned in one failed call"

def test_system_notice_permission_and_rate_limit():
    fake.deny_manage = True; notice(6, author=OWNER)
    assert any("Manage Messages" in m["content"] for m in fake.msgs(ALERTS)), "says exactly what is missing"
    fake.deny_manage = False
    bridge_mod.BLOCKED[bridge_mod.route("DELETE", f"/channels/{PARENT}/messages/1")] = CLOCK.time() + 60
    m = notice(18); assert m in fake.channels[PARENT]["msgs"] and b.sysq, "kept while limited"
    CLOCK.offset += 61; tick(); assert m not in fake.channels[PARENT]["msgs"] and not b.sysq

def test_system_notices_can_be_turned_off():
    b.cfg["delete_system_notices"] = False; m = notice(18); assert m in fake.channels[PARENT]["msgs"]

# ---- outdated bot messages ----
def test_alert_repeat_edits_in_place():
    b.alert("k", "Problem"); CLOCK.offset += 7 * 3600; b.alert("k", "Problem")
    assert len(fake.msgs(ALERTS)) == 1 and "Repeated" in fake.msgs(ALERTS)[0]["content"]

def test_failed_run_alert_stays_a_record():
    origin, mid = request(); event("failed", mid, detail="boom"); tick(); CLOCK.offset += 7 * 3600; b.last_clean = CLOCK.time(); event("failed", mid, ref="r2", detail="again"); tick()  # the sweep's part is in test_sweep_removes_resolved_and_duplicate_alerts_after_six_hours
    assert len(fake.msgs(ALERTS)) == 2 and "boom" in fake.msgs(ALERTS)[0]["content"], "outcomes are never replaced"

def test_janitor1113_failure_burst_is_one_alert():
    origin, mid = request("Fix the pool"); t = b.st["threads"][mid]
    event("failed", mid, ref="r1", agent="builder", title="Mail subject", detail="auth error"); tick()
    CLOCK.offset += 30; event("failed", mid, ref="r2", agent="orchestrator", title="Mail subject", detail="auth error again"); tick()
    ms = fake.msgs(ALERTS); assert len(ms) == 1, len(ms)
    c = ms[0]["content"]; assert "builder" in c and "orchestrator" in c and "Fix the pool" in c and len(c) < 1900, c
    CLOCK.offset += 1200; b.last_clean = CLOCK.time(); event("failed", mid, ref="r3", agent="reviewer", title="Mail subject", detail="later"); tick()
    assert len(fake.msgs(ALERTS)) == 2, "a failure 20 min later is its own message"

def test_janitor1113_loop_thread_name_has_its_date():
    b.cfg["thread_state_names"] = False
    t = {"title": "Kepler: improvement cycle (ask)", "project": "Kepler", "t0": real_time.mktime((2026, 11, 12, 12, 0, 0, 0, 0, -1))}
    assert b.thread_name(t).endswith(" · Nov 12") and len(b.thread_name(t)) <= 100, b.thread_name(t)
    t["title"] = "Kepler: " + "x" * 200 + ": improvement cycle (ask)"; assert b.thread_name(t).endswith(" · Nov 12") and len(b.thread_name(t)) <= 100
    old = {"title": "Kepler: improvement cycle (ask)", "project": "Kepler", "t0": t["t0"], "name": "Kepler: improvement cycle (ask)", "dthread": "x", "archived": False}
    b.sync_name("x", old, CLOCK.time()); b.sync_name("x", old, CLOCK.time() + 60); assert old["name"] == "Kepler: improvement cycle (ask)", "existing names stay"

def test_janitor1113_retitled_thread_is_renamed_when_its_work_ends():
    b.cfg["thread_state_names"] = False
    origin, mid = request("Old plan"); t = b.st["threads"][mid]; agent_mod.ack("orchestrator", mid)
    event("started", mid); event("done", mid); answer(mid, "Done."); tick(); settle(); sweep_tick()
    assert fake.channels[origin]["name"] == "Old plan"
    owner_msg("Build the brand new thing", cid=origin); tick(); agent_mod.ack("orchestrator", inbox("orchestrator")[-1]["id"]); event("started", mid, ref="r9"); tick()
    assert t["title"] == "Build the brand new thing" and t.get("retitle")
    CLOCK.offset += 700; sweep_tick(); CLOCK.offset += 31; sweep_tick()
    assert fake.channels[origin]["name"] == "Old plan", "not while it runs"
    event("done", mid, ref="r9"); tick(); CLOCK.offset += 700; sweep_tick(); CLOCK.offset += 31; sweep_tick()
    assert fake.channels[origin]["name"] == "Build the brand new thing" and "retitle" not in t, (fake.channels[origin]["name"], b.state_word(t), {k: t.get(k) for k in ("retitle", "archived", "name", "name_want", "name_lock", "renames")})

def write_session(state="needs_you", note="Needs permission", host=MAC, sid="s1", kind="permission_prompt"):
    d = tmp / f"Queue/sessions/{host}"; d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.json").write_text(json.dumps({"session_id": sid, "host": host, "tool": "claude", "project": "Kepler", "state": state, "last_prompt": "Do it", "last_note": note, "note_type": kind, "updated": real_time.time()}), encoding="utf-8")

def test_session_note_replaced_then_removed():
    write_session(); tick(); th = fake.msgs(THREADS)[0]["id"]
    notes = lambda: [m for m in fake.msgs(th) if m["content"].startswith("**Needs you:**")]
    assert len(notes()) == 1
    write_session(note="Needs a different permission"); tick()
    assert len(notes()) == 1 and "different" in notes()[0]["content"], "the newer note replaces the older"
    write_session(state="working"); tick(); assert not notes(), "gone once it is no longer true"
    assert any(m["content"] == "> Do it" for m in fake.msgs(th)), "the prompt mirror stays"
    write_session(); tick(); assert len(notes()) == 1, "and it can come back"

def test_cleanup_never_touches_protected_messages():
    origin, mid = request(); f = tmp / "Queue/report.png"; f.write_bytes(b"\x89PNG")
    status_ids = {m["id"] for m in status_msgs(origin)}
    q = question(mid); answer(mid, "Final outcome\nATTACH: Queue/report.png"); tick()
    keep = {m["id"] for m in fake.msgs(origin) if m["components"] or m["files"] or "Final outcome" in m["content"]} | {origin}
    assert len(keep) >= 4
    write_session(); tick(); th = fake.msgs(THREADS)[0]["id"]; write_session(note="Other"); tick(); write_session(state="working"); tick()
    b.alert("k", "One"); CLOCK.offset += 7 * 3600; b.alert("k", "Two")
    gone = [p for m, p, _ in fake.calls if m == "DELETE" and "/messages/" in p and "/reactions/" not in p]  # message deletes, not the state reaction on their message
    assert all(th in p or p.rsplit("/", 1)[1] in status_ids for p in gone), gone  # the status card goes once the question card holds the decision
    have = {m["id"] for m in fake.msgs(origin)} | {m["id"] for m in fake.msgs(PARENT)}
    assert keep <= have, "The owner's message, open card, answer and file all remain"

# ---- screenshots ----
def shots_on(): b.cfg.pop("screenshots", None)
def images(cid): return [m for m in fake.msgs(cid) if m["files"]]

def test_screenshot_on_question_local():
    shots_on(); origin, mid = request(); q = question(mid, sender="sessions"); tick()
    card = next(m for m in fake.msgs(origin) if m["components"]); tick()
    pics = images(origin); assert len(pics) == 1 and pics[0]["files"][0][0].startswith("screen-") and pics[0]["files"][0][1] == b"\xff\xd8fake-jpeg"
    assert pics[0]["message_reference"]["message_id"] == card["id"], "posted under its card"
    assert not list((tmp / "Queue/captures").glob("*")) if (tmp / "Queue/captures").is_dir() else True, "nothing went through the share"
    assert not list(shot_mod.LOCAL.glob("*.jpg")), "deleted once sent"
    tick(3); assert len(images(origin)) == 1, "one picture per card"

def test_screenshot_requested_from_the_blocked_device():
    shots_on(); agent_mod.register("sessions", "any", "human")
    origin, mid = request()
    q = agent_mod.send("sessions", "owner", "Permission: Bash", "Host: TEST-MAC\nProject: x\nTool: Bash", needs_reply=True, options=[{"label": "Allow once"}, {"label": "Deny"}]); tick()
    reqs = list((tmp / "Queue/captures").glob("*.req.json")); assert len(reqs) == 1
    r = json.loads(reqs[0].read_text(encoding="utf-8")); assert r["host"] == MAC
    # what the Mac's service would do
    (tmp / "Queue/captures" / f"{r['id']}.jpg").write_bytes(b"\xff\xd8mac"); reqs[0].unlink(); tick()
    all_pics = [m for c in fake.channels for m in images(c)]
    assert len(all_pics) == 1 and all_pics[0]["files"][0][1] == b"\xff\xd8mac"
    assert not list((tmp / "Queue/captures").glob("*")), "cleared from the share after upload"

def test_screenshot_service_answers_only_its_own_requests():
    q = tmp / "Queue/captures"; shot_mod.ask("aaaaaaaa", bridge_mod.hostname()); shot_mod.ask("bbbbbbbb", MAC)
    shot_mod.serve_once()
    assert (q / "aaaaaaaa.jpg").is_file() and not (q / "aaaaaaaa.req.json").exists() and (q / "bbbbbbbb.req.json").exists() and not (q / "bbbbbbbb.jpg").exists()
    (q / "evil.req.json").write_text(json.dumps({"id": "../../evil", "host": bridge_mod.hostname(), "at": CLOCK.time()}), encoding="utf-8"); shot_mod.serve_once()
    assert not (tmp / "evil.jpg").exists() and not (q / "evil.req.json").exists(), "a request's id cannot pick a path"

def test_screenshot_on_failed_and_stuck_runs():
    shots_on(); origin, mid = request(); event("started", mid); tick(); event("failed", mid, detail="boom"); tick(2)
    assert len(images(origin)) == 1, "a failed run"
    origin2, mid2 = request("Another job"); CLOCK.offset += 130; event("started", mid2, ref="r9"); tick()
    CLOCK.offset += bridge_mod.RUN_LIMIT + 10; sweep_tick(); tick()
    assert len(images(origin2)) == 1, "a run that went silent"
    sweep_tick(); tick(); assert len(images(origin2)) == 1, "only once"

def test_screenshot_session_needs_you():
    shots_on(); write_session(host=bridge_mod.hostname()); tick(2); th = fake.msgs(THREADS)[0]["id"]
    assert len(images(th)) == 1, "a local permission prompt"
    write_session(host=bridge_mod.hostname(), sid="s2", kind="remote_permission"); CLOCK.offset += 130; tick(2)
    assert not images(fake.msgs(THREADS)[1]["id"]), "a remote request already has its own card"

def test_screenshot_rate_limit():
    shots_on(); origin, mid = request()
    for i in range(5): question(mid, f"Q{i}", sender="sessions"); tick()   # all inside the per-device cooldown
    tick(); assert len(images(origin)) == 1, "the cooldown holds a burst"
    for i in range(26):  # just under an hour, each past the cooldown
        CLOCK.offset += 130; question(mid, f"later {i}", sender="sessions"); tick()
    tick(); assert len(images(origin)) == 20, f"the hourly budget, got {len(images(origin))}"
    assert len(b.st["shots"]["log"]) == 20

def test_screenshot_failure_is_one_quiet_note():
    global capture_fail
    shots_on(); capture_fail = "the screen is not readable"; origin, mid = request(); question(mid, "A", sender="sessions"); tick(2)
    notes = lambda: [m for m in fake.msgs(origin) if "No screenshot" in m["content"]]
    assert len(notes()) == 1 and "not readable" in notes()[0]["content"] and not images(origin)
    CLOCK.offset += 130; capture_fail = "the screen is locked"; question(mid, "B", sender="sessions"); tick(2)
    assert len(notes()) == 1 and "locked" in notes()[0]["content"], "replaced in place, cannot flood"
    CLOCK.offset += 130; capture_fail = None; question(mid, "C", sender="sessions"); tick(2)
    assert not notes() and len(images(origin)) == 1, "a working capture removes the note"

def test_screenshot_offline_device_and_expiry():
    shots_on(); agent_mod.register("sessions", "any", "human"); origin, mid = request()
    fleet_mod._write(tmp / "Queue/devices" / f"{MAC}.json", {"host": MAC, "platform": "mac", "at": real_time.time() - 9999, "repos": []})
    agent_mod.send("sessions", "owner", "Permission: X", "Host: TEST-MAC\nTool: X", needs_reply=True, options=[{"label": "Allow once"}]); tick()
    assert any("offline" in m["content"] for c in fake.channels for m in fake.msgs(c) if "No screenshot" in m["content"])
    assert not list((tmp / "Queue/captures").glob("*.req.json")) if (tmp / "Queue/captures").is_dir() else True
    fleet_mod._write(tmp / "Queue/devices" / f"{MAC}.json", {"host": MAC, "platform": "mac", "at": real_time.time(), "repos": []})
    CLOCK.offset += 130; agent_mod.send("sessions", "owner", "Permission: Y", "Host: TEST-MAC\nTool: Y", needs_reply=True, options=[{"label": "Allow once"}]); tick()
    assert len(list((tmp / "Queue/captures").glob("*.req.json"))) == 1
    CLOCK.offset += bridge_mod.SHOT_WAIT + 1; tick()
    assert not list((tmp / "Queue/captures").glob("*")) and any("in time" in m["content"] for c in fake.channels for m in fake.msgs(c)), "gives up and clears"

def test_screenshot_upload_retried_when_rate_limited():
    shots_on(); origin, mid = request(); bridge_mod.BLOCKED[bridge_mod.route("POST", f"/channels/{origin}/messages")] = CLOCK.time() + 20
    question(mid, sender="sessions")
    try: tick()
    except Exception: pass
    bridge_mod.BLOCKED.clear(); tick(2)
    assert len(images(origin)) == 1

def test_screenshot_size_limit():
    if os.name != "nt" and sys.platform != "darwin": return print("(no screen capture on this platform)")
    calls = []
    def grab(dest, width, quality): calls.append(width); Path(dest).write_bytes(b"x" * (50 if width <= 1280 else 500))
    old = shot_mod.grab_windows, shot_mod.grab_mac, shot_mod.MAX_BYTES
    shot_mod.grab_windows = shot_mod.grab_mac = grab; shot_mod.MAX_BYTES = 100; d = tmp / "cap.jpg"
    try:
        real_capture(d); assert calls == [1920, 1600, 1280] and d.stat().st_size == 50, "shrinks until it fits"
        shot_mod.MAX_BYTES = 10
        try: real_capture(d); raise AssertionError("should not pass")
        except shot_mod.CaptureError: pass
        assert not d.exists(), "nothing over the limit is left behind"
    finally: shot_mod.grab_windows, shot_mod.grab_mac, shot_mod.MAX_BYTES = old

def test_screenshot_files_do_not_accumulate():
    shot_mod.LOCAL.mkdir(parents=True); q = shot_mod.shared(); q.mkdir(parents=True)
    fresh, old = q / "fresh.jpg", shot_mod.LOCAL / "old.jpg"
    for f in (fresh, old): f.write_bytes(b"x")
    os.utime(old, (real_time.time() - 3600, real_time.time() - 3600)); shot_mod.clean()
    assert fresh.exists() and not old.exists()

def test_screenshots_can_be_turned_off():
    origin, mid = request(); question(mid); tick(2); assert not images(origin) and not b.shots()["wait"]

def close_card(th): return next((m for m in fake.msgs(th) if any(x.get("custom_id") == "c:thread:0" for r in m["components"] for x in r["components"])), None)
def press_close(msg, user=OWNER):
    b.dispatch("INTERACTION_CREATE", {"type": 3, "id": fake.nid(), "token": "tok", "channel_id": msg["channel_id"], "member": {"user": {"id": user}}, "data": {"custom_id": "c:thread:0"}, "message": msg})
    r = fake.responses[-1]
    if r["type"] == 7: fake.channels[msg["channel_id"]]["msgs"][msg["id"]]["components"] = r["data"]["components"]  # as Discord applies an update response
def finished(body="All fixed."):
    origin, mid = request(); event("started", mid); answer(mid, body); agent_mod.ack("orchestrator", mid); event("done", mid); tick(2)
    return origin, mid

def test_close_button_on_finished_thread_only():
    origin, mid = request(); assert not close_card(origin), "not while queued"
    event("started", mid); tick(); assert not close_card(origin), "not while working"
    answer(mid, "Halfway there."); tick(); assert not close_card(origin), "not on an interim update"
    agent_mod.ack("orchestrator", mid); event("done", mid); tick(2)
    card = close_card(origin); assert card, "shown once finished"
    assert fake.msgs(origin)[-1]["id"] == card["id"], "sits right under the final answer"

def test_answer_only_when_finished():
    origin, mid = finished("All fixed.")
    assert not status_msgs(origin) and [m["content"] for m in fake.msgs(origin)] == ["All fixed."], [m["content"] for m in fake.msgs(origin)]
    assert close_card(origin)["content"] == "All fixed.", "the button is on the answer, not a message of its own"

def wait_over(): CLOCK.offset += bridge_mod.ANSWER_WAIT + 1; tick(2)  # the time a finished run's answer is given to arrive
def said_done(since=0): return [c for c in fake.calls[since:] if c[0] in ("POST", "PATCH") and "**Done**" in str((c[2] or {}).get("content", ""))]
def posts(cid, since=0): return [c for c in fake.calls[since:] if c[0] == "POST" and c[1] == f"/channels/{cid}/messages"]
def fail_delete(status=503):
    """Every message delete fails like a Discord outage until fake.fail is cleared."""
    fake.fail = lambda m, p, body: Err(status, 0) if m == "DELETE" and "/messages/" in p else None

def test_done_before_the_answer_posts_no_done_card():
    """The runner reports done before it mails the reply, so this is the usual order of a first turn."""
    origin, mid = request(); event("started", mid); tick(); n = len(fake.calls)
    event("done", mid); tick(2)
    assert not said_done() and not posts(origin, n) and "Working" in status_msgs(origin)[0]["content"], "the card waits for the answer"
    answer(mid, "Late answer."); agent_mod.ack("orchestrator", mid); tick(2)
    assert not said_done() and [m["content"] for m in fake.msgs(origin)] == ["Late answer."], [m["content"] for m in fake.msgs(origin)]
    assert len(posts(origin, n)) == 1 and close_card(origin)["content"] == "Late answer."

def test_follow_up_turn_posts_only_its_answer():
    """A later turn in a thread whose earlier answer is still there: no new Done card with a Close button on the way."""
    origin, mid = finished("First answer."); owner_msg("Now this", cid=origin); tick()
    last = inbox("orchestrator")[-1]; event("started", mid, ref=last["id"]); tick(); n = len(fake.calls)
    event("done", mid, ref=last["id"]); tick(2); sweep_tick()
    assert not said_done(n) and not posts(origin, n) and not close_card(origin), "nothing new before the answer"
    agent_mod.reply("orchestrator", last["id"], "Second answer."); tick(2)
    assert not said_done() and len(posts(origin, n)) == 1 and not status_msgs(origin), "the answer is the only new message"
    assert close_card(origin)["content"] == "Second answer."

def test_finished_without_answer_shows_done_after_the_wait():
    origin, mid = request(); event("started", mid); event("done", mid); tick(2)
    assert not said_done(), "the answer may still be on its way"
    wait_over(); assert "Done" in status_msgs(origin)[0]["content"], "no answer came, so the outcome is shown"
    origin2, mid2 = finished("First answer."); owner_msg("Now this", cid=origin2); tick()
    last = inbox("orchestrator")[-1]; event("started", mid2, ref=last["id"]); event("done", mid2, ref=last["id"]); tick(2); wait_over()
    assert "Done" in status_msgs(origin2)[0]["content"], "an old answer does not count for a new run"

def test_finished_without_answer_and_without_thread_shows_done_after_the_wait():
    fake.deny_threads = True; origin, mid = request(); event("started", mid); event("done", mid); tick(2)
    assert not said_done()
    wait_over(); assert "Done" in status_msgs(PARENT)[0]["content"], "the channel card shows the outcome without waiting for a sweep"

def test_withdrawn_answer_brings_the_card_back():
    origin, mid = finished("All fixed."); a = b.st["threads"][mid]["last_mail"]
    b.amend({"from": "orchestrator", "body": ""}, {"op": "withdraw", "target": a}); b.render(mid); wait_over()
    assert "Done" in status_msgs(origin)[0]["content"] and not close_card(origin)

def test_withdrawing_one_of_two_answers_keeps_the_other_as_the_outcome():
    def two():
        origin, mid = request(); event("started", mid); a1 = answer(mid, "Answer A")["id"]; tick(); a2 = answer(mid, "Answer B")["id"]
        agent_mod.ack("orchestrator", mid); event("done", mid); tick(2)
        assert close_card(origin)["content"] == "Answer B" and not status_msgs(origin)
        return origin, mid, a1, a2
    def withdraw(target): b.amend({"from": "orchestrator", "body": ""}, {"op": "withdraw", "target": target}); tick(); wait_over()
    origin, mid, a1, a2 = two(); withdraw(a1)
    assert [m["content"] for m in fake.msgs(origin)] == ["Answer B"] and close_card(origin)["content"] == "Answer B", "B is still showing: no card, button kept"
    withdraw(a2)
    assert "Done" in status_msgs(origin)[0]["content"] and not close_card(origin), "the card returns once no answer is left"
    origin, mid, a1, a2 = two(); withdraw(a2)
    assert [m["content"] for m in fake.msgs(origin)] == ["Answer A"] and close_card(origin)["content"] == "Answer A", "the button moves to the answer that is left"
    assert b.st["threads"][mid]["answer"] == "Answer A"

def test_status_card_delete_failure_is_retried():
    """One Discord 5xx must not strand the card: it stays known until a delete works or Discord says it is gone."""
    origin, mid = request(); event("started", mid); tick(); card = status_msgs(origin)[0]["id"]
    fail_delete(); answer(mid, "All fixed."); agent_mod.ack("orchestrator", mid); event("done", mid); tick(2)
    assert status_msgs(origin) and b.st["threads"][mid].get("status") == card, "kept, not forgotten"
    fake.fail = None; sweep_tick()
    assert not status_msgs(origin) and not b.st["threads"][mid].get("status"), "the next sweep removes it"
    assert [m["content"] for m in fake.msgs(origin)] == ["All fixed."]

def test_channel_card_delete_failure_is_retried():
    fake.deny_threads = True; origin, mid = request(); event("started", mid); tick(); card = b.st["threads"][mid]["card"]
    fail_delete(); answer(mid, "All fixed."); agent_mod.ack("orchestrator", mid); event("done", mid); tick(2)
    assert status_msgs(PARENT) and b.st["threads"][mid].get("card") == card, "kept, not forgotten"
    fake.fail = None; CLOCK.offset += 21; sweep_tick()  # past the backoff a failed delete sets
    assert not status_msgs(PARENT) and not b.st["threads"][mid].get("card")

def test_card_move_delete_failure_never_leaves_two_cards():
    """Moving the card under the owner's latest message: a 5xx on the delete must not orphan the old card and post a second one."""
    origin, mid = request(); event("started", mid); tick(); card = status_msgs(origin)[0]["id"]
    fail_delete(); owner_msg("One more thing", cid=origin); tick(2); sweep_tick()
    assert [m["id"] for m in status_msgs(origin)] == [card] and b.st["threads"][mid].get("status") == card, "one card, still tracked"
    fake.fail = None; CLOCK.offset += 41; sweep_tick()  # past the wait a failed move sets before its next try
    cards = status_msgs(origin); assert len(cards) == 1 and cards[0]["id"] != card and fake.msgs(origin)[-1]["id"] == cards[0]["id"], "moved once the delete works"
    assert b.st["threads"][mid]["status"] == cards[0]["id"] and not b.st["threads"][mid].get("status_move")
    answer(mid, "All fixed."); agent_mod.ack("orchestrator", mid); agent_mod.ack("orchestrator", inbox("orchestrator")[-1]["id"]); event("done", mid); tick(2)
    assert not status_msgs(origin), "nothing left reading Working after the run"

def test_started_and_done_in_one_tick_shows_no_stale_state():
    """The card never saw Working, so during the wait for the answer it must not keep saying Queued or Failed."""
    origin, mid = request(); assert "Queued" in status_msgs(origin)[0]["content"]
    event("started", mid); event("done", mid); tick(2)
    assert not status_msgs(origin) and not said_done(), [m["content"] for m in status_msgs(origin)]
    answer(mid, "All fixed."); agent_mod.ack("orchestrator", mid); tick(2)
    assert [m["content"] for m in fake.msgs(origin)] == ["All fixed."] and not said_done()
    origin2, mid2 = request("Second job"); event("started", mid2); event("failed", mid2, detail="boom"); tick(2)
    assert "Failed" in status_msgs(origin2)[0]["content"]
    event("started", mid2, ref="r2"); event("done", mid2, ref="r2"); tick(2)
    assert not status_msgs(origin2), "Failed is not shown after a retry that worked"
    wait_over(); assert "Done" in status_msgs(origin2)[0]["content"]

def test_started_and_done_in_one_tick_without_thread_shows_no_stale_state():
    fake.deny_threads = True; origin, mid = request(); assert "Queued" in status_msgs(PARENT)[0]["content"]
    event("started", mid); event("done", mid); tick(2)
    assert not status_msgs(PARENT) and not b.st["threads"][mid].get("card")
    wait_over(); assert "Done" in status_msgs(PARENT)[0]["content"]

def test_retry_flag_is_dropped_when_a_new_run_starts():
    """A failed delete flags the thread for the sweep. Once a new run owns the card the flag has no more use."""
    fake.deny_threads = True; origin, mid = request(); event("started", mid); tick()
    fail_delete(); answer(mid, "All fixed."); agent_mod.ack("orchestrator", mid); event("done", mid); tick(2)
    assert b.st["threads"][mid].get("retry")
    fake.fail = None; event("started", mid, ref="r2"); tick(2)
    assert "retry" not in b.st["threads"][mid] and "Working" in status_msgs(PARENT)[0]["content"] and len(status_msgs(PARENT)) == 1

# ---- every way a card delete fails: the threaded move and the no-thread card ----
DELETE_FAILS = {"503": lambda: Err(503, 0), "403": lambda: Err(403, 50013), "long rate limit": lambda: blocked429(), "network error": lambda: urllib.error.URLError("reset")}
def fail_delete_as(kind):
    """Every message delete fails this way until fake.fail is cleared. The rate limit is longer than MAX_SLEEP, so it is not slept through."""
    make = DELETE_FAILS[kind]; fake.fail = lambda m, p, body: make() if m == "DELETE" and "/messages/" in p else None
def card_deletes(cid, card, since=0): return sum(1 for m, p, _ in fake.calls[since:] if m == "DELETE" and p == f"/channels/{cid}/messages/{card}")
def saved_thread(mid): return bridge_mod.shared_state()["threads"][mid]
def restart():
    global b
    b = bridge_mod.Bridge(); b.st["started"] = 0; b.spawn = lambda fn: fn(); b.cfg["screenshots"] = False; b.spawn_label = lambda: None

def move_fails(kind):
    """One card, still tracked, the move kept pending and saved; one move once Discord recovers."""
    origin, mid = request(); event("started", mid); tick(); card = status_msgs(origin)[0]["id"]; t = b.st["threads"][mid]
    fail_delete_as(kind); owner_msg("One more thing", cid=origin); tick(2)
    for _ in range(3):
        CLOCK.offset += 21; sweep_tick()
        assert [m["id"] for m in status_msgs(origin)] == [card] and t.get("status") == card and t.get("status_move"), (kind, t.get("status"), t.get("status_move"))
    assert card_deletes(origin, card), kind
    saved = saved_thread(mid); assert saved.get("status_move") and saved.get("status") == card, "the pending move is in state.json"
    fake.fail = None; CLOCK.offset += 601; n = len(fake.calls); sweep_tick(); sweep_tick()
    cards = status_msgs(origin); assert len(cards) == 1 and cards[0]["id"] != card and fake.msgs(origin)[-1]["id"] == cards[0]["id"], (kind, "moved once after recovery")
    assert t["status"] == cards[0]["id"] and not [k for k in ("status_move", "status_move_retry", "status_move_wait") if k in t], (kind, t)
    assert len(posts(origin, n)) == 1 and card_deletes(origin, card, n) == 1, (kind, "one delete, one new card")
def test_card_move_503_keeps_one_card_and_moves_after_recovery(): move_fails("503")
def test_card_move_403_keeps_one_card_and_moves_after_recovery(): move_fails("403")
def test_card_move_long_rate_limit_keeps_one_card_and_moves_after_recovery(): move_fails("long rate limit")
def test_card_move_network_error_keeps_one_card_and_moves_after_recovery(): move_fails("network error")

def test_card_move_unknown_message_posts_exactly_one_new_card():
    origin, mid = request(); event("started", mid); tick(); card = status_msgs(origin)[0]["id"]; t = b.st["threads"][mid]
    del fake.channels[origin]["msgs"][card]; n = len(fake.calls)  # deleted by hand
    owner_msg("One more thing", cid=origin); tick(2); sweep_tick(); CLOCK.offset += 21; sweep_tick()
    cards = status_msgs(origin); assert len(cards) == 1 and cards[0]["id"] != card and t["status"] == cards[0]["id"], "the reference is cleared and one new card posted"
    assert card_deletes(origin, card, n) == 1 and not [k for k in ("status_move", "status_move_retry", "status_move_wait") if k in t], "asked once, nothing left pending"

def test_card_move_retry_backs_off_to_ten_minutes_and_survives_a_restart():
    """A delete that keeps being refused is not sent every sweep for ever: 20 s, doubling, at most every 10 minutes."""
    origin, mid = request(); event("started", mid); tick(); card = status_msgs(origin)[0]["id"]; t = b.st["threads"][mid]
    lines, undo = logged()
    try:
        fail_delete_as("403"); owner_msg("One more thing", cid=origin); tick(2)
        assert card_deletes(origin, card) == 1 and t["status_move_wait"] == 20 and t["status_move_retry"] > CLOCK.time(), t
        for _ in range(90): CLOCK.offset += 20; sweep_tick()  # half an hour of sweeps: tries at 0, 20, 60, 140, 300, 620 and 1220 s
        n = card_deletes(origin, card); assert 5 <= n <= 8, n
        assert sum("status card not moved" in l for l in lines) == n, "one log line per try, not per sweep"
        assert t["status_move_wait"] == bridge_mod.MOVE_RETRY_MAX == 600 and [m["id"] for m in status_msgs(origin)] == [card] and t["status"] == card
        CLOCK.offset += 601; sweep_tick(); n = card_deletes(origin, card); saved = saved_thread(mid)
        assert saved["status_move"] and saved["status_move_wait"] == 600 and saved["status_move_retry"] == t["status_move_retry"], "the wait is saved with the pending move"
        restart(); t = b.st["threads"][mid]
        for _ in range(5): CLOCK.offset += 20; sweep_tick()
        assert card_deletes(origin, card) == n and t["status_move"] and t["status"] == card, "a restart does not start the tries over"
        fake.fail = None; CLOCK.offset += 601; k = len(fake.calls); sweep_tick(); sweep_tick()
        cards = status_msgs(origin); assert len(cards) == 1 and cards[0]["id"] != card and len(posts(origin, k)) == 1, "moved once"
        assert not [x for x in ("status_move", "status_move_retry", "status_move_wait") if x in t], "a move that worked clears the wait"
    finally: fake.fail = None; undo()

def test_card_move_backoff_resets_when_a_new_run_starts():
    origin, mid = request(); event("started", mid); tick(); card = status_msgs(origin)[0]["id"]; t = b.st["threads"][mid]
    fail_delete_as("403"); owner_msg("One more thing", cid=origin); tick(2); CLOCK.offset += 21; sweep_tick()
    n = card_deletes(origin, card); assert n == 2 and t["status_move_wait"] == 40, (n, t.get("status_move_wait"))
    event("started", mid, ref="r2"); tick()
    assert card_deletes(origin, card) == n + 1 and t["status_move_wait"] == 20 and t["status_move"], "tried at once, and the wait starts over"
    assert [m["id"] for m in status_msgs(origin)] == [card]

def test_card_text_change_while_a_move_is_pending_edits_the_one_card_in_place():
    origin, mid = request(); event("started", mid); tick(); card = status_msgs(origin)[0]["id"]; t = b.st["threads"][mid]
    fail_delete_as("403"); owner_msg("One more thing", cid=origin); tick(2); before = fake.channels[origin]["msgs"][card]["content"]
    event("failed", mid, detail="boom"); tick(2); now_text = fake.channels[origin]["msgs"][card]["content"]
    assert [m["id"] for m in status_msgs(origin)] == [card] and now_text != before and now_text == t["status_text"] == b.status_text(t), (before, now_text)
    assert t["status_move"] and t["status"] == card, "still waiting to move"
    fake.fail = None; CLOCK.offset += 601; sweep_tick()
    cards = status_msgs(origin); assert len(cards) == 1 and cards[0]["id"] != card and cards[0]["content"] == t["status_text"]

def channel_card_fails(kind):
    """The no-thread card: kept and flagged through every sweep while the delete fails, flag saved, removed once Discord recovers."""
    fake.deny_threads = True; origin, mid = request(); event("started", mid); tick(); t = b.st["threads"][mid]; card = t["card"]
    fail_delete_as(kind); answer(mid, "All fixed."); agent_mod.ack("orchestrator", mid); event("done", mid); tick(2)
    for _ in range(5):
        CLOCK.offset += 21; sweep_tick()
        assert t.get("card") == card and t.get("retry") and card in fake.channels[PARENT]["msgs"], (kind, t.get("card"), t.get("retry"))
    assert kind == "long rate limit" or card_deletes(PARENT, card) == 3, (kind, card_deletes(PARENT, card), "tried at 0, 21 and 63 s: backed off")  # a blocked route is not called
    assert saved_thread(mid).get("card_retry") == t["card_retry"], "the wait is in state.json"
    assert saved_thread(mid).get("retry") and saved_thread(mid).get("card") == card, "the flag is in state.json"
    restart(); t = b.st["threads"][mid]
    fake.fail = None; CLOCK.offset += 501; n = len(fake.calls); sweep_tick(); sweep_tick()
    assert card not in fake.channels[PARENT]["msgs"] and not t.get("card") and not t.get("retry") and not status_msgs(PARENT), (kind, "removed after recovery")
    assert card_deletes(PARENT, card, n) == 1
def test_channel_card_delete_503_is_retried_backed_off(): channel_card_fails("503")
def test_channel_card_delete_403_is_retried_backed_off(): channel_card_fails("403")
def test_channel_card_delete_long_rate_limit_is_retried(): channel_card_fails("long rate limit")
def test_channel_card_delete_network_error_is_retried(): channel_card_fails("network error")

def test_channel_card_rate_limit_on_the_sweep_retry_keeps_the_flag():
    """A 503 and then a long 429 on the sweep's retry: the flag was cleared before the try and never set again."""
    fake.deny_threads = True; origin, mid = request(); event("started", mid); tick(); t = b.st["threads"][mid]; card = t["card"]
    fail_delete_as("503"); answer(mid, "All fixed."); agent_mod.ack("orchestrator", mid); event("done", mid); tick(2); assert t.get("retry")
    fail_delete_as("long rate limit"); sweep_tick(); sweep_tick()
    assert t.get("retry") and t.get("card") == card and card in fake.channels[PARENT]["msgs"]
    fake.fail = None; CLOCK.offset += 501; sweep_tick()
    assert card not in fake.channels[PARENT]["msgs"] and not t.get("card") and not t.get("retry")

def test_channel_card_unknown_message_is_forgotten_without_a_retry():
    fake.deny_threads = True; origin, mid = request(); event("started", mid); tick(); t = b.st["threads"][mid]; card = t["card"]
    del fake.channels[PARENT]["msgs"][card]; n = len(fake.calls)  # deleted by hand
    answer(mid, "All fixed."); agent_mod.ack("orchestrator", mid); event("done", mid); tick(2); sweep_tick(); sweep_tick()
    assert not [k for k in ("card", "card_text", "retry") if k in t] and card_deletes(PARENT, card, n) == 1 and not status_msgs(PARENT), t

def card_edits(card, since=0): return sum(1 for m, p, _ in fake.calls[since:] if m == "PATCH" and p == f"/channels/{PARENT}/messages/{card}")
def test_channel_card_refused_delete_backs_off_to_ten_minutes_survives_a_restart_and_resets():
    fake.deny_threads = True; origin, mid = request(); event("started", mid); tick(); t = b.st["threads"][mid]; card = t["card"]
    lines, undo = logged()
    try:
        fail_delete_as("403"); answer(mid, "All fixed."); agent_mod.ack("orchestrator", mid); event("done", mid); tick(2)
        for _ in range(90): CLOCK.offset += 20; sweep_tick()
        n = card_deletes(PARENT, card); assert 5 <= n <= 8, n
        assert sum("status card not removed" in l for l in lines) == n, "one log line per try, not per sweep"
        assert t["card_wait"] == bridge_mod.MOVE_RETRY_MAX and t.get("retry") and t["card"] == card and saved_thread(mid)["card_wait"] == 600
        CLOCK.offset += 601; sweep_tick(); n = card_deletes(PARENT, card)  # a try just now: the next is 600 s away
        restart(); t = b.st["threads"][mid]
        for _ in range(5): CLOCK.offset += 20; sweep_tick()
        assert card_deletes(PARENT, card) == n, "a restart does not start the tries over"
        event("started", mid, ref="r2"); tick()
        assert "card_wait" not in t or t["card_wait"] == 20, "a new run starts the wait over"
        fake.fail = None; event("done", mid, ref="r2"); answer(mid, "Done again."); agent_mod.ack("orchestrator", inbox("orchestrator")[-1]["id"]) if inbox("orchestrator") else None
        CLOCK.offset += 601; sweep_tick(); sweep_tick()
        assert not [k for k in ("card", "card_retry", "card_wait") if k in t], t
    finally: fake.fail = None; undo()

def test_channel_card_refused_edit_backs_off():
    fake.deny_threads = True; origin, mid = request(); t = b.st["threads"][mid]; card = t["card"]
    lines, undo = logged()
    try:
        fail_card_edit("403"); event("started", mid); tick(2)
        for _ in range(90): CLOCK.offset += 20; sweep_tick()
        n = card_edits(card); assert 5 <= n <= 8, n
        assert t["card_edit_wait"] == 600 and t.get("retry") and saved_thread(mid)["card_edit_wait"] == 600
        fake.fail = None; CLOCK.offset += 601; sweep_tick()
        assert "Working" in fake.channels[PARENT]["msgs"][card]["content"] and not [k for k in ("card_edit_retry", "card_edit_wait") if k in t]
    finally: fake.fail = None; undo()

def retire_fails(kind):
    """A thread opens after a no-thread spell and the old channel card's delete fails: still tracked, retried backed off,
    across a restart, and retired once Discord takes it."""
    fake.deny_threads = True; origin, mid = request(); event("started", mid); tick(); t = b.st["threads"][mid]; card = t["card"]
    assert "Working" in fake.channels[PARENT]["msgs"][card]["content"]
    fail_delete_as(kind); fake.deny_threads = False; t.pop("thread_retry", None); answer(mid, "Threads work now."); tick()
    assert origin in fake.threads() and card in fake.channels[PARENT]["msgs"] and t["card"] == card and not t.get("card_moved"), (kind, t)
    for _ in range(90): CLOCK.offset += 20; sweep_tick()
    n = card_deletes(PARENT, card); assert kind == "long rate limit" or 5 <= n <= 8, (kind, n)
    assert not t.get("card_moved") and t["card_wait"] == 600 and saved_thread(mid)["card_retry"] == t["card_retry"], (kind, t)
    restart(); t = b.st["threads"][mid]; fake.fail = None; CLOCK.offset += 601; sweep_tick(); sweep_tick()
    assert card not in fake.channels[PARENT]["msgs"] and t.get("card_moved") and not [k for k in ("card_retry", "card_wait") if k in t], (kind, t)
def test_retire_card_503_is_retried_until_removed(): retire_fails("503")
def test_retire_card_403_is_retried_until_removed(): retire_fails("403")
def test_retire_card_long_rate_limit_is_retried_until_removed(): retire_fails("long rate limit")
def test_retire_card_network_error_is_retried_until_removed(): retire_fails("network error")
def test_retire_card_unknown_message_counts_as_retired():
    fake.deny_threads = True; origin, mid = request(); event("started", mid); tick(); t = b.st["threads"][mid]; card = t["card"]
    del fake.channels[PARENT]["msgs"][card]; n = len(fake.calls)
    fake.deny_threads = False; t.pop("thread_retry", None); answer(mid, "Threads work now."); tick(); sweep_tick(); sweep_tick()
    assert t.get("card_moved") and card_deletes(PARENT, card, n) == 1 and "card_retry" not in t

def fail_card_edit(kind):
    make = DELETE_FAILS[kind]; fake.fail = lambda m, p, body: make() if m == "PATCH" and re.fullmatch(rf"/channels/{PARENT}/messages/\d+", p) else None
def test_channel_card_failed_edit_is_not_recorded_as_written():
    """An edit Discord refused left the card on Queued while card_text said Working: never edited again, and kept through the wait."""
    fake.deny_threads = True
    for kind in ("503", "long rate limit"):
        origin, mid = request("Job " + kind); t = b.st["threads"][mid]; card = t["card"]; shown = lambda: fake.channels[PARENT]["msgs"][card]["content"]
        assert "Queued" in shown()
        fail_card_edit(kind)
        try: event("started", mid); tick(2); sweep_tick()
        finally: fake.fail = None
        assert "Queued" in shown() and t["card_text"] == shown(), (kind, t["card_text"])
        CLOCK.offset += 501; sweep_tick()
        assert "Working" in shown() and t["card_text"] == shown() and t["card"] == card, (kind, "edited once Discord takes it")
        event("done", mid); tick()
    origin, mid = request("Last job"); t = b.st["threads"][mid]; card = t["card"]
    fail_card_edit("503"); event("started", mid); tick(); event("done", mid); tick(2); fake.fail = None
    assert card not in fake.channels[PARENT]["msgs"] and not t.get("card"), "a card still reading Queued is not kept through the wait as if it said Working"

def test_recreated_thread_still_gets_its_cards():
    """The thread and the message it hung from were deleted. Buttons and answers remembered from it must not block the new thread."""
    origin, mid = finished("All fixed."); t = b.st["threads"][mid]; assert t["close_msg"][0] == origin
    del fake.channels[origin]; del fake.channels[PARENT]["msgs"][origin]
    answer(mid, "One more detail."); tick(2); new = t["dthread"]
    assert new != origin and any(m["content"] == "One more detail." for m in fake.msgs(new))
    assert all(x[0] == new for x in (t.get("close_msg"), t.get("answer_msg")) if x), "nothing points at the deleted thread"
    event("started", mid, ref="r2"); event("failed", mid, ref="r2", detail="boom"); tick(2)
    assert "Failed" in status_msgs(new)[0]["content"] and not close_card(new)
    # state left behind by an earlier bridge: the references are dropped, the card still follows the work
    t["close_msg"] = t["answer_msg"] = ["4242", "1"]; t["answered"] = True
    event("started", mid, ref="r3"); tick(2)
    assert "Working" in status_msgs(new)[0]["content"] and "close_msg" not in t
    answer(mid, "Third."); event("done", mid, ref="r3"); tick(2)
    assert not status_msgs(new) and close_card(new)["content"] == "Third."

def test_problems_and_decisions_keep_their_card():
    origin, mid = request(); event("started", mid); answer(mid, "Partly."); event("failed", mid, detail="boom"); tick(2)
    assert "Failed" in status_msgs(origin)[0]["content"], "an answer does not hide a failure"
    origin2, mid2 = request(); event("started", mid2); answer(mid2, "Partly."); event("cutoff", mid2); tick(2)
    assert "Cut off" in status_msgs(origin2)[0]["content"]
    origin3, mid3 = request(); event("started", mid3); answer(mid3, "One thing."); question(mid3); event("done", mid3); tick(2)
    assert not status_msgs(origin3) and any(" asks:" in m["content"] and m["components"] for m in fake.msgs(origin3)), "the question card holds the decision; no 'Needs you, waiting below' card repeats it"
    origin4, mid4 = request(); event("started", mid4); answer(mid4, "Halfway."); tick(2)
    assert "Working" in status_msgs(origin4)[0]["content"], "an interim answer keeps the card"

def test_close_button_not_while_work_is_out():
    origin, mid = request(); event("started", mid); answer(mid, "Sent to the builder.")
    agent_mod.send("orchestrator", "builder", "Build it", "Do it", reply_to=mid); agent_mod.ack("orchestrator", mid); event("done", mid); tick(2)
    assert b.state_word(b.st["threads"][mid]) == "QUEUED" and not close_card(origin), "delegated mail is still out"
    agent_mod.ack("builder", inbox("builder")[0]["id"]); sweep_tick(); assert close_card(origin), "appears when nothing is out"

def test_close_button_not_on_questions():
    origin, mid = request(); event("started", mid); answer(mid, "Done."); q = question(mid); agent_mod.ack("orchestrator", mid); event("done", mid); tick(2)
    assert not close_card(origin), "a question is waiting"
    press(origin, q["id"], 0); agent_mod.ack("orchestrator", inbox("orchestrator")[0]["id"]); tick(2); sweep_tick()
    origin2, mid2 = finished("Should I also do the other pool?")
    assert not close_card(origin2), "an answer ending in a question is still open"

def test_close_archives_without_locking_and_stays_closed():
    origin, mid = finished(); card = close_card(origin); n = len(fake.msgs(origin))
    press_close(card, user="999"); assert "Only the server owner" in fake.responses[-1]["data"]["content"] and not fake.channels[origin]["archived"]
    press_close(card); r = fake.responses[-1]
    assert r["type"] == 7 and r["data"]["components"] == [] and r["data"]["content"] == card["content"], "updates the card and drops the button"
    assert fake.channels[origin]["archived"] and not fake.channels[origin].get("locked") and b.st["threads"][mid]["archived"]
    sweep_tick(); sweep_tick()
    assert fake.channels[origin]["archived"] and len(fake.msgs(origin)) == n, "nothing reopens it"

def test_close_then_later_message_works():
    origin, mid = finished(); press_close(close_card(origin))
    fake.channels[origin]["archived"] = False; b.dispatch("THREAD_UPDATE", {"id": origin, "thread_metadata": {"archived": False}})  # Discord unarchives when they post
    owner_msg("One more thing", cid=origin); tick()
    assert inbox("orchestrator")[-1]["body"] == "One more thing" and inbox("orchestrator")[-1]["thread"] == mid
    assert not close_card(origin) and status_msgs(origin)[-1]["content"].split()[1] == "**Queued**"
    last = inbox("orchestrator")[-1]; event("started", mid, ref=last["id"]); agent_mod.reply("orchestrator", last["id"], "Done too."); event("done", mid, ref=last["id"]); tick(2)
    assert close_card(origin), "the button comes back after the follow-up"

def test_agent_mail_reopens_closed_thread():
    origin, mid = finished(); press_close(close_card(origin)); answer(mid, "One late detail."); tick(2)
    assert not fake.channels[origin]["archived"] and any("One late detail." in m["content"] for m in fake.msgs(origin))

def test_close_button_sits_under_the_last_attached_file():
    origin, mid = request(); (tmp / "Queue/clip.mp4").write_bytes(b"vid")
    event("started", mid); answer(mid, "Here is the video.\nATTACH: Queue/clip.mp4"); agent_mod.ack("orchestrator", mid); event("done", mid); tick(2)
    msgs = fake.msgs(origin); card = close_card(origin)
    assert card and card["files"] and card["id"] == msgs[-1]["id"], "the buttons are on the file, the last message"

def test_newer_work_in_the_same_thread_removes_old_buttons():
    origin, mid = finished(); assert close_card(origin)
    o2, m2 = request("Second job"); t2 = b.st["threads"][m2]; th2 = t2.get("dthread")
    b.st["dthreads"][origin] = m2; t2["dthread"] = origin; t2["t0"] = b.st["threads"][mid]["t0"] + 1  # the new work took over this Discord thread
    event("started", m2); b.dirty.add(m2); tick()
    assert not close_card(origin) and not b.st["threads"][mid].get("close_msg"), "the old answer's buttons go once newer work runs here"

def test_close_refused_when_work_restarted():
    origin, mid = finished(); card = close_card(origin); owner_msg("Wait, also this", cid=origin)  # not ticked: the button is still on screen
    press_close(card)
    assert "still open" in fake.responses[-1]["data"]["content"] and not fake.channels[origin]["archived"]
    assert fake.responses[-2]["type"] == 7, "the tap is answered first, before the slow inbox read (Discord allows 3 s)"
    tick(); assert not close_card(origin), "the stale button goes away"

def test_close_failure_is_reported_and_retryable():
    origin, mid = finished(); card = close_card(origin); real = fake.route
    fake.route = lambda m, p, body, files: (_ for _ in ()).throw(Err(403, 50013)) if m == "PATCH" and p == f"/channels/{origin}" else real(m, p, body, files)
    press_close(card); fake.route = real
    assert not fake.channels[origin]["archived"] and any("Could not close" in m["content"] for m in fake.msgs(origin))
    tick(); assert close_card(origin), "the button returns"

def thread_calls(cid): return [c for c in fake.calls if c[0] != "GET" and c[1].startswith(f"/channels/{cid}")]  # writes to this thread only: reading one does not reopen it, and other channels have their own periodic passes
def test_close_fails_safe_when_mail_cannot_be_read():
    """An unreadable registry or inbox file is not "no mail": no button, and a tap on an old one does not archive."""
    origin, mid = finished(); card = close_card(origin); assert card
    agent_mod.send("orchestrator", "builder", "Build it", "Do it", reply_to=mid)  # live work, not yet seen by the bridge
    reg = agent_mod.mail_root() / "agents.json"; good = reg.read_text(encoding="utf-8")
    reg.write_text(good[:len(good) // 2], encoding="utf-8")  # half-written, as a sync can leave it
    press_close(card)
    assert "stays open" in fake.responses[-1]["data"]["content"] and not fake.channels[origin]["archived"], "a tap must not archive live work"
    sweep_tick(); assert not close_card(origin) and not fake.channels[origin]["archived"], "no button while the registry is unreadable"
    reg.write_text("", encoding="utf-8"); sweep_tick(); assert not close_card(origin), "an empty registry is not readable either"
    reg.write_text(good, encoding="utf-8"); sweep_tick(); assert not close_card(origin), "readable again: the builder's mail still holds it open"
    agent_mod.ack("builder", inbox("builder")[0]["id"]); sweep_tick(); card = close_card(origin); assert card, "back once nothing is out"
    # one inbox file that cannot be read
    bad = agent_mod.mail_root() / "builder" / "inbox" / f"{agent_mod.new_id()}.json"; bad.write_text('{"id": "2026', encoding="utf-8")
    press_close(card); assert "stays open" in fake.responses[-1]["data"]["content"] and not fake.channels[origin]["archived"]
    sweep_tick(); assert not close_card(origin), "no button while an inbox file is unreadable"
    bad.unlink(); sweep_tick(); card = close_card(origin); assert card
    press_close(card); assert fake.channels[origin]["archived"], "and it closes normally afterwards"

def test_close_ignores_fyi_mail_to_agents_nobody_runs():
    """Nothing runs a non-automatic agent, so only mail asking it for a reply holds a thread open."""
    origin, mid = finished(); assert close_card(origin)
    fyi = agent_mod.send("orchestrator", "mac", "FYI", "For whoever sits at the Mac.", reply_to=mid); sweep_tick()
    assert close_card(origin) and b.state_word(b.st["threads"][mid]) == "DONE", "FYI mail to a non-automatic agent does not block Close"
    agent_mod.send("orchestrator", "sessions", "Note", "b", reply_to=mid, needs_reply=True); sweep_tick()
    assert close_card(origin), "a person's mailbox never blocks: the owner's own questions are tracked separately"
    ask = agent_mod.send("orchestrator", "mac", "Need this", "Please answer.", reply_to=mid, needs_reply=True); sweep_tick()
    assert not close_card(origin), "mail that needs a reply is still open work"
    agent_mod.ack("mac", ask["id"]); sweep_tick(); card = close_card(origin); assert card, "back once it is answered"
    assert [m["id"] for m in inbox("mac")] == [fyi["id"]]
    press_close(card); assert fake.channels[origin]["archived"], "closes with the FYI still unread"

def test_close_sends_the_pending_name():
    """Closing before the 30-second rename settles still leaves the thread with its final name."""
    origin, mid = finished(); t = b.st["threads"][mid]; final = b.thread_name(t)
    assert final.startswith(bridge_mod.STATES["DONE"][0]) and fake.channels[origin]["name"] != final, "the Done name has not gone out yet"
    press_close(close_card(origin))
    assert fake.channels[origin]["archived"] and fake.channels[origin]["name"] == final and t["name"] == final, fake.channels[origin]["name"]
    assert not t.get("name_want") and not t.get("name_late")
    n = len(thread_calls(origin)); CLOCK.offset += 700; sweep_tick(); sweep_tick()
    assert fake.channels[origin]["archived"] and len(thread_calls(origin)) == n, ("nothing reopens it afterwards", thread_calls(origin)[n:])

def test_close_with_renames_used_up_gets_its_name_later():
    """Discord allows 2 renames per 10 minutes. A close inside that limit still archives at once; the name follows."""
    for local in (True, False):  # the bridge's own count knows, or only Discord does
        origin, mid = finished(); t = b.st["threads"][mid]; final = b.thread_name(t); now = CLOCK.time()
        fake.renames[origin] = [now, now]
        if local: t["renames"] = [now, now]
        press_close(close_card(origin))
        assert fake.channels[origin]["archived"] and t["archived"], "the close itself does not wait for the rename"
        assert fake.channels[origin]["name"] != final and t.get("name_late")
        n = fake.count("PATCH", f"/channels/{origin}"); sweep_tick(); CLOCK.offset += 60; sweep_tick()
        assert fake.count("PATCH", f"/channels/{origin}") == n and fake.channels[origin]["archived"], "not opened for a rename Discord would refuse"
        CLOCK.offset += 601; sweep_tick()
        assert fake.channels[origin]["name"] == final and fake.channels[origin]["archived"] and b.st["threads"][mid].get("archived"), fake.channels[origin]["name"]
        assert not t.get("name_late") and not t.get("name_want")
        n = len(thread_calls(origin)); sweep_tick(); assert len(thread_calls(origin)) == n, "and then stays quiet"

def test_late_name_never_leaves_the_thread_open():
    origin, mid = finished(); t = b.st["threads"][mid]; now = CLOCK.time(); fake.renames[origin] = [now, now]; t["renames"] = [now, now]
    press_close(close_card(origin)); CLOCK.offset += 601; real = fake.route
    fake.route = lambda m, p, body, files: (_ for _ in ()).throw(Err(403, 50013)) if m == "PATCH" and p == f"/channels/{origin}" and "name" in (body or {}) else real(m, p, body, files)
    sweep_tick(); fake.route = real
    assert fake.channels[origin]["archived"], "a refused rename still closes the thread again"
    n = len(thread_calls(origin)); sweep_tick(); assert len(thread_calls(origin)) == n and not t.get("name_late"), "a refusal is not retried forever"

def mcp_ask(devs, **args):
    mcp = load_module("agent_mail_mcp_test", "agent-mail-mcp.py")
    mcp._fleet = type("F", (), {"where": staticmethod(lambda repo=None: [dict(d) for d in devs])})
    return mcp.call_tool(agent_mod, "orchestrator", "ask_owner", {"question": "Which device?", "devices": True, **args})
PC_D, MAC_D = {"host": "Desk-PC", "label": "PC", "ok": True}, {"host": "Mac-1", "label": "Mac", "ok": True}

def test_ask_owner_recommended_device_label():
    r = mcp_ask([MAC_D, PC_D], recommended="Desk-PC")
    assert r["options"] == ["PC (recommended)", "Mac"], r
    m = next(x for x in inbox("owner") if x["id"] == r["id"])
    assert m["body"] == "Which device?" and [o["device"] for o in m["options"]] == ["Desk-PC", "Mac-1"], m
    assert mcp_ask([MAC_D, PC_D], recommended="pc")["options"][0] == "PC (recommended)", "label works too"

def test_ask_owner_no_or_unknown_recommendation():
    assert mcp_ask([PC_D, MAC_D])["options"] == ["Run on PC", "Run on Mac"], "unchanged without one"
    r = mcp_ask([PC_D, MAC_D], recommended="Ghost")
    assert r["options"] == ["Run on PC", "Run on Mac"] and "not available" in r["note"], r
    r = mcp_ask([PC_D], recommended="Desk-PC")
    assert r == {"asked": False, "device": "Desk-PC", "note": "only one device qualifies; use it"}, r

# ---- combined conflict thread ----
def conflict(ids, sender="orchestrator", options=("Keep A", "Keep B")):
    return agent_mod.send(sender, "owner", "Conflict: both edit the pool", "Both edit the pool. I recommend A.\n\nLINKED: " + " ".join(ids), needs_reply=True, options=[{"label": o} for o in options])
def conflict_thread(m): return b.st["threads"][m["id"]]["dthread"]
def link_card(th): return [x for x in fake.msgs(th) if x["content"].startswith("**Linked threads")]

def test_conflict_one_thread_with_a_card_for_every_linked_thread():
    o1, a = request("Pool in Kepler"); o2, c = request("Pool in Fish")
    event("started", a, agent="builder"); tick(); before = len(fake.threads())
    q = conflict([a, c]); tick(); th = conflict_thread(q)
    assert len(fake.threads()) == before + 1, "one thread for the conflict"
    assert not any("LINKED" in x["content"] for ch in fake.channels.values() for x in ch["msgs"].values()), "line stripped everywhere"
    card = link_card(th); assert len(card) == 1 and fake.msgs(th)[0]["id"] == card[0]["id"], "one card, above the question"
    text = card[0]["content"]
    assert "**Pool in Kepler**" in text and "**Pool in Fish**" in text and f"<#{o1}>" in text and f"<#{o2}>" in text, text
    assert "`builder` on" in text and "Working" in text and "Queued" in text, "the lines each thread's own status uses"
    assert text.count("Working") == 1
    btn = next(x for x in fake.msgs(th) if x["components"]); assert "Both edit the pool" in btn["content"] and "LINKED" not in btn["content"]

def test_conflict_card_follows_linked_threads_in_place():
    o1, a = request("Pool in Kepler"); o2, c = request("Pool in Fish"); q = conflict([a, c]); tick(); th = conflict_thread(q)
    first = link_card(th)[0]["id"]; assert "Done" not in link_card(th)[0]["content"]
    event("started", a, agent="builder"); tick()
    assert "`builder` on" in link_card(th)[0]["content"] and "working" in link_card(th)[0]["content"], link_card(th)[0]["content"]
    event("done", a, agent="builder"); event("started", c, agent="builder"); tick()
    text = link_card(th)[0]["content"]; assert "done (" in text and "working" in text, text
    assert len(link_card(th)) == 1 and link_card(th)[0]["id"] == first, "edited, never reposted"
    assert fake.count("POST", f"/channels/{th}/messages") == len(fake.msgs(th)), "nothing else was posted"

def test_conflict_buttons_work_as_usual():
    o1, a = request("A work"); o2, c = request("C work"); agent_mod.ack("orchestrator", a); agent_mod.ack("orchestrator", c)
    q = conflict([a, c]); tick(); th = conflict_thread(q)
    press(th, q["id"], 1); tick()
    r = inbox("orchestrator")[-1]; assert r["reply_to"] == q["id"] and r["body"] == "Keep B", r
    assert q["id"] not in b.st["q"] and len(link_card(th)) == 1

def test_conflict_card_lists_unseen_thread_then_fills_it_in():
    o1, a = request("A work"); ghost = "20261110T010101Z-abcdef12"; q = conflict([a, ghost]); tick(); th = conflict_thread(q)
    assert ghost in link_card(th)[0]["content"], "an unknown thread is still listed"
    event("started", ghost, agent="builder", title="Ghost job"); tick()
    text = link_card(th)[0]["content"]; assert "Ghost job" in text and "`builder` on" in text, text

def test_conflict_closed_is_not_reopened_by_linked_work():
    o1, a = request("A work"); o2, c = request("C work"); q = conflict([a, c]); tick(); th = conflict_thread(q)
    old = link_card(th)[0]["content"]; fake.channels[th]["archived"] = True; b.st["threads"][q["id"]]["archived"] = True
    event("started", a, agent="builder"); tick(); sweep_tick()
    assert fake.channels[th]["archived"] and link_card(th)[0]["content"] == old, "linked work did not reopen it"

def test_conflict_card_recreated_if_deleted_and_stays_in_limit():
    ids = [f"20261110T0101{i:02d}Z-abcdef{i:02d}" for i in range(30)]; q = conflict(ids); tick(); th = conflict_thread(q)
    assert len(link_card(th)[0]["content"].encode("utf-16-le")) // 2 <= 2000
    mid = link_card(th)[0]["id"]; del fake.channels[th]["msgs"][mid]; event("started", ids[0], agent="builder"); tick()
    assert len(link_card(th)) == 1 and link_card(th)[0]["id"] != mid

def test_linked_line_parsing():
    assert bridge_mod.strip_linked("Text\n\nLINKED: 2026a 2026b") == ("Text", ["2026a", "2026b"])
    for body in ("LINKED: see above", "LINKED: 1a\nnot the last line", "Plain"): assert bridge_mod.strip_linked(body) == (body, []), body
    o1, a = request(); agent_mod.send("orchestrator", "owner", "S", "Body\n\nLINKED: 1a 2b", reply_to=a); tick()
    assert not any("LINKED" in x["content"] for x in fake.msgs(o1)), "plain answers are stripped too"

def test_conflict_amend_keeps_the_line_out():
    o1, a = request(); o2, c = request(); q = conflict([a, c]); tick(); th = conflict_thread(q)
    agent_mod.amend("orchestrator", q["id"], "New words\n\nLINKED: " + a + " " + c); tick()
    assert not any("LINKED" in x["content"] for x in fake.msgs(th)) and any("New words" in x["content"] for x in fake.msgs(th))

# ---- session thread names ----
def session_file(sid="s1", prompt="Do it", host=MAC, tool="claude", state="working", project="scratch-2026-11-09-fbfdd6"):
    d = tmp / f"Queue/sessions/{host}"; d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.json").write_text(json.dumps({"session_id": sid, "host": host, "tool": tool, "project": project, "state": state, "last_prompt": prompt, "updated": real_time.time()}), encoding="utf-8")
def old_session(sid, name, prompts, archived=False, tracked=True):
    card = fake.add_msg(THREADS, "⚪ **Idle**  " + "  ·  ".join(name.rsplit(" · ", 2)) + "\n-# since x")["id"]  # a real card holds the name the thread was opened with
    fake.channels[card] = {"type": 11, "msgs": {}, "name": name, "parent": THREADS, "archived": archived}
    fake.add_msg(card, "-# Read-only mirror of this session. To reach an agent, use #orchestrator.")
    for x in prompts: fake.add_msg(card, "> " + x)
    if tracked:
        b.st.setdefault("sessions", {})[f"{MAC}/{sid}"] = {"id": card, "thread": card, "text": fake.channels[THREADS]["msgs"][card]["content"], "state": "idle", "since": 1, **({"prompt": prompts[-1]} if prompts else {})}
        b.st["dthreads"][card] = "session:" + f"{MAC}/{sid}"
    return card

def test_session_name_is_first_prompt_trimmed_then_device_and_tool():
    long = "Please look at the pool physics in Kepler because the water clips through the floor and the ripple effect stops after a while"
    session_file(prompt=long); tick()
    name = fake.channels[fake.msgs(THREADS)[0]["id"]]["name"]
    assert name.endswith(" · Mac · claude") and name.startswith("Please look at the pool") and len(name.encode("utf-16-le")) // 2 <= 100, name

def test_session_name_stays_after_later_prompts():
    session_file(prompt="First ask"); tick(); th = fake.msgs(THREADS)[0]["id"]
    session_file(prompt="Second ask"); tick(); session_file(prompt="Third ask", tool="claude"); tick()
    assert fake.channels[th]["name"] == "First ask · Mac · claude" and th not in fake.renames

def test_session_machine_first_prompt_waits_for_real_words():
    session_file(prompt="<system-reminder>scratch</system-reminder>"); tick()
    assert not fake.msgs(THREADS) and not fake.threads(), "no card or thread until they have said something: an early name must be renamed, and Discord's rename notice cannot be deleted"
    session_file(prompt="Fix the login"); tick(); th = fake.msgs(THREADS)[0]["id"]; session_file(prompt="And the logout"); tick()
    assert fake.channels[th]["name"] == "Fix the login · Mac · claude" and th not in fake.renames

def test_session_migration_renames_once_from_first_mirrored_prompt():
    th = old_session("s1", "scratch-2026-11-09-fbfdd6 · Mac · claude", ["Original ask", "Later ask"]); session_file(prompt="Latest ask"); tick()
    assert fake.channels[th]["name"] == "Original ask · Mac · claude", fake.channels[th]["name"]
    assert len(fake.renames[th]) == 1
    global b
    tick(3); b = bridge_mod.Bridge(); b.st["started"] = 0; b.cfg["screenshots"] = False; tick(2)
    assert len(fake.renames[th]) == 1, "a restart does not repeat it"

def test_session_migration_keeps_a_hand_given_name():
    th = old_session("s1", "My own name", ["Original"]); session_file(prompt="Newer"); tick(2)
    assert fake.channels[th]["name"] == "My own name" and b.st["sessions"][f"{MAC}/s1"]["named"]

def test_session_migration_on_archived_thread_closes_it_again():
    th = old_session("s1", "x · Mac · claude", ["Original"], archived=True); session_file(prompt="Original", state="idle"); tick()
    assert fake.channels[th]["name"] == "Original · Mac · claude" and fake.channels[th]["archived"]

def test_session_migration_covers_threads_no_longer_tracked():
    th = old_session("gone", "scratch-a · Mac · codex", ["Old job"], archived=True, tracked=False); silent = old_session("g2", "scratch-b · PC · claude", [], archived=True, tracked=False)
    (tmp / "Queue/sessions").mkdir(parents=True, exist_ok=True); tick()
    assert fake.channels[th]["name"] == "Old job · Mac · codex" and fake.channels[th]["archived"], fake.channels[th]
    assert fake.channels[silent]["name"] == "scratch-b · PC · claude" and b.st["session_names_swept"]
    n = fake.count("GET", "/threads/archived/public"); tick(3); assert fake.count("GET", "/threads/archived/public") == n, "one time only"

def test_session_rename_rate_limit_retries_later():
    th = old_session("s1", "x · Mac · claude", ["Original"]); session_file(prompt="Newer")
    fake.renames[th] = [CLOCK.time(), CLOCK.time()]; tick()
    assert fake.channels[th]["name"] == "x · Mac · claude" and not b.st["sessions"][f"{MAC}/s1"].get("named")
    fake.renames[th] = []; bridge_mod.BLOCKED.clear(); CLOCK.offset += 61; tick()
    assert fake.channels[th]["name"] == "Original · Mac · claude"

def test_session_names_budget_per_pass():
    for i in range(7): old_session(f"s{i}", "x · Mac · claude", [f"Ask {i}"]); session_file(sid=f"s{i}", prompt="N")
    tick(); assert sum(1 for t in fake.renames.values() if t) == bridge_mod.NAME_BATCH
    tick(); assert sum(1 for t in fake.renames.values() if t) == 7

def blocked429(): return Err(429, 0, {"retry_after": 500.0, "global": False, "message": "slow"})
def untracked(name, prompts=("Old job",), archived=True, card=None):
    th = old_session(name, name, list(prompts), archived=archived, tracked=False)
    if card: fake.channels[THREADS]["msgs"][th]["content"] = card
    (tmp / "Queue/sessions").mkdir(parents=True, exist_ok=True); return th
def logged():
    lines = []; real = bridge_mod.log; bridge_mod.log = lambda *a: (lines.append(" ".join(str(x) for x in a)), real(*a))
    return lines, lambda: setattr(bridge_mod, "log", real)
def rename_window_over(th): fake.renames[th] = []; CLOCK.offset += 501; assert not any(v > CLOCK.time() for v in bridge_mod.BLOCKED.values())

def test_sweep_renames_only_what_the_bridge_generated_and_logs_it():
    hand = untracked("Kepler · pool · final fix", card="⚪ **Ended**  scratch-q  ·  Mac  ·  claude\n> a"); real = untracked("scratch-a · Mac · codex", ["Asked first", "Then this"])
    lookalike = untracked("Kepler · Mac · claude", card="⚪ **Ended**  scratch-z  ·  Mac  ·  claude\n> a")  # hand-set in the bridge's own style
    nocard = untracked("Other · Mac · claude"); del fake.channels[THREADS]["msgs"][nocard]  # nothing proves the bridge made this name
    lines, restore = logged()
    try: tick()
    finally: restore()
    assert fake.channels[hand]["name"] == "Kepler · pool · final fix" and hand not in fake.renames, fake.channels[hand]["name"]
    assert fake.channels[lookalike]["name"] == "Kepler · Mac · claude" and fake.channels[nocard]["name"] == "Other · Mac · claude"
    assert fake.channels[real]["name"] == "Asked first · Mac · codex", fake.channels[real]["name"]
    assert any(real in x and "scratch-a · Mac · codex" in x and "Asked first · Mac · codex" in x for x in lines), lines
    assert all(fake.channels[t]["archived"] for t in (hand, real, lookalike, nocard))

def test_sweep_long_429_closes_thread_and_retries_rename():
    th = untracked("scratch-a · Mac · codex"); fake.renames[th] = [CLOCK.time()] * 2; tick()
    assert fake.channels[th]["archived"], "closed again after the blocked rename"
    assert fake.channels[th]["name"] == "scratch-a · Mac · codex" and not b.st.get("session_names_swept"), "not done yet"
    tick(3); assert fake.channels[th]["archived"], "stays closed while waiting"
    rename_window_over(th); tick()
    assert fake.channels[th]["name"] == "Old job · Mac · codex" and fake.channels[th]["archived"] and b.st["session_names_swept"], fake.channels[th]

def test_tracked_archived_session_long_429_closes_thread_and_retries():
    th = old_session("s1", "x · Mac · claude", ["Original"], archived=True); session_file(prompt="Original", state="idle")
    fake.renames[th] = [CLOCK.time()] * 2; tick()
    assert fake.channels[th]["archived"] and not b.st["sessions"][f"{MAC}/s1"].get("named"), fake.channels[th]
    rename_window_over(th); tick()
    assert fake.channels[th]["name"] == "Original · Mac · claude" and fake.channels[th]["archived"] and b.st["sessions"][f"{MAC}/s1"]["named"]

def rearchive_fails(err):
    th = untracked("scratch-a · Mac · codex"); fake.renames[th] = [CLOCK.time()] * 2
    fake.fail = lambda m, p, body: err() if m == "PATCH" and (body or {}).get("archived") is True else None
    tick()  # must not raise
    assert fake.channels[th]["archived"] is False and th in b.st["rearchive"] and not b.st.get("session_names_swept"), fake.channels[th]
    fake.fail = None; CLOCK.offset += 501; tick()
    assert fake.channels[th]["archived"] and not b.st["rearchive"], "closed once the failure passed"
    assert not b.st.get("session_names_swept"), "and the rename is still owed"
    fake.renames[th] = []; bridge_mod.BLOCKED.clear(); CLOCK.offset += 501; tick()
    assert fake.channels[th]["name"] == "Old job · Mac · codex" and fake.channels[th]["archived"] and b.st["session_names_swept"], fake.channels[th]
def test_rearchive_rate_limited_is_retried(): rearchive_fails(blocked429)
def test_rearchive_network_error_is_retried(): rearchive_fails(lambda: urllib.error.URLError("network down"))

def test_sweep_network_error_keeps_gateway_and_retries():
    th = untracked("scratch-a · Mac · codex"); fake.down = True
    tick(); assert not b.st.get("session_names_swept") and fake.channels[th]["name"] == "scratch-a · Mac · codex"
    fake.down = False; fake.fail = lambda m, p, body: urllib.error.URLError("reset") if m == "GET" and p.startswith(f"/channels/{THREADS}/messages/") else None
    CLOCK.offset += 301; tick(); assert not b.st.get("session_names_swept"), "a drop mid-sweep is handled the same way"
    fake.fail = None; CLOCK.offset += 301; tick()
    assert fake.channels[th]["name"] == "Old job · Mac · codex" and b.st["session_names_swept"]

def test_sweep_403_is_not_done():
    th = untracked("scratch-a · Mac · codex"); fake.fail = lambda m, p, body: Err(403, 50013) if "/threads/archived/" in p else None
    tick(); assert not b.st.get("session_names_swept"), "a refused list proves nothing"
    n = fake.count("GET", "/threads/archived/public"); tick(3); assert fake.count("GET", "/threads/archived/public") == n, "waits before asking again"
    fake.fail = None; CLOCK.offset += 3601; tick()
    assert fake.channels[th]["name"] == "Old job · Mac · codex" and b.st["session_names_swept"]

def test_session_in_grace_period_is_still_renamed():
    th = old_session("s1", "scratch-a · Mac · claude", ["First ask", "Later"], archived=True); c = b.st["sessions"][f"{MAC}/s1"]; c["text"] = "⚪ **Idle**  scratch-a  ·  Mac  ·  claude\n> Later"
    (tmp / "Queue/sessions").mkdir(parents=True, exist_ok=True); tick()
    assert fake.channels[th]["name"] == "First ask · Mac · claude" and fake.channels[th]["archived"] and c.get("missing") and c["named"], fake.channels[th]
    assert b.st["session_names_swept"]; tick(2); assert len(fake.renames[th]) == 1, "once"

def refuse_close(th): fake.fail = lambda m, p, body: Err(403, 50013) if m == "PATCH" and p == f"/channels/{th}" and (body or {}).get("archived") is True else None
def closes(th): return sum(1 for m, p, body in fake.calls if m == "PATCH" and p == f"/channels/{th}" and (body or {}).get("archived") is True)

def test_refused_close_is_retried_hourly_and_the_sweep_waits_for_it():
    th = untracked("scratch-a · Mac · codex"); fake.renames[th] = [CLOCK.time()] * 2; refuse_close(th); tick()
    assert fake.channels[th]["archived"] is False and th in b.st["rearchive"] and not b.st.get("session_names_swept"), "a refused close stays owed"
    rename_window_over(th); tick(3)
    assert not b.st.get("session_names_swept") and th in b.st["rearchive"], "the sweep is not done while the thread is open under its old name"
    assert closes(th) == 1, "no second try before the hour"
    fake.fail = None; CLOCK.offset += 3601; tick()
    assert fake.channels[th]["name"] == "Old job · Mac · codex" and fake.channels[th]["archived"] and b.st["session_names_swept"], fake.channels[th]
    assert not b.st["rearchive"] and not b.st.get("rearchive_refused")

def test_refused_close_gives_up_after_a_few_tries_and_logs_the_thread():
    th = untracked("scratch-a · Mac · codex"); fake.renames[th] = [CLOCK.time()] * 2; refuse_close(th)
    lines, restore = logged()
    try:
        tick(); lists = fake.count("GET", "/threads/archived/public")
        for _ in range(bridge_mod.CLOSE_REFUSED_TRIES + 3): CLOCK.offset += 3601; tick(3)
    finally: restore()
    assert closes(th) == bridge_mod.CLOSE_REFUSED_TRIES, closes(th)
    assert fake.count("GET", "/threads/archived/public") - lists <= bridge_mod.CLOSE_REFUSED_TRIES + 1, "the sweep does not poll while it waits"
    assert any("gave up closing thread" in x and th in x for x in lines), lines
    assert not b.st["rearchive"] and not b.st.get("rearchive_refused") and b.st["session_names_swept"], "bounded: nothing left to retry"

def old_backlog(n=14): return [untracked(f"scratch-{i} · Mac · codex", [f"Old job {i}"]) for i in range(n)]
def open_now(ths): return [t for t in ths if t in fake.channels and not fake.channels[t]["archived"]]
def renamed(ths): return [t for t in ths if fake.channels[t]["name"].startswith("Old job")]
def refuse_all_closes(): fake.fail = lambda m, p, body: Err(403, 50013) if m == "PATCH" and (body or {}).get("archived") is True else None
def pass1(): b.last_clean = CLOCK.time(); tick()  # one pass with the board cleanup held off: it removes untracked cards, and the card is what proves a name
def hours(n, ths, after=None):
    """Run n hours of passes; returns the most threads open at any one time."""
    peak = len(open_now(ths))
    for _ in range(n):
        CLOCK.offset += 3601
        for _ in range(3): pass1(); peak = max(peak, len(open_now(ths)))
        if after: after()
    return peak

def test_sweep_opens_no_new_batch_while_closes_are_refused():
    ths = old_backlog(); refuse_all_closes(); lines, restore = logged()
    try:
        pass1(); peak = max(len(open_now(ths)), hours(3, ths))
        assert 1 <= peak <= bridge_mod.NAME_BATCH and len(renamed(ths)) == peak, ("the reviewer's repro: 14 open after 3 hours", peak, len(renamed(ths)))
        assert not b.st.get("session_names_swept"), "not done while a close is owed"
        peak = max(peak, hours(bridge_mod.CLOSE_REFUSED_TRIES + 20, ths))
    finally: restore()
    left = open_now(ths)
    assert peak <= bridge_mod.NAME_BATCH and len(left) == peak, ("never more than one batch open", peak, left)
    assert b.st["session_names_swept"] and not b.st["rearchive"] and not b.st.get("rearchive_refused") and not b.st.get("session_names_stop"), "and the sweep still ends"
    for t in left:
        assert closes(t) == bridge_mod.CLOSE_REFUSED_TRIES, (t, closes(t))
        assert sum(1 for x in lines if "gave up closing thread" in x and t in x) == 1, lines
    assert all(closes(t) == 0 and fake.channels[t]["archived"] and t not in fake.renames for t in ths if t not in left), "the rest were never opened"
    assert sum(1 for x in lines if "session name sweep stopped" in x) == 1, lines
    asked = lambda: fake.count("GET", "/threads/archived/public") + sum(1 for m, p, body in fake.calls if m == "PATCH" and p.split("/")[-1] in ths)
    n = asked(); hours(3, ths); assert asked() == n, "no thread is listed, opened or closed once the sweep has ended"

def test_sweep_carries_on_once_the_refusal_lifts():
    ths = old_backlog(); refuse_all_closes(); pass1()
    peak = max(len(open_now(ths)), hours(2, ths)); assert 1 <= peak <= bridge_mod.NAME_BATCH and not b.st.get("session_names_swept")
    fake.fail = None; peak = max(peak, hours(4, ths))
    assert peak <= bridge_mod.NAME_BATCH, peak
    assert not open_now(ths) and len(renamed(ths)) == 14 and b.st["session_names_swept"], (open_now(ths), len(renamed(ths)), b.st.get("session_names_swept"))
    assert not b.st["rearchive"] and not b.st.get("rearchive_refused") and "session_names_seen" not in b.st
    assert all(len(fake.renames[t]) == 1 for t in ths), "each renamed once"

def test_sweep_refusal_hold_survives_restarts_with_no_extra_tries():
    global b
    ths = old_backlog(); refuse_all_closes(); pass1(); held = open_now(ths); assert len(held) == 1, held
    for _ in range(5):
        CLOCK.offset += 600; b = bridge_mod.Bridge(); b.st["started"] = 0; b.spawn = lambda fn: fn(); b.cfg["screenshots"] = False; pass1(); pass1()
    assert closes(held[0]) == 1 and open_now(ths) == held and b.st["rearchive_refused"][held[0]] == 1, (closes(held[0]), open_now(ths))
    CLOCK.offset += 601; pass1(); assert closes(held[0]) == 2 and open_now(ths) == held, "the second try comes on the hour, and still no other thread is opened"

def test_sweep_carries_on_when_the_refused_thread_is_deleted():
    ths = old_backlog(6); refuse_all_closes(); pass1(); held = open_now(ths); assert len(held) == 1
    del fake.channels[held[0]]; fake.fail = None; hours(3, ths)
    rest = [t for t in ths if t != held[0]]
    assert len(renamed(rest)) == 5 and not open_now(rest) and b.st["session_names_swept"] and not b.st.get("rearchive_refused"), (len(renamed(rest)), open_now(rest))

def test_refusal_on_a_tracked_thread_holds_the_sweep_but_does_not_end_it():
    th = old_session("s1", "x · Mac · claude", ["Original"], archived=True); session_file(prompt="Original", state="idle")
    ths = old_backlog(3); refuse_close(th); pass1()
    assert fake.channels[th]["archived"] is False and th in b.st["rearchive_refused"] and not open_now(ths) and not renamed(ths), "the sweep waits for the refused close"
    hours(bridge_mod.CLOSE_REFUSED_TRIES + 2, ths)
    assert closes(th) == bridge_mod.CLOSE_REFUSED_TRIES and len(renamed(ths)) == 3 and not open_now(ths) and b.st["session_names_swept"], (closes(th), len(renamed(ths)))

def test_tracked_hand_set_name_in_the_bridges_style_is_kept():
    th = old_session("s1", "scratch-a · Mac · claude", ["First ask", "Later"], archived=True); c = b.st["sessions"][f"{MAC}/s1"]
    fake.channels[th]["name"] = "Kepler pool notes · Mac · claude"  # named by hand, with the bridge's own ending
    (tmp / "Queue/sessions").mkdir(parents=True, exist_ok=True)  # no session file: the grace period
    lines, restore = logged()
    try: tick(2)
    finally: restore()
    assert fake.channels[th]["name"] == "Kepler pool notes · Mac · claude" and th not in fake.renames and fake.channels[th]["archived"], fake.channels[th]
    assert c["named"] and any("name kept" in x and th in x for x in lines), lines
    # and for a live session, whose card is rewritten in the same pass
    th2 = old_session("s2", "scratch-b · Mac · claude", ["First ask"]); fake.channels[th2]["name"] = "Pool notes · Mac · claude"
    session_file(sid="s2", prompt="Newer"); tick(2)
    assert fake.channels[th2]["name"] == "Pool notes · Mac · claude" and th2 not in fake.renames and b.st["sessions"][f"{MAC}/s2"]["named"]

def test_queued_close_is_dropped_when_the_session_resumes():
    th = old_session("s1", "x · Mac · claude", ["Original"], archived=True); session_file(prompt="Original", state="idle")
    fake.renames[th] = [CLOCK.time()] * 2
    fake.fail = lambda m, p, body: Err(503, 0) if m == "PATCH" and (body or {}).get("archived") is True else None
    tick(); assert fake.channels[th]["archived"] is False and th in b.st["rearchive"], "the close is queued"
    fake.fail = None; session_file(prompt="Original", state="working"); tick()  # resumed, same prompt: nothing new is posted
    CLOCK.offset += 61; session_file(prompt="Original", state="working"); tick(2)
    assert fake.channels[th]["archived"] is False and th not in b.st["rearchive"], "a working session's thread stays open"
    # a new prompt drops it too, even if the session is idle again before the close is due
    th2 = old_session("s2", "y · Mac · claude", ["Original"], archived=True); session_file(sid="s2", prompt="Original", state="idle")
    fake.renames[th2] = [CLOCK.time()] * 2
    fake.fail = lambda m, p, body: Err(503, 0) if m == "PATCH" and (body or {}).get("archived") is True else None
    tick(); assert th2 in b.st["rearchive"]
    fake.fail = None; session_file(sid="s2", prompt="Next thing", state="idle"); tick(); CLOCK.offset += 61; tick(2)
    assert fake.channels[th2]["archived"] is False and th2 not in b.st["rearchive"], fake.channels[th2]
    # an idle session with nothing new is still closed again
    th3 = old_session("s3", "z · Mac · claude", ["Original"], archived=True); session_file(sid="s3", prompt="Original", state="idle")
    fake.renames[th3] = [CLOCK.time()] * 2
    fake.fail = lambda m, p, body: Err(503, 0) if m == "PATCH" and (body or {}).get("archived") is True else None
    tick(); fake.fail = None; CLOCK.offset += 61; tick()
    assert fake.channels[th3]["archived"] and th3 not in b.st["rearchive"]

def test_conflict_card_failure_never_stops_the_threads_status():
    o1, a = request("A work"); o2, c = request("C work"); q = conflict([a, c]); tick(); th = conflict_thread(q); card = link_card(th)[0]["id"]; before = link_card(th)[0]["content"]
    assert any("Needs you" in x["content"] for x in status_msgs(th)), [x["content"] for x in fake.msgs(th)]
    fake.fail = lambda m, p, body: Err(403, 50013) if m == "PATCH" and p.endswith("/messages/" + card) else None
    event("started", a, agent="builder"); event("started", q["id"], agent="builder"); tick()
    assert any("Working" in x["content"] for x in status_msgs(th)), [x["content"] for x in fake.msgs(th)]
    assert link_card(th)[0]["content"] == before, "the card really did fail"
    fake.fail = None; sweep_tick(); assert "`builder` on" in link_card(th)[0]["content"], "and is caught up afterwards"

# ---- setup and the server layout ----
def layout():
    for cid, name in ((PARENT, "orchestrator"), (PROGRESS, "progress"), (THREADS, "sessions"), (REPORTS, "reports"), (ALERTS, "alerts"), (OVERVIEW, "overview"), (LOOPS, "loops")): fake.channels[cid]["name"] = name
    fake.channels[OVERVIEW]["parent"] = "2000"
    fake.channels["2000"] = {"type": 4, "msgs": {}, "name": "Agents"}; fake.channels["2001"] = {"type": 4, "msgs": {}, "name": "My sessions"}
    for cid in (PARENT, PROGRESS, REPORTS, ALERTS, LOOPS): fake.channels[cid]["parent"] = "2000"
    fake.channels[THREADS]["parent"] = "2001"
def run_setup(): bridge_mod.setup(GUILD); return json.loads((tmp / "Queue/discord/config.json").read_text(encoding="utf-8"))

def test_setup_keeps_the_hand_made_layout():
    layout(); fake.channels[PROGRESS]["name"] = "renamed-by-hand"; b.cfg["x"] = 1
    raw = json.loads((tmp / "Queue/discord/config.json").read_text(encoding="utf-8")); raw["thread_state_names"] = False; (tmp / "Queue/discord/config.json").write_text(json.dumps(raw), encoding="utf-8")
    cfg = run_setup()
    assert fake.count("POST", f"/guilds/{GUILD}/channels") == 0, "nothing created"
    assert cfg["channels"]["threads"] == THREADS and cfg["channels"]["progress"] == PROGRESS, "found by id"
    assert fake.channels[THREADS]["name"] == "sessions" and fake.channels[PROGRESS]["name"] == "renamed-by-hand", "names left alone"
    assert fake.channels[THREADS]["parent"] == "2001" and cfg["thread_state_names"] is False and cfg["hosts"] == [PC, MAC]

def test_setup_finds_by_name_when_the_id_is_stale():
    layout(); raw = json.loads((tmp / "Queue/discord/config.json").read_text(encoding="utf-8")); raw["channels"]["threads"] = "999999"; (tmp / "Queue/discord/config.json").write_text(json.dumps(raw), encoding="utf-8")
    cfg = run_setup(); assert cfg["channels"]["threads"] == THREADS and fake.count("POST", f"/guilds/{GUILD}/channels") == 0

def test_setup_creates_missing_inside_their_categories_and_never_threads():
    layout(); del fake.channels[THREADS]; del fake.channels[REPORTS]; raw = json.loads((tmp / "Queue/discord/config.json").read_text(encoding="utf-8")); raw["channels"].pop("threads"); (tmp / "Queue/discord/config.json").write_text(json.dumps(raw), encoding="utf-8")
    cfg = run_setup()
    made = fake.channels[cfg["channels"]["threads"]]; rep_ch = fake.channels[cfg["channels"]["reports"]]
    assert made["name"] == "sessions" and made["parent"] == "2001" and rep_ch["name"] == "reports" and rep_ch["parent"] == "2000", (made, rep_ch)
    assert not any(c.get("name") == "threads" for c in fake.channels.values()), "no threads channel"
    assert sum(1 for c in fake.channels.values() if c["type"] == 4) == 2, "categories reused"

def test_setup_creates_a_missing_category():
    layout(); del fake.channels[REPORTS]; del fake.channels["2000"]
    cfg = run_setup(); rep_ch = fake.channels[cfg["channels"]["reports"]]
    assert fake.channels[rep_ch["parent"]]["type"] == 4 and fake.channels[rep_ch["parent"]]["name"] == "Agents"

def test_setup_registers_the_owner_mailbox():
    p = tmp / "Queue/mail/agents.json"; agents = json.loads(p.read_text(encoding="utf-8")); agents.pop("owner"); p.write_text(json.dumps(agents), encoding="utf-8")
    assert bridge_mod.register_owner() is True
    got = bridge_mod.AM.load_agents()["owner"]; assert got["kind"] == "human" and got["host"] == [PC, MAC], got
    assert bridge_mod.register_owner() is False, "an existing owner mailbox is left as it is"

def test_setup_creates_the_bridge_lock():
    cfg = bridge_mod.load("config.json", {}); cfg.pop("lease_msg", None); cfg.setdefault("channels", {})["alerts"] = ALERTS; bridge_mod.save("config.json", cfg)
    assert bridge_mod.ensure_lock() is True
    th, mid = bridge_mod.load("config.json", {})["lease_msg"]
    assert fake.channels[th]["msgs"][mid]["content"].endswith("|0"), "a plain 'host|epoch' message the bridges edit in place"
    assert bridge_mod.ensure_lock() is False, "an existing lock is kept"

def test_state_names_off_never_brings_dots_back():
    b.cfg["thread_state_names"] = False
    origin, mid = request("Clean name"); t = b.st["threads"][mid]
    t["name"], t["name_want"], t["name_t"] = "🟡 Clean name", "⚪ Clean name", 0.0  # what an older bridge left behind
    event("started", mid); tick(); CLOCK.offset += 700; sweep_tick(); event("done", mid); tick(); CLOCK.offset += 700; sweep_tick()
    assert fake.channels[origin]["name"] == "Clean name" and origin not in fake.renames, fake.channels[origin]["name"]
    o2, c = request("Other"); q = conflict([mid, c]); tick(); assert fake.channels[conflict_thread(q)]["name"] == "Conflict: both edit the pool"

def test_stripped_emoji_comes_back():
    # a thread whose emoji was stripped while state names were off was locked as if the owner had renamed it
    origin, mid = request("Old thread"); t = b.st["threads"][mid]; agent_mod.ack("orchestrator", mid)
    o2, m2 = request("Mine"); t2 = b.st["threads"][m2]; agent_mod.ack("orchestrator", m2)
    o3, m3 = request("Sleeping"); t3 = b.st["threads"][m3]; agent_mod.ack("orchestrator", m3)
    o4, m4 = request("No stored name"); t4 = b.st["threads"][m4]; agent_mod.ack("orchestrator", m4)
    event("started", m3); event("done", m3); answer(m3, "Done."); tick(); sweep_tick()  # finished, so its status no longer changes
    assert b.lock_check == set(), "nothing locked: no check outstanding"
    fake.channels[origin]["name"] = "Old thread"; t["name"], t["name_lock"] = "🟡 Old thread", True
    fake.channels[o2]["name"] = "The owner's own name"; t2["name"], t2["name_lock"] = "🟡 Mine", True
    fake.channels[o3].update(name="Sleeping", archived=True); t3["name"], t3["name_lock"] = "✅ Sleeping", True  # archived, and the state forgot
    fake.channels[o4]["name"] = "No stored name"; t4.pop("name")
    fake.renames.clear(); b.lock_check = None; calls = len(fake.calls)  # as after a restart
    tick()
    assert "name_lock" not in t and t["name"] == "Old thread", "a stripped emoji is not a rename by hand"
    assert t2.get("name_lock") and fake.channels[o2]["name"] == "The owner's own name", "their own name stays locked"
    assert "name_lock" not in t3 and t3.get("archived") and fake.channels[o3]["archived"], "an archived thread is read, not opened"
    assert fake.count("GET", f"/channels/{origin}") and b.lock_check == set()
    sweep_tick(); CLOCK.offset += 31; sweep_tick()
    want = b.thread_name(t); assert want != "Old thread" and fake.channels[origin]["name"] == want, fake.channels[origin]["name"]
    assert fake.channels[o4]["name"] == b.thread_name(t4) != "No stored name", "a thread with no stored name gets the emoji form"
    assert fake.channels[o2]["name"] == "The owner's own name" and o2 not in fake.renames
    assert fake.channels[o3]["name"] == "Sleeping" and fake.channels[o3]["archived"] and o3 not in fake.renames, ("no opening just for an emoji", fake.channels[o3]["name"], fake.channels[o3]["archived"], [c for c in fake.calls[calls:] if o3 in c[1]])
    assert all(len(v) <= 2 for v in fake.renames.values())
    gets = fake.count("GET", f"/channels/{o2}"); sweep_tick(); assert fake.count("GET", f"/channels/{o2}") == gets, "checked once per start"
    # live: the emoji stripped by hand or by a tool is put back, not locked; a real rename still locks
    b.dispatch("THREAD_UPDATE", {"id": origin, "name": "Old thread", "thread_metadata": {"archived": False}})
    assert "name_lock" not in t and t["name"] == "Old thread"
    CLOCK.offset += 700; sweep_tick(); CLOCK.offset += 31; sweep_tick(); assert fake.channels[origin]["name"] == want
    b.dispatch("THREAD_UPDATE", {"id": origin, "name": "Something else", "thread_metadata": {"archived": False}}); assert t.get("name_lock")
    # the archived one gets its emoji when the owner opens it again
    fake.channels[o3]["archived"] = False; b.dispatch("THREAD_UPDATE", {"id": o3, "name": "Sleeping", "thread_metadata": {"archived": False}})
    sweep_tick(); CLOCK.offset += 31; sweep_tick(); assert fake.channels[o3]["name"] == b.thread_name(t3) != "Sleeping", fake.channels[o3]["name"]

# ---- no Done while mail in this work still waits for an agent ----
def delegated(text="Fix the pool in Kepler", to="builder"):
    """The orchestrator answers the owner, mails a task to another agent in the same thread, and its own run ends done."""
    origin, mid = request(text); event("started", mid); tick(); answer(mid, "Sent to the builder.")
    task = agent_mod.send("orchestrator", to, "Build it", "Do it", reply_to=mid); agent_mod.ack("orchestrator", mid); event("done", mid); tick(2)
    return origin, mid, task
def name_patches(th): return sum(1 for m, p, body in fake.calls if m == "PATCH" and p.endswith(f"/channels/{th}") and "name" in (body or {}))
def settle(): sweep_tick(); CLOCK.offset += 31; sweep_tick()
def not_done(origin, mid):
    t = b.st["threads"][mid]; word = b.state_word(t); assert word != "DONE", word
    cards = status_msgs(origin); assert cards and CHECK not in cards[-1]["content"] and "Done" not in cards[-1]["content"], cards and cards[-1]["content"]
    prog = [m for m in fake.msgs(PROGRESS) if clean(t) in m["content"]]
    assert prog and all(CHECK not in m["content"] and "DONE" not in m["content"] for m in prog), [m["content"] for m in prog]
    assert CHECK not in fake.channels[origin]["name"], fake.channels[origin]["name"]
    assert not close_card(origin) and not b.closeable(mid, t) and not b.answered(t)
    return cards[-1]["content"]
def clean(t): return bridge_mod.clean_title(t.get("title"))
CHECK = "\u2705"

def test_done_run_with_mail_queued_for_another_agent_is_not_done():
    assert bridge_mod.STATES["DONE"][0] == CHECK
    origin, mid, task = delegated(); t = b.st["threads"][mid]
    text = not_done(origin, mid)
    assert b.state_word(t) == "QUEUED" and "Queued" in text and "`builder`" in text and "queued" in text, text
    wait_over(); settle(); text = not_done(origin, mid)  # the wait for an answer does not turn it into Done, and the settled name has no checkmark
    assert fake.channels[origin]["name"].startswith(bridge_mod.STATES["QUEUED"][0]), fake.channels[origin]["name"]
    assert "QUEUED" in fake.msgs(PROGRESS)[-1]["content"], fake.msgs(PROGRESS)[-1]["content"]
    calls = len(fake.calls); tick(3); CLOCK.offset += 11; tick(3); assert len(fake.calls) == calls, ("nothing is sent while nothing changes", fake.calls[calls:])
    o2, c = request("Pool in Fish"); q = conflict([mid, c]); tick(2); card = link_card(conflict_thread(q))[0]["content"]  # the linked row of a conflict card
    assert CHECK not in card and "Done" not in card and "`builder`" in card, card
    CLOCK.offset += bridge_mod.ARCHIVE_AFTER + 60; sweep_tick(); sweep_tick()
    assert not fake.channels[origin].get("archived") and not t.get("archived"), "never archived with mail waiting"
    not_done(origin, mid)
    old = fake.channels[origin]["msgs"][t["status"]]; old["components"] = b.close_row(); press_close(old)  # a button left from before
    assert not fake.channels[origin].get("archived"), "a stale Close does not close it"

def test_queued_mail_picked_up_then_finished_shows_done():
    origin, mid, task = delegated(); t = b.st["threads"][mid]; settle()
    assert b.state_word(t) == "QUEUED"
    event("started", mid, ref=task["id"], agent="builder"); tick(2); assert b.state_word(t) == "WORKING", "picked up: working, the mail is still in the inbox while it runs"
    assert "Working" in status_msgs(origin)[-1]["content"]; settle()
    answer(mid, "All built."); event("done", mid, ref=task["id"], agent="builder"); tick(2)
    assert b.state_word(t) == "DONE", "its run is over: mail the runner has not acked yet is not waiting"
    agent_mod.ack("builder", task["id"]); tick(2); settle(); CLOCK.offset += 31; sweep_tick()
    assert b.state_word(t) == "DONE" and not t.get("owed") and close_card(origin), "done, and Close is offered"
    assert not status_msgs(origin), "the answer alone is the outcome, as before"
    assert name_patches(origin) <= 2 and len(fake.renames.get(origin, [])) <= 2, (name_patches(origin), "Done -> Queued -> Working -> Done stays in the rename budget")
    CLOCK.offset += 700; settle(); assert fake.channels[origin]["name"].startswith(CHECK), fake.channels[origin]["name"]

def test_second_queued_mail_keeps_it_queued_until_the_last_finishes():
    origin, mid, task = delegated(); t = b.st["threads"][mid]
    agent_mod.register("reviewer", "any", "claude", auto=True); two = agent_mod.send("orchestrator", "reviewer", "Also this", "x", reply_to=mid); tick(2)
    assert set(t["owed"]) == {"builder", "reviewer"}, t["owed"]
    assert "`builder`" in status_msgs(origin)[-1]["content"] and "`reviewer`" in status_msgs(origin)[-1]["content"], status_msgs(origin)[-1]["content"]
    event("started", mid, ref=task["id"], agent="builder"); tick(); event("done", mid, ref=task["id"], agent="builder"); agent_mod.ack("builder", task["id"]); tick(2); wait_over()
    assert b.state_word(t) == "QUEUED" and "`reviewer`" in not_done(origin, mid), "one finished, one still waits"
    agent_mod.ack("reviewer", two["id"]); tick(2); assert b.state_word(t) == "DONE"

def test_unreadable_inboxes_do_not_claim_done():
    real = bridge_mod.AM.list_inbox
    def broken(a, **k): raise OSError("inbox locked")
    try:
        # a run ends while the inboxes cannot be read: the thread keeps Working, with or without mail waiting
        o1, a = request("Pool in Kepler"); event("started", a); tick(2); t1 = b.st["threads"][a]; assert b.state_word(t1) == "WORKING"
        o2, c = request("Pool in Fish"); event("started", c); tick(2); t2 = b.st["threads"][c]
        bridge_mod.AM.list_inbox = broken
        task = agent_mod.send("orchestrator", "builder", "Build it", "Do it", reply_to=a); agent_mod.ack("orchestrator", a); event("done", a)
        agent_mod.ack("orchestrator", c); event("done", c); tick(2); wait_over(); settle()
        for o, tid, t in ((o1, a, t1), (o2, c, t2)):
            assert b.blind and b.state_word(t) == "WORKING", b.state_word(t)
            assert CHECK not in status_msgs(o)[-1]["content"] and CHECK not in fake.channels[o]["name"] and not close_card(o)
        CLOCK.offset += bridge_mod.ARCHIVE_AFTER + 60; sweep_tick(); assert not fake.channels[o1].get("archived") and not fake.channels[o2].get("archived")
        bridge_mod.AM.list_inbox = real; tick(2)
        assert not b.blind and b.state_word(t1) == "QUEUED" and "`builder`" in status_msgs(o1)[-1]["content"], "readable again: the waiting mail shows"
        assert b.state_word(t2) == "DONE" and "Done" in status_msgs(o2)[-1]["content"], "readable again and nothing waits: done"
        # a thread already shown as queued for an agent, or already done, keeps that
        bridge_mod.AM.list_inbox = broken; tick(2); sweep_tick()
        assert b.state_word(t1) == "QUEUED" and t1.get("owed") and b.state_word(t2) == "DONE"
    finally: bridge_mod.AM.list_inbox = real

def test_queued_mail_under_an_aliased_thread_id_still_counts():
    origin, mid = request(); event("started", mid); tick(); answer(mid, "Sent to the builder.")
    side = agent_mod.send("orchestrator", "builder", "Side job", "x"); other = side.get("thread") or side["id"]; assert other != mid
    b.st["alias"][other] = mid
    agent_mod.ack("orchestrator", mid); event("done", mid); tick(2); wait_over()
    text = not_done(origin, mid); assert b.state_word(b.st["threads"][mid]) == "QUEUED" and "`builder`" in text, text
    agent_mod.ack("builder", side["id"]); tick(2); assert b.state_word(b.st["threads"][mid]) == "DONE"

def test_state_word_reads_no_inbox():
    for i in range(4): delegated(f"Job {i}")
    real, n = bridge_mod.AM.list_inbox, []
    def counting(a, **k): n.append(a); return real(a, **k)
    bridge_mod.AM.list_inbox = counting
    try:
        for t in b.st["threads"].values(): b.state_word(t); b.thread_name(t); b.anchor_text(t)
        assert not n, n
        b.last_tick = 0; b.last_sweep = CLOCK.time(); b.last_qline = CLOCK.time(); b.dirty.clear(); b.tick()
        assert n and len(n) == len(set(n)) and "owner" not in n, ("each agent inbox at most once per tick", n)
    finally: bridge_mod.AM.list_inbox = real

# ---- follow-ups: one unreadable inbox file, the name through a hand-off, the hold before Done ----
def grace_on(): bridge_mod.DONE_GRACE = GRACE
def grace_over(): CLOCK.offset += GRACE + 1; tick(2)
def bad_file(agent="mac", name="20261110T000000Z-0badf00d.json", text="{trunc"):
    p = agent_mod.agent_dir(agent) / "inbox" / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text, encoding="utf-8"); return p
def log_lines():
    lines = []; real = bridge_mod.log; bridge_mod.log = lambda *a: (lines.append(" ".join(str(x) for x in a)), real(*a))
    return lines, real
def done_thread(text):
    """A request whose only run ends done with its answer delivered."""
    origin, mid = request(text); event("started", mid); tick(); answer(mid, "All done."); agent_mod.ack("orchestrator", mid); event("done", mid); tick(2)
    return origin, mid
def icon(word): return bridge_mod.STATES[word][0]
def said_working(th): return [m for m in status_msgs(th) if "Working" in m["content"]]

def test_one_corrupt_inbox_file_does_not_blind_the_other_threads():
    bad = bad_file(); lines, real = log_lines()
    try:
        o1, a = done_thread("Pool in Kepler"); t1 = b.st["threads"][a]
        assert not b.blind and b.state_word(t1) == "DONE" and not said_working(o1), ("a run that ends reaches Done with the bad file present", b.state_word(t1))
        assert not close_card(o1), "it may be mail still syncing: for the first minute nothing is closed"
        CLOCK.offset += bridge_mod.BAD_MAIL_HOLD + 1; sweep_tick(); assert close_card(o1), "after that it holds nothing"
        o2, c, task = delegated("Pool in Fish"); t2 = b.st["threads"][c]
        assert b.state_word(t2) == "QUEUED" and "`builder`" in not_done(o2, c), "readable mail is still counted"
        for _ in range(20): CLOCK.offset += 5; sweep_tick()
        assert len([l for l in lines if "inbox file unreadable" in l]) == 1 and bad.name in next(l for l in lines if "inbox file unreadable" in l), lines
        assert not [l for l in lines if "open mail check failed" in l], "one line when it starts, none per tick"
        assert not [m for m in fake.msgs(ALERTS) if bad.name in m["content"]], "no alert in the first minutes"
        CLOCK.offset += getattr(bridge_mod, "BAD_MAIL_ALERT", 300); tick(2)
        alerts = [m for m in fake.msgs(ALERTS) if bad.name in m["content"]]; assert len(alerts) == 1 and "`mac`" in alerts[0]["content"], [m["content"] for m in fake.msgs(ALERTS)]
        for _ in range(12): CLOCK.offset += 3600; sweep_tick()
        assert len([m for m in fake.msgs(ALERTS) if bad.name in m["content"]]) == 1 and len([c for c in fake.calls if c[0] == "POST" and ALERTS in c[1]]) == 1, "one alert per file, not repeated"
        assert len([l for l in lines if "inbox file unreadable" in l]) == 1
        assert b.state_word(t1) == "DONE" and b.state_word(t2) == "QUEUED"
        bad.unlink(); tick(2)
        assert len([l for l in lines if "readable again" in l and bad.name in l]) == 1 and not b.bad_mail, "and one line when it clears"
        n = []; real_list = bridge_mod.AM.list_inbox; bridge_mod.AM.list_inbox = lambda x, **k: (n.append(x), real_list(x, **k))[1]
        try: b.last_tick = 0; b.last_sweep = CLOCK.time(); b.last_qline = CLOCK.time(); b.dirty.clear(); b.tick()
        finally: bridge_mod.AM.list_inbox = real_list
        assert sorted(n) == ["builder", "mac", "orchestrator"], ("a quiet tick is one read per agent inbox again", n)
    finally: bridge_mod.log = real

def test_unreadable_mail_file_holds_only_its_own_thread():
    o1, a, task = delegated("Pool in Kepler"); t1 = b.st["threads"][a]; assert b.state_word(t1) == "QUEUED"
    p = agent_mod.agent_dir("builder") / "inbox" / (task["id"] + ".json"); good = p.read_text(encoding="utf-8")
    p.write_text(good[:40], encoding="utf-8"); tick(2)  # half-synced: read before, so its thread is known
    assert not b.blind and b.state_word(t1) == "QUEUED" and t1.get("owed") == {"builder": task["id"]}, (b.blind, t1.get("owed"))
    text = not_done(o1, a); assert "can't be read" in text and "`builder`" in text, text
    o2, c = done_thread("Pool in Fish"); t2 = b.st["threads"][c]
    assert b.state_word(t2) == "DONE" and close_card(o2), "it does not hold another thread back from Done"
    p.write_text(good, encoding="utf-8"); tick(2)
    text = not_done(o1, a); assert "can't be read" not in text and "`builder`" in text and not b.bad_mail, text
    agent_mod.ack("builder", task["id"]); tick(2); assert b.state_word(t1) == "DONE"
    # never read before, but its text still names the thread: that thread is held, and only that one
    o3, e = done_thread("Pool in Tower"); t3 = b.st["threads"][e]; assert b.state_word(t3) == "DONE"
    new = bad_file("builder", "20261110T000001Z-0badf00d.json", '{"id": "20261110T000001Z-0badf00d", "thread": "%s", "subject": "Bui' % e); tick(2)
    assert b.state_word(t3) == "QUEUED" and b.state_word(t2) == "DONE" and not b.blind
    new.unlink(); tick(2); assert b.state_word(t3) == "DONE"

def test_done_thread_gets_new_mail_while_a_corrupt_file_is_present():
    bad_file()
    origin, mid = done_thread("Pool in Kepler"); t = b.st["threads"][mid]; settle(); wait_over()
    assert b.state_word(t) == "DONE" and close_card(origin) and fake.channels[origin]["name"].startswith(CHECK), (b.state_word(t), fake.channels[origin]["name"])
    task = agent_mod.send("orchestrator", "builder", "Build it", "Do it", reply_to=mid); tick(2)
    assert b.state_word(t) == "QUEUED", "the checkmark does not survive new mail for an agent"
    assert "`builder`" in not_done_card(origin, mid) and not close_card(origin)
    settle(); assert fake.channels[origin]["name"].startswith(icon("QUEUED")), fake.channels[origin]["name"]
def not_done_card(origin, mid):
    """not_done without the name: a rename off the checkmark has to settle first."""
    t = b.st["threads"][mid]; cards = status_msgs(origin); assert cards and CHECK not in cards[-1]["content"] and "Done" not in cards[-1]["content"], cards and cards[-1]["content"]
    prog = [m for m in fake.msgs(PROGRESS) if clean(t) in m["content"]]; assert prog and all(CHECK not in m["content"] and "DONE" not in m["content"] for m in prog), [m["content"] for m in prog]
    assert not b.closeable(mid, t) and not b.answered(t)
    return cards[-1]["content"]

def test_name_keeps_working_through_a_short_hand_off_and_gets_the_checkmark_at_done():
    grace_on()
    origin, mid = request(); t = b.st["threads"][mid]; event("started", mid); tick(); settle()
    assert fake.channels[origin]["name"].startswith(icon("WORKING")) and name_patches(origin) == 1, fake.channels[origin]["name"]
    CLOCK.offset += 60; answer(mid, "Sent to the builder."); task = agent_mod.send("orchestrator", "builder", "Build it", "Do it", reply_to=mid)
    agent_mod.ack("orchestrator", mid); event("done", mid); tick(2); grace_over(); settle()
    assert b.state_word(t) == "QUEUED" and "Queued" in status_msgs(origin)[-1]["content"], "the card says Queued"
    assert fake.channels[origin]["name"].startswith(icon("WORKING")) and name_patches(origin) == 1, ("no rename spent on a short hand-off", fake.channels[origin]["name"])
    event("started", mid, ref=task["id"], agent="builder"); tick(2); CLOCK.offset += 120; sweep_tick()
    assert fake.channels[origin]["name"].startswith(icon("WORKING")) and name_patches(origin) == 1
    answer(mid, "All built."); event("done", mid, ref=task["id"], agent="builder"); agent_mod.ack("builder", task["id"]); tick(2); done_at = CLOCK.time()
    grace_over(); assert b.state_word(t) == "DONE"; settle(); sweep_tick()
    assert fake.channels[origin]["name"].startswith(CHECK) and CLOCK.time() - done_at < 60, ("the checkmark is in the name within a minute of Done", fake.channels[origin]["name"])
    assert name_patches(origin) == 2 and len(fake.renames[origin]) == 2, "two renames in all, inside Discord's limit"

def test_hand_off_that_waits_long_is_named_queued_within_the_rename_limit():
    grace_on()
    origin, mid = request(); t = b.st["threads"][mid]; event("started", mid); tick(); settle()
    answer(mid, "Sent to the builder."); task = agent_mod.send("orchestrator", "builder", "Build it", "Do it", reply_to=mid)
    agent_mod.ack("orchestrator", mid); event("done", mid); tick(2); grace_over(); settle()
    assert fake.channels[origin]["name"].startswith(icon("WORKING")) and name_patches(origin) == 1, fake.channels[origin]["name"]
    CLOCK.offset += bridge_mod.HANDOFF_NAME_WAIT; settle(); settle()
    assert fake.channels[origin]["name"].startswith(icon("QUEUED")) and name_patches(origin) == 2, ("it has waited: the name says so", fake.channels[origin]["name"])
    event("started", mid, ref=task["id"], agent="builder"); tick(2); CLOCK.offset += 60; sweep_tick()
    answer(mid, "All built."); event("done", mid, ref=task["id"], agent="builder"); agent_mod.ack("builder", task["id"]); tick(2); grace_over(); settle(); sweep_tick()
    assert CHECK not in fake.channels[origin]["name"] and len(fake.renames[origin]) == 2, "both renames of this window are spent: the checkmark waits for the limit"
    CLOCK.offset += 300; settle(); sweep_tick()
    assert fake.channels[origin]["name"].startswith(CHECK) and name_patches(origin) == 3, ("no rename was refused by Discord", fake.channels[origin]["name"], name_patches(origin))

def test_no_done_and_no_close_in_the_tick_before_the_delegated_mail_lands():
    grace_on()
    origin, mid = request(); t = b.st["threads"][mid]; event("started", mid); tick(); calls = len(fake.calls)
    answer(mid, "Sent to the builder."); agent_mod.ack("orchestrator", mid); event("done", mid); tick()  # the done event is here, the mail file is not
    assert b.state_word(t) != "DONE" and not close_card(origin) and not b.closeable(mid, t) and not said_done(calls), b.state_word(t)
    assert all(CHECK not in m["content"] and "DONE" not in m["content"] for m in fake.msgs(PROGRESS)), [m["content"] for m in fake.msgs(PROGRESS)]
    task = agent_mod.send("orchestrator", "builder", "Build it", "Do it", reply_to=mid); tick()
    assert b.state_word(t) == "QUEUED" and "`builder`" in not_done(origin, mid)
    grace_over(); wait_over(); not_done(origin, mid); assert not said_done(calls), "Done was never shown"
    # with nothing handed on, Done and Close follow as soon as the hold is over
    o2, c = request("Pool in Fish"); t2 = b.st["threads"][c]; event("started", c); tick()
    answer(c, "Fixed."); agent_mod.ack("orchestrator", c); event("done", c); tick(2)
    assert b.state_word(t2) != "DONE" and not close_card(o2)
    CLOCK.offset += GRACE + 1; tick()
    assert b.state_word(t2) == "DONE" and close_card(o2) and not status_msgs(o2), "one tick after the hold: the answer with its Close button"

# ---- #overview, pings, read-only board channels, the sweep of old bot messages ----
def ov_on():
    """Turn the overview on for a test: its channel, and project folders so sessions label themselves without a model."""
    b.ch["overview"] = OVERVIEW; b.labels = []; b.spawn_label = lambda: b.labels.append(CLOCK.time())
    for name in ("Kepler", "Bikes", "Queue/sessions"): (tmp / name).mkdir(exist_ok=True)  # a missing sessions or mail folder holds every ending back
def ov_tick(n=1, away=False):
    """An overview pass. A test moves the clock in jumps; unless AWAY, the bridge is taken to have kept up its passes through the jump."""
    for _ in range(n):
        b.last_ov = 0
        if not away and getattr(b, "ov_seen", 0): b.ov_seen = CLOCK.time() - bridge_mod.OV_EVERY
        tick()
def ov_session(sid="s1", state="working", prompt="Fix the pool", host=MAC, project="Kepler", kind="", first=None):
    d = tmp / f"Queue/sessions/{host}"; d.mkdir(parents=True, exist_ok=True)
    fleet_mod._write(tmp / "Queue/devices" / f"{host}.json", {"host": host, "platform": "mac" if host == MAC else "windows", "at": CLOCK.time(), "repos": []})  # its device keeps beating as the clock jumps
    (d / f"{sid}.json").write_text(json.dumps({"session_id": sid, "host": host, "tool": "claude", "project": project, "state": state, "first_prompt": first or prompt, "last_prompt": prompt,
                                               "last_note": "Needs permission" if state == "needs_you" else "", "note_type": kind, "updated": CLOCK.time()}), encoding="utf-8")
def ov_label(key, topic):
    p = tmp / "Queue/topics/labels.json"; p.parent.mkdir(parents=True, exist_ok=True)
    have = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    p.write_text(json.dumps({**have, key: {"topic": topic, "at": CLOCK.time()}}), encoding="utf-8")
def ov_cards(): return [m for m in fake.msgs(OVERVIEW) if m["content"].startswith("**")]
def ov_card(topic): return next((m for m in ov_cards() if m["content"].startswith(f"**{topic}**")), None)
def sent_pings():  # every ping delivered, including ones a newer event for the same item replaced
    return sorted(pings() + [m for m in getattr(fake, "gone", []) if m["content"].startswith(f"<@{OWNER}> ")], key=lambda m: int(m["id"]))
def pings(): return [m for k, c in fake.channels.items() if k == OVERVIEW or c.get("parent") == OVERVIEW for m in c["msgs"].values() if m["content"].startswith(f"<@{OWNER}> ")]
def claim(mid): fleet_mod._write(tmp / "Queue/mail/claims" / f"{mid}.json", {"at": CLOCK.time(), "host": PC})  # what a runner writes while it runs the message
def gone_wait(): CLOCK.offset += bridge_mod.ANSWER_WAIT + 1; ov_tick()  # how long a mail thread must stay missing; a session needs GONE_AFTER
def sweep(n=1):
    for _ in range(n): CLOCK.offset += bridge_mod.SWEEP_EVERY + 1; tick()
def fresh_id(age=0): return str(int((CLOCK.time() - age - 1420070400) * 1000) << 22)  # a Discord id made AGE seconds ago
def deletes(since=0): return [p for m, p, _ in fake.calls[since:] if m == "DELETE" and "/messages/" in p and "/reactions/" not in p]  # message deletes only

def test_overview_one_card_per_topic_edited_in_place():
    ov_on(); ov_session(); ov_session(sid="s2", host=PC, prompt="Bike physics", state="idle"); ov_tick()
    assert len(ov_cards()) == 1, [m["content"] for m in fake.msgs(OVERVIEW)]
    card = ov_cards()[0]; th = b.st["sessions"][f"{MAC}/s1"]["thread"]
    assert card["content"].startswith("**Kepler**") and "Need you: 0" in card["content"] and "Working: 1" in card["content"], card["content"]
    assert f"🟡 `claude · Mac` Fix the pool · <#{th}>" in card["content"] and "💤 `claude · PC` Bike physics" in card["content"], card["content"]
    # a mail thread on the same subject joins the same card, with the link to its Discord thread
    origin, mid = request("Pool lights"); ov_label("t:" + mid, "Kepler"); ov_tick()
    assert len(ov_cards()) == 1 and ov_cards()[0]["id"] == card["id"] and f"⚪ `agents` Pool lights · <#{origin}>" in ov_cards()[0]["content"], ov_cards()[0]["content"]
    # a state change edits the card: nothing is posted again
    ov_session(state="needs_you", kind="permission_prompt"); ov_tick(); claim(mid); ov_tick()
    text = ov_cards()[0]["content"]
    assert "Need you: 1" in text and "Working: 1" in text and "🔴 `claude · Mac` Fix the pool" in text and "🟡 `agents · PC` Pool lights" in text, text
    assert text.index("🔴") < text.index("🟡") < text.index("💤"), "what needs them comes first"
    assert len(posts(OVERVIEW)) == 1 and ov_cards()[0]["id"] == card["id"], "edited in place, never reposted"
    ov_tick(3); n = fake.count("PATCH", f"/channels/{OVERVIEW}/messages/"); ov_tick(3)
    assert fake.count("PATCH", f"/channels/{OVERVIEW}/messages/") == n, "no edit when nothing changed"
    # another subject is another card
    ov_session(sid="s3", project="Bikes", prompt="Tune the gears"); ov_tick()
    assert len(ov_cards()) == 2 and ov_card("Bikes") and "Tune the gears" in ov_card("Bikes")["content"]

def test_overview_new_item_is_labelled_in_its_own_process_then_shown():
    ov_on(); ov_session(sid="s9", project="scratch-2026-11-10-abc", prompt="Explain the invoice"); ov_tick()
    assert len(b.labels) == 1 and not ov_cards(), "labelling is started, and no Unsorted card flashes by meanwhile"
    ov_tick(3); assert len(b.labels) == 1, "at most one run every 2 minutes"
    CLOCK.offset += 121; ov_session(sid="s9", project="scratch-2026-11-10-abc", prompt="Explain the invoice"); ov_tick(); assert len(b.labels) == 2
    ov_label("s:s9", "Invoices"); ov_tick(); assert ov_card("Invoices") and len(ov_cards()) == 1
    CLOCK.offset += 121; ov_tick(); assert len(b.labels) == 2, "nothing left to label, nothing started"
    # if labelling never answers, the item still shows, under Unsorted
    ov_session(sid="s10", project="scratch-2026-11-10-def", prompt="Other thing"); ov_tick(); assert not ov_card("Unsorted")
    CLOCK.offset += bridge_mod.LABEL_WAIT + 1; ov_session(sid="s10", project="scratch-2026-11-10-def", prompt="Other thing"); ov_tick()
    assert ov_card("Unsorted") and "Other thing" in ov_card("Unsorted")["content"]

def test_overview_topic_drops_off_after_an_hour():
    ov_on(); ov_session(); ov_tick(); card = ov_cards()[0]["id"]
    ov_session(state="idle"); ov_tick(); assert ov_cards()[0]["id"] == card and "💤" in ov_cards()[0]["content"]
    CLOCK.offset += 3500; ov_session(state="idle"); ov_tick(); assert ov_card("Kepler"), "still within the hour"
    CLOCK.offset += 200; ov_session(state="idle"); ov_tick()
    assert not ov_cards() and "Kepler" not in b.st["overview"]["cards"], "nothing left for an hour: the card goes"
    ov_tick(3); assert not fake.msgs(OVERVIEW), "an idle session alone does not bring it back"
    ov_session(state="working", prompt="Next job"); ov_tick(); assert ov_card("Kepler") and ov_card("Kepler")["id"] != card, "new work, new card"
    # a finished mail thread stays on its card as done for the hour, then the card goes
    origin, mid = request("Pool lights"); ov_label("t:" + mid, "Lights"); ov_tick(); assert "⚪" in ov_card("Lights")["content"]
    agent_mod.ack("orchestrator", mid); ov_tick(); assert "⚪" in ov_card("Lights")["content"], "one missed read is not an ending"
    gone_wait(); assert "✅ `agents` Pool lights" in ov_card("Lights")["content"], ov_card("Lights")["content"]
    CLOCK.offset += 3601; ov_session(state="working", prompt="Next job"); ov_tick()
    assert not ov_card("Lights") and ov_card("Kepler") and not any(k.startswith("t:") for k in b.st["overview"]["items"])

def test_ping_once_when_an_item_starts_needing_owner():
    ov_on(); ov_session(); ov_tick(); assert not sent_pings(), "the first pass only records"
    CLOCK.offset += 130; ov_session(state="needs_you", kind="permission_prompt"); ov_tick()
    card = ov_cards()[0]["id"]; p = sent_pings()
    assert len(p) == 1 and p[0]["content"].startswith(f"<@{OWNER}> 🔴 **Needs you** · `claude · Mac` Fix the pool"), [x["content"] for x in p]
    assert p[0]["channel_id"] == card and fake.channels[card]["parent"] == OVERVIEW, "inside the topic card's own thread"
    body = next(x for m, path, x in reversed(fake.calls) if m == "POST" and path == f"/channels/{card}/messages")
    assert body["allowed_mentions"] == {"users": [OWNER]}, "a real mention"
    ov_tick(4); CLOCK.offset += 30; ov_session(state="needs_you", kind="permission_prompt"); ov_tick(2)
    assert len(sent_pings()) == 1 and len(posts(OVERVIEW)) == 1, "once per event, and never a new top-level message"
    # the owner answers, it works on, and needs them again: a new event
    ov_session(state="working"); ov_tick(); CLOCK.offset += 90; ov_session(state="needs_you", kind="permission_prompt"); ov_tick()
    assert len(sent_pings()) == 2 and len(posts(OVERVIEW)) == 1
    # a restart does not ping again for what it already told them
    globals()["b"] = bridge_mod.Bridge(); b.st["started"] = 0; b.cfg["screenshots"] = False; ov_on(); ov_tick(2)
    assert len(sent_pings()) == 2

def test_ping_when_long_work_finishes_and_never_for_queued_or_working():
    ov_on(); ov_session(state="idle"); ov_tick()
    CLOCK.offset += 300; ov_session(state="working"); ov_tick()
    origin, mid = request("Pool lights"); ov_label("t:" + mid, "Kepler"); ov_tick(); claim(mid); ov_tick(2)
    assert "Working: 2" in ov_card("Kepler")["content"] and not sent_pings(), "queued and working never ping"
    CLOCK.offset += 130; ov_session(state="idle"); claim(mid); ov_tick()
    p = sent_pings(); assert len(p) == 1 and "✅ **Finished** · `claude · Mac` Fix the pool" in p[0]["content"], [x["content"] for x in p]
    agent_mod.ack("orchestrator", mid); ov_tick(); gone_wait()
    p = sent_pings(); assert len(p) == 2 and "**Finished** · `agents · PC` Pool lights" in p[1]["content"] and f"<#{origin}>" in p[1]["content"], [x["content"] for x in p]
    ov_tick(3); assert len(sent_pings()) == 2
    # work under 2 minutes ends without a word, for a session and for a mail thread
    ov_session(state="working", prompt="Quick one"); ov_tick(); CLOCK.offset += 70; ov_session(state="idle", prompt="Quick one"); ov_tick()
    o2, m2 = request("Small fix"); ov_label("t:" + m2, "Kepler"); ov_tick(); claim(m2); ov_tick(); CLOCK.offset += 30; agent_mod.ack("orchestrator", m2); ov_tick(); gone_wait()
    assert len(sent_pings()) == 2, [x["content"] for x in sent_pings()]
    # work handed from one agent to the next counts as one piece of work
    o3, m3 = request("Long chain"); ov_label("t:" + m3, "Kepler"); ov_tick(); claim(m3); ov_tick(); CLOCK.offset += 70
    (tmp / "Queue/mail/claims" / f"{m3}.json").unlink(); ov_tick(); claim(m3); ov_tick(); CLOCK.offset += 70; agent_mod.ack("orchestrator", m3); ov_tick(); gone_wait()
    assert len(sent_pings()) == 3 and "Long chain" in sent_pings()[-1]["content"]
    # a session that only waits for the next prompt has finished, it does not need them
    ov_session(state="working", prompt="Big one"); ov_tick(); CLOCK.offset += 100; ov_tick(); CLOCK.offset += 100; ov_session(state="needs_you", prompt="Big one", kind="idle_prompt"); ov_tick()
    assert len(sent_pings()) == 4 and "**Finished**" in sent_pings()[-1]["content"] and "Need you: 0" in ov_card("Kepler")["content"]

def test_ping_skipped_in_the_session_owner_is_typing_in():
    ov_on(); ov_session(state="idle"); ov_tick()
    CLOCK.offset += 300; ov_session(state="working", prompt="New ask"); ov_tick()
    CLOCK.offset += 20; ov_session(state="needs_you", prompt="New ask", kind="permission_prompt"); ov_tick(2)
    assert not pings() and "Need you: 1" in ov_card("Kepler")["content"], "the owner typed there 20 seconds ago: the card shows it, nothing pings"
    # prompt and permission request both between two passes
    ov_session(state="working", prompt="New ask"); ov_tick(); CLOCK.offset += 400
    ov_session(state="needs_you", prompt="Another ask", kind="permission_prompt"); ov_tick(); assert not pings()
    # the same session once the owner has been away for a while
    ov_session(state="working", prompt="Another ask"); ov_tick(); CLOCK.offset += 90; ov_session(state="needs_you", prompt="Another ask", kind="permission_prompt"); ov_tick()
    assert len(pings()) == 1 and "**Needs you**" in pings()[0]["content"]
    # a machine turn waking the session is not the owner typing
    ov_session(sid="s2", state="idle", prompt="Watch the build"); ov_tick(); CLOCK.offset += 300
    ov_session(sid="s2", state="working", prompt="Watch the build"); ov_tick(); CLOCK.offset += 20; ov_session(sid="s2", state="needs_you", prompt="Watch the build", kind="permission_prompt"); ov_tick()
    assert len(pings()) == 2 and "Watch the build" in pings()[-1]["content"]

def test_ping_when_a_run_fails_or_is_cut_off():
    ov_on(); origin, mid = request("Bike seat"); ov_label("t:" + mid, "Bikes"); ov_tick(); assert not sent_pings()
    event("started", mid, agent="builder"); ov_tick(); event("failed", mid, agent="builder", detail="boom"); ov_tick()
    p = sent_pings(); assert len(p) == 1 and "❌ **Failed**" in p[0]["content"] and "Bike seat" in p[0]["content"] and "`builder` on" in p[0]["content"] and f"<#{origin}>" in p[0]["content"], [x["content"] for x in p]
    assert fake.channels[p[0]["channel_id"]]["parent"] == OVERVIEW and p[0]["channel_id"] == ov_card("Bikes")["id"]
    ov_tick(3); assert len(sent_pings()) == 1, "once"
    event("started", mid, ref="r2", agent="builder"); ov_tick(); event("cutoff", mid, ref="r2", agent="builder"); ov_tick()
    assert len(sent_pings()) == 2 and "⚠️ **Cut off**" in sent_pings()[-1]["content"]
    # a run that just goes silent past the limit is cut off too
    event("started", mid, ref="r3", agent="builder"); ov_tick(); CLOCK.offset += bridge_mod.RUN_LIMIT + 5; ov_tick()
    assert len(sent_pings()) == 3 and "**Cut off**" in sent_pings()[-1]["content"]; ov_tick(2); assert len(sent_pings()) == 3
    # the failed thread ending is not also announced as finished
    agent_mod.ack("orchestrator", mid); ov_tick(); gone_wait(); assert len(sent_pings()) == 3 and "⚠️ `agents" in ov_card("Bikes")["content"]
    # work the overview never listed (a queue card) still gets its ping, on a card of its own topic
    event("started", "card-77", agent="runner", title="Nightly build"); ov_tick(); event("failed", "card-77", agent="runner", title="Nightly build", detail="x"); ov_tick()
    assert len(sent_pings()) == 4 and "Nightly build" in sent_pings()[-1]["content"] and ov_card("Unsorted") and "❌" in ov_card("Unsorted")["content"]
    # failures from before the bridge first saw the overview are not news
    globals()["b"] = bridge_mod.Bridge(); b.st["started"] = 0; b.cfg["screenshots"] = False; b.st.pop("overview"); ov_on()
    for t in b.st["threads"].values():
        for r in t["runs"].values(): r.pop("pinged", None)
    n = len(sent_pings()); ov_tick(2); assert len(sent_pings()) == n

def peers(age_of_read=5, relay_up=3600, **hosts):
    """This device's last reading from the optional liveness relay: hosts are name=push_age or name=(push_age, beat_age)."""
    rows = {h.replace("_", "-"): {"push_age": v[0], "beat_age": v[1]} if isinstance(v, tuple) else {"push_age": v, "beat_age": 10} for h, v in hosts.items()}
    bridge_mod.PEERS.write_text(json.dumps({"read_at": CLOCK.time() - age_of_read, "relay_up": relay_up, "peers": rows}), encoding="utf-8")

def as_host(name): bridge_mod.hostname = lambda: name; b.active_at = 0

def test_one_bridge_serves_while_the_other_holds_a_fresh_lease():
    real_host, real_pick, real_hosts = bridge_mod.hostname, bridge_mod.fleet.pick_host, b.cfg.get("hosts")
    b.cfg["hosts"] = ["DESK-PC", "Laptop-Mac"]  # the PC is the primary
    try:
        as_host("Laptop-Mac"); bridge_mod.fleet.pick_host = lambda hosts, fresh=300: "Laptop-Mac"  # the PC's heartbeat looks late
        bridge_mod.save("lease-DESK-PC.json", {"host": "DESK-PC", "at": CLOCK.time() - 20})
        assert not b.is_active(), "the PC renewed its lease 20 s ago: the Mac stays on standby even though the PC's heartbeat looks stale"
        CLOCK.offset += bridge_mod.LEASE_STALE; assert not b.is_active(), "90 s late is normal over a Syncthing relay: the standby does not take over yet"
        CLOCK.offset += bridge_mod.TAKEOVER_STALE; assert not b.is_active(), "10 minutes of silence in synced files alone proves nothing: an offline Mac sees exactly this"
        assert not bridge_mod.dpath("lease-Laptop-Mac.json").exists(), "and a standby that holds writes no lease"
        peers(Laptop_Mac=2, DESK_PC=5); assert not b.is_active(), "the relay heard from the PC 5 s ago: it is alive"
        peers(Laptop_Mac=2, DESK_PC=400)
        assert b.is_active() and bridge_mod.load("lease-Laptop-Mac.json", {})["host"] == "Laptop-Mac", "stale lease and the relay confirms the PC down: taken over"
        assert not bridge_mod.dpath("lease.json").exists(), "only per-host lease files are written"
        bridge_mod.fleet.pick_host = lambda hosts, fresh=300: "DESK-PC"; assert not b.is_active(), "the primary is back: the standby stops serving and renewing"
        as_host("DESK-PC"); assert not b.is_active(), "the primary waits for the standby's lease to go stale"
        CLOCK.offset += bridge_mod.LEASE_STALE; assert b.is_active() and bridge_mod.load("lease-DESK-PC.json", {})["at"] > CLOCK.time() - 5
    finally: bridge_mod.hostname, bridge_mod.fleet.pick_host = real_host, real_pick; b.cfg["hosts"] = real_hosts

def test_the_legacy_lease_file_still_counts_for_one_release():
    real_host, real_pick, real_hosts = bridge_mod.hostname, bridge_mod.fleet.pick_host, b.cfg.get("hosts")
    b.cfg["hosts"] = ["DESK-PC", "Laptop-Mac"]
    try:
        as_host("DESK-PC"); bridge_mod.save("lease.json", {"host": "Laptop-Mac", "at": CLOCK.time() - 20})
        assert not b.is_active(), "an old bridge still writing lease.json holds the lock as before"
        bridge_mod.save("lease.json", {"host": "DESK-PC", "at": CLOCK.time() - 20}); assert b.is_active(), "this host's own entry in the shared file is not another host's lease"
    finally: bridge_mod.hostname, bridge_mod.fleet.pick_host = real_host, real_pick; b.cfg["hosts"] = real_hosts

def test_the_lock_in_discord_has_no_sync_lag():
    real_host, real_hosts = bridge_mod.hostname, b.cfg.get("hosts")
    th = fake.nid(); fake.channels[th] = {"type": 12, "msgs": {}, "name": "bridge lease", "parent": PARENT, "archived": False}
    lm = fake.add_msg(th, f"DESK-PC|{CLOCK.time() - 20:.0f}", author=BOT)
    b.cfg["hosts"] = ["DESK-PC", "Laptop-Mac"]; b.cfg["lease_msg"] = [th, lm["id"]]
    try:
        bridge_mod.hostname = lambda: "Laptop-Mac"
        assert not b.is_active(), "the PC renewed 20 s ago, read live from Discord"
        CLOCK.offset += bridge_mod.DISCORD_TAKEOVER; assert b.is_active() and fake.channels[th]["msgs"][lm["id"]]["content"].startswith("Laptop-Mac|"), "150 s with no renewal: the Mac takes over"
        b.save_state()  # a serving bridge saves its state; the primary waits for that file (peer_synced)
        bridge_mod.hostname = lambda: "DESK-PC"; assert b.is_active() and fake.channels[th]["msgs"][lm["id"]]["content"].startswith("DESK-PC|"), "the primary takes its lock straight back"
        bridge_mod.hostname = lambda: "Laptop-Mac"; assert not b.is_active(), "and the Mac stops at once"
    finally: bridge_mod.hostname = real_host; b.cfg["hosts"] = real_hosts; b.cfg.pop("lease_msg", None)

def lock_thread(holder="DESK-PC", ago=20):
    th = fake.nid(); fake.channels[th] = {"type": 12, "msgs": {}, "name": "bridge lease", "parent": PARENT, "archived": False}
    lm = fake.add_msg(th, f"{holder}|{CLOCK.time() - ago:.0f}", author=BOT)
    b.cfg["hosts"] = ["DESK-PC", "Laptop-Mac"]; b.cfg["lease_msg"] = [th, lm["id"]]
    return th, lm["id"]
def lock_text(th, lm): return fake.channels[th]["msgs"][lm]["content"]

def test_lock_failure_mac_off_the_network_stays_standby_and_writes_nothing():
    """Mac off the network: Discord, Syncthing and the relay are all out of reach, so every file looks old."""
    real_host, real_pick, real_hosts = bridge_mod.hostname, bridge_mod.fleet.pick_host, b.cfg.get("hosts")
    th, lm = lock_thread(ago=20)
    try:
        as_host("Laptop-Mac"); bridge_mod.fleet.pick_host = lambda hosts, fresh=300: "Laptop-Mac"
        bridge_mod.save("lease-DESK-PC.json", {"host": "DESK-PC", "at": CLOCK.time() - 3600})
        peers(age_of_read=3600, DESK_PC=3600)  # its last relay reading is from before the outage
        fake.down = True; n = len(fake.calls)
        for _ in range(5): CLOCK.offset += 700; assert not b.is_active(), "no Discord and no fresh relay reading: never serve"
        assert not bridge_mod.dpath("lease-Laptop-Mac.json").exists() and not bridge_mod.dpath("lease.json").exists(), "nothing written while it cannot see the lock"
        assert not any(m == "PATCH" for m, _, _ in fake.calls[n:]), "and the lock is not touched"
        fake.down = False; fake.channels[th]["msgs"][lm]["content"] = f"DESK-PC|{CLOCK.time() - 5:.0f}"  # back online: the PC served all along
        assert not b.is_active(), "back online, it reads the PC's fresh lock and stays standby"
    finally: bridge_mod.hostname, bridge_mod.fleet.pick_host = real_host, real_pick; b.cfg["hosts"] = real_hosts; b.cfg.pop("lease_msg", None); fake.down = False

def test_lock_failure_pc_down_mac_takes_over_from_discord_without_the_relay():
    """PC down (or its bridge crashed): the lock in Discord goes stale; the relay is not needed for this."""
    real_host, real_hosts = bridge_mod.hostname, b.cfg.get("hosts")
    th, lm = lock_thread(ago=20)
    try:
        as_host("Laptop-Mac"); assert not bridge_mod.PEERS.exists()
        assert not b.is_active()
        CLOCK.offset += bridge_mod.DISCORD_TAKEOVER; assert b.is_active() and lock_text(th, lm).startswith("Laptop-Mac|"), "150 s with no renewal: taken over with no relay reading at all"
        peers(Laptop_Mac=2, DESK_PC=3); b.active_at = 0
        assert b.is_active(), "while the lock is readable the relay is not consulted: a PC whose machine is up but whose bridge crashed is still replaced"
    finally: bridge_mod.hostname = real_host; b.cfg["hosts"] = real_hosts; b.cfg.pop("lease_msg", None)

def test_lock_failure_lock_unreadable_needs_the_relay_to_confirm_the_pc_down():
    """Discord answers but the lock message cannot be read (deleted, permissions): synced files plus the relay decide."""
    real_host, real_pick, real_hosts = bridge_mod.hostname, bridge_mod.fleet.pick_host, b.cfg.get("hosts")
    th, lm = lock_thread(ago=20)
    try:
        as_host("Laptop-Mac"); bridge_mod.fleet.pick_host = lambda hosts, fresh=300: "Laptop-Mac"
        fake.fail = lambda m, p, body: Err(404, 10008) if p.endswith(f"/messages/{lm}") else None
        bridge_mod.save("lease-DESK-PC.json", {"host": "DESK-PC", "at": CLOCK.time() - 700})
        assert not b.is_active(), "no relay reading: hold"
        peers(age_of_read=90, DESK_PC=400); assert not b.is_active(), "a relay reading 90 s old proves nothing"
        peers(relay_up=30, DESK_PC=400); assert not b.is_active(), "a relay that restarted 30 s ago has not heard from everyone yet"
        peers(DESK_PC=(3, 500)); assert b.is_active(), "the PC pushes but its own heartbeat is stale: down"
        peers(Laptop_Mac=2); assert b.is_active(), "a relay up for an hour that never heard from the PC: down"
        peers(DESK_PC=3); assert not b.is_active(), "the PC pushed 3 s ago: stop"
        bridge_mod.save("lease-DESK-PC.json", {"host": "DESK-PC", "at": CLOCK.time() - 20}); peers(DESK_PC=400)
        assert not b.is_active(), "the relay is only ever a second proof: a fresh lease file still holds the standby"
    finally: bridge_mod.hostname, bridge_mod.fleet.pick_host = real_host, real_pick; b.cfg["hosts"] = real_hosts; b.cfg.pop("lease_msg", None); fake.fail = None

def test_lock_failure_relay_unreachable_changes_nothing_while_discord_works():
    real_host, real_hosts = bridge_mod.hostname, b.cfg.get("hosts")
    th, lm = lock_thread(holder="Laptop-Mac", ago=20)
    try:
        fleet_mod._write(tmp / "Queue/discord/state.Laptop-Mac.json", {"v": 2, "threads": {}, "q": {}, "saved": CLOCK.time() - 30})
        bridge_mod.save("lease-Laptop-Mac.json", {"host": "Laptop-Mac", "at": CLOCK.time() - 20, "state": CLOCK.time() - 31})  # its renewals and its state have synced (peer_synced)
        as_host("DESK-PC"); assert not bridge_mod.PEERS.exists()
        assert b.is_active() and lock_text(th, lm).startswith("DESK-PC|"), "the primary never asks the relay: it takes its lock straight back"
        for _ in range(20): CLOCK.offset += 30; assert b.is_active(), "and keeps serving through the outage"
        assert int(lock_text(th, lm).split("|")[1]) >= CLOCK.time() - bridge_mod.LEASE_RENEW - 1, "renewing the lock as usual"
        as_host("Laptop-Mac"); assert not b.is_active(), "the standby reads the fresh lock and holds"
    finally: bridge_mod.hostname = real_host; b.cfg["hosts"] = real_hosts; b.cfg.pop("lease_msg", None)

def test_lock_failure_discord_unreachable_the_primary_keeps_its_place():
    """Discord out of reach from both: the PC follows its file rule (it cannot post anyway); the Mac holds on the PC's file."""
    real_host, real_pick, real_hosts = bridge_mod.hostname, bridge_mod.fleet.pick_host, b.cfg.get("hosts")
    th, lm = lock_thread(ago=20)
    try:
        fake.down = True
        as_host("DESK-PC"); assert b.is_active() and bridge_mod.load("lease-DESK-PC.json", {})["host"] == "DESK-PC", "no relay needed for the primary"
        as_host("Laptop-Mac"); bridge_mod.fleet.pick_host = lambda hosts, fresh=300: "Laptop-Mac"
        peers(DESK_PC=400); assert not b.is_active(), "the PC's file lease is fresh: the standby holds even if the relay says otherwise"
        as_host("DESK-PC"); bridge_mod.save("lease-Laptop-Mac.json", {"host": "Laptop-Mac", "at": CLOCK.time() - 10})
        assert not b.is_active(), "the primary without Discord still waits out a standby's fresh file lease"
    finally: bridge_mod.hostname, bridge_mod.fleet.pick_host = real_host, real_pick; b.cfg["hosts"] = real_hosts; b.cfg.pop("lease_msg", None); fake.down = False

def test_lock_failure_a_lock_a_moment_in_the_future_is_fresh():
    real_host, real_hosts = bridge_mod.hostname, b.cfg.get("hosts")
    th, lm = lock_thread(ago=-2)  # written this second and rounded up, or by a PC whose clock runs 2 s ahead
    try:
        as_host("Laptop-Mac"); assert not b.is_active() and lock_text(th, lm).startswith("DESK-PC|")
    finally: bridge_mod.hostname = real_host; b.cfg["hosts"] = real_hosts; b.cfg.pop("lease_msg", None)

class FakeWS:
    """The gateway socket for run(): hello, then one message event, then nothing."""
    def __init__(self, url):
        self.out, self.closed = [], False
        self.inbox = [json.dumps({"op": 10, "d": {"heartbeat_interval": 41250}}),
                      json.dumps({"op": 0, "s": 1, "t": "MESSAGE_CREATE", "d": {"id": "9", "channel_id": PARENT, "author": {"id": OWNER}, "content": "hi", "attachments": [], "type": 0}})]
        FakeWS.last = self
    def recv(self, t): return self.inbox.pop(0) if self.inbox else None
    def send(self, x): self.out.append(x)
    def close(self): self.closed = True

class StopLoop(BaseException): pass

def test_after_connecting_the_lock_is_checked_again_before_any_event():
    real_ws, real_active, real_dispatch, real_sleep = bridge_mod.WS, b.is_active, b.dispatch, CLOCK.sleep
    answers, seen, last = [True, False], [], [True]
    def active(cached=False):  # like the real one: a cached ask reuses the answer from a moment ago
        seen.append(cached)
        if cached: return last[0]
        if not answers: raise StopLoop()
        last[0] = answers.pop(0); return last[0]
    try:
        bridge_mod.WS = FakeWS; b.is_active = active; b.dispatch = lambda kind, d: seen.append(kind)
        CLOCK.sleep = lambda s: None
        try: b.run(); assert False, "run should have stopped"
        except StopLoop: pass
        assert seen[:2] == [False, False], f"an uncached check right after connect: {seen}"
        assert "MESSAGE_CREATE" not in seen and FakeWS.last.closed, "the lock was lost while connecting: no event handled, socket closed"
    finally: bridge_mod.WS, b.is_active, b.dispatch, CLOCK.sleep = real_ws, real_active, real_dispatch, real_sleep

def test_an_answer_that_vanishes_is_posted_again_once():
    origin, mid = finished("**Checked:** not published yet."); th = b.st["threads"][mid]["dthread"]
    gone = next(m for m in fake.msgs(th) if "not published yet" in m["content"]); del fake.channels[th]["msgs"][gone["id"]]  # a second bridge deleted it
    CLOCK.offset += 700; b.heal_at = 0; tick()
    again = [m for m in fake.msgs(th) if "not published yet" in m["content"]]
    assert len(again) == 1 and "Posted again" in again[0]["content"], "it comes back, saying so"
    del fake.channels[th]["msgs"][again[0]["id"]]; CLOCK.offset += 700; b.heal_at = 0; tick()
    assert not any("not published yet" in m["content"] for m in fake.msgs(th)), "only once: a second removal is respected"

def test_janitor_starts_five_minutes_in_then_every_half_hour():
    runs = []; b.spawn_janitor = lambda: runs.append(CLOCK.time())
    b.maybe_janitor(CLOCK.time()); assert not runs, "off unless the config says so"
    b.cfg["janitor"] = True; b.__dict__.pop("last_janitor", None)
    b.maybe_janitor(CLOCK.time()); CLOCK.offset += 200; b.maybe_janitor(CLOCK.time()); assert not runs, "not in the first 5 minutes"
    CLOCK.offset += 110; b.maybe_janitor(CLOCK.time()); assert len(runs) == 1
    CLOCK.offset += bridge_mod.JANITOR_EVERY - 5; b.maybe_janitor(CLOCK.time()); assert len(runs) == 1
    CLOCK.offset += 10; b.maybe_janitor(CLOCK.time()); assert len(runs) == 2

def test_probe_and_codex_background_sessions_are_not_listed():
    sh = load_module("session_hook_test", "session-hook.py")
    for cwd in ("/var/folders/hp/x/T/tmp.9UqF8roq22", "/private/tmp/claude-501/a/b/scratchpad/pt-proj/proj", r"C:\Users\alex\AppData\Local\OpenAI\Codex\bin\f71a721e0e6193a9", r"C:\Users\alex\AppData\Local\Temp\x"):
        assert sh.machine(cwd), cwd
    for cwd in ("/Users/alex/Developer/Work/Kepler", r"C:\Dev\Work\Field Notebook", "/Users/alex/Library/Application Support/Claude/scratch-workspaces/b1/6f/scratch-2026-11-09-fbfdd6", ""):
        assert not sh.machine(cwd), cwd

def test_ping_thread_keeps_the_last_few_and_goes_with_the_card():
    ov_on(); ov_session(); ov_tick()
    for i in range(bridge_mod.PINGS_SHOWN + 2):
        CLOCK.offset += 90; ov_session(state="needs_you", kind="permission_prompt"); ov_tick(); ov_session(state="working"); ov_tick()
    th = ov_cards()[0]["id"]; assert len(pings()) == 1 == len(fake.msgs(th)), "one item keeps one notice: each new event replaces the last"
    ov_session(state="idle"); ov_tick(); CLOCK.offset += 3601; ov_session(state="idle"); ov_tick()
    assert th not in fake.channels and not fake.msgs(OVERVIEW), "the thread is removed with its card"

def test_ping_without_threads_is_one_replaced_message_under_the_card():
    ov_on(); fake.deny_threads = True; ov_session(); ov_tick(); card = ov_cards()[0]["id"]
    CLOCK.offset += 130; ov_session(state="needs_you", kind="permission_prompt"); ov_tick()
    p = pings(); assert len(p) == 1 and p[0]["channel_id"] == OVERVIEW and p[0]["message_reference"]["message_id"] == card
    ov_session(state="working"); ov_tick(); CLOCK.offset += 90; ov_session(state="needs_you", kind="permission_prompt"); ov_tick()
    q = pings(); assert len(q) == 1 and q[0]["id"] != p[0]["id"] and len(fake.msgs(OVERVIEW)) == 2, "the earlier ping is replaced, not added to"
    ov_session(state="idle"); ov_tick(); CLOCK.offset += 3601; ov_session(state="idle"); ov_tick(); assert not fake.msgs(OVERVIEW)

def test_pings_wait_out_a_rate_limit():
    ov_on(); ov_session(); ov_tick(); card = ov_cards()[0]["id"]
    bridge_mod.BLOCKED[bridge_mod.route("POST", f"/channels/{card}/messages")] = CLOCK.time() + 60  # the card's thread has the card's id
    CLOCK.offset += 130; bridge_mod.BLOCKED[bridge_mod.route("POST", f"/channels/{card}/messages")] = CLOCK.time() + 60
    ov_session(state="needs_you", kind="permission_prompt"); ov_tick(2); assert not pings() and b.st["overview"]["pings"]
    CLOCK.offset += 61; ov_session(state="needs_you", kind="permission_prompt"); ov_tick(2); assert len(pings()) == 1 and not b.st["overview"]["pings"]

def board_bot(cid): return [m for m in fake.msgs(cid) if m["author"]["id"] == BOT]
def test_board_message_becomes_a_request_and_is_removed():
    b.ch["overview"] = OVERVIEW; b.spawn_label = lambda: None
    for n, cid in enumerate((PROGRESS, ALERTS, REPORTS, THREADS, OVERVIEW)):
        text = f"Why did job {n} fail?\nSecond line"; m = owner_msg(text, cid=cid); tick()
        got = inbox("orchestrator")[-1]; assert got["from"] == "owner" and got["body"] == text and len(inbox("orchestrator")) == n + 1, got
        t = b.st["threads"][got["id"]]; th = t["dthread"]
        assert fake.channels[th]["parent"] == PARENT and f"Why did job {n} fail?" in fake.channels[th]["name"], "its thread is in #orchestrator"
        assert any(f"> Why did job {n} fail?\n> Second line" in x["content"] and f"<#{cid}>" in x["content"] for x in fake.msgs(th)), "their words are in the thread"
        quote = [x for _, path, x in posts(th) if "> Why did job" in x["content"]]
        assert len(quote) == 1 and f"<@{OWNER}>" not in quote[0]["content"] and quote[0]["allowed_mentions"] == {"parse": []}, "the quote does not ping them: they just typed it"
        assert "Queued" in status_msgs(th)[0]["content"], "and it is followed like any request"
        assert m not in fake.channels[cid]["msgs"] and not board_bot(cid), "the original is removed and nothing is left in its place"
    assert not b.st.get("redirects")
    a = answer(inbox("orchestrator")[-1]["id"], "Because of X."); tick(); assert any("Because of X." in x["content"] for x in fake.msgs(th)), "the answer arrives there"
    # 'builder: ...' still addresses one agent
    owner_msg("builder: look at this", cid=PROGRESS); tick(); assert inbox("builder")[-1]["body"] == "look at this"
    # other people, and anything inside a thread of a board channel, are left alone
    n = len(inbox("orchestrator")); other = owner_msg("hello", cid=PROGRESS, author="777"); tick()
    card = fake.add_msg(OVERVIEW, "**Topic**")["id"]; fake.channels[card] = {"type": 11, "msgs": {}, "name": "Topic", "parent": OVERVIEW, "archived": False}
    inside = owner_msg("a note to self", cid=card); tick()
    assert len(inbox("orchestrator")) == n and other in fake.channels[PROGRESS]["msgs"] and inside in fake.channels[card]["msgs"]
    # the same gateway event twice mails once
    m = fake.add_msg(ALERTS, "Once only", author=OWNER); d = {"id": m["id"], "channel_id": ALERTS, "author": {"id": OWNER}, "content": "Once only", "attachments": [], "type": 0}
    b.dispatch("MESSAGE_CREATE", d); b.dispatch("MESSAGE_CREATE", d); tick(); assert len(inbox("orchestrator")) == n + 1

def test_board_message_without_manage_messages_stays_with_one_reply():
    fake.deny_manage = True; m = owner_msg("Run the pool test", cid=ALERTS); tick()
    got = inbox("orchestrator")[-1]; th = b.st["threads"][got["id"]]["dthread"]; assert got["body"] == "Run the pool test"
    assert m in fake.channels[ALERTS]["msgs"], "left where it is"
    r = board_bot(ALERTS); assert len(r) == 1 and f"<#{th}>" in r[0]["content"] and r[0]["message_reference"]["message_id"] == m and r[0]["content"].startswith("Read-only"), [x["content"] for x in r]
    tick(3); sweep(3); assert len(board_bot(ALERTS)) == 1 and m in fake.channels[ALERTS]["msgs"] and len(inbox("orchestrator")) == 1, "one reply, one mail, however long it takes"
    # once the bot is given Manage Messages, the sweep removes the original and the reply
    fake.deny_manage = False; sweep()
    assert m not in fake.channels[ALERTS]["msgs"] and not board_bot(ALERTS) and not b.st["redirects"]

def test_board_message_that_cannot_be_mailed_is_kept_and_retried():
    real = agent_mod.send
    def broken(*a, **k): raise OSError("disk full")
    bridge_mod.AM.send = broken
    try: m = owner_msg("Do it later", cid=PROGRESS); tick()
    finally: bridge_mod.AM.send = real
    assert m in fake.channels[PROGRESS]["msgs"] and not inbox("orchestrator") and m in b.st["unsent"], "nothing is removed before the mail exists"
    CLOCK.offset += 40; tick()
    assert inbox("orchestrator")[-1]["body"] == "Do it later" and m not in fake.channels[PROGRESS]["msgs"] and not board_bot(PROGRESS) and not b.st["unsent"]

def test_board_message_with_a_file_that_was_not_saved_is_kept():
    for name, size in (("shot.png", 2048), ("film.mov", 200 * 1024 * 1024)):  # the harness refuses the download; the second is over the 100 MB limit
        m = fake.add_msg(PROGRESS, "Look at this", author=OWNER)["id"]; n = len(inbox("orchestrator"))
        b.dispatch("MESSAGE_CREATE", {"id": m, "channel_id": PROGRESS, "author": {"id": OWNER}, "content": "Look at this", "type": 0,
                                      "attachments": [{"filename": name, "url": "https://cdn.example/" + name, "size": size}]}); tick()
        got = inbox("orchestrator"); assert len(got) == n + 1 and name in got[-1]["body"] and ("download failed" in got[-1]["body"] or "skipped" in got[-1]["body"]), "the mail still goes out, and says so"
        th = b.st["threads"][got[-1]["id"]]["dthread"]; r = [x for x in board_bot(PROGRESS) if (x["message_reference"] or {}).get("message_id") == m]
        assert m in fake.channels[PROGRESS]["msgs"], "the only copy of their file stays"
        assert len(r) == 1 and r[0]["content"].startswith("Read-only") and f"<#{th}>" in r[0]["content"] and name in r[0]["content"], [x["content"] for x in r]
        tick(3); sweep(3); assert m in fake.channels[PROGRESS]["msgs"] and r[0]["id"] in fake.channels[PROGRESS]["msgs"] and len(inbox("orchestrator")) == n + 1, "the sweep leaves it, and nothing is mailed twice"
    # a message whose files were all saved is still removed
    real = bridge_mod.Bridge.attachments; bridge_mod.Bridge.attachments = lambda self, d, lost=None: "\n\n- Queue/attachments/x/ok.png"
    try: m = fake.add_msg(PROGRESS, "Saved one", author=OWNER)["id"]; b.dispatch("MESSAGE_CREATE", {"id": m, "channel_id": PROGRESS, "author": {"id": OWNER}, "content": "Saved one", "type": 0, "attachments": [{"filename": "ok.png"}]}); tick()
    finally: bridge_mod.Bridge.attachments = real
    assert m not in fake.channels[PROGRESS]["msgs"] and "ok.png" in inbox("orchestrator")[-1]["body"]

def test_reports_keep_the_newest_300_however_many_are_adopted():
    old = [fake.add_msg(REPORTS, f"report {i}")["id"] for i in range(350)]  # posted before reports were tracked
    for _ in range(8): sweep()
    assert b.st["sweep"].get("reports_adopted") and b.st["report_msgs"] == old[-300:], "the newest 300 are the ones adopted"
    for _ in range(6): sweep()
    have = fake.channels[REPORTS]["msgs"]; assert all(i in have for i in old[-300:]), "and none of them is removed afterwards"
    rep = tmp / "Queue/done/job.report.md"; rep.parent.mkdir(parents=True, exist_ok=True); rep.write_text("Report body", encoding="utf-8"); tick()
    mid = fake.msgs(REPORTS)[-1]["id"]; assert b.st["report_msgs"][-1] == mid and b.st["report_msgs"] == old[-299:] + [mid], "a new report pushes out the oldest, never a recent one"

def test_remote_permission_prompt_pings_once_on_its_question_card():
    ov_on(); ov_session(); ov_tick(); CLOCK.offset += 130
    ov_session(state="needs_you", kind="remote_permission"); q = question(None, body="Allow Bash?", sender="sessions"); ov_tick(2)  # what permission-hook.py does: the state, and a question to the owner
    mentions = [x for _, path, x in fake.calls if path.endswith("/messages") and x and x.get("allowed_mentions") == {"users": [OWNER]}]
    assert len(mentions) == 1 and "Allow Bash?" in mentions[0]["content"], [x["content"] for x in mentions]
    assert not pings() and "Need you: 1" in ov_card("Kepler")["content"], "the overview shows it and adds no second ping"
    # a local permission prompt has no question card, so the overview is what pings
    ov_session(state="working"); ov_tick(); CLOCK.offset += 90; ov_session(state="needs_you", kind="permission_prompt"); ov_tick(); assert len(pings()) == 1

def test_ping_follows_its_item_when_a_label_moves_it_to_another_topic():
    ask = dict(sid="s9", project="scratch-2026-11-10-abc", prompt="Explain the invoice")
    ov_on(); ov_session(**ask); ov_tick(); CLOCK.offset += 130; ov_session(**ask, state="needs_you", kind="permission_prompt"); ov_tick()
    un = ov_card("Unsorted")["id"]; p = pings(); assert len(p) == 1 and p[0]["channel_id"] == un, "no label yet: it pings under Unsorted"
    ov_tick(3); assert len(pings()) == 1 and len(mention_posts()) == 1, "nothing moved, nothing repeated"
    ov_label("s:s9", "Invoices"); ov_tick()
    assert not ov_card("Unsorted") and un not in fake.channels, "the label landed: Unsorted goes, and its thread with the first mention"
    card = ov_card("Invoices")["id"]; p = pings()
    assert len(p) == 1 and p[0]["channel_id"] == card and "**Needs you**" in p[0]["content"] and "Explain the invoice" in p[0]["content"], "the mention is in the new topic's thread"
    assert len(mention_posts()) == 2 and mention_posts()[-1][1] == f"/channels/{card}/messages", "sent once more, as a real mention"
    ov_tick(4); CLOCK.offset += 30; ov_session(**ask, state="needs_you", kind="permission_prompt"); ov_tick(2); assert len(mention_posts()) == 2, "once"
    globals()["b"] = bridge_mod.Bridge(); b.st["started"] = 0; b.cfg["screenshots"] = False; ov_on(); ov_tick(3)
    assert len(mention_posts()) == 2 and len(pings()) == 1, "a restart does not ping again"
    # moved out of a topic that stays: the old thread loses the mention, the new one has it
    ov_session(sid="s1", prompt="Fix the pool"); ov_tick(); kab = ov_card("Kepler")["id"]
    gears = dict(sid="s2", prompt="Tune the gears", project="")  # no project: a label alone picks its topic (janitor 1112f: one topic per project)
    ov_label("s:s2", "Kepler"); ov_session(**gears); ov_tick(); CLOCK.offset += 130; ov_session(**gears, state="needs_you", kind="permission_prompt"); ov_tick()
    assert [m["channel_id"] for m in pings() if "Tune the gears" in m["content"]] == [kab]
    ov_label("s:s2", "Bikes"); ov_tick(); got = [m["channel_id"] for m in pings() if "Tune the gears" in m["content"]]
    assert got == [ov_card("Bikes")["id"]] and kab in fake.channels and ov_card("Kepler"), got
    # once it no longer needs them, a later move carries nothing along
    ov_session(**gears, state="working"); ov_tick(); n = len(mention_posts()); ov_label("s:s2", "Kepler"); ov_tick(3); assert len(mention_posts()) == n
def mention_posts(): return [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/messages") and c[2] and c[2].get("allowed_mentions") == {"users": [OWNER]} and "**Needs you**" in c[2]["content"]]
def finished_pings(): return [m for m in sent_pings() if "**Finished**" in m["content"]]

def test_waiting_on_owner_does_not_count_as_work():
    ov_on(); ov_session(state="idle"); ov_tick(); CLOCK.offset += 300
    ov_session(state="working", prompt="Ask"); ov_tick(); CLOCK.offset += 10; ov_session(state="needs_you", prompt="Ask", kind="permission_prompt"); ov_tick()
    for _ in range(36): CLOCK.offset += 100; ov_session(state="needs_you", prompt="Ask", kind="permission_prompt"); ov_tick()  # an hour until they approve
    ov_session(state="working", prompt="Ask"); ov_tick(); CLOCK.offset += 5; ov_session(state="idle", prompt="Ask"); ov_tick(2)
    assert not finished_pings(), "15 seconds of work and an hour of waiting is not long work"
    # the spans on both sides of a wait add up, also across a restart during the wait
    ov_session(state="working", prompt="Ask"); ov_tick(); CLOCK.offset += 70; ov_session(state="needs_you", prompt="Ask", kind="permission_prompt"); ov_tick()
    globals()["b"] = bridge_mod.Bridge(); b.st["started"] = 0; b.cfg["screenshots"] = False; ov_on(); CLOCK.offset += 1000
    ov_session(state="working", prompt="Ask"); ov_tick(); CLOCK.offset += 70; ov_session(state="idle", prompt="Ask"); ov_tick()
    assert len(finished_pings()) == 1, "70 s + 70 s of work: the restart did not reset it"
    # the bridge was away while the session stopped working: the time away is not counted as work
    ov_session(state="working", prompt="Ask"); ov_tick(); CLOCK.offset += 20; ov_tick()
    CLOCK.offset += 1000; globals()["b"] = bridge_mod.Bridge(); b.st["started"] = 0; b.cfg["screenshots"] = False; ov_on()
    ov_session(state="needs_you", prompt="Ask", kind="permission_prompt"); ov_tick(); ov_session(state="working", prompt="Ask"); ov_tick(); CLOCK.offset += 5; ov_session(state="idle", prompt="Ask"); ov_tick()
    assert len(finished_pings()) == 1, "the restart did not inflate it"
    # a mail thread: queued time between two agents does not count either
    o, m = request("Slow chain"); ov_label("t:" + m, "Kepler"); ov_tick(); claim(m); ov_tick(); CLOCK.offset += 40; (tmp / "Queue/mail/claims" / f"{m}.json").unlink(); ov_tick()
    for _ in range(5): CLOCK.offset += 100; ov_tick()
    claim(m); ov_tick(); CLOCK.offset += 40; agent_mod.ack("orchestrator", m); ov_tick(); gone_wait(); assert len(finished_pings()) == 1, "80 s of work around 500 s in the queue"

def test_mail_thread_with_a_slow_hop_gets_one_finished_pings():
    ov_on(); origin, mid = request("Pool lights"); ov_label("t:" + mid, "Kepler"); ov_tick(); claim(mid); ov_tick()
    CLOCK.offset += 130; claim(mid); ov_tick(); agent_mod.ack("orchestrator", mid); ov_tick()  # the first agent is done; its mail to the next one is still syncing
    for _ in range(3): CLOCK.offset += 10; ov_tick()
    assert not pings() and "gone" not in b.st["overview"]["items"]["t:" + mid], "30 s without mail is not the end of the chain"
    hop = agent_mod.send("orchestrator", "builder", "Build the lights", "Go.", reply_to=mid)["id"]; ov_tick(); claim(hop); ov_tick()
    assert "🟡 `agents · PC` Pool lights" in ov_card("Kepler")["content"] and not pings()
    CLOCK.offset += 130; claim(hop); ov_tick(); agent_mod.ack("builder", hop); ov_tick(); CLOCK.offset += 30; ov_tick(); assert not pings()
    gone_wait(); assert len(finished_pings()) == 1 and "Pool lights" in finished_pings()[0]["content"], [m["content"] for m in pings()]
    ov_tick(3); assert len(pings()) == 1, "exactly one"

def test_missing_mail_or_sessions_folder_ends_nothing():
    ov_on(); ov_session(); origin, mid = request("Pool lights"); ov_label("t:" + mid, "Kepler"); ov_tick(); claim(mid); ov_tick(); CLOCK.offset += 130; ov_session(); claim(mid); ov_tick()
    for name in ("sessions", "mail"):  # the folder is briefly not there, as during a sync or with the drive away
        (tmp / "Queue" / name).rename(tmp / "Queue" / (name + ".away"))
        for _ in range(12): CLOCK.offset += 10; ov_tick()
        it = b.st["overview"]["items"]; assert not pings() and "gone" not in it["s:s1"] and "gone" not in it["t:" + mid], name
        if (tmp / "Queue" / name).is_dir(): (tmp / "Queue" / name).rmdir()  # a reader may have made it again, empty; Windows will not rename onto it. rmdir fails if anything was written
        (tmp / "Queue" / (name + ".away")).rename(tmp / "Queue" / name); ov_session(); claim(mid); ov_tick()
    assert "Working: 2" in ov_card("Kepler")["content"] and not pings()
    agent_mod.ack("orchestrator", mid); ov_tick(); gone_wait(); assert len(finished_pings()) == 1, "the real ending still pings"

def thread_deletes(th): return [c for c in fake.calls if c[0] == "DELETE" and c[1] == f"/channels/{th}"]
def test_refused_ping_thread_delete_is_logged_once_and_retried_hourly():
    ov_on(); ov_session(); ov_tick(); CLOCK.offset += 130; ov_session(state="needs_you", kind="permission_prompt"); ov_tick(); th = ov_cards()[0]["id"]; assert th in fake.channels
    fake.fail = lambda m, p, body: Err(403, 50013) if m == "DELETE" and re.fullmatch(r"/channels/\d+", p) else None; out, restore = logged()  # no Manage Threads in #overview
    try:
        ov_session(state="idle"); ov_tick(); CLOCK.offset += 3601; ov_session(state="idle"); ov_tick()
        assert not ov_cards() and not b.st["overview"]["cards"] and th in fake.channels and b.st["overview"]["orphans"] == [th], "the card goes; the thread is remembered, not assumed gone"
        ov_tick(6); CLOCK.offset += 600; ov_tick(3); assert len(thread_deletes(th)) == 1, "not tried again on every pass"
        CLOCK.offset += 3001; ov_tick(2); assert len(thread_deletes(th)) == 2 and th in fake.channels, "once an hour"
        assert len([x for x in out if "Manage Threads" in x]) == 1 and not fake.msgs(ALERTS), out
    finally: restore()
    globals()["b"] = bridge_mod.Bridge(); b.st["started"] = 0; b.cfg["screenshots"] = False; ov_on(); ov_tick(2); assert len(thread_deletes(th)) == 2, "a restart does not hammer it either"
    fake.fail = None; CLOCK.offset += 3601; ov_tick(); assert th not in fake.channels and "orphans" not in b.st["overview"], "permission granted: the thread left behind is removed"

def test_card_deleted_by_hand_takes_its_thread_along():
    ov_on(); ov_session(); ov_tick(); CLOCK.offset += 130; ov_session(state="needs_you", kind="permission_prompt"); ov_tick(); card = ov_cards()[0]["id"]; assert card in fake.channels
    del fake.channels[OVERVIEW]["msgs"][card]  # Discord keeps the thread of a deleted starter message
    ov_session(state="working"); ov_tick(); new = ov_cards()[0]["id"]
    assert new != card and card not in fake.channels and b.st["overview"]["cards"]["Kepler"].get("thread") is None, "the old thread is removed, not left behind"
    CLOCK.offset += 90; ov_session(state="needs_you", kind="permission_prompt"); ov_tick(); assert [m["channel_id"] for m in pings()] == [new]

def restart_bridge():
    globals()["b"] = bridge_mod.Bridge(); b.st["started"] = 0; b.cfg["screenshots"] = False; ov_on()
def test_outage_over_the_gap_does_not_end_a_mail_thread_between_hops():
    ov_on(); origin, mid = request("Pool lights"); ov_label("t:" + mid, "Kepler"); ov_tick(); claim(mid); ov_tick()
    CLOCK.offset += 130; claim(mid); ov_tick(); agent_mod.ack("orchestrator", mid)  # the first agent is done; its mail to the next one is still syncing
    CLOCK.offset += 600; restart_bridge(); ov_tick()  # the bridge was away ten minutes
    assert not pings() and "gone" not in b.st["overview"]["items"]["t:" + mid], "the wait for the next hop starts now, not when the bridge went away"
    hop = agent_mod.send("orchestrator", "builder", "Build the lights", "Go.", reply_to=mid)["id"]; ov_tick(); claim(hop); ov_tick()
    CLOCK.offset += 130; claim(hop); ov_tick(); agent_mod.ack("builder", hop); ov_tick(); gone_wait()
    assert len(finished_pings()) == 1, [m["content"] for m in pings()]
    ov_tick(3); assert len(finished_pings()) == 1, "exactly one"

def test_short_outage_is_not_counted_as_work():
    ov_on(); ov_session(state="idle"); ov_tick(); CLOCK.offset += 300
    ov_session(state="working", prompt="Ask"); ov_tick(); CLOCK.offset += 20; ov_tick()
    CLOCK.offset += 150; restart_bridge(); ov_session(state="idle", prompt="Ask"); ov_tick()  # down 2.5 minutes, under the old 3-minute gap; it stopped meanwhile
    assert not finished_pings(), "20 s of work seen, not 170"
    ov_session(state="working", prompt="Ask"); ov_tick(); CLOCK.offset += 20; ov_tick()
    CLOCK.offset += 150; ov_session(state="idle", prompt="Ask"); ov_tick(away=True)  # the same, with the process stalled instead of restarted
    assert not finished_pings(), "a stall is not work either"

def test_rate_limited_orphan_delete_holds_back_no_ping():
    ov_on(); ov_session(); ov_tick(); ov = b.st["overview"]; ov["orphans"], ov["orphan_at"] = ["1234"], 0
    bridge_mod.BLOCKED[bridge_mod.route("DELETE", "/channels/1234")] = CLOCK.time() + 600
    CLOCK.offset += 130; ov_session(state="needs_you", kind="permission_prompt"); ov_tick()
    assert len(pings()) == 1 and b.st["overview"]["orphans"] == ["1234"], "the ping went out; the orphan waits for a later try"
    bridge_mod.BLOCKED.clear()

def test_card_deleted_by_hand_keeps_its_open_mention():
    ov_on(); ov_session(); ov_tick(); CLOCK.offset += 130; ov_session(state="needs_you", kind="permission_prompt"); ov_tick(); card = ov_cards()[0]["id"]
    assert len(mention_posts()) == 1
    del fake.channels[OVERVIEW]["msgs"][card]; ov_session(sid="s2", prompt="Tune the gears"); ov_tick(); new = ov_cards()[0]["id"]
    assert new != card and [m["channel_id"] for m in pings()] == [new] and len(mention_posts()) == 2, "the mention is in the new card's thread"
    ov_tick(3); ov_session(sid="s3", prompt="Paint the walls"); ov_tick(2); assert len(mention_posts()) == 2, "nothing lost, nothing repeated"

def test_missing_session_ends_after_gone_after():
    ov_on(); ov_session(); ov_tick(); CLOCK.offset += 130; ov_session(); ov_tick()
    (tmp / f"Queue/sessions/{MAC}/s1.json").unlink(); ov_tick(); assert "gone" not in b.st["overview"]["items"]["s:s1"]
    CLOCK.offset += bridge_mod.GONE_AFTER + 1; ov_tick(); r = b.st["overview"]["items"]["s:s1"]
    assert "gone" in r and r["state"] == "ended", ("a session counts as gone after GONE_AFTER, not the 90 s gone_wait allows", r)

def test_sweep_removes_answered_question_cards_after_an_hour():
    origin, mid = request(); q1 = question(mid, body="First?"); tick(); q2 = question(mid, body="Second?"); tick(); q3 = question(mid, body="Third?"); tick()
    cards = {k: next(m["id"] for m in fake.msgs(origin) if any(x.get("custom_id", "").startswith(f"a:{k}:") for r in m["components"] for x in r["components"])) for k in (q1["id"], q2["id"], q3["id"])}
    press(origin, q1["id"], 0); owner_msg("the small one", cid=origin, reference={"message_id": cards[q2["id"]]}); tick()
    assert q1["id"] not in b.st["q"] and q2["id"] not in b.st["q"] and q3["id"] in b.st["q"]
    old = fake.add_msg(origin, f"<@{OWNER}> **orchestrator** asks:\nFrom last week?\n**Answer:** Yes")["id"]  # answered before cards were remembered
    said = fake.add_msg(origin, "**orchestrator**\nThe client asks: what now?\n**Answer:** nothing")["id"]; their = fake.add_msg(origin, f"<@{OWNER}> **x** asks:\nq\n**Answer:** y", author=OWNER)["id"]
    CLOCK.offset += 2900; tick(); have = fake.channels[origin]["msgs"]
    assert cards[q1["id"]] in have and cards[q2["id"]] in have, "a pass ran, but the hour is not up"
    sweep(2); have = fake.channels[origin]["msgs"]
    assert cards[q1["id"]] not in have and cards[q2["id"]] not in have, "answered by button and by text: both gone after the hour"
    assert q3["id"] in b.st["q"] and b.st["q"][q3["id"]]["msg"] in have, "an open question stays"
    assert old not in have, "and so is one answered before this was tracked"
    assert said in have and their in have and origin in fake.channels[PARENT]["msgs"], "answers, the owner's messages and anything not a question card are never touched"
    assert not b.st["answered"] and b.st["sweep"].get("backfilled")

def test_sweep_removes_resolved_and_duplicate_alerts_after_six_hours():
    origin, mid = request(); event("started", mid); event("failed", mid, detail="boom"); tick(); a1 = fake.msgs(ALERTS)[0]["id"]
    CLOCK.offset += 5 * 3600; tick(); CLOCK.offset += 2 * 3600; tick()
    assert a1 in fake.channels[ALERTS]["msgs"], "still failed: it stays as the record however old"
    event("started", mid, ref="r2"); event("failed", mid, ref="r2", detail="again"); tick(); sweep()
    left = fake.msgs(ALERTS); assert len(left) == 1 and "again" in left[0]["content"] and a1 not in fake.channels[ALERTS]["msgs"], "the older of two alerts for the same failure goes"
    a2 = left[0]["id"]; event("started", mid, ref="r3"); event("done", mid, ref="r3"); tick()
    CLOCK.offset += 5 * 3600; tick(); assert a2 in fake.channels[ALERTS]["msgs"], "resolved, but not 6 hours old"
    CLOCK.offset += 3600; sweep(); assert a2 not in fake.channels[ALERTS]["msgs"] and not b.st["alerts"], "the work succeeded since: resolved"
    # a replaceable alert goes once its problem has not come up for 6 hours
    b.alert("k", "Problem"); n = fake.msgs(ALERTS)[0]["id"]; CLOCK.offset += 5 * 3600; tick(); b.alert("k", "Problem")
    CLOCK.offset += 5 * 3600; tick(); assert n in fake.channels[ALERTS]["msgs"], "seen again 5 hours ago"
    CLOCK.offset += 3600; sweep(); assert not fake.msgs(ALERTS) and "alert:k" not in b.st["notices"]

def test_sweep_removes_progress_cards_of_finished_work_after_a_day():
    origin, mid = request("Job A"); event("started", mid); tick(); answer(mid, "All fixed."); agent_mod.ack("orchestrator", mid); event("done", mid); tick(2)
    o2, m2 = request("Job B"); event("started", m2); event("done", m2); tick(); q = question(m2); tick(2)
    pa, pb = b.st["threads"][mid]["prog"], b.st["threads"][m2]["prog"]
    assert b.state_word(b.st["threads"][m2]) == "NEEDS YOU" and {pa, pb} <= set(fake.channels[PROGRESS]["msgs"])
    CLOCK.offset += 23 * 3600; tick(); assert pa in fake.channels[PROGRESS]["msgs"], "not a day yet"
    CLOCK.offset += 2 * 3600; tick(); sweep()
    assert pa not in fake.channels[PROGRESS]["msgs"] and "prog" not in b.st["threads"][mid], "finished for a day: gone"
    assert pb in fake.channels[PROGRESS]["msgs"], "work still waiting on the owner keeps its card"
    sweep_tick(); sweep(2); assert [m["id"] for m in fake.msgs(PROGRESS)] == [pb], "and it is not posted again"
    event("started", mid, ref="r2"); tick(); assert len(fake.msgs(PROGRESS)) == 2 and "WORKING" in fake.msgs(PROGRESS)[-1]["content"], "new work on it gets a card again"

def test_sweep_removes_untracked_bot_messages_in_board_channels_only():
    ov_on(); ov_session(); origin, mid = request(); event("started", mid); event("failed", mid, detail="boom"); ov_tick()
    rep = tmp / "Queue/done/job.report.md"; rep.parent.mkdir(parents=True, exist_ok=True); rep.write_text("Report body", encoding="utf-8"); tick()
    b.alert("k", "Problem")
    tracked = {b.st["threads"][mid]["prog"], b.st["sessions"][f"{MAC}/s1"]["id"], ov_cards()[0]["id"], *b.st["alerts"], *b.st["report_msgs"], b.st["notices"]["alert:k"]["id"]}
    assert len(tracked) == 6 and all(any(i in fake.channels[c]["msgs"] for c in (PROGRESS, THREADS, OVERVIEW, ALERTS, REPORTS)) for i in tracked)
    stray = [fake.add_msg(c, "left over")["id"] for c in (PROGRESS, ALERTS, THREADS, OVERVIEW)]
    keep = [fake.add_msg(PROGRESS, "The owner's own", author=OWNER)["id"], fake.add_msg(PROGRESS, "someone else", author="777")["id"], fake.add_msg(PROGRESS, "", type=20)["id"],
            fake.add_msg(PARENT, "bot text outside the boards")["id"], fake.add_msg(origin, "bot text in a work thread")["id"], fake.add_msg(REPORTS, "a report from before reports were tracked")["id"]]
    new = fake.add_msg(PROGRESS, "posted a moment ago", mid=fresh_id())["id"]
    sweep(); every = {i for c in fake.channels.values() for i in c["msgs"]}
    assert not set(stray) & every, "the bot's own untracked messages are gone from every board channel"
    assert set(keep) <= every and tracked <= every and new in every, "nobody else's, nothing tracked, nothing outside the boards, nothing just posted"
    assert keep[-1] in b.st["report_msgs"], "old reports are adopted, not removed"
    assert all(any(f"/channels/{c}/messages/" in p for c in (PROGRESS, ALERTS, THREADS, OVERVIEW)) for p in deletes()), deletes()
    late = fake.add_msg(REPORTS, "untracked after the first walk")["id"]
    CLOCK.offset += 3600; ov_session(); sweep(2); every = {i for c in fake.channels.values() for i in c["msgs"]}
    assert new not in every and late not in every and set(keep) <= every, "after its hour the new one goes too"
    # a session card whose session is gone for good is no longer tracked
    card = b.st["sessions"][f"{MAC}/s1"]["id"]; (tmp / f"Queue/sessions/{MAC}/s1.json").unlink(); tick(); CLOCK.offset += 3601; b.last_clean = CLOCK.time(); tick()
    assert "Ended" in fake.channels[THREADS]["msgs"][card]["content"]; sweep(); assert card not in fake.channels[THREADS]["msgs"]

def test_sweep_walks_back_through_a_long_channel():
    stray = [fake.add_msg(PROGRESS, "left over")["id"] for _ in range(3)]
    for i in range(120): fake.add_msg(PROGRESS, f"note {i}", author=OWNER)
    sweep(); assert all(s in fake.channels[PROGRESS]["msgs"] for s in stray) and b.st["sweep"]["cursor"].get("progress"), "the newest page held nothing of the bot's"
    sweep(); assert not any(s in fake.channels[PROGRESS]["msgs"] for s in stray) and len(fake.msgs(PROGRESS)) == 120
    assert not b.st["sweep"]["cursor"].get("progress"), "at the end it starts again from the newest"

def test_sweep_is_bounded_per_pass_and_at_most_every_ten_minutes():
    for _ in range(bridge_mod.SWEEP_MAX + 5): fake.add_msg(PROGRESS, "left over")
    tick(3); CLOCK.offset += 300; tick(3); assert not deletes(), "no pass in the first 10 minutes after a start"
    CLOCK.offset += 301; tick(); assert len(deletes()) == bridge_mod.SWEEP_MAX and len(fake.msgs(PROGRESS)) == 5, "bounded"
    tick(5); CLOCK.offset += 500; tick(3); assert len(deletes()) == bridge_mod.SWEEP_MAX, "one pass per 10 minutes"
    CLOCK.offset += 101; tick(); assert not fake.msgs(PROGRESS) and len(deletes()) == bridge_mod.SWEEP_MAX + 5

def test_sweep_is_quiet_on_rate_limits():
    for _ in range(4): fake.add_msg(PROGRESS, "left over")
    fake.fail = lambda m, p, body: blocked429() if m == "DELETE" else None; out, restore = logged()
    try:
        sweep(); assert len(deletes()) == 1 and len(fake.msgs(PROGRESS)) == 4, "the pass stops at the first refusal"
        tick(3); assert len(deletes()) == 1 and not fake.msgs(ALERTS) and not any("sweep" in x.lower() or "rate" in x.lower() for x in out), out
    finally: restore()
    fake.fail = None; sweep(); assert not fake.msgs(PROGRESS), "and the next pass finishes the job"

def loops_on(repos=None, projects=None):
    b.ch["loops"] = LOOPS; b.budget = 50
    (tmp / "Queue/loop").mkdir(parents=True, exist_ok=True)
    (tmp / "Queue/loop/config.json").write_text(json.dumps({"enabled": True, "interval_hours": 8, "hosts": [PC], "repos": repos or {"Field Notebook": {"mode": "ask"}},
        "projects": projects or {"Stack.io": {"mode": "ask", "path": "/x", "host": MAC, "brief": "The funnel.", "every": 24}}}), encoding="utf-8")
    (tmp / "Queue/loop/state.json").write_text(json.dumps({"last": {"Field Notebook": CLOCK.time() - 3600}}), encoding="utf-8")
def loop_tick(): b.st.get("loops", {}).pop("at", None); b.render_loops(CLOCK.time())

def test_loops_board_shows_every_loop_and_edits_in_place():
    loops_on(); loop_tick(); m = board_bot(LOOPS)
    assert len(m) == 3 and "Improvement loop ON" in m[0]["content"], [x["content"] for x in m]
    assert "**Field Notebook**" in m[1]["content"] and "next <t:" in m[1]["content"] and "**Stack.io**" in m[2]["content"] and "Focus: The funnel." in m[2]["content"]
    n = len(fake.calls); loop_tick(); assert len(fake.calls) == n, "nothing changed: nothing sent"
    loops_on(repos={"Field Notebook": {"mode": "ask"}, "Kepler": {"mode": "auto"}}); loop_tick(); m = board_bot(LOOPS)
    assert len(m) == 4 and any("**Kepler**" in x["content"] and "auto mode" in x["content"] and "due now" in x["content"] for x in m), "a new loop gets its card"
    b.last_sweep = 0; sweep(); assert len(board_bot(LOOPS)) == 4, "the sweep keeps the cards it tracks"

def test_loops_message_goes_to_the_orchestrator_as_a_loop_request_and_is_removed():
    loops_on(); loop_tick(); mine = owner_msg("loop Birch, check onboarding daily", cid=LOOPS); tick()
    got = inbox("orchestrator")[-1]
    assert got["body"].startswith("Loop: Birch, check onboarding daily") and "improve-loop.py" in got["body"] and "their Add" in got["body"], got["body"]
    th = b.st["threads"][got["id"]]["dthread"]; assert any("> loop Birch" in x["content"] for x in fake.msgs(th)), "their words are in the thread"
    assert mine not in fake.channels[LOOPS]["msgs"] and len(board_bot(LOOPS)) == 3, "their message is removed; only the cards stay"
    b.dispatch("INTERACTION_CREATE", {"type": 2, "id": fake.nid(), "token": "tok", "channel_id": LOOPS, "member": {"user": {"id": OWNER}},
                                      "data": {"name": "loop", "options": [{"name": "action", "value": "add"}, {"name": "what", "value": "stop looping Kepler"}]}}); tick()
    assert inbox("orchestrator")[-1]["body"].startswith("Loop: stop looping Kepler") and "Sent to the orchestrator" in fake.responses[-1]["data"]["content"]

def test_web_prompt_becomes_a_request_once():
    w = tmp / "Queue/discord/web"; w.mkdir(parents=True, exist_ok=True)
    (w / "web-aaa.json").write_text(json.dumps({"text": "Check the Kepler shop", "thread": "", "at": 1}), encoding="utf-8"); tick()
    got = inbox("orchestrator")[-1]; assert got["body"].startswith("Check the Kepler shop") and not (w / "web-aaa.json").exists()
    th = b.st["threads"][got["id"]]["dthread"]; assert any("From outside Discord:\n> Check the Kepler shop" in x["content"] for x in fake.msgs(th))
    (w / "web-aaa.json").write_text(json.dumps({"text": "Check the Kepler shop", "thread": "", "at": 1}), encoding="utf-8"); tick()
    assert len(inbox("orchestrator")) == 1 and not (w / "web-aaa.json").exists(), "a resent prompt is not mailed twice"
    (w / "web-bbb.json").write_text(json.dumps({"text": "Also the trade UI", "thread": got["id"], "at": 2}), encoding="utf-8"); tick()
    assert any("Also the trade UI" in x["content"] for x in fake.msgs(th)), "a prompt for a thread continues it"

# ---- live card and Gemini triage ----
def triage_on(result):
    b.cfg["notepad"] = True; b.cfg["triage"] = True
    def spawn(path):
        r = result(json.loads(Path(path).read_text(encoding="utf-8"))) if callable(result) else result
        Path(path).with_name(Path(path).stem + ".out.json").write_text(json.dumps(r), encoding="utf-8")
    b.triage_spawn = spawn

def test_live_card_shows_steps_and_what_the_agent_is_doing():
    origin, mid = request(); th = b.st["threads"][mid]["dthread"]
    event("started", mid, ref="r1"); event("done", mid, ref="r1"); event("started", mid, ref="r2", agent="builder"); tick()
    live = tmp / "Queue/live"; live.mkdir(parents=True, exist_ok=True)
    (live / "r2.json").write_text(json.dumps({"doing": "Editing shop_service.py", "at": CLOCK.time()}), encoding="utf-8")
    b.last_live = 0; tick()
    text = status_msgs(th)[-1]["content"]
    assert "Plan ✓ → Build ●" in text and "`builder` on" in text and "Editing shop_service.py" in text, text

def test_triage_answers_a_status_question_without_an_agent_run():
    origin, mid = request(); th = b.st["threads"][mid]["dthread"]
    triage_on(lambda job: {"action": "answer", "thread": job["thread"], "reply": "Yes, the pool fix is done.", "model": "g"})
    n = len(inbox("orchestrator")); owner_msg("is it done?", cid=th); CLOCK.offset += 5; tick(); tick()
    assert len(inbox("orchestrator")) == n, "no agent run for a question the context answers"
    assert any(m["content"].startswith("✦ Yes, the pool fix is done.") for m in fake.msgs(th))
    assert "is it done?" in (tmp / f"Queue/discord/sidechat/{mid}.jsonl").read_text(encoding="utf-8"), "logged for the orchestrator"
    owner_msg("fix the lights too", cid=th); CLOCK.offset += 5; tick(); tick()
    assert inbox("orchestrator")[-1]["body"].startswith("fix the lights too"), "work is never just answered, whatever the model says"

def test_triage_moves_a_message_to_the_thread_it_belongs_to():
    o1, m1 = request("Fix the pool in Kepler"); o2, m2 = request("Make the Fish trailer")
    th1, th2 = b.st["threads"][m1]["dthread"], b.st["threads"][m2]["dthread"]
    triage_on({"action": "forward", "thread": m1, "reply": "Added to the pool fix."})
    owner_msg("also the pool lights", cid=th2); CLOCK.offset += 5; tick(); tick()
    last = inbox("orchestrator")[-1]; assert last["body"].startswith("also the pool lights") and last["thread"] == m2, "typed inside a thread: Gemini never moves it on its own"
    owner_msg("also the pool lights, second note"); CLOCK.offset += 5; tick(); tick()
    last = inbox("orchestrator")[-1]; assert last["body"].startswith("also the pool lights, second") and last["thread"] == m1, "from the main channel it goes to the thread it belongs to"
    assert any(m["content"].startswith("From <#") for m in fake.msgs(th1))

def test_triage_new_request_gets_its_reply_inside_the_new_thread():
    triage_on({"action": "new", "thread": "", "reply": "New request: Opus is on it."})
    owner_msg("Build a leaderboard"); tick(); tick()
    got = inbox("orchestrator")[-1]; th = b.st["threads"][got["id"]]["dthread"]
    assert got["body"].startswith("Build a leaderboard") and any(m["content"].startswith("✦ New request") for m in fake.msgs(th))

def test_triage_that_never_answers_falls_back_to_the_orchestrator():
    b.cfg["notepad"] = True; b.cfg["triage"] = True; b.triage_spawn = lambda path: None
    owner_msg("Build a leaderboard"); tick(); assert not inbox("orchestrator")
    CLOCK.offset += bridge_mod.TRIAGE_WAIT + 1; tick(); assert inbox("orchestrator")[-1]["body"].startswith("Build a leaderboard")

def test_triage_without_agy_relays_the_message_unchanged():
    triage_on({"error": "agy not installed"})
    owner_msg("Build a leaderboard"); tick(); tick()
    assert inbox("orchestrator")[-1]["body"].startswith("Build a leaderboard"), "no triage CLI: the plain relay"
    assert not any(m["content"].startswith("✦ Getting that in") for m in fake.msgs(PARENT)), "the acknowledgement goes"

def test_triage_keeps_the_question_open_when_the_owners_reply_is_a_complaint():
    origin, mid = request(); th = b.st["threads"][mid]["dthread"]
    q = question(mid, "Publish the fix?", options=("Publish", "Not now")); tick()
    card = b.st["q"][q["id"]]["msg"]
    triage_on({"action": "forward", "thread": mid, "reply": "Sent to Opus; publishing waits until this is fixed.", "answers_question": False})
    owner_msg("This screenshot only shows Discord", cid=th, reference={"message_id": card}); CLOCK.offset += 5; tick(); tick()
    assert q["id"] not in b.st["q"], "the question goes on hold"
    got = next(m for m in fake.msgs(th) if m["id"] == card)
    assert not got.get("components") and "On hold" in got["content"], "its buttons come off"
    body = inbox("orchestrator")[-1]["body"]
    assert body.startswith("This screenshot only shows Discord") and "On hold" in body and q["id"] in body, "the orchestrator fixes first, then asks again"
    CLOCK.offset += 60; tick(); tick()
    asks = lambda: [m for m in fake.msgs(th) if any(c.get("custom_id", "").startswith("a:") for r in m.get("components") or [] for c in r.get("components") or [])]
    assert not asks(), "no card is posted again below their message"
    q2 = question(mid, "Publish the fix now?", options=("Publish", "Not now")); tick()
    triage_on({"action": "forward", "thread": mid, "reply": "Publishing.", "answers_question": True})
    owner_msg("ok publish it", cid=th); CLOCK.offset += 5; tick(); tick()
    assert q2["id"] not in b.st["q"], "a real answer closes the new question"

def test_asking_again_replaces_the_older_card():
    origin, mid = request(); q1 = question(mid, "Build the aim guide?", options=("Build it", "Skip")); tick()
    q2 = question(mid, "Build the aim guide paths?", options=("Build it", "Skip")); tick()
    assert q1["id"] not in b.st["q"] and q2["id"] in b.st["q"], "one open question per asker and thread"
    assert not any("Build the aim guide?" in m["content"] for m in fake.msgs(origin)), "the old card is gone, so it cannot start the work a second time"

def smart_on(result):
    b.cfg["smart_buttons"] = True; b.cfg["feedback"] = True; seen = []
    def spawn(path):
        job = json.loads(Path(path).read_text(encoding="utf-8")); seen.append(job)
        r = result(job) if callable(result) else result
        Path(path).with_name(Path(path).stem + ".out.json").write_text(json.dumps(r), encoding="utf-8")
    b.triage_spawn = spawn; return seen

def labels(m): return [c["label"] for r in m.get("components") or [] for c in r["components"]]

def test_argon_picks_the_buttons_and_updates_the_repos_other_threads():
    o1, m1 = request("Publish Kepler and announce"); o2, m2 = request("Kepler shop fix")
    for m in (m1, m2): b.st["threads"][m]["project"] = "Kepler"
    q = question(m2, "Publish v4716 now?", options=("Publish", "Not now")); tick()
    th1, th2 = b.st["threads"][m1]["dthread"], b.st["threads"][m2]["dthread"]
    old = fake.add_msg(th1, "Not posted yet, the builder is on it.", author=BOT)["id"]
    seen = smart_on(lambda job: {"buttons": [{"label": "Push to followers", "say": "Publish the announcement to followers."}], "feedback": False,
                                 "obsolete": [old], "updates": [{"thread": m2, "note": "Published v4716 in another thread.", "drop_question": True}]})
    event("started", m1); answer(m1, "Posted the announcement. Tell me if you want it pushed to followers."); agent_mod.ack("orchestrator", m1); event("done", m1); tick(3)
    assert m2 in seen[0]["others"] and f"{old} | bot | [deletable] | Not posted yet" in seen[0]["thread_text"], "Argon sees the other Kepler thread and the whole thread, the old message marked deletable"
    ans = next(m for m in fake.msgs(th1) if "Posted the announcement" in m["content"])
    assert labels(ans)[0] == "Push to followers" and "Accept" not in labels(ans), labels(ans)
    assert not any(m["id"] == old for m in fake.msgs(th1)), "the redundant message is gone"
    assert q["id"] not in b.st["q"] and any(m["content"].startswith("✦ Published v4716") for m in fake.msgs(th2)), "the other thread is told and its stale question dropped"
    b.dispatch("INTERACTION_CREATE", {"type": 3, "id": fake.nid(), "token": "tok", "channel_id": th1, "member": {"user": {"id": OWNER}},
                                      "data": {"custom_id": f"s:{b.st['threads'][m1]['suggest']['for']}:0"}, "message": ans}); tick()
    assert inbox("orchestrator")[-1]["body"].startswith("Publish the announcement to followers."), "a tap is their message in the thread"

def test_new_buttons_take_the_old_ones_off_at_once():
    origin, mid = finished("All fixed."); th = b.st["threads"][mid]["dthread"]
    stale = fake.add_msg(th, "older result", author=BOT); stale["components"] = [{"type": 1, "components": [{"type": 2, "label": "Accept", "custom_id": "f:job:accept"}]}]
    event("started", mid, ref="r2"); answer(mid, "Fixed again."); event("done", mid, ref="r2"); tick(2)
    assert not fake.channels[th]["msgs"][stale["id"]]["components"], "only the newest buttons stay live"

def test_gallery_buttons_open_cards_and_forms_and_change_nothing():
    tap = lambda cid, typ=3, **data: b.dispatch("INTERACTION_CREATE", {"type": typ, "id": fake.nid(), "token": "tok", "channel_id": PARENT, "member": {"user": {"id": OWNER}}, "data": {"custom_id": cid, **data}})
    n = len(inbox("orchestrator")); tap("demo:open:report")
    r = fake.responses[-1]; assert r["type"] == 4 and r["data"]["flags"] == 64 | 32768 and r["data"]["components"][0]["type"] == 17, r
    tap("demo:form:0"); assert fake.responses[-1]["type"] == 9 and fake.responses[-1]["data"]["custom_id"] == "demo:form:submit"
    tap("demo:form:submit", 5, components=[{"type": 1, "components": [{"type": 4, "custom_id": "what", "value": "faster shop"}]}])
    assert "faster shop" in fake.responses[-1]["data"]["content"] and fake.responses[-1]["data"]["flags"] == 64
    tap("demo:publish:0"); assert "Nothing changed" in fake.responses[-1]["data"]["content"] and len(inbox("orchestrator")) == n, "a demo never mails an agent"

def test_split_off_work_links_both_threads():
    origin, mid = request(); th = b.st["threads"][mid]["dthread"]
    os.environ["AGENT_MAIL_PARENT"] = mid
    try: sub = agent_mod.send("orchestrator", "builder", "Field Notebook: UI pass", "separate work", new_thread=True)
    finally: os.environ.pop("AGENT_MAIL_PARENT", None)
    assert sub["thread"] == sub["id"] and sub["from_thread"] == mid, sub
    event("started", sub["id"], agent="builder"); tick(2)
    new = b.st["threads"][sub["id"]]["dthread"]
    assert any(f"Split off from <#{th}>" in m["content"] for m in fake.msgs(new)) and any(f"<#{new}>" in m["content"] for m in fake.msgs(th))

def test_a_notice_under_a_question_does_not_repost_it():
    origin, mid = request(); q = question(mid, "Publish?", options=("Publish", "Not now")); tick()
    card = b.st["q"][q["id"]]["msg"]; fake.add_msg(origin, "✦ Published v4716 in another thread.\n-# from <#1>", author=BOT)
    CLOCK.offset += 30; b.refresh_questions()
    assert b.st["q"][q["id"]]["msg"] == card, "a routing note is no reason to move the card"

def test_split_keeps_indent_and_starts_parts_at_headings():
    text = "## One\n" + ("word " * 300) + "\n## Two\n- item\n  - nested\n" + ("more " * 200)
    parts = bridge_mod.split_message(text, 1900)
    assert any(p.startswith("## Two") for p in parts), [p[:20] for p in parts]
    assert bridge_mod.fenced("```lua\nx") .endswith("```") and bridge_mod.quoted("a```b") == "a```b"

def ask_refresh(host=None, at=1): bridge_mod.save(f"refresh.{host or bridge_mod.hostname()}.json", {"at": at})
def test_refresh_goes_over_open_threads_one_at_a_time():
    o1, m1 = finished("First done."); o2, m2 = finished("Second done.")
    seen = smart_on({"buttons": [{"label": "Check it", "say": "Check it."}], "feedback": False, "obsolete": [], "updates": []})
    ask_refresh(); b.last_refresh = 0; tick()
    assert len(seen) == 1 and not list((tmp / "Queue/discord").glob("refresh*.json")), "one thread per step"
    CLOCK.offset += 20; b.last_refresh = 0; tick(); tick()
    assert len(seen) == 2 and {j["answer"] for j in seen} == {"First done.", "Second done."}

def test_refresh_from_both_hosts_each_in_its_own_file():
    """Review finding 5: either device wrote the one refresh.json. Now refresh.<host>.json, and the bridge removes only its own."""
    finished("First done."); me = bridge_mod.hostname(); d = tmp / "Queue/discord"; queued = lambda: sum("refresh queued" in l for l in bridge_mod.LOG_TAIL)
    smart_on({"buttons": [], "feedback": False, "obsolete": [], "updates": []}); n0 = queued()
    r = subprocess.run([sys.executable, "-X", "utf8", str(HERE / "discord-bridge.py"), "refresh"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert r.returncode == 0 and (d / f"refresh.{me}.json").exists() and not (d / "refresh.json").exists(), "the CLI writes this device's own request file"
    ask_refresh("OTHER-HOST", at=CLOCK.time()); other = (d / "refresh.OTHER-HOST.json").read_bytes(); b.last_refresh = 0; tick()
    assert queued() - n0 == 1 and not (d / f"refresh.{me}.json").exists(), "both requests are taken in one go; its own file is removed"
    assert (d / "refresh.OTHER-HOST.json").read_bytes() == other, "the other host's file is that host's: left as it is"
    CLOCK.offset += 20; tick(2); assert queued() - n0 == 1, "a request already acted on does not start a second round"
    ask_refresh("OTHER-HOST", at=CLOCK.time() + 5); tick(); assert queued() - n0 == 2, "a newer request from the other host does"

def test_internal_tags_files_and_status_card_stay_tidy():
    origin, mid = request(); th = b.st["threads"][mid]["dthread"]
    d = tmp / "Queue/out"; d.mkdir(parents=True, exist_ok=True)
    N = bridge_mod.MAX_FILES + 2
    for i in range(N): (d / f"part{i}.mp4").write_bytes(b"x" * (i + 1))
    body = "Built it.\n\nAREA: orchestration\n" + "\n".join(f"ATTACH: Queue/out/part{i}.mp4" for i in range(N))
    answer(mid, body); tick(2)
    assert not any("AREA:" in m["content"] for m in fake.msgs(th)), "routing tags never reach the owner"
    assert sum(1 for m in fake.msgs(th) if m["files"]) == bridge_mod.MAX_FILES and any("2 more file(s) not attached" in m["content"] for m in fake.msgs(th))
    answer(mid, "Again.\nATTACH: Queue/out/part0.mp4\nATTACH: Queue/out/part0.mp4"); tick(2)
    assert sum(1 for m in fake.msgs(th) if m["files"]) == bridge_mod.MAX_FILES + 1, "a file sent again later is sent (they may have asked for it), but only once per answer"

def test_at_most_four_attachments_the_newest_and_the_rest_listed():
    """The owner, Nov 13: an answer showed every file of the session. Only the 4 newest by mtime (the current progress) are attached."""
    origin, mid = request(); th = b.st["threads"][mid]["dthread"]
    d = tmp / "Queue/out"; d.mkdir(parents=True, exist_ok=True)
    names = [f"shot{i}.png" for i in range(7)]
    for n, age in zip(names, (5, 1, 6, 2, 7, 3, 4)): (d / n).write_bytes(b"x" * 5); os.utime(d / n, (1000 + age, 1000 + age))
    answer(mid, "All boards." + chr(10) + chr(10).join(f"ATTACH: Queue/out/{n}" for n in names)); tick(3)
    sent = [f[0] for m in fake.msgs(th) for f in m["files"]]
    assert sent == ["shot0.png", "shot2.png", "shot4.png", "shot6.png"], sent
    assert any("3 more file(s) not attached, older: shot1.png, shot3.png, shot5.png" in m["content"] for m in fake.msgs(th)), [m["content"] for m in fake.msgs(th)]

def test_janitor_1113e_loop_prompt_is_mirrored_once_per_session_thread():
    for p in ("Start the film", "/loop do a pass", "Other instruction", "/loop do a pass", "Other instruction"):
        session_file(prompt=p); tick()
    th = fake.msgs(THREADS)[0]["id"]; got = [m["content"] for m in fake.msgs(th)]
    assert got.count("> /loop do a pass") == 1 and got.count("> Other instruction") == 2, got


def card_text(m): return bridge_mod.msg_text(m)
def card_labels(m): return [c["label"] for c in bridge_mod.walk_components(m.get("components")) if c.get("type") == 2]
def card_color(m): return m["components"][0]["accent_color"]

def test_cards_carry_answers_questions_and_status_and_survive_taps():
    b.cfg["cards"] = True
    import cards as cards_mod
    origin, mid = request(); th = b.st["threads"][mid]["dthread"]
    event("started", mid); tick()
    st = next(m for m in fake.msgs(th) if "Working" in card_text(m))
    assert st.get("flags") == 32768 and card_color(st) == cards_mod.COLORS["working"], "the status is a blue card"
    q = question(mid, "Publish the fix?", options=("Publish", "Not now")); tick()
    qc = next(m for m in fake.msgs(th) if "Publish the fix?" in card_text(m))
    assert card_color(qc) == cards_mod.COLORS["ask"] and card_labels(qc) == ["Publish", "Not now", "Done elsewhere"], "a question is a yellow card with its buttons inside"
    press(th, q["id"], 0)
    r = fake.responses[-1]; assert r["type"] == 7 and "content" not in r["data"] and "**Answer:** Publish" in bridge_mod.msg_text(r["data"]), r
    for m in inbox("orchestrator"): agent_mod.ack("orchestrator", m["id"])
    event("done", mid); answer(mid, "**Built and live.** The pool is fixed."); tick(3)
    ans = next(m for m in fake.msgs(th) if "Built and live" in card_text(m))
    assert card_color(ans) == cards_mod.COLORS["done"] and "Close" in card_labels(ans), "a finished answer is a green card with Close inside"
    live = lambda m: m.get("flags") == 32768 and m["id"] != ans["id"] and card_labels(m) and (b.card_of(m["id"]) or {}).get("rows")  # a tapped card is updated by Discord itself; the fake does not apply that
    stale = next((m for m in fake.msgs(th) if live(m)), None)
    assert stale is None, ("no older card keeps buttons", card_text(stale)[:80] if stale else "", card_labels(stale) if stale else "")
    b.set_row(b.st["threads"][mid], [th, ans["id"]], [])
    assert "Built and live" in card_text(fake.channels[th]["msgs"][ans["id"]]) and not card_labels(fake.channels[th]["msgs"][ans["id"]]), "taking buttons off keeps the card's text"

def test_refresh_turns_existing_messages_into_cards():
    origin, mid = finished("**Built and live.** Old plain answer."); th = b.st["threads"][mid]["dthread"]
    old_msg = next(m for m in fake.msgs(th) if "Old plain answer" in m["content"]); old_msg["content"] += "\nAREA: orchestration"  # what an older bridge let through
    plain = next(m for m in fake.msgs(th) if "Old plain answer" in m["content"])
    b.cfg["cards"] = True; smart_on({"buttons": [], "obsolete": [], "updates": []})
    ask_refresh(); b.last_refresh = 0; tick(2)
    m = fake.channels[th]["msgs"][plain["id"]]
    assert m.get("flags") == 32768 and "Old plain answer" in card_text(m) and not m["content"], "edited in place into a card"
    assert "Close" in card_labels(m), "its buttons moved inside the card"
    assert "AREA:" not in card_text(m), "routing tags are dropped on the way"

def test_argon_thread_remembers_acts_and_escalates():
    b.cfg["argon_chat"] = True; tick()
    th = b.st["argon_thread"]; assert any("Talk to me here" in m["content"] for m in fake.msgs(th)), "the thread opens with an intro"
    origin, mid = finished("**Built.** Done."); wth = b.st["threads"][mid]["dthread"]
    junk = fake.add_msg(wth, "Nothing is built yet, waiting on your tap.", author=BOT)
    seen = []
    def spawn(path):
        job = json.loads(Path(path).read_text(encoding="utf-8")); seen.append(job)
        Path(path).with_name(Path(path).stem + ".out.json").write_text(json.dumps({"reply": "Got it, no more stale status messages.", "remember": ["Delete stale status messages."],
            "forget": [], "escalate": "Tidy stale statuses on every answer.", "actions": [{"op": "delete", "target": junk["id"], "text": ""}, {"op": "delete", "target": origin, "text": ""}]}), encoding="utf-8")
    b.triage_spawn = spawn
    n = len(inbox("orchestrator")); owner_msg("stop leaving stale status messages", cid=th); tick(2)
    assert junk["id"] in seen[0]["server"] and seen[0]["kind"] == "chat", "Argon sees the server with ids"
    assert junk["id"] not in fake.channels[wth]["msgs"] and origin in fake.channels[PARENT]["msgs"], "it deletes bot messages, never their"
    assert bridge_mod.load("argon-memory.json", [])[-1]["rule"] == "Delete stale status messages.", "the rule is remembered"
    last = inbox("orchestrator")[-1]; assert len(inbox("orchestrator")) == n + 1 and "AREA: discord-bridge" in last["body"], "a code change goes to the loop"
    reply = fake.msgs(th)[-1]["content"]; assert reply.startswith("✦ Got it") and "Remembered" in reply and "Done: deleted" in reply
    shot = tmp / "shot.png"; shot.write_bytes(b"\x89PNG")
    real_open = bridge_mod.urllib.request.urlopen
    class R:
        def __init__(s, *a, **k): pass
        def read(s, n=-1):
            out, s.done = (b"" if getattr(s, "done", False) else b"\x89PNG"), True
            return out
        def __enter__(s): return s
        def __exit__(s, *a): return False
    bridge_mod.urllib.request.urlopen = lambda req, timeout=60: R() if "cdn.example" in getattr(req, "full_url", str(req)) else real_open(req, timeout=timeout)
    try:
        m = fake.add_msg(th, "", author=OWNER)
        b.dispatch("MESSAGE_CREATE", {"id": m["id"], "channel_id": th, "author": {"id": OWNER}, "content": "", "type": 0,
                                      "attachments": [{"url": "https://cdn.example/a.png", "filename": "screen shot.png", "size": 10}]}); tick(2)
        assert seen[-1]["files"] and seen[-1]["files"][0].endswith("screen_shot.png") and seen[-1]["text"] == "(see the attached file)", seen[-1]
    finally: bridge_mod.urllib.request.urlopen = real_open

def test_every_bot_message_becomes_a_card_and_old_ones_are_swept():
    old = fake.add_msg(PARENT, "✅ **Finished** · `agents` Old ping", author=BOT)
    b.cfg["cards"] = True; smart_on({"buttons": [], "obsolete": [], "updates": []})
    mid = b.send_to(PARENT, "<@1> ⚠️ Cut off · builder on PC")
    assert fake.channels[PARENT]["msgs"][mid].get("flags") == 32768, "a new ping is a card"
    ask_refresh(); b.last_refresh = 0
    for _ in range(8): CLOCK.offset += 6; b.sweep_wait = 0; tick()
    m = fake.channels[PARENT]["msgs"][old["id"]]
    assert m.get("flags") == 32768 and "Old ping" in card_text(m), "an old plain message is converted in place"
    assert bridge_mod.card_kind("<@1> ⚠️ Cut off · builder") == "fail" and bridge_mod.card_kind("✅ **Finished** · agents") == "done"

def test_bridge_restarts_itself_on_new_code_once_it_settles():
    """Only onto tested code: tested-<code_hash>.ok, written by this harness after a full pass, must exist for the new code."""
    code, real_tk = tmp / "toolkit", bridge_mod.TK; code.mkdir()
    for n in bridge_mod.RELOAD_FILES: shutil.copy2(real_tk / n, code / n)
    b.cfg["self_reload"] = True; bridge_mod.TK = code; f = code / "cards.py"; orig = f.stat().st_mtime
    def settle():
        CLOCK.offset += 16; b.code_check = 0; b.maybe_reload(CLOCK.time())  # seen changing: waits
        CLOCK.offset += 25; b.code_check = 0; b.maybe_reload(CLOCK.time())
    try:
        b.code_check = 0; b.maybe_reload(CLOCK.time()); ran = b.code_ran
        os.utime(f, (orig + 5, orig + 5)); settle()  # touched but the same code: no restart (a SystemExit here fails the test)
        f.write_text(f.read_text(encoding="utf-8") + "\nUNTESTED = 1\n", encoding="utf-8"); os.utime(f, (orig + 9, orig + 9))
        settle(); new = bridge_mod.code_hash(code); assert new != ran
        for _ in range(3): CLOCK.offset += 16; b.code_check = 0; b.maybe_reload(CLOCK.time())
        assert sum(f"has no tested-{new}.ok marker" in l for l in bridge_mod.LOG_TAIL) == 1, "compiles but untested: it stays on the old code and says why, once"
        bridge_mod.save(f"tested-{new}.ok", {"code": new})
        try: CLOCK.offset += 16; b.code_check = 0; b.maybe_reload(CLOCK.time()); assert False, "should exit once the marker arrives"
        except SystemExit: pass
    finally: bridge_mod.TK = real_tk; b.cfg["self_reload"] = False

def test_the_harness_marks_only_a_full_pass_of_unchanged_code():
    out, was = tmp / "marks", os.environ.get("BRIDGE_TESTED_DIR"); os.environ["BRIDGE_TESTED_DIR"] = str(out); h = bridge_mod.code_hash(HERE)
    try:
        mark_tested(h, "lock", 0, 5); mark_tested(h, "", 1, 5); mark_tested("0000000000", "", 0, 5)
        assert not out.exists(), "a filtered run, a failure, or code that changed during the run writes nothing"
        mark_tested(h, "", 0, 5); assert json.loads((out / f"tested-{h}.ok").read_text(encoding="utf-8"))["code"] == h
    finally:
        if was is None: os.environ.pop("BRIDGE_TESTED_DIR", None)
        else: os.environ["BRIDGE_TESTED_DIR"] = was  # the run's own marker folder

def test_without_overview_threads_a_ping_goes_to_the_works_own_thread():
    b.cfg["overview_threads"] = False; had = "overview" in b.ch; b.ch.setdefault("overview", PARENT)
    try:
        origin, mid = request(); th = b.st["threads"][mid]["dthread"]
        ov = b.st.setdefault("overview", {"cards": {}, "items": {}, "pings": []})
        ov["items"]["m:" + mid] = {"topic": "Kepler", "state": "needs_you", "title": "Fix the pool"}
        c = {"id": fake.add_msg(b.ch["overview"], "card", author=BOT)["id"], "thread": "999"}; ov["cards"]["Kepler"] = c
        fake.channels["999"] = {"type": 11, "msgs": {}, "name": "Kepler", "parent": b.ch["overview"], "archived": False}
        ov.pop("threads_dropped", None); b.drop_overview_threads(ov); assert "999" not in fake.channels and "thread" not in c, "the topic thread is deleted"
        m1 = b.post_ping("Kepler", c, "<@1> needs you", "m:" + mid); m2 = b.post_ping("Kepler", c, "<@1> needs you again", "m:" + mid)
        assert m2 in fake.channels[th]["msgs"] and m1 not in fake.channels[th]["msgs"], "the ping lands in the work's thread, newest replacing the last"
    finally:
        b.cfg["overview_threads"] = True
        if not had: b.ch.pop("overview", None)

def test_messages_sent_while_the_bridge_restarted_are_caught_up():
    origin, mid = request(); th = b.st["threads"][mid]["dthread"]
    owner_msg("first note", cid=th); CLOCK.offset += 400; tick()
    n = len(inbox("orchestrator"))
    missed = fake.add_msg(th, "make a one time webpage", author=OWNER)  # arrived while the gateway was down: no MESSAGE_CREATE
    b.catch_up(); CLOCK.offset += 400; tick()
    assert len(inbox("orchestrator")) == n + 1 and inbox("orchestrator")[-1]["body"].startswith("make a one time webpage"), "the missed message is handled"
    b.catch_up(); CLOCK.offset += 400; tick(); assert len(inbox("orchestrator")) == n + 1, "never twice"

def test_card_kind_reads_the_message():
    k = bridge_mod.card_kind
    assert k("🟡 **Working** · Plan ✓") == "working" and k("🔴 **Needs you** · x") == "ask" and k("**Nothing is built yet.** waiting") == "ask"
    assert k("**Built and checked, live.**") == "done" and k("**Build failed** at ShopService") == "fail" and k("✦ Published v4716") == "update"
    assert k("<@1> **orchestrator** asks:\nPublish?") == "ask" and k("<@1> **orchestrator** asks:\nPublish?\n**Answer:** Publish") == "info"

def test_instant_ack_then_adding_this_too_then_the_reply_in_the_same_message():
    origin, mid = request(); th = b.st["threads"][mid]["dthread"]
    b.cfg["notepad"] = True; b.cfg["triage"] = True; jobs = []; b.health = "done"  # the start-up health check is not this test
    b.triage_spawn = lambda path: jobs.append(Path(path))
    owner_msg("fix the shop button", cid=th)
    ack = next(m for m in fake.msgs(th) if m["content"] == "✦ Getting that in…")
    CLOCK.offset += 5; tick(); assert len(jobs) == 1, "Carbon gets the first message"
    owner_msg("and make it bigger", cid=th)
    assert fake.channels[th]["msgs"][ack["id"]]["content"] == "✦ Adding this too…", "a follow-up edits the same acknowledgement"
    CLOCK.offset += 5; tick(); assert len(jobs) == 1, "the follow-up waits until the first is read, in order"
    jobs[0].with_name(jobs[0].stem + ".out.json").write_text(json.dumps({"action": "forward", "thread": mid, "reply": "Fixing the shop button.", "answers_question": False}), encoding="utf-8")
    tick(); tick()
    assert fake.channels[th]["msgs"][ack["id"]]["content"] == "✦ Fixing the shop button.", "the acknowledgement becomes the reply"
    assert len(jobs) == 2 and any(m["content"] == "✦ Getting that in…" and m["id"] != ack["id"] for m in fake.msgs(th)), "the follow-up is read next, with its own acknowledgement"

def test_triage_never_answers_a_history_question_itself():
    origin, mid = request(); th = b.st["threads"][mid]["dthread"]
    q = question(mid, "Publish the fix?", options=("Publish", "More changes")); tick()
    triage_on({"action": "answer", "thread": mid, "reply": "The throw fix was saved.", "answers_question": False})
    n = len(inbox("orchestrator")); owner_msg("First what has changed since last publish?", cid=th); CLOCK.offset += 5; tick(); tick()
    assert len(inbox("orchestrator")) == n + 1 and inbox("orchestrator")[-1]["body"].startswith("First what has changed"), "an agent researches it"
    assert q["id"] not in b.st["q"] and not any("The throw fix was saved." in m["content"] for m in fake.msgs(th)), "no thin Gemini answer; the question waits until theirs is answered"

def test_setup_creates_overview_on_top_and_marks_board_channels_read_only():
    layout(); del fake.channels[OVERVIEW]; cfg = run_setup(); ov = cfg["channels"]["overview"]; c = fake.channels[ov]
    assert c["name"] == "overview" and c["parent"] == "2000" and c["position"] == 0 and all(fake.channels[x]["position"] > 0 for x in (PARENT, PROGRESS, REPORTS, ALERTS)), "first in Agents"
    assert "position" not in fake.channels[THREADS], "other categories are left alone"
    for key in ("overview", "progress", "alerts", "reports", "threads"):
        assert fake.channels[cfg["channels"][key]]["topic"].startswith("Read-only: the bot posts here. Type in #orchestrator."), key
    assert not fake.channels[PARENT]["topic"].startswith("Read-only") and len(fake.channels[ov]["topic"]) <= 1024
    fake.channels[ov]["position"] = 3; n = fake.count("POST", f"/guilds/{GUILD}/channels"); cfg2 = run_setup()
    assert fake.count("POST", f"/guilds/{GUILD}/channels") == n and cfg2["channels"]["overview"] == ov and fake.channels[ov]["position"] == 3, "run again: found, not made or moved"

# ---- a question's buttons stay at the bottom; screenshots only for permission prompts ----
def q_card(cid, key): return [m for m in fake.msgs(cid) if any(x.get("custom_id", "").startswith(f"a:{key}:") for r in m["components"] for x in r["components"])]
def asked(body="Which one?"):
    """One open agent question in its thread, with the card as the last message."""
    origin, mid = request(); q = question(mid, body); tick(); st = b.st["q"][q["id"]]
    assert st["channel"] == origin and fake.msgs(origin)[-1]["id"] == st["msg"], "starts as the last message"
    return origin, q["id"], st
def writes(cid, since): return [c for c in fake.calls[since:] if c[0] in ("POST", "DELETE", "PATCH") and c[1].startswith(f"/channels/{cid}/messages")]
def recent_id(): return str(int(CLOCK.time() * 1000 - 1420070400000) << 22)  # a Discord id carries the time it was made

def test_agent_question_requests_no_screenshot():
    shots_on(); origin, mid = request(); q = question(mid); tick(3)
    assert q_card(origin, q["id"]), "the question itself is posted"
    assert not [m for c in fake.channels for m in images(c)] and not any("No screenshot" in m["content"] for c in fake.channels for m in fake.msgs(c))
    s = b.st.get("shots") or {}; assert not s.get("wait") and not s.get("log") and f"q:{q['id']}" not in s.get("seen", {}), s
    assert not list((tmp / "Queue/captures").glob("*")) if (tmp / "Queue/captures").is_dir() else True, "nothing asked of another device either"
    p = agent_mod.send("sessions", "owner", "Permission: Bash", f"Host: {bridge_mod.hostname()}\nProject: x\nTool: Bash", needs_reply=True, options=[{"label": "Allow once"}, {"label": "Deny"}]); tick(3)
    assert f"q:{p['id']}" in b.st["shots"]["seen"], "a permission prompt from a session still asks for one"

def test_question_reposted_at_the_bottom_when_something_lands_below():
    origin, key, st = asked(); old = fake.channels[origin]["msgs"][st["msg"]]; old_id, text, row = old["id"], old["content"], old["components"]
    fake.add_msg(origin, "a status card"); n = len(fake.calls); b.refresh_questions()
    cards = q_card(origin, key); assert len(cards) == 1 and cards[0]["id"] != old_id and old_id not in fake.channels[origin]["msgs"], "one card, the old copy deleted"
    assert fake.msgs(origin)[-1]["id"] == cards[0]["id"] == st["msg"], "last in the thread and tracked"
    assert cards[0]["content"] == text and cards[0]["components"] == row, "same words, same buttons"
    assert [c[0] for c in writes(origin, n)] == ["POST", "DELETE"], writes(origin, n)
    press(origin, key, 1); tick(); r = inbox("orchestrator")[-1]; assert r["reply_to"] == key and r["body"] == "B", "the new copy's buttons answer"

def test_question_repost_does_not_ping():
    origin, key, st = asked(); first = [c for c in posts(origin) if (c[2] or {}).get("components")]
    assert first[-1][2]["allowed_mentions"] == {"users": [b.owner]}, "the first post pings once"
    fake.add_msg(origin, "below"); n = len(fake.calls); b.refresh_questions()
    again = posts(origin, n); assert len(again) == 1 and again[0][2]["allowed_mentions"] == {"parse": []} and again[0][2]["components"], again

def test_question_already_last_is_left_alone():
    origin, key, st = asked(); mid = st["msg"]
    for _ in range(3):
        n = len(fake.calls); CLOCK.offset += 30; b.refresh_questions()
        assert not writes(origin, n) and st["msg"] == mid and "moved" not in st

def test_question_in_the_orchestrator_channel_is_not_moved():
    origin, key, st = asked(); card = fake.add_msg(PARENT, "card", components=fake.channels[origin]["msgs"][st["msg"]]["components"])
    st["channel"], st["msg"] = PARENT, card["id"]; fake.add_msg(PARENT, "The owner's next request", author=OWNER); fake.add_msg(PARENT, "a bot line")
    n = len(fake.calls); CLOCK.offset += 30; b.refresh_questions()
    assert not writes(PARENT, n) and st["msg"] == card["id"] and not [c for c in fake.calls[n:] if c[0] == "GET" and c[1].startswith(f"/channels/{PARENT}/messages?limit=")], "the main channel is never reshuffled"

def test_question_move_waits_20_seconds_between_moves():
    origin, key, st = asked(); fake.add_msg(origin, "one"); b.refresh_questions(); first = st["msg"]
    fake.add_msg(origin, "two"); n = len(fake.calls); CLOCK.offset += 19; b.refresh_questions()
    assert st["msg"] == first and not writes(origin, n), "a busy thread does not make the card jump every pass"
    CLOCK.offset += 2; b.refresh_questions()
    assert st["msg"] != first and fake.msgs(origin)[-1]["id"] == st["msg"] and len(q_card(origin, key)) == 1

def test_question_stays_put_while_owner_is_typing_below_it():
    origin, key, st = asked(); mid = st["msg"]; fake.add_msg(origin, "B I think", author=OWNER, mid=recent_id())
    n = len(fake.calls); CLOCK.offset += 14; b.refresh_questions()
    assert st["msg"] == mid and not writes(origin, n), "their words may be the answer"
    CLOCK.offset += 2; b.refresh_questions()
    assert st["msg"] != mid and fake.msgs(origin)[-1]["id"] == st["msg"], "after 15 s it moves below their message"
    origin, key, st = asked("Another?"); mid = st["msg"]; fake.add_msg(origin, "a bot line", mid=recent_id()); b.refresh_questions()
    assert st["msg"] != mid, "only the owner's own message holds it back"

def test_question_move_survives_a_failed_delete_of_the_old_copy():
    origin, key, st = asked(); old = st["msg"]; fake.add_msg(origin, "below"); lines, undo = logged()
    fake.fail = lambda m, p, body: Err(403, 50013) if m == "DELETE" and p.endswith(f"/messages/{old}") else None
    try:
        b.q_bottom(key, st)  # called directly: it must not raise by itself
        assert st["msg"] != old and fake.msgs(origin)[-1]["id"] == st["msg"], "the new copy is the tracked one"
        assert sum("old question copy not removed" in l for l in lines) == 1, lines
        fake.fail = None; old = st["msg"]; del fake.channels[origin]["msgs"][old]; fake.add_msg(origin, "below again")   # the tracked copy was removed by hand
        before = len(lines); CLOCK.offset += 21; b.q_bottom(key, st)
        assert st["msg"] != old and fake.msgs(origin)[-1]["id"] == st["msg"] and not any("old question copy not removed" in l for l in lines[before:]), "an old copy that is already gone is not an error"
    finally: fake.fail = None; undo()

def test_question_in_a_closed_thread_is_not_moved():
    origin, key, st = asked(); mid = st["msg"]; fake.add_msg(origin, "below")
    fake.channels[origin]["archived"] = True; b.st["threads"][st["thread"]]["archived"] = True
    n = len(fake.calls); CLOCK.offset += 30; b.refresh_questions()
    assert fake.channels[origin]["archived"] and st["msg"] == mid and not writes(origin, n), "moving it would reopen a thread the owner closed"

def test_two_open_questions_share_the_bottom_without_leapfrogging():
    origin, mid = request(); q1 = question(mid, "First?"); tick(); q2 = question(mid, "Second?"); tick()
    a, c = b.st["q"][q1["id"]], b.st["q"][q2["id"]]; assert a["channel"] == c["channel"] == origin
    for _ in range(3):
        n = len(fake.calls); CLOCK.offset += 30; b.refresh_questions()
        assert not writes(origin, n), "both are at the bottom already"
    fake.add_msg(origin, "a status card"); CLOCK.offset += 30; b.refresh_questions()
    assert {m["id"] for m in fake.msgs(origin)[-2:]} == {a["msg"], c["msg"]}, "both moved below it"
    n = len(fake.calls); CLOCK.offset += 30; b.refresh_questions(); assert not writes(origin, n), "and then they rest"

def press_msg(msg, key, i):
    """A tap on one given copy of a card, which may be a copy that no longer exists."""
    b.dispatch("INTERACTION_CREATE", {"type": 3, "id": fake.nid(), "token": "tok", "channel_id": msg["channel_id"], "member": {"user": {"id": OWNER}}, "data": {"custom_id": f"a:{key}:{i}"}, "message": msg})
def stranded():
    """An open question whose first move could not delete the old copy: (thread, key, state, old id). Deletes still fail."""
    origin, key, st = asked(); old = st["msg"]; fake.add_msg(origin, "below"); fail_delete(); b.refresh_questions()
    assert st["msg"] != old and st["old"] == [old] and len(q_card(origin, key)) == 2, "the copy that stayed is remembered on the question"
    return origin, key, st, old

def test_old_question_copy_that_would_not_delete_is_retried_until_gone():
    lines, undo = logged()
    try:
        origin, key, st, old = stranded(); dels = lambda n: [c for c in fake.calls[n:] if c[0] == "DELETE" and c[1].endswith(f"/messages/{old}")]
        n = len(fake.calls); CLOCK.offset += 29; b.refresh_questions(); assert not dels(n), "not again before 30 s"
        CLOCK.offset += 2; b.refresh_questions(); assert len(dels(n)) == 1 and st["old"] == [old], "retried, Discord still refuses"
        n = len(fake.calls); CLOCK.offset += 59; b.refresh_questions(); assert not dels(n), "the wait doubles"
        fake.fail = None; CLOCK.offset += 2; b.refresh_questions()
        assert old not in fake.channels[origin]["msgs"] and len(q_card(origin, key)) == 1 and fake.msgs(origin)[-1]["id"] == st["msg"], "one card left, the last message"
        assert not any(k in st for k in ("old", "old_n", "old_at")), st
        assert sum("old question copy not removed" in l for l in lines) == 1, lines
        n = len(fake.calls); CLOCK.offset += 700; b.refresh_questions(); assert not writes(origin, n), "and nothing more to do"
    finally: fake.fail = None; undo()

def test_old_question_copy_retry_waits_at_most_10_minutes():
    try:
        origin, key, st, old = stranded()
        for _ in range(8): CLOCK.offset += 601; b.refresh_questions()
        assert st["old"] == [old] and st["old_n"] == 9 and 598 < st["old_at"] - CLOCK.time() <= 600, st
    finally: fake.fail = None

def test_dropped_question_takes_its_stranded_old_copy_with_it():
    try:
        origin, key, st, old = stranded()
        b.drop_q(key); assert key in b.st["q"], "kept while Discord refuses, so the next pass tries again"
        fake.fail = None; b.drop_q(key)
        assert key not in b.st["q"] and not q_card(origin, key), "both copies went with the question"
    finally: fake.fail = None

def test_answered_question_removes_its_stranded_old_copy():
    try:
        origin, key, st, old = stranded(); new = st["msg"]
        press_msg(fake.channels[origin]["msgs"][new], key, 0)  # answered during the outage: the copy is parked, not forgotten
        assert key not in b.st["q"] and b.st["q_old"][0]["old"] == [old] and old in fake.channels[origin]["msgs"]
        fake.fail = None; CLOCK.offset += 601; b.refresh_questions()
        assert old not in fake.channels[origin]["msgs"] and new in fake.channels[origin]["msgs"] and not b.st["q_old"], "the old copy goes, the answered card stays as the record"
        assert [m["body"] for m in inbox("orchestrator") if m.get("reply_to") == key] == ["A"]
        origin, key, st, old = stranded(); fake.fail = None; owner_msg("the second one", cid=origin)  # a typed answer, Discord well again
        assert key not in b.st["q"] and old not in fake.channels[origin]["msgs"] and not b.st["q_old"], "removed at the answer"
    finally: fake.fail = None

def test_tap_on_the_stranded_old_copy_answers_once_and_removes_it():
    try:
        origin, key, st, old = stranded(); new = st["msg"]; fake.fail = None
        press_msg(fake.channels[origin]["msgs"][old], key, 1)
        assert old not in fake.channels[origin]["msgs"] and [m["body"] for m in inbox("orchestrator") if m.get("reply_to") == key] == ["B"]
        card = fake.channels[origin]["msgs"][new]; assert card["content"].endswith("**Answer:** B") and not card["components"], card
    finally: fake.fail = None

def test_old_question_copy_already_gone_is_not_retried():
    origin, key, st = asked(); old = st["msg"]; fake.add_msg(origin, "below"); lines, undo = logged()
    fake.fail = lambda m, p, body: Err(404, 10008) if m == "DELETE" and p.endswith(f"/messages/{old}") else None
    try:
        b.refresh_questions(); assert st["msg"] != old and "old" not in st and not b.st.get("q_old"), st
        n = len(fake.calls); CLOCK.offset += 700; b.refresh_questions(); b.drop_q(key)
        assert not [c for c in fake.calls[n:] if c[0] == "DELETE" and c[1].endswith(f"/messages/{old}")] and not any("not removed" in l for l in lines), "a 404 is the end of it"
    finally: fake.fail = None; undo()

def test_question_error_is_logged_once_and_a_deleted_thread_is_left_alone():
    origin, key, st = asked(); fake.add_msg(origin, "below"); lines, undo = logged()
    look = lambda c: c[0] == "GET" and c[1].startswith(f"/channels/{origin}/messages?limit=")
    try:
        fake.fail = lambda m, p, body: Err(500, 0) if look((m, p)) else None
        for _ in range(5): CLOCK.offset += 10; b.refresh_questions()
        assert sum("ERR question" in l for l in lines) == 1, lines
        CLOCK.offset += 600; b.refresh_questions(); assert sum("ERR question" in l for l in lines) == 2, "said again after 10 minutes"
        fake.fail = lambda m, p, body: Err(404, 10003) if look((m, p)) else None
        n = len(fake.calls); before = len(lines)
        for _ in range(4): CLOCK.offset += 30; b.refresh_questions()
        assert len([c for c in fake.calls[n:] if look(c)]) == 1 and st.get("lost"), "a thread that is gone is asked about once"
        assert not any("ERR question" in l for l in lines[before:]) and sum("thread is gone" in l for l in lines) == 1, lines
        assert key in b.st["q"] and not writes(origin, n), "the question itself stays open"
    finally: fake.fail = None; undo()

def test_tap_on_a_replaced_copy_marks_the_surviving_card_answered():
    origin, key, st = asked(); old = dict(fake.channels[origin]["msgs"][st["msg"]]); fake.add_msg(origin, "below"); b.refresh_questions()
    new = st["msg"]; assert new != old["id"] and old["id"] not in fake.channels[origin]["msgs"], "the tapped copy was replaced a moment before"
    press_msg(old, key, 1)
    assert [m["body"] for m in inbox("orchestrator") if m.get("reply_to") == key] == ["B"], "answered once"
    card = fake.channels[origin]["msgs"][new]; assert card["content"].endswith("**Answer:** B") and not card["components"], card
    n = len(fake.calls); press_msg(card, key, 0); assert [m["body"] for m in inbox("orchestrator") if m.get("reply_to") == key] == ["B"], "a later tap changes nothing"

def only_old_fails(old, status=503):
    """Only the delete of this one old copy fails; every other call works."""
    fake.fail = lambda m, p, body: Err(status, 0) if m == "DELETE" and p.endswith(f"/messages/{old}") else None
def old_dels(old, n=0): return [c for c in fake.calls[n:] if c[0] == "DELETE" and c[1].endswith(f"/messages/{old}")]

def test_withdrawn_question_with_an_undeletable_old_copy_stays_withdrawn():
    lines, undo = logged()
    try:
        origin, key, st, old = stranded(); only_old_fails(old); mid = st["thread"]
        agent_mod.amend("orchestrator", key); tick()  # the agent withdraws it: the card deletes, the old copy does not
        assert key not in b.st["q"] and not b.st["threads"][mid].get("asking"), "the question is gone"
        assert [r["old"] for r in b.st["q_old"]] == [[old]], "the copy that stayed is parked for the backed-off retry"
        n = len(fake.calls); fake.add_msg(origin, "below")
        for _ in range(3): CLOCK.offset += 30; b.refresh_questions(); tick()
        assert not [c for c in writes(origin, n) if c[0] == "POST"] and [m["id"] for m in q_card(origin, key)] == [old], "the card is not posted again"
        owner_msg("carry on with the pool", cid=origin); tick()
        assert not any("discord -> answer" in l for l in lines) and any("discord -> thread" in l for l in lines), "their next message is not an answer to it"
        sent = next(m for m in inbox("orchestrator") if m["body"] == "carry on with the pool"); assert sent["thread"] == mid and sent.get("needs_reply"), sent
        fake.fail = None; CLOCK.offset += 601; b.refresh_questions()
        assert old not in fake.channels[origin]["msgs"] and not b.st["q_old"], "the copy goes once Discord lets it"
    finally: fake.fail = None; undo()

def test_stale_question_with_an_undeletable_old_copy_is_not_retried_every_pass():
    lines, undo = logged()
    try:
        origin, key, st, old = stranded(); only_old_fails(old); b.q_stale = lambda q: True; n = len(fake.calls); before = len(lines)
        for _ in range(30): CLOCK.offset += 10; b.refresh_questions()
        assert key not in b.st["q"] and not [m for m in q_card(origin, key) if m["id"] != old], "dropped on the first pass"
        assert len(old_dels(old, n)) <= 4, f"backed off, not one try per pass: {len(old_dels(old, n))}"
        assert not any("delete failed" in l or "not removed" in l for l in lines[before:]), lines[before:]
        assert [r["old"] for r in b.st["q_old"]] == [[old]]
    finally: fake.fail = None; undo()

def test_reply_to_a_stranded_old_copy_answers_its_own_question():
    try:
        origin, mid = request(); q1 = question(mid, "First?"); tick(); a = b.st["q"][q1["id"]]; old = a["msg"]
        fake.add_msg(origin, "below"); only_old_fails(old); b.refresh_questions()
        assert a["old"] == [old] and a["msg"] != old, "the first question's old copy is stranded"
        q2 = question(mid, "Second?"); tick()
        owner_msg("about the first", cid=origin, reference={"message_id": old}); tick()
        assert next(m for m in inbox("orchestrator") if m["body"] == "about the first")["reply_to"] == q1["id"], "not the newest question"
        assert q1["id"] not in b.st["q"] and q2["id"] in b.st["q"]
    finally: fake.fail = None

def parked():
    """An answered question whose old copy still will not delete: (thread, old id, the st["q_old"] record)."""
    origin, key, st, old = stranded(); press_msg(fake.channels[origin]["msgs"][st["msg"]], key, 0)
    assert len(b.st["q_old"]) == 1 and b.st["q_old"][0]["old"] == [old]
    return origin, old, b.st["q_old"][0]

def test_parked_old_copy_waits_out_its_backoff():
    try:
        origin, old, rec = parked(); n = len(fake.calls)
        for _ in range(3): CLOCK.offset += 10; b.refresh_questions()
        assert not old_dels(old, n), "not tried on every pass"
        CLOCK.offset += 600; b.refresh_questions(); assert len(old_dels(old, n)) == 1, "tried once the wait is over"
    finally: fake.fail = None

def test_parked_old_copy_is_given_up_after_20_tries_without_its_buttons():
    lines, undo = logged()
    try:
        origin, old, rec = parked(); n = len(fake.calls)
        for _ in range(30): CLOCK.offset += 601; b.refresh_questions()
        assert not b.st["q_old"] and rec["old_n"] == 20 and len(old_dels(old)) == 20, (rec, len(old_dels(old)))
        assert sum("given up on" in l for l in lines) == 1 and any("given up on" in l and "buttons removed" in l for l in lines), lines
        left = fake.channels[origin]["msgs"][old]; assert left["components"] == [] and "asks:" in left["content"], "the copy that stays has no live buttons, its text untouched"
        assert len([c for c in fake.calls[n:] if c[0] == "PATCH" and c[1].endswith(f"/messages/{old}")]) == 1, "one last edit, no more"
        n = len(fake.calls)
        for _ in range(5): CLOCK.offset += 601; b.refresh_questions()
        assert not [c for c in fake.calls[n:] if c[1].endswith(f"/messages/{old}")], "and then it is left alone"
    finally: fake.fail = None; undo()

def test_given_up_old_copy_whose_edit_fails_too_is_tried_once_and_logged():
    lines, undo = logged()
    try:
        origin, old, rec = parked()
        fake.fail = lambda m, p, body: Err(503, 0) if m in ("DELETE", "PATCH") and p.endswith(f"/messages/{old}") else None
        for _ in range(30): CLOCK.offset += 601; b.refresh_questions()
        assert not b.st["q_old"] and len([c for c in fake.calls if c[0] == "PATCH" and c[1].endswith(f"/messages/{old}")]) == 1, "bounded: one edit"
        assert any("given up on" in l and f"buttons still on ['{old}']" in l for l in lines), lines
    finally: fake.fail = None; undo()

def test_tap_on_a_replaced_copy_puts_the_surviving_card_in_the_answered_sweep():
    origin, key, st = asked(); old = dict(fake.channels[origin]["msgs"][st["msg"]]); fake.add_msg(origin, "below"); b.refresh_questions()
    new = st["msg"]; assert new != old["id"]
    press_msg(old, key, 1)
    assert b.st["answered"].get(new, {}).get("ch") == origin, "the card that stays is cleaned up like any answered card"
    CLOCK.offset += bridge_mod.Q_KEEP + 1; b.last_clean = 0; tick()
    assert new not in fake.channels[origin]["msgs"] and new not in b.st["answered"], "and the sweep removes it after its hour"

# ---- thread names start with the project ----
def bare(origin): return bridge_mod.Bridge.bare_name(fake.channels[origin]["name"])
def settle(): tick(); CLOCK.offset += bridge_mod.RENAME_SETTLE + 1; tick()
def units(s): return len(s.encode("utf-16-le")) // 2

def test_project_label_and_prefix():
    m = bridge_mod
    assert m.project_label(area="discord-bridge") == "Discord bridge" and m.project_label(area="mail_runner.v2") == "Mail runner v2"
    assert m.project_label(repo="Photo Library App") == "Photo Library App", "a repo folder is used as it is"
    (tmp / "Kepler").mkdir(); assert m.project_label(repo="kepler") == "Kepler", "in the folder's own spelling"
    assert m.project_of({"repo": "Kepler", "body": "x\n\nAREA: discord-bridge"}) == "Kepler" and m.project_of({"body": "do it\n\nAREA: discord-bridge"}) == "Discord bridge" and m.project_of({"body": "hi"}) == ""
    assert m.with_project("Kepler", "page size in dark theme") == "Kepler: page size in dark theme"
    assert m.with_project("", "why is the sky blue") == "why is the sky blue", "no project: the plain title, nothing in front"
    for title in ("Kepler: page size", "kepler:page size", "Re: Kepler: page size"): assert m.with_project("Kepler", title) == "Kepler: page size", title
    for title in ("Discord bridge: card moves", "discord-bridge: card moves", "Discord Bridge: card moves"): assert m.with_project("Discord bridge", title) == "Discord bridge: card moves", title
    assert m.with_project("Kepler", "Question: is Kepler: fine") == "Kepler: Question: is Kepler: fine", "another prefix is part of the description"
    project = "Old Build- Legacy Canvas Toolkit v1"; long = "make the canvas toolkit paint flow much smoother when many users join at once and " * 2
    got = m.with_project(project, long); assert got.startswith(project + ": make the canvas") and got.endswith("…") and units(got) <= 90, got

def test_thread_gains_its_project_from_area_mail():
    origin, mid = request("card moves when a question is answered"); t = b.st["threads"][mid]
    assert bare(origin) == "card moves when a question is answered" and not t.get("project"), "opened before a project is known: plain name"
    agent_mod.send("orchestrator", "builder", "Discord bridge: card moves", "Do it.\n\nAREA: discord-bridge", reply_to=mid, needs_reply=True); settle()
    assert t["project"] == "Discord bridge" and bare(origin) == "Discord bridge: card moves when a question is answered", fake.channels[origin]["name"]
    assert fake.channels[origin]["name"].split(" ", 1)[0] in [v[0] for v in bridge_mod.STATES.values()], "the state emoji stays in front"
    assert len(fake.renames[origin]) == 1
    agent_mod.send("orchestrator", "builder", "Kepler: other", "x", repo="Kepler", check_repo=False, reply_to=mid); settle()
    assert t["project"] == "Discord bridge", "the first project stays, so the name gains it once"

def test_thread_gains_its_project_from_a_repo_event_without_doubling():
    origin, mid = request("kepler: page size in dark theme"); (tmp / "Kepler").mkdir()
    fleet_mod.event("started", "t", agent="builder", thread=mid, ref="r1", repo="kepler"); settle()
    assert b.st["threads"][mid]["project"] == "Kepler" and bare(origin) == "Kepler: page size in dark theme", fake.channels[origin]["name"]

def test_thread_without_a_project_keeps_its_plain_name():
    origin, mid = request("why is the sky blue"); event("started", mid); answer(mid, "Rayleigh scattering."); agent_mod.ack("orchestrator", mid); event("done", mid); settle(); sweep_tick()
    assert fake.channels[origin]["name"].startswith(bridge_mod.STATES["DONE"][0]), fake.channels[origin]["name"]
    assert bare(origin) == "why is the sky blue" and "project" not in b.st["threads"][mid], fake.channels[origin]["name"]

def test_project_name_fits_100_by_shortening_the_description():
    project = "Old Build- Legacy Canvas Toolkit v1"; origin, mid = request("make the canvas toolkit paint flow much smoother when many users join at once and then some more")
    b.st["threads"][mid]["project"] = project; b.dirty.add(mid); settle()
    name = fake.channels[origin]["name"]; assert units(name) <= 100 and bare(origin).startswith(project + ": make the canvas toolkit") and name.endswith("…"), name

def test_hand_typed_name_is_not_prefixed():
    origin, mid = request("page size"); t = b.st["threads"][mid]
    fake.channels[origin]["name"] = "My name"; b.dispatch("THREAD_UPDATE", {"id": origin, "name": "My name", "thread_metadata": {"archived": False}}); assert t.get("name_lock")
    fleet_mod.event("started", "t", agent="builder", thread=mid, ref="r1", repo="Kepler"); settle(); sweep_tick()
    assert t["project"] == "Kepler" and fake.channels[origin]["name"] == "My name" and origin not in fake.renames

def test_prefixed_name_is_the_bridges_own():
    origin, mid = request("page size"); t = b.st["threads"][mid]; t["project"] = "Kepler"
    b.dispatch("THREAD_UPDATE", {"id": origin, "name": "Kepler: page size", "thread_metadata": {"archived": False}})
    assert not t.get("name_lock"), "the name with its project is not a rename by hand"

def test_open_threads_gain_the_project_one_at_a_time():
    rows = [request(f"job {i}") for i in range(3)]
    for _, mid in rows: b.st["threads"][mid]["project"] = "Kepler"; b.dirty.add(mid)
    settle(); done = lambda: [o for o, _ in rows if bare(o).startswith("Kepler: job")]
    assert len(done()) == 1, [fake.channels[o]["name"] for o, _ in rows]
    tick(3); assert len(done()) == 1, "no burst"
    CLOCK.offset += bridge_mod.PROJECT_PACE + 1; tick(); assert len(done()) == 2
    CLOCK.offset += bridge_mod.PROJECT_PACE + 1; tick(); assert len(done()) == 3 and all(len(fake.renames[o]) == 1 for o, _ in rows)

def test_existing_threads_learn_the_project_from_finished_mail():
    origin, mid = request("page size"); o2, m2 = request("sleeping one"); t, t2 = b.st["threads"][mid], b.st["threads"][m2]
    for tid in (mid, m2): agent_mod.ack("builder", agent_mod.send("orchestrator", "builder", "job", "x", repo="Kepler", check_repo=False, reply_to=tid)["id"])
    fake.channels[o2]["archived"] = True; t2["archived"] = True; before = fake.channels[o2]["name"]
    assert b.st.get("projects_backfilled") and not t.get("project"); b.st.pop("projects_backfilled")
    tick(); assert t["project"] == "Kepler" and t2["project"] == "Kepler" and b.st["projects_backfilled"] and bare(origin) == "page size", "nothing is renamed on the spot"
    sweep_tick(); CLOCK.offset += bridge_mod.RENAME_SETTLE + 1; sweep_tick()
    assert bare(origin) == "Kepler: page size" and len(fake.renames[origin]) == 1, fake.channels[origin]["name"]
    assert fake.channels[o2]["name"] == before and fake.channels[o2]["archived"] and o2 not in fake.renames, "an archived thread is not opened for it"

def test_session_thread_named_before_gains_the_project():
    b.cfg["project_renames"] = True; key = lambda sid: b.st["sessions"][f"{MAC}/{sid}"]
    th = old_session("s1", "Original · Mac · claude", ["Original", "Later"]); key("s1")["named"] = True; session_file(prompt="Later", project="Kepler")
    shut = old_session("s2", "Original · Mac · claude", ["Original"], archived=True); key("s2")["named"] = True; session_file(sid="s2", prompt="Original", state="idle", project="Kepler")
    hand = old_session("s3", "My notes · Mac · claude", ["Original"]); key("s3")["named"] = True; session_file(sid="s3", prompt="Original", project="Kepler")
    tick()
    assert fake.channels[th]["name"] == "Kepler: Original · Mac · claude" and len(fake.renames[th]) == 1, fake.channels[th]["name"]
    assert fake.channels[shut]["name"] == "Original · Mac · claude" and fake.channels[shut]["archived"] and shut not in fake.renames, "an archived session thread is not opened for it"
    assert fake.channels[hand]["name"] == "My notes · Mac · claude" and hand not in fake.renames, "a name set by hand is kept"
    assert all(key(s).get("proj") for s in ("s1", "s2", "s3")); n = fake.count("GET", f"/channels/{th}"); tick(3)
    assert len(fake.renames[th]) == 1 and fake.count("GET", f"/channels/{th}") == n, "once"

def test_session_project_rename_needs_project_renames():
    """With project_renames absent, the only session rename is the first-prompt one."""
    th = old_session("s1", "Original · Mac · claude", ["Original", "Later"]); c = b.st["sessions"][f"{MAC}/s1"]; c["named"] = True; session_file(prompt="Later", project="Kepler")
    tick(3); assert th not in fake.renames and fake.channels[th]["name"] == "Original · Mac · claude" and not c.get("proj"), fake.channels[th]["name"]
    session_file(sid="s2", prompt="<system-reminder>x</system-reminder>", project="Kepler"); tick(); assert not any(x.get("name") == "Kepler · Mac · claude" for x in fake.channels.values())
    session_file(sid="s2", prompt="Fix the login", project="Kepler"); tick(); first = next(i for i, x in fake.channels.items() if x.get("name") == "Kepler: Fix the login · Mac · claude")
    assert first not in fake.renames, "opened with its prompt, so never renamed"
    b.cfg["project_renames"] = True; tick(); assert fake.channels[th]["name"] == "Kepler: Original · Mac · claude" and len(fake.renames[th]) == 1, "turning it on later still gains it once"

def test_session_temp_folder_is_no_project():
    for name in ("proj", "PROJ", "scratch", "scratch-2026-11-09-fbfdd6", "tmp", "tmp.Ab12", "temp", "?"): assert b.where_project(f"{name}  ·  Mac  ·  claude") == "", name
    for name in ("Kepler", "Projector", "Scratchpad", "Temple Run"): assert b.where_project(f"{name}  ·  Mac  ·  claude") == name, name
    session_file(prompt="Fix it", project="proj"); session_file(sid="s2", prompt="Other", project="scratch-2026-11-09-fbfdd6"); tick()
    names = sorted(c["name"] for c in fake.channels.values() if c["type"] == 11); assert names == ["Fix it · Mac · claude", "Other · Mac · claude"], names

def test_first_mail_opens_its_thread_with_the_project():
    """A first mail to the owner carries the project into the name its thread opens with, so no rename is needed."""
    b.cfg["thread_state_names"] = False; (tmp / "Kepler").mkdir()
    named = lambda mid: fake.channels[b.st["threads"][mid]["dthread"]]
    repo = agent_mod.send("orchestrator", "owner", "fix the pool", "Done.", repo="Kepler", check_repo=False)["id"]
    area = agent_mod.send("orchestrator", "owner", "card moves", "Done.\n\nAREA: discord-bridge")["id"]
    ask = agent_mod.send("orchestrator", "owner", "which pool", "Which?", repo="Kepler", check_repo=False, options=["A", "B"])["id"]
    settle(); sweep_tick()
    assert named(repo)["name"] == "Kepler: fix the pool", named(repo)["name"]
    assert named(area)["name"] == "Discord bridge: card moves", named(area)["name"]
    assert named(ask)["name"] == "Kepler: which pool", named(ask)["name"]
    assert not any(b.st["threads"][m]["dthread"] in fake.renames for m in (repo, area, ask))

def test_session_name_does_not_double_the_project():
    session_file(prompt="Kepler: fix the throw", project="Kepler"); session_file(sid="s2", prompt="No project here", project=""); tick()
    names = sorted(c["name"] for c in fake.channels.values() if c["type"] == 11)
    assert names == ["Kepler: fix the throw · Mac · claude", "No project here · Mac · claude"], names

def test_state_names_off_gains_the_project_only_when_allowed():
    """The live setting: names carry no state and are never renamed. A thread opened with its project known has it from
    the start; one opened plain keeps its name, unless project_renames allows the one rename that adds the project."""
    b.cfg["thread_state_names"] = False
    fleet_mod.event("started", "Kepler: fix the pool", agent="builder", thread="T1", ref="T1", repo="Kepler"); tick()
    assert b.thread_name(b.st["threads"]["T1"]) == "Kepler: fix the pool", "known before its thread opens: no rename needed"
    origin, mid = request("page size"); o2, m2 = request("why is the sky blue"); t = b.st["threads"][mid]
    fleet_mod.event("started", "t", agent="builder", thread=mid, ref="r1", repo="Kepler"); settle(); sweep_tick()
    assert t["project"] == "Kepler" and fake.channels[origin]["name"] == "page size" and origin not in fake.renames, "renames stay off"
    b.cfg["project_renames"] = True; sweep_tick(); CLOCK.offset += bridge_mod.RENAME_SETTLE + 1; sweep_tick()
    assert fake.channels[origin]["name"] == "Kepler: page size" and len(fake.renames[origin]) == 1, fake.channels[origin]["name"]
    event("done", mid, ref="r1"); CLOCK.offset += 700; sweep_tick(); CLOCK.offset += 31; sweep_tick()
    assert len(fake.renames[origin]) == 1 and fake.channels[o2]["name"] == "why is the sky blue" and o2 not in fake.renames, "nothing else is ever renamed"

def test_notepad_batches_replies_and_withdrawals():
    """Several messages in a row reach the agents as one, with what each replied to; a deleted one is taken back; the
    notepad shows what is unread; an agent run starting marks them read."""
    notes = bridge_mod.NOTES
    origin, mid = request(); agent_mod.ack("orchestrator", mid); n0 = len(inbox("orchestrator"))
    b.cfg["notepad"] = True
    try:
        bot = fake.add_msg(origin, "Which pool do you mean?", author="bot")
        one = owner_msg("The big one", cid=origin)
        b.dispatch("MESSAGE_CREATE", {"id": fresh_id(), "channel_id": origin, "author": {"id": OWNER}, "content": "and make it deeper", "attachments": [], "type": 19,
                                      "message_reference": {"message_id": bot["id"]}, "referenced_message": {"id": bot["id"], "author": {"id": "bot"}, "content": "Which pool do you mean?"}})
        gone = owner_msg("also paint it red", cid=origin); tick()
        assert len(inbox("orchestrator")) == n0, "nothing is mailed while they may still be writing"
        assert len(notes.unread(mid)) == 3 and b.st["threads"][mid].get("unread") == 3, "all three are in the notepad as unread"
        b.dispatch("MESSAGE_DELETE", {"id": gone, "channel_id": origin}); tick()
        CLOCK.offset += bridge_mod.QUIET + 1; tick()
        new = inbox("orchestrator")[n0:]
        assert len(new) == 1, f"one mail for the whole batch, not {len(new)}"
        body = new[0]["body"]
        assert "2 messages in a row" in body and "The big one" in body and "make it deeper" in body and "paint it red" not in body, body
        assert "replying to the bot" in body and "Which pool do you mean?" in body, "the reply target travels with the answer"
        pad = notes.render(mid)
        assert "[NEW]" in pad and "WITHDRAWN" in pad, pad
        event("started", mid, ref=new[0]["id"]); tick()
        assert not notes.unread(mid) and not b.st["threads"][mid].get("unread"), "a run starting marks them read"
        assert any(c[0] == "PUT" and f"/messages/{one}/reactions/" in c[1] for c in fake.calls), "their message gets the read mark"
        n1 = len(inbox("orchestrator"))
        b.dispatch("MESSAGE_DELETE", {"id": one, "channel_id": origin}); CLOCK.offset += bridge_mod.QUIET + 1; tick()
        late = inbox("orchestrator")[n1:]
        assert len(late) == 1 and "take it back" in late[0]["body"] and "The big one" in late[0]["body"], "a message deleted after it was read is reported as taken back"
    finally: b.cfg.pop("notepad", None)

def test_feedback_buttons_write_the_ledger():
    """No Accept / Needs fixing / Undo row any more (the owner, Nov 12); a tap on an old one still writes one ledger row, no agent run."""
    b.cfg["feedback"] = True
    try:
        origin, mid = request(); agent_mod.ack("orchestrator", mid)
        fleet_mod.stat({"engine": "claude", "agent": "builder", "thread": mid, "repo": "", "secs": 60, "cost": 0.5, "model": "claude-sonnet-5-5", "effort": "medium"})
        answer(mid, "Built the popup."); tick(2)
        t = b.st["threads"][mid]
        assert t.get("fb"), "a result of a job that built something asks for a reaction"
        assert [c["label"] for c in b.close_row(t)[0]["components"]] == ["Close"], "finished work gets no Accept or Undo"
        n = len(fleet_mod.ledger_rows(1)); m = len(inbox("orchestrator"))
        b.feedback({"id": fake.nid(), "token": "tok", "message": {"content": "Built the popup."}}, mid, "accept")
        led = fleet_mod.ledger_rows(1)
        assert len(led) == n + 1 and led[-1]["reaction"] == "accept" and led[-1]["builds"] == 1 and led[-1]["build_models"] == ["claude-sonnet-5-5 medium"], led[-1]
        assert not t.get("fb") and len(inbox("orchestrator")) == m, "the buttons go away and no agent is started for Accept"
        assert [c["label"] for c in b.close_row(t)[0]["components"]] == ["Close"]
    finally: b.cfg.pop("feedback", None)

def answer_text(origin, word):
    return next(m["content"] for m in fake.msgs(origin) if word in m["content"])

def test_receipt_under_the_answer():
    """An orchestrator answer ends with one small line: share of the weekly subscription limit, runs, minutes and the model split since the owner's last message. No money."""
    b.cfg["receipt"] = True
    try:
        origin, mid = request(); agent_mod.ack("orchestrator", mid)
        fleet_mod.stat({"engine": "claude", "agent": "builder", "thread": mid, "secs": 120, "cost": 0.75, "model": "claude-opus-5-5", "week0": 10, "week1": 12})
        fleet_mod.stat({"engine": "claude", "agent": "reviewer", "thread": mid, "secs": 60, "cost": 0.25, "model": "claude-sonnet-5-5", "week0": 12, "week1": 12.5})
        answer(mid, "Built the popup."); tick(2)
        text = answer_text(origin, "Built the popup.")
        assert text.splitlines()[-1] == "-# 2.5% of Claude's week · 2 runs · 3 min · Opus 75% · Sonnet 25%" and "$" not in text, text
        assert b.st["threads"][mid]["answer"] == "Built the popup.", "the stored answer has no receipt"
        b.cfg["receipt"] = False
        origin2, mid2 = request("Fix the lights"); agent_mod.ack("orchestrator", mid2)
        fleet_mod.stat({"engine": "claude", "agent": "builder", "thread": mid2, "secs": 60, "cost": 0.5, "model": "claude-opus-5-5"})
        answer(mid2, "Lights fixed."); tick(2)
        assert not any("of Claude's week" in l for m in fake.msgs(origin2) for l in m["content"].splitlines()), "receipt off: no cost line"
    finally: b.cfg.pop("receipt", None)

def test_receipt_counts_only_since_the_owners_last_message():
    b.cfg["receipt"] = True
    try:
        origin, mid = request(); agent_mod.ack("orchestrator", mid)
        fleet_mod.stat({"engine": "claude", "agent": "builder", "thread": mid, "secs": 120, "cost": 0.75, "model": "claude-opus-5-5"})
        answer(mid, "Which pool?"); tick(2)
        CLOCK.offset += 60; owner_msg("The big one", cid=origin); tick()
        agent_mod.ack("orchestrator", inbox("orchestrator")[-1]["id"])
        fleet_mod.stat({"engine": "claude", "agent": "orchestrator", "thread": mid, "secs": 30, "cost": 0.10, "model": "claude-sonnet-5-5"})
        fleet_mod.stat({"engine": "claude", "agent": "builder", "thread": mid, "secs": 90, "cost": 0.40, "model": "claude-opus-5-5"})
        answer(mid, "Fixed the big pool."); tick(2)
        foot = answer_text(origin, "Fixed the big pool.").splitlines()[-1]
        assert foot.startswith("-# 2 runs ·"), foot
        assert not answer_text(origin, "Which pool?").splitlines()[-1].startswith("-#"), "a one-run answer has no receipt"
    finally: b.cfg.pop("receipt", None)

def test_share_text_and_usage():
    assert fleet_mod.share_text([]) == ""
    assert fleet_mod.share_text([{"model": "claude-opus-5-5", "secs": 30}, {"model": "claude-fable-5-2", "secs": 90}]) == "Fable 75% · Opus 25%", "no cost: weighed by run time"
    five = [{"model": m, "cost": c} for m, c in (("claude-opus-5-5", 4), ("claude-fable-5-2", 3), ("claude-sonnet-5-5", 1), ("gemini-3", 1), ("composer-2", 1))]
    assert fleet_mod.share_text(five, top=3) == "Opus 40% · Fable 30% · Sonnet 10% · other 20%", fleet_mod.share_text(five, top=3)
    fleet_mod.stat({"engine": "claude", "agent": "builder", "thread": "t1", "subject": "Kepler: pool", "secs": 60, "cost": 1.0, "model": "claude-opus-5-5", "input_tokens": 10, "cache_read_input_tokens": 90})
    out = fleet_mod.stats_text(24)
    assert "By model: Opus 100%" in out and "Cache reads: 90% of input tokens" in out and "Costliest requests: Kepler: pool $1.00" in out, out

def test_burn_alert_once():
    """A request in progress that passes burn_alert dollars since the owner's last message gets one notice in #alerts."""
    origin, mid = request(); t = b.st["threads"][mid]
    t["runs"]["x"] = {"agent": "builder", "label": "PC", "t0": CLOCK.time()}
    for c in (10, 10, 5): fleet_mod.stat({"engine": "claude", "agent": "builder", "thread": mid, "secs": 60, "cost": c, "model": "claude-opus-5-5"})
    burns = lambda: [m for m in fake.msgs(ALERTS) if "has used $25" in m["content"]]
    b.cfg["burn_alert"] = 0
    try: b.sweep_burn(CLOCK.time()); assert not burns(), "burn_alert 0: off"
    finally: b.cfg.pop("burn_alert", None)
    b.sweep_burn(CLOCK.time()); b.sweep_burn(CLOCK.time())
    assert len(burns()) == 1, [m["content"] for m in fake.msgs(ALERTS)]

def test_pace_alert_once():
    """Claude's weekly limit on pace to run out before it resets gets one notice in #alerts per week."""
    now = CLOCK.time(); u = tmp / "Queue" / "usage"; u.mkdir(parents=True, exist_ok=True)
    (u / "claude.json").write_text(json.dumps({"windows": {"seven_day": {"utilization": 0.6, "resetsAt": now + 4.5 * 86400}}, "at": now, "host": "x"}), encoding="utf-8")
    paces = lambda: [m for m in fake.msgs(ALERTS) if "on pace for" in m["content"]]
    b.cfg["pace_alert"] = 0
    try: b.sweep_pace(now); assert not paces(), "pace_alert 0: off"
    finally: b.cfg.pop("pace_alert", None)
    b.sweep_pace(now); b.sweep_pace(now)
    assert len(paces()) == 1 and "would reach 100%" in paces()[0]["content"], [m["content"] for m in fake.msgs(ALERTS)]
    # at parity (39% used with 39% of the week gone) the limit is reached only at the reset: no alert
    b.st.pop("pace_alerted", None); n = len(paces())
    (u / "claude.json").write_text(json.dumps({"windows": {"seven_day": {"utilization": 0.39, "resetsAt": now + 0.61 * 7 * 86400}}, "at": now, "host": "x"}), encoding="utf-8")
    b.sweep_pace(now); assert len(paces()) == n, [m["content"] for m in fake.msgs(ALERTS)]

# ---- janitor findings, 2026-11-11 ----
def test_slash_command_title_uses_the_rest_of_the_prompt():
    assert bridge_mod.clean_title("/ponytail:ponytail off") == "Session", "a bare command and its one-word argument name nothing"
    assert bridge_mod.clean_title("/review fix the shop prices") == "fix the shop prices"
    assert bridge_mod.clean_title("Fix the pool") == "Fix the pool" and bridge_mod.clean_title("") == "Request", "other titles unchanged"
    assert bridge_mod.clean_title("/Dev/Work/x.lua crashes") == "/Dev/Work/x.lua crashes", "a path is not a command"
    assert b.session_name("/ponytail:ponytail off", " · Mac · claude").startswith("Session · Mac")
    text = b.card_text("Scratch Sessions", [("s:x", {"state": "idle", "title": "/ponytail:ponytail off", "tool": "claude", "where": "Mac"})])
    assert "/ponytail" not in text and "Session" in text, text

def test_needs_you_only_while_a_question_is_open():
    origin, mid = request(); q = question(mid); tick(); t = b.st["threads"][mid]
    assert b.state_word(t) == "NEEDS YOU"
    b.st["q"].clear()  # prune trims st["q"] without touching the flag
    assert t.get("asking") and b.state_word(t) != "NEEDS YOU", "the flag alone no longer shows Needs you"
    # #loops: a question on an older cycle stops showing once a newer cycle of that project finished
    loops_on(); old, new = "20261110T000000Z-aaaa0001", "20261111T000000Z-aaaa0002"
    to = b.thread(old, "Improvement cycle started for Stack.io (ask)"); to.update(mailed=True, asking="Pick", t0=CLOCK.time() - 7200)
    b.st["q"]["qk"] = {"from": "orchestrator", "options": [{"label": "A"}], "thread": old}
    loop_tick(); card = next(m["content"] for m in board_bot(LOOPS) if "**Stack.io**" in m["content"])
    assert card.startswith("🔴"), card
    tn = b.thread(new, "Improvement cycle started for Stack.io (ask)"); tn.update(mailed=True, t0=CLOCK.time() - 60)
    assert b.state_word(tn) == "DONE"
    loop_tick(); card = next(m["content"] for m in board_bot(LOOPS) if "**Stack.io**" in m["content"])
    assert not card.startswith("🔴") and "needs your answer" not in card, card

def test_loops_card_keeps_needs_you_when_newest_cycle_asks_after_a_done_one():
    loops_on(); ids = ["20261109T000000Z-bbbb0001", "20261110T000000Z-bbbb0002", "20261111T000000Z-bbbb0003"]
    for i, (tid, age) in enumerate(zip(ids, (10800, 7200, 60))):
        th = b.thread(tid, "Improvement cycle started for Stack.io (ask)"); th.update(mailed=True, t0=CLOCK.time() - age, dthread=str(7770 + i))
        if i != 1: th["asking"] = "Pick"; b.st["q"]["qk%d" % i] = {"from": "orchestrator", "options": [{"label": "A"}], "thread": tid}
    assert b.state_word(b.st["threads"][ids[1]]) == "DONE"
    loop_tick(); card = next(m["content"] for m in board_bot(LOOPS) if "**Stack.io**" in m["content"])
    assert card.startswith("🔴") and "7772" in card, card

def test_reply_in_an_overview_card_thread_goes_to_the_orchestrator():
    ov_on(); ov_session(); ov_tick(); c = b.st["overview"]["cards"]["Kepler"]
    th = fake.nid(); fake.channels[th] = {"type": 11, "msgs": {}, "parent": OVERVIEW}; c["thread"] = th
    n = len(inbox("orchestrator")); m = owner_msg("Why is this one slow?", cid=th); tick()
    got = inbox("orchestrator"); assert len(got) == n + 1 and got[-1]["body"] == "Why is this one slow?", got
    assert m not in fake.channels[th]["msgs"], "removed from the read-only card thread, like a top-level #overview message"
    assert fake.channels[b.st["threads"][got[-1]["id"]]["dthread"]]["parent"] == PARENT, "its thread is in #orchestrator"

def test_overview_working_clears_when_the_work_is_done():
    ov_on(); origin, mid = request("Pool lights"); ov_label("t:" + mid, "Kepler"); claim(mid); ov_tick()
    assert "Working: 1" in ov_card("Kepler")["content"], ov_card("Kepler")["content"]
    event("started", mid); tick(); event("done", mid); answer(mid, "Done, the lights work."); tick()  # the runner has not acked its mail yet: the mail view still says running
    assert b.state_word(b.st["threads"][mid]) == "DONE" and inbox("orchestrator"), "Done in #progress, mail still unacked"
    ov_tick(); text = ov_card("Kepler")["content"]
    assert "Working: 0" in text and "🟡" not in text.splitlines()[2], text

def test_board_message_of_only_links_is_ignored():
    b.ch["overview"] = OVERVIEW; b.spawn_label = lambda: None
    for text in ("<#1234567890123456>", f"<#123> <@{OWNER}>"):
        n, k = len(inbox("orchestrator")), len(fake.threads()); owner_msg(text, cid=OVERVIEW); tick()
        assert len(inbox("orchestrator")) == n and len(fake.threads()) == k, "no Request thread for a bare link"
    owner_msg("see <#123> please", cid=OVERVIEW); tick(); assert inbox("orchestrator")[-1]["body"] == "see <#123> please", "a link inside words still goes"

# ---- janitor findings: one test per finding ----
def test_janitor1_no_finished_ping_for_an_orchestrator_turn_or_twice_for_one_finish():
    ov_on(); o, m = request("Why is it slow"); ov_label("t:" + m, "Kepler"); ov_tick(); claim(m); event("started", m); ov_tick()
    CLOCK.offset += 130; claim(m); answer(m, "Because of X."); event("done", m); agent_mod.ack("orchestrator", m); ov_tick(); gone_wait()
    assert not finished_pings(), "an orchestrator turn of its own is no finish"
    o2, m2 = request("Build the lights"); ov_label("t:" + m2, "Kepler"); ov_tick(); claim(m2); event("started", m2, ref="b1", agent="builder"); ov_tick()
    CLOCK.offset += 130; claim(m2); event("done", m2, ref="b1", agent="builder"); answer(m2, "Built."); agent_mod.ack("orchestrator", m2); ov_tick(); gone_wait()
    assert len(finished_pings()) == 1 and "Build the lights" in finished_pings()[0]["content"], [x["content"] for x in sent_pings()]
    ov = b.st["overview"]; r = ov["items"]["t:" + m2]; n = len(ov["pings"])
    b.ping(ov, "t:" + m2, r, "done"); assert len(ov["pings"]) == n, "Finished already sent and nothing worked since"
    r["fresh"] = True; b.ping(ov, "t:" + m2, r, "done"); assert len(ov["pings"]) == n + 1, "new work since: a new finish"

def test_janitor2_an_old_end_less_run_does_not_cut_off_a_finished_request():
    now = CLOCK.time(); t = b.thread("j2"); t["mailed"] = True
    t["runs"] = {"old": {"agent": "reviewer", "label": "Mac", "host": MAC, "t0": now - 90000},
                 "new": {"agent": "orchestrator", "label": "PC", "host": PC, "t0": now - 30, "end": "done", "t1": now - 6}}
    assert b.state_word(t) == "DONE", b.state_word(t)
    t["runs"]["live"] = {"agent": "builder", "label": "PC", "host": PC, "t0": now - 24}; assert b.state_word(t) == "WORKING"
    t["runs"].pop("new"); t["runs"]["live"]["t0"] = now - bridge_mod.RUN_LIMIT - 5; assert b.state_word(t) == "CUT OFF", "the newest run went silent past the limit"

def test_janitor3_threads_cards_name_throwaway_folders_by_the_prompt():
    assert b.session_label({"project": "scratch-2026-11-09-fbfdd6", "first_prompt": "Look for more areas"}) == "Look for more areas"
    assert b.session_label({"project": "fini", "cwd": "/Users/alex/Documents/Codex/2027-01-10/fini", "first_prompt": "Finish my set problems"}) == "Finish my set problems"
    assert b.session_label({"project": "Kepler", "cwd": "C:/Dev/Work/Kepler", "first_prompt": "x y"}) == "Kepler: x y"
    assert b.session_label({"project": "Kepler", "cwd": "C:/Dev/Work/Kepler"}) == "Kepler"
    ov_on(); ov_session(project="scratch-2026-11-09-abcdef", prompt="Fix the pool"); b.sessions(CLOCK.time())
    card = next(m["content"] for m in fake.msgs(THREADS) if m["content"].startswith("🟡"))
    assert "scratch" not in card and card.split("\n")[0].endswith("Fix the pool  ·  Mac  ·  claude"), card

def test_janitor_1113c_session_card_names_project_and_first_prompt():
    x = {"project": "multi-device", "cwd": "/x/multi-device", "first_prompt": "Look at the current stage of the vault", "last_prompt": "Yes, do it"}
    assert b.session_label(x, "Yes, do it").startswith("multi-device: Look at the current stage"), b.session_label(x, "Yes, do it")
    assert b.where_project("multi-device: Look at the vault  ·  Mac  ·  claude") == "multi-device"
    ov_on(); ov_session(project="Kepler", prompt="Fix the pool"); b.sessions(CLOCK.time())
    card = next(m["content"] for m in fake.msgs(THREADS) if m["content"].startswith("🟡"))
    assert "Kepler: Fix the pool" in card.split(chr(10))[0], card

def test_janitor_1113c_snapshot_skips_bridge_lock():
    jan = load_module("discord_janitor_1113c", "discord-janitor.py"); real = jan.br.api
    def stub(method, path, *a, **k): return {"threads": [{"id": "900001", "parent_id": "A", "name": "bridge lock"}, {"id": "900002", "parent_id": "A", "name": "work"}]} if "/threads/active" in path else []
    jan.br.api = stub
    try: text, facts = jan.snapshot({"guild_id": "g", "channels": {}, "lease_msg": ["900001", "M"]}, "bot", set())
    finally: jan.br.api = real
    assert "900001" not in text and "900002" in text and "900001" not in facts["threads"] and "900002" in facts["threads"], (text, facts)

def test_janitor4_new_work_in_a_finished_thread_resets_steps_and_title():
    ov_on(); origin, mid = request("Airdrop a random photo"); ov_label("t:" + mid, "Kepler"); t = b.st["threads"][mid]
    event("started", mid); tick(); event("started", mid, ref="b1", agent="builder"); event("done", mid, ref="b1", agent="builder"); tick()
    answer(mid, "Sent."); event("done", mid); agent_mod.ack("orchestrator", mid); tick(2)
    assert b.state_word(t) == "DONE" and "Build" in b.track_line(t)
    owner_msg("Why a random one?", cid=origin); tick(); assert t["title"] == "Airdrop a random photo", "a question about the old work keeps it"
    event("started", mid, ref="o2"); answer(mid, "It was asked."); event("done", mid, ref="o2"); agent_mod.ack("orchestrator", inbox("orchestrator")[-1]["id"]); tick(2)
    assert b.state_word(t) == "DONE"
    CLOCK.offset += 60; owner_msg("Fix the device chooser", cid=origin); tick()
    assert t["title"] == "Fix the device chooser" and b.track_line(t) == "" and not b.steps(t), (t["title"], b.track_line(t))
    assert "Build" not in (status_msgs(origin) or [{"content": ""}])[-1]["content"]
    ov_tick(); assert "Fix the device chooser" in ov_card("Kepler")["content"], ov_card("Kepler")["content"]

def test_janitor5_loop_due_shows_why_and_an_expired_lock_is_free():
    loops_on(); GL = bridge_mod.GL; st = tmp / "Queue/loop/state.json"
    st.write_text(json.dumps({"last": {"Field Notebook": CLOCK.time() - 9 * 3600}}), encoding="utf-8")
    stamp = lambda s: real_time.strftime("%Y-%m-%dT%H:%M:%SZ", real_time.gmtime(real_time.time() + s))
    lock = tmp / "Queue/locks/Field Notebook.json"; lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"repo": "Field Notebook", "owner": MAC, "since": stamp(-7200), "until": stamp(3600)}), encoding="utf-8")
    assert GL.held("Field Notebook"), "a lock within its time holds"
    if any(d["ok"] for d in fleet_mod.where("Field Notebook")):
        loop_tick(); card = next(m["content"] for m in board_bot(LOOPS) if "**Field Notebook**" in m["content"])
        assert "due, waiting: Field Notebook is claimed by Mac" in card, card
    lock.write_text(json.dumps({"repo": "Field Notebook", "owner": MAC, "since": stamp(-86400), "until": stamp(-3600)}), encoding="utf-8")
    assert GL.held("Field Notebook") is None, "past its until the lock is free, as in repo-handoff"
    lock.unlink(); assert GL.held("Field Notebook") is None

def test_janitor_1112b_loop_card_queued_mid_cycle():
    loops_on(); st = tmp / "Queue/loop/state.json"; st.write_text(json.dumps({"last": {"Field Notebook": CLOCK.time() - 600}}), encoding="utf-8")
    origin, mid = request("Field Notebook: improvement cycle"); t = b.st["threads"][mid]; t["title"] = "Field Notebook: improvement cycle 1"
    t.pop("origin", None); event("started", mid, ref="r1"); event("done", mid, ref="r1"); tick(); CLOCK.offset += 60
    agent_mod.send("builder", "orchestrator", "next", "go", reply_to=mid); tick(); assert b.state_word(t) == "QUEUED" and t["runs"], b.state_word(t)
    loop_tick(); card = next(m["content"] for m in board_bot(LOOPS) if "**Field Notebook**" in m["content"])
    assert "cycle queued for its next step" in card, card
    t["runs"] = {}; loop_tick(); card = next(m["content"] for m in board_bot(LOOPS) if "**Field Notebook**" in m["content"])
    assert "cycle queued, waiting to start" in card, card

def test_janitor_1112b_old_janitor_cards_removed():
    title = "Discord flow: 8 finding(s) from Gemini for your review"; ids = []
    for text in (title, title, title, "Fix the other pool"):
        origin, mid = finished_idle(text); ids.append((origin, mid)); b.st["threads"][mid]["title"] = text
        CLOCK.offset += 60; b.st["threads"][mid]["t0"] = CLOCK.time()
    asker = ids[0][1]; b.st["threads"][asker]["t0"] -= 10  # oldest
    CLOCK.offset += 3 * 3600; b.budget = 10
    th = {mid: b.st["threads"][mid]["dthread"] for _, mid in ids}
    b.st["threads"][ids[1][1]]["anchor"] = "999"
    q = question(ids[0][1], "Keep it?"); tick(); assert b.asks(b.st["threads"][asker]), "setup: an open question"
    b.budget = 10; b.sweep_janitor(CLOCK.time())
    T = b.st["threads"]
    assert th[ids[0][1]] in fake.channels and not T[ids[0][1]].get("gone"), "open question stays"
    assert th[ids[1][1]] not in fake.channels and T[ids[1][1]].get("gone"), "older finished janitor card removed"
    assert th[ids[2][1]] in fake.channels and not T[ids[2][1]].get("gone"), "newest janitor card stays"
    assert th[ids[3][1]] in fake.channels and not T[ids[3][1]].get("gone"), "non-janitor thread untouched"

def test_janitor_1113_old_loop_cycle_cards_removed():
    title = "SloopApp: improvement cycle (ask)"; ids = []
    for text in (title, title, title, "Kepler: improvement cycle (ask)", title, title, "Fix the pool"):
        origin, mid = finished_idle(text); ids.append(mid); b.st["threads"][mid]["title"] = text
        CLOCK.offset += 60; b.st["threads"][mid]["t0"] = CLOCK.time()
    T = b.st["threads"]; th = {m: T[m]["dthread"] for m in ids}
    T[ids[0]]["t0"] -= 10  # oldest
    CLOCK.offset += 3 * 3600
    T[ids[5]]["last_end"] = T[ids[5]]["active_at"] = CLOCK.time() - 600  # finished 10 minutes ago
    T[ids[4]]["owner_at"] = CLOCK.time() - 7200
    for m in ids[:3] + ids[3:4]: T[m]["last_end"] = T[m]["active_at"] = CLOCK.time() - 3 * 3600
    question(ids[0], "Keep it?"); tick(); assert b.asks(T[ids[0]]), "setup: an open question"
    b.budget = 10; b.sweep_janitor(CLOCK.time())
    live = lambda m: th[m] in fake.channels and not T[m].get("gone")
    assert live(ids[0]), "open question stays"
    assert not live(ids[1]) and not live(ids[2]), "older finished loop cards removed"
    assert live(ids[3]), "lone Kepler card stays"
    assert live(ids[4]), "owner_at stays"
    assert live(ids[5]), "recently finished stays"
    assert live(ids[6]), "non-loop title untouched"

def test_janitor_1113b_needs_you_counts_each_question():
    b.cfg["progress_boards"] = True
    origin, mid = request("Fish job"); question(mid, "Which style?", ("D1", "D2")); question(mid, "Publish the title?", ("Publish", "More changes")); tick()
    assert b.q_count(b.st["threads"][mid]) == 2
    b.st["boards"] = {"ids": [], "text": []}; b.render_boards(CLOCK.time() + 10)
    need = b.st["boards"]["text"][-5]
    assert "(2)" in need.splitlines()[0] and "2 questions" in need, need
    text = b.card_text("Fish", [("t:" + mid, {"state": "needs_you", "title": "Fish job", "tool": "agents", "where": ""})])
    assert "Need you: 2" in text and "2 questions" in text, text
    origin, m2 = request("Single job"); question(m2, "One?", ("A", "B")); tick()
    b.st["boards"] = {"ids": [], "text": []}; b.render_boards(CLOCK.time() + 20)
    need = b.st["boards"]["text"][-5]
    line = [l for l in need.splitlines() if "Single job" in l][0]
    assert "questions" not in line and "(3)" in need.splitlines()[0], need

def test_janitor_1112d_fyi_to_non_auto_agent_is_not_active_work():
    fw = bridge_mod.fleet.active_work
    fyi = agent_mod.send("orchestrator", "mac", "Note", "FYI", needs_reply=False)
    assert not any(r["thread"] == (fyi.get("thread") or fyi["id"]) for r in fw()), "FYI to a non-auto agent is not work"
    ask = agent_mod.send("orchestrator", "mac", "Please", "Do it", needs_reply=True)
    assert any(r["thread"] == (ask.get("thread") or ask["id"]) for r in fw()), "a reply-needed mail is work"
    bm = agent_mod.send("orchestrator", "builder", "Build", "Go", needs_reply=False)
    assert any(r["thread"] == (bm.get("thread") or bm["id"]) for r in fw()), "mail to an auto agent is work"

def test_janitor_1112d_ask_gate_asks_for_a_one_line_result():
    GL = load_module("improve_loop_test_1112d", "improve-loop.py")
    assert "Not built yet: asked the owner" in GL.GATES["ask"] and "Build it" in GL.GATES["ask"]

def test_janitor_1112d_done_board_collapses_same_title():
    b.cfg["progress_boards"] = True
    for text in ("Same job", "Same job", "Same job", "Other job"):
        origin, mid = request(text); event("started", mid); agent_mod.ack("orchestrator", mid); event("done", mid); answer(mid, "Done."); b.st["threads"][mid]["title"] = text
        CLOCK.offset += 5
    tick(); b.st["boards"] = {"ids": [], "text": []}; b.render_boards(CLOCK.time())
    done = b.st["boards"]["text"][-1]
    assert done.count("Same job") == 1 and "×3" in done and done.count("Other job") == 1 and "(2)" in done.splitlines()[0], done
    for text in ("Live job", "Live job"):
        origin, mid = request(text); event("started", mid); b.st["threads"][mid]["title"] = text
    tick(); b.st["boards"] = {"ids": [], "text": []}; b.render_boards(CLOCK.time() + 10)
    assert b.st["boards"]["text"][-4].count("Live job") == 2, b.st["boards"]["text"][-4]

def test_janitor_1112d_finished_one_off_cards_removed():
    titles = ("Permission: Edit in A on Y", "Permission: Bash in X on Y", "Permission: Bash in Q on Y", "Permission: Edit in B on Y", "Fix the pool"); ids = []
    for text in titles:
        origin, mid = finished_idle(text); b.st["threads"][mid]["title"] = text; ids.append(mid)
    young = ids[3]; asker = ids[2]
    CLOCK.offset += 3 * 3600
    b.st["threads"][young]["last_end"] = b.st["threads"][young]["active_at"] = b.st["threads"][young]["t0"] = CLOCK.time() - 600
    th = {m: b.st["threads"][m]["dthread"] for m in ids}
    question(asker, "Allow?"); tick(); assert b.asks(b.st["threads"][asker]), "setup: open question"
    b.budget = 10; b.sweep_janitor(CLOCK.time()); T = b.st["threads"]
    for m in ids[:2]: assert th[m] not in fake.channels and T[m].get("gone"), "finished one-off card removed"
    for m in ids[2:]: assert th[m] in fake.channels and not T[m].get("gone"), "open question / young / ordinary thread stays"

def test_janitor6_overview_keeps_needs_you_for_a_thread_whose_mail_left_the_view():
    ov_on(); o, m = request("Publish the throw fix"); ov_label("t:" + m, "Kepler"); ov_tick(); claim(m); event("started", m, ref="b1", agent="builder"); ov_tick()
    CLOCK.offset += 130; claim(m); event("done", m, ref="b1", agent="builder"); question(m, "Publish it?", ("Publish", "Not now")); agent_mod.ack("orchestrator", m); tick(); ov_tick(); gone_wait()
    text = ov_card("Kepler")["content"]
    assert "Need you: 1" in text and "🔴 `agents" in text and "✅" not in text, text
    assert not finished_pings(), "no Finished while its question waits"

def test_janitor7_a_finished_ping_never_brings_back_a_card():
    ov_on(); ov_tick(); ov = b.st["overview"]
    ov["pings"].append({"key": "s:ghost", "topic": "Ghost", "text": f"<@{OWNER}> ✅ **Finished** · old", "at": CLOCK.time(), "what": "done"})
    ov_tick(); assert not ov_card("Ghost") and not pings() and not ov["pings"], "an old Finished with no card is dropped, no card or thread made for it"

def test_janitor8_needs_you_only_for_a_real_open_question_and_the_interim_answer_goes():
    b.cfg["one_answer_per_turn"] = True; origin, mid = request("Send the skill to Mike"); t = b.st["threads"][mid]
    event("started", mid); tick(); answer(mid, "**Not sent yet.** The builder is queued to send it."); tick()
    assert any("Not sent yet" in m["content"] for m in fake.msgs(origin))
    event("done", mid); question(mid, "Which Mike?", ("Mike A", "Mike B")); tick(2)
    assert not any("Not sent yet" in m["content"] for m in fake.msgs(origin)), "the question card supersedes the interim answer"
    assert b.state_word(t) == "NEEDS YOU" and any("asks:" in m["content"] for m in fake.msgs(origin))
    task = agent_mod.send("orchestrator", "builder", "Send it", "Go.", reply_to=mid); tick()
    assert b.state_word(t) != "NEEDS YOU" and not any("Needs you" in m["content"] for m in status_msgs(origin)), "work is owed: not Needs you"
    agent_mod.ack("builder", task["id"]); tick(); assert b.state_word(t) == "NEEDS YOU"
    owner_msg("Is it the one from work?", cid=origin); tick(); assert b.state_word(t) != "NEEDS YOU", "they spoke after the question: the agents have the turn"

def test_janitor9_failed_thread_keeps_title_and_detail_for_a_question_without_a_mark():
    origin, mid = request("Build the lights"); t = b.st["threads"][mid]
    event("started", mid); event("failed", mid, detail="boom"); tick(2); assert b.state_word(t) == "FAILED"
    d = t.get("detail"); assert d
    owner_msg("why did it fail", cid=origin); tick()
    assert t["title"] == "Build the lights" and t.get("detail") == d and not t.get("epoch"), (t["title"], t.get("detail"), t.get("epoch"))

def test_janitor9_an_acknowledgement_in_a_done_thread_keeps_the_title():
    origin, mid = request("Build the lights"); t = b.st["threads"][mid]
    event("started", mid); answer(mid, "Done."); event("done", mid); agent_mod.ack("orchestrator", mid); tick(2); assert b.state_word(t) == "DONE"
    owner_msg("thanks", cid=origin); tick(); assert t["title"] == "Build the lights" and not t.get("epoch")
    assert not bridge_mod.new_work("Got it") and not bridge_mod.new_work("what changed") and not bridge_mod.new_work("👍") and bridge_mod.new_work("add a shop button")

def test_janitor9_new_work_in_a_done_thread_takes_the_new_title():
    origin, mid = request("Build the lights"); t = b.st["threads"][mid]
    event("started", mid); answer(mid, "Done."); event("done", mid); agent_mod.ack("orchestrator", mid); tick(2); assert b.state_word(t) == "DONE"
    CLOCK.offset += 60; owner_msg("add a shop button", cid=origin); tick()
    assert t["title"] == "add a shop button" and t.get("epoch"), (t["title"], t.get("epoch"))

def test_janitor_1113f_head_line_skips_wrapper_and_reply_prefix():
    h = bridge_mod.head_line
    assert h("The owner sent 2 messages in a row. Read them together as one message:\n1. fix the lights\n2. and the door") == "fix the lights"
    assert h('(replying to the bot: "Which pool?") make it deeper') == "make it deeper"
    assert h("add a shop button\nmore detail") == "add a shop button" and h("") == "" and h(None) == ""

def test_janitor_1113f_batched_messages_mail_the_first_message_as_subject():
    origin, mid = request(); agent_mod.ack("orchestrator", mid); n0 = len(inbox("orchestrator"))
    b.cfg["notepad"] = True
    try:
        owner_msg("fix the lights please", cid=origin); owner_msg("and the door too", cid=origin); tick()
        CLOCK.offset += bridge_mod.QUIET + 1; tick()
        new = inbox("orchestrator")[n0:]
        assert len(new) == 1, len(new)
        sub = new[0]["subject"]
        assert sub.startswith("fix the lights please") and "messages in a row" not in sub and "replying to" not in sub, sub
        assert "2 messages in a row" in new[0]["body"]
    finally: b.cfg.pop("notepad", None)

def _1113f_quiet(body, sender):
    origin, mid = request(); t = b.st["threads"][mid]
    event("started", mid); answer(mid, body, sender=sender); event("done", mid); agent_mod.ack("orchestrator", mid); tick(2)
    return origin, mid, t

def test_janitor_1113f_builder_plain_text_question_waits_on_owner():
    origin, mid, t = _1113f_quiet("Your choice: close the windows or pick it yourself. Give me a go-ahead after either.", "builder")
    assert b.state_word(t) == "NEEDS YOU" and not b.finished(t), (b.state_word(t), t.get("text_ask"))
    CLOCK.offset += 5; owner_msg("close them", cid=origin); tick()
    assert b.state_word(t) != "NEEDS YOU", b.state_word(t)

def test_janitor_1113f_builder_report_without_question_is_done():
    origin, mid, t = _1113f_quiet("Published v12, confirmed in version history.", "builder")
    assert b.state_word(t) == "DONE" and not t.get("text_ask"), (b.state_word(t), t.get("text_ask"))
    origin, mid, t = _1113f_quiet("Failed. Want me to retry?", "orchestrator")
    assert not t.get("text_ask"), "the orchestrator's answers never set it"

def test_janitor_1113g_asks_owner_only_the_last_own_line():
    a = bridge_mod.asks_owner
    assert not a('Report\n- Orchestrator "Want me to retry?": never sets it\nAll done.')
    assert not a("**Why did it fail?**\nBecause of the lock.\nFixed.")
    assert a("Done.\nShould I also update the README?\n---\nMeasured by the runner, not reported by the builder: 2 file(s) changed.\n- a.py (+1 -0)")
    assert not a("Done.\n```\nis it ok?\n```")
    assert a("Built.\nGive me a go-ahead to ship.") and not a("") and not a(None)

def test_janitor_1113g_builder_report_with_mid_text_quoted_question_is_done():
    origin, mid, t = _1113f_quiet('Fixed.\n- Orchestrator "Want me to retry?": never sets it\n**Why did it fail?**\nPublished v12.', "builder")
    assert b.state_word(t) == "DONE" and not t.get("text_ask"), (b.state_word(t), t.get("text_ask"))

def test_janitor9_a_finished_ping_skipped_for_unfinished_is_retried_once_finished():
    ov_on(); o, m = request("Build the lights"); ov_label("t:" + m, "Kepler"); ov_tick(); claim(m); event("started", m, ref="b1", agent="builder"); ov_tick()
    CLOCK.offset += 130; claim(m); event("done", m, ref="b1", agent="builder"); answer(m, "Built."); agent_mod.ack("orchestrator", m)
    real = b.finished; b.finished = lambda tt: False
    try: ov_tick(); gone_wait()
    finally: b.finished = real
    r = b.st["overview"]["items"]["t:" + m]; assert r.get("ping_due") and not finished_pings(), (r, finished_pings())
    ov_tick(); assert len(finished_pings()) == 1 and not r.get("ping_due"); ov_tick(2); assert len(finished_pings()) == 1, "once"

def test_janitor9_drop_interim_keeps_a_real_report_and_deletes_a_not_sent_yet_one():
    b.cfg["one_answer_per_turn"] = True
    origin, mid = request("Send the skill"); t = b.st["threads"][mid]; event("started", mid); tick()
    answer(mid, "**Built.** All good."); tick(); assert t.get("turn_answer") and t["turn_answer"]["head"].startswith("Built") or "Built" in t["turn_answer"]["head"]
    n = len(fake.msgs(origin)); b.drop_interim(t); assert len(fake.msgs(origin)) == n and "turn_answer" not in t, "a real report stays"
    origin2, mid2 = request("Send the skill again"); t2 = b.st["threads"][mid2]; event("started", mid2); tick()
    answer(mid2, "**Not sent yet.** Queued."); tick(); assert any("Not sent yet" in m["content"] for m in fake.msgs(origin2))
    b.drop_interim(t2); assert not any("Not sent yet" in m["content"] for m in fake.msgs(origin2)), "interim deleted"

def test_janitor_codex_fork_turn_is_not_a_session():
    sh = load_module("session_hook_fork_test", "session-hook.py"); fork = "You are in a fork of an existing Codex thread.\nFill the structured description field..."
    assert not sh.real_prompt(fork) and sh.real_prompt("Is the report complete?"); assert b.clean_prompt(fork) == ""

def test_loops_card_red_for_older_cycle_question_asked_after_newer_cycle_done():
    loops_on(); ids = ["20261111T200758Z-cccc0001", "20261112T040807Z-cccc0002"]
    for i, (tid, age) in enumerate(zip(ids, (30000, 20000))):
        th = b.thread(tid, "Improvement cycle started for Stack.io (ask)"); th.update(mailed=True, t0=CLOCK.time() - age, dthread=str(8880 + i))
    b.st["threads"][ids[1]]["last_end"] = CLOCK.time() - 10000
    b.st["threads"][ids[0]]["asking"] = "Publish"; b.st["q"]["qpub"] = {"from": "orchestrator", "options": [{"label": "A"}], "thread": ids[0], "at": CLOCK.time() - 5000}
    assert b.state_word(b.st["threads"][ids[1]]) == "DONE"
    loop_tick(); card = next(m["content"] for m in board_bot(LOOPS) if "**Stack.io**" in m["content"])
    assert card.startswith("🔴") and "8880" in card, card

def test_janitor10_a_slash_only_first_prompt_takes_the_next_real_prompt_as_title():
    assert b.session_label({"project": "scratch-2026-11-09-abcdef", "first_prompt": "/ponytail:ponytail off"}, "Fix the login page") == "Fix the login page"
    assert b.session_label({"project": "scratch-2026-11-09-abcdef", "first_prompt": "/ponytail:ponytail off"}, "/clear") == "Session"
    ov_session(project="scratch-2026-11-09-abcdef", first="/ponytail:ponytail off", prompt="Fix the login page")
    it = next(x for x in bridge_mod.topics.items(CLOCK.time()) if x["kind"] == "session"); assert it["title"] == "Fix the login page", it
    session_file(prompt="/ponytail:ponytail off"); tick(); c = b.st["sessions"][f"{MAC}/s1"]
    assert not c.get("thread"), "a slash-only prompt opens no thread (janitor 1112f)"
    session_file(prompt="Fix the login page"); tick(); th = c["thread"]
    assert fake.channels[th]["name"] == "Fix the login page · Mac · claude" and c["named"] and th not in fake.renames, fake.channels[th]["name"]

def test_janitor_1113d_new_work_that_puts_a_question_on_hold_keeps_title_and_epoch():
    origin, mid = request("Build the lights"); t = b.st["threads"][mid]; event("started", mid); event("done", mid); q = question(mid); tick()
    title, epoch = t["title"], t.get("epoch")
    CLOCK.offset += 60; owner_msg("Make them less detailed, add a shop button?", cid=origin); tick()
    assert t["title"] == title and t.get("epoch") == epoch, (t["title"], t.get("epoch"))
    assert q["id"] not in b.st["q"] and any("asks:" in m["content"] and "On hold" in m["content"] for m in fake.msgs(origin)), "the question is on hold"

def test_janitor_1113d_session_first_prompt_skips_a_slash_only_start():
    sh = load_module("session_hook_1113d", "session-hook.py")
    for pr in ("/ponytail:ponytail off", "Fix the login page", "Now the footer"): rec = sh.update("s1113d", cwd="/Users/alex/Kepler", last_prompt=pr)
    assert rec["first_prompt"] == "Fix the login page" and rec["last_prompt"] == "Now the footer", rec
    assert sh.slash_only("/a:b off") and not sh.slash_only("/fix the login page") and not sh.slash_only("Fix it")

def test_janitor_1113d_session_finished_ping_names_the_turn_not_the_first_prompt():
    ov_on(); ov_session(state="idle", prompt="Fix the pool"); ov_tick()
    CLOCK.offset += 300; ov_session(state="working", prompt="Now the shop", first="Fix the pool"); ov_tick()
    CLOCK.offset += 130; ov_session(state="idle", prompt="Now the shop", first="Fix the pool"); ov_tick()
    p = sent_pings(); assert p and "**Finished**" in p[-1]["content"] and "Now the shop" in p[-1]["content"] and "Fix the pool" not in p[-1]["content"], [x["content"] for x in p]

def test_janitor10_gemini_routing_notice_goes_when_the_answer_posts_but_a_gemini_answer_stays():
    b.cfg["one_answer_per_turn"] = True
    origin, mid = request("Fix the pool"); th = b.st["threads"][mid]["dthread"]; origin2, mid2 = request("Fix the shop"); th2 = b.st["threads"][mid2]["dthread"]
    triage_on({"action": "forward", "thread": mid, "reply": "Sent to the orchestrator."})
    owner_msg("also the lights", cid=th); CLOCK.offset += 5; tick(); tick()
    assert any(m["content"].startswith("✦ Sent to the orchestrator.") for m in fake.msgs(th)) and b.st["threads"][mid].get("gemini_notes")
    answer(mid, "**Done.** Lights fixed."); tick()
    assert not any(m["content"].startswith("✦") for m in fake.msgs(th)) and any("Lights fixed" in m["content"] for m in fake.msgs(th)), [m["content"] for m in fake.msgs(th)]
    triage_on({"action": "answer", "thread": mid2, "reply": "Yes, it is queued.", "model": "g"})
    owner_msg("is it queued?", cid=th2); CLOCK.offset += 5; tick(); tick()
    answer(mid2, "**Done.** Shop fixed."); tick()
    assert any(m["content"].startswith("✦ Yes, it is queued.") for m in fake.msgs(th2)), "a Gemini answer is never deleted"

def test_janitor10_live_loop_cycle_names_the_device_running_it():
    loops_on(); tid = "20261111T000000Z-cccc0001"
    t = b.thread(tid, "Field Notebook: improvement cycle"); t.update(mailed=True, t0=CLOCK.time() - 60)
    t["runs"]["r1"] = {"agent": "builder", "label": "PC", "host": PC, "t0": CLOCK.time() - 30}; assert b.state_word(t) == "WORKING"
    loop_tick(); card = next(m["content"] for m in board_bot(LOOPS) if "**Field Notebook**" in m["content"])
    assert "running on PC" in card and "no device can open it now" not in card and "cycle running now" in card, card

def test_tap_starts_a_new_answer_turn():
    origin, mid = request(); q = question(mid); tick(); t = b.st["threads"][mid]
    t["turn_answer"] = {"head": "old", "id": "1"}
    press(origin, q["id"], 0)
    assert "turn_answer" not in t, t.get("turn_answer")

def test_overview_and_loops_show_queued():
    row = lambda st: ("s:" + st, {"state": st, "title": "x " + st, "tool": "claude", "where": "Mac"})
    assert "Queued: 1" in b.card_text("Kepler", [row("queued"), row("working")]).splitlines()[1]
    assert "Queued" not in b.card_text("Kepler", [row("working")]), "no queued row: header unchanged"
    # loop_cards fixture costs too much setup: only card_text is tested here

def test_track_line_drops_report_before_a_later_build():
    runs = lambda *rs: [{"agent": a, "t0": bridge_mod.time.time() - 100 + n, "end": 1 if done else None} for n, (a, done) in enumerate(rs)]
    t = {}
    try:
        b.cur_runs = lambda _t: runs(("orchestrator", 1), ("builder", 1), ("orchestrator", 1), ("builder", 0))
        assert b.track_line(t) == "Plan ✓ → Build ● ×2", b.track_line(t)
        b.cur_runs = lambda _t: runs(("orchestrator", 1), ("builder", 1), ("orchestrator", 1))
        assert b.track_line(t) == "Plan ✓ → Build ✓ → Report ✓", b.track_line(t)
    finally: del b.cur_runs

# ---- janitor findings, 2026-11-12 (1112c) ----
def test_loop_session_pings_finished_once_per_typed_prompt():
    ov_on(); ov_session(state="idle", prompt="/loop do a pass"); ov_tick()
    for _ in range(2):  # two /loop passes, no new prompt from the owner
        CLOCK.offset += 300; ov_session(state="working", prompt="/loop do a pass"); ov_tick()
        CLOCK.offset += 130; ov_session(state="idle", prompt="/loop do a pass"); ov_tick()
    assert len(finished_pings()) == 1, [m["content"] for m in sent_pings()]
    CLOCK.offset += 300; ov_session(state="working", prompt="/loop do another pass"); ov_tick()  # the owner typed a new prompt
    CLOCK.offset += 130; ov_session(state="idle", prompt="/loop do another pass"); ov_tick()
    assert len(finished_pings()) == 2, [m["content"] for m in sent_pings()]
    # a session that is not a /loop still pings after each piece of new work
    ov_session(sid="s2", state="idle", prompt="Fix it"); ov_tick()
    for _ in range(2):
        CLOCK.offset += 300; ov_session(sid="s2", state="working", prompt="Fix it"); ov_tick()
        CLOCK.offset += 130; ov_session(sid="s2", state="idle", prompt="Fix it"); ov_tick()
    assert len(finished_pings()) == 4, [m["content"] for m in sent_pings()]

def test_state_reaction_on_owners_thread_starter():
    q = urllib.parse.quote
    origin, mid = request(); event("started", mid); tick(); t = b.st["threads"][mid]
    assert t.get("dthread") == origin, t.get("dthread")
    mine = lambda: [(m, e) for m, msg, e in fake.react_log if msg == origin and e not in (bridge_mod.SEEN_EMOJI, bridge_mod.FAIL_EMOJI)]
    assert mine() == [("PUT", q("🟡"))], mine()
    answer(mid, "Fixed."); agent_mod.ack("orchestrator", mid); event("done", mid); tick(2)
    assert b.state_word(t) == "DONE" and mine() == [("PUT", q("🟡")), ("DELETE", q("🟡")), ("PUT", q("✅"))], mine()
    n = len(fake.react_log); b.render_status(mid, t); assert len(fake.react_log) == n, "same state: no reaction call"
    # a bot-anchored thread (no message of theirs started it) gets none
    o2, m2 = request("Pool in Fish"); t2 = b.st["threads"][m2]; t2.pop("origin", None); t2.pop("state_react", None)
    n = len(fake.react_log); event("started", m2); tick(); b.render_status(m2, t2)
    assert not [x for x in fake.react_log[n:] if x[2] not in (bridge_mod.SEEN_EMOJI, bridge_mod.FAIL_EMOJI)], fake.react_log[n:]

RESTART1 = "Restart servers now? The bridge and the mail runner both changed, so a restart is needed to load the new code."
RESTART2 = "Restart servers now? The bridge and mail runner both changed, so a restart is needed to load the new code."
def two_open(body1="Which one?", body2="Another thing?", opts2=("A", "B")):
    origin, mid = request(); q1 = question(mid, body1); tick()
    q2 = question(mid, body2, options=opts2); tick()
    return origin, mid, q1, q2
def dup_of(q1, origin, key="dup-1"):
    """a second open copy of q1 in the same thread (what the replace-on-ask check would normally prevent)"""
    import copy
    d = copy.deepcopy(b.st["q"][q1["id"]]); d["msg"] = fake.add_msg(origin, "copy card")["id"]; d["at"] = float(d.get("at") or 0) - 1; d["extra"] = []
    b.st["q"] = {key: d, **b.st["q"]}; return key, d  # first in the dict: the typed answer goes to the newest, q1
def card_of(msgid): return next(m for c in fake.channels.values() for m in c["msgs"].values() if m["id"] == msgid)

def test_one_question_answered_thread_still_asks():
    origin, mid, q1, q2 = two_open(opts2=("C", "D"))
    t = b.st["threads"][mid]; assert b.asks(t)
    press(origin, q1["id"], 0)
    assert q1["id"] not in b.st["q"] and q2["id"] in b.st["q"], "the other question stays open"
    assert t.get("asking") == b.st["q"][q2["id"]]["subject"][:120] and b.asks(t)

def test_last_question_answered_clears_asking():
    origin, mid, q1, q2 = two_open(opts2=("C", "D"))
    t = b.st["threads"][mid]
    press(origin, q1["id"], 0); press(origin, q2["id"], 0)
    assert not b.st["q"] and not t.get("asking") and not b.asks(t)

def test_tap_closes_duplicate_question():
    origin, mid = request(); q1 = question(mid, RESTART1, options=("Yes", "No")); tick()
    key, d = dup_of(q1, origin); b.st["threads"][mid]["asking"] = "Pick one"
    other = question(mid, "Something entirely different to decide here", options=("X", "Y")); tick()
    press(origin, q1["id"], 0)
    assert key not in b.st["q"] and other["id"] in b.st["q"], "duplicate closed, different question open"
    c = card_of(d["msg"]); assert c["components"] == [] and "Closed" in c["content"], c
    assert b.asks(b.st["threads"][mid])

def test_typed_answer_closes_duplicate_question():
    origin, mid = request(); q1 = question(mid, RESTART1, options=("Yes", "No")); tick()
    key, d = dup_of(q1, origin); d["body"] = RESTART2
    owner_msg("yes restart", cid=origin, reference=None); tick()
    left = [k for k in (q1["id"], key) if k in b.st["q"]]
    assert len(left) == 0, left
    assert card_of(d["msg"])["components"] == [] and not b.asks(b.st["threads"][mid])

def test_on_question_replaces_same_question():
    origin, mid = request(); q1 = question(mid, RESTART1, options=("Yes", "No")); tick()
    old = b.st["q"][q1["id"]]["msg"]
    q2 = question(mid, RESTART2, options=("Yes", "No")); tick()
    assert q1["id"] not in b.st["q"] and q2["id"] in b.st["q"]
    assert not any(m["id"] == old for c in fake.channels.values() for m in c["msgs"].values()), "the older copy of the same question is gone"
    assert b.asks(b.st["threads"][mid])

def test_janitor_1112e_answering_one_question_keeps_the_other_open():
    origin, mid = request(); t = b.st["threads"][mid]
    q1 = question(mid, "Pick a fish color?", ("Red", "Blue")); q2 = question(mid, "Ship the shark fix?", ("Ship", "Hold"))
    tick(); press(None, q1["id"], 0); tick()
    assert q1["id"] not in b.st["q"] and q2["id"] in b.st["q"] and b.asks(t) is True and b.finished(t) is False
    press(None, q2["id"], 0); tick(); assert b.asks(t) is False

def test_janitor_1112e_progress_board_lists_question_while_working():
    b.cfg["progress_boards"] = True
    origin, mid = request("Shark pose fix"); b.st["threads"][mid]["title"] = "Shark pose fix"; event("started", mid)
    question(mid, "Probe and fix both?", ("Probe", "Skip")); tick()
    b.st["boards"] = {"ids": [], "text": []}; b.render_boards(CLOCK.time())
    T = b.st["boards"]["text"]
    assert "Shark pose fix" in T[-5] and "Shark pose fix" not in T[-4], T

def test_janitor1112f_progress_lists_agent_only_work():
    b.cfg["progress_boards"] = True
    event("started", "agent-only-1", title="Janitor findings"); event("started", "notice-1", agent="memory", title="Memory notice"); tick()
    t = b.st["threads"]["agent-only-1"]; assert not (t.get("origin") or t.get("mailed") or t.get("dthread")), t
    b.st["boards"] = {"ids": [], "text": []}; b.render_boards(CLOCK.time())
    T = b.st["boards"]["text"]
    assert "Janitor findings" in T[-4] and "Working** (1)" in T[-4], T
    assert not any("Memory notice" in x for x in T), "a device notice stays off #progress"

def test_janitor1112f_slash_only_session_waits_for_a_real_prompt():
    session_file(prompt="/ponytail:ponytail off"); tick(); c = b.st["sessions"][f"{MAC}/s1"]
    assert not c.get("thread"), c
    session_file(prompt="Fix the bridge naming"); tick(); th = c["thread"]
    name = fake.channels[th]["name"]
    assert name.startswith("Fix the bridge naming") and "/" not in name and c["named"] and th not in fake.renames, name

def test_janitor1112f_anchor_red_while_question_open():
    origin, mid = request("Fish pose fix"); t = b.st["threads"][mid]; event("started", mid); tick()
    assert b.state_word(t) == "WORKING" and b.anchor_text(t).startswith("🟡"), b.anchor_text(t)
    question(mid, "Ship it?", ("Ship", "Hold")); tick()
    assert b.asks(t) and b.state_word(t) == "WORKING" and b.anchor_text(t).startswith("🔴"), b.anchor_text(t)

def test_janitor_1112f_topic_from_folder():
    tp = bridge_mod.topics; real_items, real_ask = tp.items, tp.ask_model
    def it(k, folder): return {"key": k, "project": "", "folder": folder, "title": "x", "state": "idle", "updated": 0.0}
    def run(folder, known, ask):
        tp.items = lambda now=None: [it("s:1", folder)]; tp.ask_model = ask
        tp.save_labels({f"k{n}": {"topic": x, "at": 1} for n, x in enumerate(known)}); tp.label(force=True); return tp.load_labels().get("s:1", {}).get("topic")
    def boom(todo, known): raise AssertionError("model called")
    try:
        assert run("SAGE", ["SAGE Wrist Band", "Kepler"], boom) == "SAGE Wrist Band"
        assert run("SAGE", ["SAGE Wrist Band", "SAGE Docs"], lambda todo, known: {"s:1": "Model Pick"}) == "Model Pick"
        assert run("SAGE", ["Kepler"], lambda todo, known: {"s:1": "Model Pick"}) == "Model Pick"
    finally: tp.items, tp.ask_model = real_items, real_ask

def test_janitor_1112f_argon_welcome_stored_and_backfilled():
    real_api, calls = bridge_mod.api, []
    try:
        b.cfg["argon_chat"] = True; b.st.pop("argon_thread", None)
        def api_create(method, path, body=None, **k): return {"id": "T1"}
        bridge_mod.api = api_create; b.send_to = lambda ch, text, **k: "W1"; b.join_owner = lambda cid: None
        b.ensure_argon(CLOCK.time()); assert b.st["argon_thread"] == "T1" and b.st["argon_welcome"] == "W1"
        b.st["cards"] = {"30": {"md": "✦ later"}, "10": {"md": "✦ welcome"}, "5": {"md": "plain"}, "2": {"md": "✦ mine"}}  # bot posts are cards with empty content
        for case, rows in (("pick", [{"id": "30", "author": {"id": BOT}, "content": ""}, {"id": "10", "author": {"id": BOT}, "content": ""},
                                      {"id": "5", "author": {"id": BOT}, "content": ""}, {"id": "2", "author": {"id": OWNER}, "content": ""}]), ("fail", None)):
            b.st.pop("argon_welcome", None); b.argon_backfilled = False; b.st["argon_thread"] = "T1"
            def api_get(method, path, body=None, **k):
                calls.append(path)
                if rows is None: raise RuntimeError("down")
                return rows
            bridge_mod.api = api_get; b.ensure_argon(CLOCK.time())
            assert b.st.get("argon_welcome") == ("10" if rows else None), (case, b.st.get("argon_welcome"))
        n = len(calls); b.ensure_argon(CLOCK.time()); assert len(calls) == n  # once per start
    finally: bridge_mod.api = real_api; b.st.pop("cards", None)

def mac_offline(): fleet_mod._write(tmp / "Queue/devices" / f"{MAC}.json", {"host": MAC, "platform": "mac", "at": real_time.time() - 9999, "repos": []})

def test_move_ping_removes_a_ping_stranded_in_the_old_topic():
    calls = []; real_api = bridge_mod.api
    bridge_mod.api = lambda method, path, body=None, **k: calls.append((method, path))
    try:
        ov = {"items": {"s:a": {"topic": "Seek", "state": "idle"}, "s:b": {"topic": "Kepler", "state": "idle"}}, "pings": [],
              "cards": {"Kepler": {"thread": "TH", "pings": ["1", "2", "3"], "by_key": {"s:a": "1", "s:b": "2", "s:gone": "3"}}}}
        b.move_pings(ov, CLOCK.time())
        c = ov["cards"]["Kepler"]
        assert calls == [("DELETE", "/channels/TH/messages/1")], calls
        assert c["by_key"] == {"s:b": "2", "s:gone": "3"} and c["pings"] == ["2", "3"], c
    finally: bridge_mod.api = real_api

def test_offline_device_working_session_is_idle_in_topics():
    import topics
    ov_session(host=MAC, state="working"); ov_session(sid="s2", host=PC, state="working")
    got = {i["key"]: i for i in topics.items(CLOCK.time())}
    assert got["s:s1"]["state"] == "working" and not got["s:s1"]["offline"]
    mac_offline()
    got = {i["key"]: i for i in topics.items(CLOCK.time())}
    assert got["s:s1"]["state"] == "idle" and got["s:s1"]["offline"] is True, got["s:s1"]
    assert got["s:s2"]["state"] == "working" and not got["s:s2"]["offline"], "this host is never offline"

def test_offline_device_gives_no_finished_ping():
    ov_on(); ov_session(state="idle"); ov_tick()
    CLOCK.offset += 300; ov_session(state="working"); ov_tick()
    CLOCK.offset += 130; ov_session(state="working"); mac_offline(); ov_tick()
    assert not sent_pings(), [x["content"] for x in sent_pings()]

def test_offline_device_session_card_says_idle_and_offline():
    d = tmp / f"Queue/sessions/{MAC}"; d.mkdir(parents=True)
    (d / "s1.json").write_text(json.dumps({"session_id": "s1", "host": MAC, "tool": "claude", "project": "Kepler", "state": "working", "last_prompt": "Do the thing", "updated": real_time.time()}), encoding="utf-8")
    mac_offline(); tick()
    t = fake.msgs(THREADS)[0]["content"]
    assert "Idle" in t and "Working" not in t and "Mac is offline" in t, t

def test_janitor_1113h_loop_card_claim_shows_off_now_and_when_it_frees():
    loops_on(); GL = bridge_mod.GL; st = tmp / "Queue/loop/state.json"
    st.write_text(json.dumps({"last": {"Field Notebook": CLOCK.time() - 9 * 3600}}), encoding="utf-8")
    stamp = lambda s: real_time.strftime("%Y-%m-%dT%H:%M:%SZ", real_time.gmtime(real_time.time() + s))
    lock = tmp / "Queue/locks/Field Notebook.json"; lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"repo": "Field Notebook", "owner": MAC, "since": stamp(-7200), "until": stamp(3600)}), encoding="utf-8")
    d = json.loads(lock.read_text(encoding="utf-8")); assert GL.held("Field Notebook") == d
    assert abs(GL.lock_until(d) - (real_time.time() + 3600)) < 5 and GL.lock_until({"since": stamp(-3600)}) > real_time.time() + 10 * 3600 and GL.lock_until({"since": "x"}) is None and GL.lock_until({}) is None
    real_av = bridge_mod.fleet.available
    try:
        if any(x["ok"] for x in fleet_mod.where("Field Notebook")):
            for up in (False, True):
                bridge_mod.fleet.available = lambda h, up=up: up
                loop_tick(); card = next(m["content"] for m in board_bot(LOOPS) if "**Field Notebook**" in m["content"])
                assert "claimed by Mac" in card and "frees <t:" in card and (("(off now)" in card) == (not up)), card
    finally: bridge_mod.fleet.available = real_av
    lock.unlink()

def test_janitor_1113h_asks_owner_sees_a_plain_ask_before_a_short_aside():
    a = bridge_mod.asks_owner
    body = ('**Publish is still not done.**\n\n- **Left open:** the dialog is still open.\n- **Your choice:** either **close the windows** or **pick the repo** and click Publish. Give me a go-ahead after either, and I' + "'ll check." + '\n'
            '- **Your note:** I haven' + "'t saved the rule to memory. The orchestrator or you should add it." + '\n\n---\nMeasured by the runner: no source file changed in this run.')
    assert a(body) and a('Your choice: A or B.\n- Note: saved.')
    assert not a("All done.\nNo action needed.\nPublished v12.")

def test_janitor_1113h_loop_cycle_titles_take_their_projects_topic_without_the_model():
    tp = bridge_mod.topics; real_items, real_ask = tp.items, tp.ask_model
    def boom(todo, known): raise AssertionError("model called")
    def run(title, known):
        tp.items = lambda now=None: [{"key": "t:1", "project": "", "folder": "", "title": title, "state": "idle", "updated": 0.0}]; tp.ask_model = boom
        tp.save_labels({f"k{n}": {"topic": x, "at": 1} for n, x in enumerate(known)}); tp.label(force=True); return tp.load_labels().get("t:1", {}).get("topic")
    try:
        assert run("mysite: improvement cycle (ask)", ["Kepler"]) == "mysite"
        assert run("SloopApp: improvement cycle (auto)", ["Sloop App", "Kepler"]) == "Sloop App"
        assert run("Orchestration: improvement cycle (ask)", ["Orchestration Loop"]) == "Orchestration Loop"
        assert run("Improvement cycle started for Stack.io (ask mode)", ["Stack.io"]) == "Stack.io"
        assert run("[loop] Improvement cycle: Kepler", ["Kepler"]) == "Kepler"
    finally: tp.items, tp.ask_model = real_items, real_ask


# ---- Nov 13: usage limits pause, live cards, Open in Claude, file buttons, device notices ----
LIMIT_TEXT = "Claude's weekly limit is reached. Resumes on its own Mon Nov 16, 9:00 AM PT, or sooner on Codex when it has quota."
def raw_event(kind, tid, ref=None, agent="orchestrator", detail="", host=PC, **extra):
    """An event as the new runner writes it: may carry session (fleet.event has no such argument)."""
    e = {"id": f"e{fake.nid()}", "ts": CLOCK.time(), "host": host, "kind": kind, "title": "t", "detail": detail, "agent": agent, "thread": tid, "repo": "", "ref": ref or tid, **extra}
    f = tmp / "Queue/events/raw.jsonl"; f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as h: h.write(json.dumps(e) + "\n")
def state_card(th, word): return next((m for m in reversed(fake.msgs(th)) if m["content"].startswith(f"{bridge_mod.STATES[word][0]} **{bridge_mod.STATES[word][1]}**")), None)
def ids(m): return [x.get("custom_id") for x in bridge_mod.walk_components(m["components"]) if x.get("custom_id")]
def tap(cid, custom, msg=None):
    b.dispatch("INTERACTION_CREATE", {"type": 3, "id": fake.nid(), "token": "tok" + custom, "channel_id": cid, "member": {"user": {"id": OWNER}}, "data": {"custom_id": custom}, "message": msg or {"id": "0"}})
def at_original(since=0): return [c for c in fake.calls[since:] if c[0] == "PATCH" and c[1].endswith("/messages/@original")]
def write_live(ref, n, says, **extra):
    fleet_mod._write(tmp / "Queue/live" / f"{ref}.json", {"agent": "builder", "host": PC, "doing": f"step {n - 1}", "tools": n, "at": CLOCK.time(), "t0": CLOCK.time(), "trail": [[0, f"step {k}"] for k in range(n)], "says": says, **extra})
    b.last_live = 0

def test_usage_limit_pauses_without_a_fail_ping_or_alert_and_resumes():
    ov_on(); origin, mid = request("Bike seat"); ov_label("t:" + mid, "Bikes"); ov_tick()
    raw_event("started", mid, agent="builder"); ov_tick()
    raw_event("paused", mid, agent="builder", detail=LIMIT_TEXT, session="sess-9"); ov_tick(3)
    t = b.st["threads"][mid]; assert b.state_word(t) == "PAUSED", b.state_word(t)
    card = state_card(origin, "PAUSED"); assert card and "Resumes on its own Mon Nov 16" in card["content"], [m["content"] for m in fake.msgs(origin)]
    assert f"o:{mid}:0" in ids(card), "the session from the paused event gives the card Open in Claude"
    assert not fake.msgs(ALERTS), "no failure alert"
    assert not [p for p in sent_pings() if "Failed" in p["content"] or "Finished" in p["content"]], [p["content"] for p in sent_pings()]
    assert urllib.parse.quote("❌") not in [x[2] for x in fake.react_log], "no failed mark on their message"
    b.cfg["quick_status"] = True; n = len(inbox("orchestrator")); owner_msg("?", cid=origin); tick()
    assert len(inbox("orchestrator")) == n, "a bare ? in a paused thread gets the quick status"
    raw_event("started", mid, agent="builder"); tick()
    assert b.state_word(t) == "WORKING" and state_card(origin, "WORKING"), "a later start resumes it"

def test_legacy_limit_failure_is_shown_paused():
    origin, mid = request(); event("started", mid); event("failed", mid, detail="Claude AI usage limit reached. You've hit your weekly limit."); tick()
    t = b.st["threads"][mid]; assert b.state_word(t) == "PAUSED" and not fake.msgs(ALERTS), (b.state_word(t), fake.msgs(ALERTS))
    for r in t["runs"].values(): r["end"] = "failed"  # recorded as failed before this code: still rendered paused
    t["detail"] = "You've hit your session limit · resets 4pm"; assert b.state_word(t) == "PAUSED"
    t["detail"] = "boom"; assert b.state_word(t) == "FAILED", "any other failure stays a failure"

def test_live_card_shows_device_start_last_steps_and_words_and_edits_at_most_every_20s():
    origin, mid = request(); raw_event("started", mid, agent="builder"); tick()
    write_live(mid, 4, "I found the bug in the pool script " + "x" * 400); tick()
    card = state_card(origin, "WORKING"); c = card["content"]
    assert "`builder` on PC, started <t:" in c and "step 1 → step 2 → step 3" in c and "step 0" not in c, c
    words = next(l for l in c.splitlines() if l.startswith("> ")); assert words.startswith("> I found the bug") and len(words) <= 203, words
    edits = lambda: sum(1 for m, path, _ in fake.calls if m == "PATCH" and path == f"/channels/{origin}/messages/{card['id']}")
    n = edits(); CLOCK.offset += 5; write_live(mid, 5, "Later words"); tick()
    assert edits() == n and "Later words" not in state_card(origin, "WORKING")["content"], "within 20 s of the last edit: not edited"
    CLOCK.offset += 16; b.last_live = 0; tick()
    assert edits() == n + 1 and "Later words" in state_card(origin, "WORKING")["content"], "edited once 20 s have passed"
    CLOCK.offset += 30; b.last_live = 0; tick(); assert edits() == n + 1, "no change in the live file: no edit"

def test_open_in_claude_writes_the_request_and_edits_the_reply_with_the_link():
    origin, mid = request(); raw_event("started", mid, agent="builder"); tick()
    card = state_card(origin, "WORKING"); assert not ids(card), "no session known yet: no button"
    write_live(mid, 2, "Working on it", session="sess-1", cwd="C:/Dev/Work/Kepler"); tick()
    card = state_card(origin, "WORKING"); assert ids(card) == [f"o:{mid}:0"], ids(card)
    n = len(fake.calls); tap(origin, f"o:{mid}:0", card)
    assert fake.responses[-1] == {"type": 5, "data": {"flags": 64}}, fake.responses[-1]
    reqs = [f for f in (tmp / "Queue/open" / PC).glob("*.json")]; assert len(reqs) == 1, reqs
    r = json.loads(reqs[0].read_text(encoding="utf-8"))
    assert r["kind"] == "session" and r["session"] == "sess-1" and r["cwd"] == "C:/Dev/Work/Kepler" and r["name"] == "Fix the pool in Kepler" and r["by"] == "bridge" and r["at"], r
    tick(); assert not at_original(n), "nothing to say until the runner answers"
    reqs[0].with_name(reqs[0].stem + ".done.json").write_text(json.dumps({"url": "https://claude.ai/code/session_1"}), encoding="utf-8"); tick()
    e = at_original(n); assert len(e) == 1 and "/webhooks/" in e[0][1] and "/tok" in e[0][1], e
    btn = e[0][2]["components"][0]["components"][0]; assert btn["style"] == 5 and btn["url"] == "https://claude.ai/code/session_1" and btn["label"] == "Open in Claude", btn
    assert not list((tmp / "Queue/open" / PC).glob("*.json")) and not b.st["opens"], "handled and cleaned up"
    m = len(fake.calls); tap(origin, f"o:{mid}:0", card); CLOCK.offset += bridge_mod.OPEN_WAIT + 1; tick()
    e = at_original(m); assert len(e) == 1 and "No answer from the PC" in e[0][2]["content"] and not e[0][2]["components"], e
    raw_event("done", mid, agent="builder", session="sess-2"); tick()
    assert b.st["threads"][mid]["runs"][mid]["session"] == "sess-2", "the end event's session counts after the run"

def test_answer_row_keeps_open_in_claude_until_a_newer_run_answers():
    origin, mid = request(); raw_event("started", mid); agent_mod.ack("orchestrator", mid); raw_event("done", mid, session="sess-a"); answer(mid, "Done."); tick(2)
    card = close_card(origin); assert card and ids(card) == [f"o:{mid}:0", "c:thread:0"], (card and ids(card))
    assert not state_card(origin, "DONE"), "the answer alone is the outcome"
    n = len(fake.calls); tap(origin, f"o:{mid}:0", card); req = next((tmp / "Queue/open" / PC).glob("*.json"))
    assert json.loads(req.read_text(encoding="utf-8"))["session"] == "sess-a" and fake.responses[-1] == {"type": 5, "data": {"flags": 64}}
    owner_msg("now make it blue", cid=origin); tick(); new = inbox("orchestrator")[-1]["id"]
    raw_event("started", mid, ref=new); write_live(new, 2, "On it", session="sess-b"); tick()
    assert not [x for m in fake.msgs(origin) for x in ids(m) if x == f"o:{mid}:0"], "the newer run replaced it"
    assert ids(state_card(origin, "WORKING")) == [f"o:{new}:0"], "the live card opens the running session"
    agent_mod.ack("orchestrator", new); raw_event("done", mid, ref=new, session="sess-b"); answer(mid, "Blue now."); tick(2)
    assert ids(close_card(origin)) == [f"o:{new}:0", "c:thread:0"] and fake.msgs(origin)[-1]["id"] == close_card(origin)["id"], ids(close_card(origin))
    question(mid, "Publish it?", ("Yes", "No")); tick(2)
    assert fake.msgs(origin)[-1]["content"].startswith(f"<@{OWNER}>") and not close_card(origin), "a question with buttons stays last; the answer row waits"

def test_ended_thread_with_close_and_open_in_claude_is_archived():
    origin, mid = request(); b.st["threads"][mid].pop("origin", None); b.st["threads"][mid].pop("owner_at", None)
    raw_event("started", mid); agent_mod.ack("orchestrator", mid); raw_event("done", mid, session="sess-a"); answer(mid, "Done."); tick(2); sweep_tick()
    assert ids(close_card(origin)) == [f"o:{mid}:0", "c:thread:0"] and b.ended(mid, b.st["threads"][mid])
    idle(bridge_mod.ARCHIVE_AFTER // 60 + 1); assert closed(origin), "Open in Claude is no follow-up: it archives like Close-only"

def open_buttons(cid): return [(x["label"], x["custom_id"]) for m in fake.msgs(cid) for x in bridge_mod.walk_components(m["components"]) if str(x.get("custom_id", "")).startswith("of:")]
def test_answer_naming_local_files_gets_open_buttons_per_machine():
    origin, mid = request(); (tmp / "Kepler/docs").mkdir(parents=True); (tmp / "Queue/out").mkdir(parents=True); (tmp / "Queue/out/page.html").write_text("<p>", encoding="utf-8")
    answer(mid, "Here: `/Users/alex/Developer/Work/Queue/web/rate.html`, the plan at C:\\Dev\\Work\\Kepler\\docs\\plan.pdf and Kepler/docs/notes.md. https://example.com/x.html is a site, docs/x.md is not ours."); tick()
    got = open_buttons(origin)
    assert [l for l, _ in got] == ["Open rate.html on Mac", "Open rate.html on PC", "Open plan.pdf on Mac", "Open plan.pdf on PC", "Open notes.md on Mac", "Open notes.md on PC"], got
    paths = {k: v["path"] for k, v in b.st["threads"][mid]["open_files"].items()}
    assert sorted(paths.values()) == ["Kepler/docs/notes.md", "Kepler/docs/plan.pdf", "Queue/web/rate.html"], paths
    agent_mod.ack("orchestrator", mid); event("done", mid); tick(2); assert close_card(origin) and len(open_buttons(origin)) == 6, "the Close row does not strip the open buttons"
    o2, m2 = request("Second"); answer(m2, "Saved to /Users/alex/Desktop/shot.png"); tick()
    assert open_buttons(o2) == [("Open on Mac", "of:0:mac")], open_buttons(o2)
    o3, m3 = request("Third"); answer(m3, "See `C:\\Users\\alex\\Downloads\\report.pdf`\nATTACH: Queue/out/page.html"); tick()
    assert [l for l, _ in open_buttons(o3)] == ["Open report.pdf on PC", "Open page.html on Mac", "Open page.html on PC"], open_buttons(o3)
    # a tap: the request goes to that machine, the private reply says what happened
    cid = dict((l, c) for l, c in got)["Open rate.html on Mac"]; n = len(fake.calls); tap(origin, cid)
    req = next((tmp / "Queue/open" / MAC).glob("*.json")); r = json.loads(req.read_text(encoding="utf-8"))
    assert r["kind"] == "file" and r["path"] == "Queue/web/rate.html" and r["by"] == "bridge", r
    req.with_name(req.stem + ".done.json").write_text("{}", encoding="utf-8"); tick()
    assert at_original(n)[-1][2]["content"] == "Opened rate.html on the Mac.", at_original(n)
    cid = dict(open_buttons(o2))["Open on Mac"]; n = len(fake.calls); tap(o2, cid)
    req = next((tmp / "Queue/open" / MAC).glob("*.json")); assert json.loads(req.read_text(encoding="utf-8"))["path"] == "/Users/alex/Desktop/shot.png"
    req.with_name(req.stem + ".done.json").write_text(json.dumps({"error": "no such file"}), encoding="utf-8"); tick()
    assert at_original(n)[-1][2]["content"] == "Could not open it on the Mac: no such file", at_original(n)

def test_open_button_for_a_device_that_is_down_is_not_offered():
    fleet_mod._write(tmp / "Queue/devices" / f"{PC}.json", {"host": PC, "platform": "windows", "at": real_time.time() - 3600, "repos": []})
    origin, mid = request(); answer(mid, "Built `Queue/web/rate.html`"); tick()
    assert open_buttons(origin) == [("Open on Mac", "of:0:mac")], open_buttons(origin)

def test_triage_off_the_owners_message_is_marked_at_once_and_the_card_shows_when_the_run_starts():
    origin, mid = finished_idle(); th = b.st["threads"][mid]["dthread"]; b.cfg["notepad"] = True; b.cfg["triage"] = False; n = len(inbox("orchestrator"))
    one = owner_msg("make it deeper", cid=th)
    assert ("PUT", one, bridge_mod.SEEN_EMOJI) in fake.react_log, "a visible mark the moment it arrives"
    CLOCK.offset += bridge_mod.QUIET + 1; tick(); new = inbox("orchestrator")[n:]; assert len(new) == 1, new
    assert state_card(th, "QUEUED"), [m["content"] for m in fake.msgs(th)]
    event("started", mid, ref=new[0]["id"]); tick(); assert state_card(th, "WORKING"), [m["content"] for m in fake.msgs(th)]

def test_device_and_loop_notices_make_no_thread_and_sit_on_the_usage_line():
    b.cfg["progress_boards"] = True; n0 = len(b.st["threads"])
    fleet_mod.event("waiting", f"{PC} is low on memory (1.7 GB free); new agent runs wait")
    fleet_mod.event("waiting", "Loop skipped Kepler: PC low on memory: commit 98%", repo="Kepler"); tick()
    assert len(b.st["threads"]) == n0 and not fake.threads(), "no thread record, no Discord thread"
    top = lambda s: (b.st.__setitem__("boards", {"ids": [], "text": []}), b.render_boards(CLOCK.time() + s), b.st["boards"]["text"][0])[2]
    t1 = top(10); assert "low on memory (1.7 GB free)" in t1 and "Loop skipped Kepler" in t1 and t1.startswith("📊"), t1
    fleet_mod.event("done", f"{PC} memory recovered (17.9 GB free); agent runs resume"); tick()
    t2 = top(20); assert "memory" not in t2.replace("Loop skipped Kepler: PC low on memory", "") and "Loop skipped Kepler" in t2, t2
    CLOCK.offset += bridge_mod.NOTICE_KEEP + 1; assert "Loop skipped" not in top(30), "an old skip note goes"
    assert len(b.st["threads"]) == n0

def test_janitor_critic_mail_and_orchestration_loop_cycles_get_no_thread():
    for subj in ("Discord flow: 3 finding(s) from Gemini for your review", "[loop] Improvement cycle: Orchestration"):
        m = agent_mod.send("builder", "orchestrator", subj, "x"); event("started", m["id"], title=subj); tick()
        agent_mod.send("orchestrator", "owner", "Re: " + subj, "Findings: all fine.", reply_to=m["id"]); agent_mod.ack("orchestrator", m["id"]); event("done", m["id"], title=subj); tick(2)
    assert not fake.threads() and not fake.msgs(PARENT) and not fake.msgs(PROGRESS), (fake.threads(), [x["content"] for x in fake.msgs(PARENT) + fake.msgs(PROGRESS)])
    assert not inbox("owner"), "the mail is still taken"
    m = agent_mod.send("builder", "orchestrator", "[loop] Improvement cycle: Kepler", "x"); event("started", m["id"], title="[loop] Improvement cycle: Kepler")
    agent_mod.send("orchestrator", "owner", "Re: cycle", "Kepler cycle done.", reply_to=m["id"]); tick(2); assert fake.threads(), "other loops keep their threads"

# ---- one writer per synced file: each bridge writes Queue/discord/state.<host>.json, readers merge (the owner, 15 Nov) ----
PCH, MACH = "DESK-PC", "Laptop-Mac"
def host_state(h): return tmp / "Queue/discord" / f"state.{h}.json"
def put_host_state(h, saved, **keys): fleet_mod._write(host_state(h), {"threads": {}, "q": {}, "v": 2, **keys, "saved": saved})
def waits_logged(): return sum("waiting up to" in l for l in bridge_mod.LOG_TAIL)

def test_state_host_two_hosts_write_at_once():
    real_host = bridge_mod.hostname
    try:
        as_host(PCH); b.st["alias"] = {"a": "pc"}; b.save_state()
        as_host(MACH); CLOCK.offset += 1; b2 = bridge_mod.Bridge(); assert b2.st["alias"] == {"a": "pc"}, "the Mac starts from the PC's copy"
        b2.st["alias"]["m"] = "mac"; as_host(PCH); b.st["alias"]["p"] = "pc2"; CLOCK.offset += 1; b.save_state()
        as_host(MACH); CLOCK.offset += 1; b2.save_state()  # both saved while apart
        names = {f.name for f in (tmp / "Queue/discord").glob("state*.json")}
        assert names == {f"state.{PCH}.json", f"state.{MACH}.json"}, names
        assert not (tmp / "Queue/discord/state.json").exists(), "no bridge writes the shared name any more"
        assert json.loads(host_state(PCH).read_text(encoding="utf-8"))["alias"] == {"a": "pc", "p": "pc2"}, "each copy holds only what its own device wrote"
        assert bridge_mod.shared_state()["alias"] == {"a": "pc", "m": "mac", "p": "pc2"}, "readers see both"
        as_host(PCH); CLOCK.offset += 1; b.st = b.load_state(); del b.st["alias"]["m"]; b.save_state()
        assert "m" not in bridge_mod.shared_state()["alias"], "a key removed after reading the other copy stays removed"
    finally: bridge_mod.hostname = real_host

def test_state_host_first_run_migrates_and_keeps_the_legacy_file():
    q = tmp / "Queue/discord"; fleet_mod._write(q / "state.json", {"v": 2, "threads": {}, "q": {}, "alias": {"old": "1"}})
    before = (q / "state.json").read_bytes()
    global b
    b = bridge_mod.Bridge(); assert b.st["alias"] == {"old": "1"}, "the first run reads the legacy file"
    b.st["alias"]["new"] = "2"; b.save_state()
    own = json.loads(fleet_mod.host_path(q / "state.json", bridge_mod.hostname()).read_text(encoding="utf-8"))
    assert own["alias"] == {"old": "1", "new": "2"} and own["saved"] > 0, own
    assert (q / "state.json").read_bytes() == before, "the legacy file is read-only: never written, never deleted"
    assert bridge_mod.shared_state()["alias"] == {"old": "1", "new": "2"}, "and the device's own copy outranks it"

def test_takeover_with_a_stale_local_state_merges_the_serving_hosts_copy_first():
    real_host, real_hosts = bridge_mod.hostname, b.cfg.get("hosts")
    th, lm = lock_thread(ago=20)  # the PC serves
    try:
        now = CLOCK.time(); fleet_mod._write(host_state(MACH), {"v": 2, "threads": {}, "q": {}, "alias": {"old": "mac"}, "saved": now - 38 * 3600})
        as_host(MACH); b.st = b.load_state(); assert not b.is_active(), "standby while the PC's lock is fresh"
        fleet_mod._write(host_state(PCH), {"v": 2, "threads": {}, "q": {}, "alias": {"new": "pc"}, "saved": now})  # the PC's newer copy has synced in
        CLOCK.offset += bridge_mod.DISCORD_TAKEOVER; assert b.is_active() and lock_text(th, lm).startswith(MACH + "|"), "the PC went quiet: the Mac takes over"
        assert b.st["alias"].get("new") == "pc" and b.st["alias"].get("old") == "mac", ("taking over merges every host's state first", b.st["alias"])
        b.save_state(); assert json.loads(host_state(MACH).read_text(encoding="utf-8"))["alias"]["new"] == "pc", "and the Mac's own copy carries it on"
    finally: bridge_mod.hostname = real_host; b.cfg["hosts"] = real_hosts; b.cfg.pop("lease_msg", None)

def stale_mac_serving(th, lm):
    """The Mac served for 38 h while the PC was off; the PC is back but Syncthing has not delivered the Mac's files yet."""
    now = CLOCK.time()
    fleet_mod._write(host_state(MACH), {"v": 2, "threads": {}, "q": {}, "alias": {"mac": "38 h old"}, "saved": now - 38 * 3600})
    bridge_mod.save(f"lease-{MACH}.json", {"host": MACH, "at": now - 38 * 3600, "state": now - 38 * 3600})
def mac_renews(th, lm): fake.channels[th]["msgs"][lm]["content"] = f"{MACH}|{CLOCK.time() - 5:.0f}"; b.active_at = 0

def test_takeover_primary_back_from_offline_waits_for_the_peer_state():
    real_host, real_hosts = bridge_mod.hostname, b.cfg.get("hosts")
    th, lm = lock_thread(holder=MACH, ago=5)
    try:
        stale_mac_serving(th, lm); as_host(PCH); n0 = waits_logged()
        assert not b.is_active() and lock_text(th, lm).startswith(MACH + "|"), "the Mac's state has not synced: the PC does not take the lock back yet"
        for _ in range(4): CLOCK.offset += 30; mac_renews(th, lm); assert not b.is_active()
        assert waits_logged() - n0 == 1, "one log line while it waits"
        t = CLOCK.time(); fleet_mod._write(host_state(MACH), {"v": 2, "threads": {}, "q": {}, "alias": {"mac": "fresh"}, "saved": t - 10})
        mac_renews(th, lm); assert not b.is_active(), "the state alone is not enough while the lease file is 38 h old"
        bridge_mod.save(f"lease-{MACH}.json", {"host": MACH, "at": t - 5, "state": t - 10})  # Syncthing caught up
        mac_renews(th, lm); assert b.is_active() and lock_text(th, lm).startswith(PCH + "|"), "caught up: the primary takes the lock back"
        assert b.st["alias"].get("mac") == "fresh", ("and it starts from the Mac's newest state", b.st["alias"])
    finally: bridge_mod.hostname = real_host; b.cfg["hosts"] = real_hosts; b.cfg.pop("lease_msg", None)

def test_takeover_an_old_format_lease_or_a_missing_state_file_waits():
    """Review finding 1: a lease file without "state" read as state 0, so the primary took the bridge back with stale state."""
    real_host, real_hosts = bridge_mod.hostname, b.cfg.get("hosts")
    th, lm = lock_thread(holder=MACH, ago=5)
    try:
        as_host(PCH); n0 = waits_logged(); now = CLOCK.time()
        bridge_mod.save(f"lease-{MACH}.json", {"host": MACH, "at": now - 5})  # a fresh lease from a bridge before 15 Nov; no state file for the Mac
        assert not b.is_active() and lock_text(th, lm).startswith(MACH + "|"), "an old-format lease is not proof of anything: wait"
        fleet_mod._write(host_state(MACH), {"v": 2, "threads": {}, "q": {}, "saved": now - 10}); mac_renews(th, lm)
        assert not b.is_active(), "still no state named in the lease"
        host_state(MACH).unlink(); bridge_mod.save(f"lease-{MACH}.json", {"host": MACH, "at": CLOCK.time() - 5, "state": 0}); mac_renews(th, lm)
        assert not b.is_active(), "the Mac is serving (a fresh lease), so its state file must exist before the PC takes over"
        assert waits_logged() - n0 == 1, "one log line while it waits"
        fleet_mod._write(host_state(MACH), {"v": 2, "threads": {}, "q": {}, "saved": CLOCK.time() - 10}); mac_renews(th, lm)
        assert b.is_active() and lock_text(th, lm).startswith(PCH + "|"), "state file there and named: the primary takes the lock back"
        host_state(MACH).unlink(); bridge_mod.save(f"lease-{MACH}.json", {"host": MACH, "at": CLOCK.time() - 5}); fake.channels[th]["msgs"][lm]["content"] = f"{MACH}|{CLOCK.time() - 5:.0f}"; b.active_at = 0
        assert not b.is_active(); CLOCK.offset += bridge_mod.RECLAIM_WAIT + 1; mac_renews(th, lm)
        assert b.is_active(), "capped: after RECLAIM_WAIT the primary serves anyway"
    finally: bridge_mod.hostname = real_host; b.cfg["hosts"] = real_hosts; b.cfg.pop("lease_msg", None)

def test_takeover_primary_reclaim_wait_is_capped_at_10_minutes():
    real_host, real_hosts = bridge_mod.hostname, b.cfg.get("hosts")
    th, lm = lock_thread(holder=MACH, ago=5)
    try:
        stale_mac_serving(th, lm); as_host(PCH); n0 = waits_logged(); assert not b.is_active()
        CLOCK.offset += bridge_mod.RECLAIM_WAIT - 30; mac_renews(th, lm); assert not b.is_active(), "still inside the 10 minutes"
        CLOCK.offset += 31; mac_renews(th, lm); assert b.is_active() and lock_text(th, lm).startswith(PCH + "|"), "Syncthing never caught up: after 10 minutes the primary serves anyway"
        assert waits_logged() - n0 == 1
        stale_mac_serving(th, lm); mac_renews(th, lm)  # the Mac served again and the PC is back again
        assert not b.is_active(), "a later return waits again: the cap does not carry over"
    finally: bridge_mod.hostname = real_host; b.cfg["hosts"] = real_hosts; b.cfg.pop("lease_msg", None)

def asks_doc():
    f = tmp / f"Queue/asks/open.{bridge_mod.hostname()}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
def all_cards(): return [m for c in fake.channels.values() for m in c["msgs"].values()]
def asked_card(key):
    """The card of question KEY, found by the message id the bridge recorded (its buttons are gone once it closes)."""
    mid = (b.st["q"].get(key) or {}).get("msg") or CARD_IDS.get(key)
    return next(m for m in all_cards() if m["id"] == mid)
CARD_IDS = {}
def remember(key): CARD_IDS[key] = b.st["q"][key]["msg"]; return key

def test_open_questions_are_published_for_sessions_and_closes_carry_how():
    origin, mid = request()
    q = question(mid, body="Merge PR 6?", options=("Merge", "Not now")); tick(); sweep_tick()
    d = asks_doc(); row = next(r for r in d["open"] if r["id"] == q["id"])
    assert row["options"] == ["Merge", "Not now"] and row["from"] == "orchestrator" and row["link"].startswith("https://discord.com/channels/" + GUILD) and row["tid"]
    press(PARENT, q["id"], 0); tick(); sweep_tick()
    d = asks_doc(); assert not any(r["id"] == q["id"] for r in d["open"])
    c = next(x for x in d["closed"] if x["id"] == q["id"]); assert c["how"] == "answered on Discord: Merge", c
    pq = agent_mod.send("sessions", "owner", "Permission: Bash in x on y", "Answer within 8 minutes.", options=[{"label": "Allow once"}, {"label": "Deny"}]); tick(); sweep_tick()
    assert not any(r["id"] == pq["id"] for r in asks_doc()["open"]), "a permission prompt is the session's own, not listed"
    CLOCK.offset += bridge_mod.ASKS_CLOSED_KEEP + 60; sweep_tick()
    assert not any(x["id"] == q["id"] for x in asks_doc()["closed"]), "closes are kept two days"

def test_answer_relayed_from_a_session_closes_the_card_and_reaches_the_asker():
    origin, mid = request()
    q = question(mid, body="Deploy the fix?", options=("Deploy it", "Not yet")); tick(); remember(q["id"])
    before = len(inbox("orchestrator"))
    agent_mod.send("orchestrator", "owner", f"[relay] {q['id']}", "x", check_repo=False, control={"target": q["id"], "op": "relay", "option": 0, "words": "deploy it", "where": "x"}); tick()
    assert q["id"] in b.st["q"], "an automatic agent never answers for him"
    agent_mod.send("mac", "owner", f"[relay] {q['id']}", "Deploy it", check_repo=False, control={"target": q["id"], "op": "relay", "option": 0, "text": None, "words": "yes deploy it", "where": "a claude session on the Mac (Orbit)"}); tick()
    assert q["id"] not in b.st["q"]
    got = [m for m in inbox("orchestrator")[before:] if m.get("reply_to") == q["id"]]
    assert got and got[0]["from"] == "owner" and got[0]["body"].startswith("Deploy it") and "yes deploy it" in got[0]["body"], got
    card = asked_card(q["id"]); assert "**Answer:** Deploy it" in card["content"] and "Answered in a claude session on the Mac (Orbit)" in card["content"] and not card["components"]
    sweep_tick(); c = next(x for x in asks_doc()["closed"] if x["id"] == q["id"]); assert c["how"].startswith("answered in a claude session on the Mac"), c
    # a free answer that is not a button
    q2 = question(mid, body="Which colour?", options=("Red", "Blue")); tick()
    agent_mod.send("mac", "owner", f"[relay] {q2['id']}", "green", check_repo=False, control={"target": q2["id"], "op": "relay", "option": None, "words": "green like the logo", "where": "a claude session on the Mac"}); tick()
    assert q2["id"] not in b.st["q"] and any(m["body"].startswith("green like the logo") for m in inbox("orchestrator") if m.get("reply_to") == q2["id"]), "his words, as he wrote them"
    # an answer to wording he saw before the agent changed the question is refused
    q3 = question(mid, body="Publish v3?", options=("Publish", "Wait")); tick()
    agent_mod.amend("orchestrator", q3["id"], "Publish v4?"); tick()
    agent_mod.send("mac", "owner", f"[relay] {q3['id']}", "publish", check_repo=False, control={"target": q3["id"], "op": "relay", "option": 0, "words": "publish", "where": "x", "rev": 0}); tick()
    assert q3["id"] in b.st["q"] and b.st["q"][q3["id"]]["rev"] == 1
    # the same relay read twice (a crash before it was acked) answers once
    relay = {"target": q3["id"], "op": "relay", "option": 0, "words": "publish", "where": "x", "rev": 1}
    agent_mod.send("mac", "owner", f"[relay] {q3['id']}", "publish", check_repo=False, control=relay)
    tick(); assert q3["id"] not in b.st["q"]
    b.st["q"][q3["id"]] = {**b.st["q_closed"][q3["id"]], "id": q3["id"], "from": "orchestrator", "options": [{"label": "Publish"}, {"label": "Wait"}], "rev": 1, "at": CLOCK.time() - 5, "thread": None}
    agent_mod.send("mac", "owner", f"[relay] {q3['id']}", "publish", check_repo=False, control=relay); tick()
    assert sum(1 for m in inbox("orchestrator") if m.get("reply_to") == q3["id"]) == 1, "never two answers to one question"

def test_answer_from_a_session_still_counts_after_the_card_closed_on_his_activity():
    """His session looked busy with the question, so the card closed as settled; his answer from there, arriving a moment
    later, still reaches the agent that asked."""
    agent_mod.register("pc", PC, "codex", auto=False)
    mq = agent_mod.send("mac", "owner", "Unlock it?", "Unlock it?", options=[{"label": "Unlocked"}, {"label": "Later"}]); tick(); remember(mq["id"])
    (tmp / "Queue/mail/origins").mkdir(parents=True, exist_ok=True)
    (tmp / f"Queue/mail/origins/{mq['id']}.json").write_text(json.dumps({"host": MAC, "cwd": "/x/Nova", "root": "/x/Nova"}), encoding="utf-8")
    sd = tmp / f"Queue/sessions/{MAC}"; sd.mkdir(parents=True, exist_ok=True)
    (sd / "s9.json").write_text(json.dumps({"session_id": "s9", "host": MAC, "tool": "claude", "project": "Nova", "cwd": "/x/Nova", "root": "/x/Nova", "state": "idle", "last_prompt": "later", "prompt_at": CLOCK.time() + 30, "updated": CLOCK.time() + 30}), encoding="utf-8")
    b.last_elsewhere = 0; sweep_tick(); assert mq["id"] not in b.st["q"]
    agent_mod.send("pc", "owner", f"[relay] {mq['id']}", "later", check_repo=False, control={"target": mq["id"], "op": "relay", "option": 1, "words": "later", "where": "a codex session on the PC"}); tick()
    assert any(m.get("reply_to") == mq["id"] and m["body"].startswith("Later") for m in inbox("mac"))
    assert b.st["q_closed"][mq["id"]]["how"] == "answered in a codex session on the PC: Later" and "late" not in b.st["q_closed"][mq["id"]]
    agent_mod.send("pc", "owner", f"[relay] {mq['id']}", "unlocked", check_repo=False, control={"target": mq["id"], "op": "relay", "option": 0, "words": "unlocked", "where": "x"}); tick()
    assert sum(1 for m in inbox("mac") if m.get("reply_to") == mq["id"]) == 1, "only the first late answer counts"

def test_retired_thread_stays_closed():
    o1, m1 = request("Kepler: merge the bikes PR")
    old = agent_mod.send("orchestrator", "owner", "Kepler: merge PR 6?", "Tap Merge to ship PR 6.", reply_to=m1, repo="Kepler", check_repo=False, options=[{"label": "Merge"}, {"label": "Not now"}]); tick()
    o2, m2 = request("Kepler: fences")
    agent_mod.send("orchestrator", "owner", "Kepler: merge PR 7?", "PR 7 holds PR 6.\n\nREPLACES: " + old["id"], reply_to=m2, repo="Kepler", check_repo=False, options=[{"label": "Merge"}, {"label": "Not now"}]); tick()
    for m in list(inbox("orchestrator")): agent_mod.ack("orchestrator", m["id"])
    t1 = b.st["threads"][b.resolve(m1)]
    CLOCK.offset += bridge_mod.RETIRE_AFTER + 5; b.last_clean = 0; sweep_tick()
    assert t1.get("archived") and t1.get("closed") and not t1.get("retire")
    b.last_clean = 0; sweep_tick(); assert fake.channels[t1["dthread"]].get("archived"), "keep-visible does not bring it back"

def test_settle_question_from_a_session_or_the_orchestrator():
    origin, mid = request()
    q = question(mid, body="Publish v3?", options=("Publish", "Wait")); tick(); remember(q["id"])
    agent_mod.send("builder", "owner", f"[settle] {q['id']}", "not mine to close", check_repo=False, control={"target": q["id"], "op": "settle"}); tick()
    assert q["id"] in b.st["q"], "another automatic agent may not close his question"
    agent_mod.register("janitor", "any", "claude", auto=False)
    agent_mod.send("janitor", "owner", f"[settle] {q['id']}", "machine mailbox", check_repo=False, control={"target": q["id"], "op": "settle"}); tick()
    assert q["id"] in b.st["q"], "a machine mailbox that is not one of his sessions may not close it either"
    agent_mod.send("mac", "owner", f"[settle] {q['id']}", "published v3 in this session", check_repo=False, control={"target": q["id"], "op": "settle"}); tick()
    assert q["id"] not in b.st["q"]
    card = asked_card(q["id"]); assert "Settled elsewhere:** published v3 in this session (mac)" in card["content"] and not card["components"]
    q2 = question(mid, body="Old plan?", options=("A", "B")); tick()
    agent_mod.send("orchestrator", "owner", f"[settle] {q2['id']}", "the newer plan covers it", check_repo=False, control={"target": q2["id"], "op": "settle"}); tick()
    assert q2["id"] not in b.st["q"]

def test_replaces_closes_the_older_question_and_retires_its_thread():
    o1, m1 = request("Kepler: merge the bikes PR")
    old = agent_mod.send("orchestrator", "owner", "Kepler: merge PR 6?", "Tap Merge to ship PR 6.", reply_to=m1, repo="Kepler", check_repo=False, options=[{"label": "Merge"}, {"label": "Not now"}]); tick(); remember(old["id"])
    o2, m2 = request("Kepler: fences")
    new = agent_mod.send("orchestrator", "owner", "Kepler: merge PR 7?", "Tap Merge to ship PR 7, which also holds PR 6.\n\nREPLACES: " + old["id"], reply_to=m2, repo="Kepler", check_repo=False, options=[{"label": "Merge"}, {"label": "Not now"}]); tick()
    assert old["id"] not in b.st["q"] and new["id"] in b.st["q"]
    nq = b.st["q"][new["id"]]; assert "REPLACES" not in nq["body"] and "REPLACES" not in asked_card(new["id"])["content"]
    oc = asked_card(old["id"]); assert "Replaced by the newer question" in oc["content"] and not oc["components"]
    t1 = b.st["threads"][b.resolve(m1)]; assert t1.get("retire")
    th1 = t1["dthread"]; assert not fake.channels[th1].get("archived")
    for m in list(inbox("orchestrator")): agent_mod.ack("orchestrator", m["id"])  # nothing left running in the old thread
    CLOCK.offset += bridge_mod.RETIRE_AFTER + 5; b.last_clean = 0; sweep_tick()
    assert fake.channels[th1].get("archived"), "the old thread closes once its only question moved"
    side = (tmp / f"Queue/discord/sidechat/{b.resolve(m1)}.jsonl").read_text(encoding="utf-8")
    assert new["id"] in side, "the old thread's orchestrator learns where its question went"
    # replaces naming a question that is newer, or unknown, changes nothing
    later = agent_mod.send("orchestrator", "owner", "Kepler: x?", "x?\n\nREPLACES: 20990101T000000Z-deadbeef", reply_to=m2, repo="Kepler", check_repo=False, options=[{"label": "A"}, {"label": "B"}]); tick()
    assert later["id"] in b.st["q"] and new["id"] in b.st["q"]

def test_supersede_pass_closes_older_questions_a_newer_one_covers():
    spawned = []
    def fake_spawn(path):
        job = json.loads(Path(path).read_text(encoding="utf-8")); spawned.append(job)
        ids = [x["id"] for x in job["questions"]]; old, new = order[0], order[-1]  # by when each was asked, not by id: two ids from one second sort at random
        Path(path).with_name(Path(path).stem + ".out.json").write_text(json.dumps({"replaced": [{"old": old, "by": new, "why": "PR 7 holds PR 6"}, {"old": new, "by": old, "why": "backwards"}] if old in ids and new in ids else []}), encoding="utf-8")
    order = []
    b.triage_spawn = fake_spawn
    o1, m1 = request("Atlas.app: merge")
    a = agent_mod.send("orchestrator", "owner", "Atlas.app: merge PR 6?", "Tap Merge to ship PR 6 with the login fix.", reply_to=m1, repo="Atlas.app", check_repo=False, options=[{"label": "Merge"}, {"label": "Not now"}]); tick(); remember(a["id"]); order.append(a["id"])
    b.last_supersede = 0; sweep_tick(); assert not spawned, "one question alone is not checked"
    CLOCK.offset += 2
    o2, m2 = request("Atlas.app: backend")
    c = agent_mod.send("orchestrator", "owner", "Atlas.app: merge PR 7?", "PR 7 adds checkout on top of everything in PR 6. Merge it?", reply_to=m2, repo="atlas.app", check_repo=False, options=[{"label": "Merge PR 7"}, {"label": "Later"}]); tick()
    assert a["id"] in b.st["q"], "different buttons: not the same question to the old rule"
    order.append(c["id"]); b.last_q = CLOCK.time() + 10 ** 6  # nothing below runs a pass before the test asks for one
    b.last_supersede = 0; sweep_tick(); assert len(spawned) == 1 and spawned[0]["kind"] == "supersede" and len(spawned[0]["questions"]) == 2
    b.last_supersede = 0; sweep_tick()
    assert not any("backwards" in str(x) for x in b.st["q_closed"].values()), "a newer question is never closed by an older one"
    assert a["id"] not in b.st["q"] and c["id"] in b.st["q"], "only the older one closes, never the newer"
    assert "PR 7 holds PR 6" in asked_card(a["id"])["content"]
    b.last_supersede = 0; sweep_tick(); assert len(spawned) == 1, "a project with one question left is not asked again"
    # a verdict about questions that changed while it ran is dropped
    o3, m3 = request("Atlas.app: third")
    e = agent_mod.send("orchestrator", "owner", "Atlas.app: icons?", "Which icon set?", reply_to=m3, repo="Atlas.app", check_repo=False, options=[{"label": "Line"}, {"label": "Solid"}]); tick()
    b.last_supersede = 0; sweep_tick(); assert len(spawned) == 2
    agent_mod.amend("orchestrator", c["id"], "PR 7 is now only the icon change. Merge it?"); tick()
    b.last_supersede = 0; sweep_tick(); assert c["id"] in b.st["q"] and e["id"] in b.st["q"], "stale verdict dropped"

def test_question_in_a_closed_thread_settles_after_a_while():
    origin, mid = request()
    q = question(mid, body="Still?", options=("Yes", "No")); tick(); remember(q["id"])
    t = b.st["threads"][b.resolve(mid)]; t["archived"] = True
    b.last_elsewhere = 0; sweep_tick(); assert q["id"] in b.st["q"]
    CLOCK.offset += bridge_mod.ARCHIVED_Q_AFTER + 60; b.last_elsewhere = 0; sweep_tick()
    assert q["id"] not in b.st["q"] and "its thread was closed" in asked_card(q["id"])["content"]
    # an old question whose thread was closed only now waits the full time from then
    q2 = question(mid, body="Again?", options=("Yes", "No")); tick(); t["archived"] = False
    CLOCK.offset += bridge_mod.ARCHIVED_Q_AFTER + 60; b.last_elsewhere = 0; sweep_tick(); assert q2["id"] in b.st["q"]
    t["archived"] = True; b.last_elsewhere = 0; sweep_tick(); assert q2["id"] in b.st["q"], "closed seconds ago: not settled yet"

def test_settle_check_waits_before_judging_the_same_question_again():
    verdict = {"done": False, "why": "unrelated", "session": ""}; spawned = []
    def fake_spawn(path):
        spawned.append(1); Path(path).with_name(Path(path).stem + ".out.json").write_text(json.dumps(verdict), encoding="utf-8")
    b.triage_spawn = fake_spawn
    gq = agent_mod.send("orchestrator", "owner", "Kepler: fences?", "How to stop bikes going through fences?", repo="Kepler", options=[{"label": "Colliders"}, {"label": "Leave"}], check_repo=False); tick()
    sd = tmp / f"Queue/sessions/{MAC}"; sd.mkdir(parents=True, exist_ok=True)
    def work(n): (sd / "s1.json").write_text(json.dumps({"session_id": "s1", "host": MAC, "tool": "claude", "project": "Kepler", "state": "idle", "last_prompt": f"bug {n}", "prompt_at": CLOCK.time() + 20, "updated": CLOCK.time() + 30, "started": CLOCK.time()}), encoding="utf-8")
    def look(): b.last_elsewhere = 0; sweep_tick()
    work(1); look(); look(); assert len(spawned) == 1
    for n in range(2, 6): CLOCK.offset += 120; work(n); look(); look()
    assert len(spawned) == 1, "new turns on other bugs within 20 minutes do not ask Gemini again"
    CLOCK.offset += bridge_mod.SETTLE_RECHECK; work(9); look(); look(); assert len(spawned) == 2


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and want in n]
    setup(); failed, h0 = 0, bridge_mod.code_hash(HERE); teardown()  # the code under test, before any test runs
    for name, fn in tests:
        setup()
        try: fn(); print("PASS", name)
        except BaseException:
            failed += 1; print("FAIL", name); traceback.print_exc()
        finally: teardown()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    mark_tested(h0, want, failed, len(tests))
    sys.exit(1 if failed else 0)

def mark_tested(h0, want, failed, n):
    """After a full pass of unchanged code, BRIDGE_TESTED_DIR (the live Queue/discord at deploy time) gets tested-<hash>.ok:
    the running bridges reload only onto code that has one."""
    out = os.environ.get("BRIDGE_TESTED_DIR")
    h1 = bridge_mod.code_hash(HERE) if bridge_mod else None
    if want or failed or h0 != h1: return print("no tested marker: " + ("a filtered run" if want else f"{failed} failed" if failed else "the code changed during the run"))
    if not out: return print(f"code {h0} passed; set BRIDGE_TESTED_DIR to write tested-{h0}.ok")
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / f"tested-{h0}.ok").write_text(json.dumps({"code": h0, "tests": n, "at": real_time.time(), "host": bridge_mod.hostname()}), encoding="utf-8")
    print(f"wrote {Path(out) / f'tested-{h0}.ok'}")

if __name__ == "__main__": main()
