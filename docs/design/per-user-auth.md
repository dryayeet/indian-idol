# Per-user Spotify auth: flow and regression checks

Design decisions and implemented flow for binding Spotify authorization to name
selection. Started 2026-10-09; the roadmap entry this satisfies is
[roadmap](../roadmap.md) "Per-user Spotify auth with isolated library context".

## Goal

The CLIs control one Spotify account via `SPOTIFY_REFRESH_TOKEN` in `.env`.
The Streamlit UI gives every named profile its own Spotify authorization:
identity comes from the store's `user_id`, the Spotify binding comes from
Authorization Code flow with state, and every Spotify call the session makes
runs on that profile's token. The main `.env` account remains the fallback for
the CLIs.

## Login flow

```text
name entry
  ├─ no match ─────────────────────► Connect Spotify (OAuth) ──► /me id
  │                                        │                       │
  │                                        │                       ├─ id unbound ─► new profile + token
  │                                        │                       └─ id bound ───► offer that profile
  └─ match ─► "Is this your account?" cards
       │       (joined / last seen / connected as <spotify name>)
       ├─ "Yes, it's me" ─► token present ─► in
       │      token missing ─► Connect Spotify (binds this profile)
       └─ "No, new account" ─► Connect Spotify ─► /me id
              ├─ id unbound ─► new profile named as typed + token
              └─ id bound ───► offer that profile instead of a duplicate
```

- A first-time profile without a bound token gets the connect screen, not tools.
  A returning viewer who clicks "Yes, it's me" can reuse that profile's binding.
  **The name and confirmation card do not authenticate the viewer.** Keep the UI
  restricted to trusted viewers.
- A new profile is created and bound in one SQLite transaction after `/me`.
  Two simultaneous connects to the same Spotify id yield the existing profile,
  without replacing its token or leaving an unbound duplicate name.
- `session_state` holds profile metadata, conversation and OAuth state, never
  refresh tokens or their ciphertext. The store's public lookup methods do not
  return encrypted token columns.
- A refused refresh or a second 401 from Spotify prompts reconnect in both
  Tools mode and Agent mode. The sidebar also offers manual reconnect.
- Reconnect must reach OAuth even if the old token is still in SQLite. A token
  that cannot be decrypted after a key change also leads to reconnect. A
  different, unbound Spotify id cannot silently replace an existing binding.

## Connect transports

Same registered redirect URI for both: `http://127.0.0.1:8888/callback`.

- **Local**: a daemon-thread `HTTPServer` on 127.0.0.1:8888 catches the
  callback. Started once per Streamlit process. The viewer returns to the UI
  and clicks "I approved it, check again". Callbacks are kept by registered
  random `state` for up to 10 minutes, so one session cannot consume another's.
  If the port is busy, paste-back still works.
- **Paste-back**: the screen always offers a "paste the URL you were
  redirected to" box and a separate submit button. The URL must be the exact
  registered callback; code and state must be single, nonempty values and the
  state must match this session. A malformed paste does not call Spotify or
  repeatedly exchange a one-use code on rerun. This covers a hosted UI where
  the browser redirect hits the viewer's own machine and the code is still
  visible in the address bar.

After exchanging a code, the app checks that Spotify granted **all** requested
scopes and returned an access token and refresh token. `/me` must provide a
Spotify user id before a profile can be created or updated. Exchange failures
stay on the connect screen, allowing a new attempt.

## Pieces

| Piece | What |
|---|---|
| `spotify_oauth.py` (new) | `authorize_url`, `exchange`, `me`, `parse_paste_url`, local listener helpers, scopes constant. `--selfcheck` covers URL shape and paste parsing offline. |
| `store.py` | Columns `refresh_enc`, `scopes`, `spotify_name`, `auth_at`. Fernet key from `USERS_DB_KEY` in `.env`, auto-generated and written on first use. All encrypt/decrypt inside store; public profile reads omit ciphertext. `create_spotify_user` atomically creates and binds new users. Migration via `PRAGMA table_info` so an existing `data/users.db` survives. |
| `spotify_mcp.py` | Context-local `use_refresh_token()` for each in-process tool call; process env fallback for the CLIs; access-token and playlist caches keyed by refresh token; one 401 retry with a freshly obtained access token, then a reconnect error. |
| `agent.py` | `turn()`/`run()`/`decide()`/`collect()` take `spotify_refresh_token`, inject into the spawned Spotify MCP subprocess env per turn. Prompt playlist names become per-user for free. |
| `streamlit_app.py` | Name entry, confirmation cards, connect screen, reconnect on `SpotifyAuthError`; Tools mode binds the token only for the duration of its in-process call; Agent mode passes it into a dedicated MCP subprocess. |
| `get_token.py` | Refactored onto `spotify_oauth.py`; CLI behavior unchanged. |
| `requirements.txt` | `cryptography`. |
| `.env.example` | `USERS_DB_KEY`. |

## Decisions (asked and answered)

1. Fresh name with no profile: **force connect first**. No Spotify tools
   until OAuth completes.
2. Transports: **local listener + paste-back**, cloud included.
3. Token plumbing in `spotify_mcp`: **per-turn injection**, not MCP protocol
   context propagation. Subprocess env for agent mode, context-local binding
   for one Tools-mode call. Concurrent sessions cannot share an active token.
4. Encryption: **Fernet key in `.env`** (`USERS_DB_KEY`), portable to cloud.
5. v1 extras: **reconnect on auth failure only**. Logout button, local
   revoke, 6-month banner stay on the roadmap.

## Tests

- `tests/test_store.py`: auth round-trip, ciphertext != plaintext, public
  queries omit ciphertext, migration of pre-auth schema, `find_by_spotify_id`.
- `tests/test_auth.py`: callback delivery to the correct state (actual local
  HTTP server), strict paste parsing, rejected/incomplete tokens and scopes,
  prompted CLI credentials, concurrent token contexts, per-account playlist
  cache, 401 recovery, and agent auth-error events.
- `spotify_oauth.py --selfcheck`: authorize URL shape, paste parser.
- `spotify_mcp.py --selfcheck`: active-token selection, per-token cache,
  auth-failure path, offline.
- `tests/test_ui.py`: headless Streamlit gates, same-name/collision flows,
  pasted/local redirect, mismatch and incomplete callback, reconnection,
  key change, Agent-mode error, and two real FastMCP tool calls with Spotify
  HTTP mocked. No model or live OAuth calls.
- `agent.py --selfcheck`: unchanged assertions still pass with the new
  parameter threaded through.

## Out of scope

- Logout button, local revoke, 6-month re-auth banner (next on the roadmap).
- Durable conversation memory (separate roadmap item).
- Public multi-user service posture: a name confirmation is not proof of
  identity. `data/users.db` is local SQLite; a hosted deployment needs
  persistent storage and a stable `USERS_DB_KEY` in its secrets to survive a
  process restart.
