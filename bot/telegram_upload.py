"""Upload playable MP4 parts with the same Telegram bot over MTProto.

PTB handles commands; Pyrofork is used only for outgoing media. Credentials
are read when an upload starts, and no interactive Telegram login is needed.
"""
import asyncio
import json
import math
import os
from pathlib import Path
import tempfile
import time

_upload_lock = asyncio.Lock()
_active_paths: set[Path] = set()
MAX_PART_BYTES = 2000 * 1024 * 1024  # Leave headroom below Telegram's 2 GiB.


def is_configured() -> bool:
    return (os.getenv("TELEGRAM_UPLOAD_ENABLED", "false").lower() == "true"
            and bool(os.getenv("TELEGRAM_API_ID"))
            and bool(os.getenv("TELEGRAM_API_HASH"))
            and bool(os.getenv("TELEGRAM_TOKEN")))


def part_limit() -> int:
    limit = int(os.getenv("TELEGRAM_SPLIT_MB", "1900")) * 1024 * 1024
    if not 1024 * 1024 <= limit <= MAX_PART_BYTES:
        raise ValueError("TELEGRAM_SPLIT_MB must be between 1 and 2000")
    return limit


def is_uploading(path: Path) -> bool:
    resolved = path.resolve()
    return any(p == resolved or resolved in p.parents for p in tuple(_active_paths))


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await proc.communicate()
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise RuntimeError(f"{args[0]} failed: {err.decode(errors='replace')[-1200:]}")
    return out.decode()


async def probe(path: Path) -> dict:
    data = json.loads(await _run(
        "ffprobe", "-v", "error", "-show_format", "-show_streams",
        "-of", "json", str(path)))
    video = next((s for s in data["streams"] if s.get("codec_type") == "video"), {})
    duration = float(data["format"].get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Video duration is missing or invalid")
    return {"duration": math.ceil(duration), "width": video.get("width", 0),
            "height": video.get("height", 0)}


async def split_video(path: Path, directory: Path, limit: int) -> list[Path]:
    """Stream-copy at keyframes; verify real sizes and retry smaller segments."""
    if path.stat().st_size <= limit:
        return [path]
    duration = (await probe(path))["duration"]
    segment_secs = max(1.0, duration * limit / path.stat().st_size * 0.8)
    for attempt in range(10):
        output = directory / f"attempt-{attempt}"
        output.mkdir()
        await _run(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
            "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-f", "segment",
            "-segment_time", str(segment_secs), "-reset_timestamps", "1",
            "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart",
            str(output / f"{path.stem[:100].replace('%', '%%')}-part-%03d.mp4"))
        parts = sorted(output.glob("*.mp4"))
        if parts and all(0 < p.stat().st_size <= limit for p in parts):
            return parts
        for part in parts:
            part.unlink()
        output.rmdir()
        segment_secs /= 2
    raise RuntimeError("A keyframe interval exceeds the upload limit; original video retained")


async def upload_recording(bot, chat_id: int, path: Path) -> list[int]:
    """Serial uploads, bounded FloodWait retry, and cleanup on failure/cancel.

    The original is never deleted. A complete marker is written only after
    every part succeeds. On retry, parts already sent to the same chat are skipped.
    """
    if not is_configured():
        raise RuntimeError("Set TELEGRAM_UPLOAD_ENABLED, TELEGRAM_API_ID and TELEGRAM_API_HASH")
    from mtproto_client import UploadOnlyClient
    from pyrogram.errors import FloodWait
    from pyrogram.enums import ParseMode

    path = path.resolve()
    if path in _active_paths:
        raise RuntimeError("This recording is already queued or uploading")
    _active_paths.add(path)
    try:
        async with _upload_lock:
            limit = part_limit()
            stat = path.stat()
            fingerprint = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                           "limit": limit, "chat_id": chat_id}
            marker_path = path.with_suffix(".telegram.json")
            try:
                marker = json.loads(marker_path.read_text())
            except (OSError, ValueError):
                marker = {}
            sent = marker.get("message_ids", []) if marker.get("source") == fingerprint else []
            await bot.send_message(chat_id, f"📤 Telegram upload: {path.name}\nPreparing MP4 parts…")
            with tempfile.TemporaryDirectory(prefix="zoomy-upload-", dir=path.parent) as temp:
                parts = await split_video(path, Path(temp), limit)
                client = UploadOnlyClient(
                    "zoomy-upload", api_id=int(os.environ["TELEGRAM_API_ID"]),
                    api_hash=os.environ["TELEGRAM_API_HASH"],
                    bot_token=os.environ["TELEGRAM_TOKEN"],
                    in_memory=True, no_updates=True, sleep_threshold=60,
                    max_concurrent_transmissions=1)
                async with client:
                    if chat_id > 0:
                        await client.get_users(chat_id)
                    for i, part in enumerate(parts):
                        if i < len(sent):
                            continue
                        info = await probe(part)
                        last_notice = 0.0

                        async def progress(current, total):
                            nonlocal last_notice
                            now = time.monotonic()
                            if now - last_notice >= 30:
                                last_notice = now
                                # Reporting failure must not abort the media upload.
                                try:
                                    await bot.send_message(chat_id,
                                        f"📤 Part {i + 1}/{len(parts)}: {current * 100 // max(total, 1)}%")
                                except Exception:
                                    pass

                        for retry in range(4):
                            try:
                                message = await client.send_video(
                                    chat_id, str(part), supports_streaming=True,
                                    caption=f"{path.stem[:800]} · {i + 1}/{len(parts)}",
                                    parse_mode=ParseMode.DISABLED, progress=progress, **info)
                                if message is None:
                                    raise RuntimeError("Telegram upload was stopped")
                                break
                            except FloodWait as exc:
                                if retry == 3 or exc.value > 300:
                                    raise
                                await asyncio.sleep(exc.value)
                        sent.append(message.id)
                        marker = {"source": fingerprint, "message_ids": sent,
                                  "complete": len(sent) == len(parts)}
                        pending = marker_path.with_suffix(".json.tmp")
                        pending.write_text(json.dumps(marker, indent=2))
                        pending.replace(marker_path)
                await bot.send_message(chat_id, f"✅ Telegram: {len(parts)} video part(s) uploaded.")
                return sent
    finally:
        _active_paths.discard(path)
