from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from textual.widgets import DataTable, Input, TabbedContent, TabPane

from ocdeck.app import (
    AGENT_STATE_LABEL,
    NextStepsView,
    OCDeckApp,
    PROJECT_ACCENTS,
    build_destinations_payload,
    main as app_main,
    parse_args,
    project_accent,
    render_once,
    session_display_status,
    status_symbol,
    status_text,
)
from ocdeck.models import (
    DashboardSnapshot,
    NextStepRecord,
    ProjectBriefingRecord,
    ProjectRecord,
    ServiceRecord,
    SessionRecord,
    SystemMetrics,
)
from ocdeck.source import LiveOpenCodePane


class FakeSource:
    opencode_bin = None

    async def collect(self) -> DashboardSnapshot:
        session = SessionRecord(
            id="session-1",
            title="Build [/bold] \x1b]52;c;payload\x07 the dashboard",
            directory="/work/ocdeck",
            project_id="project-1",
            created_ms=100,
            updated_ms=200,
            status="busy",
            instance_count=2,
            terminals=("oc-session-1",),
            terminal_attached=True,
            permission="",
        )
        return DashboardSnapshot(
            sessions=(session,),
            projects=(
                ProjectRecord(
                    id="project-1",
                    directory="/work/ocdeck",
                    name="ocdeck",
                    session_count=1,
                    active_count=1,
                    attached_count=1,
                    instance_count=2,
                    updated_ms=200,
                ),
            ),
            services=(
                ServiceRecord(
                    unit="opencode-web.service",
                    label="OpenCode Web",
                    role="local API",
                    state="active",
                ),
            ),
            metrics=SystemMetrics(memory_percent=42, load_1m=0.5),
            connection="live",
            connection_detail="test [bold] API",
            unmapped_instance_count=1,
        )


class RecordingSource(FakeSource):
    def __init__(self, error: str = "") -> None:
        self.error = error
        self.renamed: list[tuple[str, str]] = []
        self.collect_calls = 0

    async def collect(self) -> DashboardSnapshot:
        self.collect_calls += 1
        snapshot = await super().collect()
        if not self.renamed or self.error:
            return snapshot
        session_id, title = self.renamed[-1]
        sessions = tuple(
            replace(session, title=title)
            if session.id == session_id
            else session
            for session in snapshot.sessions
        )
        return replace(snapshot, sessions=sessions)

    async def rename_session(self, session_id: str, title: str) -> str:
        self.renamed.append((session_id, title))
        return self.error


class MultiProjectSource:
    opencode_bin = None

    async def collect(self) -> DashboardSnapshot:
        sessions = (
            SessionRecord(
                id="s3",
                title="Beta main",
                directory="/work/beta",
                project_id="p2",
                created_ms=800,
                updated_ms=900,
            ),
            SessionRecord(
                id="s2",
                title="Alpha nested",
                directory="/work/alpha/packages/ui",
                project_id="p1",
                created_ms=400,
                updated_ms=500,
                status="busy",
                instance_count=1,
            ),
            SessionRecord(
                id="s1",
                title="Alpha root",
                directory="/work/alpha",
                project_id="p1",
                created_ms=90,
                updated_ms=100,
            ),
        )
        return DashboardSnapshot(
            sessions=sessions,
            projects=(
                ProjectRecord(
                    id="p1",
                    directory="/work/alpha",
                    name="alpha",
                    session_count=2,
                    active_count=1,
                    attached_count=1,
                    instance_count=1,
                    updated_ms=500,
                ),
                ProjectRecord(
                    id="p2",
                    directory="/work/beta",
                    name="beta",
                    session_count=1,
                    updated_ms=900,
                ),
            ),
            metrics=SystemMetrics(),
            connection="live",
            connection_detail="test",
        )


class BriefingSource:
    opencode_bin = None

    def __init__(
        self,
        *,
        report_status: str = "completed",
        include_briefing: bool = True,
        stale: bool = False,
        research_status: str = "running",
        has_evidence: bool = True,
    ) -> None:
        self.report_status = report_status
        self.include_briefing = include_briefing
        self.stale = stale
        self.research_status = research_status
        self.has_evidence = has_evidence

    async def collect(self) -> DashboardSnapshot:
        now = datetime.now(timezone.utc)
        generated_at = now - timedelta(days=2) if self.stale else now
        projects = (
            ProjectRecord(id="p1", directory="/work/alpha", name="Alpha"),
            ProjectRecord(id="p2", directory="/work/beta", name="Beta"),
        )
        briefings = ()
        if self.include_briefing:
            briefings = (
                ProjectBriefingRecord(
                    project_id="artifact-alpha",
                    project_path="/work/alpha",
                    name="Alpha",
                    assessment="at-risk",
                    summary="Secret [bold] summary\x1b]52;c;payload\x07",
                    confidence="medium",
                    evidence_at=generated_at if self.has_evidence else None,
                    completed_outputs=(
                        ("Private result", "/work/alpha/results/private.txt"),
                    ),
                    blockers=("Waiting for reviewer approval",),
                    next_steps=(
                        NextStepRecord(
                            id="step-now",
                            title="Inspect private evidence",
                            detail="Read the result; do not execute it",
                            state="now",
                        ),
                        NextStepRecord(
                            id="step-next",
                            title="Plan follow-up",
                            detail="Requires an explicit decision",
                            state="next",
                        ),
                    ),
                    evidence=("pytest passed",),
                    research_status=self.research_status,
                ),
            )
        return DashboardSnapshot(
            projects=projects,
            metrics=SystemMetrics(),
            connection="live",
            connection_detail="test",
            collected_at=now,
            briefings=briefings,
            briefing_report_id="private-report-id",
            briefing_generated_at=generated_at if self.report_status else None,
            briefing_status=self.report_status,
        )


def rendered_next_text(app: OCDeckApp) -> str:
    return str(app.query_one("#next-content").render())


def rendered_attention_text(app: OCDeckApp) -> str:
    return str(app.query_one("#attention").render())


class AppTests(unittest.IsolatedAsyncioTestCase):
    def test_briefings_cli_argument_is_available(self) -> None:
        args = parse_args(["--briefings-file", "/tmp/latest.json"])
        self.assertEqual(args.briefings_file, "/tmp/latest.json")

    def test_destinations_json_cli_argument_is_available(self) -> None:
        args = parse_args(["--destinations-json"])
        self.assertTrue(args.destinations_json)

    def test_destinations_payload_enriches_verified_panes(self) -> None:
        snapshot = asyncio.run(FakeSource().collect())
        panes = (
            LiveOpenCodePane(
                destination_id="dst_exact",
                pane_id="%42",
                session_id="session-1",
                session_name="private-tmux-name",
                window_index="2",
                pane_index="1",
                terminal_state="foreground",
            ),
        )

        payload = build_destinations_payload(snapshot, panes)

        self.assertEqual(payload["schema_version"], 1)
        destination = payload["destinations"][0]
        self.assertEqual(destination["destination_id"], "dst_exact")
        self.assertEqual(destination["pane_id"], "%42")
        self.assertEqual(destination["state"], "busy")
        self.assertEqual(destination["project"], "ocdeck")
        self.assertIn("pane 2.1", destination["label"])
        self.assertNotIn("private-tmux-name", json.dumps(destination))
        self.assertNotIn("\x1b", json.dumps(destination))

    def test_destinations_json_empty_result_precedes_non_tty_report(self) -> None:
        output = io.StringIO()
        with mock.patch("ocdeck.app.read_live_opencode_panes", return_value=()):
            with redirect_stdout(output), self.assertRaises(SystemExit) as exit_info:
                app_main(["--destinations-json"])

        self.assertEqual(exit_info.exception.code, 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"schema_version": 1, "destinations": []},
        )

    async def test_next_tab_cycles_shared_project_selection_and_views(self) -> None:
        app = OCDeckApp(BriefingSource(), auto_refresh=False)
        async with app.run_test(size=(100, 32)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("5")
            await pilot.pause()

            self.assertEqual(app.query_one("#tabs", TabbedContent).active, "next")
            self.assertIsInstance(app.screen.focused, NextStepsView)
            self.assertEqual(
                [pane.id for pane in app.query(TabPane)],
                ["overview", "services", "keys-view", "agents", "next"],
            )
            self.assertEqual(app.selected_project_id, "p1")
            self.assertIn("Secret [bold] summary", rendered_next_text(app))
            self.assertIn("confidence MEDIUM", rendered_next_text(app))

            await pilot.press("down")
            self.assertEqual(app.selected_project_id, "p2")
            self.assertIn("NO PROJECT BRIEFING", rendered_next_text(app))

            await pilot.press("j")
            self.assertEqual(app.selected_project_id, "p1")
            await pilot.press("up")
            self.assertEqual(app.selected_project_id, "p2")

            await pilot.press("ctrl+left")
            await pilot.pause()
            self.assertEqual(app.query_one("#tabs", TabbedContent).active, "agents")
            await pilot.press("ctrl+right")
            await pilot.pause()
            self.assertEqual(app.query_one("#tabs", TabbedContent).active, "next")

    async def test_next_animation_advances_without_sessions_and_only_redraws_active_tab(
        self,
    ) -> None:
        app = OCDeckApp(BriefingSource(), auto_refresh=False)
        async with app.run_test(size=(100, 32)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("5")
            await pilot.pause()
            app.activity_frame = 0
            app._render_next()
            before = rendered_next_text(app)

            app._advance_activity_animation()

            self.assertEqual(app.activity_frame, 1)
            self.assertNotEqual(before, rendered_next_text(app))

            await pilot.press("1")
            await pilot.pause()
            app.activity_frame = 2
            app._render_next()
            hidden_before = rendered_next_text(app)
            app._advance_activity_animation()
            self.assertEqual(app.activity_frame, 3)
            self.assertEqual(rendered_next_text(app), hidden_before)

    async def test_completed_research_does_not_animate_a_now_recommendation(
        self,
    ) -> None:
        app = OCDeckApp(
            BriefingSource(research_status="completed"), auto_refresh=False
        )
        async with app.run_test(size=(100, 32)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("5")
            await pilot.pause()
            app.activity_frame = 4
            app._render_next()
            before = rendered_next_text(app)

            app._advance_activity_animation()

            self.assertEqual(app.activity_frame, 4)
            self.assertEqual(rendered_next_text(app), before)

    async def test_next_renders_unknown_age_for_null_evidence(self) -> None:
        app = OCDeckApp(
            BriefingSource(has_evidence=False), auto_refresh=False
        )
        async with app.run_test(size=(100, 32)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("5")
            await pilot.pause()

            self.assertIn("evidence unknown", rendered_next_text(app))

    async def test_next_view_is_read_only_and_privacy_rerenders_immediately(self) -> None:
        app = OCDeckApp(BriefingSource(), auto_refresh=False)
        async with app.run_test(size=(100, 32)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("5")
            await pilot.pause()
            actions: list[str] = []
            app.action_new_session = lambda: actions.append("new")
            app.action_open_session = lambda: actions.append("open")

            await pilot.press("n", "enter", "o")
            self.assertEqual(actions, [])
            self.assertIn("Private result", rendered_next_text(app))
            self.assertNotIn("\x1b", rendered_next_text(app))

            await pilot.press("p")

            private_text = rendered_next_text(app)
            self.assertTrue(app.private)
            self.assertIn("PRIVACY MODE", private_text)
            self.assertNotIn("Secret", private_text)
            self.assertNotIn("Private result", private_text)
            self.assertNotIn("/work/alpha", private_text)

    async def test_direct_next_activation_guards_all_process_actions(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(100, 32)) as pilot:
            await asyncio.sleep(0.1)
            app.query_one("#tabs", TabbedContent).active = "next"
            await pilot.pause()
            self.assertIsInstance(app.screen.focused, NextStepsView)
            app.set_focus(None)

            effects: list[str] = []
            app._attach_live_terminal = lambda session: effects.append("attach") or False
            app._run_opencode = lambda *args, **kwargs: effects.append("run")
            app._launch_tmux = lambda *args, **kwargs: effects.append("launch")
            app._tmux_has_session = lambda name: effects.append("tmux") or True
            with mock.patch.object(app, "notify") as notify:
                app.action_open_session()
                app.action_open_auto()
                app.action_stop_job()
                app.action_new_session()
                app.action_new_terminal()

            self.assertEqual(effects, [])
            self.assertEqual(app.stop_confirm, "")
            self.assertEqual(notify.call_count, 5)
            self.assertTrue(
                all(call.args[0] == "NEXT is read-only" for call in notify.call_args_list)
            )

    async def test_next_view_remains_usable_in_a_narrow_terminal(self) -> None:
        app = OCDeckApp(BriefingSource(), auto_refresh=False)
        async with app.run_test(size=(42, 18)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("5")
            await pilot.pause()

            view = app.query_one("#next-view", NextStepsView)
            screenshot = app.export_screenshot()
            self.assertGreater(view.size.height, 0)
            self.assertIn("PORTFOLIO // NEXT STEPS", rendered_next_text(app))
            self.assertIn("SUMMARY", rendered_next_text(app))
            self.assertNotIn("\x1b]52", screenshot)
            await pilot.press("end")
            await pilot.pause()
            self.assertGreater(view.scroll_y, 0)

    async def test_next_view_distinguishes_empty_partial_and_stale_reports(self) -> None:
        missing_app = OCDeckApp(
            BriefingSource(report_status="", include_briefing=False),
            auto_refresh=False,
        )
        async with missing_app.run_test(size=(100, 30)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("5")
            await pilot.pause()
            self.assertIn("NO BRIEFING REPORT", rendered_next_text(missing_app))

        partial_app = OCDeckApp(
            BriefingSource(report_status="partial", stale=True),
            auto_refresh=False,
        )
        async with partial_app.run_test(size=(100, 30)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("5")
            await pilot.pause()
            partial_text = rendered_next_text(partial_app)
            self.assertIn("PARTIAL REPORT", partial_text)
            self.assertIn("mixed completed and failed outcomes", partial_text)
            self.assertIn("STALE REPORT", rendered_next_text(partial_app))
            await pilot.press("down")
            unmatched_text = rendered_next_text(partial_app)
            self.assertIn("NO PROJECT BRIEFING", unmatched_text)
            self.assertNotIn("absent from the partial report", unmatched_text)

    def test_detected_terminal_displays_as_open(self) -> None:
        session = SessionRecord(
            id="s-open",
            title="Open terminal",
            directory="/work/open",
            project_id="p1",
            created_ms=1,
            updated_ms=1,
            instance_count=1,
        )
        self.assertEqual(session_display_status(session), "open")
        self.assertEqual(AGENT_STATE_LABEL["open"], "IDLE")
        self.assertEqual(status_text("open"), status_text("open", 7))
        self.assertEqual(status_symbol("open"), "○")

    async def test_stop_job_requires_confirmation_then_kills(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("1")
            await pilot.pause()
            killed: list[str] = []
            sessions = app.query_one("#sessions-table", DataTable)
            sessions.move_cursor(row=0)
            await pilot.pause()

            def fake_kill(name: str) -> bool:
                killed.append(name)
                return True

            app._tmux_kill_session = fake_kill
            app._tmux_has_session = lambda name: name == "oc-s2"

            app.action_stop_job()
            self.assertEqual(killed, [])
            self.assertEqual(app.stop_confirm, "s2")

            app.action_stop_job()
            await asyncio.sleep(0.1)
            self.assertEqual(killed, ["oc-s2"])
            self.assertEqual(app.stop_confirm, "")

    async def test_stop_job_without_live_tmux_session_is_noop(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            await asyncio.sleep(0.1)
            killed: list[str] = []
            app._tmux_kill_session = lambda name: killed.append(name) or True
            app._tmux_has_session = lambda name: False

            app.action_stop_job()
            await asyncio.sleep(0.1)
            self.assertEqual(killed, [])
            self.assertEqual(app.stop_confirm, "")

    async def test_dashboard_renders_and_toggles_privacy(self) -> None:
        app = OCDeckApp(FakeSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("1")
            await pilot.pause()
            sessions = app.query_one("#sessions-table", DataTable)
            projects = app.query_one("#projects-table", DataTable)
            self.assertEqual(sessions.row_count, 1)
            self.assertEqual(projects.row_count, 1)
            self.assertTrue(app.screen.has_class("compact"))
            self.assertFalse(app.query_one("#detail-pane").display)
            screenshot = app.export_screenshot()
            self.assertNotIn("\x1b", screenshot)
            self.assertEqual(str(sessions.get_row("session-1")[1]), "2")
            self.assertEqual(str(sessions.get_row("session-1")[3]), "ocdeck")
            await pilot.press("p")
            await asyncio.sleep(0.1)
            self.assertTrue(app.private)
            app.query_one("#session-search").value = "no-match"
            await asyncio.sleep(0.1)
            self.assertEqual(sessions.row_count, 0)
            self.assertIsNone(app._current_session())
            app.query_one("#session-search").value = ""
            await pilot.resize_terminal(120, 28)
            await asyncio.sleep(0.1)
            self.assertTrue(app.screen.has_class("compact"))
            self.assertFalse(app.query_one("#detail-pane").display)
            self.assertLessEqual(sessions.virtual_size.width, sessions.size.width)
            await pilot.resize_terminal(60, 18)
            await asyncio.sleep(0.2)
            self.assertTrue(app.screen.has_class("compact"))
            self.assertTrue(app.screen.has_class("narrow"))
            self.assertTrue(app.screen.has_class("short"))
            self.assertTrue(app.screen.has_class("tiny"))
            self.assertFalse(app.query_one("#detail-pane").display)
            self.assertGreater(sessions.size.height, 0)

    async def test_project_selection_scopes_session_list(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            await asyncio.sleep(0.1)
            sessions = app.query_one("#sessions-table", DataTable)
            self.assertEqual(sessions.row_count, 3)

            app.action_toggle_filter()
            await asyncio.sleep(0.1)
            self.assertEqual(sessions.row_count, 2)
            self.assertTrue(app.project_filter)
            self.assertEqual(
                {row_key.value for row_key in sessions.rows},
                {"s1", "s2"},
            )

            app.selected_project_id = "p2"
            app._render_sessions()
            await asyncio.sleep(0.1)
            self.assertEqual(sessions.row_count, 1)

            app.action_toggle_filter()
            await asyncio.sleep(0.1)
            self.assertFalse(app.project_filter)
            self.assertEqual(sessions.row_count, 3)

    async def test_refresh_rebuild_does_not_activate_project_scope(self) -> None:
        source = MultiProjectSource()
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            projects = app.query_one("#projects-table", DataTable)
            sessions = app.query_one("#sessions-table", DataTable)
            projects.focus()

            app._apply_snapshot(await source.collect())
            await pilot.pause()

            self.assertFalse(app.project_filter)
            self.assertEqual(app.selected_project_id, "p1")
            self.assertEqual(sessions.row_count, 3)

    async def test_refresh_preserves_nonfirst_project_and_session_selection(self) -> None:
        source = MultiProjectSource()
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("1")
            await pilot.pause()
            projects = app.query_one("#projects-table", DataTable)
            sessions = app.query_one("#sessions-table", DataTable)

            sessions.move_cursor(row=0)
            await pilot.pause()
            projects.move_cursor(row=1)
            await pilot.pause()
            projects.focus()

            self.assertEqual(app.selected_session_id, "s2")
            self.assertEqual(app.selected_project_id, "p2")
            self.assertFalse(app.project_filter)

            app._apply_snapshot(await source.collect())
            await pilot.pause()

            self.assertFalse(app.project_filter)
            self.assertEqual(app.selected_project_id, "p2")
            self.assertEqual(app.selected_session_id, "s2")
            self.assertEqual(
                [str(row_key.value) for row_key in sessions.rows],
                ["s2", "s3", "s1"],
            )
            selected = sessions.coordinate_to_cell_key(sessions.cursor_coordinate)
            self.assertEqual(str(selected.row_key.value), "s2")

    async def test_subdirectory_sessions_match_project_root(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            await asyncio.sleep(0.1)
            project = app.project_by_id["p1"]
            nested = app.session_by_id["s2"]
            self.assertTrue(app._session_matches_project(nested, project))
            beta = app.session_by_id["s3"]
            self.assertFalse(app._session_matches_project(beta, project))
            self.assertTrue(app._session_matches_project(beta, None))

    def test_routed_session_opens_from_canonical_project_root(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_directory = root / "old-home"
            project_directory = root / "agents-start"
            old_directory.mkdir()
            project_directory.mkdir()
            project = ProjectRecord(
                id="agents-start",
                directory=str(project_directory),
                name="Agents Start",
            )
            session = SessionRecord(
                id="moved",
                title="Moved task",
                directory=str(old_directory),
                project_id=project.id,
                created_ms=1,
                updated_ms=1,
            )
            app.project_by_id = {project.id: project}

            self.assertEqual(app._session_directory(session), str(project_directory))

            nested = project_directory / "dashboard"
            nested.mkdir()
            session = SessionRecord(
                id="nested",
                title="Nested task",
                directory=str(nested),
                project_id=project.id,
                created_ms=1,
                updated_ms=1,
            )
            self.assertEqual(app._session_directory(session), str(nested))

    async def test_highlight_change_refilters_while_scoped(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            sessions = app.query_one("#sessions-table", DataTable)
            projects = app.query_one("#projects-table", DataTable)
            projects.focus()
            await pilot.press("down")
            await asyncio.sleep(0.1)
            self.assertTrue(app.project_filter)
            self.assertEqual(app.selected_project_id, "p2")
            self.assertEqual({key.value for key in sessions.rows}, {"s3"})
            await pilot.press("up")
            await asyncio.sleep(0.1)
            self.assertEqual(app.selected_project_id, "p1")
            self.assertEqual({key.value for key in sessions.rows}, {"s1", "s2"})

    async def test_keyboard_navigation_moves_between_rows_panes_and_views(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            self.assertEqual(app.query_one("#tabs", TabbedContent).active, "agents")
            self.assertIs(app.screen.focused, app.query_one("#agents-table"))
            await pilot.press("1")
            await pilot.pause()
            projects = app.query_one("#projects-table", DataTable)
            sessions = app.query_one("#sessions-table", DataTable)

            self.assertIs(app.screen.focused, sessions)
            sessions.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("j")
            self.assertEqual(app.selected_session_id, "s3")
            await pilot.press("k")
            self.assertEqual(app.selected_session_id, "s2")

            self.assertEqual(
                [str(row_key.value) for row_key in sessions.rows],
                ["s2", "s3", "s1"],
            )

            await pilot.press("left")
            self.assertIs(app.screen.focused, projects)
            await pilot.press("down")
            self.assertEqual(app.selected_project_id, "p2")
            self.assertEqual({key.value for key in sessions.rows}, {"s3"})
            await pilot.press("enter")
            await pilot.pause()
            self.assertIs(app.screen.focused, sessions)

            await pilot.press("2")
            await pilot.pause()
            self.assertEqual(app.query_one("#tabs", TabbedContent).active, "services")
            self.assertIs(app.screen.focused, app.query_one("#services-table"))
            await pilot.press("ctrl+left")
            await pilot.pause()
            self.assertEqual(app.query_one("#tabs", TabbedContent).active, "overview")
            self.assertIs(app.screen.focused, sessions)

            await pilot.press("3")
            await pilot.pause()
            self.assertEqual(app.query_one("#tabs", TabbedContent).active, "keys-view")
            self.assertIs(app.screen.focused, app.query_one("#key-reference"))

    async def test_search_keeps_normal_text_input_keys(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("/")
            search = app.query_one("#session-search", Input)
            self.assertIs(app.screen.focused, search)
            await pilot.press("h", "j", "k", "l")
            self.assertEqual(search.value, "hjkl")
            await pilot.press("tab")
            self.assertIs(app.screen.focused, app.query_one("#sessions-table"))

    async def test_scoped_search_ranks_project_matches_before_other_projects(
        self,
    ) -> None:
        class SharedKeywordSource(MultiProjectSource):
            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                sessions = tuple(
                    replace(
                        session,
                        title=(
                            "Shared beta task"
                            if session.id == "s3"
                            else "Shared alpha task"
                            if session.id == "s2"
                            else session.title
                        ),
                    )
                    for session in snapshot.sessions
                )
                return replace(snapshot, sessions=sessions)

        app = OCDeckApp(SharedKeywordSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            sessions = app.query_one("#sessions-table", DataTable)
            app.action_toggle_filter()
            self.assertEqual(
                [str(row_key.value) for row_key in sessions.rows], ["s2", "s1"]
            )

            await pilot.press("/")
            await pilot.press("s", "h", "a", "r", "e", "d")
            await pilot.pause()

            self.assertEqual(
                [str(row_key.value) for row_key in sessions.rows], ["s2", "s3"]
            )
            self.assertIn(
                "ALPHA FIRST", str(app.query_one("#sessions-title").render())
            )

            app.query_one("#session-search", Input).value = ""
            await pilot.pause()
            self.assertEqual(
                [str(row_key.value) for row_key in sessions.rows], ["s2", "s1"]
            )

    async def test_search_enter_opens_first_matching_session(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            opened: list[list[str]] = []
            app._run_opencode = (
                lambda arguments, **kwargs: opened.append(list(arguments))
            )

            await pilot.press("/")
            await pilot.press("b", "e", "t", "a")
            await pilot.press("enter")
            await pilot.pause()

            self.assertIs(app.screen.focused, app.query_one("#sessions-table"))
            self.assertEqual(opened, [["/work/beta", "--session", "s3"]])

    async def test_search_down_moves_into_matching_results(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("/")
            await pilot.press("a", "l", "p", "h", "a")
            await pilot.press("down")
            await pilot.pause()

            sessions = app.query_one("#sessions-table", DataTable)
            self.assertIs(app.screen.focused, sessions)
            self.assertEqual(app._current_session(), app.session_by_id["s2"])

    async def test_clicking_session_name_opens_inline_rename_editor(self) -> None:
        app = OCDeckApp(RecordingSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            table = app.query_one("#sessions-table", DataTable)
            row_key = next(iter(table.rows))
            table.clicked_column_index = app.session_title_index
            app.on_row_selected(DataTable.RowSelected(table, 0, row_key))
            await pilot.pause()

            editor = app.query_one("#session-rename", Input)
            search = app.query_one("#session-search", Input)
            self.assertTrue(editor.display)
            self.assertFalse(search.display)
            self.assertIs(app.screen.focused, editor)
            self.assertEqual(app.renaming_session_id, "session-1")
            self.assertIn("the dashboard", editor.value)

            await pilot.press("escape")
            self.assertFalse(editor.display)
            self.assertTrue(search.display)
            self.assertEqual(app.renaming_session_id, "")
            self.assertIs(app.screen.focused, table)
            self.assertIsNone(table.clicked_column_index)
            self.assertEqual(app.source.renamed, [])

    async def test_selecting_other_cells_does_not_open_the_session(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            opened: list[list[str]] = []
            app._run_opencode = (
                lambda arguments, **kwargs: opened.append(list(arguments))
            )
            table = app.query_one("#sessions-table", DataTable)
            row_key = next(iter(table.rows))
            table.clicked_column_index = None
            app.on_row_selected(DataTable.RowSelected(table, 0, row_key))

            self.assertEqual(opened, [])
            self.assertEqual(app.selected_session_id, "s3")
            self.assertFalse(app.query_one("#session-rename", Input).display)
            self.assertEqual(app.renaming_session_id, "")

    async def test_mobile_title_selection_opens_instead_of_renaming(self) -> None:
        app = OCDeckApp(RecordingSource(), auto_refresh=False, inline_tmux=True)
        async with app.run_test(size=(50, 30)) as pilot:
            await asyncio.sleep(0.1)
            opened: list[str] = []
            app._attach_live_terminal = lambda session: opened.append(session.id) or True
            table = app.query_one("#sessions-table", DataTable)
            row_key = next(iter(table.rows))
            table.clicked_column_index = app.session_title_index

            app.on_row_selected(DataTable.RowSelected(table, 0, row_key))
            await pilot.pause()

            self.assertEqual(opened, ["session-1"])
            self.assertFalse(app.query_one("#session-rename", Input).display)

    async def test_mobile_session_opens_on_first_click(self) -> None:
        class ActiveSource(MultiProjectSource):
            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                sessions = tuple(
                    replace(
                        session,
                        instance_count=1,
                        terminals=(f"oc-{session.id}",),
                    )
                    for session in snapshot.sessions
                )
                return replace(snapshot, sessions=sessions)

        app = OCDeckApp(ActiveSource(), auto_refresh=False, inline_tmux=True)
        async with app.run_test(size=(50, 30)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("1")
            await pilot.pause()
            opened: list[str] = []
            app._attach_live_terminal = lambda session: opened.append(session.id) or True
            table = app.query_one("#sessions-table", DataTable)
            row_keys = [str(key.value) for key in table.rows]
            second_row = table._get_row_region(1)

            await pilot.click(table, offset=(1, second_row.y))
            await pilot.pause()

            self.assertEqual(opened, [row_keys[1]])

    async def test_mobile_subagent_uses_parent_input_terminal(self) -> None:
        class SubagentSource(MultiProjectSource):
            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                parent = replace(
                    snapshot.sessions[0],
                    id="parent",
                    title="Parent task",
                    instance_count=1,
                    terminals=("oc-parent",),
                )
                child = replace(
                    snapshot.sessions[1],
                    id="child",
                    title="Investigate issue",
                    parent_id=parent.id,
                    agent_parent_id=parent.id,
                    instance_count=1,
                    terminals=("oc-child",),
                )
                return replace(snapshot, sessions=(child, parent))

        app = OCDeckApp(SubagentSource(), auto_refresh=False, inline_tmux=True)
        async with app.run_test(size=(50, 30)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("1")
            await pilot.pause()
            attached: list[tuple[str, str | None]] = []
            app._attach_live_terminal = (
                lambda session, title=None: attached.append((session.id, title)) or True
            )
            app.selected_session_id = "child"
            await pilot.press("down")

            app.action_open_session()

            self.assertEqual(attached, [("parent", "Investigate issue")])

    async def test_mobile_session_picker_shows_active_agent_columns(self) -> None:
        app = OCDeckApp(RecordingSource(), auto_refresh=False, inline_tmux=True)
        async with app.run_test(size=(50, 30)):
            await asyncio.sleep(0.1)
            sessions = app.query_one("#sessions-table", DataTable)

            self.assertTrue(app.screen.has_class("mobile"))
            self.assertEqual(len(sessions.columns), 2)
            self.assertEqual(app.session_title_index, 1)
            self.assertGreaterEqual(sessions.columns[app.session_title_column].width, 35)
            self.assertEqual(str(sessions.get_row("session-1")[0]), "RUNNING")
            self.assertIn("ACTIVE AGENTS", str(app.query_one("#sessions-title").render()))
            self.assertFalse(app.query_one("#projects-pane").display)
            self.assertFalse(app.query_one("#detail-pane").display)

    async def test_submitting_rename_updates_row_and_persists_via_api(self) -> None:
        source = RecordingSource()
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app._begin_rename("session-1")
            editor = app.query_one("#session-rename", Input)
            editor.value = "Renamed by click"
            await pilot.press("enter")
            await asyncio.sleep(0.3)

            self.assertEqual(source.renamed, [("session-1", "Renamed by click")])
            self.assertEqual(
                app.session_by_id["session-1"].title, "Renamed by click"
            )
            table = app.query_one("#sessions-table", DataTable)
            self.assertIn("Renamed by click", str(table.get_row("session-1")[2]))
            self.assertFalse(editor.display)
            self.assertTrue(app.query_one("#session-search", Input).display)

    async def test_failed_rename_notifies_and_refresh_restores_title(self) -> None:
        source = RecordingSource(error="API unavailable")
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            original = app.session_by_id["session-1"].title
            app._begin_rename("session-1")
            app.query_one("#session-rename", Input).value = "Temporary name"
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("enter")
                await asyncio.sleep(0.4)

            failures = [
                call
                for call in notify.call_args_list
                if call.args and str(call.args[0]).startswith("Rename failed")
            ]
            self.assertEqual(len(failures), 1)
            self.assertEqual(source.renamed, [("session-1", "Temporary name")])
            self.assertEqual(app.session_by_id["session-1"].title, original)
            self.assertGreaterEqual(source.collect_calls, 2)

    async def test_empty_or_unchanged_names_skip_the_api(self) -> None:
        source = RecordingSource()
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            current = app.session_by_id["session-1"].title

            app._begin_rename("session-1")
            app.query_one("#session-rename", Input).value = ""
            await pilot.press("enter")

            app._begin_rename("session-1")
            app.query_one("#session-rename", Input).value = current
            await pilot.press("enter")

            self.assertEqual(source.renamed, [])
            self.assertEqual(app.renaming_session_id, "")
            self.assertFalse(app.query_one("#session-rename", Input).display)

    async def test_privacy_mode_blocks_the_rename_editor(self) -> None:
        app = OCDeckApp(RecordingSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app.private = True
            with mock.patch.object(app, "notify") as notify:
                app._begin_rename("session-1")

            self.assertFalse(app.query_one("#session-rename", Input).display)
            self.assertEqual(app.renaming_session_id, "")
            notified = [call.args[0] for call in notify.call_args_list if call.args]
            self.assertTrue(any("privacy" in str(item).lower() for item in notified))

    async def test_rerender_preserves_keyboard_selection(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("1")
            await pilot.pause()
            sessions = app.query_one("#sessions-table", DataTable)
            sessions.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("down")
            self.assertEqual(app.selected_session_id, "s3")
            app.action_privacy()
            self.assertEqual(app._current_session(), app.session_by_id["s3"])

    async def test_open_auto_passes_permission_flag(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("1")
            await pilot.pause()
            captured: list[tuple[list[str], str]] = []
            sessions = app.query_one("#sessions-table", DataTable)
            sessions.move_cursor(row=0)
            await pilot.pause()

            def capture_run(
                arguments, tmux_name=None, project_id=None, title=""
            ) -> None:
                captured.append((list(arguments), title))

            app._run_opencode = capture_run

            app.action_open_auto()
            self.assertEqual(
                captured[-1],
                (["/work/alpha/packages/ui", "--session", "s2", "--auto"], "Alpha nested"),
            )

            app.action_open_session()
            self.assertEqual(
                captured[-1],
                (["/work/alpha/packages/ui", "--session", "s2"], "Alpha nested"),
            )

    async def test_open_attaches_to_live_terminal_instead_of_spawning(self) -> None:
        app = OCDeckApp(FakeSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            await asyncio.sleep(0.1)
            launched: list[tuple[tuple, dict]] = []
            attached: list[tuple[str, str]] = []
            app._run_opencode = lambda *args, **kwargs: launched.append((args, kwargs))
            app._raise_existing_window = lambda name, title="": False
            app._tmux_attach = (
                lambda name, directory, title="": attached.append((name, title)) or True
            )

            app.action_open_session()

            session = app.session_by_id["session-1"]
            self.assertEqual(attached, [("oc-session-1", session.title)])
            self.assertEqual(launched, [])

    async def test_open_focuses_exact_existing_tmux_viewer(self) -> None:
        app = OCDeckApp(FakeSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            await asyncio.sleep(0.1)
            focused: list[tuple[str, str]] = []
            attached: list[str] = []
            app._raise_existing_window = (
                lambda name, title="": focused.append((name, title)) or True
            )
            app._tmux_attach = (
                lambda name, directory, title="": attached.append(name) or True
            )

            app.action_open_session()

            session = app.session_by_id["session-1"]
            self.assertEqual(focused, [("oc-session-1", session.title)])
            self.assertEqual(attached, [])

    async def test_focus_service_failure_does_not_duplicate_tmux_viewer(self) -> None:
        app = OCDeckApp(FakeSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            await asyncio.sleep(0.1)
            attached: list[str] = []
            launched: list[tuple[tuple, dict]] = []
            app._raise_existing_window = lambda name, title="": None
            app._tmux_attach = (
                lambda name, directory, title="": attached.append(name) or True
            )
            app._run_opencode = lambda *args, **kwargs: launched.append((args, kwargs))

            app.action_open_session()

            self.assertEqual(attached, [])
            self.assertEqual(launched, [])

    async def test_shell_focus_result_is_parsed(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            with mock.patch("ocdeck.app.subprocess.run") as run:
                run.return_value = mock.Mock(returncode=0, stdout=b"(true,)\n")
                self.assertTrue(app._focus_tmux_via_shell("oc-s3"))

                run.return_value = mock.Mock(returncode=0, stdout=b"(false,)\n")
                self.assertFalse(app._focus_tmux_via_shell("oc-s3"))

                run.return_value = mock.Mock(returncode=1, stdout=b"")
                self.assertIsNone(app._focus_tmux_via_shell("oc-s3"))

    async def test_direct_ptyxis_focus_result_is_parsed(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            with mock.patch("ocdeck.app.subprocess.run") as run:
                run.return_value = mock.Mock(returncode=0)
                self.assertTrue(app._focus_tmux_via_ptyxis("oc-s3"))

                run.return_value = mock.Mock(returncode=3)
                self.assertFalse(app._focus_tmux_via_ptyxis("oc-s3"))

                run.return_value = mock.Mock(returncode=2)
                self.assertIsNone(app._focus_tmux_via_ptyxis("oc-s3"))

    async def test_wayland_xdotool_miss_does_not_imply_missing_viewer(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            app._focus_tmux_via_shell = lambda name: None
            app._focus_tmux_via_ptyxis = lambda name: None
            with mock.patch.dict(
                os.environ,
                {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
                clear=False,
            ):
                with mock.patch("ocdeck.app.subprocess.run") as run:
                    run.return_value = mock.Mock(returncode=1, stdout=b"")
                    self.assertIsNone(app._raise_existing_window("oc-s3"))

    async def test_agents_board_renders_navigates_and_opens(self) -> None:
        app = OCDeckApp(FakeSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app.action_show_tab("agents")
            await pilot.pause()
            agents = app.query_one("#agents-table", DataTable)
            self.assertEqual(app.query_one("#tabs", TabbedContent).active, "agents")
            self.assertIs(app.screen.focused, agents)
            self.assertEqual(agents.row_count, 1)
            self.assertIn("RUNNING", str(agents.get_row("session-1")[0]))
            self.assertIn("OPEN", str(agents.get_row("session-1")[1]))

            opened: list[str] = []
            app._attach_live_terminal = lambda session: opened.append(session.id) or True
            await pilot.press("enter")
            self.assertEqual(opened, ["session-1"])

    async def test_mobile_agent_opens_on_first_click(self) -> None:
        app = OCDeckApp(FakeSource(), auto_refresh=False, inline_tmux=True)
        async with app.run_test(size=(50, 30)) as pilot:
            await asyncio.sleep(0.1)
            agents = app.query_one("#agents-table", DataTable)
            opened: list[str] = []
            app._attach_live_terminal = lambda session: opened.append(session.id) or True
            session_column = agents._get_column_region(app.agent_session_index)
            first_row = agents._get_row_region(0)

            await pilot.click(
                agents,
                offset=(session_column.x + 1, first_row.y),
            )
            await pilot.pause()

            self.assertEqual(opened, ["session-1"])

    async def test_mobile_parent_name_opens_while_arrow_expands(self) -> None:
        class SubagentSource(FakeSource):
            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                parent = replace(snapshot.sessions[0], id="parent", title="Home Agent")
                child = replace(
                    parent,
                    id="child",
                    title="Project worker",
                    agent_parent_id=parent.id,
                    status="busy",
                    terminals=("oc-child",),
                )
                return replace(snapshot, sessions=(child, parent))

        app = OCDeckApp(SubagentSource(), auto_refresh=False, inline_tmux=True)
        async with app.run_test(size=(50, 30)) as pilot:
            await asyncio.sleep(0.1)
            agents = app.query_one("#agents-table", DataTable)
            opened: list[str] = []
            app._attach_live_terminal = lambda session: opened.append(session.id) or True
            session_column = agents._get_column_region(app.agent_session_index)
            first_row = agents._get_row_region(0)

            await pilot.click(agents, offset=(session_column.x + 7, first_row.y))
            await pilot.pause()
            self.assertEqual(opened, ["parent"])
            self.assertEqual([str(key.value) for key in agents.rows], ["parent"])

            await pilot.click(agents, offset=(session_column.x + 2, first_row.y))
            await pilot.pause()
            self.assertTrue(agents.clicked_expand_control)
            self.assertEqual(
                [str(key.value) for key in agents.rows], ["parent", "child"]
            )

    async def test_agents_board_expands_live_subagents_with_left_arrow(self) -> None:
        class SubagentSource(FakeSource):
            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                parent = replace(
                    snapshot.sessions[0],
                    id="parent",
                    title="Home Agent",
                    last_interaction_ms=200,
                    status="idle",
                    instance_count=1,
                )
                child = replace(
                    parent,
                    id="child",
                    title="Project worker",
                    agent_parent_id=parent.id,
                    last_interaction_ms=300,
                    status="busy",
                    terminals=("oc-child",),
                    terminal_attached=False,
                )
                return replace(snapshot, sessions=(child, parent))

        app = OCDeckApp(SubagentSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app.action_show_tab("agents")
            await pilot.pause()
            agents = app.query_one("#agents-table", DataTable)

            self.assertEqual(
                [str(row_key.value) for row_key in agents.rows], ["parent"]
            )
            self.assertIn("▸[1]", str(agents.get_row("parent")[2]))
            self.assertIn("RUNNING", str(agents.get_row("parent")[0]))

            session_column = agents._get_column_region(app.agent_session_index)
            first_row = agents._get_row_region(0)
            await pilot.click(
                agents,
                offset=(session_column.x + 1, first_row.y),
            )
            await pilot.pause()
            self.assertEqual(
                [str(row_key.value) for row_key in agents.rows],
                ["parent", "child"],
            )
            self.assertIn("▾[1]", str(agents.get_row("parent")[2]))

            session_column = agents._get_column_region(app.agent_session_index)
            await pilot.click(
                agents,
                offset=(session_column.x + 1, first_row.y),
            )
            await pilot.pause()
            self.assertEqual(
                [str(row_key.value) for row_key in agents.rows],
                ["parent"],
            )

            await pilot.press("left")
            await pilot.pause()
            self.assertEqual(
                [str(row_key.value) for row_key in agents.rows],
                ["parent", "child"],
            )
            self.assertIn("▾[1]", str(agents.get_row("parent")[2]))
            self.assertIn("└", str(agents.get_row("child")[2]))
            self.assertIn("BG TMUX", str(agents.get_row("child")[1]))

            opened: list[str] = []
            app._attach_live_terminal = lambda session: opened.append(session.id) or True
            await pilot.press("down")
            await pilot.press("enter")
            self.assertEqual(opened, ["child"])

            await pilot.press("left")
            await pilot.pause()
            self.assertEqual(
                [str(row_key.value) for row_key in agents.rows], ["parent"]
            )
            self.assertEqual(app.selected_session_id, "parent")

    async def test_agents_board_marks_detached_tmux_as_background(self) -> None:
        class BackgroundSource(FakeSource):
            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                return replace(
                    snapshot,
                    sessions=(
                        replace(snapshot.sessions[0], terminal_attached=False),
                    ),
                )

        app = OCDeckApp(BackgroundSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app.action_show_tab("agents")
            await pilot.pause()
            row = app.query_one("#agents-table", DataTable).get_row("session-1")
            self.assertIn("BG TMUX", str(row[1]))

    async def test_agents_board_falls_back_when_updated_timestamp_is_missing(self) -> None:
        class MissingUpdateSource(FakeSource):
            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                return replace(
                    snapshot,
                    sessions=(
                        replace(
                            snapshot.sessions[0],
                            updated_ms=0,
                            last_interaction_ms=int(
                                datetime.now(timezone.utc).timestamp() * 1000
                            ),
                        ),
                    ),
                )

        app = OCDeckApp(MissingUpdateSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app.action_show_tab("agents")
            await pilot.pause()
            row = app.query_one("#agents-table", DataTable).get_row("session-1")
            self.assertNotIn("unknown", str(row[4]).lower())

    async def test_agents_board_orders_by_latest_user_prompt(self) -> None:
        class PromptOrderSource(FakeSource):
            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                busy = replace(
                    snapshot.sessions[0],
                    updated_ms=400,
                    last_interaction_ms=100,
                )
                newer_prompt = replace(
                    busy,
                    id="session-2",
                    title="Prompted more recently",
                    status="idle",
                    updated_ms=200,
                    last_interaction_ms=300,
                )
                return replace(snapshot, sessions=(busy, newer_prompt))

        app = OCDeckApp(PromptOrderSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app.action_show_tab("agents")
            await pilot.pause()
            agents = app.query_one("#agents-table", DataTable)
            self.assertEqual(
                [str(row_key.value) for row_key in agents.rows],
                ["session-2", "session-1"],
            )

    async def test_agents_board_preserves_selection_when_prompt_order_changes(self) -> None:
        class ReorderingSource(FakeSource):
            reordered = False

            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                first = replace(
                    snapshot.sessions[0],
                    last_interaction_ms=300 if self.reordered else 100,
                )
                second = replace(
                    first,
                    id="session-2",
                    title="Second agent",
                    last_interaction_ms=100 if self.reordered else 300,
                )
                return replace(snapshot, sessions=(first, second))

        source = ReorderingSource()
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app.action_show_tab("agents")
            await pilot.pause()
            agents = app.query_one("#agents-table", DataTable)
            await pilot.press("down")
            self.assertEqual(app.selected_session_id, "session-1")

            source.reordered = True
            app._apply_snapshot(await source.collect())
            await pilot.pause()

            row_key = agents.coordinate_to_cell_key(agents.cursor_coordinate).row_key
            self.assertEqual(str(row_key.value), "session-1")
            self.assertEqual(app.selected_session_id, "session-1")

    async def test_agents_board_reflects_assistant_turn_metadata(self) -> None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        class TurnStateSource(FakeSource):
            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                running = replace(
                    snapshot.sessions[0],
                    status="idle",
                    assistant_active=True,
                )
                finished = replace(
                    running,
                    id="session-2",
                    title="Finished worker",
                    instance_count=1,
                    terminals=("oc-session-2",),
                    assistant_active=False,
                    assistant_done_ms=now_ms - 30_000,
                    last_interaction_ms=now_ms - 60_000,
                )
                stopped = replace(
                    running,
                    id="session-3",
                    title="Stopped worker",
                    instance_count=0,
                    terminals=(),
                    terminal_attached=False,
                )
                return replace(snapshot, sessions=(running, finished, stopped))

        app = OCDeckApp(TurnStateSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app.action_show_tab("agents")
            await pilot.pause()
            agents = app.query_one("#agents-table", DataTable)
            self.assertEqual(
                [str(row_key.value) for row_key in agents.rows],
                ["session-2", "session-1"],
            )
            self.assertIn("STALLED", str(agents.get_row("session-1")[0]))
            self.assertIn("!", str(agents.get_row("session-1")[0]))
            self.assertIn("REVIEW", str(agents.get_row("session-2")[0]))
            self.assertIn("waiting for you", str(agents.get_row("session-2")[5]))
            # A stopped terminal with orphaned in-flight metadata leaves the
            # board instead of lingering as RUNNING.
            self.assertNotIn(
                "session-3", [str(key.value) for key in agents.rows]
            )

    async def test_activity_pulse_updates_without_full_collect(self) -> None:
        class PulsedSource(FakeSource):
            def __init__(self) -> None:
                self.collect_calls = 0
                self.pulse_calls = 0
                self.permission_label = ""

            async def collect(self) -> DashboardSnapshot:
                self.collect_calls += 1
                return await super().collect()

            async def collect_activity(self) -> DashboardSnapshot:
                self.pulse_calls += 1
                snapshot = await super().collect()
                session = replace(
                    snapshot.sessions[0], permission=self.permission_label
                )
                return replace(snapshot, sessions=(session,))

        source = PulsedSource()
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app.action_show_tab("agents")
            await pilot.pause()
            agents = app.query_one("#agents-table", DataTable)
            self.assertEqual(source.collect_calls, 1)

            source.permission_label = "bash cargo test"
            app._request_activity_refresh()
            await pilot.pause()
            await asyncio.sleep(0.1)
            await pilot.pause()
            self.assertEqual(source.pulse_calls, 1)
            self.assertEqual(source.collect_calls, 1)
            row = agents.get_row("session-1")
            self.assertIn("PERMISSION", str(row[0]))

    async def test_stale_activity_snapshot_cannot_overwrite_new_full_refresh(self) -> None:
        class OutOfOrderSource(FakeSource):
            def __init__(self) -> None:
                self.full_calls = 0
                self.pulse_started = asyncio.Event()
                self.release_pulse = asyncio.Event()

            async def collect(self) -> DashboardSnapshot:
                self.full_calls += 1
                snapshot = await super().collect()
                label = "initial" if self.full_calls == 1 else "fresh-full"
                return replace(snapshot, connection_detail=label)

            async def collect_activity(self) -> DashboardSnapshot:
                self.pulse_started.set()
                await self.release_pulse.wait()
                snapshot = await super().collect()
                return replace(snapshot, connection_detail="stale-pulse")

        source = OutOfOrderSource()
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app._request_activity_refresh()
            await asyncio.wait_for(source.pulse_started.wait(), timeout=1)
            app._request_refresh(force=True)
            for _ in range(20):
                if app.snapshot.connection_detail == "fresh-full":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(app.snapshot.connection_detail, "fresh-full")

            source.release_pulse.set()
            await pilot.pause()
            await asyncio.sleep(0.05)
            self.assertEqual(app.snapshot.connection_detail, "fresh-full")

    async def test_activity_pulse_skips_unsupported_sources(self) -> None:
        app = OCDeckApp(FakeSource(), auto_refresh=False)
        async with app.run_test(size=(100, 32)) as pilot:
            app._request_activity_refresh()
            await pilot.pause()
            self.assertEqual(app.snapshot.sessions[0].id, "session-1")

    async def test_agents_board_marks_pending_permission_in_state(self) -> None:
        class PermissionSource(FakeSource):
            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                session = snapshot.sessions[0]
                permission_session = SessionRecord(
                    id=session.id,
                    title=session.title,
                    directory=session.directory,
                    project_id=session.project_id,
                    created_ms=session.created_ms,
                    updated_ms=session.updated_ms,
                    status=session.status,
                    instance_count=session.instance_count,
                    terminals=session.terminals,
                    terminal_attached=session.terminal_attached,
                    permission="bash npm test",
                )
                return DashboardSnapshot(
                    sessions=(permission_session,),
                    projects=snapshot.projects,
                    services=snapshot.services,
                    metrics=snapshot.metrics,
                    connection="locked",
                    connection_detail="API locked",
                )

        app = OCDeckApp(PermissionSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app.action_show_tab("agents")
            await pilot.pause()
            row = app.query_one("#agents-table", DataTable).get_row("session-1")
            self.assertIn("PERMISSION", str(row[0]))
            self.assertIn("OPEN", str(row[1]))
            self.assertIn("bash npm test", str(row[5]))

    async def test_parent_agent_surfaces_subagent_permission_and_approves_child(
        self,
    ) -> None:
        class SubagentPermissionSource(FakeSource):
            def __init__(self) -> None:
                self.approved: list[tuple[str, str]] = []

            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                base = snapshot.sessions[0]
                parent = replace(
                    base,
                    id="main-agent",
                    title="Main agent",
                    status="busy",
                    last_interaction_ms=200,
                )
                child = replace(
                    parent,
                    id="subagent",
                    title="Worker subagent",
                    parent_id=parent.id,
                    last_interaction_ms=300,
                    permission="bash npm test",
                    permission_id="permission-child",
                )
                return replace(snapshot, sessions=(child, parent))

            async def approve_permission(self, session_id: str, permission_id: str) -> str:
                self.approved.append((session_id, permission_id))
                return ""

        source = SubagentPermissionSource()
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app.action_show_tab("agents")
            await pilot.pause()
            agents = app.query_one("#agents-table", DataTable)
            parent_row = agents.get_row("main-agent")
            self.assertIn("PERMISSION", str(parent_row[0]))
            self.assertIn("subagent: bash npm test", str(parent_row[5]))

            agents.focus()
            agents.move_cursor(row=0)
            app.action_approve_permission()
            await pilot.pause()
            await asyncio.sleep(0.05)
            self.assertEqual(source.approved, [("subagent", "permission-child")])

    async def test_agents_board_marks_pending_question_in_state(self) -> None:
        class QuestionSource(FakeSource):
            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                session = replace(
                    snapshot.sessions[0],
                    question="What happened after pressing the key?",
                )
                return replace(snapshot, sessions=(session,))

        app = OCDeckApp(QuestionSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app.action_show_tab("agents")
            await pilot.pause()
            row = app.query_one("#agents-table", DataTable).get_row("session-1")
            self.assertIn("QUESTION", str(row[0]))
            self.assertIn("What happened", str(row[5]))

    async def test_attention_inbox_tracks_question_and_privacy(self) -> None:
        class QuestionSource(FakeSource):
            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                session = replace(
                    snapshot.sessions[0],
                    question="What happened after pressing the key?",
                )
                return replace(snapshot, sessions=(session,))

        app = OCDeckApp(QuestionSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            attention = app.query_one("#attention")
            self.assertTrue(attention.display)
            self.assertIn("NEED ATTENTION", rendered_attention_text(app))
            self.assertIn("QUESTION", rendered_attention_text(app))
            self.assertIn("Build", rendered_attention_text(app))
            self.assertIn("now", rendered_attention_text(app))

            app.action_privacy()
            self.assertNotIn("Build", rendered_attention_text(app))
            self.assertIn("Hidden session", rendered_attention_text(app))

    async def test_agents_state_shows_elapsed_time_and_resets_on_transition(self) -> None:
        app = OCDeckApp(FakeSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            await asyncio.sleep(0.1)
            agents = app.query_one("#agents-table", DataTable)
            self.assertIn("RUNNING", str(agents.get_row("session-1")[0]))
            first_state = app._state_since_ms["session-1"]

            settled = replace(app.snapshot.sessions[0], status="idle")
            app._apply_snapshot(replace(app.snapshot, sessions=(settled,)))

            self.assertEqual(app._state_since_ms["session-1"][0], "open")
            self.assertGreaterEqual(
                app._state_since_ms["session-1"][1], first_state[1]
            )
            self.assertIn("IDLE", str(agents.get_row("session-1")[0]))

    async def test_permission_action_approves_selected_session_once(self) -> None:
        class PermissionSource(FakeSource):
            def __init__(self) -> None:
                self.approved: list[tuple[str, str]] = []

            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                session = replace(
                    snapshot.sessions[0],
                    permission="bash npm test",
                    permission_id="permission-1",
                )
                return replace(snapshot, sessions=(session,))

            async def approve_permission(self, session_id: str, permission_id: str) -> str:
                self.approved.append((session_id, permission_id))
                return ""

        source = PermissionSource()
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app.action_approve_permission()
            await pilot.pause()
            await asyncio.sleep(0.05)
            self.assertEqual(source.approved, [("session-1", "permission-1")])

    async def test_agents_board_shows_idle_for_live_inactive_session(self) -> None:
        class LiveIdleSource(FakeSource):
            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                session = replace(snapshot.sessions[0], status="idle")
                return replace(snapshot, sessions=(session,))

        app = OCDeckApp(LiveIdleSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app.action_show_tab("agents")
            await pilot.pause()
            row = app.query_one("#agents-table", DataTable).get_row("session-1")
            self.assertIn("IDLE", str(row[0]))
            self.assertNotIn("OPEN", str(row[0]))
            self.assertIn("OPEN", str(row[1]))

    async def test_minimize_hides_window_without_exiting(self) -> None:
        app = OCDeckApp(FakeSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            calls: list[list[str]] = []

            def fake_run(command, **kwargs):
                calls.append(list(command))
                return mock.Mock(returncode=0)

            with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
                with mock.patch("ocdeck.app.subprocess.run", side_effect=fake_run):
                    self.assertTrue(app._minimize_window())
            self.assertEqual(calls[0][:2], ["ydotool", "key"])

    def test_project_accent_is_deterministic(self) -> None:
        self.assertEqual(project_accent("p1"), project_accent("p1"))
        self.assertIn(project_accent("p1"), PROJECT_ACCENTS)
        self.assertEqual(project_accent(""), "#7890a2")

    async def test_tmux_launch_applies_project_theme(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            calls: list[list[str]] = []

            def fake_run(command, **kwargs):
                calls.append(list(command))
                return mock.Mock(returncode=0)

            app._tmux_has_session = lambda name: False
            app._tmux_attach = lambda name, directory, title="": True
            accent, label = app._project_theme("p1")
            with mock.patch("ocdeck.app.subprocess.run", side_effect=fake_run):
                app._launch_tmux(
                    "oc-x", Path("/tmp"), ["bash"], accent=accent, label=label
                )
            options = {
                call[4]: call[5]
                for call in calls
                if call[:2] == ["tmux", "set-option"]
            }
            self.assertEqual(options["status-style"], f"bg=#0d1a25,fg={accent}")
            self.assertIn(label, options["status-left"])
            self.assertEqual(options["pane-active-border-style"], f"fg={accent},bold")

    async def test_session_tmux_name_uses_session_id(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            await asyncio.sleep(0.1)
            session = app.session_by_id["s3"]
            self.assertEqual(app._session_tmux_name(session), "oc-s3")

    async def test_busy_session_indicator_animates(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            await asyncio.sleep(0.1)
            sessions = app.query_one("#sessions-table", DataTable)
            before = str(sessions.get_row("s2")[0])
            app._advance_activity_animation()
            after = str(sessions.get_row("s2")[0])
            self.assertNotEqual(before, after)

    async def test_live_idle_session_state_is_static_while_busy_animates(self) -> None:
        class LiveIdleSource(MultiProjectSource):
            async def collect(self) -> DashboardSnapshot:
                snapshot = await super().collect()
                live_idle = SessionRecord(
                    id="s-idle-live",
                    title="Idle live",
                    directory="/work/beta",
                    project_id="p2",
                    created_ms=800,
                    updated_ms=950,
                    instance_count=1,
                    terminals=("oc-s-idle-live",),
                    terminal_attached=True,
                )
                return replace(
                    snapshot, sessions=(live_idle,) + snapshot.sessions
                )

        app = OCDeckApp(LiveIdleSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            await asyncio.sleep(0.1)
            sessions = app.query_one("#sessions-table", DataTable)
            idle_before = str(sessions.get_row("s-idle-live")[0])
            busy_before = str(sessions.get_row("s2")[0])

            app._advance_activity_animation()

            self.assertEqual(str(sessions.get_row("s-idle-live")[0]), idle_before)
            self.assertNotEqual(str(sessions.get_row("s2")[0]), busy_before)

    async def test_tmux_attach_opens_autoclosing_ptyxis_window(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        with mock.patch("ocdeck.app.subprocess.Popen") as launch:
            self.assertTrue(
                app._tmux_attach("oc-s3", Path("/work/beta"), "Beta main")
            )
        command = launch.call_args.args[0]
        self.assertIn("--standalone", command)
        self.assertEqual(command[command.index("--title") + 1], "OpenCode · Beta main")
        self.assertIn("attach-session", command)
        self.assertEqual(command[-1], "oc-s3")
        self.assertTrue(launch.call_args.kwargs["start_new_session"])

        with mock.patch("ocdeck.app.subprocess.Popen") as launch:
            self.assertTrue(app._tmux_attach("oc-s3", Path("/work/beta")))
        command = launch.call_args.args[0]
        self.assertEqual(command[command.index("--title") + 1], "OpenCode · oc-s3")

    async def test_inline_tmux_attach_records_target_and_uses_current_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.json"
            app = OCDeckApp(
                MultiProjectSource(),
                auto_refresh=False,
                inline_tmux=True,
                mobile_target_file=target,
            )
            async with app.run_test(size=(50, 30)):
                with mock.patch.object(app, "suspend") as suspend:
                    suspend.return_value.__enter__ = mock.Mock()
                    suspend.return_value.__exit__ = mock.Mock(return_value=False)
                    with mock.patch("ocdeck.app.subprocess.run") as run:
                        run.return_value = mock.Mock(returncode=0)
                        self.assertTrue(
                            app._tmux_attach("oc-s3", Path("/work/beta"), "Beta main")
                        )

            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["tmux"], "oc-s3")
            self.assertEqual(payload["title"], "Beta main")
            self.assertGreater(payload["updatedMs"], 0)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            command = run.call_args.args[0]
            self.assertEqual(
                command,
                [
                    "tmux",
                    "attach-session",
                    "-f",
                    "ignore-size",
                    "-t",
                    "oc-s3",
                ],
            )
            self.assertEqual(run.call_args.kwargs["cwd"], Path("/work/beta"))
            self.assertNotIn("TMUX", run.call_args.kwargs["env"])

    async def test_inline_live_session_skips_desktop_window_focus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = OCDeckApp(
                FakeSource(),
                auto_refresh=False,
                inline_tmux=True,
                mobile_target_file=Path(tmp) / "target.json",
            )
            async with app.run_test(size=(50, 30)):
                await asyncio.sleep(0.1)
                focused: list[str] = []
                attached: list[str] = []
                app._raise_existing_window = (
                    lambda name, title="": focused.append(name) or True
                )
                app._tmux_attach = (
                    lambda name, directory, title="": attached.append(name) or True
                )

                app.action_open_session()

            self.assertEqual(focused, [])
            self.assertEqual(attached, ["oc-session-1"])

    async def test_private_mode_keeps_tmux_id_in_viewer_title(self) -> None:
        app = OCDeckApp(FakeSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            await asyncio.sleep(0.1)
            app.private = True
            focused: list[tuple[str, str]] = []
            app._raise_existing_window = (
                lambda name, title="": focused.append((name, title)) or True
            )

            app.action_open_session()

            self.assertEqual(focused, [("oc-session-1", "")])

    async def test_missing_project_directory_is_created(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "fresh-project"
                resolved = app._ensure_launch_directory(target)
                self.assertEqual(resolved, target)
                self.assertTrue(target.is_dir())

    async def test_unwritable_project_directory_falls_back_to_workspace(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                blocked = home / "run-media"
                blocked.mkdir(mode=0o500)
                target = blocked / "external" / "project-alpha"
                with mock.patch.dict(os.environ, {"HOME": str(home)}):
                    resolved = app._ensure_launch_directory(target)
                self.assertEqual(resolved, home / "ocdeck-workspaces" / "project-alpha")
                self.assertIsNotNone(resolved)
                assert resolved is not None
                self.assertTrue(resolved.is_dir())

    async def test_forced_refresh_is_queued(self) -> None:
        class SlowSource(FakeSource):
            def __init__(self) -> None:
                self.calls = 0
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def collect(self) -> DashboardSnapshot:
                self.calls += 1
                if self.calls == 1:
                    self.started.set()
                    await self.release.wait()
                return await super().collect()

        source = SlowSource()
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(140, 42)):
            await asyncio.wait_for(source.started.wait(), timeout=1)
            app._request_refresh(force=True)
            source.release.set()
            for _ in range(20):
                if source.calls >= 2:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(source.calls, 2)

    async def test_once_report_neutralizes_terminal_controls(self) -> None:
        snapshot = await FakeSource().collect()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(render_once(snapshot), 0)
        self.assertNotIn("\x1b]52", output.getvalue())
        self.assertIn("TUI: 3", output.getvalue())


if __name__ == "__main__":
    unittest.main()
