"""P0-4 project-root admission and C112 memory riders (provenance + gating).

Admission: the server's working directory is the session trust anchor — the
home directory, dot-directories, and paths outside the session root are
refused; Git-root promotion may not escape the session root.
Memory: global-scope writes are operator-gated; every entry records writer
harness/session provenance; legacy databases migrate in place.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocdeck.index_server import IndexServer

TEMP_ROOT = Path(tempfile.gettempdir()) / "index-admission-tests"


def git(directory: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=str(directory),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=True,
    )


class AdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.workspace = Path(tempfile.mkdtemp(prefix="admission-", dir=str(TEMP_ROOT)))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.workspace, ignore_errors=True))
        self.hub = self.workspace / "hub"
        self.project = self.workspace / "project"
        self.project.mkdir()
        environment = mock.patch.dict(os.environ, {"OCDECK_HUB_DIR": str(self.hub)})
        environment.start()
        self.addCleanup(environment.stop)
        git(self.project, "init", "-q")
        git(self.project, "config", "user.email", "t@example.com")
        git(self.project, "config", "user.name", "t")

    def server(self, cwd: Path | None = None) -> IndexServer:
        return IndexServer(cwd=cwd or self.project)

    def test_home_directory_is_refused(self) -> None:
        server = self.server()
        with self.assertRaises(ValueError) as caught:
            server.project_root(str(Path.home()))
        self.assertIn("home directory", str(caught.exception))

    def test_outside_session_root_is_refused(self) -> None:
        outside = self.workspace / "outside"
        outside.mkdir()
        with self.assertRaises(ValueError) as caught:
            self.server().project_root(str(outside))
        self.assertIn("outside this session's root", str(caught.exception))

    def test_dot_directory_is_refused(self) -> None:
        hidden = self.project / ".secrets"
        hidden.mkdir()
        with self.assertRaises(ValueError) as caught:
            self.server().project_root(str(hidden))
        self.assertIn("dot-directory", str(caught.exception))

    def test_subdirectory_of_session_root_is_admitted(self) -> None:
        sub = self.project / "src"
        sub.mkdir()
        # setUp makes the session root itself a git repository, so a subdir
        # request promotes to the repo root — which equals the session root
        # and is therefore within the trusted scope (admission, not escape).
        root = self.server().project_root(str(sub))
        self.assertEqual(root, self.project.resolve())

    def test_git_promotion_may_not_escape_the_session_root(self) -> None:
        # A session rooted in a repository subdirectory must not have its
        # scope promoted to the enclosing repository (P0-4).
        sub = self.project / "sub"
        sub.mkdir()
        (sub / "file.txt").write_text("x")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-qm", "init")
        self.assertEqual(self.server(cwd=sub).project_root(), sub.resolve())

    def test_git_promotion_within_session_root_is_kept(self) -> None:
        nested = self.project / "nested"
        nested.mkdir()
        git(nested, "init", "-q")
        git(nested, "config", "user.email", "t@example.com")
        git(nested, "config", "user.name", "t")
        root = self.server().project_root(str(nested))
        self.assertEqual(root, nested.resolve())


class MemoryRiderTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.workspace = Path(tempfile.mkdtemp(prefix="memory-riders-", dir=str(TEMP_ROOT)))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.workspace, ignore_errors=True))
        self.hub = self.workspace / "hub"
        self.project = self.workspace / "project"
        self.project.mkdir()
        # A test launched from an agent terminal must not inherit that agent's
        # identity or its global-write switch into the anonymous fixture.
        environment = mock.patch.dict(os.environ, {
            "OCDECK_HUB_DIR": str(self.hub),
            "OCDECK_INDEX_HARNESS": "", "OCDECK_INDEX_SESSION": "",
            "CLAUDECODE": "", "OCDECK_INDEX_ALLOW_GLOBAL_MEMORY": "",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def server(self) -> IndexServer:
        return IndexServer(cwd=self.project)

    def test_global_write_is_gated_by_default(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.server().memory_write("steer everything", project="global")
        self.assertIn("gated", str(caught.exception))

    def test_global_write_allowed_for_operator_session(self) -> None:
        with mock.patch.dict(os.environ, {"OCDECK_INDEX_ALLOW_GLOBAL_MEMORY": "1"}):
            written = self.server().memory_write("operator note", project="global")
        self.assertIn("id", written)

    def test_writer_provenance_is_recorded(self) -> None:
        server = self.server()
        with mock.patch.dict(os.environ, {
            "OCDECK_INDEX_HARNESS": "claude",
            "OCDECK_INDEX_SESSION": "ses_prov1",
        }):
            server.memory_write("provenanced note", project=str(self.project))
        entry = server.memory_search("provenanced", project=str(self.project))[0]
        self.assertEqual(entry["writer_harness"], "claude")
        self.assertEqual(entry["writer_session"], "ses_prov1")

    def test_unknown_writer_is_labeled(self) -> None:
        server = self.server()
        environment = {
            key: "" for key in ("OCDECK_INDEX_HARNESS", "OCDECK_INDEX_SESSION", "CLAUDECODE")
        }
        with mock.patch.dict(os.environ, environment):
            server.memory_write("anonymous note", project=str(self.project))
        entry = server.memory_search("anonymous", project=str(self.project))[0]
        self.assertEqual(entry["writer_harness"], "unknown")
        self.assertEqual(entry["writer_session"], "unknown")

    def test_legacy_database_migrates_in_place(self) -> None:
        # Pre-provenance schema (4 columns) must migrate and keep old rows.
        self.hub.mkdir(parents=True, exist_ok=True)
        legacy = self.hub / "memory.sqlite"
        connection = sqlite3.connect(legacy)
        with connection:
            connection.execute(
                "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "scope TEXT NOT NULL, text TEXT NOT NULL, tags TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO memories (scope, text, tags, created_at) VALUES (?, ?, ?, ?)",
                (str(self.project.resolve()), "legacy row", "[]", "2026-01-01T00:00:00Z"),
            )
        connection.close()

        server = self.server()
        server.memory_write("fresh row", project=str(self.project))
        results = {entry["text"]: entry for entry in server.memory_search(project=str(self.project))}
        self.assertIn("legacy row", results)
        self.assertEqual(results["legacy row"]["writer_harness"], "unknown")
        self.assertEqual(results["fresh row"]["writer_harness"], "unknown")


if __name__ == "__main__":
    unittest.main()
