from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

from ocdeck.models import (
    DashboardSnapshot,
    NextStepRecord,
    ProjectBriefingRecord,
    SessionRecord,
    agent_state,
    apply_session_routes,
    assign_project_roots,
    build_projects,
    compact_path,
    parse_known_projects,
    parse_sessions,
    relative_time,
    sanitize_terminal_text,
)


class ModelTests(unittest.TestCase):
    def test_briefing_records_are_frozen_slotted_and_snapshot_defaults_are_empty(
        self,
    ) -> None:
        step = NextStepRecord(
            id="next-1",
            title="Review result",
            detail="Confirm the evidence before proceeding",
            state="now",
        )
        briefing = ProjectBriefingRecord(
            project_id="report-project",
            project_path="/work/alpha",
            name="Alpha",
            assessment="on-track",
            summary="Ready for review",
            confidence="high",
            evidence_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
            next_steps=(step,),
        )

        self.assertFalse(hasattr(step, "__dict__"))
        self.assertFalse(hasattr(briefing, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            step.state = "done"  # type: ignore[misc]

        snapshot = DashboardSnapshot()
        self.assertEqual(snapshot.briefings, ())
        self.assertEqual(snapshot.briefing_report_id, "")
        self.assertIsNone(snapshot.briefing_generated_at)
        self.assertEqual(snapshot.briefing_status, "")

    def test_parse_sessions_filters_invalid_rows_and_sorts(self) -> None:
        payload = [
            {
                "id": "older",
                "title": "Older",
                "directory": "/tmp/alpha",
                "projectId": "p1",
                "created": 100,
                "updated": 200,
            },
            {"id": "invalid"},
            {
                "id": "newer",
                "title": "Newer",
                "directory": "/tmp/beta",
                "projectId": "p2",
                "created": 200,
                "updated": 400,
            },
        ]
        sessions = parse_sessions(
            payload,
            {"newer": {"type": "busy"}},
            {"newer": 2},
        )
        self.assertEqual([session.id for session in sessions], ["newer", "older"])
        self.assertEqual(sessions[0].status, "busy")
        self.assertEqual(sessions[0].instance_count, 2)
        self.assertEqual(sessions[1].status, "idle")
        self.assertEqual(sessions[1].instance_count, 0)

    def test_parse_sessions_attaches_terminals_and_permissions(self) -> None:
        sessions = parse_sessions(
            [
                {
                    "id": "s1",
                    "title": "Live",
                    "directory": "/work/alpha",
                    "projectId": "p1",
                    "updated": 500,
                },
                {
                    "id": "s2",
                    "title": "Blocked",
                    "directory": "/work/beta",
                    "projectId": "p1",
                    "updated": 400,
                },
            ],
            terminals={"s1": ("oc-s1", "oc-new-1")},
            terminal_attached={"s1": True},
            last_interactions={"s1": 450},
            permissions={
                "s2": [
                    {"id": "perm-1", "permission": "bash", "pattern": "rm -rf /"}
                ],
                "s1": [
                    {
                        "id": "question-1",
                        "permission": "question",
                        "pattern": "Which result?",
                    }
                ],
            },
        )
        by_id = {session.id: session for session in sessions}
        self.assertEqual(by_id["s1"].terminals, ("oc-s1", "oc-new-1"))
        self.assertTrue(by_id["s1"].terminal_attached)
        self.assertFalse(by_id["s2"].terminal_attached)
        self.assertEqual(by_id["s1"].last_interaction_ms, 450)
        self.assertEqual(by_id["s2"].last_interaction_ms, 0)
        self.assertEqual(by_id["s2"].permission, "bash rm -rf /")
        self.assertEqual(by_id["s2"].permission_id, "perm-1")
        self.assertEqual(by_id["s1"].permission, "")
        self.assertEqual(by_id["s1"].question, "Which result?")
        self.assertEqual(by_id["s1"].question_id, "question-1")

    def test_parse_sessions_attaches_turn_activity(self) -> None:
        sessions = parse_sessions(
            [
                {
                    "id": "s1",
                    "title": "Live turn",
                    "directory": "/work/alpha",
                    "projectId": "p1",
                    "updated": 500,
                },
                {
                    "id": "s2",
                    "title": "Finished turn",
                    "directory": "/work/beta",
                    "projectId": "p1",
                    "updated": 400,
                },
            ],
            turn_activity={"s1": (True, 0), "s2": (False, 450)},
        )
        by_id = {session.id: session for session in sessions}
        self.assertTrue(by_id["s1"].assistant_active)
        self.assertEqual(by_id["s1"].assistant_done_ms, 0)
        self.assertFalse(by_id["s2"].assistant_active)
        self.assertEqual(by_id["s2"].assistant_done_ms, 450)

    def test_agent_state_priority_and_review_window(self) -> None:
        now = 10_000_000

        def record(**overrides: object) -> SessionRecord:
            base = dict(
                id="s1",
                title="Session",
                directory="/work/alpha",
                project_id="p1",
                created_ms=1,
                updated_ms=now,
            )
            base.update(overrides)
            return SessionRecord(**base)

        self.assertEqual(
            agent_state(record(permission="bash"), now_ms=now), "permission"
        )
        self.assertEqual(
            agent_state(record(question="Choose an answer"), now_ms=now), "question"
        )
        self.assertEqual(agent_state(record(status="busy"), now_ms=now), "busy")
        self.assertEqual(agent_state(record(status="retry"), now_ms=now), "retry")

        # API busy wins even when local metadata claims an unfinished turn.
        self.assertEqual(
            agent_state(
                record(status="busy", assistant_active=True), now_ms=now
            ),
            "busy",
        )

        # An unfinished metadata row needs attention when no explicit busy
        # signal is available.
        self.assertEqual(
            agent_state(
                record(instance_count=1, assistant_active=True), now_ms=now
            ),
            "stalled",
        )

        # Completed recently and after the latest prompt => review.
        self.assertEqual(
            agent_state(
                record(
                    instance_count=1,
                    assistant_done_ms=now - 5 * 60 * 1000,
                    last_interaction_ms=now - 6 * 60 * 1000,
                ),
                now_ms=now,
            ),
            "review",
        )

        # Completed before the latest user prompt is not a fresh review.
        self.assertEqual(
            agent_state(
                record(
                    instance_count=1,
                    assistant_done_ms=now - 8 * 60 * 1000,
                    last_interaction_ms=now - 5 * 60 * 1000,
                ),
                now_ms=now,
            ),
            "open",
        )

        # A stale completion falls back to open.
        self.assertEqual(
            agent_state(
                record(
                    instance_count=1,
                    assistant_done_ms=now - 30 * 60 * 1000,
                    last_interaction_ms=now - 31 * 60 * 1000,
                ),
                now_ms=now,
            ),
            "open",
        )

        # Without turn metadata a live terminal stays open, never review.
        self.assertEqual(agent_state(record(instance_count=1), now_ms=now), "open")

        # An orphaned in-flight row with no live process is never RUNNING.
        self.assertEqual(
            agent_state(record(assistant_active=True), now_ms=now), "idle"
        )
        self.assertEqual(agent_state(record(), now_ms=now), "idle")

    def test_instance_counts_aggregate_without_double_counting_sessions(self) -> None:
        sessions = parse_sessions(
            [
                {
                    "id": "s1",
                    "title": "Duplicated",
                    "directory": "/work/alpha",
                    "projectId": "p1",
                    "updated": 500,
                },
                {
                    "id": "s2",
                    "title": "Single",
                    "directory": "/work/alpha",
                    "projectId": "p1",
                    "updated": 400,
                },
            ],
            instance_counts={"s1": 2, "s2": 1},
        )
        project = build_projects(sessions, {"p1": "/work/alpha"})[0]
        snapshot = DashboardSnapshot(sessions=sessions, unmapped_instance_count=2)

        self.assertEqual(project.attached_count, 2)
        self.assertEqual(project.instance_count, 3)
        self.assertEqual(snapshot.attached_session_count, 2)
        self.assertEqual(snapshot.mapped_instance_count, 3)
        self.assertEqual(snapshot.terminal_instance_count, 5)

    def test_build_projects_includes_known_empty_projects(self) -> None:
        sessions = parse_sessions(
            [
                {
                    "id": "s1",
                    "title": "Work",
                    "directory": "/work/alpha",
                    "projectId": "p1",
                    "updated": 500,
                }
            ]
        )
        projects = build_projects(sessions, {"p1": "/work/alpha", "p2": "/work/beta"})
        self.assertEqual({project.id for project in projects}, {"p1", "p2"})
        beta = next(project for project in projects if project.id == "p2")
        self.assertEqual(beta.session_count, 0)
        self.assertEqual(beta.name, "beta")

    def test_build_projects_uses_session_age_fallback(self) -> None:
        sessions = parse_sessions(
            [
                {
                    "id": "s1",
                    "title": "Work",
                    "directory": "/work/alpha",
                    "projectId": "p1",
                    "created": 100,
                    "updated": 0,
                }
            ],
            last_interactions={"s1": 200},
        )

        projects = build_projects(sessions, {"p1": "/work/alpha"})

        self.assertEqual(projects[0].updated_ms, 200)

    def test_build_projects_prefers_known_worktree_over_session_subdirectory(self) -> None:
        sessions = parse_sessions(
            [
                {
                    "id": "s1",
                    "title": "Nested work",
                    "directory": "/work/alpha/packages/ui",
                    "projectId": "p1",
                    "updated": 500,
                }
            ]
        )
        projects = build_projects(sessions, {"p1": "/work/alpha"})
        self.assertEqual(projects[0].directory, "/work/alpha")

    def test_build_projects_uses_catalog_name(self) -> None:
        projects = build_projects(
            (),
            {"catalog": "/work/project-alpha"},
            {"catalog": "Project Alpha"},
        )

        self.assertEqual(projects[0].name, "Project Alpha")

    def test_session_routes_override_catch_all_project(self) -> None:
        sessions = parse_sessions(
            [
                {
                    "id": "s1",
                    "title": "OC Deck work",
                    "directory": "/home/user",
                    "projectId": "global",
                    "updated": 500,
                }
            ]
        )

        routed = apply_session_routes(
            sessions,
            {"s1": "OC Deck"},
            {"ocdeck-id": "OC Deck"},
        )

        self.assertEqual(routed[0].project_id, "ocdeck-id")

    def test_subagent_inherits_parent_task_route(self) -> None:
        sessions = parse_sessions(
            [
                {
                    "id": "child",
                    "parentID": "parent",
                    "title": "Inspect implementation (@explore subagent)",
                    "directory": "/home/user",
                    "projectId": "global",
                    "updated": 600,
                },
                {
                    "id": "parent",
                    "title": "OC Deck work",
                    "directory": "/home/user",
                    "projectId": "global",
                    "updated": 500,
                },
            ]
        )

        routed = apply_session_routes(
            sessions,
            {"parent": "OC Deck"},
            {"ocdeck-id": "OC Deck"},
        )

        self.assertEqual({session.project_id for session in routed}, {"ocdeck-id"})
        self.assertEqual(routed[0].parent_id, "parent")

    def test_managed_agent_parent_does_not_change_project_routing(self) -> None:
        sessions = parse_sessions(
            [
                {
                    "id": "worker",
                    "title": "Project worker",
                    "directory": "/work/project",
                    "projectId": "worker-project",
                    "updated": 600,
                },
                {
                    "id": "controller",
                    "title": "Home Agent",
                    "directory": "/work/home-agent",
                    "projectId": "home-project",
                    "updated": 500,
                },
            ],
            agent_parent_ids={"worker": "controller"},
        )

        routed = apply_session_routes(
            sessions,
            {"controller": "Home Agent"},
            {"home-project": "Home Agent"},
        )
        by_id = {session.id: session for session in routed}

        self.assertEqual(by_id["worker"].agent_parent_id, "controller")
        self.assertEqual(by_id["worker"].project_id, "worker-project")
        self.assertEqual(by_id["controller"].project_id, "home-project")

    def test_catalog_names_limit_empty_discovered_projects(self) -> None:
        projects = build_projects(
            (),
            {"catalog": "/work/catalog", "incidental": "/home/user"},
            {"catalog": "Catalog Project"},
        )

        self.assertEqual([project.id for project in projects], ["catalog"])

    def test_parse_known_projects_ignores_global_project(self) -> None:
        payload = [
            {"id": "global", "worktree": "/"},
            {"id": "p1", "worktree": "/work/alpha"},
        ]
        self.assertEqual(parse_known_projects(payload), {"p1": "/work/alpha"})

    def test_assign_project_roots_splits_sandbox_from_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            worktree = root / "opencode_start"
            sandbox = root / "agents_start"
            worktree.mkdir()
            sandbox.mkdir()

            sessions = parse_sessions(
                [
                    {
                        "id": "s1",
                        "title": "In worktree",
                        "directory": str(worktree),
                        "projectId": "p1",
                        "updated": 300,
                    },
                    {
                        "id": "s2",
                        "title": "In sandbox",
                        "directory": str(sandbox),
                        "projectId": "p1",
                        "updated": 200,
                    },
                    {
                        "id": "s3",
                        "title": "Nested",
                        "directory": str(worktree / "packages" / "ui"),
                        "projectId": "p1",
                        "updated": 100,
                    },
                ]
            )
            resolved = assign_project_roots(sessions, {"p1": str(worktree)})
            by_id = {session.id: session.project_id for session in resolved}
            self.assertEqual(by_id["s1"], "p1")
            self.assertEqual(by_id["s3"], "p1")
            self.assertEqual(by_id["s2"], f"dir::{sandbox}")

            projects = build_projects(resolved, {"p1": str(worktree)})
            counts = {project.name: project.session_count for project in projects}
            self.assertEqual(counts, {"opencode_start": 2, "agents_start": 1})

    def test_renamed_worktree_merges_sessions_into_live_directory(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            old_path = root / "opencode_start"
            new_path = root / "agents_start"
            new_path.mkdir()

            sessions = parse_sessions(
                [
                    {
                        "id": "old1",
                        "title": "Before rename",
                        "directory": str(old_path),
                        "projectId": "p1",
                        "updated": 300,
                    },
                    {
                        "id": "new1",
                        "title": "After rename",
                        "directory": str(new_path),
                        "projectId": "p1",
                        "updated": 400,
                    },
                ]
            )
            known = parse_known_projects(
                [
                    {
                        "id": "p1",
                        "worktree": str(old_path),
                        "sandboxes": [str(new_path)],
                    }
                ]
            )
            self.assertEqual(known, {"p1": str(new_path)})

            resolved = assign_project_roots(sessions, known)
            self.assertEqual({session.project_id for session in resolved}, {"p1"})
            projects = build_projects(resolved, known)
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0].name, "agents_start")
            self.assertEqual(projects[0].session_count, 2)
            self.assertEqual(projects[0].directory, str(new_path))

    def test_parse_known_projects_prefers_live_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            worktree = root / "repo"
            sandbox = root / "extra"
            worktree.mkdir()
            sandbox.mkdir()
            payload = [
                {
                    "id": "p1",
                    "worktree": str(worktree),
                    "sandboxes": [str(sandbox)],
                }
            ]
            self.assertEqual(parse_known_projects(payload), {"p1": str(worktree)})
            worktree.rmdir()
            self.assertEqual(parse_known_projects(payload), {"p1": str(sandbox)})

    def test_assign_project_roots_prefers_longest_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            (root / "alpha" / "sub").mkdir(parents=True)
            deep = root / "alpha" / "sub" / "deep"
            deep.mkdir()

            sessions = parse_sessions(
                [
                    {
                        "id": "s1",
                        "title": "Deep",
                        "directory": str(deep),
                        "projectId": "px",
                        "updated": 100,
                    }
                ]
            )
            resolved = assign_project_roots(
                sessions,
                {"p1": str(root / "alpha"), "p2": str(root / "alpha" / "sub")},
            )
            self.assertEqual(resolved[0].project_id, "p2")

    def test_stale_session_matches_exact_catalog_path(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            missing = str(Path(base) / "offline-project")
            sessions = parse_sessions(
                [
                    {
                        "id": "s1",
                        "title": "Stored session",
                        "directory": missing,
                        "projectId": "old-id",
                        "updated": 100,
                    }
                ]
            )

            resolved = assign_project_roots(sessions, {"catalog-id": missing})

            self.assertEqual(resolved[0].project_id, "catalog-id")

    def test_relative_time(self) -> None:
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        epoch = int(datetime(2026, 8, 20, 11, 45, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(relative_time(epoch, now), "15m")

    def test_compact_path_privacy(self) -> None:
        self.assertEqual(compact_path("/work/alpha", private=True), "[hidden]")

    def test_terminal_controls_are_removed(self) -> None:
        value = "safe\x1b]52;c;payload\x07\nnext"
        cleaned = sanitize_terminal_text(value)
        self.assertNotIn("\x1b", cleaned)
        self.assertNotIn("\x07", cleaned)
        self.assertEqual(cleaned, "safe]52;c;payload next")


if __name__ == "__main__":
    unittest.main()
