import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import agent_browser as browser


class AgentBrowserTests(unittest.TestCase):
    monitors = [
        {"connector": "eDP-1", "x": 0, "y": 0, "width": 1920, "height": 1200},
        {"connector": "DP-1", "x": 1920, "y": 0, "width": 3440, "height": 1440},
    ]

    def test_window_bounds_stay_inside_selected_monitor(self):
        for monitor in self.monitors:
            bounds = browser.monitor_bounds(self.monitors, monitor["connector"])
            self.assertGreaterEqual(bounds["left"], monitor["x"])
            self.assertGreaterEqual(bounds["top"], monitor["y"])
            self.assertLessEqual(bounds["left"] + bounds["width"], monitor["x"] + monitor["width"])
            self.assertLessEqual(bounds["top"] + bounds["height"], monitor["y"] + monitor["height"])
        with self.assertRaisesRegex(ValueError, "not active"):
            browser.monitor_bounds(self.monitors, "missing")

    def test_chrome_uses_dedicated_profile_loopback_debugging_and_positionable_window(self):
        config = {"browser": "/usr/bin/google-chrome-stable", "profile": "/dedicated/profile", "port": 9223}
        args = browser.chrome_command(config, browser.monitor_bounds(self.monitors, "eDP-1"))
        self.assertIn("--user-data-dir=/dedicated/profile", args)
        self.assertIn("--remote-debugging-address=127.0.0.1", args)
        self.assertIn("--window-position=32,48", args)
        self.assertIn("--ozone-platform=x11", args)
        self.assertIn("--renderer-process-limit=6", args)
        self.assertNotIn("--no-sandbox", args)
        self.assertNotIn("--headless=new", args)

    def test_headless_mode_drops_the_window_and_gpu(self):
        config = {"browser": "/usr/bin/google-chrome-stable", "profile": "/dedicated/profile",
                  "port": 9223, "headless": True}
        args = browser.chrome_command(config, browser.monitor_bounds(self.monitors, "eDP-1"))
        self.assertIn("--headless=new", args)
        self.assertIn("--disable-gpu", args)
        self.assertFalse(any(item.startswith("--window-position") for item in args))
        self.assertFalse(any(item.startswith("--ozone-platform") for item in args))

    def test_clean_closes_all_but_the_requested_pages(self):
        cdp = mock.Mock()
        cdp.call.side_effect = [
            {"targetInfos": [{"type": "page", "targetId": "a"}, {"type": "page", "targetId": "b"},
                             {"type": "page", "targetId": "c"}, {"type": "other", "targetId": "d"}]},
            {}, {},
        ]
        with mock.patch.object(browser, "CDP", return_value=cdp):
            self.assertEqual(browser.clean({}, keep=1), {"closed": 2, "remaining": 1})
        self.assertEqual([call.args[0] for call in cdp.call.call_args_list],
                         ["Target.getTargets", "Target.closeTarget", "Target.closeTarget"])
        cdp.close.assert_called_once()

    def test_restart_restores_saved_session_instead_of_opening_an_extra_home_tab(self):
        with TemporaryDirectory() as profile:
            config = {"browser": "/usr/bin/google-chrome-stable", "profile": profile, "port": 9223}
            bounds = browser.monitor_bounds(self.monitors, "eDP-1")
            self.assertIn("https://chatgpt.com/", browser.chrome_command(config, bounds))
            sessions = Path(profile) / "Default/Sessions"
            sessions.mkdir(parents=True)
            (sessions / "Session_saved").touch()
            args = browser.chrome_command(config, bounds)
            self.assertIn("--restore-last-session", args)
            self.assertNotIn("--new-window", args)
            self.assertNotIn("https://chatgpt.com/", args)

    def test_recovering_an_already_headed_browser_does_not_restart_or_resubmit(self):
        config = {"headless": False, "monitor": "eDP-1"}
        path = mock.Mock()
        report = {"needsAttention": True}
        with mock.patch.object(browser, "version", return_value={"User-Agent": "Chrome/152"}), \
                mock.patch.object(browser, "status", return_value=report), \
                mock.patch.object(browser.subprocess, "run") as run:
            result = browser.recover(config, path)
        self.assertFalse(result["restarted"])
        self.assertIn("Complete the site's verification", result["nextStep"])
        path.write_text.assert_not_called()
        run.assert_not_called()

    def test_checkpoint_restores_exact_web_tabs_when_native_restore_is_unavailable(self):
        with TemporaryDirectory() as profile:
            config = {"browser": "/usr/bin/google-chrome-stable", "profile": profile, "port": 9223}
            cdp = mock.Mock()
            cdp.call.return_value = {"targetInfos": [
                {"type": "page", "url": "https://chatgpt.com/c/project"},
                {"type": "page", "url": "https://calendar.google.com/"},
                {"type": "page", "url": "chrome://newtab/"},
                {"type": "service_worker", "url": "https://example.com/worker"},
            ]}
            with mock.patch.object(browser, "CDP", return_value=cdp):
                urls = browser.save_tabs(config)
            args = browser.chrome_command(config, browser.monitor_bounds(self.monitors, "eDP-1"))
            self.assertEqual(args[-3:], ["--new-window", "https://chatgpt.com/c/project", "https://calendar.google.com/"])
            self.assertEqual(urls, args[-2:])
            self.assertEqual([call.args[0] for call in cdp.call.call_args_list], ["Target.getTargets"])
            cdp.close.assert_called_once()

    def test_headed_recovery_checks_monitor_before_changing_mode(self):
        config = {"headless": True, "monitor": "missing"}
        path = mock.Mock()
        with mock.patch.object(browser, "monitors", return_value=self.monitors), \
                mock.patch.object(browser.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "not active"):
                browser.set_mode(config, path, False)
        self.assertTrue(config["headless"])
        path.write_text.assert_not_called()
        run.assert_not_called()

    def test_healthy_cdp_does_not_hide_a_challenge_or_claim_a_login(self):
        cdp = mock.Mock()
        pages = [{"type": "page", "targetId": "challenge", "title": "Just a moment...", "url": "https://chatgpt.com/"},
                 {"type": "page", "targetId": "other", "title": "ChatGPT", "url": "https://chatgpt.com/c/example"}]
        cdp.call.return_value = {"targetInfos": pages}
        with mock.patch.object(browser, "CDP", return_value=cdp), \
                mock.patch.object(browser, "window_ids", return_value=[]):
            result = browser.status({"profile": "/dedicated", "port": 9223, "monitor": "eDP-1", "headless": True})
        self.assertEqual(result["connection"], "ready")
        self.assertTrue(result["needsAttention"])
        self.assertEqual([page["state"] for page in result["pages"]], ["human-verification-required", "unverified"])
        cdp.close.assert_called_once()

    def test_cdp_skips_events_and_matches_the_response_id(self):
        connection = mock.Mock()
        connection.recv.side_effect = [json.dumps({"method": "Target.created"}),
                                       json.dumps({"id": 1, "result": {"windowId": 9}})]
        with mock.patch.object(browser, "version", return_value={"webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/browser/test"}), \
                mock.patch.object(browser.websocket, "create_connection", return_value=connection):
            cdp = browser.CDP({})
            self.assertEqual(cdp.call("Browser.getWindowForTarget", {"targetId": "test"}), {"windowId": 9})
            cdp.close()
        self.assertEqual(json.loads(connection.send.call_args.args[0])["params"], {"targetId": "test"})
        connection.close.assert_called_once()

    def test_placement_only_moves_windows_in_the_dedicated_cdp_browser(self):
        cdp = mock.Mock()
        cdp.call.side_effect = [
            {"targetInfos": [{"type": "page", "targetId": "a"}, {"type": "page", "targetId": "b"},
                             {"type": "service_worker", "targetId": "worker"}]},
            {"windowId": 7}, {"windowId": 7},
            {"bounds": {"left": 0, "top": 0, "width": 800, "height": 600, "windowState": "minimized"}},
            {}, {},
        ]
        with mock.patch.object(browser, "CDP", return_value=cdp), \
                mock.patch.object(browser, "monitors", return_value=self.monitors):
            result = browser.place({"monitor": "eDP-1"})
        self.assertEqual(result["windows"], [7])
        self.assertEqual(result["moved"], [7])
        changes = [call for call in cdp.call.call_args_list if call.args[0] == "Browser.setWindowBounds"]
        self.assertEqual(len(changes), 2)
        self.assertEqual(changes[-1].args[1]["bounds"]["left"], 32)
        cdp.close.assert_called_once()

    def test_placement_leaves_an_already_positioned_window_alone(self):
        cdp = mock.Mock()
        cdp.call.side_effect = [
            {"targetInfos": [{"type": "page", "targetId": "a"}]},
            {"windowId": 7},
            {"bounds": {"left": 32, "top": 48, "width": 1600, "height": 1050, "windowState": "normal"}},
        ]
        bounds = browser.monitor_bounds(self.monitors, "eDP-1")
        self.assertEqual((bounds["left"], bounds["top"], bounds["width"], bounds["height"]),
                         (32, 48, 1600, 1050))
        with mock.patch.object(browser, "CDP", return_value=cdp), \
                mock.patch.object(browser, "monitors", return_value=self.monitors):
            result = browser.place({"monitor": "eDP-1"})
        self.assertEqual(result["moved"], [])
        self.assertFalse(any(call.args[0] == "Browser.setWindowBounds" for call in cdp.call.call_args_list))


if __name__ == "__main__":
    unittest.main()


class LeanBrowserTests(unittest.TestCase):
    """Fewer, lighter renderers: no extensions on request, and no artifact panels or duplicate tabs on restart."""

    bounds = {"left": 32, "top": 48, "width": 1600, "height": 1050}
    CHAT = "https://claude.ai/chat/3dac03f2-ebf9-49e6-8ccd-423c5b99aed3"

    def config(self, profile="/dedicated/profile", **extra):
        return {"browser": "/usr/bin/google-chrome-stable", "profile": profile, "port": 9223, **extra}

    def test_extensions_stay_on_unless_the_config_turns_them_off(self):
        self.assertNotIn("--disable-extensions", browser.chrome_command(self.config(), self.bounds))
        self.assertNotIn("--disable-extensions", browser.chrome_command(self.config(extensions=True), self.bounds))
        self.assertIn("--disable-extensions", browser.chrome_command(self.config(extensions=False), self.bounds))
        self.assertIn("--disable-extensions", browser.chrome_command(self.config(extensions=False, headless=True), self.bounds))

    def test_the_extensions_setting_must_be_a_boolean(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "agent-browser.json"
            for value, ok in ((False, True), (True, True), ("no", False), (0, False)):
                path.write_text(json.dumps({"enabled": True, "port": 9223, "profile": folder,
                                            "browser": "/bin/sh", "extensions": value}))
                if ok:
                    self.assertEqual(browser.load_config(path)["extensions"], value)
                else:
                    with self.assertRaisesRegex(ValueError, "extensions"):
                        browser.load_config(path)

    def test_claude_artifact_panels_and_repeated_tabs_are_dropped_from_the_saved_addresses(self):
        saved = [f"{self.CHAT}?agentA=1&artifact=e400d287", f"{self.CHAT}?agentB=1&artifact=e400d287",
                 f"{self.CHAT}?agentB=1&artifact=e400d287", f"{self.CHAT}?agentB=1",
                 f"{self.CHAT}?artifact=e400&agentC=1&x=", "https://example.org/page?artifact=keep",
                 "https://notclaude.ai/chat?artifact=keep", "https://example.org/page?artifact=keep"]
        self.assertEqual(browser.tidy_destinations(saved), [
            f"{self.CHAT}?agentA=1", f"{self.CHAT}?agentB=1", f"{self.CHAT}?agentC=1&x=",
            "https://example.org/page?artifact=keep", "https://notclaude.ai/chat?artifact=keep"])
        self.assertEqual(browser.tidy_destinations([]), [])
        plain = [f"{self.CHAT}?agentA=1#top", "https://claude.ai/new"]
        self.assertEqual(browser.tidy_destinations(plain), plain)  # nothing to change: untouched

    def test_the_launch_reopens_the_tidied_addresses_only(self):
        with TemporaryDirectory() as profile:
            Path(profile, "opencode-tabs.json").write_text(json.dumps({"version": 1, "urls": [
                f"{self.CHAT}?agentB=1&artifact=e4", f"{self.CHAT}?agentB=1&artifact=e4", f"{self.CHAT}?agentC=1&artifact=e4"]}))
            args = browser.chrome_command(self.config(profile), self.bounds)
            reopened = args[args.index("--new-window") + 1:]
            self.assertEqual(reopened, [f"{self.CHAT}?agentB=1", f"{self.CHAT}?agentC=1"])
            self.assertIn("artifact=e4", Path(profile, "opencode-tabs.json").read_text())  # the file itself is not rewritten


class TabJanitorTests(unittest.TestCase):
    """Each agent tab closes once it has sat unchanged for the idle period."""

    CAL = ("cal", "https://calendar.google.com/calendar/r", "Calendar")

    def test_a_tab_untouched_for_the_idle_period_closes(self):
        janitor = browser.TabJanitor([], idle_seconds=300, now=0)
        pages = [("old", "https://pubmed.ncbi.nlm.nih.gov/1", "Paper"), ("new", "https://claude.ai/c", "Chat")]
        self.assertEqual(janitor.observe(pages, 0), [])
        janitor.observe([pages[0], ("new", "https://claude.ai/c", "Answer ready")], 200)  # new stays busy
        self.assertEqual(janitor.observe([pages[0], ("new", "https://claude.ai/c", "Answer ready")], 300), ["old"])

    def test_a_busy_tab_does_not_protect_stale_ones(self):
        janitor = browser.TabJanitor([], idle_seconds=300, now=0)
        for step in range(0, 301, 60):  # one tab changes every minute
            closing = janitor.observe([("stale", "https://a/", "A"), ("busy", "https://b/", f"t{step}")], step)
        self.assertEqual(closing, ["stale"])

    def test_keep_prefixes_and_the_newest_page_are_never_closed(self):
        janitor = browser.TabJanitor(["https://calendar.google.com/"], idle_seconds=60, now=0)
        pages = [self.CAL, ("a", "https://x/", "X"), ("b", "https://y/", "Y")]
        janitor.observe(pages, 0)
        closing = janitor.observe(pages, 60)
        self.assertEqual(sorted(closing), ["a", "b"])  # the kept Calendar tab keeps Chrome open
        only = browser.TabJanitor([], idle_seconds=60, now=0)
        only.observe([("last", "https://z/", "Z")], 0)
        self.assertEqual(only.observe([("last", "https://z/", "Z")], 999), [])

    def test_restored_tabs_are_cleaned_too_and_zero_disables(self):
        janitor = browser.TabJanitor([], idle_seconds=60, now=0)
        restored = [("r1", "https://old/", "Old"), ("r2", "https://older/", "Older")]
        janitor.observe(restored, 0)
        self.assertEqual(len(janitor.observe(restored, 60)), 1)
        off = browser.TabJanitor([], idle_seconds=0, now=0)
        off.observe(restored, 0)
        self.assertEqual(off.observe(restored, 10_000), [])

    def test_a_pass_closes_through_cdp_and_saves_the_tab_checkpoint(self):
        janitor = browser.TabJanitor([], idle_seconds=60, now=0)
        targets = {"targetInfos": [{"type": "page", "targetId": "keep", "url": "u", "title": "t"},
                                   {"type": "page", "targetId": "a1", "url": "x", "title": "y"},
                                   {"type": "service_worker", "targetId": "sw", "url": "s"}]}
        cdp = mock.Mock()
        cdp.call.side_effect = lambda method, params=None: targets if method == "Target.getTargets" else {}
        with mock.patch.object(browser, "CDP", return_value=cdp), \
                mock.patch.object(browser, "save_tabs") as save:
            self.assertEqual(browser.purge_idle_tabs({}, janitor, now=0), [])
            save.assert_not_called()
            closed = browser.purge_idle_tabs({}, janitor, now=60)
            self.assertEqual(len(closed), 1)
            save.assert_called_once()
        cdp.call.assert_any_call("Target.closeTarget", {"targetId": closed[0]})



class WaylandWindowTests(unittest.TestCase):
    def test_wayland_backend_drops_x11_positioning(self):
        config = {"browser": "/usr/bin/google-chrome-stable", "profile": "/dedicated/profile",
                  "port": 9223, "window_backend": "wayland"}
        args = browser.chrome_command(config, {"left": 32, "top": 48, "width": 1600, "height": 1050})
        self.assertIn("--ozone-platform=wayland", args)
        self.assertNotIn("--ozone-platform=x11", args)
        self.assertFalse(any(arg.startswith("--window-position") for arg in args))
        self.assertTrue(browser.wayland_window(config))
        self.assertFalse(browser.wayland_window({**config, "headless": True}))
        self.assertFalse(browser.wayland_window({k: v for k, v in config.items() if k != "window_backend"}))


class AgentTabAndCapTests(unittest.TestCase):
    """Agent chat tabs (?agentA=1 ...) are never auto-closed; the rest is kept tidy."""

    CHAT = "https://claude.ai/chat/3dac03f2?agentA=1&artifact=e400"

    def test_the_marker_is_read_only_from_a_real_agent_query_flag(self):
        for url, marker in ((self.CHAT, "agentA"), ("https://x/?a=1&agentC=1", "agentC"),
                            ("https://x/?agentB=1#top", "agentB"), ("https://x/?agentB=1", "agentB")):
            self.assertEqual(browser.agent_marker(url), marker, url)
        for url in ("https://x/?agentA=10", "https://x/?agentAB=1", "https://x/agentA=1",
                    "https://x/?notagentA=1", "https://x/", ""):
            self.assertIsNone(browser.agent_marker(url), url)

    def test_an_idle_agent_tab_is_never_closed_but_ordinary_idle_tabs_are(self):
        janitor = browser.TabJanitor([], idle_seconds=900, now=0)
        pages = [("chat", self.CHAT, "Claude"), ("paper", "https://pubmed/1", "Paper"),
                 ("chat2", "https://claude.ai/chat/9?agentC=1", "Claude")]
        janitor.observe(pages, 0)
        self.assertEqual(janitor.observe(pages, 10_000), ["paper"])

    def test_the_idle_period_defaults_to_fifteen_minutes(self):
        self.assertEqual(browser.DEFAULT_PURGE_IDLE_MINUTES, 15)
        self.assertEqual(browser.DEFAULT_MAX_TABS, 12)

    def test_over_the_cap_the_oldest_idle_ordinary_tabs_close_first(self):
        janitor = browser.TabJanitor([], idle_seconds=900, now=0, max_tabs=4)
        pages = [(f"t{index}", f"https://site/{index}", "T") for index in range(4)]
        pages += [("chat", self.CHAT, "Claude"), ("fresh", "https://site/new", "New")]
        janitor.observe(pages[:4] + [pages[4]], 0)
        janitor.observe(pages, 100)  # "fresh" appears at t=100; the others have been idle longer
        closing = janitor.observe(pages, 200)
        # 6 tabs, cap 4: two go. Idle for at least 120 s is required, so "fresh" (100 s) stays;
        # the chat tab is exempt; the two oldest of t0..t3 close.
        self.assertEqual(len(closing), 2)
        self.assertNotIn("chat", closing)
        self.assertNotIn("fresh", closing)

    def test_the_cap_never_closes_a_tab_that_is_still_busy(self):
        janitor = browser.TabJanitor([], idle_seconds=0, now=0, max_tabs=1)
        pages = [("a", "https://a/", "A"), ("b", "https://b/", "B")]
        janitor.observe(pages, 0)
        self.assertEqual(janitor.observe(pages, 60), [])  # idle only 60 s
        self.assertEqual(len(janitor.observe(pages, 130)), 1)


class MemoryGuardTests(unittest.TestCase):
    GB = 2**30

    def test_plan_flags_then_reloads_and_never_closes(self):
        heaps = {"small": ("agentA", self.GB // 2), "heavy": ("agentB", 2 * self.GB),
                 "huge": ("agentC", 4 * self.GB)}
        flags, reloads = browser.plan_memory_actions(heaps, 20 * self.GB, now=1000, last_reload={})
        self.assertEqual(sorted(flags), ["heavy", "huge"])
        self.assertEqual(reloads, ["huge"])

    def test_a_short_machine_reloads_only_the_heaviest_flagged_tab(self):
        heaps = {"a": ("agentA", 2 * self.GB), "b": ("agentB", int(1.6 * self.GB)),
                 "c": ("agentC", self.GB // 4)}
        _flags, reloads = browser.plan_memory_actions(heaps, self.GB, now=1000, last_reload={})
        self.assertEqual(reloads, ["a"])
        _flags, reloads = browser.plan_memory_actions({"c": heaps["c"]}, self.GB, now=1000, last_reload={})
        self.assertEqual(reloads, [])  # nothing is heavy enough to be worth a reload

    def test_a_reloaded_tab_is_left_alone_for_the_cooldown(self):
        heaps = {"huge": ("agentC", 4 * self.GB)}
        self.assertEqual(browser.plan_memory_actions(heaps, None, 1000, {"huge": 900})[1], [])
        self.assertEqual(browser.plan_memory_actions(heaps, None, 1600, {"huge": 900})[1], ["huge"])

    def test_a_pass_writes_flags_reloads_when_the_lock_is_free_and_closes_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            lock = Path(base) / "browser.lock"
            config = {"flag_dir": str(Path(base) / "flags"), "browser_lock": str(lock)}
            cdp = mock.Mock()
            sizes = {"mid": 2 * self.GB, "huge": 4 * self.GB, "ok": self.GB // 8}
            pages = [("mid", "https://c/1?agentA=1", "T"), ("huge", "https://c/2?agentB=1", "T"),
                     ("ok", "https://c/3?agentC=1", "T"), ("plain", "https://c/4", "T")]
            with mock.patch.object(browser, "CDP", return_value=cdp), \
                    mock.patch.object(browser, "page_targets", return_value=pages), \
                    mock.patch.object(browser, "measure_heap", side_effect=lambda _cdp, target: sizes[target]), \
                    mock.patch.object(browser, "available_memory_bytes", return_value=20 * self.GB), \
                    mock.patch.object(browser, "reload_tab") as reload:
                heaps, done = browser.memory_pass(config, {}, now=1000)
            self.assertEqual(done, ["huge"])
            reload.assert_called_once_with(cdp, "huge")
            self.assertTrue((Path(base) / "flags/agentA_reload").exists())
            self.assertTrue((Path(base) / "flags/agentB_reload").exists())
            self.assertFalse((Path(base) / "flags/agentC_reload").exists())
            self.assertNotIn("plain", heaps)  # only agent tabs are measured
            for call in cdp.call.mock_calls:
                self.assertNotIn("closeTarget", str(call))

    def test_a_busy_browser_lock_defers_the_reload(self):
        import fcntl, tempfile
        with tempfile.TemporaryDirectory() as base:
            lock = Path(base) / "browser.lock"
            config = {"flag_dir": str(Path(base) / "flags"), "browser_lock": str(lock)}
            held = open(lock, "a+")
            fcntl.flock(held, fcntl.LOCK_EX)  # an agent is mid-action
            try:
                with mock.patch.object(browser, "CDP", return_value=mock.Mock()), \
                        mock.patch.object(browser, "page_targets", return_value=[("t", "https://c/?agentA=1", "T")]), \
                        mock.patch.object(browser, "measure_heap", return_value=4 * self.GB), \
                        mock.patch.object(browser, "available_memory_bytes", return_value=None), \
                        mock.patch.object(browser, "reload_tab") as reload:
                    _heaps, done = browser.memory_pass(config, {}, now=1000)
                reload.assert_not_called()
                self.assertEqual(done, [])
            finally:
                held.close()

    def test_a_cleared_flag_is_removed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            flags = Path(base) / "flags"
            flags.mkdir()
            (flags / "agentA_reload").write_text("old")
            with mock.patch.object(browser, "CDP", return_value=mock.Mock()), \
                    mock.patch.object(browser, "page_targets", return_value=[("t", "https://c/?agentA=1", "T")]), \
                    mock.patch.object(browser, "measure_heap", return_value=self.GB // 8), \
                    mock.patch.object(browser, "available_memory_bytes", return_value=None):
                browser.memory_pass({"flag_dir": str(flags)}, {}, now=1000)
            self.assertFalse((flags / "agentA_reload").exists())
