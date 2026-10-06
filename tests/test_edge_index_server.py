"""Hostile-input and boundary coverage for the ocdeck-index stdio server."""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from ocdeck import index_server
from ocdeck.index_server import IndexServer

# Temp workspaces stay inside this worktree so no other directory is touched.
WORKTREE = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = index_server.MAX_FILE_BYTES


class EdgeCaseTests(unittest.TestCase):
    """Throwaway project plus fully isolated HOME/XDG/git/hub state."""

    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="edge-index-"))  # outside any repository
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.hub = self.workspace / "hub"
        self.project = self.workspace / "project"
        self.project.mkdir()
        home = self.workspace / "home"
        home.mkdir()
        environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(self.workspace / "xdg-config"),
                "XDG_DATA_HOME": str(self.workspace / "xdg-data"),
                "XDG_STATE_HOME": str(self.workspace / "xdg-state"),
                "CODEX_HOME": str(self.workspace / "codex"),
                "OCDECK_HUB_DIR": str(self.hub),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    # -- helpers ---------------------------------------------------------
    def make_server(self, cwd: Path | None = None) -> IndexServer:
        return IndexServer(cwd=cwd or self.project)

    def write(self, relative: str, content: str, root: Path | None = None) -> Path:
        target = (root or self.project) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    @staticmethod
    def line(identifier: Any, method: str, params: Any = ...) -> bytes:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier, "method": method}
        if params is not ...:
            message["params"] = params
        return json.dumps(message).encode("utf-8") + b"\n"

    @staticmethod
    def notify(method: str, params: Any = ...) -> bytes:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not ...:
            message["params"] = params
        return json.dumps(message).encode("utf-8") + b"\n"

    def serve(self, payload: bytes, server: IndexServer | None = None) -> list[Any]:
        output = io.StringIO()
        index_server.serve(server or self.make_server(), io.BytesIO(payload), output)
        return [json.loads(entry) for entry in output.getvalue().splitlines()]

    def git(self, *arguments: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            ["git", *arguments],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"git {' '.join(arguments)} exited {result.returncode}: "
                f"{result.stderr.decode('utf-8', 'replace')}"
            )
        return result

    def build_repository(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", str(path), cwd=self.workspace)
        self.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=path)
        self.git("config", "user.email", "edge-tests@example.com", cwd=path)
        self.git("config", "user.name", "Edge Tests", cwd=path)
        (path / "tracked.txt").write_text("one\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=path)
        self.git("commit", "-q", "-m", "initial commit", cwd=path)


class JsonRpcFramingEdgeTests(EdgeCaseTests):
    def test_batch_arrays_answer_one_invalid_request_and_the_stream_continues(self) -> None:
        """Batching is unsupported: a single -32600 object, then normal service."""
        payload = b"".join(
            [
                json.dumps(
                    [
                        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    ]
                ).encode("utf-8")
                + b"\n",
                b"[]\n",
                b"[1, 2, 3]\n",
                b'[{"jsonrpc": "2.0", "id": 3, "method": "ping"}]\n',
                self.line(4, "ping"),
            ]
        )

        responses = self.serve(payload)

        self.assertEqual(len(responses), 5, responses)
        for reply in responses[:4]:
            self.assertIsInstance(reply, dict, "batch replies must stay JSON objects")
            self.assertEqual(set(reply), {"jsonrpc", "id", "error"})
            self.assertEqual(reply["jsonrpc"], "2.0")
            self.assertIsNone(reply["id"])
            self.assertEqual(reply["error"]["code"], -32600)
        self.assertEqual(responses[4], {"jsonrpc": "2.0", "id": 4, "result": {}})

    def test_string_number_and_null_ids_are_echoed_verbatim(self) -> None:
        identifiers: list[Any] = ["plain-id", "", 0, -7, 2**70, 1.5, None]
        payload = b"".join(self.line(value, "ping") for value in identifiers)

        responses = self.serve(payload)

        self.assertEqual([reply["id"] for reply in responses], identifiers)
        for reply in responses:
            self.assertEqual(reply["result"], {})
            self.assertNotIn("error", reply)

    def test_object_array_boolean_and_non_finite_ids_are_rejected(self) -> None:
        server = self.make_server()
        for identifier in ({"id": 1}, [1, 2], True, float("nan"), float("inf")):
            with self.subTest(identifier=identifier):
                reply = index_server.handle_request(
                    server, {"jsonrpc": "2.0", "id": identifier, "method": "ping"}
                )
                self.assertEqual(reply["id"], None)
                self.assertEqual(reply["error"]["code"], -32600)
                self.assertEqual(reply["error"]["message"], "Invalid Request")

        # 1e400 parses to infinity, and a NaN literal is not JSON at all.
        responses = self.serve(b'{"jsonrpc": "2.0", "id": 1e400, "method": "ping"}\n')
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertIsNone(responses[0]["id"])
        responses = self.serve(b'{"jsonrpc": "2.0", "id": NaN, "method": "ping"}\n')
        self.assertEqual(responses[0]["error"]["code"], -32700)

    def test_notifications_never_produce_output_even_for_unknown_methods(self) -> None:
        payload = b"".join(
            [
                self.notify("does/not/exist"),
                self.notify("notifications/initialized"),
                self.notify("ping"),
                self.notify("tools/call", {"name": "unknown_tool", "arguments": {}}),
                self.notify("tools/list", [1, 2]),
                b'{"jsonrpc": "1.0", "method": "ping"}\n',
                b'{"method": "ping"}\n',
                self.line("final", "ping"),
            ]
        )

        responses = self.serve(payload)

        self.assertEqual(responses, [{"jsonrpc": "2.0", "id": "final", "result": {}}])

    def test_wrong_param_types_report_invalid_params(self) -> None:
        payload = b"".join(
            [
                self.line(1, "ping", [1, 2]),
                self.line(2, "ping", "not-an-object"),
                self.line(3, "tools/list", None),
                self.line(4, "initialize", "not-an-object"),
                self.line(5, "initialize", {}),
                self.line(6, "initialize", {"protocolVersion": 7}),
                self.line(7, "ping"),
            ]
        )

        responses = self.serve(payload)

        self.assertEqual([reply["id"] for reply in responses], [1, 2, 3, 4, 5, 6, 7])
        for reply in responses[:4]:
            self.assertEqual(reply["error"]["code"], -32602, reply)
            self.assertEqual(reply["error"]["message"], "Invalid params")
        for reply in responses[4:6]:
            self.assertEqual(reply["error"]["code"], -32602, reply)
            self.assertEqual(reply["error"]["message"], "protocolVersion is required")
        self.assertEqual(responses[6]["result"], {})

    def test_non_objects_and_wrong_protocol_version_are_invalid_requests(self) -> None:
        payload = b"".join(
            [
                b"42\n",
                b"null\n",
                b'"hello"\n',
                b'{"jsonrpc": "1.0", "id": 1, "method": "ping"}\n',
                b'{"jsonrpc": "2.0", "id": 2}\n',
                b'{"id": 3, "method": "ping"}\n',
                self.line(4, "ping"),
            ]
        )

        responses = self.serve(payload)

        self.assertEqual(len(responses), 7)
        # Scalars carry no id of their own; object requests keep theirs.
        self.assertEqual([reply["id"] for reply in responses[:6]], [None, None, None, 1, 2, 3])
        for reply in responses[:6]:
            self.assertEqual(reply["error"]["code"], -32600, reply)
            self.assertEqual(reply["error"]["message"], "Invalid Request")
        self.assertEqual(responses[6]["result"], {})

    def test_megabyte_sized_lines_are_answered_without_hanging(self) -> None:
        giant_id = "y" * (1024 * 1024)
        payload = b"".join(
            [
                b"z" * (4 * 1024 * 1024) + b"\n",
                self.line(giant_id, "ping"),
                self.line(1, "tools/call", {"name": "memory_write", "arguments": {"text": "m" * 300000}}),
                self.line(2, "ping"),
            ]
        )

        started = time.monotonic()
        responses = self.serve(payload)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 30.0, "a huge line must not stall the stdio loop")
        self.assertEqual(len(responses), 4)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["id"], giant_id)
        self.assertEqual(responses[1]["result"], {})
        self.assertIs(responses[2]["result"]["isError"], True)
        self.assertIn("error", json.loads(responses[2]["result"]["content"][0]["text"]))
        self.assertEqual(responses[3]["result"], {})

    def test_invalid_utf8_on_stdin_isolated_to_its_own_line(self) -> None:
        payload = b"".join(
            [
                b"\xff\xfe bad utf-8\n",
                b'{"jsonrpc": "2.0", "id": "caf\xc3\xa9", "method": "ping"}\n',
                b'{"jsonrpc": "2.0", "id": "embedded-\xff", "method": "ping"}\n',
                self.line("after", "ping"),
            ]
        )

        responses = self.serve(payload)

        self.assertEqual(len(responses), 4)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["id"], "café")
        self.assertEqual(responses[1]["result"], {})
        self.assertEqual(responses[2]["error"]["code"], -32700)
        self.assertEqual(responses[3]["id"], "after")

    def test_eof_in_the_middle_of_a_line_reports_parse_error(self) -> None:
        responses = self.serve(b'{"jsonrpc": "2.0", "id": 9, "method": "pi')

        self.assertEqual(len(responses), 1, responses)
        self.assertIsNone(responses[0]["id"])
        self.assertEqual(responses[0]["error"]["code"], -32700)

        # A complete final line without a trailing newline is still serviced.
        responses = self.serve(self.line(10, "ping")[:-1])
        self.assertEqual(responses, [{"jsonrpc": "2.0", "id": 10, "result": {}}])

        # A truncated tail after good traffic only costs the tail.
        responses = self.serve(self.line(11, "ping") + b'{"jsonrpc": "2.0", "id": 12, "met')
        self.assertEqual(
            [reply.get("id") for reply in responses], [11, None]
        )
        self.assertEqual(responses[0]["result"], {})
        self.assertEqual(responses[1]["error"]["code"], -32700)

    def test_unexpected_handler_failure_returns_internal_error_then_recovers(self) -> None:
        real_handler = index_server.handle_request
        calls: list[int] = []

        def flaky(server: IndexServer, request: Any) -> Any:
            if not calls:
                calls.append(1)
                raise RuntimeError("simulated handler failure")
            return real_handler(server, request)

        payload = self.line("boom", "ping") + self.notify("ping") + self.line("ok", "ping")
        with mock.patch.object(index_server, "handle_request", flaky):
            responses = self.serve(payload)

        self.assertEqual(len(responses), 2, responses)
        self.assertEqual(responses[0]["id"], "boom")
        self.assertEqual(responses[0]["error"]["code"], -32603)
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": "ok", "result": {}})


class ToolCallArgumentEdgeTests(EdgeCaseTests):
    def test_unknown_tool_name_is_an_error_result(self) -> None:
        server = self.make_server()

        direct = server.call_tool("no_such_tool", {})
        self.assertIs(direct["isError"], True)
        self.assertIn("Unknown tool: no_such_tool", json.loads(direct["content"][0]["text"])["error"])

        reply = self.serve(
            self.line(1, "tools/call", {"name": "no_such_tool", "arguments": {}}), server
        )[0]
        self.assertIs(reply["result"]["isError"], True)
        self.assertIn("Unknown tool", json.loads(reply["result"]["content"][0]["text"])["error"])

        for name in (5, None, ""):
            with self.subTest(name=name):
                result = server.call_tool(name, {})
                self.assertIs(result["isError"], True)
                self.assertIn("Unknown tool", json.loads(result["content"][0]["text"])["error"])

        # An unhashable name still fails closed, even if the wording is internal.
        unhashable = server.call_tool(["search_code"], {})
        self.assertIs(unhashable["isError"], True)

    def test_missing_required_arguments_become_error_results(self) -> None:
        server = self.make_server()
        self.write("hit.py", "needleword here\n")
        server.index_project()

        for name in ("search_code", "memory_write", "list_files"):
            with self.subTest(tool=name):
                result = server.call_tool(name, {})
                if name == "list_files":
                    self.assertNotIn("isError", result, "list_files has no required argument")
                    continue
                self.assertIs(result["isError"], True)
                message = json.loads(result["content"][0]["text"])["error"]
                self.assertIn("missing", message)

        reply = self.serve(
            self.line(1, "tools/call", {"name": "search_code"}), server
        )[0]
        self.assertIs(reply["result"]["isError"], True)
        self.assertIn("missing", json.loads(reply["result"]["content"][0]["text"])["error"])

    def test_extra_arguments_are_rejected(self) -> None:
        server = self.make_server()
        self.write("hit.py", "needleword here\n")
        server.index_project()

        result = server.call_tool("search_code", {"query": "needleword", "bogus": 1})
        self.assertIs(result["isError"], True)
        self.assertIn("unexpected keyword argument", json.loads(result["content"][0]["text"])["error"])

        reply = self.serve(
            self.line(1, "tools/call", {"name": "list_files", "arguments": {"limit": 5, "extra": True}}),
            server,
        )[0]
        self.assertIs(reply["result"]["isError"], True)

    def test_arguments_that_are_not_an_object_are_rejected(self) -> None:
        server = self.make_server()
        for arguments in ([1, 2], "search_code", None, 7):
            with self.subTest(arguments=arguments):
                result = server.call_tool("search_code", arguments)
                self.assertIs(result["isError"], True)
                self.assertIn("Tool arguments must be an object", json.loads(result["content"][0]["text"])["error"])

        reply = self.serve(
            self.line(1, "tools/call", {"name": "search_code", "arguments": ["query"]}), server
        )[0]
        self.assertIs(reply["result"]["isError"], True)

    def test_tools_call_with_non_object_params_reports_unknown_tool(self) -> None:
        """Current contract: unparseable params degrade into a tool error result."""
        server = self.make_server()
        reply = self.serve(self.line(1, "tools/call", [1, 2]), server)[0]

        self.assertEqual(reply["id"], 1)
        self.assertIs(reply["result"]["isError"], True)
        self.assertIn("Unknown tool", json.loads(reply["result"]["content"][0]["text"])["error"])

    def test_project_pointing_at_a_file_is_refused(self) -> None:
        target = self.write("target.txt", "plain file\n")
        server = self.make_server()

        with self.assertRaises(ValueError) as caught:
            server.index_project(str(target))
        self.assertIn("Project directory does not exist", str(caught.exception))

        reply = self.serve(
            self.line(1, "tools/call", {"name": "index_project", "arguments": {"project": str(target)}}),
            server,
        )[0]
        self.assertIs(reply["result"]["isError"], True)
        self.assertIn(
            "Project directory does not exist",
            json.loads(reply["result"]["content"][0]["text"])["error"],
        )
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "plain file\n")

    def test_project_pointing_at_a_missing_directory_is_refused(self) -> None:
        server = self.make_server()
        missing = self.workspace / "never-created"

        with self.assertRaises(ValueError) as caught:
            server.search_code("anything", project=str(missing))
        # Outside the session root: refused by name before any filesystem access.
        self.assertRegex(str(caught.exception), "does not exist|outside this session's root")

        for arguments in (
            {"project": str(missing)},
            {"project": str(missing / "deeper")},
            {"project": "relative-but-missing"},
        ):
            with self.subTest(arguments=arguments):
                reply = self.serve(
                    self.line(1, "tools/call", {"name": "list_files", "arguments": arguments}), server
                )[0]
                self.assertIs(reply["result"]["isError"], True)

    def test_project_symlink_loops_are_refused(self) -> None:
        self.write("inside.py", "needleword inside\n")
        server = self.make_server()

        self_loop = self.workspace / "self-loop"
        os.symlink(self_loop, self_loop)
        first = self.workspace / "loop-a"
        second = self.workspace / "loop-b"
        os.symlink(second, first)
        os.symlink(first, second)

        for candidate in (self_loop, first, second):
            with self.subTest(candidate=candidate.name):
                with self.assertRaises(ValueError) as caught:
                    server.index_project(str(candidate))
                self.assertRegex(str(caught.exception), "does not exist|outside this session's root")
                reply = self.serve(
                    self.line(1, "tools/call", {"name": "index_project", "arguments": {"project": str(candidate)}}),
                    server,
                )[0]
                self.assertIs(reply["result"]["isError"], True)

    def test_directory_outside_any_repository_is_fully_usable(self) -> None:
        self.write("alpha.py", "alpha needleword\n")
        self.write("docs/guide.md", "# guide\nneedleword docs\n")
        server = self.make_server()
        root = server.project_root()

        self.assertEqual(root, self.project.resolve())
        indexed = server.index_project()
        self.assertEqual(indexed["root"], str(root))
        self.assertEqual(indexed["indexed"], 2)
        self.assertEqual(indexed["skipped"], 0)
        self.assertEqual(server.list_files(), ["alpha.py", "docs/guide.md"])
        self.assertEqual(
            [hit["path"] for hit in server.search_code("needleword")],
            ["alpha.py", "docs/guide.md"],
        )
        self.assertEqual(
            server.git_status(),
            {"branch": None, "upstream": None, "ahead": 0, "behind": 0,
             "changes": [], "recent_commits": []},
        )
        # Outside a repository the enumeration falls back to a plain walk.
        self.assertEqual(
            sorted(path for path in index_server.project_files(root)),
            ["alpha.py", "docs/guide.md"],
        )


class FilesystemEdgeTests(EdgeCaseTests):
    def test_names_with_newlines_unicode_and_leading_dashes_index_and_search(self) -> None:
        names = [
            "weird\nnewline.txt",
            "héllo-日本語.py",
            "-rf.txt",
            "-- leading dash.md",
            "-",
        ]
        for name in names:
            self.write(name, f"content of needleword in {name!r}\n")
        server = self.make_server()

        result = server.index_project()

        self.assertEqual(result["indexed"], len(names), result)
        self.assertEqual(result["skipped"], 0)
        listed = server.list_files()
        self.assertEqual(sorted(listed), sorted(names))
        hits = server.search_code("needleword")
        self.assertEqual(sorted(hit["path"] for hit in hits), sorted(names))
        for hit in hits:
            self.assertFalse(Path(hit["path"]).is_absolute())
            self.assertNotIn("..", Path(hit["path"]).parts)
            self.assertEqual(hit["path"], json.loads(json.dumps(hit["path"])))

        self.assertEqual(server.list_files(pattern="-rf.txt"), ["-rf.txt"])
        self.assertEqual(server.list_files(pattern="-*.md"), ["-- leading dash.md"])

    def test_git_enumeration_keeps_odd_names_and_does_not_reindex(self) -> None:
        names = ["weird\nnewline.txt", "héllo-日本語.py", "-rf.txt", "-"]
        for name in names:
            self.write(name, f"needleword inside {name!r}\n")
        server = self.make_server()
        self.assertEqual(server.index_project()["indexed"], len(names))

        self.git("init", "-q", str(self.project), cwd=self.workspace)

        refreshed = server.index_project()
        self.assertEqual(refreshed["indexed"], len(names))
        self.assertEqual(refreshed["updated"], 0, "git enumeration must not invalidate the index")
        self.assertEqual(refreshed["removed"], 0)
        self.assertEqual(sorted(server.list_files()), sorted(names))
        self.assertEqual(
            sorted(hit["path"] for hit in server.search_code("needleword")), sorted(names)
        )

    def test_binary_detection_boundary_is_exactly_the_first_8_kib(self) -> None:
        tail = " boundaryword tail\n"
        # NUL at offset 8191 is still inside the first 8 KiB window.
        (self.project / "nul-at-8191.txt").write_bytes(b"a" * 8191 + b"\x00" + tail.encode())
        # NUL at offset 8192 is one byte past the window and stays text.
        (self.project / "nul-at-8192.txt").write_bytes(b"b" * 8192 + b"\x00" + tail.encode())
        server = self.make_server()

        result = server.index_project()

        self.assertEqual(result["indexed"], 1, result)
        self.assertEqual(result["skipped"], 1, result)
        self.assertEqual(server.list_files(), ["nul-at-8192.txt"])
        self.assertEqual(
            [hit["path"] for hit in server.search_code("boundaryword")], ["nul-at-8192.txt"]
        )

        root = server.project_root()
        sniffed_binary = self.project / "nul-at-8191.txt"
        sniffed_text = self.project / "nul-at-8192.txt"
        self.assertIsNone(
            index_server.read_text_file(root, "nul-at-8191.txt", sniffed_binary.stat())
        )
        self.assertIsInstance(
            index_server.read_text_file(root, "nul-at-8192.txt", sniffed_text.stat()), str
        )

    def test_files_of_exactly_512_kib_index_and_one_byte_more_is_skipped(self) -> None:
        exact_tail = b"exact boundary tailneedle\n"
        (self.project / "exact.txt").write_bytes(
            b"x" * (MAX_FILE_BYTES - len(exact_tail)) + exact_tail
        )
        over_tail = b"oversize boundary overneedle\n"
        (self.project / "over.txt").write_bytes(
            b"y" * (MAX_FILE_BYTES + 1 - len(over_tail)) + over_tail
        )
        self.assertEqual((self.project / "exact.txt").stat().st_size, MAX_FILE_BYTES)
        self.assertEqual((self.project / "over.txt").stat().st_size, MAX_FILE_BYTES + 1)
        server = self.make_server()

        result = server.index_project()

        self.assertEqual(result["indexed"], 1, result)
        self.assertEqual(result["skipped"], 1, result)
        self.assertEqual(server.list_files(), ["exact.txt"])
        self.assertEqual([hit["path"] for hit in server.search_code("tailneedle")], ["exact.txt"])
        self.assertEqual(server.search_code("overneedle"), [])
        # The oversized file is cached as skipped and not re-read on the next run.
        second = server.index_project()
        self.assertEqual(second["updated"], 0, second)
        self.assertEqual(second["skipped"], 1)

    def test_deleted_then_recreated_file_leaves_then_rejoins_the_index(self) -> None:
        self.write("cycle.txt", "cycle alphacontent\n")
        self.write("keeper.py", "keeper needleword\n")
        server = self.make_server()
        self.assertEqual(server.index_project()["indexed"], 2)
        self.assertEqual([hit["path"] for hit in server.search_code("alphacontent")], ["cycle.txt"])

        (self.project / "cycle.txt").unlink()
        removed = server.index_project()
        self.assertEqual(removed["removed"], 1)
        self.assertEqual(removed["indexed"], 1)
        self.assertEqual(server.list_files(), ["keeper.py"])
        self.assertEqual(server.search_code("alphacontent"), [])
        self.assertEqual([hit["path"] for hit in server.search_code("needleword")], ["keeper.py"])

        self.write("cycle.txt", "cycle betacontent rebuilt later\n")
        recreated = server.index_project()
        self.assertEqual(recreated["removed"], 0, recreated)
        self.assertEqual(recreated["updated"], 1)
        self.assertEqual(recreated["indexed"], 2)
        self.assertEqual(server.list_files(), ["cycle.txt", "keeper.py"])
        self.assertEqual([hit["path"] for hit in server.search_code("betacontent")], ["cycle.txt"])
        self.assertEqual(server.search_code("alphacontent"), [])


class SearchQueryEdgeTests(EdgeCaseTests):
    def setUp(self) -> None:
        super().setUp()
        self.write("alpha.py", "alpha only text\n")
        self.write("needle.py", "needleword here\n")
        self.server = self.make_server()
        self.server.index_project()

    def test_operator_and_punctuation_queries_return_plain_lists(self) -> None:
        hostile = [
            '"',
            "''",
            "AND",
            "OR",
            "NOT",
            "NEAR",
            "NEAR(a b)",
            "(a OR b)",
            "*",
            "-",
            "-rf",
            "^caret",
            "col1:val1",
            "{}",
            "[",
            "(",
            ")",
            "\\",
            "%",
            "_",
            '"needleword',
            "needleword'",
            "needle\x00word",
            "   ",
        ]
        for query in hostile:
            with self.subTest(query=query):
                result = self.server.search_code(query)
                self.assertIsInstance(result, list)
                for hit in result:
                    self.assertIn(hit["path"], {"alpha.py", "needle.py"})

        # Operators are plain text now: "OR" is a substring search, and the fixture
        # legitimately contains "or", so it is only checked above for safety.
        for query in ('"', "AND", "NOT", "NEAR", "*", "-", "^caret", "col1:val1"):
            with self.subTest(unmatched=query):
                self.assertEqual(self.server.search_code(query), [])

        # Hostile input must not poison the index.
        self.assertEqual(
            [hit["path"] for hit in self.server.search_code("needleword")], ["needle.py"]
        )

    def test_unicode_queries_match_unicode_content(self) -> None:
        unicode_project = self.workspace / "unicode-project"
        self.write("unicode.txt", "日本語 привет café straße needleword\n", root=unicode_project)
        server = self.make_server(unicode_project)
        server.index_project()

        for query in ("日本語", "привет", "café", "CAFÉ", "straße", "needleword"):
            with self.subTest(query=query):
                self.assertEqual(
                    [hit["path"] for hit in server.search_code(query)], ["unicode.txt"]
                )

        # Partial tokens currently miss (FTS tokenizer semantics); the
        # mode-disagreement expectedFailure below covers that bug.
        self.assertIsInstance(server.search_code("日本"), list)

    def test_ten_thousand_character_query_returns_an_empty_list_quickly(self) -> None:
        giant = "q" * 10000
        started = time.monotonic()
        result = self.server.search_code(giant)
        elapsed = time.monotonic() - started

        self.assertIsInstance(result, list)
        self.assertEqual(result, [])
        self.assertLess(elapsed, 30.0, "an oversized token must not stall search")

        mixed = self.server.search_code("needleword " + giant)
        self.assertIsInstance(mixed, list)
        self.assertEqual(mixed, [])
        self.assertEqual(
            [hit["path"] for hit in self.server.search_code("needleword")], ["needle.py"],
            "the index must survive an oversized query",
        )

    def test_substring_queries_agree_between_fts5_and_like_modes(self) -> None:
        """BUG: index_server.search_rows (src/ocdeck/index_server.py:240-250).

        In fts5 mode a query is sent to ``documents MATCH`` and the function
        returns whatever the tokenizer produces, with no fallback to the
        substring scan below it. Prefixes ("needle" for "needleword"),
        partial unicode words ("日本") and symbols the unicode61 tokenizer
        drops ("🚀") therefore return [] in fts5 mode, while the very same
        project indexed in "like" mode (fts5 unavailable) returns the file,
        because ``like_pattern`` and the post-filter in ``search_code`` both
        implement substring semantics. Suggested fix: fall back to the LIKE
        scan when the MATCH query yields no rows (or append '*' to each token)
        so both modes return the same hits.
        """
        content = "launch 🚀 needleword 日本語\n"
        fts_project = self.workspace / "fts-project"
        like_project = self.workspace / "like-project"
        self.write("sample.txt", content, root=fts_project)
        self.write("sample.txt", content, root=like_project)

        fts_server = self.make_server(fts_project)
        fts_server.index_project()
        like_server = self.make_server(like_project)

        class NoFts5Connection(sqlite3.Connection):
            def execute(self, sql, *parameters, **kwargs):
                if sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
                    raise sqlite3.OperationalError("no such module: fts5")
                return super().execute(sql, *parameters, **kwargs)

        original_connect = index_server.sqlite3.connect

        def connect_without_fts5(*args, **kwargs):
            kwargs.setdefault("factory", NoFts5Connection)
            return original_connect(*args, **kwargs)

        with mock.patch.object(index_server.sqlite3, "connect", connect_without_fts5):
            like_server.index_project()

        for query in ("needle", "🚀", "日本"):
            with self.subTest(query=query):
                self.assertEqual(
                    fts_server.search_code(query),
                    like_server.search_code(query),
                    f"search results for {query!r} depend on the index search mode",
                )


class MemoryEdgeTests(EdgeCaseTests):
    def test_memory_text_at_the_length_boundary(self) -> None:
        server = self.make_server()

        accepted = server.memory_write("a" * 20000, project=str(self.project))
        self.assertIsInstance(accepted["id"], int)
        stored = server.memory_search(project=str(self.project), limit=5)
        self.assertEqual([entry["text"] for entry in stored], ["a" * 20000])

        with self.assertRaises(ValueError) as caught:
            server.memory_write("b" * 20001, project=str(self.project))
        self.assertIn("20000", str(caught.exception))

        over = server.call_tool(
            "memory_write", {"text": "c" * 20001, "project": str(self.project)}
        )
        self.assertIs(over["isError"], True)
        self.assertIn("20000", json.loads(over["content"][0]["text"])["error"])

        just_right = server.call_tool(
            "memory_write", {"text": "d" * 20000, "project": str(self.project)}
        )
        self.assertNotIn("isError", just_right)
        self.assertEqual(
            sorted(len(entry["text"]) for entry in server.memory_search(project=str(self.project))),
            [20000, 20000],
        )

    def test_tags_that_are_not_a_list_of_strings_are_rejected(self) -> None:
        server = self.make_server()
        for tags in ("tag", {"a": 1}, 7, ["ok", 2], [None], [["nested"]]):
            with self.subTest(tags=tags):
                with self.assertRaises(ValueError) as caught:
                    server.memory_write("text", project=str(self.project), tags=tags)
                self.assertIn("tags must be a list of strings", str(caught.exception))

                result = server.call_tool(
                    "memory_write", {"text": "text", "project": str(self.project), "tags": tags}
                )
                self.assertIs(result["isError"], True)
                self.assertIn(
                    "tags must be a list of strings",
                    json.loads(result["content"][0]["text"])["error"],
                )

        stored = server.call_tool(
            "memory_write", {"text": "text", "project": str(self.project), "tags": ["a", "b"]}
        )
        self.assertNotIn("isError", stored)
        entry = server.memory_search("text", project=str(self.project))[0]
        self.assertEqual(entry["tags"], ["a", "b"])

    def test_concurrent_writers_share_one_memory_database(self) -> None:
        server = self.make_server()
        scope = str(self.project.resolve())
        writers, readers, failures = [], [], []
        start = threading.Barrier(7)

        def write(worker: int) -> None:
            try:
                start.wait(timeout=30)
                for item in range(4):
                    writers.append(
                        server.memory_write(
                            f"worker {worker} entry {item}", project=scope, tags=[f"w{worker}"]
                        )["id"]
                    )
            except BaseException as error:  # surfaced by the assertions below
                failures.append(error)

        def read() -> None:
            try:
                start.wait(timeout=30)
                for _ in range(6):
                    readers.append(len(server.memory_search(project=scope, limit=1000)))
            except BaseException as error:
                failures.append(error)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(6)]
        threads.append(threading.Thread(target=read))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
        for thread in threads:
            self.assertFalse(thread.is_alive(), "memory writer thread hung")

        self.assertEqual(failures, [])
        self.assertEqual(len(writers), 24, writers)
        self.assertEqual(len(set(writers)), 24, "every insert must get its own row id")
        # Readers run while the writers insert: counts only grow and stay bounded.
        self.assertTrue(all(0 <= count <= 24 for count in readers), readers)
        self.assertEqual(readers, sorted(readers), readers)
        self.assertEqual(
            sorted(entry["id"] for entry in server.memory_search(project=scope, limit=1000)),
            sorted(writers),
        )

    def test_memory_database_swapped_for_a_symlink_is_refused(self) -> None:
        server = self.make_server()
        server.memory_write("first note", project=str(self.project))
        database_path = self.hub / "memory.sqlite"
        self.assertTrue(database_path.is_file())

        victim = self.workspace / "victim.txt"
        victim.write_text("DO NOT TOUCH\n", encoding="utf-8")
        database_path.unlink()
        os.symlink(victim, database_path)

        for call in (
            lambda: server.memory_search("first", project=str(self.project)),
            lambda: server.memory_write("second note", project=str(self.project)),
        ):
            with self.assertRaises(ValueError) as caught:
                call()
            self.assertIn("must not be a symlink", str(caught.exception))

        result = server.call_tool("memory_search", {"query": "first"})
        self.assertIs(result["isError"], True)
        self.assertIn("must not be a symlink", json.loads(result["content"][0]["text"])["error"])

        self.assertTrue(victim.exists())
        self.assertEqual(victim.read_text(encoding="utf-8"), "DO NOT TOUCH\n")

        # A dangling symlink target must not be created by a failed call.
        database_path.unlink()
        missing = self.workspace / "would-be-created.sqlite"
        os.symlink(missing, database_path)
        with self.assertRaises(ValueError):
            server.memory_search(project=str(self.project))
        self.assertFalse(missing.exists())


class IndexStorageEdgeTests(EdgeCaseTests):
    def test_index_database_swapped_for_a_symlink_is_refused(self) -> None:
        self.write("code.py", "needleword source\n")
        server = self.make_server()
        server.index_project()
        root = server.project_root()
        database_path = server.index_path(root)
        self.assertTrue(database_path.is_file())

        victim = self.workspace / "victim.txt"
        victim.write_text("DO NOT TOUCH\n", encoding="utf-8")
        database_path.unlink()
        os.symlink(victim, database_path)

        for call in (
            lambda: server.index_project(),
            lambda: server.search_code("needleword"),
            lambda: server.list_files(),
            lambda: index_server.IndexServer(cwd=self.project).ensure_index(root),
        ):
            with self.assertRaises(ValueError) as caught:
                call()
            self.assertIn("must not be a symlink", str(caught.exception))

        reply = self.serve(self.line(1, "tools/call", {"name": "list_files", "arguments": {}}))[0]
        self.assertIs(reply["result"]["isError"], True)
        self.assertIn(
            "must not be a symlink",
            json.loads(reply["result"]["content"][0]["text"])["error"],
        )

        # git_status never touches the index database, so it keeps working.
        self.assertIn("changes", server.git_status())
        self.assertEqual(victim.read_text(encoding="utf-8"), "DO NOT TOUCH\n")

    def test_transient_read_failure_keeps_the_previous_index_entry(self) -> None:
        """BUG: index_project drops files whose read fails (src/ocdeck/index_server.py:320-324).

        When ``read_text_file`` raises OSError (its documented "file changed
        while being indexed" race, or a transient EACCES) for a file that is
        already indexed, the handler counts it as ``skipped`` but never adds it
        to ``present``, so the row and its documents are deleted by the
        ``previous.keys() - present`` sweep below. The same file is therefore
        reported as both ``skipped`` and ``removed``, disappears from
        ``list_files``/``search_code``, and stays invisible for up to
        INDEX_MAX_AGE because ``ensure_index`` only refreshes a stale index.
        Suggested fix: on OSError keep the previous row (``present.add`` and,
        when it was indexed, keep it in ``current``) so a transient failure is
        counted as skipped alone and the stale content remains searchable.
        """
        self.write("alpha.py", "alpha needleword\n")
        self.write("beta.py", "beta needleword\n")
        server = self.make_server()
        self.assertEqual(server.index_project()["indexed"], 2)

        with open(self.project / "alpha.py", "a", encoding="utf-8") as handle:
            handle.write("touched\n")
        metadata = (self.project / "alpha.py").stat()
        os.utime(self.project / "alpha.py", ns=(metadata.st_mtime_ns + 10**9,) * 2)

        original_read = index_server.read_text_file

        def failing_read(root, relative, file_metadata):
            if relative == "alpha.py":
                raise OSError("simulated: file changed while being indexed")
            return original_read(root, relative, file_metadata)

        with mock.patch.object(index_server, "read_text_file", failing_read):
            result = server.index_project()

        self.assertEqual(result["removed"], 0, result)
        self.assertEqual(result["skipped"], 1, result)
        self.assertEqual(sorted(server.list_files()), ["alpha.py", "beta.py"])
        self.assertEqual(
            sorted(hit["path"] for hit in server.search_code("needleword")),
            ["alpha.py", "beta.py"],
        )


class GitStatusEdgeTests(EdgeCaseTests):
    def test_repository_without_any_commit_reports_an_unborn_branch(self) -> None:
        self.git("init", "-q", str(self.project), cwd=self.workspace)
        self.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.project)
        self.write("loose.txt", "untracked\n")

        status = self.make_server().git_status()

        expected = self.git("symbolic-ref", "--short", "HEAD", cwd=self.project)
        self.assertEqual(status["branch"], expected.stdout.decode().strip())
        self.assertIsNone(status["upstream"])
        self.assertEqual(status["ahead"], 0)
        self.assertEqual(status["behind"], 0)
        self.assertEqual(status["changes"], ["?? loose.txt"])
        self.assertEqual(status["recent_commits"], [], "git log fails on an empty history")
        self.assertEqual(set(status), {"branch", "upstream", "ahead", "behind",
                                       "changes", "recent_commits"})

    def test_detached_head_reports_no_branch_but_keeps_commits_and_changes(self) -> None:
        self.build_repository(self.project)
        self.git("checkout", "--detach", "HEAD", cwd=self.project)
        self.write("tracked.txt", "one\ntwo\n")
        self.write("loose.txt", "new\n")

        status = self.make_server().git_status()

        self.assertIsNone(status["branch"])
        self.assertIsNone(status["upstream"])
        self.assertEqual(status["ahead"], 0)
        self.assertEqual(status["behind"], 0)
        self.assertTrue(status["recent_commits"], status)
        self.assertEqual(len(status["recent_commits"][0].split(" ", 1)), 2)
        self.assertIn(" M tracked.txt", status["changes"])
        self.assertIn("?? loose.txt", status["changes"])

    def install_hostile_config(self, repository: Path) -> list[Path]:
        """core.fsmonitor, core.pager and hooksPath all point at marker makers."""
        markers = [
            self.workspace / "marker-fsmonitor",
            self.workspace / "marker-pager",
            self.workspace / "marker-hook",
        ]
        evil = self.workspace / "evil-command.sh"
        evil.write_text(
            "#!/bin/sh\n"
            f"touch {markers[0]}\n"
            f"touch {markers[1]}\n"
            "echo '{\"ok\": true}'\n"
            "exit 0\n",
            encoding="utf-8",
        )
        evil.chmod(0o755)
        hooks = self.workspace / "hostile-hooks"
        hooks.mkdir(exist_ok=True)
        hook = hooks / "post-index-change"
        hook.write_text(f"#!/bin/sh\ntouch {markers[2]}\n", encoding="utf-8")
        hook.chmod(0o755)

        config = repository / ".git" / "config"
        config.write_text(
            config.read_text(encoding="utf-8")
            + "\n[core]\n"
            + f"\tfsmonitor = {evil}\n"
            + f"\tpager = {evil}\n"
            + f"\thooksPath = {hooks}\n"
            + "[diff]\n"
            + f"\texternal = {evil}\n",
            encoding="utf-8",
        )
        return markers

    def test_hostile_git_config_is_never_executed_by_index_or_status_calls(self) -> None:
        self.build_repository(self.project)
        self.write("tracked.txt", "one\ntwo\nneedleword edit\n")
        self.write("extra.py", "needleword extra\n")
        markers = self.install_hostile_config(self.project)
        server = self.make_server()

        # The command payload itself works when it really runs, so silence is
        # meaningful. (The hook marker only fires when git refreshes the index.)
        subprocess.run(
            ["/bin/sh", str(self.workspace / "evil-command.sh")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        self.assertTrue(
            all(marker.exists() for marker in markers[:2]),
            "marker payload is broken",
        )
        for marker in markers:
            marker.unlink(missing_ok=True)

        status = server.git_status()
        indexed = server.index_project()
        hits = server.search_code("needleword")
        listed = server.list_files()
        server.memory_write("hostile config note", project=str(self.project))

        for marker in markers:
            self.assertFalse(marker.exists(), f"{marker.name} was executed by the server")

        self.assertEqual(status["branch"], "main")
        self.assertIsNone(status["upstream"])
        self.assertEqual(len(status["recent_commits"]), 1, status)
        self.assertTrue(status["changes"], status)
        self.assertEqual(indexed["root"], str(self.project.resolve()))
        self.assertEqual(indexed["indexed"], 2)
        self.assertEqual(sorted(hit["path"] for hit in hits), ["extra.py", "tracked.txt"])
        self.assertEqual(sorted(listed), ["extra.py", "tracked.txt"])

    def test_hostile_config_actually_fires_without_the_guard(self) -> None:
        """Proof the guard is load-bearing: a plain git status runs the payload."""
        self.build_repository(self.project)
        self.write("tracked.txt", "one\ntwo\n")
        markers = self.install_hostile_config(self.project)

        try:
            result = subprocess.run(
                ["git", "status", "--porcelain=v1"],
                cwd=str(self.project),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.skipTest("unguarded git status timed out on this platform")

        fired = [marker for marker in markers if marker.exists()]
        if not fired:
            self.skipTest("this git never executes core.fsmonitor/hooksPath for status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(fired)


if __name__ == "__main__":
    unittest.main()


class InsideRootLoopTests(EdgeCaseTests):
    def test_symlink_loops_inside_the_project_are_refused_cleanly(self) -> None:
        server = self.make_server()
        loop = self.project / "loop"
        os.symlink(loop, loop)
        a, b = self.project / "a", self.project / "b"
        os.symlink(b, a)
        os.symlink(a, b)
        for candidate in ("loop", "a", str(b)):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                server.index_project(candidate)

