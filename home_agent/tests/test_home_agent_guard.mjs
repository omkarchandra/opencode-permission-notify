import assert from "node:assert/strict"
import { mkdtempSync, mkdirSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import test from "node:test"

import {
  HOME_ORCHESTRATORS,
  HomeAgentGuard,
  VOICE_INGRESS_AGENTS,
  containsDeleteDirective,
  isTrustedControllerCommand,
  parseCatalog,
  pathAllowed,
} from "../plugin/home-agent-guard-core.js"

import * as productionGuardModule from "../plugin/home-agent-guard.js"

const PHONE_VOICE_PERMISSION_DEFAULTS = [
  { permission: "question", pattern: "*", action: "deny" },
  { permission: "plan_enter", pattern: "*", action: "deny" },
  { permission: "plan_exit", pattern: "*", action: "deny" },
]
const NORMALIZED_VOICE_PERMISSION = [
  { permission: "doom_loop", pattern: "*", action: "deny" },
]

test("the auto-discovered guard exposes exactly one plugin function", () => {
  assert.deepEqual(Object.keys(productionGuardModule), ["default"])
  assert.equal(typeof productionGuardModule.default, "function")
})

function applyNoReplyPrompt(session, body, { agent = true, permission = true } = {}) {
  if (agent) session.agent = body.agent
  if (body.model) session.model = { providerID: body.model.providerID, id: body.model.modelID }
  if (permission) {
    const rules = Object.entries(body.tools ?? {}).map(([name, enabled]) => ({
      permission: name,
      pattern: "*",
      action: enabled ? "allow" : "deny",
    }))
    if (rules.length) session.permission = rules
  }
  return { data: { info: { id: body.messageID }, parts: [] } }
}

test("catalog parsing returns code roots and absolute durable notes", () => {
  const source = `
| Project | Host | Code | Vault note |
| --- | --- | --- | --- |
| Demo | laptop | \`/work/demo\` | Projects/demo/main.md |
`
  assert.deepEqual(parseCatalog(source, "/vault"), {
    roots: ["/work/demo"],
    files: ["/vault/Projects/demo/main.md"],
  })
})

test("config hook grants only paths advertised by the catalog", async () => {
  const hooks = await HomeAgentGuard(
    { client: {}, directory: "/work/home-agent" },
    {
      catalogPath: new URL("./fixtures/voice-catalog.md", import.meta.url),
      vaultRoot: "/work/vault",
      routesPath: "/work/vault/session-routes.json",
      sandboxPath: "/bin/true",
    },
  )
  const config = {
    agent: {
      home_agent: { permission: { read: "allow" } },
      jarvis: { permission: {} },
      build: { permission: {} },
    },
  }

  hooks.config(config)

  const external = config.agent.home_agent.permission.external_directory
  assert.equal(external["*"], "deny")
  assert.equal(external["/work/home-agent/**"], "allow")
  assert.equal(external["/work/project-beta/**"], "allow")
  assert.equal(external["/work/vault/session-routes.json"], "allow")
  assert.deepEqual(config.agent.jarvis.permission.external_directory, external)
  assert.equal(config.agent.build.permission.external_directory, undefined)
})

test("delete directives and controller command chaining are detected", () => {
  assert.equal(containsDeleteDirective("*** Begin Patch\n*** Delete File: old.txt\n*** End Patch"), true)
  assert.equal(containsDeleteDirective("*** Update File: old.txt\n*** Move to: new.txt"), true)
  assert.equal(containsDeleteDirective("*** Update File: old.txt\n-old\n+new"), false)

  assert.equal(isTrustedControllerCommand("home-agentctl status --json"), true)
  assert.equal(isTrustedControllerCommand("home-agentctl request --task 'review; report'"), true)
  assert.equal(isTrustedControllerCommand("home-agentctl status && rm file"), false)
  assert.equal(isTrustedControllerCommand('home-agentctl request --task "$(rm file)"'), false)
  assert.equal(isTrustedControllerCommand("home-agentctl-fake status"), false)
})

test("path checks resolve symlinks before applying a project boundary", () => {
  const root = mkdtempSync(join(tmpdir(), "home-agent-guard-"))
  try {
    const project = join(root, "project")
    const outside = join(root, "outside")
    mkdirSync(project)
    mkdirSync(outside)
    writeFileSync(join(outside, "secret.txt"), "secret")
    symlinkSync(join(outside, "secret.txt"), join(project, "link.txt"))

    assert.equal(pathAllowed(join(project, "new.txt"), [project]), true)
    assert.equal(pathAllowed(join(project, "link.txt"), [project]), false)
    assert.equal(pathAllowed(join(outside, "secret.txt"), [project]), false)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("managed worker hooks enforce paths, tools, patches, and Bash sandboxing", async () => {
  const root = mkdtempSync(join(tmpdir(), "home-agent-plugin-"))
  try {
    const project = join(root, "project")
    const vault = join(root, "vault")
    const outside = join(root, "outside")
    mkdirSync(project)
    mkdirSync(vault)
    mkdirSync(outside)
    const note = join(vault, "main.md")
    writeFileSync(note, "note")
    const session = {
      id: "ses_worker",
      agent: "build",
      directory: project,
      metadata: {
        homeAgent: {
          kind: "project-worker",
          projectPath: project,
          notePath: note,
        },
      },
    }
    const client = { session: { get: async () => ({ data: session }) } }
    const hooks = await HomeAgentGuard(
      { client, directory: project },
      { catalogPath: join(root, "unused.md"), vaultRoot: vault, sandboxPath: "/bin/true" },
    )
    const before = hooks["tool.execute.before"]

    await before({ tool: "read", sessionID: session.id }, { args: { filePath: join(project, "README.md") } })
    await assert.rejects(
      before({ tool: "read", sessionID: session.id }, { args: { filePath: join(outside, "secret.txt") } }),
      /outside the selected project/,
    )
    await before(
      { tool: "apply_patch", sessionID: session.id },
      { args: { patchText: "*** Begin Patch\n*** Add File: output.txt\n+ok\n*** End Patch" } },
    )
    await assert.rejects(
      before(
        { tool: "apply_patch", sessionID: session.id },
        { args: { patchText: "*** Begin Patch\n*** Delete File: output.txt\n*** End Patch" } },
      ),
      /forbids deleting/,
    )
    await before({ tool: "task", sessionID: session.id }, { args: {} })
    await assert.rejects(
      before({ tool: "custom_delete", sessionID: session.id }, { args: {} }),
      /forbids tool/,
    )

    const shell = { args: { command: "printf '%s' ok > output.txt" } }
    await before({ tool: "bash", sessionID: session.id, callID: "call_guarded" }, shell)
    assert.match(shell.args.command, /^'\/bin\/true' '--write-root'/)
    assert.match(shell.args.command, /\/bin\/bash -c/)
    const environment = { env: { KEEP: "yes" } }
    await hooks["shell.env"](
      { sessionID: session.id, callID: "call_guarded" },
      environment,
    )
    assert.equal(environment.env.OPENCODE_SERVER_PASSWORD, "")
    await assert.rejects(
      hooks["shell.env"](
        { sessionID: session.id, callID: "call_direct" },
        { env: {} },
      ),
      /outside the guarded Bash tool/,
    )
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("Task children inherit the managed parent policy and remain actionable", async () => {
  const root = mkdtempSync(join(tmpdir(), "home-agent-child-"))
  try {
    const project = join(root, "project")
    const outside = join(root, "outside")
    mkdirSync(project)
    mkdirSync(outside)
    const sessions = {
      ses_parent: {
        id: "ses_parent",
        agent: "build",
        directory: project,
        metadata: {
          homeAgent: {
            kind: "project-worker",
            projectPath: project,
          },
        },
      },
      ses_child: {
        id: "ses_child",
        parentID: "ses_parent",
        agent: "general",
        directory: project,
      },
      ses_sibling: {
        id: "ses_sibling",
        parentID: "ses_parent",
        agent: "general",
        directory: project,
      },
      ses_named_home: {
        id: "ses_named_home",
        parentID: "ses_parent",
        agent: "home_agent",
        directory: project,
      },
      ses_unmanaged: {
        id: "ses_unmanaged",
        agent: "general",
        directory: project,
      },
      ses_other_project: {
        id: "ses_other_project",
        agent: "build",
        directory: outside,
        metadata: {
          homeAgent: {
            kind: "project-worker",
            projectPath: outside,
          },
        },
      },
      ses_same_project: {
        id: "ses_same_project",
        agent: "build",
        directory: project,
        metadata: {
          homeAgent: {
            kind: "project-worker",
            projectPath: project,
          },
        },
      },
    }
    const client = {
      session: {
        get: async ({ path }) => ({ data: sessions[path.id] }),
        update: async ({ path, body }) => {
          sessions[path.id] = { ...sessions[path.id], ...body }
          return { data: sessions[path.id] }
        },
      },
    }
    const hooks = await HomeAgentGuard(
      { client, directory: project },
      { sandboxPath: "/bin/true" },
    )
    const before = hooks["tool.execute.before"]

    await before(
      { tool: "edit", sessionID: "ses_child" },
      { args: { filePath: join(project, "result.txt") } },
    )
    await before(
      { tool: "task", sessionID: "ses_parent" },
      { args: { task_id: "ses_child" } },
    )
    await assert.rejects(
      before(
        { tool: "task", sessionID: "ses_parent" },
        { args: { task_id: "ses_unmanaged" } },
      ),
      /unmanaged, unrelated, or broader Task session/,
    )
    await assert.rejects(
      before(
        { tool: "task", sessionID: "ses_parent" },
        { args: { task_id: "ses_other_project" } },
      ),
      /unmanaged, unrelated, or broader Task session/,
    )
    await assert.rejects(
      before(
        { tool: "task", sessionID: "ses_parent" },
        { args: { task_id: "ses_parent" } },
      ),
      /unmanaged, unrelated, or broader Task session/,
    )
    await assert.rejects(
      before(
        { tool: "task", sessionID: "ses_child" },
        { args: { task_id: "ses_sibling" } },
      ),
      /unmanaged, unrelated, or broader Task session/,
    )
    await assert.rejects(
      before(
        { tool: "task", sessionID: "ses_parent" },
        { args: { task_id: "ses_same_project" } },
      ),
      /unmanaged, unrelated, or broader Task session/,
    )
    await assert.rejects(
      before(
        { tool: "write", sessionID: "ses_child" },
        { args: { filePath: join(outside, "result.txt") } },
      ),
      /outside the selected project/,
    )
    const shell = { args: { command: "touch result.txt" } }
    await before(
      { tool: "bash", sessionID: "ses_child", callID: "call_child" },
      shell,
    )
    assert.match(shell.args.command, /--write-root.*project/)

    const namedHomeShell = { args: { command: "home-agentctl status --json" } }
    await before(
      { tool: "bash", sessionID: "ses_named_home", callID: "call_named_home" },
      namedHomeShell,
    )
    assert.notEqual(namedHomeShell.args.command, "home-agentctl status --json")
    await assert.rejects(
      before(
        { tool: "write", sessionID: "ses_named_home" },
        { args: { filePath: join(outside, "escaped.txt") } },
      ),
      /outside the selected project/,
    )
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("direct Home Agent Task children receive one explicit catalog project scope", async () => {
  const root = mkdtempSync(join(tmpdir(), "home-agent-direct-task-"))
  try {
    const projectA = join(root, "project-a")
    const projectB = join(root, "project-b")
    const vault = join(root, "vault")
    mkdirSync(projectA)
    mkdirSync(projectB)
    mkdirSync(vault)
    const noteA = join(vault, "a.md")
    const noteB = join(vault, "b.md")
    writeFileSync(noteA, "a")
    writeFileSync(noteB, "b")
    const catalog = join(root, "projects.md")
    writeFileSync(catalog, `
| Project | Host | Code | Vault note |
| --- | --- | --- | --- |
| Project A | laptop | \`${projectA}\` | a.md |
| Project B | laptop | \`${projectB}\` | b.md |
`)
    const sessions = {
      ses_home: {
        id: "ses_home",
        agent: "home_agent",
        directory: root,
      },
      ses_direct_child: {
        id: "ses_direct_child",
        parentID: "ses_home",
        agent: "general",
        directory: root,
      },
    }
    const client = {
      session: {
        get: async ({ path }) => ({ data: sessions[path.id] }),
        update: async ({ path, body }) => {
          sessions[path.id] = { ...sessions[path.id], ...body }
          return { data: sessions[path.id] }
        },
      },
    }
    const hooks = await HomeAgentGuard(
      { client, directory: root },
      { catalogPath: catalog, vaultRoot: vault, sandboxPath: "/bin/true" },
    )
    const before = hooks["tool.execute.before"]
    const task = {
      args: {
        prompt: `Implement the approved change in ${projectA}.`,
        subagent_type: "general",
      },
    }
    await before(
      { tool: "task", sessionID: "ses_home", callID: "call_task" },
      task,
    )
    assert.match(task.args.prompt, /^<home_agent_scope token="[0-9a-f-]{36}" \/>/)

    const childMessage = { parts: [{ type: "text", text: task.args.prompt }] }
    await hooks["chat.message"](
      { sessionID: "ses_direct_child", agent: "general" },
      childMessage,
    )
    assert.doesNotMatch(childMessage.parts[0].text, /home_agent_scope/)
    assert.deepEqual(sessions.ses_direct_child.metadata.homeAgent, {
      kind: "project-worker",
      projectPath: projectA,
      notePath: noteA,
      delegatedBy: "task",
    })
    await before(
      { tool: "write", sessionID: "ses_direct_child" },
      { args: { filePath: join(projectA, "result.txt") } },
    )
    await assert.rejects(
      before(
        { tool: "write", sessionID: "ses_direct_child" },
        { args: { filePath: join(projectB, "escaped.txt") } },
      ),
      /outside the selected project/,
    )
    await assert.rejects(
      before(
        { tool: "task", sessionID: "ses_home", callID: "call_unscoped" },
        { args: { prompt: "Do some work", subagent_type: "general" } },
      ),
      /must include the selected catalog project path/,
    )

    const restartedHooks = await HomeAgentGuard(
      { client, directory: root },
      { catalogPath: catalog, vaultRoot: vault, sandboxPath: "/bin/true" },
    )
    await restartedHooks["tool.execute.before"](
      { tool: "write", sessionID: "ses_direct_child" },
      { args: { filePath: join(projectA, "after-restart.txt") } },
    )
    await assert.rejects(
      restartedHooks["tool.execute.before"](
        { tool: "write", sessionID: "ses_direct_child" },
        { args: { filePath: join(projectB, "after-restart.txt") } },
      ),
      /outside the selected project/,
    )
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("direct Task scope persistence fails closed on an API update error", async () => {
  const root = mkdtempSync(join(tmpdir(), "home-agent-task-fail-"))
  try {
    const project = join(root, "project")
    const vault = join(root, "vault")
    mkdirSync(project)
    mkdirSync(vault)
    const catalog = join(root, "projects.md")
    writeFileSync(catalog, `
| Project | Host | Code | Vault note |
| --- | --- | --- | --- |
| Project | laptop | \`${project}\` | main.md |
`)
    const sessions = {
      parent: { id: "parent", agent: "home_agent", directory: root },
      child: { id: "child", parentID: "parent", agent: "general", directory: root },
    }
    const client = {
      session: {
        get: async ({ path }) => ({ data: sessions[path.id] }),
        update: async () => ({ error: { status: 400 } }),
      },
    }
    const hooks = await HomeAgentGuard(
      { client, directory: root },
      { catalogPath: catalog, vaultRoot: vault, sandboxPath: "/bin/true" },
    )
    const task = {
      args: {
        prompt: `Work only in ${project}.`,
        subagent_type: "general",
      },
    }
    await hooks["tool.execute.before"](
      { tool: "task", sessionID: "parent", callID: "call_task_fail" },
      task,
    )
    await assert.rejects(
      hooks["chat.message"](
        { sessionID: "child", agent: "general" },
        { parts: [{ type: "text", text: task.args.prompt }] },
      ),
      /could not persist the delegated Task scope/,
    )
    assert.equal(sessions.child.metadata, undefined)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("every tracked voice route is covered by the promotion guard", () => {
  const routed = [
    ...readdirSync(new URL("../../agent/", import.meta.url)),
    ...readdirSync(new URL("../agent/", import.meta.url)),
  ]
    .filter((name) => /^voice-.*\.md$/.test(name))
    .map((name) => name.replace(/\.md$/, ""))
    .sort()

  assert.deepEqual([...VOICE_INGRESS_AGENTS].sort(), routed)
})

test("every voice ingress becomes the same managed Jarvis session", async () => {
  for (const ingressAgent of VOICE_INGRESS_AGENTS) {
    const session = {
      id: `ses_${ingressAgent}`,
      agent: ingressAgent,
      directory: "/work/home-agent",
      metadata: { source: "voice-test" },
    }
    if (ingressAgent === VOICE_INGRESS_AGENTS[0]) session.permission = []
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
          return applyNoReplyPrompt(session, body)
        },
      },
    }
    const hooks = await HomeAgentGuard(
      { client, directory: session.directory },
      {
        catalogPath: new URL("./fixtures/voice-catalog.md", import.meta.url),
        vaultRoot: "/work/vault",
        sandboxPath: new URL("../plugin/home-agent-guard.js", import.meta.url).pathname,
      },
    )
    const message = {
      id: `msg_${ingressAgent}`,
      agent: ingressAgent,
      model: { providerID: "openrouter", modelID: "openai/gpt-4.1-nano" },
    }

    await hooks["chat.message"](
      { sessionID: session.id, agent: ingressAgent, messageID: message.id },
      { message, parts: [{ type: "text", text: "Voice request" }] },
    )

    assert.equal(message.agent, "jarvis")
    assert.deepEqual(message.model, HOME_ORCHESTRATORS.jarvis.model)
    assert.equal(session.agent, "jarvis")
    assert.equal(session.title, "Jarvis Voice")
    assert.deepEqual(session.model, { providerID: "openai", id: "gpt-5.6-sol" })
    assert.deepEqual(session.permission, NORMALIZED_VOICE_PERMISSION)
    assert.equal(session.metadata.source, "voice-test")
    assert.deepEqual(session.metadata.homeAgent, {
      kind: "orchestrator",
      role: "voice-orchestration",
      ingress: "voice",
      ingressAgent,
      voiceMessageID: message.id,
      agent: "jarvis",
      displayName: "Jarvis",
      model: "openai/gpt-5.6-sol",
    })
    assert.equal(updates.length, 1)
    assert.deepEqual(Object.keys(updates[0]), ["title", "metadata"])
    assert.equal(prompts.length, 1)
    assert.deepEqual(prompts[0], {
      messageID: message.id,
      agent: "jarvis",
      model: { providerID: "openai", modelID: "gpt-5.6-sol" },
      noReply: true,
      tools: { doom_loop: false },
      parts: [],
    })
  }
})

test("real phone voice permission defaults are accepted and normalized during promotion", async () => {
  const session = {
    id: "ses_phone",
    agent: "voice-home-agent",
    directory: "/work/home-agent",
    metadata: null,
    permission: PHONE_VOICE_PERMISSION_DEFAULTS.map((rule) => ({ ...rule })),
  }
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
        return applyNoReplyPrompt(session, body)
      },
    },
  }
  const hooks = await HomeAgentGuard({ client, directory: session.directory })
  const message = { id: "msg_phone", agent: "voice-home-agent" }

  await hooks["chat.message"](
    { sessionID: session.id, agent: "voice-home-agent", messageID: message.id },
    { message, parts: [{ type: "text", text: "Check project status" }] },
  )

  assert.equal(message.agent, "jarvis")
  assert.deepEqual(message.model, HOME_ORCHESTRATORS.jarvis.model)
  assert.equal(session.agent, "jarvis")
  assert.equal(session.title, "Jarvis Voice")
  assert.deepEqual(session.permission, NORMALIZED_VOICE_PERMISSION)
  assert.equal(updates.length, 1)
  assert.deepEqual(Object.keys(updates[0]), ["title", "metadata"])
  assert.equal(prompts.length, 1)
  assert.equal(prompts[0].agent, "jarvis")
  assert.deepEqual(prompts[0].model, { providerID: "openai", modelID: "gpt-5.6-sol" })
  assert.equal(prompts[0].noReply, true)
  assert.deepEqual(prompts[0].tools, { doom_loop: false })
  assert.deepEqual(session.metadata.homeAgent, {
    kind: "orchestrator",
    role: "voice-orchestration",
    ingress: "voice",
    ingressAgent: "voice-home-agent",
    voiceMessageID: message.id,
    agent: "jarvis",
    displayName: "Jarvis",
    model: "openai/gpt-5.6-sol",
  })
})

test("promoted voice sessions receive catalog policy and no-delete Bash wrapping", async () => {
  const session = {
    id: "ses_voice",
    agent: "voice-general",
    directory: "/work/home-agent",
  }
  const client = {
    session: {
      get: async () => ({ data: session }),
      update: async ({ body }) => {
        Object.assign(session, body)
        return { data: session }
      },
      prompt: async ({ body }) => applyNoReplyPrompt(session, body),
    },
  }
  const hooks = await HomeAgentGuard(
    { client, directory: session.directory },
    {
      catalogPath: new URL("./fixtures/voice-catalog.md", import.meta.url),
      vaultRoot: "/work/vault",
      sandboxPath: new URL("../plugin/home-agent-guard.js", import.meta.url).pathname,
    },
  )
  const message = { id: "msg_voice", agent: session.agent }
  await hooks["chat.message"](
    { sessionID: session.id, agent: session.agent, messageID: message.id },
    { message, parts: [{ type: "text", text: "Inspect both projects" }] },
  )

  const before = hooks["tool.execute.before"]
  await before(
    { tool: "read", sessionID: session.id },
    { args: { filePath: "/work/home-agent/README.md" } },
  )
  await before(
    { tool: "read", sessionID: session.id },
    { args: { filePath: "/work/project-beta/README.md" } },
  )
  await assert.rejects(
    before(
      { tool: "read", sessionID: session.id },
      { args: { filePath: "/work/outside/README.md" } },
    ),
    /outside the selected project/,
  )

  const trusted = { args: { command: "home-agentctl status --json" } }
  await before({ tool: "bash", sessionID: session.id, callID: "call_trusted" }, trusted)
  assert.equal(trusted.args.command, "home-agentctl status --json")

  const chained = { args: { command: "home-agentctl status && touch marker" } }
  await before({ tool: "bash", sessionID: session.id, callID: "call_chained" }, chained)
  assert.notEqual(chained.args.command, "home-agentctl status && touch marker")
  assert.match(chained.args.command, /--write-root.*home-agent/)
})

test("voice promotion is idempotent per message and rejects a second utterance", async () => {
  const session = {
    id: "ses_voice_once",
    agent: "voice-system",
    directory: "/work/home-agent",
  }
  let updates = 0
  let prompts = 0
  const client = {
    session: {
      get: async () => ({ data: session }),
      update: async ({ body }) => {
        updates++
        Object.assign(session, body)
        return { data: session }
      },
      prompt: async ({ body }) => {
        prompts++
        return applyNoReplyPrompt(session, body)
      },
    },
  }
  const hooks = await HomeAgentGuard({ client, directory: session.directory })
  const submit = async (id) => {
    const message = { id, agent: "voice-system" }
    await hooks["chat.message"](
      { sessionID: session.id, agent: "voice-system", messageID: id },
      { message, parts: [{ type: "text", text: "Status" }] },
    )
    return message
  }

  assert.equal((await submit("msg_once")).agent, "jarvis")
  assert.equal((await submit("msg_once")).agent, "jarvis")
  assert.equal(updates, 1)
  assert.equal(prompts, 1)
  await assert.rejects(submit("msg_second"), /already accepted another request/)
  assert.equal(updates, 1)
  assert.equal(prompts, 1)
})

test("voice promotion recovers from ambiguous persistence transport failures", async () => {
  const session = {
    id: "ses_voice_ambiguous",
    agent: "voice-general",
    directory: "/work/home-agent",
    permission: PHONE_VOICE_PERMISSION_DEFAULTS.map((rule) => ({ ...rule })),
  }
  let updates = 0
  let prompts = 0
  const client = {
    session: {
      get: async () => ({ data: session }),
      update: async ({ body }) => {
        updates++
        Object.assign(session, body)
        throw new Error("claim transport timed out")
      },
      prompt: async ({ body }) => {
        prompts++
        applyNoReplyPrompt(session, body)
        throw new Error("prompt transport timed out")
      },
    },
  }
  const hooks = await HomeAgentGuard({ client, directory: session.directory })
  const first = { id: "msg_ambiguous", agent: "voice-general" }

  await hooks["chat.message"](
    { sessionID: session.id, agent: "voice-general", messageID: first.id },
    { message: first, parts: [{ type: "text", text: "Status" }] },
  )
  assert.equal(first.agent, "jarvis")
  assert.equal(session.agent, "jarvis")
  assert.deepEqual(session.permission, NORMALIZED_VOICE_PERMISSION)

  const retry = { id: first.id, agent: "voice-general" }
  await hooks["chat.message"](
    { sessionID: session.id, agent: "voice-general", messageID: retry.id },
    { message: retry, parts: [{ type: "text", text: "Status" }] },
  )
  assert.equal(retry.agent, "jarvis")
  assert.equal(updates, 1)
  assert.equal(prompts, 1)
})

test("voice promotion fails closed for children, unsafe permissions, and incomplete updates", async () => {
  const scenarios = [
    {
      name: "child",
      session: { parentID: "ses_parent" },
      error: /fresh root session/,
    },
    {
      name: "permissive-permission",
      session: {
        permission: PHONE_VOICE_PERMISSION_DEFAULTS.map((rule, index) => (
          index === 0 ? { ...rule, action: "allow" } : rule
        )),
      },
      error: /permission overrides/,
    },
    {
      name: "interactive-permission",
      session: {
        permission: PHONE_VOICE_PERMISSION_DEFAULTS.map((rule, index) => (
          index === 0 ? { ...rule, action: "ask" } : rule
        )),
      },
      error: /permission overrides/,
    },
    {
      name: "unknown-denial",
      session: {
        permission: [
          ...PHONE_VOICE_PERMISSION_DEFAULTS.slice(0, 2),
          { permission: "bash", pattern: "*", action: "deny" },
        ],
      },
      error: /permission overrides/,
    },
    {
      name: "extra-denial",
      session: {
        permission: [
          ...PHONE_VOICE_PERMISSION_DEFAULTS,
          { permission: "bash", pattern: "*", action: "deny" },
        ],
      },
      error: /permission overrides/,
    },
    {
      name: "extended-known-denial",
      session: {
        permission: PHONE_VOICE_PERMISSION_DEFAULTS.map((rule, index) => (
          index === 0 ? { ...rule, source: "unexpected" } : rule
        )),
      },
      error: /permission overrides/,
    },
    {
      name: "ignored-permission-normalization",
      session: { permission: PHONE_VOICE_PERMISSION_DEFAULTS.map((rule) => ({ ...rule })) },
      ignorePermission: true,
      error: /could not persist/,
    },
    {
      name: "ignored-agent-change",
      session: { permission: PHONE_VOICE_PERMISSION_DEFAULTS.map((rule) => ({ ...rule })) },
      ignoreAgent: true,
      error: /could not persist/,
    },
    {
      name: "prompt-error",
      session: { permission: PHONE_VOICE_PERMISSION_DEFAULTS.map((rule) => ({ ...rule })) },
      promptError: true,
      error: /could not persist/,
    },
    {
      name: "prompt-tools",
      session: {},
      messageTools: { bash: true },
      error: /prompt-level tool permission overrides/,
    },
    {
      name: "different-ingress-agent",
      session: { agent: "voice-system" },
      error: /owned by another agent/,
    },
    {
      name: "update",
      session: {},
      unchangedUpdate: true,
      error: /could not persist/,
    },
  ]

  for (const scenario of scenarios) {
    const session = {
      id: `ses_voice_${scenario.name}`,
      agent: "voice-builder",
      directory: "/work/home-agent",
      ...scenario.session,
    }
    const client = {
      session: {
        get: async () => ({ data: session }),
        update: async ({ body }) => {
          if (scenario.unchangedUpdate) return { data: session }
          return { data: Object.assign(session, body) }
        },
        prompt: async ({ body }) => {
          if (scenario.promptError) throw new Error("prompt failed")
          return applyNoReplyPrompt(session, body, {
            agent: !scenario.ignoreAgent,
            permission: !scenario.ignorePermission,
          })
        },
      },
    }
    const hooks = await HomeAgentGuard({ client, directory: session.directory })
    const message = {
      id: `msg_${scenario.name}`,
      agent: "voice-builder",
      ...(scenario.messageTools ? { tools: scenario.messageTools } : {}),
    }

    await assert.rejects(
      hooks["chat.message"](
        { sessionID: session.id, agent: "voice-builder", messageID: message.id },
        { message, parts: [{ type: "text", text: "Change files" }] },
      ),
      scenario.error,
    )
    assert.equal(message.agent, "voice-builder")
  }
})

test("voice promotion retries repair safe incomplete agent and permission state", async () => {
  for (const staleState of [
    { name: "agent", agent: "voice-system", permission: null },
    {
      name: "permission",
      agent: "jarvis",
      permission: PHONE_VOICE_PERMISSION_DEFAULTS.map((rule) => ({ ...rule })),
    },
  ]) {
    const messageID = `msg_stale_${staleState.name}`
    const session = {
      id: `ses_stale_${staleState.name}`,
      agent: staleState.agent,
      directory: "/work/home-agent",
      permission: staleState.permission,
      metadata: {
        homeAgent: {
          kind: "orchestrator",
          role: "voice-orchestration",
          ingress: "voice",
          ingressAgent: "voice-system",
          voiceMessageID: messageID,
          agent: "jarvis",
          displayName: "Jarvis",
          model: "openai/gpt-5.6-sol",
        },
      },
    }
    let updates = 0
    let prompts = 0
    const client = {
      session: {
        get: async () => ({ data: session }),
        update: async ({ body }) => {
          updates++
          Object.assign(session, body)
          return { data: session }
        },
        prompt: async ({ body }) => {
          prompts++
          return applyNoReplyPrompt(session, body)
        },
      },
    }
    const hooks = await HomeAgentGuard({ client, directory: session.directory })

    const message = { id: messageID, agent: "voice-system" }
    await hooks["chat.message"](
      { sessionID: session.id, agent: "voice-system", messageID },
      {
        message,
        parts: [{ type: "text", text: "Retry" }],
      },
    )
    assert.equal(message.agent, "jarvis")
    assert.equal(session.agent, "jarvis")
    assert.deepEqual(session.permission, NORMALIZED_VOICE_PERMISSION)
    assert.equal(updates, 1)
    assert.equal(prompts, 1)
  }
})

test("unpromoted voice sessions cannot execute controller commands", async () => {
  const session = {
    id: "ses_voice_unpromoted",
    agent: "voice-home-agent",
    directory: "/work/home-agent",
    metadata: {
      homeAgent: {
        kind: "orchestrator",
        role: "voice-orchestration",
        ingress: "voice",
        ingressAgent: "voice-home-agent",
        voiceMessageID: "msg_unpromoted",
      },
    },
  }
  const client = { session: { get: async () => ({ data: session }) } }
  const hooks = await HomeAgentGuard({ client, directory: session.directory })

  await assert.rejects(
    hooks["tool.execute.before"](
      { tool: "bash", sessionID: session.id, callID: "call_unpromoted" },
      { args: { command: "home-agentctl status --json" } },
    ),
    /forbids tool: bash/,
  )
})
