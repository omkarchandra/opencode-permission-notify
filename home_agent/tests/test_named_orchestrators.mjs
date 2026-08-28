import assert from "node:assert/strict"
import { homedir } from "node:os"
import { resolve } from "node:path"
import test from "node:test"

import {
  CATALOG_PATH,
  HOME_ORCHESTRATORS,
  HomeAgentGuard,
  selectVoiceOrchestrator,
} from "../plugin/home-agent-guard-core.js"

const DIRECTORY = "/work/home-agent"
const OPTIONS = {
  catalogPath: new URL("./fixtures/voice-catalog.md", import.meta.url),
  vaultRoot: "/work/vault",
  sandboxPath: new URL("../plugin/home-agent-guard.js", import.meta.url).pathname,
}

function applyPrompt(session, body) {
  session.agent = body.agent
  session.model = { providerID: body.model.providerID, id: body.model.modelID }
  session.permission = Object.entries(body.tools || {}).map(([permission, enabled]) => ({
    permission,
    pattern: "*",
    action: enabled ? "allow" : "deny",
  }))
}

function voiceHarness(ingress = "voice-home-agent") {
  const session = { id: `ses_${ingress}`, agent: ingress, directory: DIRECTORY }
  const updates = []
  const prompts = []
  const client = {
    session: {
      get: async () => ({ data: session }),
      update: async ({ body }) => {
        updates.push(body)
        Object.assign(session, body)
        return { data: session }
      },
      prompt: async ({ body }) => {
        prompts.push(body)
        applyPrompt(session, body)
        return { data: { info: { id: body.messageID }, parts: [] } }
      },
    },
  }
  return { session, client, updates, prompts }
}

async function hooksForSessions(sessions) {
  const client = {
    session: {
      get: async ({ path }) => ({ data: sessions[path.id] }),
    },
  }
  return HomeAgentGuard({ client, directory: DIRECTORY }, OPTIONS)
}

test("canonical catalog authority uses the portable config directory", () => {
  assert.equal(
    CATALOG_PATH,
    resolve(homedir(), ".config/home-agent/projects.md"),
  )
})

test("voice addressing selects named orchestrators deterministically", () => {
  assert.equal(selectVoiceOrchestrator([{ type: "text", text: "Check project status" }]).id, "jarvis")
  assert.equal(selectVoiceOrchestrator([{ type: "text", text: "Jarvis, check status" }]).id, "jarvis")
  assert.equal(selectVoiceOrchestrator([{ type: "text", text: "Hey Jasmine, research this" }]).id, "jasmine")
  assert.equal(selectVoiceOrchestrator([{ type: "text", text: "switch to Jasmine for this" }]).id, "jasmine")
  assert.equal(selectVoiceOrchestrator([{ type: "text", text: "Research jasmine flowers" }]).id, "jarvis")
})

test("default voice promotion keeps one visible root and selects Jarvis model and identity", async () => {
  const harness = voiceHarness("voice-general")
  const hooks = await HomeAgentGuard({ client: harness.client, directory: DIRECTORY }, OPTIONS)
  const message = { id: "msg_default", agent: "voice-general" }

  await hooks["chat.message"](
    { sessionID: harness.session.id, agent: "voice-general", messageID: message.id },
    { message, parts: [{ type: "text", text: "Check project status" }] },
  )

  assert.equal(message.agent, "jarvis")
  assert.deepEqual(message.model, HOME_ORCHESTRATORS.jarvis.model)
  assert.equal(harness.session.id, "ses_voice-general")
  assert.equal(harness.session.agent, "jarvis")
  assert.equal(harness.session.title, "Jarvis Voice")
  assert.deepEqual(harness.session.model, { providerID: "openai", id: "gpt-5.6-sol" })
  assert.equal(harness.session.metadata.homeAgent.agent, "jarvis")
  assert.equal(harness.session.metadata.homeAgent.displayName, "Jarvis")
  assert.equal(harness.updates.length, 1)
  assert.equal(harness.prompts.length, 1)
})

test("explicit Jasmine promotion sends only local text to the free text-output model", async () => {
  const harness = voiceHarness()
  const hooks = await HomeAgentGuard({ client: harness.client, directory: DIRECTORY }, OPTIONS)
  const message = { id: "msg_jasmine", agent: "voice-home-agent" }

  await hooks["chat.message"](
    { sessionID: harness.session.id, agent: "voice-home-agent", messageID: message.id },
    { message, parts: [{ type: "text", text: "Jasmine, summarize the public docs" }] },
  )

  assert.equal(message.agent, "jasmine")
  assert.deepEqual(message.model, HOME_ORCHESTRATORS.jasmine.model)
  assert.equal(harness.session.agent, "jasmine")
  assert.equal(harness.session.title, "Jasmine Voice")
  assert.deepEqual(harness.session.model, {
    providerID: "openrouter",
    id: "thinkingmachines/inkling:free",
  })
  assert.deepEqual(harness.session.metadata.homeAgent, {
    kind: "orchestrator",
    role: "voice-orchestration",
    ingress: "voice",
    ingressAgent: "voice-home-agent",
    voiceMessageID: message.id,
    agent: "jasmine",
    displayName: "Jasmine",
    model: "openrouter/thinkingmachines/inkling:free",
  })
})

test("Jasmine rejects every non-text voice part before loading or changing a session", async () => {
  const harness = voiceHarness()
  let gets = 0
  harness.client.session.get = async () => {
    gets++
    return { data: harness.session }
  }
  const hooks = await HomeAgentGuard({ client: harness.client, directory: DIRECTORY }, OPTIONS)
  const message = { id: "msg_private", agent: "voice-home-agent" }

  await assert.rejects(
    hooks["chat.message"](
      { sessionID: harness.session.id, agent: "voice-home-agent", messageID: message.id },
      {
        message,
        parts: [
          { type: "text", text: "Jasmine, inspect this" },
          { type: "file", mime: "audio/wav", url: "data:audio/wav;base64,private" },
        ],
      },
    ),
    /text transcripts only/,
  )
  assert.equal(gets, 0)
  assert.equal(harness.updates.length, 0)
  assert.equal(harness.prompts.length, 0)
  assert.equal(message.agent, "voice-home-agent")
})

test("direct Jasmine sessions also reject attachments before model execution", async () => {
  const sessions = {
    jasmine: { id: "jasmine", agent: "jasmine", directory: DIRECTORY },
  }
  const hooks = await hooksForSessions(sessions)
  await assert.rejects(
    hooks["chat.message"](
      { sessionID: "jasmine", agent: "jasmine", messageID: "msg_direct" },
      {
        message: { id: "msg_direct", agent: "jasmine" },
        parts: [{ type: "file", mime: "image/png", url: "data:image/png;base64,private" }],
      },
    ),
    /text transcripts only/,
  )
})

test("browser access is exclusive to named orchestrators and web URLs", async () => {
  const sessions = {
    jarvis: { id: "jarvis", agent: "jarvis", directory: DIRECTORY },
    home: { id: "home", agent: "home_agent", directory: DIRECTORY },
    other: { id: "other", agent: "build", directory: DIRECTORY },
    child: { id: "child", parentID: "jarvis", agent: "build", directory: DIRECTORY },
  }
  const hooks = await hooksForSessions(sessions)
  const before = hooks["tool.execute.before"]

  await before(
    { tool: "signed_in_tabs_browser_navigate", sessionID: "jarvis" },
    { args: { url: "https://example.com/research" } },
  )
  await before(
    { tool: "signed_in_tabs_browser_tabs", sessionID: "jarvis", callID: "call_new" },
    { args: { action: "new", url: "https://chatgpt.com/" } },
  )
  await hooks["tool.execute.after"]({
    tool: "signed_in_tabs_browser_tabs",
    sessionID: "jarvis",
    callID: "call_new",
  })
  await before(
    { tool: "signed_in_tabs_browser_tabs", sessionID: "jarvis", callID: "call_close" },
    { args: { action: "close" } },
  )
  await hooks["tool.execute.after"]({
    tool: "signed_in_tabs_browser_tabs",
    sessionID: "jarvis",
    callID: "call_close",
  })

  await assert.rejects(
    before(
      { tool: "signed_in_tabs_browser_navigate", sessionID: "jarvis" },
      { args: { url: "file:///work/private.txt" } },
    ),
    /HTTP and HTTPS/,
  )
  await assert.rejects(
    before(
      { tool: "signed_in_tabs_browser_snapshot", sessionID: "jarvis" },
      { args: { filename: "snapshot.md" } },
    ),
    /forbids writing/,
  )
  await assert.rejects(
    before(
      { tool: "signed_in_tabs_browser_evaluate", sessionID: "jarvis" },
      { args: { function: "() => document.cookie" } },
    ),
    /forbids browser tool/,
  )
  await assert.rejects(
    before(
      { tool: "signed_in_tabs_browser_snapshot", sessionID: "home" },
      { args: {} },
    ),
    /forbids browser tool/,
  )
  await assert.rejects(
    before(
      { tool: "signed_in_tabs_browser_snapshot", sessionID: "other" },
      { args: {} },
    ),
    /forbids browser tool/,
  )
  await assert.rejects(
    before(
      { tool: "signed_in_tabs_browser_snapshot", sessionID: "child" },
      { args: {} },
    ),
    /forbids browser tool/,
  )
})

test("tab closing is limited to the current tab created by that agent session", async () => {
  const hooks = await hooksForSessions({
    jarvis: { id: "jarvis", agent: "jarvis", directory: DIRECTORY },
  })
  const before = hooks["tool.execute.before"]
  let call = 0
  const tabs = async (args) => {
    const callID = `call_tabs_${call++}`
    await before(
      { tool: "signed_in_tabs_browser_tabs", sessionID: "jarvis", callID },
      { args },
    )
    await hooks["tool.execute.after"]({
      tool: "signed_in_tabs_browser_tabs",
      sessionID: "jarvis",
      callID,
    })
  }

  await assert.rejects(tabs({ action: "close" }), /only the current tab/)
  await before(
    { tool: "signed_in_tabs_browser_tabs", sessionID: "jarvis", callID: "call_failed_new" },
    { args: { action: "new" } },
  )
  await hooks["tool.execute.after"](
    {
      tool: "signed_in_tabs_browser_tabs",
      sessionID: "jarvis",
      callID: "call_failed_new",
    },
    { isError: true },
  )
  await assert.rejects(tabs({ action: "close" }), /only the current tab/)
  await tabs({ action: "new" })
  await assert.rejects(tabs({ action: "close", index: 0 }), /only the current tab/)
  await tabs({ action: "close" })
  await tabs({ action: "new", url: "https://example.com" })
  await tabs({ action: "select", index: 0 })
  await assert.rejects(tabs({ action: "close" }), /only the current tab/)
})

test("Jasmine requires a successful warned first tab approval and then permits normal browsing", async () => {
  const hooks = await hooksForSessions({
    jasmine: { id: "jasmine", agent: "jasmine", directory: DIRECTORY },
  })
  const before = hooks["tool.execute.before"]

  await assert.rejects(
    before(
      { tool: "signed_in_tabs_browser_snapshot", sessionID: "jasmine" },
      { args: {} },
    ),
    /privacy approval/,
  )
  await before(
    { tool: "signed_in_tabs_browser_tabs", sessionID: "jasmine", callID: "call_failed_consent" },
    { args: { action: "list" } },
  )
  await hooks["tool.execute.after"](
    {
      tool: "signed_in_tabs_browser_tabs",
      sessionID: "jasmine",
      callID: "call_failed_consent",
    },
    { isError: true },
  )
  await assert.rejects(
    before(
      { tool: "signed_in_tabs_browser_snapshot", sessionID: "jasmine" },
      { args: {} },
    ),
    /privacy approval/,
  )
  const permission = {
    id: "permission_consent",
    sessionID: "jasmine",
    permission: "signed_in_tabs_browser_tabs",
    metadata: { description: "Manage tabs" },
  }
  await hooks.event({ event: { type: "permission.asked", properties: permission } })
  assert.match(permission.metadata.warning, /free third-party endpoint.*may log/i)
  const decision = { status: "ask" }
  await hooks["permission.ask"](permission, decision)
  assert.equal(decision.status, "ask")
  assert.match(permission.metadata.warning, /free third-party endpoint.*may log/i)

  await before(
    { tool: "signed_in_tabs_browser_tabs", sessionID: "jasmine", callID: "call_consent" },
    { args: { action: "list" } },
  )
  await hooks["tool.execute.after"]({
    tool: "signed_in_tabs_browser_tabs",
    sessionID: "jasmine",
    callID: "call_consent",
  })
  await before(
    { tool: "signed_in_tabs_browser_navigate", sessionID: "jasmine" },
    { args: { url: "https://example.com" } },
  )
})

test("named orchestrators hard-deny literal sudo before sandbox execution", async () => {
  const hooks = await hooksForSessions({
    jarvis: { id: "jarvis", agent: "jarvis", directory: DIRECTORY },
  })
  for (const command of ["sudo true", "/usr/bin/sudo -n true", "env sudo true", "bash -c 'sudo true'"]) {
    await assert.rejects(
      hooks["tool.execute.before"](
        { tool: "bash", sessionID: "jarvis", callID: `call_${command.length}` },
        { args: { command } },
      ),
      /forbids sudo/,
    )
  }
})
