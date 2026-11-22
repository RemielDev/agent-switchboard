from _load import load
hk = load("hk", "housekeeper.py")

def P(name, mb=100, pid=1, born=0, session=1): return {"name": name + ".exe", "mb": mb, "pid": pid, "ppid": 0, "born": born, "cmd": "", "session": session}

def test_candidates_offer_everything_but_infrastructure():
    d = {"procs": [P("Mailer", 300, 1), P("Mailer", 50, 2), P("svchost", 900, 4), P("python", 200, 5),
                   P("MSPCManagerService", 80, 6, session=0), P("Dictate", 20, 7), P("chrome", 500, 8)],
         "titles": {"7": "Status"}}
    tabs = [{"key": "9|Flow", "title": "Flow", "sel": False, "group": "SAGE", "mb": 550}, {"key": "9|(3) Discord", "title": "(3) Discord", "sel": True, "mb": 500}]
    c = hk.candidates(d, tabs, {}, 600)
    assert set(c) == {"app:Mailer", "app:Dictate", "tab:9|Flow", "tab:9|(3) Discord"}  # grouped and Discord tabs are offered too
    assert c["app:Mailer"]["mb"] == 350 and c["app:Dictate"]["windows"] == ["Status"] and c["tab:9|Flow"]["group"] == "SAGE"

def test_tab_unseen_clock():
    st, tabs = {}, [{"key": "1|Docs", "title": "Docs", "sel": False}]
    hk.candidates({"procs": [], "titles": {}}, tabs, st, 0)
    assert hk.candidates({"procs": [], "titles": {}}, tabs, st, 7200)["tab:1|Docs"]["unseen_min"] == 120
    tabs[0]["sel"] = True
    assert hk.candidates({"procs": [], "titles": {}}, tabs, st, 7300)["tab:1|Docs"]["unseen_min"] == 0

def test_due():
    cfg = {"judge_every_min": 10, "pressure_free_gb": 6}
    assert hk.due({"judged": 0}, 500, 10, cfg, False) is False and hk.due({"judged": 0}, 601, 10, cfg, False)
    assert hk.due({"judged": 0}, 200, 4, cfg, False)  # low memory: every 3 minutes
    assert hk.due({"judged": 1000}, 1001, 10, cfg, True)

def test_carry_out_dry_ignores_unknown_ids():
    cands = {"app:Mailer": {"mb": 350}, "app:Dictate": {"mb": 3000}}
    acts = []
    freed = hk.carry_out({"close": [{"id": "app:Mailer", "why": "not used"}, {"id": "app:explorer", "why": "x"}, {"id": "app:Dictate", "why": "no Kepler work"}]}, cands, {}, True, 0, acts)
    assert freed == 3350 and len(acts) == 2 and acts[0].startswith("closed Mailer") and "Kepler" in acts[1]

def test_follow_through_forces_once_and_forgets():
    st, acts = {"asked": {"app:Mailer": 0, "app:Gone": 0}}, []
    procs = [P("Mailer", 300, 1)]
    hk.follow_through(procs, {}, st, True, 30, acts); assert acts == []  # grace
    hk.follow_through(procs, {}, st, True, 60, acts); assert acts == ["force-closed Mailer: it ignored the close"]
    assert "app:Gone" not in st["asked"]
    hk.follow_through(procs, {}, st, True, 300, acts); assert len(acts) == 1  # said once

def test_follow_through_forgets_what_exited():
    st, acts = {"asked": {"app:Mailer": 0}}, []
    hk.follow_through([], {}, st, True, 60, acts)
    assert st["asked"] == {} and acts == []
    hk.follow_through([P("Mailer")], {}, st, True, 120, acts); assert acts == []  # reopened later: left alone until judged again

def test_work_context_reads_the_mail_folders():
    assert hk.work_context(0).startswith("THREADS")
