import os
import tempfile
import unittest

from cryptography.fernet import Fernet

import db_sync
import store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._held = store.DB
        store.DB = os.path.join(self.tmp.name, "users.db")
        # a fixed key: the real one is generated into .env on first use, and a
        # test must not write there
        self._held_key = os.environ.get(store.KEY_VAR)
        os.environ[store.KEY_VAR] = Fernet.generate_key().decode()
        # no profile pushes from a test run, even on a machine with sync configured
        self._held_sync = tuple(os.environ.get(v) for v in (db_sync.GIST_ID_VAR, db_sync.TOKEN_VAR))
        os.environ.pop(db_sync.GIST_ID_VAR, None)
        os.environ.pop(db_sync.TOKEN_VAR, None)

    def tearDown(self):
        store.DB = self._held
        if self._held_key is None:
            os.environ.pop(store.KEY_VAR, None)
        else:
            os.environ[store.KEY_VAR] = self._held_key
        for var, held in zip((db_sync.GIST_ID_VAR, db_sync.TOKEN_VAR), self._held_sync):
            if held is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = held
        self.tmp.cleanup()

    def test_create_and_find_case_insensitive(self):
        user = store.create_user("lemon")
        self.assertTrue(user["id"])
        self.assertIsNone(user["spotify_user_id"])
        found = store.find_users("LEMON")
        self.assertEqual([u["id"] for u in found], [user["id"]])
        self.assertEqual(found[0]["display_name"], "lemon")
        self.assertEqual(store.find_users("nope"), [])

    def test_duplicate_names_list_oldest_first(self):
        first = store.create_user("sam")
        second = store.create_user("sam")
        ids = [u["id"] for u in store.find_users("sam")]
        self.assertEqual(ids, [first["id"], second["id"]])

    def test_names_are_trimmed_and_collapsed(self):
        user = store.create_user("  lemon   meringue ")
        self.assertEqual(user["display_name"], "lemon meringue")
        with self.assertRaises(ValueError):
            store.create_user("   ")

    def test_touch_updates_last_seen(self):
        user = store.create_user("lemon")
        store.touch(user["id"])
        after = store.get_user(user["id"])
        self.assertGreaterEqual(after["last_seen_at"], user["last_seen_at"])

    def test_get_user_missing_is_none(self):
        self.assertIsNone(store.get_user("does-not-exist"))

    def test_link_spotify_binds_profile(self):
        user = store.create_user("lemon")
        store.link_spotify(user["id"], "spotify_user_1")
        self.assertEqual(store.get_user(user["id"])["spotify_user_id"], "spotify_user_1")

    def test_data_survives_reopen(self):
        user = store.create_user("lemon")
        store.touch(user["id"])
        again = store.get_user(user["id"])
        self.assertEqual(again["display_name"], "lemon")

    def test_spotify_auth_round_trip(self):
        user = store.create_user("lemon")
        self.assertIsNone(store.get_spotify_auth(user["id"]))
        store.save_spotify_auth(
            user["id"],
            "the-refresh-token",
            scopes="user-top-read",
            spotify_user_id="sid123",
            spotify_name="Lemon On Spotify",
        )
        auth = store.get_spotify_auth(user["id"])
        self.assertEqual(auth["refresh_token"], "the-refresh-token")
        self.assertEqual(auth["spotify_user_id"], "sid123")
        self.assertEqual(auth["spotify_name"], "Lemon On Spotify")
        self.assertTrue(auth["auth_at"])

    def test_token_is_encrypted_at_rest(self):
        import sqlite3

        user = store.create_user("lemon")
        store.save_spotify_auth(
            user["id"],
            "plaintext-secret",
            scopes="s",
            spotify_user_id="sid",
            spotify_name="n",
        )
        con = sqlite3.connect(store.DB)
        row = con.execute(
            "SELECT refresh_enc FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        con.close()
        self.assertNotIn("plaintext-secret", row[0], "token stored in the clear")
        self.assertTrue(row[0])

    def test_save_spotify_auth_overwrites(self):
        """A reconnect writes through the same seam: one profile, newest token."""
        user = store.create_user("lemon")
        for i in range(2):
            store.save_spotify_auth(
                user["id"],
                f"token-{i}",
                scopes="s",
                spotify_user_id=f"sid{i}",
                spotify_name="n",
            )
        auth = store.get_spotify_auth(user["id"])
        self.assertEqual(auth["refresh_token"], "token-1")
        self.assertEqual(auth["spotify_user_id"], "sid1")

    def test_find_by_spotify_id(self):
        self.assertIsNone(store.find_by_spotify_id("nobody"))
        user = store.create_user("lemon")
        store.save_spotify_auth(
            user["id"], "t", scopes="s", spotify_user_id="sid9", spotify_name="n"
        )
        found = store.find_by_spotify_id("sid9")
        self.assertEqual(found["id"], user["id"])
        self.assertEqual(found["display_name"], "lemon")

    def test_new_profile_is_bound_atomically_and_duplicate_id_does_not_create_orphan(self):
        first, created = store.create_spotify_user(
            "sam", "first-token", spotify_user_id="same-spotify",
            spotify_name="Sam Music", scopes="user-top-read",
        )
        self.assertTrue(created)
        second, created = store.create_spotify_user(
            "another sam", "second-token", spotify_user_id="same-spotify",
            spotify_name="Sam Music", scopes="user-top-read",
        )
        self.assertFalse(created)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(store.find_users("another sam"), [])
        self.assertEqual(store.get_spotify_auth(first["id"])["refresh_token"], "first-token")

    def test_old_schema_migrates(self):
        """A database made before the auth columns existed still carries them."""
        import sqlite3

        con = sqlite3.connect(store.DB)
        con.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, display_name TEXT NOT NULL, "
            "spotify_user_id TEXT UNIQUE, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL)"
        )
        con.execute(
            "INSERT INTO users VALUES ('oldid', 'lemon', NULL, '2026-01-01', '2026-01-01')"
        )
        con.commit()
        con.close()
        self.assertIsNone(store.get_spotify_auth("oldid"))  # migration runs on connect
        store.save_spotify_auth(
            "oldid", "t", scopes="s", spotify_user_id="sidx", spotify_name="n"
        )
        self.assertEqual(store.get_spotify_auth("oldid")["refresh_token"], "t")

    def test_find_users_sees_auth_columns(self):
        """The login cards need binding metadata, not encrypted token material."""
        user = store.create_user("lemon")
        store.save_spotify_auth(
            user["id"], "t", scopes="s", spotify_user_id="sidz", spotify_name="Spotifyy"
        )
        row = store.find_users("lemon")[0]
        self.assertTrue(row["connected"])
        self.assertEqual(row["spotify_name"], "Spotifyy")
        self.assertNotIn("refresh_enc", row)
        self.assertNotIn("refresh_enc", store.get_user(user["id"]))
        self.assertNotIn("refresh_enc", store.find_by_spotify_id("sidz"))


if __name__ == "__main__":
    unittest.main()
