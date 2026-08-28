# Home Agent

`home_agent` is the persistent OpenCode project orchestrator for the projects
shown in OC Deck. All Home Agent implementation, agent prompts, task state, and
systemd source units live in this directory.

## Workflow

1. The user asks `home_agent`, `jarvis`, or `jasmine` directly, or submits a
   confirmed voice request.
2. A voice submission creates one fresh root session through the existing voice
   application. If the router selected a legacy `voice-*` name, the global
   guard promotes that same session and its first message to Jarvis by default
   or explicitly addressed Jasmine before the model runs; it never creates a
   second intake or monitor session.
3. The selected Home Agent primary resolves the project from OC Deck's canonical
   Markdown catalog.
4. It reads progress from the last two or three relevant OpenCode sessions and
   then verifies that progress in the project directory and durable project
   note.
5. It creates a **new** tracked OpenCode worker session in that project
   directory or uses one or more Task subagents for bounded delegated work.
6. An explicit or pinned preferred agent wins; otherwise the controller uses
   the agent from the newest project session, then its last controller launch,
   and finally `build`.
7. For controller workers, the systemd timer records completion when the worker
   becomes idle with an assistant result. OC Deck discovers the new session by
   its project path; Task results return directly to their parent.

Each controller worker is created with explicit read, search, edit,
verification, planning, and Task permissions inside the one selected catalog
project and may update that project's advertised durable note. The guard uses
a tool allowlist, blocks other filesystem roots and unknown tools, and carries
the same project/no-delete policy into Task child sessions.

The original interactive primary remains titled `home_agent`. Each voice
utterance has its own visible `Jarvis Voice` or `Jasmine Voice` orchestration
session. Requests explicitly queued with
`home-agentctl request` and timer dispatch use the separate
`home_agent_monitor` session so background work cannot race with an active TUI
conversation.

No scientific or coding task is created autonomously. The monitor only runs
requests explicitly queued by the user.

## Filesystem Policy

`projects.md` is the filesystem authority for Home Agent work. The primary
orchestrator may read every listed code root and durable note, but an approved
task is written only through a worker scoped to the selected row. Files may be
created or overwritten in that project root and its advertised note. Files and
directories must never be deleted, moved, or renamed.

The orchestrator may delegate through `home-agentctl`, which uses the OpenCode
API for durable OC Deck sessions, or through the Task tool for
foreground/background subagents. Delegates are active workers: subject to their
agent specialization, they may inspect, edit, run tests, and complete the
assigned work. Task children inherit the managed parent's filesystem and
no-delete policy. A direct orchestrator Task prompt must contain one exact
catalog code path, which is converted into a one-use child scope token;
resuming an unrelated or broader `task_id` is rejected.

The restriction is enforced in layers rather than relying on prompt wording:

- Agent and create-time session permissions start with `* = deny`, then allow
  only the tools and external paths required by that role.
- `plugin/home-agent-guard.js` recognizes controller-tagged sessions, checks
  read/write tool paths after resolving symlinks, rejects unknown tools, and
  rejects `apply_patch` delete and move directives.
- Managed Bash commands run through `bin/no-delete-exec.c`, a Landlock wrapper
  that allows writes only in the selected project roots while denying unlink,
  directory removal, and rename syscalls everywhere.
- A single syntactically isolated `home-agentctl` command is exempt from the
  Bash wrapper because the trusted controller must atomically update its own
  state. It has no project-file deletion command. Chaining, redirection, and
  command substitution remove that exemption.

Voice orchestration remains controller-only, and portfolio reporters remain
strictly read-only. Existing OpenCode processes must be restarted after an
agent or plugin update.

The Bash wrapper requires Linux Landlock ABI 3 or newer so truncation and all
write locations can be constrained together. It fails closed when that kernel
support is unavailable.

## Portfolio Briefings

Portfolio briefings are a separate read-only research workflow. A start takes
an immutable snapshot of every catalog project, its durable note location, and
up to three recent relevant non-reporter sessions. It then launches one fresh
OpenCode reporter session per available project, with at most the configured
number active at once. Missing project directories remain visible as failed,
unknown assessments and are not launched.

Only one briefing may be active controller-wide. Startup atomically rejects a
second running briefing and verifies the selected agent through `/agent` in
every unique available project directory before writing state. Thus
`--max-workers` is a global Home Agent briefing bound, not a per-run loophole.

```bash
home-agentctl briefing start
home-agentctl briefing start --agent project-reporter --max-workers 3 --json
home-agentctl briefing status [report-id] --json
home-agentctl briefing show [report-id]
home-agentctl briefing show [report-id] --json
```

`--max-workers` defaults to 3 and is clamped to 1 through 4. `start` validates
the selected agent against OpenCode's `/agent` response before creating a run.
`status` and `show` use the latest report when no ID is supplied and are
strictly read-only. Non-JSON `show` prints the terminal progress diagram;
`--json` emits the canonical artifact.

The existing monitor harvests idle reporter sessions, validates and sanitizes
their JSON, marks errors or missing output as failed, and fills newly available
worker slots. Running reports expose queued, running, completed, and failed
project research so OC Deck can display progress. Final status is `completed`
when every project succeeds, `partial` for mixed outcomes, and `failed` when no
project succeeds.

Reporter sessions receive their own deny-by-default permission ruleset even
when `--agent` selects another available agent. The controller allows only
local read/search tools, exact project and durable-note external paths,
web fetch/search, and a fixed Playwright research allowlist.
It requests a bounded strict JSON Schema response with two validation retries;
the create-time rules remain intact instead of being replaced by deprecated
per-prompt tool flags.

The session ID is persisted before prompt submission. Ambiguous create or
prompt transport failures retain their worker slot while the monitor searches
for a session with matching briefing metadata. Every worker has a 30-minute
deadline; the monitor aborts a known session and marks the project failed when
that deadline expires. Malformed or unsupported briefing state fails closed
rather than replacing saved history.

Reports can contain proposed next steps, but every one is stored with
`requiresApproval: true`. Neither briefing startup nor the monitor queues,
launches, or executes those recommendations. The user must approve a separate
task through the normal request workflow.

## Commands

```bash
home-agentctl projects
home-agentctl recent --project "Project Alpha" --query "release status" --limit 3
home-agentctl set-agent --project "Project Beta" --agent build
home-agentctl request --project "Project Beta" --task "Run the smoke tests"
home-agentctl request --project "Project Alpha" --agent build --task "Inspect release inputs"
home-agentctl briefing start --max-workers 3
home-agentctl briefing status --json
home-agentctl briefing show
home-agentctl status
```

`set-agent` pins a preferred agent for a project. Without that pin or a
per-request `--agent`, the controller inspects the newest project sessions and
uses the most recently used non-Home-Agent worker.

## Voice Orchestration

Voice requests containing a project name or "home agent" still route through
`voice-home-agent`, and mutation-capable requests retain the voice
application's existing `go ahead` confirmation gate. All eleven tracked
`voice-*` outcomes are now compatibility ingress names only. Their definitions
deny every tool and fail closed if the global guard is not active.

For a root voice session, `plugin/home-agent-guard.js` handles the first message
before any model call and first persists a claim containing the ingress agent
and message ID. It selects `jarvis` by default; a transcript beginning with an
explicit Jarvis address also selects `jarvis`, while an explicit Jasmine address
selects `jasmine`. OpenCode's session PATCH cannot save an agent or replace a
permission ruleset, so the guard performs a no-reply rewrite of that same,
not-yet-saved message ID. This saves the selected named agent and model and
replaces the accepted voice defaults with the normal `doom_loop` denial without
adding a final message or invoking a model. The root session is visibly titled
`Jarvis Voice` or `Jasmine Voice`, and `homeAgent` metadata records the selected
agent, display name, model, ingress agent, and exact message ID.

Jarvis uses `openai/gpt-5.6-sol`. Jasmine uses the explicitly registered
`openrouter/thinkingmachines/inkling:free` endpoint and accepts text transcript
parts only. Raw audio, images, files, and other attachments are rejected before
the session is loaded or changed, and Jasmine's configured output modality is
text only. The local phone/watch speech layer may speak the resulting text.
Free-endpoint prompts may be logged, so Jasmine must not receive sensitive or
confidential text or browser content.

The guard reloads the session and verifies its title, agent, model,
`homeAgent.kind = orchestrator` metadata, and normalized permission state before
releasing the original message. Safe incomplete persistence can be retried;
unknown, permissive, or prompt-level permission overrides fail closed. There is
no automatic Jarvis-to-Jasmine fallback: OpenCode does not expose a verified
pre-side-effect retry boundary, so a provider failure remains in Jarvis and the
user must explicitly request Jasmine.

The existing session then uses the selected full Home Agent alias and
catalog-wide permissions under the project path guard and no-delete sandbox.
The guard does not call the session-create API, rejects child-session promotion,
and accepts only one distinct voice message per ingress session. The controller
reserves `home_agent`, `jarvis`, `jasmine`, every `voice-*` ingress, and the
read-only reporter from project-worker selection, preventing recursion.

The tracked desktop launcher always writes `agent` to `/tmp/voice_ptt_mode`,
including when a terminal has focus, before signaling the existing helper.
The helper installed as `~/.local/bin/voice-ptt` is outside this
repository and is not installed or modified here. It must continue to honor
that mode file, create a fresh session for each submission, and select one of
the tracked `voice-*` ingress names. A direct `home_agent` submission has
no trusted voice-ingress provenance and is intentionally not promoted. Any
untracked helper route requires a corresponding external update rather than a
guard bypass.

## Jarvis And Jasmine Browser

`config/opencode.jsonc` defines a separate `signed_in_tabs` MCP server pinned to
`@playwright/mcp@0.0.79`. It uses the official `--extension` connection and does
not replace the reporter's separate isolated `playwright_*` server. Install the
[official Playwright Extension](https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm)
in Chrome, approve the MCP connection, and select or drag only the intended tabs
into that client's colored tab group. New tabs created through the connection
share the browser's logged-in state. The extension group limits what that client
normally sees, but neither it, Playwright MCP, nor an origin list is described
as a security boundary.

Only root Jarvis and Jasmine sessions can execute `signed_in_tabs_*` tools. The
guard denies the prefix to Home Agent, reporters, workers, Task children, and
unmanaged agents. Jarvis and an approved Jasmine session may list/select tabs,
open new tabs, navigate to any HTTP(S) site, go back, inspect accessibility
snapshots, find content, click, hover, drag, type, fill forms, select options,
press keys, handle dialogs, and wait. A session may close only its current tab
when the guard observed that same session create it. Local-file and non-web URL
schemes, output filenames, uploads/drops, screenshots, browser shutdown,
network/console inspection, JavaScript evaluation, unsafe Playwright code, and
browser installation remain denied. The MCP command intentionally omits
`--allow-unrestricted-file-access` and optional network/devtools capabilities.

Jasmine's first browser operation must be `browser_tabs`. OpenCode keeps that
permission at `ask`. The guard enriches the current `permission.asked` event
(and the legacy permission hook) with a warning that the free endpoint may log
prompts and page content. A failed MCP result does not grant browser consent or
tab ownership. Once the first tab operation succeeds, normal navigation and
research tools are low-hassle; choosing **Always allow** for the tab tool avoids
repeated tab-management prompts in that OpenCode session. Both aliases
must still obtain explicit user confirmation or a separate OpenCode permission
before public posting, purchases, account/security changes, uploads, remote
deletion, or another truly consequential browser action.

If the extension cannot be used, the documented fallback is a separate headed
Chrome profile, not the user's live Chrome profile. Replace `--extension` in the
`signed_in_tabs` command with:

```text
--browser chrome --user-data-dir ~/.cache/playwright-mcp/jarvis-jasmine
```

Sign in once in that headed profile. Do not combine this fallback with
`--extension`, and do not point Playwright at Chrome's normal profile because a
profile can be owned by only one browser process at a time.

## Phone Approval Bridge

The global auto-discovered `approval-forward.js` wrapper loads the tracked
plugin source. A process-wide, workspace-keyed registry prevents duplicate
hooks without suppressing approvals for other OpenCode workspaces. The existing local
`permission-notify.js` plugin remains auto-discovered and unchanged, so desktop
permission behavior continues alongside phone/watch forwarding.

The companion contract is:

- `POST /api/approvals` with `requester.agent`, `requester.host`, `permission`,
  bounded `pattern` and `summary` text, and `ttl_seconds`; the response supplies
  an approval `id`.
- Poll `GET /api/approvals/{id}` until `status` is `answered`, `expired`, or
  `cancelled`. An answered result has `decision: allow` or `decision: deny`.
- Map `allow` to OpenCode `once` and `deny` to `reject`, replying to the exact
  original session ID and permission request ID. A local native reply cancels
  the matching poll and resolves the backend projection with the same decision,
  including when the native reply races approval creation. Duplicate permission
  events share one pending request.
- Posting yields for one microtask so guard-enriched warnings on the same
  permission event reach FullClock regardless of plugin load order. Both legacy
  and current SDK reply errors remain retryable for the request TTL.
- Backend errors, malformed responses, missing client methods, and TTL expiry
  leave the native OpenCode prompt pending. They never auto-allow or auto-deny.

The default base is `http://127.0.0.1:8443`. TTL is bounded to the backend's
30-900 second contract and polling/connection values are validated. Runtime overrides are
`FULLCLOCK_BASE_URL`, `FULLCLOCK_TOKEN`, `FULLCLOCK_LOCAL_APPROVALS`,
`FULLCLOCK_TTL_MS`, `FULLCLOCK_POLL_MS`, and
`FULLCLOCK_CONNECT_TIMEOUT_MS` and `FULLCLOCK_REQUESTER_LABEL`. This repository
does not create a companion client or inspect an unregistered companion source.

## Files

| Path | Purpose |
| --- | --- |
| `home_agent.py` | Catalog lookup, session context, task state, and API launch controller |
| `agent/home_agent.md` | Global primary OpenCode agent source |
| `agent/jarvis.md` | OpenAI-backed named Home Agent primary |
| `agent/jasmine.md` | Text-only free OpenRouter named Home Agent primary |
| `agent/project-reporter.md` | Hidden read-only one-project briefing reporter source |
| `agent/voice-home-agent.md` | Fail-closed Home Agent voice ingress source |
| `plugin/home-agent-guard.js` | Managed-session tool/path enforcement and Bash wrapping |
| `bin/no-delete-exec.c` | Landlock write-scope and no-delete launcher source |
| `voice/route.json` | High-priority voice router entry |
| `$XDG_STATE_HOME/home-agent/runtime.json` | Persistent Home Agent session and last-used agents |
| `$XDG_STATE_HOME/home-agent/tasks.json` | Explicit task queue and worker outcomes |
| `$XDG_STATE_HOME/home-agent/briefings.json` | Persistent briefing runs, snapshots, worker IDs, and outcomes |
| `$XDG_STATE_HOME/home-agent/reports/` | Canonical and latest report views |
| `state/*.example.json` | Empty, non-personal runtime schemas |
| `systemd/` | Persistent user monitor source units |

Installed OpenCode agent files and systemd units are symlinks back to this
directory, so this repository remains the source of truth.
OpenCode reads agent files only at configuration time. After installing or
updating `home_agent`, `project-reporter`, or another agent source, quit and
restart OpenCode before starting a briefing; otherwise `/agent` will not expose
the new definition to the controller.

Voice promotion additionally requires the global guard symlink below and all
tracked sources in `../agent/voice-*.md` plus `agent/voice-home-agent.md`,
`agent/jarvis.md`, and `agent/jasmine.md` to be installed in OpenCode's global
agent directory. The effective OpenCode configuration must incorporate
`../config/opencode.jsonc`; it registers the exact Jasmine model and the pinned
`signed_in_tabs` MCP. Approval forwarding is installed through plugin
auto-discovery instead of a machine-specific file URL.

For activation, install the official Chrome extension, ensure those agent and
guard links point at this source tree, deploy the tracked config through the
workstation's existing config mechanism, then quit every OpenCode TUI and
restart the OpenCode server. Confirm `/agent` lists `jarvis` and `jasmine`, and
that MCP status lists `signed_in_tabs` separately from the reporter's isolated
Playwright server. The first extension connection requires browser approval and
tab-group selection. No `home-agent-monitor` or GNOME restart is required for
these agent/plugin/config changes.

After deploying `keymapping/gnome-extension/voice-launch@local` to the user's
GNOME extension directory, a Wayland session requires logout/login to reload
the JavaScript. Merely restarting OpenCode does not reload GNOME Shell code.

Install the guard from this source tree with:

```bash
mkdir -p ~/.local/lib/home-agent ~/.config/opencode/plugins
cc -std=c17 -O2 -Wall -Wextra -Werror \
  -o ~/.local/lib/home-agent/no-delete-exec bin/no-delete-exec.c
ln -sfn "$PWD/plugin/home-agent-guard.js" \
  ~/.config/opencode/plugins/zz-home-agent-guard.js
ln -sfn "$PWD/../plugins/approval-forward.js" \
  ~/.config/opencode/plugins/approval-forward.js
```

Set `HOME_AGENT_PROJECTS_FILE`, `HOME_AGENT_ROUTES_FILE`, and optionally
`HOME_AGENT_VAULT_ROOT` in the OpenCode and monitor environments. Portable
defaults live under `~/.config/home-agent/`; private runtime state defaults to
`$XDG_STATE_HOME/home-agent/` (or `~/.local/state/home-agent/`).

## Sync

Project outcomes belong in each catalog entry's advertised durable note.
Runtime state remains local and must not be synced or committed.
