"""Tests for claude-memory-loader.py: a temp vault and registry, hook JSON on stdin, as Claude Code runs it.

Run: python -X utf8 -m unittest tests.test_memory_loader (or pytest). Stdlib only. Nothing outside a temp folder is read
or written.

Every test runs in two layouts, on any machine. LoaderCase links ~/.switchboard/MEMORY.md to the vault-root MEMORY.md,
which reaches the session through that link, so the hook leaves it out. CopiedGlobalLayout is a device without link
rights (often Windows): that file is a copy, so the hook loads the vault-root MEMORY.md itself, ahead of the narrower scopes."""
import contextlib, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, types, unittest
from pathlib import Path

_TK = Path(__file__).resolve().parent.parent
LOADER = _TK / "switchboard/claude-memory-loader.py"
SLOTS = 8

DEVICE = "Device preamble.\n\n" + "".join(
    f"## {name}\n" + f"{name} line.\n" * reps + "\n" for name, reps in
    [("Build tools", 40), ("Computer use", 40), ("Terminal tools", 60), ("Project facts", 80)])
DEVICE += "## Fenced\n```\n## Computer use\nnot a heading inside a fence\n```\n"
AREA = "# Area rules\n\n## Computer use\nArea rule that must never be dropped.\n"
REPO = "# Repo rules\n\nRepo rule one.\n"


class LoaderCase(unittest.TestCase):
    LINK_GLOBAL = True  # the one platform difference: is BASE/MEMORY.md the vault-root file itself (a link) or a copy

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loader-test-")).resolve()  # registry roots are compared unresolved
        self.base, self.vault, self.dev = self.tmp / "base", self.tmp / "vault", self.tmp / "dev"
        for d in (self.base, self.vault / "_Local", self.vault / "Work/Repos/Repo", self.dev / "Repo/Source"): d.mkdir(parents=True)
        self.root = self.vault / "MEMORY.md"; self.root.write_text("Root rules.\n")
        self.device = self.vault / "_Local/Device.md"; self.device.write_text(DEVICE)
        self.area = self.vault / "Work/MEMORY.md"; self.area.write_text(AREA)
        self.repo = self.vault / "Work/Repos/Repo/MEMORY.md"; self.repo.write_text(REPO)
        if not self.LINK_GLOBAL: shutil.copy2(self.root, self.base / "MEMORY.md")
        else:
            try: os.symlink(self.root, self.base / "MEMORY.md")
            except OSError: shutil.rmtree(self.base); self.base = self.vault  # no link rights: BASE/MEMORY.md is still the root file
        self.assertEqual((self.base / "MEMORY.md").resolve() == self.root.resolve(), self.LINK_GLOBAL)
        # The MEMORY.md files the hook sends, in scope order: the vault root only where no link carries it.
        self.memory = [str(p.resolve()) for p in ([] if self.LINK_GLOBAL else [self.root]) + [self.area, self.repo]]
        (self.base / "registry.json").write_text(json.dumps({"vault": str(self.vault), "device_note": "_Local/Device.md", "scopes": [
            {"root": str(self.dev), "canonical": str(self.area)},
            {"root": str(self.dev / "Repo"), "canonical": str(self.repo)}]}))
        self.cwd = str(self.dev / "Repo")
        self.env = {k: v for k, v in os.environ.items() if k not in ("AGENT_MAIL_AGENT", "AGENT_MAIL_PARENT")}
        self.env["AI_MEMORY_BASE"] = str(self.base)

    def tearDown(self): shutil.rmtree(self.tmp, ignore_errors=True)

    def policy(self, data):
        (self.base / "runner-memory.json").write_text(data if isinstance(data, str) else json.dumps(data))

    def call(self, event, chunk, chunks=SLOTS, runner=None, loader=LOADER, stdin=None):
        env = dict(self.env, **({"AGENT_MAIL_AGENT": runner} if runner else {}))
        p = subprocess.run([sys.executable, "-X", "utf8", str(loader), "--chunk", str(chunk), "--chunks", str(chunks)],
                           input=stdin if stdin is not None else json.dumps(event), capture_output=True, text=True, env=env, cwd=self.cwd)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def fire(self, name, session="s1", chunks=SLOTS, runner=None, loader=LOADER, cwd=None):
        """Every slot of one hook event, as Claude Code runs them; returns the injected texts in slot order."""
        event = {"hook_event_name": name, "session_id": session, "cwd": cwd or self.cwd, "source": "startup", "prompt": "hi"}
        out = []
        for i in range(chunks):
            text = self.call(event, i, chunks, runner, loader)
            if text.strip():
                d = json.loads(text)["hookSpecificOutput"]
                self.assertEqual(d["hookEventName"], name)
                out.append(d["additionalContext"])
        return out

    def module(self, extra_env=None):
        """The loader imported fresh with this test's environment (BASE is read at import)."""
        old = dict(os.environ)
        os.environ.clear(); os.environ.update(self.env, **(extra_env or {}))
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        spec = importlib.util.spec_from_file_location("loader_under_test", LOADER)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        return mod

    def legacy_chunks(self):
        mod = self.module()
        return mod.context_chunks(mod.scope_files(Path(self.cwd)))

    @staticmethod
    def sources(texts): return [t.split("\nSource: ", 1)[1].split("\n", 1)[0] for t in texts]

    # SessionStart

    def test_session_start_sends_every_file_unchanged(self):
        got = self.fire("SessionStart")
        self.assertEqual(got, self.legacy_chunks())  # byte-identical to the previous chunking
        self.assertEqual(set(self.sources(got)), {str(self.device.resolve()), *self.memory})
        self.assertEqual("".join(got).count("Root rules."), 0 if self.LINK_GLOBAL else 1)  # never twice

    def test_without_a_device_note_only_memory_files_are_sent(self):
        reg = json.loads((self.base / "registry.json").read_text()); del reg["device_note"]
        (self.base / "registry.json").write_text(json.dumps(reg))
        got = self.fire("SessionStart")
        self.assertEqual(self.sources(got), self.memory)
        self.assertNotIn("Device preamble.", "".join(got))

    def test_session_start_slots_in_parallel(self):
        event = json.dumps({"hook_event_name": "SessionStart", "session_id": "par", "cwd": self.cwd})
        procs = [subprocess.Popen([sys.executable, str(LOADER), "--chunk", str(i), "--chunks", str(SLOTS)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True, env=self.env) for i in range(SLOTS)]
        outs = [p.communicate(event)[0] for p in procs]
        got = [json.loads(o)["hookSpecificOutput"]["additionalContext"] for o in outs if o.strip()]
        self.assertEqual(got, self.legacy_chunks())
        self.assertEqual(self.fire("UserPromptSubmit", "par"), [])

    # UserPromptSubmit

    def test_prompt_without_change_sends_nothing(self):
        self.fire("SessionStart")
        self.assertEqual(self.fire("UserPromptSubmit"), [])
        self.assertEqual(self.fire("UserPromptSubmit"), [])

    def test_prompt_after_repo_note_change_sends_only_that_note(self):
        self.fire("SessionStart")
        self.repo.write_text(REPO + "Repo rule two.\n")
        got = self.fire("UserPromptSubmit")
        self.assertEqual(self.sources(got), [str(self.repo.resolve())])
        self.assertIn("Repo rule two.", got[0])
        self.assertEqual(self.fire("UserPromptSubmit"), [])  # sent once

    def test_prompt_after_device_note_change_sends_only_the_device_note(self):
        self.fire("SessionStart")
        self.device.write_text(DEVICE + "## Added\nnew fact\n")
        got = self.fire("UserPromptSubmit")
        self.assertEqual(set(self.sources(got)), {str(self.device.resolve())})
        self.assertIn("new fact", got[-1])

    def test_first_event_a_prompt_sends_everything(self):
        self.assertEqual(self.fire("UserPromptSubmit", "late"), self.legacy_chunks())

    def test_compact_resends_everything(self):
        self.fire("SessionStart")
        self.assertEqual(self.fire("SessionStart"), self.legacy_chunks())

    def test_corrupt_state_file_resends_everything(self):
        self.fire("SessionStart")
        for f in (self.base / ".cache/claude-memory-loader").glob("*.json"): f.write_text("{not json")
        self.assertEqual(self.fire("UserPromptSubmit"), self.legacy_chunks())
        self.assertEqual(self.fire("UserPromptSubmit"), [])

    # Runner jobs

    POLICY = {"<dev>": {"Device.md": ["## Computer use", "Terminal tools", "## Not a real heading"]}}

    def runner_policy(self):
        self.policy({str(self.dev): self.POLICY["<dev>"]})

    def test_runner_job_skips_listed_device_sections_only(self):
        self.runner_policy()
        got = self.fire("SessionStart", runner="builder")
        text = "".join(got)
        self.assertNotIn("Computer use line.", text); self.assertNotIn("Terminal tools line.", text)
        self.assertIn("Build tools line.", text); self.assertIn("Project facts line.", text)
        self.assertIn("not a heading inside a fence", text)  # a fenced line is not a section start
        self.assertIn("are not sent these sections of this note: Computer use; Terminal tools. Read them in " + str(self.device.resolve()), text)
        self.assertIn(AREA, text); self.assertIn(REPO, text)  # same-named heading in a MEMORY.md stays
        self.assertEqual(self.fire("UserPromptSubmit", runner="builder"), [])

    def test_interactive_session_ignores_the_policy(self):
        self.runner_policy()
        self.assertEqual(self.fire("SessionStart"), self.legacy_chunks())

    def test_runner_without_policy_is_byte_identical(self):
        self.assertEqual(self.fire("SessionStart", runner="builder"), self.legacy_chunks())

    def test_policy_for_another_folder_does_not_apply(self):
        self.policy({str(self.tmp / "elsewhere"): self.POLICY["<dev>"]})
        self.assertEqual(self.fire("SessionStart", runner="builder"), self.legacy_chunks())

    def test_policy_cannot_filter_a_memory_file(self):
        self.policy({str(self.dev): {"MEMORY.md": ["## Computer use"], "Device.md": []}})
        self.assertEqual(self.fire("SessionStart", runner="builder"), self.legacy_chunks())

    def test_longest_policy_root_wins(self):
        self.policy({str(self.dev): {"Device.md": ["Computer use"]}, str(self.dev / "Repo"): {"Device.md": ["Build tools"]}})
        text = "".join(self.fire("SessionStart", runner="builder"))
        self.assertIn("Computer use line.", text); self.assertNotIn("Build tools line.", text)

    # Overflow

    def test_overflow_keeps_every_memory_file_and_names_what_was_left_out(self):
        self.device.write_text(DEVICE + "".join(f"## Big {i}\n" + "x" * 5990 + "\n" for i in range(10)))
        got = self.fire("SessionStart")
        self.assertEqual(len(got), SLOTS)
        self.assertIn(AREA, "".join(got)); self.assertIn(REPO, "".join(got))
        self.assertEqual(set(self.sources(got[:-1])) - {str(self.device.resolve())}, set(self.memory))
        self.assertTrue(got[-1].startswith("Canonical memory index."), got[-1][:80])
        self.assertIn(str(self.device.resolve()) + " from the section \"Big ", got[-1])
        self.assertEqual(self.sources(got[:-1]).count(str(self.device.resolve())), SLOTS - 1 - len(self.memory))
        self.assertEqual(self.fire("UserPromptSubmit"), [])

    def test_overflow_with_tiny_slot_count_drops_device_first(self):
        self.device.write_text(DEVICE + "## Big\n" + "x" * 6000 + "\n")
        for slots in sorted({3, len(self.memory) + 1}):  # too few slots for every MEMORY.md, and just enough
            with self.subTest(slots=slots):
                got = self.fire("SessionStart", session="tiny" + str(slots), chunks=slots)
                self.assertEqual(len(got), slots)
                # Vault root first, then the narrower scopes outer to inner; the device note is cut first.
                self.assertEqual(self.sources(got[:-1]), self.memory[:slots - 1])
                for p in self.memory[slots - 1:] + [str(self.device.resolve())]: self.assertIn(p + " from the start", got[-1])

    # Failure paths: always exit 0, and fall back to the previous logic with everything

    def test_bad_policy_falls_back_to_full_injection(self):
        self.policy("{broken")
        self.assertEqual(self.fire("SessionStart", runner="builder"), self.legacy_chunks())
        log = (self.base / ".cache/claude-memory-loader/errors.log").read_text()
        self.assertIn("fell back to the previous logic", log)

    def test_internal_error_falls_back_in_process(self):
        mod = self.module()
        mod.fit = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            sys.stdin = io.StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "x", "cwd": self.cwd}))
            try: code = mod.main(["--chunk", "0"])
            finally: sys.stdin = sys.__stdin__
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"], self.legacy_chunks()[0])

    def test_both_paths_failing_still_exits_zero_and_says_so(self):
        (self.base / "registry.json").write_text("{broken")
        start = self.fire("SessionStart")
        self.assertEqual(len(start), 1); self.assertIn("loader failed", start[0])
        self.assertEqual(self.fire("UserPromptSubmit"), [])

    def test_garbage_stdin_exits_zero_silently(self):
        self.assertEqual(self.call(None, 0, stdin="not json"), "")

    def test_windows_lock_path(self):
        fake = types.ModuleType("msvcrt"); calls = []
        fake.LK_LOCK = 1; fake.locking = lambda fd, mode, n: calls.append(mode)
        saved = {k: sys.modules.get(k) for k in ("fcntl", "msvcrt")}
        sys.modules["fcntl"] = None; sys.modules["msvcrt"] = fake
        try: mod = self.module()
        finally:
            for k, v in saved.items():
                if v is None: sys.modules.pop(k, None)
                else: sys.modules[k] = v
        self.assertIsNone(mod.fcntl)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mod.run({"hook_event_name": "SessionStart", "session_id": "w", "cwd": self.cwd}, 0, SLOTS)
        self.assertEqual(calls, [1])
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"], self.legacy_chunks()[0])

    # PreToolUse is unchanged

    def test_pre_tool_use_for_a_new_scope_still_injects(self):
        (self.dev / "Other").mkdir(); other = self.vault / "Work/Repos/Other/MEMORY.md"
        other.parent.mkdir(); other.write_text("Other repo rule.\n")
        reg = json.loads((self.base / "registry.json").read_text())
        reg["scopes"].append({"root": str(self.dev / "Other"), "canonical": str(other)})
        (self.base / "registry.json").write_text(json.dumps(reg))
        self.fire("SessionStart")
        event = {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": self.cwd, "tool_name": "Edit",
                 "tool_input": {"file_path": str(self.dev / "Other/a.py")}}
        d = json.loads(self.call(event, 0))["hookSpecificOutput"]
        self.assertIn("Other repo rule.", d["additionalContext"])
        self.assertEqual(d["permissionDecision"], "deny")
        self.assertEqual(self.call(event, 0), "")


class CopiedGlobalLayout(LoaderCase):
    """The Windows PC: no link rights, so ~/.switchboard/MEMORY.md is a copy and the hook loads the vault-root file."""
    LINK_GLOBAL = False


if __name__ == "__main__": unittest.main()
