"""Check native launch/OCR using a deliberately invalid, three-digit ID.

Run after sourcing scripts/runtime-env.sh. Does not enter a real meeting,
create a recording, or print account data.
"""
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
from recorder import DisplayPool
from zoom_desktop import NativeZoom


async def main():
    if Path('/tmp/.X85-lock').exists():
        raise RuntimeError('Display :85 is already in use; run the check later.')
    pool = DisplayPool(base=85, max_slots=1)
    number, display, sink = await pool.acquire()
    native = NativeZoom(display, sink.removesuffix('.monitor'), asyncio.Event())
    try:
        await native.open('https://zoom.us/j/123', 'Zoomy runtime check')
        deadline = asyncio.get_running_loop().time() + 120
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(2)
            state = await asyncio.wait_for(native.snapshot(), timeout=25)
            if state == 'invalid':
                assert not native.admitted
                print('PASS: native Zoom opens and rejects the invalid meeting without recording.')
                return
        raise RuntimeError('Native Zoom did not detect the invalid ID within 120 seconds.')
    finally:
        await native.close()
        await pool.release(number)


if __name__ == '__main__':
    asyncio.run(main())
