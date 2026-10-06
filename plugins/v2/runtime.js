import { setTimeout as delay } from "node:timers/promises"

export function locationKey(location) {
  return JSON.stringify([
    typeof location?.directory === "string" ? location.directory : "",
    typeof location?.workspaceID === "string" ? location.workspaceID : null,
  ])
}

export function matchesLocation(eventLocation, pluginLocation) {
  if (!eventLocation || typeof eventLocation.directory !== "string") return false
  return eventLocation.directory === pluginLocation.directory &&
    (eventLocation.workspaceID ?? null) === (pluginLocation.workspaceID ?? null)
}

export function startEventSubscription(eventDomain, onEvent, rawOptions = {}) {
  const retryMs = rawOptions.retryMs ?? 1000
  const cleanupTimeoutMs = rawOptions.cleanupTimeoutMs ?? 1000
  const onError = rawOptions.onError ?? (() => {})
  const onReconnect = rawOptions.onReconnect ?? (() => {})
  const controller = new AbortController()
  let iterator
  let stopped = false
  const report = (error) => {
    try { onError(error) } catch {}
  }

  const done = (async () => {
    while (!stopped) {
      let current
      try {
        current = eventDomain.subscribe({ signal: controller.signal })[Symbol.asyncIterator]()
        iterator = current
        let pending = Promise.resolve(current.next())
        // Start the SSE request before taking a recovery snapshot so events
        // racing the snapshot are queued on the new stream.
        void pending.catch(() => {})
        if (!stopped) {
          try {
            await onReconnect()
          } catch (error) {
            report(error)
          }
        }
        while (!stopped) {
          const next = await pending
          if (next.done) break
          pending = Promise.resolve(current.next())
          void pending.catch(() => {})
          try {
            await onEvent(next.value)
          } catch (error) {
            report(error)
          }
        }
      } catch (error) {
        if (!stopped) report(error)
      } finally {
        if (iterator === current) iterator = undefined
        if (current && !stopped) {
          try { await current.return?.() } catch {}
        }
      }

      if (!stopped) {
        try {
          await delay(retryMs, undefined, { signal: controller.signal })
        } catch {}
      }
    }
  })()

  return {
    done,
    async dispose() {
      if (stopped) return
      stopped = true
      controller.abort()
      const closing = Promise.resolve()
        .then(() => iterator?.return?.())
        .catch(() => {})

      let timer
      const timeout = new Promise((resolve) => {
        timer = setTimeout(resolve, cleanupTimeoutMs)
      })
      await Promise.race([Promise.allSettled([done, closing]), timeout])
      clearTimeout(timer)
    },
  }
}
