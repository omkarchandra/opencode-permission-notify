import assert from "node:assert/strict"
import test from "node:test"

import * as approvalForwardModule from "./approval-forward.js"
import { createApprovalForward } from "./approval-forward-core.js"

const { ApprovalForward } = approvalForwardModule

function response(body, ok = true, status = 200) {
  return { ok, status, json: async () => body }
}

function options(fetch) {
  return {
    fetch,
    localApprovals: true,
    ttlMs: 60_000,
    pollMs: 1,
    connectTimeoutMs: 0,
    delay: async () => {},
    setTimeout: () => ({ unref() {} }),
    clearTimeout: () => {},
    requesterLabel: "test-device",
  }
}

async function eventually(assertion) {
  for (let attempt = 0; attempt < 20; attempt++) {
    try {
      assertion()
      return
    } catch (error) {
      if (attempt === 19) throw error
      await new Promise((resolve) => setImmediate(resolve))
    }
  }
}

test("approval forwarding posts the bounded contract and replies to the exact OpenCode request", async () => {
  const requests = []
  const replies = []
  const fetch = async (url, init = {}) => {
    requests.push({ url, init })
    if (init.method === "POST") return response({ id: "approval-42" })
    return response({ status: "answered", decision: "allow" })
  }
  const client = {
    postSessionIdPermissionsPermissionId: async (request) => replies.push(request),
  }
  const hooks = createApprovalForward({ client, directory: "/workspace" }, options(fetch))
  const properties = {
    id: "permission-7",
    sessionID: "session-3",
    permission: "signed_in_tabs_browser_tabs",
    patterns: ["*"],
    metadata: { description: "Jasmine browser privacy warning" },
  }

  await hooks.event({ event: { type: "permission.asked", properties } })
  await eventually(() => assert.equal(replies.length, 1))

  assert.equal(requests[0].url, "http://127.0.0.1:8443/api/approvals")
  assert.equal(requests[1].url, "http://127.0.0.1:8443/api/approvals/approval-42")
  assert.deepEqual(JSON.parse(requests[0].init.body), {
    requester: { agent: "opencode", host: "test-device" },
    permission: "signed_in_tabs_browser_tabs",
    pattern: "*",
    summary: "Jasmine browser privacy warning",
    ttl_seconds: 60,
  })
  assert.deepEqual(replies[0], {
    path: { id: "session-3", permissionID: "permission-7" },
    body: { response: "once" },
    throwOnError: true,
  })
})

test("approval posting yields once for same-event privacy enrichment", async () => {
  const requests = []
  const fetch = async (_url, init = {}) => {
    requests.push(init)
    return init.method === "POST"
      ? response({ id: "approval-warning" })
      : response({ status: "expired" })
  }
  const hooks = createApprovalForward({ client: {}, directory: "/workspace" }, options(fetch))
  const properties = {
    id: "permission-warning",
    sessionID: "session-warning",
    permission: "signed_in_tabs_browser_tabs",
    patterns: ["*"],
    metadata: {},
  }

  const forwarding = hooks.event({ event: { type: "permission.asked", properties } })
  properties.metadata.description = "Jasmine privacy warning"
  await forwarding

  assert.equal(JSON.parse(requests[0].body).summary, "Jasmine privacy warning")
})

test("modern SDK replies preserve the exact request ID and deny decision", async () => {
  const replies = []
  const fetch = async (_url, init = {}) => (
    init.method === "POST"
      ? response({ id: "approval-modern" })
      : response({ status: "answered", decision: "deny" })
  )
  const hooks = createApprovalForward(
    {
      client: { permission: { reply: async (request) => replies.push(request) } },
      directory: "/workspace",
    },
    options(fetch),
  )

  await hooks.event({
    event: {
      type: "permission.asked",
      properties: { id: "request-modern", sessionID: "session-modern", permission: "bash" },
    },
  })
  await eventually(() => assert.equal(replies.length, 1))
  assert.deepEqual(replies[0], {
    path: { requestID: "request-modern" },
    query: { directory: "/workspace" },
    body: { reply: "reject" },
    throwOnError: true,
  })
})

test("legacy SDK response errors keep polling instead of dropping the native prompt", async () => {
  let replies = 0
  const fetch = async (_url, init = {}) => (
    init.method === "POST"
      ? response({ id: "approval-retry" })
      : response({ status: "answered", decision: "allow" })
  )
  const hooks = createApprovalForward(
    {
      client: {
        postSessionIdPermissionsPermissionId: async () => {
          replies++
          return replies === 1 ? { error: new Error("not delivered") } : { data: true }
        },
      },
      directory: "/workspace",
    },
    options(fetch),
  )

  await hooks.event({
    event: {
      type: "permission.asked",
      properties: { id: "permission-retry", sessionID: "session-retry", permission: "bash" },
    },
  })
  await eventually(() => assert.equal(replies, 2))
})

test("duplicate permission events share one pending backend request", async () => {
  let releasePost
  let posts = 0
  const post = new Promise((resolve) => {
    releasePost = resolve
  })
  const fetch = async (_url, init = {}) => {
    if (init.method === "POST") {
      posts++
      return post
    }
    return response({ status: "expired" })
  }
  const hooks = createApprovalForward({ client: {}, directory: "/workspace" }, options(fetch))
  const event = {
    type: "permission.asked",
    properties: { id: "same", sessionID: "session", permission: "bash" },
  }

  const first = hooks.event({ event })
  await new Promise((resolve) => setImmediate(resolve))
  await hooks.event({ event })
  assert.equal(posts, 1)
  releasePost(response({ id: "approval" }))
  await first
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(posts, 1)
})

test("a native reply cancels an in-flight remote decision without a duplicate answer", async () => {
  let releaseDecision
  const pendingDecision = new Promise((resolve) => {
    releaseDecision = resolve
  })
  const requests = []
  const fetch = async (url, init = {}) => {
    requests.push({ url, init })
    if (url.endsWith("/respond")) return response({ ok: true })
    return init.method === "POST" ? response({ id: "approval" }) : pendingDecision
  }
  const replies = []
  const hooks = createApprovalForward(
    {
      client: { postSessionIdPermissionsPermissionId: async (request) => replies.push(request) },
      directory: "/workspace",
    },
    options(fetch),
  )

  await hooks.event({
    event: {
      type: "permission.asked",
      properties: { id: "permission", sessionID: "session", permission: "bash" },
    },
  })
  await hooks.event({
    event: {
      type: "permission.replied",
      properties: { requestID: "permission", sessionID: "session", reply: "reject" },
    },
  })
  await eventually(() => assert.equal(requests.filter(({ url }) => url.endsWith("/respond")).length, 1))
  releaseDecision(response({ status: "answered", decision: "deny" }))
  await new Promise((resolve) => setImmediate(resolve))
  assert.deepEqual(replies, [])
  assert.deepEqual(JSON.parse(requests.find(({ url }) => url.endsWith("/respond")).init.body), {
    decision: "deny",
  })
})

test("a native reply during approval creation closes the one backend request", async () => {
  let releasePost
  const pendingPost = new Promise((resolve) => {
    releasePost = resolve
  })
  const requests = []
  const fetch = async (url, init = {}) => {
    requests.push({ url, init })
    if (url.endsWith("/api/approvals")) return pendingPost
    if (url.endsWith("/respond")) return response({ ok: true })
    return response({ status: "pending" })
  }
  const hooks = createApprovalForward({ client: {}, directory: "/workspace" }, options(fetch))
  const properties = { id: "permission-race", sessionID: "session-race", permission: "bash" }

  const asking = hooks.event({ event: { type: "permission.asked", properties } })
  await new Promise((resolve) => setImmediate(resolve))
  await hooks.event({
    event: {
      type: "permission.replied",
      properties: { requestID: properties.id, sessionID: properties.sessionID, reply: "once" },
    },
  })
  await hooks.event({ event: { type: "permission.asked", properties } })
  releasePost(response({ id: "approval-race" }))
  await asking
  await eventually(() => assert.equal(requests.filter(({ url }) => url.endsWith("/respond")).length, 1))

  assert.equal(requests.filter(({ url }) => url.endsWith("/api/approvals")).length, 1)
  assert.equal(requests.filter(({ url }) => url.endsWith("/api/approvals/approval-race")).length, 0)
  assert.deepEqual(JSON.parse(requests.find(({ url }) => url.endsWith("/respond")).init.body), {
    decision: "allow",
  })
})

test("backend failure and unsupported clients leave the native permission pending", async () => {
  const failed = createApprovalForward(
    { client: {}, directory: "/workspace" },
    options(async () => response({}, false, 503)),
  )
  await failed.event({
    event: {
      type: "permission.asked",
      properties: { id: "permission", sessionID: "session", permission: "bash" },
    },
  })

  let polls = 0
  const unsupported = createApprovalForward(
    { client: {}, directory: "/workspace" },
    {
      ...options(async (_url, init = {}) => {
        if (init.method === "POST") return response({ id: "approval" })
        polls++
        return response({ status: "expired" })
      }),
    },
  )
  await unsupported.event({
    event: {
      type: "permission.asked",
      properties: { id: "permission-2", sessionID: "session", permission: "bash" },
    },
  })
  await eventually(() => assert.equal(polls, 1))
})

test("the production export activates only once even if configured and auto-discovered", () => {
  const input = { client: {}, directory: "/workspace" }
  assert.deepEqual(Object.keys(approvalForwardModule), ["ApprovalForward"])
  assert.equal(typeof ApprovalForward(input).event, "function")
  assert.deepEqual(ApprovalForward(input), {})
  assert.equal(typeof ApprovalForward({ ...input, directory: "/other-workspace" }).event, "function")
})
