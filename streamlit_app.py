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
        return int(st.number_input(label, min_value=1, max_value=50, value=default or 10, step=1))
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


def _agent_panel() -> None:
    st.subheader("Chat")
    st.caption(f"LangGraph ReAct loop over the MCP tools, model `{agent.MODEL}`")

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        key = st.text_input(
            "OpenRouter API key",
            type="password",
            help="Or put OPENROUTER_API_KEY in .env and restart.",
        )

    if "thread" not in st.session_state:
        st.session_state.thread = uuid.uuid4().hex
        st.session_state.chat = []

    if st.button("New conversation"):
        st.session_state.thread = uuid.uuid4().hex
        st.session_state.chat = []
        st.rerun()

    for role, parts in st.session_state.chat:
        with st.chat_message(role):
            # plain if/else, not a ternary: a bare expression here would be a statement
            # whose value Streamlit's magic renders (st.markdown returns a DeltaGenerator)
            if role == "assistant":
                _draw(parts)
            else:
                st.markdown(parts)

    question = st.chat_input("songs that feel like driving away from my hometown")
    if not question:
        return
    if not key:
        st.warning("An OpenRouter key is needed to run the agent.")
        return
    os.environ["OPENROUTER_API_KEY"] = key  # agent._llm() reads it at call time

    st.session_state.chat.append(("user", question))
    with st.chat_message("user"):
        st.markdown(question)

    parts: list[tuple[str, str]] = []
    with st.chat_message("assistant"):
        def show(kind: str, text: str) -> None:
            """Render each step the moment the agent produces it, not at the end."""
            parts.append((kind, text))
            _draw([(kind, text)])

        with st.spinner("thinking, searching, deciding..."):
            try:
                asyncio.run(
                    agent.run(
                        question,
                        show,
                        checkpointer=_memory(),
                        thread_id=st.session_state.thread,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - surface model and API failures
                st.error(f"{type(exc).__name__}: {exc}")
                return
    st.session_state.chat.append(("assistant", parts))


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
