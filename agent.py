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

MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-5.4-mini")
SERVERS = {
    "spotify": {
        "command": sys.executable,
        "args": [os.path.join(HERE, "spotify_mcp.py")],
        "transport": "stdio",
    }
}

SYSTEM = """You are a music agent with access to the user's Spotify account.

Requests are usually affective rather than categorical: a feeling or a scene, not a
genre. Your job is to turn that feeling into words a song might actually be called.

search_by_feel matches song, artist, and album names, so `description` should be two
to five title-like words, not a sentence and not a mood description. "driving away
from my hometown for the last time" is a request, not a query; "leaving home" and
"small town" are queries. The three numbers only nudge it and cannot search alone.

search_by_feel always returns something, even for nonsense, so read the results before
trusting them. If they do not fit, search again with different words rather than the
same ones, and try more than one phrasing when the request is vague.

When the request is about what a song *says* rather than what it is called, use
search_by_lyrics instead. It reads the words of every candidate, so it is slower and
it can legitimately come back empty, which is a real answer and not a failure. Give it
the phrase the lyrics should contain, and widen `search_terms` if it finds nothing.

When you list tracks, give each one as a markdown link using the `url` the tool
returned, like [Title](url), followed by the artist. Never build a link yourself from
an id; use the url as given.

To read what someone has been listening to, use listening_lyrics: it collects the
tracks and their words in one call. Do not call get_lyrics once per track for this;
that is one round trip per song and it fills the conversation with lyrics.

Only create a playlist when the user asks for one. When you build one, say what you
were aiming for in its description."""


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


async def build(checkpointer=None):
    """Compile the agent. Pass a checkpointer to give it memory across turns."""
    tools = await MultiServerMCPClient(SERVERS).get_tools()
    return create_react_agent(_llm(), tools, prompt=SYSTEM, checkpointer=checkpointer)


async def run(question: str, on_part, checkpointer=None, thread_id: str | None = None):
    """Run one turn, calling on_part(kind, text) as each tool call and reply arrives.

    With a checkpointer and a thread_id, earlier turns on that thread are in scope,
    so follow-ups like "make that a playlist" resolve.
    """
    graph = await build(checkpointer)
    config = {"configurable": {"thread_id": thread_id}} if checkpointer else None
    async for step in graph.astream(
        {"messages": [("user", question)]}, config, stream_mode="values"
    ):
        for part in _parts(step["messages"][-1]):
            on_part(*part)


async def collect(question: str, **kw) -> list[tuple[str, str]]:
    """run(), buffered. Returns every tool call and reply, in order."""
    out: list[tuple[str, str]] = []
    await run(question, lambda kind, text: out.append((kind, text)), **kw)
    return out


async def main(question: str) -> None:
    await run(question, lambda kind, text: print(f"  -> {text}" if kind == "tool" else f"\n{text}"))


async def _selfcheck() -> None:
    tools = await MultiServerMCPClient(SERVERS).get_tools()
    names = sorted(t.name for t in tools)
    # a subset, not an exact list: adding a tool to the server should not fail this
    assert {"search_by_feel", "create_playlist", "get_lyrics"} <= set(names), names
    feel = next(t for t in tools if t.name == "search_by_feel")
    schema = feel.args_schema["properties"]
    assert "valence" in schema and "description" in schema, schema
    assert feel.args_schema.get("required") == ["description"], feel.args_schema
    assert "matching a mood" in feel.description, feel.description
    print(f"ok — {len(names)} tools over MCP stdio, model {MODEL}")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        asyncio.run(_selfcheck())
    elif len(sys.argv) > 1:
        asyncio.run(main(" ".join(sys.argv[1:])))
    else:
        raise SystemExit('ask it something: python agent.py "..."')
