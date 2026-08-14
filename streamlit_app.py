"""Streamlit UI for the Spotify MCP tools.

    streamlit run streamlit_app.py

Forms are generated from each tool's input schema, so adding a tool to
spotify_mcp.py makes it show up here with no changes to this file.
"""

import asyncio
import json

import streamlit as st

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


tools = _tools()
names = [t.name for t in tools]

st.title("🎧 Spotify MCP")
st.caption("Same tools the agent calls, driven by hand.")

with st.sidebar:
    st.header("Tools")
    chosen = st.radio("Pick one", names, label_visibility="collapsed")
    st.divider()
    for tool in tools:
        st.markdown(f"**{tool.name}**  \n{(tool.description or '').strip()}")

tool = next(t for t in tools if t.name == chosen)
st.subheader(tool.name)
st.write((tool.description or "").strip())

schema = tool.input_schema
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
                for block in result.content:
                    _render(getattr(block, "text", str(block)))
