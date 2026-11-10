"""Open a temporary Zoom browser link; save via the page's Save button."""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import signal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))
from auth_store import auth_store
from login_link import BrowserLoginLink
from zoom_browser_auth import ZoomBrowserLogin


async def main(args):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    os.environ["ZOOM_LOGIN_DISPLAY"] = str(args.display)
    browser = ZoomBrowserLogin()
    finished = asyncio.Event()
    link = BrowserLoginLink(browser, ttl=args.ttl, on_saved=lambda: notify(finished))
    metadata = ROOT / ".runtime/zoom-login-link.json"
    try:
        if auth_store.configured:
            await auth_store.load()
        await browser.open(manual=True)
        url = await link.open()
        data = {"url": url, "pid": os.getpid(),
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=args.ttl)).isoformat()}
        fd = os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        print("Private login link ready; metadata is in .runtime/zoom-login-link.json", flush=True)
        try:
            await asyncio.wait_for(finished.wait(), timeout=args.ttl)
            print("Zoom login verified and saved.", flush=True)
        except asyncio.TimeoutError:
            print("Temporary login link expired.", flush=True)
    finally:
        await link.close()
        await browser.close()
        await auth_store.close()
        if metadata.exists():
            try:
                if json.loads(metadata.read_text()).get("pid") == os.getpid():
                    metadata.unlink()
            except (ValueError, OSError):
                pass


async def notify(event):
    event.set()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ttl", type=int, default=900)
    parser.add_argument("--display", type=int, default=92)
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    except Exception as exc:
        print(f"Temporary login failed ({type(exc).__name__}); no credentials logged.", flush=True)
        raise SystemExit(1)
