import re

from _load import load

mr = load("mail_runner_plan_checks", "mail-runner.py")


def test_checkdir_runs_in_subfolder(tmp_path):
    (tmp_path / "sub").mkdir(); (tmp_path / "sub" / "ok.py").write_text("raise SystemExit(0)")
    out, n, ok, setup = mr.run_plan_checks(tmp_path, "CHECKDIR: sub\nCHECK: python ok.py")
    assert (n, ok) == (1, 1), out


def test_checkdir_outside_is_ignored(tmp_path):
    out, n, ok, setup = mr.run_plan_checks(tmp_path, "CHECKDIR: ../\nCHECK: python --version")
    assert "NOTE CHECKDIR" in out and "ignored" in out


def _ok(d):
    d.mkdir(parents=True); (d / "ok.py").write_text("raise SystemExit(0)")


def test_checkdir_repo_name_prefix(tmp_path):
    _ok(tmp_path / "Repo" / "sub")
    out, n, ok, setup = mr.run_plan_checks(tmp_path / "Repo", "CHECKDIR: Repo/sub\nCHECK: python ok.py")
    assert (n, ok) == (1, 1), out


def test_checkdir_under_dev_root(tmp_path):
    _ok(tmp_path / "Queue" / "refs"); (tmp_path / "Repo").mkdir()
    out, n, ok, setup = mr.run_plan_checks(tmp_path / "Repo", "CHECKDIR: Queue/refs\nCHECK: python ok.py", dev_root=tmp_path)
    assert (n, ok) == (1, 1) and "read as" in out, out


def test_checkdir_dev_root_cannot_escape(tmp_path):
    (tmp_path / "dev").mkdir(); (tmp_path / "outside").mkdir()
    out, n, ok, setup = mr.run_plan_checks(tmp_path / "dev", "CHECKDIR: ../outside\nCHECK: python --version", dev_root=tmp_path / "dev")
    assert "ignored" in out, out


def test_checkdir_dev_root_path_ignored_without_dev_root(tmp_path):
    _ok(tmp_path / "Queue" / "refs"); (tmp_path / "Repo").mkdir()
    out, n, ok, setup = mr.run_plan_checks(tmp_path / "Repo", "CHECKDIR: Queue/refs\nCHECK: python --version")
    assert "ignored" in out, out


def test_pytest_fallback(tmp_path):
    out, n, ok, setup = mr.run_plan_checks(tmp_path, "CHECK: pytest --version")
    assert (n, ok) == (1, 1), out


def test_folder_named_in_task_is_used(tmp_path):
    (tmp_path / "sub").mkdir(); (tmp_path / "sub" / "ok.py").write_text("raise SystemExit(0)")
    out, n, ok, setup = mr.run_plan_checks(tmp_path, f"Folder: {tmp_path / 'sub'}\nCHECK: python ok.py")
    assert (n, ok) == (1, 1) and "named in the task" in out, out


def test_relative_folder_named_in_task(tmp_path):
    (tmp_path / "sub").mkdir(); (tmp_path / "sub" / "ok.py").write_text("raise SystemExit(0)")
    out, n, ok, setup = mr.run_plan_checks(tmp_path, "Work in sub/ok.py only.\nCHECK: python ok.py")
    assert (n, ok) == (1, 1), out


def test_folder_outside_cwd_is_not_used(tmp_path):
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir(); (tmp_path / "b" / "ok.py").write_text("raise SystemExit(0)")
    out, n, ok, setup = mr.run_plan_checks(tmp_path / "a", f"Folder: {tmp_path / 'b'}\nCHECK: python ok.py")
    assert ok == 0 and "named in the task" not in out, out


def test_checkdir_wins_over_task_folder(tmp_path):
    (tmp_path / "x").mkdir(); (tmp_path / "y").mkdir()
    (tmp_path / "x" / "ok.py").write_text("raise SystemExit(0)"); (tmp_path / "y" / "ok.py").write_text("raise SystemExit(1)")
    out, n, ok, setup = mr.run_plan_checks(tmp_path, f"CHECKDIR: y\nFolder: {tmp_path / 'x'}\nCHECK: python ok.py")
    assert ok == 0, out


def test_default_timeout_is_300():
    import inspect
    assert inspect.signature(mr.run_plan_checks).parameters["timeout"].default == 300


def test_missing_script_is_setup_not_fail(tmp_path):
    out, n, ok, setup = mr.run_plan_checks(tmp_path, "CHECK: python nothere.py")
    assert (n, ok, setup) == (1, 0, 1) and "- SETUP" in out and "FAIL" not in out and "could not run" in out, out


def test_missing_script_the_task_names_stays_fail(tmp_path):
    out, n, ok, setup = mr.run_plan_checks(tmp_path, "Add tests/test_pool.py with one case for the empty pool.\nCHECK: python tests/test_pool.py")
    assert (n, ok, setup) == (1, 0, 0) and "- FAIL" in out and "task names it" in out, out


def test_script_in_changed_files_folder_is_found(tmp_path):
    (tmp_path / "pkg").mkdir(); (tmp_path / "pkg" / "t_ok.py").write_text("raise SystemExit(0)")
    out, n, ok, setup = mr.run_plan_checks(tmp_path, "CHECK: python t_ok.py", changed=["pkg/mod.py"])
    assert (n, ok, setup) == (1, 1, 0) and "a file this build changed" in out, out


def test_changed_folder_comes_before_task_folder(tmp_path):
    for d, code in (("a", 1), ("b", 0)):
        (tmp_path / d).mkdir(); (tmp_path / d / "t.py").write_text(f"raise SystemExit({code})")
    out, n, ok, setup = mr.run_plan_checks(tmp_path, f"Folder: {tmp_path / 'a'}\nCHECK: python t.py", changed=["b/x.py"])
    assert ok == 1, out


def test_timeout_is_setup(tmp_path):
    (tmp_path / "slow.py").write_text("import time; time.sleep(5)")
    out, n, ok, setup = mr.run_plan_checks(tmp_path, "CHECK: python slow.py", timeout=1)
    assert (ok, setup) == (0, 1) and "time limit" in out, out


def test_failing_assertion_stays_fail(tmp_path):
    (tmp_path / "bad.py").write_text("assert 1 == 2")
    out, n, ok, setup = mr.run_plan_checks(tmp_path, "CHECK: python bad.py")
    assert (ok, setup) == (0, 0) and "- FAIL" in out and "(exit 1," in out, out


def test_pytest_missing_path_is_setup_but_bad_option_is_fail(tmp_path):
    out, n, ok, setup = mr.run_plan_checks(tmp_path, "CHECK: pytest tests/none_here.py::test_x")
    assert setup == 1 and "- SETUP" in out, out
    out, n, ok, setup = mr.run_plan_checks(tmp_path, "CHECK: pytest --no-such-option-xyz")
    assert setup == 0 and "- FAIL" in out, out


def test_missing_tool_is_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(mr.shutil, "which", lambda *a, **k: None)
    out, n, ok, setup = mr.run_plan_checks(tmp_path, "CHECK: eslint src")
    assert setup == 1 and "not installed" in out, out


def test_a_tool_off_the_list_is_not_run(tmp_path):
    out, n, ok, setup = mr.run_plan_checks(tmp_path, "CHECK: make test")
    assert (ok, setup) == (0, 0) and "NOT RUN" in out, out


def test_python_c_is_still_refused(tmp_path):
    out, n, ok, setup = mr.run_plan_checks(tmp_path, "CHECK: python -c \"import os; print(1)\"")
    assert (ok, setup) == (0, 0) and "NOT RUN" in out, out


def test_seconds_on_pass_lines(tmp_path):
    (tmp_path / "ok.py").write_text("raise SystemExit(0)")
    out, *_ = mr.run_plan_checks(tmp_path, "CHECK: python ok.py")
    assert re.search(r"- PASS `python ok.py` \(\d+ s\)", out), out

