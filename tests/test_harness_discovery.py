"""Installation/settings changes are picked up without resetting adapter caches."""
import unittest
from unittest import mock

from ocdeck.harnesses import MultiHarnessSource
from ocdeck.models import DashboardSnapshot


class HarnessDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_install_settings_and_launch_selection_follow_refresh(self):
        installed = {}
        settings = {"opencode": "off", "claude": "auto", "codex": "auto"}
        source = MultiHarnessSource(None, refresh_harnesses=True)
        source._merge = mock.AsyncMock(side_effect=lambda snapshot: snapshot)
        with mock.patch("ocdeck.harnesses.load_harness_settings", return_value=settings), \
                mock.patch("ocdeck.harnesses.find_binary", side_effect=installed.get):
            await source.collect()
            self.assertEqual(source.enabled_harnesses, ())
            installed["claude"] = "/fixture/claude"
            await source.collect()
            self.assertEqual(source.enabled_harnesses, ("claude",))
            adapter = source.adapter("claude")
            self.assertEqual(adapter.binary, "/fixture/claude")
            self.assertEqual(source.launch_harness, "claude")
            installed["codex"] = "/fixture/codex"
            await source.collect()
            self.assertIs(source.adapter("claude"), adapter)
            self.assertEqual(source.enabled_harnesses, ("claude", "codex"))
            source.launch_harness = "codex"
            await source.collect()
            self.assertEqual(source.launch_harness, "codex")
            settings["codex"] = "off"
            await source.collect()
            self.assertEqual(source.enabled_harnesses, ("claude",))
            self.assertEqual(source.launch_harness, "claude")

    async def test_explicit_override_remains_pinned(self):
        source = MultiHarnessSource(None, refresh_harnesses=True, harness_override=("claude",))
        source._merge = mock.AsyncMock(side_effect=lambda snapshot: snapshot)
        with mock.patch("ocdeck.harnesses.load_harness_settings", return_value={"claude": "off", "codex": "on"}), \
                mock.patch("ocdeck.harnesses.find_binary", side_effect=lambda name: f"/fixture/{name}"):
            await source.collect()
            self.assertEqual(source.enabled_harnesses, ("claude",))

    async def test_opencode_install_found_at_backend_specific_location(self):
        opencode = mock.Mock(backend="v2", opencode_bin=None)
        opencode.collect = mock.AsyncMock(return_value=DashboardSnapshot(connection="live"))
        opencode._find_opencode2.return_value = "/fixture/opencode2"
        source = MultiHarnessSource(opencode, opencode_enabled=False, refresh_harnesses=True)
        source._merge = mock.AsyncMock(side_effect=lambda snapshot: snapshot)
        with mock.patch("ocdeck.harnesses.load_harness_settings", return_value={"opencode": "auto"}), \
                mock.patch("ocdeck.harnesses.find_binary", return_value=None):
            await source.collect()
            self.assertEqual(source.enabled_harnesses, ("opencode",))
            self.assertEqual(source.opencode_bin, "/fixture/opencode2")
            opencode.collect.assert_awaited_once()
            opencode._find_opencode.assert_not_called()
