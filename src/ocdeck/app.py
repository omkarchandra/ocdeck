from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.events import Key, Resize
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Input,
    Markdown,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
)

from .agent_tabs import attached_agent_tabs, is_managed_session, purge_attached_tabs
from .alarm_view import alarm_cells, alarm_detail, alarm_session, health_text
from .backend import read_saved_backend
from .direct_terminal import close_renderers, direct_renderers, read_opencode_processes
from .harnesses import (
    HARNESS_BADGES, HARNESS_LABELS, HARNESS_STYLES, SCRATCH_PROJECT_ID, MultiHarnessSource, build_adapters,
    agent_browser_server, is_transcript_subagent, load_browser_grants, load_harness_settings, model_name,
    resolve_enabled_harnesses, runtime_label, save_browser_grants, split_session_key,
)
from .recent_open import (
    MAX_RECENT_OPEN_SESSIONS,
    RecentOpenHistory,
    default_recent_open_file,
)
from .session_origin import (
    AGENT_MARKER,
    AGENT_TITLE_STYLE,
    AGENT_VERDICT,
    SessionOrigin,
    classify_session,
    load_owner_sessions,
    record_owner_launch,
    record_owner_session,
    refresh_owner_opened,
)
from .agents_layout import COLUMNS, Widths, column_widths, legend, state_cell, term_cell, term_kind
from .archive import archive_session, load_archived_sessions, unarchive_session
from .dismiss_all import DismissAllScreen
from .process_stop import identify, stop_processes
from .launch_picker import LaunchChoice, LaunchPicker, agent_arguments, discover_agents
from .models import (
    DashboardSnapshot,
    ProjectBriefingRecord,
    ProjectRecord,
    SessionRecord,
    agent_state,
    compact_path,
    format_uptime,
    relative_time,
    sanitize_terminal_text,
    session_age_ms,
)
from .source import (
    DashboardSource,
    LiveOpenCodePane,
    normalized_project_path,
    read_live_opencode_panes,
    read_system_metrics,
    session_renderer_pids,
)
from .tmux_header import apply_header, headers_for_sessions
from .v2_interrupt import IDLE, INTERRUPTED, NOT_OPENCODE, interrupt_v2_turn
from .sentinel.acks import ack_path, append_ack
from .sentinel.health import SentinelHealth, SentinelReport, read_report


STATUS_STYLE = {
    "busy": "bold #4ade80",
    "retry": "bold #f2b84b",
    "stalled": "bold #f2b84b",
    "open": "bold #5eead4",
    "permission": "bold #ff6b7a",
    "question": "bold #d4a6ff",
    "review": "bold #ffa657",
    "idle": "dim #668094",
    "closed": "dim #7890a2",
    "job": "bold #86b7ff",
}
MAX_RECENT_AGENT_ROWS = MAX_RECENT_OPEN_SESSIONS

SUBAGENT_STYLE = "#ffa657"

ARCHIVED_MARKER = "[archived]"
ARCHIVED_TITLE_STYLE = "dim #668094"

AGENT_STATE_LABEL = {
    "busy": "RUNNING",
    "permission": "PERMISSION",
    "question": "QUESTION",
    "retry": "RETRY",
    "stalled": "STALLED",
    "review": "REVIEW",
    "open": "IDLE",
    "idle": "IDLE",
    "closed": "CLOSED",
    "job": "BACKGROUND JOB",
}

HIDDEN_PROMPT_LABEL = "[hidden]"
TERM_STYLE = {
    "attached": "bold #5eead4", "tmux": "#86b7ff", "direct": "#f2b84b",
    "server": "#86b7ff", "saved": "dim #668094",
}
HARNESS_STYLE = HARNESS_STYLES  # live registry view: plugin harnesses bring their own colour


def session_harness(session: SessionRecord) -> str:
    return getattr(session, "harness", "") or "opencode"
AGENT_DETAIL_CLIP = 32


class ConfirmationButton(Button):
    """A second click must reach the explicit confirmation state machine."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Button.__init__ assigns the instance attribute, so a class-level
        # override is shadowed and a quick second click would be swallowed.
        self.active_effect_duration = 0

PROJECT_ACCENTS = (
    "#5eead4",
    "#86b7ff",
    "#d4a6ff",
    "#f2b84b",
    "#ff9e7a",
    "#7ee081",
    "#f7a2c4",
    "#9ff2e0",
    "#c0b6ff",
    "#ffd166",
)

MOBILE_TARGET_FILE = "ocdeck-mobile-target.json"


def project_accent(project_id: str) -> str:
    if not project_id:
        return "#7890a2"
    digest = hashlib.sha256(project_id.encode("utf-8")).digest()
    return PROJECT_ACCENTS[digest[0] % len(PROJECT_ACCENTS)]


def default_mobile_target_file() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    root = Path(runtime) if runtime else Path("/tmp") / f"ocdeck-{os.getuid()}"
    return root / MOBILE_TARGET_FILE

ACTIVITY_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
BRIEFING_STALE_SECONDS = 24 * 60 * 60

ASSESSMENT_STYLE = {
    "on-track": "bold #4ade80",
    "at-risk": "bold #f2b84b",
    "blocked": "bold #ff6b7a",
    "waiting": "bold #86b7ff",
    "complete": "bold #5eead4",
    "unknown": "dim #7890a2",
}

STEP_STYLE = {
    "now": "bold #5eead4",
    "next": "bold #86b7ff",
    "blocked": "bold #ff6b7a",
    "done": "dim #7890a2",
}


class MetricCard(Static):
    def set_metric(self, label: str, value: str, detail: str, tone: str = "#5eead4") -> None:
        label = sanitize_terminal_text(label)
        value = sanitize_terminal_text(value)
        detail = sanitize_terminal_text(detail)
        self.update(
            f"[dim #7890a2]{escape(label.upper())}[/]\n"
            f"[bold {tone}]{escape(value)}[/]\n"
            f"[dim]{escape(detail)}[/]"
        )


class KeyboardDataTable(DataTable):
    BINDINGS = [
        Binding("j", "cursor_down", "Move down", show=False),
        Binding("k", "cursor_up", "Move up", show=False),
    ]


class AgentsTable(KeyboardDataTable):
    BINDINGS = [
        Binding("left", "app.toggle_subagents", "Subagents", show=False),
        Binding("enter", "app.open_session", "Open", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.clicked_column_index: int | None = None
        self.clicked_expand_control = False
        self.click_serial = 0

    async def _on_click(self, event: events.Click) -> None:
        # Keep the clicked column so the app can distinguish the expand arrow
        # from the normal row-open action.
        self.clicked_column_index = None
        self.clicked_expand_control = False
        self.click_serial += 1
        previous_column = self.cursor_coordinate.column
        x = event.get_content_offset_capture(self).x + int(self.scroll_x)
        meta = event.style.meta
        if "row" in meta and "column" in meta:
            row = meta.get("row")
            column = meta.get("column")
            if isinstance(row, int) and row >= 0 and isinstance(column, int):
                self.clicked_column_index = column
        if self.clicked_column_index is None:
            for column_index in range(len(self.columns)):
                region = self._get_column_region(column_index)
                if region.x <= x < region.x + region.width:
                    self.clicked_column_index = column_index
                    break
        if self.clicked_column_index == getattr(self.app, "agent_session_index", -1):
            region = self._get_column_region(self.clicked_column_index)
            self.clicked_expand_control = x < region.x + 5
        if (
            getattr(self.app, "inline_tmux", False)
            or self.clicked_column_index != previous_column
        ):
            self._post_selected_message()

    def action_select_cursor(self) -> None:
        self.clicked_column_index = None
        self.clicked_expand_control = False
        self.click_serial = 0
        super().action_select_cursor()


class AlarmsTable(KeyboardDataTable):
    BINDINGS = [Binding("enter", "app.inspect_alarm", "Locate session", show=False)]


class NavigationTable(KeyboardDataTable):
    """A row table with spatial keyboard navigation between adjacent panes."""

    BINDINGS = [
        Binding("left,h", "app.focus_adjacent_table(-1)", "Previous pane", show=False),
        Binding("right,l", "app.focus_adjacent_table(1)", "Next pane", show=False),
    ]


class ProjectsTable(NavigationTable):
    """Keep mouse selection in Projects; Enter moves to its sessions."""

    def action_select_cursor(self) -> None:
        super().action_select_cursor()
        self.app.action_focus_adjacent_table(1)


class SessionsTable(NavigationTable):
    """Session list that remembers which column received the last mouse click."""

    BINDINGS = NavigationTable.BINDINGS + [
        Binding("enter", "app.open_session", "Open", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.clicked_column_index: int | None = None

    def _on_click(self, event: events.Click) -> None:
        self.clicked_column_index = None
        previous_coordinate = self.cursor_coordinate
        meta = event.style.meta
        if isinstance(meta, dict):
            row = meta.get("row")
            column = meta.get("column")
            if isinstance(row, int) and row >= 0 and isinstance(column, int):
                self.clicked_column_index = column
        if (
            getattr(self.app, "inline_tmux", False)
            and self.clicked_column_index is not None
        ):
            self.call_after_refresh(
                self._select_mobile_click,
                previous_coordinate,
                Coordinate(row, column),
            )

    def _select_mobile_click(
        self, previous_coordinate: Coordinate, clicked_coordinate: Coordinate
    ) -> None:
        if (
            self.cursor_coordinate == clicked_coordinate
            and self.cursor_coordinate != previous_coordinate
        ):
            self._post_selected_message()

    def action_select_cursor(self) -> None:
        self.clicked_column_index = None
        super().action_select_cursor()


class RenameInput(Input):
    """Inline editor for a session's name; Escape cancels without saving."""

    BINDINGS = [
        Binding("escape", "app.cancel_session_rename", "Cancel rename", show=False),
    ]


class KeyReference(Markdown, can_focus=True):
    BINDINGS = [
        Binding("up,k", "scroll_up", "Scroll up", show=False),
        Binding("down,j", "scroll_down", "Scroll down", show=False),
        Binding("pageup", "page_up", "Page up", show=False),
        Binding("pagedown", "page_down", "Page down", show=False),
        Binding("home", "scroll_home", "Top", show=False),
        Binding("end", "scroll_end", "Bottom", show=False),
    ]


class NextStepsView(VerticalScroll, can_focus=True):
    BINDINGS = [
        Binding("up,k", "app.select_next_project(-1)", "Previous project", show=False),
        Binding("down,j", "app.select_next_project(1)", "Next project", show=False),
        Binding("pageup", "page_up", "Scroll up", show=False),
        Binding("pagedown", "page_down", "Scroll down", show=False),
        Binding("home", "scroll_home", "Top", show=False),
        Binding("end", "scroll_end", "Bottom", show=False),
        Binding("enter,o,a,x,n,t,N,B", "noop", "Read only", show=False),
    ]

    def action_noop(self) -> None:
        pass


class OCDeckApp(App[None]):
    TITLE = "OC Deck"
    SUB_TITLE = "OpenCode operations console"

    CSS = """
    Screen {
        background: #071018;
        color: #cbd9e3;
        layout: vertical;
    }

    #brand {
        height: 3;
        padding: 0 2;
        background: #0d1a25;
        border-bottom: tall #284456;
        content-align: left middle;
    }

    #metrics {
        height: 7;
        padding: 1 1 0 1;
    }

    .metric {
        width: 1fr;
        height: 6;
        margin: 0 1;
        padding: 0 1;
        background: #0b1620;
        border: round #274356;
    }

    #tabs {
        height: 1fr;
        margin: 0 2 1 2;
    }

    #attention {
        height: auto;
        min-height: 3;
        max-height: 9;
        margin: 0 2 1 2;
        padding: 0 1;
        background: #21131b;
        border: round #71334d;
    }

    #sentinel-status {
        height: 1;
        margin: 0 2;
    }

    #alarms-health, #alarm-detail {
        height: auto;
        max-height: 9;
        padding: 0 1;
    }

    TabbedContent {
        background: #071018;
    }

    ContentSwitcher {
        background: #071018;
    }

    TabPane {
        padding: 1 0 0 0;
        background: #071018;
    }

    Tabs {
        background: #0b1620;
        color: #7890a2;
        border-bottom: tall #1e3444;
    }

    Tab.-active {
        color: #5eead4;
        text-style: bold;
    }

    #overview-grid {
        height: 1fr;
    }

    .pane {
        height: 1fr;
        background: #0a141d;
        border: round #1f394a;
    }

    .pane:focus-within {
        border: round #5eead4;
    }

    #projects-pane {
        width: 29%;
        margin-right: 1;
    }

    #sessions-pane {
        width: 46%;
        margin-right: 1;
    }

    #detail-pane {
        width: 25%;
    }

    .pane-title {
        height: 2;
        padding: 0 1;
        color: #8ba4b5;
        background: #0e1d28;
        text-style: bold;
        content-align: left middle;
    }

    .pane:focus-within > .pane-title {
        color: #5eead4;
        background: #102a36;
    }

    #project-search, #session-search {
        height: 3;
        margin: 0 1;
        border: tall transparent;
        background: #101f2b;
    }

    #project-search:focus, #session-search:focus {
        border: tall #5eead4;
    }

    #session-search.filtered {
        border: tall #f2b84b;
    }

    #project-register {
        height: 3;
        margin: 0 1;
        border: tall transparent;
        background: #101f2b;
    }

    #project-register:focus {
        border: tall #86b7ff;
    }

    #add-project, #new-session, #new-browser-session, #enable-browser {
        width: 1fr;
        min-width: 0;
        height: 3;
        margin: 0 1;
        border: none;
        background: #123043;
        color: #5eead4;
        text-style: bold;
    }

    #add-project:hover, #add-project:focus, #new-session:hover, #new-session:focus,
    #new-browser-session:hover, #new-browser-session:focus, #enable-browser:hover, #enable-browser:focus {
        background: #1b4558;
        color: #e7f5fc;
    }

    #new-session:disabled, #new-browser-session:disabled, #enable-browser:disabled {
        background: #101f2b;
        color: #526d7d;
    }

    #session-rename {
        height: 3;
        margin: 0 1;
        border: tall transparent;
        background: #101f2b;
    }

    #session-rename:focus {
        border: tall #d4a6ff;
    }

    #session-filters {
        height: auto;
        padding: 0 1;
        color: #f2b84b;
    }

    #session-controls {
        height: 3;
        padding: 0 1;
    }

    #include-agent-sessions {
        width: 1fr;
        border: none;
        background: #101f2b;
    }

    #clear-session-filters {
        width: 15;
        min-width: 0;
        border: none;
        background: #123043;
        color: #5eead4;
    }

    DataTable {
        height: 1fr;
        background: #0a141d;
        color: #cbd9e3;
        scrollbar-color: #315164;
        scrollbar-background: #0a141d;
    }

    DataTable > .datatable--header {
        background: #102330;
        color: #8ba4b5;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #102a36;
        color: #cbd9e3;
    }

    DataTable:focus > .datatable--cursor {
        background: #1b4b5e;
        color: #f4fbff;
        text-style: bold;
    }

    #session-detail {
        padding: 1 2;
    }

    #services-table {
        height: 1fr;
        border: round #1f394a;
    }

    #agents-history-controls {
        height: 3;
    }

    #choose-launch {
        width: 1fr;
        border: none;
        background: #182b45;
        color: #86b7ff;
    }

    #agent-focus {
        height: auto;
        max-height: 6;
        padding: 0 1;
        border-top: solid #22384a;
    }

    #relaunch-agents {
        width: 1fr;
        border: none;
        background: #123043;
        color: #5eead4;
    }

    #relaunch-agents:disabled {
        color: #7890a2;
        background: #101f2b;
    }

    #purge-agent-tabs {
        width: 1fr;
        border: none;
        background: #2b2416;
        color: #f2b84b;
    }

    #purge-agent-tabs:disabled {
        color: #7890a2;
        background: #101f2b;
    }

    .mobile #agents-history-controls {
        display: none;
    }

    #services-table:focus {
        border: round #5eead4;
    }

    #key-reference {
        height: 1fr;
        padding: 1 3;
        border: round #1f394a;
        background: #0a141d;
        overflow-y: auto;
    }

    #named-agent-status {
        height: 6;
        padding: 0 1;
        border: round #1f394a;
        background: #0a141d;
        text-wrap: nowrap;
        overflow-x: hidden;
    }

    .narrow #named-agent-status, .mobile #named-agent-status {
        height: 12;
    }

    .tiny #named-agent-status {
        height: 8;
    }

    #key-reference:focus {
        border: round #5eead4;
    }

    #next-view {
        height: 1fr;
        padding: 1 3;
        border: round #1f394a;
        background: #0a141d;
        overflow-y: auto;
    }

    #next-view:focus {
        border: round #5eead4;
    }

    #next-content {
        width: 100%;
        height: auto;
    }

    Footer {
        background: #0d1a25;
        color: #7890a2;
    }

    Footer > .footer--key {
        background: #173849;
        color: #5eead4;
    }

    .compact #projects-pane {
        width: 36%;
    }

    .mobile #metrics, .mobile #attention, .mobile #projects-pane,
    .mobile #detail-pane, .mobile #session-controls, .mobile Tabs, .mobile Footer {
        display: none;
    }

    Screen.mobile #sessions-pane {
        width: 100%;
        margin-right: 0;
        border: none;
    }

    .mobile #sessions-title {
        height: 2;
        color: #5eead4;
    }

    #mobile-session-scope, #mobile-session-navigation, #mobile-session-empty {
        display: none;
    }

    .mobile #mobile-session-scope, .mobile #mobile-session-navigation {
        display: block;
        height: 3;
    }

    .mobile #mobile-session-scope Button, .mobile #mobile-session-navigation Button {
        width: 1fr;
        min-width: 0;
        height: 3;
        border: tall #284456;
        background: #123043;
        color: #e7f5fc;
    }

    .mobile #mobile-session-scope Button.selected, .mobile #mobile-open {
        background: #1b4b5e;
        color: #5eead4;
        text-style: bold;
    }

    .mobile #mobile-session-empty {
        height: auto;
        padding: 1;
        color: #8ba4b5;
    }

    .mobile #brand {
        height: 2;
        padding: 0 1;
    }

    .mobile #tabs {
        margin: 0;
    }

    .mobile #enable-browser {
        display: none;
    }

    .mobile #sessions-table {
        scrollbar-size-vertical: 2;
    }

    .compact #sessions-pane {
        width: 64%;
        margin-right: 0;
    }

    .compact #detail-pane {
        display: none;
    }

    .narrow #projects-pane {
        display: none;
    }

    .narrow #sessions-pane {
        width: 100%;
        margin-right: 0;
    }

    .narrow #next-view {
        padding: 1;
    }

    .short #metrics {
        display: none;
    }

    .short #tabs {
        margin-top: 0;
    }

    .tiny #brand {
        height: 1;
        padding: 0 1;
        border-bottom: none;
    }

    .tiny #project-search, .tiny #session-search, .tiny #session-rename,
    .tiny .pane-title {
        display: none;
    }

    .tiny #next-view {
        padding: 0 1;
        border: none;
    }

    .tiny #tabs {
        margin: 0;
    }

    .mobile #session-search, .mobile #sessions-title {
        display: block;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_data", "Refresh"),
        Binding("/", "search", "Search"),
        Binding("tab", "focus_next", "Next pane", key_display="Tab"),
        Binding("shift+tab", "focus_previous", "Previous pane", show=False),
        Binding("p", "privacy", "Privacy"),
        Binding("o", "open_session", "Open"),
        Binding("enter", "open_session", "Open", show=False),
        Binding("a", "open_auto", "Auto open"),
        Binding(
            "L",
            "relaunch_previous_sessions",
            "Relaunch agents",
            key_display="Shift+L",
            show=False,
        ),
        Binding("y", "approve_permission", "Allow once"),
        Binding("g", "focus_permission", "Permission"),
        Binding("x", "stop_job", "Stop job"),
        Binding(
            "D",
            "dismiss_alarm",
            "Dismiss alarm",
            key_display="Shift+D",
            show=False,
        ),
        Binding(
            "P",
            "pin_viewer_slot",
            "Pin window spot",
            key_display="Shift+P",
            show=False,
        ),
        Binding(
            "A",
            "shift_a",
            "Dismiss all alarms",
            key_display="Shift+A",
            show=False,
        ),
        Binding(
            "U",
            "toggle_show_archived",
            "Show archived",
            key_display="Shift+U",
            show=False,
        ),
        Binding("z", "purge_agent_tabs", "Purge agent tabs", show=False),
        Binding("n", "new_session_choose", "New session"),
        Binding("H", "cycle_harness", "Harness", key_display="Shift+H"),
        Binding("S", "choose_launch", "Harness / agent", key_display="Shift+S", show=False),
        Binding("C", "handoff_session", "Hand off", key_display="Shift+C", show=False),
        Binding("N", "new_browser_session", "New browser session", key_display="Shift+N", show=False),
        Binding("B", "enable_browser", "Enable browser", key_display="Shift+B", show=False),
        Binding("d", "add_project", "Add project"),
        Binding("t", "new_terminal", "Terminal"),
        Binding("f", "toggle_filter", "Scope"),
        Binding("b", "toggle_agent_sessions", "Include agent sessions", show=False),
        Binding("m", "minimize_window", "Minimize"),
        Binding("1", "show_tab('overview')", "Overview", show=False),
        Binding("2", "show_tab('services')", "Services", show=False),
        Binding("3", "show_tab('keys-view')", "Keys", show=False),
        Binding("4", "show_tab('agents')", "Agents", show=False),
        Binding("5", "show_tab('next')", "Next", show=False),
        Binding("6", "show_tab('alarms')", "Alarms", show=False),
        Binding("ctrl+left", "cycle_view(-1)", "Previous view", show=False),
        Binding("ctrl+right", "cycle_view(1)", "Next view", show=False),
        Binding("escape", "clear_search", "Clear search", show=False),
    ]

    def __init__(
        self,
        source: DashboardSource,
        *,
        refresh_seconds: float = 15,
        auto_refresh: bool = True,
        activity_seconds: float = 2,
        inline_tmux: bool = False,
        mobile_target_file: Path | None = None,
        home_agentctl_bin: str | Path | None = None,
        recent_open_file: Path | None = None,
        owner_opened_file: Path | None = None,
        archived_sessions_file: Path | None = None,
    ) -> None:
        super().__init__()
        self.source = source
        self.recent_open_file = recent_open_file
        self._recent_history = RecentOpenHistory(recent_open_file)
        # Sessions the owner opened from OC Deck render normally even when an
        # agent signal also matches; None means the default state file.
        self.owner_opened_file = owner_opened_file
        # Sessions the owner archived off the board (OC Deck-only, reversible);
        # None means the default state file.
        self.archived_sessions_file = archived_sessions_file
        self.show_archived_sessions = False
        self.archive_confirm = ""
        self._archived_ids: set[str] = set()
        self._archive_marked_ids: set[str] = set()
        self._history_write_warning = False
        self.refresh_seconds = max(3, refresh_seconds)
        self.activity_seconds = max(1, activity_seconds)
        self.inline_tmux = inline_tmux
        self.mobile_target_file = mobile_target_file or default_mobile_target_file()
        self.home_agentctl_bin = str(
            home_agentctl_bin
            or os.environ.get("HOME_AGENTCTL")
            or Path.home() / ".local/bin/home-agentctl"
        )
        self.periodic_refresh_enabled = auto_refresh
        self.snapshot = DashboardSnapshot()
        self.session_by_id: dict[str, SessionRecord] = {}
        self.project_by_id: dict[str, ProjectRecord] = {}
        self.briefing_by_project_id: dict[str, ProjectBriefingRecord] = {}
        self.selected_session_id = ""
        self.selected_project_id = ""
        self.project_filter = False
        self.project_search_term = ""
        self.search_term = ""
        self.show_agent_sessions = False
        self.private = False
        self.refresh_in_progress = False
        self.refresh_pending = False
        self._refresh_generation = 0
        self._last_refresh_error = ""
        self._state_since_ms: dict[str, tuple[str, int]] = {}
        self.requested_tab_id = "overview" if inline_tmux else "agents"
        self.mobile_all_sessions = False
        self._mobile_session_order: list[str] = []
        self.session_title_column = None
        self.session_title_index = -1
        self.renaming_session_id = ""
        self.session_project_column = None
        self.initial_focus_set = False
        self.activity_frame = 0
        self.activity_in_progress = False
        self.stop_confirm = ""
        self.dismiss_confirm = ""
        self.auto_confirm = ""
        self.browser_auto_confirm = ""
        self.expanded_agent_ids: set[str] = set()
        self.agent_children_by_id: dict[str, tuple[str, ...]] = {}
        self.agent_parent_by_id: dict[str, str] = {}
        self.agent_display_state_by_id: dict[str, str] = {}
        self.agent_attention_source_by_id: dict[str, str] = {}
        # Agent-opened rows currently rendered CLOSED (nested or trailing).
        self.agent_closed_row_ids: set[str] = set()
        self.agent_origin_by_id: dict[str, SessionOrigin] = {}
        self.agent_session_index = -1
        self.agent_runtime_full = False
        self.agent_widths: Widths = column_widths(120)
        self._harness_launch_pending: dict[str, float] = {}
        self._last_mobile_agent_click_serial = 0
        self._permission_replies_in_flight: set[tuple[str, str]] = set()
        self._confirmed_permission_replies: set[tuple[str, str]] = set()
        self._browser_operations: set[tuple[str, str]] = set()
        self._browser_terminal_ids: set[str] = set()
        self.relaunch_confirm: tuple[str, ...] = ()
        self._relaunch_confirm_until = 0.0
        self._relaunch_pending: dict[str, float] = {}
        self._purge_confirm_until = 0.0
        self._headers_in_progress = False
        self._headers_pending = False
        self.sentinel_report = SentinelReport(SentinelHealth("OFFLINE", ("No completed scan artifact.",)))
        self.alarm_by_id: dict[str, dict] = {}
        self.selected_alarm_id = ""

    def compose(self) -> ComposeResult:
        yield Static(id="brand")
        with Horizontal(id="metrics"):
            yield MetricCard(classes="metric", id="metric-projects")
            yield MetricCard(classes="metric", id="metric-sessions")
            yield MetricCard(classes="metric", id="metric-memory")
            yield MetricCard(classes="metric", id="metric-connection")
        yield Static(id="attention")
        yield Static(id="sentinel-status")

        with TabbedContent(initial=self.requested_tab_id, id="tabs"):
            with TabPane("01 / OPERATIONS", id="overview"):
                with Horizontal(id="overview-grid"):
                    with Vertical(classes="pane", id="projects-pane"):
                        yield Static("PROJECTS", classes="pane-title", id="projects-title")
                        yield Input(
                            placeholder="Filter projects · Enter selects · ↓ results",
                            id="project-search",
                        )
                        yield Input(
                            placeholder="Paste absolute folder path · Enter registers",
                            id="project-register",
                        )
                        yield ProjectsTable(id="projects-table")
                        yield Button(
                            "+ BROWSE & ADD PROJECT DIRECTORY", id="add-project"
                        )
                        yield Button(
                            "+ NEW SESSION  (N)", id="new-session", disabled=True
                        )
                        yield Button(
                            "+ BROWSER SESSION  (Shift+N)", id="new-browser-session", disabled=True,
                            tooltip="Open a blank browser-enabled session; choose the model with /models",
                        )
                    with Vertical(classes="pane", id="sessions-pane"):
                        yield Static("RECENT SESSIONS", classes="pane-title", id="sessions-title")
                        with Horizontal(id="mobile-session-scope"):
                            yield Button("Live sessions", id="mobile-live", classes="selected")
                            yield Button("All sessions", id="mobile-all")
                        yield Input(
                            placeholder=(
                                "Find a session · tap to open"
                                if self.inline_tmux
                                else "Filter · Enter opens · ↓ results"
                            ),
                            id="session-search",
                        )
                        yield RenameInput(
                            placeholder="New name · Enter saves · Esc cancels",
                            id="session-rename",
                        )
                        yield Static(id="session-filters", markup=False)
                        yield Static(id="mobile-session-empty", markup=False)
                        yield SessionsTable(id="sessions-table")
                        with Horizontal(id="mobile-session-navigation"):
                            yield Button("↑ Previous", id="mobile-previous")
                            yield Button("Next ↓", id="mobile-next")
                            yield Button("Open", id="mobile-open")
                        with Horizontal(id="session-controls"):
                            yield Checkbox(
                                "Include agent sessions",
                                id="include-agent-sessions",
                                tooltip="B: include subagents and automated workers",
                            )
                            yield Button("Clear filters", id="clear-session-filters")
                        yield Button(
                            "ENABLE BROWSER  (Shift+B)", id="enable-browser",
                            tooltip="Grant the selected idle primary signed-in browser access; keep its model",
                            disabled=not hasattr(self.source, "enable_session_browser"),
                        )
                    with Vertical(classes="pane", id="detail-pane"):
                        yield Static("SESSION SIGNAL", classes="pane-title")
                        yield Static(id="session-detail")
            with TabPane("02 / SERVICES", id="services"):
                yield KeyboardDataTable(id="services-table")
            with TabPane("03 / KEYS", id="keys-view"):
                yield KeyReference(KEY_REFERENCE, id="key-reference")
            with TabPane("04 / AGENTS", id="agents"):
                yield Static(id="named-agent-status")
                yield AgentsTable(id="agents-table")
                yield Static(id="agent-focus")
                with Horizontal(id="agents-history-controls"):
                    yield Button("HARNESS / AGENT (Shift+S)", id="choose-launch")
                    yield ConfirmationButton("REOPEN PREVIOUS (Shift+L)", id="relaunch-agents", disabled=True)
                    yield ConfirmationButton(
                        "PURGE AGENT TABS → BACKGROUND (Z Z)",
                        id="purge-agent-tabs",
                        tooltip=(
                            "Close every attached OpenCode terminal window; each tmux "
                            "session keeps running in the background"
                        ),
                    )
            with TabPane("05 / NEXT", id="next"):
                with NextStepsView(id="next-view"):
                    yield Static(id="next-content")
            with TabPane("06 / ALARMS", id="alarms"):
                yield Static(id="alarms-health")
                yield AlarmsTable(id="alarms-table")
                yield Static(id="alarm-detail")
        yield Footer()

    def on_mount(self) -> None:
        self.screen.set_class(self.inline_tmux, "mobile")
        self._configure_tables()
        self.watch(
            self.query_one("#tabs", TabbedContent),
            "active",
            self._on_active_tab_changed,
            init=False,
        )
        self._render_brand("SCANNING")
        self.query_one("#attention", Static).display = False
        self.action_refresh_data()
        self.set_interval(0.12, self._advance_activity_animation)
        if self.periodic_refresh_enabled:
            self.set_interval(self.refresh_seconds, self.action_refresh_data)
            self.set_interval(self.activity_seconds, self._request_activity_refresh)
            # Independent of source collection: a stalled backend must not hide
            # a stopped scanner. This reads only the bounded alarm artifact.
            self.set_interval(5, self._sentinel_refresh_worker)

    def _on_active_tab_changed(self, active: str) -> None:
        if active == "alarms":
            self.requested_tab_id = active
            self._render_alarms()
            self.query_one("#alarms-table", DataTable).focus()
            return
        if active != "next":
            return
        self.requested_tab_id = "next"
        self._render_next()
        self._focus_active_next_view()

    def _focus_active_next_view(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        if tabs.active == "next":
            self.query_one("#next-view", NextStepsView).focus()

    def _configure_tables(self) -> None:
        projects = self.query_one("#projects-table", DataTable)
        projects.cursor_type = "row"
        projects.add_column("PROJECT", key="project", width=20)
        projects.add_column("SESS", key="sessions", width=5)
        projects.add_column("RECENT", key="recent", width=6)

        sessions = self.query_one("#sessions-table", SessionsTable)
        sessions.cursor_type = "row"
        sessions.clicked_column_index = None
        if self.inline_tmux:
            sessions.show_header = False
            sessions.zebra_stripes = True
        else:
            sessions.add_column("", key="state", width=2)
            sessions.add_column("INST", key="instances", width=4)
        self.session_title_column = sessions.add_column(
            "SESSION",
            key="title",
            width=max(12, self.size.width - 6) if self.inline_tmux else 25,
        )
        self.session_title_index = list(sessions.columns).index(
            self.session_title_column
        )
        if not self.inline_tmux:
            self.session_project_column = sessions.add_column(
                "PROJECT", key="project", width=14
            )
            sessions.add_column("AGE", key="age", width=6)

        self.query_one("#session-rename", RenameInput).display = False

        services = self.query_one("#services-table", DataTable)
        services.cursor_type = "row"
        services.add_columns("STATE", "SERVICE", "ROLE", "UNIT")

        agents = self.query_one("#agents-table", DataTable)
        agents.cursor_type = "row"
        agents.clicked_column_index = None
        self.agent_widths = column_widths(self.size.width)
        self.agent_runtime_full = self.agent_widths.full_runtime
        for name, width in zip(COLUMNS, self.agent_widths.as_tuple()):
            if width:
                agents.add_column(name, key=name, width=width)
        self.agent_session_index = 2

        alarms = self.query_one("#alarms-table", DataTable)
        alarms.cursor_type = "row"
        for label, width in (("SEVERITY", 8), ("RULE", 5), ("HARNESS", 11),
                             ("SESSION", 24), ("PROJECT", 16), ("SUMMARY", 64)):
            alarms.add_column(label, key=label, width=width)

    @work(group="refresh", exit_on_error=False)
    async def _refresh_worker(self) -> None:
        self._render_brand("SCANNING")
        try:
            snapshot = await self.source.collect()
            self._apply_snapshot(snapshot)
            self._refresh_tmux_headers()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._report_refresh_error(error)
        finally:
            self.refresh_in_progress = False
            if self.refresh_pending:
                self.refresh_pending = False
                self._request_refresh()

    def action_refresh_data(self) -> None:
        self._sentinel_refresh_worker()
        self._request_refresh()

    @work(group="sentinel-health", exclusive=True, exit_on_error=False)
    async def _sentinel_refresh_worker(self) -> None:
        self.sentinel_report = await asyncio.to_thread(read_report)
        self._render_alarms()

    def _render_alarms(self) -> None:
        table = self.query_one("#alarms-table", DataTable)
        selected_id = self._selected_row_id(table, self.selected_alarm_id)
        records = sorted(self.sentinel_report.records, key=lambda record: record["firedAt"], reverse=True)
        self.alarm_by_id = {record["id"]: record for record in records}
        if selected_id not in self.alarm_by_id:
            selected_id = records[0]["id"] if records else ""
        rows = [(record["id"], alarm_cells(record, self.session_by_id, self.project_by_id,
                                          private=self.private,
                                          dismissed=(record["id"], record["hash"]) in self.sentinel_report.acked))
                for record in records]
        self._update_table_rows(table, rows, selected_id)
        self.selected_alarm_id = selected_id
        self.query_one("#sentinel-status", Static).update(health_text(self.sentinel_report))
        self.query_one("#alarms-health", Static).update(health_text(self.sentinel_report, details=True))
        self._render_alarm_detail()

    def _render_alarm_detail(self) -> None:
        record = self.alarm_by_id.get(self.selected_alarm_id)
        acked = bool(record) and (record["id"], record["hash"]) in self.sentinel_report.acked
        self.query_one("#alarm-detail", Static).update(
            alarm_detail(record, private=self.private, acked=acked)
        )

    def action_dismiss_alarm(self) -> None:
        """Record operator receipt of the selected alarm (C106/C108).

        Receipt only: the dismissal never edits the bounded alarm artifact,
        it appends to the separate ack log and the row stays listed (dimmed).
        The read-only guard is deliberately skipped: dismissal is the one
        write action the ALARMS view owns.
        """
        try:
            active = self.query_one("#tabs", TabbedContent).active
        except NoMatches:
            return
        table = self.query_one("#alarms-table", DataTable)
        record = self.alarm_by_id.get(self._selected_row_id(table, self.selected_alarm_id))
        if active != "alarms" or not record:
            self.notify("Dismiss works in ALARMS (6)", severity="warning", timeout=4)
            return
        alarm_id = record["id"]
        if (alarm_id, record["hash"]) in self.sentinel_report.acked:
            self._clear_dismiss_confirm()
            self.notify("Alarm already dismissed")
            return
        if self.dismiss_confirm != alarm_id:
            self.dismiss_confirm = alarm_id
            self.notify(
                "Press Shift+D again to dismiss this "
                f"{sanitize_terminal_text(record['rule'])} alarm",
                timeout=6,
            )
            self.set_timer(6, self._clear_dismiss_confirm)
            return
        self._clear_dismiss_confirm()
        self._dismiss_alarm_worker(alarm_id, record["hash"])

    def _clear_dismiss_confirm(self) -> None:
        self.dismiss_confirm = ""

    @work(group="job-control", exit_on_error=False)
    async def _dismiss_alarm_worker(self, alarm_id: str, alarm_hash: str) -> None:
        # Receipt only: append to the ack log; never touch the artifact.
        try:
            await asyncio.to_thread(append_ack, ack_path(), alarm_id, alarm_hash)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.notify(
                "Could not record the dismissal; the alarm stays active",
                severity="error",
            )
            return
        self.notify("Dismissal recorded; the alarm stays listed dimmed", timeout=4)
        self._sentinel_refresh_worker()

    def _undismissed_alarms(self) -> tuple[tuple[str, str], ...]:
        """(id, hash) of every listed alarm that is not dismissed yet."""
        return tuple(
            (record["id"], record["hash"]) for record in self.sentinel_report.records
            if (record["id"], record["hash"]) not in self.sentinel_report.acked
        )

    def action_dismiss_all_alarms(self) -> None:
        """Dismiss every listed alarm after a typed count (receipt only).

        Only the exact set listed when the owner asked is dismissed: if an
        alarm arrives or changes before they confirm, nothing is recorded.
        """
        try:
            active = self.query_one("#tabs", TabbedContent).active
        except NoMatches:
            return
        if active != "alarms":
            self.notify("Dismiss all works in ALARMS (6)", severity="warning", timeout=4)
            return
        pending = self._undismissed_alarms()
        if not pending:
            self.notify("No alarms to dismiss")
            return

        def confirmed(ok: bool | None) -> None:
            if not ok:
                self.notify("Nothing dismissed", timeout=4)
                return
            if set(self._undismissed_alarms()) != set(pending):
                self.notify("The alarm list changed; review it and try again. Nothing dismissed.",
                            severity="warning", timeout=6)
                return
            self._dismiss_all_worker(pending)

        self.push_screen(DismissAllScreen(len(pending)), confirmed)

    @work(group="job-control", exit_on_error=False)
    async def _dismiss_all_worker(self, pending: tuple[tuple[str, str], ...]) -> None:
        # Receipt only: one ack-log entry per alarm; never touch the artifact.
        recorded = 0
        try:
            for alarm_id, alarm_hash in pending:
                await asyncio.to_thread(append_ack, ack_path(), alarm_id, alarm_hash)
                recorded += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            self.notify(
                f"Could not record every dismissal ({recorded} of {len(pending)} recorded)",
                severity="error",
            )
        else:
            self.notify(f"Dismissed {recorded} alarms; they stay listed dimmed", timeout=4)
        self._sentinel_refresh_worker()

    def action_shift_a(self) -> None:
        """Route Shift+A by the active tab: dismiss-all in ALARMS, archive elsewhere."""
        try:
            active = self.query_one("#tabs", TabbedContent).active
        except NoMatches:
            return
        if active == "alarms":
            self.action_dismiss_all_alarms()
            return
        self.action_archive_session()

    def action_archive_session(self) -> None:
        """Hide the selected session from OC Deck (Shift+A twice, reversible).

        OC Deck-only: nothing in Claude/Codex/OpenCode changes and nothing is
        stopped. A live session stays visible until it stops; Shift+U lists
        archived rows dimmed, and Shift+A twice on one unarchives it.
        """
        if self._guard_next_read_only():
            return
        session = self._current_session()
        if not session:
            self.notify("Select a session first", severity="warning")
            return
        title = sanitize_terminal_text(session.title)
        unarchiving = session.id in self._archive_marked_ids
        if self.archive_confirm != session.id:
            self.archive_confirm = session.id
            if unarchiving:
                self.notify(f"Press Shift+A again to unarchive {title}", timeout=6)
            else:
                notice = f"Press Shift+A again to archive {title}"
                if self._session_still_running(session):
                    notice += " (still running; it stays visible until it stops)"
                self.notify(notice, timeout=6)
            self.set_timer(6, self._clear_archive_confirm)
            return
        self._clear_archive_confirm()
        try:
            if unarchiving:
                unarchive_session(session.id, self.archived_sessions_file)
            else:
                archive_session(session.id, self.archived_sessions_file)
        except OSError:  # best effort, like the owner-opened memory
            self.notify("Could not save the archive state; nothing changed", severity="error")
            return
        if unarchiving:
            self.notify(f"Unarchived {title}", timeout=4)
        elif self._session_still_running(session):
            self.notify(
                f"Archived {title}; it is still running and stays visible until it stops "
                "(Shift+U shows archived sessions)",
                timeout=8,
            )
        else:
            self.notify(f"Archived {title} (Shift+U shows archived sessions)", timeout=6)
        self._request_refresh(force=True)

    def _clear_archive_confirm(self) -> None:
        self.archive_confirm = ""

    def action_toggle_show_archived(self) -> None:
        """Show or hide the sessions archived from OC Deck (Shift+U)."""
        self.show_archived_sessions = not self.show_archived_sessions
        self._clear_archive_confirm()
        self.notify(
            "Archived sessions shown dimmed; Shift+A twice unarchives one"
            if self.show_archived_sessions
            else "Archived sessions hidden",
            timeout=4,
        )
        self._request_refresh(force=True)

    def _session_still_running(self, session: SessionRecord) -> bool:
        """Whether an archived row must stay visible: archiving never stops anything."""
        return (
            session.instance_count > 0
            or agent_state(session) in {"busy", "permission", "question"}
        )

    def action_inspect_alarm(self) -> None:
        table = self.query_one("#alarms-table", DataTable)
        record = self.alarm_by_id.get(self._selected_row_id(table, ""))
        session = alarm_session(record, self.session_by_id) if record else None
        if session is None:
            self.notify("No exact known session for this alarm", severity="warning")
            return
        # Navigation only; do not resume a job or act on an artifact's commands.
        self.action_clear_filters()
        self.show_agent_sessions = True
        self.query_one("#include-agent-sessions", Checkbox).value = True
        self.selected_project_id = session.project_id
        self.selected_session_id = session.id
        self._render_sessions()
        self._restore_table_cursor(self.query_one("#sessions-table", DataTable), session.id)
        self._render_detail()
        self.action_show_tab("overview")

    def _request_refresh(self, *, force: bool = False) -> None:
        if self.refresh_in_progress:
            if force:
                self.refresh_pending = True
            return
        self._refresh_generation += 1
        self.refresh_in_progress = True
        self._refresh_worker()

    def _request_activity_refresh(self) -> None:
        """Lightweight pulse so RUNNING/REVIEW/stopped states stay current.

        Only metadata, process liveness, and status endpoints are queried;
        the heavier full collection keeps its slower cadence.
        """
        if self.refresh_in_progress or self.activity_in_progress:
            return
        if not hasattr(self.source, "collect_activity"):
            return
        self.activity_in_progress = True
        self._activity_worker(self._refresh_generation)

    @work(group="activity", exit_on_error=False)
    async def _activity_worker(self, generation: int) -> None:
        try:
            snapshot = await self.source.collect_activity()
            if snapshot is not None and generation == self._refresh_generation:
                self._apply_snapshot(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if generation == self._refresh_generation:
                self._report_refresh_error(error)
        finally:
            self.activity_in_progress = False

    def _report_refresh_error(self, error: Exception) -> None:
        self._render_brand("DEGRADED")
        message = f"Refresh failed: {type(error).__name__}"
        self.query_one("#metric-connection", MetricCard).set_metric(
            "Signal", "DEGRADED", message, "#f2b84b"
        )
        if message != self._last_refresh_error:
            self.notify(message, severity="error")
        self._last_refresh_error = message

    def _advance_activity_animation(self) -> None:
        active = tuple(
            session
            for session in self._filtered_sessions()
            if session_display_status(session)
            in {"busy", "retry", "review"}
        )
        next_active = self._next_animation_active()
        if not active and not next_active:
            return
        self.activity_frame = (self.activity_frame + 1) % len(ACTIVITY_FRAMES)
        if active and not self.inline_tmux:
            try:
                table = self.query_one("#sessions-table", DataTable)
            except NoMatches:
                table = None
            if table is not None:
                visible = {str(row_key.value) for row_key in table.rows}
                for session in active:
                    if session.id in visible:
                        table.update_cell(
                            session.id,
                            "state",
                            status_text(
                                session_display_status(session), self.activity_frame
                            ),
                            update_width=False,
                        )
            selected = self.session_by_id.get(self.selected_session_id)
            if selected and session_display_status(selected) in {
                "busy",
                "retry",
                "review",
            }:
                self._render_detail()
        if next_active:
            try:
                next_is_active = (
                    self.query_one("#tabs", TabbedContent).active == "next"
                )
            except NoMatches:
                next_is_active = False
            if next_is_active:
                self._render_next()

    def _next_animation_active(self) -> bool:
        briefing = self.briefing_by_project_id.get(self.selected_project_id)
        if briefing is None:
            return False
        return briefing.research_status in {"queued", "running"}

    def _archive_filtered_snapshot(self, snapshot: DashboardSnapshot) -> DashboardSnapshot:
        """Drop archived sessions and their nested helper children from the board.

        Archiving never stops anything, so a still-running session stays
        visible until it stops; showing archived (Shift+U) keeps every
        archived row listed, dimmed, so Shift+A twice can unarchive it.
        """
        try:
            self._archived_ids = load_archived_sessions(self.archived_sessions_file)
        except OSError:  # a broken state file must never break the board
            self._archived_ids = set()
        sessions = snapshot.sessions
        parent_by_id = {
            session.id: (session.parent_id or session.agent_parent_id)
            for session in sessions
        }
        marked: set[str] = set()
        for session in sessions:
            seen: set[str] = set()
            cursor = session.id
            while cursor and cursor not in seen:
                if cursor in self._archived_ids:
                    marked.add(session.id)  # archived itself, or nested under one
                    break
                seen.add(cursor)
                cursor = parent_by_id.get(cursor, "")
        self._archive_marked_ids = marked
        if self.show_archived_sessions or not marked:
            return snapshot
        kept = tuple(
            session
            for session in sessions
            if session.id not in marked or self._session_still_running(session)
        )
        return replace(
            snapshot,
            sessions=kept,
            projects=self._recount_projects(snapshot.projects, kept),
        )

    @staticmethod
    def _recount_projects(
        projects: tuple[ProjectRecord, ...], sessions: tuple[SessionRecord, ...]
    ) -> tuple[ProjectRecord, ...]:
        """Project counts without the archived sessions, like build_projects."""
        grouped: dict[str, list[SessionRecord]] = {}
        for session in sessions:
            grouped.setdefault(session.project_id, []).append(session)
        return tuple(
            replace(
                project,
                session_count=len(grouped.get(project.id, ())),
                active_count=sum(
                    session.status in {"busy", "retry"}
                    for session in grouped.get(project.id, ())
                ),
                attached_count=sum(
                    session.instance_count > 0
                    for session in grouped.get(project.id, ())
                ),
                instance_count=sum(
                    session.instance_count
                    for session in grouped.get(project.id, ())
                ),
            )
            for project in projects
        )

    def _apply_snapshot(self, snapshot: DashboardSnapshot) -> None:
        snapshot = self._archive_filtered_snapshot(snapshot)
        self.snapshot = snapshot
        self.session_by_id = {session.id: session for session in snapshot.sessions}
        if self.inline_tmux:
            # Keep touch targets stationary while background activity refreshes.
            known = set(self._mobile_session_order)
            self._mobile_session_order = [
                session_id for session_id in self._mobile_session_order
                if session_id in self.session_by_id
            ] + [
                session.id for session in sorted(snapshot.sessions, key=lambda session: (
                    session.instance_count <= 0,
                    -(session.last_interaction_ms or session.updated_ms),
                )) if session.id not in known
            ]
        self._remember_recent_open_sessions()
        self._confirmed_permission_replies = {
            key
            for key in self._confirmed_permission_replies
            if self.session_by_id.get(key[0]) is not None
            and self.session_by_id[key[0]].permission_id == key[1]
        }
        self.project_by_id = {project.id: project for project in snapshot.projects}
        project_ids_by_path = {
            normalized_project_path(project.directory): project.id
            for project in snapshot.projects
            if project.directory
        }
        self.briefing_by_project_id = {}
        for briefing in snapshot.briefings:
            project_id = project_ids_by_path.get(
                normalized_project_path(briefing.project_path)
            )
            if project_id is not None:
                self.briefing_by_project_id.setdefault(project_id, briefing)
        if self.selected_session_id not in self.session_by_id:
            self.selected_session_id = snapshot.sessions[0].id if snapshot.sessions else ""
        if self.selected_project_id not in self.project_by_id:
            filtered_projects = self._filtered_projects()
            if filtered_projects:
                self.selected_project_id = filtered_projects[0].id
            else:
                self.selected_project_id = (
                    snapshot.projects[0].id if snapshot.projects else ""
                )
                if self.project_search_term:
                    self.project_filter = False
        self.query_one("#new-session", Button).disabled = (
            self.selected_project_id not in self.project_by_id
        )
        self.query_one("#new-browser-session", Button).disabled = (
            self.selected_project_id not in self.project_by_id
            or not hasattr(self.source, "create_browser_session")
        )
        self._refresh_state_clock()
        self._render_metrics()
        self._render_attention()
        self._render_projects()
        self._render_sessions()
        self._render_services()
        self._render_named_agents()
        self._render_agents()
        self._render_detail()
        self._render_next()
        self._render_alarms()
        if not self.initial_focus_set:
            self.initial_focus_set = True
            self.call_after_refresh(self._focus_initial_table)
        if snapshot.warning:
            self.notify(snapshot.warning, severity="warning")
        self._last_refresh_error = ""
        self._render_brand(snapshot.connection.upper())

    def _render_brand(self, state: str) -> None:
        state = sanitize_terminal_text(state)
        clock = datetime.now().astimezone().strftime("%H:%M:%S")
        tone = "#5eead4" if state == "LIVE" else "#f2b84b" if state in {"LOCKED", "SCANNING"} else "#ff6b7a"
        if self.inline_tmux:
            self.query_one("#brand", Static).update(
                f"[bold #e7f5fc]OC DECK[/] · [{tone}]{state}[/]"
            )
            return
        self.query_one("#brand", Static).update(
            "[bold #e7f5fc]OC DECK[/]  [#446274]//[/]  "
            "[dim]LOCAL OPERATIONS CONSOLE[/]"
            f"  [#446274]────────────────[/]  [{tone}]{state}[/]  [dim]{clock}[/]"
        )

    def _render_metrics(self) -> None:
        snapshot = self.snapshot
        instances = snapshot.terminal_instance_count
        attached = snapshot.attached_session_count
        running_services = sum(service.state == "active" for service in snapshot.services)
        self.query_one("#metric-projects", MetricCard).set_metric(
            "Projects",
            str(len(snapshot.projects)),
            f"{attached} linked session{'s' if attached != 1 else ''} · {instances} terminals",
        )
        unlinked = snapshot.unmapped_instance_count
        instance_detail = f"{snapshot.mapped_instance_count} linked"
        if unlinked:
            instance_detail += f" · {unlinked} unlinked TUI"
        self.query_one("#metric-sessions", MetricCard).set_metric(
            "All sessions"
            if self.show_agent_sessions or self.inline_tmux
            else "Main sessions",
            str(len(self._listed_sessions())),
            instance_detail,
            "#86b7ff",
        )
        self.query_one("#metric-memory", MetricCard).set_metric(
            "Machine",
            f"{snapshot.metrics.memory_percent:.0f}% RAM",
            f"load {snapshot.metrics.load_1m:.2f} · up {format_uptime(snapshot.metrics.uptime_seconds)}",
            "#d4a6ff",
        )
        self.query_one("#metric-connection", MetricCard).set_metric(
            "Signal",
            snapshot.connection.upper(),
            f"{running_services}/{len(snapshot.services)} services · {snapshot.connection_detail}",
            "#5eead4" if snapshot.connection == "live" else "#f2b84b",
        )

    def _refresh_state_clock(self) -> None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        current_ids = {session.id for session in self.snapshot.sessions}
        self._state_since_ms = {
            session_id: value
            for session_id, value in self._state_since_ms.items()
            if session_id in current_ids
        }
        for session in self.snapshot.sessions:
            state = agent_state(session)
            previous = self._state_since_ms.get(session.id)
            if previous is None or previous[0] != state:
                self._state_since_ms[session.id] = (state, now_ms)

    def _state_age(self, session: SessionRecord) -> str:
        state = agent_state(session)
        since = self._state_since_ms.get(session.id)
        if since is None or since[0] != state:
            return "now"
        return relative_time(since[1])

    def _agent_display_state(self, session: SessionRecord) -> str:
        return self.agent_display_state_by_id.get(session.id, agent_state(session))

    def _agent_attention_source(self, session: SessionRecord) -> SessionRecord:
        source_id = self.agent_attention_source_by_id.get(session.id)
        return self.session_by_id.get(source_id, session) if source_id else session

    def _agent_state_age(self, session: SessionRecord) -> str:
        return self._state_age(self._agent_attention_source(session))

    def _render_attention(self) -> None:
        try:
            attention = self.query_one("#attention", Static)
        except NoMatches:
            return
        sessions = [
            session
            for session in self.snapshot.sessions
            if agent_state(session) in {"question", "permission"}
        ]
        if not sessions:
            attention.display = False
            return
        sessions.sort(
            key=lambda session: (
                agent_state(session) != "permission",
                -(session.last_interaction_ms or session.updated_ms),
            )
        )
        content = Text()
        content.append(
            f"{len(sessions)} PENDING REQUEST{'S' if len(sessions) != 1 else ''}\n",
            style="bold #ff6b7a",
        )
        for index, session in enumerate(sessions):
            state = agent_state(session)
            label = AGENT_STATE_LABEL[state]
            title = "Hidden session" if self.private else session.title
            project = self.project_by_id.get(session.project_id)
            project_label = "Hidden project" if self.private else (
                project.name if project else compact_path(session.directory)
            )
            requirement = (
                "Hidden request"
                if self.private
                else session.permission or session.question or "Review required"
            )
            content.append(
                f"{label} ", style=STATUS_STYLE.get(state, STATUS_STYLE["idle"])
            )
            content.append(sanitize_terminal_text(requirement))
            content.append(
                f"\n  {clip_text(sanitize_terminal_text(title), 40)}"
                f" · {clip_text(sanitize_terminal_text(project_label), 28)}"
                f" · {self._state_age(session)}",
                style="dim #aebfcb",
            )
            if index < len(sessions) - 1:
                content.append("\n")
        attention.update(content)
        attention.display = True

    def _render_projects(self) -> None:
        table = self.query_one("#projects-table", DataTable)
        selected_id = self._selected_row_id(table, self.selected_project_id)
        rows: list[tuple[str, tuple[object, ...]]] = []
        filtered = self._filtered_projects()
        recent_by_project: dict[str, int] = {}
        count_by_project: dict[str, int] = {}
        for session in self._listed_sessions():
            count_by_project[session.project_id] = (
                count_by_project.get(session.project_id, 0) + 1
            )
            timestamp = session.last_interaction_ms or session.updated_ms
            recent_by_project[session.project_id] = max(
                timestamp, recent_by_project.get(session.project_id, 0)
            )
        for index, project in enumerate(filtered, start=1):
            name = project.name if not self.private else f"Project {index:02d}"
            rows.append((project.id, (
                Text(clip_text(name, 24), style=project_accent(project.id)),
                str(count_by_project.get(project.id, 0)),
                relative_time(recent_by_project[project.id])
                if project.id in recent_by_project
                else "-",
            )))
        found = self._update_table_rows(table, rows, selected_id)
        if found:
            self.selected_project_id = selected_id
        title = self.query_one("#projects-title", Static)
        if self.project_search_term:
            title.update(f"PROJECTS · {len(filtered)}/{len(self.snapshot.projects)} MATCH")
        else:
            title.update(f"PROJECTS · {len(filtered)}")

    def _filtered_projects(self) -> tuple[ProjectRecord, ...]:
        term = self.project_search_term.casefold()
        if not term:
            return self.snapshot.projects
        return tuple(
            project
            for project in self.snapshot.projects
            if term in project.name.casefold()
            or term in project.id.casefold()
            or term in project.directory.casefold()
            or term in Path(project.directory).name.casefold()
        )

    def _filtered_sessions(self) -> tuple[SessionRecord, ...]:
        # A search looks everywhere: a session hidden by the Main-sessions filter
        # (e.g. one Home Agent tagged as a monitor) must still be findable.
        pool = self.snapshot.sessions if self.search_term else self._listed_sessions()
        sessions = tuple(
            sorted(
                pool,
                key=lambda session: (
                    session.instance_count <= 0,
                    -(session.last_interaction_ms or session.updated_ms),
                    -session.updated_ms,
                ),
            )
        )
        if self.inline_tmux:
            order = {session_id: index for index, session_id in enumerate(self._mobile_session_order)}
            sessions = tuple(sorted(sessions, key=lambda session: order.get(session.id, len(order))))
            if not self.mobile_all_sessions:
                sessions = tuple(
                    session
                    for session in sessions
                    if session.instance_count > 0 and session.terminals
                )
        if not self.search_term:
            if not self.project_filter:
                return sessions
            project = self.project_by_id.get(self.selected_project_id)
            return tuple(
                session
                for session in sessions
                if self._session_matches_project(session, project)
            )

        term = self.search_term.casefold()
        matches = tuple(
            session
            for session in sessions
            if term in session.title.casefold()
            or term in Path(session.directory).name.casefold()
            or term in self._session_project_name(session).casefold()
        )
        if not self.project_filter:
            return matches
        project = self.project_by_id.get(self.selected_project_id)
        return tuple(
            session
            for session in matches
            if self._session_matches_project(session, project)
        ) + tuple(
            session
            for session in matches
            if not self._session_matches_project(session, project)
        )

    def _listed_sessions(self) -> tuple[SessionRecord, ...]:
        # Keep the complete snapshot for live-agent state and permission alerts.
        if self.show_agent_sessions or self.inline_tmux:
            return self.snapshot.sessions
        return tuple(
            session for session in self.snapshot.sessions if not session.agent_session_kind
        )

    def _session_project_name(self, session: SessionRecord) -> str:
        project = self.project_by_id.get(session.project_id)
        return project.name if project else Path(session.directory).name

    @staticmethod
    def _session_matches_project(
        session: SessionRecord, project: ProjectRecord | None
    ) -> bool:
        if project is None:
            return True
        return session.project_id == project.id

    def _render_sessions(self) -> None:
        table = self.query_one("#sessions-table", DataTable)
        selected_id = self._selected_row_id(table, self.selected_session_id)
        rows: list[tuple[str, tuple[object, ...]]] = []
        filtered = self._filtered_sessions()
        self._render_sessions_title(len(filtered))
        filtered_ids = {session.id for session in filtered}
        if selected_id not in filtered_ids:
            # The selected row went away (archived, filtered): keep the place,
            # taking the row that moves up into it, not the first row.
            shown = [str(key.value) for key in table.rows]
            index = shown.index(selected_id) if selected_id in shown else 0
            remaining = [row_id for row_id in shown if row_id in filtered_ids]
            following = [row_id for row_id in shown[index + 1:] if row_id in filtered_ids]
            preceding = [row_id for row_id in shown[:index] if row_id in filtered_ids]
            selected_id = (following[0] if following else preceding[-1] if preceding
                           else remaining[0] if remaining else filtered[0].id if filtered else "")
        title_width = table.columns[self.session_title_column].width
        project_width = (
            table.columns[self.session_project_column].width
            if self.session_project_column is not None
            else 0
        )
        for index, session in enumerate(filtered, start=1):
            title = f"Session {index:02d}" if self.private else session.title
            title_style = (
                SUBAGENT_STYLE
                if not self.private and session.agent_session_kind
                else ""
            )
            if not self.private and session.agent_session_kind:
                title = f"[{session.agent_session_kind}] {title}"
            badge = HARNESS_BADGES.get(session_harness(session), "")
            if badge:
                title = f"{badge} {title}"
            if not self.private and session.id in self._archive_marked_ids:
                title = f"{ARCHIVED_MARKER} {title}"
                title_style = ARCHIVED_TITLE_STYLE
            if self.inline_tmux:
                state = session_display_status(session)
                label = "READY" if state in {"idle", "open"} else AGENT_STATE_LABEL.get(
                    state, state.upper()
                )
                if not session.instance_count and state == "idle":
                    label = "SAVED"
                title_lines = Text(
                    sanitize_terminal_text(title), style=title_style or "bold #e7f5fc"
                ).wrap(self.console, title_width, overflow="fold")
                if len(title_lines) > 2:
                    title_lines[1].truncate(title_width - 1)
                    title_lines[1].append("…")
                card = Text("\n").join(title_lines[:2])
                card.append("\n" * (3 - min(2, len(title_lines))))
                card.append(label, style=STATUS_STYLE.get(state, STATUS_STYLE["idle"]))
                project = "hidden" if self.private else self._session_project_name(session)
                card.append(
                    f" · {clip_text(sanitize_terminal_text(project), max(1, title_width - len(label) - 3))}",
                    style="#8ba4b5",
                )
                rows.append((session.id, (card,)))
                continue
            project = "hidden" if self.private else self._session_project_name(session)
            project_style = (
                "" if self.private else project_accent(session.project_id)
            )
            rows.append((session.id, (
                status_text(session_display_status(session), self.activity_frame),
                instance_text(session.instance_count),
                Text(clip_text(title, title_width), style=title_style),
                Text(clip_text(project, project_width), style=project_style),
                relative_time(session_age_ms(session)),
            )))
        self._update_table_rows(table, rows, selected_id, row_height=5 if self.inline_tmux else 1)
        self.selected_session_id = selected_id
        if self.inline_tmux:
            empty = self.query_one("#mobile-session-empty", Static)
            empty.display = not filtered
            empty.update(
                "No matching sessions. Clear the search or try All sessions."
                if self.search_term else "No live terminals. Try All sessions to reopen a conversation."
            )
            for button_id in ("mobile-previous", "mobile-next", "mobile-open"):
                self.query_one(f"#{button_id}", Button).disabled = not filtered

    def _update_table_rows(
        self,
        table: DataTable,
        rows: list[tuple[str, tuple[object, ...]]],
        selected_id: str,
        *,
        row_height: int = 1,
    ) -> bool:
        """Update live cells without treating refreshes as user navigation."""
        row_ids = [row_id for row_id, _cells in rows]
        with table.prevent(DataTable.RowHighlighted):
            if [str(key.value) for key in table.rows] == row_ids:
                for row_id, cells in rows:
                    for column, value in zip(table.columns, cells):
                        if table.get_cell(row_id, column) != value:
                            table.update_cell(
                                row_id, column, value,
                                update_width=table.columns[column].auto_width,
                            )
                return self._restore_table_cursor(table, selected_id)

            hover = table.hover_coordinate
            hover_id = (
                str(table.coordinate_to_cell_key(hover).row_key.value)
                if table.is_valid_coordinate(hover)
                else ""
            )
            scroll_x, scroll_y = table.scroll_x, table.scroll_y
            old_ids = [str(key.value) for key in table.rows]
            old_index = old_ids.index(selected_id) if selected_id in old_ids else table.cursor_row
            table.clear(columns=False)
            for row_id, cells in rows:
                table.add_row(*cells, key=row_id, height=row_height)
            found = self._restore_table_cursor(table, selected_id)
            if not found and rows and old_index > 0:
                # The selected row went away (archived, closed, filtered): stay
                # at the same place, on the row that moved up, not at the top.
                table.move_cursor(row=min(old_index, len(rows) - 1), scroll=False)
            table.hover_coordinate = (
                Coordinate(row_ids.index(hover_id), hover.column)
                if hover_id in row_ids
                else Coordinate(-1, -1)
            )
            table.scroll_to(x=scroll_x, y=scroll_y, animate=False, immediate=True)
            return found

    @staticmethod
    def _restore_table_cursor(table: DataTable, row_id: str) -> bool:
        for row_index, row_key in enumerate(table.rows):
            if str(row_key.value) == row_id:
                table.move_cursor(row=row_index, scroll=False)
                return True
        return False

    @staticmethod
    def _selected_row_id(table: DataTable, fallback_id: str) -> str:
        """Identity that should survive a rebuild: cursor row when the user
        is navigating that table, otherwise the stored selection."""
        if (
            table.has_focus
            and table.row_count
            and table.is_valid_coordinate(table.cursor_coordinate)
        ):
            return str(
                table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            )
        return fallback_id

    def _render_sessions_title(self, count: int) -> None:
        title = self.query_one("#sessions-title", Static)
        filters = []
        if self.search_term:
            term = (
                "[hidden]" if self.private else sanitize_terminal_text(self.search_term)
            )
            filters.append(f'Search: "{term}"')
        if self.project_filter:
            project = self.project_by_id.get(self.selected_project_id)
            name = "[hidden]" if self.private else sanitize_terminal_text(
                project.name if project else "selected"
            )
            filters.append(f"Project: {name}{' first' if self.search_term else ' only'}")
        summary = self.query_one("#session-filters", Static)
        summary.update(" | ".join(filters))
        summary.display = bool(filters)
        self.query_one("#session-search", Input).set_class(
            bool(self.search_term), "filtered"
        )
        if self.inline_tmux:
            scope = "ALL SESSIONS" if self.mobile_all_sessions else "LIVE SESSIONS"
            title.update(f"{scope} ({count}) · TAP TO OPEN")
            return
        scope = "ALL PROJECTS"
        if self.project_filter:
            project = self.project_by_id.get(self.selected_project_id)
            name = "" if self.private else sanitize_terminal_text(
                project.name if project else ""
            ).upper()
            scope = name or "PROJECT"
            if self.search_term:
                scope += " FIRST"
        heading = "PRIVATE SESSIONS" if self.private else (
            "ALL SESSIONS" if self.show_agent_sessions else "MAIN SESSIONS"
        )
        title.update(f"{heading} · {scope} · {count}")

    def _render_services(self) -> None:
        table = self.query_one("#services-table", DataTable)
        selected_id = self._selected_row_id(table, "")
        rows: list[tuple[str, tuple[object, ...]]] = []
        for service in self.snapshot.services:
            if service.state == "active":
                state = Text("● ACTIVE", style="bold #5eead4")
            elif service.state in {"inactive", "failed"}:
                state = Text(f"● {service.state.upper()}", style="bold #ff6b7a")
            else:
                state = Text("○ UNKNOWN", style="dim")
            rows.append((service.unit, (
                state,
                Text(sanitize_terminal_text(service.label)),
                Text(sanitize_terminal_text(service.role)),
                Text(sanitize_terminal_text(service.unit)),
            )))
        self._update_table_rows(table, rows, selected_id)

    def _render_named_agents(self) -> None:
        content = Text(no_wrap=True, overflow="ellipsis")
        state_styles = {
            "unavailable": "bold #ff6b7a",
            "ready": "bold #86b7ff",
            "idle": "bold #5eead4",
            "active": "bold #4ade80",
            "attention": "bold #ff6b7a",
        }

        def state_label(agent) -> str:
            state = agent.state.lower()
            return (
                f"STALE/{state.upper()}"
                if self.snapshot.named_agents_stale
                else state.upper()
            )

        def detail_label(agent) -> str:
            detail_parts = [agent.detail]
            if not agent.configured and "agent file missing" not in agent.detail:
                detail_parts.append("agent file missing")
            if agent.description:
                detail_parts.append(agent.description)
            browser = self.snapshot.signed_in_tabs_status
            if self.snapshot.signed_in_tabs_stale:
                browser = f"STALE/{browser}"
            detail_parts.append(f"browser {browser}")
            if self.snapshot.named_agents_stale and self.snapshot.named_agents_error:
                detail_parts.insert(0, self.snapshot.named_agents_error)
            if self.snapshot.signed_in_tabs_stale and self.snapshot.signed_in_tabs_error:
                detail_parts.append(self.snapshot.signed_in_tabs_error)
            return "; ".join(part for part in detail_parts if part)

        panel = self.query_one("#named-agent-status", Static)
        # No orchestrator configured (see home-agent/README.md): nothing to show.
        has_named_agents = bool(self.snapshot.named_agents)
        panel.display = has_named_agents
        if not has_named_agents:
            return
        panel_width = panel.content_size.width or max(1, self.size.width - 8)
        compact = panel_width < 90
        panel_height = 12 if compact else 6
        if panel.styles.height is None or panel.styles.height.value != panel_height:
            panel.styles.height = panel_height
        if compact:
            content.append("STATE        AGENT       SESSION", style="bold #7890a2")
            detail_width = max(10, panel_width - 2)
            for agent in self.snapshot.named_agents:
                state = agent.state.lower()
                label = state_label(agent)
                content.append("\n")
                content.append(
                    f"{clip_text(label, 12):<13}",
                    style="bold #f2b84b"
                    if self.snapshot.named_agents_stale
                    else state_styles.get(state, "dim #668094"),
                )
                content.append(f"{clip_text(agent.name.upper(), 11):<12}")
                content.append(str(agent.session_count or "-"))
                content.append(
                    f"\n  MODEL {clip_text(agent.model or '-', detail_width - 8)}",
                    style="#86b7ff",
                )
                content.append(
                    f"\n  {clip_text(f'{agent.role} · {detail_label(agent)}', detail_width)}",
                    style="dim #aebfcb",
                )
            if not self.snapshot.named_agents:
                content.append("\nNamed agent status unavailable", style="dim #668094")
            self.query_one("#named-agent-status", Static).update(content)
            return

        content.append(
            clip_text(f"{'STATE':<13}{'AGENT':<12}{'ROLE':<23}{'MODEL':<28}{'SESSION':<9}DETAIL", panel_width),
            style="bold #7890a2",
        )
        for index, agent in enumerate(self.snapshot.named_agents):
            content.append("\n")
            state = agent.state.lower()
            label = state_label(agent)
            content.append(
                f"{clip_text(label, 12):<13}",
                style="bold #f2b84b"
                if self.snapshot.named_agents_stale
                else state_styles.get(state, "dim #668094"),
            )
            content.append(
                f"{clip_text(agent.name.upper(), 11):<12}", style="bold #e7f5fc"
            )
            content.append(f"{clip_text(agent.role, 22):<23}")
            content.append(f"{clip_text(agent.model or '-', 27):<28}", style="#86b7ff")
            content.append(f"{agent.session_count or '-':<9}")
            content.append(
                clip_text(sanitize_terminal_text(detail_label(agent)), max(1, panel_width - 85)),
                style="dim #aebfcb",
            )
        if not self.snapshot.named_agents:
            content.append("\nNamed agent status unavailable", style="dim #668094")
        self.query_one("#named-agent-status", Static).update(content)

    def _owner_opened_ids(self) -> set[str]:
        """Resolve recorded owner launches against this snapshot (display only)."""
        try:
            return refresh_owner_opened(self.snapshot.sessions, self.owner_opened_file)
        except OSError:  # a broken state file must never break the board
            return load_owner_sessions(self.owner_opened_file)

    def _record_owner_opened_session(self, session_id: str) -> None:
        """Remember a session the owner opened from OC Deck; cosmetic, best effort."""
        if not hasattr(self, "owner_opened_file"):
            return  # a partially built app (tests) never writes real state
        try:
            record_owner_session(session_id, self.owner_opened_file)
        except OSError:
            pass

    def _record_owner_opened_launch(self, terminal: str, directory: str) -> None:
        """Remember a launch whose session id the CLI has not chosen yet."""
        if not hasattr(self, "owner_opened_file"):
            return
        try:
            record_owner_launch(terminal, directory, self.owner_opened_file)
        except OSError:
            pass

    def _render_agents(self, selection_id: str | None = None) -> None:
        table = self.query_one("#agents-table", DataTable)
        selected_id = (
            self._selected_row_id(table, self.selected_session_id)
            if selection_id is None
            else selection_id
        )
        rows: list[tuple[str, tuple[object, ...]]] = []
        base_states = {
            session.id: agent_state(session) for session in self.snapshot.sessions
        }
        sessions_by_id = {
            session.id: session for session in self.snapshot.sessions
        }
        # Who opened each session: an owner record beats every agent signal,
        # and no signal renders like an owner row (the owner's older sessions).
        owner_ids = self._owner_opened_ids()  # one state read per render, not per row
        self.agent_origin_by_id = {
            session.id: classify_session(session, owner_ids)
            for session in self.snapshot.sessions
        }

        def agent_opened(session: SessionRecord) -> bool:
            origin = self.agent_origin_by_id.get(session.id)
            return origin is not None and origin.verdict == AGENT_VERDICT

        def eligible_open_session(
            session: SessionRecord, visiting: set[str] | None = None
        ) -> bool:
            parent_id = session.parent_id or session.agent_parent_id
            if not parent_id:
                return True
            parent = sessions_by_id.get(parent_id)
            if parent is None:
                return False
            visiting = set() if visiting is None else visiting
            if session.id in visiting:
                return False
            visiting.add(session.id)
            parent_state = base_states.get(parent.id, "idle")
            if parent_state == "idle":
                return False
            if parent_state == "open":
                return eligible_open_session(parent, visiting)
            return True

        active_ids = {
            session.id
            for session in self.snapshot.sessions
            if base_states[session.id] != "idle"
            and (
                base_states[session.id] != "open"
                or eligible_open_session(session)
            )
        }
        parent_by_id = {
            session.id: session.parent_id or session.agent_parent_id
            for session in self.snapshot.sessions
        }
        # Keep an idle parent visible when one of its native child sessions is
        # active, otherwise a subagent request cannot reach the main row.
        changed = True
        while changed:
            changed = False
            for session_id in tuple(active_ids):
                parent_id = parent_by_id.get(session_id, "")
                if parent_id and parent_id not in active_ids:
                    active_ids.add(parent_id)
                    changed = True
        live = [
            session for session in self.snapshot.sessions if session.id in active_ids
        ]
        live.sort(
            key=lambda session: (
                -(session.last_interaction_ms or session.updated_ms),
                -session.updated_ms,
            )
        )
        live_by_id = {session.id: session for session in live}
        children_by_parent: dict[str, list[SessionRecord]] = {}
        roots: list[SessionRecord] = []
        for session in live:
            parent_id = session.parent_id or session.agent_parent_id
            parent = live_by_id.get(parent_id)
            if parent is None or parent.id == session.id:
                roots.append(session)
                continue
            children_by_parent.setdefault(parent.id, []).append(session)

        # CLOSED agent-opened sessions nest under their visible parent, like
        # live helpers: a finished helper stays under the session that launched
        # it instead of waiting among the owner's history rows.
        self.agent_closed_row_ids = set()
        closed_children: dict[str, list[SessionRecord]] = {}
        for session in self.snapshot.sessions:
            parent_id = session.parent_id or session.agent_parent_id
            if (
                session.id in live_by_id
                or session.instance_count > 0
                or not agent_opened(session)
                or agent_state(session) != "idle"
                or not parent_id
                or parent_id == session.id
                or parent_id not in live_by_id
            ):
                continue
            closed_children.setdefault(parent_id, []).append(session)
        for parent_id, children in closed_children.items():
            children.sort(key=lambda session: -session.updated_ms)
            children = children[:MAX_RECENT_AGENT_ROWS]
            closed_children[parent_id] = children
            self.agent_closed_row_ids.update(child.id for child in children)
            # Live helpers first, closed helpers after them, under one arrow.
            children_by_parent.setdefault(parent_id, []).extend(children)
        # Agent-opened sessions without a place to nest list after the
        # owner's own rows instead of interleaving by recency.
        roots = [session for session in roots if not agent_opened(session)] + [
            session for session in roots if agent_opened(session)
        ]

        self.agent_children_by_id = {
            parent_id: tuple(child.id for child in children)
            for parent_id, children in children_by_parent.items()
        }
        self.agent_parent_by_id = {
            child.id: parent_id
            for parent_id, children in children_by_parent.items()
            for child in children
        }
        self.agent_display_state_by_id = {}
        self.agent_attention_source_by_id = {}

        state_priority = {
            "idle": 0,
            "open": 1,
            "review": 2,
            "busy": 3,
            "job": 4,
            "retry": 5,
            "stalled": 6,
            "question": 7,
            "permission": 8,
        }

        def descendant_state(
            session_id: str, visiting: set[str] | None = None
        ) -> tuple[str, str] | None:
            visiting = set() if visiting is None else visiting
            if session_id in visiting:
                return None
            visiting.add(session_id)
            best: tuple[str, str] | None = None
            for child_id in self.agent_children_by_id.get(session_id, ()):
                child = live_by_id.get(child_id)
                if child is None:
                    continue
                child_state = base_states.get(child.id, agent_state(child))
                candidates: list[tuple[str, str]] = [(child_state, child.id)]
                nested = descendant_state(child.id, visiting)
                if nested is not None:
                    candidates.append(nested)
                for candidate in candidates:
                    if best is None or state_priority[candidate[0]] > state_priority[best[0]]:
                        best = candidate
            return best

        for session in live:
            own_state = base_states.get(session.id, agent_state(session))
            chosen = (own_state, session.id)
            descendant = descendant_state(session.id)
            if descendant is not None and state_priority[descendant[0]] > state_priority[chosen[0]]:
                chosen = descendant
            self.agent_display_state_by_id[session.id] = chosen[0]
            self.agent_attention_source_by_id[session.id] = chosen[1]
        self.expanded_agent_ids.intersection_update(self.agent_children_by_id)

        visible: list[tuple[SessionRecord, int]] = []
        visited: set[str] = set()

        def add_branch(session: SessionRecord, depth: int) -> None:
            if session.id in visited:
                return
            visited.add(session.id)
            visible.append((session, depth))
            if session.id not in self.expanded_agent_ids:
                return
            for child in children_by_parent.get(session.id, ()):
                add_branch(child, depth + 1)

        for session in roots:
            add_branch(session, 0)

        for session, depth in visible:
            state = self._agent_display_state(session)
            signal_session = self._agent_attention_source(session)
            title = "Hidden session" if self.private else session.title
            badge = HARNESS_BADGES.get(session_harness(session), "")
            if badge and not self.private:
                # Visible even when the RUNTIME column is scrolled off a narrow window.
                title = f"{badge} {title}"
            archived = not self.private and session.id in self._archive_marked_ids
            if archived:
                title = f"{ARCHIVED_MARKER} {title}"
            if session.id in self.agent_closed_row_ids:
                # A closed helper nested under its visible parent.
                if not self.private:
                    title = f"{AGENT_MARKER} {title}"
                project = "hidden" if self.private else self._session_project_name(session)
                title = f"{'  ' * min(depth, 4)}└ {title}" if depth else title
                rows.append((session.id, self._closed_agent_row(session, title, project)))
                continue
            if agent_opened(session) and not self.private:
                title = f"{AGENT_MARKER} {title}"
            project = "hidden" if self.private else self._session_project_name(session)
            if self.private and state in {"question", "permission", "job"}:
                detail = HIDDEN_PROMPT_LABEL
            elif state == "question":
                detail = signal_session.question or "Input required"
                if signal_session.id != session.id:
                    detail = f"subagent: {detail}"
            elif state == "permission":
                detail = signal_session.permission or "Permission required"
                if signal_session.id != session.id:
                    detail = f"subagent: {detail}"
            elif state == "job":
                jobs = signal_session.background_jobs or session.background_jobs
                detail = ("Background job: " + "; ".join(jobs)) if jobs else "Background job"
                if signal_session.id != session.id:
                    detail = f"subagent: {detail}"
            elif signal_session.id != session.id:
                detail = HIDDEN_PROMPT_LABEL if self.private else (
                    f"helper {AGENT_STATE_LABEL[state]}: {signal_session.title}"
                )
            elif self.private and session.last_prompt:
                # Prompt content must never be visible while private.
                detail = HIDDEN_PROMPT_LABEL
            elif session.last_prompt:
                detail = session.last_prompt
            elif state == "review":
                detail = "waiting for you"
            else:
                detail = session.permission
            kind = term_kind(
                attached=session.terminal_attached, tmux=bool(session.terminals),
                live=session.instance_count > 0,
            )
            terminal = Text(term_cell(kind), style=TERM_STYLE[kind])
            children = self.agent_children_by_id.get(session.id, ())
            indent = "  " * min(depth, 4)
            if children:
                arrow = "▾" if session.id in self.expanded_agent_ids else "▸"
                title = f"{indent}{arrow}[{len(children)}] {title}"
            elif depth:
                title = f"{indent}└ {title}"
            elif session.parent_id or session.agent_parent_id:
                title = f"↳ {title}"
            widths = self.agent_widths
            title_style = (
                ARCHIVED_TITLE_STYLE
                if archived
                else AGENT_TITLE_STYLE if agent_opened(session) and not self.private else ""
            )
            rows.append((session.id, (
                Text(
                    state_cell(state, self._agent_state_age(session), inherited=signal_session.id != session.id),
                    style=STATUS_STYLE.get(state, STATUS_STYLE["idle"]),
                ),
                terminal,
                Text(
                    clip_text(sanitize_terminal_text(title), widths.session),
                    style=title_style,
                ),
                Text(
                    clip_text(sanitize_terminal_text(project), max(1, widths.project)),
                    style="" if self.private else project_accent(session.project_id),
                ),
                relative_time(session_age_ms(session)),
                Text(clip_text(sanitize_terminal_text(detail), max(AGENT_DETAIL_CLIP, widths.detail))),
                self._runtime_text(session),
            )))
        closed_sessions = [
            session for session in self._closed_agent_sessions()
            if session.id not in self.agent_closed_row_ids
        ]
        # The owner's own closed history rows first, then agent-opened ones.
        closed_sessions = (
            [session for session in closed_sessions if not agent_opened(session)]
            + [session for session in closed_sessions if agent_opened(session)]
        )
        for session in closed_sessions:
            title = "Hidden session" if self.private else session.title
            badge = HARNESS_BADGES.get(session_harness(session), "")
            if badge and not self.private:
                title = f"{badge} {title}"
            if agent_opened(session) and not self.private:
                title = f"{AGENT_MARKER} {title}"
            if not self.private and session.id in self._archive_marked_ids:
                title = f"{ARCHIVED_MARKER} {title}"
            project = "hidden" if self.private else self._session_project_name(session)
            rows.append((session.id, self._closed_agent_row(session, title, project)))
        relaunch = self.query_one("#relaunch-agents", Button)
        relaunch.label = (
            f"REOPEN {len(closed_sessions)} CLOSED / {len(self.recent_open_sessions)} REMEMBERED (Shift+L)"
        )
        relaunch.disabled = not closed_sessions or self.inline_tmux or self.snapshot.connection != "live"
        purge = self.query_one("#purge-agent-tabs", Button)
        purge.disabled = self.inline_tmux or self.snapshot.connection != "live"
        visible_columns = [COLUMNS.index(str(key.value)) for key in table.columns]
        rows = [(key, tuple(cells[index] for index in visible_columns)) for key, cells in rows]
        self._update_table_rows(table, rows, selected_id)
        if table.has_focus:
            self.selected_session_id = self._selected_row_id(table, "")
        self._render_agent_focus()

    def _closed_agent_row(self, session: SessionRecord, title: str, project: str) -> tuple[object, ...]:
        """A closed row's cells: dim, saved terminal, relaunchable with o."""
        return (
            Text(state_cell("closed"), style=STATUS_STYLE["closed"]),
            Text(term_cell("saved"), style=TERM_STYLE["saved"]),
            Text(
                clip_text(sanitize_terminal_text(title), self.agent_widths.session),
                style=AGENT_TITLE_STYLE,
            ),
            Text(
                clip_text(sanitize_terminal_text(project), max(1, self.agent_widths.project)),
                style="" if self.private else project_accent(session.project_id),
            ),
            relative_time(session_age_ms(session)),
            Text("previous session · o relaunches", style="dim #aebfcb"),
            self._runtime_text(session),
        )

    def action_toggle_subagents(self) -> None:
        table = self.query_one("#agents-table", DataTable)
        if (
            not table.row_count
            or not table.is_valid_coordinate(table.cursor_coordinate)
        ):
            return
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        session_id = str(row_key.value)
        if self.agent_children_by_id.get(session_id):
            if session_id in self.expanded_agent_ids:
                self.expanded_agent_ids.remove(session_id)
            else:
                self.expanded_agent_ids.add(session_id)
            self.selected_session_id = session_id
            self._render_agents(selection_id=session_id)
            return

        parent_id = self.agent_parent_by_id.get(session_id)
        if parent_id is None:
            return
        self.expanded_agent_ids.discard(parent_id)
        self.selected_session_id = parent_id
        self._render_agents(selection_id=parent_id)

    @property
    def recent_open_sessions(self) -> list[str]:
        return self._recent_history.ids

    def _remember_recent_open_sessions(self) -> None:
        """Observe open roots without equating a partial listing with deletion."""
        if not self.session_by_id:
            return
        open_ids = [
            session.id
            for session in self.snapshot.sessions
            if session.instance_count > 0 and not session.agent_session_kind
        ]
        self._recent_history.observe(open_ids)
        if self._recent_history.write_failed and not self._history_write_warning:
            self.notify("Relaunch history could not be saved; it is retained in memory", severity="warning")
        self._history_write_warning = self._recent_history.write_failed
        now = time.monotonic()
        self._relaunch_pending = {
            session_id: deadline for session_id, deadline in self._relaunch_pending.items()
            if deadline > now and session_id not in open_ids
        }

    def _closed_agent_sessions(self) -> list[SessionRecord]:
        """Previously open agent sessions that can be relaunched now."""
        sessions: list[SessionRecord] = []
        for session_id in self.recent_open_sessions:
            session = self.session_by_id.get(session_id)
            if (
                session is None
                or session.instance_count > 0
                or session.agent_session_kind
                or agent_state(session) != "idle"
                or session.id in self.agent_display_state_by_id
                or self._relaunch_pending.get(session.id, 0) > time.monotonic()
            ):
                continue
            sessions.append(session)
            if len(sessions) >= MAX_RECENT_AGENT_ROWS:
                break
        return sessions

    def _render_next(self) -> None:
        try:
            view = self.query_one("#next-view", NextStepsView)
            surface = self.query_one("#next-content", Static)
        except NoMatches:
            return
        width = max(24, view.size.width - 8)
        content = Text()

        def add(value: str, style: str = "") -> None:
            content.append(sanitize_terminal_text(value), style=style)

        def line(value: str = "", style: str = "") -> None:
            add(value, style)
            content.append("\n")

        line("PORTFOLIO // NEXT STEPS", "bold #e7f5fc")
        line(
            "Read-only briefing signal · recommendations never execute here",
            "dim #7890a2",
        )
        line("─" * min(width, 78), "#284456")

        projects = self.snapshot.projects
        project = self.project_by_id.get(self.selected_project_id)
        if not projects or project is None:
            line("NO PROJECTS", "bold #f2b84b")
            line("No catalog projects are available for a next-steps view.", "dim")
            surface.update(content)
            return

        project_index = next(
            (
                index
                for index, candidate in enumerate(projects, start=1)
                if candidate.id == project.id
            ),
            1,
        )
        project_name = (
            f"Project {project_index:02d}"
            if self.private
            else sanitize_terminal_text(project.name)
        )
        add(f"PROJECT {project_index:02d}/{len(projects):02d}  ", "dim #7890a2")
        add(clip_text(project_name, max(12, width - 24)), project_accent(project.id))
        if width >= 52:
            add("  ↑/↓ or j/k", "dim #668094")
        content.append("\n\n")

        generated_at = self.snapshot.briefing_generated_at
        report_status = self.snapshot.briefing_status
        if not report_status or generated_at is None:
            line("NO BRIEFING REPORT", "bold #f2b84b")
            line(
                "Home Agent has not published a valid supported briefing artifact yet.",
                "dim #8ba4b5",
            )
            line("Refresh after reports/latest.json is available.", "dim #668094")
            surface.update(content)
            return

        now = self.snapshot.collected_at
        if now.tzinfo is None or now.utcoffset() is None:
            now = now.replace(tzinfo=timezone.utc)
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            generated_at = generated_at.replace(tzinfo=timezone.utc)
        report_age_seconds = max(0, int((now - generated_at).total_seconds()))
        report_age = relative_time(int(generated_at.timestamp() * 1000), now)
        status_style = {
            "completed": "bold #4ade80",
            "running": "bold #5eead4",
            "partial": "bold #f2b84b",
            "failed": "bold #ff6b7a",
        }.get(report_status, "dim #7890a2")
        add("REPORT  ", "dim #7890a2")
        add(report_status.upper(), status_style)
        add(f"  ·  generated {report_age} ago", "dim #8ba4b5")
        if not self.private and width >= 72 and self.snapshot.briefing_report_id:
            add(
                "  ·  "
                + clip_text(
                    sanitize_terminal_text(self.snapshot.briefing_report_id), 24
                ),
                "dim #668094",
            )
        content.append("\n")

        if report_status == "running":
            line("LIVE REPORT · content may change on the next refresh", "#5eead4")
        elif report_status == "partial":
            line(
                "PARTIAL REPORT · project research has mixed completed and failed outcomes",
                "#f2b84b",
            )
        elif report_status == "failed":
            line("FAILED REPORT · displayed content may be incomplete", "#ff6b7a")
        if report_age_seconds > BRIEFING_STALE_SECONDS:
            line("STALE REPORT · verify this briefing before relying on it", "#f2b84b")

        if self.private:
            content.append("\n")
            line("PRIVACY MODE", "bold #d4a6ff")
            line(
                "Summary, blockers, outputs, locators, and recommendation text are hidden.",
                "dim #8ba4b5",
            )
            surface.update(content)
            return

        briefing = self.briefing_by_project_id.get(project.id)
        if briefing is None:
            content.append("\n")
            line("NO PROJECT BRIEFING", "bold #f2b84b")
            if report_status == "failed":
                line("The failed report contains no usable entry for this project.", "dim #8ba4b5")
            else:
                line("No artifact entry exactly matches this project's normalized path.", "dim #8ba4b5")
            surface.update(content)
            return

        add("ASSESSMENT  ", "dim #7890a2")
        add(
            briefing.assessment.replace("-", " ").upper(),
            ASSESSMENT_STYLE.get(briefing.assessment, ASSESSMENT_STYLE["unknown"]),
        )
        add(f"  ·  confidence {briefing.confidence.upper()}", "#8ba4b5")
        evidence_at = briefing.evidence_at
        if evidence_at is None:
            add("  ·  evidence unknown", "#8ba4b5")
        else:
            if evidence_at.tzinfo is None or evidence_at.utcoffset() is None:
                evidence_at = evidence_at.replace(tzinfo=timezone.utc)
            evidence_age_seconds = max(0, int((now - evidence_at).total_seconds()))
            evidence_age = relative_time(
                int(evidence_at.timestamp() * 1000), now
            )
            add(f"  ·  evidence {evidence_age} ago", "#8ba4b5")
            if evidence_age_seconds > BRIEFING_STALE_SECONDS:
                add("  STALE", "bold #f2b84b")
        content.append("\n")

        research_active = briefing.research_status in {"queued", "running"}
        research_symbol = {
            "completed": "✓",
            "failed": "!",
        }.get(
            briefing.research_status,
            ACTIVITY_FRAMES[self.activity_frame % len(ACTIVITY_FRAMES)],
        )
        research_style = {
            "completed": "#4ade80",
            "failed": "bold #ff6b7a",
            "queued": "bold #86b7ff",
            "running": "bold #5eead4",
        }.get(briefing.research_status, "dim")
        add("RESEARCH    ", "dim #7890a2")
        add(
            f"{research_symbol} {briefing.research_status.upper()}",
            research_style,
        )
        content.append("\n\n")

        line("SUMMARY", "bold #86b7ff")
        line(briefing.summary or "No summary was provided.", "#cbd9e3")
        content.append("\n")

        line("BLOCKERS", "bold #ff9e7a")
        if briefing.blockers:
            for blocker in briefing.blockers:
                add("!  ", "bold #ff6b7a")
                line(blocker, "#cbd9e3")
        else:
            line("○  None reported", "dim #7890a2")
        content.append("\n")

        line("COMPLETED OUTPUTS", "bold #7ee081")
        if briefing.completed_outputs:
            for label, locator in briefing.completed_outputs:
                add("✓  ", "bold #4ade80")
                line(label, "#cbd9e3")
                add("   ")
                line(clip_text(locator, max(12, width - 4)), "dim #7890a2")
        else:
            line("○  None reported", "dim #7890a2")
        content.append("\n")

        line("NEXT STEPS", "bold #d4a6ff")
        if not briefing.next_steps:
            line("○  No recommended steps in this report.", "dim #7890a2")
        for index, step in enumerate(briefing.next_steps):
            last = index == len(briefing.next_steps) - 1
            connector = "└─" if last else "├─"
            continuation = "   " if last else "│  "
            symbol = (
                ACTIVITY_FRAMES[self.activity_frame % len(ACTIVITY_FRAMES)]
                if step.state == "now" and research_active
                else {"next": "○", "blocked": "!", "done": "✓"}.get(
                    step.state, "●" if step.state == "now" else "○"
                )
            )
            style = STEP_STYLE.get(step.state, "dim #7890a2")
            add(f"{connector} {symbol} {step.state.upper():7} ", style)
            line(step.title, "bold #e7f5fc" if step.state != "done" else "dim")
            if step.detail:
                add(continuation, "dim #446274")
                line(step.detail, "#aebfcb")
            add(continuation, "dim #446274")
            line("approval required · advisory only", "dim #f2b84b")
        surface.update(content)

    def _render_detail(self) -> None:
        try:
            detail = self.query_one("#session-detail", Static)
        except NoMatches:
            return
        session = self.session_by_id.get(self.selected_session_id)
        if not session:
            detail.update("[dim]Select a session to inspect its signal.[/]")
            return
        title = "Hidden session" if self.private else session.title
        path = compact_path(session.directory, self.private)
        session_id = "[hidden]" if self.private else session.id[:18] + "…"
        title = sanitize_terminal_text(title)
        path = sanitize_terminal_text(path)
        session_id = sanitize_terminal_text(session_id)
        instance_tone = "#f2b84b" if session.instance_count > 1 else "#5eead4"
        instance_label = (
            f"[bold {instance_tone}]{session.instance_count} open[/]"
            if session.instance_count
            else "[dim]None detected[/]"
        )
        terminals = "[hidden]" if self.private else ", ".join(session.terminals)
        terminal_block = ""
        if session.terminals:
            terminal_block = (
                f"\n[dim]TERMINAL[/]\n[#86b7ff]{escape(sanitize_terminal_text(terminals))}[/]\n\n"
            )
        display_status = self._agent_display_state(session)
        signal_session = self._agent_attention_source(session)
        request_block = ""
        if display_status == "permission":
            if self.private:
                request_block = "[dim]PENDING PERMISSION[/]\n[hidden]\n\n"
            else:
                resources = signal_session.permission_resources
                resource_lines = "".join(
                    f"  {index}. {escape(sanitize_terminal_text(resource))}\n"
                    for index, resource in enumerate(resources, start=1)
                )
                request_block = (
                    "[dim]PENDING PERMISSION[/]\n"
                    f"[#ff6b7a]{escape(sanitize_terminal_text(signal_session.permission))}[/]\n"
                    + (f"[dim]ALL RESOURCES[/]\n{resource_lines}" if resources else "")
                    + "\n"
                )
        elif display_status == "question":
            request = "[hidden]" if self.private else signal_session.question
            request_block = (
                "[dim]PENDING QUESTION[/]\n"
                f"[#d4a6ff]{escape(sanitize_terminal_text(request))}[/]\n\n"
            )
        if session.instance_count:
            hint = (
                "Press [bold]o[/] to attach · [bold]x[/] to stop the tmux job."
            )
        else:
            hint = "Press [bold]o[/] to start its terminal."
        hint += " Click its name to rename it."
        background_jobs_block = ""
        if session.background_jobs:
            if self.private:
                background_jobs_block = "[dim]BACKGROUND JOBS[/]\n[hidden]\n\n"
            else:
                jobs_text = "\n".join(
                    f"  {escape(sanitize_terminal_text(job))}"
                    for job in session.background_jobs
                )
                background_jobs_block = (
                    "[dim]BACKGROUND JOBS[/]\n"
                    f"[#86b7ff]{jobs_text}[/]\n\n"
                )
        browser_block = (
            "[dim]BROWSER[/]\n[#5eead4]Signed-in Chrome · user-selected model[/]\n\n"
            if session.browser_enabled else ""
        )
        detail.update(
            f"[bold #e7f5fc]{escape(title)}[/]\n\n"
            f"[dim]STATE[/]\n{status_markup(display_status, self.activity_frame)}  "
            f"{AGENT_STATE_LABEL.get(display_status, display_status.upper())} · "
            f"{self._agent_state_age(session)}\n\n"
            f"[dim]TERMINAL INSTANCES[/]\n{instance_label}\n\n"
            f"{terminal_block}"
            f"{request_block}"
            f"{background_jobs_block}"
            f"{self._runtime_block(session)}"
            f"[dim]PROJECT[/]\n[#86b7ff]{escape(path)}[/]{self._git_suffix(session)}\n\n"
            f"{browser_block}"
            f"[dim]SESSION ID[/]\n[#7890a2]{escape(session_id)}[/]\n\n"
            f"[dim]UPDATED[/]\n{relative_time(session.updated_ms)} ago\n\n"
            f"[dim]{hint}[/]"
        )

    @on(Input.Changed, "#project-search")
    def on_project_search_changed(self, event: Input.Changed) -> None:
        self.project_search_term = event.value.strip()
        self._render_projects()

    @on(Input.Submitted, "#project-search")
    def on_project_search_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._select_first_project_result(focus_sessions=True)

    @on(Input.Submitted, "#project-register")
    def on_project_register_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._register_project(event.value)

    @on(Button.Pressed, "#new-session")
    def on_new_session_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_new_session_choose()

    @on(Button.Pressed, "#new-browser-session")
    def on_new_browser_session_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_new_browser_session()

    @on(Button.Pressed, "#enable-browser")
    def on_enable_browser_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_enable_browser()

    @on(Button.Pressed, "#add-project")
    def on_add_project_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._browse_project_worker()

    @on(Input.Changed, "#session-search")
    def on_search_changed(self, event: Input.Changed) -> None:
        self.search_term = event.value.strip()
        self._render_sessions()
        self._render_detail()

    @on(Button.Pressed, "#mobile-live")
    @on(Button.Pressed, "#mobile-all")
    def on_mobile_scope_pressed(self, event: Button.Pressed) -> None:
        self.mobile_all_sessions = event.button.id == "mobile-all"
        self.query_one("#mobile-live", Button).set_class(not self.mobile_all_sessions, "selected")
        self.query_one("#mobile-all", Button).set_class(self.mobile_all_sessions, "selected")
        self._render_sessions()

    @on(Button.Pressed, "#mobile-previous")
    @on(Button.Pressed, "#mobile-next")
    @on(Button.Pressed, "#mobile-open")
    def on_mobile_navigation_pressed(self, event: Button.Pressed) -> None:
        table = self.query_one("#sessions-table", DataTable)
        if not table.row_count:
            return
        table.focus()
        if event.button.id == "mobile-open":
            self.action_open_session()
        elif event.button.id == "mobile-next":
            table.action_cursor_down()
        else:
            table.action_cursor_up()

    @on(Input.Submitted, "#session-search")
    def on_search_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._open_from_search()

    @on(Checkbox.Changed, "#include-agent-sessions")
    def on_include_agent_sessions_changed(self, event: Checkbox.Changed) -> None:
        self.show_agent_sessions = event.value
        self._render_metrics()
        self._render_projects()
        self._render_sessions()
        self._render_detail()

    def action_toggle_agent_sessions(self) -> None:
        self.query_one("#include-agent-sessions", Checkbox).toggle()

    @on(Button.Pressed, "#clear-session-filters")
    def action_clear_filters(self) -> None:
        self.project_filter = False
        self.project_search_term = ""
        self.search_term = ""
        self.query_one("#project-search", Input).value = ""
        self.query_one("#session-search", Input).value = ""
        self._render_projects()
        self._render_sessions()
        self._render_detail()
        self.query_one("#sessions-table", DataTable).focus()

    def on_key(self, event: Key) -> None:
        focused = self.screen.focused
        focused_id = getattr(focused, "id", "") if focused is not None else ""
        if focused_id not in {"project-search", "project-register", "session-search"}:
            return
        if event.key == "down":
            event.stop()
            event.prevent_default()
            if focused_id == "project-search":
                self._select_first_project_result(focus_sessions=False)
            elif focused_id == "project-register":
                event.stop()
                event.prevent_default()
            else:
                self._focus_search_results()

    def _select_first_project_result(self, *, focus_sessions: bool) -> None:
        table = self.query_one("#projects-table", DataTable)
        if table.row_count == 0:
            self.notify("No matching projects", severity="warning")
            return
        table.move_cursor(row=0)
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        self.selected_project_id = str(row_key.value)
        self.project_filter = True
        self._render_sessions()
        self._render_detail()
        self._render_next()
        if focus_sessions:
            self.query_one("#sessions-table", DataTable).focus()
        else:
            table.focus()

    def _focus_search_results(self) -> None:
        table = self.query_one("#sessions-table", DataTable)
        if table.row_count == 0:
            return
        table.focus()
        table.move_cursor(row=0)

    def _open_from_search(self) -> None:
        table = self.query_one("#sessions-table", DataTable)
        if table.row_count == 0:
            self.notify("No matching sessions", severity="warning")
            return
        table.focus()
        table.move_cursor(row=0)
        self.action_open_session()

    @on(Input.Submitted, "#session-rename")
    def on_rename_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit_rename()

    def action_cancel_session_rename(self) -> None:
        self._close_rename_editor()

    def _begin_rename(self, session_id: str) -> None:
        session = self.session_by_id.get(session_id)
        if session is None:
            return
        if self._guard_next_read_only():
            return
        if self._opencode_only(session, "Renaming"):
            return
        if self.private:
            self.notify(
                "Exit privacy mode to rename sessions",
                severity="warning",
                timeout=4,
            )
            return
        self.renaming_session_id = session.id
        try:
            search = self.query_one("#session-search", Input)
            editor = self.query_one("#session-rename", RenameInput)
        except NoMatches:
            return
        search.display = False
        editor.display = True
        editor.value = session.title
        editor.focus()

    def _close_rename_editor(self) -> None:
        if not self.renaming_session_id:
            return
        self.renaming_session_id = ""
        try:
            editor = self.query_one("#session-rename", RenameInput)
            editor.display = False
            editor.value = ""
            self.query_one("#session-search", Input).display = True
            table = self.query_one("#sessions-table", SessionsTable)
        except NoMatches:
            return
        table.focus()

    def _submit_rename(self) -> None:
        session_id = self.renaming_session_id
        try:
            new_title = sanitize_terminal_text(
                self.query_one("#session-rename", RenameInput).value.strip()
            )
        except NoMatches:
            return
        self._close_rename_editor()
        session = self.session_by_id.get(session_id)
        if (
            session is None
            or not new_title
            or new_title == sanitize_terminal_text(session.title)
        ):
            return
        self._rename_worker(session.id, new_title)

    def _apply_local_title(self, session_id: str, title: str) -> None:
        sessions = tuple(
            replace(session, title=title) if session.id == session_id else session
            for session in self.snapshot.sessions
        )
        self.snapshot = replace(self.snapshot, sessions=sessions)
        self.session_by_id = {session.id: session for session in sessions}
        self._render_sessions()
        self._render_agents()
        self._render_detail()

    @work(group="rename", exit_on_error=False)
    async def _rename_worker(self, session_id: str, title: str) -> None:
        error = await self.source.rename_session(session_id, title)
        if error:
            self.notify(f"Rename failed: {error}", severity="error", timeout=8)
        else:
            self._apply_local_title(session_id, title)
        self._request_refresh(force=True)

    @on(DataTable.RowHighlighted)
    def on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None:
            return
        table = event.data_table
        key = str(event.row_key.value)
        if (
            table.row_count
            and table.is_valid_coordinate(table.cursor_coordinate)
            and str(
                table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            )
            != key
        ):
            # Echo queued by a programmatic rebuild whose cursor was restored
            # elsewhere before messages drained; real navigation always
            # highlights the row the cursor actually sits on.
            return
        if table.id in {"sessions-table", "agents-table"}:
            self.selected_session_id = key
            self._render_detail()
            if table.id == "agents-table":
                self._render_agent_focus()
        elif table.id == "projects-table":
            changed = self.selected_project_id != key
            self.selected_project_id = key
            if table.has_focus and not self.project_filter:
                self.project_filter = True
                changed = True
            if changed:
                self._render_sessions()
                self._render_detail()
                self._render_next()
        elif table.id == "alarms-table":
            self.selected_alarm_id = key
            self._render_alarm_detail()

    @on(DataTable.RowSelected)
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id in {"sessions-table", "agents-table"}:
            if isinstance(event.data_table, AgentsTable) and self.inline_tmux:
                click_serial = event.data_table.click_serial
                if click_serial and click_serial == self._last_mobile_agent_click_serial:
                    return
                self._last_mobile_agent_click_serial = click_serial
            if (
                isinstance(event.data_table, AgentsTable)
                and event.row_key is not None
                and event.data_table.clicked_expand_control
                and self.agent_children_by_id.get(str(event.row_key.value))
            ):
                event.data_table.clicked_column_index = None
                self.selected_session_id = str(event.row_key.value)
                self.action_toggle_subagents()
                return
            if (
                isinstance(event.data_table, SessionsTable)
                and event.row_key is not None
                and not self.inline_tmux
                and event.data_table.clicked_column_index
                == self.session_title_index
            ):
                event.data_table.clicked_column_index = None
                self.selected_session_id = str(event.row_key.value)
                self._begin_rename(str(event.row_key.value))
                return
            # Desktop clicks select; Enter/o are the explicit open actions.
            # Inline/mobile mode keeps title-click as its direct attach gesture.
            if isinstance(event.data_table, SessionsTable) and self.inline_tmux:
                self.action_open_session()
            elif isinstance(event.data_table, AgentsTable) and self.inline_tmux:
                self.action_open_session()
            return
        elif event.data_table.id == "projects-table":
            if not self.project_filter:
                self.project_filter = True
                self._render_sessions()
                self._render_detail()

    @on(Resize)
    def on_resize(self, event: Resize) -> None:
        self.screen.set_class(event.size.width < 144, "compact")
        self.screen.set_class(event.size.width < 80, "narrow")
        self.screen.set_class(event.size.height < 25, "short")
        self.screen.set_class(event.size.height < 19, "tiny")
        self._resize_session_columns(event.size.width)
        self._resize_agents_columns(event.size.width)
        self._render_named_agents()
        self._render_next()

    def _resize_session_columns(self, screen_width: int) -> None:
        table = self.query_one("#sessions-table", DataTable)
        if self.inline_tmux:
            if self.session_title_column is not None:
                table.columns[self.session_title_column].width = max(12, screen_width - 6)
                self._render_sessions()
                table.refresh(layout=True)
            return
        if screen_width < 60:
            title_width, project_width = 13, 7
        elif screen_width < 90:
            title_width, project_width = 22, 10
        else:
            title_width, project_width = 28, 14
        if self.session_title_column is None or self.session_project_column is None:
            return
        table.columns[self.session_title_column].width = title_width
        table.columns[self.session_project_column].width = project_width
        table.refresh(layout=True)

    def _resize_agents_columns(self, screen_width: int) -> None:
        try:
            table = self.query_one("#agents-table", DataTable)
        except NoMatches:
            return
        if not table.columns:
            return  # Resize may arrive before on_mount configures the tables.
        # Every column through RUNTIME fits (agents_layout.column_widths); DETAIL
        # hides first and the focus strip always shows the complete text.
        layout = column_widths(screen_width)
        if layout == self.agent_widths:
            return
        selected_id = self._selected_row_id(table, self.selected_session_id)
        scroll_y = table.scroll_y
        self.agent_widths = layout
        self.agent_runtime_full = layout.full_runtime
        # Textual columns have no display flag. Rebuild explicit-width columns
        # so hidden columns consume no padding and auto-sizing cannot undo the fit.
        with table.prevent(DataTable.RowHighlighted):
            table.clear(columns=True)
            for name, width in zip(COLUMNS, layout.as_tuple()):
                if width:
                    table.add_column(name, key=name, width=width)
            self._render_agents(selection_id=selected_id)
            table.scroll_to(x=0, y=scroll_y, animate=False, immediate=True)

    def action_search(self) -> None:
        focused = self.screen.focused
        focused_id = getattr(focused, "id", "") if focused is not None else ""
        search_id = (
            "#project-search"
            if focused_id in {"project-search", "projects-table"}
            else "#session-search"
        )
        search = self.query_one(search_id, Input)
        self.requested_tab_id = "overview"
        self.query_one("#tabs", TabbedContent).active = "overview"
        search.focus()

    def action_clear_search(self) -> None:
        focused = self.screen.focused
        focused_id = getattr(focused, "id", "") if focused is not None else ""
        project_search = self.query_one("#project-search", Input)
        session_search = self.query_one("#session-search", Input)
        if focused_id in {"project-search", "projects-table"}:
            if project_search.value:
                project_search.value = ""
            elif self.project_filter:
                self.action_toggle_filter()
            else:
                self.set_focus(self.query_one("#projects-table", DataTable))
            return
        if focused_id in {"session-search", "sessions-table"}:
            if session_search.value:
                session_search.value = ""
                return
        elif project_search.value:
            project_search.value = ""
            return
        elif session_search.value:
            session_search.value = ""
            return
        if self.project_filter:
            self.action_toggle_filter()
            return
        self.set_focus(self.query_one("#sessions-table", DataTable))

    def action_privacy(self) -> None:
        enabling = not self.private
        if enabling:
            self._close_rename_editor()
            self.project_search_term = ""
            self.search_term = ""
            for selector in (
                "#project-search",
                "#session-search",
                "#project-register",
                "#session-rename",
            ):
                try:
                    self.query_one(selector, Input).value = ""
                except NoMatches:
                    pass
            focused_id = getattr(self.screen.focused, "id", "")
            if focused_id in {
                "project-search",
                "session-search",
                "project-register",
                "session-rename",
            }:
                self.query_one("#sessions-table", DataTable).focus()
        self.private = enabling
        self._render_projects()
        self._render_sessions()
        self._render_attention()
        self._render_agents()
        self._render_detail()
        self._render_next()
        self.notify("Privacy mode on" if self.private else "Privacy mode off")
        self._render_alarms()
        self._refresh_tmux_headers()

    def action_show_tab(self, tab_id: str) -> None:
        self.requested_tab_id = tab_id
        self.set_focus(None)
        self.query_one("#tabs", TabbedContent).active = tab_id
        if tab_id == "next":
            self._render_next()
        self.call_after_refresh(self._focus_default_for_tab, tab_id)
        self.set_timer(0.05, lambda: self._focus_default_for_tab(tab_id))

    def action_cycle_view(self, direction: int) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        view_ids = ("overview", "services", "keys-view", "agents", "next", "alarms")
        current = view_ids.index(tabs.active) if tabs.active in view_ids else 0
        self.action_show_tab(view_ids[(current + direction) % len(view_ids)])

    def action_select_next_project(self, direction: int) -> None:
        projects = self.snapshot.projects
        if not projects:
            return
        project_ids = [project.id for project in projects]
        try:
            current = project_ids.index(self.selected_project_id)
        except ValueError:
            current = 0
        self.selected_project_id = project_ids[
            (current + direction) % len(project_ids)
        ]
        try:
            table = self.query_one("#projects-table", DataTable)
        except NoMatches:
            pass
        else:
            self._restore_table_cursor(table, self.selected_project_id)
        self._render_next()
        try:
            self.query_one("#next-view", NextStepsView).scroll_home(
                animate=False, immediate=True
            )
        except NoMatches:
            pass

    def _focus_initial_table(self) -> None:
        if self.screen.focused is None or isinstance(self.screen.focused, Tabs):
            active = self.query_one("#tabs", TabbedContent).active or "overview"
            self._focus_default_for_tab(active)

    def _focus_default_for_tab(self, tab_id: str) -> None:
        if self.requested_tab_id != tab_id:
            return
        tabs = self.query_one("#tabs", TabbedContent)
        if tabs.active != tab_id:
            tabs.active = tab_id
        if tab_id == "overview":
            sessions = self.query_one("#sessions-table", DataTable)
            projects = self.query_one("#projects-table", DataTable)
            target = sessions if sessions.row_count or not projects.row_count else projects
        elif tab_id == "services":
            target = self.query_one("#services-table", DataTable)
        elif tab_id == "agents":
            target = self.query_one("#agents-table", DataTable)
        elif tab_id == "alarms":
            target = self.query_one("#alarms-table", DataTable)
        elif tab_id == "next":
            target = self.query_one("#next-view", NextStepsView)
        else:
            target = self.query_one("#key-reference", KeyReference)
        target.focus()

    def action_focus_adjacent_table(self, direction: int) -> None:
        tables = [
            self.query_one("#projects-table", NavigationTable),
            self.query_one("#sessions-table", NavigationTable),
        ]
        tables = [table for table in tables if table in self.screen.focus_chain]
        focused = self.screen.focused
        if focused not in tables:
            return
        target_index = tables.index(focused) + direction
        if 0 <= target_index < len(tables):
            tables[target_index].focus()

    def action_toggle_filter(self) -> None:
        self.project_filter = not self.project_filter
        self._render_sessions()
        self._render_detail()
        state = "scoped to selected project" if self.project_filter else "all projects"
        self.notify(f"Session list: {state}")

    def _session_directory(self, session: SessionRecord) -> str:
        project = self.project_by_id.get(session.project_id)
        if project and Path(project.directory).is_dir():
            try:
                Path(session.directory).relative_to(project.directory)
            except ValueError:
                return project.directory
        if Path(session.directory).is_dir():
            return session.directory
        if project and Path(project.directory).is_dir():
            return project.directory
        return session.directory

    def _guard_next_read_only(self) -> bool:
        try:
            active = self.query_one("#tabs", TabbedContent).active
        except NoMatches:
            return False
        if active not in {"next", "alarms"}:
            return False
        self.notify(f"{active.upper()} is read-only", severity="warning", timeout=3)
        return True

    def action_open_session(self) -> None:
        if self._guard_next_read_only():
            return
        selected = self._current_session()
        if not selected:
            self.notify("Select a session first", severity="warning")
            return
        session = self._mobile_input_session(selected) if self.inline_tmux else selected
        title_override = selected.title if session.id != selected.id else None
        self._open_existing_session(session, title_override)

    def _open_existing_session(
        self, session: SessionRecord, title_override: str | None = None, *, auto: bool = False
    ) -> bool:
        """Single routing path for normal opens and history restoration."""
        if is_transcript_subagent(session):
            # A helper agent has no terminal or resumable session of its own:
            # open the session that spawned it.
            parent = self.session_by_id.get(session.parent_id)
            if parent is None:
                self.notify("This helper agent's parent session is not listed", severity="warning")
                return False
            return self._open_existing_session(parent, title_override, auto=auto)
        # Relaunching a closed session makes it the owner's from now on. Only
        # viewing a live one (e.g. peeking at an agent's work) changes nothing.
        if session.instance_count <= 0:
            self._record_owner_opened_session(session.id)
        if session_harness(session) != "opencode":
            return self._open_harness_session(session, title_override)
        # Browser sessions on BOTH backends take the browser path, which owns
        # the per-backend launch details. --auto there auto-approves actions in
        # the owner's signed-in browser tabs (council P0b G2, risk R-G2), so it
        # is only passed after an explicit warning and a second press (owner
        # decision, 2026-09-29; V2 only).
        browser_session = (
            session.browser_enabled or session.id in self._browser_terminal_ids
        )
        if browser_session:
            if auto:
                if getattr(self, "browser_auto_confirm", "") != session.id:
                    self.browser_auto_confirm = session.id
                    self.notify(
                        "Auto mode auto-approves actions in your signed-in browser tabs. "
                        "Press a again to confirm",
                        severity="warning",
                        timeout=8,
                    )
                    self.set_timer(8, self._clear_browser_auto_confirm)
                    return False
                self._clear_browser_auto_confirm()
                if session.instance_count > 0:
                    relaunched = self._reopen_with_auto(session, title_override, confirmed=True)
                    if relaunched is not None:
                        return relaunched
            browser_terminal = f"oc-browser-{session.id}"
            if session.instance_count > 0 and session.terminals:
                # Focus or reattach the terminal that already runs this session.
                # A browser tmux session whose pane belongs to something else
                # (for example a stale viewer) must never be trusted blindly.
                preferred = (
                    browser_terminal
                    if browser_terminal in session.terminals
                    else session.terminals[0]
                )
                attached = (
                    self._attach_live_terminal(session, title_override, preferred)
                    if title_override is not None
                    else self._attach_live_terminal(session, tmux_name=preferred)
                )
                if attached:
                    return True
            return self._run_browser_session(Path(session.directory), session.id,
                                             session.project_id, title_override or session.title,
                                             auto=auto)
        if auto and session.instance_count > 0:
            relaunched = self._reopen_with_auto(session, title_override)
            if relaunched is not None:
                return relaunched
        attached = (
            self._attach_live_terminal(session, title_override)
            if title_override is not None
            else self._attach_live_terminal(session)
        )
        if attached:
            return True
        if session.instance_count > 0:
            self.notify("This session is already running; its terminal could not be attached", severity="warning")
            return False
        arguments = [self._session_directory(session), "--session", session.id]
        if auto:
            arguments.append("--auto")
        return self._run_opencode(
            arguments,
            tmux_name=self._session_tmux_name(session),
            project_id=session.project_id,
            title=title_override or session.title,
        )

    def _reopen_with_auto(
        self, session: SessionRecord, title_override: str | None, *, confirmed: bool = False
    ) -> bool | None:
        """a on a running OpenCode session that lacks --auto: reopen it with --auto.

        Only OC Deck's own terminal for the session is replaced, after a second
        press. Returns None when this does not apply (already auto, or not in
        OC Deck's terminal), so the normal focus/attach path runs instead.
        """
        name = self._session_tmux_name(session)
        if name not in session.terminals or not self._tmux_has_session(name):
            return None
        backend = getattr(self.source, "backend", "v1")
        processes = [process for process in read_opencode_processes(backend=backend)
                     if process.session_id == session.id]
        if not processes or any(opencode_process_has_auto(process.pid) for process in processes):
            return None
        if not confirmed and self.auto_confirm != session.id:
            self.auto_confirm = session.id
            self.notify(
                "Press a again to reopen this session with auto-approve (--auto)"
                + ("; the agent keeps running in the OpenCode service" if backend == "v2"
                   else "; restarting its terminal stops a running turn"),
                timeout=6,
            )
            self.set_timer(6, self._clear_auto_confirm)
            return False
        self._clear_auto_confirm()
        if not self._tmux_kill_session(name):
            self.notify(f"Could not close {sanitize_terminal_text(name)} to reopen it with --auto",
                        severity="error")
            return False
        return self._run_opencode(
            [self._session_directory(session), "--session", session.id, "--auto"],
            tmux_name=name,
            project_id=session.project_id,
            title=title_override or session.title,
        )

    def action_pin_viewer_slot(self) -> None:
        """New session windows open at this session window's position and size."""
        if self._guard_next_read_only():
            return
        session = self._current_session()
        if not session:
            self.notify("Select a session first", severity="warning")
            return
        names = [name for name in session.terminals if self._tmux_has_session(name)]
        if not names:
            self.notify("Open this session's window first, then press Shift+P", severity="warning")
            return
        for name in names:
            try:
                result = subprocess.run(
                    ["gdbus", "call", "--session", "--dest", "org.local.OCDeckPlacement",
                     "--object-path", "/org/local/OCDeckPlacement",
                     "--method", "org.local.OCDeckPlacement.SetAgentReference", name],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode == 0 and "true" in result.stdout.decode(errors="replace").casefold():
                self.notify(
                    "New session windows will open at this window's position and size",
                    timeout=5,
                )
                return
        self.notify("Could not find this session's window to pin", severity="warning", timeout=6)

    def _clear_auto_confirm(self) -> None:
        self.auto_confirm = ""

    def _clear_browser_auto_confirm(self) -> None:
        self.browser_auto_confirm = ""

    # --- multi-harness support ------------------------------------------------
    def _render_agent_focus(self) -> None:
        """Untruncated text for the highlighted Agents row."""
        try:
            strip = self.query_one("#agent-focus", Static)
            table = self.query_one("#agents-table", DataTable)
        except NoMatches:
            return
        session = None
        if table.row_count and table.is_valid_coordinate(table.cursor_coordinate):
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            session = self.session_by_id.get(str(key))
        if session is None:
            strip.update("")
            return
        if self.private:
            strip.update("[dim]Privacy mode: session details hidden[/]")
            return
        harness = session_harness(session)
        state = self._agent_display_state(session)
        if session in self._closed_agent_sessions() or session.id in self.agent_closed_row_ids:
            state = "closed"
        signal = self._agent_attention_source(session)
        if state == "permission":
            detail = signal.permission or "Permission required"
        elif state == "question":
            detail = signal.question or "Input required"
        elif state == "job":
            jobs = signal.background_jobs or session.background_jobs
            detail = "; ".join(jobs) if jobs else "running"
        else:
            detail = session.last_prompt or "—"
        project = self.project_by_id.get(session.project_id)
        place = project.name if project else self._session_project_name(session)

        terminals = ", ".join(session.terminals) or ("direct terminal" if session.instance_count else "none")
        kind = term_kind(attached=session.terminal_attached, tmux=bool(session.terminals),
                         live=session.instance_count > 0, closed=state == "closed")
        own_state = agent_state(session) if signal.id != session.id else state
        spelled = legend(own_state, term_cell(kind))
        clean = sanitize_terminal_text
        content = Text()
        content.append(clean(session.title or "Untitled session") + "\n", style="bold #e7f5fc")
        content.append(clean(runtime_label(harness, session.model, full=True)),
                       style=HARNESS_STYLE.get(harness, "#8ba4b5"))
        content.append("  ·  ", style="dim")
        content.append(AGENT_STATE_LABEL.get(own_state, own_state.upper()))
        content.append(f" ({clean(spelled)})", style="dim")
        if signal.id != session.id:
            content.append(f"  · helper {AGENT_STATE_LABEL[state]}: ", style="dim")
            content.append(clean(signal.title))
            content.append(f" ({clean(runtime_label(session_harness(signal), signal.model, full=True))})", style="dim")
        content.append("  ·  ", style="dim")
        content.append(clean(place) + " ")
        content.append(clean(compact_path(session.directory, False)), style="dim")
        content.append("  · terminal: ", style="dim")
        content.append(clean(terminals) + "\n")
        content.append(
            "REQUEST " if state in {"permission", "question"}
            else "BACKGROUND JOB " if state == "job"
            else "LAST PROMPT ",
            style="dim",
        )
        content.append(clean(detail))
        strip.update(content)

    def _runtime_block(self, session: SessionRecord) -> str:
        if self.private:
            return "[dim]RUNTIME[/]\n[hidden]\n\n"
        harness = session_harness(session)
        model = model_name(session.model) or "model not reported"
        label = HARNESS_LABELS.get(harness, harness)
        return (
            f"[dim]RUNTIME[/]\n[{HARNESS_STYLE.get(harness, '#8ba4b5')}]"
            f"{escape(sanitize_terminal_text(label))} · {escape(sanitize_terminal_text(model))}[/]\n\n"
        )

    def _git_suffix(self, session: SessionRecord) -> str:
        project = self.project_by_id.get(session.project_id)
        branch = getattr(project, "git_branch", "") if project else ""
        if not branch or self.private:
            return ""
        dirty = getattr(project, "git_dirty", -1)
        state = f" · {dirty} changed" if dirty > 0 else " · clean" if dirty == 0 else ""
        return f"\n[dim]⎇ {escape(sanitize_terminal_text(branch))}{state}[/]"

    def _runtime_text(self, session: SessionRecord) -> Text:
        if self.private:
            return Text("hidden", style="dim")
        harness = session_harness(session)
        label = runtime_label(harness, session.model, full=self.agent_runtime_full)
        return Text(clip_text(label, 30), style=HARNESS_STYLE.get(harness, "#8ba4b5"))

    def _enabled_harnesses(self) -> tuple[str, ...]:
        return tuple(getattr(self.source, "enabled_harnesses", ("opencode",)))

    def _launch_harness(self) -> str:
        return getattr(self.source, "launch_harness", "opencode")

    def _session_launch_project(self, session: SessionRecord) -> ProjectRecord:
        project = self.project_by_id.get(session.project_id)
        if project is None or project.id == SCRATCH_PROJECT_ID:
            # Scratch is a display bucket, not a shared working directory.
            return ProjectRecord(id=session.project_id, directory=session.directory,
                                 name=Path(session.directory).name or session.directory)
        return project

    def _selected_launch_project(self) -> ProjectRecord | None:
        project = self.project_by_id.get(self.selected_project_id)
        if project is None or project.id != SCRATCH_PROJECT_ID:
            return project
        session = self._current_session()
        if session and session.project_id == project.id:
            return self._session_launch_project(session)
        return project

    def _harness_adapter(self, harness: str):
        adapter = getattr(self.source, "adapter", None)
        return adapter(harness) if callable(adapter) else None

    def _opencode_only(self, session: SessionRecord | None, action: str) -> bool:
        """Warn and return True when an OpenCode-only action targets another harness."""
        if session is None or session_harness(session) == "opencode":
            return False
        label = HARNESS_LABELS.get(session_harness(session), session_harness(session))
        self.notify(
            f"{action} is OpenCode-only; use the {label} terminal for this session",
            severity="warning",
            timeout=5,
        )
        return True

    def action_cycle_harness(self) -> None:
        enabled = self._enabled_harnesses()
        if not enabled:
            self.notify("No harness is enabled; see ~/.config/ocdeck/harnesses.json", severity="warning")
            return
        current = self._launch_harness()
        following = enabled[(enabled.index(current) + 1) % len(enabled)] if current in enabled else enabled[0]
        try:
            self.source.launch_harness = following
        except AttributeError:
            self.notify("This source cannot switch harnesses", severity="warning")
            return
        self.notify(f"New sessions launch with {HARNESS_LABELS[following]}", timeout=4)

    @on(Button.Pressed, "#choose-launch")
    def action_choose_launch(self) -> None:
        if self._guard_next_read_only():
            return
        enabled = self._enabled_harnesses()
        if not enabled:
            self.notify("No harness is enabled", severity="warning")
            return
        project = self.project_by_id.get(self.selected_project_id)
        session = self._current_session()
        # A session row identifies the project to continue. A focused project
        # row instead requests a new conversation in that project.
        projects = self.query_one("#projects-table", DataTable)
        if projects.has_focus:
            project_id = self._selected_row_id(projects, self.selected_project_id)
            project = self.project_by_id.get(project_id)
            session = None
        elif session is not None:
            project = self._session_launch_project(session)
        if project is None:
            self.notify("Select a project or session first", severity="warning")
            return
        self._choose_launch_worker(project, session, enabled)

    @work(group="launch-picker", exclusive=True, exit_on_error=False)
    async def _choose_launch_worker(
        self, project: ProjectRecord, session: SessionRecord | None, enabled: tuple[str, ...]
    ) -> None:
        known = tuple(agent.name for agent in self.snapshot.named_agents if agent.configured or agent.loaded)
        choices = await asyncio.to_thread(discover_agents, Path(project.directory), known)
        label = "Selected project" if self.private else project.name
        choice = await self.push_screen_wait(LaunchPicker(
            label, enabled, self._launch_harness(), choices, can_handoff=session is not None,
        ))
        if choice is None:
            return
        self._launch_project_choice(project, session, choice)

    def _launch_project_choice(
        self, project: ProjectRecord, session: SessionRecord | None, choice: LaunchChoice
    ) -> None:
        if self._guard_next_read_only():
            return
        if choice.harness not in self._enabled_harnesses():
            self.notify("The selected harness is no longer enabled", severity="warning")
            return
        try:
            extra = agent_arguments(choice.harness, choice.agent)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        if choice.handoff:
            if session is None or session.project_id != project.id:
                self.notify("Select a session in this project to continue", severity="warning")
                return
            self._handoff_worker(session, choice.harness, project, agent=choice.agent)
        elif choice.harness != "opencode":
            self._new_harness_session(choice.harness, project, agent=choice.agent)
        elif getattr(self.source, "backend", "v1") == "v2" and hasattr(self.source, "create_session"):
            directory = self._ensure_launch_directory(Path(project.directory).expanduser())
            if directory is not None:
                self._create_v2_session_worker(directory, project.id, project.name, agent=choice.agent)
        else:
            self._run_opencode([project.directory, *extra], tmux_name=f"oc-new-{time.time_ns()}",
                               project_id=project.id, title=project.name)

    def _open_harness_session(
        self, session: SessionRecord, title_override: str | None = None
    ) -> bool:
        harness = session_harness(session)
        label = HARNESS_LABELS.get(harness, harness)
        adapter = self._harness_adapter(harness)
        if adapter is None:
            self.notify(f"{label} is disabled in OC Deck", severity="warning")
            return False
        if session.terminals and self._attach_live_terminal(session, title_override):
            return True
        _, native = split_session_key(session.id)
        if session.instance_count > 0 and not self.inline_tmux:
            # Running directly in a terminal tab: focus the window hosting it.
            for pid in getattr(adapter, "live_pids", {}).get(native, ()):
                if self._focus_process_via_ptyxis(pid) is True:
                    self.notify(f"Focused the {label} session's window", timeout=3)
                    return True
        if session.instance_count > 0:
            # Never start a second process on a transcript that is being written.
            self.notify(
                f"This {label} session is already running outside tmux; switch to its window, "
                "or press x twice to stop it and o to reopen it here",
                severity="warning",
                timeout=6,
            )
            return False
        if not adapter.binary:
            self.notify(f"{label} executable not found", severity="error")
            return False
        pending_until = self._harness_launch_pending.get(session.id, 0.0)
        if time.monotonic() < pending_until or adapter.is_live(native):
            # The snapshot can be up to one refresh old; check the processes now.
            self.notify(f"This {label} session is already starting or running", severity="warning")
            return False
        directory = self._ensure_launch_directory(Path(self._session_directory(session)))
        if directory is None:
            return False
        self._harness_launch_pending[session.id] = time.monotonic() + 15
        accent, project_label = self._project_theme(session.project_id)
        # Window titles stay "OpenCode · …" for every agent so the GNOME placement
        # extension tiles all harnesses alike; the status bar names the harness.
        return self._launch_tmux(
            adapter.tmux_name(native),
            directory,
            adapter.resume_command(native, str(directory), browser=session.browser_enabled),
            accent=accent,
            label=project_label,
            title="" if self.private else title_override or session.title,
            harness=harness,
            model=session.model,
        )

    def action_handoff_session(self) -> None:
        """Continue the selected session in the harness chosen with Shift+H."""
        if self._guard_next_read_only():
            return
        session = self._current_session()
        if session is None:
            self.notify("Select a session first", severity="warning")
            return
        source_harness = session_harness(session)
        target = self._launch_harness()
        if target == source_harness:
            self.notify(
                f"Pick a different harness with Shift+H (currently {HARNESS_LABELS[target]})",
                severity="warning",
            )
            return
        project = self._session_launch_project(session)
        self.notify(f"Preparing handoff to {HARNESS_LABELS[target]}…", timeout=3)
        self._handoff_worker(session, target, project)

    @work(group="handoff", exclusive=True, exit_on_error=False)
    async def _handoff_worker(
        self, session: SessionRecord, target: str, project: ProjectRecord, *, agent: str = ""
    ) -> None:
        # Writing notes runs git and scans transcripts; keep it off the UI thread.
        try:
            from .hub import write_handoff

            path, prompt = await asyncio.to_thread(write_handoff, session, target)
        except Exception as error:  # the hub is optional; report and stay usable
            self.notify(f"Handoff failed: {type(error).__name__}: {error}", severity="error")
            return
        if target == "opencode":
            launched = self._run_opencode(
                [project.directory, *agent_arguments(target, agent), "--prompt", prompt],
                tmux_name=f"oc-handoff-{time.time_ns()}",
                project_id=project.id,
                title=f"handoff: {session.title}",
            )
        else:
            launched = self._new_harness_session(target, project, prompt, **({"agent": agent} if agent else {}))
        if launched:
            self.notify(f"Handed off to {HARNESS_LABELS[target]} · notes in {path}", timeout=8)

    def _toggle_harness_browser(self, session: SessionRecord) -> None:
        """Grant or revoke the dedicated agent browser for one Claude/Codex session."""
        if self._guard_next_read_only():
            return
        label = HARNESS_LABELS.get(session_harness(session), session_harness(session))
        if agent_browser_server() is None:
            self.notify("The agent browser is not installed", severity="error")
            return
        grants = load_browser_grants()
        granted = session.id not in grants
        if granted:
            grants.add(session.id)
        else:
            grants.discard(session.id)
        try:
            save_browser_grants(grants)
        except OSError as error:
            self.notify(f"Could not save the browser grant: {type(error).__name__}", severity="error")
            return
        action = "enabled" if granted else "revoked"
        when = (
            "takes effect when the session restarts (x, then o)"
            if session.instance_count > 0
            else "applies the next time it opens"
        )
        self.notify(f"Agent browser {action} for this {label} session; {when}", timeout=8)
        self._request_refresh(force=True)

    def _new_harness_session(
        self, harness: str, project: ProjectRecord, prompt: str = "", *, browser: bool = False,
        agent: str = "",
    ) -> bool:
        adapter = self._harness_adapter(harness)
        if adapter is None:
            self.notify(f"{HARNESS_LABELS.get(harness, harness)} is disabled in OC Deck", severity="warning")
            return False
        if not adapter.binary:
            self.notify(f"{HARNESS_LABELS[harness]} executable not found", severity="error")
            return False
        directory = self._ensure_launch_directory(Path(project.directory).expanduser())
        if directory is None:
            return False
        command, native = adapter.new_command(str(directory), prompt, browser=browser)
        command = [command[0], *agent_arguments(harness, agent), *command[1:]]
        if browser and not native:
            self.notify(
                f"{HARNESS_LABELS[harness]} starts with the agent browser; press Shift+B on the "
                "new session once it appears to keep the browser on future resumes",
                timeout=10,
            )
        if browser and native:
            grants = load_browser_grants()
            grants.add(adapter.session_key(native))
            try:
                save_browser_grants(grants)
            except OSError:
                self.notify("Browser grant could not be saved; it lasts for this launch only", severity="warning")
        name = adapter.tmux_name(native) if native else f"{adapter.tmux_prefix}-new-{time.time_ns()}"
        accent, label = self._project_theme(project.id)
        # Claude pre-assigns its session id; Codex picks one after launch, so
        # its terminal and directory carry the pending owner launch instead.
        if native:
            self._record_owner_opened_session(adapter.session_key(native))
        else:
            self._record_owner_opened_launch(name, str(directory))
        return self._launch_tmux(
            name, directory, command, accent=accent, label=label,
            title="" if self.private else project.name,
            harness=harness,
        )

    def _mobile_input_session(self, session: SessionRecord) -> SessionRecord:
        seen = {session.id}
        current = session
        while parent_id := current.parent_id or current.agent_parent_id:
            if parent_id in seen:
                break
            parent = self.session_by_id.get(parent_id)
            if parent is None:
                break
            current = parent
            seen.add(parent.id)
        return current

    def action_open_auto(self) -> None:
        if self._guard_next_read_only():
            return
        projects = self.query_one("#projects-table", DataTable)
        if projects.has_focus:
            if not projects.row_count:
                self.notify("Select a project first", severity="warning")
                return
            self.selected_project_id = self._selected_row_id(
                projects, self.selected_project_id
            )
            self.action_new_session(auto=True)
            return
        session = self._current_session()
        if not session:
            self.notify("Select a session first", severity="warning")
            return
        self._open_existing_session(session, auto=True)

    @on(Button.Pressed, "#relaunch-agents")
    def action_relaunch_previous_sessions(self) -> None:
        if self._guard_next_read_only():
            self._clear_relaunch_confirm()
            return
        tabs = self.query_one("#tabs", TabbedContent)
        if tabs.active != "agents":
            self._clear_relaunch_confirm()
            self.notify(
                "Open AGENTS (4) to relaunch previous sessions",
                severity="warning",
            )
            return
        if self.inline_tmux:
            self.notify("Select a closed row and press Enter to reopen one session", severity="warning")
            return
        if self.snapshot.connection != "live":
            self._clear_relaunch_confirm()
            self.notify("Refresh the connection before relaunching multiple sessions", severity="warning")
            return
        pending = self._closed_agent_sessions()
        if not pending:
            self._clear_relaunch_confirm()
            self.notify("No previous agent sessions to relaunch", severity="warning")
            return
        identities = tuple(session.id for session in pending)
        now = time.monotonic()
        if self.relaunch_confirm != identities or now >= self._relaunch_confirm_until:
            self.relaunch_confirm = identities
            self._relaunch_confirm_until = now + 6
            count = len(pending)
            self.notify(
                f"Press Shift+L again to relaunch {count} previous session"
                f"{'s' if count != 1 else ''}",
                timeout=6,
            )
            return
        self._clear_relaunch_confirm()
        launched = 0
        for session in pending:
            self._relaunch_pending[session.id] = time.monotonic() + 15
            if self._open_existing_session(session):
                launched += 1
            else:
                self._relaunch_pending.pop(session.id, None)
        if launched:
            self.notify(
                f"Relaunched {launched} previous session"
                f"{'s' if launched != 1 else ''}",
                timeout=6,
            )
        else:
            self.notify("No previous sessions could be relaunched", severity="warning")

    def _clear_relaunch_confirm(self) -> None:
        self.relaunch_confirm = ()
        self._relaunch_confirm_until = 0.0

    @on(Button.Pressed, "#purge-agent-tabs")
    def action_purge_agent_tabs(self) -> None:
        if self._guard_next_read_only():
            return
        if self.inline_tmux:
            self.notify(
                "Agent tabs can only be purged from a desktop OC Deck window",
                severity="warning",
            )
            return
        if self.snapshot.connection != "live":
            self.notify(
                "Refresh the connection before purging agent tabs",
                severity="warning",
            )
            return
        now = time.monotonic()
        if now >= self._purge_confirm_until:
            self._purge_confirm_until = now + 6
            self.notify(
                "Press z again to send every attached agent terminal to the "
                "background; tmux sessions keep running",
                timeout=6,
            )
            self.set_timer(6, self._clear_purge_confirm)
            return
        self._purge_confirm_until = 0.0
        self._purge_agent_tabs_worker()

    def _clear_purge_confirm(self) -> None:
        self._purge_confirm_until = 0.0

    @work(group="agent-tabs", exit_on_error=False)
    async def _purge_agent_tabs_worker(self) -> None:
        tabs = await asyncio.to_thread(attached_agent_tabs)
        if not tabs:
            self.notify("No attached agent terminal windows to purge", severity="warning")
            return
        report = await asyncio.to_thread(purge_attached_tabs, tabs)
        if report.windows_shared:
            self.notify(
                f"Kept {report.windows_shared} window"
                f"{'s' if report.windows_shared != 1 else ''} open because "
                f"{'they host' if report.windows_shared != 1 else 'it hosts'} other tabs; "
                "their agent tabs were detached",
                timeout=8,
            )
            if report.windows_closed <= 0 and report.windows_remaining == report.windows_shared:
                self._request_refresh(force=True)
                return
        if report.windows_closed <= 0:
            self.notify(
                "Could not close the attached agent terminal windows; sessions "
                "were left running",
                severity="error",
                timeout=10,
            )
            return
        if report.windows_remaining:
            self.notify(
                f"Closed {report.windows_closed} of {report.windows} agent tabs; "
                f"{report.windows_remaining} still open; {report.sessions_running} "
                f"session{'s' if report.sessions_running != 1 else ''} still running",
                severity="warning",
                timeout=10,
            )
        else:
            self.notify(
                f"Sent {report.windows_closed} agent tab"
                f"{'s' if report.windows_closed != 1 else ''} to the background; "
                f"{report.sessions_running} session"
                f"{'s' if report.sessions_running != 1 else ''} still running",
                timeout=8,
            )
        self._request_refresh(force=True)

    def action_approve_permission(self) -> None:
        if self._guard_next_read_only():
            return
        session = self._current_session()
        if not session:
            self.notify("Select a session first", severity="warning")
            return
        if self._opencode_only(session, "Permission approval"):
            return
        display_state = self._agent_display_state(session)
        permission_session = self._agent_attention_source(session)
        if display_state != "permission" or not permission_session.permission_id:
            self.notify("No pending permission for this session", severity="warning")
            return
        if not hasattr(self.source, "approve_permission"):
            self.notify("Permission approval is unavailable", severity="error")
            return
        reply_key = (permission_session.id, permission_session.permission_id)
        if (
            reply_key in self._permission_replies_in_flight
            or reply_key in self._confirmed_permission_replies
        ):
            self.notify("Permission reply already submitted", severity="warning")
            return
        self._permission_replies_in_flight.add(reply_key)
        self._approve_permission_worker(
            permission_session.id, permission_session.permission_id
        )

    def action_focus_permission(self) -> None:
        pending = next(
            (
                session
                for session in self.snapshot.sessions
                if agent_state(session) == "permission"
            ),
            None,
        )
        if pending is None:
            self.notify("No pending permission requests", severity="warning")
            return
        self.action_clear_filters()
        if pending.agent_session_kind:
            self.show_agent_sessions = True
            self.query_one("#include-agent-sessions", Checkbox).value = True
        self.selected_project_id = pending.project_id
        self.project_filter = False
        self.selected_session_id = pending.id
        self.action_show_tab("overview")
        self._render_projects()
        self._render_sessions()
        self._render_detail()
        table = self.query_one("#sessions-table", DataTable)
        if pending.id in table.rows:
            table.focus()
            table.move_cursor(row=list(table.rows).index(next(
                row for row in table.rows if str(row.value) == pending.id
            )))

    def action_add_project(self) -> None:
        if self._guard_next_read_only():
            return
        editor = self.query_one("#project-register", Input)
        editor.focus()

    @staticmethod
    def _choose_project_directory() -> str | None:
        try:
            result = subprocess.run(
                [
                    "zenity",
                    "--file-selection",
                    "--directory",
                    "--title=Select project directory",
                    f"--filename={Path.home()}/",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value or None

    @work(group="project-picker", exit_on_error=False)
    async def _browse_project_worker(self) -> None:
        directory = await asyncio.to_thread(self._choose_project_directory)
        if directory is None:
            return
        self.query_one("#project-register", Input).value = directory
        self._register_project(directory)

    def _register_project(self, raw_directory: str) -> None:
        value = raw_directory.strip()
        if not value:
            self.notify("Enter an absolute directory to register", severity="warning")
            return
        directory = Path(value).expanduser()
        if not directory.is_absolute():
            self.notify("Project directory must be absolute", severity="warning")
            return
        try:
            directory = directory.resolve(strict=True)
        except OSError:
            self.notify("Project directory does not exist", severity="warning")
            return
        if not directory.is_dir() or directory == directory.parent:
            self.notify("Project path must be a non-root directory", severity="warning")
            return
        existing = next(
            (
                project
                for project in self.snapshot.projects
                if project.registered
                and normalized_project_path(project.directory) == normalized_project_path(directory)
            ),
            None,
        )
        if existing is not None:
            self.query_one("#project-register", Input).value = ""
            self.notify(f"Already registered project {sanitize_terminal_text(existing.name)}")
            return
        self._register_project_worker(directory, directory.name)

    @staticmethod
    def _run_project_registration(controller: str, directory: Path) -> tuple[dict[str, object] | None, str]:
        try:
            result = subprocess.run(
                [controller, "register-project", directory.name, str(directory)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return None, str(error)
        output = result.stdout.strip()
        if result.returncode != 0:
            return None, result.stderr.strip() or output or "home-agentctl failed"
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            return None, "home-agentctl returned invalid JSON"
        if not isinstance(payload, dict) or not isinstance(payload.get("name"), str):
            return None, "home-agentctl returned an invalid registration result"
        return payload, ""

    @work(group="project-registration", exit_on_error=False)
    async def _register_project_worker(self, directory: Path, name: str) -> None:
        result, error = await asyncio.to_thread(
            self._run_project_registration, self.home_agentctl_bin, directory
        )
        if error:
            self.notify(
                f"Project registration failed: {sanitize_terminal_text(error)[:240]}",
                severity="error",
                timeout=10,
            )
            return
        assert result is not None
        self.query_one("#project-register", Input).value = ""
        changed = bool(result.get("catalogChanged") or result.get("registryChanged"))
        label = sanitize_terminal_text(str(result.get("name") or name))
        self.notify(
            f"{'Registered' if changed else 'Already registered'} project {label}",
            timeout=6,
        )
        self._request_refresh(force=True)

    @work(group="permission", exit_on_error=False)
    async def _approve_permission_worker(
        self, session_id: str, permission_id: str
    ) -> None:
        reply_key = (session_id, permission_id)
        try:
            error = await self.source.approve_permission(session_id, permission_id)
            if error:
                self.notify(f"Permission approval failed: {error}", severity="error")
                return
            self._confirmed_permission_replies.add(reply_key)
            self._apply_confirmed_permission(session_id, permission_id)
            self.notify("Permission approved once")
            self._request_activity_refresh()
        finally:
            self._permission_replies_in_flight.discard(reply_key)

    def _apply_confirmed_permission(
        self, session_id: str, permission_id: str
    ) -> None:
        session = self.session_by_id.get(session_id)
        if session is None or session.permission_id != permission_id:
            return
        sessions = tuple(
            replace(
                item,
                permission="",
                permission_id="",
                permission_resources=(),
            )
            if item.id == session_id
            else item
            for item in self.snapshot.sessions
        )
        self.snapshot = replace(self.snapshot, sessions=sessions)
        self.session_by_id = {item.id: item for item in sessions}
        self._render_attention()
        self._render_sessions()
        self._render_agents()
        self._render_detail()

    def action_stop_job(self) -> None:
        if self._guard_next_read_only():
            return
        session = self._current_session()
        if not session:
            self.notify("Select a session first", severity="warning")
            return
        # Only OC Deck-managed sessions may be killed: a CLI running inside the
        # user's own tmux session maps to that session's name too.
        candidates = [name for name in session.terminals if is_managed_session(name)]
        canonical = self._session_tmux_name(session)
        if canonical not in candidates:
            candidates.append(canonical)
        name = next((item for item in candidates if self._tmux_has_session(item)), "")
        if not name:
            backend = getattr(self.source, "backend", "v1")
            targets = direct_renderers(session.id, backend)
            if (not targets and self._served_by_v2(session.id)
                    and hasattr(self.source, "interrupt_session")):
                # No terminal to act in (e.g. a headless `opencode run` an agent
                # started): the turn runs in the service, so interrupt it there.
                if self.stop_confirm != session.id:
                    self.stop_confirm = session.id
                    self.notify(
                        "Press x again to interrupt this session's agent in the OpenCode "
                        "service (the session stays open)",
                        timeout=6,
                    )
                    self.set_timer(6, self._clear_stop_confirm)
                    return
                self._clear_stop_confirm()
                self._interrupt_service_worker(session.id)
                return
            harness, native = split_session_key(session.id)
            adapter = self._harness_adapter(harness) if harness != "opencode" else None
            pids = getattr(adapter, "stoppable_pids", {}).get(native, ()) if adapter else ()
            if not targets and pids:
                # A CLI outside OC Deck's tmux (e.g. an agent's `claude -p`, or
                # one started in a plain terminal): stop that exact process.
                label = HARNESS_LABELS.get(harness, harness)
                if self.stop_confirm != session.id:
                    processes = tuple(target for target in map(identify, pids) if target)
                    if not processes:
                        self.notify(f"This {label} session's process has already exited", timeout=4)
                        return
                    self.stop_confirm = session.id
                    self._process_stop_targets = processes
                    shown = ", ".join(str(target.pid) for target in processes)
                    self.notify(
                        f"Press x again to stop this {label} session (process {shown}); "
                        "the conversation is saved and o reopens it",
                        timeout=6,
                    )
                    self.set_timer(6, self._clear_stop_confirm)
                    return
                self._clear_stop_confirm()
                self._stop_process_worker(label, getattr(self, "_process_stop_targets", ()))
                return
            if not targets or session.terminals:
                self._clear_stop_confirm()
                self.notify("No live terminal is attached to this session", severity="warning")
                return
            if self.stop_confirm != session.id or getattr(self, "_direct_stop_targets", ()) != targets:
                self.stop_confirm = session.id
                self._direct_stop_targets = targets
                self.notify("Press x again to close this session's direct terminal", timeout=6)
                self.set_timer(6, self._clear_stop_confirm)
                return
            self._clear_stop_confirm()
            self._stop_direct_job_worker(targets)
            return
        v2 = self._served_by_v2(session.id)
        if self.stop_confirm != session.id:
            self.stop_confirm = session.id
            self.notify(
                f"Press x again to interrupt the agent in {sanitize_terminal_text(name)} "
                "(Esc twice, as in OpenCode; the session stays open)"
                if v2
                else f"Press x again to stop tmux job {sanitize_terminal_text(name)}",
                timeout=6,
            )
            self.set_timer(6, self._clear_stop_confirm)
            return
        self.stop_confirm = ""
        if v2:
            # A V2 turn runs in the OpenCode service: killing the terminal never
            # stopped it. Interrupt it the way the owner would, in its own pane.
            self._interrupt_v2_worker(name)
            return
        self._stop_job_worker(session.id, name)

    def _clear_stop_confirm(self) -> None:
        self.stop_confirm = ""
        self._direct_stop_targets = ()

    @work(group="job-control", exit_on_error=False)
    async def _stop_direct_job_worker(self, targets) -> None:
        closed, unresolved = await asyncio.to_thread(close_renderers, targets)
        if unresolved:
            self.notify(f"Closed {closed} direct terminal(s); {unresolved} could not be verified as closed", severity="warning")
        else:
            self.notify(f"Closed {closed} direct terminal(s); session history retained", timeout=4)
        self._request_refresh(force=True)

    def _tmux_kill_session(self, name: str) -> bool:
        try:
            result = subprocess.run(
                # Exact match (G5): a prefix target could kill an unrelated
                # session whose name merely starts with this one.
                ["tmux", "kill-session", "-t", f"={name}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    @work(group="job-control", exit_on_error=False)
    async def _stop_job_worker(self, session_id: str, name: str) -> None:
        stopped = await asyncio.to_thread(self._tmux_kill_session, name)
        if not stopped:
            self.notify(
                f"Could not stop tmux job {sanitize_terminal_text(name)}",
                severity="error",
            )
            return
        self.notify(f"Stopped tmux job {sanitize_terminal_text(name)}", timeout=4)
        if self.selected_session_id == session_id:
            self._render_detail()
        self._request_refresh(force=True)

    @work(group="job-control", exit_on_error=False)
    async def _interrupt_v2_worker(self, name: str) -> None:
        shown = sanitize_terminal_text(name)
        result = await asyncio.to_thread(interrupt_v2_turn, name)
        if result == INTERRUPTED:
            self.notify(f"Interrupted the agent in {shown}; the session stays open", timeout=5)
        elif result == IDLE:
            # Nothing to interrupt, so stopping means closing the terminal. The
            # service keeps the session and its history; o reopens it.
            if await asyncio.to_thread(self._tmux_kill_session, name):
                self.notify(f"Nothing was running; closed {shown} (o reopens it)", timeout=5)
            else:
                self.notify(f"Nothing is running in {shown}, and its terminal could not be closed",
                            severity="warning", timeout=6)
        elif result == NOT_OPENCODE:
            self.notify(f"{shown} is not showing OpenCode; no keys were sent", severity="warning", timeout=6)
        else:
            self.notify(
                f"Could not confirm the interrupt in {shown}; open it and press Esc twice",
                severity="error",
                timeout=8,
            )
        self._request_refresh(force=True)

    @work(group="job-control", exit_on_error=False)
    async def _interrupt_service_worker(self, session_id: str) -> None:
        interrupted, error = await self.source.interrupt_session(session_id)
        if error:
            self.notify(f"Could not interrupt the agent: {sanitize_terminal_text(error)}",
                        severity="error", timeout=8)
        elif interrupted:
            self.notify("Interrupted the agent in the OpenCode service; the session stays open", timeout=5)
        else:
            self.notify("Nothing is running in this session", timeout=4)
        self._request_refresh(force=True)

    @work(group="job-control", exit_on_error=False)
    async def _stop_process_worker(self, label: str, targets) -> None:
        stopped, unresolved = await asyncio.to_thread(stop_processes, tuple(targets))
        if unresolved or not stopped:
            self.notify(f"Could not verify the {label} process before stopping it; nothing was sent",
                        severity="warning", timeout=6)
        else:
            self.notify(f"Stopped the {label} session; press o to reopen it", timeout=5)
        self._request_refresh(force=True)

    def _served_by_v2(self, session_id: str) -> bool:
        harness, _ = split_session_key(session_id)
        return harness == "opencode" and getattr(self.source, "backend", "v1") == "v2"

    def _attach_live_terminal(
        self, session: SessionRecord, title_override: str | None = None, tmux_name: str = ""
    ) -> bool:
        if session.instance_count <= 0:
            return False
        if not session.terminals:
            return self._focus_direct_renderer(session)
        name = tmux_name if tmux_name in session.terminals else session.terminals[0]
        title = "" if self.private else title_override or session.title
        if self.inline_tmux:
            directory = Path(self._session_directory(session))
            if not self._tmux_attach(name, directory, title):
                return False
            self._request_refresh(force=True)
            return True
        focus_result = self._raise_existing_window(name, title)
        if focus_result is True:
            self.notify(f"Focused live terminal {name}", timeout=3)
            return True
        if focus_result is None or self._tmux_client_attached(name):
            # Never stack another viewer on a terminal that is already open.
            self.notify(
                f"Live terminal {name} exists, but exact window focus is unavailable",
                severity="warning",
                timeout=8,
            )
            return True
        directory = Path(self._session_directory(session))
        if not self._tmux_attach(name, directory, title):
            return False
        self.notify(f"Attached to live terminal {name}", timeout=3)
        self._request_refresh(force=True)
        return True

    def _focus_direct_renderer(self, session: SessionRecord) -> bool:
        """Focus the window hosting a renderer that runs outside tmux."""
        if self.inline_tmux:
            return False
        backend = getattr(self.source, "backend", "v1")
        try:
            pids = session_renderer_pids(session.id, backend=backend)
        except OSError:
            pids = ()
        for pid in pids:
            if self._focus_process_via_ptyxis(pid) is True:
                self.notify("Focused the session's live terminal window", timeout=3)
                return True
        return False

    def _focus_process_via_ptyxis(self, pid: int) -> bool | None:
        helper = Path(__file__).with_name("focus_helper.py")
        python = Path("/usr/bin/python3")
        if not helper.is_file() or not python.is_file():
            return None
        try:
            result = subprocess.run(
                [str(python), str(helper), "--pid", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode == 0:
            return True
        if result.returncode == 3:
            return False
        return None

    def _raise_existing_window(self, tmux_name: str, title: str = "") -> bool | None:
        shell_result = self._focus_tmux_via_shell(tmux_name)
        if shell_result is True:
            return True
        # A shell "not found" is not final: an older extension build does not
        # recognise exact "=name" attach targets, so still ask the Ptyxis helper.
        ptyxis_result = self._focus_tmux_via_ptyxis(tmux_name)
        if ptyxis_result is not None:
            return ptyxis_result
        if shell_result is False:
            return False
        if not os.environ.get("DISPLAY"):
            return None if os.environ.get("WAYLAND_DISPLAY") else False
        viewer_titles = [clip_text(title, 80)] if title else []
        viewer_titles.append(tmux_name)
        try:
            window_ids: list[str] = []
            for viewer_title in viewer_titles:
                search = subprocess.run(
                    [
                        "xdotool",
                        "search",
                        "--name",
                        re.escape(f"OpenCode · {viewer_title}"),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    check=False,
                )
                window_ids = search.stdout.decode().split()
                if window_ids:
                    break
            if not window_ids:
                return None if os.environ.get("WAYLAND_DISPLAY") else False
            subprocess.run(
                ["xdotool", "windowactivate", window_ids[-1]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None if os.environ.get("WAYLAND_DISPLAY") else False
        return True

    def _focus_tmux_via_shell(self, tmux_name: str) -> bool | None:
        try:
            result = subprocess.run(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.local.OCDeckSwitch",
                    "--object-path",
                    "/org/local/OCDeckSwitch",
                    "--method",
                    "org.local.OCDeckSwitch.FocusTmux",
                    tmux_name,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        output = result.stdout.decode("utf-8", errors="replace").casefold()
        if "true" in output:
            return True
        if "false" in output:
            return False
        return None

    def _focus_tmux_via_ptyxis(self, tmux_name: str) -> bool | None:
        helper = Path(__file__).with_name("focus_helper.py")
        python = Path("/usr/bin/python3")
        if not helper.is_file() or not python.is_file():
            return None
        try:
            result = subprocess.run(
                [str(python), str(helper), tmux_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode == 0:
            return True
        if result.returncode == 3:
            return False
        return None

    def _browser_action_available(self) -> bool:
        if self._guard_next_read_only():
            return False
        if not all(hasattr(self.source, name) for name in ("create_browser_session", "enable_session_browser")):
            self.notify("Browser access integration is unavailable", severity="error")
            return False
        return True

    def action_new_browser_session(self) -> None:
        if self._guard_next_read_only():
            return
        harness = self._launch_harness()
        if harness != "opencode":
            project = self.project_by_id.get(self.selected_project_id)
            if project is None:
                self.notify("Select a project first", severity="warning")
                return
            if agent_browser_server() is None:
                self.notify("The agent browser is not installed", severity="error")
                return
            self._new_harness_session(harness, project, browser=True)
            return
        if not self._browser_action_available():
            return
        project = self._selected_launch_project()
        if project is None:
            self.notify("Select a project first", severity="warning")
            return
        if not self.source.opencode_bin:
            self.notify("OpenCode executable not found", severity="error")
            return
        if not Path(project.directory).is_dir():
            self.notify("The registered project directory must be available", severity="warning")
            return
        self._start_browser_operation(project, None)

    def action_enable_browser(self) -> None:
        session = self._current_session()
        if session is not None and session_harness(session) != "opencode":
            self._toggle_harness_browser(session)
            return
        if not self._browser_action_available():
            return
        session = self._current_session()
        if session is None:
            self.notify("Select a session first", severity="warning")
            return
        if session.parent_id or session.agent_parent_id:
            self.notify("Select the primary session to enable browser access", severity="warning")
            return
        if session.status in {"busy", "retry"} or session.assistant_active or session.permission_id or session.question_id:
            self.notify("Wait for this session to become idle and resolve its pending requests", severity="warning")
            return
        project = self.project_by_id.get(session.project_id)
        if project is None:
            self.notify("Register this session's project first", severity="warning")
            return
        self._start_browser_operation(project, session.id)

    def _start_browser_operation(self, project: ProjectRecord, session_id: str | None) -> None:
        key = ("existing", session_id) if session_id is not None else ("new", project.id)
        if key in self._browser_operations:
            self.notify("Browser operation already in progress", severity="warning")
            return
        self._browser_operations.add(key)
        self.notify("Enabling signed-in browser access…", timeout=3)
        self._browser_access_worker(project, session_id, key)

    @work(group="browser-access", exit_on_error=False)
    async def _browser_access_worker(
        self, project: ProjectRecord, session_id: str | None, key: tuple[str, str]
    ) -> None:
        try:
            directory = Path(project.directory)
            result = (await self.source.enable_session_browser(directory, session_id)
                      if session_id is not None else await self.source.create_browser_session(directory))
            if result.error:
                detail = f" (session {result.session_id})" if result.session_id else ""
                self.notify(result.error + detail, severity="error", timeout=10)
                return
            self._browser_terminal_ids.add(result.session_id)
            if session_id is None:
                launched = self._run_browser_session(directory, result.session_id, project.id, f"{project.name} browser")
                if launched is False:
                    self.notify(f"Browser session {result.session_id} was created; select it and press Enter to reopen",
                                severity="warning", timeout=10)
                    return
            self.notify("Browser enabled. Enter opens its connected terminal; /models chooses the model.", timeout=8)
        except Exception:
            self.notify("Browser operation could not be verified; inspect the session before retrying",
                        severity="error", timeout=10)
        finally:
            self._browser_operations.discard(key)
            self._request_refresh(force=True)

    def _run_browser_session(self, directory: Path, session_id: str, project_id: str, title: str,
                             *, auto: bool = False) -> bool:
        # Use the backend where the runtime MCP was registered. A standalone
        # TUI can have an older tool/config cache even while sharing the same DB.
        if not directory.is_dir() or not self.source.opencode_bin:
            self.notify("Browser-session directory or executable is unavailable", severity="error")
            return False
        if getattr(self.source, "backend", "v1") == "v2":
            arguments = [str(directory), "--session", session_id]
            if auto:
                arguments.append("--auto")
            return self._run_opencode(arguments,
                                     tmux_name=f"oc2-{session_id}", project_id=project_id, title=title)
        if auto:
            self.notify("Auto mode for browser sessions needs OpenCode V2; opening normally", timeout=5)
        command = [sys.executable, "-B", "-m", "ocdeck.browser_session",
                   "--opencode", self.source.opencode_bin, "--url", self.source.api_url,
                   "--directory", str(directory), "--session", session_id,
                   "--server-env", str(self.source.server_env_file)]
        accent, label = self._project_theme(project_id)
        name = f"oc-browser-{session_id}"
        self._record_owner_opened_session(session_id)
        if self._tmux_has_session(name) and not self._tmux_pane_runs(name, "opencode"):
            # The managed browser terminal is occupied by something else, such
            # as a stale viewer; attaching to it would show the wrong thing.
            if self._tmux_kill_session(name):
                self.notify(
                    f"Replaced a stale browser terminal for {sanitize_terminal_text(title)}",
                    timeout=6,
                )
        return self._launch_tmux(name, directory, command,
                                 accent=accent, label=label, title="" if self.private else title)

    def action_new_session_choose(self) -> None:
        """N / + New session: choose the harness first when several are enabled."""
        if self._guard_next_read_only():
            return
        enabled = self._enabled_harnesses()
        if len(enabled) <= 1:
            self.action_new_session()
            return
        project = self._selected_launch_project()
        if project is None:
            self.notify("Select a project first", severity="warning")
            return
        # A new conversation only: the picker offers no Continue without a session.
        self._choose_launch_worker(project, None, enabled)

    def action_new_session(self, *, auto: bool = False) -> None:
        if self._guard_next_read_only():
            return
        project = self._selected_launch_project()
        if project is None:
            self.notify("Select a project first", severity="warning")
            return
        harness = self._launch_harness()
        if harness not in self._enabled_harnesses():
            self.notify(
                "No enabled harness can start sessions; see ~/.config/ocdeck/harnesses.json",
                severity="warning",
            )
            return
        if harness != "opencode":
            if auto:
                self.notify("Auto mode is OpenCode-only; starting a normal session", timeout=4)
            self._new_harness_session(harness, project)
            return
        if getattr(self.source, "backend", "v1") == "v2" and hasattr(
            self.source, "create_session"
        ):
            directory = self._ensure_launch_directory(Path(project.directory).expanduser())
            if directory is None:
                return
            self._create_v2_session_worker(
                directory, project.id, project.name, auto=auto
            )
            return
        name = f"oc-new-{time.time_ns()}"
        arguments = [project.directory]
        if auto:
            arguments.append("--auto")
        self._run_opencode(
            arguments, tmux_name=name, project_id=project.id
        )

    @work(group="new-session", exit_on_error=False)
    async def _create_v2_session_worker(
        self, directory: Path, project_id: str, project_name: str, *, auto: bool = False,
        agent: str = "",
    ) -> None:
        session_id, error = await self.source.create_session(directory)
        if error:
            self.notify(f"Could not create V2 session: {error}", severity="error")
            return
        self._record_owner_opened_session(session_id)
        arguments = [str(directory), "--session", session_id, *agent_arguments("opencode", agent)]
        if auto:
            arguments.append("--auto")
        launched = self._run_opencode(
            arguments,
            tmux_name=self._session_tmux_name_for_id(session_id),
            project_id=project_id,
            title=project_name,
        )
        if launched is False:
            if not hasattr(self.source, "remove_session"):
                self.notify(
                    f"V2 session {session_id} was created but could not be launched; "
                    "automatic rollback is unavailable",
                    severity="error",
                    timeout=10,
                )
                self._request_refresh(force=True)
                return
            rollback_error = await self.source.remove_session(session_id)
            if rollback_error:
                self.notify(
                    f"V2 session {session_id} could not be launched or rolled back: "
                    f"{rollback_error}",
                    severity="error",
                    timeout=10,
                )
            else:
                self.notify(
                    "Could not launch V2; unused session was removed",
                    severity="error",
                    timeout=8,
                )
            self._request_refresh(force=True)

    def action_new_terminal(self) -> None:
        if self._guard_next_read_only():
            return
        project = self._selected_launch_project()
        directory = self._ensure_launch_directory(
            Path(project.directory if project else os.getcwd()).expanduser()
        )
        if directory is None:
            return
        name = f"oc-sh-{time.time_ns()}"
        shell = os.environ.get("SHELL") or "/bin/bash"
        accent, label = self._project_theme(self.selected_project_id)
        self._launch_tmux(name, directory, [shell], accent=accent, label=label,
                          harness="Shell", model=Path(shell).name)

    def _ensure_launch_directory(self, directory: Path) -> Path | None:
        if directory.is_dir():
            return directory
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        else:
            self.notify(f"Created project directory {directory}", timeout=4)
            return directory
        label = directory.name or "workspace"
        fallback = Path.home() / "ocdeck-workspaces" / label
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self.notify(
                "Cannot prepare a launch directory: "
                f"{type(error).__name__}",
                severity="error",
                timeout=8,
            )
            return None
        self.notify(
            f"Project path unavailable; using {fallback} instead",
            severity="warning",
            timeout=8,
        )
        return fallback

    def _current_session(self) -> SessionRecord | None:
        focused = self.screen.focused
        if isinstance(focused, DataTable):
            if focused.id not in {"sessions-table", "agents-table"}:
                return None
            table = focused
        else:
            active = self.query_one("#tabs", TabbedContent).active
            if active not in {"overview", "agents"}:
                return None
            table = self.query_one(
                "#agents-table" if active == "agents" else "#sessions-table", DataTable
            )
        if table.row_count == 0 or not table.is_valid_coordinate(table.cursor_coordinate):
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        self.selected_session_id = str(key)
        return self.session_by_id.get(self.selected_session_id)

    def _session_tmux_name(self, session: SessionRecord) -> str:
        return self._session_tmux_name_for_id(session.id)

    def _session_tmux_name_for_id(self, session_id: str) -> str:
        harness, native = split_session_key(session_id)
        adapter = self._harness_adapter(harness) if harness != "opencode" else None
        if adapter is not None:
            return adapter.tmux_name(native)
        prefix = "oc2" if getattr(self.source, "backend", "v1") == "v2" else "oc"
        return f"{prefix}-{session_id}"

    def _tmux_client_attached(self, name: str) -> bool:
        """Whether a desktop tmux client already shows this session (an open window).

        Remote clients (over SSH, e.g. Termius on the owner's phone) do not
        count: they must never stop the desktop from opening its own viewer.
        """
        try:
            result = subprocess.run(
                ["tmux", "list-clients", "-t", f"={name}", "-F", "#{client_pid}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
        pids = [int(value) for value in result.stdout.split() if value.strip().isdigit()]
        return any(not tmux_client_is_remote(pid) for pid in pids)

    def _tmux_has_session(self, name: str) -> bool:
        try:
            result = subprocess.run(
                # "=name" forces an exact session-name match (G5): a plain
                # target is a prefix/fnmatch pattern an impostor can satisfy.
                ["tmux", "has-session", "-t", f"={name}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _tmux_session_matches(self, name: str, command: list[str]) -> bool:
        """Best-effort identity check before attaching to an existing managed
        session (G5): a squatted managed name running something else must not
        be attached. Prefix-tolerant comparison absorbs comm/symlink naming
        (python vs python3.14, opencode vs opencode2). Uncertain results stay
        permissive — same policy as ``_tmux_pane_runs`` — so a tmux hiccup
        never bricks opening terminals; the C111 private socket is the
        structural fix.
        """
        expected = Path(command[0]).name if command else ""
        if len(expected) < 4:
            return True
        try:
            result = subprocess.run(
                ["tmux", "list-panes", "-t", f"={name}:", "-F", "#{pane_current_command}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return True
        if result.returncode != 0:
            return True
        commands = result.stdout.decode("utf-8", errors="replace").split()
        if not commands:
            return True

        def compatible(item: str, other: str) -> bool:
            return item == other or item.startswith(other) or other.startswith(item)

        return all(compatible(item, expected) for item in commands)

    def _tmux_pane_runs(self, name: str, expected: str) -> bool:
        """Whether every pane in a tmux session runs the expected command.

        Uncertain results report True so a managed terminal is never killed
        just because tmux could not be queried.
        """
        try:
            result = subprocess.run(
                ["tmux", "list-panes", "-t", f"={name}:", "-F", "#{pane_current_command}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return True
        if result.returncode != 0:
            return True
        commands = result.stdout.decode("utf-8", errors="replace").split()
        return bool(commands) and all(item == expected for item in commands)

    def _tmux_attach(self, name: str, directory: Path, title: str = "") -> bool:
        if self.inline_tmux:
            if not self._write_mobile_target(name, title):
                return False
            environment = os.environ.copy()
            environment.pop("TMUX", None)
            environment.pop("TMUX_PANE", None)
            try:
                with self.suspend():
                    result = subprocess.run(
                        [
                            "tmux",
                            "attach-session",
                            "-f",
                            "ignore-size",
                            "-t",
                            f"={name}",
                        ],
                        cwd=directory,
                        env=environment,
                        check=False,
                    )
            except OSError:
                return False
            return result.returncode == 0
        viewer_title = clip_text(title, 80) or sanitize_terminal_text(name)
        try:
            # --standalone creates a separate window; --tab avoids Ptyxis's
            # command-window mode, which disables native tab shortcuts.
            subprocess.Popen(
                [
                    "/usr/bin/ptyxis",
                    "--standalone",
                    "--tab",
                    "--title",
                    f"OpenCode · {viewer_title}",
                    f"--working-directory={directory}",
                    "--",
                    "/usr/bin/tmux",
                    "attach-session",
                    "-t",
                    f"={name}",
                ],
                start_new_session=True,
            )
        except OSError:
            return False
        return True

    def _write_mobile_target(self, name: str, title: str = "") -> bool:
        target = self.mobile_target_file
        payload = {
            "tmux": name,
            "title": sanitize_terminal_text(title),
            "updatedMs": time.time_ns() // 1_000_000,
        }
        temporary_name = ""
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                os.chmod(temporary_name, 0o600)
                json.dump(payload, temporary, separators=(",", ":"))
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, target)
        except OSError:
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
            return False
        return True

    def _project_theme(self, project_id: str) -> tuple[str, str]:
        project = self.project_by_id.get(project_id)
        label = sanitize_terminal_text(project.name) if project else ""
        return project_accent(project_id), label

    def _run_opencode(
        self,
        arguments: list[str],
        tmux_name: str | None = None,
        project_id: str | None = None,
        title: str = "",
    ) -> bool:
        if not self.source.opencode_bin:
            self.notify("OpenCode executable not found", severity="error")
            return False
        directory = self._ensure_launch_directory(Path(arguments[0]).expanduser())
        if directory is None:
            return False
        session = self._current_session()
        if project_id is None and session is not None:
            project_id = session.project_id
        accent, label = self._project_theme(project_id or "")
        name = tmux_name or (
            self._session_tmux_name(session)
            if session is not None
            else f"oc-new-{time.time_ns()}"
        )
        command = [self.source.opencode_bin]
        if (
            getattr(self.source, "backend", "v1") == "v2"
            and getattr(self.source, "api_url", "")
        ):
            command.extend(("--server", self.source.api_url))
        command.extend((str(directory), *arguments[1:]))
        native_id = arguments[arguments.index("--session") + 1] if "--session" in arguments else ""
        resumed = self.session_by_id.get(native_id)
        # An owner launch from OC Deck: a known id is recorded at once; a new
        # OpenCode session gets its id from the CLI and is matched later.
        if native_id:
            self._record_owner_opened_session(native_id)
        else:
            self._record_owner_opened_launch(name, str(directory))
        return self._launch_tmux(
            name,
            directory,
            command,
            accent=accent,
            label=label,
            title="" if self.private else title or label,
            model=resumed.model if resumed else "",
        )

    def _launch_tmux(
        self,
        name: str,
        directory: Path,
        command: list[str],
        *,
        accent: str = "",
        label: str = "",
        title: str = "",
        harness: str = "opencode",
        model: str = "",
    ) -> bool:
        if not self._tmux_has_session(name):
            launch = [
                "tmux",
                "new-session",
                "-d",
                "-s",
                name,
                "-c",
                str(directory),
                *command,
            ]
            try:
                created = subprocess.run(launch, timeout=10, check=False)
            except subprocess.TimeoutExpired:
                self.notify(f"tmux did not respond while starting {name}", severity="error")
                return False
            except OSError:
                self.notify("tmux is not available on this system", severity="error")
                return False
            if created.returncode != 0:
                self.notify(
                    f"Could not start tmux session {name}", severity="error"
                )
                return False
            self.notify(f"Started {name}; attaching…", timeout=3)
        else:
            if not self._tmux_session_matches(name, command):
                # G5: an existing managed name whose panes do not match the
                # expected terminal may be squatted by another process.
                self.notify(
                    f"Refusing to attach to {name}: its panes do not match the expected terminal",
                    severity="warning",
                )
                self._request_refresh(force=True)
                return False
            if not self.inline_tmux and (
                self._raise_existing_window(name, title) is True or self._tmux_client_attached(name)
            ):
                self.notify(f"Live terminal {name} is already open", timeout=3)
                self._request_refresh(force=True)
                return True
            self.notify(f"Attaching to live terminal {name}…", timeout=3)
        apply_header(
            name, harness=harness, model="" if self.private else model,
            project="Hidden project" if self.private else label,
            title="Hidden session" if self.private else title,
            accent=accent, run=subprocess.run,
        )
        if not self._tmux_attach(name, directory, title):
            self.notify("Could not open the tmux terminal", severity="error")
            self._request_refresh(force=True)
            return True
        self._request_refresh(force=True)
        return True

    def _refresh_tmux_headers(self) -> None:
        if self._headers_in_progress:
            self._headers_pending = True
            return
        headers = list(headers_for_sessions(
            self.snapshot.sessions,
            {project.id: project.name for project in self.snapshot.projects},
            {project.id: project_accent(project.id) for project in self.snapshot.projects},
        ))
        if headers:
            self._headers_in_progress = True
            self._tmux_headers_worker(headers)

    @work(group="tmux-headers", exit_on_error=False)
    async def _tmux_headers_worker(self, headers: list[tuple[str, dict]]) -> None:
        """Refresh metadata off the UI thread; a slow tmux never blocks the board."""
        try:
            for name, values in headers:
                if self.private:
                    values = {**values, "model": "", "project": "Hidden project", "title": "Hidden session"}
                await asyncio.to_thread(apply_header, name, **values, run=subprocess.run)
        except asyncio.CancelledError:
            self._headers_pending = False
            raise
        finally:
            self._headers_in_progress = False
            if self._headers_pending:
                self._headers_pending = False
                self._refresh_tmux_headers()

    def action_minimize_window(self) -> None:
        if self._minimize_window():
            return
        self.notify("Could not minimize this window", severity="warning")

    def _minimize_window(self) -> bool:
        if os.environ.get("WAYLAND_DISPLAY"):
            if self._ydotool_hide_window():
                return True
            return self._xdotool_minimize_window()
        if self._xdotool_minimize_window():
            return True
        return self._ydotool_hide_window()

    def _xdotool_minimize_window(self) -> bool:
        try:
            result = subprocess.run(
                ["xdotool", "getactivewindow", "windowminimize"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _ydotool_hide_window(self) -> bool:
        try:
            result = subprocess.run(
                ["ydotool", "key", "133:1", "35:1", "35:0", "133:0"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0


KEY_REFERENCE = """
# Keyboard map

| Key | Signal |
| --- | --- |
| **1 / 2 / 3 / 4 / 5 / 6** | Switch operations, services, keys, agents, next steps, and alarms |
| **Ctrl+← / Ctrl+→** | Switch to the previous or next view |
| **Tab / Shift+Tab** | Move focus through the controls |
| **/** | Search all sessions; scoped-project matches rank first |
| **↑ ↓ / j k** | Move through rows; in NEXT, cycle the selected project |
| **← → / h l** | Move between project and session panes |
| **← in AGENTS** | Expand/collapse live subagents under the selected agent |
| **o / Enter** | Attach to the session's live terminal; on a CLOSED agent row, relaunch it |
| **Enter in ALARMS** | Locate the exact known session in Operations |
| **Shift+D in ALARMS** | Record your review of the selected alarm (press twice; the row stays listed, dimmed) |
| **y** | Approve the selected pending permission once |
| **Click a session's name** | Rename it; Enter saves, Esc cancels |
| **a / Auto** | New/resume with `--auto`; on a running OpenCode session without it, press twice to reopen it with `--auto`; server-attached browser sessions open with saved server permissions |
| **Shift+P / Pin window spot** | New session windows open at the selected session window's position and size (kept across logins) |
| **Shift+L in AGENTS** | Relaunch all previously open agent sessions (press twice to confirm) |
| **Shift+H** | Cycle the harness for new sessions (OpenCode / Claude Code / Codex) |
| **Shift+S** | Choose harness and agent/profile for the selected project; New or Continue with handoff notes |
| **Shift+C** | Hand the selected session off to the Shift+H harness, with notes and git state |
| **z in AGENTS** | Send every attached agent terminal to the background; tmux sessions keep running (press twice to confirm) |
| **x** | Close the session's tmux job or direct terminal (press twice; history retained) |
| **Shift+A** | Archive the selected session off the board (press twice). OC Deck-only and reversible: nothing is stopped, a still-running session stays visible until it stops, and Shift+U lists archived rows again. In ALARMS it dismisses every listed alarm instead |
| **Shift+U** | Show or hide archived sessions: listed dimmed with an [archived] marker; Shift+A twice on one unarchives it |
| **n / + NEW SESSION** | Start a session in the selected project |
| **Shift+N / + BROWSER SESSION** | New blank browser-enabled session; choose its model with `/models` |
| **Shift+B / ENABLE BROWSER** | Give the selected eligible idle primary browser access; preserve its model |
| **t** | Open a fresh shell terminal in the selected project |
| **f** | Scope the session list to the selected project |
| **b** | Include/hide agent-created sessions in Operations |
| **Clear filters** | Clear search and project scope; keep the agent-session choice |
| **m** | Minimize this window; OC Deck keeps running in the background |
| **r** | Refresh all signals |
| **p** | Hide or reveal project and session names |
| **Esc** | Clear search, then release the project scope |
| **q** | Leave OC Deck |

## Status marks

- **! red PERM** — the agent is asking for permission
- **? purple ASK** — the agent is waiting for your answer
- **● green RUN** — actively working (API busy, or a live TUI's unfinished
  assistant turn has fresh database activity)
- **! amber STAL** — a live TUI has an unfinished assistant turn but no
  activity for 15 minutes
- **◑ orange REV** — finished a turn recently and waits for your judgement
- **◆ amber RTRY** — retrying after an error
- **○ cyan IDLE** — a live terminal with no active turn
- **○ slate** — stored, no live terminal
- **○ slate CLSD** — a previously open agent session; `o` relaunches it and
  `Shift+L` relaunches all of them

TERM: **OPN** open window, **TMX** background tmux, **DIR** direct terminal,
**SRV** server session, **SAV** saved session. The focus strip spells out the
codes and preserves full titles, runtime and prompts when columns shrink.

A leading **↳** in STATE means the signal comes from a nested helper. The
focus strip shows this session's own state plus the helper's state and runtime;
press Left to expand the group. Local Claude slash commands do not start a
model turn, and synthetic CLI notices do not replace the model identity.

`QUESTION` and `PERMISSION` sessions appear in the attention strip above the
views. Agent STATE labels include elapsed time in the current state; `y`
approves a selected permission once through the loopback API. Questions still
open in the terminal because OpenCode's installed client exposes no safe answer
endpoint.

OC Deck reads session metadata and, for live agents-board rows only, each
session's latest textual user prompt (shown sanitized in DETAIL; privacy mode
replaces it with [hidden]). It never renders transcripts, tool output, or
attachment data, and never reads provider credentials. The local OpenCode
database is opened read-only for archived IDs, prompt times and text, and
assistant turn timestamps, plus the native parent IDs used for the expandable
subagent hierarchy — never for assistant content, tool
results, or full history.

The NEXT view is advisory and read-only. Its recommendations cannot be run
from the view.

ALARMS (6) shows the global Sentinel observation feed, including unassigned
projects. The status strip reports OFFLINE, UNAVAILABLE, DEGRADED or OBSERVED
independently of backend refresh. Privacy mode hides alarm details. Enter
locates a matching session; Shift+D dismisses one and Shift+A all listed
alarms after you type their count (records receipt only — the
alarm stays listed, dimmed, and the artifact is never edited); approval and
process actions are blocked here.

Every project owns an accent color. Project and session rows, the detail pane,
and each project's tmux status bar and pane borders share that accent, so every
terminal for a project carries the same theme.

If a project directory is missing, OC Deck creates it when possible; when the
location cannot be created (for example an unmounted drive), sessions and
terminals start in `~/ocdeck-workspaces/<project>` instead.
"""


def render_once(snapshot: DashboardSnapshot) -> int:
    console = Console()
    metrics = snapshot.metrics
    console.print(
        Panel.fit(
            f"[bold #5eead4]OC DECK[/]  [dim]// terminal operations console[/]\n"
            f"Signal: [bold]{escape(sanitize_terminal_text(snapshot.connection.upper()))}[/]  "
            f"Projects: [bold]{len(snapshot.projects)}[/]  "
            f"Sessions: [bold]{len(snapshot.sessions)}[/]  "
            f"TUI: [bold]{snapshot.terminal_instance_count}[/] "
            f"({snapshot.mapped_instance_count} linked, {snapshot.unmapped_instance_count} unlinked)  "
            f"RAM: [bold]{metrics.memory_percent:.0f}%[/]  "
            f"Load: [bold]{metrics.load_1m:.2f}[/]",
            border_style="#284456",
        )
    )

    sessions = Table(box=None, header_style="bold #7890a2", expand=True)
    console.print(health_text(read_report()))
    sessions.add_column("STATE", width=7)
    sessions.add_column("INST", width=4, justify="right")
    sessions.add_column("SESSION")
    sessions.add_column("PROJECT", ratio=1)
    sessions.add_column("AGE", justify="right")
    projects_by_id = {project.id: project for project in snapshot.projects}
    for session in snapshot.sessions[:12]:
        project = projects_by_id.get(session.project_id)
        display_state = session_display_status(session)
        sessions.add_row(
            AGENT_STATE_LABEL.get(display_state, display_state.upper()),
            str(session.instance_count) if session.instance_count else "-",
            Text(sanitize_terminal_text(session.title)),
            Text(
                sanitize_terminal_text(
                    project.name if project else Path(session.directory).name
                )
            ),
            relative_time(session.updated_ms),
        )
    console.print(sessions)

    if snapshot.named_agents:
        named_agents = Table(box=None, header_style="bold #7890a2")
        named_agents.add_column("STATE")
        named_agents.add_column("AGENT")
        named_agents.add_column("ROLE")
        named_agents.add_column("MODEL")
        named_agents.add_column("SESSION", justify="right")
        named_agents.add_column("DETAIL")
        for agent in snapshot.named_agents:
            state = (
                f"STALE/{agent.state.upper()}"
                if snapshot.named_agents_stale
                else agent.state.upper()
            )
            detail = agent.detail
            if snapshot.named_agents_stale and snapshot.named_agents_error:
                detail = f"{snapshot.named_agents_error}; {detail}"
            browser = snapshot.signed_in_tabs_status
            if snapshot.signed_in_tabs_stale:
                browser = f"STALE/{browser}"
            detail += f"; browser {browser}"
            if snapshot.signed_in_tabs_stale and snapshot.signed_in_tabs_error:
                detail += f"; {snapshot.signed_in_tabs_error}"
            named_agents.add_row(
                Text(sanitize_terminal_text(state)),
                Text(sanitize_terminal_text(agent.name)),
                Text(sanitize_terminal_text(agent.role)),
                Text(sanitize_terminal_text(agent.model or "-")),
                str(agent.session_count) if agent.session_count else "-",
                Text(sanitize_terminal_text(detail)),
            )
        console.print(named_agents)

    services = Text("\n")
    for item in snapshot.services:
        services.append("●", style="#5eead4" if item.state == "active" else "#ff6b7a")
        services.append(f" {sanitize_terminal_text(item.label)}  ")
    console.print(services)
    if snapshot.warning:
        Console(stderr=True).print(
            f"[bold #ff6b7a]{escape(sanitize_terminal_text(snapshot.warning))}[/]"
        )
        return 2
    return 0


def build_destinations_payload(
    snapshot: DashboardSnapshot,
    panes: tuple[LiveOpenCodePane, ...],
) -> dict[str, object]:
    sessions = {session.id: session for session in snapshot.sessions}
    projects = {project.id: project for project in snapshot.projects}
    destinations: list[dict[str, str]] = []
    for pane in panes:
        session = sessions.get(pane.session_id)
        if session is None:
            title = "OpenCode terminal"
            project = ""
            state = "open"
        else:
            title = clip_text(session.title, 96) or "Untitled session"
            project_record = projects.get(session.project_id)
            project = clip_text(
                project_record.name if project_record else Path(session.directory).name,
                64,
            )
            state = agent_state(
                replace(session, instance_count=max(1, session.instance_count))
            )
        coordinates = ".".join(
            item for item in (pane.window_index, pane.pane_index) if item
        )
        label_parts = [title]
        if project:
            label_parts.append(project)
        if coordinates:
            label_parts.append(f"pane {coordinates}")
        destinations.append(
            {
                "destination_id": pane.destination_id,
                "pane_id": pane.pane_id,
                "label": clip_text(" · ".join(label_parts), 180),
                "title": title,
                "project": project,
                "state": state,
                "terminal_state": pane.terminal_state,
            }
        )
    return {"schema_version": 1, "destinations": destinations}


def render_destinations_json(
    snapshot: DashboardSnapshot,
    panes: tuple[LiveOpenCodePane, ...],
) -> int:
    print(
        json.dumps(
            build_destinations_payload(snapshot, panes),
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Terminal operations console for OpenCode",
        allow_abbrev=False,
    )
    backend_default = os.environ.get("OCDECK_OPENCODE_BACKEND")
    parser.add_argument(
        "--backend",
        choices=("v2", "v1"),
        default=backend_default,
        help=(
            "OpenCode backend (defaults to OCDECK_OPENCODE_BACKEND, then the "
            "saved ocdeck-backend selection, then v2 on an unconfigured installation)"
        ),
    )
    parser.add_argument(
        "--harness",
        default=None,
        help=(
            "comma-separated harnesses to enable (opencode,claude,codex); overrides "
            "OCDECK_HARNESSES and ~/.config/ocdeck/harnesses.json"
        ),
    )
    parser.add_argument("--once", action="store_true", help="print one report and exit")
    parser.add_argument(
        "--destinations-json",
        action="store_true",
        help="print verified live OpenCode pane destinations as JSON and exit",
    )
    parser.add_argument("--url", default=None, help="OpenCode server URL")
    parser.add_argument("--limit", type=int, default=500, help="maximum stored sessions (default: 500)")
    parser.add_argument("--refresh", type=float, default=15, help="refresh interval in seconds")
    parser.add_argument(
        "--inline-tmux",
        action="store_true",
        help="attach selected tmux sessions inside this terminal",
    )
    parser.add_argument(
        "--projects-file",
        default=None,
        help="Markdown project catalog (defaults to ~/.config/home-agent/projects.md; optional)",
    )
    parser.add_argument(
        "--session-routes-file",
        default=None,
        help="JSON map assigning historical session IDs to catalog projects",
    )
    parser.add_argument(
        "--briefings-file",
        default=None,
        help="Home Agent briefing JSON artifact",
    )
    args = parser.parse_args(argv)
    if args.backend is None:
        try:
            args.backend = read_saved_backend() or "v2"
        except (OSError, ValueError) as error:
            parser.error(f"Cannot read OC Deck backend selection: {error}")
    if args.backend not in {"v1", "v2"}:
        parser.error("OCDECK_OPENCODE_BACKEND must be 'v1' or 'v2'")
    return args


def build_source(args: argparse.Namespace) -> MultiHarnessSource:
    """Build the deck source from whichever harnesses are enabled."""
    override = args.harness or os.environ.get("OCDECK_HARNESSES") or None
    settings = load_harness_settings()
    try:
        enabled = resolve_enabled_harnesses(
            settings, override.split(",") if override else None
        )
    except ValueError as error:
        raise SystemExit(f"ocdeck: {error}") from None
    opencode_enabled = "opencode" in enabled
    try:
        opencode = DashboardSource(
            backend=args.backend,
            api_url=args.url,
            limit=args.limit,
            projects_file=args.projects_file,
            session_routes_file=args.session_routes_file,
            briefings_file=args.briefings_file,
        )
    except (OSError, ValueError):
        if opencode_enabled:
            raise
        opencode = None  # OpenCode is disabled; its configuration may be absent
    if override is None and settings.get("opencode") == "auto":
        # OpenCode is found at fixed install paths, not through PATH, so "auto"
        # follows the source's own discovery (launchers may run with a thin PATH).
        opencode_enabled = opencode is not None and bool(getattr(opencode, "opencode_bin", None))
    return MultiHarnessSource(
        opencode,
        build_adapters(enabled),
        opencode_enabled=opencode_enabled,
        metrics_reader=read_system_metrics,
        refresh_harnesses=True,
        harness_override=tuple(override.split(",")) if override is not None else None,
    )


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    source = build_source(args)
    if args.destinations_json and not source.opencode_enabled:
        raise SystemExit(render_destinations_json(DashboardSnapshot(), ()))
    if args.destinations_json:
        try:
            panes = read_live_opencode_panes(backend=source.backend)
            if not panes:
                raise SystemExit(
                    render_destinations_json(DashboardSnapshot(), ())
                )
            snapshot = asyncio.run(source.collect())
            panes = read_live_opencode_panes(backend=source.backend)
        except SystemExit:
            raise
        except Exception as error:
            Console(stderr=True).print(
                f"[bold red]OC Deck destination discovery failed:[/] "
                f"{escape(type(error).__name__)}"
            )
            raise SystemExit(2) from None
        raise SystemExit(render_destinations_json(snapshot, panes))
    if args.once or not sys.stdout.isatty():
        try:
            snapshot = asyncio.run(source.collect())
        except Exception as error:
            Console(stderr=True).print(
                f"[bold red]OC Deck failed:[/] {escape(type(error).__name__)}"
            )
            raise SystemExit(2) from None
        raise SystemExit(render_once(snapshot))
    OCDeckApp(
        source,
        refresh_seconds=args.refresh,
        inline_tmux=args.inline_tmux,
        recent_open_file=default_recent_open_file(source.backend, getattr(source, "api_url", "")),
    ).run()


def status_symbol(status: str, frame: int = 0) -> str:
    if status == "busy":
        return ACTIVITY_FRAMES[frame % len(ACTIVITY_FRAMES)]
    if status == "retry":
        return "◆" if frame % 2 else "◇"
    if status == "stalled":
        return "!"
    if status == "permission":
        return "!"
    if status == "question":
        return "?"
    if status == "review":
        return "◑" if frame % 2 else "◐"
    if status == "job":
        return "◆"
    return "○"


def status_markup(status: str, frame: int = 0) -> str:
    style = STATUS_STYLE.get(status, STATUS_STYLE["idle"])
    return f"[{style}]{status_symbol(status, frame)}[/]"


def status_text(status: str, frame: int = 0) -> Text:
    symbol = status_symbol(status, frame)
    return Text(symbol, style=STATUS_STYLE.get(status, STATUS_STYLE["idle"]))


def session_display_status(session: SessionRecord) -> str:
    return agent_state(session)


def instance_text(count: int) -> Text:
    if count <= 0:
        return Text("-", style="dim #668094")
    tone = "bold #f2b84b" if count > 1 else "bold #5eead4"
    return Text(str(count), style=tone)


def clip_text(value: str, width: int) -> str:
    value = sanitize_terminal_text(value)
    if width <= 1:
        return value[: max(0, width)]
    return value if len(value) <= width else value[: width - 1] + "…"


def opencode_process_has_auto(pid: int, proc_root: Path = Path("/proc")) -> bool:
    """Whether a running OpenCode process was started with --auto."""
    try:
        arguments = (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return b"--auto" in arguments


def tmux_client_is_remote(pid: int, proc_root: Path = Path("/proc")) -> bool:
    """A tmux client started over SSH (e.g. Termius). Unreadable counts as local."""
    try:
        environment = (proc_root / str(pid) / "environ").read_bytes().split(b"\0")
    except OSError:
        return False
    return any(entry.startswith(b"SSH_CONNECTION=") for entry in environment)
