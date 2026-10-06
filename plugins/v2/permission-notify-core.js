import { spawn } from "node:child_process"
import { createHash } from "node:crypto"
import {
  mkdirSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs"
import { homedir, tmpdir } from "node:os"
import { join } from "node:path"

import { locationKey, matchesLocation } from "./runtime.js"

const GDBUS = "/usr/bin/gdbus"
const BUSCTL = "/usr/bin/busctl"
const TMUX = "/usr/bin/tmux"
const PTYXIS = "/usr/bin/ptyxis"
const GTK_NOTIFICATIONS_OBJECT = "/org/gtk/Notifications"
const NOTIFICATION_APP_ID = "org.local.OCDeckSwitch"
const OCDECK_SWITCH_PATH = "/org/local/OCDeckSwitch"
const APPROVE_ACTION = "opencode.permission.once"
const APPROVE_ALWAYS_ACTION = "opencode.permission.always"
const REJECT_ACTION = "opencode.permission.reject"
const FOCUS_ACTION = "opencode.permission.focus"
const NOTIFIER_VERSION = 9
const DELETE_TTL_MS = 5 * 60 * 1000

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
  return `'${text(value, limit).replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`
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

function runQuiet(program, args, timeoutMs = 1000) {
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
    }, timeoutMs)
    timeout.unref?.()
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

function notificationID(kind, location, sessionID, requestID) {
  const hash = createHash("sha256")
    .update(`${locationKey(location)}\n${kind}\n${sessionID}\n${requestID}`)
    .digest("hex")
    .slice(0, 24)
  const prefix = kind === "permission" ? "opencode-permission" : "opencode-question"
  return `${prefix}-${process.pid}-${hash}`
}

function permissionBody(request) {
  const metadata = request.metadata && typeof request.metadata === "object" && !Array.isArray(request.metadata)
    ? request.metadata
    : {}
  return [
    metadata.description,
    metadata.command,
    metadata.filepath,
    metadata.filePath,
    metadata.url,
    metadata.query,
    request.message,
    request.resources,
  ].map((value) => text(value, 700)).find(Boolean) || "Approval required in the terminal"
}

function formBody(form) {
  const fields = Array.isArray(form.fields) ? form.fields : []
  return fields
    .map((field) => text(field?.title, 120) || text(field?.description, 240) || text(field?.key, 120))
    .filter(Boolean)
    .join("; ") || "Input required in the terminal"
}

function validPermissionRequest(request, sessionID) {
  return request &&
    typeof request.id === "string" &&
    typeof request.sessionID === "string" &&
    (sessionID === undefined || request.sessionID === sessionID) &&
    typeof request.action === "string" &&
    Array.isArray(request.resources) &&
    request.resources.every((resource) => typeof resource === "string")
}

export function createDesktopNotifications(location, rawOptions = {}) {
  const binaries = {
    gdbus: rawOptions.gdbus ?? GDBUS,
    busctl: rawOptions.busctl ?? BUSCTL,
    tmux: rawOptions.tmux ?? TMUX,
    ptyxis: rawOptions.ptyxis ?? PTYXIS,
    hotkey: rawOptions.hotkey ?? join(homedir(), ".local/bin/ocdeck-hotkey"),
  }
  const restartMs = rawOptions.monitorRestartMs ?? 1000
  let actionHandler
  let monitor
  let restartTimer
  let disposed = false

  const currentNotificationOwner = async () => {
    const output = await runQuiet(binaries.busctl, [
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
    try {
      return text(JSON.parse(output)?.data?.[0], 100)
    } catch {
      return ""
    }
  }

  const startMonitor = () => {
    if (disposed || monitor) return
    let child
    try {
      child = spawn(binaries.busctl, [
        "--user",
        "--json=short",
        `--match=type='signal',path='${GTK_NOTIFICATIONS_OBJECT}',interface='org.gtk.Notifications',member='ActionInvoked'`,
        "monitor",
      ], { shell: false, stdio: ["ignore", "pipe", "ignore"] })
    } catch {
      child = null
    }

    if (!child) {
      restartTimer = setTimeout(startMonitor, restartMs)
      restartTimer.unref?.()
      return
    }

    monitor = child
    let buffer = ""
    const finish = () => {
      if (monitor !== child) return
      monitor = undefined
      if (!disposed && !restartTimer) {
        restartTimer = setTimeout(() => {
          restartTimer = undefined
          startMonitor()
        }, restartMs)
        restartTimer.unref?.()
      }
    }
    const consume = (line) => {
      let message
      try { message = JSON.parse(line) } catch { return }
      const data = message?.payload?.data
      if (
        message?.type !== "signal" ||
        message?.path !== GTK_NOTIFICATIONS_OBJECT ||
        message?.interface !== "org.gtk.Notifications" ||
        message?.member !== "ActionInvoked" ||
        !Array.isArray(data) ||
        data[0] !== NOTIFICATION_APP_ID ||
        ![APPROVE_ACTION, APPROVE_ALWAYS_ACTION, REJECT_ACTION, FOCUS_ACTION].includes(data[2])
      ) return

      const notification = text(data[1], 300)
      const action = data[2]
      const target = text(data[3]?.[0]?.data, 300)
      const token = text(data[4]?.["activation-token"]?.data, 1000)
      if (!notification || target !== notification || typeof actionHandler !== "function") return

      void (async () => {
        const owner = await currentNotificationOwner()
        if (!owner || owner !== message.sender || disposed) return
        await actionHandler(action, notification, token)
      })().catch(() => {})
    }

    child.stdout.setEncoding("utf8")
    child.stdout.on("data", (chunk) => {
      buffer += chunk
      if (buffer.length > 1024 * 1024) buffer = ""
      for (;;) {
        const newline = buffer.indexOf("\n")
        if (newline < 0) break
        consume(buffer.slice(0, newline))
        buffer = buffer.slice(newline + 1)
      }
    })
    child.stdout.on("end", () => { if (buffer.trim()) consume(buffer) })
    child.on("error", finish)
    child.on("exit", finish)
  }

  return {
    start(handler) {
      actionHandler = handler
      startMonitor()
    },
    showPermission(entry) {
      const actions = []
      if (entry.allowAlways) actions.push([APPROVE_ALWAYS_ACTION, "Always allow"])
      actions.push([APPROVE_ACTION, "Allow once"], [REJECT_ACTION, "Reject"])
      const buttons = actions.map(([action, label]) =>
        `{'label': <${variantString(label)}>, 'action': <${variantString(action)}>, ` +
        `'target': <${variantString(entry.notificationID)}>}`,
      ).join(", ")
      notificationCall("AddNotification", [
        NOTIFICATION_APP_ID,
        entry.notificationID,
        `{'title': <${variantString(`OpenCode: ${text(entry.request.action, 80) || "permission"}`)}>, ` +
        `'body': <${variantString(entry.body, 700)}>, ` +
        `'icon': <('themed', <['dialog-password']>)>, 'priority': <'urgent'>, ` +
        `'default-action': <${variantString(FOCUS_ACTION)}>, ` +
        `'default-action-target': <${variantString(entry.notificationID)}>, ` +
        `'buttons': <[${buttons}]>}`,
      ])
    },
    showForm(entry) {
      notificationCall("AddNotification", [
        NOTIFICATION_APP_ID,
        entry.notificationID,
        `{'title': <${variantString(`OpenCode: ${text(entry.form.title, 100) || "question"}`, 120)}>, ` +
        `'body': <${variantString(entry.body, 700)}>, ` +
        `'icon': <('themed', <['dialog-question']>)>, 'priority': <'urgent'>, ` +
        `'default-action': <${variantString(FOCUS_ACTION)}>, ` +
        `'default-action-target': <${variantString(entry.notificationID)}>, ` +
        `'buttons': <[{'label': <'Open question'>, 'action': <${variantString(FOCUS_ACTION)}>, ` +
        `'target': <${variantString(entry.notificationID)}>}]>}`,
      ])
    },
    async showSession(entry) {
      const output = await runQuiet(binaries.gdbus, [
        "call", "--session", "--dest", "org.gtk.Notifications",
        "--object-path", GTK_NOTIFICATIONS_OBJECT,
        "--method", "org.gtk.Notifications.AddNotification",
        NOTIFICATION_APP_ID, entry.notificationID,
        `{'title': <${variantString(`OpenCode: ${entry.title}`, 120)}>, ` +
        `'body': <${variantString(entry.body, 700)}>, ` +
        `'icon': <('themed', <['utilities-terminal']>)>, ` +
        `'default-action': <${variantString(FOCUS_ACTION)}>, ` +
        `'default-action-target': <${variantString(entry.notificationID)}>}`,
      ], 3000)
      return Boolean(output.trim())
    },
    async openDeck() {
      // The same serialized, tab-aware route as Super+O.
      return Boolean((await runQuiet(binaries.hotkey, ["--dispatch"], 95_000)).trim())
    },
    remove(id) {
      if (id) notificationCall("RemoveNotification", [NOTIFICATION_APP_ID, text(id, 300)])
    },
    async focus(entry) {
      if (!entry.tmuxSession || entry.sessionID === "global") return false
      const output = await runQuiet(binaries.busctl, [
        "--user",
        "call",
        NOTIFICATION_APP_ID,
        OCDECK_SWITCH_PATH,
        NOTIFICATION_APP_ID,
        "FocusTmux",
        "s",
        entry.tmuxSession,
      ])
      if (/\btrue\b/i.test(output)) return true

      return spawnQuiet(binaries.ptyxis, [
        "--standalone",
        "--new-window",
        "--title",
        `OpenCode - ${text(entry.sessionID, 80) || "attention"}`,
        `--working-directory=${entry.directory ?? location.directory}`,
        "--",
        binaries.tmux,
        "attach-session",
        "-t",
        entry.tmuxSession,
      ])
    },
    stop() {
      disposed = true
      clearTimeout(restartTimer)
      restartTimer = undefined
      const child = monitor
      monitor = undefined
      try { child?.kill("SIGTERM") } catch {}
    },
  }
}

export function createPermissionNotifyV2(context, rawOptions = {}) {
  const location = {
    directory: context.location.directory,
    ...(context.location.workspaceID ? { workspaceID: context.location.workspaceID } : {}),
  }
  const runtimeRoot = rawOptions.runtimeRoot ?? process.env.XDG_RUNTIME_DIR ??
    join(tmpdir(), `ocdeck-${process.getuid?.() ?? "user"}`)
  const stateDir = join(runtimeRoot, "ocdeck-permissions")
  const producer = `${process.pid}-${processStartTicks() || "unknown"}`
  const stateHash = createHash("sha256").update(locationKey(location)).digest("hex").slice(0, 20)
  const stateFile = join(stateDir, `v2-${stateHash}-${producer}.json`)
  const now = rawOptions.now ?? Date.now
  const setTimer = rawOptions.setTimeout ?? setTimeout
  const clearTimer = rawOptions.clearTimeout ?? clearTimeout
  const restoreMs = rawOptions.restoreMs ?? 250
  const notificationsEnabled = rawOptions.notifications !== false
  const desktop = rawOptions.desktop ?? createDesktopNotifications(location, rawOptions)
  const permissions = new Map()
  const forms = new Map()
  const statuses = new Map()
  const deletions = new Map()
  const knownSessions = new Set()
  let shownPermission = ""
  let sequence = 0
  let disposed = false

  const requestKey = (sessionID, id) => `${sessionID}\n${id}`
  const tmuxSession = (sessionID) => sessionID === "global" ? "" : `oc2-${sessionID}`

  const syncState = () => {
    try {
      const timestamp = now()
      for (const [sessionID, deletion] of deletions) {
        if (timestamp - deletion.updated > DELETE_TTL_MS) deletions.delete(sessionID)
      }
      if (disposed) {
        try { unlinkSync(stateFile) } catch {}
        return
      }

      mkdirSync(stateDir, { recursive: true, mode: 0o700 })
      const payload = {
        pid: process.pid,
        notifierVersion: NOTIFIER_VERSION,
        apiVersion: "0.0.0-beta-18707",
        processStartTicks: processStartTicks(),
        updated: timestamp,
        workspace: location.directory,
        workspaceID: location.workspaceID ?? null,
        permissions: [...permissions.values()].map((entry) => ({
          notificationID: entry.notificationID,
          tmuxSession: entry.tmuxSession,
          id: entry.request.id,
          sessionID: entry.sessionID,
          permission: text(entry.request.action, 80) || "permission",
          pattern: text(entry.request.resources, 1000),
          updated: entry.updated,
        })),
        questions: [...forms.values()].map((entry) => ({
          notificationID: entry.notificationID,
          tmuxSession: entry.tmuxSession,
          id: entry.form.id,
          sessionID: entry.sessionID,
          question: entry.body,
          updated: entry.updated,
        })),
        statuses: [...statuses].map(([sessionID, entry]) => ({
          sessionID,
          status: entry.status,
          updated: entry.updated,
        })),
        deletedSessions: [...deletions].map(([sessionID, entry]) => ({
          sessionID,
          updated: entry.updated,
        })),
      }
      const temporary = `${stateFile}.${process.pid}.${timestamp}.tmp`
      writeFileSync(temporary, JSON.stringify(payload), { encoding: "utf8", mode: 0o600 })
      renameSync(temporary, stateFile)
    } catch {}
  }

  const sessionHasPending = (sessionID) =>
    [...permissions.values(), ...forms.values()].some((entry) => entry.sessionID === sessionID)

  const showPermission = (entry) => {
    if (!notificationsEnabled || disposed) return
    try { desktop.showPermission(entry) } catch {}
  }

  const showNextPermission = () => {
    if (!notificationsEnabled || disposed || shownPermission || !permissions.size) return
    const entry = permissions.values().next().value
    shownPermission = entry.key
    showPermission(entry)
  }

  const deletePermission = (key, withdraw = true) => {
    const entry = permissions.get(key)
    if (!entry) return false
    clearTimer(entry.restoreTimer)
    permissions.delete(key)
    if (shownPermission === key) {
      shownPermission = ""
      if (withdraw && notificationsEnabled) {
        try { desktop.remove(entry.notificationID) } catch {}
      }
    }
    return true
  }

  const deleteForm = (key, withdraw = true) => {
    const entry = forms.get(key)
    if (!entry) return false
    clearTimer(entry.restoreTimer)
    forms.delete(key)
    if (withdraw && notificationsEnabled) {
      try { desktop.remove(entry.notificationID) } catch {}
    }
    return true
  }

  const addPermission = (request, eventSequence, show = true) => {
    if (
      !validPermissionRequest(request) ||
      deletions.has(request.sessionID)
    ) return false
    knownSessions.add(request.sessionID)
    const key = requestKey(request.sessionID, request.id)
    const updated = now()
    const existing = permissions.get(key)
    if (existing) {
      existing.request = request
      existing.body = permissionBody(request)
      existing.allowAlways = Array.isArray(request.save) && request.save.length > 0 &&
        request.action !== "signed_in_tabs_browser_file_upload"
      existing.updated = updated
      existing.sequence = Math.max(existing.sequence, eventSequence)
      statuses.set(request.sessionID, { status: "busy", updated })
      syncState()
      if (show && shownPermission === key) showPermission(existing)
      return true
    }

    permissions.set(key, {
      key,
      request,
      sessionID: request.sessionID,
      sequence: eventSequence,
      updated,
      body: permissionBody(request),
      allowAlways: Array.isArray(request.save) && request.save.length > 0 &&
        request.action !== "signed_in_tabs_browser_file_upload",
      notificationID: notificationID("permission", location, request.sessionID, request.id),
      tmuxSession: tmuxSession(request.sessionID),
      restoreTimer: undefined,
      acting: false,
    })
    statuses.set(request.sessionID, { status: "busy", updated })
    syncState()
    if (show) showNextPermission()
    return true
  }

  const addForm = (form, eventSequence) => {
    if (!form || typeof form.id !== "string" || typeof form.sessionID !== "string" || deletions.has(form.sessionID)) {
      return false
    }
    knownSessions.add(form.sessionID)
    const key = requestKey(form.sessionID, form.id)
    const updated = now()
    const existing = forms.get(key)
    if (existing) {
      existing.form = form
      existing.body = formBody(form)
      existing.updated = updated
      existing.sequence = Math.max(existing.sequence, eventSequence)
      statuses.set(form.sessionID, { status: "busy", updated })
      syncState()
      if (notificationsEnabled) {
        try { desktop.showForm(existing) } catch {}
      }
      return true
    }

    const entry = {
      key,
      form,
      sessionID: form.sessionID,
      sequence: eventSequence,
      updated,
      body: formBody(form),
      notificationID: notificationID("form", location, form.sessionID, form.id),
      tmuxSession: tmuxSession(form.sessionID),
      restoreTimer: undefined,
      acting: false,
    }
    forms.set(key, entry)
    statuses.set(form.sessionID, { status: "busy", updated })
    syncState()
    if (notificationsEnabled) {
      try { desktop.showForm(entry) } catch {}
    }
    return true
  }

  const clearPermissions = (sessionID, cutoff = Number.POSITIVE_INFINITY) => {
    let changed = false
    for (const [key, entry] of permissions) {
      if (entry.sessionID === sessionID && entry.sequence <= cutoff) {
        changed = deletePermission(key) || changed
      }
    }
    if (changed) {
      syncState()
      showNextPermission()
    }
  }

  const clearSession = (sessionID, cutoff = Number.POSITIVE_INFINITY) => {
    let changed = false
    for (const [key, entry] of permissions) {
      if (entry.sessionID === sessionID && entry.sequence <= cutoff) {
        changed = deletePermission(key) || changed
      }
    }
    for (const [key, entry] of forms) {
      if (entry.sessionID === sessionID && entry.sequence <= cutoff) {
        changed = deleteForm(key) || changed
      }
    }
    if (changed) {
      syncState()
      showNextPermission()
    }
  }

  const reconcilePermissions = async (sessionID, cutoff) => {
    if (
      typeof context.permission.list !== "function" ||
      typeof context.session?.get !== "function"
    ) return false
    let requests
    try {
      const session = await context.session.get({ sessionID })
      if (session?.id !== sessionID || !matchesLocation(session.location, location)) return false
      requests = await context.permission.list({ sessionID })
    } catch {
      return false
    }
    if (
      disposed ||
      deletions.has(sessionID) ||
      !Array.isArray(requests) ||
      !requests.every((request) => validPermissionRequest(request, sessionID))
    ) return false

    const current = new Set(requests.map((request) => requestKey(request.sessionID, request.id)))
    for (const [key, entry] of permissions) {
      if (entry.sessionID === sessionID && entry.sequence <= cutoff && !current.has(key)) {
        deletePermission(key)
      }
    }
    for (const request of requests) addPermission(request, cutoff, false)
    syncState()
    showNextPermission()
    return true
  }

  const scheduleRestore = (entry, kind) => {
    clearTimer(entry.restoreTimer)
    entry.restoreTimer = setTimer(() => {
      entry.restoreTimer = undefined
      if (disposed) return
      if (kind === "permission" && permissions.get(entry.key) === entry && shownPermission === entry.key) {
        showPermission(entry)
      } else if (kind === "form" && forms.get(entry.key) === entry && notificationsEnabled) {
        try { desktop.showForm(entry) } catch {}
      }
    }, restoreMs)
    entry.restoreTimer?.unref?.()
  }

  const findNotification = (id) => {
    for (const entry of permissions.values()) if (entry.notificationID === id) return ["permission", entry]
    for (const entry of forms.values()) if (entry.notificationID === id) return ["form", entry]
    return []
  }

  async function handleAction(action, id) {
    if (disposed) return
    const [kind, entry] = findNotification(id)
    if (!entry) return
    if (action === FOCUS_ACTION) {
      if (entry.acting) return
      entry.acting = true
      try { await desktop.focus(entry) } catch {}
      finally { entry.acting = false }
      scheduleRestore(entry, kind)
      return
    }
    if (kind !== "permission" || entry.acting) return
    const reply = action === APPROVE_ACTION
      ? "once"
      : action === APPROVE_ALWAYS_ACTION
        ? "always"
        : action === REJECT_ACTION
          ? "reject"
          : ""
    if (!reply || (reply === "always" && !entry.allowAlways)) return

    entry.acting = true
    const cutoff = sequence
    try {
      await context.permission.reply({
        sessionID: entry.sessionID,
        requestID: entry.request.id,
        decision: reply,
      })
      if (reply === "reject") clearPermissions(entry.sessionID, cutoff)
      else {
        deletePermission(entry.key)
        syncState()
        showNextPermission()
      }
    } catch {
      if (permissions.get(entry.key) === entry) scheduleRestore(entry, "permission")
    } finally {
      entry.acting = false
    }
  }

  if (notificationsEnabled) {
    try { desktop.start(handleAction) } catch {}
  }
  syncState()

  return {
    stateFile,
    handleAction,
    async recover() {
      if (disposed) return
      const cutoff = ++sequence
      const sessions = new Set(knownSessions)
      for (const entry of permissions.values()) sessions.add(entry.sessionID)
      for (const entry of forms.values()) sessions.add(entry.sessionID)
      for (const sessionID of sessions) await reconcilePermissions(sessionID, cutoff)
    },
    async handle(event) {
      if (disposed || !matchesLocation(event?.location, location)) return
      const eventSequence = ++sequence
      const data = event.data
      if (typeof data?.sessionID === "string" && event.type !== "session.deleted") {
        knownSessions.add(data.sessionID)
      }

      if (event.type === "permission.asked") {
        addPermission(data, eventSequence)
        return
      }
      if (event.type === "permission.replied") {
        if (typeof data?.sessionID !== "string" || typeof data?.requestID !== "string") return
        if (data.reply === "reject") clearPermissions(data.sessionID, eventSequence)
        else {
          deletePermission(requestKey(data.sessionID, data.requestID))
          syncState()
          showNextPermission()
        }
        return
      }
      if (event.type === "form.created") {
        addForm(data?.form, eventSequence)
        return
      }
      if (event.type === "form.replied" || event.type === "form.cancelled") {
        if (typeof data?.sessionID !== "string" || typeof data?.id !== "string") return
        deleteForm(requestKey(data.sessionID, data.id))
        syncState()
        return
      }
      if (event.type === "session.created") {
        if (typeof data?.sessionID === "string") deletions.delete(data.sessionID)
        syncState()
        return
      }
      if (event.type === "session.deleted") {
        if (typeof data?.sessionID !== "string") return
        clearSession(data.sessionID, eventSequence)
        statuses.delete(data.sessionID)
        knownSessions.delete(data.sessionID)
        deletions.set(data.sessionID, { updated: now(), sequence: eventSequence })
        syncState()
        return
      }
      if (event.type === "session.status" || event.type === "session.idle") {
        const sessionID = data?.sessionID
        if (typeof sessionID !== "string" || deletions.has(sessionID)) return
        await reconcilePermissions(sessionID, eventSequence)
        if (disposed || deletions.has(sessionID)) return
        const status = event.type === "session.idle" ? "idle" : text(data?.status?.type, 40)
        statuses.set(sessionID, {
          status: status === "idle" && sessionHasPending(sessionID) ? "busy" : status || "busy",
          updated: now(),
        })
        syncState()
      }
    },
    async dispose() {
      if (disposed) return
      disposed = true
      for (const entry of permissions.values()) {
        clearTimer(entry.restoreTimer)
        if (notificationsEnabled) {
          try { desktop.remove(entry.notificationID) } catch {}
        }
      }
      for (const entry of forms.values()) {
        clearTimer(entry.restoreTimer)
        if (notificationsEnabled) {
          try { desktop.remove(entry.notificationID) } catch {}
        }
      }
      permissions.clear()
      forms.clear()
      statuses.clear()
      deletions.clear()
      knownSessions.clear()
      try { desktop.stop() } catch {}
      syncState()
    },
  }
}
