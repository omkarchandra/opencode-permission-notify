from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from ocdeck.models import ProjectRecord, agent_state
from ocdeck.source import (
    DashboardSource,
    MAX_BRIEFING_ARRAY_ITEMS,
    MAX_BRIEFING_EVIDENCE_ITEMS,
    MAX_BRIEFING_TEXT_LENGTH,
    MAX_BRIEFINGS_FILE_BYTES,
    api_credentials_are_safe,
    classify_opencode_command,
    communicate_with_cleanup,
    merge_permissions,
    merge_project_catalog,
    match_project_briefings,
    parse_briefings,
    parse_markdown_projects,
    read_agent_parent_ids,
    read_archived_session_ids,
    read_briefings_file,
    read_last_user_interactions,
    read_live_opencode_panes,
    read_local_permissions,
    read_local_statuses,
    read_opencode_instances,
    read_process_tty,
    read_session_routes,
    read_session_turn_activity,
    read_tmux_tty_state,
    read_tmux_tty_sessions,
    validate_api_url,
)


def write_session_db(path: Path, rows: dict[str, int | None]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE session ("
            "id TEXT PRIMARY KEY, time_archived INTEGER)"
        )
        connection.executemany(
            "INSERT INTO session (id, time_archived) VALUES (?, ?)",
            list(rows.items()),
        )
        connection.commit()
    finally:
        connection.close()


def briefing_project(project_path: str, project_id: str = "artifact-alpha") -> dict:
    return {
        "projectID": project_id,
        "projectPath": project_path,
        "name": "Alpha",
        "assessment": "on-track",
        "summary": "Ready for the next review",
        "confidence": "high",
        "evidenceAt": "2026-08-24T10:00:00Z",
        "completedOutputs": [{"label": "Report", "locator": "/tmp/report.txt"}],
        "blockers": [],
        "nextSteps": [
            {
                "id": "step-1",
                "title": "Review report",
                "detail": "Check the evidence",
                "state": "now",
                "requiresApproval": True,
            }
        ],
        "evidence": ["pytest passed"],
        "researchStatus": "completed",
    }


def briefing_report(*projects: dict, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "reportID": "report-1",
        "generatedAt": "2026-08-24T10:05:00Z",
        "status": "completed",
        "projects": list(projects),
    }
    payload.update(overrides)
    return payload


class FakeApiSource(DashboardSource):
    def __init__(self) -> None:
        super().__init__(api_url="http://127.0.0.1:4096", opencode_bin="/bin/false")
        self.paths: list[str] = []
        self.patches: list[tuple[str, str, object]] = []

    def _request_json(
        self,
        path: str,
        password: str,
        *,
        method: str = "GET",
        payload: object | None = None,
    ):
        self.paths.append(path)
        if method == "PATCH":
            self.patches.append((path, method, payload))
            return {"id": path.rsplit("/", 1)[-1]}
        if path == "/global/health":
            return {"healthy": True, "version": "test"}
        if path == "/session/status":
            return {"session-1": {"type": "busy"}}
        if path == "/permission":
            return [
                {
                    "id": "perm-1",
                    "sessionID": "session-1",
                    "permission": "bash",
                    "patterns": ["npm test"],
                }
            ]
        if path == "/question":
            return [
                {
                    "id": "question-1",
                    "sessionID": "session-2",
                    "questions": [
                        {
                            "header": "Test result",
                            "question": "What happened after pressing the key?",
                        }
                    ],
                }
            ]
        raise AssertionError(path)


class SourceTests(unittest.IsolatedAsyncioTestCase):
    def test_briefing_contract_is_parsed_sanitized_and_capped(self) -> None:
        project = briefing_project("/work/alpha")
        project["summary"] = "safe\x1b]52;c;payload\x07\n" + "x" * (
            MAX_BRIEFING_TEXT_LENGTH + 100
        )
        project["completedOutputs"] = [
            {"label": f"output-{index}", "locator": f"/tmp/{index}"}
            for index in range(MAX_BRIEFING_ARRAY_ITEMS + 2)
        ]
        project["blockers"] = [
            {"summary": f"blocker-{index}"}
            for index in range(MAX_BRIEFING_ARRAY_ITEMS + 2)
        ]
        project["nextSteps"] = [
            {
                "id": f"step-{index}",
                "title": "Review [bold] literally",
                "detail": "detail\x1b[31m",
                "state": "now" if index == 0 else "next",
                "requiresApproval": True,
            }
            for index in range(MAX_BRIEFING_ARRAY_ITEMS + 2)
        ]
        project["evidence"] = [
            f"evidence-{index}" for index in range(MAX_BRIEFING_EVIDENCE_ITEMS + 2)
        ]

        report = parse_briefings(json.dumps(briefing_report(project)))

        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.report_id, "report-1")
        self.assertEqual(len(report.projects), 1)
        parsed = report.projects[0]
        self.assertEqual(parsed.confidence, "high")
        self.assertNotIn("\x1b", parsed.summary)
        self.assertNotIn("\x07", parsed.summary)
        self.assertLessEqual(len(parsed.summary), MAX_BRIEFING_TEXT_LENGTH)
        self.assertEqual(len(parsed.completed_outputs), MAX_BRIEFING_ARRAY_ITEMS)
        self.assertEqual(len(parsed.blockers), MAX_BRIEFING_ARRAY_ITEMS)
        self.assertEqual(len(parsed.next_steps), MAX_BRIEFING_ARRAY_ITEMS)
        self.assertEqual(len(parsed.evidence), MAX_BRIEFING_EVIDENCE_ITEMS)
        self.assertIn("[bold]", parsed.next_steps[0].title)
        self.assertNotIn("\x1b", parsed.next_steps[0].detail)

    def test_literal_home_agent_artifact_accepts_nullable_incomplete_evidence(
        self,
    ) -> None:
        artifact = """{
          "schemaVersion": 1,
          "reportID": "home-agent-20260824",
          "generatedAt": "2026-08-24T10:05:00Z",
          "status": "running",
          "projects": [
            {
              "projectID": "queued-project",
              "projectPath": "/work/queued",
              "name": "Queued",
              "assessment": "unknown",
              "summary": "Research has not started.",
              "confidence": "low",
              "evidenceAt": null,
              "completedOutputs": [],
              "blockers": [],
              "nextSteps": [{
                "id": "wait-for-research",
                "title": "Wait for research",
                "detail": "No action is available yet.",
                "state": "blocked",
                "requiresApproval": true
              }],
              "evidence": [],
              "researchStatus": "queued"
            },
            {
              "projectID": "running-project",
              "projectPath": "/work/running",
              "name": "Running",
              "assessment": "waiting",
              "summary": "Research is in progress.",
              "confidence": "medium",
              "evidenceAt": null,
              "completedOutputs": [],
              "blockers": [],
              "nextSteps": [],
              "evidence": [],
              "researchStatus": "running"
            },
            {
              "projectID": "failed-project",
              "projectPath": "/work/failed",
              "name": "Failed",
              "assessment": "blocked",
              "summary": "Research failed before evidence was collected.",
              "confidence": "high",
              "evidenceAt": null,
              "completedOutputs": [],
              "blockers": [{"summary": "Research worker failed."}],
              "nextSteps": [],
              "evidence": [],
              "researchStatus": "failed"
            },
            {
              "projectID": "unknown-project",
              "projectPath": "/work/unknown",
              "name": "Unknown",
              "assessment": "unknown",
              "summary": "Completed research produced no current evidence.",
              "confidence": "low",
              "evidenceAt": null,
              "completedOutputs": [],
              "blockers": [],
              "nextSteps": [],
              "evidence": [],
              "researchStatus": "completed"
            }
          ]
        }"""

        report = parse_briefings(artifact)

        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(
            [project.confidence for project in report.projects],
            ["low", "medium", "high", "low"],
        )
        self.assertEqual(
            [project.research_status for project in report.projects],
            ["queued", "running", "failed", "completed"],
        )
        self.assertTrue(all(project.evidence_at is None for project in report.projects))

    def test_any_malformed_project_rejects_the_whole_briefing_artifact(self) -> None:
        self.assertIsNone(
            parse_briefings(
                briefing_report(briefing_project("/work/good", "good"), {})
            )
        )

        malformed_row = briefing_project("/work/bad", "bad")
        malformed_row["confidence"] = 0.5
        artifact = briefing_report(
            briefing_project("/work/good", "good"),
            malformed_row,
        )

        self.assertIsNone(parse_briefings(artifact))

        completed_without_evidence = briefing_project("/work/no-evidence")
        completed_without_evidence["evidenceAt"] = None
        self.assertIsNone(
            parse_briefings(briefing_report(completed_without_evidence))
        )

    def test_optional_briefing_file_missing_malformed_oversized_or_unsupported(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "latest.json"
            self.assertIsNone(read_briefings_file(path))

            path.write_text("not-json", encoding="utf-8")
            self.assertIsNone(parse_briefings(read_briefings_file(path)))

            path.write_bytes(b"x" * (MAX_BRIEFINGS_FILE_BYTES + 1))
            self.assertIsNone(read_briefings_file(path))

        unsupported = briefing_report(briefing_project("/work/alpha"))
        unsupported["schemaVersion"] = 2
        self.assertIsNone(parse_briefings(json.dumps(unsupported)))

    def test_briefings_match_only_normalized_exact_project_paths(self) -> None:
        report = parse_briefings(
            json.dumps(
                briefing_report(
                    briefing_project("/work/alpha/", "unrelated-report-id"),
                    briefing_project("/work/alpha/subdir", "p1"),
                    briefing_project("/work/alpha-other", "p2"),
                )
            )
        )
        assert report is not None
        projects = (
            ProjectRecord(id="p1", directory="/work/alpha", name="Alpha"),
            ProjectRecord(id="p2", directory="/work/beta", name="Beta"),
        )

        matched = match_project_briefings(report.projects, projects)

        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].project_id, "unrelated-report-id")
        self.assertEqual(matched[0].project_path, "/work/alpha/")

    async def test_malformed_optional_briefing_does_not_fail_collection(self) -> None:
        class CollectSource(DashboardSource):
            async def _api_status(self):
                return "offline", "test", {}, {}

            async def _command_json(self, *arguments, cwd=None, timeout=15):
                return []

            async def _collect_sessions(self, known_projects):
                return []

            async def _service_states(self):
                return ()

        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            artifact = root / "latest.json"
            artifact.write_text("{broken", encoding="utf-8")
            source = CollectSource(
                opencode_bin="/bin/false",
                projects_file=root / "missing.md",
                session_routes_file=root / "missing-routes.json",
                briefings_file=artifact,
                session_db_file=root / "missing-opencode.db",
                permission_state_dir=root / "permissions",
            )

            snapshot = await source.collect()

        self.assertEqual(snapshot.briefings, ())
        self.assertEqual(snapshot.briefing_status, "")
        self.assertEqual(snapshot.warning, "")

    async def test_collection_attaches_valid_briefing_metadata_by_path(self) -> None:
        class CollectSource(DashboardSource):
            def __init__(self, project_path: Path, **kwargs) -> None:
                super().__init__(**kwargs)
                self.project_path = project_path

            async def _api_status(self):
                return "offline", "test", {}, {}

            async def _command_json(self, *arguments, cwd=None, timeout=15):
                if arguments[:2] == ("debug", "scrap"):
                    return [{"id": "runtime-id", "worktree": str(self.project_path)}]
                return []

            async def _collect_sessions(self, known_projects):
                return [
                    {
                        "id": "session-1",
                        "title": "Alpha work",
                        "directory": str(self.project_path),
                        "projectId": "runtime-id",
                        "created": 1,
                        "updated": 2,
                    }
                ]

            async def _service_states(self):
                return ()

        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            project_path = root / "alpha"
            project_path.mkdir()
            artifact = root / "latest.json"
            artifact.write_text(
                json.dumps(briefing_report(briefing_project(str(project_path)))),
                encoding="utf-8",
            )
            source = CollectSource(
                project_path,
                opencode_bin="/bin/false",
                projects_file=root / "missing.md",
                session_routes_file=root / "missing-routes.json",
                briefings_file=artifact,
                session_db_file=root / "missing-opencode.db",
                permission_state_dir=root / "permissions",
            )

            snapshot = await source.collect()

        self.assertEqual(snapshot.briefing_report_id, "report-1")
        self.assertEqual(snapshot.briefing_status, "completed")
        self.assertEqual(len(snapshot.briefings), 1)
        self.assertEqual(snapshot.briefings[0].project_path, str(project_path))

    def test_briefings_file_configuration_prefers_argument_then_environment(self) -> None:
        with patch.dict(
            os.environ, {"OCDECK_BRIEFINGS_FILE": "/tmp/from-env.json"}
        ):
            from_env = DashboardSource(opencode_bin="/bin/false")
            explicit = DashboardSource(
                opencode_bin="/bin/false",
                briefings_file="/tmp/from-cli.json",
            )
        self.assertEqual(from_env.briefings_file, Path("/tmp/from-env.json"))
        self.assertEqual(explicit.briefings_file, Path("/tmp/from-cli.json"))

    def test_local_permission_state_tracks_only_live_opencode_processes(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            state_dir = root / "state"
            proc_root = root / "proc"
            state_dir.mkdir()
            process = proc_root / "123"
            process.mkdir(parents=True)
            (process / "comm").write_text("opencode\n", encoding="utf-8")
            (state_dir / "123.json").write_text(
                '{"pid":123,"permissions":[{"id":"perm-1",'
                '"sessionID":"session-1","permission":"bash",'
                '"pattern":"npm test"}],"questions":[{"id":"q-1",'
                '"sessionID":"session-2","question":"Choose a result"}],'
                '"statuses":[{"sessionID":"session-1","status":"busy"}]}',
                encoding="utf-8",
            )
            stale = state_dir / "456.json"
            stale.write_text(
                '{"pid":456,"permissions":[{"sessionID":"stale"}]}',
                encoding="utf-8",
            )

            self.assertEqual(
                read_local_permissions(state_dir, proc_root),
                {
                    "session-1": [
                        {
                            "id": "perm-1",
                            "permission": "bash",
                            "pattern": "npm test",
                        }
                    ],
                    "session-2": [
                        {
                            "id": "q-1",
                            "permission": "question",
                            "pattern": "Choose a result",
                        }
                    ],
                },
            )
            self.assertEqual(
                read_local_statuses(state_dir, proc_root), {"session-1": "busy"}
            )
            self.assertFalse(stale.exists())

    def test_local_statuses_use_newest_record_for_duplicate_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            state_dir = root / "state"
            proc_root = root / "proc"
            state_dir.mkdir()
            for pid in (123, 124):
                process = proc_root / str(pid)
                process.mkdir(parents=True)
                (process / "comm").write_text("opencode\n", encoding="utf-8")

            (state_dir / "123.json").write_text(
                '{"pid":123,"updated":100,"statuses":'
                '[{"sessionID":"session-1","status":"busy"}]}',
                encoding="utf-8",
            )
            (state_dir / "124.json").write_text(
                '{"pid":124,"updated":200,"statuses":'
                '[{"sessionID":"session-1","status":"idle"}]}',
                encoding="utf-8",
            )

            self.assertEqual(
                read_local_statuses(state_dir, proc_root),
                {"session-1": "idle"},
            )

    def test_local_and_api_permissions_are_deduplicated(self) -> None:
        request = {
            "id": "perm-1",
            "permission": "bash",
            "pattern": "npm test",
        }
        self.assertEqual(
            merge_permissions(
                {"session-1": [request]},
                {"session-1": [request], "session-2": [{"permission": "edit"}]},
            ),
            {
                "session-1": [request],
                "session-2": [
                    {"id": "", "permission": "edit", "pattern": ""}
                ],
            },
        )

    async def test_unprotected_api_fetches_live_status(self) -> None:
        source = FakeApiSource()
        with patch.dict(os.environ, {}, clear=True):
            state, detail, statuses, permissions = await source._api_status()
        self.assertEqual(state, "live")
        self.assertIn("test", detail)
        self.assertEqual(statuses, {"session-1": "busy"})
        self.assertEqual(
            permissions,
            {
                "session-1": [
                    {
                        "id": "perm-1",
                        "permission": "bash",
                        "pattern": "npm test",
                    }
                ],
                "session-2": [
                    {
                        "id": "question-1",
                        "permission": "question",
                        "pattern": "What happened after pressing the key?",
                    }
                ],
            },
        )
        self.assertEqual(
            source.paths,
            ["/global/health", "/session/status", "/permission", "/question"],
        )

    async def test_approve_permission_uses_open_code_permission_route(self) -> None:
        source = DashboardSource(api_url="http://127.0.0.1:4096", opencode_bin="/bin/false")
        with mock.patch.object(source, "_request_json", return_value=None) as request:
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    await source.approve_permission("session-1", "perm-1"), ""
                )
        request.assert_called_once_with(
            "/session/session-1/permissions/perm-1",
            "",
            method="POST",
            payload={"response": "once"},
        )

    async def test_rename_session_patches_the_loopback_api(self) -> None:
        source = FakeApiSource()
        with patch.dict(os.environ, {}, clear=True):
            error = await source.rename_session("ses_one", "New title")
        self.assertEqual(error, "")
        self.assertEqual(
            source.patches,
            [("/session/ses_one", "PATCH", {"title": "New title"})],
        )

    async def test_rename_session_reports_api_failures_without_raising(self) -> None:
        source = FakeApiSource()
        cases = [
            (
                urllib.error.HTTPError(
                    "http://127.0.0.1:4096/session/x",
                    401,
                    "locked",
                    None,
                    None,
                ),
                "API locked; set OPENCODE_SERVER_PASSWORD to rename",
            ),
            (
                urllib.error.HTTPError(
                    "http://127.0.0.1:4096/session/x",
                    500,
                    "boom",
                    None,
                    None,
                ),
                "API returned HTTP 500",
            ),
            (urllib.error.URLError("refused"), "API unavailable; title kept unchanged"),
            (TimeoutError("slow"), "API unavailable; title kept unchanged"),
        ]
        for raised, expected in cases:
            with mock.patch.object(source, "_request_json", side_effect=raised):
                with patch.dict(os.environ, {}, clear=True):
                    message = await source.rename_session("ses_x", "t")
            self.assertEqual(message, expected)

    async def test_rename_session_reports_locked_credentials_with_password(self) -> None:
        source = FakeApiSource()
        error = urllib.error.HTTPError(
            "http://127.0.0.1:4096/session/x",
            401,
            "locked",
            None,
            None,
        )
        with mock.patch.object(source, "_request_json", side_effect=error):
            with patch.dict(
                os.environ, {"OPENCODE_SERVER_PASSWORD": "secret"}, clear=True
            ):
                message = await source.rename_session("ses_x", "t")
        self.assertEqual(message, "API rejected the configured credentials")

    def test_rename_refuses_non_loopback_credentials_and_bad_urls(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            source = DashboardSource(
                api_url="http://10.0.0.5:4096",
                opencode_bin="/bin/false",
                projects_file=root / "projects.md",
            )
            with patch.dict(
                os.environ, {"OPENCODE_SERVER_PASSWORD": "secret"}, clear=True
            ):
                refusal = asyncio.run(source.rename_session("ses_one", "t"))
            self.assertEqual(refusal, "Refusing credentials over non-loopback HTTP")

            invalid = DashboardSource(
                api_url="http://127.0.0.1:99999",
                opencode_bin="/bin/false",
                projects_file=root / "projects.md",
            )
            message = asyncio.run(invalid.rename_session("ses_one", "t"))
            self.assertEqual(message, "Invalid OpenCode API URL")

    async def test_timed_out_process_is_reaped(self) -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        result = await communicate_with_cleanup(process, timeout=0.05)
        self.assertIsNone(result)
        self.assertIsNotNone(process.returncode)

    async def test_session_metadata_retries_transient_cli_failure(self) -> None:
        class RetrySource(DashboardSource):
            def __init__(self) -> None:
                super().__init__(opencode_bin="/bin/false")
                self.calls: dict[str, int] = {}

            async def _command_json(
                self,
                *arguments: str,
                cwd: Path | None = None,
                timeout: float = 15,
            ):
                key = str(cwd)
                self.calls[key] = self.calls.get(key, 0) + 1
                if self.calls[key] == 1:
                    return None
                return [
                    {
                        "id": f"session-{len(self.calls)}",
                        "title": "Recovered metadata",
                        "updated": 10,
                    }
                ]

        source = RetrySource()
        sessions = await source._collect_sessions({"project": "/tmp"})

        self.assertIsNotNone(sessions)
        self.assertTrue(sessions)
        self.assertTrue(all(count == 2 for count in source.calls.values()))

    async def test_session_metadata_api_includes_child_sessions(self) -> None:
        class ChildSource(DashboardSource):
            def __init__(self) -> None:
                super().__init__(
                    api_url="http://127.0.0.1:4096",
                    opencode_bin="/bin/false",
                )
                self.paths: list[str] = []

            def _request_json(self, path, password, *, method="GET", payload=None):
                self.paths.append(path)
                return [
                    {
                        "id": "parent",
                        "title": "Home Agent",
                        "directory": "/tmp",
                        "updated": 10,
                    },
                    {
                        "id": "child",
                        "parentID": "parent",
                        "title": "Worker",
                        "directory": "/tmp",
                        "updated": 11,
                    },
                ]

            async def _command_json(self, *arguments, cwd=None, timeout=15):
                raise AssertionError("API metadata should avoid the roots-only CLI")

        source = ChildSource()
        with patch.dict(os.environ, {}, clear=True):
            sessions = await source._collect_sessions({"project": "/tmp"})

        self.assertEqual({item["id"] for item in sessions or []}, {"parent", "child"})
        self.assertEqual(next(item for item in sessions if item["id"] == "child")["parentID"], "parent")
        self.assertTrue(source.paths)
        self.assertTrue(all("roots=false" in path for path in source.paths))

    def test_api_url_validation_and_credential_policy(self) -> None:
        self.assertEqual(validate_api_url("http://127.0.0.1:4096"), "")
        self.assertNotEqual(validate_api_url("file:///tmp/socket"), "")
        self.assertTrue(api_credentials_are_safe("http://localhost:4096"))
        self.assertTrue(api_credentials_are_safe("https://example.com"))
        self.assertFalse(api_credentials_are_safe("http://example.com"))

    def test_opencode_command_classification(self) -> None:
        self.assertEqual(
            classify_opencode_command(
                ["/home/user/.opencode/bin/opencode", "/work", "--session", "ses_one"]
            ),
            (True, "ses_one"),
        )
        self.assertEqual(
            classify_opencode_command(["opencode", "-s=ses_two", "--auto"]),
            (True, "ses_two"),
        )
        self.assertEqual(
            classify_opencode_command(["opencode", "--session=ses_three"]),
            (True, "ses_three"),
        )
        self.assertEqual(classify_opencode_command(["opencode"]), (True, ""))
        self.assertEqual(
            classify_opencode_command(["opencode", "--session", "ses_one", "--fork"]),
            (True, ""),
        )
        self.assertEqual(
            classify_opencode_command(["opencode", "web", "--port", "4096"]),
            (False, ""),
        )
        self.assertEqual(
            classify_opencode_command(["opencode", "session", "list"]),
            (False, ""),
        )

    def test_proc_scan_counts_duplicate_and_unlinked_tuis(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            proc_root = Path(base)

            def add_process(pid: int, comm: str, arguments: list[str] | None) -> None:
                process = proc_root / str(pid)
                process.mkdir()
                (process / "comm").write_text(comm + "\n")
                if arguments is not None:
                    (process / "cmdline").write_bytes(
                        b"\0".join(argument.encode() for argument in arguments) + b"\0"
                    )

            add_process(100, "opencode", ["opencode", "--session", "ses_same"])
            add_process(101, "opencode", ["opencode", "-s=ses_same"])
            add_process(102, "opencode", ["opencode", "/work/project"])
            add_process(103, "opencode", ["opencode", "web", "--port", "4096"])
            add_process(104, "python3", ["python3", "opencode", "--session", "ses_fake"])
            add_process(105, "opencode", None)

            (proc_root / "100" / "fd").mkdir()
            os.symlink("/dev/pts/3", proc_root / "100" / "fd" / "0")
            (proc_root / "101" / "fd").mkdir()
            os.symlink("/dev/pts/7", proc_root / "101" / "fd" / "0")

            counts, unmapped, ttys = read_opencode_instances(proc_root)
            direct = read_process_tty(proc_root / "100")

        self.assertEqual(counts, {"ses_same": 2})
        self.assertEqual(unmapped, 1)
        self.assertEqual(ttys, {"ses_same": ("/dev/pts/3", "/dev/pts/7")})
        self.assertEqual(direct, "/dev/pts/3")

    def test_tmux_tty_mapping_resolves_session_names(self) -> None:
        fake_result = mock.Mock(returncode=0)
        fake_result.stdout = (
            b"/dev/pts/3\toc-ses_same\t1\n/dev/pts/9\tmain\t0\n"
        )
        with mock.patch(
            "ocdeck.source.subprocess.run", return_value=fake_result
        ) as runner:
            mapped, attached = read_tmux_tty_state(
                {"ses_same": ("/dev/pts/3", "/dev/pts/8"), "ses_other": ("/dev/pts/9",)}
            )
        self.assertEqual(mapped, {"ses_same": ("oc-ses_same",), "ses_other": ("main",)})
        self.assertEqual(attached, {"ses_same": True, "ses_other": False})
        command = runner.call_args.args[0]
        self.assertEqual(command[:3], ["tmux", "list-panes", "-a"])

        with mock.patch("ocdeck.source.subprocess.run", return_value=fake_result):
            self.assertEqual(
                read_tmux_tty_sessions({"ses_same": ("/dev/pts/3",)}),
                {"ses_same": ("oc-ses_same",)},
            )

    def test_tmux_tty_mapping_ignores_tmux_failures(self) -> None:
        with mock.patch(
            "ocdeck.source.subprocess.run", side_effect=OSError("no tmux")
        ):
            self.assertEqual(read_tmux_tty_sessions({"s": ("/dev/pts/3",)}), {})
        self.assertEqual(read_tmux_tty_sessions({}), {})

    def test_live_opencode_panes_keep_exact_duplicate_session_panes(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            proc_root = Path(base)

            def add_process(pid: int, tty: str, session_id: str = "ses_same") -> None:
                process = proc_root / str(pid)
                process.mkdir()
                (process / "comm").write_text("opencode\n", encoding="utf-8")
                arguments = ["opencode"]
                if session_id:
                    arguments.extend(("--session", session_id))
                (process / "cmdline").write_bytes(
                    b"\0".join(item.encode() for item in arguments) + b"\0"
                )
                (process / "stat").write_text(
                    f"{pid} (opencode) S " + " ".join(["0"] * 18 + [str(pid * 10)]),
                    encoding="ascii",
                )
                (process / "fd").mkdir()
                os.symlink(tty, process / "fd" / "0")

            add_process(100, "/dev/pts/3")
            add_process(101, "/dev/pts/7")
            add_process(102, "/dev/pts/8", "")
            result = mock.Mock(returncode=0)
            result.stdout = (
                b"%3\t/dev/pts/3\toc-one\t0\t0\t1\t1\t1\t0\n"
                b"%7\t/dev/pts/7\toc-one\t0\t1\t0\t0\t0\t0\n"
                b"%8\t/dev/pts/8\toc-unlinked\t1\t0\t1\t1\t1\t0\n"
            )
            with mock.patch(
                "ocdeck.source.subprocess.run", return_value=result
            ) as runner:
                panes = read_live_opencode_panes(proc_root)

        self.assertEqual([pane.pane_id for pane in panes], ["%3", "%8", "%7"])
        self.assertEqual([pane.session_id for pane in panes], ["ses_same", "", "ses_same"])
        self.assertEqual(
            [pane.terminal_state for pane in panes],
            ["foreground", "foreground", "background"],
        )
        self.assertEqual(len({pane.destination_id for pane in panes}), 3)
        self.assertTrue(all(pane.destination_id.startswith("dst_") for pane in panes))
        self.assertEqual(runner.call_args.args[0][:3], ["tmux", "list-panes", "-a"])

    def test_live_opencode_panes_fail_closed_on_ambiguous_or_dead_panes(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            proc_root = Path(base)
            for pid, session_id in ((100, "ses_one"), (101, "ses_two")):
                process = proc_root / str(pid)
                process.mkdir()
                (process / "comm").write_text("opencode\n", encoding="utf-8")
                (process / "cmdline").write_bytes(
                    f"opencode\0--session\0{session_id}\0".encode()
                )
                (process / "stat").write_text(
                    f"{pid} (opencode) S " + " ".join(["0"] * 18 + [str(pid)]),
                    encoding="ascii",
                )
                (process / "fd").mkdir()
                os.symlink("/dev/pts/3", process / "fd" / "0")
            result = mock.Mock(returncode=0)
            result.stdout = (
                b"%3\t/dev/pts/3\toc-one\t0\t0\t1\t1\t1\t0\n"
                b"%4\t/dev/pts/3\toc-one\t0\t1\t1\t1\t0\t1\n"
                b"not-a-pane\t/dev/pts/3\toc-one\t0\t2\t1\t1\t0\t0\n"
            )
            with mock.patch("ocdeck.source.subprocess.run", return_value=result):
                panes = read_live_opencode_panes(proc_root)

        self.assertEqual(panes, ())

    def test_markdown_project_table_resolves_paths_from_vault_root(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            vault = Path(base)
            (vault / ".obsidian").mkdir()
            projects_file = (
                vault
                / "Projects/agents-start/docs/project_notes/Projects/agents-start/docs/projects.md"
            )
            projects_file.parent.mkdir(parents=True)
            source = """\
# Projects

| Project | Host | Code | Vault note |
| --- | --- | --- | --- |
| Project Alpha | test | `project_alpha` | Projects/project-alpha/main.md |
| Project Beta | test | /work/project-beta | Projects/project-beta/main.md |
"""

            projects = parse_markdown_projects(source, projects_file)

            self.assertEqual(
                projects,
                (
                    ("Project Alpha", str(vault / "project_alpha")),
                    ("Project Beta", "/work/project-beta"),
                ),
            )

    def test_markdown_catalog_merges_exact_discovered_paths(self) -> None:
        projects, names = merge_project_catalog(
            {"open-code-id": "/work/agents_start"},
            (
                ("Agents Start", "/work/agents_start"),
                ("Project Gamma", "/work/project-gamma"),
            ),
        )

        self.assertEqual(len(projects), 2)
        self.assertEqual(names["open-code-id"], "Agents Start")
        gamma_id = next(
            project_id
            for project_id, directory in projects.items()
            if directory == "/work/project-gamma"
        )
        self.assertEqual(names[gamma_id], "Project Gamma")

    def test_agent_parent_ids_only_group_agent_spawned_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            db_file = Path(base) / "opencode.db"
            connection = sqlite3.connect(db_file)
            try:
                connection.execute(
                    "CREATE TABLE session ("
                    "id TEXT PRIMARY KEY, parent_id TEXT, metadata TEXT, "
                    "time_updated INTEGER, time_archived INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
                    [
                        ("native", "parent", None, 50, None),
                        (
                            "self-parent",
                            "self-parent",
                            None,
                            60,
                            None,
                        ),
                        (
                            "controller-old",
                            None,
                            '{"homeAgent":{"kind":"orchestrator"}}',
                            100,
                            None,
                        ),
                        (
                            "controller",
                            None,
                            '{"role":"home_agent_monitor",'
                            '"managedBy":"home_agent.py"}',
                            200,
                            None,
                        ),
                        (
                            "worker",
                            None,
                            '{"homeAgent":{"project":"OC Deck",'
                            '"workerAgent":"build"}}',
                            300,
                            None,
                        ),
                        (
                            "archived-worker",
                            "parent",
                            None,
                            400,
                            500,
                        ),
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            self.assertEqual(
                read_agent_parent_ids(db_file),
                {"native": "parent"},
            )

    def test_archived_session_ids_read_only_and_null_aware(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            db_file = root / "opencode.db"
            self.assertEqual(read_archived_session_ids(root / "missing.db"), set())

            write_session_db(
                db_file,
                {"ses_live": None, "ses_dead": 1766588160000},
            )
            self.assertEqual(
                read_archived_session_ids(db_file), {"ses_dead"}
            )

            corrupt = root / "corrupt.db"
            corrupt.write_bytes(b"this is not a sqlite database")
            self.assertEqual(read_archived_session_ids(corrupt), set())

    def test_last_user_interactions_reads_only_user_messages(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            db_file = Path(base) / "opencode.db"
            connection = sqlite3.connect(db_file)
            try:
                connection.execute(
                    "CREATE TABLE message (session_id TEXT, time_created INTEGER, data TEXT)"
                )
                connection.executemany(
                    "INSERT INTO message VALUES (?, ?, ?)",
                    [
                        ("s1", 100, '{"role":"user"}'),
                        ("s1", 300, '{"role":"assistant"}'),
                        ("s1", 200, '{"role":"user"}'),
                        ("s2", 400, '{"role":"assistant"}'),
                        ("s3", 500, "invalid json"),
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            self.assertEqual(read_last_user_interactions(db_file), {"s1": 200})

    def test_session_turn_activity_reads_latest_assistant_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            db_file = Path(base) / "opencode.db"
            self.assertEqual(
                read_session_turn_activity(Path(base) / "missing.db"), {}
            )

            connection = sqlite3.connect(db_file)
            try:
                connection.execute(
                    "CREATE TABLE message ("
                    "id TEXT PRIMARY KEY, session_id TEXT, "
                    "time_created INTEGER, data TEXT)"
                )
                connection.execute(
                    "CREATE TABLE part (message_id TEXT, time_updated INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO message VALUES (?, ?, ?, ?)",
                    [
                        ("m0", "s_active", 100, '{"role":"user"}'),
                        (
                            "m1",
                            "s_active",
                            200,
                            '{"role":"assistant","time":{"created":200}}',
                        ),
                        (
                            "m2",
                            "s_done",
                            300,
                            '{"role":"assistant",'
                            '"time":{"created":300,"completed":350},'
                            '"finish":"stop"}',
                        ),
                        (
                            "m3",
                            "s_tools",
                            400,
                            '{"role":"assistant",'
                            '"time":{"created":400,"completed":450},'
                            '"finish":"tool-calls"}',
                        ),
                        (
                            "m4",
                            "s_abort",
                            500,
                            '{"role":"assistant",'
                            '"time":{"created":500,"completed":550}}',
                        ),
                        ("m5", "s_junk", 600, "not json"),
                        ("m6", "", 700, '{"role":"assistant","finish":"stop"}'),
                        (
                            "z-old",
                            "s_order",
                            800,
                            '{"role":"assistant",'
                            '"time":{"created":800,"completed":850},'
                            '"finish":"stop"}',
                        ),
                        (
                            "a-new",
                            "s_order",
                            900,
                            '{"role":"assistant","time":{"created":900}}',
                        ),
                        (
                            "stale",
                            "s_stale",
                            1,
                            '{"role":"assistant","time":{"created":1}}',
                        ),
                        (
                            "slow",
                            "s_slow",
                            1400000,
                            '{"role":"assistant","time":{"created":1400000}}',
                        ),
                    ],
                )
                connection.executemany(
                    "INSERT INTO part VALUES (?, ?)",
                    [
                        ("m1", 1999900),
                        ("m3", 1999900),
                        ("a-new", 1999950),
                        ("stale", 1),
                        ("slow", 1400000),
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            self.assertEqual(
                read_session_turn_activity(db_file, now_ms=2000000),
                {
                    "s_active": (True, 0),
                    "s_done": (False, 350),
                    "s_tools": (True, 450),
                    "s_abort": (False, 550),
                    "s_order": (True, 0),
                    "s_stale": (False, 0),
                    "s_slow": (True, 0),
                },
            )
            self.assertEqual(
                read_session_turn_activity(
                    db_file,
                    now_ms=2000000,
                    allow_stale=True,
                )["s_stale"],
                (True, 0),
            )

            corrupt = Path(base) / "corrupt.db"
            corrupt.write_bytes(b"this is not a sqlite database")
            self.assertEqual(read_session_turn_activity(corrupt), {})

    async def test_collect_activity_refreshes_without_cli_sweep(self) -> None:
        class PulsedSource(DashboardSource):
            def __init__(self, project_path: Path, db_file: Path) -> None:
                super().__init__(
                    opencode_bin="/bin/false",
                    api_url="http://127.0.0.1:99999",
                    projects_file=project_path.parent / "missing.md",
                    session_routes_file=project_path.parent / "missing-routes.json",
                    briefings_file=project_path.parent / "missing.json",
                    session_db_file=db_file,
                    permission_state_dir=project_path.parent / "permissions",
                )
                self.project_path = project_path
                self.cli_calls = 0
                self.api_busy = False

            async def _api_status(self):
                status = {"ses_live": {"type": "busy"}} if self.api_busy else {}
                return "live", "test", status, {}

            async def _command_json(self, *arguments, cwd=None, timeout=15):
                self.cli_calls += 1
                if arguments[:2] == ("debug", "scrap"):
                    return [
                        {"id": "runtime-id", "worktree": str(self.project_path)}
                    ]
                return [
                    {
                        "id": "ses_live",
                        "title": "Live work",
                        "directory": str(self.project_path),
                        "projectId": "runtime-id",
                        "created": 1,
                        "updated": 2,
                    }
                ]

            async def _service_states(self):
                return ()

        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            project_path = root / "alpha"
            project_path.mkdir()
            db_file = root / "opencode.db"
            connection = sqlite3.connect(db_file)
            try:
                connection.execute(
                    "CREATE TABLE message ("
                    "id TEXT PRIMARY KEY, session_id TEXT, "
                    "time_created INTEGER, data TEXT)"
                )
                connection.execute(
                    "INSERT INTO message VALUES (?, ?, ?, ?)",
                    (
                        "m1",
                        "ses_live",
                        100,
                        '{"role":"assistant","time":{"created":100}}',
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            source = PulsedSource(project_path, db_file)
            self.assertIsNone(await source.collect_activity())

            first = await source.collect()
            self.assertTrue(first.sessions[0].assistant_active)
            cli_after_full_collect = source.cli_calls
            self.assertGreater(cli_after_full_collect, 0)

            with mock.patch(
                "ocdeck.source.read_opencode_instances",
                return_value=({"ses_live": 1}, 0, {}),
            ):
                running = await source.collect_activity()
                self.assertIsNotNone(running)
                self.assertEqual(source.cli_calls, cli_after_full_collect)
                self.assertEqual(agent_state(running.sessions[0]), "stalled")

                connection = sqlite3.connect(db_file)
                try:
                    connection.execute(
                        "UPDATE message SET data = ? WHERE id = ?",
                        (
                            '{"role":"assistant",'
                            '"time":{"created":100,"completed":150},'
                            '"finish":"stop"}',
                            "m1",
                        ),
                    )
                    connection.commit()
                finally:
                    connection.close()

                settled = await source.collect_activity()
                self.assertFalse(settled.sessions[0].assistant_active)
                self.assertEqual(agent_state(settled.sessions[0]), "open")

                source.api_busy = True
                busy = await source.collect_activity()
                self.assertEqual(busy.sessions[0].status, "busy")

            self.assertEqual(source.cli_calls, cli_after_full_collect)

    async def test_archived_sessions_are_hidden_from_all_views(self) -> None:
        class ArchiveSource(DashboardSource):
            def __init__(self, project_path: Path, db_file: Path) -> None:
                super().__init__(
                    opencode_bin="/bin/false",
                    api_url="http://127.0.0.1:99999",
                    projects_file=project_path.parent / "missing.md",
                    session_routes_file=project_path.parent / "missing-routes.json",
                    briefings_file=project_path.parent / "missing.json",
                    session_db_file=db_file,
                    permission_state_dir=project_path.parent / "permissions",
                )
                self.project_path = project_path

            async def _api_status(self):
                return "offline", "test", {}, {}

            async def _command_json(self, *arguments, cwd=None, timeout=15):
                if arguments[:2] == ("debug", "scrap"):
                    return [
                        {"id": "runtime-id", "worktree": str(self.project_path)}
                    ]
                return [
                    {
                        "id": "ses_live",
                        "title": "Live work",
                        "directory": str(self.project_path),
                        "projectId": "runtime-id",
                        "created": 1,
                        "updated": 2,
                    },
                    {
                        "id": "ses_dead",
                        "title": "Archived work",
                        "directory": str(self.project_path),
                        "projectId": "runtime-id",
                        "created": 1,
                        "updated": 3,
                    },
                ]

            async def _service_states(self):
                return ()

        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            project_path = root / "alpha"
            project_path.mkdir()
            db_file = root / "opencode.db"
            write_session_db(db_file, {"ses_dead": 1766588160000})

            snapshot = await ArchiveSource(project_path, db_file).collect()

        self.assertEqual(
            [session.id for session in snapshot.sessions], ["ses_live"]
        )
        self.assertEqual(len(snapshot.projects), 1)
        self.assertEqual(snapshot.projects[0].session_count, 1)
        self.assertEqual(snapshot.warning, "")

    async def test_database_failure_shows_every_session_unfiltered(self) -> None:
        class FailingDbSource(DashboardSource):
            async def _api_status(self):
                return "offline", "test", {}, {}

            async def _command_json(self, *arguments, cwd=None, timeout=15):
                return []

            async def _collect_sessions(self, known_projects):
                return [
                    {
                        "id": "session-1",
                        "title": "Kept",
                        "directory": "/work/kept",
                        "created": 1,
                        "updated": 2,
                    }
                ]

            async def _service_states(self):
                return ()

        source = FailingDbSource(
            opencode_bin="/bin/false",
            session_db_file="/nonexistent-parent/opencode.db",
        )
        snapshot = await source.collect()

        self.assertEqual(
            [session.id for session in snapshot.sessions], ["session-1"]
        )
        self.assertEqual(snapshot.warning, "")

    def test_session_routes_reject_invalid_files(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            routes_file = Path(base) / "routes.json"
            routes_file.write_text(
                '{"sessions":{"session-1":"OC Deck","":"Ignored","bad":7}}'
            )

            self.assertEqual(
                read_session_routes(routes_file),
                {"session-1": "OC Deck"},
            )
            routes_file.write_text("not-json")
            self.assertEqual(read_session_routes(routes_file), {})


if __name__ == "__main__":
    unittest.main()
