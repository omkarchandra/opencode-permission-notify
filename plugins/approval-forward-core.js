import { setTimeout as delay } from "node:timers/promises"

function text(value, limit = 200) {
  if (Array.isArray(value)) value = value.filter((item) => typeof item === "string").join(", ")
  if (typeof value !== "string") return ""
  return value
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, limit)
}

function duration(value, fallback, minimum, maximum, allowZero = false) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return fallback
  if (allowZero && parsed === 0) return 0
  return Math.min(maximum, Math.max(minimum, parsed))
}

function nativeDecision(properties) {
  const reply = properties?.reply ?? properties?.response
  if (reply === "once" || reply === "always" || reply === "allow") return "allow"
  if (reply === "reject" || reply === "deny") return "deny"
  return null
}

export function createApprovalForward(input, rawOptions = {}) {
  const env = rawOptions.env || process.env
  const baseURL = (rawOptions.baseURL || env.FULLCLOCK_BASE_URL || "http://127.0.0.1:8443").replace(/\/+$/, "")
  const token = rawOptions.token ?? env.FULLCLOCK_TOKEN ?? ""
  const localApprovals = rawOptions.localApprovals ?? env.FULLCLOCK_LOCAL_APPROVALS !== "0"
  const ttlMs = duration(rawOptions.ttlMs ?? env.FULLCLOCK_TTL_MS, 150_000, 30_000, 900_000)
  const pollMs = duration(rawOptions.pollMs ?? env.FULLCLOCK_POLL_MS, 2_000, 100, 30_000)
  const connectTimeoutMs = duration(
    rawOptions.connectTimeoutMs ?? env.FULLCLOCK_CONNECT_TIMEOUT_MS,
    5_000,
    250,
    60_000,
    true,
  )
  const fetchImpl = rawOptions.fetch || globalThis.fetch
  const delayImpl = rawOptions.delay || delay
  const now = rawOptions.now || Date.now
  const setTimer = rawOptions.setTimeout || setTimeout
  const clearTimer = rawOptions.clearTimeout || clearTimeout
  const requesterLabel = text(
    rawOptions.requesterLabel ?? env.FULLCLOCK_REQUESTER_LABEL ?? "local-device",
    80,
  ) || "local-device"
  const active = new Map()
  const backendEnabled = Boolean(token) || localApprovals

  const backendFetch = async (path, options = {}) => {
    if (typeof fetchImpl !== "function") return { ok: false, status: 0, body: {} }
    const controller = connectTimeoutMs > 0 ? new AbortController() : null
    const timer = controller ? setTimer(() => controller.abort(), connectTimeoutMs) : null
    try {
      const headers = {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      }
      if (token) headers.Authorization = `Bearer ${token}`
      const response = await fetchImpl(`${baseURL}${path}`, {
        ...options,
        ...(controller ? { signal: controller.signal } : {}),
        headers,
      })
      return {
        ok: response.ok,
        status: response.status,
        body: await response.json().catch(() => ({})),
      }
    } catch {
      return { ok: false, status: 0, body: {} }
    } finally {
      if (timer) clearTimer(timer)
    }
  }

  const postApproval = async (properties) => {
    const postedAt = now()
    const { ok, body } = await backendFetch("/api/approvals", {
      method: "POST",
      body: JSON.stringify({
        requester: { agent: "opencode", host: requesterLabel },
        permission: properties.permission || properties.tool?.callID || "unknown",
        pattern: text(properties.patterns),
        summary: text(properties.metadata?.description || properties.metadata?.command || properties.patterns),
        ttl_seconds: Math.round(ttlMs / 1000),
      }),
    })
    if (!ok) return null
    if (typeof body?.id !== "string" && typeof body?.id !== "number") return null
    const serverExpiry = Number(body.expires_at) * 1_000
    return {
      id: body.id,
      expiresAt: Number.isFinite(serverExpiry) && serverExpiry > postedAt
        ? Math.min(serverExpiry, postedAt + ttlMs)
        : postedAt + ttlMs,
    }
  }

  const resolveApproval = async (approvalID, decision) => {
    if (decision !== "allow" && decision !== "deny") return
    await backendFetch(`/api/approvals/${encodeURIComponent(approvalID)}/respond`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    })
  }

  const answerPermission = async (properties, decision) => {
    const client = input?.client
    if (client?.postSessionIdPermissionsPermissionId) {
      const response = await client.postSessionIdPermissionsPermissionId({
        path: { id: properties.sessionID, permissionID: properties.id },
        body: { response: decision === "allow" ? "once" : "reject" },
        throwOnError: true,
      })
      if (response?.error) throw response.error
      return true
    }
    if (client?.permission?.reply) {
      const response = await client.permission.reply({
        path: { requestID: properties.id },
        query: { directory: input.directory },
        body: { reply: decision === "allow" ? "once" : "reject" },
        throwOnError: true,
      })
      if (response?.error) throw response.error
      return true
    }
    return false
  }

  const forget = (key, entry) => {
    if (active.get(key) !== entry) return
    active.delete(key)
    if (entry.timer) clearTimer(entry.timer)
  }

  const waitForDecision = async (approval, properties, key, entry) => {
    const deadline = approval.expiresAt
    while (active.get(key) === entry && now() < deadline) {
      const { ok, body } = await backendFetch(`/api/approvals/${encodeURIComponent(approval.id)}`)
      if (active.get(key) !== entry) return
      if (ok && body) {
        if (body.status === "answered" && (body.decision === "allow" || body.decision === "deny")) {
          try {
            const delivered = await answerPermission(properties, body.decision)
            forget(key, entry)
            if (!delivered) return
          } catch {
            // Leave the exact native prompt pending and retry until its bounded TTL.
          }
          if (active.get(key) !== entry) return
        }
        if (body.status === "expired" || body.status === "cancelled") {
          forget(key, entry)
          return
        }
      }
      await delayImpl(pollMs)
    }
    forget(key, entry)
  }

  return {
    dispose: async () => {
      for (const [key, entry] of active) forget(key, entry)
    },
    event: async ({ event }) => {
      if (!backendEnabled || !event?.properties) return
      if (event.type === "permission.asked") {
        const properties = event.properties
        if (!properties.id || !properties.sessionID) return
        const key = `${properties.sessionID}:${properties.id}`
        if (active.has(key)) return

        const entry = { timer: null, approval: null, nativeDecision: null }
        active.set(key, entry)
        entry.timer = setTimer(() => forget(key, entry), ttlMs + pollMs + 1_000)
        entry.timer?.unref?.()
        let approval
        try {
          // OpenCode currently invokes event hooks without awaiting each hook.
          // Yield once so an in-process guard can enrich this shared permission
          // event (for example, with Jasmine's privacy warning) before posting.
          await Promise.resolve()
          approval = await postApproval(properties)
        } catch {
          forget(key, entry)
          return
        }
        if (!approval) {
          forget(key, entry)
          return
        }
        entry.approval = approval
        if (entry.nativeDecision) {
          const decision = entry.nativeDecision
          forget(key, entry)
          void resolveApproval(approval.id, decision)
          return
        }
        if (active.get(key) === entry) void waitForDecision(approval, properties, key, entry)
        return
      }

      if (event.type === "permission.replied") {
        const properties = event.properties
        const key = `${properties.sessionID}:${properties.requestID ?? properties.permissionID ?? ""}`
        const entry = active.get(key)
        if (!entry) return
        const decision = nativeDecision(properties)
        if (!decision) {
          forget(key, entry)
          return
        }
        entry.nativeDecision = decision
        if (entry.approval) {
          const approvalID = entry.approval.id
          forget(key, entry)
          void resolveApproval(approvalID, decision)
        }
      }
    },
  }
}
