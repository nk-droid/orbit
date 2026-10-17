"""A stand-in for Mem0's MCP server, run as a child process by the tests.

It offers the memory tools Orbit uses, with arguments modelled on Mem0's own, and
keeps the memories in a JSON file the test can read. That lets the agent be driven
through LiveKit's real MCP client without a Mem0 account.

    python mem0_stub.py <memories.json>
"""

import asyncio
import json
import sys
import uuid
from pathlib import Path

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

STORE = Path(sys.argv[1])
OPTIONAL_ID = {"type": "string", "description": "Leave empty to use the default."}
TOOLS = [
    types.Tool(
        name="add_memory",
        description="Store a new preference, fact, or conversation snippet.",
        inputSchema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Plain sentence summarizing what to store.",
                },
                "user_id": OPTIONAL_ID,
                "agent_id": OPTIONAL_ID,
                "metadata": {"type": "object"},
            },
            "required": ["text"],
        },
    ),
    types.Tool(
        name="search_memories",
        description="Run a semantic search over existing memories.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language description of what to find.",
                },
                "filters": {"type": "object"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="get_memories",
        description="List memories via structured filters or pagination.",
        inputSchema={
            "type": "object",
            "properties": {
                "filters": {"type": "object"},
                "page": {"type": "integer"},
                "page_size": {"type": "integer"},
            },
        },
    ),
    types.Tool(
        name="update_memory",
        description="Overwrite an existing memory's text.",
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["memory_id", "text"],
        },
    ),
    types.Tool(
        name="delete_memory",
        description="Delete one memory after confirming its memory_id.",
        inputSchema={
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
        },
    ),
]

server = Server("mem0-stub")


def load() -> list[dict]:
    return json.loads(STORE.read_text()) if STORE.exists() else []


def answer(memories: list[dict], arguments: dict, name: str) -> dict:
    if name == "add_memory":
        added = {"id": uuid.uuid4().hex[:8], "memory": arguments["text"]}
        memories.append(added)
        return {"results": [{**added, "event": "ADD"}]}
    if name in ("search_memories", "get_memories"):
        # No real semantic search here: every memory comes back, as a small store would.
        return {"results": memories}
    found = next((m for m in memories if m["id"] == arguments["memory_id"]), None)
    if found is None:
        raise ValueError("Memory not found.")
    if name == "update_memory":
        found["memory"] = arguments["text"]
        return {"message": "Memory updated."}
    memories.remove(found)
    return {"message": "Memory deleted."}


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    memories = load()
    result = answer(memories, arguments, name)
    STORE.write_text(json.dumps(memories))
    return [types.TextContent(type="text", text=json.dumps(result))]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


asyncio.run(main())
