import os
import tempfile
import unittest

import store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._held = store.DB
        store.DB = os.path.join(self.tmp.name, "users.db")

    def tearDown(self):
        store.DB = self._held
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


if __name__ == "__main__":
    unittest.main()
