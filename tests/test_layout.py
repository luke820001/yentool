"""
The 2026-09-09 reorganisation moved 50 scripts out of the repo root. Python puts
the SCRIPT's directory on sys.path -- not the working directory -- so every one
of them stopped being able to `import scanner` the moment it moved, and nothing
caught it: they are scripts, so no test imported them and no scan ran them.

tools/backfill_ledger.py failed with ModuleNotFoundError the first time it was
needed, AFTER reset_rule_outcomes() had already cleared 5,880 rows. The data was
recoverable from a backup, but the failure mode -- a maintenance script that
dies half way through a destructive two-step -- is worth never repeating.

These tests are cheap and they fail loudly the next time something moves.

    python -m unittest discover -s tests -v
"""
import ast
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Packages that only resolve when the repo ROOT is on sys.path.
PROJECT_PACKAGES = {
    "analyzer", "config", "gemini_hook", "gui", "ingestion", "portfolio",
    "scanner", "storage", "tools",
}


def _project_imports(path):
    """Top-level project packages a module imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found & PROJECT_PACKAGES


class TestToolsAreRunnable(unittest.TestCase):
    """tools/ holds maintenance scripts the owner runs by hand from the root."""

    def scripts(self):
        return sorted(p for p in (ROOT / "tools").glob("*.py")
                      if p.name != "__init__.py")

    def test_there_are_tools_to_check(self):
        self.assertTrue(self.scripts(), "expected scripts under tools/")

    def test_every_tool_bootstraps_its_own_sys_path(self):
        """A script in a subdirectory that imports scanner/config/... must put
        the project root on sys.path itself, because `python tools/x.py` from
        the repo root does not."""
        for script in self.scripts():
            needs = _project_imports(script)
            if not needs:
                continue
            source = script.read_text(encoding="utf-8")
            self.assertIn(
                "sys.path.insert", source.replace("_sys.path.insert",
                                                  "sys.path.insert"),
                "{} imports {} but never puts the project root on sys.path -- "
                "running it from the repo root will raise ModuleNotFoundError"
                .format(script.name, ", ".join(sorted(needs))))

    def test_every_tool_parses(self):
        for script in self.scripts():
            try:
                ast.parse(script.read_text(encoding="utf-8"), str(script))
            except SyntaxError as exc:
                self.fail("{} does not parse: {}".format(script.name, exc))


class TestArchivedScriptsResolve(unittest.TestCase):
    """archive/research/ keeps the evidence behind STRATEGY.md's numbers.

    They are documented as `PYTHONPATH=. python archive/research/x.py`, which
    puts BOTH the repo root (for scanner.*) and the script's own directory (for
    sibling `from eval_prelaunch_overlays import ...`) on the path. This test
    pins that the documented invocation is still sufficient.
    """

    def test_all_imports_resolve_under_the_documented_invocation(self):
        research = ROOT / "archive" / "research"
        if not research.is_dir():
            self.skipTest("archive/research/ not present")
        scripts = sorted(research.glob("*.py"))
        self.assertTrue(scripts)
        siblings = {p.stem for p in scripts}

        unresolved = []
        for script in scripts:
            tree = ast.parse(script.read_text(encoding="utf-8"), str(script))
            mods = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 \
                        and node.module:
                    mods.add(node.module.split(".")[0])
            for mod in mods:
                if mod in siblings or mod in PROJECT_PACKAGES:
                    continue          # resolved by script dir / repo root
                try:
                    if importlib.util.find_spec(mod) is None:
                        unresolved.append("{}: {}".format(script.name, mod))
                except (ImportError, ValueError):
                    unresolved.append("{}: {}".format(script.name, mod))

        self.assertEqual(unresolved, [],
                         "archived scripts have unresolvable imports")

    def test_archive_has_no_init_so_sibling_imports_keep_working(self):
        """An __init__.py would invite `python -m archive.research.x`, under
        which the sibling bare imports break. The README documents the
        PYTHONPATH form on purpose."""
        for name in ("archive/__init__.py", "archive/research/__init__.py"):
            self.assertFalse((ROOT / name).exists(),
                             "{} should not exist -- these are scripts to RUN, "
                             "not a package to import".format(name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
