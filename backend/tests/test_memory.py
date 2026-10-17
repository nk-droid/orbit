import json
import sys
from pathlib import Path

import pytest
from livekit.agents import mcp

from agent import Assistant
from agent_tools.memory import (
    NO_MEMORY,
    NOTHING_YET,
    TOOLS,
    _texts,
    connect_memory,
    open_memory,
)

STUB = str(Path(__file__).parent / "mem0_stub.py")


def stub_server(store: Path) -> mcp.MCPServerStdio:
    return mcp.MCPServerStdio(
        command=sys.executable,
        args=[STUB, str(store)],
        client_session_timeout_seconds=15,
    )


@pytest.mark.parametrize(
    "answer",
    [
        # As Mem0 returns it, wrapped once more by MCP and by LiveKit.
        json.dumps(
            {
                "type": "text",
                "text": json.dumps(
                    {"results": [{"id": "1", "memory": "Prefers window seats"}]}
                ),
            }
        ),
        json.dumps({"results": [{"id": "1", "memory": " Prefers window seats "}]}),
        json.dumps([{"id": "1", "memory": "Prefers window seats"}]),
        [{"type": "text", "text": '{"results": [{"memory": "Prefers window seats"}]}'}],
    ],
)
def test_memories_are_read_out_of_the_servers_answer(answer):
    assert _texts(answer) == ["Prefers window seats"]


@pytest.mark.parametrize("answer", ["not json", "{}", '{"results": []}', None, 7])
def test_an_answer_without_memories_gives_none(answer):
    assert _texts(answer) == []


async def test_without_a_key_orbit_says_it_has_no_memory(monkeypatch):
    monkeypatch.delenv("MEM0_API_KEY", raising=False)

    assert await connect_memory() == (None, NO_MEMORY)


async def test_what_mem0_holds_is_put_in_front_of_the_model(tmp_path):
    store = tmp_path / "memories.json"
    store.write_text(
        json.dumps(
            [
                {"id": "a1", "memory": "Prefers window seats on flights"},
                {"id": "b2", "memory": "Sister is called Meera"},
            ]
        )
    )

    toolset, note = await open_memory(stub_server(store))
    try:
        assert sorted(tool.id for tool in toolset.tools) == sorted(TOOLS)
        assert "- Prefers window seats on flights" in note
        assert "- Sister is called Meera" in note
        instructions = Assistant(toolset, note).instructions
        assert "\n- Sister is called Meera\n" in instructions
        # The whole prompt still dedents as one block.
        assert instructions.startswith("You are Orbit")
    finally:
        await toolset.aclose()


async def test_an_empty_memory_is_said_to_be_empty(tmp_path):
    toolset, note = await open_memory(stub_server(tmp_path / "memories.json"))
    try:
        assert note == NOTHING_YET
    finally:
        await toolset.aclose()


async def test_a_memory_server_that_cannot_be_reached_leaves_orbit_without_memory():
    unreachable = mcp.MCPServerStdio(
        command=sys.executable,
        args=["-c", "raise SystemExit(1)"],
        client_session_timeout_seconds=5,
    )

    assert await open_memory(unreachable) == (None, NO_MEMORY)


def test_without_memory_the_model_is_told_so():
    instructions = Assistant().instructions

    assert "You have no long-term memory in this conversation" in instructions
