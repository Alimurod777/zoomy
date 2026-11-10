"""Encrypted shared Zoom browser state. Passwords/OTP are never stored here."""
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import tempfile
from urllib.parse import urlparse
from zoom_profile import clean_profile_name

logger = logging.getLogger(__name__)


class AuthStoreError(RuntimeError):
    pass


def zoom_domain(domain: str) -> bool:
    host = domain.lstrip(".").lower()
    return host == "zoom.us" or host.endswith(".zoom.us")


def validate_state(state: dict) -> dict:
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise AuthStoreError("Invalid Zoom browser state")
    cookies = [c for c in state["cookies"] if isinstance(c, dict) and zoom_domain(c.get("domain", ""))]
    if not cookies or any(not all(k in c for k in ("name", "value", "domain", "path")) for c in cookies):
        raise AuthStoreError("Zoom cookies are missing or invalid")
    origins = [o for o in state.get("origins", []) if isinstance(o, dict)
               and zoom_domain(urlparse(o.get("origin", "")).hostname or "")]
    result = {"cookies": cookies, "origins": origins}
    profile = state.get("profile", {})
    name = clean_profile_name(profile.get("display_name")) if isinstance(profile, dict) else None
    if name:
        result["profile"] = {"display_name": name}
    return result


def atomic_state_file(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".zoom-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)  # mkstemp creates 0600 before writing secrets.
    finally:
        Path(name).unlink(missing_ok=True)


class AuthStore:
    def __init__(self, path=None):
        self._client = None
        self._lock = asyncio.Lock()
        self.revision = 0
        self._path_override = path

    @property
    def configured(self) -> bool:
        return bool(os.getenv("MONGODB_URI"))

    @property
    def path(self) -> Path:
        return Path(self._path_override or os.getenv("ZOOM_STATE_PATH", "/secrets/zoom_state.json"))

    @property
    def identity(self) -> str:
        return "zoom:" + os.getenv("ZOOM_AUTH_ID", "default")

    def _cipher(self):
        from cryptography.fernet import Fernet
        try:
            return Fernet(os.environ["MONGODB_AUTH_KEY"].encode())
        except (KeyError, ValueError):
            raise AuthStoreError("Set a valid MONGODB_AUTH_KEY (Fernet key)") from None

    def _collection(self):
        self._cipher()  # Validate encryption before making a connection.
        try:
            if self._client is None:
                from pymongo import AsyncMongoClient
                self._client = AsyncMongoClient(
                    os.environ["MONGODB_URI"], serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=5000, socketTimeoutMS=10000,
                    appname="zoomy-auth")
            return self._client[os.getenv("MONGODB_DATABASE", "zoomy")]["browser_auth"]
        except Exception:
            raise AuthStoreError("MongoDB connection configuration is invalid") from None

    def _seal(self, state: dict) -> str:
        return self._cipher().encrypt(json.dumps(state).encode()).decode()

    def _open(self, document: dict) -> dict:
        try:
            state = validate_state(json.loads(self._cipher().decrypt(document["sealed_state"].encode())))
            self.revision = int(document.get("revision", 0))
            return state
        except Exception:
            raise AuthStoreError("Cannot decrypt Zoom auth; check the shared MONGODB_AUTH_KEY") from None

    def _local(self):
        if not self.path.is_file():
            return None
        try:
            return validate_state(json.loads(self.path.read_text()))
        except Exception:
            raise AuthStoreError("Local Zoom state is invalid") from None

    def _cache(self, state: dict) -> bool:
        try:
            try:
                existing = json.loads(self.path.read_text()) if self.path.is_file() else None
            except ValueError:
                existing = None
            if (existing == state and self.path.stat().st_mode & 0o777 == 0o600):
                return True
            atomic_state_file(self.path, state)
            return True
        except OSError:
            # Docker may mount /secrets read-only. Recorder reads Mongo directly.
            logger.warning("Zoom auth saved in MongoDB; local cache is not writable")
            return False

    async def load(self):
        """Mongo is authoritative; a configured but unavailable DB is an error."""
        async with self._lock:
            if not self.configured:
                return self._local()
            collection = self._collection()
            try:
                document = await collection.find_one({"_id": self.identity})
            except Exception:
                raise AuthStoreError("MongoDB auth sync failed; check connection and access") from None
            return self._open(document) if document else None

    async def save(self, state: dict) -> dict:
        state = validate_state(state)
        async with self._lock:
            if not self.configured:
                atomic_state_file(self.path, state)
                return {"mongo": False, "cached": True, "revision": 0}
            collection = self._collection()
            from pymongo import ReturnDocument
            sealed = self._seal(state)
            try:
                document = await collection.find_one_and_update(
                    {"_id": self.identity},
                    {"$set": {"sealed_state": sealed, "updated_at": datetime.now(timezone.utc)},
                     "$inc": {"revision": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
            except Exception:
                raise AuthStoreError("MongoDB auth save failed; browser session is still open for retry") from None
            self.revision = document["revision"]
            return {"mongo": True, "cached": self._cache(state), "revision": self.revision}

    async def sync(self) -> bool:
        """Pull the shared state. Bootstrap an empty DB once from a local login."""
        if not self.configured:
            return self._local() is not None
        async with self._lock:
            collection = self._collection()
            try:
                document = await collection.find_one({"_id": self.identity})
                if not document:
                    local = self._local()
                    if local is None:
                        return False
                    from pymongo import ReturnDocument
                    # Atomic seed: another server's newer login always wins.
                    document = await collection.find_one_and_update(
                        {"_id": self.identity}, {"$setOnInsert": {
                            "sealed_state": self._seal(local), "revision": 1,
                            "updated_at": datetime.now(timezone.utc)}},
                        upsert=True, return_document=ReturnDocument.AFTER)
            except AuthStoreError:
                raise
            except Exception:
                raise AuthStoreError("MongoDB auth sync failed; check connection and access") from None
            self._cache(self._open(document))
            return True

    async def close(self):
        if self._client is not None:
            await self._client.close()
            self._client = None


auth_store = AuthStore()
