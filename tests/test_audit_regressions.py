from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest import mock

from textual.widgets import DataTable

import ocdeck_permission_watcher as watcher
from ocdeck.app import OCDeckApp
from ocdeck.models import (
    DashboardSnapshot, ServiceRecord, SessionRecord, build_projects,
    clean_int, compact_path, expected_named_agents, parse_named_agent_registry,
    relative_time, summarize_named_agents,
)
from ocdeck.source import DashboardSource, EXPECTED_V2_VERSION, V2ApiError
from tests.test_app import MultiProjectSource


class MetadataAuditTests(unittest.TestCase):
    def test_nonfinite_numbers_and_out_of_range_dates_do_not_crash_rendering(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertEqual(clean_int(value), 0)
        self.assertEqual(relative_time(10 ** 400), "unknown")

    def test_home_abbreviation_respects_directory_boundaries(self):
        with mock.patch("ocdeck.models.Path.home", return_value=Path("/home/operator")):
            self.assertEqual(compact_path("/home/operator/work"), "~/work")
            self.assertEqual(compact_path("/home/operator2/work"), "/home/operator2/work")

    def test_legacy_agent_registry_and_sessions_map_to_current_identity(self):
        registry = parse_named_agent_registry(
            [{"name": "jarvis", "model": {"providerID": "test", "modelID": "old"}}],
            {"jarvis": True},
        )
        status = next(item for item in registry if item.name == "maverik")
        self.assertTrue(status.loaded)
        self.assertTrue(status.configured)
        self.assertEqual(status.model, "test/old")
        session = SessionRecord("s", "Legacy voice", "/work", "p", 1, 2,
                                home_agent="jarvis", status="busy")
        summary = summarize_named_agents(registry, (session,))
        self.assertEqual(next(item for item in summary if item.name == "maverik").state, "active")
        self.assertTrue(next(item for item in expected_named_agents({"jarvis": True})
                             if item.name == "maverik").configured)

    def test_canonical_agent_wins_over_legacy_alias(self):
        registry = parse_named_agent_registry([
            {"name": "maverik", "model": {"providerID": "test", "modelID": "new"}},
            {"name": "jarvis", "model": {"providerID": "test", "modelID": "old"}},
        ], {"maverik": False, "jarvis": True})
        status = next(item for item in registry if item.name == "maverik")
        self.assertEqual(status.model, "test/new")
        self.assertFalse(status.configured)


class DashboardAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_visible_unregistered_directory_is_persisted_by_registration(self):
        with tempfile.TemporaryDirectory() as base:
            directory = Path(base)
            session = SessionRecord("s", "Discovered", str(directory), "p", 1, 2)
            app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
            app._register_project_worker = mock.Mock()
            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause()
                projects = build_projects((session,), {"p": str(directory)}, {})
                app._apply_snapshot(DashboardSnapshot(sessions=(session,), projects=projects))
                app._register_project(str(directory))
                app._register_project_worker.assert_called_once_with(directory, directory.name)

                app._register_project_worker.reset_mock()
                registered = build_projects((session,), {"p": str(directory)}, {"p": "Registered"})
                app._apply_snapshot(replace(app.snapshot, projects=registered))
                app._register_project(str(directory))
                app._register_project_worker.assert_not_called()

    async def test_services_refresh_keeps_selection_and_updates_state(self):
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            services = (
                ServiceRecord("a.service", "First", "test", "active"),
                ServiceRecord("b.service", "Second", "test", "active"),
            )
            app._apply_snapshot(replace(app.snapshot, services=services))
            await pilot.press("2", "down")
            table = app.query_one("#services-table", DataTable)
            app._apply_snapshot(replace(app.snapshot, services=(services[0], replace(services[1], state="failed"))))
            await pilot.pause()
            self.assertEqual(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value, "b.service")
            self.assertIn("FAILED", str(table.get_row("b.service")[0]))

    async def test_v2_poll_failures_retain_last_state_until_a_valid_empty_result(self):
        source = DashboardSource(backend="v2", named_agent_files={})
        health = {"healthy": True, "version": EXPECTED_V2_VERSION}
        source._v2_api_json = mock.AsyncMock(side_effect=[
            health, {"data": {"ses_live": {"type": "running"}}},
            health, V2ApiError("temporary failure"),
            V2ApiError("service offline"),
            health, {"data": {}},
        ])
        self.assertEqual((await source._v2_health_status())[2], {"ses_live": "busy"})
        stale = await source._v2_health_status()
        self.assertEqual(stale[0], "degraded")
        self.assertEqual(stale[2], {"ses_live": "busy"})
        self.assertIsNot(stale[2], source._v2_active_statuses)
        self.assertEqual((await source._v2_health_status())[2], {"ses_live": "busy"})
        self.assertEqual((await source._v2_health_status())[2], {})

    def test_explicit_empty_agent_file_mapping_does_not_use_workstation_files(self):
        self.assertEqual(DashboardSource(backend="v2", named_agent_files={}).named_agent_files, {})


class WatcherAuditTests(unittest.TestCase):
    request = {"id": "p", "sessionID": "s", "kind": "permission", "detail": "test"}

    def test_notify_reports_delivery_failure(self):
        for code in (0, 1):
            with self.subTest(code=code), mock.patch.object(
                watcher.subprocess, "run", return_value=subprocess.CompletedProcess([], code)
            ):
                self.assertIs(watcher.notify(self.request), code == 0)

    def test_malformed_seen_state_is_ignored(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "seen.json"
            for value in (None, 1, "wrong", {}):
                path.write_text(json.dumps({"pending": value}))
                self.assertEqual(watcher.load_seen(path), set())

    def run_watcher(self, delivery, failed_polls=()):
        class StopWatcher(Exception):
            pass

        cycle = 0

        def pause(_seconds):
            nonlocal cycle
            cycle += 1
            if cycle == 3:
                raise StopWatcher()

        def open_request(request, **_kwargs):
            if cycle in failed_polls:
                raise urllib.error.URLError("temporary outage")
            payload = [{"id": "p", "sessionID": "s", "permission": "bash"}] if request.full_url.endswith("/permission") else []
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            return response

        opener = mock.Mock()
        opener.open.side_effect = open_request
        with mock.patch.object(watcher, "load_seen", return_value=set()), \
             mock.patch.object(watcher, "save_seen") as save, \
             mock.patch.object(watcher, "read_server_environment", return_value={}), \
             mock.patch.object(watcher, "local_pending_requests", return_value=[]), \
             mock.patch.object(watcher.urllib.request, "build_opener", return_value=opener), \
             mock.patch.object(watcher.time, "sleep", side_effect=pause), \
             mock.patch.object(watcher, "notify", side_effect=delivery) as notify:
            with self.assertRaises(StopWatcher):
                watcher.main(["--backend", "v1"])
        return notify, save

    def test_failed_notification_is_retried_then_deduplicated(self):
        notify, save = self.run_watcher([False, True])
        self.assertEqual(notify.call_count, 2)
        self.assertEqual(save.call_args.args[1], {"permission:s:p"})

    def test_api_outage_does_not_forget_delivered_notifications(self):
        notify, _save = self.run_watcher([True, True], failed_polls=(1,))
        self.assertEqual(notify.call_count, 1)
