"""The USAGE tab: how the numbers read, and that the deck shows them."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from textual.widgets import Static

from ocdeck.app import OCDeckApp
from ocdeck.usage import Limit, ProviderUsage, Tally, UsageReport, WINDOWS
from ocdeck.usage_view import bar, render_usage
from tests.test_harness_app import FakeHarnessSource, make_snapshot

NOW = 1_791_500_000.0


def tallies(fresh=1500, cached=2_000_000, cost=None):
    return {label: Tally(fresh, 0, cached, cost, 3) for label, _ in WINDOWS}


def report():
    return UsageReport(NOW, (
        ProviderUsage("claude-code", "Anthropic · Claude Code",
                      (Limit("5h", 12.0, NOW + 3 * 3600, "Claude Code status line", NOW - 30),
                       Limit("7d", 91.0, NOW + 4 * 86400, "Claude Code status line", NOW - 7200)), tallies()),
        ProviderUsage("codex", "OpenAI · Codex", (Limit("7d", None, NOW - 5, "Codex rollout", NOW - 9, stale=True),),
                      tallies(0, 0), note="plan prolite"),
        ProviderUsage("opencode:deepseek", "DeepSeek · via OpenCode", (), tallies(cost=1.5)),
    ))


def test_bar_fills_and_colours_by_how_full_it_is():
    assert bar(0)[0] == "░" * 20 and bar(100)[0] == "█" * 20 and bar(50)[0].count("█") == 10
    assert bar(10)[1] != bar(70)[1] != bar(95)[1]
    assert bar(-5)[0] == "░" * 20 and bar(500)[0] == "█" * 20


def test_the_tab_text_shows_used_left_resets_tokens_and_a_budget_hint():
    text = render_usage(report(), NOW).plain
    assert "ANTHROPIC · CLAUDE CODE" in text and "12.0% used" in text and "88.0% left" in text
    assert "resets in 3h" in text and "91.0% used" in text and "(read 2h ago)" in text
    assert "reset; waiting for a new reading" in text           # the expired Codex window
    assert "1.5K" in text and "cached 7d 2M" in text and "$1.50" in text
    assert "plan prolite" in text
    assert "add\na budget in" in text and "usage.json" in text  # OpenCode providers have no reported limit
    assert '{"limits": {"opencode:deepseek": {"7d": {"usd": 25}}}}' in text
    assert "keys: opencode:deepseek" in text


def test_an_empty_report_says_so():
    assert "No usage found." in render_usage(UsageReport(NOW, ()), NOW).plain


class UsageTabTests(unittest.IsolatedAsyncioTestCase):
    def source(self):
        source = FakeHarnessSource(Path("/nonexistent-fixture"))
        source.snap = make_snapshot("/project")
        return source

    async def test_key_7_opens_the_tab_and_reads_the_usage(self):
        with mock.patch("ocdeck.app.collect_usage", return_value=report()) as collect:
            app = OCDeckApp(self.source(), auto_refresh=False)
            async with app.run_test(size=(120, 40)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.press("7")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(app.query_one("#tabs").active, "usage")
                self.assertIs(app.focused, app.query_one("#usage-view"))
                shown = str(app.query_one("#usage-content", Static).visual)
                self.assertIn("ANTHROPIC · CLAUDE CODE", shown)
                self.assertIn("88.0% left", shown)
                calls = collect.call_count
                await pilot.press("r")                       # refresh re-reads while the tab shows
                await app.workers.wait_for_complete()
                self.assertGreater(collect.call_count, calls)

    async def test_nothing_is_read_while_another_tab_is_showing(self):
        with mock.patch("ocdeck.app.collect_usage", return_value=report()) as collect:
            app = OCDeckApp(self.source(), auto_refresh=False)
            async with app.run_test(size=(120, 40)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.press("r")
                await app.workers.wait_for_complete()
                app._usage_tick()
                await app.workers.wait_for_complete()
                collect.assert_not_called()

    async def test_ctrl_arrows_cycle_through_the_usage_view_and_back(self):
        app = OCDeckApp(self.source(), auto_refresh=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("6")
            await pilot.press("ctrl+right")
            await pilot.pause()
            self.assertEqual(app.query_one("#tabs").active, "usage")
            await pilot.press("ctrl+right")
            await pilot.pause()
            self.assertEqual(app.query_one("#tabs").active, "overview")
            await pilot.press("7")
            await pilot.pause()
            self.assertEqual(app.query_one("#tabs").active, "usage")
