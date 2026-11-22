"""memoryctl.py lint on a temporary vault: one finding per planted defect, and nothing is edited."""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

_TK = Path(__file__).resolve().parent.parent
_p = _TK / "switchboard/memoryctl.py"
_s = importlib.util.spec_from_file_location("memoryctl", _p)
mc = importlib.util.module_from_spec(_s); _s.loader.exec_module(mc)

GOOD = "---\ntags:\n  - area/work\n  - memory/guide\n---\n\n# {title}\n\nSee [[Work/Work]] and [[Work/Guides/Linked Guide|the guide]].\n"


class MemoryLintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        vault = root / "vault"

        def write(rel, text):
            path = vault / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

        write("MEMORY.md", "# Rules\n\nPlain notes without tags are fine.\n")
        write("Work/Work.md", "---\ntags:\n  - area/work\n  - memory/rules\n---\n\n# Work\n")
        write("Work/Guides/Linked Guide.md", GOOD.format(title="Linked"))
        write("Work/Guides/Broken Link.md", GOOD.format(title="Broken") + "\nAlso [[Work/Missing Note]].\n")  # link
        write("Work/Shared/Auto.md",  # frontmatter
              '---\nname: ""\ndescription: x\n---\n\n# Auto\n')
        write("Work/Shared/Two Kinds.md",  # tags
              "---\ntags: [memory/rules, memory/guide]\n---\n\n# Two\n")
        write("Work/Repos/Kepler/MEMORY.md",  # device-path
              "---\ntags:\n  - area/work\n  - memory/rules\n---\n\n# Kepler\n\nPlan: C:/Dev/Work/Kepler/plan.md\n")
        write("Work/Plans.md",  # placeholder
              "---\ntags: [memory/reference]\n---\n\n# Plans\n\nThe export step is not built yet.\n")
        write("Notes/Untagged.md", "Mentions TODO but carries no memory tag, so no placeholder finding.\n")
        write("Device/This Device.md", "C:/Dev/anything is fine here, and [[Device/Elsewhere]] too\n")
        write(".trash/Old.md", "C:/Dev/ignored [[Nowhere]]\n")
        self.registry = root / "registry.json"
        self.registry.write_text(json.dumps({"vault": str(vault), "device_note": "Device/This Device.md"}), encoding="utf-8")
        self.vault = vault

    def tearDown(self):
        self.tmp.cleanup()

    def run_lint(self):
        before = {p: p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mc.lint(self.registry)
        after = {p: p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "lint must never edit a note")
        self.assertEqual(code, 0)
        return [l for l in out.getvalue().splitlines() if l.strip()]

    def test_one_finding_per_defect(self):
        lines = self.run_lint()
        checks = sorted({(l.split(":")[0], l.split(": ")[1]) for l in lines})
        self.assertEqual(checks, [
            ("Work/Guides/Broken Link.md", "link"),
            ("Work/Plans.md", "placeholder"),
            ("Work/Repos/Kepler/MEMORY.md", "device-path"),
            ("Work/Shared/Auto.md", "frontmatter"),
            ("Work/Shared/Two Kinds.md", "tags"),
        ], "\n".join(lines))
        self.assertEqual(len(lines), 5, "\n".join(lines))

    def test_clean_vault_reports_nothing(self):
        for rel in ("Work/Guides/Broken Link.md", "Work/Plans.md", "Work/Repos/Kepler/MEMORY.md",
                    "Work/Shared/Auto.md", "Work/Shared/Two Kinds.md"):
            (self.vault / rel).unlink()
        self.assertEqual(self.run_lint(), [])

    def test_without_a_device_note_its_folder_is_linted_too(self):
        self.registry.write_text(json.dumps({"vault": str(self.vault)}), encoding="utf-8")
        found = {(l.split(":")[0], l.split(": ")[1]) for l in self.run_lint()}
        self.assertIn(("Device/This Device.md", "device-path"), found)
        self.assertIn(("Device/This Device.md", "link"), found)


if __name__ == "__main__":
    unittest.main()
