"""Regressions for the Nov 11 upload, silent audio and stalled-join failures."""
import asyncio
from datetime import datetime
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import recorder as rec
import transcriber


class UploadUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsolicited_update_after_sqlite_close_is_safe(self):
        from mtproto_client import UploadOnlyClient
        from pyrogram import Client,raw
        client=UploadOnlyClient('offline',api_id=123,api_hash='offline',in_memory=True,no_updates=True)
        await client.storage.open()
        await client.storage.close()
        updates=raw.types.Updates(updates=[],users=[],chats=[],date=datetime.now(),seq=1)
        # This reproduces the upstream shutdown race without Telegram/network.
        with self.assertRaises(sqlite3.ProgrammingError):
            await Client.handle_updates(client,updates)
        await client.handle_updates(updates)


class SpeechGateTests(unittest.TestCase):
    def test_no_speech_never_loads_whisper_or_overwrites_transcripts(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'silent.mp4'
            path.with_suffix('.txt').write_text('existing')
            with patch.object(transcriber,'_has_speech',return_value=False), patch.object(transcriber,'_get_model') as model:
                with self.assertRaises(transcriber.NoSpeechError):
                    transcriber._transcribe_sync(str(path),'large-v3',None)
            model.assert_not_called()
            self.assertEqual(path.with_suffix('.txt').read_text(),'existing')
            self.assertFalse(path.with_suffix('.srt').exists())

    def test_empty_decoder_output_is_not_reported_as_success(self):
        model=Mock(transcribe=Mock(return_value=([],None)))
        with patch.object(transcriber,'_has_speech',return_value=True), patch.object(transcriber,'_get_model',return_value=model):
            with self.assertRaises(transcriber.NoSpeechError):
                transcriber._transcribe_sync('/tmp/no-transcript-created.mp4','small',None)

    def test_speech_writes_text_and_timestamps(self):
        model=Mock(transcribe=Mock(return_value=([SimpleNamespace(text=' Salom ',start=0,end=1.25)],None)))
        with tempfile.TemporaryDirectory() as folder, patch.object(transcriber,'_has_speech',return_value=True), patch.object(transcriber,'_get_model',return_value=model):
            txt,srt=transcriber._transcribe_sync(str(Path(folder)/'speech.mp4'),'small','uz')
            self.assertEqual(Path(txt).read_text(),'Salom')
            self.assertIn('00:00:01,250',Path(srt).read_text())
            self.assertEqual(model.transcribe.call_args.kwargs['language'],'uz')

    def test_large_model_requires_explicit_opt_in(self):
        with patch.dict(os.environ,WHISPER_ALLOW_LARGE='false'), patch.object(transcriber,'_has_speech',return_value=True), patch.object(transcriber,'_get_model') as model:
            with self.assertRaisesRegex(RuntimeError,'Large'):
                transcriber._transcribe_sync('unused.mp4','large-v3',None)
        model.assert_not_called()


class WorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_terminates_model_process_group(self):
        began=asyncio.Event()
        async def communicate():
            began.set()
            await asyncio.sleep(1000)
        process=SimpleNamespace(pid=987654,returncode=None,communicate=communicate,wait=AsyncMock(return_value=-15))
        with patch.object(transcriber.asyncio,'create_subprocess_exec',AsyncMock(return_value=process)) as launch, patch.object(transcriber.os,'killpg') as kill:
            job=asyncio.create_task(transcriber.transcribe('unused.mp4'))
            await began.wait()
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):await job
        kill.assert_called_once_with(987654,signal.SIGTERM)
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        self.assertIn('ZOOMY_WORKER_PARENT_PID',launch.call_args.kwargs['env'])

    async def test_audio_wait_setting_is_a_deadline_not_a_boolean(self):
        recorder=rec.ZoomRecorder()
        process=SimpleNamespace(returncode=None,stdout=SimpleNamespace(read=AsyncMock()),kill=Mock(),wait=AsyncMock(return_value=0))
        ticks=iter((0,6))
        with patch.object(rec,'AUDIO_WAIT_TIMEOUT',5), patch.object(rec,'time',SimpleNamespace(monotonic=lambda:next(ticks))), patch.object(rec.asyncio,'create_subprocess_exec',AsyncMock(return_value=process)):
            await recorder._wait_for_audio()
        process.stdout.read.assert_not_called()
        process.kill.assert_called_once()

    async def test_unknown_join_page_stops_within_connection_deadline(self):
        recorder=rec.ZoomRecorder()
        clock=SimpleNamespace(now=0)
        recorder._open_join_page=AsyncMock()
        recorder._state=AsyncMock(return_value='connecting')
        async def delay(seconds):
            clock.now+=seconds
            return False
        recorder._wait_or_stop=AsyncMock(side_effect=delay)
        with patch.object(rec,'time',SimpleNamespace(monotonic=lambda:clock.now)),patch.dict(os.environ,ZOOM_CONNECT_TIMEOUT='30',ZOOM_JOIN_ATTEMPTS='1'):
            with self.assertRaisesRegex(rec.MeetingJoinError,'30'):
                await recorder._join_meeting(SimpleNamespace(),'https://zoom.us/wc/12345678900/join')
        self.assertLessEqual(clock.now,30)
        recorder._open_join_page.assert_awaited_once()

    async def test_shared_only_fails_closed_when_renderer_cannot_start(self):
        recorder=rec.ZoomRecorder()
        page=SimpleNamespace(evaluate=AsyncMock(side_effect=OSError('browser unavailable')))
        with patch.dict(os.environ,ZOOM_CLEAN_RECORDING='true',ZOOM_RECORDING_SOURCE='shared-only'):
            with self.assertRaisesRegex(RuntimeError,'kamera yozuvi boshlanmadi'):
                await recorder._prepare_recording_view(page)

    async def test_display_allocator_preserves_an_existing_x_server_lock(self):
        pool=rec.DisplayPool(base=99,max_slots=1)
        pool._ensure_pulseaudio=AsyncMock()
        proc=Mock(poll=Mock(return_value=None))
        with patch.object(rec.os.path,'exists',side_effect=lambda path:path=='/tmp/.X99-lock'), patch.object(rec.os,'remove') as remove, patch.object(rec.subprocess,'Popen',return_value=proc) as launch, patch.object(rec.subprocess,'run'), patch.object(rec.asyncio,'sleep',AsyncMock()):
            number,display,_=await pool.acquire()
        self.assertEqual(number,100)
        self.assertEqual(display,':100')
        remove.assert_not_called()
        self.assertIn(':100',launch.call_args.args[0])

    def test_invitation_tokens_are_removed_from_logs(self):
        self.assertEqual(rec.log_zoom_url('https://zoom.us/j/12345678900?pwd=private&tk=secret#token'),'https://zoom.us/j/12345678900')

    def test_encoder_has_bounded_threads_and_frame_rate(self):
        recorder=rec.ZoomRecorder()
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,FFMPEG_THREADS='2',RECORD_FPS='15'),patch.object(rec.subprocess,'Popen') as launch:
            recorder._start_ffmpeg(str(Path(folder)/'video.mp4'))
            recorder._ffmpeg_log.close()
        args=launch.call_args.args[0]
        self.assertEqual(args[args.index('-threads')+1],'2')
        self.assertEqual(args[args.index('-r')+1],'15')


if __name__=='__main__':unittest.main()
