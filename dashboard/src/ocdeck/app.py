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
    DataTable,
    Footer,
    Input,
    Markdown,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
)

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
)


STATUS_STYLE = {
    "busy": "bold #4ade80",
    "retry": "bold #f2b84b",
    "stalled": "bold #f2b84b",
    "open": "bold #5eead4",
    "permission": "bold #ff6b7a",
    "question": "bold #d4a6ff",
    "review": "bold #ffa657",
    "idle": "dim #668094",
}

AGENT_STATE_LABEL = {
    "busy": "RUNNING",
    "permission": "PERMISSION",
    "question": "QUESTION",
    "retry": "RETRY",
    "stalled": "STALLED",
    "review": "REVIEW",
    "open": "IDLE",
    "idle": "IDLE",
}

HIDDEN_PROMPT_LABEL = "[hidden]"
AGENT_DETAIL_CLIP = 32

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

TMUX_THEME_BACKGROUND = "#0d1a25"
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


class NavigationTable(KeyboardDataTable):
    """A row table with spatial keyboard navigation between adjacent panes."""

    BINDINGS = [
        Binding("left,h", "app.focus_adjacent_table(-1)", "Previous pane", show=False),
        Binding("right,l", "app.focus_adjacent_table(1)", "Next pane", show=False),
    ]


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
        Binding("enter,o,a,x,n,t", "noop", "Read only", show=False),
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
        height: 3;
        margin: 0 2 1 2;
        padding: 0 1;
        background: #21131b;
        border: round #71334d;
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

    #session-search {
        height: 3;
        margin: 0 1;
        border: tall transparent;
        background: #101f2b;
    }

    #session-search:focus {
        border: tall #5eead4;
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
    .mobile #detail-pane, .mobile Tabs, .mobile Footer {
        display: none;
    }

    .mobile #sessions-pane {
        width: 100%;
        margin-right: 0;
    }

    .mobile #sessions-title {
        height: 2;
        color: #5eead4;
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

    .tiny #session-search, .tiny #session-rename, .tiny .pane-title {
        display: none;
    }

    .tiny #next-view {
        padding: 0 1;
        border: none;
    }

    .tiny #tabs {
        margin: 0;
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
        Binding("y", "approve_permission", "Allow once"),
        Binding("x", "stop_job", "Stop job"),
        Binding("n", "new_session", "New"),
        Binding("t", "new_terminal", "Terminal"),
        Binding("f", "toggle_filter", "Scope"),
        Binding("m", "minimize_window", "Minimize"),
        Binding("1", "show_tab('overview')", "Overview", show=False),
        Binding("2", "show_tab('services')", "Services", show=False),
        Binding("3", "show_tab('keys-view')", "Keys", show=False),
        Binding("4", "show_tab('agents')", "Agents", show=False),
        Binding("5", "show_tab('next')", "Next", show=False),
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
    ) -> None:
        super().__init__()
        self.source = source
        self.refresh_seconds = max(3, refresh_seconds)
        self.activity_seconds = max(1, activity_seconds)
        self.inline_tmux = inline_tmux
        self.mobile_target_file = mobile_target_file or default_mobile_target_file()
        self.periodic_refresh_enabled = auto_refresh
        self.snapshot = DashboardSnapshot()
        self.session_by_id: dict[str, SessionRecord] = {}
        self.project_by_id: dict[str, ProjectRecord] = {}
        self.briefing_by_project_id: dict[str, ProjectBriefingRecord] = {}
        self.selected_session_id = ""
        self.selected_project_id = ""
        self.project_filter = False
        self.search_term = ""
        self.private = False
        self.refresh_in_progress = False
        self.refresh_pending = False
        self._refresh_generation = 0
        self._state_since_ms: dict[str, tuple[str, int]] = {}
        self.requested_tab_id = "agents"
        self.session_title_column = None
        self.session_title_index = -1
        self.renaming_session_id = ""
        self.session_project_column = None
        self.initial_focus_set = False
        self.activity_frame = 0
        self.activity_in_progress = False
        self.stop_confirm = ""
        self._rebuild_echo_ids: dict[str, str] = {}
        self.expanded_agent_ids: set[str] = set()
        self.agent_children_by_id: dict[str, tuple[str, ...]] = {}
        self.agent_parent_by_id: dict[str, str] = {}
        self.agent_display_state_by_id: dict[str, str] = {}
        self.agent_attention_source_by_id: dict[str, str] = {}
        self.agent_session_index = -1
        self._last_mobile_agent_click_serial = 0

    def compose(self) -> ComposeResult:
        yield Static(id="brand")
        with Horizontal(id="metrics"):
            yield MetricCard(classes="metric", id="metric-projects")
            yield MetricCard(classes="metric", id="metric-sessions")
            yield MetricCard(classes="metric", id="metric-memory")
            yield MetricCard(classes="metric", id="metric-connection")
        yield Static(id="attention")

        with TabbedContent(initial="agents", id="tabs"):
            with TabPane("01 / OPERATIONS", id="overview"):
                with Horizontal(id="overview-grid"):
                    with Vertical(classes="pane", id="projects-pane"):
                        yield Static("PROJECTS", classes="pane-title")
                        yield NavigationTable(id="projects-table")
                    with Vertical(classes="pane", id="sessions-pane"):
                        yield Static("RECENT SESSIONS", classes="pane-title", id="sessions-title")
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
                        yield SessionsTable(id="sessions-table")
                    with Vertical(classes="pane", id="detail-pane"):
                        yield Static("SESSION SIGNAL", classes="pane-title")
                        yield Static(id="session-detail")
            with TabPane("02 / SERVICES", id="services"):
                yield KeyboardDataTable(id="services-table")
            with TabPane("03 / KEYS", id="keys-view"):
                yield KeyReference(KEY_REFERENCE, id="key-reference")
            with TabPane("04 / AGENTS", id="agents"):
                yield AgentsTable(id="agents-table")
            with TabPane("05 / NEXT", id="next"):
                with NextStepsView(id="next-view"):
                    yield Static(id="next-content")
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

    def _on_active_tab_changed(self, active: str) -> None:
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
        projects.add_column("AGE", key="age", width=6)

        sessions = self.query_one("#sessions-table", SessionsTable)
        sessions.cursor_type = "row"
        sessions.clicked_column_index = None
        if self.inline_tmux:
            sessions.add_column("ACTIVE", key="state", width=11)
        else:
            sessions.add_column("", key="state", width=2)
            sessions.add_column("INST", key="instances", width=4)
        self.session_title_column = sessions.add_column(
            "AGENT" if self.inline_tmux else "SESSION",
            key="title",
            width=48 if self.inline_tmux else 25,
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
        agents.add_columns("STATE", "TERM", "SESSION", "PROJECT", "AGE", "DETAIL")
        self.agent_session_index = 2

    @work(group="refresh", exit_on_error=False)
    async def _refresh_worker(self) -> None:
        self._render_brand("SCANNING")
        try:
            snapshot = await self.source.collect()
            self._apply_snapshot(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._render_brand("DEGRADED")
            self.notify(f"Refresh failed: {type(error).__name__}", severity="error")
        finally:
            self.refresh_in_progress = False
            if self.refresh_pending:
                self.refresh_pending = False
                self._request_refresh()

    def action_refresh_data(self) -> None:
        self._request_refresh()

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
        self._activity_worker()

    @work(group="activity", exit_on_error=False)
    async def _activity_worker(self) -> None:
        generation = self._refresh_generation
        try:
            snapshot = await self.source.collect_activity()
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        finally:
            self.activity_in_progress = False
        if snapshot is not None and generation == self._refresh_generation:
            self._apply_snapshot(snapshot)

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

    def _apply_snapshot(self, snapshot: DashboardSnapshot) -> None:
        self.snapshot = snapshot
        self.session_by_id = {session.id: session for session in snapshot.sessions}
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
            self.selected_project_id = snapshot.projects[0].id if snapshot.projects else ""
        self._refresh_state_clock()
        self._render_brand(snapshot.connection.upper())
        self._render_metrics()
        self._render_attention()
        self._render_projects()
        self._render_sessions()
        self._render_services()
        self._render_agents()
        self._render_detail()
        self._render_next()
        if not self.initial_focus_set:
            self.initial_focus_set = True
            self.call_after_refresh(self._focus_initial_table)
        if snapshot.warning:
            self.notify(snapshot.warning, severity="warning")

    def _render_brand(self, state: str) -> None:
        state = sanitize_terminal_text(state)
        clock = datetime.now().astimezone().strftime("%H:%M:%S")
        tone = "#5eead4" if state == "LIVE" else "#f2b84b" if state in {"LOCKED", "SCANNING"} else "#ff6b7a"
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
            "Sessions",
            str(len(snapshot.sessions)),
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
        content = Text()
        content.append(
            f"{len(sessions)} NEED ATTENTION  ",
            style="bold #ff6b7a",
        )
        for index, session in enumerate(sessions):
            state = agent_state(session)
            label = AGENT_STATE_LABEL[state]
            title = "Hidden session" if self.private else session.title
            if index:
                content.append("  ·  ", style="dim #7890a2")
            content.append(
                f"{label} ", style=STATUS_STYLE.get(state, STATUS_STYLE["idle"])
            )
            content.append(clip_text(sanitize_terminal_text(title), 28))
            content.append(f" · {self._state_age(session)}", style="dim #aebfcb")
        attention.update(content)
        attention.display = True

    def _render_projects(self) -> None:
        table = self.query_one("#projects-table", DataTable)
        selected_id = self._selected_row_id(table, self.selected_project_id)
        table.clear(columns=False)
        for index, project in enumerate(self.snapshot.projects, start=1):
            name = project.name if not self.private else f"Project {index:02d}"
            table.add_row(
                Text(clip_text(name, 24), style=project_accent(project.id)),
                str(project.session_count),
                relative_time(project.updated_ms),
                key=project.id,
            )
        found = self._restore_table_cursor(table, selected_id)
        if found:
            self.selected_project_id = selected_id
        self._arm_rebuild_echo(table, selected_id if found else "")

    def _filtered_sessions(self) -> tuple[SessionRecord, ...]:
        sessions = tuple(
            sorted(
                self.snapshot.sessions,
                key=lambda session: (
                    session.instance_count <= 0,
                    -(session.last_interaction_ms or session.updated_ms),
                    -session.updated_ms,
                ),
            )
        )
        if self.inline_tmux:
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
        table.clear(columns=False)
        filtered = self._filtered_sessions()
        self._render_sessions_title(len(filtered))
        filtered_ids = {session.id for session in filtered}
        if selected_id not in filtered_ids:
            selected_id = filtered[0].id if filtered else ""
        title_width = table.columns[self.session_title_column].width
        project_width = (
            table.columns[self.session_project_column].width
            if self.session_project_column is not None
            else 0
        )
        for index, session in enumerate(filtered, start=1):
            title = f"Session {index:02d}" if self.private else session.title
            if self.inline_tmux:
                state = session_display_status(session)
                label = "READY" if state in {"idle", "open"} else AGENT_STATE_LABEL.get(
                    state, state.upper()
                )
                table.add_row(
                    Text(label, style=STATUS_STYLE.get(state, STATUS_STYLE["idle"])),
                    Text(clip_text(title, title_width)),
                    key=session.id,
                )
                continue
            project = "hidden" if self.private else self._session_project_name(session)
            project_style = (
                "" if self.private else project_accent(session.project_id)
            )
            table.add_row(
                status_text(session_display_status(session), self.activity_frame),
                instance_text(session.instance_count),
                Text(clip_text(title, title_width)),
                Text(clip_text(project, project_width), style=project_style),
                relative_time(session_age_ms(session)),
                key=session.id,
            )
        self._restore_table_cursor(table, selected_id)
        self.selected_session_id = selected_id

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

    def _arm_rebuild_echo(self, table: DataTable, row_id: str) -> None:
        """Ignore the next highlight emitted by restoring a rebuilt table."""
        table_id = table.id
        if table_id is None:
            return
        if row_id:
            self._rebuild_echo_ids[table_id] = row_id
        else:
            self._rebuild_echo_ids.pop(table_id, None)

    def _render_sessions_title(self, count: int) -> None:
        title = self.query_one("#sessions-title", Static)
        if self.inline_tmux:
            title.update(f"ACTIVE AGENTS · TAP TO OPEN · {count}")
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
        heading = "PRIVATE SESSIONS" if self.private else "SESSIONS"
        title.update(f"{heading} · {scope} · {count}")

    def _render_services(self) -> None:
        table = self.query_one("#services-table", DataTable)
        table.clear(columns=False)
        for service in self.snapshot.services:
            if service.state == "active":
                state = Text("● ACTIVE", style="bold #5eead4")
            elif service.state in {"inactive", "failed"}:
                state = Text(f"● {service.state.upper()}", style="bold #ff6b7a")
            else:
                state = Text("○ UNKNOWN", style="dim")
            table.add_row(
                state,
                Text(sanitize_terminal_text(service.label)),
                Text(sanitize_terminal_text(service.role)),
                Text(sanitize_terminal_text(service.unit)),
                key=service.unit,
            )

    def _render_agents(self, selection_id: str | None = None) -> None:
        table = self.query_one("#agents-table", DataTable)
        selected_id = (
            self._selected_row_id(table, self.selected_session_id)
            if selection_id is None
            else selection_id
        )
        table.clear(columns=False)
        base_states = {
            session.id: agent_state(session) for session in self.snapshot.sessions
        }
        active_ids = {
            session_id
            for session_id, state in base_states.items()
            if state != "idle"
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
            "retry": 4,
            "stalled": 5,
            "question": 6,
            "permission": 7,
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
            project = "hidden" if self.private else self._session_project_name(session)
            if self.private and state in {"question", "permission"}:
                detail = HIDDEN_PROMPT_LABEL
            elif state == "question":
                detail = signal_session.question or "Input required"
                if signal_session.id != session.id:
                    detail = f"subagent: {detail}"
            elif state == "permission":
                detail = signal_session.permission or "Permission required"
                if signal_session.id != session.id:
                    detail = f"subagent: {detail}"
            elif self.private and session.last_prompt:
                # Prompt content must never be visible while private.
                detail = HIDDEN_PROMPT_LABEL
            elif session.last_prompt:
                detail = session.last_prompt
            elif state == "review":
                detail = "waiting for you"
            else:
                detail = session.permission
            if session.terminal_attached:
                terminal = Text("● OPEN", style="bold #5eead4")
            elif session.terminals:
                terminal = Text("○ BG TMUX", style="#86b7ff")
            else:
                terminal = Text("◆ DIRECT", style="#f2b84b")
            children = self.agent_children_by_id.get(session.id, ())
            indent = "  " * min(depth, 4)
            if children:
                arrow = "▾" if session.id in self.expanded_agent_ids else "▸"
                title = f"{indent}{arrow}[{len(children)}] {title}"
            elif depth:
                title = f"{indent}└ {title}"
            elif session.parent_id or session.agent_parent_id:
                title = f"↳ {title}"
            marker = status_symbol(state, self.activity_frame) if state == "stalled" else "●"
            table.add_row(
                Text(
                    f"{marker} {AGENT_STATE_LABEL[state]} {self._agent_state_age(session)}",
                    style=STATUS_STYLE.get(state, STATUS_STYLE["idle"]),
                ),
                terminal,
                Text(clip_text(sanitize_terminal_text(title), 26)),
                Text(
                    clip_text(sanitize_terminal_text(project), 12),
                    style="" if self.private else project_accent(session.project_id),
                ),
                relative_time(session_age_ms(session)),
                Text(clip_text(sanitize_terminal_text(detail), AGENT_DETAIL_CLIP)),
                key=session.id,
            )
        found = self._restore_table_cursor(table, selected_id)
        # When the selected agent left the board, its rebuild still queues a
        # highlight for whatever row now sits on top; that echo must never
        # steal the shared Operations selection.
        self._drop_next_agents_highlight = not found and table.row_count > 0

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
        if session.instance_count:
            hint = (
                "Press [bold]o[/] to attach · [bold]x[/] to stop the tmux job."
            )
        else:
            hint = "Press [bold]o[/] to start its terminal."
        hint += " Click its name to rename it."
        detail.update(
            f"[bold #e7f5fc]{escape(title)}[/]\n\n"
            f"[dim]STATE[/]\n{status_markup(display_status, self.activity_frame)}  "
            f"{AGENT_STATE_LABEL.get(display_status, display_status.upper())} · "
            f"{self._agent_state_age(session)}\n\n"
            f"[dim]TERMINAL INSTANCES[/]\n{instance_label}\n\n"
            f"{terminal_block}"
            f"[dim]PROJECT[/]\n[#86b7ff]{escape(path)}[/]\n\n"
            f"[dim]SESSION ID[/]\n[#7890a2]{escape(session_id)}[/]\n\n"
            f"[dim]UPDATED[/]\n{relative_time(session.updated_ms)} ago\n\n"
            f"[dim]{hint}[/]"
        )

    @on(Input.Changed, "#session-search")
    def on_search_changed(self, event: Input.Changed) -> None:
        self.search_term = event.value.strip()
        self._render_sessions()
        self._render_detail()

    @on(Input.Submitted, "#session-search")
    def on_search_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._open_from_search()

    def on_key(self, event: Key) -> None:
        focused = self.screen.focused
        if focused is None or getattr(focused, "id", "") != "session-search":
            return
        if event.key == "down":
            event.stop()
            event.prevent_default()
            self._focus_search_results()

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
        self._apply_local_title(session.id, new_title)
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
        self._request_refresh(force=True)

    @on(DataTable.RowHighlighted)
    def on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None:
            return
        table = event.data_table
        key = str(event.row_key.value)
        table_id = table.id or ""
        if self._rebuild_echo_ids.get(table_id) == key:
            self._rebuild_echo_ids.pop(table_id, None)
            return
        if table.id == "agents-table" and self._drop_next_agents_highlight:
            self._drop_next_agents_highlight = False
            return
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
            self.action_focus_adjacent_table(1)

    @on(Resize)
    def on_resize(self, event: Resize) -> None:
        self.screen.set_class(event.size.width < 144, "compact")
        self.screen.set_class(event.size.width < 80, "narrow")
        self.screen.set_class(event.size.height < 25, "short")
        self.screen.set_class(event.size.height < 19, "tiny")
        self._resize_session_columns(event.size.width)
        self._resize_agents_columns(event.size.width)
        self._render_next()

    def _resize_session_columns(self, screen_width: int) -> None:
        table = self.query_one("#sessions-table", DataTable)
        if self.inline_tmux:
            if self.session_title_column is not None:
                table.columns[self.session_title_column].width = max(20, screen_width - 15)
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
        if screen_width < 60:
            widths = (10, 14, 8, 4, 0, 0)
        elif screen_width < 90:
            widths = (12, 20, 10, 5, 12, 16)
        else:
            widths = (13, 26, 12, 6, 20, 32)
        for column, width in zip(table.columns.values(), widths):
            column.width = width
            column.display = width > 0
        table.refresh(layout=True)

    def action_search(self) -> None:
        search = self.query_one("#session-search", Input)
        self.requested_tab_id = "overview"
        self.query_one("#tabs", TabbedContent).active = "overview"
        search.focus()

    def action_clear_search(self) -> None:
        search = self.query_one("#session-search", Input)
        if search.value:
            search.value = ""
            return
        if self.project_filter:
            self.action_toggle_filter()
            return
        self.set_focus(self.query_one("#sessions-table", DataTable))

    def action_privacy(self) -> None:
        self.private = not self.private
        self._render_projects()
        self._render_sessions()
        self._render_attention()
        self._render_agents()
        self._render_detail()
        self._render_next()
        self.notify("Privacy mode on" if self.private else "Privacy mode off")

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
        view_ids = ("overview", "services", "keys-view", "agents", "next")
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
        if active != "next":
            return False
        self.notify("NEXT is read-only", severity="warning", timeout=3)
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
        attached = (
            self._attach_live_terminal(session, title_override)
            if title_override is not None
            else self._attach_live_terminal(session)
        )
        if attached:
            return
        self._run_opencode(
            [self._session_directory(session), "--session", session.id],
            tmux_name=self._session_tmux_name(session) if self.inline_tmux else None,
            project_id=session.project_id,
            title=title_override or session.title,
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
        session = self._current_session()
        if not session:
            self.notify("Select a session first", severity="warning")
            return
        if self._attach_live_terminal(session):
            return
        self._run_opencode(
            [self._session_directory(session), "--session", session.id, "--auto"],
            project_id=session.project_id,
            title=session.title,
        )

    def action_approve_permission(self) -> None:
        if self._guard_next_read_only():
            return
        session = self._current_session()
        if not session:
            self.notify("Select a session first", severity="warning")
            return
        display_state = self._agent_display_state(session)
        permission_session = self._agent_attention_source(session)
        if display_state != "permission" or not permission_session.permission_id:
            self.notify("No pending permission for this session", severity="warning")
            return
        if not hasattr(self.source, "approve_permission"):
            self.notify("Permission approval is unavailable", severity="error")
            return
        self._approve_permission_worker(
            permission_session.id, permission_session.permission_id
        )

    @work(group="permission", exit_on_error=False)
    async def _approve_permission_worker(
        self, session_id: str, permission_id: str
    ) -> None:
        error = await self.source.approve_permission(session_id, permission_id)
        if error:
            self.notify(f"Permission approval failed: {error}", severity="error")
            return
        self.notify("Permission approved once")
        self._request_activity_refresh()

    def action_stop_job(self) -> None:
        if self._guard_next_read_only():
            return
        session = self._current_session()
        if not session:
            self.notify("Select a session first", severity="warning")
            return
        candidates = list(session.terminals)
        canonical = self._session_tmux_name(session)
        if canonical not in candidates:
            candidates.append(canonical)
        name = next((item for item in candidates if self._tmux_has_session(item)), "")
        if not name:
            self.notify("No active tmux job for this session", severity="warning")
            return
        if self.stop_confirm != session.id:
            self.stop_confirm = session.id
            self.notify(
                f"Press x again to stop tmux job {sanitize_terminal_text(name)}",
                timeout=6,
            )
            self.set_timer(6, self._clear_stop_confirm)
            return
        self.stop_confirm = ""
        self._stop_job_worker(session.id, name)

    def _clear_stop_confirm(self) -> None:
        self.stop_confirm = ""

    def _tmux_kill_session(self, name: str) -> bool:
        try:
            result = subprocess.run(
                ["tmux", "kill-session", "-t", name],
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

    def _attach_live_terminal(
        self, session: SessionRecord, title_override: str | None = None
    ) -> bool:
        if session.instance_count <= 0 or not session.terminals:
            return False
        name = session.terminals[0]
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
        if focus_result is None:
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

    def _raise_existing_window(self, tmux_name: str, title: str = "") -> bool | None:
        shell_result = self._focus_tmux_via_shell(tmux_name)
        if shell_result is not None:
            return shell_result
        ptyxis_result = self._focus_tmux_via_ptyxis(tmux_name)
        if ptyxis_result is not None:
            return ptyxis_result
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

    def action_new_session(self) -> None:
        if self._guard_next_read_only():
            return
        project = self.project_by_id.get(self.selected_project_id)
        directory = project.directory if project else os.getcwd()
        name = f"oc-new-{int(datetime.now().timestamp())}"
        self._run_opencode(
            [directory], tmux_name=name, project_id=self.selected_project_id
        )

    def action_new_terminal(self) -> None:
        if self._guard_next_read_only():
            return
        project = self.project_by_id.get(self.selected_project_id)
        directory = self._ensure_launch_directory(
            Path(project.directory if project else os.getcwd()).expanduser()
        )
        if directory is None:
            return
        name = f"oc-sh-{int(datetime.now().timestamp())}"
        shell = os.environ.get("SHELL") or "/bin/bash"
        accent, label = self._project_theme(self.selected_project_id)
        self._launch_tmux(name, directory, [shell], accent=accent, label=label)

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
            table = focused
        else:
            table = self.query_one("#sessions-table", DataTable)
        if table.row_count == 0 or not table.is_valid_coordinate(table.cursor_coordinate):
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        self.selected_session_id = str(key)
        return self.session_by_id.get(self.selected_session_id)

    def _session_tmux_name(self, session: SessionRecord) -> str:
        return f"oc-{session.id}"

    def _tmux_has_session(self, name: str) -> bool:
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            return False
        return result.returncode == 0

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
                            name,
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
            subprocess.Popen(
                [
                    "/usr/bin/ptyxis",
                    "--standalone",
                    "--new-window",
                    "--title",
                    f"OpenCode · {viewer_title}",
                    f"--working-directory={directory}",
                    "--",
                    "/usr/bin/tmux",
                    "attach-session",
                    "-t",
                    name,
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
    ) -> None:
        if not self.source.opencode_bin:
            self.notify("OpenCode executable not found", severity="error")
            return
        directory = self._ensure_launch_directory(Path(arguments[0]).expanduser())
        if directory is None:
            return
        session = self._current_session()
        if project_id is None and session is not None:
            project_id = session.project_id
        accent, label = self._project_theme(project_id or "")
        name = tmux_name or (
            self._session_tmux_name(session)
            if session is not None
            else f"oc-new-{int(datetime.now().timestamp())}"
        )
        self._launch_tmux(
            name,
            directory,
            [self.source.opencode_bin, str(directory), *arguments[1:]],
            accent=accent,
            label=label,
            title="" if self.private else title or label,
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
    ) -> None:
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
                created = subprocess.run(launch, check=False)
            except OSError:
                self.notify("tmux is not available on this system", severity="error")
                return
            if created.returncode != 0:
                self.notify(
                    f"Could not start tmux session {name}", severity="error"
                )
                return
            self._apply_tmux_theme(name, accent, label)
            self.notify(f"Started {name}; attaching…", timeout=3)
        else:
            self.notify(f"Attaching to live terminal {name}…", timeout=3)
        if not self._tmux_attach(name, directory, title):
            self.notify("Could not open the tmux terminal", severity="error")
            return
        self._request_refresh(force=True)

    def _apply_tmux_theme(self, name: str, accent: str, label: str) -> None:
        if not accent:
            return
        status_left = f"#[bold]{label}#[default] · #S " if label else " #S "
        options = (
            ("status-style", f"bg={TMUX_THEME_BACKGROUND},fg={accent}"),
            ("status-left", status_left),
            ("pane-border-style", f"fg={accent}"),
            ("pane-active-border-style", f"fg={accent},bold"),
        )
        for option, value in options:
            try:
                subprocess.run(
                    ["tmux", "set-option", "-t", name, option, value],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return

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
| **1 / 2 / 3 / 4 / 5** | Switch operations, services, keys, agents, and next steps |
| **Ctrl+← / Ctrl+→** | Switch to the previous or next view |
| **Tab / Shift+Tab** | Move focus through the controls |
| **/** | Search all sessions; scoped-project matches rank first |
| **↑ ↓ / j k** | Move through rows; in NEXT, cycle the selected project |
| **← → / h l** | Move between project and session panes |
| **← in AGENTS** | Expand/collapse live subagents under the selected agent |
| **o / Enter** | Attach to the session's live terminal (starts it if not running) |
| **y** | Approve the selected pending permission once |
| **Click a session's name** | Rename it; Enter saves, Esc cancels |
| **a** | Resume with permissions auto-approved (`--auto`) |
| **x** | Stop the session's active tmux job (press twice to confirm) |
| **n** | Start a session in the selected project |
| **t** | Open a fresh shell terminal in the selected project |
| **f** | Scope the session list to the selected project |
| **m** | Minimize this window; OC Deck keeps running in the background |
| **r** | Refresh all signals |
| **p** | Hide or reveal project and session names |
| **Esc** | Clear search, then release the project scope |
| **q** | Leave OC Deck |

## Status marks

- **● red PERMISSION** — the agent is asking for permission
- **? purple QUESTION** — the agent is waiting for your answer
- **● green RUNNING** — actively working (API busy, or its latest assistant turn is still open)
- **● orange REVIEW** — finished a turn recently and waits for your judgement
- **◆ amber RETRY** — retrying after an error
- **○ cyan IDLE** — a live terminal with no active turn
- **○ slate** — stored, no live terminal

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
    parser = argparse.ArgumentParser(description="Terminal operations console for OpenCode")
    parser.add_argument("--once", action="store_true", help="print one report and exit")
    parser.add_argument(
        "--destinations-json",
        action="store_true",
        help="print verified live OpenCode pane destinations as JSON and exit",
    )
    parser.add_argument("--url", default=None, help="OpenCode server URL")
    parser.add_argument("--limit", type=int, default=100, help="maximum stored sessions")
    parser.add_argument("--refresh", type=float, default=15, help="refresh interval in seconds")
    parser.add_argument(
        "--inline-tmux",
        action="store_true",
        help="attach selected tmux sessions inside this terminal",
    )
    parser.add_argument(
        "--projects-file",
        default=None,
        help="Markdown project catalog (defaults to the Obsidian projects note)",
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    source = DashboardSource(
        api_url=args.url,
        limit=args.limit,
        projects_file=args.projects_file,
        session_routes_file=args.session_routes_file,
        briefings_file=args.briefings_file,
    )
    if args.destinations_json:
        try:
            panes = read_live_opencode_panes()
            if not panes:
                raise SystemExit(
                    render_destinations_json(DashboardSnapshot(), ())
                )
            snapshot = asyncio.run(source.collect())
            panes = read_live_opencode_panes()
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
