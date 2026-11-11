"""Offline MTProto orchestration and real FFmpeg splitting regression tests."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
import telegram_upload as upload


class UploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "recording.mp4"
        self.path.write_bytes(b"original video")
        self.bot = SimpleNamespace(send_message=AsyncMock())
        from unittest.mock import MagicMock
        self.client = MagicMock()
        self.client.get_users = AsyncMock()
        self.client.send_video = AsyncMock()
        self.client.__aenter__ = AsyncMock(return_value=self.client)
        self.client.__aexit__ = AsyncMock(return_value=False)
        self.context = self.client
        self.env = patch.dict(os.environ, {
            "TELEGRAM_UPLOAD_ENABLED": "true", "TELEGRAM_API_ID": "123",
            "TELEGRAM_API_HASH": "offline", "TELEGRAM_TOKEN": "123:offline"})
        self.env.start()
        self.addCleanup(self.env.stop)
        upload._upload_lock = asyncio.Lock()
        upload._active_paths.clear()

    async def test_small_file_needs_no_split(self):
        result = await upload.split_video(self.path, Path(self.temp.name), 1024)
        self.assertEqual(result, [self.path])

    async def test_failed_part_retry_skips_successful_parts(self):
        parts = [self.path, self.path]
        with patch("mtproto_client.UploadOnlyClient", return_value=self.context), \
             patch.object(upload, "split_video", new=AsyncMock(return_value=parts)), \
             patch.object(upload, "probe", new=AsyncMock(return_value={"duration": 1})):
            self.client.send_video.side_effect = [SimpleNamespace(id=101), OSError("offline")]
            with self.assertRaises(OSError):
                await upload.upload_recording(self.bot, 7, self.path)
            marker = json.loads(self.path.with_suffix(".telegram.json").read_text())
            self.assertFalse(marker["complete"])
            self.assertEqual(marker["message_ids"], [101])
            self.assertFalse(upload.is_uploading(self.path))
            self.client.send_video.reset_mock(side_effect=True)
            self.client.send_video.return_value = SimpleNamespace(id=102)
            result = await upload.upload_recording(self.bot, 7, self.path)
            self.assertEqual(result, [101, 102])
            self.client.send_video.assert_awaited_once()
            self.assertTrue(self.path.exists())
            self.assertTrue(json.loads(self.path.with_suffix(".telegram.json").read_text())["complete"])

    async def test_upload_error_cleans_temporary_parts(self):
        with patch("mtproto_client.UploadOnlyClient", return_value=self.context), \
             patch.object(upload, "split_video", new=AsyncMock(side_effect=RuntimeError("split failed"))):
            with self.assertRaises(RuntimeError):
                await upload.upload_recording(self.bot, 7, self.path)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertFalse(upload.is_uploading(self.path.parent))

    async def test_duplicate_upload_is_rejected(self):
        upload._active_paths.add(self.path.resolve())
        with self.assertRaisesRegex(RuntimeError, "already"):
            await upload.upload_recording(self.bot, 7, self.path)

    def test_limit_rejects_above_bot_limit(self):
        with patch.dict(os.environ, TELEGRAM_SPLIT_MB="4000"):
            with self.assertRaises(ValueError):
                upload.part_limit()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg not on PATH")
    async def test_real_parts_are_playable_and_below_limit(self):
        await upload._run(
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
            "testsrc2=size=640x360:rate=24", "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
            "-g", "12", "-c:a", "aac", str(self.path))
        limit = self.path.stat().st_size // 3
        directory = self.path.parent / "parts"
        directory.mkdir()
        parts = await upload.split_video(self.path, directory, limit)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(part.stat().st_size, limit)
            self.assertGreater((await upload.probe(part))["duration"], 0)
            await upload._run("ffmpeg", "-v", "error", "-i", str(part), "-f", "null", "-")
        self.assertTrue(self.path.exists())
