from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str
    title: str
    directory: str
    project_id: str
    created_ms: int
    updated_ms: int
    parent_id: str = ""
    agent_parent_id: str = ""
    last_interaction_ms: int = 0
    status: str = "idle"
    instance_count: int = 0
    terminals: tuple[str, ...] = ()
    terminal_attached: bool = False
    permission: str = ""
    question: str = ""
    last_prompt: str = ""
    assistant_active: bool = False
    assistant_done_ms: int = 0
    permission_id: str = ""
    question_id: str = ""


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    id: str
    directory: str
    name: str
    session_count: int = 0
    active_count: int = 0
    attached_count: int = 0
    instance_count: int = 0
    updated_ms: int = 0


@dataclass(frozen=True, slots=True)
class NextStepRecord:
    id: str
    title: str
    detail: str
    state: str
    requires_approval: bool = True


@dataclass(frozen=True, slots=True)
class ProjectBriefingRecord:
    project_id: str
    project_path: str
    name: str
    assessment: str
    summary: str
    confidence: str
    evidence_at: datetime | None
    completed_outputs: tuple[tuple[str, str], ...] = ()
    blockers: tuple[str, ...] = ()
    next_steps: tuple[NextStepRecord, ...] = ()
    evidence: tuple[str, ...] = ()
    research_status: str = "completed"


@dataclass(frozen=True, slots=True)
class BriefingReportRecord:
    report_id: str
    generated_at: datetime
    status: str
    projects: tuple[ProjectBriefingRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class ServiceRecord:
    unit: str
    label: str
    role: str
    state: str


@dataclass(frozen=True, slots=True)
class SystemMetrics:
    load_1m: float = 0.0
    cpu_count: int = 1
    memory_percent: float = 0.0
    disk_percent: float = 0.0
    uptime_seconds: int = 0


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    sessions: tuple[SessionRecord, ...] = ()
    projects: tuple[ProjectRecord, ...] = ()
    services: tuple[ServiceRecord, ...] = ()
    metrics: SystemMetrics = field(default_factory=SystemMetrics)
    connection: str = "offline"
    connection_detail: str = "OpenCode API unavailable"
    unmapped_instance_count: int = 0
    collected_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    warning: str = ""
    briefings: tuple[ProjectBriefingRecord, ...] = ()
    briefing_report_id: str = ""
    briefing_generated_at: datetime | None = None
    briefing_status: str = ""

    @property
    def mapped_instance_count(self) -> int:
        return sum(session.instance_count for session in self.sessions)

    @property
    def terminal_instance_count(self) -> int:
        return self.mapped_instance_count + self.unmapped_instance_count

    @property
    def attached_session_count(self) -> int:
        return sum(session.instance_count > 0 for session in self.sessions)


def parse_sessions(
    payload: Any,
    statuses: dict[str, str] | None = None,
    instance_counts: dict[str, int] | None = None,
    terminals: dict[str, tuple[str, ...]] | None = None,
    permissions: dict[str, list[dict[str, str]]] | None = None,
    terminal_attached: dict[str, bool] | None = None,
    last_interactions: dict[str, int] | None = None,
    turn_activity: dict[str, tuple[bool, int]] | None = None,
    latest_prompts: dict[str, str] | None = None,
    agent_parent_ids: dict[str, str] | None = None,
) -> tuple[SessionRecord, ...]:
    if not isinstance(payload, list):
        return ()

    status_map = statuses or {}
    instance_map = instance_counts or {}
    terminal_map = terminals or {}
    permission_map = permissions or {}
    attached_map = terminal_attached or {}
    interaction_map = last_interactions or {}
    turn_map = turn_activity or {}
    prompt_map = latest_prompts or {}
    agent_parent_map = agent_parent_ids or {}
    sessions: list[SessionRecord] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        session_id = clean_string(item.get("id"))
        directory = clean_string(item.get("directory"))
        if not session_id or not directory:
            continue
        requests = permission_map.get(session_id) or ()
        permission_label = ""
        question_label = ""
        permission_id = ""
        question_id = ""
        for request in requests:
            request_type = clean_string(request.get("permission"))
            pattern = clean_string(request.get("pattern"))
            if request_type == "question":
                question_label = question_label or pattern or "Input required"
                question_id = question_id or clean_string(request.get("id"))
            elif not permission_label:
                permission_label = f"{request_type} {pattern}".strip()
                permission_id = clean_string(request.get("id"))
        turn_active, assistant_done_ms = turn_map.get(session_id, (False, 0))
        sessions.append(
            SessionRecord(
                id=session_id,
                title=clean_string(item.get("title")) or "Untitled session",
                directory=directory,
                project_id=clean_string(item.get("projectId")) or "unknown",
                created_ms=clean_int(item.get("created")),
                updated_ms=clean_int(item.get("updated")),
                parent_id=clean_string(item.get("parentID")),
                agent_parent_id=clean_string(agent_parent_map.get(session_id)),
                last_interaction_ms=clean_int(interaction_map.get(session_id, 0)),
                status=normalize_status(status_map.get(session_id, "idle")),
                instance_count=clean_int(instance_map.get(session_id, 0)),
                terminals=tuple(terminal_map.get(session_id, ())),
                terminal_attached=bool(attached_map.get(session_id, False)),
                permission=permission_label,
                question=question_label,
                last_prompt=clean_string(prompt_map.get(session_id, ""))[
                    :MAX_LAST_PROMPT_LENGTH
                ],
                assistant_active=bool(turn_active),
                assistant_done_ms=clean_int(assistant_done_ms),
                permission_id=permission_id,
                question_id=question_id,
            )
        )

    return tuple(sorted(sessions, key=lambda item: item.updated_ms, reverse=True))


def parse_known_projects(payload: Any) -> dict[str, str]:
    if not isinstance(payload, list):
        return {}
    projects: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        project_id = clean_string(item.get("id"))
        if not project_id or project_id == "global":
            continue
        worktree = clean_string(item.get("worktree"))
        directory = worktree if worktree and Path(worktree).is_dir() else ""
        if not directory:
            sandboxes = item.get("sandboxes")
            if isinstance(sandboxes, list):
                for sandbox in sandboxes:
                    candidate = clean_string(sandbox)
                    if candidate and Path(candidate).is_dir():
                        directory = candidate
                        break
        chosen = directory or worktree
        if chosen:
            projects[project_id] = chosen
    return projects


def assign_project_roots(
    sessions: Iterable[SessionRecord],
    known_projects: dict[str, str] | None = None,
) -> tuple[SessionRecord, ...]:
    roots = [
        (directory.rstrip("/"), project_id)
        for project_id, directory in (known_projects or {}).items()
        if directory
    ]
    live_projects = {
        project_id: directory
        for project_id, directory in (known_projects or {}).items()
        if directory and Path(directory).is_dir()
    }
    resolved: list[SessionRecord] = []
    for session in sessions:
        directory = session.directory.rstrip("/")
        stale = bool(directory) and not Path(session.directory).is_dir()
        match: tuple[int, str] | None = None
        for root, project_id in roots:
            if directory == root or (
                not stale and directory.startswith(root + "/")
            ):
                if match is None or len(root) > match[0]:
                    match = (len(root), project_id)
        if match is not None:
            project_id = match[1]
        elif stale and session.project_id in live_projects:
            project_id = session.project_id
        else:
            project_id = f"dir::{directory or session.project_id}"
        if project_id != session.project_id:
            session = replace(session, project_id=project_id)
        resolved.append(session)
    return tuple(resolved)


def apply_session_routes(
    sessions: Iterable[SessionRecord],
    routes: dict[str, str],
    project_names: dict[str, str],
) -> tuple[SessionRecord, ...]:
    project_ids_by_name = {
        name.casefold(): project_id for project_id, name in project_names.items()
    }
    sessions_by_id = {session.id: session for session in sessions}
    resolved: dict[str, str] = {}

    def resolve_project(session: SessionRecord, resolving: set[str]) -> str:
        if session.id in resolved:
            return resolved[session.id]
        explicit = project_ids_by_name.get(routes.get(session.id, "").casefold())
        if explicit:
            resolved[session.id] = explicit
            return explicit
        if session.parent_id and session.id not in resolving:
            parent = sessions_by_id.get(session.parent_id)
            if parent is not None:
                project_id = resolve_project(parent, resolving | {session.id})
                resolved[session.id] = project_id
                return project_id
        resolved[session.id] = session.project_id
        return session.project_id

    routed: list[SessionRecord] = []
    for session in sessions_by_id.values():
        project_id = resolve_project(session, set())
        routed.append(
            replace(session, project_id=project_id)
            if project_id != session.project_id
            else session
        )
    return tuple(routed)


def build_projects(
    sessions: Iterable[SessionRecord],
    known_projects: dict[str, str] | None = None,
    project_names: dict[str, str] | None = None,
) -> tuple[ProjectRecord, ...]:
    grouped: dict[str, list[SessionRecord]] = {}
    for session in sessions:
        grouped.setdefault(session.project_id, []).append(session)

    for project_id in (known_projects or {}):
        if project_names is None or project_id in project_names:
            grouped.setdefault(project_id, [])

    projects: list[ProjectRecord] = []
    for project_id, project_sessions in grouped.items():
        directory = (
            (known_projects or {}).get(project_id, "")
            or (project_sessions[0].directory if project_sessions else "")
        )
        projects.append(
            ProjectRecord(
                id=project_id,
                directory=directory,
                name=(project_names or {}).get(project_id) or project_name(directory),
                session_count=len(project_sessions),
                active_count=sum(
                    session.status in {"busy", "retry"}
                    for session in project_sessions
                ),
                attached_count=sum(
                    session.instance_count > 0 for session in project_sessions
                ),
                instance_count=sum(
                    session.instance_count for session in project_sessions
                ),
                updated_ms=max(
                    (session_age_ms(session) for session in project_sessions),
                    default=0,
                ),
            )
        )

    return tuple(
        sorted(
            projects,
            key=lambda item: (item.updated_ms, item.name.casefold()),
            reverse=True,
        )
    )


def normalize_status(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("type")
    status = clean_string(value).lower()
    return status if status in {"busy", "retry", "idle"} else "idle"


REVIEW_WINDOW_MS = 15 * 60 * 1000
MAX_LAST_PROMPT_LENGTH = 200


def agent_state(session: SessionRecord, now_ms: int | None = None) -> str:
    if session.permission:
        return "permission"
    if session.question:
        return "question"
    if session.status == "busy":
        return "busy"
    if session.status == "retry":
        return "retry"
    if session.instance_count > 0:
        current = (
            now_ms
            if now_ms is not None
            else int(datetime.now(timezone.utc).timestamp() * 1000)
        )
        # An unfinished metadata row needs attention unless the API/plugin
        # explicitly reported the live process as busy above.
        if session.assistant_active:
            return "stalled"
        completed_after_prompt = (
            session.assistant_done_ms > 0
            and session.last_interaction_ms > 0
            and session.assistant_done_ms >= session.last_interaction_ms
        )
        recent = (
            current - session.assistant_done_ms <= REVIEW_WINDOW_MS
            if session.assistant_done_ms > 0
            else False
        )
        if completed_after_prompt and recent:
            return "review"
        return "open"
    return "idle"


def clean_string(value: Any) -> str:
    return sanitize_terminal_text(value) if isinstance(value, str) else ""


def sanitize_terminal_text(value: str) -> str:
    cleaned: list[str] = []
    for character in value:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs"}:
            if character in {"\t", "\n", "\r"}:
                cleaned.append(" ")
            continue
        cleaned.append(character)
    return " ".join("".join(cleaned).split())


def clean_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


def project_name(directory: str) -> str:
    if not directory:
        return "Unknown project"
    path = Path(directory)
    return path.name or str(path)


def relative_time(epoch_ms: int, now: datetime | None = None) -> str:
    if epoch_ms <= 0:
        return "unknown"
    current = now or datetime.now(timezone.utc)
    instant = datetime.fromtimestamp(epoch_ms / 1000, timezone.utc)
    seconds = max(0, int((current - instant).total_seconds()))
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    if seconds < 604800:
        return f"{seconds // 86400}d"
    return instant.astimezone().strftime("%b %d")


def session_age_ms(session: SessionRecord) -> int:
    """Choose the best available timestamp for a session's displayed age."""
    return session.updated_ms or session.last_interaction_ms or session.created_ms


def format_uptime(seconds: int) -> str:
    if seconds < 3600:
        return f"{max(0, seconds) // 60}m"
    hours = seconds // 3600
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def compact_path(path: str, private: bool = False) -> str:
    if private:
        return "[hidden]"
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home) else path
