"""Streamlit UI for the Spotify MCP tools.

    streamlit run streamlit_app.py

Forms are generated from each tool's input schema, so adding a tool to
spotify_mcp.py makes it show up here with no changes to this file.
"""

import asyncio
import json
import os
import uuid

import streamlit as st
from langgraph.checkpoint.memory import InMemorySaver

import agent
from spotify_mcp import app

if __name__ == "__main__" and not st.runtime.exists():
    raise SystemExit("run this with:  streamlit run streamlit_app.py")

st.set_page_config(page_title="Spotify MCP", page_icon="🎧", layout="wide")


@st.cache_resource
def _tools():
    return asyncio.run(app.list_tools())


def _widget(key: str, spec: dict, required: bool):
    kind = spec.get("type")
    default = spec.get("default")
    label = f"{key} *" if required else key

    if key == "time_range":
        return st.selectbox(label, ["short_term", "medium_term", "long_term"])
    if kind == "integer":
        # the ceiling follows the tool's own default: paging tools default to 200, and a
        # hardcoded 50 here made Streamlit refuse to render them at all
        top = max(50, default or 0)  # ui_check.py renders every form to keep this honest
        return int(st.number_input(label, min_value=1, max_value=top, value=default or 10, step=1))
    if kind == "number":
        if default is not None and 0.0 <= default <= 1.0:
            return st.slider(label, 0.0, 1.0, float(default), 0.05)
        return float(st.number_input(label, value=float(default or 0)))
    if kind == "array":
        raw = st.text_area(label, placeholder="one spotify:track:... per line", height=120)
        return [line.strip() for line in raw.splitlines() if line.strip()]
    return st.text_input(label, value=default or "")


def _render(text: str) -> None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        st.text(text or "(empty)")
        return
    if isinstance(data, list) and data and isinstance(data[0], dict):
        st.caption(f"{len(data)} results")
        st.dataframe(data)
    elif isinstance(data, str):
        st.text(data or "(empty)")
    else:
        st.json(data)


@st.cache_resource
def _memory():
    """One saver for the process. Conversations live until Streamlit restarts."""
    return InMemorySaver()


def _draw(parts) -> None:
    for kind, text in parts:
        if kind == "tool":
            st.code(text, language="python")
        else:
            st.markdown(text)


HELP = (
    "**/manual** every tool call waits for you  \n"
    "**/afk** reads run freely, playlist tools wait for you  \n"
    "**/auto** everything runs, nothing waits  \n"
    "**/mode** show the current mode  \n"
    "**/help** this list"
)


def _mode_key() -> str:
    """Widget key carrying the current mode.

    Streamlit forbids writing a widget's key once the widget has rendered, so a
    slash command cannot push its value into the buttons. Changing the key instead
    makes Streamlit build a fresh widget that takes `default` from the new mode.
    """
    return f"mode_picker_{st.session_state.mode}"


def _set_mode() -> None:
    """Called when the buttons change. The mode has not moved yet, so the key holds."""
    if picked := st.session_state.get(_mode_key()):
        st.session_state.mode = picked  # None means deselected; keep the old mode


def _command(text: str) -> None:
    """Slash commands typed in the chat bar. The only way to change mode."""
    word = text.strip().lstrip("/").split()[0].lower() if text.strip("/ ") else "help"
    st.session_state.chat.append(("user", text))
    if word in agent.MODES:
        st.session_state.mode = word  # the buttons follow via _mode_key()
        reply = f"Mode is now **{word}** — {agent.MODES[word]}."
    elif word == "mode":
        reply = f"Mode is **{st.session_state.mode}** — {agent.MODES[st.session_state.mode]}."
    elif word == "help":
        reply = HELP
    else:
        reply = f"No such command `/{word}`.\n\n{HELP}"
    st.session_state.chat.append(("assistant", [("text", reply)]))
    st.rerun()


def _continue(approve: bool, key: str) -> None:
    """Answer the pending approval and let the turn finish."""
    os.environ[agent.KEY_VAR] = key
    parts: list[tuple[str, str]] = []
    with st.chat_message("assistant"):
        if not approve:
            st.markdown("_declined_")
            parts.append(("text", "_declined_"))
        with st.spinner("continuing..."):
            try:
                st.session_state.pending = asyncio.run(
                    agent.decide(
                        approve,
                        lambda kind, text: parts.append(
                            ("text" if kind == "token" else kind, text)
                        ),
                        checkpointer=_memory(),
                        thread_id=st.session_state.thread,
                        mode=st.session_state.mode,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - surface model and API failures
                if type(exc).__module__.startswith("streamlit"):
                    raise  # a rerun or Stop, not a failure
                st.error(f"{type(exc).__name__}: {exc}")
                return
    merged: list[tuple[str, str]] = []
    for kind, text in parts:  # rejoin streamed tokens for the history
        if kind == "text" and merged and merged[-1][0] == "text":
            merged[-1] = ("text", merged[-1][1] + text)
        else:
            merged.append((kind, text))
    st.session_state.chat.append(("assistant", merged))
    st.rerun()


def _agent_panel() -> None:
    st.subheader("Chat")
    st.caption(
        f"LangGraph ReAct loop over the MCP tools, model `{agent.MODEL}`"
    )

    key = os.environ.get(agent.KEY_VAR)
    if not key:
        key = st.text_input(
            "OpenRouter API key",
            type="password",
            help=f"Or put {agent.KEY_VAR} in .env and restart.",
        )

    if "thread" not in st.session_state:
        st.session_state.thread = uuid.uuid4().hex
        st.session_state.chat = []
        st.session_state.mode = "afk"
        st.session_state.pending = None

    picker, right = st.columns([3, 1])
    with picker:
        st.segmented_control(
            "Mode",
            list(agent.MODES),
            default=st.session_state.mode,
            key=_mode_key(),
            on_change=_set_mode,
            label_visibility="collapsed",
        )
    if right.button("New conversation"):
        st.session_state.thread = uuid.uuid4().hex
        st.session_state.chat = []
        st.session_state.pending = None
        st.rerun()
    st.caption(agent.MODES[st.session_state.mode])

    for role, parts in st.session_state.chat:
        with st.chat_message(role):
            # plain if/else, not a ternary: a bare expression here would be a statement
            # whose value Streamlit's magic renders (st.markdown returns a DeltaGenerator)
            if role == "assistant":
                _draw(parts)
            else:
                st.markdown(parts)

    if pending := st.session_state.pending:
        with st.chat_message("assistant"):
            st.warning(f"waiting for you — mode `{st.session_state.mode}`")
            for call in pending:
                st.code(f"{call['name']}({call['args']})", language="python")
            yes, no = st.columns(2)
            approved = yes.button("Approve", type="primary")
            declined = no.button("Decline")
        if approved or declined:
            _continue(approve=approved, key=key)
            return

    raw = st.chat_input(
        "a request, or /manual /afk /auto /mode",
        accept_file="multiple",
        file_type=["png", "jpg", "jpeg", "webp", "pdf"],
    )
    if not raw:
        return
    # with accept_file on, the widget returns an object; AppTest still sends a str
    question = (getattr(raw, "text", None) or (raw if isinstance(raw, str) else "")).strip()
    files = list(getattr(raw, "files", None) or [])

    if question.startswith("/") and not files:
        _command(question)
        return
    if not key:
        st.warning("An OpenRouter key is needed to run the agent.")
        return
    os.environ[agent.KEY_VAR] = key  # agent._llm() reads it at call time
    if not question:
        question = "Look at what I attached and tell me what you make of it."

    shown = question + "".join(f"\n\n:paperclip: `{f.name}`" for f in files)
    st.session_state.chat.append(("user", shown))
    with st.chat_message("user"):
        st.markdown(shown)

    attachments = []
    if files:
        # each image costs one vision call here; the reading it produces is what
        # later turns see instead of the pixels
        with st.spinner("reading what you attached..."):
            try:
                attachments = [agent.prepare_upload(f.name, f.getvalue()) for f in files]
            except Exception as exc:  # noqa: BLE001 - a bad file should not eat the turn
                if type(exc).__module__.startswith("streamlit"):
                    raise
                st.error(f"could not read an attachment: {exc}")
                return

    # rendered before the blocking call so it is clickable during it. Clicking makes
    # Streamlit stop this script run at its next st call, which kills the turn; the
    # handler below then files what had already streamed.
    st.button("⏹ Stop", help="Abandon this turn")

    parts: list[tuple[str, str]] = []
    with st.chat_message("assistant"):
        live = {"text": "", "box": None}

        def flush() -> None:
            """Move the streamed text into history and start a fresh block after it."""
            if live["text"].strip():
                parts.append(("text", live["text"]))
            live["text"], live["box"] = "", None

        def show(kind: str, text: str) -> None:
            """Render output as it arrives: tokens grow in place, tools appear between."""
            if kind == "token":
                if live["box"] is None:
                    live["box"] = st.empty()
                live["text"] += text
                live["box"].markdown(live["text"])
                return
            flush()
            parts.append((kind, text))
            _draw([(kind, text)])

        with st.spinner("thinking, searching, deciding..."):
            try:
                st.session_state.pending = asyncio.run(
                    agent.turn(
                        question,
                        show,
                        checkpointer=_memory(),
                        thread_id=st.session_state.thread,
                        mode=st.session_state.mode,
                        attachments=attachments,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - surface model and API failures
                if type(exc).__module__.startswith("streamlit"):
                    # the Stop button (or any interaction) killed the run mid-turn.
                    # Keep what had streamed, then let Streamlit take the rerun.
                    flush()
                    parts.append(("text", "_stopped_"))
                    st.session_state.chat.append(("assistant", parts))
                    raise
                st.error(f"{type(exc).__name__}: {exc}")
                return
        flush()
    st.session_state.chat.append(("assistant", parts))
    if st.session_state.pending:
        st.rerun()  # show the approval buttons


tools = _tools()
names = [t.name for t in tools]

st.title("🎧 Spotify MCP")
st.caption("An agent over the tools, or the tools by hand.")

with st.sidebar:
    st.header("Mode")
    mode = st.radio("Mode", ["Agent", "Tools"], label_visibility="collapsed")
    st.divider()
    st.caption("Tools on the server")
    for tool in tools:
        st.markdown(f"**{tool.name}**  \n{(tool.description or '').strip()}")

if mode == "Agent":
    _agent_panel()
    st.stop()

chosen = st.selectbox("Tool", names)
tool = next(t for t in tools if t.name == chosen)
st.subheader(tool.name)
st.write((tool.description or "").strip())

schema = tool.inputSchema
required = set(schema.get("required") or [])
args = {}
for key, spec in (schema.get("properties") or {}).items():
    args[key] = _widget(key, spec, key in required)

if st.button("Run", type="primary"):
    missing = [k for k in required if not args.get(k)]
    if missing:
        st.warning(f"fill in: {', '.join(missing)}")
    else:
        with st.spinner("calling Spotify..."):
            try:
                result = asyncio.run(app.call_tool(tool.name, args))
            except Exception as exc:  # noqa: BLE001 - surface anything the API throws
                st.error(f"{type(exc).__name__}: {exc}")
                if "missing env var" in str(exc):
                    st.info("Run `python get_token.py` first to write .env")
            else:
                # FastMCP 1.x returns (content_blocks, structured_result)
                blocks = result[0] if isinstance(result, tuple) else result
                for block in blocks:
                    _render(getattr(block, "text", str(block)))
