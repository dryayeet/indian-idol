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
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphRecursionError
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
    },
    "psych": {
        "command": sys.executable,
        "args": [os.path.join(HERE, "psych_mcp.py")],
        "transport": "stdio",
    },
}

SYSTEM = """<who you are>
You know this person's music library and you have opinions about it. Not a search box
and not a critic filing a review. Someone who went through their playlists, noticed
things, and is telling them what they found.
</who you are>

<always call a tool first>
Every answer about this person, their music, their history, or their state of mind
starts with a tool call this turn. A reply with no tool call behind it is invented,
and that stays true when the answer feels obvious. The one exception is a follow-up
about text already in this conversation.
You never know a track's link: give every track as a markdown link copied from the
`url` a tool returned this turn, like [Title](url), then the artist. Never assemble a
link from an id or from memory; that invents links to the wrong song or to nothing.
Only create a playlist when asked. When you do, say what you were aiming for in its
description.
</always call a tool first>

<how you talk>
Plain sentences, contractions, varied lengths. Say "is", not "serves as". Lead with what
you noticed rather than a summary of what you are about to say. Being unsure is fine.
Warm, but never performing warmth.
Never write: "not just X, it's Y" (say the second half only); three adjectives in a row;
a study in, paints a picture of, a testament to, at its core, sonic, journey, tapestry,
landscape, vibe, delve, underscores, showcases, captures the essence; bold headings or
"**Mood:** ..." lists; Great question, Certainly, Let's dive in; em dashes; a closing
paragraph that restates the reply.
</how you talk>

<answering>
A set of tracks is evidence, not the answer. Say what the pattern is, where it breaks,
and which tracks carry the claim. A reply that only lists what a tool returned has not
answered anything.
Say plainly when the evidence is thin. "Only 13 of these 15 could be measured" beats a
confident average.
Think before you write, not in front of the user: no "wait, that's not right", no
correcting yourself mid-reply. Search again, then write once.
Which album a track is from, who wrote it, what year: web_search it or leave it out. A
confident wrong credit is worse than a shorter answer. When a claim leans on the web,
say where it came from.
When the request is open, read it more than one way: two searches from different angles
beat one. When it names a playlist, artist, or song, go straight there and spend the
effort on what you say about it.
</answering>

<finding music>
"driving away from my hometown for the last time" is a request; "leaving home" and
"small town" are queries. Search with queries.
If two or three phrasings all come back empty, stop and say what you tried: an honest
miss beats a tenth search, and more rewordings of the same idea find the same nothing.
For more music by a named artist, use artist_albums and similar_artists; search_by_feel
is not an artist lookup, and an artist's name as its `description` mostly matches
covers and tributes.
</finding music>

<what they already have>
A name from the list at the end of this prompt is a playlist, not a song: read it with
playlist_tracks or measure it with playlist_vibe, never search for it as a title.
For listening history use listening_lyrics, one call, not get_lyrics once per track.
To read the person rather than the music, score what they have been hearing: feed
listening_lyrics text to get_big_five and get_emotion_labels. Report the scores as
estimates from word choice, with the chunk count, and never as a diagnosis.
</what they already have>

<attachments>
An uploaded image or pdf arrives already read: its complete text is in the message,
inside the <attached ...> block. Never say you cannot open, see, or read an attachment;
that refuses text you already have. Treat it as what the user showed you, not as a tool
result. Numbers about how music sounds, and anything about what is in the user's
library, still come from tools: never state a measurement or a count you did not see a
tool return this turn. A screenshot of a playlist the user owns is an invitation to
measure the real playlist.
</attachments>"""


async def _prompt(session) -> str:
    """SYSTEM, plus what the user's playlists are actually called.

    Three system-prompt wordings failed to stop the model searching for "Unmaad" as a
    song title, because no wording can: nothing in its context said the word was a
    playlist. This is 346 tokens against ~2.3k of tool schemas, and it removes the
    guess rather than trying to improve it. The list is cached in the server, so this
    is two requests per conversation, not per turn.
    """
    try:
        res = await session.call_tool("playlist_names", {})
        # FastMCP 1.x sends a list return as one text block per item, not as JSON
        names = [c.text for c in res.content if getattr(c, "text", "").strip()]
    except Exception:  # noqa: BLE001 - a prompt garnish must never fail a turn
        return SYSTEM
    if not names:
        return SYSTEM
    return f"{SYSTEM}\n\nThe user's playlists are called: {', '.join(names)}."


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


UPLOAD_DIM = 1280  # longest image side sent to the model; screenshots stay readable
PDF_VISION_PAGES = 12  # vision-read at most this many scanned pages, then say so
PDF_KEEP = 3000  # chars of an attached pdf kept in turns after the first

READING_PROMPT = (
    "Read this image completely. Transcribe every piece of legible text exactly as "
    "written, keeping the order and structure. Then say what the image is (a playlist, "
    "a poster, a chat, album art...) and note anything else informative: names, "
    "numbers, dates, artwork. Your reading becomes the permanent record of this image "
    "for the rest of the conversation, so leave nothing legible out."
)


def _shrunk_image(data: bytes) -> tuple[str, str]:
    """Image bytes as (base64, mime), downscaled. A phone screenshot is megabytes."""
    import base64
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img.thumbnail((UPLOAD_DIM, UPLOAD_DIM))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


def _read_image(b64: str, mime: str, model: str | None = None) -> str:
    """One vision call producing the full reading that outlives the pixels."""
    reply = _llm(model).invoke(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": READING_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ]
    )
    text = reply.content
    if isinstance(text, list):
        text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
    return text.strip()


def prepare_upload(name: str, data: bytes, model: str | None = None) -> dict:
    """A chat upload as an attachment dict the turn can carry.

    Images: {"kind": "image", "name", "b64", "mime", "reading"} — the raw pixels ride
    the first turn, the reading replaces them afterwards.
    PDFs: {"kind": "pdf", "name", "text"} — text per page; a page with no text but an
    embedded image (any scan) is vision-read rather than silently dropped, which is
    what pypdf's extract_text alone would do.
    """
    if not name.lower().endswith(".pdf"):
        b64, mime = _shrunk_image(data)
        return {"kind": "image", "name": name, "b64": b64, "mime": mime,
                "reading": _read_image(b64, mime, model)}

    import io

    from pypdf import PdfReader

    parts: list[str] = []
    scans = 0
    for n, page in enumerate(PdfReader(io.BytesIO(data)).pages, 1):
        text = (page.extract_text() or "").strip()
        if len(text) < 40 and page.images:  # a scan: the content is the picture
            if scans >= PDF_VISION_PAGES:
                parts.append(f"[page {n}: scanned, not read - page limit reached]")
                continue
            scans += 1
            for im in page.images:
                b64, mime = _shrunk_image(im.data)
                parts.append(f"[page {n}, scanned, read as:]\n{_read_image(b64, mime, model)}")
        elif text:
            parts.append(f"[page {n}]\n{text}")
    return {"kind": "pdf", "name": name, "text": "\n\n".join(parts) or "[empty pdf]"}


def _user_message(question: str, attachments: list[dict] | None):
    """The turn's opening message. Plain text unless something was attached."""
    if not attachments:
        return ("user", question)
    blocks: list[dict] = [{"type": "text", "text": question}]
    readings: list[str] = []
    for a in attachments:
        if a["kind"] == "image":
            blocks.append(
                {"type": "image_url",
                 "image_url": {"url": f"data:{a['mime']};base64,{a['b64']}"}}
            )
            readings.append(a["reading"])
        else:
            blocks.append(
                {"type": "text",
                 "text": (
                     f'The user attached "{a["name"]}". It has already been read; its '
                     f'complete extracted text is:\n<attached pdf "{a["name"]}">\n'
                     f'{a["text"]}\n</attached>'
                 )}
            )
    # readings ride the message so the shrink hook can swap them in later turns
    return HumanMessage(content=blocks, additional_kwargs={"readings": readings})


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
        elif m.type == "human" and i < current_turn and isinstance(m.content, list):
            # an upload's first turn carried real pixels; from here on the model gets
            # the stored reading instead, and long pdf text is cut to its head
            readings = iter((getattr(m, "additional_kwargs", None) or {}).get("readings") or [])
            blocks = []
            for b in m.content:
                if not isinstance(b, dict):
                    blocks.append(b)
                elif b.get("type") == "image_url":
                    read = next(readings, "an image, already read earlier this chat")
                    blocks.append(
                        {"type": "text",
                         "text": f"[attached image, previously read in full:]\n{read}"}
                    )
                elif (
                    b.get("type") == "text"
                    and '<attached pdf "' in b["text"]
                    and len(b["text"]) > PDF_KEEP
                ):
                    blocks.append(
                        {"type": "text",
                         "text": b["text"][:PDF_KEEP] + "\n[pdf cut here for this turn]"}
                    )
                else:
                    blocks.append(b)
            trimmed.append(HumanMessage(content=blocks, id=m.id))
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
    client = MultiServerMCPClient(SERVERS)
    async with client.session("spotify") as session, client.session("psych") as psych:
        tools = await load_mcp_tools(session) + await load_mcp_tools(psych)
        yield create_react_agent(
            _llm(model),
            tools,
            prompt=await _prompt(session),
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


MAX_ROUNDS = 20  # tool rounds in one turn; past this the model is looping, not working
TOOL_BUDGET = 6  # calls to any ONE tool per turn. Rewording a search evades the exact-
# repeat check, so a per-tool count is what catches "kept searching with new words".
# 6, not 4: fanning similar_artists over followed artists legitimately runs five.


def _call_key(calls: list[dict]) -> str:
    """One round of tool calls as a comparable string, argument order ignored."""
    return repr(sorted((c["name"], repr(sorted(c["args"].items()))) for c in calls))


async def _drive(graph, config, start, on_part, mode: str, gated: bool) -> list[dict]:
    """Stream until the turn ends or a tool call needs the user. Returns what waits.

    kind is "tool" for a complete tool call, or "token" for a piece of the reply as
    the model writes it. Calls the mode allows are resumed automatically, so only
    gated ones ever stop the turn. An empty list means the turn finished.

    Two brakes live here, because LangGraph's own recursion limit is reset every time
    a gated graph resumes, which is every auto-approved tool call: a turn that repeats
    an identical round of calls, or runs past MAX_ROUNDS, has its calls answered with
    "stop and conclude" instead of results. One request looped 50+ identical
    search_by_feel calls before these existed.
    """
    inp = start
    seen: set[str] = set()
    uses: dict[str, int] = {}  # calls per tool this turn, for the TOOL_BUDGET brake
    shown: set[str] = set()  # resuming re-emits the paused state, so dedupe by message id
    rounds = 0
    while True:
        try:
            async for kind, payload in graph.astream(
                inp, config, stream_mode=["values", "messages"]
            ):
                if kind == "messages":  # token by token, as the model writes
                    if text := _token(payload[0]):
                        on_part("token", text)
                    continue
                last = payload["messages"][-1]
                if last.id in shown:
                    continue
                if calls := [p for p in _parts(last) if p[0] == "tool"]:
                    shown.add(last.id)
                    for part in calls:  # replies come from the token stream instead
                        on_part(*part)
        except GraphRecursionError:
            # ungated graphs never pause, so this built-in step cap is their only brake
            on_part("token", "\n\nStopped: too many steps without an answer.")
            return []

        if not gated:
            # nothing can pause this graph, so the stream ending means the turn ended.
            # aget_state would also fail here: without a checkpointer there is no state
            return []
        state = await graph.aget_state(config)
        if not state.next:  # nothing pending, the model has finished
            return []
        pending = state.values["messages"][-1].tool_calls
        key = _call_key(pending)
        rounds += 1
        over = next((c["name"] for c in pending if uses.get(c["name"], 0) >= TOOL_BUDGET), None)
        if key in seen or over or rounds > MAX_ROUNDS:
            if rounds > MAX_ROUNDS + 5:  # told to conclude and still calling: cut it off
                on_part("token", "\n\nStopped: the model kept repeating tool calls.")
                return []
            if key in seen:
                why = "This exact call already ran this turn, and its result has not changed."
            elif over:
                why = (
                    f"{over} has already run {TOOL_BUDGET} times this turn. More calls "
                    "to it are blocked: rewording the same search finds the same music."
                )
            else:
                why = f"This turn has used {MAX_ROUNDS} rounds of tool calls."
            await graph.aupdate_state(
                config,
                {
                    "messages": [
                        ToolMessage(
                            content=why + " Stop calling tools and answer from what you have.",
                            tool_call_id=c["id"],
                            name=c["name"],
                        )
                        for c in pending
                    ]
                },
                as_node="tools",
            )
            inp = None
            continue
        seen.add(key)
        for c in pending:
            uses[c["name"]] = uses.get(c["name"], 0) + 1
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


async def turn(
    question, on_part, *, checkpointer, thread_id, mode="afk", attachments=None, **kw
):
    """One user message, with any uploads. Returns the tool calls waiting, if any."""
    gated = mode != "auto"
    async with build(checkpointer, kw.get("model"), gated) as g:
        config = _config(checkpointer, thread_id)
        start = {"messages": [_user_message(question, attachments)]}
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
    # the prompt must carry the playlist names, silently falling back to SYSTEM if the
    # tool is renamed or its return shape changes, which is exactly what would go unnoticed
    async with MultiServerMCPClient(SERVERS).session("spotify") as session:
        prompt = await _prompt(session)
    assert prompt != SYSTEM, "playlist names did not reach the prompt"
    assert prompt.startswith(SYSTEM), "SYSTEM must survive intact"
    tools = await MultiServerMCPClient(SERVERS).get_tools()
    names = sorted(t.name for t in tools)
    # a subset, not an exact list: adding a tool to the server should not fail this
    assert {"search_by_feel", "create_playlist", "get_lyrics"} <= set(names), names
    feel = next(t for t in tools if t.name == "search_by_feel")
    schema = feel.args_schema["properties"]
    assert "valence" in schema and "description" in schema, schema
    assert feel.args_schema.get("required") == ["description"], feel.args_schema
    assert "matching a mood" in feel.description, feel.description
    # the loop brake's key must ignore argument and call order, or a repeat slips by
    a = [{"name": "f", "args": {"x": 1, "y": 2}}, {"name": "g", "args": {}}]
    b = [{"name": "g", "args": {}}, {"name": "f", "args": {"y": 2, "x": 1}}]
    assert _call_key(a) == _call_key(b), "same round must key identically"
    assert _call_key(a) != _call_key([{"name": "f", "args": {"x": 9}}]), "different args must differ"

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
