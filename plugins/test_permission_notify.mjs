import assert from "node:assert/strict"
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import test from "node:test"

test("idle clears only that producer session's pending requests", async () => {
  const root = mkdtempSync(join(tmpdir(), "permission-notify-"))
  const previousRuntime = process.env.XDG_RUNTIME_DIR
  process.env.XDG_RUNTIME_DIR = root

  try {
    const moduleURL = new URL(
      `./permission-notify.js?test=${Date.now()}`,
      import.meta.url,
    )
    const { PermissionNotify } = await import(moduleURL.href)
    let confirmed = { permissions: new Set(), questions: new Set() }
    let pendingLookup = async (dir) => confirmed
    const hooks = await PermissionNotify(
      { client: {}, directory: "/work/workspace-a" },
      {
        notifications: false,
        pendingRequests: (...arguments_) => pendingLookup(...arguments_),
      },
    )

    const emit = (type, properties) => hooks.event({ event: { type, properties } })
    const permission = (id, sessionID) => ({
      id,
      sessionID,
      permission: "bash",
      metadata: { command: "npm test" },
    })
    const question = (id, sessionID) => ({
      id,
      sessionID,
      questions: [{ header: "Continue?", question: "Choose a result" }],
    })
    const state = () => {
      const name = readdirSync(join(root, "ocdeck-permissions"))
        .find((item) => item.startsWith("%2Fwork%2Fworkspace-a-"))
      return JSON.parse(readFileSync(join(root, "ocdeck-permissions", name), "utf8"))
    }

    await emit("permission.asked", permission("perm-a", "session-a"))
    await emit("permission.asked", permission("perm-a", "session-a"))
    await emit("question.asked", question("question-a", "session-a"))
    await emit("question.asked", question("question-a", "session-a"))
    await emit("permission.asked", permission("perm-b", "session-b"))
    await emit("question.asked", question("question-b", "session-b"))
    await emit("session.status", {
      sessionID: "session-a",
      status: { type: "busy" },
    })

    assert.equal(state().notifierVersion, 8)
    assert.match(state().processStartTicks, /^\d+$/)
    assert.equal(state().workspace, "/work/workspace-a")
    const stateFile = join(
      root,
      "ocdeck-permissions",
      readdirSync(join(root, "ocdeck-permissions"))
        .find((item) => item.startsWith("%2Fwork%2Fworkspace-a-")),
    )
    assert.equal(state().permissions.every((item) => item.updated > 0), true)
    assert.equal(state().questions.every((item) => item.updated > 0), true)
    assert.deepEqual(
      state().permissions.map((item) => item.id),
      ["perm-a", "perm-b"],
    )
    assert.deepEqual(
      state().questions.map((item) => item.id),
      ["question-a", "question-b"],
    )

    await emit("session.status", {
      sessionID: "session-a",
      status: "idle",
    })
    assert.deepEqual(
      state().permissions.map((item) => item.id),
      ["perm-b"],
    )
    assert.deepEqual(
      state().questions.map((item) => item.id),
      ["question-b"],
    )

    await emit("permission.asked", permission("perm-a-new", "session-a"))
    assert.equal(
      state().statuses.find((item) => item.sessionID === "session-a")?.status,
      "busy",
    )
    await emit("session.status", {
      sessionID: "session-a",
      status: "busy",
    })
    assert.deepEqual(
      state().permissions.map((item) => item.id),
      ["perm-b", "perm-a-new"],
    )

    await emit("session.idle", { sessionID: "session-a" })
    await emit("permission.replied", { requestID: "perm-b" })
    await emit("question.rejected", { requestID: "question-b" })
    assert.deepEqual(state().permissions, [])
    assert.deepEqual(state().questions, [])

    await emit("permission.asked", permission("perm-c", "session-c"))
    await emit("question.asked", question("question-c", "session-c"))
    await emit("session.idle", { sessionID: "unrelated-session" })
    assert.deepEqual(state().permissions.map((item) => item.id), ["perm-c"])
    await emit("permission.replied", { requestID: "unknown-request" })
    assert.deepEqual(state().questions.map((item) => item.id), ["question-c"])
    await emit("session.deleted", { info: { id: "session-c" } })
    assert.deepEqual(state().permissions, [])
    assert.deepEqual(state().questions, [])
    assert.equal(
      state().statuses.some((item) => item.sessionID === "session-c"),
      false,
    )
    await emit("permission.asked", permission("late-deleted", "session-c"))
    await emit("session.status", { sessionID: "session-c", status: "busy" })
    assert.equal(
      state().permissions.some((item) => item.sessionID === "session-c"),
      false,
    )
    assert.equal(
      state().statuses.some((item) => item.sessionID === "session-c"),
      false,
    )

    await emit("permission.asked", permission("perm-delayed", "session-delayed"))
    confirmed = {
      permissions: new Set(["session-delayed\nperm-delayed"]),
      questions: new Set(),
    }
    await emit("session.idle", { sessionID: "session-delayed" })
    assert.deepEqual(state().permissions.map((item) => item.id), ["perm-delayed"])
    assert.equal(
      state().statuses.find((item) => item.sessionID === "session-delayed")?.status,
      "busy",
    )
    confirmed = { permissions: new Set(), questions: new Set() }
    await emit("session.idle", { sessionID: "session-delayed" })
    assert.deepEqual(state().permissions, [])

    await emit("permission.asked", permission("race-old", "session-race"))
    let releaseLookup
    pendingLookup = () => new Promise((resolve) => {
      releaseLookup = resolve
    })
    const delayedIdle = emit("session.idle", { sessionID: "session-race" })
    await new Promise((resolve) => setImmediate(resolve))
    assert.equal(
      state().statuses.find((item) => item.sessionID === "session-race")?.status,
      "busy",
    )
    await emit("permission.asked", permission("race-new", "session-race"))
    releaseLookup({ permissions: new Set(), questions: new Set() })
    await delayedIdle
    assert.deepEqual(
      state().permissions.filter((item) => item.sessionID === "session-race").map((item) => item.id),
      ["race-new"],
    )
    assert.equal(
      state().statuses.find((item) => item.sessionID === "session-race")?.status,
      "busy",
    )

    pendingLookup = async () => null
    await emit("session.idle", { sessionID: "session-race" })
    assert.deepEqual(
      state().permissions.filter((item) => item.sessionID === "session-race").map((item) => item.id),
      ["race-new"],
    )
    confirmed = { permissions: new Set(), questions: new Set() }
    pendingLookup = async () => confirmed
    await emit("session.idle", { sessionID: "session-race" })
    assert.equal(
      state().permissions.some((item) => item.sessionID === "session-race"),
      false,
    )

    await emit("session.deleted", { sessionID: "session-a" })
    await emit("session.deleted", { sessionID: "session-b" })
    await emit("session.deleted", { sessionID: "unrelated-session" })
    await emit("session.deleted", { sessionID: "session-delayed" })
    await emit("session.deleted", { sessionID: "session-race" })
    assert.equal(state().deletedSessions.length >= 5, true)

    await emit("permission.asked", permission("dispose-race", "dispose-session"))
    let releaseDisposeLookup
    pendingLookup = () => new Promise((resolve) => {
      releaseDisposeLookup = resolve
    })
    const disposeIdle = emit("session.idle", { sessionID: "dispose-session" })
    await new Promise((resolve) => setImmediate(resolve))

    await hooks.dispose()
    releaseDisposeLookup({ permissions: new Set(), questions: new Set() })
    await disposeIdle
    assert.equal(existsSync(stateFile), false)
    await emit("permission.asked", permission("late", "late-session"))
    assert.equal(existsSync(stateFile), false)
  } finally {
    if (previousRuntime === undefined) delete process.env.XDG_RUNTIME_DIR
    else process.env.XDG_RUNTIME_DIR = previousRuntime
    rmSync(root, { recursive: true, force: true })
  }
})

test("multi-workspace isolation: separate state per directory", async () => {
  const root = mkdtempSync(join(tmpdir(), "permission-notify-multi-"))
  const previousRuntime = process.env.XDG_RUNTIME_DIR
  process.env.XDG_RUNTIME_DIR = root

  try {
    const moduleURL = new URL(
      `./permission-notify.js?test=${Date.now()}`,
      import.meta.url,
    )
    const { PermissionNotify } = await import(moduleURL.href)
    let confirmed = { permissions: new Set(), questions: new Set() }
    let pendingLookup = async (dir) => confirmed

    const hooksA = await PermissionNotify(
      { client: {}, directory: "/work/workspace-a" },
      { notifications: false, pendingRequests: (...arguments_) => pendingLookup(...arguments_) },
    )
    const hooksB = await PermissionNotify(
      { client: {}, directory: "/work/workspace-b" },
      { notifications: false, pendingRequests: (...arguments_) => pendingLookup(...arguments_) },
    )

    const emitA = (type, properties) => hooksA.event({ event: { type, properties } })
    const emitB = (type, properties) => hooksB.event({ event: { type, properties } })
    const permission = (id, sessionID) => ({
      id,
      sessionID,
      permission: "bash",
      metadata: { command: "npm test" },
    })

    const stateFile = (workspace) => join(
      root,
      "ocdeck-permissions",
      readdirSync(join(root, "ocdeck-permissions"))
        .find((item) => item.startsWith(`${encodeURIComponent(workspace)}-`)),
    )
    const stateA = () => JSON.parse(readFileSync(stateFile("/work/workspace-a"), "utf8"))
    const stateB = () => JSON.parse(readFileSync(stateFile("/work/workspace-b"), "utf8"))

    await emitA("permission.asked", permission("perm-a", "session-a"))
    await emitB("permission.asked", permission("perm-b", "session-b"))

    assert.deepEqual(stateA().permissions.map((item) => item.id), ["perm-a"])
    assert.deepEqual(stateB().permissions.map((item) => item.id), ["perm-b"])
    assert.equal(stateA().workspace, "/work/workspace-a")
    assert.equal(stateB().workspace, "/work/workspace-b")
    const stateFileA = stateFile("/work/workspace-a")
    const stateFileB = stateFile("/work/workspace-b")

    await emitA("session.idle", { sessionID: "session-a" })
    await emitB("session.idle", { sessionID: "session-b" })

    assert.deepEqual(stateA().permissions, [])
    assert.deepEqual(stateB().permissions, [])

    await hooksA.dispose()
    await hooksB.dispose()

    assert.equal(existsSync(stateFileA), false)
    assert.equal(existsSync(stateFileB), false)
  } finally {
    if (previousRuntime === undefined) delete process.env.XDG_RUNTIME_DIR
    else process.env.XDG_RUNTIME_DIR = previousRuntime
    rmSync(root, { recursive: true, force: true })
  }
})

test("v2 permission and question events remain pending until their v2 replies", async () => {
  const root = mkdtempSync(join(tmpdir(), "permission-notify-v2-"))
  const previousRuntime = process.env.XDG_RUNTIME_DIR
  process.env.XDG_RUNTIME_DIR = root

  try {
    const moduleURL = new URL(
      `./permission-notify.js?v2-test=${Date.now()}`,
      import.meta.url,
    )
    const { PermissionNotify } = await import(moduleURL.href)
    const hooks = await PermissionNotify(
      { client: {}, directory: "/work/v2" },
      {
        notifications: false,
        pendingRequests: async () => ({ permissions: new Set(), questions: new Set() }),
      },
    )
    const emit = (type, properties) => hooks.event({ event: { type, properties } })
    const state = () => {
      const name = readdirSync(join(root, "ocdeck-permissions"))[0]
      return JSON.parse(readFileSync(join(root, "ocdeck-permissions", name), "utf8"))
    }

    await emit("permission.v2.asked", {
      id: "v2-permission",
      sessionID: "v2-session",
      action: "bash",
      resources: ["npm test"],
      save: ["npm test"],
    })
    await emit("question.v2.asked", {
      id: "v2-question",
      sessionID: "v2-session",
      questions: [{ header: "Continue?", question: "Choose a result" }],
    })
    assert.deepEqual(state().permissions, [{
      id: "v2-permission",
      sessionID: "v2-session",
      permission: "bash",
      pattern: "npm test",
      updated: state().permissions[0].updated,
    }])
    assert.equal(state().questions[0].id, "v2-question")

    await emit("session.idle", { sessionID: "v2-session" })
    assert.equal(state().permissions.length, 1)
    assert.equal(state().questions.length, 1)

    await emit("permission.v2.replied", {
      sessionID: "v2-session",
      requestID: "v2-permission",
      reply: "reject",
    })
    await emit("question.v2.rejected", {
      sessionID: "v2-session",
      requestID: "v2-question",
    })
    assert.deepEqual(state().permissions, [])
    assert.deepEqual(state().questions, [])
    await hooks.dispose()
  } finally {
    if (previousRuntime === undefined) delete process.env.XDG_RUNTIME_DIR
    else process.env.XDG_RUNTIME_DIR = previousRuntime
    rmSync(root, { recursive: true, force: true })
  }
})

test("runtime entry exports only the notifier plugin factory", async () => {
  const moduleURL = new URL(
    `./permission-notify-entry.js?entry-test=${Date.now()}`,
    import.meta.url,
  )
  const runtime = await import(moduleURL.href)
  assert.deepEqual(Object.keys(runtime), ["PermissionNotify"])
  assert.equal(typeof runtime.PermissionNotify, "function")
})

test("pending API lookup is loopback-only and rejects partial malformed data", async () => {
  const moduleURL = new URL(
    `./permission-notify.js?fetch-test=${Date.now()}`,
    import.meta.url,
  )
  const { PermissionNotify, fetchPendingRequests } = await import(moduleURL.href)
  const previousFetch = globalThis.fetch
  const previousPassword = process.env.OPENCODE_SERVER_PASSWORD
  let requests = []

  try {
    globalThis.fetch = async (url, options) => {
      requests.push({ url: String(url), options })
      const payload = new URL(String(url)).pathname === "/permission"
        ? [{ id: "permission-1", sessionID: "session-1" }]
        : [null]
      return { ok: true, json: async () => payload }
    }
    process.env.OPENCODE_SERVER_PASSWORD = "not-logged"

    assert.equal(await fetchPendingRequests("https://example.com:4096", "/work/test"), null)
    assert.equal(requests.length, 0)
    assert.equal(await fetchPendingRequests("http://127.0.0.1:4096", "/work/test"), null)
    assert.equal(requests.length, 2)
    assert.match(requests[0].options.headers.Authorization, /^Basic /)
    assert.equal(new URL(requests[0].url).searchParams.get("directory"), "/work/test")
  } finally {
    globalThis.fetch = previousFetch
    if (previousPassword === undefined) delete process.env.OPENCODE_SERVER_PASSWORD
    else process.env.OPENCODE_SERVER_PASSWORD = previousPassword
  }
})

test("fetchPendingRequests passes directory parameter to API calls", async () => {
  const moduleURL = new URL(
    `./permission-notify.js?fetch-test2=${Date.now()}`,
    import.meta.url,
  )
  const { fetchPendingRequests } = await import(moduleURL.href)
  const previousFetch = globalThis.fetch
  let requests = []

  try {
    globalThis.fetch = async (url, options) => {
      requests.push({ url: String(url), options })
      return { ok: true, json: async () => [{ id: "permission-1", sessionID: "session-1" }] }
    }

    await fetchPendingRequests("http://127.0.0.1:4096", "/work/specific-project")

    assert.equal(requests.length, 2)
    const permUrl = new URL(requests[0].url)
    assert.equal(permUrl.searchParams.get("directory"), "/work/specific-project")
    const qUrl = new URL(requests[1].url)
    assert.equal(qUrl.searchParams.get("directory"), "/work/specific-project")
  } finally {
    globalThis.fetch = previousFetch
  }
})
