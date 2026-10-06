import { createInterface } from "node:readline"
import { once } from "node:events"
import { OpenCode } from "@opencode/client"
import { Service } from "@opencode/client/service"
import { createReadAPI } from "./read-api-core.js"

const request = createReadAPI({
  discover: () => Service.discover({ version: "2.0.14" }),
  makeClient: endpoint => OpenCode.make({ baseUrl: endpoint.url, headers: Service.headers(endpoint) }),
})
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity })
for await (const line of lines) {
  let input
  let output
  try {
    if (Buffer.byteLength(line) > 65536) throw new Error("Read request too large")
    input = JSON.parse(line)
    const data = await request(input)
    if (Buffer.byteLength(JSON.stringify(data ?? null)) > 1024 * 1024)
      throw new Error("Read response too large")
    output = { id: input.id, data: data ?? null }
  } catch {
    output = { id: input?.id, error: "Managed OpenCode read unavailable" }
  }
  if (!process.stdout.write(JSON.stringify(output) + "\n")) await once(process.stdout, "drain")
}
// EOF means the owning dashboard/watcher exited. Do not leave an orphan helper.
process.exit(0)
