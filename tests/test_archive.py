"""Archive storage: reversible, owner-only, bounded, and never crashing."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from ocdeck.archive import (
    ARCHIVED_SESSIONS_LIMIT,
    archive_session,
    default_archived_sessions_file,
    load_archived_sessions,
    unarchive_session,
)


class ArchiveStoreTests(unittest.TestCase):
    def setUp(self):
        # These tests write the default state paths: never the real ones.
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        os.environ["XDG_STATE_HOME"] = state.name
        self.state = state.name
        self.path = Path(state.name) / "ocdeck" / "archived-sessions.json"

    def test_default_file_lives_under_xdg_state_home(self):
        self.assertEqual(
            default_archived_sessions_file(), self.path
        )

    def test_missing_file_means_nothing_archived(self):
        self.assertEqual(load_archived_sessions(self.path), set())
        self.assertEqual(load_archived_sessions(), set())

    def test_archive_unarchive_round_trip_keeps_order_and_permissions(self):
        archive_session("ses_one", self.path)
        archive_session("claude:two", self.path)
        self.assertEqual(load_archived_sessions(self.path), {"ses_one", "claude:two"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"version": 1, "sessions": ["ses_one", "claude:two"]})
        unarchive_session("ses_one", self.path)
        self.assertEqual(load_archived_sessions(self.path), {"claude:two"})
        self.assertEqual(json.loads(self.path.read_text())["sessions"], ["claude:two"])

    def test_operations_are_idempotent_and_reject_invalid_ids(self):
        archive_session("ses_one", self.path)
        before = self.path.read_bytes()
        archive_session("ses_one", self.path)  # already archived: no rewrite
        self.assertEqual(self.path.read_bytes(), before)
        unarchive_session("claude:absent", self.path)  # not archived: no rewrite
        self.assertEqual(self.path.read_bytes(), before)
        for bad in ("", "not a session id!", "SES_upper", "../escape", None, 7):
            archive_session(bad, self.path)
        self.assertEqual(load_archived_sessions(self.path), {"ses_one"})

    def test_corrupt_file_archives_nothing_and_survives(self):
        for corrupt in ("{not json", "[]", '{"version": 2}', '{"sessions": "nope"}',
                        '{"version": 1, "sessions": [null, 5, "ses_ok", "ses_ok"]}'):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(corrupt)
            self.assertEqual(load_archived_sessions(self.path), {"ses_ok"} if "ses_ok" in corrupt else set())
        self.path.write_text("{not json")
        archive_session("ses_new", self.path)  # recovers by rewriting the file
        self.assertEqual(load_archived_sessions(self.path), {"ses_new"})

    def test_store_is_bounded(self):
        for index in range(ARCHIVED_SESSIONS_LIMIT + 25):
            archive_session(f"ses_{index:04d}", self.path)
        stored = json.loads(self.path.read_text())["sessions"]
        self.assertEqual(len(stored), ARCHIVED_SESSIONS_LIMIT)
        self.assertEqual(stored[0], "ses_0025")  # the oldest fell off the end
        self.assertEqual(stored[-1], f"ses_{ARCHIVED_SESSIONS_LIMIT + 24:04d}")
        self.assertEqual(len(load_archived_sessions(self.path)), ARCHIVED_SESSIONS_LIMIT)

    def test_atomic_write_leaves_no_temporary_behind(self):
        archive_session("ses_one", self.path)
        leftovers = [item for item in self.path.parent.iterdir() if item.name != self.path.name]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
