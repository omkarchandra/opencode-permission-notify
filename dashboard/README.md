# OC Deck

OC Deck is a terminal-native operations console for OpenCode. It provides a
compact view of projects, sessions, local services, and machine health without
reading OpenCode's database or credential files.

It is an original terminal UI. No Berd code, branding, or assets are included.

## Install

```bash
./install.sh
```

The installer creates an isolated virtual environment in `dashboard/.venv` and
adds `ocdeck` to `~/.local/bin`.

## Run

```bash
ocdeck
```

Use `ocdeck --once` for a noninteractive report.

Use `ocdeck --inline-tmux` in a web terminal. Opening a session temporarily
suspends the dashboard and attaches to its tmux session in the same terminal;
detaching or exiting restores OC Deck. The selected target is written to
`$XDG_RUNTIME_DIR/ocdeck-mobile-target.json` for reviewed phone dictation.

Use `ocdeck --briefings-file PATH` to read a different Home Agent briefing
artifact for the `NEXT` view.

## Top-bar launcher

The `OC Deck` status indicator opens the dashboard in a new Ptyxis window.
Click the indicator and choose **Open OC Deck**, or middle-click it for a quick
launch. If Hide Top Bar is enabled, move the pointer to the top edge first.
Caps Lock emits Super on the configured keyboards, so Caps Lock + `O` launches
OC Deck when absent and cycles through existing OC Deck windows. Both launchers
use standalone Ptyxis instances that close automatically when OC Deck exits.

```bash
systemctl --user status ocdeck-indicator.service
systemctl --user restart ocdeck-indicator.service
```

The project list also reads the `Project` and `Code` columns from the Markdown
catalog at `~/.config/home-agent/projects.md`.
Relative code paths are resolved from the vault root. Set
`OCDECK_PROJECTS_FILE` or pass `--projects-file PATH` to use another catalog.
The catalog is reread on every refresh, so projects appear even before they
have an OpenCode session.

Historical sessions launched from generic directories are assigned by exact
session ID using `Projects/_session-routes.json`. Set
`OCDECK_SESSION_ROUTES_FILE` or pass `--session-routes-file PATH` to override
it. Subagent sessions inherit their parent task's routed project. Routing
changes OC Deck's grouping only; it does not modify OpenCode data. Reopening a
routed session starts it from the canonical project root instead of its old
generic directory.

The `NEXT` view reads Home Agent's latest briefing from
`$XDG_STATE_HOME/home-agent/reports/latest.json`. Set
`OCDECK_BRIEFINGS_FILE` or pass `--briefings-file PATH` to override it. OC Deck
supports schema version `1` and joins report entries to dashboard projects only
when their normalized `projectPath` values are exactly equal. It never guesses
from `projectID`, names, or parent directories. Missing, oversized, unsupported,
or malformed artifacts are ignored without failing the rest of the dashboard
refresh. A malformed listed project invalidates the whole artifact; valid
projects with unknown paths are filtered only during exact-path matching.

Sessions are grouped by their project directory: a session appears under the
project whose worktree contains its directory, and directories outside any
known worktree (e.g. registered sandboxes) become their own projects.

## Keys

| Key | Action |
| --- | --- |
| `1` | Operations overview |
| `2` | Service health |
| `3` | Key reference |
| `4` | Live agents board |
| `5` | Portfolio briefing and next steps |
| `Ctrl+Left` / `Ctrl+Right` | Previous or next view |
| `Tab` / `Shift+Tab` | Move focus through controls |
| `Up` / `Down` or `j` / `k` | Move through rows |
| `Left` / `Right` or `h` / `l` | Move between project and session panes |
| `Left` in AGENTS | Expand/collapse live subagents beneath the selected agent |
| `/` | Search all sessions; scoped-project matches appear first |
| `r` | Refresh |
| `p` | Toggle privacy mode |
| `o` or `Enter` | Attach to the selected session's terminal |
| `y` | Approve the selected pending permission once |
| Click a session's name | Rename it; `Enter` saves, `Esc` cancels |
| `a` | Resume selected session with permissions auto-approved (`--auto`) |
| `x` | Stop the selected session's active tmux job (press twice to confirm) |
| `n` | Start a session in the selected project |
| `t` | Open a fresh shell terminal in the selected project |
| `f` | Scope the session list to the selected project |
| `m` | Minimize the window; OC Deck keeps running in the background |
| `q` | Quit |

Highlighting a project scopes the session list to that project; press `f` to
release the scope and show every session again. `Enter` on a project moves into
its session list. The cyan pane border shows where keyboard input is active.
While search text is present, matching sessions from the scoped project appear
first, followed by keyword matches from every other project. Clearing the search
restores the strict project-only scope.
In the `NEXT` view, `Up` / `Down` or `j` / `k` cycles the same selected project
without changing the session scope; `PageUp` / `PageDown` scrolls long reports.

The resume/new-session actions run OpenCode in a named tmux session and open a
standalone Ptyxis viewer for it. OC Deck remains visible and animates sessions
reported as busy. Closing the viewer detaches without stopping OpenCode;
quitting OpenCode ends the tmux session and closes the viewer automatically.
Existing OpenCode sessions use their human-readable session name in the Ptyxis
window title; new sessions use the project name until OpenCode assigns a title.
Pressing `a` adds OpenCode's `--auto` flag, which auto-approves permission
prompts that are not explicitly denied. Pressing `t` starts a plain shell in
the selected project without launching OpenCode.

Clicking a highlighted session's name opens an inline rename editor. `Enter`
saves through OpenCode's loopback API (`PATCH /session/<id>`), `Esc` cancels,
and an empty name cancels too. Renaming needs the live API: when it is locked,
set `OPENCODE_SERVER_PASSWORD`; when offline, the old name is kept and a notice
explains why. Privacy mode blocks renaming so hidden titles stay hidden.

Pending `QUESTION` and `PERMISSION` sessions appear in the attention strip above
the views. Agent STATE labels include elapsed time in the current state, and
`y` approves a selected permission once through the loopback API. Questions
still open in the terminal because OpenCode's installed client exposes no safe
answer endpoint.

If a session already has a live OpenCode TUI process, `o`, `a`, `Enter`, and
clicking the `INST` count attach to that existing terminal instead of starting
a second instance. When the terminal lives in a tmux session, OC Deck finds it
by matching the process's pane tty. On GNOME Wayland, the OC Deck Switch
extension identifies the exact standalone Ptyxis viewer from its tmux process
and raises that window directly. Until the extension reloads, OC Deck uses the
standalone Ptyxis process's unique D-Bus connection for the same exact-window
activation. OC Deck opens another viewer only when it confirms that no viewer
exists. The detail pane names the tmux session each live
terminal runs in. Pressing `x` stops that tmux session — the OpenCode process
ends, the stored session stays resumable — with a double-press confirmation.

Every project owns a deterministic accent color. Project and session rows and
the detail pane use it, and each new tmux session is themed with it — status
bar, pane borders, and a status-left label naming the project — so every
terminal for a project carries the same theme.

`m` minimizes the window (ydotool's Super+H on Wayland, xdotool on X11)
without exiting; OC Deck keeps refreshing in the background.

If a project directory does not exist, OC Deck creates it when the location is
writable. When it cannot (for example a catalog entry on an unmounted drive),
sessions and terminals start in `~/ocdeck-workspaces/<project>` instead and a
notice explains the substitution.

## Portfolio next steps

Press `5` for a terminal-native portfolio briefing. The selected project view
shows its assessment, summary, confidence and evidence age, blockers, completed
outputs, research status, and a vertical `now` / `next` / `blocked` / `done`
step diagram. Confidence is reported as `low`, `medium`, or `high`; evidence age
is `unknown` while queued, running, failed, or otherwise unknown research has no
evidence timestamp. Queued or running research animates on the dashboard's
existing activity clock, including when no OpenCode session is live.

The report header distinguishes running, completed, partial, and failed
artifacts. Reports or evidence older than 24 hours are marked stale. A missing
artifact and a report with no exact match for the selected project have distinct
empty states. `partial` means project research has mixed completed and failed
outcomes; it does not imply normal project omission.

The view is advisory and read-only. It has no command for executing a
recommendation, and process/session actions are blocked whenever the NEXT tab
is active, regardless of keyboard focus or mouse activation.
Report values are rendered as sanitized plain `Rich Text`, never as Rich markup
or Markdown. Privacy mode immediately replaces project and report details with
a hidden-content notice.

## Terminal instance counts

The `INST` session column counts running OpenCode TUI processes that explicitly
name that session with `--session` or `-s`. If the same session is open in two
terminals, its count is `2`. Plain `opencode` and `--continue` launches do not
expose their current session ID, so they are included in the total as
**unlinked TUI** instances instead of being guessed onto a session.

Sessions reported as `busy` or `retry` animate their state icon, as does any
session whose own latest assistant turn is still open (see the agents board
below). A live terminal with no active turn shows a static IDLE marker; only
genuine activity animates.

## Live agents board

Press `4` for the agents board: every session that is currently alive, sorted
by the time of your latest prompt (newest first), with full keyboard navigation
(`j`/`k` to move, `Enter` or `o` to attach). The `AGE` column continues to show
the session's latest update age. Live subagents are grouped beneath their live
parent. Press `Left` on a parent row to expand or collapse its inline list;
pressing `Left` on a leaf subagent collapses the list and returns to its parent.
Each child keeps its own state, terminal exposure, project, age, and detail.
The state cell also shows how long the agent has remained in its current state.

- **PERMISSION (red)** — OpenCode reports a pending permission request
- **QUESTION (purple)** — a tool question is waiting for your answer
- **RUNNING (green)** — the HTTP API or local plugin reports the session busy
- **STALLED (yellow)** — a live TUI has an unfinished assistant turn without
  an explicit busy signal; inspect or fix the terminal before continuing
- **RETRY (amber)** — the session is retrying after an error
- **REVIEW (orange)** — a live terminal whose latest assistant turn completed
  recently, after your latest prompt, and now waits for your judgement
- **IDLE (cyan)** — a live terminal with no active or freshly finished turn

States stay truthful in both directions: an explicitly busy task reads RUNNING,
while an unfinished turn without that signal reads STALLED until it is fixed or
completed. A stopped job — killed from OC Deck's `x` action, inside the TUI, or
by process death anywhere else — leaves RUNNING, STALLED, or REVIEW within
roughly two seconds. A lightweight pulse re-checks only process liveness, tmux
mapping, permission files, the status endpoints, and session database metadata;
the slower full collection sweep keeps its regular cadence.
A dead terminal never stays RUNNING merely because its last message row is
missing a completion timestamp.

Full refreshes and activity pulses are generation-ordered: an older, slower
pulse cannot overwrite a newer full snapshot and roll labels back to stale
values.

The `TERM` column distinguishes terminal exposure:

- **OPEN** — the tmux session currently has an attached terminal viewer
- **BG TMUX** — OpenCode is running in tmux without an attached viewer
- **DIRECT** — the live OpenCode TUI is not mapped to a tmux session

Pending permission and question states are also published locally by the
permission-notify plugin, so `PERMISSION` and `QUESTION` remain visible when the
header shows `LOCKED`.
Busy/retry states still come from OpenCode's HTTP API; export
`OPENCODE_SERVER_PASSWORD` to OC Deck's environment (for example via
`systemctl --user import-environment OPENCODE_SERVER_PASSWORD` before launching
the indicator) to expose those states.

## Data sources

- `opencode session list --format json --pure`
- `opencode debug scrap --pure`
- OpenCode's session database, opened read-only, solely for session metadata:
  archived IDs (`session.time_archived`), native parent IDs, your latest prompt
  time per session, and each session's latest assistant-turn timestamps and
  finish reason (`message.data` JSON fields only). Only native parent IDs — a
  subagent actually spawned by its parent agent — group live subagents without
  changing project routing; worker sessions launched by an orchestrator stay
  top-level. Archived sessions never
  appear in OC Deck's session list, project panes, counts, or agents board; turn
  metadata drives the truthful RUNNING / REVIEW / OPEN states. Tool output is
  never queried
- The configured Markdown project catalog
- The configured, size-bounded Home Agent `latest.json` briefing artifact
- OpenCode's loopback HTTP health/status endpoints (including pending
  permission requests) when `OPENCODE_SERVER_PASSWORD` is available
- `systemctl --user` for a small allowlist of local services
- Same-user `/proc/<pid>/comm` and `/proc/<pid>/cmdline` for OpenCode TUI counts
- `/proc/<pid>/fd/0` and `tmux list-panes` to locate the tmux session behind a
  live terminal
- `/proc` aggregate files and `shutil.disk_usage` for machine health

Session metadata collection uses bounded parallelism across catalog project
directories and retries one transient CLI failure before displaying a warning.

OC Deck never reads `auth.json`, OpenCode transcripts, tool output, or logs. It
opens OpenCode's SQLite database read-only, and only for the session metadata
  described above (archived IDs, native parent IDs, prompt times,
  assistant-turn timestamps); it writes nothing there and never opens credential
  files. It does not read process
environments. It keeps snapshots in memory only. It does not call
`home-agentctl` or inspect Home Agent transcripts or state files; the briefing
artifact is its only Home Agent input. If the database is missing, locked, or
unreadable, OC Deck shows every session as usual instead of failing, with live
terminals falling back to OPEN rather than guessing RUNNING or REVIEW.
