import { randomUUID } from "node:crypto"
import { existsSync, readFileSync, realpathSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path"
import { fileURLToPath } from "node:url"

const CONFIG_HOME = process.env.XDG_CONFIG_HOME || join(homedir(), ".config")
export const VAULT_ROOT = resolve(
  process.env.HOME_AGENT_VAULT_ROOT || join(CONFIG_HOME, "home-agent"),
)
export const CATALOG_PATH = resolve(
  process.env.HOME_AGENT_PROJECTS_FILE || join(VAULT_ROOT, "projects.md"),
)
export const ROUTES_PATH = resolve(
  process.env.HOME_AGENT_ROUTES_FILE || join(VAULT_ROOT, "session-routes.json"),
)
export const SANDBOX_PATH = resolve(
  process.env.HOME_AGENT_SANDBOX_PATH || join(homedir(), ".local/lib/home-agent/no-delete-exec"),
)
const CONTROLLER_COMMAND = resolve(
  process.env.HOME_AGENT_CONTROLLER || join(homedir(), ".local/bin/home-agentctl"),
)
export const VOICE_INGRESS_AGENTS = Object.freeze([
  "voice-admin",
  "voice-builder",
  "voice-calendar",
  "voice-code-read",
  "voice-files",
  "voice-general",
  "voice-git-read",
  "voice-git-write",
  "voice-home-agent",
  "voice-research",
  "voice-system",
])

const HOME_AGENT = "home_agent"
export const HOME_ORCHESTRATORS = Object.freeze({
  jarvis: Object.freeze({
    id: "jarvis",
    displayName: "Jarvis",
    title: "Jarvis Voice",
    model: Object.freeze({ providerID: "openai", modelID: "gpt-5.6-sol" }),
  }),
  jasmine: Object.freeze({
    id: "jasmine",
    displayName: "Jasmine",
    title: "Jasmine Voice",
    model: Object.freeze({ providerID: "openrouter", modelID: "thinkingmachines/inkling:free" }),
  }),
})
const DEFAULT_VOICE_ORCHESTRATOR = HOME_ORCHESTRATORS.jarvis
const ORCHESTRATOR_AGENT_SET = new Set([HOME_AGENT, ...Object.keys(HOME_ORCHESTRATORS)])
const VOICE_INGRESS_SET = new Set(VOICE_INGRESS_AGENTS)
const MANAGED_AGENT_SET = new Set([...ORCHESTRATOR_AGENT_SET, ...VOICE_INGRESS_AGENTS, "project-reporter"])
const VOICE_INGRESS_DENIALS = new Set(["question", "plan_enter", "plan_exit"])
const VOICE_PERMISSION_RESET_TOOL = "doom_loop"

function authorityFiles(catalogPath, vaultRoot, routesPath) {
  const catalogFile = catalogPath instanceof URL
    ? fileURLToPath(catalogPath)
    : resolve(catalogPath)
  return [catalogFile, join(vaultRoot, "registry.json"), resolve(routesPath)]
}

const REPORTER_BROWSER_TOOLS = new Set([
  "playwright_browser_navigate",
  "playwright_browser_navigate_back",
  "playwright_browser_snapshot",
  "playwright_browser_click",
  "playwright_browser_hover",
  "playwright_browser_tabs",
  "playwright_browser_wait_for",
  "playwright_browser_console_messages",
  "playwright_browser_network_requests",
  "playwright_browser_close",
])

const RESEARCH_TOOLS = new Set([
  "read",
  "glob",
  "list",
  "webfetch",
  "websearch",
  ...REPORTER_BROWSER_TOOLS,
])

const PROJECT_TOOLS = new Set([
  "read",
  "glob",
  "grep",
  "list",
  "lsp",
  "edit",
  "write",
  "apply_patch",
  "bash",
  "question",
  "todowrite",
  "skill",
  "task",
  "webfetch",
  "websearch",
])

const BROWSER_TOOLS = new Set([
  "signed_in_tabs_browser_navigate",
  "signed_in_tabs_browser_navigate_back",
  "signed_in_tabs_browser_snapshot",
  "signed_in_tabs_browser_find",
  "signed_in_tabs_browser_click",
  "signed_in_tabs_browser_hover",
  "signed_in_tabs_browser_drag",
  "signed_in_tabs_browser_tabs",
  "signed_in_tabs_browser_wait_for",
  "signed_in_tabs_browser_type",
  "signed_in_tabs_browser_fill_form",
  "signed_in_tabs_browser_select_option",
  "signed_in_tabs_browser_press_key",
  "signed_in_tabs_browser_handle_dialog",
])
const BROWSER_TOOL_PREFIX = "signed_in_tabs_"
const JASMINE_BROWSER_WARNING =
  "Jasmine uses a free third-party endpoint that may log prompts and browser content. Approve only if this browser task contains no sensitive or confidential data."

function markdownValue(value) {
  let result = value.trim()
  if (result.startsWith("`") && result.endsWith("`")) result = result.slice(1, -1)
  if (result.startsWith("[[") && result.endsWith("]]")) {
    result = result.slice(2, -2).split("|").at(-1)
  }
  return result.trim()
}

function catalogEntries(source, vaultRoot = VAULT_ROOT) {
  const lines = source.split(/\r?\n/)
  let columns
  let header = -1
  for (let index = 0; index < lines.length; index++) {
    if (!lines[index].includes("|")) continue
    const cells = lines[index].trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim().toLowerCase())
    if (!cells.includes("project") || !cells.includes("code")) continue
    columns = { code: cells.indexOf("code"), note: cells.indexOf("vault note") }
    header = index
    break
  }
  if (!columns || header < 0) throw new Error(`Home Agent catalog table is missing: ${CATALOG_PATH}`)

  const entries = []
  for (const line of lines.slice(header + 1)) {
    if (!line.trim()) {
      if (entries.length) break
      continue
    }
    if (!line.includes("|")) continue
    const cells = line.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim())
    if (cells.every((cell) => /^[-: ]+$/.test(cell))) continue
    const code = markdownValue(cells[columns.code] ?? "")
    const note = markdownValue(cells[columns.note] ?? "")
    if (code) {
      entries.push({
        root: resolve(code),
        note: note ? (isAbsolute(note) ? resolve(note) : resolve(vaultRoot, note)) : "",
      })
    }
  }
  if (!entries.length) throw new Error(`Home Agent catalog is empty: ${CATALOG_PATH}`)
  return entries
}

export function parseCatalog(source, vaultRoot = VAULT_ROOT) {
  const entries = catalogEntries(source, vaultRoot)
  return {
    roots: [...new Set(entries.map((entry) => entry.root))],
    files: [...new Set(entries.map((entry) => entry.note).filter(Boolean))],
  }
}

export function catalogPolicy(
  catalogPath = CATALOG_PATH,
  vaultRoot = VAULT_ROOT,
  routesPath = ROUTES_PATH,
) {
  const parsed = parseCatalog(readFileSync(catalogPath, "utf8"), vaultRoot)
  return {
    kind: "orchestrator",
    readRoots: parsed.roots,
    readFiles: [
      ...parsed.files,
      ...authorityFiles(catalogPath, vaultRoot, routesPath),
    ],
    writeRoots: parsed.roots,
    writeFiles: parsed.files,
    tools: PROJECT_TOOLS,
  }
}

function nearestRealPath(path) {
  let current = resolve(path)
  const missing = []
  while (!existsSync(current)) {
    const parent = dirname(current)
    if (parent === current) return resolve(path)
    missing.unshift(current.slice(parent.length + (parent.endsWith(sep) ? 0 : 1)))
    current = parent
  }
  return resolve(realpathSync.native(current), ...missing)
}

function inside(root, target) {
  const relation = relative(nearestRealPath(root), nearestRealPath(target))
  return relation === "" || (!relation.startsWith(`..${sep}`) && relation !== ".." && !isAbsolute(relation))
}

export function pathAllowed(target, roots, files = []) {
  const candidate = nearestRealPath(target)
  return roots.some((root) => inside(root, candidate)) || files.some((file) => nearestRealPath(file) === candidate)
}

function taskProjectPolicy(prompt, catalogPath, vaultRoot) {
  if (typeof prompt !== "string") throw new Error("Home Agent Task delegation requires a prompt")
  const entries = catalogEntries(readFileSync(catalogPath, "utf8"), vaultRoot)
  const matches = entries
    .filter((entry) => prompt.includes(entry.root))
    .sort((left, right) => right.root.length - left.root.length)
  if (!matches.length) {
    throw new Error("Home Agent Task prompt must include the selected catalog project path")
  }
  const selected = matches[0]
  const unrelated = matches.some(
    (entry) => !inside(entry.root, selected.root) && !inside(selected.root, entry.root),
  )
  if (unrelated) throw new Error("Home Agent Task prompt names more than one catalog project")
  return {
    kind: "delegate",
    readRoots: [selected.root],
    readFiles: selected.note ? [selected.note] : [],
    writeRoots: [selected.root],
    writeFiles: selected.note ? [selected.note] : [],
    tools: PROJECT_TOOLS,
  }
}

export function containsDeleteDirective(patchText) {
  return typeof patchText === "string" && /^(?:\*\*\* Delete File:|\*\*\* Move to:)/m.test(patchText.replace(/\r\n?/g, "\n"))
}

function patchPaths(patchText) {
  const paths = []
  for (const line of patchText.replace(/\r\n?/g, "\n").split("\n")) {
    const match = line.match(/^\*\*\* (?:Add|Update) File:\s*(.+?)\s*$/)
    if (match) paths.push(match[1])
  }
  return paths
}

function shellQuote(value) {
  return `'${String(value).replaceAll("'", `'"'"'`)}'`
}

export function isTrustedControllerCommand(command) {
  if (typeof command !== "string") return false
  const trimmed = command.trim()
  if (
    trimmed !== "home-agentctl" &&
    !trimmed.startsWith("home-agentctl ") &&
    trimmed !== CONTROLLER_COMMAND &&
    !trimmed.startsWith(`${CONTROLLER_COMMAND} `)
  ) return false

  let quote = ""
  for (let index = 0; index < trimmed.length; index++) {
    const character = trimmed[index]
    if (quote === "'") {
      if (character === "'") quote = ""
      continue
    }
    if (quote === '"') {
      if (character === "\\") {
        index++
        continue
      }
      if (character === '"') {
        quote = ""
        continue
      }
      if (character === "`" || (character === "$" && trimmed[index + 1] === "(")) return false
      continue
    }
    if (character === "'" || character === '"') {
      quote = character
      continue
    }
    if (character === "\\") {
      index++
      continue
    }
    if ("\n\r;&|<>`".includes(character) || (character === "$" && trimmed[index + 1] === "(")) return false
  }
  return quote === ""
}

export function sandboxCommand(command, policy, sandboxPath = SANDBOX_PATH) {
  const arguments_ = []
  for (const root of policy.writeRoots) arguments_.push("--write-root", root)
  for (const file of policy.writeFiles.filter((path) => existsSync(path))) arguments_.push("--write-file", file)
  return [
    shellQuote(sandboxPath),
    ...arguments_.map(shellQuote),
    "--",
    "/bin/bash",
    "-c",
    shellQuote(command),
  ].join(" ")
}

function sessionPolicy(info, options) {
  const agent = info?.agent
  const metadata = info?.metadata?.homeAgent
  const directory = resolve(info?.directory || options.directory)
  if (VOICE_INGRESS_SET.has(agent)) {
    return { kind: "voice-ingress", readRoots: [], readFiles: [], writeRoots: [], writeFiles: [], tools: new Set() }
  }
  if (metadata?.kind === "orchestrator" && !info?.parentID && ORCHESTRATOR_AGENT_SET.has(agent)) {
    const policy = catalogPolicy(
      options.catalogPath,
      options.vaultRoot,
      options.routesPath,
    )
    return {
      ...policy,
      agent,
      tools: HOME_ORCHESTRATORS[agent]
        ? new Set([...policy.tools, ...BROWSER_TOOLS])
        : policy.tools,
    }
  }
  const notePath = metadata?.notePath ? resolve(metadata.notePath) : ""
  const projectPath = resolve(metadata?.projectPath || directory)
  if (metadata?.kind === "portfolio-research") {
    return {
      kind: "reporter",
      readRoots: [projectPath],
      readFiles: notePath ? [notePath] : [],
      writeRoots: [],
      writeFiles: [],
      tools: RESEARCH_TOOLS,
    }
  }
  if (metadata) {
    return {
      kind: "worker",
      readRoots: [projectPath],
      readFiles: notePath ? [notePath] : [],
      writeRoots: [projectPath],
      writeFiles: notePath ? [notePath] : [],
      tools: PROJECT_TOOLS,
    }
  }
  if (info?.parentID) return null
  if (ORCHESTRATOR_AGENT_SET.has(agent)) {
    const policy = catalogPolicy(
      options.catalogPath,
      options.vaultRoot,
      options.routesPath,
    )
    return {
      ...policy,
      agent,
      tools: HOME_ORCHESTRATORS[agent]
        ? new Set([...policy.tools, ...BROWSER_TOOLS])
        : policy.tools,
    }
  }
  if (agent === "project-reporter") {
    return {
      kind: "reporter",
      readRoots: [directory],
      readFiles: [],
      writeRoots: [],
      writeFiles: [],
      tools: RESEARCH_TOOLS,
    }
  }
  return null
}

function policyContains(parent, child) {
  const rootsFit = (roots, allowedRoots, allowedFiles) =>
    roots.every((root) => pathAllowed(root, allowedRoots, allowedFiles))
  const filesFit = (files, allowedRoots, allowedFiles) =>
    files.every((file) => pathAllowed(file, allowedRoots, allowedFiles))
  return (
    [...child.tools].every((tool) => parent.tools.has(tool)) &&
    rootsFit(child.readRoots, parent.readRoots, parent.readFiles) &&
    filesFit(child.readFiles, parent.readRoots, parent.readFiles) &&
    rootsFit(child.writeRoots, parent.writeRoots, parent.writeFiles) &&
    filesFit(child.writeFiles, parent.writeRoots, parent.writeFiles)
  )
}

function assertToolPath(tool, args, policy, directory) {
  const read = ["read", "glob", "grep", "list", "lsp"].includes(tool)
  const write = ["edit", "write", "apply_patch"].includes(tool)
  if (!read && !write) return
  const roots = read ? policy.readRoots : policy.writeRoots
  const files = read ? policy.readFiles : policy.writeFiles

  if (tool === "apply_patch") {
    if (containsDeleteDirective(args?.patchText)) throw new Error("Home Agent policy forbids deleting, moving, or renaming files")
    const paths = patchPaths(args?.patchText ?? "")
    if (!paths.length) throw new Error("Home Agent policy could not validate the patch paths")
    for (const path of paths) {
      const target = isAbsolute(path) ? path : resolve(directory, path)
      if (!pathAllowed(target, roots, files)) throw new Error(`Home Agent patch path is outside the selected project: ${path}`)
    }
    return
  }

  const supplied = ["read", "edit", "write", "lsp"].includes(tool) ? args?.filePath : args?.path
  const target = supplied ? (isAbsolute(supplied) ? supplied : resolve(directory, supplied)) : directory
  if (!pathAllowed(target, roots, files)) throw new Error(`Home Agent ${tool} path is outside the selected project: ${target}`)
}

function responseData(response) {
  return response?.data ?? response
}

function permissionCleared(permission) {
  return permission == null || (Array.isArray(permission) && permission.length === 0)
}

function acceptedVoiceIngressPermission(permission) {
  if (permissionCleared(permission)) return true
  if (!Array.isArray(permission) || permission.length !== VOICE_INGRESS_DENIALS.size) return false
  const found = new Set()
  for (const rule of permission) {
    if (!rule || typeof rule !== "object" || Array.isArray(rule)) return false
    const keys = Object.keys(rule).sort()
    if (keys.join("\0") !== "action\0pattern\0permission") return false
    if (
      rule.action !== "deny" ||
      rule.pattern !== "*" ||
      !VOICE_INGRESS_DENIALS.has(rule.permission) ||
      found.has(rule.permission)
    ) {
      return false
    }
    found.add(rule.permission)
  }
  return found.size === VOICE_INGRESS_DENIALS.size
}

function normalizedVoicePermission(permission) {
  if (permissionCleared(permission)) return true
  if (!Array.isArray(permission) || permission.length !== 1) return false
  const [rule] = permission
  return Boolean(
    rule &&
    typeof rule === "object" &&
    !Array.isArray(rule) &&
    Object.keys(rule).sort().join("\0") === "action\0pattern\0permission" &&
    rule.permission === VOICE_PERMISSION_RESET_TOOL &&
    rule.pattern === "*" &&
    rule.action === "deny",
  )
}

function voiceTranscript(parts) {
  return (parts ?? [])
    .filter((part) => part?.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n")
    .trim()
}

export function selectVoiceOrchestrator(parts) {
  const transcript = voiceTranscript(parts)
  const addressed = transcript.match(
    /^(?:(?:hey|hi|hello|ok|okay)\s+)?(?:(?:use|ask|select|switch\s+to)\s+)?(jarvis|jasmine)\b/i,
  )?.[1]?.toLowerCase()
  return HOME_ORCHESTRATORS[addressed] || DEFAULT_VOICE_ORCHESTRATOR
}

function assertVoiceParts(target, parts) {
  if (target.id !== HOME_ORCHESTRATORS.jasmine.id) return
  if ((parts ?? []).some((part) => part?.type !== "text" || typeof part.text !== "string")) {
    throw new Error("Jasmine accepts local text transcripts only; raw audio, images, files, and attachments are forbidden")
  }
}

function modelMatches(info, target) {
  const model = info?.model
  return Boolean(
    model &&
    model.providerID === target.model.providerID &&
    (model.modelID ?? model.id) === target.model.modelID,
  )
}

function assertWebURL(value, context) {
  if (typeof value !== "string" || !value.trim()) throw new Error(`Home Agent ${context} requires a URL`)
  let url
  try {
    url = new URL(value)
  } catch {
    throw new Error(`Home Agent ${context} received an invalid URL`)
  }
  if (!new Set(["http:", "https:"]).has(url.protocol)) {
    throw new Error(`Home Agent ${context} permits HTTP and HTTPS URLs only`)
  }
}

function containsSudo(command) {
  return /(^|[^A-Za-z0-9_./-])(?:\/(?:usr\/)?bin\/)?sudo(?=$|[^A-Za-z0-9_-])/m.test(command)
}

function assertBrowserTool(tool, args, state) {
  if (!BROWSER_TOOLS.has(tool)) throw new Error(`Home Agent policy forbids browser tool: ${tool}`)
  if (args?.filename != null) {
    throw new Error("Home Agent browser policy forbids writing snapshots or browser output to local files")
  }
  if (tool === "signed_in_tabs_browser_navigate") assertWebURL(args?.url, "browser navigation")
  if (tool !== "signed_in_tabs_browser_tabs") return "none"

  const action = args?.action
  if (!["list", "new", "select", "close"].includes(action)) {
    throw new Error(`Home Agent browser policy forbids tab action: ${String(action)}`)
  }
  if (action === "new") {
    if (args?.url != null) assertWebURL(args.url, "new browser tab")
    return "new"
  }
  if (action === "select") {
    if (!Number.isInteger(args?.index) || args.index < 0) {
      throw new Error("Home Agent browser tab selection requires a non-negative integer index")
    }
    return "select"
  }
  if (action === "close") {
    if (args?.index != null || !state.currentTabCreated) {
      throw new Error("Home Agent may close only the current tab that it created in this session")
    }
    return "close"
  }
  return "none"
}

export const HomeAgentGuard = async (input, rawOptions = {}) => {
  const options = {
    catalogPath: rawOptions.catalogPath || CATALOG_PATH,
    vaultRoot: rawOptions.vaultRoot || VAULT_ROOT,
    routesPath: rawOptions.routesPath || ROUTES_PATH,
    sandboxPath: rawOptions.sandboxPath || SANDBOX_PATH,
    directory: input.directory,
  }
  const sessions = new Map()
  const unmanaged = new Set()
  const delegatedPolicies = new Map()
  const pendingTaskScopes = new Map()
  const allowedShellCalls = new Map()
  const voiceClaims = new Map()
  const browserStates = new Map()
  const pendingBrowserActions = new Map()

  const allowShellCall = (sessionID, callID, preserveCredentials) => {
    const key = `${sessionID}:${callID}`
    allowedShellCalls.set(key, preserveCredentials)
    const timer = setTimeout(() => allowedShellCalls.delete(key), 60_000)
    timer.unref?.()
  }

  const rememberTaskScope = (policy) => {
    const token = randomUUID()
    pendingTaskScopes.set(token, policy)
    const timer = setTimeout(() => pendingTaskScopes.delete(token), 60_000)
    timer.unref?.()
    return token
  }

  const remember = (info) => {
    if (!info?.id) return
    sessions.set(info.id, { ...(sessions.get(info.id) || {}), ...info })
    if (info.metadata?.homeAgent || MANAGED_AGENT_SET.has(info.agent)) {
      unmanaged.delete(info.id)
    }
  }

  const persistDelegatedPolicy = async (sessionID, policy) => {
    const currentResponse = await input.client.session.get({
      path: { id: sessionID },
      query: { directory: input.directory },
      throwOnError: true,
    })
    const current = responseData(currentResponse)
    if (!current?.id) throw new Error("Home Agent could not load the delegated Task session")
    const metadata = {
      ...(current.metadata || {}),
      homeAgent: {
        kind: "project-worker",
        projectPath: policy.writeRoots[0],
        notePath: policy.writeFiles[0] || "",
        delegatedBy: "task",
      },
    }
    const updatedResponse = await input.client.session.update({
      path: { id: sessionID },
      query: { directory: input.directory },
      body: { metadata },
      throwOnError: true,
    })
    const updated = responseData(updatedResponse)
    if (
      !updated?.id ||
      updated.metadata?.homeAgent?.kind !== "project-worker" ||
      updated.metadata.homeAgent.projectPath !== policy.writeRoots[0]
    ) {
      throw new Error("Home Agent could not persist the delegated Task scope")
    }
    remember(updated)
  }

  const promoteVoiceSession = async (sessionID, messageID, ingressAgent, target, promptTools) => {
    if (!messageID) throw new Error("Voice orchestration requires a message ID")
    if (
      promptTools != null &&
      (typeof promptTools !== "object" || Array.isArray(promptTools) || Object.keys(promptTools).length)
    ) {
      throw new Error("Voice orchestration refuses prompt-level tool permission overrides")
    }
    const currentResponse = await input.client.session.get({
      path: { id: sessionID },
      query: { directory: input.directory },
      throwOnError: true,
    })
    const current = responseData(currentResponse)
    if (!current?.id || current.id !== sessionID) {
      throw new Error("Home Agent could not load the voice ingress session")
    }
    if (current.parentID) {
      throw new Error("Voice orchestration requires a fresh root session")
    }
    if (current.agent !== target.id && current.agent !== ingressAgent) {
      throw new Error("Voice orchestration cannot promote a session owned by another agent")
    }

    const existing = current.metadata?.homeAgent
    const matchesClaim = (info) => (
      info?.metadata?.homeAgent?.kind === "orchestrator" &&
      info.metadata.homeAgent.role === "voice-orchestration" &&
      info.metadata.homeAgent.ingress === "voice" &&
      info.metadata.homeAgent.ingressAgent === ingressAgent &&
      info.metadata.homeAgent.voiceMessageID === messageID &&
      info.metadata.homeAgent.agent === target.id &&
      info.metadata.homeAgent.displayName === target.displayName &&
      info.metadata.homeAgent.model === `${target.model.providerID}/${target.model.modelID}`
    )
    if (existing) {
      if (!matchesClaim(current)) {
        throw new Error("Voice ingress session is already managed for another request")
      }
      if (
        current.agent === target.id &&
        current.title === target.title &&
        modelMatches(current, target) &&
        normalizedVoicePermission(current.permission)
      ) {
        return current
      }
      const safeIncomplete = (
        (current.agent === ingressAgent && acceptedVoiceIngressPermission(current.permission)) ||
        (
          current.agent === target.id &&
          (acceptedVoiceIngressPermission(current.permission) || normalizedVoicePermission(current.permission))
        )
      )
      if (!safeIncomplete) {
        throw new Error("Voice orchestration session has unsafe incomplete promotion state")
      }
    } else if (!acceptedVoiceIngressPermission(current.permission)) {
      throw new Error("Voice orchestration refuses unknown or permissive session permission overrides")
    }

    const metadata = {
      ...(current.metadata || {}),
      homeAgent: {
        kind: "orchestrator",
        role: "voice-orchestration",
        ingress: "voice",
        ingressAgent,
        voiceMessageID: messageID,
        agent: target.id,
        displayName: target.displayName,
        model: `${target.model.providerID}/${target.model.modelID}`,
      },
    }
    let claimError
    try {
      await input.client.session.update({
        path: { id: sessionID },
        query: { directory: input.directory },
        body: { title: target.title, metadata },
        throwOnError: true,
      })
    } catch (error) {
      claimError = error
    }
    const stagedResponse = await input.client.session.get({
      path: { id: sessionID },
      query: { directory: input.directory },
      throwOnError: true,
    })
    const staged = responseData(stagedResponse)
    if (!staged?.id || staged.id !== sessionID || !matchesClaim(staged)) {
      throw new Error("Home Agent could not persist the voice orchestration claim", {
        cause: claimError,
      })
    }

    // Session PATCH cannot replace permissions or persist an agent in OpenCode 1.18.
    // A no-reply rewrite of this unsaved message does both without adding a turn.
    let normalizationError
    try {
      await input.client.session.prompt({
        path: { id: sessionID },
        query: { directory: input.directory },
        body: {
          messageID,
          agent: target.id,
          model: target.model,
          noReply: true,
          tools: { [VOICE_PERMISSION_RESET_TOOL]: false },
          parts: [],
        },
        throwOnError: true,
      })
    } catch (error) {
      normalizationError = error
    }
    const verifiedResponse = await input.client.session.get({
      path: { id: sessionID },
      query: { directory: input.directory },
      throwOnError: true,
    })
    const updated = responseData(verifiedResponse)
    if (
      !updated?.id ||
      updated.id !== sessionID ||
      updated.agent !== target.id ||
      updated.title !== target.title ||
      !modelMatches(updated, target) ||
      !normalizedVoicePermission(updated.permission) ||
      !matchesClaim(updated)
    ) {
      throw new Error("Home Agent could not persist the voice orchestration session", {
        cause: normalizationError,
      })
    }
    remember(updated)
    return updated
  }

  const policyFor = async (sessionID, seen = new Set()) => {
    if (seen.has(sessionID)) return null
    seen.add(sessionID)
    if (delegatedPolicies.has(sessionID)) return delegatedPolicies.get(sessionID)
    if (unmanaged.has(sessionID)) return null
    let info = sessions.get(sessionID)
    if (!info || (!info.metadata && !MANAGED_AGENT_SET.has(info.agent))) {
      try {
        const response = await input.client.session.get({
          path: { id: sessionID },
          query: { directory: input.directory },
        })
        info = responseData(response)
        remember(info)
      } catch {
        // Unknown non-Home-Agent sessions must remain unaffected by this global plugin.
      }
    }
    const policy = sessionPolicy(info, options)
    if (policy) return policy
    if (info?.parentID) {
      const parent = await policyFor(info.parentID, seen)
      if (parent) return { ...parent, kind: "delegate", agent: info.agent }
      return null
    }
    if (info) unmanaged.add(sessionID)
    return null
  }

  return {
    config: (config) => {
      let policy
      try {
        policy = catalogPolicy(
          options.catalogPath,
          options.vaultRoot,
          options.routesPath,
        )
      } catch {
        return
      }
      const externalDirectory = { "*": "deny" }
      for (const root of policy.readRoots) {
        externalDirectory[`${root.replace(/\/$/, "")}/**`] = "allow"
      }
      for (const file of policy.readFiles) externalDirectory[file] = "allow"
      for (const agent of ORCHESTRATOR_AGENT_SET) {
        const definition = config.agent?.[agent]
        if (!definition) continue
        definition.permission = {
          ...(definition.permission || {}),
          external_directory: { ...externalDirectory },
        }
      }
    },
    event: async ({ event }) => {
      if (event?.type === "permission.asked") {
        const properties = event.properties
        const tool = properties?.permission
        const info = properties?.sessionID ? sessions.get(properties.sessionID) : null
        if (
          typeof tool === "string" &&
          tool.startsWith(BROWSER_TOOL_PREFIX) &&
          info?.agent === HOME_ORCHESTRATORS.jasmine.id
        ) {
          properties.metadata = {
            ...(properties.metadata || {}),
            description: JASMINE_BROWSER_WARNING,
            warning: JASMINE_BROWSER_WARNING,
          }
        }
        return
      }
      if (event?.type === "session.deleted") {
        const sessionID = event.properties?.info?.id || event.properties?.sessionID
        sessions.delete(sessionID)
        unmanaged.delete(sessionID)
        delegatedPolicies.delete(sessionID)
        voiceClaims.delete(sessionID)
        browserStates.delete(sessionID)
        for (const key of pendingBrowserActions.keys()) {
          if (key.startsWith(`${sessionID}:`)) pendingBrowserActions.delete(key)
        }
        return
      }
      if (event?.type === "session.created" || event?.type === "session.updated") remember(event.properties?.info)
    },
    "chat.message": async ({ sessionID, agent, messageID }, output) => {
      if (VOICE_INGRESS_SET.has(agent)) {
        const voiceMessageID = output.message?.id || messageID
        const target = selectVoiceOrchestrator(output.parts)
        assertVoiceParts(target, output.parts)
        const claimed = voiceClaims.get(sessionID)
        if (claimed && claimed !== voiceMessageID) {
          throw new Error("Voice ingress session already accepted another request")
        }
        const claimedHere = !claimed
        if (claimedHere) voiceClaims.set(sessionID, voiceMessageID)
        try {
          await promoteVoiceSession(sessionID, voiceMessageID, agent, target, output.message?.tools)
          output.message.agent = target.id
          output.message.model = { ...target.model }
        } catch (error) {
          if (claimedHere && voiceClaims.get(sessionID) === voiceMessageID) {
            voiceClaims.delete(sessionID)
          }
          throw error
        }
        return
      }
      if (agent === HOME_ORCHESTRATORS.jasmine.id) {
        assertVoiceParts(HOME_ORCHESTRATORS.jasmine, output.parts)
      }
      for (const part of output.parts ?? []) {
        if (part?.type !== "text" || typeof part.text !== "string") continue
        const match = part.text.match(/^<home_agent_scope token="([0-9a-f-]{36})" \/>\n?/)
        if (!match) continue
        const policy = pendingTaskScopes.get(match[1])
        if (!policy) throw new Error("Home Agent Task scope token is invalid or expired")
        await persistDelegatedPolicy(sessionID, policy)
        pendingTaskScopes.delete(match[1])
        delegatedPolicies.set(sessionID, policy)
        part.text = part.text.slice(match[0].length)
        break
      }
      if (MANAGED_AGENT_SET.has(agent)) {
        unmanaged.delete(sessionID)
        remember({ id: sessionID, agent, directory: input.directory })
      }
    },
    "tool.execute.before": async ({ tool, sessionID, callID }, output) => {
      const policy = await policyFor(sessionID)
      if (tool.startsWith(BROWSER_TOOL_PREFIX)) {
        const target = policy?.kind === "orchestrator" && HOME_ORCHESTRATORS[policy.agent]
        if (!target) throw new Error(`Home Agent policy forbids browser tool: ${tool}`)
        const state = browserStates.get(sessionID) || {
          consented: target.id !== HOME_ORCHESTRATORS.jasmine.id,
          currentTabCreated: false,
        }
        if (!state.consented && tool !== "signed_in_tabs_browser_tabs") {
          throw new Error("Jasmine must obtain browser privacy approval through tab management before using browser content")
        }
        const transition = assertBrowserTool(tool, output.args, state)
        const grantsConsent = !state.consented
        if ((grantsConsent || transition !== "none") && !callID) {
          throw new Error("Home Agent browser state changes require an exact tool call ID")
        }
        if (callID && (grantsConsent || transition !== "none")) {
          pendingBrowserActions.set(`${sessionID}:${callID}`, { grantsConsent, transition })
        }
        return
      }
      if (!policy) return
      if (!policy.tools.has(tool)) throw new Error(`Home Agent policy forbids tool: ${tool}`)
      if (tool === "task" && output.args?.task_id) {
        const target = await policyFor(output.args.task_id)
        const targetInfo = sessions.get(output.args.task_id)
        if (
          !target ||
          target.kind === "voice-ingress" ||
          targetInfo?.parentID !== sessionID ||
          !policyContains(policy, target)
        ) {
          throw new Error("Home Agent policy forbids resuming an unmanaged, unrelated, or broader Task session")
        }
      }
      if (tool === "task" && !output.args?.task_id && policy.kind === "orchestrator") {
        const scoped = taskProjectPolicy(
          output.args?.prompt,
          options.catalogPath,
          options.vaultRoot,
        )
        const token = rememberTaskScope(scoped)
        output.args.prompt = [
          `<home_agent_scope token="${token}" />`,
          `Managed project scope: ${scoped.writeRoots[0]}`,
          scoped.writeFiles[0] ? `Advertised durable note: ${scoped.writeFiles[0]}` : "",
          "Never delete, move, or rename files or directories.",
          "",
          output.args.prompt,
        ].filter((line) => line !== "").join("\n")
      }

      assertToolPath(tool, output.args, policy, input.directory)
      if (tool !== "bash") return
      const command = output.args?.command
      if (typeof command !== "string" || !command.trim()) throw new Error("Home Agent received an invalid Bash command")
      if (containsSudo(command)) throw new Error("Home Agent policy forbids sudo and privilege escalation")
      const trustedController = policy.kind === "orchestrator" && isTrustedControllerCommand(command)
      if (!trustedController) {
        if (!existsSync(options.sandboxPath)) throw new Error(`Home Agent no-delete sandbox is unavailable: ${options.sandboxPath}`)
        output.args.command = sandboxCommand(command, policy, options.sandboxPath)
      }
      allowShellCall(sessionID, callID, trustedController)
    },
    "tool.execute.after": async ({ tool, sessionID, callID }, output) => {
      if (typeof tool !== "string" || !tool.startsWith(BROWSER_TOOL_PREFIX) || !callID) return
      const key = `${sessionID}:${callID}`
      const pending = pendingBrowserActions.get(key)
      if (!pending) return
      pendingBrowserActions.delete(key)
      if (output?.isError === true) return
      const policy = await policyFor(sessionID)
      const target = policy?.kind === "orchestrator" && HOME_ORCHESTRATORS[policy.agent]
      if (!target) return
      const state = browserStates.get(sessionID) || {
        consented: target.id !== HOME_ORCHESTRATORS.jasmine.id,
        currentTabCreated: false,
      }
      if (pending.grantsConsent) state.consented = true
      if (pending.transition === "new") state.currentTabCreated = true
      if (pending.transition === "select" || pending.transition === "close") {
        state.currentTabCreated = false
      }
      browserStates.set(sessionID, state)
    },
    "permission.ask": async (permission, output) => {
      const tool = permission?.permission
      if (typeof tool !== "string" || !tool.startsWith(BROWSER_TOOL_PREFIX)) return
      const policy = await policyFor(permission.sessionID)
      const target = policy?.kind === "orchestrator" && HOME_ORCHESTRATORS[policy.agent]
      if (!target) {
        output.status = "deny"
        return
      }
      if (target.id !== HOME_ORCHESTRATORS.jasmine.id) return
      permission.metadata = {
        ...(permission.metadata || {}),
        description: JASMINE_BROWSER_WARNING,
        warning: JASMINE_BROWSER_WARNING,
      }
      output.status = "ask"
    },
    "shell.env": async ({ sessionID, callID }, output) => {
      if (!sessionID) return
      const policy = await policyFor(sessionID)
      if (!policy) return
      const key = `${sessionID}:${callID}`
      if (!callID || !allowedShellCalls.has(key)) {
        throw new Error("Home Agent policy forbids shell execution outside the guarded Bash tool")
      }
      const preserveCredentials = allowedShellCalls.get(key)
      allowedShellCalls.delete(key)
      if (!preserveCredentials) {
        output.env.OPENCODE_SERVER_PASSWORD = ""
        output.env.OPENCODE_SERVER_USERNAME = ""
      }
    },
  }
}

export default HomeAgentGuard
