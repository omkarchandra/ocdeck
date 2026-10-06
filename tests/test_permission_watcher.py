from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from ocdeck.source import V2Location
from ocdeck_permission_watcher import (
    main as watcher_main,
    notification_candidates,
    parse_args,
    pending_requests,
    runtime_state_file,
    save_seen,
    start_completion_watcher,
    v2_api_json,
    v2_pending_requests,
    v2_request_locations,
)


class PermissionWatcherTests(unittest.TestCase):
    def test_backend_defaults_to_v2_with_explicit_v1_rollback(self) -> None:
        self.assertEqual(parse_args([]).backend, "v2")
        self.assertEqual(parse_args(["--backend", "v1"]).backend, "v1")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["--back", "v1"])
        self.assertNotEqual(runtime_state_file("v2"), runtime_state_file("v1"))

        with mock.patch.dict(
            os.environ, {"OCDECK_OPENCODE_BACKEND": "invalid"}, clear=False
        ):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args([])
            self.assertEqual(parse_args(["--backend", "v1"]).backend, "v1")

    def test_v2_api_uses_cli_operation_id_and_explicit_server(self) -> None:
        result = mock.Mock(
            returncode=0,
            stdout=(
                b'{"location":{"directory":"/work/alpha",'
                b'"project":{"id":"project-1",'
                b'"directory":"/work/alpha","canonical":"/work/alpha"}},'
                b'"data":[]}'
            ),
        )
        with mock.patch(
            "ocdeck_permission_watcher.subprocess.run", return_value=result
        ) as runner:
            payload = v2_api_json(
                "/usr/bin/opencode2",
                "v2.form.request.list",
                server="https://example.test",
                location=V2Location("/work/alpha"),
            )

        self.assertEqual(payload["data"], [])
        command = runner.call_args.args[0]
        self.assertEqual(
            command[:3],
            ["/usr/bin/opencode2", "api", "form.list"],
        )
        self.assertEqual(
            command[3:],
            [
                "--server",
                "https://example.test",
                "--param",
                "location[directory]=/work/alpha",
            ],
        )
        self.assertNotIn("cwd", runner.call_args.kwargs)
        self.assertNotIn("Authorization", " ".join(command))

        result.stdout = result.stdout.replace(b"/work/alpha", b"/work/other")
        with mock.patch(
            "ocdeck_permission_watcher.subprocess.run", return_value=result
        ):
            self.assertIsNone(
                v2_api_json(
                    "/usr/bin/opencode2",
                    "v2.form.request.list",
                    location=V2Location("/work/alpha"),
                )
            )

    def test_v1_credentials_are_never_sent_over_remote_plain_http(self) -> None:
        with mock.patch(
            "ocdeck_permission_watcher.urllib.request.build_opener"
        ) as opener:
            requests = pending_requests(
                "http://example.test",
                {"OPENCODE_SERVER_PASSWORD": "secret"},
            )

        self.assertEqual(requests, ([], False))
        opener.assert_not_called()

    def test_seen_state_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "seen.json"
            save_seen(path, {"permission:ses_one:per_one"})
            mode = path.stat().st_mode & 0o777

        self.assertEqual(mode, 0o600)

    def test_v2_location_discovery_uses_authoritative_loaded_locations(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            idle = root / "idle"
            live = root / "live"
            idle.mkdir()
            live.mkdir()
            calls: list[str] = []

            def fake_api(_binary, operation, **kwargs):
                calls.append(operation)
                return [
                    {"directory": str(idle)},
                    {"directory": str(live), "workspaceID": "wrk_live"},
                    {"directory": str(live), "workspaceID": "wrk_other"},
                ]

            with mock.patch(
                "ocdeck_permission_watcher.v2_api_json", side_effect=fake_api
            ):
                locations, complete = v2_request_locations("/usr/bin/opencode2")

        self.assertTrue(complete)
        self.assertEqual(
            locations,
            (
                V2Location(str(idle)),
                V2Location(str(live), "wrk_live"),
                V2Location(str(live), "wrk_other"),
            ),
        )
        self.assertEqual(calls, ["v2.debug.location.list"])

    def test_v2_pending_requests_reads_permissions_and_forms_by_operation_id(self) -> None:
        location = V2Location("/work/alpha", "wrk_alpha")
        calls: list[tuple[str, V2Location | None]] = []

        def fake_api(_binary, operation, **kwargs):
            calls.append((operation, kwargs.get("location")))
            if operation == "v2.permission.request.list":
                return {
                    "data": [
                        {
                            "id": "per_one",
                            "sessionID": "ses_one",
                            "action": "shell",
                            "resources": ["npm test"],
                        }
                    ]
                }
            return {
                "data": [
                    {
                        "id": "frm_one",
                        "sessionID": "ses_one",
                        "title": "Choose release",
                        "fields": [{"key": "release", "type": "string"}],
                    }
                ]
            }

        with mock.patch(
            "ocdeck_permission_watcher.v2_api_json", side_effect=fake_api
        ):
            requests, complete = v2_pending_requests(
                "/usr/bin/opencode2", (location,)
            )

        self.assertTrue(complete)
        self.assertEqual(
            requests,
            [
                {
                    "id": "per_one",
                    "sessionID": "ses_one",
                    "kind": "permission",
                    "detail": "npm test",
                },
                {
                    "id": "frm_one",
                    "sessionID": "ses_one",
                    "kind": "question",
                    "detail": "Choose release",
                },
            ],
        )
        self.assertEqual(
            calls,
            [
                ("v2.permission.request.list", location),
                ("v2.form.request.list", location),
            ],
        )

    def test_failed_location_refresh_remains_non_authoritative_between_refreshes(
        self,
    ) -> None:
        class StopWatcher(Exception):
            pass

        seen = {"permission:ses_old:per_old"}
        with mock.patch(
            "ocdeck_permission_watcher.load_seen", return_value=seen
        ), mock.patch(
            "ocdeck_permission_watcher.v2_request_locations",
            return_value=((), False),
        ) as discover, mock.patch(
            "ocdeck_permission_watcher.v2_pending_requests",
            return_value=([], True),
        ), mock.patch(
            "ocdeck_permission_watcher.local_pending_requests", return_value=[]
        ), mock.patch(
            "ocdeck_permission_watcher.read_server_environment",
            side_effect=AssertionError("V2 read V1 credentials"),
        ), mock.patch(
            "ocdeck_permission_watcher.save_seen"
        ) as save, mock.patch(
            "ocdeck_permission_watcher.start_completion_watcher", return_value=None
        ), mock.patch(
            "ocdeck_permission_watcher.time.monotonic", side_effect=(0.0, 1.0)
        ), mock.patch(
            "ocdeck_permission_watcher.time.sleep",
            side_effect=(None, StopWatcher()),
        ):
            with self.assertRaises(StopWatcher):
                watcher_main(
                    ["--backend", "v2", "--opencode2-bin", "/usr/bin/opencode2"]
                )

        self.assertEqual(discover.call_count, 1)
        save.assert_not_called()

    def test_completion_watcher_uses_managed_profile_only_for_local_v2(self) -> None:
        with mock.patch("ocdeck_permission_watcher.shutil.which", return_value="/usr/bin/node"), mock.patch(
            "ocdeck_permission_watcher.subprocess.Popen"
        ) as spawn:
            self.assertIsNone(start_completion_watcher(parse_args(["--backend", "v1"])))
            self.assertIsNone(start_completion_watcher(parse_args(["--url", "http://127.0.0.1:49374"])))
            spawn.assert_not_called()
            start_completion_watcher(parse_args(["--backend", "v2"]))
        self.assertEqual(spawn.call_args.args[0][0], "/usr/bin/node")
        self.assertTrue(spawn.call_args.args[0][1].endswith("plugins/v2/session-notify-watcher.mjs"))
        self.assertEqual(
            spawn.call_args.kwargs["env"]["XDG_CONFIG_HOME"],
            str(Path.home() / ".config/ocdeck-v2-runtime"),
        )

    def test_live_plugin_request_suppresses_api_fallback_notification(self) -> None:
        request = {
            "id": "permission-1",
            "sessionID": "session-1",
            "kind": "permission",
            "detail": "npm test",
        }

        candidates, current = notification_candidates([request], [request])

        self.assertEqual(candidates, [])
        self.assertEqual(current, {"permission:session-1:permission-1"})

    def test_api_only_request_remains_a_notification_candidate(self) -> None:
        request = {
            "id": "permission-1",
            "sessionID": "session-1",
            "kind": "permission",
            "detail": "npm test",
        }

        candidates, current = notification_candidates([request], [])

        self.assertEqual(candidates, [request])
        self.assertEqual(current, {"permission:session-1:permission-1"})


if __name__ == "__main__":
    unittest.main()
