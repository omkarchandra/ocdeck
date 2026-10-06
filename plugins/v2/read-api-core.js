// Persistent, discovery-only reads for OC Deck. Background refreshes must never
// invoke Service.ensure() or repeatedly try to start a failed systemd service.
export const READ_OPERATIONS = new Set([
  "v2.health.get", "v2.session.list", "v2.session.active", "v2.project.list",
  "v2.debug.location.list", "v2.permission.request.list", "v2.form.request.list",
  "v2.agent.list", "v2.mcp.list", "v2.session.message.list", "v2.shell.list",
])
const LOCATION_OPERATIONS = new Set([
  "v2.permission.request.list", "v2.form.request.list", "v2.agent.list", "v2.mcp.list",
  "v2.shell.list",
])

export function createReadAPI({ discover, makeClient, now = Date.now }) {
  let client
  let refreshAt = 0
  let retryAt = 0
  let failures = 0

  return async function request({ operation, params = {}, location, timeoutMs = 10000 }) {
    if (!READ_OPERATIONS.has(operation)) throw new Error("Unsupported read operation")
    if (LOCATION_OPERATIONS.has(operation) &&
        (!location || typeof location.directory !== "string" || !location.directory.startsWith("/") ||
         Object.keys(location).some(key => key !== "directory"))) {
      throw new Error("An explicit directory location is required")
    }
    if (now() < retryAt) throw new Error("Managed OpenCode service unavailable; retry delayed")
    try {
      if (!client || now() >= refreshAt) {
        const endpoint = await discover()
        if (!endpoint) throw new Error("Managed OpenCode service unavailable")
        client = makeClient(endpoint)
        refreshAt = now() + 15000
      }
      const input = { ...params }
      if (input.limit !== undefined) {
        input.limit = Number(input.limit)
        if (!Number.isInteger(input.limit) || input.limit < 1 || input.limit > 1000)
          throw new Error("Invalid read limit")
      }
      const options = { signal: AbortSignal.timeout(Math.max(1, Math.min(timeoutMs, 30000))) }
      let result
      switch (operation) {
        case "v2.health.get": result = await client.server.info(options); break
        case "v2.session.list": result = await client.session.list(input, options); break
        case "v2.session.active": result = { data: await client.session.active(options) }; break
        case "v2.project.list": result = await client.project.list(options); break
        case "v2.debug.location.list": result = await client.debug.location.list(options); break
        case "v2.permission.request.list": result = await client.permission.request.list({ location }, options); break
        case "v2.form.request.list": result = await client.form.list({ location }, options); break
        case "v2.agent.list": result = await client.agent.list({ location }, options); break
        case "v2.mcp.list": result = await client.mcp.list({ location }, options); break
        case "v2.session.message.list": result = await client.message.list(input, options); break
        case "v2.shell.list": result = await client.shell.list({ location }, options); break
      }
      failures = 0
      return result
    } catch {
      client = undefined
      retryAt = now() + Math.min(30000, 2000 * 2 ** Math.min(failures++, 4))
      // Neither private endpoint credentials nor API response bodies enter logs.
      throw new Error("Managed OpenCode read failed; retry delayed")
    }
  }
}
