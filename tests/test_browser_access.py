"""Browser controls and operator integration, using no live API or cleanup."""

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from textual.widgets import Button, DataTable, Input, TabbedContent

from ocdeck.app import OCDeckApp
from ocdeck.browser_access import BrowserAccessResult, browser_access, grant_helper
from ocdeck.browser_session import main as attach
from ocdeck.models import DashboardSnapshot, ProjectRecord, SessionRecord, parse_sessions
from ocdeck.source import DashboardSource

ROOT = Path(__file__).resolve().parent


class BrowserSource:
    backend = "v1"
    opencode_bin = "/usr/bin/opencode"
    api_url = "http://127.0.0.1:6500"
    server_env_file = Path("/unused/server.env")

    def __init__(self):
        self.created = []
        self.granted = []
        self.release = None
        self.failure = ""
        self.session = SessionRecord("ses_astra", "User session", str(ROOT), "project", 1, 2,
                                     agent="build", model="openai/gpt-6-astra")

    async def collect(self):
        return DashboardSnapshot(sessions=(self.session,),
                                 projects=(ProjectRecord("project", str(ROOT), "Project"),), connection="live")

    async def enable_session_browser(self, directory, session_id):
        self.granted.append((directory, session_id))
        if self.release:
            await self.release.wait()
        return BrowserAccessResult(session_id, self.failure)

    async def create_browser_session(self, directory):
        self.created.append(directory)
        return BrowserAccessResult("ses_created", self.failure)


class ControlsTests(unittest.IsolatedAsyncioTestCase):
    async def test_shift_n_opens_blank_browser_session_on_the_selected_backend(self):
        source = BrowserSource()
        app = OCDeckApp(source, auto_refresh=False)
        app._launch_tmux = mock.Mock(return_value=True)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.press("1")
            app.query_one("#projects-table", DataTable).focus()
            await pilot.press("N")
            await app.workers.wait_for_complete()
        self.assertEqual(source.created, [ROOT])
        name, directory, command = app._launch_tmux.call_args.args
        self.assertEqual((name, directory), ("oc-browser-ses_created", ROOT))
        self.assertIn("ocdeck.browser_session", command)
        self.assertEqual(command[command.index("--url") + 1], source.api_url)
        self.assertEqual(command[command.index("--session") + 1], "ses_created")
        self.assertTrue({"--model", "--agent", "--prompt", "--auto", "--password"}.isdisjoint(command))

    async def test_shift_b_preserves_selected_astra_or_sol_and_enter_uses_connected_terminal(self):
        for model in ("openai/gpt-6-astra", "openai/gpt-5.6-sol"):
            with self.subTest(model=model):
                source = BrowserSource()
                source.session = replace(source.session, model=model)
                app = OCDeckApp(source, auto_refresh=False)
                app._launch_tmux = mock.Mock(return_value=True)
                app._attach_live_terminal = mock.Mock(side_effect=AssertionError("Do not resume a stale standalone client"))
                async with app.run_test(size=(140, 42)) as pilot:
                    await pilot.pause()
                    await pilot.press("1")
                    app.query_one("#sessions-table", DataTable).focus()
                    await pilot.press("B")
                    await app.workers.wait_for_complete()
                    await pilot.press("enter")
                    self.assertEqual(source.session.model, model)
                self.assertEqual(source.granted, [(ROOT, "ses_astra")])
                self.assertEqual(source.created, [])
                self.assertEqual(app._launch_tmux.call_args.args[0], "oc-browser-ses_astra")

    async def test_repeated_shortcut_does_not_submit_a_concurrent_grant(self):
        source = BrowserSource()
        source.release = asyncio.Event()
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.press("1")
            app.query_one("#sessions-table", DataTable).focus()
            await pilot.press("B", "B")
            self.assertEqual(source.granted, [(ROOT, "ses_astra")])
            source.release.set()
            await app.workers.wait_for_complete()

    async def test_shortcuts_do_not_mutate_from_inputs_next_busy_or_child_sessions(self):
        source = BrowserSource()
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.press("1")
            search = app.query_one("#session-search", Input)
            search.focus()
            await pilot.press("B", "N")
            self.assertEqual(search.value, "BN")
            search.value = ""
            await pilot.pause()
            for change in ({"status": "busy"}, {"parent_id": "ses_parent"}, {"permission_id": "per_pending"}):
                app._apply_snapshot(replace(await source.collect(), sessions=(replace(source.session, **change),)))
                app.query_one("#sessions-table", DataTable).focus()
                app.action_enable_browser()
            app.query_one("#tabs", TabbedContent).active = "next"
            app.action_enable_browser()
            app.action_new_browser_session()
            self.assertEqual((source.created, source.granted), ([], []))

    async def test_buttons_and_v2_do_not_fallback_or_delete_failed_launches(self):
        source = BrowserSource()
        app = OCDeckApp(source, auto_refresh=False)
        app._launch_tmux = mock.Mock(return_value=False)
        source.remove_session = mock.AsyncMock()
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.press("1")
            await pilot.click("#new-browser-session")
            await app.workers.wait_for_complete()
            self.assertEqual(source.created, [ROOT])
            source.remove_session.assert_not_awaited()
            await pilot.click("#enable-browser")
            await app.workers.wait_for_complete()
            self.assertEqual(source.granted, [(ROOT, "ses_astra")])
            source.backend = "v2"
            app._apply_snapshot(await source.collect())
            self.assertFalse(app.query_one("#new-browser-session", Button).disabled)
            app.action_enable_browser()
            app.action_new_browser_session()
            await app.workers.wait_for_complete()
            self.assertEqual(len(source.created), 2)
            self.assertEqual(len(source.granted), 2)
            source.remove_session.assert_not_awaited()


def fake_helper():
    """A stand-in for an optional Home Agent browser-grant helper."""
    @dataclass
    class Settings:
        api_version: str = "v1"
        api_url: str = "http://127.0.0.1:9999"
        catalog_file: Path = ROOT
        registry_file: Path = ROOT
        routes_file: Path = ROOT
        server_env: Path = ROOT

    class GrantError(Exception):
        pass

    def require(ok, message):
        if not ok:
            raise GrantError(message)

    ha = SimpleNamespace(Settings=SimpleNamespace(from_environment=Settings),
                         parse_catalog=lambda _: (), OpenCodeAPI=lambda *_: SimpleNamespace())
    return {"ha": ha, "require": require, "GrantError": GrantError,
            "INSPECTION_ERRORS": (OSError, ValueError)}


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def source(self, **kwargs):
        return DashboardSource(backend=kwargs.pop("backend", "v1"), api_url="http://127.0.0.1:6500",
                               opencode_bin="/bin/false", server_env_file="/unused/server.env", **kwargs)

    async def test_uncertain_creation_is_not_replayed_on_another_click(self):
        source = self.source()
        result = BrowserAccessResult("ses_retained", "Inspect the result", True)
        with mock.patch("ocdeck.source.browser_access", return_value=result) as create:
            self.assertEqual(await source.create_browser_session(ROOT), result)
            self.assertEqual(await source.create_browser_session(ROOT), result)
        create.assert_called_once_with(source, ROOT)

    def test_bridge_preserves_backend_affinity_and_passes_no_model_selection(self):
        @dataclass
        class Settings:
            api_version: str = "v2"
            api_url: str = "http://127.0.0.1:9999"
            catalog_file: Path = ROOT
            registry_file: Path = ROOT
            routes_file: Path = ROOT
            server_env: Path = ROOT

        class GrantError(Exception):
            pass

        source = self.source()
        api = SimpleNamespace()
        ha = SimpleNamespace(Settings=SimpleNamespace(from_environment=Settings),
                             parse_catalog=lambda _: [SimpleNamespace(path=ROOT, name="Project")],
                             OpenCodeAPI=mock.Mock(return_value=api))
        helper = {"ha": ha, "require": lambda ok, message: self.assertTrue(ok, message),
                  "GrantError": GrantError, "INSPECTION_ERRORS": (OSError, ValueError),
                  "enable": mock.Mock(return_value={"status": "granted", "sessionID": "ses_sol"}),
                  "create": mock.Mock(return_value={"status": "created", "sessionID": "ses_new"})}
        with mock.patch("ocdeck.browser_access.grant_helper", return_value=helper):
            self.assertEqual(browser_access(source, ROOT, "ses_sol").session_id, "ses_sol")
            settings = ha.OpenCodeAPI.call_args.args[0]
            self.assertEqual((settings.api_version, settings.api_url), ("v1", source.api_url))
            self.assertEqual(settings.catalog_file, source.projects_file)
            helper["enable"].assert_called_once_with(settings, api, "Project", "ses_sol", grant=True)
            self.assertEqual(browser_access(source, ROOT).session_id, "ses_new")
            helper["create"].assert_called_once_with(settings, api, "Project")

    def test_v2_refuses_before_loading_any_v1_helper_or_credentials(self):
        source = self.source(backend="v2")
        with mock.patch("ocdeck.browser_access.grant_helper", side_effect=AssertionError("V1 fallback")):
            self.assertIn("V1", browser_access(source, ROOT, "ses_v2").error)

    def test_discovered_directory_uses_native_creation_without_registration(self):
        source = self.source()
        helper = fake_helper()
        api = SimpleNamespace()
        native = mock.Mock(return_value={"status": "created", "sessionID": "ses_native"})
        helper["create_native"] = native
        with mock.patch("ocdeck.browser_access.grant_helper", return_value=helper), \
                mock.patch.object(helper["ha"], "parse_catalog", return_value=()), \
                mock.patch.object(helper["ha"], "OpenCodeAPI", return_value=api):
            result = browser_access(source, ROOT)
        self.assertEqual(result, BrowserAccessResult("ses_native"))
        settings, used_api, directory = native.call_args.args
        self.assertEqual((used_api, directory), (api, ROOT))
        self.assertEqual(settings.catalog_file, source.projects_file)
        self.assertEqual(settings.api_url, source.api_url)

    def test_duplicate_catalog_roots_never_fall_back_to_native_creation(self):
        source = self.source()
        helper = fake_helper()
        helper["create_native"] = mock.Mock()
        project = SimpleNamespace(path=ROOT, name="Duplicate")
        with mock.patch("ocdeck.browser_access.grant_helper", return_value=helper), \
                mock.patch.object(helper["ha"], "parse_catalog", return_value=(project, project)), \
                mock.patch.object(helper["ha"], "OpenCodeAPI", return_value=SimpleNamespace()):
            result = browser_access(source, ROOT)
        self.assertTrue(result.error)
        helper["create_native"].assert_not_called()


class AttachmentAndVisibilityTests(unittest.TestCase):
    def test_attach_selects_no_model_and_keeps_credentials_out_of_arguments(self):
        args = ["--opencode", "/bin/false", "--url", "http://127.0.0.1:6500", "--directory", str(ROOT),
                "--session", "ses_existing", "--server-env", "/unused/server.env"]
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("ocdeck.browser_session.read_server_credentials", return_value=("operator", "synthetic-secret")), \
                mock.patch("ocdeck.browser_session.os.execvpe") as execute:
            self.assertEqual(attach(args), 0)
        executable, command, environment = execute.call_args.args
        self.assertEqual(executable, "/bin/false")
        self.assertEqual(command, ["/bin/false", "attach", "http://127.0.0.1:6500", "--dir", str(ROOT), "--session", "ses_existing"])
        self.assertNotIn("synthetic-secret", command)
        self.assertEqual(environment["OPENCODE_SERVER_PASSWORD"], "synthetic-secret")

    def test_interactive_browser_grants_remain_in_the_main_sessions_list(self):
        home = {"kind": "project-worker", "source": "operator-browser-grant", "browserEnabled": True}
        item = {"id": "ses_user", "title": "My session", "directory": str(ROOT), "metadata": {"homeAgent": home}}
        session = parse_sessions([item])[0]
        self.assertEqual(session.agent_session_kind, "")
        self.assertTrue(session.browser_enabled)
        child = parse_sessions([{**item, "parentID": "ses_parent"}])[0]
        self.assertEqual(child.agent_session_kind, "Subagent")
        self.assertFalse(child.browser_enabled)

    def test_native_browser_sessions_keep_the_connected_terminal_route_after_refresh(self):
        item = {"id": "ses_native", "title": "Home browser", "directory": str(ROOT),
                "metadata": {"ocdeckBrowser": {"version": 1, "source": "operator-native-browser", "directory": str(ROOT)}}}
        session = parse_sessions([item])[0]
        self.assertTrue(session.browser_enabled)
        self.assertEqual(session.agent_session_kind, "")
        self.assertFalse(parse_sessions([{**item, "parentID": "ses_parent"}])[0].browser_enabled)
        self.assertFalse(parse_sessions([{**item, "directory": "/another/path"}])[0].browser_enabled)


if __name__ == "__main__":
    unittest.main()


class MissingHelperTests(unittest.TestCase):
    def test_without_a_configured_helper_browser_actions_report_it_unavailable(self):
        grant_helper.cache_clear()
        with mock.patch.dict("os.environ", {"OCDECK_BROWSER_GRANT_HELPER": ""}):
            source = DashboardSource(backend="v1", api_url="http://127.0.0.1:6500",
                                     opencode_bin="/bin/false", server_env_file="/unused/server.env")
            result = browser_access(source, ROOT)
        grant_helper.cache_clear()
        self.assertEqual(result.error, "The browser grant helper is unavailable")
