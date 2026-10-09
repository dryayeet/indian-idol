"""Streamlit UI for the Spotify MCP tools.

    streamlit run streamlit_app.py

Forms are generated from each tool's input schema, so adding a tool to
spotify_mcp.py makes it show up here with no changes to this file.

A name screen and a Spotify connect screen gate everything: each profile
carries its own authorization, stored encrypted by the user store, and no
Spotify call runs until the account is linked.
"""

import asyncio
import json
import os
import uuid

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError
from langgraph.checkpoint.memory import InMemorySaver

# Streamlit Cloud exposes configured secrets via st.secrets, not os.environ.
# Load them before importing modules that initialize their API clients from env.
try:
    _secrets = st.secrets.to_dict()
except StreamlitSecretNotFoundError:
    _secrets = {}
for _name in (
    "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "USERS_DB_KEY",
    "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "HF_TOKEN", "LASTFM_API_KEY",
):
    if _secrets.get(_name) and not os.environ.get(_name):
        os.environ[_name] = str(_secrets[_name])

import agent
import spotify_oauth
import store
from psych_mcp import app as psych_app
import spotify_mcp
from spotify_mcp import app

if __name__ == "__main__" and not st.runtime.exists():
    raise SystemExit("run this with:  streamlit run streamlit_app.py")

st.set_page_config(page_title="Spotify MCP", page_icon="🎧", layout="wide")


def _login_gate() -> None:
    """The name screen. Identity is the store's user_id; the name is a lookup handle.

    A name someone already uses gets a confirmation screen rather than a silent
    reuse, because a name alone cannot prove who is typing. A name nobody uses
    yet stores nothing here: the profile is only created once Spotify has
    authorized, because the Spotify account id is the real identity.
    """
    if st.session_state.get("user_id"):
        return
    st.title("🎧 Spotify MCP")
    st.caption("An agent over the tools, or the tools by hand.")
    submitted = st.session_state.get("login_name_submitted")
    candidates = st.session_state.get("login_candidates")
    if submitted and not candidates:
        return  # the connect screen owns this name now
    if not submitted:
        st.subheader("What should I call you?")
        name = st.text_input(
            "Name", max_chars=40, key="login_name", label_visibility="collapsed"
        )
        if st.button("Continue", key="login_continue", type="primary"):
            clean = " ".join(name.split())
            if not clean:
                st.warning("Enter a name to continue.")
                st.stop()
            st.session_state.login_name_submitted = clean
            if matches := store.find_users(clean):
                # ask before reusing: someone else may share the name
                st.session_state.login_candidates = matches
            st.rerun()  # straight to the cards, or to the connect screen
        st.stop()

    st.subheader("Is this your account?")
    for cand in candidates:
        bound = bool(cand["connected"])
        card = (
            f"**{cand['display_name']}** · joined {cand['created_at'][:10]} · "
            f"last seen {cand['last_seen_at'][:10]} · "
            + (
                f"Spotify: **{cand['spotify_name'] or cand['spotify_user_id']}**"
                if bound
                else "Spotify not connected"
            )
        )
        st.markdown(card)
        if st.button("Yes, it's me", key=f"login_yes_{cand['id']}", type="primary"):
            store.touch(cand["id"])
            st.session_state.user_id = cand["id"]
            st.session_state.user_name = cand["display_name"]
            st.session_state.pop("login_candidates", None)
            st.rerun()
    if st.button("No, this is a new account", key="login_new"):
        # keep the submitted name; the profile is created only after the connect
        st.session_state.pop("login_candidates", None)
        st.rerun()
    st.stop()


def _my_refresh() -> str | None:
    """The signed-in profile's refresh token, read at call time. Session state
    never holds tokens; the store does, encrypted."""
    if not st.session_state.get("user_id"):
        return None
    auth = store.get_spotify_auth(st.session_state.user_id)
    return auth["refresh_token"] if auth else None


def _require_reconnect() -> None:
    """Gate the next run until this profile authorizes again."""
    st.session_state.auth_ok = False
    st.session_state.auth_failed = True
    st.session_state.reconnect = True
    st.session_state.pending = None


def _finish_connect(user_id: str, token: dict, profile: dict, *, already_saved=False) -> None:
    """Bind the authorization and go: overwrite for a reconnect, fresh row for
    a first connect. Either way the old capture never survives."""
    if not already_saved:
        store.save_spotify_auth(
            user_id,
            token["refresh_token"],
            scopes=token.get("scope", ""),
            spotify_user_id=profile["id"],
            spotify_name=profile.get("display_name") or profile["id"],
        )
    if state := st.session_state.get("oauth_state"):
        spotify_oauth.forget_state(state)
    st.session_state.auth_ok = True
    st.session_state.pop("auth_failed", None)
    st.session_state.pop("reconnect", None)
    st.session_state.pop("oauth_state", None)
    st.session_state.pop("oauth_paste", None)
    st.session_state.pop("spotify_taken", None)
    st.session_state.pop("login_candidates", None)
    st.session_state.pop("login_name_submitted", None)
    st.rerun()


def _connect_gate() -> None:
    """Nobody reaches a Spotify tool before the profile has its own authorization.

    The redirect comes back two ways: a listener on 127.0.0.1:8888 (local), or
    pasting the URL Spotify landed on (anywhere, including a cloud deploy where
    nothing is listening on the viewer's machine). The state ties a redirect to
    this attempt; a mismatch is refused rather than trusted.
    """
    user_id = st.session_state.get("user_id")
    submitted = st.session_state.get("login_name_submitted")
    if not user_id and not submitted:
        return  # the name screen owns this run
    reconnect = st.session_state.get("reconnect") or st.session_state.get("auth_failed")
    auth = None
    if user_id:
        try:
            auth = store.get_spotify_auth(user_id)
        except RuntimeError:  # a lost/rotated Fernet key also needs fresh authorization
            st.session_state.auth_failed = True
            st.session_state.auth_ok = False
            reconnect = True
    if auth and not reconnect:
        st.session_state.auth_ok = True
        return

    st.title("🎧 Spotify MCP")
    st.subheader("Connect Spotify")
    st.caption(
        "Every call runs on your own authorization, so nothing works until the account is linked."
    )

    if taken_id := st.session_state.get("spotify_taken"):
        taken = store.get_user(taken_id)
        if not taken:
            st.session_state.pop("spotify_taken", None)
            st.rerun()
        # the Spotify id is the strong identity, so a bind collision resolves here
        st.warning(f"That Spotify account is already linked as **{taken['display_name']}**.")
        if st.button(f"Continue as {taken['display_name']}", key="taken_yes", type="primary"):
            spotify_oauth.forget_state(st.session_state.get("oauth_state", ""))
            store.touch(taken["id"])
            st.session_state.user_id = taken["id"]
            st.session_state.user_name = taken["display_name"]
            st.session_state.auth_ok = True
            for key in ("thread", "chat", "pending", "mode", "reconnect"):
                st.session_state.pop(key, None)  # a different profile needs a new conversation
            st.session_state.pop("auth_failed", None)
            st.session_state.pop("spotify_taken", None)
            st.session_state.pop("oauth_state", None)
            st.session_state.pop("oauth_paste", None)
            st.session_state.pop("login_name_submitted", None)
            st.rerun()
        if st.button("Use a different account", key="taken_no"):
            spotify_oauth.forget_state(st.session_state.get("oauth_state", ""))
            st.session_state.pop("spotify_taken", None)
            st.session_state.pop("user_id", None)
            st.session_state.pop("login_name_submitted", None)
            st.session_state.pop("oauth_state", None)
            st.session_state.pop("oauth_paste", None)
            st.rerun()
        st.stop()

    if st.session_state.get("auth_failed"):
        st.warning(
            "The stored Spotify authorization cannot be used. Reconnect below."
        )
    state = st.session_state.get("oauth_state") or spotify_oauth.new_state()
    st.session_state.oauth_state = state
    spotify_oauth.register_state(state)
    listening = spotify_oauth.start_listener()
    st.markdown(
        f"1. open [the Spotify authorize page]({spotify_oauth.authorize_url(state)}) and approve"
    )
    st.caption(
        "listening on `http://127.0.0.1:8888/callback`, so the approval lands here"
        if listening
        else "not listening locally (port busy, or a cloud deploy): paste the URL instead"
    )
    pasted = st.text_input("or paste the URL Spotify sent you to", key="oauth_paste")
    submitted_paste = st.button("Use pasted redirect", key="oauth_submit")
    result = spotify_oauth.poll_listener(state)
    if not result and submitted_paste:
        result = spotify_oauth.parse_paste_url(pasted)
        if not result:
            st.error("Paste the complete Spotify redirect URL, including code and state.")
            st.stop()
    if not result:
        if st.button("I approved it — check again", key="oauth_check"):
            st.rerun()
        st.stop()
    if result.get("state") != state:
        st.error(
            "That redirect is not from this screen (state mismatch). Approve again from the link above."
        )
        st.stop()
    if result.get("error"):
        st.error(f"Spotify said: {result['error']}")
        st.stop()

    try:
        with st.spinner("linking the account..."):
            token = spotify_oauth.exchange(result["code"])
            profile = spotify_oauth.me(token["access_token"])
    except (RuntimeError, ValueError, KeyError) as exc:
        st.error(f"Could not connect Spotify: {exc}. Approve again and try a new redirect.")
        st.stop()
    existing = store.find_by_spotify_id(profile["id"])
    if existing and existing["id"] != user_id:
        st.session_state.spotify_taken = existing["id"]
        st.rerun()
    if user_id:
        bound = store.get_user(user_id)
        if bound["spotify_user_id"] and bound["spotify_user_id"] != profile["id"]:
            st.error("This profile belongs to another Spotify account. Reconnect that account.")
            st.stop()
    if not user_id:
        # Create and bind together; a concurrent connect to this Spotify id
        # returns the existing profile rather than leaving an orphan name.
        user, created = store.create_spotify_user(
            submitted or profile.get("display_name") or profile["id"],
            token["refresh_token"], spotify_user_id=profile["id"],
            spotify_name=profile.get("display_name") or profile["id"],
            scopes=token.get("scope", ""),
        )
        if not created:
            st.session_state.spotify_taken = user["id"]
            st.rerun()
        st.session_state.user_id = user["id"]
        st.session_state.user_name = user["display_name"]
        user_id = user["id"]
        _finish_connect(user_id, token, profile, already_saved=True)
    else:
        _finish_connect(user_id, token, profile)


_login_gate()
_connect_gate()


@st.cache_resource
def _tools():
    """Every tool from both servers, plus which server app answers for each name."""
    home: dict[str, object] = {}
    listed = []
    for server in (app, psych_app):
        for tool in asyncio.run(server.list_tools()):
            listed.append(tool)
            home[tool.name] = server
    return listed, home


def _widget(key: str, spec: dict, required: bool):
    kind = spec.get("type")
    default = spec.get("default")
    label = f"{key} *" if required else key

    if key == "time_range":
        return st.selectbox(label, ["short_term", "medium_term", "long_term"])
    if kind == "integer":
        # the ceiling follows the tool's own default: paging tools default to 200, and a
        # hardcoded 50 here made Streamlit refuse to render them at all
        top = max(50, default or 0)  # tests/test_ui.py renders every form to keep this honest
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


def _copy_button(text: str) -> None:
    """A one-click copy button under a finished reply.

    Rendered as a tiny HTML iframe because Streamlit has no clipboard call of its
    own. Some browsers refuse the clipboard API inside an iframe, so the old
    execCommand path is the fallback; between them every browser is covered.
    json.dumps makes the text a safe JS literal, and the "</" escape stops a literal
    </script> inside a reply from ending the script block early.
    """
    payload = json.dumps(text).replace("</", "<\\/")
    st.iframe(
        f"""<style>
        /* the iframe body ships an 8px margin, which pushed the button out of the
           viewport and drew scrollbars around a clipped chip */
        body {{ margin: 0; overflow: hidden; }}
        </style>
        <button id="b" title="copy reply" style="font: 15px sans-serif;
            padding: 2px 10px; border: 1px solid #80849555; border-radius: 6px;
            background: transparent; cursor: pointer;">&#128203;</button>
        <script>
        const text = {payload};
        const b = document.getElementById("b");
        const flash = () => {{
            b.textContent = "\\u2713";
            setTimeout(() => b.textContent = "\\ud83d\\udccb", 1200);
        }};
        b.onclick = async () => {{
            try {{ await navigator.clipboard.writeText(text); flash(); }}
            catch (e) {{
                const ta = document.createElement("textarea");
                ta.value = text;
                document.body.appendChild(ta);
                ta.select();
                document.execCommand("copy");
                ta.remove();
                flash();
            }}
        }};
        </script>""",
        width=64,
        height=36,
    )


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

    def show(kind: str, text: str) -> None:
        if kind == "auth_error":
            _require_reconnect()
        else:
            parts.append(("text" if kind == "token" else kind, text))

    with st.chat_message("assistant"):
        if not approve:
            st.markdown("_declined_")
            parts.append(("text", "_declined_"))
        with st.spinner("continuing..."):
            try:
                st.session_state.pending = asyncio.run(
                    agent.decide(
                        approve,
                        show,
                        checkpointer=_memory(),
                        thread_id=st.session_state.thread,
                        mode=st.session_state.mode,
                        spotify_refresh_token=_my_refresh(),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - surface model and API failures
                if type(exc).__module__.startswith("streamlit"):
                    raise  # a rerun or Stop, not a failure
                st.error(f"{type(exc).__name__}: {exc}")
                return
    if st.session_state.get("auth_failed"):
        st.rerun()
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
                # the answer text only, not the tool-call lines
                if reply := "\n\n".join(t for k, t in parts if k == "text").strip():
                    _copy_button(reply)
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
            if kind == "auth_error":
                _require_reconnect()
                st.warning("Spotify disconnected. Reconnect your account to keep using its tools.")
                return
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
                        spotify_refresh_token=_my_refresh(),
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
    # always rerun: history becomes the single render path, which shows the approval
    # buttons when calls wait, and gives the finished reply its copy button
    st.rerun()


tools, _home = _tools()
names = [t.name for t in tools]

st.title("🎧 Spotify MCP")
st.caption("An agent over the tools, or the tools by hand.")

with st.sidebar:
    st.header("Mode")
    mode = st.radio("Mode", ["Agent", "Tools"], label_visibility="collapsed")
    st.divider()
    if uid := st.session_state.get("user_id"):
        auth = store.get_spotify_auth(uid)
        st.caption(
            f"signed in as **{st.session_state.user_name}** · "
            + (
                f"Spotify **{auth['spotify_name']}**"
                if auth
                else "no Spotify binding"
            )
        )
        if st.button("Reconnect Spotify"):
            # the connect screen re-runs OAuth and overwrites the stored token
            st.session_state.auth_ok = False
            st.session_state.reconnect = True
            st.rerun()
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
                with spotify_mcp.use_refresh_token(_my_refresh()):
                    result = asyncio.run(_home[tool.name].call_tool(tool.name, args))
            except Exception as exc:  # noqa: BLE001 - surface anything the API throws
                st.error(f"{type(exc).__name__}: {exc}")
                if spotify_mcp.SpotifyAuthError.MARKER in str(exc):
                    # refused refresh: the account has to reconnect, not retry
                    _require_reconnect()
                    st.rerun()
                if "missing env var" in str(exc):
                    st.info("Run `python get_token.py` first to write .env")
            else:
                # FastMCP 1.x returns (content_blocks, structured_result)
                blocks = result[0] if isinstance(result, tuple) else result
                texts = [getattr(block, "text", str(block)) for block in blocks]
                for text in texts:
                    _render(text)
                if any(spotify_mcp.SpotifyAuthError.MARKER in t for t in texts):
                    # a tool error reaches the UI as text, never as the type
                    _require_reconnect()
                    st.rerun()
