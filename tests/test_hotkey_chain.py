"""The Super+O chain must keep working on its own, whatever the rest of OC Deck does.

It runs under the system Python from an installed snapshot containing only
three files. These tests copy exactly those files into an empty directory and
load them the way Super+O does; a new import from the ocdeck package (the
cause of an earlier outage) fails here instead of on the user's desktop.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1]
CHAIN = {
    "ocdeck_hotkey.py": DASHBOARD / "ocdeck_hotkey.py",
    "focus_helper.py": DASHBOARD / "src/ocdeck/focus_helper.py",
    "ptyxis_tabs.py": DASHBOARD / "src/ocdeck/ptyxis_tabs.py",
}
SYSTEM_PYTHON = "/usr/bin/python3"


def gi_available() -> bool:
    return subprocess.run([SYSTEM_PYTHON, "-c", "import gi"], capture_output=True).returncode == 0


@unittest.skipUnless(Path(SYSTEM_PYTHON).exists(), "no system Python")
class ChainIsSelfContainedTests(unittest.TestCase):
    def test_chain_files_import_nothing_from_the_ocdeck_package(self):
        for name, path in CHAIN.items():
            source = path.read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertNotRegex(source, r"(?m)^\s*(from|import)\s+(\.|ocdeck\b|agent_tabs\b|harnesses\b)")

    @unittest.skipUnless(gi_available(), "system Python has no PyGObject")
    def test_a_bare_snapshot_loads_under_the_system_python(self):
        with tempfile.TemporaryDirectory() as base:
            for name, path in CHAIN.items():
                shutil.copy(path, Path(base) / name)
            result = subprocess.run(
                [SYSTEM_PYTHON, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                 "import focus_helper, ptyxis_tabs, ocdeck_hotkey; print(ocdeck_hotkey.helper_path())", base],
                capture_output=True, text=True, timeout=30, cwd="/",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(Path(base).resolve() / "focus_helper.py"))


@unittest.skipUnless(gi_available(), "system Python has no PyGObject")
class InstallerTests(unittest.TestCase):
    def run_installer(self, home: Path, *args: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "HOME": str(home), "OCDECK_HOTKEY_LIB": str(home / "lib"),
               "OCDECK_HOTKEY_BIN": str(home / "bin")}
        return subprocess.run([str(DASHBOARD / "bin/install-ocdeck-hotkey"), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_install_rollback_and_workspace(self):
        with tempfile.TemporaryDirectory() as base:
            home = Path(base)
            self.assertEqual(self.run_installer(home).returncode, 0)
            link = home / "bin/ocdeck-hotkey"
            self.assertEqual(link.resolve(), (home / "lib/current/ocdeck_hotkey.py").resolve())
            self.assertEqual((home / "lib/current/workspace").read_text().strip(), str(DASHBOARD))
            self.assertEqual(self.run_installer(home).returncode, 0)  # second install keeps a previous
            self.assertTrue((home / "lib/previous/ocdeck_hotkey.py").exists())
            rolled = self.run_installer(home, "--rollback")
            self.assertEqual(rolled.returncode, 0, rolled.stderr)
            self.assertTrue(link.resolve().is_file())

    def test_a_broken_snapshot_never_replaces_the_working_one(self):
        with tempfile.TemporaryDirectory() as base:
            home = Path(base)
            self.assertEqual(self.run_installer(home).returncode, 0)
            before = (home / "lib/current/ocdeck_hotkey.py").read_text()
            env = {**os.environ, "HOME": str(home), "OCDECK_HOTKEY_LIB": str(home / "lib"),
                   "OCDECK_HOTKEY_BIN": str(home / "bin"), "OCDECK_SYSTEM_PYTHON": "/bin/false"}
            broken = subprocess.run([str(DASHBOARD / "bin/install-ocdeck-hotkey")], env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertNotEqual(broken.returncode, 0)
            self.assertIn("left unchanged", broken.stderr)
            self.assertEqual((home / "lib/current/ocdeck_hotkey.py").read_text(), before)
            self.assertEqual(list((home / "lib").glob(".staging*")), [])


if __name__ == "__main__":
    unittest.main()
