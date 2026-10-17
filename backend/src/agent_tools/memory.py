"""Long-term memory for the agent, kept by Mem0 and reached through its MCP server.

LiveKit connects to the server and offers its memory tools to the model, which
decides when to save something and when to look something up. What Mem0 already
holds is also read once when a conversation starts and put in the instructions, so
the agent knows the user without spending a tool call on every question.
See https://docs.mem0.ai/platform/mem0-mcp and
https://docs.livekit.io/agents/logic/tools/mcp/
"""

import json
import logging
import os
from typing import Any

from livekit.agents import mcp

logger = logging.getLogger("orbit.memory")

MEM0_URL = "https://mcp.mem0.ai/mcp"
# The server also offers bulk deletion and account-wide listings. Orbit leaves those
# out: forgetting is done one memory at a time, when the user asks.
TOOLS = (
    "add_memory",
    "search_memories",
    "get_memories",
    "update_memory",
    "delete_memory",
)
# How many memories are read into the instructions when a conversation starts.
RECALL_LIMIT = 30
_CALL_TIMEOUT_SECONDS = 15

NO_MEMORY = (
    "You have no long-term memory in this conversation: nothing from earlier "
    "conversations is available and nothing can be saved for later ones. If the "
    "user asks you to remember something, say so."
)
NOTHING_YET = "You do not remember anything about the user yet."


def _texts(answer: Any) -> list[str]:
    """The memories in a get_memories answer, whatever wrapping it arrives in."""
    while isinstance(answer, str):
        try:
            answer = json.loads(answer)
        except ValueError:
            return []
    if isinstance(answer, dict):
        # An MCP text item wraps the server's JSON; Mem0 puts memories in "results".
        for key in ("text", "results", "memories"):
            if key in answer:
                return _texts(answer[key])
        return []
    if not isinstance(answer, list):
        return []
    texts = []
    for item in answer:
        if isinstance(item, dict) and isinstance(item.get("memory"), str):
            texts.append(item["memory"].strip())
        elif isinstance(item, dict) and "text" in item:
            texts.extend(_texts(item["text"]))
    return [text for text in texts if text]


def _note(remembered: list[str]) -> str:
    if not remembered:
        return NOTHING_YET
    lines = "\n".join(f"- {text}" for text in remembered)
    return f"What you remember about the user from earlier conversations:\n{lines}"


async def open_memory(server: mcp.MCPServer) -> tuple[mcp.MCPToolset | None, str]:
    """Connects to a memory server. Returns its toolset and a note for the model
    saying what it remembers, or no toolset and the reason if it cannot be reached."""
    toolset = mcp.MCPToolset(id="mem0", mcp_server=server)
    try:
        await toolset.setup()
        listing = next(tool for tool in toolset.tools if tool.id == "get_memories")
        remembered = _texts(await listing({"page_size": RECALL_LIMIT}))
    except Exception as e:
        logger.warning("Mem0 is not available: %s", e)
        await toolset.aclose()
        return None, NO_MEMORY
    return toolset, _note(remembered[:RECALL_LIMIT])


async def connect_memory() -> tuple[mcp.MCPToolset | None, str]:
    """The agent's memory, if MEM0_API_KEY is set and Mem0 answers."""
    key = os.getenv("MEM0_API_KEY")
    if not key:
        return None, NO_MEMORY
    server = mcp.MCPServerHTTP(
        MEM0_URL,
        transport_type="streamable_http",
        allowed_tools=list(TOOLS),
        headers={"Authorization": f"Bearer {key}"},
        client_session_timeout_seconds=_CALL_TIMEOUT_SECONDS,
    )
    return await open_memory(server)
