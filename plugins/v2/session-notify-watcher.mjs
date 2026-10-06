import { execFile } from "node:child_process"
import { readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import { setTimeout as delay } from "node:timers/promises"
import { promisify } from "node:util"
import { OpenCode } from "@opencode/client"
import { Service } from "@opencode/client/service"

import { createDesktopNotifications } from "./permission-notify-core.js"
import { createSessionNotifier } from "./session-notify-core.js"

const exec = promisify(execFile)
const controller = new AbortController()
for (const signal of ["SIGTERM", "SIGINT"]) process.on(signal, () => controller.abort())

const configFile = join(process.env.XDG_CONFIG_HOME || join(homedir(), ".config"), "opencode/cli.json")
function enabled() {
  try {
    return JSON.parse(readFileSync(configFile, "utf8")).attention?.notifications !== false
  } catch (error) {
    return error.code === "ENOENT"
  }
}

async function terminalState(sessionID) {
  const name = `oc2-${sessionID}`
  const options = { timeout: 2000, maxBuffer: 256 * 1024, encoding: "utf8" }
  const [sessions, clients] = await Promise.all([
    exec("/usr/bin/tmux", ["list-sessions", "-F", "#{session_name}"], options).catch(() => ({ stdout: "" })),
    exec("/usr/bin/tmux", ["list-clients", "-F", "#{session_name}\t#{client_flags}"], options).catch(() => ({ stdout: "" })),
  ])
  return {
    exists: sessions.stdout.split("\n").includes(name),
    focused: clients.stdout.split("\n").some((line) => {
      const [session, flags = ""] = line.split("\t")
      return session === name && flags.split(",").includes("focused")
    }),
  }
}

let client
const notifier = createSessionNotifier({
  getSession: (sessionID) => client.session.get({ sessionID }, { signal: AbortSignal.timeout(5000) }),
  desktop: createDesktopNotifications({ directory: homedir() }),
  enabled,
  terminalState,
  // Keep session contents and authentication data out of the service journal.
  onError: () => console.error("OC Deck completion notification delivery failed"),
})
let pending = Promise.resolve()
try {
  while (!controller.signal.aborted) {
    try {
      // Discovery only: never create another service or history store.
      const endpoint = await Service.discover()
      if (!endpoint) throw new Error("Managed service unavailable")
      client = OpenCode.make({ baseUrl: endpoint.url, headers: Service.headers(endpoint) })
      for await (const event of client.event.subscribe({ signal: controller.signal })) {
        if (event.type === "server.connected") console.log("OC Deck completion notifications connected")
        if (!event.type.startsWith("session.execution.") && event.type !== "session.deleted") continue
        // Drain the shared event stream promptly; desktop delivery can take seconds.
        pending = pending.then(() => notifier.handle(event)).catch(() => {
          console.error("OC Deck completion event handling failed")
        })
      }
    } catch {
      if (!controller.signal.aborted) console.error("OC Deck completion stream disconnected; reconnecting")
    }
    await pending
    if (!controller.signal.aborted) await delay(2000, undefined, { signal: controller.signal }).catch(() => {})
  }
} finally {
  notifier.dispose()
  await pending
}
