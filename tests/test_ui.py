"""Drive the Streamlit app headlessly: the login gates, then the mode controls.

    python tests/test_ui.py

Streamlit's own test harness runs the real script, so this catches the widget-state
rules that only bite at runtime. It calls no model and spends no credits. The
Spotify OAuth legs are faked in-process; the gates, the store, and the paste
parser are the real ones.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # the repo modules both this test and the script import

from cryptography.fernet import Fernet  # noqa: E402 - after the path fix
from streamlit.testing.v1 import AppTest  # noqa: E402

import spotify_oauth  # noqa: E402
import store  # noqa: E402
import spotify_mcp  # noqa: E402
import agent  # noqa: E402

import db_sync  # noqa: E402 - its .env load must happen before the pops below

STATE = "STATE"
_HELD_DB = store.DB
_HELD_KEY = os.environ.get(store.KEY_VAR)
_HELD_SYNC = tuple(os.environ.get(v) for v in (db_sync.GIST_ID_VAR, db_sync.TOKEN_VAR))
_TEMP_DB: tempfile.TemporaryDirectory | None = None

# the real functions, restored on the way out so a later import sees the truth
_REAL = {
    name: getattr(spotify_oauth, name)
    for name in ("new_state", "start_listener", "poll_listener", "exchange", "me")
}


def _fake_oauth() -> None:
    """Stand-in legs for the OAuth flow; the gates, store, and parser stay real."""
    global _TEMP_DB
    _TEMP_DB = tempfile.TemporaryDirectory()
    store.DB = os.path.join(_TEMP_DB.name, "users.db")
    os.environ[store.KEY_VAR] = Fernet.generate_key().decode()
    # no profile pushes from a test run, even on a machine with sync configured
    os.environ.pop(db_sync.GIST_ID_VAR, None)
    os.environ.pop(db_sync.TOKEN_VAR, None)
    spotify_oauth.new_state = lambda: STATE
    spotify_oauth.start_listener = lambda: False
    spotify_oauth.poll_listener = lambda state: None
    spotify_oauth.exchange = lambda code: {
        "access_token": "A",
        "refresh_token": "R",
        "expires_in": 3600,
        "scope": spotify_oauth.SCOPES,
    }
    spotify_oauth.me = lambda tok: {"id": "SID", "display_name": "Spotify Name"}


def _restore_oauth() -> None:
    for name, fn in _REAL.items():
        setattr(spotify_oauth, name, fn)
    store.DB = _HELD_DB
    if _HELD_KEY is None:
        os.environ.pop(store.KEY_VAR, None)
    else:
        os.environ[store.KEY_VAR] = _HELD_KEY
    for var, held in zip((db_sync.GIST_ID_VAR, db_sync.TOKEN_VAR), _HELD_SYNC):
        if held is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = held
    if _TEMP_DB is not None:
        _TEMP_DB.cleanup()


def _new_app() -> AppTest:
    return AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=180)


def _enter_name(app: AppTest, name: str) -> None:
    app.text_input(key="login_name").set_value(name).run()
    app.button(key="login_continue").click().run()


def _fake_me(sid: str = "SID", name: str = "Spotify Name") -> None:
    """Which account /me vouches for this flow; unique ids avoid real collisions."""
    spotify_oauth.me = lambda tok: {"id": sid, "display_name": name}


def _paste_connect(app: AppTest, state: str = STATE, code: str = "CODE") -> None:
    url = f"{spotify_oauth.REDIRECT}?code={code}&state={state}"
    app.text_input(key="oauth_paste").set_value(url).run()
    app.button(key="oauth_submit").click().run()


def _ss(app: AppTest, key: str):
    """A session_state read that tolerates a missing key (no .get on SafeSessionState)."""
    return app.session_state[key] if key in app.session_state else None


def _assert_clean(app: AppTest, where: str) -> None:
    assert not app.exception, f"{where}: {app.exception[0].message if app.exception else ''}"


def main() -> None:
    _fake_oauth()
    try:
        _fresh_name_flow()
        _returning_name_flow()
        _new_account_collision_flow()
        _wrong_state_refused()
        _listener_flow()
        _mode_controls()
        _reconnect_flow()
        _stale_scope_hint_flow()
        _incomplete_redirect()
        _tools_use_signed_in_account()
        _agent_failure_requires_reconnect()
        _lost_encryption_key_flow()
    finally:
        _restore_oauth()
    print("ok - gates bind the right profiles, buttons and slash commands stay in step")


def _fresh_name_flow() -> AppTest:
    """A name nobody uses stores nothing until Spotify authorizes, then binds."""
    app = _new_app()
    app.run()
    assert len(app.get("text_input")) == 1, "the name screen should own the first run"
    _enter_name(app, "lemon")
    _assert_clean(app, "name entry")
    assert not _ss(app, "user_id"), "nothing stored before the connect"
    assert app.text_input(key="oauth_paste"), "the connect screen should own the run"
    _paste_connect(app)
    _assert_clean(app, "connect")
    uid = app.session_state.user_id
    auth = store.get_spotify_auth(uid)
    assert auth and auth["refresh_token"] == "R", auth
    assert store.find_by_spotify_id("SID")["id"] == uid, "binding keyed by spotify id"
    assert app.session_state.user_name == "lemon"
    return app


def _mode_controls() -> None:
    """The original checks: buttons and slash commands stay in step."""
    _fake_me("MID")
    spotify_oauth.poll_listener = lambda state: None  # no listener capture
    app = _new_app()
    app.run()
    _enter_name(app, "modes")
    _paste_connect(app)
    _assert_clean(app, "login for mode controls")
    picker = lambda: app.get("button_group")[0]  # noqa: E731 - re-read after each run
    assert app.session_state.mode == "afk", app.session_state.mode
    assert picker().value == "afk", picker().value

    picker().set_value("manual").run()
    assert app.session_state.mode == "manual", "button did not set the mode"
    _assert_clean(app, "manual pick")

    # the regression: writing a widget key after the widget rendered raises, so the
    # slash command must move the buttons by changing the key, not by assignment
    app.chat_input[0].set_value("/auto").run()
    _assert_clean(app, "/auto")
    assert app.session_state.mode == "auto", app.session_state.mode
    assert picker().value == "auto", f"buttons stale at {picker().value}"

    picker().set_value("afk").run()
    assert app.session_state.mode == "afk", "button did not override the command"

    app.chat_input[0].set_value("/nonsense").run()
    _assert_clean(app, "/nonsense")
    assert "No such command" in app.session_state.chat[-1][1][0][1]
    assert app.session_state.mode == "afk", "a bad command changed the mode"

    _forms(app)


def _returning_name_flow() -> None:
    """A known name gets cards; 'Yes, it's me' enters with the stored binding."""
    uid = store.find_by_spotify_id("SID")["id"]
    app = _new_app()
    app.run()
    _enter_name(app, "LEMON")  # case-insensitive lookup
    _assert_clean(app, "name entry")
    assert _ss(app, "login_candidates"), "matches should show as cards"
    app.button(key=f"login_yes_{uid}").click().run()
    _assert_clean(app, "confirm")
    assert app.session_state.user_id == uid, "the card's profile, not a new one"
    assert app.session_state.auth_ok, "a bound profile needs no reconnect"


def _new_account_collision_flow() -> None:
    """'No, new account' still lands on the Spotify id's real profile."""
    _fake_me("SID")  # the same id the fresh-name flow bound
    uid = store.find_by_spotify_id("SID")["id"]
    app = _new_app()
    app.run()
    _enter_name(app, "lemon")
    app.button(key="login_new").click().run()
    _assert_clean(app, "new account")
    assert not _ss(app, "user_id"), "still nothing before the connect"
    _paste_connect(app)  # same Spotify id SID, already bound
    _assert_clean(app, "collision")
    taken = _ss(app, "spotify_taken")
    assert taken == uid, taken
    app.button(key="taken_yes").click().run()
    _assert_clean(app, "taken confirm")
    assert app.session_state.user_id == uid, "the id's profile wins over the typed name"
    assert app.session_state.auth_ok


def _wrong_state_refused() -> None:
    """A redirect from another attempt is refused, not trusted."""
    _fake_me("WID")
    app = _new_app()
    app.run()
    _enter_name(app, "sam")
    _paste_connect(app, state="someone-elses-attempt")
    _assert_clean(app, "wrong state")
    assert not _ss(app, "user_id"), "a mismatched state must not log in"
    assert len(app.get("error")) == 1, "the refusal should say so"
    # the same state, once approved properly, still works from this screen
    _paste_connect(app)
    assert store.get_spotify_auth(app.session_state.user_id)["refresh_token"] == "R"


def _listener_flow() -> None:
    """The local listener path: poll hands over what the callback caught."""
    _fake_me("LID")
    spotify_oauth.start_listener = lambda: True
    spotify_oauth.poll_listener = lambda state: {"code": "CODE", "state": state}
    app = _new_app()
    app.run()
    _enter_name(app, "meringue")
    _assert_clean(app, "listener")
    assert store.get_spotify_auth(app.session_state.user_id)["refresh_token"] == "R"


def _reconnect_flow() -> None:
    """Manual reconnect and a refused refresh must not reuse the old binding."""
    app = _new_app()
    app.run()
    _enter_name(app, "lemon")
    uid = store.find_by_spotify_id("SID")["id"]
    app.button(key=f"login_yes_{uid}").click().run()
    _assert_clean(app, "before reconnect")
    app.sidebar.button[0].click().run()
    _assert_clean(app, "manual reconnect")
    assert app.text_input(key="oauth_paste"), "reconnect must show the OAuth screen"
    _fake_me("SID")
    spotify_oauth.exchange = lambda code: {
        "access_token": "new-access", "refresh_token": "renewed",
        "scope": spotify_oauth.SCOPES,
    }
    _paste_connect(app)
    _assert_clean(app, "reconnect completion")
    assert store.get_spotify_auth(uid)["refresh_token"] == "renewed"

    refused = _new_app()
    refused.run()
    _enter_name(refused, "lemon")
    refused.button(key=f"login_yes_{uid}").click().run()
    refused.session_state.auth_ok = False
    refused.session_state.auth_failed = True
    refused.run()
    _assert_clean(refused, "refused refresh")
    assert refused.text_input(key="oauth_paste"), "a refused token must require OAuth"

    # An unbound different Spotify id cannot silently replace this profile.
    _fake_me("not-SID")
    _paste_connect(refused)
    _assert_clean(refused, "reconnect wrong account")
    assert len(refused.error) == 1
    assert store.get_spotify_auth(uid)["spotify_user_id"] == "SID"


def _stale_scope_hint_flow() -> None:
    """A grant missing newly requested scopes says so in the sidebar, up front."""
    user = store.create_user("stale")
    old_grant = " ".join(
        s for s in spotify_oauth.SCOPES.split() if s != "user-modify-playback-state"
    )
    store.save_spotify_auth(
        user["id"], "refresh-stale", scopes=old_grant,
        spotify_user_id="spotify-stale", spotify_name="stale",
    )
    app = _new_app()
    app.run()
    _enter_name(app, "stale")
    app.button(key=f"login_yes_{user['id']}").click().run()
    _assert_clean(app, "stale grant login")
    assert len(app.sidebar.warning) == 1, "the missing feature should be visible"
    assert "playback controls" in app.sidebar.warning[0].value
    # a reconnect with the full grant retires the hint
    _fake_me("spotify-stale", "stale")
    app.sidebar.button[0].click().run()
    _paste_connect(app)
    _assert_clean(app, "stale grant reconnect")
    assert len(app.sidebar.warning) == 0
    assert store.get_spotify_auth(user["id"])["scopes"] == spotify_oauth.SCOPES


def _incomplete_redirect() -> None:
    """Incomplete paste should stay on connect and never exchange a code."""
    app = _new_app()
    app.run()
    _enter_name(app, "incomplete")
    app.text_input(key="oauth_paste").set_value(
        f"{spotify_oauth.REDIRECT}?state={STATE}"
    ).run()
    app.button(key="oauth_submit").click().run()
    _assert_clean(app, "incomplete redirect")
    assert not _ss(app, "user_id")


def _lost_encryption_key_flow() -> None:
    """A valid but changed key should lead to reauthorization, not a UI crash."""
    old_key = os.environ[store.KEY_VAR]
    os.environ[store.KEY_VAR] = Fernet.generate_key().decode()
    try:
        app = _new_app()
        app.run()
        _enter_name(app, "lemon")
        uid = store.find_by_spotify_id("SID")["id"]
        app.button(key=f"login_yes_{uid}").click().run()
        _assert_clean(app, "changed key")
        assert app.text_input(key="oauth_paste")
    finally:
        os.environ[store.KEY_VAR] = old_key


def _tools_use_signed_in_account() -> None:
    """Run a real FastMCP tool in each UI session, with HTTP mocked at the edge."""
    seen = []
    for name in ("alice", "bob"):
        user = store.create_user(name)
        store.save_spotify_auth(
            user["id"], f"refresh-{name}", scopes=spotify_oauth.SCOPES,
            spotify_user_id=f"spotify-{name}", spotify_name=name,
        )
        app = _new_app()
        app.run()
        _enter_name(app, name)
        app.button(key=f"login_yes_{user['id']}").click().run()
        app.sidebar.radio[0].set_value("Tools").run()
        app.selectbox[0].set_value("now_playing").run()

        def mock_call(*args, **kwargs):
            seen.append(spotify_mcp._refresh_token())
            return {}

        with patch.object(spotify_mcp, "_call", side_effect=mock_call):
            next(b for b in app.button if b.label == "Run").click().run()
        _assert_clean(app, f"Tools mode for {name}")
    assert seen == ["refresh-alice", "refresh-bob"], seen


def _agent_failure_requires_reconnect() -> None:
    """The subprocess's tool error must make the agent UI reconnect as well."""
    user = store.find_users("alice")[0]
    app = _new_app()
    app.run()
    _enter_name(app, "alice")
    app.button(key=f"login_yes_{user['id']}").click().run()

    async def failed_turn(question, on_part, **kwargs):
        assert kwargs["spotify_refresh_token"] == "refresh-alice"
        on_part("auth_error", "Spotify disconnected: invalid_grant")
        return []

    with patch.object(agent, "turn", side_effect=failed_turn), patch.dict(
        os.environ, {agent.KEY_VAR: "test-openrouter-key"}
    ):
        app.chat_input[0].set_value("my listening").run()
    _assert_clean(app, "agent auth error")
    assert app.text_input(key="oauth_paste"), "agent tool failure must show reconnect"


def _forms(app: AppTest) -> None:
    """Render the form of every tool.

    Streamlit refuses a number_input whose value is above max_value, so the day a tool
    default went to 200 against a hardcoded ceiling of 50, most of the Tools page threw
    before drawing anything. The widget bounds have to follow the schema.
    """
    app.sidebar.radio[0].set_value("Tools").run()
    _assert_clean(app, "tools mode")
    names = app.selectbox[0].options
    assert len(names) > 10, names
    for name in names:
        app.selectbox[0].set_value(name).run()
        assert not app.exception, f"{name}: {app.exception[0].message}"


if __name__ == "__main__":
    main()
