"""Verify encrypted persistence and synchronization using two Mongo clients.

Only a randomly named test document is created and removed; the real Zoom
login document is never changed. Source .env before running this script.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))
from auth_store import AuthStore


async def main():
    if not os.getenv("MONGODB_URI"):
        raise SystemExit("Set MONGODB_URI and MONGODB_AUTH_KEY first.")
    previous = os.environ.get("ZOOM_AUTH_ID")
    os.environ["ZOOM_AUTH_ID"] = "integration-test-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        first = AuthStore(path=Path(directory) / "server-a.json")
        second = AuthStore(path=Path(directory) / "server-b.json")
        state = {"cookies": [{"name": "test-only", "value": "synthetic-test-cookie",
            "domain": ".zoom.us", "path": "/", "expires": -1,
            "secure": True, "httpOnly": True, "sameSite": "Lax"}], "origins": []}
        try:
            await first.save(state)
            await first.close()  # A new connection must recover persisted state.
            assert await second.sync()
            assert json.loads(second.path.read_text()) == state
            document = await second._collection().find_one({"_id": second.identity})
            assert "synthetic-test-cookie" not in str(document)
            state["cookies"][0]["value"] = "updated-test-cookie"
            await second.save(state)
            assert await first.load() == state
            assert first.revision == 2
            print("PASS: real MongoDB persistence, encrypted document, two-client auth sync, revision 2.")
        finally:
            await second._collection().delete_one({"_id": second.identity})
            await first.close()
            await second.close()
            if previous is None:
                os.environ.pop("ZOOM_AUTH_ID", None)
            else:
                os.environ["ZOOM_AUTH_ID"] = previous


asyncio.run(main())
