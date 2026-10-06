"""Terminal browser launcher tests; no sessions, terminals, or files are created."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from ocdeck.browser_access import BrowserAccessResult
from ocdeck.browser_cli import main, project_directory

ROOT = Path(__file__).resolve().parents[1]


class BrowserCliTests(unittest.TestCase):
    def setUp(self):
        selection = mock.patch("ocdeck.browser_cli.read_saved_backend", return_value="v1")
        selection.start()
        self.addCleanup(selection.stop)

    def source(self, directory=ROOT):
        def request(path, password, *, method="GET", payload=None):
            if method == "POST":
                return {"info": {"role": "user"}, "parts": payload["parts"]}
            return {"id": path.split("/")[2], "directory": str(directory), "agent": "build",
                    "model": {"providerID": "openai", "id": "gpt-6-astra", "variant": "max"}}
        return SimpleNamespace(
            opencode_bin="/bin/false", api_url="http://127.0.0.1:6500",
            api_url_error="", _password=mock.Mock(return_value="synthetic-secret"),
            _request_json=mock.Mock(side_effect=request),
            server_env_file=Path("/unused/server.env"), projects_file=Path("/unused/projects.md"),
            create_browser_session=mock.AsyncMock(return_value=BrowserAccessResult("ses_new")),
            enable_session_browser=mock.AsyncMock(return_value=BrowserAccessResult("ses_existing")),
        )

    def test_nearest_registered_root_is_selected_for_nested_terminals(self):
        projects = [SimpleNamespace(path=ROOT.parent), SimpleNamespace(path=ROOT)]
        helper = {"ha": SimpleNamespace(parse_catalog=lambda _: projects)}
        with mock.patch("ocdeck.browser_cli.grant_helper", return_value=helper):
            self.assertEqual(project_directory(self.source(), ROOT / "tests"), ROOT)
            projects.append(SimpleNamespace(path=ROOT))
            with self.assertRaisesRegex(ValueError, "conflicting"):
                project_directory(self.source(), ROOT)
            projects.clear()
            with self.assertRaisesRegex(ValueError, "Register"):
                project_directory(self.source(), ROOT)

    def test_new_terminal_command_creates_then_attaches_without_model_or_prompt(self):
        source = self.source()
        with mock.patch("ocdeck.browser_cli.DashboardSource", return_value=source) as factory, \
                mock.patch("ocdeck.browser_cli.project_directory", return_value=ROOT) as resolve, \
                mock.patch("ocdeck.browser_cli.attach_session", return_value=0) as attach, redirect_stdout(StringIO()):
            self.assertEqual(main([]), 0)
        factory.assert_called_once_with(backend="v1", api_url=None)
        resolve.assert_called_once_with(source, ".")
        source.create_browser_session.assert_awaited_once_with(ROOT)
        source.enable_session_browser.assert_not_awaited()
        args = attach.call_args.args[0]
        self.assertEqual(args[args.index("--session") + 1], "ses_new")
        self.assertTrue({"--model", "--agent", "--prompt", "--auto", "--password"}.isdisjoint(args))

    def test_existing_session_command_does_not_create_or_fork(self):
        source = self.source()
        with mock.patch("ocdeck.browser_cli.DashboardSource", return_value=source), \
                mock.patch("ocdeck.browser_cli.project_directory", return_value=ROOT), \
                mock.patch("ocdeck.browser_cli.attach_session", return_value=0), redirect_stdout(StringIO()):
            self.assertEqual(main([str(ROOT), "--session", "ses_existing"]), 0)
        source.enable_session_browser.assert_awaited_once_with(ROOT, "ses_existing")
        source.create_browser_session.assert_not_awaited()

    def test_selected_v2_uses_native_resume_without_v1_credentials(self):
        source = self.source()
        source.backend = "v2"
        source._v2_api_json = mock.AsyncMock(return_value={"data": {
            "id": "ses_existing", "location": {"directory": str(ROOT)},
            "agent": "build", "model": {"providerID": "openai", "id": "gpt-6-astra"},
        }})
        with mock.patch("ocdeck.browser_cli.read_saved_backend", return_value="v2"), \
                mock.patch("ocdeck.browser_cli.DashboardSource", return_value=source) as factory, \
                mock.patch("ocdeck.browser_cli.attach_session") as legacy, \
                mock.patch("ocdeck.browser_cli.os.execvpe") as execute, redirect_stdout(StringIO()):
            self.assertEqual(main(["--session", "ses_existing"]), 0)
        factory.assert_called_once_with(backend="v2", api_url=None)
        legacy.assert_not_called()
        source._password.assert_not_called()
        self.assertEqual(execute.call_args.args[1], ["/bin/false", "--server", source.api_url, str(ROOT), "--session", "ses_existing"])

    def test_session_only_uses_saved_root_even_inside_another_registered_project(self):
        source = self.source(ROOT.parent)
        with mock.patch("ocdeck.browser_cli.DashboardSource", return_value=source), \
                mock.patch("ocdeck.browser_cli.project_directory", side_effect=AssertionError("Do not infer from cwd")), \
                mock.patch("ocdeck.browser_cli.attach_session", return_value=0) as attach, redirect_stdout(StringIO()):
            self.assertEqual(main(["--session", "ses_existing"]), 0)
        source.enable_session_browser.assert_awaited_once_with(ROOT.parent, "ses_existing")
        args = attach.call_args.args[0]
        self.assertEqual(args[args.index("--directory") + 1], str(ROOT.parent))

    def test_explicit_conflicting_directory_reports_the_actual_root(self):
        source = self.source(ROOT.parent)
        with mock.patch("ocdeck.browser_cli.DashboardSource", return_value=source), \
                mock.patch("ocdeck.browser_cli.project_directory", return_value=ROOT), \
                mock.patch("ocdeck.browser_cli.attach_session") as attach, redirect_stderr(StringIO()) as output:
            self.assertEqual(main([str(ROOT), "--session", "ses_existing"]), 1)
        self.assertIn(str(ROOT.parent), output.getvalue())
        source.enable_session_browser.assert_not_awaited()
        source.create_browser_session.assert_not_awaited()
        attach.assert_not_called()

    def test_chatgpt_url_is_a_page_target_and_never_an_authenticated_api_destination(self):
        source = self.source()
        page = "https://chatgpt.com/c/6aa4f5ec-ed54-83ea-ac73-948ad10d4e8d"
        with mock.patch("ocdeck.browser_cli.DashboardSource", return_value=source) as factory, \
                mock.patch("ocdeck.browser_cli.attach_session", return_value=0) as attach, redirect_stdout(StringIO()):
            self.assertEqual(main(["--session", "ses_existing", "--url", page]), 0)
        factory.assert_called_once_with(backend="v1", api_url=None)
        source.create_browser_session.assert_not_awaited()
        posted = [call for call in source._request_json.call_args_list if call.kwargs.get("method") == "POST"]
        self.assertEqual(len(posted), 1)
        self.assertTrue(posted[0].args[0].startswith("/session/ses_existing/message?directory="))
        payload = posted[0].kwargs["payload"]
        self.assertTrue(payload["noReply"])
        self.assertIn(page, payload["parts"][0]["text"])
        self.assertEqual(payload["model"], {"providerID": "openai", "modelID": "gpt-6-astra"})
        self.assertEqual(payload["variant"], "max")
        args = attach.call_args.args[0]
        self.assertEqual(args[args.index("--url") + 1], source.api_url)
        self.assertNotIn(page, args)

    def test_explicit_api_url_and_legacy_loopback_url_select_only_the_backend(self):
        for option in ("--api-url", "--url"):
            source = self.source()
            with self.subTest(option=option), mock.patch("ocdeck.browser_cli.DashboardSource", return_value=source) as factory, \
                    mock.patch("ocdeck.browser_cli.attach_session", return_value=0), redirect_stdout(StringIO()):
                self.assertEqual(main(["--session", "ses_existing", option, "http://127.0.0.1:6500"]), 0)
            factory.assert_called_once_with(backend="v1", api_url="http://127.0.0.1:6500")
            self.assertFalse(any(c.kwargs.get("method") == "POST" for c in source._request_json.call_args_list))

    def test_failed_target_recording_retains_session_without_replay(self):
        source = self.source()
        request = source._request_json.side_effect
        def failing(path, password, **kwargs):
            if kwargs.get("method") == "POST":
                raise TimeoutError()
            return request(path, password, **kwargs)
        source._request_json.side_effect = failing
        with mock.patch("ocdeck.browser_cli.DashboardSource", return_value=source), \
                mock.patch("ocdeck.browser_cli.attach_session") as attach, redirect_stderr(StringIO()) as output:
            self.assertEqual(main(["--session", "ses_existing", "--url", "https://chatgpt.com/c/example"]), 2)
        self.assertIn("Session retained: ses_existing", output.getvalue())
        self.assertEqual(sum(c.kwargs.get("method") == "POST" for c in source._request_json.call_args_list), 1)
        attach.assert_not_called()

    def test_invalid_page_urls_fail_before_backend_or_credential_access(self):
        for page in ("file:///tmp/note", "https://user:password@example.com/", "https://example.com/ bad", "https://example.com:99999/"):
            with self.subTest(page=page), mock.patch("ocdeck.browser_cli.DashboardSource", side_effect=AssertionError("No startup effects")), \
                    redirect_stderr(StringIO()), self.assertRaises(SystemExit) as stop:
                main(["--url", page])
            self.assertEqual(stop.exception.code, 2)

    def test_uncertain_creation_reports_retained_session_without_attach_or_retry(self):
        source = self.source()
        source.create_browser_session.return_value = BrowserAccessResult("ses_retained", "Inspect before retrying", True)
        with mock.patch("ocdeck.browser_cli.DashboardSource", return_value=source), \
                mock.patch("ocdeck.browser_cli.project_directory", return_value=ROOT), \
                mock.patch("ocdeck.browser_cli.attach_session") as attach, redirect_stderr(StringIO()) as output:
            self.assertEqual(main([]), 2)
        source.create_browser_session.assert_awaited_once()
        attach.assert_not_called()
        self.assertIn("ses_retained", output.getvalue())

    def test_help_does_not_load_credentials_or_contact_a_backend(self):
        with mock.patch("ocdeck.browser_cli.DashboardSource", side_effect=AssertionError("No startup effects")), \
                redirect_stdout(StringIO()) as output, self.assertRaises(SystemExit) as stop:
            main(["--help"])
        self.assertEqual(stop.exception.code, 0)
        self.assertIn("/models", output.getvalue())
        self.assertIn("--session", output.getvalue())

    def test_empty_existing_session_never_falls_through_to_new_creation(self):
        with mock.patch("ocdeck.browser_cli.DashboardSource", side_effect=AssertionError("No startup effects")), \
                redirect_stderr(StringIO()), self.assertRaises(SystemExit) as stop:
            main(["--session", ""])
        self.assertEqual(stop.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
