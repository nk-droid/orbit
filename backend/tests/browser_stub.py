"""A stand-in for Playwright's MCP server, run as a child process by the tests.

It offers the same tool definitions Orbit uses (agent_tools/browser_tools.json) and
answers from a script, so the agent can be driven through LiveKit's real MCP client
without opening a browser.

    python browser_stub.py <shown|hidden> <script.json> <calls.jsonl> [browser]

The script maps a tool name to the text it answers with. Every call is appended to
the calls file together with the mode and the name of the browser the server is
attached to, which is how a test sees which browser was actually used.
"""

import asyncio
import json
import sys
from pathlib import Path

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

MODE, SCRIPT, CALLS = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
BROWSER = sys.argv[4] if len(sys.argv) > 4 else ""
DEFINITIONS = json.loads(
    (
        Path(__file__).parents[1] / "src" / "agent_tools" / "browser_tools.json"
    ).read_text()
)

server = Server("playwright-stub")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [types.Tool(**definition) for definition in DEFINITIONS]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    with CALLS.open("a") as calls:
        call = {"mode": MODE, "browser": BROWSER, "tool": name, "arguments": arguments}
        calls.write(json.dumps(call) + "\n")
    script = json.loads(SCRIPT.read_text())
    return [types.TextContent(type="text", text=script.get(name, "### Result\nDone."))]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


asyncio.run(main())
