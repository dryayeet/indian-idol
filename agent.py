"""LangGraph ReAct agent that drives the Spotify MCP server through OpenRouter.

    python agent.py "songs that feel like driving away from my hometown for the last time"
    python agent.py --selfcheck      lists the tools over real MCP stdio, no LLM call

Needs OPENROUTER_API_KEY in .env, plus the Spotify variables spotify_mcp.py reads.
Pick a different model with OPENROUTER_MODEL; it must support tool calling.

The agent speaks MCP: it launches spotify_mcp.py as a subprocess over stdio and reads
the tool list from the server, so tools added there appear here with no change to this
file.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
SERVERS = {
    "spotify": {
        "command": sys.executable,
        "args": [os.path.join(HERE, "spotify_mcp.py")],
        "transport": "stdio",
    }
}

SYSTEM = """You are a music agent with access to the user's Spotify account.

Requests are usually affective rather than categorical: a feeling or a scene, not a
genre. Translate them into the emotional coordinates search_by_feel takes, where
valence is sad to happy, energy is calm to intense, and acousticness is produced to
acoustic, all from 0 to 1. Put anything genre-like, era-like, or artist-like into
extra_query instead.

Search before you conclude anything about what exists. Only create a playlist when the
user asks for one; otherwise report the tracks. When you build one, say what you were
aiming for in the description."""


def _llm():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("missing OPENROUTER_API_KEY — copy .env.example to .env")
    return ChatOpenAI(
        model=MODEL,
        api_key=key,
        base_url="https://openrouter.ai/api/v1",
        max_tokens=4000,
    )


def _parts(message) -> list[tuple[str, str]]:
    """One message as [(kind, text)], where kind is 'tool' or 'text'."""
    if calls := getattr(message, "tool_calls", None):
        return [("tool", f"{c['name']}({c['args']})") for c in calls]
    if message.type != "ai" or not message.content:
        return []
    text = message.content
    if isinstance(text, list):  # content blocks when the model thinks
        text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
    return [("text", text.strip())] if text.strip() else []


async def collect(question: str) -> list[tuple[str, str]]:
    """Run the agent to completion. Returns every tool call and reply, in order."""
    tools = await MultiServerMCPClient(SERVERS).get_tools()
    graph = create_react_agent(_llm(), tools, prompt=SYSTEM)
    out = []
    async for step in graph.astream({"messages": [("user", question)]}, stream_mode="values"):
        out += _parts(step["messages"][-1])
    return out


async def main(question: str) -> None:
    for kind, text in await collect(question):
        print(f"  -> {text}" if kind == "tool" else f"\n{text}")


async def _selfcheck() -> None:
    tools = await MultiServerMCPClient(SERVERS).get_tools()
    names = sorted(t.name for t in tools)
    assert names == [
        "create_playlist",
        "get_lyrics",
        "recently_played",
        "search_by_feel",
        "top_tracks",
    ], names
    feel = next(t for t in tools if t.name == "search_by_feel")
    assert "valence" in feel.args_schema["properties"], feel.args_schema
    assert "affective targets" in feel.description, feel.description
    print(f"ok — {len(names)} tools over MCP stdio, model {MODEL}")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        asyncio.run(_selfcheck())
    elif len(sys.argv) > 1:
        asyncio.run(main(" ".join(sys.argv[1:])))
    else:
        raise SystemExit('ask it something: python agent.py "..."')
