import { spawn } from "node:child_process"
import {
  closeSync,
  constants,
  mkdirSync,
  openSync,
  renameSync,
  unlinkSync,
  writeFileSync,
  writeSync,
} from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

const PTYXIS_APP_ID = "org.gnome.Ptyxis"
const GTK_NOTIFICATIONS_OBJECT = "/org/gtk/Notifications"

const GDBUS = "/usr/bin/gdbus"
const BUSCTL = "/usr/bin/busctl"
const TMUX = "/usr/bin/tmux"
const PTYXIS = "/usr/bin/ptyxis"

const OCDECK_SWITCH_DEST = "org.local.OCDeckSwitch"
const OCDECK_SWITCH_PATH = "/org/local/OCDeckSwitch"
const FOCUS_TMUX_METHOD = "org.local.OCDeckSwitch.FocusTmux"
const NOTIFICATION_APP_ID = OCDECK_SWITCH_DEST

// Non-app actions are returned through org.gtk.Notifications.ActionInvoked.
// An app.* action would bypass this process and be sent to the desktop app.
const APPROVE_ACTION = "opencode.permission.once"
const APPROVE_ALWAYS_ACTION = "opencode.permission.always"
const FOCUS_ACTION = "opencode.permission.focus"
const ACTION_MONITOR_RESTART_MS = 1000
const NOTIFICATION_RESTORE_MS = 250
const NOTIFICATION_ID = `opencode-permission-${process.pid}`
const QUESTION_NOTIFICATION_PREFIX = `opencode-question-${process.pid}`
const NOTIFIER_VERSION = 7
const RUNTIME_ROOT = process.env.XDG_RUNTIME_DIR ||
  join(tmpdir(), `ocdeck-${process.getuid?.() ?? "user"}`)
const PERMISSION_STATE_DIR = join(RUNTIME_ROOT, "ocdeck-permissions")
const PERMISSION_STATE_FILE = join(PERMISSION_STATE_DIR, `${process.pid}.json`)

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

const PREEXEC = "\u001b]666;vte.shell.preexec!\u001b\\"
const PRECMD = "\u001b]666;vte.shell.precmd!\u001b\\"

const IN_TMUX = Boolean(process.env.TMUX)

function wrapForMultiplexer(sequence) {
  if (!IN_TMUX) return sequence
  const payload = sequence.replace(/\u001b/g, "\u001b\u001b")
  return `\u001bPtmux;${payload}\u001b\\`
}

let ttyFD
let tabUUID = ""
let tabOwner = ""
let currentTmuxSession = ""
let discovery
let shownRequest = ""
let shownNotification = ""
let actionMonitor
let actionMonitorRestart
let actionHandler
let disposed = false

const pending = new Map()
const pendingQuestions = new Map()
const sessionStatuses = new Map()
const approving = new Set()

function hasPendingActions() {
  return pending.size > 0 || pendingQuestions.size > 0
}

function syncPermissionState() {
  try {
    if (!pending.size && !pendingQuestions.size && !sessionStatuses.size) {
      try {
        unlinkSync(PERMISSION_STATE_FILE)
      } catch {}
      return
    }

    mkdirSync(PERMISSION_STATE_DIR, { recursive: true, mode: 0o700 })
    const payload = {
      pid: process.pid,
      notifierVersion: NOTIFIER_VERSION,
      updated: Date.now(),
      permissions: [...pending.values()].map(({ request }) => ({
        id: text(request.id, 300),
        sessionID: text(request.sessionID, 300),
        permission: text(request.permission, 80) || "permission",
        pattern: permissionText(request),
      })),
      questions: [...pendingQuestions.values()].map(({ request }) => ({
        id: text(request.id, 300),
        sessionID: text(request.sessionID, 300),
        question: questionText(request),
      })),
      statuses: [...sessionStatuses].map(([sessionID, status]) => ({
        sessionID: text(sessionID, 300),
        status: text(status, 40),
      })),
    }
    const temporary = `${PERMISSION_STATE_FILE}.${Date.now()}.tmp`
    writeFileSync(temporary, JSON.stringify(payload), {
      encoding: "utf8",
      mode: 0o600,
    })
    renameSync(temporary, PERMISSION_STATE_FILE)
  } catch {}
}

function text(value, limit = 500) {
  if (Array.isArray(value)) value = value.filter((item) => typeof item === "string").join(", ")
  if (typeof value !== "string") return ""

  return value
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, limit)
}

function variantString(value, limit = 1000) {
  const valueText = text(value, limit)
    .replace(/\\/g, "\\\\")
    .replace(/'/g, "\\'")

  return `'${valueText}'`
}

function writeTerminal(value) {
  if (ttyFD === undefined) {
    try {
      ttyFD = openSync("/dev/tty", constants.O_WRONLY | (constants.O_NOCTTY ?? 0))
    } catch {
      ttyFD = null
    }
  }

  if (ttyFD === null) return false

  try {
    writeSync(ttyFD, value)
    return true
  } catch {
    try {
      closeSync(ttyFD)
    } catch {}
    ttyFD = null
    return false
  }
}

function spawnQuiet(program, args) {
  try {
    const child = spawn(program, args, { shell: false, stdio: "ignore" })
    child.on("error", () => {})
    child.unref()
    return true
  } catch {
    return false
  }
}

function runQuiet(program, args) {
  return new Promise((resolve) => {
    let child
    let stdout = ""
    let settled = false
    let timeout

    const finish = (result) => {
      if (settled) return
      settled = true
      clearTimeout(timeout)
      resolve(result)
    }

    try {
      child = spawn(program, args, { shell: false, stdio: ["ignore", "pipe", "ignore"] })
    } catch {
      finish("")
      return
    }

    child.stdout.setEncoding("utf8")
    child.stdout.on("data", (chunk) => {
      stdout += chunk
    })
    child.on("error", () => finish(""))
    child.on("exit", (code) => finish(code === 0 ? stdout : ""))
    timeout = setTimeout(() => {
      try {
        child.kill("SIGKILL")
      } catch {}
      finish("")
    }, 750)
  })
}

function notificationCall(method, args) {
  return spawnQuiet(GDBUS, [
    "call",
    "--session",
    "--dest",
    "org.gtk.Notifications",
    "--object-path",
    GTK_NOTIFICATIONS_OBJECT,
    "--method",
    `org.gtk.Notifications.${method}`,
    ...args,
  ])
}

function withdrawPtyxisNotification(notificationID = shownNotification) {
  if (!notificationID) return
  notificationCall("RemoveNotification", [NOTIFICATION_APP_ID, text(notificationID, 300)])
}

function stopActionMonitor() {
  clearTimeout(actionMonitorRestart)
  actionMonitorRestart = undefined

  const monitor = actionMonitor
  actionMonitor = undefined

  try {
    monitor?.kill("SIGTERM")
  } catch {}
}

async function startActionMonitor(handler = actionHandler) {
  actionHandler = handler

  if (disposed || actionMonitor || !hasPendingActions() || typeof actionHandler !== "function") return

  const ownerOutput = await runQuiet(BUSCTL, [
    "--user",
    "--json=short",
    "call",
    "org.freedesktop.DBus",
    "/org/freedesktop/DBus",
    "org.freedesktop.DBus",
    "GetNameOwner",
    "s",
    "org.gtk.Notifications",
  ])

  if (disposed || actionMonitor || !hasPendingActions()) return

  let notificationOwner = ""

  try {
    notificationOwner = text(JSON.parse(ownerOutput)?.data?.[0], 100)
  } catch {}

  if (!notificationOwner) {
    if (!actionMonitorRestart) {
      actionMonitorRestart = setTimeout(() => {
        actionMonitorRestart = undefined
        void startActionMonitor()
      }, ACTION_MONITOR_RESTART_MS)
    }
    return
  }

  let monitor
  let buffer = ""

  const finish = () => {
    if (actionMonitor !== monitor) return
    actionMonitor = undefined

    if (!disposed && hasPendingActions() && !actionMonitorRestart) {
      actionMonitorRestart = setTimeout(() => {
        actionMonitorRestart = undefined
        void startActionMonitor()
      }, ACTION_MONITOR_RESTART_MS)
    }
  }

  const consume = (line) => {
    if (!line.trim()) return

    let message

    try {
      message = JSON.parse(line)
    } catch {
      return
    }

    const data = message?.payload?.data

    if (
      message?.type !== "signal" ||
      message?.path !== GTK_NOTIFICATIONS_OBJECT ||
      message?.interface !== "org.gtk.Notifications" ||
      message?.member !== "ActionInvoked" ||
      message?.sender !== notificationOwner ||
      !Array.isArray(data) ||
      data[0] !== NOTIFICATION_APP_ID ||
      ![APPROVE_ACTION, APPROVE_ALWAYS_ACTION, FOCUS_ACTION].includes(data[2])
    ) {
      return
    }

    const notificationID = text(data[1], 300)
    const action = data[2]
    const requestID = text(data[3]?.[0]?.data, 300)
    const activationToken = text(data[4]?.["activation-token"]?.data, 1000)
    const entry = pending.get(requestID) ?? pendingQuestions.get(requestID)

    if (!entry || entry.notificationID !== notificationID) return
    actionHandler(action, requestID, activationToken)
  }

  try {
    monitor = spawn(
      BUSCTL,
      [
        "--user",
        "--json=short",
        `--match=type='signal',path='${GTK_NOTIFICATIONS_OBJECT}',interface='org.gtk.Notifications',member='ActionInvoked'`,
        "monitor",
      ],
      { shell: false, stdio: ["ignore", "pipe", "ignore"] },
    )
  } catch {
    finish()
    return
  }

  actionMonitor = monitor
  monitor.stdout.setEncoding("utf8")

  monitor.stdout.on("data", (chunk) => {
    buffer += chunk

    for (;;) {
      const newline = buffer.indexOf("\n")
      if (newline < 0) break

      const line = buffer.slice(0, newline)
      buffer = buffer.slice(newline + 1)
      consume(line)
    }
  })

  monitor.stdout.on("end", () => {
    if (buffer.trim()) consume(buffer)
  })

  monitor.on("error", finish)
  monitor.on("exit", finish)
}

function discoverTabUUIDOnce() {
  if (!process.env.PTYXIS_VERSION || !process.env.VTE_VERSION) {
    return Promise.resolve(["", ""])
  }

  if (!writeTerminal("")) return Promise.resolve(["", ""])

  return new Promise((resolve) => {
    let monitor
    let buffer = ""
    let finished = false
    let integrationTouched = false
    let precmdSent = false
    let startTimer
    let precmdTimer
    let deadline

    const finish = (uuid = "", owner = "") => {
      if (finished) return
      finished = true

      clearTimeout(startTimer)
      clearTimeout(precmdTimer)
      clearTimeout(deadline)

      if (integrationTouched) writeTerminal(wrapForMultiplexer(PREEXEC))

      try {
        monitor?.kill("SIGTERM")
      } catch {}

      resolve(UUID_PATTERN.test(uuid) ? [uuid, text(owner, 100)] : ["", ""])
    }

    const consume = (line) => {
      if (!precmdSent || !line.trim()) return

      let message

      try {
        message = JSON.parse(line)
      } catch {
        return
      }

      const data = message?.payload?.data

      if (
        message?.type !== "method_call" ||
        message?.destination !== "org.gtk.Notifications" ||
        message?.interface !== "org.gtk.Notifications" ||
        message?.member !== "AddNotification" ||
        !Array.isArray(data) ||
        data[0] !== PTYXIS_APP_ID
      ) {
        return
      }

      const id = data[1]
      const notification = data[2]
      const action = notification?.["default-action"]?.data
      const target = notification?.["default-action-target"]?.data

      if (action === "app.focus-tab-by-uuid" && target === id && UUID_PATTERN.test(target)) {
        finish(target, message.sender)
      }
    }

    try {
      monitor = spawn(
        BUSCTL,
        [
          "--user",
          "--json=short",
          "--match=type='method_call',destination='org.gtk.Notifications',path='/org/gtk/Notifications',interface='org.gtk.Notifications',member='AddNotification'",
          "monitor",
        ],
        { shell: false, stdio: ["ignore", "pipe", "ignore"] },
      )
    } catch {
      finish()
      return
    }

    monitor.stdout.setEncoding("utf8")

    monitor.stdout.on("data", (chunk) => {
      buffer += chunk

      for (;;) {
        const newline = buffer.indexOf("\n")
        if (newline < 0) break

        const line = buffer.slice(0, newline)
        buffer = buffer.slice(newline + 1)
        consume(line)
      }
    })

    monitor.stdout.on("end", () => {
      if (buffer.trim()) consume(buffer)
    })

    monitor.on("error", () => finish())
    monitor.on("exit", () => finish())

    monitor.on("spawn", () => {
      startTimer = setTimeout(() => {
        integrationTouched = writeTerminal(wrapForMultiplexer(PREEXEC))

        if (!integrationTouched) {
          finish()
          return
        }

        precmdTimer = setTimeout(() => {
          precmdSent = true

          if (!writeTerminal(wrapForMultiplexer(PRECMD))) finish()
        }, 500)
      }, 150)
    })

    deadline = setTimeout(() => finish(), 2500)
  })
}

function getTabUUID() {
  if (UUID_PATTERN.test(tabUUID)) return Promise.resolve([tabUUID, tabOwner])
  if (discovery) return discovery

  const attempt = discoverTabUUIDOnce().then(([uuid, owner]) => {
    if (UUID_PATTERN.test(uuid)) {
      tabUUID = uuid
      tabOwner = owner
    }
    return [uuid, owner]
  })

  const wrapped = attempt.finally(() => {
    if (discovery === wrapped) discovery = null
  })

  discovery = wrapped
  return wrapped
}

function getTmuxSession() {
  if (!IN_TMUX) return Promise.resolve("")
  if (currentTmuxSession) return Promise.resolve(currentTmuxSession)

  return runQuiet(TMUX, ["display-message", "-p", "#S"]).then((output) => {
    currentTmuxSession = text(output, 200)
    return currentTmuxSession
  })
}

async function focusTmuxSession(sessionName) {
  if (!sessionName) return false

  const output = await runQuiet(GDBUS, [
    "call",
    "--session",
    "--dest",
    OCDECK_SWITCH_DEST,
    "--object-path",
    OCDECK_SWITCH_PATH,
    "--method",
    FOCUS_TMUX_METHOD,
    sessionName,
  ])

  return /\btrue\b/i.test(output)
}

function openTmuxSession(entry) {
  const sessionName = text(entry.tmuxSession, 200)
  if (!sessionName) return false

  const title = text(entry.request?.sessionID, 80) || "permission"
  return spawnQuiet(PTYXIS, [
    "--standalone",
    "--new-window",
    "--title",
    `OpenCode - ${title}`,
    `--working-directory=${process.cwd()}`,
    "--",
    TMUX,
    "attach-session",
    "-t",
    sessionName,
  ])
}

async function focusPtyxis(requestID, activationToken) {
  const entry = pending.get(requestID) ?? pendingQuestions.get(requestID)
  if (!entry) return

  const platformData = activationToken
    ? `{'activation-token': <${variantString(activationToken)}>}`
    : "{}"
  const destination = entry.owner || PTYXIS_APP_ID

  let handled = await focusTmuxSession(entry.tmuxSession)

  if (!handled && UUID_PATTERN.test(entry.uuid)) {
    const output = await runQuiet(GDBUS, [
      "call",
      "--session",
      "--dest",
      destination,
      "--object-path",
      "/org/gnome/Ptyxis",
      "--method",
      "org.gtk.Actions.Activate",
      "focus-tab-by-uuid",
      `[<${variantString(entry.uuid)}>]`,
      platformData,
    ])
    handled = Boolean(output.trim())
  }

  if (!handled) openTmuxSession(entry)

  clearTimeout(entry.restoreTimer)
  entry.restoreTimer = setTimeout(() => {
    entry.restoreTimer = undefined
    if (pending.has(requestID) && shownRequest === requestID) {
      showPtyxisPermission(entry, entry.uuid, entry.owner, entry.tmuxSession)
    } else if (pendingQuestions.has(requestID)) {
      showPtyxisQuestion(entry, entry.uuid, entry.owner, entry.tmuxSession)
    }
  }, NOTIFICATION_RESTORE_MS)
}

function showPtyxisPermission(entry, uuid = "", owner = "", tmuxSession = "") {
  const { request, summary, body } = entry

  if (UUID_PATTERN.test(uuid)) withdrawPtyxisNotification(uuid)

  const payload =
    `{` +
    `'title': <${variantString(summary, 120)}>, ` +
    `'body': <${variantString(body, 700)}>, ` +
    `'icon': <('themed', <['dialog-password']>)>, ` +
    `'priority': <'urgent'>, ` +
    `'default-action': <${variantString(FOCUS_ACTION)}>, ` +
    `'default-action-target': <${variantString(request.id)}>, ` +
    `'buttons': <[` +
    `{` +
    `'label': <'Always allow'>, ` +
    `'action': <${variantString(APPROVE_ALWAYS_ACTION)}>, ` +
    `'target': <${variantString(request.id)}>` +
    `}, ` +
    `{` +
    `'label': <'Allow once'>, ` +
    `'action': <${variantString(APPROVE_ACTION)}>, ` +
    `'target': <${variantString(request.id)}>` +
    `}` +
    `]>` +
    `}`

  entry.notificationID = NOTIFICATION_ID
  entry.uuid = uuid || entry.uuid || ""
  entry.owner = owner || entry.owner || ""
  entry.tmuxSession = tmuxSession || entry.tmuxSession || ""
  shownRequest = request.id
  shownNotification = NOTIFICATION_ID

  notificationCall("AddNotification", [NOTIFICATION_APP_ID, NOTIFICATION_ID, payload])
}

function questionNotificationID(requestID) {
  const suffix = text(requestID, 100).replace(/[^a-zA-Z0-9_-]/g, "") || "question"
  return `${QUESTION_NOTIFICATION_PREFIX}-${suffix}`
}

function showPtyxisQuestion(entry, uuid = "", owner = "", tmuxSession = "") {
  const { request, summary, body } = entry

  if (UUID_PATTERN.test(uuid)) withdrawPtyxisNotification(uuid)

  const payload =
    `{` +
    `'title': <${variantString(summary, 120)}>, ` +
    `'body': <${variantString(body, 700)}>, ` +
    `'icon': <('themed', <['dialog-question']>)>, ` +
    `'priority': <'urgent'>, ` +
    `'default-action': <${variantString(FOCUS_ACTION)}>, ` +
    `'default-action-target': <${variantString(request.id)}>, ` +
    `'buttons': <[` +
    `{` +
    `'label': <'Open question'>, ` +
    `'action': <${variantString(FOCUS_ACTION)}>, ` +
    `'target': <${variantString(request.id)}>` +
    `}` +
    `]>` +
    `}`

  entry.notificationID = entry.notificationID || questionNotificationID(request.id)
  entry.uuid = uuid || entry.uuid || ""
  entry.owner = owner || entry.owner || ""
  entry.tmuxSession = tmuxSession || entry.tmuxSession || ""

  notificationCall("AddNotification", [NOTIFICATION_APP_ID, entry.notificationID, payload])
}

function resolveRequest(requestID) {
  const entry = pending.get(requestID)
  if (!entry) return

  pending.delete(requestID)
  syncPermissionState()
  approving.delete(requestID)
  clearTimeout(entry.restoreTimer)

  if (shownRequest !== requestID) return

  shownRequest = ""
  shownNotification = ""

  if (pending.size) {
    void showNextPermission()
  } else {
    withdrawPtyxisNotification(entry.notificationID)
    if (!hasPendingActions()) stopActionMonitor()
  }
}

function resolveQuestion(requestID) {
  const entry = pendingQuestions.get(requestID)
  if (!entry) return

  pendingQuestions.delete(requestID)
  syncPermissionState()
  clearTimeout(entry.restoreTimer)
  withdrawPtyxisNotification(entry.notificationID)

  if (!hasPendingActions()) stopActionMonitor()
}

async function approveRequest(requestID, response) {
  const entry = pending.get(requestID)
  if (!entry || approving.has(requestID)) return

  approving.add(requestID)

  try {
    if (typeof entry.client?.postSessionIdPermissionsPermissionId !== "function") {
      throw new Error("The installed OpenCode client cannot reply to permissions")
    }

    const result = await entry.client.postSessionIdPermissionsPermissionId({
      path: {
        id: entry.request.sessionID,
        permissionID: entry.request.id,
      },
      body: { response },
      throwOnError: true,
    })

    if (result?.error) throw result.error
    resolveRequest(requestID)
  } catch {
    if (pending.has(requestID) && shownRequest === requestID) {
      showPtyxisPermission(entry, entry.uuid, entry.owner, entry.tmuxSession)
    }
  } finally {
    approving.delete(requestID)
  }
}

async function showNextPermission() {
  if (disposed || shownRequest || !pending.size) return

  const entry = pending.values().next().value
  const requestID = entry.request.id
  shownRequest = requestID

  let uuid = ""
  let owner = ""
  let tmuxSession = ""

  ;[[uuid, owner], tmuxSession] = await Promise.all([
    getTabUUID().catch(() => ["", ""]),
    getTmuxSession().catch(() => ""),
  ])

  if (!pending.has(requestID) || shownRequest !== requestID) {
    if (!shownRequest && !pending.size && uuid) withdrawPtyxisNotification(uuid)
    return
  }

  showPtyxisPermission(entry, uuid, owner, tmuxSession)
}

async function showQuestion(entry) {
  const requestID = entry.request.id
  let uuid = ""
  let owner = ""
  let tmuxSession = ""

  ;[[uuid, owner], tmuxSession] = await Promise.all([
    getTabUUID().catch(() => ["", ""]),
    getTmuxSession().catch(() => ""),
  ])

  if (!pendingQuestions.has(requestID)) return
  showPtyxisQuestion(entry, uuid, owner, tmuxSession)
}

function permissionText(request) {
  const metadata =
    request.metadata && typeof request.metadata === "object" && !Array.isArray(request.metadata)
      ? request.metadata
      : {}

  return (
    ["command", "filepath", "filePath", "description", "url", "query", "pattern"]
      .map((key) => text(metadata[key]))
      .find(Boolean) ||
    text(request.patterns) ||
    "Approval required in the terminal"
  )
}

function questionText(request) {
  if (!Array.isArray(request.questions)) return "Input required in the terminal"
  return (
    request.questions
      .map((question) => text(question?.question) || text(question?.header))
      .find(Boolean) || "Input required in the terminal"
  )
}

function questionSummary(request) {
  const header = Array.isArray(request.questions)
    ? request.questions.map((question) => text(question?.header, 80)).find(Boolean)
    : ""
  return header ? `OpenCode question: ${header}` : "OpenCode question"
}

function handleNotificationAction(action, requestID, activationToken) {
  if (action === APPROVE_ACTION) {
    void approveRequest(requestID, "once")
  } else if (action === APPROVE_ALWAYS_ACTION) {
    void approveRequest(requestID, "always")
  } else if (action === FOCUS_ACTION) {
    void focusPtyxis(requestID, activationToken)
  }
}

export const PermissionNotify = async ({ client } = {}) => {
  disposed = false
  syncPermissionState()

  if (IN_TMUX) {
    spawnQuiet(TMUX, ["set", "-g", "allow-passthrough", "on"])
  }

  return {
    event: async ({ event }) => {
      if (event.type === "session.status") {
        const properties = event.properties ?? event.data
        const sessionID = properties?.sessionID
        const status = properties?.status?.type ?? properties?.status
        if (sessionID && status) {
          sessionStatuses.set(sessionID, status)
          syncPermissionState()
        }
        return
      }

      if (event.type === "session.idle") {
        const properties = event.properties ?? event.data
        if (properties?.sessionID) {
          sessionStatuses.set(properties.sessionID, "idle")
          syncPermissionState()
        }
        return
      }

      if (event.type === "question.replied" || event.type === "question.rejected") {
        const properties = event.properties ?? event.data
        const requestID = properties?.requestID ?? properties?.id
        if (requestID) resolveQuestion(requestID)
        return
      }

      if (event.type === "question.asked") {
        const request = event.properties ?? event.data
        if (request && typeof request === "object" && request.id && request.sessionID) {
          const entry = {
            request,
            summary: questionSummary(request),
            body: questionText(request),
            notificationID: "",
            uuid: "",
            owner: "",
            tmuxSession: "",
          }
          pendingQuestions.set(request.id, entry)
          syncPermissionState()
          await startActionMonitor(handleNotificationAction)
          void showQuestion(entry)
        }
        return
      }

      if (event.type === "permission.replied") {
        const properties = event.properties ?? event.data
        const replyID = properties?.requestID ?? properties?.permissionID ?? shownRequest
        if (replyID) resolveRequest(replyID)
        return
      }

      if (event.type !== "permission.asked") return

      const request = event.properties ?? event.data
      if (!request || typeof request !== "object" || !request.id || !request.sessionID) return

      pending.set(request.id, {
        request,
        summary: `OpenCode permission: ${text(request.permission, 80) || "unknown"}`,
        body: permissionText(request),
        notificationID: "",
        uuid: "",
        owner: "",
        tmuxSession: "",
        client,
      })
      syncPermissionState()

      await startActionMonitor(handleNotificationAction)
      void showNextPermission()
    },
    dispose: async () => {
      disposed = true
      for (const entry of pending.values()) clearTimeout(entry.restoreTimer)
      for (const entry of pendingQuestions.values()) {
        clearTimeout(entry.restoreTimer)
        withdrawPtyxisNotification(entry.notificationID)
      }
      pending.clear()
      pendingQuestions.clear()
      sessionStatuses.clear()
      syncPermissionState()
      approving.clear()
      stopActionMonitor()
      withdrawPtyxisNotification()
      shownRequest = ""
      shownNotification = ""

      if (typeof ttyFD === "number") {
        try {
          closeSync(ttyFD)
        } catch {}
      }
      ttyFD = undefined
    },
  }
}
