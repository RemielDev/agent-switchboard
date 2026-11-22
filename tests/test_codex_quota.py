import json, os, time
from _load import load
import fleet

def rollout(home, *limits):
    d = home / ".codex/sessions/2025/03/01"; d.mkdir(parents=True)
    rows = [json.dumps({"payload": {"rate_limits": {"primary": p, "secondary": s}}}) for p, s in limits]
    (d / "rollout-x.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")

def test_reads_weekly_and_five_hour_windows(tmp_path, monkeypatch):
    now = time.time()
    rollout(tmp_path, ({"used_percent": 50.0, "window_minutes": 300, "resets_at": int(now + 3600)}, {"used_percent": 10.0, "window_minutes": 10080, "resets_at": int(now + 86400)}),
            ({"used_percent": 92.0, "window_minutes": 300, "resets_at": int(now + 3600)}, {"used_percent": 39.0, "window_minutes": 10080, "resets_at": int(now + 86400)}),
            (None, None))
    monkeypatch.setattr(fleet.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(fleet, "codex_exe", lambda: "/x/codex")
    q = fleet.codex_quota()
    assert q["left"] == 61.0 and q["left_5h"] == 8.0, q
    assert fleet.codex_usable(q, {"codex_min_left": 30}) and not fleet.codex_usable({**q, "left_5h": 4.0}, {"codex_min_left": 30})
    assert not fleet.codex_usable({**q, "left": 20.0}, {"codex_min_left": 30}) and fleet.codex_usable({"left": 50.0}, {"codex_min_left": 30})

def test_no_weekly_reading_is_unknown(tmp_path, monkeypatch):
    rollout(tmp_path, ({"used_percent": 10.0, "window_minutes": 300, "resets_at": int(time.time() + 3600)}, None))
    monkeypatch.setattr(fleet.Path, "home", lambda: tmp_path); monkeypatch.setattr(fleet, "codex_exe", lambda: "/x/codex")
    assert fleet.codex_quota() is None

def test_codex_exe_finds_npm_global_outside_path(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet.Path, "home", lambda: tmp_path)
    import shutil; monkeypatch.setattr(shutil, "which", lambda n: None)
    if os.name == "nt":
        monkeypatch.setenv("APPDATA", str(tmp_path)); (tmp_path / "npm").mkdir(); (tmp_path / "npm/codex.cmd").write_text("")
        assert fleet.codex_exe() == str(tmp_path / "npm/codex.cmd")
    else:
        (tmp_path / ".npm-global/bin").mkdir(parents=True); (tmp_path / ".npm-global/bin/codex").write_text("")
        assert fleet.codex_exe() == str(tmp_path / ".npm-global/bin/codex")
    assert fleet.codex_exe() is not None
