from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import (
    BriefingReportRecord,
    DashboardSnapshot,
    MAX_LAST_PROMPT_LENGTH,
    NextStepRecord,
    ProjectBriefingRecord,
    ProjectRecord,
    ServiceRecord,
    SystemMetrics,
    apply_session_routes,
    assign_project_roots,
    build_projects,
    clean_int,
    clean_string,
    normalize_status,
    parse_known_projects,
    parse_sessions,
    sanitize_terminal_text,
)


SERVICE_ALLOWLIST = (
    ("opencode-web.service", "OpenCode Web", "local API and web client"),
    ("voice-agent.service", "Voice Agent", "voice task intake"),
    ("home-agent-monitor.timer", "Home Agent", "project task monitor"),
    ("rclone-google-drive.service", "Google Drive", "rclone mount"),
    ("kinesis-dictionary.service", "Dictionary", "mouse lookup listener"),
    ("ydotool.service", "Input Bridge", "local input automation"),
)

MAX_API_RESPONSE_BYTES = 1024 * 1024
MAX_SESSION_CACHE_BYTES = 4 * 1024 * 1024
SESSION_CACHE_TTL_MS = 10 * 1000
SESSION_CACHE_LOCK_STALE_SECONDS = 60
MAX_BRIEFINGS_FILE_BYTES = 1024 * 1024
MAX_BRIEFING_PROJECTS = 256
MAX_BRIEFING_ARRAY_ITEMS = 64
MAX_BRIEFING_EVIDENCE_ITEMS = 128
MAX_BRIEFING_IDENTIFIER_LENGTH = 256
MAX_BRIEFING_TEXT_LENGTH = 4096
MAX_BRIEFING_PATH_LENGTH = 4096
DEFAULT_BRIEFINGS_FILE = (
    Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    / "home-agent/reports/latest.json"
)
DEFAULT_SESSION_DB_FILE = (
    Path.home() / ".local/share/opencode/opencode.db"
)
RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)
DEFAULT_PROJECTS_FILE = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "home-agent/projects.md"
)
NON_TUI_SUBCOMMANDS = {
    "acp",
    "agent",
    "auth",
    "completion",
    "db",
    "debug",
    "export",
    "github",
    "import",
    "mcp",
    "models",
    "plugin",
    "plug",
    "pr",
    "providers",
    "run",
    "serve",
    "session",
    "stats",
    "uninstall",
    "upgrade",
    "web",
}
PANE_ID_PATTERN = re.compile(r"^%[0-9]+$")


@dataclass(frozen=True, slots=True)
class OpenCodeProcess:
    pid: int
    session_id: str
    tty: str
    start_time: int


@dataclass(frozen=True, slots=True)
class LiveOpenCodePane:
    destination_id: str
    pane_id: str
    session_id: str
    session_name: str
    window_index: str
    pane_index: str
    terminal_state: str


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class DashboardSource:
    def __init__(
        self,
        *,
        opencode_bin: str | None = None,
        api_url: str | None = None,
        limit: int = 100,
        projects_file: str | Path | None = None,
        session_routes_file: str | Path | None = None,
        briefings_file: str | Path | None = None,
        session_db_file: str | Path | None = None,
        permission_state_dir: str | Path | None = None,
    ) -> None:
        self.opencode_bin = opencode_bin or self._find_opencode()
        self.api_url = (
            api_url or os.environ.get("OPENCODE_URL") or "http://127.0.0.1:4096"
        ).rstrip("/")
        self.api_url_error = validate_api_url(self.api_url)
        self.limit = max(1, min(limit, 500))
        configured_projects_file = (
            projects_file
            or os.environ.get("OCDECK_PROJECTS_FILE")
            or DEFAULT_PROJECTS_FILE
        )
        self.projects_file = Path(configured_projects_file).expanduser()
        self.session_routes_file = Path(
            session_routes_file
            or os.environ.get("OCDECK_SESSION_ROUTES_FILE")
            or find_vault_root(self.projects_file) / "Projects/_session-routes.json"
        ).expanduser()
        self.briefings_file = Path(
            briefings_file
            or os.environ.get("OCDECK_BRIEFINGS_FILE")
            or DEFAULT_BRIEFINGS_FILE
        ).expanduser()
        self.session_db_file = Path(
            session_db_file
            or os.environ.get("OCDECK_SESSION_DB_FILE")
            or DEFAULT_SESSION_DB_FILE
        ).expanduser()
        self.permission_state_dir = Path(
            permission_state_dir or default_permission_state_dir()
        ).expanduser()
        self._activity_cache: dict[str, Any] | None = None

    async def collect(self) -> DashboardSnapshot:
        status_task = asyncio.create_task(self._api_status())
        projects_task = asyncio.create_task(
            self._command_json("debug", "scrap", "--pure")
        )
        catalog_task = asyncio.create_task(
            asyncio.to_thread(read_markdown_projects, self.projects_file)
        )
        routes_task = asyncio.create_task(
            asyncio.to_thread(read_session_routes, self.session_routes_file)
        )
        briefings_task = asyncio.create_task(
            asyncio.to_thread(read_briefings_file, self.briefings_file)
        )
        services_task = asyncio.create_task(self._service_states())
        metrics_task = asyncio.create_task(asyncio.to_thread(read_system_metrics))
        instances_task = asyncio.create_task(
            asyncio.to_thread(read_opencode_instances)
        )
        local_permissions_task = asyncio.create_task(
            asyncio.to_thread(read_local_permissions, self.permission_state_dir)
        )
        local_statuses_task = asyncio.create_task(
            asyncio.to_thread(read_local_statuses, self.permission_state_dir)
        )

        (
            status_result,
            projects_result,
            catalog,
            routes,
            briefing_source,
            services,
            metrics,
            instance_result,
            local_permissions,
            local_statuses,
        ) = await asyncio.gather(
            status_task,
            projects_task,
            catalog_task,
            routes_task,
            briefings_task,
            services_task,
            metrics_task,
            instances_task,
            local_permissions_task,
            local_statuses_task,
        )

        known_projects, project_names = merge_project_catalog(
            parse_known_projects(projects_result), catalog
        )
        archived_task = asyncio.create_task(
            asyncio.to_thread(read_archived_session_ids, self.session_db_file)
        )
        interactions_task = asyncio.create_task(
            asyncio.to_thread(read_last_user_interactions, self.session_db_file)
        )
        turns_task = asyncio.create_task(
            asyncio.to_thread(
                read_session_turn_activity,
                self.session_db_file,
                allow_stale=True,
            )
        )
        agent_parents_task = asyncio.create_task(
            asyncio.to_thread(read_agent_parent_ids, self.session_db_file)
        )
        sessions_result = await self._collect_sessions_shared(known_projects)
        (
            archived_ids,
            last_interactions,
            turn_activity,
            agent_parent_ids,
        ) = await asyncio.gather(
            archived_task,
            interactions_task,
            turns_task,
            agent_parents_task,
        )
        turn_activity = dict(turn_activity)
        for session_id, status in local_statuses.items():
            if status == "idle":
                turn_activity[session_id] = (
                    False,
                    turn_activity.get(session_id, (False, 0))[1],
                )
        if sessions_result and archived_ids:
            sessions_result = [
                item
                for item in sessions_result
                if isinstance(item, dict) and item.get("id") not in archived_ids
            ]

        connection, connection_detail, statuses, permissions = status_result
        statuses = {**statuses, **local_statuses}
        permissions = merge_permissions(permissions, local_permissions)
        instance_counts, unmapped_instances, instance_ttys = instance_result
        prompts_task = asyncio.create_task(
            asyncio.to_thread(
                read_latest_user_prompts,
                self.session_db_file,
                live_session_ids(
                    statuses, instance_counts, permissions, sessions_result
                ),
            )
        )
        terminal_map, attached_map = await asyncio.to_thread(
            read_tmux_tty_state, instance_ttys
        )
        latest_prompts = await prompts_task
        sessions = parse_sessions(
            sessions_result,
            statuses,
            instance_counts,
            terminal_map,
            permissions,
            attached_map,
            last_interactions,
            turn_activity,
            latest_prompts,
            agent_parent_ids,
        )
        sessions = assign_project_roots(sessions, known_projects)
        sessions = apply_session_routes(sessions, routes, project_names)
        projects = build_projects(sessions, known_projects, project_names)
        briefing_report = parse_briefings(briefing_source)
        briefings = (
            match_project_briefings(briefing_report.projects, projects)
            if briefing_report is not None
            else ()
        )
        warning = "" if sessions_result is not None else "Session metadata unavailable"
        if sessions_result is not None:
            # Cache the heavy collection results so collect_activity() can
            # refresh live signals later without rerunning CLI sweeps.
            self._activity_cache = {
                "sessions": sessions_result,
                "known_projects": known_projects,
                "project_names": project_names,
                "routes": routes,
                "agent_parent_ids": agent_parent_ids,
                "services": services,
                "metrics": metrics,
                "briefings": briefings,
                "briefing_report_id": (
                    briefing_report.report_id if briefing_report is not None else ""
                ),
                "briefing_generated_at": (
                    briefing_report.generated_at
                    if briefing_report is not None
                    else None
                ),
                "briefing_status": (
                    briefing_report.status if briefing_report is not None else ""
                ),
            }
        return DashboardSnapshot(
            sessions=sessions,
            projects=projects,
            services=services,
            metrics=metrics,
            connection=connection,
            connection_detail=connection_detail,
            unmapped_instance_count=unmapped_instances,
            warning=warning,
            briefings=briefings,
            briefing_report_id=(
                briefing_report.report_id if briefing_report is not None else ""
            ),
            briefing_generated_at=(
                briefing_report.generated_at if briefing_report is not None else None
            ),
            briefing_status=(
                briefing_report.status if briefing_report is not None else ""
            ),
        )

    async def collect_activity(self) -> DashboardSnapshot | None:
        """Refresh live agent signals without rerunning CLI collection.

        Combines the session payload cached by the last full ``collect()``
        with fast local signals only: HTTP status and permissions, process
        liveness, tmux mapping, and read-only session database metadata.
        Returns ``None`` until the first full collection populated the cache.
        """
        cache = self._activity_cache
        if cache is None:
            return None
        status_task = asyncio.create_task(self._api_status())
        instances_task = asyncio.create_task(
            asyncio.to_thread(read_opencode_instances)
        )
        local_permissions_task = asyncio.create_task(
            asyncio.to_thread(read_local_permissions, self.permission_state_dir)
        )
        local_statuses_task = asyncio.create_task(
            asyncio.to_thread(read_local_statuses, self.permission_state_dir)
        )
        activity_task = asyncio.create_task(
            asyncio.to_thread(
                read_session_turn_activity,
                self.session_db_file,
                allow_stale=True,
            )
        )
        interactions_task = asyncio.create_task(
            asyncio.to_thread(read_last_user_interactions, self.session_db_file)
        )
        connection, connection_detail, statuses, permissions = await status_task
        instance_counts, unmapped_instances, instance_ttys = await instances_task
        local_permissions, local_statuses, turn_activity, last_interactions = await asyncio.gather(
            local_permissions_task,
            local_statuses_task,
            activity_task,
            interactions_task,
        )
        turn_activity = dict(turn_activity)
        for session_id, status in local_statuses.items():
            if status == "idle":
                turn_activity[session_id] = (
                    False,
                    turn_activity.get(session_id, (False, 0))[1],
                )
        statuses = {**statuses, **local_statuses}
        permissions = merge_permissions(permissions, local_permissions)
        prompts_task = asyncio.create_task(
            asyncio.to_thread(
                read_latest_user_prompts,
                self.session_db_file,
                live_session_ids(
                    statuses, instance_counts, permissions, cache["sessions"]
                ),
            )
        )
        terminal_map, attached_map = await asyncio.to_thread(
            read_tmux_tty_state, instance_ttys
        )
        latest_prompts = await prompts_task
        known_projects = cache["known_projects"]
        project_names = cache["project_names"]
        sessions = parse_sessions(
            cache["sessions"],
            statuses,
            instance_counts,
            terminal_map,
            permissions,
            attached_map,
            last_interactions,
            turn_activity,
            latest_prompts,
            cache["agent_parent_ids"],
        )
        sessions = assign_project_roots(sessions, known_projects)
        sessions = apply_session_routes(sessions, cache["routes"], project_names)
        projects = build_projects(sessions, known_projects, project_names)
        return DashboardSnapshot(
            sessions=sessions,
            projects=projects,
            services=cache["services"],
            metrics=cache["metrics"],
            connection=connection,
            connection_detail=connection_detail,
            unmapped_instance_count=unmapped_instances,
            briefings=cache["briefings"],
            briefing_report_id=cache["briefing_report_id"],
            briefing_generated_at=cache["briefing_generated_at"],
            briefing_status=cache["briefing_status"],
        )

    async def _collect_sessions(
        self, known_projects: dict[str, str]
    ) -> list[Any] | None:
        directories: list[Path] = [Path.cwd(), Path.home()]
        for directory in known_projects.values():
            path = Path(directory)
            if path.is_dir() and path not in directories:
                directories.append(path)

        semaphore = asyncio.Semaphore(3)
        password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
        api_available = not self.api_url_error and (
            not password or api_credentials_are_safe(self.api_url)
        )

        async def collect_directory(path: Path) -> Any:
            async with semaphore:
                if api_available:
                    query = urllib.parse.urlencode(
                        {
                            "directory": str(path),
                            "roots": "false",
                            "limit": self.limit,
                        }
                    )
                    try:
                        payload = await asyncio.to_thread(
                            self._request_json,
                            f"/session?{query}",
                            password,
                        )
                        payload = unwrap_data(payload)
                        if isinstance(payload, list):
                            return payload
                    except (
                        OSError,
                        ValueError,
                        urllib.error.HTTPError,
                        urllib.error.URLError,
                        TimeoutError,
                    ):
                        pass
                payload = await self._command_json(
                    "session",
                    "list",
                    "--format",
                    "json",
                    "--max-count",
                    str(self.limit),
                    "--pure",
                    cwd=path,
                    timeout=30,
                )
                if isinstance(payload, list):
                    return payload
                await asyncio.sleep(0.2)
                return await self._command_json(
                    "session",
                    "list",
                    "--format",
                    "json",
                    "--max-count",
                    str(self.limit),
                    "--pure",
                    cwd=path,
                    timeout=30,
                )

        results = await asyncio.gather(
            *(collect_directory(path) for path in directories)
        )

        merged: dict[str, Any] = {}
        any_succeeded = False
        for payload in results:
            if not isinstance(payload, list):
                continue
            any_succeeded = True
            for item in payload:
                if not isinstance(item, dict):
                    continue
                session_id = item.get("id")
                if not isinstance(session_id, str) or not session_id:
                    continue
                existing = merged.get(session_id)
                if (
                    existing is None
                    or clean_int(item.get("updated"))
                    >= clean_int(existing.get("updated"))
                ):
                    merged[session_id] = item
        return list(merged.values()) if any_succeeded else None

    def _session_cache_path(self, known_projects: dict[str, str]) -> Path:
        identity = "\0".join(
            [
                self.opencode_bin or "",
                str(self.limit),
                str(self.projects_file),
                str(self.session_db_file),
                *(f"{key}={value}" for key, value in sorted(known_projects.items())),
            ]
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        return default_permission_state_dir().parent / f"ocdeck-sessions-{digest}.json"

    @staticmethod
    def _read_shared_session_cache(path: Path) -> list[Any] | None:
        try:
            age_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - int(
                path.stat().st_mtime * 1000
            )
            if age_ms > SESSION_CACHE_TTL_MS:
                return None
            content = path.read_bytes()
            if len(content) > MAX_SESSION_CACHE_BYTES:
                return None
            payload = json.loads(content)
            sessions = payload.get("sessions") if isinstance(payload, dict) else None
            return sessions if isinstance(sessions, list) else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_shared_session_cache(path: Path, sessions: list[Any]) -> None:
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary.write_text(
                json.dumps(
                    {
                        "created_ms": int(
                            datetime.now(timezone.utc).timestamp() * 1000
                        ),
                        "sessions": sessions,
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except (OSError, TypeError, ValueError):
            with contextlib.suppress(OSError):
                temporary.unlink()

    async def _collect_sessions_shared(
        self, known_projects: dict[str, str]
    ) -> list[Any] | None:
        """Share the expensive session-list sweep between OC Deck windows."""
        cache_path = self._session_cache_path(known_projects)
        lock_path = cache_path.with_suffix(".lock")
        while True:
            cached = self._read_shared_session_cache(cache_path)
            if cached is not None:
                return cached
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    if (
                        datetime.now(timezone.utc).timestamp() - lock_path.stat().st_mtime
                        > SESSION_CACHE_LOCK_STALE_SECONDS
                    ):
                        lock_path.unlink()
                        continue
                except OSError:
                    pass
                await asyncio.sleep(0.1)
                continue
            except OSError:
                return await self._collect_sessions(known_projects)
            try:
                cached = self._read_shared_session_cache(cache_path)
                if cached is not None:
                    return cached
                sessions = await self._collect_sessions(known_projects)
                if sessions is not None:
                    self._write_shared_session_cache(cache_path, sessions)
                return sessions
            finally:
                os.close(lock_fd)
                with contextlib.suppress(OSError):
                    lock_path.unlink()

    async def _command_json(
        self,
        *arguments: str,
        cwd: Path | None = None,
        timeout: float = 15,
    ) -> Any:
        if not self.opencode_bin:
            return None
        try:
            process = await asyncio.create_subprocess_exec(
                self.opencode_bin,
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(cwd) if cwd else None,
            )
            result = await communicate_with_cleanup(process, timeout=timeout)
        except OSError:
            return None
        if result is None:
            return None
        stdout, _ = result
        if process.returncode != 0:
            return None
        try:
            return json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None

    async def _api_status(
        self,
    ) -> tuple[str, str, dict[str, str], dict[str, list[dict[str, str]]]]:
        password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
        if self.api_url_error:
            return "blocked", self.api_url_error, {}, {}
        if password and not api_credentials_are_safe(self.api_url):
            return "blocked", "Refusing credentials over non-loopback HTTP", {}, {}
        try:
            health = await asyncio.to_thread(self._request_json, "/global/health", password)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                if password:
                    return "locked", "API rejected the configured credentials", {}, {}
                return (
                    "locked",
                    "API locked; set OPENCODE_SERVER_PASSWORD for agent states",
                    {},
                    {},
                )
            return "offline", f"API returned HTTP {error.code}", {}, {}
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
            TimeoutError,
        ):
            return "offline", "API offline; CLI metadata active", {}, {}

        health_data = unwrap_data(health)
        if not isinstance(health_data, dict) or health_data.get("healthy") is not True:
            return "offline", "API health response was invalid", {}, {}

        statuses: dict[str, str] = {}
        try:
            payload = await asyncio.to_thread(
                self._request_json,
                "/session/status",
                password,
            )
            data = unwrap_data(payload)
            if isinstance(data, dict):
                statuses = {
                    str(session_id): normalize_status(value)
                    for session_id, value in data.items()
                }
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
        ):
            pass

        permissions, questions = await asyncio.gather(
            self._api_permissions(password), self._api_questions(password)
        )
        permissions = merge_permissions(permissions, questions)

        version = ""
        version_value = health_data.get("version")
        if isinstance(version_value, str):
            version = clean_string(version_value)
        detail = "Live API" + (f" v{version}" if version else "")
        return "live", detail, statuses, permissions

    async def _api_permissions(
        self, password: str
    ) -> dict[str, list[dict[str, str]]]:
        try:
            payload = await asyncio.to_thread(
                self._request_json,
                "/permission",
                password,
            )
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
        ):
            return {}
        data = unwrap_data(payload)
        if not isinstance(data, list):
            return {}
        pending: dict[str, list[dict[str, str]]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            session_id = clean_string(item.get("sessionID"))
            if not session_id:
                continue
            patterns = item.get("patterns")
            pattern = ""
            if isinstance(patterns, list) and patterns:
                pattern = clean_string(str(patterns[0]))
            pending.setdefault(session_id, []).append(
                {
                    "id": clean_string(item.get("id")),
                    "permission": clean_string(item.get("permission"))
                    or clean_string(item.get("type"))
                    or "permission",
                    "pattern": pattern,
                }
            )
        return pending

    async def _api_questions(
        self, password: str
    ) -> dict[str, list[dict[str, str]]]:
        try:
            payload = await asyncio.to_thread(
                self._request_json,
                "/question",
                password,
            )
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
        ):
            return {}
        data = unwrap_data(payload)
        if not isinstance(data, list):
            return {}
        pending: dict[str, list[dict[str, str]]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            session_id = clean_string(item.get("sessionID"))
            if not session_id:
                continue
            prompt = ""
            questions = item.get("questions")
            if isinstance(questions, list):
                for question in questions:
                    if not isinstance(question, dict):
                        continue
                    prompt = clean_string(question.get("question")) or clean_string(
                        question.get("header")
                    )
                    if prompt:
                        break
            pending.setdefault(session_id, []).append(
                {
                    "id": clean_string(item.get("id")),
                    "permission": "question",
                    "pattern": prompt or "Input required in the terminal",
                }
            )
        return pending

    async def rename_session(self, session_id: str, title: str) -> str:
        """Update a session title through the loopback API.

        Returns an empty string on success, or a human-readable error message.
        """
        password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
        if self.api_url_error:
            return self.api_url_error
        if password and not api_credentials_are_safe(self.api_url):
            return "Refusing credentials over non-loopback HTTP"
        try:
            await asyncio.to_thread(
                self._request_json,
                f"/session/{urllib.parse.quote(session_id, safe='')}",
                password,
                method="PATCH",
                payload={"title": title},
            )
        except urllib.error.HTTPError as error:
            if error.code == 401:
                if password:
                    return "API rejected the configured credentials"
                return "API locked; set OPENCODE_SERVER_PASSWORD to rename"
            return f"API returned HTTP {error.code}"
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
            TimeoutError,
        ):
            return "API unavailable; title kept unchanged"
        return ""

    async def approve_permission(self, session_id: str, permission_id: str) -> str:
        """Approve one pending permission through OpenCode's loopback API."""
        password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
        if self.api_url_error:
            return self.api_url_error
        if password and not api_credentials_are_safe(self.api_url):
            return "Refusing credentials over non-loopback HTTP"
        path = (
            f"/session/{urllib.parse.quote(session_id, safe='')}/permissions/"
            f"{urllib.parse.quote(permission_id, safe='')}"
        )
        try:
            await asyncio.to_thread(
                self._request_json,
                path,
                password,
                method="POST",
                payload={"response": "once"},
            )
        except urllib.error.HTTPError as error:
            if error.code == 401:
                if password:
                    return "API rejected the configured credentials"
                return "API locked; set OPENCODE_SERVER_PASSWORD to approve"
            return f"API returned HTTP {error.code}"
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
            TimeoutError,
        ):
            return "API unavailable; permission was not approved"
        return ""

    def _request_json(
        self,
        path: str,
        password: str,
        *,
        method: str = "GET",
        payload: Any = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if password:
            username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        request = urllib.request.Request(
            self.api_url + path, data=data, headers=headers, method=method
        )
        opener = urllib.request.build_opener(NoRedirectHandler())
        with opener.open(request, timeout=2.5) as response:
            content = response.read(MAX_API_RESPONSE_BYTES + 1)
        if len(content) > MAX_API_RESPONSE_BYTES:
            raise ValueError("API response exceeded the size limit")
        return json.loads(content.decode("utf-8")) if content else None

    async def _service_states(self) -> tuple[ServiceRecord, ...]:
        units = [unit for unit, _, _ in SERVICE_ALLOWLIST]
        try:
            process = await asyncio.create_subprocess_exec(
                "systemctl",
                "--user",
                "is-active",
                *units,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            result = await communicate_with_cleanup(process, timeout=5)
            states = (
                result[0].decode("utf-8", errors="replace").splitlines()
                if result is not None
                else []
            )
        except OSError:
            states = []

        records: list[ServiceRecord] = []
        for index, (unit, label, role) in enumerate(SERVICE_ALLOWLIST):
            state = states[index].strip() if index < len(states) else "unknown"
            records.append(ServiceRecord(unit=unit, label=label, role=role, state=state))
        return tuple(records)

    @staticmethod
    def _find_opencode() -> str | None:
        discovered = shutil.which("opencode")
        if discovered:
            return discovered
        fallback = Path.home() / ".opencode" / "bin" / "opencode"
        return str(fallback) if fallback.is_file() else None


def unwrap_data(payload: Any) -> Any:
    if isinstance(payload, dict) and set(payload).issuperset({"data"}):
        return payload.get("data")
    return payload


def default_permission_state_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    root = Path(runtime) if runtime else Path("/tmp") / f"ocdeck-{os.getuid()}"
    return root / "ocdeck-permissions"


def read_local_permissions(
    state_dir: Path | None = None,
    proc_root: Path = Path("/proc"),
) -> dict[str, list[dict[str, str]]]:
    directory = state_dir or default_permission_state_dir()
    try:
        files = tuple(directory.glob("*.json"))
    except OSError:
        return {}

    uid = os.getuid()
    pending: dict[str, list[dict[str, str]]] = {}
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("permission state must be an object")
            pid = clean_int(payload.get("pid"))
            process = proc_root / str(pid)
            live = (
                pid > 0
                and process.stat().st_uid == uid
                and (process / "comm").read_text(errors="replace").strip()
                == "opencode"
            )
        except (OSError, ValueError, json.JSONDecodeError):
            live = False
            payload = {}
        if not live:
            with contextlib.suppress(OSError):
                path.unlink()
            continue

        requests = payload.get("permissions")
        for item in requests if isinstance(requests, list) else ():
            if not isinstance(item, dict):
                continue
            session_id = clean_string(item.get("sessionID"))
            if not session_id:
                continue
            pending.setdefault(session_id, []).append(
                {
                    "id": clean_string(item.get("id")),
                    "permission": clean_string(item.get("permission"))
                    or "permission",
                    "pattern": clean_string(item.get("pattern")),
                }
            )
        questions = payload.get("questions")
        for item in questions if isinstance(questions, list) else ():
            if not isinstance(item, dict):
                continue
            session_id = clean_string(item.get("sessionID"))
            if not session_id:
                continue
            pending.setdefault(session_id, []).append(
                {
                    "id": clean_string(item.get("id")),
                    "permission": "question",
                    "pattern": clean_string(item.get("question"))
                    or "Input required in the terminal",
                }
            )
    return pending


def read_local_statuses(
    state_dir: Path | None = None,
    proc_root: Path = Path("/proc"),
) -> dict[str, str]:
    directory = state_dir or default_permission_state_dir()
    try:
        files = tuple(directory.glob("*.json"))
    except OSError:
        return {}

    uid = os.getuid()
    statuses: dict[str, str] = {}
    status_updates: dict[str, int] = {}
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            pid = clean_int(payload.get("pid"))
            process = proc_root / str(pid)
            live = (
                pid > 0
                and process.stat().st_uid == uid
                and (process / "comm").read_text(errors="replace").strip()
                == "opencode"
            )
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not live:
            continue
        records = payload.get("statuses")
        updated = clean_int(payload.get("updated"))
        if updated <= 0:
            try:
                updated = int(path.stat().st_mtime * 1000)
            except OSError:
                updated = 0
        for item in records if isinstance(records, list) else ():
            if not isinstance(item, dict):
                continue
            session_id = clean_string(item.get("sessionID"))
            status = normalize_status(item.get("status"))
            if session_id:
                if updated >= status_updates.get(session_id, -1):
                    statuses[session_id] = status
                    status_updates[session_id] = updated
    return statuses


def merge_permissions(
    *sources: dict[str, list[dict[str, str]]],
) -> dict[str, list[dict[str, str]]]:
    merged: dict[str, list[dict[str, str]]] = {}
    seen: dict[str, set[tuple[str, str, str]]] = {}
    for source in sources:
        for session_id, requests in source.items():
            for request in requests:
                key = (
                    clean_string(request.get("id")),
                    clean_string(request.get("permission")),
                    clean_string(request.get("pattern")),
                )
                if key in seen.setdefault(session_id, set()):
                    continue
                seen[session_id].add(key)
                merged.setdefault(session_id, []).append(
                    {
                        "id": key[0],
                        "permission": key[1] or "permission",
                        "pattern": key[2],
                    }
                )
    return merged


def read_markdown_projects(projects_file: Path) -> tuple[tuple[str, str], ...]:
    try:
        source = projects_file.read_text(encoding="utf-8")
    except OSError:
        return ()
    return parse_markdown_projects(source, projects_file)


def read_session_routes(routes_file: Path) -> dict[str, str]:
    try:
        payload = json.loads(routes_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, dict):
        return {}
    return {
        clean_string(session_id): clean_string(project_name)
        for session_id, project_name in sessions.items()
        if clean_string(session_id) and clean_string(project_name)
    }


def read_agent_parent_ids(
    db_file: str | Path | None = None,
) -> dict[str, str]:
    """Read native agent-spawned subagent relationships without message content.

    Only OpenCode's own ``parent_id`` links group a subagent beneath the agent
    session that spawned it. Orchestration metadata (for example Home Agent
    worker launches) is launch provenance, not a subagent relationship, and is
    deliberately ignored.
    """
    path = Path(db_file) if db_file else DEFAULT_SESSION_DB_FILE
    try:
        if not path.is_file():
            return {}
        connection = sqlite3.connect(
            f"file:{urllib.parse.quote(str(path))}?mode=ro",
            uri=True,
            timeout=1,
        )
    except (OSError, sqlite3.Error):
        return {}
    try:
        rows = connection.execute(
            "SELECT id, parent_id FROM session "
            "WHERE time_archived IS NULL "
            "AND parent_id IS NOT NULL AND parent_id != '' AND parent_id != id"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return {
        session_id: parent_id
        for session_id, parent_id in rows
        if isinstance(session_id, str)
        and session_id
        and isinstance(parent_id, str)
        and parent_id
    }


def read_briefings_file(
    briefings_file: Path,
    max_bytes: int = MAX_BRIEFINGS_FILE_BYTES,
) -> str | None:
    try:
        with briefings_file.open("rb") as handle:
            content = handle.read(max(0, max_bytes) + 1)
    except OSError:
        return None
    if len(content) > max_bytes:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_archived_session_ids(
    db_file: str | Path | None = None,
) -> frozenset[str]:
    path = Path(db_file) if db_file else DEFAULT_SESSION_DB_FILE
    try:
        if not path.is_file():
            return frozenset()
        connection = sqlite3.connect(
            f"file:{urllib.parse.quote(str(path))}?mode=ro",
            uri=True,
            timeout=1,
        )
    except (OSError, sqlite3.Error):
        return frozenset()
    try:
        rows = connection.execute(
            "SELECT id FROM session WHERE time_archived IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return frozenset()
    finally:
        connection.close()
    return frozenset(
        row[0] for row in rows if isinstance(row[0], str) and row[0]
    )


def read_last_user_interactions(
    db_file: str | Path | None = None,
) -> dict[str, int]:
    path = Path(db_file) if db_file else DEFAULT_SESSION_DB_FILE
    try:
        if not path.is_file():
            return {}
        connection = sqlite3.connect(
            f"file:{urllib.parse.quote(str(path))}?mode=ro",
            uri=True,
            timeout=1,
        )
    except (OSError, sqlite3.Error):
        return {}
    try:
        rows = connection.execute(
            "SELECT session_id, MAX(time_created) FROM message "
            "WHERE json_valid(data) AND json_extract(data, '$.role') = 'user' "
            "GROUP BY session_id"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return {
        session_id: timestamp
        for session_id, timestamp in rows
        if isinstance(session_id, str)
        and session_id
        and isinstance(timestamp, int)
        and timestamp > 0
    }


# Long provider calls can be silent between database writes for several minutes.
# Keep the fallback bounded, but align it with the existing review window.
TURN_ACTIVITY_FALLBACK_WINDOW_MS = 15 * 60 * 1000


def read_session_turn_activity(
    db_file: str | Path | None = None,
    now_ms: int | None = None,
    allow_stale: bool = False,
) -> dict[str, tuple[bool, int]]:
    """Read latest assistant-turn metadata per session from the local DB.

    Returns ``{session_id: (turn_in_progress, completed_ms)}`` derived only
    from ``message.data`` JSON metadata (timestamps and finish reason).
    Message text and tool output live in a separate table that is never
    queried; a missing or unreadable database simply yields no signals.
    """
    path = Path(db_file) if db_file else DEFAULT_SESSION_DB_FILE
    try:
        if not path.is_file():
            return {}
        connection = sqlite3.connect(
            f"file:{urllib.parse.quote(str(path))}?mode=ro",
            uri=True,
            timeout=1,
        )
    except (OSError, sqlite3.Error):
        return {}
    try:
        has_parts = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'part'"
        ).fetchone() is not None
        activity_column = (
            "COALESCE((SELECT MAX(p.time_updated) FROM part AS p "
            "WHERE p.message_id = ranked.id), ranked.time_created)"
            if has_parts
            else "ranked.time_created"
        )
        rows = connection.execute(
            f"SELECT session_id, data, {activity_column} FROM ("
            "SELECT id, session_id, time_created, data, ROW_NUMBER() OVER ("
            "PARTITION BY session_id ORDER BY time_created DESC, id DESC"
            ") AS position FROM message "
            "WHERE json_valid(data) "
            "AND json_extract(data, '$.role') = 'assistant'"
            ") AS ranked WHERE position = 1"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    activity: dict[str, tuple[bool, int]] = {}
    current = (
        now_ms
        if now_ms is not None
        else int(datetime.now(timezone.utc).timestamp() * 1000)
    )
    for session_id, payload, latest_activity_ms in rows:
        if not isinstance(session_id, str) or not session_id:
            continue
        try:
            data = json.loads(payload) if isinstance(payload, str) else None
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        time_info = data.get("time")
        completed = (
            clean_int(time_info.get("completed"))
            if isinstance(time_info, dict)
            else 0
        )
        finish = data.get("finish")
        # No completion timestamp means the assistant turn is still being
        # written; `tool-calls` means tools are executing between messages.
        active = completed <= 0 or finish == "tool-calls"
        if has_parts and active and not allow_stale:
            active = (
                current - clean_int(latest_activity_ms)
                <= TURN_ACTIVITY_FALLBACK_WINDOW_MS
            )
        activity[session_id] = (active, completed)
    return activity


WORKER_TASK_MARKER = "user-approved task:"
MAX_PROMPT_JSON_BYTES = 4096


def live_session_ids(
    statuses: dict[str, str],
    instance_counts: dict[str, int],
    permissions: dict[str, list[dict[str, str]]],
    sessions_result: list[Any] | None,
) -> tuple[str, ...]:
    """Session IDs that currently own a live agents-board row.

    Mirrors ``agent_state``'s non-idle conditions so prompts are read only
    for sessions the dashboard can actually show them for.
    """
    if not sessions_result:
        return ()
    listed = {
        item.get("id")
        for item in sessions_result
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    return tuple(
        sorted(
            session_id
            for session_id in listed
            if session_id
            and (
                normalize_status(statuses.get(session_id)) in {"busy", "retry"}
                or clean_int(instance_counts.get(session_id, 0)) > 0
                or session_id in permissions
            )
        )
    )


def condense_worker_prompt(text: str) -> str:
    """Prefer the approved task inside Home Agent launch boilerplate.

    Worker launch messages start with fixed boilerplate before an explicit
    ``User-approved task:`` marker; everything after the marker is the part
    worth showing. A marker with no trailing text means nothing is usable.
    """
    marker_at = text.casefold().find(WORKER_TASK_MARKER)
    if marker_at < 0:
        return text
    candidate = text[marker_at + len(WORKER_TASK_MARKER) :].lstrip(" :-")
    return candidate if candidate.strip() else ""


def read_latest_user_prompts(
    db_file: str | Path | None = None,
    session_ids: Iterable[str] | None = None,
) -> dict[str, str]:
    """Read each listed session's latest textual user prompt, read-only.

    Only the first ``text`` part of the newest ``user`` message per requested
    session is read; attachments, tool output, and assistant content are never
    queried. Values are sanitized to a bounded single line. A missing, locked,
    or unreadable database simply yields no prompts instead of failing.
    """
    wanted = tuple(
        dict.fromkeys(
            session_id
            for session_id in (session_ids or ())
            if isinstance(session_id, str) and session_id
        )
    )
    if not wanted:
        return {}
    path = Path(db_file) if db_file else DEFAULT_SESSION_DB_FILE
    try:
        if not path.is_file():
            return {}
        connection = sqlite3.connect(
            f"file:{urllib.parse.quote(str(path))}?mode=ro",
            uri=True,
            timeout=1,
        )
    except (OSError, sqlite3.Error):
        return {}
    prompts: dict[str, str] = {}
    try:
        for session_id in wanted:
            message_row = connection.execute(
                "SELECT id FROM message "
                "WHERE session_id = ? AND json_valid(data) "
                "AND json_extract(data, '$.role') = 'user' "
                "ORDER BY time_created DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if message_row is None or not isinstance(message_row[0], str):
                continue
            part_row = connection.execute(
                "SELECT substr(data, 1, ?) FROM part "
                "WHERE message_id = ? AND json_valid(data) "
                "AND json_extract(data, '$.type') = 'text' "
                "ORDER BY time_created ASC LIMIT 1",
                (MAX_PROMPT_JSON_BYTES, message_row[0]),
            ).fetchone()
            if part_row is None or not isinstance(part_row[0], str):
                continue
            try:
                payload = json.loads(part_row[0])
            except json.JSONDecodeError:
                continue
            text = payload.get("text") if isinstance(payload, dict) else None
            if not isinstance(text, str) or not text.strip():
                continue
            condensed = sanitize_terminal_text(condense_worker_prompt(text))
            if condensed:
                prompts[session_id] = condensed[:MAX_LAST_PROMPT_LENGTH]
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return prompts


def parse_briefings(
    source: str | bytes | dict[str, Any] | None,
) -> BriefingReportRecord | None:
    if isinstance(source, dict):
        payload: Any = source
    elif isinstance(source, (str, bytes)):
        if len(source) > MAX_BRIEFINGS_FILE_BYTES:
            return None
        if isinstance(source, str):
            try:
                if len(source.encode("utf-8")) > MAX_BRIEFINGS_FILE_BYTES:
                    return None
            except UnicodeEncodeError:
                return None
        try:
            payload = json.loads(source)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return None
    else:
        return None
    if not isinstance(payload, dict):
        return None
    schema_version = payload.get("schemaVersion")
    if type(schema_version) is not int or schema_version != 1:
        return None

    report_id = _briefing_string(
        payload.get("reportID"), MAX_BRIEFING_IDENTIFIER_LENGTH
    )
    generated_at = parse_rfc3339(payload.get("generatedAt"))
    status = _briefing_string(payload.get("status"), 16)
    projects = payload.get("projects")
    if (
        report_id is None
        or generated_at is None
        or status not in {"running", "completed", "partial", "failed"}
        or not isinstance(projects, list)
    ):
        return None

    if len(projects) > MAX_BRIEFING_PROJECTS:
        return None
    parsed_projects: list[ProjectBriefingRecord] = []
    for item in projects:
        briefing = parse_project_briefing(item)
        if briefing is None:
            return None
        parsed_projects.append(briefing)
    return BriefingReportRecord(
        report_id=report_id,
        generated_at=generated_at,
        status=status,
        projects=tuple(parsed_projects),
    )


def parse_project_briefing(payload: Any) -> ProjectBriefingRecord | None:
    if not isinstance(payload, dict):
        return None
    project_id = _briefing_string(
        payload.get("projectID"), MAX_BRIEFING_IDENTIFIER_LENGTH
    )
    project_path = _briefing_string(
        payload.get("projectPath"), MAX_BRIEFING_PATH_LENGTH
    )
    name = _briefing_string(payload.get("name"), MAX_BRIEFING_TEXT_LENGTH)
    assessment = _briefing_string(payload.get("assessment"), 16)
    summary = _briefing_string(
        payload.get("summary"), MAX_BRIEFING_TEXT_LENGTH, allow_empty=True
    )
    confidence = _briefing_string(payload.get("confidence"), 16)
    research_status = _briefing_string(payload.get("researchStatus"), 16)
    evidence_value = payload.get("evidenceAt")
    evidence_at = (
        parse_rfc3339(evidence_value) if evidence_value is not None else None
    )
    completed_outputs = payload.get("completedOutputs")
    blockers = payload.get("blockers")
    next_steps = payload.get("nextSteps")
    evidence = payload.get("evidence")
    null_evidence_allowed = (
        research_status in {"queued", "running", "failed"}
        or assessment == "unknown"
    )
    if (
        project_id is None
        or project_path is None
        or name is None
        or assessment
        not in {"on-track", "at-risk", "blocked", "waiting", "complete", "unknown"}
        or summary is None
        or confidence not in {"low", "medium", "high"}
        or research_status not in {"queued", "running", "completed", "failed"}
        or "evidenceAt" not in payload
        or (
            evidence_value is None
            and not null_evidence_allowed
        )
        or (evidence_value is not None and evidence_at is None)
        or not isinstance(completed_outputs, list)
        or not isinstance(blockers, list)
        or not isinstance(next_steps, list)
        or not isinstance(evidence, list)
    ):
        return None

    parsed_outputs: list[tuple[str, str]] = []
    for item in completed_outputs[:MAX_BRIEFING_ARRAY_ITEMS]:
        if not isinstance(item, dict):
            return None
        label = _briefing_string(item.get("label"), MAX_BRIEFING_TEXT_LENGTH)
        locator = _briefing_string(
            item.get("locator"), MAX_BRIEFING_PATH_LENGTH
        )
        if label is None or locator is None:
            return None
        parsed_outputs.append((label, locator))

    parsed_blockers: list[str] = []
    for item in blockers[:MAX_BRIEFING_ARRAY_ITEMS]:
        if not isinstance(item, dict):
            return None
        blocker = _briefing_string(
            item.get("summary"), MAX_BRIEFING_TEXT_LENGTH
        )
        if blocker is None:
            return None
        parsed_blockers.append(blocker)

    parsed_steps: list[NextStepRecord] = []
    for item in next_steps[:MAX_BRIEFING_ARRAY_ITEMS]:
        step = parse_next_step(item)
        if step is None:
            return None
        parsed_steps.append(step)

    parsed_evidence: list[str] = []
    for item in evidence[:MAX_BRIEFING_EVIDENCE_ITEMS]:
        candidate: Any = item
        if isinstance(item, dict):
            candidate = next(
                (
                    item.get(key)
                    for key in ("summary", "label", "detail", "locator")
                    if isinstance(item.get(key), str)
                ),
                None,
            )
        evidence_text = _briefing_string(
            candidate, MAX_BRIEFING_TEXT_LENGTH
        )
        if evidence_text is not None:
            parsed_evidence.append(evidence_text)

    return ProjectBriefingRecord(
        project_id=project_id,
        project_path=project_path,
        name=name,
        assessment=assessment,
        summary=summary,
        confidence=confidence,
        evidence_at=evidence_at,
        completed_outputs=tuple(parsed_outputs),
        blockers=tuple(parsed_blockers),
        next_steps=tuple(parsed_steps),
        evidence=tuple(parsed_evidence),
        research_status=research_status,
    )


def parse_next_step(payload: Any) -> NextStepRecord | None:
    if not isinstance(payload, dict):
        return None
    step_id = _briefing_string(
        payload.get("id"), MAX_BRIEFING_IDENTIFIER_LENGTH
    )
    title = _briefing_string(payload.get("title"), MAX_BRIEFING_TEXT_LENGTH)
    detail = _briefing_string(
        payload.get("detail"), MAX_BRIEFING_TEXT_LENGTH, allow_empty=True
    )
    state = _briefing_string(payload.get("state"), 16)
    if (
        step_id is None
        or title is None
        or detail is None
        or state not in {"now", "next", "blocked", "done"}
        or payload.get("requiresApproval") is not True
    ):
        return None
    return NextStepRecord(
        id=step_id,
        title=title,
        detail=detail,
        state=state,
        requires_approval=True,
    )


def parse_rfc3339(value: Any) -> datetime | None:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or RFC3339_PATTERN.fullmatch(value) is None
    ):
        return None
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        instant = datetime.fromisoformat(candidate)
        normalized = instant.astimezone(timezone.utc)
        normalized.timestamp()
    except (OSError, OverflowError, ValueError):
        return None
    if instant.tzinfo is None or instant.utcoffset() is None:
        return None
    return normalized


def match_project_briefings(
    briefings: tuple[ProjectBriefingRecord, ...],
    projects: tuple[ProjectRecord, ...],
) -> tuple[ProjectBriefingRecord, ...]:
    known_paths = {
        normalized_project_path(project.directory)
        for project in projects
        if project.directory
    }
    matched: list[ProjectBriefingRecord] = []
    seen_paths: set[str] = set()
    for briefing in briefings:
        path_key = normalized_project_path(briefing.project_path)
        if path_key not in known_paths or path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        matched.append(briefing)
    return tuple(matched)


def _briefing_string(
    value: Any,
    max_length: int,
    *,
    allow_empty: bool = False,
) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = sanitize_terminal_text(value)[:max_length]
    if not cleaned and not allow_empty:
        return None
    return cleaned


def find_vault_root(file_path: Path) -> Path:
    for parent in file_path.parents:
        if (parent / ".obsidian").is_dir():
            return parent
    return file_path.parent


def parse_markdown_projects(
    source: str, projects_file: Path
) -> tuple[tuple[str, str], ...]:
    lines = source.splitlines()
    header_index = -1
    project_column = -1
    code_column = -1
    for index, line in enumerate(lines):
        cells = markdown_table_cells(line)
        normalized = [cell.casefold() for cell in cells]
        if "project" in normalized and "code" in normalized:
            header_index = index
            project_column = normalized.index("project")
            code_column = normalized.index("code")
            break
    if header_index < 0:
        return ()

    base_directory = find_vault_root(projects_file)

    projects: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    required_columns = max(project_column, code_column)
    for line in lines[header_index + 1 :]:
        if not line.strip():
            if projects:
                break
            continue
        cells = markdown_table_cells(line)
        if len(cells) <= required_columns:
            if projects:
                break
            continue
        if is_markdown_separator(cells):
            continue
        name = markdown_cell_text(cells[project_column])
        code_path = markdown_cell_text(cells[code_column])
        if not name or not code_path:
            continue
        directory = Path(code_path).expanduser()
        if not directory.is_absolute():
            directory = base_directory / directory
        directory_text = os.path.normpath(str(directory))
        path_key = normalized_project_path(directory_text)
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        projects.append((clean_string(name), directory_text))
    return tuple(projects)


def markdown_table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if "|" not in stripped:
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def markdown_cell_text(cell: str) -> str:
    value = cell.strip()
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        value = value[1:-1].strip()
    if value.startswith("[[") and value.endswith("]]"):
        value = value[2:-2].split("|", 1)[-1].strip()
    return clean_string(value)


def is_markdown_separator(cells: list[str]) -> bool:
    return bool(cells) and all(
        "-" in cell and not set(cell).difference({"-", ":", " "})
        for cell in cells
    )


def normalized_project_path(directory: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(directory)))


def merge_project_catalog(
    discovered: dict[str, str], catalog: tuple[tuple[str, str], ...]
) -> tuple[dict[str, str], dict[str, str]]:
    projects = dict(discovered)
    names: dict[str, str] = {}
    ids_by_path = {
        normalized_project_path(directory): project_id
        for project_id, directory in projects.items()
    }
    for name, directory in catalog:
        path_key = normalized_project_path(directory)
        project_id = ids_by_path.get(path_key)
        if project_id is None:
            project_id = f"markdown::{path_key}"
            projects[project_id] = directory
            ids_by_path[path_key] = project_id
        names.setdefault(project_id, name)
    return projects, names


def validate_api_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return "Invalid OpenCode API URL"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "OpenCode API URL must use HTTP or HTTPS"
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return "OpenCode API URL must not contain credentials, query, or fragment"
    if port is not None and not 1 <= port <= 65535:
        return "OpenCode API URL has an invalid port"
    return ""


def api_credentials_are_safe(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme == "https" or is_loopback_host(parsed.hostname or "")


def is_loopback_host(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


async def communicate_with_cleanup(
    process: asyncio.subprocess.Process,
    *,
    timeout: float,
) -> tuple[bytes, bytes] | None:
    try:
        return await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        await stop_process(process)
        raise
    except TimeoutError:
        await stop_process(process)
        return None


async def stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


def classify_opencode_command(arguments: list[str]) -> tuple[bool, str]:
    if not arguments or Path(arguments[0]).name != "opencode":
        return False, ""
    if len(arguments) > 1 and arguments[1] in NON_TUI_SUBCOMMANDS:
        return False, ""
    if any(argument in {"-h", "--help", "-v", "--version"} for argument in arguments[1:]):
        return False, ""
    if "--fork" in arguments:
        return True, ""

    session_id = ""
    for index, argument in enumerate(arguments[1:], start=1):
        if argument in {"-s", "--session"}:
            if index + 1 < len(arguments):
                session_id = arguments[index + 1]
            break
        if argument.startswith("--session="):
            session_id = argument.partition("=")[2]
            break
        if argument.startswith("-s="):
            session_id = argument.partition("=")[2]
            break

    return True, session_id if session_id.startswith("ses_") else ""


def read_opencode_instances(
    proc_root: Path = Path("/proc"),
) -> tuple[dict[str, int], int, dict[str, tuple[str, ...]]]:
    counts: dict[str, int] = {}
    unmapped = 0
    ttys: dict[str, list[str]] = {}
    for process in read_opencode_processes(proc_root):
        if process.session_id:
            counts[process.session_id] = counts.get(process.session_id, 0) + 1
            if process.tty:
                ttys.setdefault(process.session_id, []).append(process.tty)
        else:
            unmapped += 1
    return counts, unmapped, {key: tuple(sorted(value)) for key, value in ttys.items()}


def read_opencode_processes(
    proc_root: Path = Path("/proc"),
) -> tuple[OpenCodeProcess, ...]:
    try:
        processes = tuple(proc_root.iterdir())
    except OSError:
        return ()

    uid = os.getuid()
    records: list[OpenCodeProcess] = []
    for process in processes:
        if not process.name.isdigit():
            continue
        record = _read_opencode_process(process, uid)
        if record is not None:
            records.append(record)
    return tuple(sorted(records, key=lambda item: item.pid))


def _read_opencode_process(process: Path, uid: int) -> OpenCodeProcess | None:
    try:
        if process.stat().st_uid != uid:
            return None
        if (process / "comm").read_text(errors="replace").strip() != "opencode":
            return None
        command = [
            os.fsdecode(argument)
            for argument in (process / "cmdline").read_bytes().split(b"\0")
            if argument
        ]
    except OSError:
        return None

    is_tui, session_id = classify_opencode_command(command)
    if not is_tui:
        return None
    state, start_time = read_process_identity(process)
    if state == "Z":
        return None
    return OpenCodeProcess(
        pid=int(process.name),
        session_id=session_id,
        tty=read_process_tty(process),
        start_time=start_time,
    )


def read_process_identity(process: Path) -> tuple[str, int]:
    """Return process state and stable start time from /proc/PID/stat."""
    try:
        content = (process / "stat").read_text(encoding="ascii", errors="replace")
        _, separator, remainder = content.rpartition(")")
        fields = remainder.split()
        if not separator or len(fields) < 20:
            return "", 0
        return fields[0], int(fields[19])
    except (OSError, ValueError):
        return "", 0


def read_process_tty(process: Path) -> str:
    try:
        target = os.readlink(process / "fd" / "0")
    except OSError:
        return ""
    return target if target.startswith("/dev/pts/") else ""


def read_live_opencode_panes(
    proc_root: Path = Path("/proc"),
    tmux_bin: str = "tmux",
) -> tuple[LiveOpenCodePane, ...]:
    """Discover exact tmux panes that currently own an OpenCode TUI."""
    processes = tuple(item for item in read_opencode_processes(proc_root) if item.tty)
    if not processes:
        return ()
    by_tty: dict[str, list[OpenCodeProcess]] = {}
    for process in processes:
        by_tty.setdefault(process.tty, []).append(process)

    format_string = "\t".join(
        (
            "#{pane_id}",
            "#{pane_tty}",
            "#{session_name}",
            "#{window_index}",
            "#{pane_index}",
            "#{session_attached}",
            "#{window_active}",
            "#{pane_active}",
            "#{pane_dead}",
        )
    )
    try:
        result = subprocess.run(
            [tmux_bin, "list-panes", "-a", "-F", format_string],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if result.returncode != 0:
        return ()

    pane_rows: dict[str, tuple[str, ...] | None] = {}
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        fields = tuple(line.split("\t"))
        if len(fields) != 9 or not PANE_ID_PATTERN.fullmatch(fields[0]):
            continue
        pane_id, tty, *_rest, pane_dead = fields
        if not tty or tty not in by_tty or pane_dead not in {"", "0"}:
            continue
        previous = pane_rows.get(pane_id)
        if previous is not None and previous != fields:
            pane_rows[pane_id] = None
        elif pane_id not in pane_rows:
            pane_rows[pane_id] = fields

    uid = os.getuid()
    live: list[LiveOpenCodePane] = []
    for pane_id, fields in pane_rows.items():
        if fields is None:
            continue
        (
            _,
            tty,
            session_name,
            window_index,
            pane_index,
            session_attached,
            window_active,
            pane_active,
            _,
        ) = fields
        current: list[OpenCodeProcess] = []
        for candidate in by_tty[tty]:
            refreshed = _read_opencode_process(proc_root / str(candidate.pid), uid)
            if refreshed == candidate:
                current.append(candidate)
        session_ids = {item.session_id for item in current}
        if not current or len(session_ids) != 1:
            continue
        session_id = next(iter(session_ids))
        identity = "\0".join(
            [
                "ocdeck-destination-v1",
                pane_id,
                tty,
                *(f"{item.pid}:{item.start_time}:{item.session_id}" for item in current),
            ]
        )
        destination_id = "dst_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        if session_attached not in {"", "0"} and window_active not in {"", "0"}:
            terminal_state = (
                "foreground" if pane_active not in {"", "0"} else "visible"
            )
        else:
            terminal_state = "background"
        live.append(
            LiveOpenCodePane(
                destination_id=destination_id,
                pane_id=pane_id,
                session_id=session_id,
                session_name=session_name,
                window_index=window_index,
                pane_index=pane_index,
                terminal_state=terminal_state,
            )
        )
    order = {"foreground": 0, "visible": 1, "background": 2}
    return tuple(
        sorted(
            live,
            key=lambda item: (
                order.get(item.terminal_state, 3),
                item.session_name.casefold(),
                item.window_index,
                item.pane_index,
                item.pane_id,
            ),
        )
    )


def read_tmux_tty_sessions(
    ttys: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    return read_tmux_tty_state(ttys)[0]


def read_tmux_tty_state(
    ttys: dict[str, tuple[str, ...]],
) -> tuple[dict[str, tuple[str, ...]], dict[str, bool]]:
    wanted = {tty for paths in ttys.values() for tty in paths}
    if not wanted:
        return {}, {}
    try:
        result = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{pane_tty}\t#{session_name}\t#{session_attached}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}, {}
    if result.returncode != 0:
        return {}, {}
    tty_state: dict[str, tuple[str, bool]] = {}
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        tty, _, remainder = line.partition("\t")
        name, _, attached = remainder.partition("\t")
        if tty and name:
            tty_state[tty] = (name, attached not in {"", "0"})
    mapped: dict[str, tuple[str, ...]] = {}
    attached_map: dict[str, bool] = {}
    for session_id, paths in ttys.items():
        states = [tty_state[tty] for tty in paths if tty in tty_state]
        names = tuple(sorted({name for name, _ in states}))
        if names:
            mapped[session_id] = names
            attached_map[session_id] = any(attached for _, attached in states)
    return mapped, attached_map


def read_system_metrics() -> SystemMetrics:
    try:
        load_1m = os.getloadavg()[0]
    except OSError:
        load_1m = 0.0

    memory_percent = 0.0
    try:
        memory: dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                key, value = line.split(":", 1)
                memory[key] = int(value.strip().split()[0])
        total = memory.get("MemTotal", 0)
        available = memory.get("MemAvailable", 0)
        if total:
            memory_percent = (total - available) / total * 100
    except (OSError, ValueError):
        pass

    disk_percent = 0.0
    try:
        disk = shutil.disk_usage(Path.home())
        if disk.total:
            disk_percent = disk.used / disk.total * 100
    except OSError:
        pass

    uptime_seconds = 0
    try:
        with open("/proc/uptime", encoding="ascii") as handle:
            uptime_seconds = int(float(handle.read().split()[0]))
    except (OSError, ValueError, IndexError):
        pass

    return SystemMetrics(
        load_1m=load_1m,
        cpu_count=os.cpu_count() or 1,
        memory_percent=memory_percent,
        disk_percent=disk_percent,
        uptime_seconds=uptime_seconds,
    )
