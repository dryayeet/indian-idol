"""LangGraph ReAct agent that drives the Spotify MCP server.

    python agent.py "songs that feel like driving away from my hometown for the last time"
    python agent.py --selfcheck      lists the tools over real MCP stdio, no LLM call

Needs OPENROUTER_API_KEY in .env, plus the Spotify variables spotify_mcp.py reads.
Pick the model with OPENROUTER_MODEL; it must support tool calling. Which model and
why: MODEL_BAKEOFF.md.

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

KEY_VAR = "OPENROUTER_API_KEY"
MODEL = os.environ.get("OPENROUTER_MODEL") or "qwen/qwen3.5-flash-02-23"
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

Answer analytically, not as a bare list. A set of tracks is evidence: say what the
pattern is, what it suggests about mood or taste, and where the pattern breaks, then
name the tracks that carry each claim. A reply that only lists what a tool returned
has not answered anything.

Explore before you conclude, but only when the request is open. Asked for a mood or a
question about taste, read it more than one way: two searches from different angles
beat one, and a thread worth pulling is worth pulling even if the user did not ask for
it. Asked about a named playlist, artist, or song, go straight there and spend the
effort on the analysis instead. Gathering what you were not asked for is not depth.

Keep the whole reply one argument. Sections and lists serve the point you are making;
they are not the point. Say what you actually think, and say plainly when the evidence
is thin rather than dressing a guess as a finding.

Only create a playlist when the user asks for one. When you build one, say what you
were aiming for in its description."""


def _llm(model: str | None = None):
    """The chat model. The override lets one process compare several, as bakeoff does."""
    key = os.environ.get(KEY_VAR)
    if not key:
        raise SystemExit(f"missing {KEY_VAR} — copy .env.example to .env and fill it in")
    return ChatOpenAI(
        model=model or MODEL,
        api_key=key,
        base_url="https://openrouter.ai/api/v1",
        max_tokens=4000,
        # Without this, qwen3.5-flash streams its deliberation as the visible reply
        # ("The user wants...", stray </think> tags). `exclude` does not help: the
        # model emits reasoning as ordinary content, so it has to be turned off.
        extra_body={"reasoning": {"enabled": False}},
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
async def build(checkpointer=None, model: str | None = None, gated: bool = False):
    """Compile the agent over ONE MCP session, held open for the whole turn.

    Without the session, every tool call opens its own stdio connection: a new
    interpreter, re-imports, a fresh handshake, about 3.3s of overhead on work that
    takes 0.4s. Inside the session, tool calls cost what the work costs.
    """
    if gated and checkpointer is None:
        # a paused graph lives in the checkpointer; without one there is nowhere to
        # park, and the approval could never be resumed
        raise ValueError("approval modes need a checkpointer to hold the paused turn")
    async with MultiServerMCPClient(SERVERS).session("spotify") as session:
        tools = await load_mcp_tools(session)
        yield create_react_agent(
            _llm(model),
            tools,
            prompt=SYSTEM,
            checkpointer=checkpointer,
            pre_model_hook=_shrink_old_tools,
            # pause before the tool node so a mode can hold calls for approval
            interrupt_before=["tools"] if gated else None,
        )


_THINK_TAGS = ("<think>", "</think>", "<thinking>", "</thinking>")


def _token(chunk) -> str:
    """Text out of a streamed model chunk, ignoring tool-call and tool-result chunks."""
    if type(chunk).__name__ != "AIMessageChunk":
        return ""
    text = chunk.content
    if isinstance(text, list):  # content blocks when the model thinks
        text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
    if not text:
        return ""
    for tag in _THINK_TAGS:  # belt and braces: some models leak the tag mid-stream
        text = text.replace(tag, "")
    return text


def _config(checkpointer, thread_id):
    """A thread config only makes sense with a checkpointer to store the thread in."""
    return {"configurable": {"thread_id": thread_id}} if checkpointer else None


async def _drive(graph, config, start, on_part, mode: str, gated: bool) -> list[dict]:
    """Stream until the turn ends or a tool call needs the user. Returns what waits.

    kind is "tool" for a complete tool call, or "token" for a piece of the reply as
    the model writes it. Calls the mode allows are resumed automatically, so only
    gated ones ever stop the turn. An empty list means the turn finished.
    """
    inp = start
    while True:
        async for kind, payload in graph.astream(
            inp, config, stream_mode=["values", "messages"]
        ):
            if kind == "messages":  # token by token, as the model writes
                if text := _token(payload[0]):
                    on_part("token", text)
            elif calls := [p for p in _parts(payload["messages"][-1]) if p[0] == "tool"]:
                for part in calls:  # replies come from the token stream instead
                    on_part(*part)

        if not gated:
            # nothing can pause this graph, so the stream ending means the turn ended.
            # aget_state would also fail here: without a checkpointer there is no state
            return []
        state = await graph.aget_state(config)
        if not state.next:  # nothing pending, the model has finished
            return []
        pending = state.values["messages"][-1].tool_calls
        if any(needs_approval(mode, c["name"]) for c in pending):
            return pending
        inp = None  # allowed by the mode, keep going without asking


async def run(
    question: str,
    on_part,
    checkpointer=None,
    thread_id: str | None = None,
    model: str | None = None,
):
    """One turn with nothing gated: the CLI and collect() path.

    With a checkpointer and a thread_id, earlier turns on that thread are in scope,
    so follow-ups like "make that a playlist" resolve.
    """
    async with build(checkpointer, model) as graph:
        await _drive(
            graph,
            _config(checkpointer, thread_id),
            {"messages": [("user", question)]},
            on_part,
            "auto",
            gated=False,
        )


async def turn(question, on_part, *, checkpointer, thread_id, mode="afk", **kw):
    """One user message. Returns the tool calls waiting for approval, if any."""
    gated = mode != "auto"
    async with build(checkpointer, kw.get("model"), gated) as g:
        config = _config(checkpointer, thread_id)
        start = {"messages": [("user", question)]}
        return await _drive(g, config, start, on_part, mode, gated)


async def decide(approve: bool, on_part, *, checkpointer, thread_id, mode="afk", **kw):
    """Answer a pending approval, then carry the turn on. Returns the next wait."""
    gated = mode != "auto"
    async with build(checkpointer, kw.get("model"), gated) as g:
        config = _config(checkpointer, thread_id)
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
        return await _drive(g, config, None, on_part, mode, gated)


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
    key = "key present" if os.environ.get(KEY_VAR) else f"NO {KEY_VAR}"
    print(f"ok — {len(names)} tools over MCP stdio, model {MODEL}, {key}")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        asyncio.run(_selfcheck())
    elif len(sys.argv) > 1:
        asyncio.run(main(" ".join(sys.argv[1:])))
    else:
        raise SystemExit('ask it something: python agent.py "..."')
