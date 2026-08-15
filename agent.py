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
from langchain_core.messages import ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

def _pick_provider() -> tuple[str, bool]:
    """Which LLM to use. LLM_PROVIDER wins; otherwise whichever key is present.

    OpenRouter is preferred when both keys exist. Returns (provider, was_explicit).
    """
    if chosen := os.environ.get("LLM_PROVIDER", "").strip().lower():
        return chosen, True
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter", False
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini", False
    return "openrouter", False  # nothing set; _llm() names the missing key


PROVIDER, PROVIDER_EXPLICIT = _pick_provider()
if PROVIDER == "gemini":
    MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.6-flash"
    KEY_VAR = "GEMINI_API_KEY"
else:
    MODEL = os.environ.get("OPENROUTER_MODEL") or "openai/gpt-5.4-mini"
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

search_by_feel keeps only tracks that actually carry one of your words, so it can come
back empty. That means those words found nothing, not that no such music exists: search
again with different words rather than the same ones, and try more than one phrasing
when the request is vague. Never tell the user there is nothing after one empty search.

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


def _llm(model: str | None = None, provider: str | None = None):
    """The chat model. Overrides let one process compare several, as bakeoff.py does."""
    provider = provider or PROVIDER
    key_var = "GEMINI_API_KEY" if provider == "gemini" else "OPENROUTER_API_KEY"
    key = os.environ.get(key_var)
    if not key:
        raise SystemExit(f"missing {key_var} — copy .env.example to .env and fill it in")
    if provider == "gemini":
        # imported here so OpenRouter users do not need the Google package installed
        from langchain_google_genai import ChatGoogleGenerativeAI

        model = model or (MODEL if PROVIDER == "gemini" else "gemini-3.6-flash")
        return ChatGoogleGenerativeAI(model=model, google_api_key=key, max_tokens=4000)
    return ChatOpenAI(
        model=model or (MODEL if PROVIDER == "openrouter" else "openai/gpt-5.4-mini"),
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


STUB_OVER = 400  # leave small tool results alone; the savings are not worth the churn


def _shrink_old_tools(state: dict) -> dict:
    """Send the model stubs in place of tool results from earlier turns.

    Raw tool output dominates a thread: two searches left 96% of the characters as
    JSON the model had already read and summarised. This replaces those bodies for
    the model's eyes only. The stored history keeps them, so the UI is unaffected,
    and the current turn's results are never touched.
    """
    messages = list(state["messages"])
    current_turn = max(
        (i for i, m in enumerate(messages) if m.type == "human"), default=len(messages)
    )
    trimmed = []
    for i, m in enumerate(messages):
        body = str(m.content)
        if m.type == "tool" and i < current_turn and len(body) > STUB_OVER:
            trimmed.append(
                ToolMessage(
                    content="[earlier results, already summarised in the reply below]",
                    tool_call_id=m.tool_call_id,
                    name=m.name,
                    id=m.id,
                )
            )
        else:
            trimmed.append(m)
    return {"llm_input_messages": trimmed}


PLAYLIST_TOOLS = {"my_playlists", "playlist_tracks", "create_playlist"}
MODES = {
    "manual": "every tool call waits for you",
    "afk": "reads run freely, playlist tools wait for you",
    "auto": "everything runs, nothing waits",
}


def needs_approval(mode: str, tool: str) -> bool:
    """Whether this mode makes this tool wait for the user."""
    if mode == "auto":
        return False
    if mode == "afk":
        return tool in PLAYLIST_TOOLS
    return True  # manual


@asynccontextmanager
async def build(
    checkpointer=None,
    model: str | None = None,
    provider: str | None = None,
    gated: bool = False,
):
    """Compile the agent over ONE MCP session, held open for the whole turn.

    Without the session, every tool call opens its own stdio connection: a new
    interpreter, re-imports, a fresh handshake, about 3.3s of overhead on work that
    takes 0.4s. Inside the session, tool calls cost what the work costs.
    """
    async with MultiServerMCPClient(SERVERS).session("spotify") as session:
        tools = await load_mcp_tools(session)
        yield create_react_agent(
            _llm(model, provider),
            tools,
            prompt=SYSTEM,
            checkpointer=checkpointer,
            pre_model_hook=_shrink_old_tools,
            # pause before the tool node so a mode can hold calls for approval
            interrupt_before=["tools"] if gated else None,
        )


def _token(chunk) -> str:
    """Text out of a streamed model chunk, ignoring tool-call and tool-result chunks."""
    if type(chunk).__name__ != "AIMessageChunk":
        return ""
    text = chunk.content
    if isinstance(text, list):  # content blocks when the model thinks
        text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
    return text or ""


async def run(
    question: str,
    on_part,
    checkpointer=None,
    thread_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
):
    """Run one turn, calling on_part(kind, text) as output arrives.

    kind is "tool" for a complete tool call, or "token" for a piece of the reply as
    the model writes it. With a checkpointer and a thread_id, earlier turns on that
    thread are in scope, so follow-ups like "make that a playlist" resolve.
    """
    config = {"configurable": {"thread_id": thread_id}} if checkpointer else None
    async with build(checkpointer, model, provider) as graph:
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


async def _drive(graph, config, start, on_part, mode: str) -> list[dict]:
    """Stream until the turn ends or a tool call needs the user. Returns what waits.

    Calls the mode allows are resumed automatically, so only gated ones ever stop
    the turn. An empty list means the turn finished.
    """
    inp = start
    while True:
        async for kind, payload in graph.astream(
            inp, config, stream_mode=["values", "messages"]
        ):
            if kind == "messages":
                if text := _token(payload[0]):
                    on_part("token", text)
            elif calls := [p for p in _parts(payload["messages"][-1]) if p[0] == "tool"]:
                for part in calls:
                    on_part(*part)

        state = await graph.aget_state(config)
        if not state.next:  # nothing pending, the model has finished
            return []
        pending = state.values["messages"][-1].tool_calls
        if any(needs_approval(mode, c["name"]) for c in pending):
            return pending
        inp = None  # allowed by the mode, keep going without asking


async def turn(question, on_part, *, checkpointer, thread_id, mode="afk", **kw):
    """One user message. Returns the tool calls waiting for approval, if any."""
    async with build(checkpointer, kw.get("model"), kw.get("provider"), mode != "auto") as g:
        config = {"configurable": {"thread_id": thread_id}}
        return await _drive(g, config, {"messages": [("user", question)]}, on_part, mode)


async def decide(approve: bool, on_part, *, checkpointer, thread_id, mode="afk", **kw):
    """Answer a pending approval, then carry the turn on. Returns the next wait."""
    async with build(checkpointer, kw.get("model"), kw.get("provider"), mode != "auto") as g:
        config = {"configurable": {"thread_id": thread_id}}
        if not approve:
            # answer each call with a refusal so the model can react instead of hanging
            pending = (await g.aget_state(config)).values["messages"][-1].tool_calls
            await g.aupdate_state(
                config,
                {
                    "messages": [
                        ToolMessage(
                            content="The user declined this tool call.",
                            tool_call_id=c["id"],
                            name=c["name"],
                        )
                        for c in pending
                    ]
                },
                as_node="tools",
            )
        return await _drive(g, config, None, on_part, mode)


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

    from langchain_core.messages import AIMessage, HumanMessage

    big = "x" * 3000
    sent = _shrink_old_tools(
        {
            "messages": [
                HumanMessage("older turn", id="h1"),
                ToolMessage(big, tool_call_id="c1", name="search_by_feel", id="t1"),
                AIMessage("the summary that replaces it", id="a1"),
                HumanMessage("current turn", id="h2"),
                ToolMessage(big, tool_call_id="c2", name="search_by_feel", id="t2"),
            ]
        }
    )["llm_input_messages"]
    assert len(str(sent[1].content)) < 100, "old tool result should be stubbed"
    assert len(str(sent[4].content)) == 3000, "current turn's result must survive"
    assert "summary" in str(sent[2].content), "the reply that summarised it must stay"
    how = "set in .env" if PROVIDER_EXPLICIT else "auto-picked from the keys present"
    key = "key present" if os.environ.get(KEY_VAR) else f"NO {KEY_VAR}"
    print(f"ok — {len(names)} tools over MCP stdio")
    print(f"     provider {PROVIDER} ({how}), model {MODEL}, {key}")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        asyncio.run(_selfcheck())
    elif len(sys.argv) > 1:
        asyncio.run(main(" ".join(sys.argv[1:])))
    else:
        raise SystemExit('ask it something: python agent.py "..."')
