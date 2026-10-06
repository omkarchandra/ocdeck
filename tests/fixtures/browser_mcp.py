"""Deterministic local MCP fixture; it never opens or controls a browser."""
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "ocdeck-browser-fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "browser_tabs", "description": "List deterministic test tabs",
                              "inputSchema": {"type": "object", "properties": {"action": {"type": "string", "enum": ["list"]}}, "required": ["action"]}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "Test browser has one tab: about:blank"}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
