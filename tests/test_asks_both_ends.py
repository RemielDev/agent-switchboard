"""Questions asked at both ends: session-hook.py shows the open questions in the owner's sessions and drops the settled ones,
and agent-mail-mcp.py answers one only with words the owner really typed, and makes an agent fold overlapping questions.

Run: python -m unittest tests/test_asks_both_ends.py (or pytest). Stdlib only; a temp dev root, nothing live is touched."""
import json, os, shutil, subprocess, sys, tempfile, time, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "switchboard"
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load  # noqa: E402  (tests/_load.py)


class AsksCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asks-test-")).resolve()
        self.old_root = os.environ.get("SWITCHBOARD_ROOT"); os.environ["SWITCHBOARD_ROOT"] = str(self.tmp)
        for k in [k for k in os.environ if k.startswith("AGENT_MAIL_")]: os.environ.pop(k)
        import common; self.host = common.hostname()
        for g in ("Kepler", "Nova", "Atlas.app"): (self.tmp / g).mkdir()
        self.q = self.tmp / "Queue"; (self.q / "asks").mkdir(parents=True); (self.q / "discord").mkdir(parents=True)
        self.mail = load("agent_mail_asks", "agent-mail.py"); sys.modules["agent_mail"] = self.mail
        for n, k, auto in (("owner", "human", False), ("orchestrator", "claude", True), ("mac", "claude", False)): self.mail.register(n, self.host if n == "mac" else "any", k, auto=auto)
        self.mcp = load("agent_mail_mcp_asks", "agent-mail-mcp.py")
        self.now = time.time()
        self.publish([{"id": "20261122T072334Z-8f0f7876", "from": "orchestrator", "project": "Atlas.app", "subject": "Merge PR 24?", "question": "Merge PR 24?", "options": ["Merge", "Not now"], "tid": "T1", "link": "https://discord.com/channels/1/2/3", "at": self.now - 3600},
                      {"id": "20261122T144508Z-8c75a924", "from": "orchestrator", "project": "Kepler", "subject": "Fences?", "question": "How to stop bikes?", "options": ["Colliders", "Leave"], "tid": "T2", "at": self.now - 60}])

    def tearDown(self):
        if self.old_root is None: os.environ.pop("SWITCHBOARD_ROOT", None)
        else: os.environ["SWITCHBOARD_ROOT"] = self.old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def publish(self, open_rows, closed=()):
        (self.q / "asks" / f"open.{self.host}.json").write_text(json.dumps({"host": self.host, "at": time.time(), "open": open_rows, "closed": list(closed)}), encoding="utf-8")

    def hook(self, prompt, sid="s1", cwd=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_MAIL_")}
        env.update(SWITCHBOARD_ROOT=str(self.tmp), CLAUDE_PROJECT_DIR=str(cwd or self.tmp / "Kepler"), SESSION_HOOK_ALL="1")
        r = subprocess.run([sys.executable, str(HERE / "session-hook.py")], input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": sid, "cwd": str(cwd or self.tmp / "Kepler"), "prompt": prompt}),
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout.strip() else ""

    def test_session_shows_new_questions_once_and_learns_when_one_is_settled(self):
        text = self.hook("fix the camera")
        self.assertIn("[20261122T144508Z-8c75a924] Kepler", text); self.assertIn("[20261122T072334Z-8f0f7876] Atlas.app", text)
        self.assertLess(text.index("Kepler"), text.index("Atlas.app"), "this session's project comes first")
        self.assertIn("answer_question", text); self.assertIn("0: Merge | 1: Not now", text)
        self.assertEqual(self.hook("and the lights"), "", "a question is shown once per session")
        self.kepler = {"id": "20261122T144508Z-8c75a924", "from": "orchestrator", "project": "Kepler", "subject": "Fences?", "question": "How to stop bikes?", "options": ["Colliders", "Leave"], "tid": "T2", "at": self.now - 60}
        self.publish([self.kepler], closed=[{"id": "20261122T072334Z-8f0f7876", "how": "answered on Discord: Merge", "at": time.time(), "subject": "Merge PR 24?"}])
        text = self.hook("next")
        self.assertIn("Settled elsewhere", text); self.assertIn("answered on Discord: Merge", text); self.assertNotIn("8c75a924] Kepler", text)
        self.assertEqual(self.hook("again"), "", "a settled one is said once")
        self.assertIn("8c75a924", self.hook("hi", sid="s2"), "another session shows it too")
        self.kepler["rev"] = 1; self.kepler["question"] = "How to stop bikes, now with ramps?"
        self.publish([self.kepler]); text = self.hook("more")
        self.assertIn("CHANGED", text); self.assertIn("ramps", text)

    def test_project_mode_and_off(self):
        (self.q / "discord" / "config.json").write_text(json.dumps({"asks_in_sessions": "project"}), encoding="utf-8")
        text = self.hook("hello"); self.assertIn("Kepler", text); self.assertNotIn("Atlas.app", text)
        (self.q / "discord" / "config.json").write_text(json.dumps({"asks_in_sessions": "off"}), encoding="utf-8")
        self.assertEqual(self.hook("hello", sid="s3"), "")

    def test_answer_question_needs_his_real_words(self):
        call = lambda name, **a: self.mcp.call_tool(self.mail, "mac", name, a)
        with self.assertRaises(self.mcp.ToolError): call("answer_question", id="20261122T072334Z-8f0f7876", option=0, words="yes merge it")
        self.hook("ok, yes merge it", cwd=self.tmp / "Atlas.app")
        r = call("answer_question", id="20261122T072334Z-8f0f7876", option=0, words="yes merge it")
        self.assertTrue(r["sent"]); self.assertEqual(r["answer"], "Merge")
        m = self.mail.list_inbox("owner")[-1]
        self.assertEqual(m["control"]["op"], "relay"); self.assertEqual(m["control"]["option"], 0); self.assertIn("Atlas.app", m["control"]["where"])
        with self.assertRaises(self.mcp.ToolError): self.mcp.call_tool(self.mail, "orchestrator", "answer_question", {"id": "20261122T072334Z-8f0f7876", "option": 0, "words": "yes merge it"})
        with self.assertRaises(self.mcp.ToolError): call("answer_question", id="20261122T072334Z-8f0f7876", option=5, words="yes merge it")

    def test_words_that_do_not_name_the_button_go_through_as_his_words(self):
        self.hook("not now, wait for the review", cwd=self.tmp / "Atlas.app")
        r = self.mcp.call_tool(self.mail, "mac", "answer_question", {"id": "20261122T072334Z-8f0f7876", "option": 0, "words": "not now, wait for the review"})
        self.assertEqual(r["answer"], "not now, wait for the review")
        c = self.mail.list_inbox("owner")[-1]["control"]; self.assertIsNone(c["option"]); self.assertEqual(c["words"], "not now, wait for the review")

    def test_only_the_question_dialog_counts_in_a_transcript(self):
        tr = self.tmp / "t.jsonl"; ts = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5))
        rows = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {}}]}},
                {"type": "user", "timestamp": ts(), "message": {"content": [{"type": "tool_result", "tool_use_id": "b1", "content": "User has answered your questions: Merge"}]}}]
        tr.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        sd = self.q / "sessions" / self.host; sd.mkdir(parents=True, exist_ok=True)
        (sd / "x.json").write_text(json.dumps({"session_id": "x", "host": self.host, "tool": "claude", "project": "Atlas.app", "updated": time.time() + 10, "transcript": str(tr)}), encoding="utf-8")
        call = lambda: self.mcp.call_tool(self.mail, "mac", "answer_question", {"id": "20261122T072334Z-8f0f7876", "option": 0, "words": "Merge"})
        with self.assertRaises(self.mcp.ToolError): call()  # a tool printed the phrase: not the owner's answer
        rows += [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": {}}]}},
                 {"type": "user", "timestamp": ts(), "message": {"content": [{"type": "tool_result", "tool_use_id": "q1", "content": [{"type": "text", "text": "User has answered your questions: \"Merge PR 24?\"=\"Merge\""}]}]}}]
        tr.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        self.assertTrue(call()["sent"])

    def test_the_button_must_be_in_his_quoted_words(self):
        self.hook("PR 24: wait. Merge PR 25 though", cwd=self.tmp / "Atlas.app")
        r = self.mcp.call_tool(self.mail, "mac", "answer_question", {"id": "20261122T072334Z-8f0f7876", "option": 0, "words": "wait"})
        self.assertEqual(r["answer"], "wait", "Merge is in the prompt, but not in the words that answer this question")

    def test_question_text_in_the_dialog_result_is_not_an_answer(self):
        tr = self.tmp / "t2.jsonl"; ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5))
        rows = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": {}}]}},
                {"type": "user", "timestamp": ts, "message": {"content": [{"type": "tool_result", "tool_use_id": "q1", "content": 'User has answered your questions: "Merge PR 24?"="Not now". You can now continue.'}]}}]
        tr.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        sd = self.q / "sessions" / self.host; sd.mkdir(parents=True, exist_ok=True)
        (sd / "y.json").write_text(json.dumps({"session_id": "y", "host": self.host, "tool": "claude", "updated": time.time() + 10, "transcript": str(tr)}), encoding="utf-8")
        with self.assertRaises(self.mcp.ToolError): self.mcp.call_tool(self.mail, "mac", "answer_question", {"id": "20261122T072334Z-8f0f7876", "option": 0, "words": "Merge"})
        r = self.mcp.call_tool(self.mail, "mac", "answer_question", {"id": "20261122T072334Z-8f0f7876", "option": 1, "words": "Not now"})
        self.assertEqual(r["answer"], "Not now")

    def test_late_answer_and_reworded_question(self):
        self.hook("merge it", cwd=self.tmp / "Atlas.app")
        self.publish([], closed=[{"id": "20261122T072334Z-8f0f7876", "how": "you went on with Atlas.app", "at": time.time(), "late_ok": True, "asked_at": self.now - 3600, "rev": 0, "options": ["Merge", "Not now"]}])
        r = self.mcp.call_tool(self.mail, "mac", "answer_question", {"id": "20261122T072334Z-8f0f7876", "option": 0, "words": "merge it"})
        self.assertTrue(r["sent"], "closed only because their session looked busy: their answer still goes")
        self.publish([], closed=[{"id": "20261122T072334Z-8f0f7876", "how": "answered on Discord: Merge", "at": time.time()}])
        self.assertFalse(self.mcp.call_tool(self.mail, "mac", "answer_question", {"id": "20261122T072334Z-8f0f7876", "option": 0, "words": "merge it"})["sent"])
        self.publish([dict(self.kepler if hasattr(self, "kepler") else {"id": "20261122T144508Z-8c75a924", "from": "orchestrator", "project": "Kepler", "subject": "Fences?", "question": "Thicker?", "options": ["Colliders", "Leave"], "at": self.now - 60}, rev=1, rev_at=time.time() + 60)])
        self.hook("colliders please")
        with self.assertRaises(self.mcp.ToolError): self.mcp.call_tool(self.mail, "mac", "answer_question", {"id": "20261122T144508Z-8c75a924", "option": 0, "words": "colliders please"})

    def test_old_prompts_do_not_count(self):
        self.publish([{"id": "20261122T999999Z-aaaaaaaa", "from": "orchestrator", "project": "Orbit", "subject": "Deploy?", "question": "Deploy?", "options": ["Deploy it", "Not yet"], "at": time.time() + 120}])
        self.hook("deploy it")  # typed before the question was asked
        with self.assertRaises(self.mcp.ToolError): self.mcp.call_tool(self.mail, "mac", "answer_question", {"id": "20261122T999999Z-aaaaaaaa", "option": 0, "words": "deploy it"})

    def test_ask_owner_shows_open_questions_on_the_project_first(self):
        call = lambda **a: self.mcp.call_tool(self.mail, "orchestrator", "ask_owner", a)
        r = call(question="Kepler: thicker fences or colliders?", options=["Thicker", "Colliders"], repo="Kepler")
        self.assertFalse(r["asked"]); self.assertEqual([x["id"] for x in r["open_on_same_project"]], ["20261122T144508Z-8c75a924"])
        self.assertEqual(self.mail.list_inbox("owner"), [], "nothing is sent before it decides")
        r = call(question="Kepler: thicker fences or colliders?", options=["Thicker", "Colliders"], repo="Kepler", replaces=["20261122T144508Z-8c75a924", "20261122T072334Z-8f0f7876"])
        self.assertTrue(r["asked"]); self.assertEqual(r["replaces"], ["20261122T144508Z-8c75a924"], "only ids open on this project are passed on")
        self.assertTrue(self.mail.list_inbox("owner")[-1]["body"].endswith("REPLACES: 20261122T144508Z-8c75a924"))
        r = call(question="Nova: ship it?", options=["Ship", "Wait"], repo="Nova")
        self.assertTrue(r["asked"], "no open question on that project: asked at once")

    def test_list_and_settle(self):
        r = self.mcp.call_tool(self.mail, "mac", "list_questions", {"repo": "kepler"})
        self.assertEqual([x["id"] for x in r["open"]], ["20261122T144508Z-8c75a924"])
        r = self.mcp.call_tool(self.mail, "mac", "settle_question", {"id": "20261122T144508Z-8c75a924", "how": "fixed the fences here"})
        self.assertTrue(r["sent"]); self.assertEqual(self.mail.list_inbox("owner")[-1]["control"], {"target": "20261122T144508Z-8c75a924", "op": "settle"})


if __name__ == "__main__": unittest.main()
