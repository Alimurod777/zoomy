"""Record three seconds of a synthetic browser screen and a 440 Hz tone."""
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))
from recorder import DisplayPool
from playwright.async_api import async_playwright
from telegram_upload import _run


async def main():
    pool = DisplayPool(base=88, max_slots=1)
    number, display, monitor = await pool.acquire()
    try:
        import os
        env = {**os.environ, "DISPLAY": display, "PULSE_SINK": "virtual_88"}
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=False, env=env,
                args=["--no-sandbox", "--window-size=1280,900", "--autoplay-policy=no-user-gesture-required"])
            try:
                page = await browser.new_page()
                await page.set_content('<body style="background:#17465c;color:white;font:48px sans-serif">Zoomy screen + audio check</body>')
                await page.evaluate('''async () => {
                    window.audioCheck = new AudioContext();
                    const oscillator = audioCheck.createOscillator();
                    oscillator.frequency.value = 440;
                    oscillator.connect(audioCheck.destination);
                    oscillator.start();
                    await audioCheck.resume();
                }''')
                output = ROOT / ".runtime/smoke.mp4"
                await _run("ffmpeg", "-v", "error", "-y", "-f", "x11grab",
                    "-video_size", "1280x900", "-framerate", "12", "-i", display,
                    "-f", "pulse", "-i", monitor, "-t", "3", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", str(output))
                details = json.loads(await _run("ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)))
                assert {s["codec_type"] for s in details["streams"]} == {"video", "audio"}
                # Decode the audio: a stream alone could still contain silence.
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-v", "info", "-i", str(output), "-af", "volumedetect",
                    "-vn", "-f", "null", "-", stderr=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL)
                _, stderr = await proc.communicate()
                import re
                match = re.search(rb"mean_volume: ([-\d.]+) dB", stderr)
                assert match and float(match.group(1)) > -60, "Recorded audio is silent"
                print(f"PASS: virtual screen + audible PulseAudio capture → {output}")
            finally:
                await browser.close()
    finally:
        await pool.release(number)


asyncio.run(main())
