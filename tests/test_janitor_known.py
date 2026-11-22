import json, time
from _load import load
jan = load("janitor", "discord-janitor.py")
HEAD = "THE CRITIC'S VERDICTS FROM THE LAST 48 H"

def setup(tmp_path, monkeypatch, rows=None, raw=None):
    monkeypatch.setattr(jan.br, "dpath", lambda n: tmp_path / n)
    (tmp_path / "janitor-issues.jsonl").write_text(json.dumps({"issue": "an issue"}) + "\n", encoding="utf-8")
    if raw is not None: (tmp_path / "janitor-critic.jsonl").write_text(raw, encoding="utf-8")
    elif rows is not None: (tmp_path / "janitor-critic.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

def test_window_and_filter(tmp_path, monkeypatch):
    now = time.time()
    setup(tmp_path, monkeypatch, [
        {"at": now - 50 * 3600, "verdict": "1. REJECTED old one"},
        {"at": now - 3600, "verdict": "Status: built and live\n2. REJECTED new one\nI rejected 3 of them\n3. AGREED fixed it"},
        {"verdict": "9. REJECTED no timestamp"}])
    t = jan.known()
    assert "REJECTED new one" in t and "AGREED fixed it" in t
    assert "old one" not in t and "no timestamp" not in t
    assert "built and live" not in t and "I rejected 3" not in t

def test_cap(tmp_path, monkeypatch):
    now = time.time()
    setup(tmp_path, monkeypatch, [{"at": now - 200 + i, "verdict": f"{i:03d} REJECTED " + "x" * 280} for i in range(100)])
    t = jan.known()
    sec = t.split(HEAD)[1].split("\n", 1)[1]
    assert len(sec) <= jan.CRITIC_KEEP_CHARS
    assert "099 REJECTED" in sec and "000 REJECTED" not in sec

def test_missing_and_malformed(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    assert HEAD not in jan.known()
    setup(tmp_path, monkeypatch, raw="{bad json\n" + json.dumps({"at": time.time(), "verdict": "1. AGREED ok"}) + "\n")
    assert "1. AGREED ok" in jan.known()

def test_message_flags():
    f = jan.message_flags
    assert f("1", True, 0, set(), {"1"}) == "Q" and f("1", True, 0, set(), set()) == "B" and f("1", False, 0, set(), {"1"}) == ""
    assert f("1", True, 0, {"1"}, {"1"}) == "QT" and f("1", False, 7, set(), set()) == "N"

def test_msg_extras():
    assert jan.msg_extras({}) == ""
    r = [{"emoji": {"name": "✅"}, "me": True}, {"emoji": {"name": "👍"}, "me": False}]
    assert jan.msg_extras({"reactions": r, "thread": {"id": "1", "thread_metadata": {"archived": True}}}) == "[✅ thread archived] "
    assert jan.msg_extras({"thread": {"id": "1", "thread_metadata": {"archived": False}}}) == "[thread open] "
    assert "never report a card, thread or message as missing" in jan.RULES
