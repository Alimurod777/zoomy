import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
from auth_store import AuthStore, AuthStoreError, validate_state

STATE = {"cookies": [{"name": "zoom-session", "value": "private-cookie-value", "domain": ".zoom.us",
    "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}], "origins": []}


class FakeCollection:
    def __init__(self):
        self.document = None

    async def find_one(self, query):
        return copy.deepcopy(self.document)

    async def find_one_and_update(self, query, update, **kwargs):
        if self.document is None:
            self.document = {"_id": query["_id"], **update.get("$setOnInsert", {})}
        self.document.update(update.get("$set", {}))
        if "$inc" in update:
            self.document["revision"] = self.document.get("revision", 0) + 1
        return copy.deepcopy(self.document)


class AuthStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.store = AuthStore(path=Path(self.temp.name) / "state.json")
        self.collection = FakeCollection()
        self.store._collection = lambda: self.collection
        self.env = patch.dict(os.environ, MONGODB_URI="mongodb://offline-test/",
                              MONGODB_AUTH_KEY=Fernet.generate_key().decode())
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_mongo_is_encrypted_and_loads_on_another_server(self):
        result = await self.store.save(STATE)
        self.assertTrue(result["mongo"])
        serialized = json.dumps(self.collection.document, default=str)
        self.assertNotIn("private-cookie-value", serialized)
        self.assertNotIn('"cookies"', serialized)
        other = AuthStore(path=Path(self.temp.name) / "other.json")
        other._collection = lambda: self.collection
        self.assertEqual(await other.load(), STATE)
        self.assertTrue(await other.sync())
        self.assertEqual(json.loads(other.path.read_text()), STATE)
        self.assertEqual(other.path.stat().st_mode & 0o777, 0o600)

    async def test_mongo_wins_over_stale_local_state(self):
        await self.store.save(STATE)
        stale = copy.deepcopy(STATE)
        stale["cookies"][0]["value"] = "stale"
        self.store.path.write_text(json.dumps(stale))
        await self.store.sync()
        self.assertEqual(json.loads(self.store.path.read_text()), STATE)

    async def test_mongo_repairs_a_corrupt_local_cache(self):
        await self.store.save(STATE)
        self.store.path.write_text("not-json")
        await self.store.sync()
        self.assertEqual(json.loads(self.store.path.read_text()), STATE)

    async def test_wrong_key_does_not_replace_good_cache(self):
        await self.store.save(STATE)
        with patch.dict(os.environ, MONGODB_AUTH_KEY=Fernet.generate_key().decode()):
            with self.assertRaises(AuthStoreError):
                await self.store.sync()
        self.assertEqual(json.loads(self.store.path.read_text()), STATE)

    async def test_empty_mongo_bootstraps_local_login(self):
        with patch.dict(os.environ, MONGODB_URI=""):
            await self.store.save(STATE)
        self.assertTrue(await self.store.sync())
        self.assertEqual(await self.store.load(), STATE)
        self.assertEqual(self.collection.document["revision"], 1)

    async def test_database_failure_does_not_silently_use_local(self):
        await self.store.save(STATE)
        self.collection.find_one = AsyncMock(side_effect=OSError("mongodb://private:password@host"))
        with self.assertRaises(AuthStoreError) as caught:
            await self.store.load()
        self.assertNotIn("password", str(caught.exception))

    async def test_database_save_failure_keeps_existing_cache(self):
        await self.store.save(STATE)
        self.collection.find_one_and_update = AsyncMock(side_effect=OSError("offline"))
        updated = copy.deepcopy(STATE)
        updated["cookies"][0]["value"] = "new"
        with self.assertRaises(AuthStoreError):
            await self.store.save(updated)
        self.assertEqual(json.loads(self.store.path.read_text()), STATE)

    def test_foreign_cookies_and_origins_are_removed(self):
        state = copy.deepcopy(STATE)
        state["cookies"].append({"name": "evil", "domain": "evilzoom.us", "value": "secret", "path": "/"})
        state["origins"] = [{"origin": "https://accounts.google.com", "localStorage": []}]
        self.assertEqual(validate_state(state), STATE)

    async def test_display_name_is_encrypted_with_auth_but_extra_profile_fields_are_dropped(self):
        state = copy.deepcopy(STATE)
        state['profile'] = {'display_name':'My Zoom Name', 'password':'never-store-this'}
        await self.store.save(state)
        restored = await self.store.load()
        self.assertEqual(restored['profile'], {'display_name':'My Zoom Name'})
        self.assertNotIn('My Zoom Name', str(self.collection.document))
        self.assertNotIn('never-store-this', str(restored))
