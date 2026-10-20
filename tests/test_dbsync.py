"""The gist sync: pushed payloads restore into working databases, failures
never break the app, and the throttle holds back cosmetic writes.

    python -m unittest -v tests.test_dbsync

All network legs are mocked at the httpx edge, so these run offline and touch
no real gist. The database path follows store.DB, like the sync itself.
"""

import os
import tempfile
import unittest
from unittest import mock

from cryptography.fernet import Fernet

import db_sync
import store


class DbSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._held_db = store.DB
        store.DB = os.path.join(self.tmp.name, "users.db")
        self._held_key = os.environ.get(store.KEY_VAR)
        os.environ[store.KEY_VAR] = Fernet.generate_key().decode()
        self._held_sync = tuple(
            os.environ.get(v) for v in (db_sync.GIST_ID_VAR, db_sync.TOKEN_VAR)
        )
        os.environ[db_sync.GIST_ID_VAR] = "gist-for-tests"
        os.environ[db_sync.TOKEN_VAR] = "token-for-tests"
        self._held_push = db_sync._last_push
        self._held_error = db_sync._last_error
        db_sync._last_push = 0.0
        db_sync._last_error = None

    def tearDown(self):
        store.DB = self._held_db
        if self._held_key is None:
            os.environ.pop(store.KEY_VAR, None)
        else:
            os.environ[store.KEY_VAR] = self._held_key
        for var, held in zip((db_sync.GIST_ID_VAR, db_sync.TOKEN_VAR), self._held_sync):
            if held is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = held
        db_sync._last_push = self._held_push
        db_sync._last_error = self._held_error
        self.tmp.cleanup()

    def _mock_client(self):
        """A fake httpx.Client; the sync's failure mode must never be real."""
        patcher = mock.patch.object(db_sync.httpx, "Client")
        cls = patcher.start()
        self.addCleanup(patcher.stop)
        return cls.return_value.__enter__.return_value

    @staticmethod
    def _remove_db(path: str) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except FileNotFoundError:
                pass

    def test_disabled_without_secrets(self):
        """No secrets, no sync: nothing is sent and nothing is fetched."""
        os.environ.pop(db_sync.GIST_ID_VAR, None)
        os.environ.pop(db_sync.TOKEN_VAR, None)
        client = self._mock_client()
        self.assertFalse(db_sync.enabled())
        self.assertFalse(db_sync.push())
        self.assertFalse(db_sync.restore())
        client.get.assert_not_called()
        client.patch.assert_not_called()

    def test_pushed_payload_restores_into_a_working_database(self):
        """What a push sends is a complete database: checkpointed out of the
        write-ahead log, decodable, and queryable once restored cold."""
        client = self._mock_client()  # before any store write: pushes never reach the wire
        user = store.create_user("lemon")
        self.assertTrue(db_sync.push())
        sent = client.patch.call_args.kwargs["json"]["files"]["users.db"]["content"]
        # a cold container: nothing on disk, the gist holds the payload
        self._remove_db(store.DB)
        client.get.return_value.json.return_value = {
            "files": {"users.db": {"content": sent}}
        }
        self.assertTrue(db_sync.restore())
        self.assertEqual(store.get_user(user["id"])["display_name"], "lemon")

    def test_restore_skips_when_a_local_database_exists(self):
        """A local copy always wins; the gist is only for cold starts."""
        store.create_user("lemon")
        client = self._mock_client()
        self.assertFalse(db_sync.restore())
        client.get.assert_not_called()

    def test_urgency_and_the_throttle(self):
        """Profile writes go immediately; a touch per login does not."""
        client = self._mock_client()  # before any store write: pushes never reach the wire
        user = store.create_user("lemon")
        self.assertEqual(client.patch.call_count, 1, "creating a profile is urgent")
        store.touch(user["id"])
        self.assertEqual(client.patch.call_count, 1, "a touch inside the window waits")
        store.save_spotify_auth(
            user["id"], "refresh", scopes="s", spotify_user_id="sid", spotify_name="n"
        )
        self.assertEqual(client.patch.call_count, 2, "a binding change does not wait")

    def test_push_failure_is_reported_not_raised(self):
        client = self._mock_client()  # before any store write: pushes never reach the wire
        store.create_user("lemon")
        client.patch.return_value.raise_for_status.side_effect = RuntimeError("500")
        self.assertFalse(db_sync.push())
        self.assertIn("push failed", db_sync.status())

    def test_restore_failure_is_reported_not_raised(self):
        client = self._mock_client()
        client.get.return_value.raise_for_status.side_effect = RuntimeError("404")
        self.assertFalse(db_sync.restore())
        self.assertIn("restore failed", db_sync.status())


if __name__ == "__main__":
    unittest.main()
