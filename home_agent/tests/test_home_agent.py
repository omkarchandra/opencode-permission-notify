from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "home_agent.py"
SPEC = importlib.util.spec_from_file_location("home_agent_controller", MODULE_PATH)
assert SPEC and SPEC.loader
home_agent = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = home_agent
SPEC.loader.exec_module(home_agent)


class FakeAPI:
    def __init__(self) -> None:
        self.created = []
        self.prompts = []

    def agents(self, _directory):
        return {"build", "specialist", "voice-builder"}

    def create_session(
        self, directory, title, agent, metadata=None, permission=None
    ):
        created = {
            "id": "ses_worker",
            "title": title,
            "agent": agent,
            "directory": str(directory),
            "metadata": metadata or {},
            "permission": permission,
        }
        self.created.append(created)
        return created

    def prompt_async(
        self, session_id, directory, agent, prompt, *, tools=None, format=None
    ):
        self.prompts.append((session_id, directory, agent, prompt))


class BriefingFakeAPI:
    def __init__(self, agents=None, create_delay=0.0) -> None:
        self.available_agents = set(agents or {"project-reporter"})
        self.agents_by_directory = {}
        self.create_delay = create_delay
        self.agent_calls = []
        self.created = []
        self.prompts = []
        self.history = []
        self.history_outputs = {}
        self.worker_outputs = {}
        self.statuses = {}
        self.aborted = []
        self.prompt_observer = None
        self.prompt_error = None
        self.create_error = None
        self.agent_barrier = None
        self.active_creates = 0
        self.max_active_creates = 0
        self._next_id = 1
        self._lock = threading.Lock()

    def agents(self, directory):
        path = Path(directory)
        self.agent_calls.append(path)
        if self.agent_barrier:
            self.agent_barrier.wait(timeout=2)
        return set(
            self.agents_by_directory.get(str(path), self.available_agents)
        )

    def list_sessions(self, _directory, limit=50):
        return list(self.history[:limit])

    def messages(self, session_id, _directory, limit=8):
        if session_id in self.history_outputs:
            return self.history_outputs[session_id][-limit:]
        output = self.worker_outputs.get(session_id)
        if output is None:
            return []
        if output[0] == "error":
            return [
                {
                    "info": {
                        "role": "assistant",
                        "error": output[1],
                        "time": {
                            "created": home_agent.now_ms() + 1,
                            "completed": home_agent.now_ms() + 2,
                        },
                        "finish": "stop",
                    },
                    "parts": [],
                }
            ]
        if output[0] == "empty":
            return [
                {
                    "info": {
                        "role": "assistant",
                        "time": {
                            "created": home_agent.now_ms() + 1,
                            "completed": home_agent.now_ms() + 2,
                        },
                        "finish": "stop",
                    },
                    "parts": [],
                }
            ]
        if output[0] == "structured":
            return [
                {
                    "info": {
                        "role": "assistant",
                        "time": {
                            "created": home_agent.now_ms() + 1,
                            "completed": home_agent.now_ms() + 2,
                        },
                        "finish": "stop",
                        "structured": output[1],
                    },
                    "parts": [],
                }
            ]
        return [
            {
                "info": {
                    "role": "assistant",
                    "time": {
                        "created": home_agent.now_ms() + 1,
                        "completed": home_agent.now_ms() + 2,
                    },
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": output[1]}],
            }
        ]

    def create_session(
        self, directory, title, agent, metadata=None, permission=None
    ):
        with self._lock:
            session_id = f"ses_report_{self._next_id}"
            self._next_id += 1
            self.active_creates += 1
            self.max_active_creates = max(
                self.max_active_creates, self.active_creates
            )
        try:
            if self.create_delay:
                time.sleep(self.create_delay)
            created = {
                "id": session_id,
                "directory": str(directory),
                "title": title,
                "agent": agent,
                "metadata": metadata or {},
                "permission": permission,
                "time": {
                    "created": home_agent.now_ms(),
                    "updated": home_agent.now_ms(),
                },
            }
            with self._lock:
                self.created.append(created)
                self.statuses[session_id] = "busy"
                if self.create_error:
                    self.history.append(created.copy())
            if self.create_error:
                raise home_agent.HomeAgentError(self.create_error)
            return created
        finally:
            with self._lock:
                self.active_creates -= 1

    def prompt_async(
        self, session_id, directory, agent, prompt, *, tools=None, format=None
    ):
        if self.prompt_observer:
            self.prompt_observer(session_id)
        with self._lock:
            self.prompts.append(
                (session_id, Path(directory), agent, prompt, tools, format)
            )
        if self.prompt_error:
            raise home_agent.HomeAgentError(self.prompt_error)

    def status(self, _directory):
        return {
            session_id: {"type": status}
            for session_id, status in self.statuses.items()
        }

    def abort_session(self, session_id, directory):
        self.aborted.append((session_id, Path(directory)))
        self.statuses[session_id] = "idle"

    def complete(self, session_id, payload):
        self.statuses[session_id] = "idle"
        self.worker_outputs[session_id] = ("structured", payload)

    def complete_text(self, session_id, payload):
        self.statuses[session_id] = "idle"
        self.worker_outputs[session_id] = ("text", json.dumps(payload))

    def complete_without_output(self, session_id):
        self.statuses[session_id] = "idle"
        self.worker_outputs[session_id] = ("empty", "")

    def fail(self, session_id, message):
        self.statuses[session_id] = "idle"
        self.worker_outputs[session_id] = ("error", message)


class HomeAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "projects" / "project_alpha"
        self.project.mkdir(parents=True)
        self.catalog = self.root / "projects.md"
        self.catalog.write_text(
            """\
# Projects

| Project | Host | Code | Vault note |
| --- | --- | --- | --- |
| Agents Start | laptop | `/work/agents_start` | Projects/agents-start/main.md |
| Home Agent | laptop | `/work/agents_start/home_agent` | Projects/home-agent/main.md |
| Project Alpha | test | `{project}` | Projects/project-alpha/main.md |
""".format(project=self.project),
            encoding="utf-8",
        )
        state = self.root / "state"
        self.settings = home_agent.Settings(
            root=self.root,
            state_file=state / "runtime.json",
            tasks_file=state / "tasks.json",
            lock_file=state / ".lock",
            catalog_file=self.catalog,
            routes_file=state / "routes.json",
            server_env=state / "server.env",
            api_url="http://127.0.0.1:4096",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def set_briefing_catalog(self, count: int, unavailable: int = 0):
        rows = []
        paths = []
        for index in range(1, count + 1):
            path = self.root / "portfolio" / f"project_{index}"
            if index > unavailable:
                path.mkdir(parents=True)
            paths.append(path)
            rows.append(
                f"| Project {index} | laptop | `{path}` | Projects/project-{index}/main.md |"
            )
        self.catalog.write_text(
            "\n".join(
                [
                    "# Projects",
                    "",
                    "| Project | Host | Code | Vault note |",
                    "| --- | --- | --- | --- |",
                    *rows,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return paths

    def reporter_payload(self, **changes):
        payload = {
            "projectID": "model-supplied-id",
            "projectPath": "/model/supplied/path",
            "name": "Model supplied name",
            "assessment": "on-track",
            "summary": "Current outputs are verified.",
            "confidence": "high",
            "evidenceAt": "2026-08-24T12:00:00Z",
            "completedOutputs": [
                {"label": "Result table", "locator": "outputs/results.tsv"}
            ],
            "blockers": [],
            "nextSteps": [
                {
                    "id": "review-results",
                    "title": "Review results",
                    "detail": "Review the result table with the project owner.",
                    "state": "next",
                    "requiresApproval": False,
                }
            ],
            "evidence": [
                {
                    "id": "output-table",
                    "kind": "file",
                    "label": "Generated result table",
                    "observedAt": "2026-08-24T12:00:00+00:00",
                }
            ],
            "researchStatus": "completed",
        }
        payload.update(changes)
        return payload

    def test_catalog_resolution_accepts_name_slug_and_partial_name(self) -> None:
        project = home_agent.resolve_project(self.settings, "Project Alpha")
        self.assertEqual(project.path, self.project)
        self.assertEqual(home_agent.resolve_project(self.settings, "project-alpha"), project)
        self.assertEqual(home_agent.resolve_project(self.settings, "alpha"), project)

    def test_durable_note_resolves_from_vault_root_when_routes_are_in_projects(self) -> None:
        routes = self.root / "vault" / "Projects" / "_session-routes.json"
        settings = home_agent.Settings(
            root=self.settings.root,
            state_file=self.settings.state_file,
            tasks_file=self.settings.tasks_file,
            lock_file=self.settings.lock_file,
            catalog_file=self.settings.catalog_file,
            routes_file=routes,
            server_env=self.settings.server_env,
            api_url=self.settings.api_url,
        )

        result = home_agent.durable_note_path(
            settings, "Projects/project-alpha/main.md"
        )

        self.assertEqual(
            result,
            str(self.root / "vault" / "Projects" / "project-alpha" / "main.md"),
        )

    def test_routed_session_overrides_nested_directory_assignment(self) -> None:
        projects = home_agent.parse_catalog(self.catalog)
        session = {"id": "ses_home", "directory": "/work/agents_start"}

        assigned = home_agent.project_for_session(
            session, projects, {"ses_home": "Home Agent"}
        )

        self.assertIsNotNone(assigned)
        self.assertEqual(assigned.name, "Home Agent")

    def test_explicit_agent_launches_fresh_session_and_updates_state(self) -> None:
        api = FakeAPI()
        project = home_agent.resolve_project(self.settings, "Project Alpha")
        task = home_agent.create_task(
            self.settings, project, "Inspect release inputs", "specialist"
        )

        result = home_agent.launch_worker(
            self.settings,
            api,
            project,
            "Inspect release inputs",
            "specialist",
            "Two earlier sessions generated the current figure.",
            task["id"],
            None,
        )

        self.assertEqual(result["sessionID"], "ses_worker")
        self.assertEqual(result["agent"], "specialist")
        self.assertEqual(len(api.created), 1)
        self.assertEqual(len(api.prompts), 1)
        self.assertIn("Two earlier sessions", api.prompts[0][3])
        self.assertIn("must never delete, move, or rename", api.prompts[0][3])
        created = api.created[0]
        metadata = created["metadata"]["homeAgent"]
        self.assertEqual(metadata["kind"], "project-worker")
        self.assertEqual(metadata["projectPath"], str(self.project))
        self.assertEqual(
            metadata["notePath"],
            str(self.settings.routes_file.parent / "Projects/project-alpha/main.md"),
        )
        rules = created["permission"]
        self.assertFalse(
            any(
                rule["permission"] == "*" and rule["action"] == "deny"
                for rule in rules
            )
        )
        allowed = {
            rule["permission"]
            for rule in rules
            if rule["action"] == "allow"
        }
        self.assertTrue(set(home_agent.WORKER_ALLOWED_PERMISSIONS) <= allowed)
        self.assertIn("task", allowed)
        self.assertIn(
            {
                "permission": "external_directory",
                "pattern": "*",
                "action": "deny",
            },
            rules,
        )
        external = {
            rule["pattern"]
            for rule in rules
            if rule["permission"] == "external_directory"
            and rule["action"] == "allow"
        }
        self.assertEqual(
            external,
            {
                f"{self.project}/**",
                str(self.settings.routes_file.parent / "Projects/project-alpha/*"),
            },
        )
        asked = {
            (rule["permission"], rule["pattern"])
            for rule in rules
            if rule["action"] == "ask"
        }
        self.assertTrue(
            {("read", pattern) for pattern in home_agent.SECRET_READ_PATTERNS}
            <= asked
        )
        self.assertTrue(
            {
                ("bash", pattern)
                for pattern in home_agent.SENSITIVE_BASH_PATTERNS
                if pattern not in home_agent.HARD_DENIED_BASH_PATTERNS
            }
            <= asked
        )
        denied = {
            (rule["permission"], rule["pattern"])
            for rule in rules
            if rule["action"] == "deny"
        }
        self.assertTrue(
            {
                ("bash", pattern)
                for pattern in home_agent.HARD_DENIED_BASH_PATTERNS
            }
            <= denied
        )
        state = json.loads(self.settings.state_file.read_text())
        self.assertEqual(state["projects"]["project-alpha"]["lastAgent"], "specialist")
        tasks = json.loads(self.settings.tasks_file.read_text())["tasks"]
        self.assertEqual(tasks[0]["status"], "running")
        self.assertEqual(tasks[0]["workerSessionID"], "ses_worker")

    def test_preferred_project_agent_is_used_when_no_override_is_given(self) -> None:
        home_agent.write_json(
            self.settings.state_file,
            {
                "version": 1,
                "projects": {
                    "project-alpha": {
                        "name": "Project Alpha",
                        "preferredAgent": "specialist",
                    }
                },
            },
        )
        api = FakeAPI()
        project = home_agent.resolve_project(self.settings, "Project Alpha")

        selected = home_agent.select_agent(self.settings, api, project, None)

        self.assertEqual(selected, "specialist")

    def test_request_id_cannot_be_reused_or_changed_at_launch(self) -> None:
        api = FakeAPI()
        project = home_agent.resolve_project(self.settings, "Project Alpha")
        task = home_agent.create_task(
            self.settings, project, "Inspect release inputs", "specialist"
        )

        with self.assertRaisesRegex(home_agent.HomeAgentError, "does not match"):
            home_agent.launch_worker(
                self.settings,
                api,
                project,
                "Run a different task",
                "specialist",
                "",
                task["id"],
                None,
            )
        self.assertEqual(api.created, [])

        home_agent.launch_worker(
            self.settings,
            api,
            project,
            "Inspect release inputs",
            "specialist",
            "",
            task["id"],
            None,
        )
        with self.assertRaisesRegex(home_agent.HomeAgentError, "cannot launch"):
            home_agent.launch_worker(
                self.settings,
                api,
                project,
                "Inspect release inputs",
                "specialist",
                "",
                task["id"],
                None,
            )
        self.assertEqual(len(api.created), 1)

    def test_home_agent_cannot_launch_itself_as_worker(self) -> None:
        api = FakeAPI()
        project = home_agent.resolve_project(self.settings, "Project Alpha")

        for agent in sorted(home_agent.HOME_ORCHESTRATOR_AGENTS):
            with self.assertRaisesRegex(home_agent.HomeAgentError, "own worker"):
                home_agent.select_agent(self.settings, api, project, agent)

        for agent in (*sorted(home_agent.VOICE_INGRESS_AGENTS), "project-reporter"):
            with self.assertRaisesRegex(home_agent.HomeAgentError, "reserved"):
                home_agent.select_agent(self.settings, api, project, agent)

    def test_briefing_start_validates_and_uses_explicit_agent(self) -> None:
        paths = self.set_briefing_catalog(2)
        unavailable_api = BriefingFakeAPI({"project-reporter"})

        with self.assertRaisesRegex(home_agent.HomeAgentError, "unavailable from /agent"):
            home_agent.start_briefing(
                self.settings, unavailable_api, "portfolio-specialist", 2
            )
        self.assertFalse(self.settings.briefings_file.exists())

        api = BriefingFakeAPI({"portfolio-specialist"})
        run = home_agent.start_briefing(
            self.settings, api, "portfolio-specialist", 0
        )

        self.assertEqual(run["agent"], "portfolio-specialist")
        self.assertEqual(run["maxWorkers"], 1)
        self.assertEqual(api.agent_calls, paths)
        self.assertEqual(len(api.created), 1)
        created = api.created[0]
        self.assertEqual(Path(created["directory"]), paths[0])
        self.assertEqual(created["agent"], "portfolio-specialist")
        metadata = created["metadata"]["homeAgent"]
        self.assertEqual(metadata["kind"], "portfolio-research")
        self.assertEqual(metadata["reportID"], run["reportID"])
        self.assertEqual(metadata["projectID"], "project-1")
        self.assertEqual(metadata["projectKey"], "project-1")
        self.assertEqual(metadata["projectPath"], str(paths[0]))
        self.assertEqual(
            metadata["notePath"],
            str(self.settings.routes_file.parent / "Projects/project-1/main.md"),
        )
        self.assertEqual(metadata["workerAgent"], "portfolio-specialist")
        self.assertEqual(api.prompts[0][2], "portfolio-specialist")
        self.assertIn("Projects/project-1/main.md", api.prompts[0][3])
        self.assertEqual(
            [project["researchStatus"] for project in run["projects"]],
            ["running", "queued"],
        )

    def test_briefing_preflights_agent_in_every_deduplicated_project_path(self) -> None:
        paths = self.set_briefing_catalog(2)
        api = BriefingFakeAPI({"portfolio-specialist"})
        api.agents_by_directory[str(paths[1])] = {"build"}

        with self.assertRaisesRegex(
            home_agent.HomeAgentError, str(paths[1])
        ):
            home_agent.start_briefing(
                self.settings, api, "portfolio-specialist", 2
            )

        self.assertEqual(api.agent_calls, paths)
        self.assertFalse(self.settings.briefings_file.exists())
        self.assertEqual(api.created, [])

        shared = self.root / "portfolio" / "shared"
        shared.mkdir(parents=True)
        self.catalog.write_text(
            f"""\
# Projects

| Project | Host | Code | Vault note |
| --- | --- | --- | --- |
| Shared One | laptop | `{shared}` | Projects/shared-one/main.md |
| Shared Two | laptop | `{shared}` | Projects/shared-two/main.md |
""",
            encoding="utf-8",
        )
        deduplicated = BriefingFakeAPI()

        home_agent.start_briefing(self.settings, deduplicated, max_workers=1)

        self.assertEqual(deduplicated.agent_calls, [shared])

    def test_only_one_briefing_start_can_win_an_atomic_race(self) -> None:
        self.set_briefing_catalog(1)
        api = BriefingFakeAPI()
        api.agent_barrier = threading.Barrier(2)
        successes = []
        errors = []
        result_lock = threading.Lock()

        def start() -> None:
            try:
                result = home_agent.start_briefing(
                    self.settings, api, max_workers=1
                )
            except home_agent.HomeAgentError as error:
                with result_lock:
                    errors.append(str(error))
            else:
                with result_lock:
                    successes.append(result["reportID"])

        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("already running", errors[0])
        state = home_agent.read_briefings_state(self.settings.briefings_file)
        self.assertEqual(list(state["briefings"]), successes)
        self.assertEqual(len(api.created), 1)

    def test_briefing_parser_defaults_and_worker_limit_clamp(self) -> None:
        parser = home_agent.build_parser()
        defaults = parser.parse_args(["briefing", "start"])
        high = parser.parse_args(
            ["briefing", "start", "--max-workers", "99", "--json"]
        )

        self.assertEqual(defaults.agent, "project-reporter")
        self.assertEqual(defaults.max_workers, 3)
        self.assertTrue(high.json)
        self.assertEqual(max(1, min(4, high.max_workers)), 4)

    def test_briefing_start_snapshots_recent_non_reporter_sessions(self) -> None:
        paths = self.set_briefing_catalog(1)
        api = BriefingFakeAPI()
        api.history = [
            {
                "id": "ses_normal",
                "title": "Verified analysis",
                "directory": str(paths[0]),
                "agent": "build",
                "time": {"updated": 1_777_000_000_000},
            },
            {
                "id": "ses_old_reporter",
                "title": "Old portfolio report",
                "directory": str(paths[0]),
                "agent": "custom-reporter",
                "metadata": {"homeAgent": {"kind": "portfolio-research"}},
                "time": {"updated": 1_778_000_000_000},
            },
        ]
        api.history_outputs["ses_normal"] = [
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "Output table is current."}],
            }
        ]

        run = home_agent.start_briefing(self.settings, api, max_workers=1)

        snapshot = run["projects"][0]
        self.assertEqual(snapshot["noteLocation"], "Projects/project-1/main.md")
        self.assertEqual(
            [session["id"] for session in snapshot["recentSessions"]],
            ["ses_normal"],
        )
        self.assertEqual(snapshot["recentSessions"][0]["progress"], "Output table is current.")

    def test_briefing_launches_are_real_concurrent_and_state_bounded(self) -> None:
        self.set_briefing_catalog(5)
        api = BriefingFakeAPI(create_delay=0.05)

        run = home_agent.start_briefing(self.settings, api, max_workers=2)

        statuses = [project["researchStatus"] for project in run["projects"]]
        self.assertEqual(statuses.count("running"), 2)
        self.assertEqual(statuses.count("queued"), 3)
        self.assertEqual(len(api.created), 2)
        self.assertEqual(api.max_active_creates, 2)
        persisted = json.loads(self.settings.briefings_file.read_text())
        persisted_run = persisted["briefings"][run["reportID"]]
        self.assertEqual(
            sum(
                project["researchStatus"] == "running"
                for project in persisted_run["projects"]
            ),
            2,
        )

    def test_session_restrictions_and_id_are_persisted_before_prompt(self) -> None:
        paths = self.set_briefing_catalog(2)
        api = BriefingFakeAPI()
        observed = []

        def observe_prompt(session_id):
            state = home_agent.read_briefings_state(self.settings.briefings_file)
            run = state["briefings"][state["latestReportID"]]
            project = run["projects"][0]
            observed.append(
                (
                    project["workerSessionID"],
                    project["launchState"],
                    bool(project.get("promptedAt")),
                    session_id,
                )
            )

        api.prompt_observer = observe_prompt
        api.prompt_error = "prompt transport timed out"

        run = home_agent.start_briefing(self.settings, api, max_workers=1)

        self.assertEqual(
            observed,
            [(api.created[0]["id"], "prompting", True, api.created[0]["id"])],
        )
        self.assertEqual(run["projects"][0]["researchStatus"], "running")
        self.assertEqual(run["projects"][0]["launchState"], "uncertain")
        self.assertEqual(run["projects"][1]["researchStatus"], "queued")
        self.assertEqual(len(api.created), 1)

        rules = api.created[0]["permission"]
        self.assertEqual(
            rules[0],
            {"permission": "*", "pattern": "*", "action": "deny"},
        )
        allowed = {
            rule["permission"]
            for rule in rules
            if rule["action"] == "allow"
        }
        self.assertTrue(
            {"read", "glob", "list", "webfetch", "websearch"}
            <= allowed
        )
        self.assertNotIn("grep", allowed)
        self.assertTrue(set(home_agent.PLAYWRIGHT_RESEARCH_TOOLS) <= allowed)
        self.assertFalse(set(home_agent.BRIEFING_DENIED_TOOLS) & allowed)
        external = {
            rule["pattern"]
            for rule in rules
            if rule["permission"] == "external_directory"
            and rule["action"] == "allow"
        }
        self.assertEqual(
            external,
            {
                str(paths[0]),
                f"{paths[0]}/**",
                str(self.settings.routes_file.parent / "Projects/project-1/*"),
            },
        )

        tools = api.prompts[0][4]
        output_format = api.prompts[0][5]
        self.assertIsNone(tools)
        self.assertIsNotNone(output_format)
        self.assertEqual(output_format["type"], "json_schema")
        self.assertEqual(output_format["retryCount"], 2)
        schema = output_format["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["nextSteps"]["maxItems"], 12)
        next_step = schema["properties"]["nextSteps"]["items"]
        self.assertFalse(next_step["additionalProperties"])
        self.assertTrue(next_step["properties"]["requiresApproval"]["const"])

    def test_opencode_api_sends_permission_tools_format_and_abort(self) -> None:
        api = home_agent.OpenCodeAPI(self.settings)
        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if path == "/session":
                return {"id": "ses_restricted"}
            if path.endswith("/abort"):
                return True
            return None

        api.request = request
        permission = [
            {"permission": "*", "pattern": "*", "action": "deny"}
        ]
        output_format = {
            "type": "json_schema",
            "schema": {"type": "object"},
            "retryCount": 2,
        }

        api.create_session(
            self.project,
            "Restricted",
            "project-reporter",
            {"homeAgent": {"kind": "portfolio-research"}},
            permission,
        )
        api.prompt_async(
            "ses_restricted",
            self.project,
            "project-reporter",
            "Report",
            tools={"*": False, "read": True},
            format=output_format,
        )
        api.abort_session("ses_restricted", self.project)

        self.assertEqual(calls[0][2]["body"]["permission"], permission)
        self.assertEqual(calls[1][2]["body"]["tools"], {"*": False, "read": True})
        self.assertEqual(calls[1][2]["body"]["format"], output_format)
        self.assertEqual(calls[2][0:2], ("POST", "/session/ses_restricted/abort"))

    def test_unavailable_project_is_unknown_failed_without_launch(self) -> None:
        paths = self.set_briefing_catalog(2, unavailable=1)
        api = BriefingFakeAPI()

        run = home_agent.start_briefing(self.settings, api, max_workers=2)

        self.assertEqual(len(api.created), 1)
        self.assertEqual(Path(api.created[0]["directory"]), paths[1])
        missing = run["projects"][0]
        self.assertEqual(missing["researchStatus"], "failed")
        self.assertEqual(missing["workerSessionID"], "")
        artifact = json.loads((self.settings.reports_dir / "latest.json").read_text())
        missing_record = artifact["projects"][0]
        self.assertEqual(missing_record["assessment"], "unknown")
        self.assertEqual(missing_record["researchStatus"], "failed")
        self.assertIn(str(paths[0]), missing_record["summary"])

    def test_reporter_output_is_validated_capped_and_sanitized(self) -> None:
        project = {
            "projectID": "catalog-id",
            "projectKey": "catalog-id",
            "projectPath": "/catalog/path",
            "name": "Catalog name",
            "noteLocation": "Projects/catalog/main.md",
            "recentSessions": [],
        }
        outputs = [
            {"label": f"Output {index}", "locator": f"out/{index}.txt"}
            for index in range(20)
        ]
        payload = self.reporter_payload(
            summary="\x1b]0;unsafe title\x07Verified\nsummary\u202e",
            completedOutputs=outputs,
        )

        record = home_agent.parse_reporter_output(json.dumps(payload), project)

        self.assertEqual(record["projectID"], "catalog-id")
        self.assertEqual(record["projectPath"], "/catalog/path")
        self.assertEqual(record["name"], "Catalog name")
        self.assertEqual(record["summary"], "Verified summary")
        self.assertEqual(len(record["completedOutputs"]), 12)
        self.assertTrue(record["nextSteps"][0]["requiresApproval"])
        serialized = json.dumps(record, ensure_ascii=False)
        self.assertNotIn("\x1b", serialized)
        self.assertNotIn("\u202e", serialized)

        invalid = self.reporter_payload(evidenceAt="yesterday")
        with self.assertRaisesRegex(home_agent.HomeAgentError, "timestamp"):
            home_agent.parse_reporter_output(json.dumps(invalid), project)

        with mock.patch.object(
            home_agent.json, "loads", side_effect=RecursionError("too deep")
        ):
            with self.assertRaisesRegex(home_agent.HomeAgentError, "invalid JSON"):
                home_agent.reporter_json("{}")

    def test_reporter_prefers_completed_structured_output_after_prompt(self) -> None:
        api = BriefingFakeAPI()
        session_id = "ses_structured"
        prompted_ms = home_agent.now_ms()
        prompted_at = home_agent.precise_iso_time(prompted_ms)
        payload = self.reporter_payload()
        api.history_outputs[session_id] = [
            {
                "info": {
                    "role": "assistant",
                    "time": {
                        "created": prompted_ms - 1000,
                        "completed": prompted_ms - 900,
                    },
                    "finish": "stop",
                    "structured": self.reporter_payload(summary="stale"),
                },
                "parts": [],
            },
            {
                "info": {
                    "role": "assistant",
                    "time": {
                        "created": prompted_ms + 1,
                        "completed": prompted_ms + 2,
                    },
                    "finish": "stop",
                    "structured": payload,
                },
                "parts": [{"type": "text", "text": "not the JSON result"}],
            },
        ]

        raw, error = home_agent.reporter_message_output(
            api, session_id, self.project, prompted_at
        )

        self.assertEqual(error, "")
        self.assertEqual(json.loads(raw), payload)

        api.history_outputs[session_id] = [
            {
                "info": {
                    "role": "assistant",
                    "time": {
                        "created": prompted_ms + 3,
                        "completed": prompted_ms + 4,
                    },
                    "finish": "tool-calls",
                    "structured": payload,
                },
                "parts": [],
            }
        ]
        _, intermediate_error = home_agent.reporter_message_output(
            api, session_id, self.project, prompted_at
        )
        self.assertIn("intermediate", intermediate_error)

        api.history_outputs[session_id][0]["info"]["finish"] = "stop"
        del api.history_outputs[session_id][0]["info"]["time"]["completed"]
        _, incomplete_error = home_agent.reporter_message_output(
            api, session_id, self.project, prompted_at
        )
        self.assertEqual(
            incomplete_error, "Reporter has no completed assistant output"
        )

        api.history_outputs[session_id] = [
            {
                "info": {
                    "role": "assistant",
                    "time": {
                        "created": prompted_ms + 5,
                        "completed": prompted_ms + 6,
                    },
                    "finish": "stop",
                    "error": {
                        "name": "APIError",
                        "data": {"message": "provider failed"},
                    },
                },
                "parts": [],
            }
        ]
        _, provider_error = home_agent.reporter_message_output(
            api, session_id, self.project, prompted_at
        )
        self.assertIn("Reporter session error", provider_error)

        api.history_outputs[session_id] = [
            {
                "info": {
                    "role": "assistant",
                    "time": {
                        "created": prompted_ms + 7,
                        "completed": prompted_ms + 8,
                    },
                    "finish": "stop",
                },
                "parts": [
                    {"type": "text", "text": json.dumps(payload)}
                ],
            }
        ]
        fallback_raw, fallback_error = home_agent.reporter_message_output(
            api, session_id, self.project, prompted_at
        )
        self.assertEqual(fallback_error, "")
        self.assertEqual(json.loads(fallback_raw), payload)

    def test_briefing_state_fails_closed_and_rejects_unsafe_report_ids(self) -> None:
        self.settings.briefings_file.parent.mkdir(parents=True)
        malformed_values = [
            "{",
            "[]",
            '{"version": 2, "latestReportID": "", "briefings": {}}',
            '{"version": 1, "latestReportID": "", "briefings": []}',
            '{"version": 1, "version": 1, "latestReportID": "", "briefings": {}}',
            "[" * 2000 + "0" + "]" * 2000,
        ]
        for source in malformed_values:
            with self.subTest(source=source[:30]):
                self.settings.briefings_file.write_text(source, encoding="utf-8")
                before = self.settings.briefings_file.read_bytes()
                with self.assertRaises(home_agent.HomeAgentError):
                    home_agent.get_briefing_run(self.settings)
                self.assertEqual(self.settings.briefings_file.read_bytes(), before)

        corrupt_history = (
            '{"version": 2, "latestReportID": "old", '
            '"briefings": {"old": {"status": "completed"}}}'
        )
        self.settings.briefings_file.write_text(corrupt_history, encoding="utf-8")
        api = BriefingFakeAPI()
        self.set_briefing_catalog(1)
        with self.assertRaisesRegex(home_agent.HomeAgentError, "version"):
            home_agent.start_briefing(self.settings, api)
        self.assertEqual(
            self.settings.briefings_file.read_text(encoding="utf-8"), corrupt_history
        )
        self.assertEqual(api.agent_calls, [])

        home_agent.write_json(
            self.settings.briefings_file, home_agent.briefings_default()
        )
        with self.assertRaisesRegex(home_agent.HomeAgentError, "Invalid briefing"):
            home_agent.get_briefing_run(self.settings, "../outside")
        with self.assertRaisesRegex(home_agent.HomeAgentError, "Invalid briefing"):
            home_agent.publish_briefing(self.settings, "../outside")
        self.assertFalse(self.settings.reports_dir.exists())

    def test_monitor_harvests_then_backfills_until_completed(self) -> None:
        self.set_briefing_catalog(3)
        api = BriefingFakeAPI()
        run = home_agent.start_briefing(self.settings, api, max_workers=2)
        first_sessions = [session["id"] for session in api.created]
        for session_id in first_sessions:
            api.complete(session_id, self.reporter_payload())

        first_tick = home_agent.advance_briefing(
            self.settings, api, run["reportID"]
        )

        progressed = home_agent.get_briefing_run(self.settings, run["reportID"])
        statuses = [project["researchStatus"] for project in progressed["projects"]]
        self.assertEqual(statuses.count("completed"), 2)
        self.assertEqual(statuses.count("running"), 1)
        self.assertEqual(len(first_tick["harvested"]), 2)
        self.assertEqual(len(first_tick["launched"]), 1)
        self.assertEqual(len(api.created), 3)

        final_session = api.created[-1]["id"]
        api.complete(final_session, self.reporter_payload(assessment="complete"))
        second_tick = home_agent.advance_briefing(
            self.settings, api, run["reportID"]
        )

        final_run = home_agent.get_briefing_run(self.settings, run["reportID"])
        self.assertEqual(second_tick["status"], "completed")
        self.assertEqual(final_run["status"], "completed")
        artifact = json.loads((self.settings.reports_dir / "latest.json").read_text())
        self.assertEqual(artifact["status"], "completed")
        self.assertTrue(
            all(project["researchStatus"] == "completed" for project in artifact["projects"])
        )

    def test_monitor_adopts_session_by_briefing_metadata(self) -> None:
        self.set_briefing_catalog(1)
        api = BriefingFakeAPI()
        run = home_agent.start_briefing(self.settings, api, max_workers=1)
        created = api.created[0]
        api.history = [created.copy()]
        api.complete(created["id"], self.reporter_payload())

        def remove_session_id(current):
            project = current["projects"][0]
            project["workerSessionID"] = ""
            project["launchState"] = "launching"

        home_agent.mutate_briefing(
            self.settings, run["reportID"], remove_session_id
        )

        result = home_agent.advance_briefing(
            self.settings, api, run["reportID"]
        )

        adopted = home_agent.get_briefing_run(self.settings, run["reportID"])
        project = adopted["projects"][0]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(project["workerSessionID"], created["id"])
        self.assertTrue(project.get("adoptedAt"))
        self.assertEqual(project["researchStatus"], "completed")

    def test_ambiguous_create_without_id_keeps_slot_until_metadata_adoption(self) -> None:
        self.set_briefing_catalog(2)
        api = BriefingFakeAPI()
        api.create_error = "create response was lost"

        run = home_agent.start_briefing(self.settings, api, max_workers=1)

        first = run["projects"][0]
        self.assertEqual(first["researchStatus"], "running")
        self.assertEqual(first["launchState"], "uncertain")
        self.assertEqual(first["workerSessionID"], "")
        self.assertEqual(run["projects"][1]["researchStatus"], "queued")
        self.assertEqual(len(api.created), 1)
        self.assertEqual(api.prompts, [])

        home_agent.advance_briefing(self.settings, api, run["reportID"])

        adopted = home_agent.get_briefing_run(self.settings, run["reportID"])
        self.assertEqual(
            adopted["projects"][0]["workerSessionID"], api.created[0]["id"]
        )
        self.assertEqual(adopted["projects"][0]["researchStatus"], "running")
        self.assertEqual(adopted["projects"][1]["researchStatus"], "queued")
        self.assertEqual(len(api.created), 1)

    def test_monitor_aborts_and_fails_reporter_at_deadline(self) -> None:
        paths = self.set_briefing_catalog(1)
        api = BriefingFakeAPI()
        run = home_agent.start_briefing(self.settings, api, max_workers=1)
        session_id = api.created[0]["id"]

        def expire(current):
            current["projects"][0]["deadlineAt"] = home_agent.iso_time(
                home_agent.now_ms() - 1000
            )

        home_agent.mutate_briefing(self.settings, run["reportID"], expire)

        result = home_agent.advance_briefing(
            self.settings, api, run["reportID"]
        )

        expired = home_agent.get_briefing_run(self.settings, run["reportID"])
        project = expired["projects"][0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(api.aborted, [(session_id, paths[0])])
        self.assertEqual(project["researchStatus"], "failed")
        self.assertTrue(project.get("deadlineExceededAt"))
        self.assertIn("30-minute deadline", project["error"])

    def test_monitor_publishes_partial_with_unknown_failed_record(self) -> None:
        self.set_briefing_catalog(2)
        api = BriefingFakeAPI()
        run = home_agent.start_briefing(self.settings, api, max_workers=2)
        api.complete(api.created[0]["id"], self.reporter_payload())
        api.complete_without_output(api.created[1]["id"])

        result = home_agent.advance_briefing(self.settings, api, run["reportID"])

        self.assertEqual(result["status"], "partial")
        artifact = json.loads((self.settings.reports_dir / "latest.json").read_text())
        self.assertEqual(artifact["status"], "partial")
        failed = next(
            project
            for project in artifact["projects"]
            if project["researchStatus"] == "failed"
        )
        self.assertEqual(failed["assessment"], "unknown")
        self.assertEqual(failed["confidence"], "low")
        self.assertIn("without a text or structured output", failed["summary"])

    def test_running_artifacts_render_json_mermaid_and_terminal_progress(self) -> None:
        self.set_briefing_catalog(3)
        api = BriefingFakeAPI()
        run = home_agent.start_briefing(self.settings, api, max_workers=1)
        report_dir = self.settings.reports_dir / run["reportID"]

        artifact = json.loads((report_dir / "report.json").read_text())
        markdown = (report_dir / "report.md").read_text()
        terminal = (report_dir / "report.txt").read_text()

        self.assertEqual(
            set(artifact),
            {"schemaVersion", "reportID", "generatedAt", "status", "projects"},
        )
        self.assertEqual(
            set(artifact["projects"][0]),
            {
                "projectID",
                "projectPath",
                "name",
                "assessment",
                "summary",
                "confidence",
                "evidenceAt",
                "completedOutputs",
                "blockers",
                "nextSteps",
                "evidence",
                "researchStatus",
            },
        )
        self.assertIn("```mermaid", markdown)
        self.assertIn("class p0 running", markdown)
        self.assertIn("class p1 queued", markdown)
        self.assertIn("[>] Project 1 (running; unknown)", terminal)
        self.assertIn("[.] Project 2 (queued; unknown)", terminal)
        self.assertEqual(markdown, home_agent.render_report_markdown(artifact))
        self.assertEqual(terminal, home_agent.render_report_text(artifact))
        self.assertEqual(
            artifact,
            json.loads((self.settings.reports_dir / "latest.json").read_text()),
        )
        self.assertEqual(
            markdown, (self.settings.reports_dir / "latest.md").read_text()
        )
        self.assertEqual(
            terminal, (self.settings.reports_dir / "latest.txt").read_text()
        )

    def test_generated_next_steps_are_never_automatically_executed(self) -> None:
        self.set_briefing_catalog(1)
        original_tasks = {"version": 1, "tasks": []}
        home_agent.write_json(self.settings.tasks_file, original_tasks)
        api = BriefingFakeAPI()
        run = home_agent.start_briefing(self.settings, api, max_workers=1)
        api.complete(api.created[0]["id"], self.reporter_payload())

        home_agent.monitor_briefings(self.settings, api)

        self.assertEqual(json.loads(self.settings.tasks_file.read_text()), original_tasks)
        self.assertEqual(len(api.created), 1)
        self.assertEqual(len(api.prompts), 1)
        artifact = json.loads((self.settings.reports_dir / "latest.json").read_text())
        self.assertTrue(artifact["projects"][0]["nextSteps"][0]["requiresApproval"])
        self.assertEqual(
            home_agent.get_briefing_run(self.settings, run["reportID"])["status"],
            "completed",
        )

    def test_briefing_status_and_show_are_read_only(self) -> None:
        self.set_briefing_catalog(1)
        api = BriefingFakeAPI()
        run = home_agent.start_briefing(self.settings, api, max_workers=1)
        state_before = self.settings.briefings_file.read_bytes()
        reports_before = {
            path.relative_to(self.settings.reports_dir): path.read_bytes()
            for path in self.settings.reports_dir.rglob("*")
            if path.is_file()
        }
        status_args = home_agent.argparse.Namespace(
            report_id=run["reportID"], json=True
        )
        show_args = home_agent.argparse.Namespace(report_id=None, json=False)

        with contextlib.redirect_stdout(io.StringIO()):
            home_agent.command_briefing_status(self.settings, api, status_args)
            home_agent.command_briefing_show(self.settings, api, show_args)

        reports_after = {
            path.relative_to(self.settings.reports_dir): path.read_bytes()
            for path in self.settings.reports_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(self.settings.briefings_file.read_bytes(), state_before)
        self.assertEqual(reports_after, reports_before)


if __name__ == "__main__":
    unittest.main()
