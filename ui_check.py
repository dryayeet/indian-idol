"""Drive the Streamlit app headlessly and check the mode controls agree.

    python ui_check.py

Streamlit's own test harness runs the real script, so this catches the widget-state
rules that only bite at runtime. It calls no model and spends no credits.
"""

from streamlit.testing.v1 import AppTest


def main() -> None:
    app = AppTest.from_file("streamlit_app.py", default_timeout=180)
    app.run()
    picker = lambda: app.get("button_group")[0]  # noqa: E731 - re-read after each run
    assert app.session_state.mode == "afk", app.session_state.mode
    assert picker().value == "afk", picker().value

    picker().set_value("manual").run()
    assert app.session_state.mode == "manual", "button did not set the mode"
    assert not app.exception, app.exception

    # the regression: writing a widget key after the widget rendered raises, so the
    # slash command must move the buttons by changing the key, not by assignment
    app.chat_input[0].set_value("/auto").run()
    assert not app.exception, app.exception[0].message
    assert app.session_state.mode == "auto", app.session_state.mode
    assert picker().value == "auto", f"buttons stale at {picker().value}"

    picker().set_value("afk").run()
    assert app.session_state.mode == "afk", "button did not override the command"

    app.chat_input[0].set_value("/nonsense").run()
    assert not app.exception, app.exception
    assert "No such command" in app.session_state.chat[-1][1][0][1]
    assert app.session_state.mode == "afk", "a bad command changed the mode"

    _forms(app)
    print("ok — buttons and slash commands stay in step, every tool form renders")


def _forms(app) -> None:
    """Render the form of every tool.

    Streamlit refuses a number_input whose value is above max_value, so the day a tool
    default went to 200 against a hardcoded ceiling of 50, most of the Tools page threw
    before drawing anything. The widget bounds have to follow the schema.
    """
    app.sidebar.radio[0].set_value("Tools").run()
    names = app.selectbox[0].options
    assert len(names) > 10, names
    for name in names:
        app.selectbox[0].set_value(name).run()
        assert not app.exception, f"{name}: {app.exception[0].message}"


if __name__ == "__main__":
    main()
