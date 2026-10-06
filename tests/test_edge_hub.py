"""Edge cases for ocdeck.hub: JSONC, managed blocks, file safety, sync and handoff.

Every test here is hermetic: ``HOME``, ``XDG_CONFIG_HOME``, ``OCDECK_HUB_DIR``
and ``CODEX_HOME`` point at a temporary tree, ``claude`` is a fake CLI and git is
a stub, so nothing outside ``tempfile.mkdtemp()`` is ever read or written.
"""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ocdeck import hub
from ocdeck.hub import (
    CLAUDE_BEGIN, CLAUDE_END, CODEX_BEGIN, CODEX_END, INDEX_SERVER, MCP_BLOCK,
    main, parse_jsonc, plan_actions, strip_jsonc, write_handoff,
)
from ocdeck.index_server import IndexServer
from ocdeck.models import SessionRecord

SERVER = {"command": ["uvx", "docs-mcp"], "env": {"TOKEN": "t"}}


class FakeClaudeCli:
    """Stand-in for ``subprocess.run`` that mimics ``claude mcp add-json``."""

    def __init__(self, settings_path: Path) -> None:
        self.settings_path = settings_path
        self.calls: list[list[str]] = []
        self.fail = False

    def __call__(self, argv, **kwargs):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, b"", b"nope")
        if argv[1:3] == ["mcp", "add-json"]:
            payload = json.loads(argv[-1])
            try:
                data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data.setdefault("mcpServers", {})[argv[-2]] = payload
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    @property
    def names(self) -> list[str]:
        return [call[-2] for call in self.calls if call[1:3] == ["mcp", "add-json"]]


def fake_git(rev_parse: tuple[int, bytes], branch: str = "main", status: str = "",
             diff: str = "", log: str = "abc1234 first commit", errors: bool = False):
    """A ``subprocess.run`` replacement for :func:`hub._git` (never runs real git)."""
    seen: list[str] = []

    def run(argv, **kwargs):
        argv = [str(part) for part in argv]
        seen.append(" ".join(argv[3:]))
        if errors:
            raise OSError("git missing")
        if seen[-1] == "rev-parse --is-inside-work-tree":
            return subprocess.CompletedProcess(argv, rev_parse[0], rev_parse[1], b"")
        if seen[-1] == "rev-parse --abbrev-ref HEAD":
            return subprocess.CompletedProcess(argv, 0, branch.encode() + b"\n", b"")
        if seen[-1] == "status --porcelain=v1":
            return subprocess.CompletedProcess(argv, 0, status.encode(), b"")
        if seen[-1] == "diff --stat":
            return subprocess.CompletedProcess(argv, 0, diff.encode(), b"")
        if seen[-1].startswith("log"):
            return subprocess.CompletedProcess(argv, 0, log.encode(), b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    return run, seen


def make_transcript(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return path


def make_opencode_db(path: Path, session_id: str, messages: list[dict]) -> Path:
    """A V2-shaped session DB: ``session_message`` rows plus ``part`` rows.

    Message dicts are chronological; ``data`` holds the inline payload and
    ``parts`` the separate part rows, exactly like the real OpenCode DB.
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
            "type TEXT NOT NULL, seq INTEGER NOT NULL, time_created INTEGER NOT NULL, "
            "time_updated INTEGER NOT NULL, data TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, "
            "session_id TEXT NOT NULL, time_created INTEGER NOT NULL, "
            "time_updated INTEGER NOT NULL, data TEXT NOT NULL)"
        )
        for index, message in enumerate(messages):
            message_id = f"msg_{index:04d}"
            connection.execute(
                "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    message.get("session", session_id),
                    message["type"],
                    message.get("seq", index),
                    message.get("time", 1_000_000 + index),
                    1_000_000 + index,
                    json.dumps(message.get("data", {})),
                ),
            )
            for part_index, part in enumerate(message.get("parts", ())):
                connection.execute(
                    "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        f"prt_{index:04d}_{part_index:03d}",
                        message_id,
                        message.get("session", session_id),
                        1_000_000 + index * 100 + part_index,
                        1_000_000 + index * 100 + part_index,
                        json.dumps(part),
                    ),
                )
        connection.commit()
    finally:
        connection.close()
    return path


# --- JSONC --------------------------------------------------------------------

class JsoncEdgeTests(unittest.TestCase):
    def test_comment_markers_inside_strings_are_kept(self):
        text = (
            '{\n'
            '  "a": "http://x//y/* z */",\n'
            '  "b": "end /*",\n'
            '  "//": "not a comment",\n'
            '  "c": "quote \\" then // still a string",\n'
            '  "d": 1 // a real comment\n'
            '}\n'
        )
        payload = parse_jsonc(text)
        self.assertEqual(payload["a"], "http://x//y/* z */")
        self.assertEqual(payload["b"], "end /*")
        self.assertEqual(payload["//"], "not a comment")
        self.assertEqual(payload["c"], 'quote " then // still a string')
        self.assertEqual(payload["d"], 1)

    def test_escaped_backslash_does_not_swallow_the_closing_quote(self):
        payload = parse_jsonc('{"a": "back\\\\", "b": "x // y"}')
        self.assertEqual(payload["a"], "back\\")
        self.assertEqual(payload["b"], "x // y")

    def test_escaped_quote_ends_the_escaped_string_and_the_comment_starts(self):
        text = '{"a": "say \\"hi\\" // gone", "b": 2}'
        self.assertEqual(parse_jsonc(text), {"a": 'say "hi" // gone', "b": 2})

    def test_block_comment_ends_at_the_first_terminator(self):
        text = '{\n  /* outer /* inner */ "a": 1,\n  "b": [1, 2],\n}\n'
        self.assertEqual(parse_jsonc(text), {"a": 1, "b": [1, 2]})

    def test_unterminated_block_comment_swallows_the_rest(self):
        self.assertEqual(parse_jsonc('{"a": 1} /* trailing'), {"a": 1})
        with self.assertRaises(ValueError):
            parse_jsonc('{"a": 1 /* never closed')

    def test_trailing_commas_in_nested_containers(self):
        text = '{\n  "a": [1, 2, ],\n  "b": {"c": [3,], "d": {"e": 1,},},\n  "f": [ ],\n}\n'
        self.assertEqual(parse_jsonc(text), {"a": [1, 2], "b": {"c": [3], "d": {"e": 1}}, "f": []})

    def test_trailing_comma_before_a_comment_and_a_closer(self):
        self.assertEqual(parse_jsonc('{\n  "a": 1, // why\n}\n'), {"a": 1})
        self.assertEqual(parse_jsonc('[1, 2, /* why */ ]'), [1, 2])
        self.assertEqual(parse_jsonc('{"a": [1, /* why */ ]}'), {"a": [1]})
        with self.assertRaises(ValueError):  # two roots, so the whole text is invalid
            parse_jsonc('{\n  "a": 1, // why\n}\n[1, 2,]\n')

    def test_crlf_and_cr_line_endings(self):
        text = '{\r\n  // c\r\n  "a": "x//y",\r\n  "b": [1,],\r\n}\r\n'
        self.assertEqual(parse_jsonc(text), {"a": "x//y", "b": [1]})
        self.assertEqual(parse_jsonc('{\r  "a": 1,\r}'), {"a": 1})

    def test_empty_and_whitespace_only_text(self):
        self.assertEqual(strip_jsonc(""), "")
        self.assertEqual(strip_jsonc("   \n\t "), "   \n\t ")
        for text in ("", "   ", "// only a comment", "/* only a comment */"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_jsonc(text)

    def test_lone_slash_and_star_are_left_alone(self):
        with self.assertRaises(ValueError):
            parse_jsonc('{"a": 1} /')
        self.assertEqual(strip_jsonc("a / b"), "a / b")
        self.assertEqual(parse_jsonc('{"a": "*/"}'), {"a": "*/"})

    def test_non_object_roots_parse_but_read_jsonc_returns_a_dict(self):
        for text in ("[]", "null", '"text"', "42", "true", '{"a": 1}'):
            with tempfile.TemporaryDirectory() as base:
                path = Path(base) / "c.json"
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text):
                    self.assertIsInstance(parse_jsonc(text), type(parse_jsonc(text)))
                    expected = parse_jsonc(text) if isinstance(parse_jsonc(text), dict) else {}
                    self.assertEqual(hub.read_jsonc(path), expected)

    def test_read_jsonc_on_a_missing_file_or_directory_is_empty(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            self.assertEqual(hub.read_jsonc(root / "nope.json"), {})
            self.assertEqual(hub.read_jsonc(root), {})  # a directory raises OSError

    def test_strip_jsonc_leaves_clean_json_untouched(self):
        text = json.dumps({"mcp": {"a": {"command": ["x", "//y"]}}}, indent=2)
        self.assertEqual(strip_jsonc(text), text)
        self.assertEqual(strip_jsonc(strip_jsonc(text)), strip_jsonc(text))

    def test_file_text_swallows_undecodable_bytes(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "bin"
            path.write_bytes(b'{"a": "\xff\xfe"}')
            self.assertEqual(hub.file_text(path), "")
            # read_jsonc only guards OSError, so the caller must be ready for
            # ValueError: import_opencode_servers and opencode_actions both are.
            with self.assertRaises(ValueError):
                hub.read_jsonc(path)

    def test_utf8_bom_is_tolerated(self):
        """REAL BUG: a UTF-8 BOM makes json.loads fail ("Unexpected UTF-8 BOM"), so
        a BOM-prefixed opencode.jsonc is never parsed and never synced.
        Fix: decode with ``utf-8-sig`` in file_text/read_jsonc, or drop a leading
        "\\ufeff" in strip_jsonc (hub.py:170)."""
        payload = parse_jsonc('﻿{\n  "mcp": {}\n}\n')
        self.assertEqual(payload, {"mcp": {}})
        self.assertEqual(strip_jsonc('﻿{}'), "{}")


# --- managed blocks -----------------------------------------------------------

class BlockEdgeTests(unittest.TestCase):
    def test_bounds_need_both_markers(self):
        self.assertIsNone(hub.block_bounds("intro\n", CLAUDE_BEGIN, CLAUDE_END))
        self.assertIsNone(hub.block_bounds(f"{CLAUDE_BEGIN}\nx\n", CLAUDE_BEGIN, CLAUDE_END))
        self.assertIsNone(hub.block_bounds(f"x\n{CLAUDE_END}\n", CLAUDE_BEGIN, CLAUDE_END))
        text = f"a\n{CLAUDE_BEGIN}\nx\n{CLAUDE_END}\nb\n"
        start, stop = hub.block_bounds(text, CLAUDE_BEGIN, CLAUDE_END)
        self.assertEqual(text[start:stop], hub.block_text(CLAUDE_BEGIN, CLAUDE_END, "x\n"))
        # No trailing newline: the block stops at the end marker itself.
        text = f"{CLAUDE_BEGIN}\nx\n{CLAUDE_END}"
        self.assertEqual(hub.block_bounds(text, CLAUDE_BEGIN, CLAUDE_END), (0, len(text)))

    def test_end_marker_before_begin_is_not_mistaken_for_a_block(self):
        text = f"prose {CLAUDE_END} more\n{CLAUDE_BEGIN}\n@x\n{CLAUDE_END}\n"
        out = hub.upsert_block(text, CLAUDE_BEGIN, CLAUDE_END, "@y\n")
        self.assertIn("prose", out)
        self.assertIn("@y", out)
        self.assertEqual(out.count(CLAUDE_BEGIN), 1)
        self.assertEqual(hub.upsert_block(out, CLAUDE_BEGIN, CLAUDE_END, "@y\n"), out)

    def test_duplicate_blocks_converge_and_keep_user_text(self):
        text = (
            f"intro\n{CLAUDE_BEGIN}\n@old1\n{CLAUDE_END}\n"
            f"between\n{CLAUDE_BEGIN}\n@old2\n{CLAUDE_END}\noutro\n"
        )
        once = hub.upsert_block(text, CLAUDE_BEGIN, CLAUDE_END, "@new\n")
        self.assertIn("between", once)
        self.assertIn("outro", once)
        self.assertIn("@new", once)
        self.assertEqual(hub.upsert_block(once, CLAUDE_BEGIN, CLAUDE_END, "@new\n"), once)

    def test_remove_block_removes_only_the_managed_region(self):
        text = f"a\n{CLAUDE_BEGIN}\n@x\n{CLAUDE_END}\nb\n"
        self.assertEqual(hub.remove_block(text, CLAUDE_BEGIN, CLAUDE_END), "a\nb\n")
        self.assertEqual(hub.remove_block("a\nb\n", CLAUDE_BEGIN, CLAUDE_END), "a\nb\n")

    def test_inline_markers_in_prose_are_left_alone(self):
        # Only whole-line markers delimit the block; a sentence that mentions
        # them is user prose and must survive untouched.
        text = f"rules: {CLAUDE_BEGIN} see {CLAUDE_END} for details\n"
        out = hub.upsert_block(text, CLAUDE_BEGIN, CLAUDE_END, "@x\n")
        self.assertTrue(out.startswith(text))
        self.assertIn(f"{CLAUDE_BEGIN}\n@x\n{CLAUDE_END}\n", out)
        self.assertEqual(hub.upsert_block(out, CLAUDE_BEGIN, CLAUDE_END, "@x\n"), out)

    def test_crlf_and_missing_trailing_newline(self):
        out = hub.upsert_block("a\r\nb", CLAUDE_BEGIN, CLAUDE_END, "@x\n")
        self.assertTrue(out.startswith("a\r\nb\n"))
        self.assertIn(f"{CLAUDE_BEGIN}\n@x\n{CLAUDE_END}\n", out)
        self.assertEqual(hub.upsert_block(out, CLAUDE_BEGIN, CLAUDE_END, "@x\n"), out)

    def test_empty_body_round_trips(self):
        out = hub.upsert_block("", CODEX_BEGIN, CODEX_END, "")
        self.assertEqual(out, f"{CODEX_BEGIN}\n\n{CODEX_END}\n")
        self.assertEqual(hub.upsert_block(out, CODEX_BEGIN, CODEX_END, ""), out)
        self.assertEqual(hub.remove_block(out, CODEX_BEGIN, CODEX_END), "")

    def test_orphan_begin_marker_does_not_eat_user_text_on_the_next_sync(self):
        """REAL BUG: an unclosed ``<!-- ocdeck-hub:begin -->`` (a hand edit or a
        crashed sync) makes block_bounds return None, so upsert_block *appends* a
        second block; the next sync then pairs the orphan begin with the new end
        marker and silently deletes everything in between. Fix: when the begin
        marker has no end, treat the orphan region as removable instead of
        appending a duplicate block (hub.py:293)."""
        text = f"intro\n{CLAUDE_BEGIN}\nUSER DATA\n"
        once = hub.upsert_block(text, CLAUDE_BEGIN, CLAUDE_END, "@/hub/AGENTS.md\n")
        twice = hub.upsert_block(once, CLAUDE_BEGIN, CLAUDE_END, "@/hub/AGENTS.md\n")
        self.assertEqual(twice.count(CLAUDE_BEGIN), 1)
        self.assertIn("USER DATA", twice)

    def test_a_body_containing_the_end_marker_still_converges(self):
        """REAL BUG: block_bounds stops at the *first* end marker, so a managed body
        that quotes the marker (a shared instructions file documenting the block)
        grows the file by one duplicated line on every sync and never converges.
        Fix: reject or escape end markers inside the body, or match the end marker
        only outside the body (hub.py:293)."""
        body = f"# rules\n{CODEX_END} is the closer\n"
        once = hub.upsert_block("model = 1\n", CODEX_BEGIN, CODEX_END, body)
        twice = hub.upsert_block(once, CODEX_BEGIN, CODEX_END, body)
        self.assertEqual(twice, once)


# --- files and backups --------------------------------------------------------

class FileSafetyTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)

    def test_symlinked_config_keeps_the_link(self):
        """REAL BUG: atomic_write opens ``<name>.ocdeck-tmp`` and os.replace()s it
        over the path, which replaces a *symlink* with a regular file: the target is
        left stale and the link (often a dotfiles entry) is destroyed. Fix: resolve
        symlinks before writing, or refuse and report a manual action (hub.py:269)."""
        target = self.base / "dotfiles" / "opencode.jsonc"
        target.parent.mkdir(parents=True)
        target.write_text('{"mcp": {}}\n', encoding="utf-8")
        link = self.base / "opencode.jsonc"
        link.symlink_to(target)
        hub.atomic_write(link, '{\n  "mcp": {\n    "a": {}\n  }\n}\n')
        self.assertTrue(link.is_symlink(), "the symlink was replaced by a regular file")
        self.assertEqual(link.resolve(), target)
        self.assertEqual(parse_jsonc(target.read_text())["mcp"], {"a": {}})

    def test_read_only_config_is_not_clobbered(self):
        """REAL BUG: os.replace() only needs write permission on the *directory*,
        so atomic_write overwrites a 0o444 config (and even restores the read-only
        mode on the new file) instead of reporting a manual step. Fix: in
        atomic_write, check ``os.access(path, os.W_OK)`` for an existing file and
        raise OSError so sync reports the failure (hub.py:269)."""
        path = self.base / "CLAUDE.md"
        path.write_text("# My notes\n", encoding="utf-8")
        path.chmod(0o444)
        self.addCleanup(path.chmod, 0o644)
        with self.assertRaises(OSError):
            hub.atomic_write(path, "clobbered\n")
        self.assertEqual(path.read_text(), "# My notes\n")

    def test_backups_are_never_overwritten(self):
        path = self.base / "CLAUDE.md"
        path.write_text("v1\n", encoding="utf-8")
        backup = path.with_name(path.name + hub.BACKUP_SUFFIX)
        self.assertEqual(hub.backup_file(path), backup)
        self.assertEqual(backup.read_text(), "v1\n")
        for text in ("v2\n", "v3\n"):
            hub.atomic_write(path, text)
            self.assertEqual(backup.read_text(), "v1\n")
        self.assertIsNone(hub.backup_file(path))  # an existing backup is never recopied
        self.assertEqual(backup.read_text(), "v1\n")

    def test_a_pre_existing_foreign_backup_survives(self):
        path = self.base / "config.toml"
        path.write_text("mine\n", encoding="utf-8")
        backup = path.with_name(path.name + hub.BACKUP_SUFFIX)
        backup.write_text("from another tool\n", encoding="utf-8")
        hub.atomic_write(path, "rewritten\n")
        self.assertEqual(backup.read_text(), "from another tool\n")

    def test_no_backup_for_a_missing_file_and_no_temp_file_left_behind(self):
        path = self.base / "CLAUDE.md"
        self.assertIsNone(hub.backup_file(path))
        self.assertEqual(list(self.base.iterdir()), [])
        hub.atomic_write(path, "hello\n")
        self.assertEqual(path.read_text(), "hello\n")
        self.assertEqual([item.name for item in self.base.iterdir()], ["CLAUDE.md"])

    def test_mode_is_preserved_and_missing_parents_are_created(self):
        path = self.base / "deep" / "nested" / "AGENTS.md"
        hub.atomic_write(path, "one\n", mode=0o644)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        path.chmod(0o640)
        hub.atomic_write(path, "two\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertEqual(path.read_text(), "two\n")

    def test_a_stale_temp_file_does_not_block_a_write(self):
        path = self.base / "AGENTS.md"
        path.with_name(path.name + ".ocdeck-tmp").write_text("garbage", encoding="utf-8")
        hub.atomic_write(path, "fresh\n")
        self.assertEqual(path.read_text(), "fresh\n")
        self.assertFalse(path.with_name(path.name + ".ocdeck-tmp").exists())


# --- codex TOML ---------------------------------------------------------------

def codex_hub(*servers: tuple[str, dict]) -> hub.HubConfig:
    return hub.HubConfig(instructions="", mcp=dict(servers))


class CodexTomlTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.toml = self.base / "config.toml"

    def apply(self, config: hub.HubConfig, existing: str) -> str:
        """Do what codex_actions does to ``existing`` and return the new text."""
        self.toml.write_text(existing, encoding="utf-8")
        block, _warnings = hub.codex_block(config, hub.codex_declared_servers(
            hub.remove_block(existing, CODEX_BEGIN, CODEX_END)))
        self.toml.write_text(hub.place_codex_block(existing, block), encoding="utf-8")
        return self.toml.read_text(encoding="utf-8")

    def test_env_values_with_quotes_backslashes_and_newlines_round_trip(self):
        env = {
            "QUOTED": 'he said "hi"',
            "BACKSLASH": "C:\\path\\to\\file",
            "NEWLINE": "one\ntwo\r\nthree",
            "TAB": "a\tb",
            "UNICODE": "café ✓",
        }
        config = codex_hub(("docs", {"command": ["uvx", "docs-mcp", "a\"b"], "env": env}))
        payload = tomllib.loads(self.apply(config, 'model = "gpt-5"\n'))
        self.assertEqual(payload["mcp_servers"]["docs"]["env"], env)
        self.assertEqual(payload["mcp_servers"]["docs"]["args"], ["docs-mcp", 'a"b'])
        self.assertEqual(payload["model"], "gpt-5")

    def test_dotted_quoted_and_bare_env_keys_are_valid_toml(self):
        env = {"A.B": "1", "with space": "2", 'quo"te': "3", "back\\slash": "4"}
        config = codex_hub(("docs", {"command": ["x"], "env": env}))
        self.assertEqual(tomllib.loads(self.apply(config, ""))["mcp_servers"]["docs"]["env"], env)

    def test_names_with_dots_spaces_or_unicode_are_skipped_with_a_warning(self):
        config = codex_hub(
            ("a.b", {"command": ["x"], "env": {}}),
            ("with space", {"command": ["x"], "env": {}}),
            ("café", {"command": ["x"], "env": {}}),
            ("good-name_1", {"command": ["x"], "env": {}}),
        )
        body, warnings = hub.codex_block(config)
        self.assertEqual(warnings, [
            'codex: skipping MCP server "a.b" (unsupported characters in the name)',
            'codex: skipping MCP server "café" (unsupported characters in the name)',
            'codex: skipping MCP server "with space" (unsupported characters in the name)',
        ])
        self.assertNotIn("a.b", body)
        payload = tomllib.loads(self.apply(config, ""))
        self.assertEqual(list(payload["mcp_servers"]), ["good-name_1"])

    def test_existing_bare_tables_are_not_duplicated(self):
        existing = ('model = "gpt-5"\n\n[mcp_servers.docs]\ncommand = "hand"\n'
                    '[mcp_servers.docs.env]\nTOKEN = "old"\n')
        config = codex_hub(("docs", {"command": ["uvx"], "env": {"TOKEN": "new"}}),
                           ("fresh", {"command": ["x"], "env": {}}))
        text = self.apply(config, existing)
        self.assertEqual(text.count("[mcp_servers.docs]"), 1)
        payload = tomllib.loads(text)
        self.assertEqual(payload["mcp_servers"]["docs"]["command"], "hand")
        self.assertEqual(payload["mcp_servers"]["docs"]["env"], {"TOKEN": "old"})
        self.assertIn("fresh", payload["mcp_servers"])

    def test_an_empty_hub_writes_nothing_and_removes_a_stale_block(self):
        self.assertEqual(self.apply(codex_hub(), 'model = "gpt-5"\n'), 'model = "gpt-5"\n')
        with_block = self.apply(codex_hub(("docs", {"command": ["uvx"], "env": {}})), 'model = "gpt-5"\n')
        self.assertIn(CODEX_BEGIN, with_block)
        cleared = self.apply(codex_hub(), with_block)
        self.assertNotIn(CODEX_BEGIN, cleared)
        self.assertEqual(tomllib.loads(cleared), {"model": "gpt-5"})

    def test_block_is_stable_and_sorted(self):
        config = codex_hub(("b", {"command": ["2"], "env": {}}),
                           ("a", {"command": ["1"], "env": {}}))
        first, _ = hub.codex_block(config)
        second, _ = hub.codex_block(config)
        self.assertEqual(first, second)
        self.assertLess(first.index("[mcp_servers.a]"), first.index("[mcp_servers.b]"))

    def test_server_without_a_command_produces_no_table(self):
        self.assertEqual(hub.codex_server_block("x", {"command": [], "env": {}}), "")
        body, warnings = hub.codex_block(codex_hub(("x", {"command": [], "env": {}})))
        self.assertEqual((body, warnings), ("", []))

    def test_declared_servers_finds_bare_tables(self):
        text = ('[mcp_servers.docs]\n[mcp_servers.other-name]\n'
                '  [mcp_servers.nested.env]\n[mcp_not_servers.x]\n')
        self.assertEqual(hub.codex_declared_servers(text), {"docs", "other-name"})

    def test_a_server_defined_in_quoted_table_form_is_not_declared_twice(self):
        """REAL BUG: codex_declared_servers captures ``"docs"`` (quotes included)
        from ``[mcp_servers."docs"]``, so the hub does not see the server as declared
        and appends ``[mcp_servers.docs]``; TOML treats a quoted and a bare key as
        the same key, so the user's whole config.toml stops parsing ("Cannot
        declare ('mcp_servers', 'docs') twice"). Fix: unquote the captured group
        when it is a quoted key (hub.py:574)."""
        config = codex_hub(("docs", {"command": ["uvx", "docs-mcp"], "env": {}}))
        text = self.apply(config, '[mcp_servers."docs"]\ncommand = "hand"\n')
        payload = tomllib.loads(text)  # raises TOMLDecodeError today
        self.assertEqual(text.count("[mcp_servers.docs]") + text.count('[mcp_servers."docs"]'), 1)
        self.assertEqual(payload["mcp_servers"]["docs"]["command"], "hand")

    def test_non_bmp_characters_do_not_break_the_generated_toml(self):
        """REAL BUG: codex_server_block escapes values with json.dumps(), whose
        default ensure_ascii writes astral characters as surrogate pairs
        ("\\ud83d\\ude00"); TOML only allows \\u escapes for Unicode scalar values,
        so an emoji in a command, an arg or an env value produces a config.toml no
        TOML parser (Codex included) can read. Fix: json.dumps(...,
        ensure_ascii=False) (hub.py:581)."""
        env = {"GREETING": "hi 😀", "PATHY": "/opt/☕/bin"}
        config = codex_hub(("docs", {"command": ["uvx", "😀pkg"], "env": env}))
        payload = tomllib.loads(self.apply(config, ""))  # raises TOMLDecodeError today
        self.assertEqual(payload["mcp_servers"]["docs"]["env"], env)
        self.assertEqual(payload["mcp_servers"]["docs"]["args"], ["😀pkg"])

    def test_top_level_keys_added_after_the_block_are_moved_back_to_top_level(self):
        """A top-level key written below the managed tables would belong to the
        last managed table in TOML; the next sync moves it back above them."""
        config = codex_hub(("docs", {"command": ["uvx"], "env": {"TOKEN": "t"}}))
        text = self.apply(config, 'model = "gpt-5"\n\n[profiles.x]\nmodel = "m"\n')
        self.assertLess(text.index(CODEX_BEGIN), text.index("[profiles.x]"))
        text = text.replace(f"{CODEX_END}\n", f'{CODEX_END}\ntemperature = 0.5\n', 1)
        payload = tomllib.loads(self.apply(config, text))
        self.assertEqual((payload["model"], payload["temperature"]), ("gpt-5", 0.5))
        self.assertEqual(payload["mcp_servers"]["docs"]["env"], {"TOKEN": "t"})
        self.assertEqual(payload["profiles"]["x"]["model"], "m")

    def test_duplicated_top_level_keys_after_the_block_are_refused(self):
        config = codex_hub(("docs", {"command": ["uvx"], "env": {"TOKEN": "t"}}))
        text = self.apply(config, 'model = "gpt-5"\n') + 'model = "gpt-6"\n'
        with self.assertRaisesRegex(ValueError, "model"):
            hub.place_codex_block(text, "x = 1")


# --- opencode insertion -------------------------------------------------------

class OpenCodeInsertTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.config = self.base / "opencode.jsonc"
        # The real config path must never be resolved in these tests.
        self.enterContext(patch.object(hub, "opencode_config_path", return_value=self.config))

    def write(self, text: str) -> None:
        self.config.write_text(text, encoding="utf-8")

    def add(self, name: str = "docs", server: dict | None = None) -> str:
        hub._add_opencode_server(name, server or SERVER)
        return self.config.read_text(encoding="utf-8")

    def test_mcp_key_with_spaces_and_a_newline_before_the_brace(self):
        self.write('{\n  "mcp"   :\n  {\n  },\n  "model": "m"\n}\n')
        payload = parse_jsonc(self.add())
        self.assertEqual(list(payload["mcp"]), ["docs"])
        self.assertEqual(payload["model"], "m")

    def test_escaped_mcp_inside_a_string_value_is_ignored(self):
        self.write('{\n  "notes": "see \\"mcp\\": { here",\n  "mcp": {\n  }\n}\n')
        payload = parse_jsonc(self.add())
        self.assertEqual(payload["notes"], 'see "mcp": { here')
        self.assertEqual(list(payload["mcp"]), ["docs"])

    def test_entry_shape_indentation_and_several_servers(self):
        self.write('{\n\t"mcp": {\n\t\t"a": {"type": "local", "command": ["x"]}\n\t}\n}\n')
        for name in ("zzz", "aaa"):
            self.add(name)
        text = self.config.read_text(encoding="utf-8")
        # The entry is indented past the key, whatever the file's own style is.
        self.assertIn('"aaa": {"type": "local"', text)
        self.assertTrue(text.split('"aaa": ')[0].endswith("\n\t  "))
        raw = text.split('"zzz": ', 1)[1].split("\n", 1)[0].rstrip(",")
        entry = json.loads("{" + raw[1:-1] + "}")
        self.assertEqual(entry, {"type": "local", "command": SERVER["command"],
                                 "environment": SERVER["env"], "enabled": True})
        # Each add re-reads the file, so the newest entry lands first.
        self.assertEqual(list(parse_jsonc(text)["mcp"]), ["aaa", "zzz", "a"])

    def test_crlf_config_stays_parseable(self):
        self.write('{\r\n  "mcp": {\r\n  },\r\n  "model": "m"\r\n}\r\n')
        payload = parse_jsonc(self.add())
        self.assertEqual(payload["model"], "m")
        self.assertEqual(list(payload["mcp"]), ["docs"])

    def test_quotes_or_newlines_in_a_server_name_are_refused_and_restored(self):
        original = '{\n  "mcp": {\n  }\n}\n'
        self.write(original)
        for name in ('bad"name', "bad\nname", "bad\tname"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    hub._add_opencode_server(name, SERVER)
                self.assertEqual(self.config.read_text(encoding="utf-8"), original)

    def test_missing_mcp_object_raises_before_touching_the_file(self):
        self.write('{\n  "model": "m"\n}\n')
        with self.assertRaises(ValueError):
            hub.insert_opencode_server(self.config.read_text(), "docs", SERVER)
        self.assertEqual(self.config.read_text(), '{\n  "model": "m"\n}\n')

    def test_the_mcp_block_regex_is_the_naive_one(self):
        self.assertEqual(MCP_BLOCK.pattern, r'"mcp"\s*:\s*\{')
        self.assertTrue(MCP_BLOCK.search('"mcp":{'))
        self.assertIsNone(MCP_BLOCK.search('"mcp" : [1]'))

    def test_a_backslash_in_a_server_name_is_refused(self):
        # Server names are plain identifiers for every harness (as in Codex TOML).
        self.write('{\n  "mcp": {\n  }\n}\n')
        with self.assertRaises(ValueError):
            self.add("weird\\name")

    def test_a_block_comment_mentioning_mcp_is_not_mistaken_for_the_key(self):
        """REAL BUG: insert_opencode_server searches the raw text with
        MCP_BLOCK = re.compile(r'"mcp"\\s*:\\s*\\{'), which also matches inside
        comments. With a /* ... */ comment that quotes the key, the new entry is
        swallowed by the comment, the file still parses and sync reports "applied"
        while the server was never registered. Fix: find the key with a JSONC-aware
        scan (skip strings and comments, depth 0) (hub.py:661)."""
        self.write('{\n  /* "mcp": { is documented here */\n  "mcp": {\n  },\n  "model": "m"\n}\n')
        text = self.add()
        payload = parse_jsonc(text)  # parses, but the server is gone
        self.assertEqual(list(payload["mcp"]), ["docs"])
        self.assertEqual(payload["model"], "m")

    def test_a_nested_mcp_object_is_not_mistaken_for_the_top_level_one(self):
        """REAL BUG: the same MCP_BLOCK problem in its second shape: a nested
        ``"experimental": {"mcp": {}}`` that appears before the real key receives the
        new server, so the top-level mcp object stays empty and the file still
        parses. Fix: as above — only a depth-0 key counts (hub.py:661)."""
        self.write('{\n  "experimental": {"mcp": {\n  }},\n  "mcp": {\n  }\n}\n')
        payload = parse_jsonc(self.add())
        self.assertEqual(list(payload["mcp"]), ["docs"])
        self.assertEqual(payload["experimental"], {"mcp": {}})


# --- browser server exclusion -------------------------------------------------

class BrowserServerTests(unittest.TestCase):
    def test_every_marker_is_recognised_in_a_name_or_a_command_part(self):
        for name, server in [
            ("signed_in_tabs", {"command": ["node"]}),
            ("docs", {"command": ["/usr/bin/playwright-mcp"]}),
            ("docs", {"command": ["node", "/x/agent_browser/bin.js"]}),
            ("docs", {"command": ["npx", "@playwright/mcp@latest"]}),
            ("agent_browser", {"command": ["x"]}),
            ("docs", {"command": ["x"], "env": {"OPENCODE_AGENT_BROWSER_CONFIG": "/c.json"}}),
        ]:
            with self.subTest(name=name, server=server):
                self.assertTrue(hub.is_browser_server(name, server))

    def test_ordinary_servers_are_kept(self):
        for name, server in [
            ("docs", {"command": ["uvx", "docs-mcp"]}),
            (INDEX_SERVER, {"command": [sys.executable, "-m", "ocdeck.index_server"]}),
            ("playwright", {"command": ["npx", "-y", "playwright"]}),
            ("browser", {"command": ["chromium", "--headless"]}),
            ("docs", {"command": ["uvx"], "env": {"OPENCODE_CONFIG": "/c"}}),
            ("docs", {"command": ["uvx"], "env": {}}),
        ]:
            with self.subTest(name=name, server=server):
                self.assertFalse(hub.is_browser_server(name, server))

    def test_missing_and_empty_shapes(self):
        self.assertFalse(hub.is_browser_server("docs", {}))
        self.assertFalse(hub.is_browser_server("docs", {"command": []}))
        self.assertFalse(hub.is_browser_server("docs", {"command": [1, None, b"x"]}))
        self.assertFalse(hub.is_browser_server("docs", {"command": ["x"], "env": None}))
        self.assertFalse(hub.is_browser_server("docs", {"command": ["x"], "env": {}}))
        self.assertFalse(hub.is_browser_server("docs", {"command": ["x"], "env": {"A": "B"}}))
        # A browser server with a non-list command cannot be recognised, but the
        # hub never builds one: _normalize_server rejects a non-list command.
        self.assertIsNone(hub._normalize_server({"command": "playwright-mcp"}))

    def test_browser_servers_never_reach_any_harness(self):
        servers = {
            "docs": SERVER,
            "sneaky": {"command": ["/x/playwright-mcp-v1"], "env": {}},
            "by_env": {"command": ["/x/other"], "env": {"OPENCODE_AGENT_BROWSER_CONFIG": "/c"}},
        }
        self.assertEqual(list(hub.active_servers(hub.HubConfig(mcp=servers))), ["docs"])


# --- paths, config and the cli ------------------------------------------------

class HubEnv:
    """A throwaway HOME/hub/codex tree; nothing outside it is ever touched."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.home = self.base / "home"
        self.home.mkdir()
        self.hub_dir = self.base / "hub"
        self.codex_dir = self.base / "codex-home"
        self.opencode_config = self.home / ".config" / "opencode" / "opencode.jsonc"
        self.claude_md = self.home / ".claude" / "CLAUDE.md"
        self.claude_json = self.home / ".claude.json"
        self.runner = FakeClaudeCli(self.claude_json)
        self.enterContext(patch.dict(os.environ, {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "OCDECK_HUB_DIR": str(self.hub_dir),
            "CODEX_HOME": str(self.codex_dir),
        }, clear=True))
        self.enterContext(patch.object(hub, "RUNNER", self.runner))
        # A writable (non-V2) OpenCode deployment that lives inside the temp tree.
        self.enterContext(patch.object(hub, "opencode_backend", return_value="v1"))

    def hub_json(self) -> dict:
        return json.loads((self.hub_dir / "hub.json").read_text(encoding="utf-8"))

    def set_servers(self, servers: dict) -> None:
        payload = self.hub_json()
        payload["mcp"] = servers
        (self.hub_dir / "hub.json").write_text(json.dumps(payload), encoding="utf-8")

    def enable(self, *harnesses: str) -> None:
        for name in ("opencode", "claude", "codex"):
            self.assertEqual(main(["harness", "on" if name in harnesses else "off", name]), 0)

    def prepare(self, *harnesses: str, opencode: str | None = '{\n  "mcp": {\n  }\n}\n',
                servers: dict | None = None) -> None:
        self.enable(*harnesses)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["init"]), 0)
        if opencode is not None:
            self.opencode_config.parent.mkdir(parents=True, exist_ok=True)
            self.opencode_config.write_text(opencode, encoding="utf-8")
        self.claude_md.parent.mkdir(parents=True, exist_ok=True)
        self.claude_md.write_text("# My notes\n", encoding="utf-8")
        self.set_servers({"docs": SERVER} if servers is None else servers)

    def apply(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(["sync", "--apply"])
        return code, out.getvalue(), err.getvalue()

    def config_actions(self) -> list:
        return [action for action in plan_actions()
                if action.target_path == str(self.opencode_config)]


class ConfigPathTests(HubEnv, unittest.TestCase):
    def test_hub_dir_resolution(self):
        self.assertEqual(hub.default_hub_dir(), self.hub_dir)
        self.assertEqual(hub.resolve_hub_dir(self.base / "other"), self.base / "other")
        self.assertEqual(hub.resolve_hub_dir("~/expanded"), self.home / "expanded")
        for blank in ("   ", ""):
            with self.subTest(blank=blank), patch.dict(os.environ, {"OCDECK_HUB_DIR": blank}):
                self.assertEqual(hub.default_hub_dir(), self.home / ".config" / "agents")
        self.assertEqual(hub.hub_config_path(self.base / "x"), self.base / "x" / hub.HUB_FILE)

    def test_codex_home_resolution(self):
        self.assertEqual(hub.codex_home(), self.codex_dir)
        self.assertEqual(hub.codex_config_path(), self.codex_dir / "config.toml")
        self.assertEqual(hub.codex_markdown_path(), self.codex_dir / "AGENTS.md")
        with patch.dict(os.environ, {"CODEX_HOME": "~/codex"}):
            self.assertEqual(hub.codex_home(), self.home / "codex")
        with patch.dict(os.environ, {"CODEX_HOME": "  "}):
            self.assertEqual(hub.codex_home(), self.home / ".codex")

    def test_claude_paths_follow_home(self):
        self.assertEqual(hub.claude_markdown_path(), self.home / ".claude" / "CLAUDE.md")
        self.assertEqual(hub.claude_settings_path(), self.home / ".claude.json")
        self.assertEqual(hub.opencode_agents_path(), self.home / ".config" / "opencode" / "AGENTS.md")

    def test_opencode_config_path_and_writability(self):
        self.assertEqual(hub.opencode_config_path(), self.opencode_config)
        self.assertEqual(hub.opencode_config_writable(), (True, ""))
        with patch.dict(os.environ, {"OCDECK_OPENCODE_CONFIG": str(self.base / "other.jsonc")}):
            self.assertEqual(hub.opencode_config_path(), self.base / "other.jsonc")
            self.assertEqual(hub.opencode_config_writable(), (True, ""))

    def test_v2_protected_config_is_reported_and_never_edited(self):
        managed = self.base / "opt" / "opencode.jsonc"
        managed.parent.mkdir(parents=True)
        managed.write_text('{"mcp": {}}\n', encoding="utf-8")
        with patch.object(hub, "opencode_backend", return_value="v2"), \
                patch.object(hub, "managed_v2_config", return_value=managed):
            self.assertEqual(hub.opencode_config_path(), managed)
            writable, reason = hub.opencode_config_writable()
            self.assertFalse(writable)
            self.assertIn(str(managed), reason)
            actions = hub.opencode_actions(hub.HubConfig(mcp={"docs": SERVER}))
            self.assertTrue(all(action.manual for action in actions))
            self.assertIn("protected", actions[0].description)
            self.assertEqual([action.harness for action in actions], ["opencode"])
            for action in actions:
                action.apply_fn()
            self.assertEqual(managed.read_text(encoding="utf-8"), '{"mcp": {}}\n')
        with patch.object(hub, "opencode_backend", return_value="v2"), \
                patch.object(hub, "managed_v2_config", return_value=None):
            writable, reason = hub.opencode_config_writable()
            self.assertFalse(writable)
            self.assertIn("protected", reason)
            self.assertNotIn(str(managed), reason)

    def test_managed_v2_config_ignores_a_missing_binary_or_file(self):
        with patch.object(hub.shutil, "which", return_value=None):
            self.assertIsNone(hub.managed_v2_config())
        binary = self.base / "bin" / "opencode2"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        with patch.object(hub.shutil, "which", return_value=str(binary)):
            self.assertIsNone(hub.managed_v2_config())  # no config/opencode/opencode.jsonc

    def test_broken_hub_json_falls_back_to_the_defaults(self):
        for text in ("", "{not json", "[]", '{"mcp": 3}', '{"instructions": "  "}'):
            with self.subTest(text=text):
                self.hub_dir.mkdir(parents=True, exist_ok=True)
                (self.hub_dir / "hub.json").write_text(text, encoding="utf-8")
                config = hub.load_hub_config()
                self.assertEqual(config.instructions, str(self.hub_dir / "AGENTS.md"))
                self.assertEqual(config.mcp, {})
        self.assertEqual(hub.load_hub_config(self.base / "nowhere").mcp, {})

    def test_disabled_and_malformed_servers_are_dropped(self):
        self.hub_dir.mkdir(parents=True, exist_ok=True)
        (self.hub_dir / "hub.json").write_text(json.dumps({"mcp": {
            "off": {"command": ["x"], "enabled": False},
            "on": {"command": ["x"]},
            "empty": {"command": []},
            "strcommand": {"command": "uvx x"},
            "notdict": ["x"],
            "numeric": {"command": [1, 2], "env": {"A": 3}},
        }}), encoding="utf-8")
        config = hub.load_hub_config()
        self.assertEqual(set(config.mcp), {"on", "numeric"})
        self.assertEqual(config.mcp["numeric"], {"command": ["1", "2"], "env": {"A": "3"}})

    def test_init_never_overwrites_and_reports_an_unreadable_config(self):
        self.assertEqual(hub.initialize_hub(), (self.hub_dir / "hub.json", True))
        self.assertEqual(stat.S_IMODE((self.hub_dir / "hub.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.hub_dir.stat().st_mode), 0o700)
        self.assertIn("# Shared instructions", (self.hub_dir / "AGENTS.md").read_text())
        (self.hub_dir / "hub.json").write_bytes(b"\x00\x01 broken")
        self.assertEqual(hub.initialize_hub(), (self.hub_dir / "hub.json", False))
        self.assertEqual((self.hub_dir / "hub.json").read_bytes(), b"\x00\x01 broken")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["init"]), 0)
        self.assertIn("already exists", out.getvalue())

    def test_init_uses_the_opencode_agents_file_when_it_exists(self):
        shared = self.home / "elsewhere" / "AGENTS.md"
        shared.parent.mkdir(parents=True)
        shared.write_text("# Team rules\n", encoding="utf-8")
        self.opencode_config.parent.mkdir(parents=True)
        self.opencode_config.write_text('{"mcp": {}}\n', encoding="utf-8")
        agents = self.home / ".config" / "opencode" / "AGENTS.md"
        agents.symlink_to(shared)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["init"]), 0)
        self.assertEqual(self.hub_json()["instructions"], str(shared.resolve()))
        self.assertFalse((self.hub_dir / "AGENTS.md").exists())

    def test_init_survives_a_binary_opencode_config(self):
        self.opencode_config.parent.mkdir(parents=True)
        self.opencode_config.write_bytes(b'{"mcp": {"x": {"type": "local", "command": ["\xff"]}}}')
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["init"]), 0)
        self.assertIn("could not parse", errors.getvalue())
        self.assertEqual(list(hub.load_hub_config().mcp), [INDEX_SERVER])

    def test_init_imports_only_local_enabled_servers(self):
        self.opencode_config.parent.mkdir(parents=True)
        self.opencode_config.write_text(json.dumps({"mcp": {
            "local": {"type": "local", "command": ["a"], "environment": {"K": "V"}},
            "remote": {"type": "remote", "url": "https://example.invalid"},
            "off": {"type": "local", "command": ["b"], "enabled": False},
            "browser": {"type": "local", "command": ["c"],
                        "environment": {"OPENCODE_AGENT_BROWSER_CONFIG": "/c"}},
            "broken": {"type": "local", "command": []},
        }}), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["init"]), 0)
        self.assertEqual(list(hub.load_hub_config().mcp), ["local", INDEX_SERVER])
        self.assertEqual(hub.load_hub_config().mcp["local"], {"command": ["a"], "env": {"K": "V"}})

    def test_harness_on_off_and_status(self):
        self.enable("opencode", "claude", "codex")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["status"]), 0)
        self.assertIn("harnesses: OpenCode, Claude Code, Codex", out.getvalue())
        self.assertIn("dry run", out.getvalue())
        self.assertIn("  claude    ", out.getvalue())
        # No hub servers and no existing block: nothing to write to config.toml.
        self.assertNotIn("config.toml", out.getvalue())
        self.assertEqual(main(["harness", "on", "cursor"]), 2)
        for argv in (["harness", "sideways", "claude"], [], ["nope"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                main(argv)
        settings = self.home / ".config" / "ocdeck" / "harnesses.json"
        self.assertEqual(json.loads(settings.read_text()),
                         {"opencode": "on", "claude": "on", "codex": "on"})

    def test_hub_survives_a_broken_harnesses_json(self):
        settings = self.home / ".config" / "ocdeck" / "harnesses.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"claude": true, "codex": "nope"}', encoding="utf-8")
        self.assertEqual(main(["harness", "off", "opencode"]), 0)
        self.assertEqual(json.loads(settings.read_text()),
                         {"opencode": "off", "claude": "on", "codex": "auto"})

    def test_default_target_harness(self):
        self.enable()
        self.assertEqual(hub.default_target_harness("codex"), "opencode")
        self.assertEqual(main(["harness", "on", "claude"]), 0)
        self.assertEqual(hub.default_target_harness("codex"), "claude")
        self.assertEqual(main(["harness", "on", "codex"]), 0)
        self.assertEqual(hub.default_target_harness("codex"), "claude")
        self.enable("opencode")  # only opencode: it is the target for everything else
        self.assertEqual(hub.default_target_harness("codex"), "opencode")
        self.enable()  # all off
        self.assertEqual(hub.default_target_harness("claude"), "opencode")


# --- sync ---------------------------------------------------------------------

class SyncEdgeTests(HubEnv, unittest.TestCase):
    def test_a_failing_harness_does_not_stop_the_plan_from_converging(self):
        self.prepare("claude", "codex", "opencode")
        calls: list[str] = []
        real = hub._write_block

        def flaky(path, begin, end, body):
            calls.append(Path(path).name)
            if len(calls) == 1:  # ~/.claude/CLAUDE.md, the first file action
                raise OSError("disk full")
            real(path, begin, end, body)

        with patch.object(hub, "_write_block", flaky):
            code, _out, err = self.apply()
        self.assertEqual(code, 1)
        self.assertIn("disk full", err)
        self.assertEqual(calls, ["CLAUDE.md"])
        self.assertEqual(self.claude_md.read_text(encoding="utf-8"), "# My notes\n")
        self.assertFalse((self.codex_dir / "config.toml").exists())
        # The opencode actions that ran before the failure are not repeated.
        self.assertEqual(list(parse_jsonc(self.opencode_config.read_text())["mcp"]), ["docs"])
        shared = self.home / ".config" / "opencode" / "AGENTS.md"
        self.assertTrue(shared.is_symlink())

        code, out, _err = self.apply()
        self.assertEqual(code, 0)
        self.assertIn("applied 4 action(s)", out)
        self.assertEqual(plan_actions(), [])
        self.assertIn(CLAUDE_BEGIN, self.claude_md.read_text())
        self.assertIn("# My notes", self.claude_md.read_text())
        self.assertIn(CODEX_BEGIN, (self.codex_dir / "config.toml").read_text())
        self.assertIn("[mcp_servers.docs]", (self.codex_dir / "config.toml").read_text())
        self.assertEqual(self.runner.names, ["docs"])
        self.assertEqual(list(parse_jsonc(self.opencode_config.read_text())["mcp"]), ["docs"])
        self.assertFalse(list(self.base.rglob("*.ocdeck-tmp")))

    def test_manual_actions_are_reported_but_never_applied(self):
        self.prepare("opencode", opencode='{\n  "model": "m"\n}\n')
        plan = self.config_actions()
        self.assertTrue(plan)
        self.assertTrue(all(action.manual for action in plan))
        code, _out, err = self.apply()
        self.assertEqual((code, err.count("manual:")), (0, len(plan)))
        self.assertEqual(self.opencode_config.read_text(encoding="utf-8"), '{\n  "model": "m"\n}\n')
        self.assertEqual([action.description for action in self.config_actions()],
                         [action.description for action in plan])

    def test_an_empty_opencode_config_is_manual_and_is_never_filled_in(self):
        self.prepare("opencode", opencode="")
        plan = self.config_actions()
        self.assertTrue(plan)
        self.assertTrue(all(action.manual for action in plan))
        self.assertIn("empty", plan[0].description)
        code, _out, _err = self.apply()
        self.assertEqual(code, 0)
        self.assertEqual(self.opencode_config.read_text(encoding="utf-8"), "")

    def test_a_non_object_opencode_config_is_manual(self):
        broken = '{"mcp": true}\n'
        self.prepare("opencode", opencode=broken)
        plan = self.config_actions()
        self.assertTrue(all(action.manual for action in plan))
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(self.opencode_config.read_text(encoding="utf-8"), broken)

    def test_codex_skips_unsupported_names_but_syncs_the_rest(self):
        self.prepare("codex", servers={
            "a.b": {"command": ["x"], "env": {}},
            "docs": SERVER,
        })
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(["sync", "--apply"]), 0)
        self.assertIn('skipping MCP server "a.b"', err.getvalue())
        toml = (self.codex_dir / "config.toml").read_text(encoding="utf-8")
        self.assertNotIn("a.b", toml)
        self.assertIn("[mcp_servers.docs]", toml)
        self.assertEqual(list(tomllib.loads(toml)["mcp_servers"]), ["docs"])

    def test_a_claude_cli_failure_does_not_break_the_file_writes(self):
        self.prepare("claude", "codex")
        self.runner.fail = True
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(["sync", "--apply"]), 0)
        self.assertIn("exited 1", err.getvalue())
        self.assertIn(CLAUDE_BEGIN, self.claude_md.read_text())
        toml = (self.codex_dir / "config.toml").read_text(encoding="utf-8")
        self.assertIn(CODEX_BEGIN, toml)
        self.assertIn("[mcp_servers.docs]", toml)
        # The server was not registered, so the next plan still asks for it.
        self.assertIn('register MCP server "docs"',
                      " ".join(action.description for action in plan_actions()))

    def test_a_broken_hub_json_survives_a_sync(self):
        self.prepare("claude")
        (self.hub_dir / "hub.json").write_text("{ broken", encoding="utf-8")
        self.assertEqual(self.apply()[0], 0)
        self.assertIn("@" + str(self.hub_dir / "AGENTS.md"), self.claude_md.read_text())
        self.assertEqual((self.hub_dir / "hub.json").read_text(encoding="utf-8"), "{ broken")

    def test_a_second_sync_is_a_no_op(self):
        self.prepare("claude", "codex", "opencode")
        self.assertEqual(self.apply()[0], 0)
        before = {path: path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        self.assertEqual(plan_actions(), [])
        code, out, _err = self.apply()
        self.assertEqual(code, 0)
        self.assertIn("in sync — nothing to do", out)
        self.assertIn("applied 0 action(s)", out)
        after = {path: path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_the_agents_symlink_is_created_once_and_never_replaced(self):
        self.prepare("opencode")
        shared = self.home / ".config" / "opencode" / "AGENTS.md"
        self.assertEqual(self.apply()[0], 0)
        self.assertTrue(shared.is_symlink())
        self.assertEqual(shared.readlink(), self.hub_dir / "AGENTS.md")
        stamp = shared.lstat().st_mtime_ns
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(shared.lstat().st_mtime_ns, stamp)
        self.assertEqual(plan_actions(), [])

    def test_a_read_only_target_is_reported_instead_of_silently_dropped(self):
        """REAL BUG: sync --apply overwrites a read-only ~/.claude/CLAUDE.md without
        a word (os.replace only needs directory permission) and still exits 0, so
        the user never learns their read-only file was replaced. Fix: atomic_write
        must refuse a target it may not write (hub.py:269)."""
        self.prepare("claude")
        self.claude_md.chmod(0o444)
        self.addCleanup(self.claude_md.chmod, 0o644)
        self.assertTrue(plan_actions())
        code, out, err = self.apply()
        self.assertEqual((code, out.count("applied")), (1, 0))
        self.assertIn("read-only", err)
        self.assertEqual(self.claude_md.read_text(encoding="utf-8"), "# My notes\n")


# --- handoff ------------------------------------------------------------------

class HandoffEdgeTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.home = self.base / "home"
        self.home.mkdir()
        self.hub_dir = self.base / "hub"
        self.codex_dir = self.base / "codex-home"
        self.enterContext(patch.dict(os.environ, {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "OCDECK_HUB_DIR": str(self.hub_dir),
            "CODEX_HOME": str(self.codex_dir),
        }, clear=True))
        self.enterContext(patch.object(hub, "RUNNER", FakeClaudeCli(self.home / ".claude.json")))
        self.workdir = self.base / "work"
        self.workdir.mkdir()
        self.stamp = datetime(2026, 9, 26, 12, 34, 56)

    def session(self, **kwargs) -> SessionRecord:
        data = {
            "id": "claude:11111111-2222-3333-4444-555555555555",
            "title": "A title",
            "directory": str(self.workdir),
            "project_id": "",
            "created_ms": 0,
            "updated_ms": 0,
            "harness": "claude",
        }
        data.update(kwargs)
        return SessionRecord(**data)

    def write(self, session: SessionRecord, target: str = "codex", hub_dir=None):
        return write_handoff(session, target, hub_dir or self.hub_dir, now=self.stamp)

    def no_git(self):
        return patch.object(hub.subprocess, "run", fake_git((128, b"fatal\n"))[0])

    def test_missing_transcripts_for_every_source(self):
        cases = {
            "claude": (self.session(id="claude:none", last_prompt="fix the parser"), "codex"),
            "codex": (self.session(id="codex:none", last_prompt="fix the parser",
                                   harness="codex"), "claude"),
            "opencode": (self.session(id="ses_1", last_prompt="fix the parser",
                                      harness="opencode"), "claude"),
        }
        for source, (session, target) in cases.items():
            with self.subTest(source=source), self.no_git():
                self.assertIsNone(hub.find_transcript(source, session.id.split(":")[-1]))
                path, prompt = self.write(session, target)
                body = path.read_text(encoding="utf-8")
                self.assertEqual(path.name, f"20260926-123456-{source}-to-{target}.md")
                self.assertIn("fix the parser", body)
                self.assertNotIn("## Last assistant reply", body)
                self.assertNotIn("## Git state", body)
                self.assertIn(f"(`{source}`)", body)
                self.assertTrue(prompt.startswith("Continue the work handed off from "))
                self.assertIn(str(path), prompt)

    def test_no_prompt_and_no_transcript_still_writes_a_readable_note(self):
        with self.no_git():
            path, _prompt = self.write(self.session(title="", last_prompt=""))
        body = path.read_text(encoding="utf-8")
        self.assertIn("# Handoff: Claude Code session", body)
        self.assertNotIn("## Recent requests", body)
        self.assertIn("- Model: unknown", body)
        self.assertIn("- Directory: " + str(self.workdir), body)
        self.assertIn("- Session: 11111111-2222-3333-4444-555555555555", body)

    def test_claude_and_codex_transcripts_are_merged(self):
        claude = make_transcript(self.home / ".claude" / "projects" / "w" / "abc.jsonl", [
            {"type": "user", "cwd": str(self.workdir), "sessionId": "abc",
             "message": {"content": "<system-reminder>injected</system-reminder>"}},
            {"type": "user", "sessionId": "abc", "message": {"content": "first ask"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "one answer"}]}},
            {"type": "user", "message": {"content": "first ask"}},  # duplicate
            {"type": "user", "message": {"content": "second ask"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "two answers"}]}},
        ])
        prompts, reply = hub.transcript_exchange(claude, "claude")
        self.assertEqual(prompts, ["first ask", "second ask"])
        self.assertEqual(reply, "two answers")

        rollout = make_transcript(
            self.codex_dir / "sessions" / "2026" / "09" / "rollout-2026-09-26T10-00-00-def.jsonl", [
                {"type": "event_msg",
                 "payload": {"type": "user_message", "message": "<environment>ctx</environment>"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "port it"}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                     "content": [{"type": "output_text",
                                                                  "text": "ported"}]}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "all done"}},
            ])
        prompts, reply = hub.transcript_exchange(rollout, "codex")
        self.assertEqual(prompts, ["port it"])
        self.assertEqual(reply, "all done")
        with self.no_git():
            path, _prompt = self.write(self.session(id="codex:def", harness="codex"))
        body = path.read_text(encoding="utf-8")
        self.assertIn("port it", body)
        self.assertIn("all done", body)
        self.assertNotIn("<environment>", body)
        self.assertIn("Codex (`codex`)", body)

    def test_at_most_five_prompts_are_kept(self):
        entries = [{"type": "user", "message": {"content": f"ask {index}"}} for index in range(9)]
        entries.append({"type": "assistant", "message": {"content": "answer"}})
        transcript = make_transcript(self.home / ".claude" / "projects" / "w" / "abc.jsonl", entries)
        prompts, _reply = hub.transcript_exchange(transcript, "claude")
        self.assertEqual(prompts, ["ask 4", "ask 5", "ask 6", "ask 7", "ask 8"])

    def test_a_huge_reply_is_trimmed_to_the_tail(self):
        entries = [{"type": "user", "message": {"content": "go"}},
                   {"type": "assistant", "message": {"content": "HEAD" + "x" * 20000 + "TAIL"}}]
        transcript = make_transcript(self.home / ".claude" / "projects" / "w" / "abc.jsonl", entries)
        prompts, reply = hub.transcript_exchange(transcript, "claude")
        self.assertEqual(prompts, ["go"])
        self.assertIn("TAIL", reply)
        with self.no_git():
            path, _prompt = self.write(self.session(id="claude:abc"))
        body = path.read_text(encoding="utf-8")
        self.assertNotIn("HEAD", body)
        self.assertIn("TAIL", body)
        self.assertIn("…", body)
        self.assertLess(len(body.split("## Last assistant reply", 1)[1]), 4200)

    def test_git_state_is_omitted_outside_a_repository(self):
        run, seen = fake_git((128, b"fatal: not a git repository\n"))
        with patch.object(hub.subprocess, "run", run):
            self.assertEqual(hub.render_git_state(str(self.workdir)), [])
            self.assertEqual(hub.render_git_state(str(self.base / "nowhere")), [])
            self.assertEqual(hub.render_git_state(""), [])
        self.assertEqual(seen, ["rev-parse --is-inside-work-tree"])  # only the probe
        with self.no_git():
            path, _prompt = self.write(self.session())
        self.assertNotIn("## Git state", path.read_text(encoding="utf-8"))

    def test_git_state_renders_and_clips(self):
        run, seen = fake_git((0, b"true\n"), branch="feature/x\n",
                             status="".join(f" M file{index}\n" for index in range(60)),
                             diff="1 file changed", log="abc1234 do the thing")
        with patch.object(hub.subprocess, "run", run):
            lines = hub.render_git_state(str(self.workdir))
        self.assertEqual(seen[0], "rev-parse --is-inside-work-tree")
        body = "\n".join(lines)
        self.assertIn("- Branch: `feature/x`", body)
        self.assertIn(" M file0", body)
        self.assertIn("… 10 more line(s)", body)
        self.assertIn("1 file changed", body)
        self.assertIn("abc1234 do the thing", body)
        with patch.object(hub.subprocess, "run", fake_git((0, b"true\n"), log="")[0]):
            self.assertIn("(nothing)", "\n".join(hub.render_git_state(str(self.workdir))))
        with patch.object(hub.subprocess, "run", fake_git((0, b"true\n"), errors=True)[0]):
            self.assertEqual(hub.render_git_state(str(self.workdir)), [])

    def test_unicode_titles_survive_the_round_trip(self):
        title = "café ☕ 日本語 — «quotes»"
        directory = self.base / "проект ☕"
        directory.mkdir()
        with self.no_git():
            path, prompt = self.write(self.session(title=title, directory=str(directory)))
        self.assertEqual(path.parent, self.hub_dir / "handoffs" / "проект--")
        self.assertIn(title, path.read_text(encoding="utf-8"))
        self.assertIn(title, prompt)
        self.assertEqual(path.name, "20260926-123456-claude-to-codex.md")

    def test_dangerous_titles_and_directories_cannot_escape_the_hub(self):
        for title, directory in [
            ("../../etc/passwd", str(self.base / "x" / ".." / "..")),
            ("..", str(self.base)),
            ("/etc/shadow", "/etc"),
            ("a/b\\c", str(self.workdir)),
            ("nul\x00byte", str(self.workdir)),
        ]:
            with self.subTest(title=title, directory=directory), self.no_git():
                path, _prompt = self.write(self.session(title=title, directory=directory))
                self.assertEqual(path.parent.parent, self.hub_dir / "handoffs")
                self.assertTrue(path.resolve().is_relative_to(self.hub_dir.resolve()))
                self.assertNotIn("..", str(path.relative_to(self.hub_dir)))
                self.assertNotIn("/", path.name)
        for name, expected in (
            ("..", "workspace"),
            (".", "workspace"),
            ("", "workspace"),
            ("/", "-"),
            ("../../etc", "-..-etc"),
            ("/" * 50, "-" * 50),
            ("a b/c", "a-b-c"),
            ("..hidden", "hidden"),
            ("... ", "-"),
        ):
            with self.subTest(name=name):
                self.assertEqual(hub._safe_name(name), expected)
        self.assertEqual(len(hub._safe_name("d" * 300)), 60)

    def test_file_and_directory_modes(self):
        with self.no_git():
            path, _prompt = self.write(self.session())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.hub_dir / "handoffs").stat().st_mode), 0o700)
        again, _prompt = self.write(self.session())
        self.assertEqual(again, path)
        backup = path.with_name(path.name + hub.BACKUP_SUFFIX)
        self.assertEqual(backup.read_text(encoding="utf-8"), path.read_text(encoding="utf-8"))
        with self.no_git():
            third, _prompt = self.write(self.session(title="Changed"))
        self.assertEqual(third, path)
        self.assertIn("A title", backup.read_text(encoding="utf-8"))
        self.assertIn("Changed", third.read_text(encoding="utf-8"))

    def test_an_unknown_target_is_rejected(self):
        for target in ("cursor", "", "OpenCode"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.write(self.session(), target)
        self.assertFalse((self.hub_dir / "handoffs").exists())

    def test_transcript_lookup_ignores_glob_metacharacters(self):
        mine = make_transcript(
            self.codex_dir / "sessions" / "2026" / "rollout-2026-09-26T10-00-00-aaa.jsonl",
            [{"type": "event_msg", "payload": {"type": "user_message", "message": "MINE"}}])
        other = make_transcript(
            self.codex_dir / "sessions" / "2026" / "rollout-2026-09-26T10-00-00-aaab.jsonl",
            [{"type": "event_msg", "payload": {"type": "user_message", "message": "SECRET"}}])
        for native_id, expected in (("aaa", mine), ("aaab", other), ("*", None), ("a?c", None),
                                    ("a/c", None), ("..", None), ("", None), ("zzz", None)):
            with self.subTest(native_id=native_id):
                self.assertEqual(hub.find_transcript("codex", native_id), expected)
        self.assertIsNone(hub.find_transcript("opencode", "aaa"))
        self.assertIsNone(hub.find_transcript("claude", "aaa"))  # no ~/.claude/projects yet

    def test_a_glob_metacharacter_cannot_select_another_transcript(self):
        """REAL BUG: find_transcript only rejects ``/*?`` in a native id and then
        globs it, so ``[`` is a pathlib character class: the id "aaa[bc]" finds the
        *other* session's transcript and the handoff quotes its prompts. Fix: reject
        every glob metacharacter (or match the exact id suffix) (hub.py:773)."""
        make_transcript(
            self.codex_dir / "sessions" / "2026" / "rollout-2026-09-26T10-00-00-aaab.jsonl",
            [{"type": "event_msg", "payload": {"type": "user_message", "message": "SECRET"}}])
        with self.no_git():
            path, _prompt = self.write(self.session(id="codex:aaa[bc]", harness="codex"))
        self.assertIsNone(hub.find_transcript("codex", "aaa[bc]"))
        self.assertNotIn("SECRET", path.read_text(encoding="utf-8"))

    def test_opencode_db_exchange_reads_parts_and_inline_text(self):
        """REAL BUG: an OpenCode-origin handoff quoted only the single
        ``last_prompt`` ("go on") because find_transcript had no OpenCode path.
        The session DB carries the real exchange: text parts (in time order) or
        the inline payload, other sessions and non-message rows stay out."""
        db = make_opencode_db(self.base / "sessions.db", "ses_EDGE", [
            {"type": "user", "data": {"text": "wire the hub sync"}},
            {"type": "assistant", "data": {"content": [
                {"type": "reasoning", "text": "quiet thinking"},
                {"type": "text", "text": "the plan looks right"},
            ]}},
            {"type": "user", "parts": [
                {"type": "text", "text": "Continue the migration in stages, "},
                {"type": "text", "text": "starting with the parser"},
                {"type": "tool", "tool": "bash", "state": {"status": "completed"}},
            ]},
            {"type": "user", "data": {"text": "<system-reminder>injected</system-reminder>"}},
            {"type": "user", "data": {"text": "go on"}},
            {"type": "user", "data": {"text": "carry on"}},
            {"type": "assistant", "parts": [{"type": "text", "text": "all tests green"}]},
            {"type": "user", "session": "ses_OTHER", "data": {"text": "SECRET OTHER SESSION"}},
            {"type": "system", "data": {"text": "SECRET SYSTEM ROW"}},
            {"type": "synthetic", "parts": [{"type": "text", "text": "SECRET SYNTHETIC ROW"}]},
        ])
        with patch.dict(os.environ, {"OCDECK_SESSION_DB_FILE": str(db)}):
            self.assertEqual(hub.find_transcript("opencode", "ses_EDGE"), db)
            prompts, reply = hub.transcript_exchange(db, "opencode", "ses_EDGE")
            self.assertEqual(prompts, [
                "wire the hub sync",                                # inline user text
                "Continue the migration in stages, starting with the parser",  # text parts only
            ])
            self.assertEqual(reply, "all tests green")  # the newest turn that said anything
        with patch.dict(os.environ, {"OCDECK_SESSION_DB_FILE": str(db)}), self.no_git():
            path, _prompt = self.write(
                self.session(id="ses_EDGE", harness="opencode", last_prompt="go on"))
        body = path.read_text(encoding="utf-8")
        self.assertEqual(path.name, "20260926-123456-opencode-to-codex.md")
        self.assertIn("OpenCode (`opencode`)", body)
        self.assertIn("wire the hub sync", body)
        self.assertIn("## Last assistant reply", body)
        self.assertIn("all tests green", body)
        self.assertNotIn("the plan looks right", body)  # an older reply is replaced
        self.assertNotIn("quiet thinking", body)         # reasoning is not the reply
        self.assertNotIn("injected", body)               # injected context is not a request
        self.assertNotIn("go on", body)                 # trivial continuations are dropped
        self.assertNotIn("SECRET", body)                # other sessions and system rows stay out

    def test_opencode_db_scan_is_capped_and_trivial_only_yields_no_prompts(self):
        messages = [{"type": "user", "data": {"text": "the real ask"}}]  # oldest
        messages += [{"type": "user", "data": {"text": "go on"}} for _ in range(68)]
        messages.append({"type": "assistant",
                        "data": {"content": [{"type": "text", "text": "still working"}]}})
        db = make_opencode_db(self.base / "trivial.db", "ses_TRIVIAL", messages)
        prompts, reply = hub.opencode_transcript_exchange(db, "ses_TRIVIAL")
        self.assertEqual(prompts, [])  # the only real prompt is older than the scan window
        self.assertEqual(reply, "still working")

    def test_opencode_db_keeps_at_most_eight_prompts(self):
        messages = [{"type": "user", "data": {"text": f"ask {index}"}} for index in range(12)]
        messages.append({"type": "assistant",
                        "data": {"content": [{"type": "text", "text": "done"}]}})
        db = make_opencode_db(self.base / "cap.db", "ses_CAP", messages)
        prompts, reply = hub.opencode_transcript_exchange(db, "ses_CAP")
        self.assertEqual(prompts, [f"ask {index}" for index in range(4, 12)])
        self.assertEqual(reply, "done")

    def test_opencode_db_failures_never_raise(self):
        garbage = self.base / "garbage.db"
        garbage.write_bytes(b"definitely not a sqlite database" * 8)
        empty = self.base / "empty.db"
        sqlite3.connect(empty).close()  # a valid DB with no tables at all
        for db_path in (self.base / "missing.db", garbage, empty):
            with self.subTest(db_path=db_path.name):
                self.assertEqual(hub.opencode_transcript_exchange(db_path, "ses_X"), ([], ""))
                self.assertEqual(hub.transcript_exchange(db_path, "opencode", "ses_X"), ([], ""))
        with patch.dict(os.environ, {"OCDECK_SESSION_DB_FILE": str(garbage)}), self.no_git():
            path, _prompt = self.write(
                self.session(id="ses_X", harness="opencode", last_prompt="draft the fix"))
        body = path.read_text(encoding="utf-8")
        self.assertIn("draft the fix", body)  # the last-prompt fallback
        self.assertNotIn("## Last assistant reply", body)

    def test_opencode_db_without_a_part_table_falls_back_to_inline_text(self):
        db = self.base / "noparts.db"
        connection = sqlite3.connect(db)
        try:
            connection.execute(
                "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                "type TEXT NOT NULL, seq INTEGER NOT NULL, time_created INTEGER NOT NULL, "
                "time_updated INTEGER NOT NULL, data TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO session_message VALUES ('msg_0001', 'ses_NOPART', 'user', 1, 1000, 1000, ?)",
                (json.dumps({"text": "just inline text"}),),
            )
            connection.commit()
        finally:
            connection.close()
        prompts, reply = hub.opencode_transcript_exchange(db, "ses_NOPART")
        self.assertEqual(prompts, ["just inline text"])
        self.assertEqual(reply, "")

    def test_opencode_parts_are_read_bounded(self):
        db = make_opencode_db(self.base / "bound.db", "ses_BOUND", [
            {"type": "user", "parts": [{"type": "text", "text": "y" * (hub.MAX_PART_CHARS + 500)}]},
        ])
        prompts, _reply = hub.opencode_transcript_exchange(db, "ses_BOUND")
        self.assertEqual(prompts, ["y" * hub.MAX_PART_CHARS])

    def test_opencode_session_db_file_prefers_the_override_then_v2_then_v1(self):
        v2 = self.home / ".local" / "share" / "opencode-v2" / "opencode.db"
        v1 = self.home / ".local" / "share" / "opencode" / "opencode.db"
        self.assertEqual(hub.opencode_session_db_file(), v2)  # neither exists: the V2 default
        v1.parent.mkdir(parents=True)
        v1.write_bytes(b"")
        self.assertEqual(hub.opencode_session_db_file(), v1)  # only the V1 file exists
        v2.parent.mkdir(parents=True, exist_ok=True)
        v2.write_bytes(b"")
        self.assertEqual(hub.opencode_session_db_file(), v2)  # V2 wins when both exist
        with patch.dict(os.environ, {"OCDECK_SESSION_DB_FILE": str(self.base / "custom.db")}):
            self.assertEqual(hub.opencode_session_db_file(), self.base / "custom.db")
        with patch.dict(os.environ, {"OCDECK_SESSION_DB_FILE": str(v2)}):
            self.assertEqual(hub.find_transcript("opencode", "ses_A"), v2)
        with patch.dict(os.environ, {"OCDECK_SESSION_DB_FILE": str(self.base / "nope.db")}):
            self.assertIsNone(hub.find_transcript("opencode", "ses_A"))

    def test_trivial_continuation_filter_is_exact_and_walks_back(self):
        entries = [
            {"type": "user", "message": {"content": "ask one"}},
            {"type": "user", "message": {"content": "go on"}},   # skipped, exact match
            {"type": "user", "message": {"content": "OK"}},      # casefolded match, skipped
            {"type": "user", "message": {"content": "  Yes  "}},  # stripped match, skipped
            {"type": "user", "message": {"content": "Continue the migration next"}},  # kept
            {"type": "user", "message": {"content": "next"}},    # skipped
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer"}]}},
            {"type": "user", "message": {"content": "go on"}},   # newest, skipped
        ]
        transcript = make_transcript(self.home / ".claude" / "projects" / "w" / "abc.jsonl", entries)
        prompts, reply = hub.transcript_exchange(transcript, "claude")
        self.assertEqual(prompts, ["ask one", "Continue the migration next"])
        self.assertEqual(reply, "answer")

    def test_trivial_continuation_walk_back_is_capped_at_five_prompts(self):
        entries = [{"type": "user", "message": {"content": f"ask {index}"}} for index in range(6)]
        entries.append({"type": "user", "message": {"content": "keep going"}})
        entries.append({"type": "assistant", "message": {"content": [{"type": "text", "text": "fin"}]}})
        transcript = make_transcript(self.home / ".claude" / "projects" / "w" / "abc.jsonl", entries)
        prompts, reply = hub.transcript_exchange(transcript, "claude")
        self.assertEqual(prompts, [f"ask {index}" for index in range(1, 6)])
        self.assertEqual(reply, "fin")

    def test_project_memory_lines_are_clipped_to_one_line_each(self):
        project = self.base / "memo"
        project.mkdir()
        with patch.dict(os.environ, {"OCDECK_INDEX_HARNESS": "claude"}):
            IndexServer(cwd=project).memory_write("x" * (hub.MAX_MEMORY_CHARS + 60), tags=["edge"])
            IndexServer(cwd=project).memory_write("second\nentry  with   gaps", tags=[])
        with self.no_git():
            path, _prompt = self.write(self.session(directory=str(project)))
        body = path.read_text(encoding="utf-8")
        self.assertIn("## PROJECT MEMORY (ocdeck-index, if any)", body)
        lines = [line for line in body.splitlines() if line.startswith("- (")]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "- (claude) second entry with gaps")  # newest first, one line
        self.assertTrue(lines[1].endswith("…"))                          # clipped, not dumped
        self.assertLessEqual(len(lines[1]), len("- (claude) ") + hub.MAX_MEMORY_CHARS)

    def test_an_empty_or_invalid_memory_scope_renders_no_section(self):
        with self.no_git():
            empty, _prompt = self.write(self.session(directory=str(self.workdir)))
            nowhere, _prompt = self.write(self.session(directory=str(self.base / "nowhere")))
        for path in (empty, nowhere):
            with self.subTest(path=path.name):
                body = path.read_text(encoding="utf-8")
                self.assertNotIn("PROJECT MEMORY", body)
                self.assertNotIn("no memory", body.lower())

    def test_a_broken_index_import_omits_the_memory_section(self):
        with self.no_git(), patch.dict(sys.modules, {"ocdeck.index_server": None}):
            path, _prompt = self.write(self.session(directory=str(self.workdir)))
        self.assertNotIn("PROJECT MEMORY", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
