import { spawn } from "node:child_process"
import {
  closeSync,
  constants,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
  writeSync,
} from "node:fs"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"

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

const APPROVE_ACTION = "opencode.permission.once"
const APPROVE_ALWAYS_ACTION = "opencode.permission.always"
const FOCUS_ACTION = "opencode.permission.focus"
const ACTION_MONITOR_RESTART_MS = 1000
const NOTIFICATION_RESTORE_MS = 250
const NOTIFIER_VERSION = 8
const SESSION_DELETE_TTL_MS = 5 * 60 * 1000
const RUNTIME_ROOT = process.env.XDG_RUNTIME_DIR ||
  join(tmpdir(), `ocdeck-${process.getuid?.() ?? "user"}`)
const PERMISSION_STATE_DIR = join(RUNTIME_ROOT, "ocdeck-permissions")

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

const PREEXEC = "\u001b]666;vte.shell.preexec!\u001b\\"
const PRECMD = "\u001b]666;vte.shell.precmd!\u001b\\"

const IN_TMUX = Boolean(process.env.TMUX)

function wrapForMultiplexer(sequence) {
  if (!IN_TMUX) return sequence
  const payload = sequence.replace(/\u001b/g, "\u001b\u001b")
  return `\u001bPtmux;${payload}\u001b\\`
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

function processStartTicks() {
  try {
    const stat = readFileSync("/proc/self/stat", "utf8")
    const closingParenthesis = stat.lastIndexOf(")")
    if (closingParenthesis < 0) return ""
    return text(stat.slice(closingParenthesis + 1).trim().split(/\s+/)[19], 40)
  } catch {
    return ""
  }
}

function requestIdentity(request) {
  return `${text(request?.sessionID, 300)}\n${text(request?.id, 300)}`
}

function loopbackServerURL(value) {
  try {
    const url = new URL(value)
    const hostname = url.hostname.replace(/^\[|\]$/g, "").toLowerCase()
    if (!["127.0.0.1", "::1", "localhost"].includes(hostname)) return null
    if (!["http:", "https:"].includes(url.protocol)) return null
    return url
  } catch {
    return null
  }
}

function permissionStateFileForWorkspace(workspace, startTicks) {
  const producer = `${process.pid}-${text(startTicks, 40) || "unknown"}`
  return join(PERMISSION_STATE_DIR, `${encodeURIComponent(workspace)}-${producer}.json`)
}

function variantString(value, limit = 1000) {
  const valueText = text(value, limit)
    .replace(/\\/g, "\\\\")
    .replace(/'/g, "\\'")

  return `'${valueText}'`
}

function writeTerminal(value) {
  let ttyFD
  try {
    ttyFD = openSync("/dev/tty", constants.O_WRONLY | (constants.O_NOCTTY ?? 0))
  } catch {
    return false
  }

  try {
    writeSync(ttyFD, value)
    closeSync(ttyFD)
    return true
  } catch {
    try { closeSync(ttyFD) } catch {}
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
    child.stdout.on("data", (chunk) => { stdout += chunk })
    child.on("error", () => finish(""))
    child.on("exit", (code) => finish(code === 0 ? stdout : ""))
    timeout = setTimeout(() => {
      try { child.kill("SIGKILL") } catch {}
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

function withdrawPtyxisNotification(notificationID) {
  if (!notificationID) return
  notificationCall("RemoveNotification", [NOTIFICATION_APP_ID, text(notificationID, 300)])
}

function createWorkspaceState(workspace, serverUrl, processStartTicks, notifierVersion) {
  return {
    workspace,
    serverUrl,
    processStartTicks,
    notifierVersion,
    ttyFD: undefined,
    tabUUID: "",
    tabOwner: "",
    currentTmuxSession: "",
    discovery: undefined,
    shownRequest: "",
    shownNotification: "",
    actionMonitor: undefined,
    actionMonitorRestart: undefined,
    actionHandler: undefined,
    disposed: false,
    eventSequence: 0,
    pending: new Map(),
    pendingQuestions: new Map(),
    sessionStatuses: new Map(),
    sessionDeletions: new Map(),
    sessionActivity: new Map(),
    approving: new Set(),
    syncPermissionState() {
      try {
        const now = Date.now()
        for (const [sessionID, updated] of this.sessionDeletions) {
          if (now - updated > SESSION_DELETE_TTL_MS) this.sessionDeletions.delete(sessionID)
        }
        const stateFile = permissionStateFileForWorkspace(this.workspace, this.processStartTicks)
        if (this.disposed) {
          try { unlinkSync(stateFile) } catch {}
          return
        }

        mkdirSync(PERMISSION_STATE_DIR, { recursive: true, mode: 0o700 })
        const payload = {
          pid: process.pid,
          notifierVersion: this.notifierVersion,
          updated: Date.now(),
          processStartTicks: this.processStartTicks,
          workspace: this.workspace,
          permissions: [...this.pending.values()].map(({ request, updated }) => ({
            id: text(request.id, 300),
            sessionID: text(request.sessionID, 300),
            permission: text(request.permission, 80) || "permission",
            pattern: permissionText(request),
            updated,
          })),
          questions: [...this.pendingQuestions.values()].map(({ request, updated }) => ({
            id: text(request.id, 300),
            sessionID: text(request.sessionID, 300),
            question: questionText(request),
            updated,
          })),
          statuses: [...this.sessionStatuses].map(([sessionID, entry]) => ({
            sessionID: text(sessionID, 300),
            status: text(entry.status, 40),
            updated: entry.updated,
          })),
          deletedSessions: [...this.sessionDeletions].map(([sessionID, updated]) => ({
            sessionID: text(sessionID, 300),
            updated,
          })),
        }
        const temporary = `${stateFile}.${Date.now()}.tmp`
        writeFileSync(temporary, JSON.stringify(payload), { encoding: "utf8", mode: 0o600 })
        renameSync(temporary, stateFile)
      } catch {}
    },
    hasPendingActions() {
      return this.pending.size > 0 || this.pendingQuestions.size > 0
    },
    markSessionActivity(sessionID) {
      const target = text(sessionID, 300)
      if (!target) return 0
      const sequence = ++this.eventSequence
      this.sessionActivity.set(target, sequence)
      return sequence
    },
  }
}

async function fetchPendingRequests(serverUrl, directory) {
  const base = loopbackServerURL(serverUrl)
  if (!base) return null
  const password = process.env.OPENCODE_SERVER_PASSWORD || ""
  const username = process.env.OPENCODE_SERVER_USERNAME || "opencode"
  const headers = {}
  if (password) {
    headers.Authorization = `Basic ${Buffer.from(`${username}:${password}`).toString("base64")}`
  }

  const read = async (path) => {
    try {
      const url = new URL(path, base)
      if (directory) url.searchParams.set("directory", directory)
      const response = await fetch(url, { headers, signal: AbortSignal.timeout(1500) })
      if (!response.ok) return null
      const payload = await response.json()
      const data = payload && !Array.isArray(payload) && typeof payload === "object" && "data" in payload
        ? payload.data
        : payload
      if (!Array.isArray(data)) return null
      const identities = new Set()
      for (const request of data) {
        if (!request || typeof request !== "object" || !text(request.id) || !text(request.sessionID)) {
          return null
        }
        identities.add(requestIdentity(request))
      }
      return identities
    } catch {
      return null
    }
  }

  const [permissions, questions] = await Promise.all([
    read("/permission"),
    read("/question"),
  ])
  return permissions && questions ? { permissions, questions } : null
}

function permissionText(request) {
  const pattern = request?.patterns
  if (!pattern) return ""
  if (Array.isArray(pattern)) return pattern.filter((p) => typeof p === "string").join(", ")
  return String(pattern)
}

function normalizedPermissionRequest(request, eventType) {
  if (eventType !== "permission.v2.asked") return request
  return {
    ...request,
    permission: text(request?.action, 80) || "permission",
    patterns: Array.isArray(request?.resources) ? request.resources : [],
    always: Array.isArray(request?.save) ? request.save : [],
    protocol: "v2",
  }
}

function questionText(request) {
  if (!request || !Array.isArray(request.questions)) return ""
  return request.questions
    .map((q) => `${text(q.header, 100)}: ${text(q.question, 500)}`)
    .filter(Boolean)
    .join("; ")
}

function questionSummary(request) {
  const header = Array.isArray(request.questions)
    ? request.questions.map((question) => text(question?.header, 80)).find(Boolean)
    : ""
  return header ? `OpenCode question: ${header}` : "OpenCode question"
}

function stopActionMonitor(state) {
  clearTimeout(state.actionMonitorRestart)
  state.actionMonitorRestart = undefined

  const monitor = state.actionMonitor
  state.actionMonitor = undefined

  try { monitor?.kill("SIGTERM") } catch {}
}

async function startActionMonitor(state, handler) {
  state.actionHandler = handler

  if (state.disposed || state.actionMonitor || !state.hasPendingActions() || typeof state.actionHandler !== "function") return

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

  if (state.disposed || state.actionMonitor || !state.hasPendingActions()) return

  let notificationOwner = ""
  try {
    notificationOwner = text(JSON.parse(ownerOutput)?.data?.[0], 100)
  } catch {}

  if (!notificationOwner) {
    if (!state.actionMonitorRestart) {
      state.actionMonitorRestart = setTimeout(() => {
        state.actionMonitorRestart = undefined
        void startActionMonitor(state)
      }, ACTION_MONITOR_RESTART_MS)
    }
    return
  }

  let monitor
  let buffer = ""

  const finish = () => {
    if (state.actionMonitor !== monitor) return
    state.actionMonitor = undefined

    if (!state.disposed && state.hasPendingActions() && !state.actionMonitorRestart) {
      state.actionMonitorRestart = setTimeout(() => {
        state.actionMonitorRestart = undefined
        void startActionMonitor(state)
      }, ACTION_MONITOR_RESTART_MS)
    }
  }

  const consume = (line) => {
    if (!line.trim()) return

    let message
    try { message = JSON.parse(line) } catch { return }

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
    const entry = state.pending.get(requestID) ?? state.pendingQuestions.get(requestID)

    if (!entry || entry.notificationID !== notificationID) return
    state.actionHandler(action, requestID, activationToken)
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

  state.actionMonitor = monitor
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

  monitor.stdout.on("end", () => { if (buffer.trim()) consume(buffer) })
  monitor.on("error", finish)
  monitor.on("exit", finish)
}

async function getTabUUID() {
  if (!process.env.PTYXIS_VERSION || !process.env.VTE_VERSION) return ["", ""]
  if (!writeTerminal("")) return ["", ""]

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
      try { monitor?.kill("SIGTERM") } catch {}
      resolve(UUID_PATTERN.test(uuid) ? [uuid, text(owner, 100)] : ["", ""])
    }

    const consume = (line) => {
      if (!precmdSent || !line.trim()) return

      let message
      try { message = JSON.parse(line) } catch { return }

      const data = message?.payload?.data
      if (
        message?.type === "signal" &&
        message?.path === "/org/gnome/Ptyxis/Tab" &&
        message?.interface === "org.gnome.Ptyxis.Tab" &&
        message?.member === "TitleChanged" &&
        Array.isArray(data) &&
        data.length >= 2 &&
        typeof data[0] === "string" &&
        typeof data[1] === "string"
      ) {
        const [uuid, owner] = data
        if (UUID_PATTERN.test(uuid)) finish(uuid, owner)
      }
    }

    try {
      monitor = spawn(BUSCTL, [
        "--user", "--json=short",
        "--match=type='signal',path='/org/gnome/Ptyxis/Tab',interface='org.gnome.Ptyxis.Tab',member='TitleChanged'",
        "monitor",
      ], { shell: false, stdio: ["ignore", "pipe", "ignore"] })
    } catch { return finish() }

    monitor.stdout.setEncoding("utf8")
    monitor.stdout.on("data", (chunk) => { buffer += chunk; for (;;) { const nl = buffer.indexOf("\n"); if (nl < 0) break; consume(buffer.slice(0, nl)); buffer = buffer.slice(nl + 1) } })
    monitor.on("error", () => finish())
    monitor.on("exit", () => finish())

    startTimer = setTimeout(() => { finish() }, 300)
    precmdTimer = setTimeout(() => {
      integrationTouched = true
      precmdSent = true
      writeTerminal(wrapForMultiplexer(PRECMD))
    }, 50)
    deadline = setTimeout(() => finish(), 1500)
  })
}

async function getTmuxSession() {
  try {
    const output = await runQuiet(TMUX, ["display-message", "-p", "#S"])
    return text(output, 100)
  } catch { return "" }
}

async function showPtyxisPermission(entry, uuid, owner, tmuxSession) {
  entry.uuid = uuid
  entry.owner = owner
  entry.tmuxSession = tmuxSession
  entry.notificationID = `opencode-permission-${process.pid}-${entry.request.id}`

  const body = entry.body || entry.summary
  const title = `OpenCode: ${text(entry.request.permission, 40) || "permission"}`
  const actions = [[APPROVE_ACTION, "Allow once"]]
  if (entry.request.permission !== "signed_in_tabs_browser_file_upload") {
    actions.unshift([APPROVE_ALWAYS_ACTION, "Always allow"])
  }
  const buttons = actions.map(([action, label]) =>
    `{'label': <${variantString(label)}>, 'action': <${variantString(action)}>, ` +
    `'target': <${variantString(entry.request.id)}>` +
    `}`,
  ).join(", ")

  notificationCall("AddNotification", [
    NOTIFICATION_APP_ID,
    text(entry.notificationID, 300),
    `{'title': <${variantString(title)}>, ` +
    `'body': <${variantString(body)}>, ` +
    `'icon': <('themed', <['dialog-password']>)>, ` +
    `'priority': <'urgent'>, ` +
    `'default-action': <${variantString(FOCUS_ACTION)}>, ` +
    `'default-action-target': <${variantString(entry.request.id)}>, ` +
    `'buttons': <[${buttons}]>}`,
  ])
}

async function showQuestion(entry) {
  const uuid = entry.uuid || (await getTabUUID())[0]
  entry.uuid = uuid
  entry.notificationID = `opencode-question-${process.pid}-${entry.request.id}`

  const title = "OpenCode question"
  const body = entry.body || entry.summary
  notificationCall("AddNotification", [
    NOTIFICATION_APP_ID,
    text(entry.notificationID, 300),
    `{'title': <${variantString(title)}>, ` +
    `'body': <${variantString(body)}>, ` +
    `'icon': <('themed', <['dialog-question']>)>, ` +
    `'priority': <'urgent'>, ` +
    `'default-action': <${variantString(FOCUS_ACTION)}>, ` +
    `'default-action-target': <${variantString(entry.request.id)}>, ` +
    `'buttons': <[` +
    `{'label': <'Open question'>, 'action': <${variantString(FOCUS_ACTION)}>, ` +
    `'target': <${variantString(entry.request.id)}>` +
    `}]>}`,
  ])
}

async function approveRequest(entry, response, state) {
  if (state.approving.has(entry.request.id)) return
  state.approving.add(entry.request.id)

  try {
    if (entry.request.protocol === "v2") {
      const base = loopbackServerURL(state.serverUrl)
      if (!base) throw new Error("OpenCode server is not loopback")
      const url = new URL(
        `/api/session/${encodeURIComponent(entry.request.sessionID)}/permission/${encodeURIComponent(entry.request.id)}/reply`,
        base,
      )
      const headers = { "Content-Type": "application/json" }
      const password = process.env.OPENCODE_SERVER_PASSWORD || ""
      if (password) {
        const username = process.env.OPENCODE_SERVER_USERNAME || "opencode"
        headers.Authorization = `Basic ${Buffer.from(`${username}:${password}`).toString("base64")}`
      }
      const result = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify({ reply: response }),
        signal: AbortSignal.timeout(1500),
      })
      if (!result.ok) throw new Error(`OpenCode permission reply failed: ${result.status}`)
    } else {
      if (typeof entry.client?.postSessionIdPermissionsPermissionId !== "function") {
        throw new Error("The installed OpenCode client cannot reply to permissions")
      }

      const result = await entry.client.postSessionIdPermissionsPermissionId({
        path: { id: entry.request.sessionID, permissionID: entry.request.id },
        body: { response },
        throwOnError: true,
      })

      if (result?.error) throw result.error
    }
    resolveRequest(entry.request.id, state)
  } catch {
    if (state.pending.has(entry.request.id) && state.shownRequest === entry.request.id) {
      showPtyxisPermission(entry, entry.uuid, entry.owner, entry.tmuxSession)
    }
  } finally {
    state.approving.delete(entry.request.id)
  }
}

function resolveRequest(requestID, state) {
  const entry = state.pending.get(requestID)
  if (state.shownRequest === requestID) {
    state.shownRequest = ""
    if (state.notificationsEnabled && entry?.notificationID) {
      withdrawPtyxisNotification(entry.notificationID)
    }
  }
  state.pending.delete(requestID)
  state.syncPermissionState()
  if (state.notificationsEnabled && state.pending.size) void showNextPermission(state)
}

function resolveQuestion(requestID, state) {
  if (state.pendingQuestions.has(requestID)) {
    const entry = state.pendingQuestions.get(requestID)
    if (state.notificationsEnabled && entry.notificationID) {
      withdrawPtyxisNotification(entry.notificationID)
    }
    state.pendingQuestions.delete(requestID)
    state.syncPermissionState()
  }
}

function clearSessionRequests(sessionID, snapshot, idleSequence, state) {
  const target = text(sessionID, 300)
  let cleared = false
  for (const [requestID, entry] of state.pending) {
    if (text(entry.request?.sessionID, 300) !== target) continue
    if (idleSequence && entry.sequence > idleSequence) continue
    if (entry.request?.protocol === "v2") continue
    if (snapshot?.permissions?.has(requestIdentity(entry.request))) continue
    if (state.shownRequest === requestID) state.shownRequest = ""
    state.pending.delete(requestID)
    cleared = true
  }
  for (const [requestID, entry] of state.pendingQuestions) {
    if (text(entry.request?.sessionID, 300) !== target) continue
    if (idleSequence && entry.sequence > idleSequence) continue
    if (entry.request?.protocol === "v2") continue
    if (snapshot?.questions?.has(requestIdentity(entry.request))) continue
    if (state.notificationsEnabled && entry.notificationID) {
      withdrawPtyxisNotification(entry.notificationID)
    }
    state.pendingQuestions.delete(requestID)
    cleared = true
  }
  return cleared
}

function sessionHasPending(sessionID, state) {
  const target = text(sessionID, 300)
  return [...state.pending.values(), ...state.pendingQuestions.values()].some(
    (entry) => text(entry.request?.sessionID, 300) === target,
  )
}

async function reconcileIdleSession(sessionID, pendingRequests, state, idleSequence) {
  let snapshot = null
  try {
    snapshot = await pendingRequests(state.workspace)
  } catch {}
  if (state.disposed) return
  if (snapshot?.permissions instanceof Set && snapshot?.questions instanceof Set) {
    clearSessionRequests(sessionID, snapshot, idleSequence, state)
  }
  if (state.sessionActivity.get(text(sessionID, 300)) !== idleSequence) {
    state.syncPermissionState()
    return
  }
  const retained = sessionHasPending(sessionID, state)
  state.sessionStatuses.set(sessionID, { status: retained ? "busy" : "idle", updated: Date.now() })
  state.syncPermissionState()
}

async function showNextPermission(state) {
  if (state.disposed || state.shownRequest || !state.pending.size) return

  const entry = state.pending.values().next().value
  const requestID = entry.request.id
  state.shownRequest = requestID

  const [uuid, owner] = await getTabUUID().catch(() => ["", ""])
  const tmuxSession = await getTmuxSession().catch(() => "")

  if (!state.pending.has(requestID) || state.shownRequest !== requestID) {
    if (!state.shownRequest && !state.pending.size && uuid) withdrawPtyxisNotification(uuid)
    return
  }

  showPtyxisPermission(entry, uuid, owner, tmuxSession)
}

function handleNotificationAction(state, action, requestID, activationToken) {
  if (action === APPROVE_ACTION) {
    const entry = state.pending.get(requestID)
    if (entry) void approveRequest(entry, "once", state)
  } else if (action === APPROVE_ALWAYS_ACTION) {
    const entry = state.pending.get(requestID)
    if (entry) void approveRequest(entry, "always", state)
  } else if (action === FOCUS_ACTION) {
    const entry = state.pending.get(requestID) ?? state.pendingQuestions.get(requestID)
    if (entry) void focusPtyxis(requestID, activationToken, entry)
  }
}

async function focusPtyxis(requestID, activationToken, entry) {
  const tmuxSession = text(entry?.tmuxSession, 200) ||
    `oc-${text(entry?.request?.sessionID, 300)}`
  if (!tmuxSession || tmuxSession === "oc-") return
  try {
    await runQuiet(BUSCTL, [
      "--user", "call", OCDECK_SWITCH_DEST, OCDECK_SWITCH_PATH,
      FOCUS_TMUX_METHOD, "s", tmuxSession,
    ])
  } catch {}
}

const workspaces = new Map()

export const PermissionNotify = async ({ client, serverUrl, directory } = {}, options = {}) => {
  const workspace = resolve(directory || process.cwd())
  let state = workspaces.get(workspace)
  if (!state) {
    state = createWorkspaceState(workspace, serverUrl, processStartTicks(), NOTIFIER_VERSION)
    workspaces.set(workspace, state)
  }
  state.notificationsEnabled = options.notifications !== false
  state.client = client
  state.pendingRequests = typeof options.pendingRequests === "function"
    ? options.pendingRequests
    : (dir) => fetchPendingRequests(serverUrl, dir)
  state.syncPermissionState()

  if (IN_TMUX) {
    spawnQuiet(TMUX, ["set", "-g", "allow-passthrough", "on"])
  }

  return {
    event: async ({ event }) => {
      if (state.disposed) return
      if (event.type === "session.status") {
        const properties = event.properties ?? event.data
        const sessionID = properties?.sessionID
        const status = properties?.status?.type ?? properties?.status
        if (sessionID && status) {
          if (state.sessionDeletions.has(sessionID)) return
          const sequence = state.markSessionActivity(sessionID)
          if (text(status, 40).toLowerCase() === "idle") {
            await reconcileIdleSession(sessionID, state.pendingRequests, state, sequence)
          } else {
            state.sessionStatuses.set(sessionID, { status, updated: Date.now() })
            state.syncPermissionState()
          }
        }
        return
      }

      if (event.type === "session.idle") {
        const properties = event.properties ?? event.data
        if (properties?.sessionID) {
          if (state.sessionDeletions.has(properties.sessionID)) return
          const sequence = state.markSessionActivity(properties.sessionID)
          await reconcileIdleSession(properties.sessionID, state.pendingRequests, state, sequence)
        }
        return
      }

      if (event.type === "session.deleted") {
        const properties = event.properties ?? event.data
        const sessionID = properties?.info?.id ?? properties?.sessionID ?? properties?.id
        if (sessionID) {
          state.markSessionActivity(sessionID)
          state.sessionDeletions.set(sessionID, Date.now())
          state.sessionStatuses.delete(sessionID)
          clearSessionRequests(sessionID, null, 0, state)
          state.syncPermissionState()
        }
        return
      }

      if (["question.replied", "question.rejected", "question.v2.replied", "question.v2.rejected"].includes(event.type)) {
        const properties = event.properties ?? event.data
        const requestID = properties?.requestID ?? properties?.id
        if (requestID) resolveQuestion(requestID, state)
        return
      }

      if (event.type === "question.asked" || event.type === "question.v2.asked") {
        const rawRequest = event.properties ?? event.data
        const request = event.type === "question.v2.asked"
          ? { ...rawRequest, protocol: "v2" }
          : rawRequest
        if (request && typeof request === "object" && request.id && request.sessionID) {
          if (state.sessionDeletions.has(request.sessionID)) return
          const updated = Date.now()
          const sequence = state.markSessionActivity(request.sessionID)
          const existing = state.pendingQuestions.get(request.id)
          if (existing) {
            if (existing.request.sessionID !== request.sessionID) return
            existing.request = request
            existing.updated = updated
            existing.sequence = sequence
            state.sessionStatuses.set(request.sessionID, { status: "busy", updated })
            state.syncPermissionState()
            return
          }
          const entry = {
            request,
            updated,
            sequence,
            summary: questionSummary(request),
            body: questionText(request),
            notificationID: "",
            uuid: "",
            owner: "",
            tmuxSession: "",
          }
          state.sessionStatuses.set(request.sessionID, { status: "busy", updated })
          state.pendingQuestions.set(request.id, entry)
          state.syncPermissionState()
          if (state.notificationsEnabled) {
            await startActionMonitor(state, (action, requestID, token) => handleNotificationAction(state, action, requestID, token))
            void showQuestion(entry)
          }
        }
        return
      }

      if (event.type === "permission.replied" || event.type === "permission.v2.replied") {
        const properties = event.properties ?? event.data
        const replyID = properties?.requestID ?? properties?.permissionID ?? state.shownRequest
        if (replyID) resolveRequest(replyID, state)
        return
      }

      if (event.type !== "permission.asked" && event.type !== "permission.v2.asked") return

      const request = normalizedPermissionRequest(
        event.properties ?? event.data,
        event.type,
      )
      if (!request || typeof request !== "object" || !request.id || !request.sessionID) return
      if (state.sessionDeletions.has(request.sessionID)) return

      const updated = Date.now()
      const sequence = state.markSessionActivity(request.sessionID)
      const existing = state.pending.get(request.id)
      if (existing) {
        if (existing.request.sessionID !== request.sessionID) return
        existing.request = request
        existing.updated = updated
        existing.sequence = sequence
        existing.client = client
        state.sessionStatuses.set(request.sessionID, { status: "busy", updated })
        state.syncPermissionState()
        return
      }
      state.pending.set(request.id, {
        request,
        updated,
        sequence,
        summary: `OpenCode permission: ${text(request.permission, 80) || "unknown"}`,
        body: permissionText(request),
        notificationID: "",
        uuid: "",
        owner: "",
        tmuxSession: "",
        client,
      })
      state.sessionStatuses.set(request.sessionID, { status: "busy", updated })
      state.syncPermissionState()

      if (state.notificationsEnabled) {
        await startActionMonitor(state, (action, requestID, token) => handleNotificationAction(state, action, requestID, token))
        void showNextPermission(state)
      }
    },
    dispose: async () => {
      if (state.disposed) return
      state.disposed = true
      for (const entry of state.pending.values()) clearTimeout(entry.restoreTimer)
      for (const entry of state.pendingQuestions.values()) {
        clearTimeout(entry.restoreTimer)
        if (state.notificationsEnabled && entry.notificationID) {
          withdrawPtyxisNotification(entry.notificationID)
        }
      }
      state.pending.clear()
      state.pendingQuestions.clear()
      state.sessionStatuses.clear()
      state.sessionDeletions.clear()
      state.sessionActivity.clear()
      state.approving.clear()
      state.syncPermissionState()
      stopActionMonitor(state)
      if (state.notificationsEnabled) withdrawPtyxisNotification()
      state.shownRequest = ""
      state.shownNotification = ""
      workspaces.delete(workspace)
    },
  }
}

export { workspaces, fetchPendingRequests }
