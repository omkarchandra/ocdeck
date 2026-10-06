from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ocdeck import index_server
from ocdeck.index_server import IndexServer

TEMP_ROOT = Path("/tmp/opencode")

MEMORY_TOOL_NAMES = {
    "index_project",
    "search_code",
    "list_files",
    "git_status",
    "memory_write",
    "memory_search",
}


def git(*arguments: str, cwd: Path) -> str:
    """Run Git with an argument list, no shell, and a bounded timeout."""
    result = subprocess.run(
        ["git", *arguments],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} exited {result.returncode}: "
            f"{result.stderr.decode('utf-8', 'replace')}"
        )
    return result.stdout.decode("utf-8", "replace").rstrip("\n")


class IndexServerTests(unittest.TestCase):
    """Shared fixture: a throwaway project plus a hub outside the project."""

    def setUp(self) -> None:
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.workspace = Path(
            tempfile.mkdtemp(prefix="index-server-", dir=str(TEMP_ROOT))
        )
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.hub = self.workspace / "hub"
        self.project = self.workspace / "project"
        self.project.mkdir()
        hub_environment = mock.patch.dict(
            os.environ, {"OCDECK_HUB_DIR": str(self.hub)}
        )
        hub_environment.start()
        self.addCleanup(hub_environment.stop)

    def make_server(self) -> IndexServer:
        return IndexServer(cwd=self.project)

    def write(self, relative: str, content: str) -> Path:
        target = self.project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def project_snapshot(self) -> dict[str, tuple[int, int, str]]:
        """Content, size and mtime of every project file, including .git."""
        snapshot: dict[str, tuple[int, int, str]] = {}
        for directory, _names, files in os.walk(self.project):
            base = Path(directory)
            for name in files:
                path = base / name
                data = path.read_bytes()
                metadata = path.stat()
                snapshot[path.relative_to(self.project).as_posix()] = (
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    hashlib.sha256(data).hexdigest(),
                )
        return snapshot


class IncrementalIndexingTests(IndexServerTests):
    def test_unchanged_files_are_not_reread_and_updates_and_removals_apply(self) -> None:
        self.write("alpha.py", "alpha originalword\n")
        self.write("beta.py", "beta removalword\n")
        server = self.make_server()
        original_read = index_server.read_text_file
        reads: list[str] = []

        def recording_read(root, relative, metadata):
            reads.append(relative)
            return original_read(root, relative, metadata)

        with mock.patch.object(index_server, "read_text_file", recording_read):
            first = server.index_project()
            self.assertEqual(sorted(reads), ["alpha.py", "beta.py"])
            self.assertEqual(
                {key: first[key] for key in ("indexed", "updated", "removed", "skipped")},
                {"indexed": 2, "updated": 2, "removed": 0, "skipped": 0},
            )

            reads.clear()
            second = server.index_project()
            self.assertEqual(reads, [], "unchanged files must not be read again")
            self.assertEqual(
                {key: second[key] for key in ("indexed", "updated", "removed", "skipped")},
                {"indexed": 2, "updated": 0, "removed": 0, "skipped": 0},
            )

            self.write("alpha.py", "alpha replacementword\n")
            reads.clear()
            third = server.index_project()
            self.assertEqual(reads, ["alpha.py"])
            self.assertEqual(third["updated"], 1)
            self.assertEqual(third["indexed"], 2)
            self.assertEqual(third["removed"], 0)
            self.assertEqual(server.search_code("originalword"), [])
            self.assertEqual(
                [hit["path"] for hit in server.search_code("replacementword")],
                ["alpha.py"],
            )

            (self.project / "beta.py").unlink()
            reads.clear()
            fourth = server.index_project()
            self.assertEqual(reads, [], "removing one file must not reread the others")
            self.assertEqual(fourth["removed"], 1)
            self.assertEqual(fourth["indexed"], 1)
            self.assertEqual(fourth["updated"], 0)

        self.assertEqual(server.list_files(), ["alpha.py"])
        self.assertEqual(server.search_code("removalword"), [])

    def test_binary_and_oversized_files_are_skipped_but_the_boundary_is_indexed(self) -> None:
        self.write("text.py", "alpha needleword present\n")
        (self.project / "binary.dat").write_bytes(b"MZ\x00binary needleword payload\n")
        (self.project / "huge.txt").write_bytes(
            b"huge needleword " + b"x" * (512 * 1024)
        )
        (self.project / "boundary.txt").write_bytes(
            b"boundary " + b"y" * (512 * 1024 - 9)
        )
        server = self.make_server()

        result = server.index_project()

        self.assertEqual(result["indexed"], 2)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(server.list_files(), ["boundary.txt", "text.py"])
        self.assertEqual([hit["path"] for hit in server.search_code("needleword")], ["text.py"])
        self.assertEqual(server.search_code("binary"), [])
        self.assertEqual(server.search_code("huge"), [])

    def test_only_a_nul_in_the_first_8kib_marks_a_file_binary(self) -> None:
        prefix = b"needleword "
        (self.project / "late-nul.txt").write_bytes(
            prefix + b"z" * (9000 - len(prefix)) + b"\x00" + b"tail"
        )
        # Byte 8191 is still inside the first 8 KiB; byte 8192 is past it.
        (self.project / "early-nul.txt").write_bytes(b"e" * 8191 + b"\x00" + b"tail")
        server = self.make_server()

        result = server.index_project()

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(server.list_files(), ["late-nul.txt"])
        self.assertEqual(
            [hit["path"] for hit in server.search_code("needleword")],
            ["late-nul.txt"],
        )

    def test_symlinked_and_escaping_paths_are_ignored(self) -> None:
        outside = self.workspace / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("needleword secret outside\n", encoding="utf-8")
        self.write("kept.py", "needleword kept inside\n")
        os.symlink(outside / "secret.txt", self.project / "link.txt")
        os.symlink(outside, self.project / "linkdir")
        server = self.make_server()
        root = server.project_root()

        result = server.index_project()

        self.assertEqual(result["indexed"], 1)
        self.assertGreaterEqual(result["skipped"], 1)
        self.assertEqual(server.list_files(), ["kept.py"])
        self.assertEqual([hit["path"] for hit in server.search_code("needleword")], ["kept.py"])
        self.assertIsNone(index_server.file_metadata(root, "link.txt"))
        self.assertIsNone(index_server.file_metadata(root, "linkdir/secret.txt"))
        self.assertIsNone(index_server.file_metadata(root, "../outside/secret.txt"))
        self.assertIsNone(index_server.file_metadata(root, str(outside / "secret.txt")))

    def test_concurrent_first_time_initialization_from_separate_servers(self) -> None:
        for index in range(4):
            self.write(f"shared{index}.py", f"needleword shared{index}\n")
        results: list[dict] = []
        failures: list[BaseException] = []
        start = threading.Barrier(2)

        def build() -> None:
            try:
                start.wait(timeout=20)
                results.append(IndexServer(cwd=self.project).index_project())
            except BaseException as error:  # surfaced through the test assertions
                failures.append(error)

        threads = [threading.Thread(target=build) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        for thread in threads:
            self.assertFalse(thread.is_alive(), "indexer thread hung")

        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(result["indexed"] for result in results), [4, 4])
        self.assertEqual(sorted(result["updated"] for result in results), [0, 4])
        self.assertEqual({result["removed"] for result in results}, {0})
        self.assertEqual({result["skipped"] for result in results}, {0})
        self.assertEqual(
            {result["root"] for result in results}, {str(self.project.resolve())}
        )

        verifier = self.make_server()
        self.assertEqual(len(verifier.list_files()), 4)
        self.assertEqual(len(verifier.search_code("needleword")), 4)

    def test_undecodable_filenames_are_skipped_not_rewritten(self) -> None:
        self.write("kept.py", "needleword kept\n")
        descriptor = os.open(
            os.fsencode(str(self.project)) + b"/undecodable-\xff\xfe.txt",
            os.O_CREAT | os.O_WRONLY,
            0o644,
        )
        try:
            os.write(descriptor, b"needleword undecodable content\n")
        finally:
            os.close(descriptor)
        server = self.make_server()

        result = server.index_project()

        self.assertEqual(result["indexed"], 1)
        self.assertGreaterEqual(result["skipped"], 1)
        listed = server.list_files()
        self.assertEqual(listed, ["kept.py"])
        for path in listed:
            self.assertNotIn("\ufffd", path)
            try:
                path.encode("utf-8")
            except UnicodeEncodeError:
                self.fail(f"reported path is not valid UTF-8: {path!r}")
        self.assertEqual([hit["path"] for hit in server.search_code("needleword")], ["kept.py"])
        self.assertIsNone(
            index_server.file_metadata(
                server.project_root(), "undecodable-\udcff\udcfe.txt"
            )
        )


class SearchTests(IndexServerTests):
    def test_search_code_builds_a_missing_index_automatically(self) -> None:
        self.write("auto.py", "autoindex needleword\n")
        server = self.make_server()

        hits = server.search_code("needleword")

        self.assertEqual([hit["path"] for hit in hits], ["auto.py"])
        self.assertTrue(server.index_path(server.project_root()).exists())

    def test_search_refreshes_an_index_older_than_300_seconds(self) -> None:
        self.write("first.py", "first zebrafish\n")
        server = self.make_server()
        root = server.project_root()
        self.assertEqual([hit["path"] for hit in server.search_code("zebrafish")], ["first.py"])

        self.write("second.py", "second qxyterm\n")
        self.assertEqual(
            server.search_code("qxyterm"),
            [],
            "a fresh index must not be rebuilt on every search",
        )

        connection = sqlite3.connect(server.index_path(root))
        try:
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'last_index'",
                (str(time.time() - 301),),
            )
            connection.commit()
        finally:
            connection.close()

        self.assertEqual([hit["path"] for hit in server.search_code("qxyterm")], ["second.py"])

    def test_hostile_queries_return_lists_without_matching_everything(self) -> None:
        self.write("alpha.py", "alpha only text\n")
        self.write("needle.py", "needleword here\n")
        server = self.make_server()
        server.index_project()

        for query in (
            'foo" OR (bar',
            "nonexistent OR alpha",
            "*",
            '"',
            "'",
            "needle\x00word",
            "",
            "   ",
        ):
            with self.subTest(query=query):
                self.assertIsInstance(server.search_code(query), list)

        self.assertEqual(server.search_code('foo" OR (bar'), [])
        self.assertEqual(server.search_code("nonexistent OR alpha"), [])
        self.assertEqual(server.search_code("*"), [])
        self.assertEqual(server.search_code('"'), [])
        self.assertEqual(server.search_code(""), [])
        self.assertEqual(
            [hit["path"] for hit in server.search_code("needleword")],
            ["needle.py"],
        )

    def test_list_files_filters_with_glob_patterns(self) -> None:
        self.write("a.py", "alpha\n")
        self.write("b.py", "beta\n")
        self.write("docs/readme.md", "# docs\n")
        self.write("src/mod.py", "gamma\n")
        server = self.make_server()
        server.index_project()

        self.assertEqual(server.list_files(pattern="*.py"), ["a.py", "b.py", "src/mod.py"])
        self.assertEqual(server.list_files(pattern="docs/*"), ["docs/readme.md"])
        self.assertEqual(server.list_files(pattern="*.md"), ["docs/readme.md"])
        self.assertEqual(server.list_files(pattern="src/*"), ["src/mod.py"])
        self.assertEqual(server.list_files(pattern="*missing*"), [])
        self.assertEqual(server.list_files(pattern="*", limit=2), ["a.py", "b.py"])

    def test_result_paths_are_relative_except_root_and_scope_metadata(self) -> None:
        self.write("src/rel.py", "relative needleword path\n")
        server = self.make_server()

        hits = server.search_code("needleword")
        self.assertEqual([hit["path"] for hit in hits], ["src/rel.py"])
        for hit in hits:
            self.assertEqual(set(hit), {"path", "matches"})
            path = Path(hit["path"])
            self.assertFalse(path.is_absolute())
            self.assertNotIn("..", path.parts)
            self.assertLessEqual(len(hit["matches"]), 5)
            for match in hit["matches"]:
                self.assertIsInstance(match["line"], int)
                self.assertIsInstance(match["text"], str)

        for listed in server.list_files():
            self.assertFalse(Path(listed).is_absolute())
            self.assertNotIn("..", Path(listed).parts)

        self.assertTrue(Path(server.index_project()["root"]).is_absolute())

        server.memory_write("scoped memory", project=str(self.project))
        entries = server.memory_search()
        self.assertTrue(entries)
        for entry in entries:
            self.assertIn(entry["scope"], (str(self.project.resolve()), "global"))

    def test_fts5_unavailable_falls_back_to_a_plain_like_index(self) -> None:
        class NoFts5Connection(sqlite3.Connection):
            def execute(self, sql, *parameters, **kwargs):
                if sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
                    raise sqlite3.OperationalError("no such module: fts5")
                return super().execute(sql, *parameters, **kwargs)

        self.write("fallback.py", "fallback needleword text\n")
        # NUL sits at byte 8192, past the binary window, so the file must index;
        # the LIKE fallback must still match text recorded after that NUL.
        (self.project / "nul-split.txt").write_text(
            "x" * 8192 + "\0\nneedle_after_nul\n", encoding="utf-8"
        )
        server = self.make_server()
        original_connect = sqlite3.connect

        def connect_without_fts5(*args, **kwargs):
            kwargs.setdefault("factory", NoFts5Connection)
            return original_connect(*args, **kwargs)

        with mock.patch.object(index_server.sqlite3, "connect", connect_without_fts5):
            result = server.index_project()
            hits = server.search_code("needleword")
            nul_hits = server.search_code("needle_after_nul")

        self.assertEqual(result["indexed"], 2)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual([hit["path"] for hit in hits], ["fallback.py"])
        self.assertEqual([hit["path"] for hit in nul_hits], ["nul-split.txt"])
        self.assertEqual(
            sorted(server.list_files()), ["fallback.py", "nul-split.txt"]
        )

        connection = sqlite3.connect(server.index_path(server.project_root()))
        try:
            mode = connection.execute(
                "SELECT value FROM metadata WHERE key = 'search_mode'"
            ).fetchone()[0]
            document_type = connection.execute(
                "SELECT type FROM sqlite_master WHERE name = 'documents'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(mode, "like")
        self.assertEqual(document_type, "table")


class GitStatusTests(IndexServerTests):
    def setUp(self) -> None:
        super().setUp()
        git_environment = mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        git_environment.start()
        self.addCleanup(git_environment.stop)

    def test_non_repository_project_reports_empty_status(self) -> None:
        status = self.make_server().git_status()

        self.assertEqual(
            status,
            {
                "branch": None,
                "upstream": None,
                "ahead": 0,
                "behind": 0,
                "changes": [],
                "recent_commits": [],
            },
        )

    def build_committed_repository(self) -> None:
        git("init", cwd=self.project)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.project)
        git("config", "user.email", "index-tests@example.com", cwd=self.project)
        git("config", "user.name", "Index Tests", cwd=self.project)
        (self.project / "tracked.txt").write_text("one\n", encoding="utf-8")
        git("add", "tracked.txt", cwd=self.project)
        git("commit", "-m", "initial commit", cwd=self.project)

    def test_repository_reports_branch_upstream_divergence_changes_and_commits(self) -> None:
        origin = self.workspace / "origin.git"
        git("init", "--bare", str(origin), cwd=self.workspace)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=origin)
        self.build_committed_repository()
        git("remote", "add", "origin", str(origin), cwd=self.project)
        git("push", "--set-upstream", "origin", "main", cwd=self.project)

        # One unpushed commit makes the branch ahead of its upstream.
        (self.project / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
        git("add", "tracked.txt", cwd=self.project)
        git("commit", "-m", "local change", cwd=self.project)

        # A pushed commit elsewhere makes the branch behind after the fetch.
        other = self.workspace / "other"
        git("clone", str(origin), str(other), cwd=self.workspace)
        git("config", "user.email", "index-tests@example.com", cwd=other)
        git("config", "user.name", "Index Tests", cwd=other)
        (other / "remote.txt").write_text("remote\n", encoding="utf-8")
        git("add", "remote.txt", cwd=other)
        git("commit", "-m", "remote change", cwd=other)
        git("push", "origin", "main", cwd=other)
        git("fetch", "origin", cwd=self.project)

        # Working-tree edits feed the porcelain output.
        (self.project / "tracked.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        (self.project / "untracked.txt").write_text("new\n", encoding="utf-8")

        status = self.make_server().git_status()

        self.assertEqual(status["branch"], "main")
        self.assertEqual(status["upstream"], "origin/main")
        self.assertEqual(status["ahead"], 1)
        self.assertEqual(status["behind"], 1)
        self.assertTrue(
            any(line.endswith("tracked.txt") and "M" in line for line in status["changes"]),
            status["changes"],
        )
        self.assertIn("?? untracked.txt", status["changes"])
        subjects = [line.split(" ", 1)[1] for line in status["recent_commits"]]
        self.assertEqual(subjects[:2], ["local change", "initial commit"])
        self.assertLessEqual(len(status["recent_commits"]), 10)

    def test_project_files_survive_index_search_status_and_memory_calls(self) -> None:
        self.build_committed_repository()
        # Same bytes but a fresh mtime: a plain `git status` would rewrite
        # .git/index here, so the snapshot proves the no-optional-locks guard.
        (self.project / "tracked.txt").write_text("one\n", encoding="utf-8")
        self.write("scratch.txt", "needleword scratch\n")
        server = self.make_server()

        before = self.project_snapshot()
        self.assertIn(".git/index", before)
        first = server.index_project()
        hits = server.search_code("needleword")
        listed = server.list_files(pattern="*.txt")
        status = server.git_status()
        server.memory_write("no project writes", project=str(self.project))
        memory = server.memory_search("no project writes", project=str(self.project))
        after = self.project_snapshot()

        self.assertEqual(after, before)
        self.assertEqual(first["indexed"], 2)
        self.assertEqual([hit["path"] for hit in hits], ["scratch.txt"])
        self.assertEqual(listed, ["scratch.txt", "tracked.txt"])
        self.assertTrue(status["changes"])
        self.assertEqual([entry["text"] for entry in memory], ["no project writes"])


class MemoryTests(IndexServerTests):
    def test_scopes_isolate_projects_while_global_is_shared(self) -> None:
        other = self.workspace / "other"
        other.mkdir()
        server = self.make_server()
        # P0-4: each server admits only its own session root, so the second
        # project is written/searched through its own session server.
        other_server = IndexServer(cwd=other)
        with mock.patch.dict(os.environ, {"OCDECK_INDEX_ALLOW_GLOBAL_MEMORY": "1"}):
            server.memory_write("global guidance", project="global")
        server.memory_write("alpha only", project=str(self.project))
        other_server.memory_write("other only", project=str(other))

        alpha = {entry["text"] for entry in server.memory_search(project=str(self.project))}
        self.assertEqual(alpha, {"global guidance", "alpha only"})

        shared = {entry["text"] for entry in other_server.memory_search(project=str(other))}
        self.assertEqual(shared, {"global guidance", "other only"})

        globals_only = server.memory_search(project="global")
        self.assertEqual({entry["text"] for entry in globals_only}, {"global guidance"})
        self.assertTrue(all(entry["scope"] == "global" for entry in globals_only))

        defaulted = {entry["text"] for entry in server.memory_search()}
        self.assertEqual(defaulted, {"global guidance", "alpha only"})

    def test_memory_search_matches_text_and_tags_case_insensitively(self) -> None:
        server = self.make_server()
        server.memory_write(
            "Deploy Checklist ready", project=str(self.project), tags=["ReleaseGate"]
        )

        by_text = server.memory_search("deploy CHECKlist", project=str(self.project))
        self.assertEqual([entry["text"] for entry in by_text], ["Deploy Checklist ready"])

        by_tag = server.memory_search("releasegate", project=str(self.project))
        self.assertEqual([entry["text"] for entry in by_tag], ["Deploy Checklist ready"])
        self.assertEqual(by_tag[0]["tags"], ["ReleaseGate"])

        self.assertEqual(server.memory_search("absent phrase", project=str(self.project)), [])

    def test_memory_write_rejects_blank_or_oversized_text(self) -> None:
        server = self.make_server()
        for invalid in ("", "   ", "\n\t", "x" * 20001):
            with self.subTest(length=len(invalid)):
                with self.assertRaises(ValueError):
                    server.memory_write(invalid, project=str(self.project))

        accepted = server.memory_write("y" * 20000, project=str(self.project))
        self.assertIsInstance(accepted["id"], int)
        self.assertGreater(accepted["id"], 0)
        self.assertEqual(
            [entry["text"] for entry in server.memory_search(project=str(self.project))],
            ["y" * 20000],
        )


class StorageTests(IndexServerTests):
    def test_index_and_memory_databases_stay_private(self) -> None:
        self.write("private.py", "private needleword\n")
        server = self.make_server()
        server.index_project()
        server.memory_write("private note", project=str(self.project))

        self.assertEqual(stat.S_IMODE(self.hub.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.hub / "index").stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(server.index_path(server.project_root()).stat().st_mode),
            0o600,
        )
        self.assertEqual(stat.S_IMODE((self.hub / "memory.sqlite").stat().st_mode), 0o600)

    def test_storage_must_not_be_created_inside_the_project(self) -> None:
        with mock.patch.dict(os.environ, {"OCDECK_HUB_DIR": str(self.project / "hub")}):
            server = IndexServer(cwd=self.project)
            with self.assertRaises(ValueError):
                server.index_project()
        self.assertFalse((self.project / "hub").exists())


class JsonRpcProcessTests(IndexServerTests):
    def test_stdio_jsonrpc_process_round_trip(self) -> None:
        self.write("hit.py", "needleword from subprocess\n")
        opening = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "index-test", "version": "9.9.9"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "search_code", "arguments": {"query": "needleword"}},
            },
            {"jsonrpc": "2.0", "id": 5, "method": "does/not/exist"},
        ]
        closing = [
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "memory_write", "arguments": {"text": ""}},
            },
            {"jsonrpc": "2.0", "id": 7, "method": "ping"},
        ]
        payload = "".join(json.dumps(item) + "\n" for item in opening)
        payload += "{not valid json\n"
        payload += "".join(json.dumps(item) + "\n" for item in closing)

        process = subprocess.run(
            [sys.executable, "-m", "ocdeck.index_server"],
            cwd=str(self.project),
            env=dict(os.environ),
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)

        lines = process.stdout.splitlines()
        self.assertEqual(len(lines), 8, process.stdout)
        responses = [json.loads(line) for line in lines]
        # The initialized notification must stay silent; everything else answers in order.
        self.assertEqual(
            [reply.get("id") for reply in responses],
            [1, 2, 3, 4, 5, None, 6, 7],
        )

        initialize = responses[0]["result"]
        self.assertEqual(initialize["protocolVersion"], "2024-11-05")
        self.assertEqual(initialize["serverInfo"]["name"], "ocdeck-index")
        self.assertIn("tools", initialize["capabilities"])

        self.assertEqual(responses[1]["result"], {})

        tools = responses[2]["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, MEMORY_TOOL_NAMES)
        for tool in tools:
            self.assertIn("description", tool)
            self.assertIn("inputSchema", tool)

        call = responses[3]["result"]
        self.assertNotIn("isError", call)
        self.assertEqual(call["content"][0]["type"], "text")
        hits = json.loads(call["content"][0]["text"])
        self.assertEqual([hit["path"] for hit in hits], ["hit.py"])
        self.assertFalse(Path(hits[0]["path"]).is_absolute())

        self.assertEqual(responses[4]["error"]["code"], -32601)
        self.assertIsNone(responses[5]["id"])
        self.assertEqual(responses[5]["error"]["code"], -32700)

        failure = responses[6]["result"]
        self.assertIs(failure["isError"], True)
        self.assertEqual(failure["content"][0]["type"], "text")
        self.assertIn("error", json.loads(failure["content"][0]["text"]))

        self.assertEqual(responses[7]["result"], {})

    def test_stdio_recovers_after_bad_utf8_deep_json_and_nan(self) -> None:
        depth = 100000
        payload = b"".join(
            (
                b"\xff\xfe invalid utf-8 line\n",
                b"[" * depth + b"]" * depth + b"\n",
                json.dumps(
                    {"jsonrpc": "2.0", "id": 9, "method": "ping", "params": float("nan")}
                ).encode("utf-8")
                + b"\n",
                json.dumps({"jsonrpc": "2.0", "id": 10, "method": "ping"}).encode("utf-8")
                + b"\n",
            )
        )

        process = subprocess.run(
            [sys.executable, "-m", "ocdeck.index_server"],
            cwd=str(self.project),
            env=dict(os.environ),
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", "replace"))

        lines = process.stdout.splitlines()
        self.assertEqual(len(lines), 4, process.stdout[:4000])
        responses = [json.loads(line) for line in lines]
        self.assertEqual([reply.get("id") for reply in responses], [None, None, None, 10])
        for reply in responses[:3]:
            self.assertEqual(reply["error"]["code"], -32700)
            self.assertEqual(reply["error"]["message"], "Parse error")
        self.assertEqual(responses[3]["result"], {})


class UnifiedMemoryTests(IndexServerTests):
    """One memory per project, whichever harness or subfolder the agent runs in."""

    def git_init(self, path: Path) -> None:
        subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)

    def test_harnesses_in_the_repo_root_and_a_subfolder_share_one_memory(self) -> None:
        self.git_init(self.project)
        sub = self.project / "dashboard"
        sub.mkdir()
        root_server, sub_server = IndexServer(cwd=self.project), IndexServer(cwd=sub)
        with mock.patch.dict(os.environ, {"OCDECK_INDEX_HARNESS": "claude", "OCDECK_INDEX_SESSION": "c1"}):
            sub_server.memory_write("the deck uses tmux prefixes from the registry", tags=["design"])
        with mock.patch.dict(os.environ, {"OCDECK_INDEX_HARNESS": "opencode", "OCDECK_INDEX_SESSION": "o1"}):
            root_server.memory_write("run tests/e2e_keys.py before handing over")
        for server in (root_server, sub_server):
            entries = server.memory_search()
            self.assertEqual(
                {(e["text"], e["writer_harness"]) for e in entries},
                {("the deck uses tmux prefixes from the registry", "claude"),
                 ("run tests/e2e_keys.py before handing over", "opencode")},
            )
            self.assertEqual({e["scope"] for e in entries}, {str(self.project.resolve())})
        self.assertEqual([e["text"] for e in sub_server.memory_search("registry")],
                         ["the deck uses tmux prefixes from the registry"])

    def test_separate_projects_keep_separate_memories(self) -> None:
        other = self.workspace / "other"
        other.mkdir()
        self.git_init(self.project)
        IndexServer(cwd=self.project).memory_write("alpha fact")
        IndexServer(cwd=other).memory_write("other fact")
        self.assertEqual([e["text"] for e in IndexServer(cwd=other).memory_search()], ["other fact"])

    def test_a_home_directory_repository_is_not_a_project_identity(self) -> None:
        self.git_init(self.workspace)  # e.g. a dotfiles repo at $HOME
        with mock.patch.object(Path, "home", return_value=self.workspace):
            IndexServer(cwd=self.project).memory_write("project only")
            entries = IndexServer(cwd=self.project).memory_search()
        self.assertEqual({e["scope"] for e in entries}, {str(self.project.resolve())})

    def test_file_reads_stay_bounded_by_the_session_folder(self) -> None:
        self.git_init(self.project)
        sub = self.project / "dashboard"
        sub.mkdir()
        self.write("secret.txt", "outside the session folder\n")
        with self.assertRaises(ValueError):
            IndexServer(cwd=sub).project_root(str(self.project))


class ServerInstructionsTests(unittest.TestCase):
    def test_initialize_tells_every_harness_how_to_use_shared_memory(self) -> None:
        from ocdeck import index_server
        text = index_server.SERVER_INSTRUCTIONS
        for phrase in ("memory_search", "memory_write", "OpenCode, Claude Code, Codex", "untrusted", "secrets"):
            self.assertIn(phrase, text)


class AdmissionOrderTests(IndexServerTests):
    """Memory follow-up gap 2: refuse outside-root paths before touching the filesystem."""

    def test_outside_root_paths_are_refused_without_filesystem_access(self) -> None:
        from ocdeck import index_server
        outside = self.workspace / "other"
        outside.mkdir()
        touched: list[str] = []
        real_resolve, real_is_dir = Path.resolve, Path.is_dir

        def spy_resolve(path, *args, **kwargs):
            touched.append(str(path))
            return real_resolve(path, *args, **kwargs)

        def spy_is_dir(path):
            touched.append(str(path))
            return real_is_dir(path)

        cwd = self.project.resolve()
        for candidate in (str(outside), "../other", str(Path.home()), "sub/../../other"):
            touched.clear()
            with self.subTest(candidate=candidate), \
                    mock.patch.object(Path, "resolve", spy_resolve), mock.patch.object(Path, "is_dir", spy_is_dir):
                with self.assertRaises(ValueError):
                    index_server.resolve_project(candidate, cwd)
                self.assertFalse([p for p in touched if "other" in p or p == str(Path.home())], touched)

    def test_symlink_escape_is_still_refused_after_resolution(self) -> None:
        from ocdeck import index_server
        outside = self.workspace / "other"
        outside.mkdir()
        (self.project / "link").symlink_to(outside)
        with self.assertRaises(ValueError):
            index_server.resolve_project("link", self.project.resolve())
