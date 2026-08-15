"""LangGraph ReAct agent that drives the Spotify MCP server.

    python agent.py "songs that feel like driving away from my hometown for the last time"
    python agent.py --selfcheck      lists the tools over real MCP stdio, no LLM call

Two providers, chosen with LLM_PROVIDER in .env:

    openrouter  (default)  OPENROUTER_API_KEY, OPENROUTER_MODEL
    gemini                 GEMINI_API_KEY, GEMINI_MODEL

Whichever model you pick must support tool calling. The Spotify variables that
spotify_mcp.py reads are needed either way.

The agent speaks MCP: it launches spotify_mcp.py as a subprocess over stdio and reads
the tool list from the server, so tools added there appear here with no change to this
file.
"""

import asyncio
import os
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

PROVIDER = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()
if PROVIDER == "gemini":
    MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
    KEY_VAR = "GEMINI_API_KEY"
else:
    MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-5.4-mini")
    KEY_VAR = "OPENROUTER_API_KEY"
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
    key = os.environ.get(KEY_VAR)
    if not key:
        raise SystemExit(f"missing {KEY_VAR} — copy .env.example to .env and fill it in")
    if PROVIDER == "gemini":
        # imported here so OpenRouter users do not need the Google package installed
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=MODEL, google_api_key=key, max_tokens=4000)
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


@asynccontextmanager
async def build(checkpointer=None):
    """Compile the agent over ONE MCP session, held open for the whole turn.

    Without the session, every tool call opens its own stdio connection: a new
    interpreter, re-imports, a fresh handshake, about 3.3s of overhead on work that
    takes 0.4s. Inside the session, tool calls cost what the work costs.
    """
    async with MultiServerMCPClient(SERVERS).session("spotify") as session:
        tools = await load_mcp_tools(session)
        yield create_react_agent(_llm(), tools, prompt=SYSTEM, checkpointer=checkpointer)


def _token(chunk) -> str:
    """Text out of a streamed model chunk, ignoring tool-call and tool-result chunks."""
    if type(chunk).__name__ != "AIMessageChunk":
        return ""
    text = chunk.content
    if isinstance(text, list):  # content blocks when the model thinks
        text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
    return text or ""


async def run(question: str, on_part, checkpointer=None, thread_id: str | None = None):
    """Run one turn, calling on_part(kind, text) as output arrives.

    kind is "tool" for a complete tool call, or "token" for a piece of the reply as
    the model writes it. With a checkpointer and a thread_id, earlier turns on that
    thread are in scope, so follow-ups like "make that a playlist" resolve.
    """
    config = {"configurable": {"thread_id": thread_id}} if checkpointer else None
    async with build(checkpointer) as graph:
        async for mode, payload in graph.astream(
            {"messages": [("user", question)]},
            config,
            stream_mode=["values", "messages"],
        ):
            if mode == "messages":  # token by token, as the model writes
                if text := _token(payload[0]):
                    on_part("token", text)
            elif tool_calls := [p for p in _parts(payload["messages"][-1]) if p[0] == "tool"]:
                for part in tool_calls:  # replies come from the token stream instead
                    on_part(*part)


async def collect(question: str, **kw) -> list[tuple[str, str]]:
    """run(), buffered. Tokens are joined back into whole replies."""
    out: list[tuple[str, str]] = []

    def add(kind: str, text: str) -> None:
        if kind == "token" and out and out[-1][0] == "text":
            out[-1] = ("text", out[-1][1] + text)
        else:
            out.append(("text" if kind == "token" else kind, text))

    await run(question, add, **kw)
    return [(kind, text.strip()) for kind, text in out if text.strip()]


async def main(question: str) -> None:
    def show(kind: str, text: str) -> None:
        print(f"\n  -> {text}\n" if kind == "tool" else text, end="", flush=True)

    await run(question, show)
    print()


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
    assert PROVIDER in ("openrouter", "gemini"), f"unknown LLM_PROVIDER {PROVIDER!r}"
    print(f"ok — {len(names)} tools over MCP stdio, provider {PROVIDER}, model {MODEL}")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        asyncio.run(_selfcheck())
    elif len(sys.argv) > 1:
        asyncio.run(main(" ".join(sys.argv[1:])))
    else:
        raise SystemExit('ask it something: python agent.py "..."')
