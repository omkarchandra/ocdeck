/** GNOME delivery for completion events, independent of terminal OSC support. */
export function createSessionNotifier({ getSession, desktop, enabled, terminalState, onError = () => {} }) {
  const terminal = new Set()
  const notifications = new Map()
  let disposed = false

  desktop.start(async (action, id) => {
    if (action !== "opencode.permission.focus" || disposed) return
    const entry = notifications.get(id)
    if (!entry) return
    const state = await terminalState(entry.sessionID)
    if (state.exists) await desktop.focus(entry)
    else await desktop.openDeck()
  })

  return {
    async handle(event) {
      const sessionID = event?.data?.sessionID
      if (disposed || typeof sessionID !== "string" || !sessionID.startsWith("ses")) return
      if (event.type === "session.execution.started") {
        terminal.delete(sessionID)
        return
      }
      if (event.type === "session.deleted") {
        terminal.delete(sessionID)
        const id = `ocdeck-session-${sessionID}`
        if (notifications.delete(id)) desktop.remove(id)
        return
      }
      // A permission wait or user cancellation is not a completed response.
      if (!["session.execution.succeeded", "session.execution.failed"].includes(event.type)) return
      if (terminal.has(sessionID)) return
      terminal.add(sessionID)
      if (terminal.size > 2048) terminal.delete(terminal.values().next().value)
      try {
        if (!enabled()) return
        const session = await getSession(sessionID)
        if (disposed || !session || session.id !== sessionID || session.parentID) return
        const state = await terminalState(sessionID)
        if (disposed || state.focused) return
        const entry = {
          notificationID: `ocdeck-session-${sessionID}`,
          sessionID,
          tmuxSession: `oc2-${sessionID}`,
          directory: session.location?.directory,
          title: session.title || "Session",
          body: event.type === "session.execution.failed"
            ? `Session failed: ${event.data.error?.message || "Open the session for details."}`
            : "Session finished. Click to return to it.",
        }
        if (await desktop.showSession(entry)) {
          notifications.set(entry.notificationID, entry)
          if (notifications.size > 256) {
            const oldest = notifications.keys().next().value
            notifications.delete(oldest)
            desktop.remove(oldest)
          }
        } else {
          terminal.delete(sessionID)
          onError(new Error("Desktop notification delivery failed"))
        }
      } catch (error) {
        terminal.delete(sessionID)
        onError(error)
      }
    },
    dispose() {
      disposed = true
      desktop.stop()
    },
  }
}
