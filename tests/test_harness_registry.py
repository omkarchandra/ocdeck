import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocdeck import harness_plugins, harnesses
from ocdeck.agent_tabs import is_managed_session
from ocdeck.harnesses import (
    HarnessSpec, TranscriptHarness, TranscriptInfo, build_adapters, harness_ids,
    load_harness_settings, load_harness_plugins, managed_tmux_prefixes, register_harness,
    resolve_enabled_harnesses, runtime_label, split_session_key, unregister_harness,
)
from ocdeck.recent_open import load_recent_open_sessions, save_recent_open_sessions


class FakeHarness(TranscriptHarness):
    harness = "gemini"

    def __init__(self):
        super().__init__(Path("/nonexistent"), "/bin/gemini")

    def transcript_files(self):
        return []

    def parse_transcript(self, path, size):
        return TranscriptInfo("x", "/w", "t", 1, 2)

    def process_session_id(self, process):
        return ""

    def resume_command(self, session_id, directory, *, browser=False):
        return ["/bin/gemini", "--resume", session_id]

    def new_command(self, directory, prompt="", *, browser=False):
        return ["/bin/gemini"], ""


SPEC = HarnessSpec("gemini", "Gemini CLI", "GM", ("gemini",), tmux_prefix="gm",
                   badge="GM", style="#c0b6ff", adapter=FakeHarness)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        register_harness(SPEC)
        self.addCleanup(unregister_harness, "gemini")

    def test_a_registered_harness_is_visible_everywhere(self):
        self.assertEqual(harness_ids()[-1], "gemini")
        self.assertEqual(harnesses.HARNESS_LABELS["gemini"], "Gemini CLI")
        self.assertEqual(harnesses.HARNESS_BADGES["gemini"], "GM")
        self.assertEqual(runtime_label("gemini", "gemini-3-pro", full=False), "GM GMN")
        self.assertIn("gm-", managed_tmux_prefixes())
        self.assertTrue(is_managed_session("gm-123"))
        self.assertEqual(split_session_key("gemini:abc"), ("gemini", "abc"))
        self.assertEqual(FakeHarness().tmux_name("abc"), "gm-abc")
        self.assertEqual([type(a) for a in build_adapters(["gemini", "opencode"])], [FakeHarness])
        which = lambda harness: "/bin/x" if harness == "gemini" else None
        self.assertEqual(resolve_enabled_harnesses({"gemini": "auto"}, which=which), ("gemini",))
        with tempfile.TemporaryDirectory() as base:
            self.assertEqual(load_harness_settings(Path(base) / "none.json")["gemini"], "auto")
            path = Path(base) / "recent.json"
            save_recent_open_sessions(path, ["gemini:abc", "ses_x", "bad key"])
            self.assertEqual(load_recent_open_sessions(path), ["gemini:abc", "ses_x"])

    def test_unregistering_removes_it(self):
        unregister_harness("gemini")
        self.assertNotIn("gemini", harness_ids())
        self.assertFalse(is_managed_session("gm-123"))
        self.assertEqual(split_session_key("gemini:abc"), ("opencode", "gemini:abc"))
        register_harness(SPEC)  # restore for the cleanup

    def test_hub_skips_a_harness_without_a_sync_planner(self):
        from ocdeck import hub
        with tempfile.TemporaryDirectory() as base, \
                mock.patch.dict(os.environ, {"OCDECK_HUB_DIR": base}), \
                mock.patch.object(hub, "resolve_enabled_harnesses", return_value=("gemini",)), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(hub.plan_actions(Path(base)), [])
        self.assertIn("Gemini CLI: no config sync available", err.getvalue())

    def test_invalid_specs_are_rejected(self):
        bad = [
            HarnessSpec("Gemini", "x", "GM", ("g",), tmux_prefix="gz"),        # id case
            HarnessSpec("other", "x", "gm", ("g",), tmux_prefix="ot"),         # code case
            HarnessSpec("other", "x", "OT", ("g",), tmux_prefix="oc"),         # reserved prefix
            HarnessSpec("other", "x", "OT", ("g",), tmux_prefix="gm"),         # prefix clash
            HarnessSpec("other", "x", "GM", ("g",), tmux_prefix="ot"),         # code clash
            HarnessSpec("other", "x", "OT", ("g",), tmux_prefix="o-t"),        # prefix chars
        ]
        for spec in bad:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                register_harness(spec)
        self.assertNotIn("other", harness_ids())
        with self.assertRaises(ValueError):
            unregister_harness("opencode")


class PluginLoaderTests(unittest.TestCase):
    def test_plugins_load_and_a_broken_one_is_skipped(self):
        with tempfile.TemporaryDirectory() as base:
            folder = Path(base)
            (folder / "goodharness.py").write_text(
                "from ocdeck.harnesses import HarnessSpec, register_harness\n"
                "register_harness(HarnessSpec('plugged', 'Plugged', 'PL', ('plugged',), tmux_prefix='pl'))\n"
            )
            (folder / "broken.py").write_text("raise RuntimeError('boom')\n")
            (folder / "_private.py").write_text("raise RuntimeError('must not be imported')\n")
            self.addCleanup(lambda: unregister_harness("plugged") if "plugged" in harness_ids() else None)
            for name in ("goodharness", "broken", "_private"):
                self.addCleanup(sys.modules.pop, f"ocdeck.harness_plugins.{name}", None)
            with mock.patch.object(harness_plugins, "__path__", [str(folder)]), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                loaded = load_harness_plugins()
            self.assertEqual(loaded, ["goodharness"])
            self.assertIn("plugged", harness_ids())
            self.assertIn("broken failed to load: boom", err.getvalue())

    def test_the_template_is_valid_python_and_never_loaded(self):
        template = Path(harness_plugins.__file__).with_name("_template.py")
        compile(template.read_text(), str(template), "exec")
        self.assertNotIn("example", harness_ids())


if __name__ == "__main__":
    unittest.main()
