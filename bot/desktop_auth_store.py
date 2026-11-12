"""Encrypted, bounded native-profile snapshots; never claims a verified login.

The caller holds the native per-user lock. Existing profiles are never restored
over, and a foreign divergent profile cannot overwrite the shared snapshot.
"""
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import tempfile

from auth_store import AuthStore, AuthStoreError
from native_processes import living_pids

MAX_BYTES = 8 * 1024 * 1024  # base64 + Fernet still fit a 16 MiB Mongo document
CONFIGS = {'.config/zoomus.conf', '.config/zoom.conf'}
DB_NAME = re.compile(r'(?:zoomus(?:\.enc(?:\.v2)?|\.zmdb\.kvs\.enc|\.zmdb\.default\.noenc\.rlock)|zoommeeting\.enc(?:\.v2)?|zoombase_[a-zA-Z0-9_-]+\.enc)\.db(?:-wal|-journal)?\Z')


def allowed(name):
    path = Path(name)
    return len(name) <= 256 and path.as_posix() == name and (name in CONFIGS or (path.parent.as_posix() == '.zoom/data'
            and bool(DB_NAME.fullmatch(path.name))))


class DesktopAuthStore(AuthStore):
    def __init__(self, home=None):
        super().__init__()
        self.home = Path(home) if home else Path.home()
        self.base_revision = None
        self.can_save = False
        self.prepared = False
        self.device = hashlib.sha256(f'{platform.node()}:{os.getuid()}'.encode()).hexdigest()

    @property
    def configured(self):
        return super().configured and os.getenv('ZOOM_DESKTOP_SYNC', 'true').lower() in {'1', 'true', 'yes'}

    @property
    def identity(self):
        return 'zoom-desktop:' + os.getenv('ZOOM_AUTH_ID', 'default')

    def _collection(self):
        # Reuse the configured encrypted auth connection, separate collection.
        super()._collection()
        return self._client[os.getenv('MONGODB_DATABASE', 'zoomy')]['desktop_auth']

    async def _idle(self):
        proc = await asyncio.create_subprocess_exec('pgrep', '-u', str(os.getuid()), '-x', 'zoom',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out,_ = await asyncio.wait_for(proc.communicate(), 3)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if proc.returncode not in {0,1} or living_pids(out.decode().split()):
            raise AuthStoreError('Desktop profile sync requires Zoom to be closed')

    def _safe_path(self, name):
        if not isinstance(name, str) or not allowed(name):
            raise AuthStoreError('Unsupported Desktop profile path')
        path = self.home / name
        for part in (path, *path.parents):
            if part == self.home:
                break
            if part.is_symlink():
                raise AuthStoreError('Desktop profile symlinks are not supported')
        return path

    def _files(self):
        candidates = set(CONFIGS)
        folder = self.home / '.zoom/data'
        if folder.is_symlink() or (self.home / '.zoom').is_symlink():
            raise AuthStoreError('Desktop profile symlinks are not supported')
        if folder.is_dir():
            candidates.update('.zoom/data/' + p.name for p in folder.iterdir() if allowed('.zoom/data/' + p.name))
        if len(candidates) > 128:
            raise AuthStoreError('Too many Desktop profile files')
        result, total = {}, 0
        for name in sorted(candidates):
            path = self._safe_path(name)
            if not path.exists():
                continue
            if not stat.S_ISREG(path.stat().st_mode):
                raise AuthStoreError('Desktop profile contains a non-regular file')
            with path.open('rb') as stream:
                content = stream.read(MAX_BYTES - total + 1)
            total += len(content)
            if total > MAX_BYTES:
                raise AuthStoreError('Desktop profile exceeds the 8 MiB snapshot limit')
            result[name] = base64.b64encode(content).decode()
        return result

    def _decode(self, document):
        try:
            payload = json.loads(self._cipher().decrypt(document['sealed_profile'].encode()))
            files = payload['files']
            if payload['version'] != 1 or not isinstance(files, dict) or not 1 <= len(files) <= 128:
                raise ValueError()
            total = 0
            for name, value in files.items():
                self._safe_path(name)
                total += len(base64.b64decode(value, validate=True))
                if total > MAX_BYTES:
                    raise ValueError()
            if self._digest(files) != document['digest']:
                raise ValueError()
            return files
        except Exception:
            raise AuthStoreError('Cannot validate Desktop snapshot; check MONGODB_AUTH_KEY') from None

    @staticmethod
    def _digest(files):
        return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()

    def _restore(self, files):
        staged, installed = [], []
        try:
            for name, value in files.items():
                path = self._safe_path(name)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, temp = tempfile.mkstemp(prefix='.zoom-restore-', dir=path.parent)
                staged.append((Path(temp), path))
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(base64.b64decode(value, validate=True))
                    stream.flush()
                    os.fsync(stream.fileno())
            for temp, path in staged:
                # link fails if a concurrent writer created the destination.
                os.link(temp, path)
                installed.append(path)
        except Exception:
            for path in installed:
                path.unlink(missing_ok=True)
            raise AuthStoreError('Desktop restore failed; existing files were preserved') from None
        finally:
            for temp, _ in staged:
                temp.unlink(missing_ok=True)

    async def prepare(self):
        """Restore only into an empty profile, before launching Zoom."""
        if not self.configured:
            return 'disabled'
        await self._idle()
        try:
            document = await self._collection().find_one({'_id': self.identity})
            local = self._files()
            if not document:
                self.can_save = True
                outcome = 'local'
            else:
                files = self._decode(document)
                self.base_revision = document['revision']
                self.can_save = (not local or self._digest(local) == document['digest']
                                 or document.get('device') == self.device)
                if not local:
                    await self._idle()
                    self._restore(files)
                    outcome = 'restored'
                else:
                    outcome = 'local' if self.can_save else 'conflict'
            self.prepared = True
            return outcome
        except AuthStoreError:
            raise
        except Exception:
            raise AuthStoreError('Desktop MongoDB sync failed; local profile preserved') from None

    async def backup(self):
        """Called after graceful shutdown only; compare-and-swap prevents loss."""
        if not self.configured:
            return 'disabled'
        if not self.prepared or not self.can_save:
            raise AuthStoreError('Desktop snapshot not saved: profile sync was unavailable or conflicted')
        await self._idle()
        files = self._files()
        if not files:
            return 'empty'
        await self._idle()
        document = {'sealed_profile': self._cipher().encrypt(json.dumps({'version': 1, 'files': files}).encode()).decode(),
                    'digest': self._digest(files), 'device': self.device,
                    'updated_at': datetime.now(timezone.utc)}
        collection = self._collection()
        try:
            if self.base_revision is None:
                await collection.insert_one({'_id': self.identity, 'revision': 1, **document})
                self.base_revision = 1
            else:
                result = await collection.update_one({'_id': self.identity, 'revision': self.base_revision},
                        {'$set': document, '$inc': {'revision': 1}})
                if result.matched_count != 1:
                    raise AuthStoreError('Desktop snapshot changed on another server; local profile preserved')
                self.base_revision += 1
            return 'saved'
        except AuthStoreError:
            raise
        except Exception:
            raise AuthStoreError('Desktop snapshot upload failed; local profile preserved') from None


class AccountDesktopAuthStore(DesktopAuthStore):
    def __init__(self,home=None):
        super().__init__(home)
        try:
            machine=Path('/etc/machine-id').read_text().strip()
        except OSError:
            machine=''
        self.device=hashlib.sha256(f'{self.device}:{machine}'.encode()).hexdigest()

    @property
    def identity(self):
        # Old snapshots may be guest/meeting profiles with broken login state.
        # Keep them available, but never bootstrap the dedicated account Home
        # from those unverified legacy snapshots.
        return super().identity + ':account-v1:' + self.device
