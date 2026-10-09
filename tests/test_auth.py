"""Offline regression tests for per-profile OAuth and Spotify tool isolation."""

import threading
import unittest
import urllib.request
import urllib.error
import asyncio
import os
from http.server import HTTPServer
from unittest.mock import Mock, patch

import spotify_mcp
import spotify_oauth
import agent
from langchain_core.messages import ToolMessage


class OAuthTests(unittest.TestCase):
    def test_pasted_redirect_must_be_the_registered_callback(self):
        self.assertIsNone(spotify_oauth.parse_paste_url(
            "https://someone.example/callback?code=stolen&state=good"
        ))
        self.assertIsNone(spotify_oauth.parse_paste_url(
            "http://127.0.0.1:8888/other?code=stolen&state=good"
        ))
        self.assertIsNone(spotify_oauth.parse_paste_url(
            "http://127.0.0.1:8888/callback?code=a&code=b&state=good"
        ))
        self.assertEqual(
            spotify_oauth.parse_paste_url(
                "http://127.0.0.1:8888/callback?code=ok&state=good"
            ),
            {"code": "ok", "state": "good"},
        )

    def test_callback_from_two_sessions_is_not_stolen_by_first_poll(self):
        with spotify_oauth._lock:
            spotify_oauth._captured.clear()
        spotify_oauth.register_state("first")
        spotify_oauth.register_state("second")
        server = HTTPServer(("127.0.0.1", 0), spotify_oauth._Callback)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/callback?code=two&state=second"
            ) as response:
                self.assertEqual(response.status, 200)
            self.assertIsNone(spotify_oauth.poll_listener("first"))
            self.assertEqual(
                spotify_oauth.poll_listener("second"), {"code": "two", "state": "second"}
            )
            self.assertIsNone(spotify_oauth.poll_listener("second"))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_expired_state_rejects_callback(self):
        spotify_oauth.register_state("expired-state")
        with spotify_oauth._lock:
            spotify_oauth._pending["expired-state"] -= spotify_oauth.STATE_TTL + 1
        server = HTTPServer(("127.0.0.1", 0), spotify_oauth._Callback)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/callback?code=late&state=expired-state"
                )
            self.assertEqual(caught.exception.code, 400)
            self.assertIsNone(spotify_oauth.poll_listener("expired-state"))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_exchange_rejects_missing_scope_and_refresh_token(self):
        complete = {"access_token": "access", "refresh_token": "refresh", "scope": spotify_oauth.SCOPES}
        credentials = {"SPOTIFY_CLIENT_ID": "test-id", "SPOTIFY_CLIENT_SECRET": "test-secret"}
        with patch.dict(os.environ, credentials), patch.object(spotify_oauth._http, "post", return_value=Mock(status_code=200, json=lambda: {**complete, "scope": "user-top-read"})):
            with self.assertRaisesRegex(ValueError, "scope"):
                spotify_oauth.exchange("code")
        with patch.dict(os.environ, credentials), patch.object(spotify_oauth._http, "post", return_value=Mock(status_code=200, json=lambda: {**complete, "refresh_token": ""})):
            with self.assertRaisesRegex(ValueError, "refresh_token"):
                spotify_oauth.exchange("code")

    def test_cli_exchange_uses_prompted_client_credentials(self):
        complete = {"access_token": "access", "refresh_token": "refresh", "scope": spotify_oauth.SCOPES}
        with patch.object(spotify_oauth._http, "post", return_value=Mock(status_code=200, json=lambda: complete)) as post:
            spotify_oauth.exchange("code", client_id="prompted-id", client_secret="prompted-secret")
        self.assertEqual(post.call_args.kwargs["auth"], ("prompted-id", "prompted-secret"))


class SpotifyIsolationTests(unittest.TestCase):
    def test_401_retries_with_a_fresh_access_token(self):
        response = lambda status: Mock(
            status_code=status, content=b"{}", headers={}, text="unauthorized",
            json=lambda: {"ok": True},
        )
        with patch.object(spotify_mcp, "_token", side_effect=["old", "new"]), \
             patch.object(spotify_mcp._http, "request", side_effect=[response(401), response(200)]) as request, \
             spotify_mcp.use_refresh_token("alice"):
            self.assertEqual(spotify_mcp._call("GET", "/me"), {"ok": True})
        self.assertEqual(
            [call.kwargs["headers"]["Authorization"] for call in request.call_args_list],
            ["Bearer old", "Bearer new"],
        )

    def test_concurrent_tool_contexts_do_not_switch_accounts(self):
        barrier = threading.Barrier(2)
        results = {}

        def call(profile):
            with spotify_mcp.use_refresh_token(profile):
                barrier.wait(timeout=3)
                results[profile] = spotify_mcp._refresh_token()

        workers = [threading.Thread(target=call, args=(p,)) for p in ("alice", "bob")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=4)
        self.assertEqual(results, {"alice": "alice", "bob": "bob"})

    def test_playlist_cache_is_keyed_by_account(self):
        calls = []

        def pages(*args, **kwargs):
            calls.append(spotify_mcp._refresh_token())
            return [{"id": "one", "name": spotify_mcp._refresh_token()}]

        with patch.object(spotify_mcp, "_pages", side_effect=pages):
            with spotify_mcp.use_refresh_token("alice"):
                self.assertEqual(spotify_mcp._playlists()[0]["name"], "alice")
            with spotify_mcp.use_refresh_token("bob"):
                self.assertEqual(spotify_mcp._playlists()[0]["name"], "bob")
            with spotify_mcp.use_refresh_token("alice"):
                self.assertEqual(spotify_mcp._playlists()[0]["name"], "alice")
        self.assertEqual(calls, ["alice", "bob"])


class AgentAuthEventsTests(unittest.TestCase):
    def test_tool_auth_failure_reaches_ui_callback(self):
        class Graph:
            continued_after_failure = False

            async def astream(self, *args, **kwargs):
                yield "values", {"messages": [ToolMessage(
                    content="Spotify disconnected: invalid_grant",
                    tool_call_id="call-1", id="message-1",
                )]}
                self.continued_after_failure = True

        events = []
        graph = Graph()
        asyncio.run(agent._drive(graph, None, {}, lambda *event: events.append(event),
                                 mode="auto", gated=False))
        self.assertIn(("auth_error", "Spotify disconnected: invalid_grant"), events)
        self.assertFalse(graph.continued_after_failure)


if __name__ == "__main__":
    unittest.main()
