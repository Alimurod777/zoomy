import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
from auth_store import AuthStoreError
from desktop_auth_store import DesktopAuthStore
from zoom_desktop import NativeZoom

DB = '.zoom/data/zoomus.enc.v2.db'


class Collection:
    def __init__(self):
        self.document = None

    async def find_one(self, query):
        return copy.deepcopy(self.document)

    async def insert_one(self, document):
        if self.document is not None:
            raise RuntimeError('duplicate')
        self.document = copy.deepcopy(document)

    async def update_one(self, query, update):
        match = self.document and self.document['revision'] == query['revision']
        if match:
            self.document.update(update['$set'])
            self.document['revision'] += 1
        return SimpleNamespace(matched_count=int(bool(match)))


class DesktopStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.home.mkdir()
        self.collection = Collection()
        self.env = patch.dict(os.environ, MONGODB_URI='mongodb://offline/',
                MONGODB_AUTH_KEY=Fernet.generate_key().decode(), ZOOM_DESKTOP_SYNC='true')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = self.make_store(self.home)

    def make_store(self, home):
        store = DesktopAuthStore(home)
        store._collection = lambda: self.collection
        store._idle = AsyncMock()
        return store

    def write(self, name=DB, value=b'private-native-profile'):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return path

    async def seed(self):
        self.write()
        self.assertEqual(await self.store.prepare(), 'local')
        self.assertEqual(await self.store.backup(), 'saved')

    async def test_encrypted_roundtrip_on_another_home_and_private_permissions(self):
        self.write('.config/zoomus.conf', b'private-settings')
        self.write('.zoom/logs/sensitive.log', b'not-for-backup')
        self.write('.zoom/data/person@xmpp.zoom.us.encks.db', b'chat-history')
        await self.seed()
        serialized = json.dumps(self.collection.document, default=str)
        for secret in ('private-native-profile', 'private-settings', 'not-for-backup', 'chat-history'):
            self.assertNotIn(secret, serialized)
        other = self.make_store(Path(self.temp.name) / 'other')
        other.device = 'other-host'
        self.assertEqual(await other.prepare(), 'restored')
        self.assertEqual((other.home / DB).read_bytes(), b'private-native-profile')
        self.assertEqual((other.home / DB).stat().st_mode & 0o777, 0o600)
        self.assertFalse((other.home / '.zoom/logs').exists())
        self.assertFalse((other.home / '.zoom/data/person@xmpp.zoom.us.encks.db').exists())
        self.assertEqual(await other.backup(), 'saved')

    async def test_foreign_local_profile_is_preserved_and_cannot_overwrite_remote(self):
        await self.seed()
        before = copy.deepcopy(self.collection.document)
        self.write(value=b'other-login')
        other = self.make_store(self.home)
        other.device = 'different-device'
        self.assertEqual(await other.prepare(), 'conflict')
        with self.assertRaises(AuthStoreError):
            await other.backup()
        self.assertEqual((self.home / DB).read_bytes(), b'other-login')
        self.assertEqual(self.collection.document, before)

    async def test_same_host_changes_are_saved_after_reopen(self):
        await self.seed()
        self.write(value=b'new-login')
        reopened = self.make_store(self.home)
        self.assertEqual(await reopened.prepare(), 'local')
        await reopened.backup()
        self.assertEqual(self.collection.document['revision'], 2)

    async def test_compare_and_swap_rejects_a_newer_remote_revision(self):
        await self.seed()
        await self.store.prepare()
        self.collection.document['revision'] += 1
        with self.assertRaisesRegex(AuthStoreError, 'another server'):
            await self.store.backup()

    async def test_simultaneous_initial_seed_does_not_overwrite(self):
        self.write()
        other = self.make_store(self.home)
        await self.store.prepare()
        await other.prepare()
        await self.store.backup()
        before = copy.deepcopy(self.collection.document)
        with self.assertRaises(AuthStoreError):
            await other.backup()
        self.assertEqual(before, self.collection.document)

    async def test_wrong_key_never_writes_files(self):
        await self.seed()
        other = self.make_store(Path(self.temp.name) / 'empty')
        with patch.dict(os.environ, MONGODB_AUTH_KEY=Fernet.generate_key().decode()):
            with self.assertRaises(AuthStoreError):
                await other.prepare()
        self.assertFalse(other.home.exists())

    async def test_symlink_cannot_escape_profile(self):
        target = Path(self.temp.name) / 'outside'
        target.mkdir()
        (self.home / '.zoom').symlink_to(target)
        with self.assertRaises(AuthStoreError):
            await self.store.prepare()

    async def test_tampered_paths_rejected_even_when_encrypted(self):
        await self.seed()
        doc = self.collection.document
        doc['sealed_profile'] = self.store._cipher().encrypt(json.dumps({
            'version': 1, 'files': {'../outside': 'c2VjcmV0'}}).encode()).decode()
        other = self.make_store(Path(self.temp.name) / 'empty')
        with self.assertRaises(AuthStoreError):
            await other.prepare()
        self.assertFalse(other.home.exists())

    async def test_size_limit_and_active_client_block_backup(self):
        self.write(value=b'x' * 20)
        with patch('desktop_auth_store.MAX_BYTES', 10):
            with self.assertRaises(AuthStoreError):
                await self.store.prepare()
        await self.store.prepare()
        self.store._idle.side_effect = AuthStoreError('active')
        with self.assertRaises(AuthStoreError):
            await self.store.backup()
        self.assertIsNone(self.collection.document)

    async def test_database_outage_does_not_upload_after_failed_prepare(self):
        self.write()
        self.collection.find_one = AsyncMock(side_effect=RuntimeError('private-password'))
        with self.assertRaises(AuthStoreError) as caught:
            await self.store.prepare()
        self.assertNotIn('private-password', str(caught.exception))
        with self.assertRaises(AuthStoreError):
            await self.store.backup()

    async def test_failed_restore_rolls_back_without_overwriting_existing_file(self):
        self.write('.config/zoomus.conf', b'existing')
        with self.assertRaises(AuthStoreError):
            self.store._restore({DB: 'bmV3', '.config/zoomus.conf': 'b3RoZXI='})
        self.assertFalse((self.home / DB).exists())
        self.assertEqual((self.home / '.config/zoomus.conf').read_bytes(), b'existing')
        self.assertFalse(list(self.home.rglob('.zoom-restore-*')))

    async def test_disabled_sync_requires_no_database(self):
        with patch.dict(os.environ, ZOOM_DESKTOP_SYNC='false'):
            self.assertEqual(await self.store.prepare(), 'disabled')
            self.assertEqual(await self.store.backup(), 'disabled')
        self.store._idle.assert_not_awaited()

    async def test_idle_checks_real_process_exit_status(self):
        store = DesktopAuthStore(self.home)
        for code in (0, 1, 2):
            proc = SimpleNamespace(wait=AsyncMock(return_value=code), returncode=code)
            with patch('desktop_auth_store.asyncio.create_subprocess_exec', AsyncMock(return_value=proc)):
                if code == 1:
                    await store._idle()
                else:
                    with self.assertRaises(AuthStoreError):
                        await store._idle()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_after_exit_and_before_unlock(self):
        native = NativeZoom(':85', 'virtual_85', asyncio.Event())
        native.process = SimpleNamespace(returncode=None, pid=123)
        async def exit_zoom():
            native.process.returncode = 0
        native.process.wait = AsyncMock(side_effect=exit_zoom)
        native.auth_store = SimpleNamespace(backup=AsyncMock(return_value='saved'), close=AsyncMock())
        native.lock_file = SimpleNamespace(close=lambda: None)
        async def backup():
            self.assertEqual(native.process.returncode, 0)
        native.auth_store.backup.side_effect = backup
        with patch('zoom_desktop.os.killpg'):
            await native.close()
        native.auth_store.backup.assert_awaited_once()
        native.auth_store.close.assert_awaited_once()

    async def test_forced_kill_skips_snapshot(self):
        native = NativeZoom(':85', 'virtual_85', asyncio.Event())
        native.process = SimpleNamespace(returncode=None, pid=123)
        async def wait():
            if native.process.wait.await_count == 1:
                raise asyncio.TimeoutError()
            native.process.returncode = -9
        native.process.wait = AsyncMock(side_effect=wait)
        native.auth_store = SimpleNamespace(backup=AsyncMock(), close=AsyncMock())
        with patch('zoom_desktop.os.killpg'):
            await native.close()
        native.auth_store.backup.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
