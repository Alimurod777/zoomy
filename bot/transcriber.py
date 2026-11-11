import asyncio
import logging
import os
import json
from pathlib import Path
import signal
import subprocess
import sys

WhisperModel = None  # imported only inside the isolated transcription worker

logger = logging.getLogger(__name__)

# .env.example intentionally leaves this optional setting empty.
_CPU_THREADS = max(1, min(8, int(os.environ.get("WHISPER_THREADS", "").strip() or "2")))

# initial_prompt primes Whisper's context window — teaches spelling of domain terms.
# hotwords boosts probability of specific words during beam search.
_DEFAULT_INITIAL_PROMPT = (
    "Rapat koordinasi pemerintah. Peserta dari BSSN, Direktorat KSS, D32, BSrE, Komdigi, "
    "Diskominfo, Pemda, Pemprov, Pemkot, Pemkab, Sekda, Dukcapil, DPMPTSP, BKAD, BKPSDM. "
    "Topik: Pemdigi, Pemdi, IKASANDI, IKAMI, SPBE, TTE, PSrE, NSPK, RKA, KSS, PDP, IPPD, LPPD, "
    "TTIS, CSIRT, Phising, STR, ComCheck, Sanapati, Forkomsanda, Manrisk, PTKKSS, "
    "Menpan RB, Kepka, Perka, Perban, Permen, KL, "
    "verifikasi, self-assessment, tindak lanjut, rekomendasi, email."
)
_DEFAULT_HOTWORDS = (
    "BSSN,BSrE,Komdigi,Kominfo,IKASANDI,IKAMI,Pemdigi,Pemdi,D32,KSS,TTE,SPBE,PSrE,NSPK,RKA,"
    "PDP,IPPD,LPPD,TTIS,CSIRT,Phising,STR,ComCheck,Sanapati,Forkomsanda,Manrisk,PTKKSS,"
    "Diskominfo,Pemda,Pemprov,Pemkot,Pemkab,Dukcapil,DPMPTSP,BKAD,BKPSDM,Sekda,"
    "Menpan,Kepka,Perka,Perban,Permen,KL,email"
)

WHISPER_INITIAL_PROMPT = os.environ.get("WHISPER_INITIAL_PROMPT", _DEFAULT_INITIAL_PROMPT)
WHISPER_HOTWORDS = os.environ.get("WHISPER_HOTWORDS", _DEFAULT_HOTWORDS)

_model = None
_model_name: str | None = None


def _get_model(model_name: str):
    global _model, _model_name
    if _model is None or _model_name != model_name:
        if _model is not None:
            logger.info("Switching Whisper model: %s → %s", _model_name, model_name)
        logger.info("Loading Whisper model '%s' with %d CPU threads", model_name, _CPU_THREADS)
        factory = WhisperModel
        if factory is None:
            from faster_whisper import WhisperModel as factory
        _model = factory(
            model_name,
            device="cpu",
            compute_type="int8",
            cpu_threads=_CPU_THREADS,
        )
        _model_name = model_name
    return _model


class NoSpeechError(RuntimeError):
    pass


def _has_speech(path: str) -> bool:
    """Stream small chunks through bundled Silero VAD before loading Whisper.

    Memory stays bounded even for long silent recordings. Silence/noise must
    not load a multi-GB model or invent a detected language/transcript.
    """
    import numpy as np
    from faster_whisper.vad import get_speech_timestamps, VadOptions
    process = subprocess.Popen([
        'ffmpeg', '-v', 'error', '-threads', '1', '-i', path,
        '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000',
        '-threads', '1', '-f', 'f32le', 'pipe:1'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        overlap = b''
        while chunk := process.stdout.read(16000 * 4 * 10):
            audio = np.frombuffer(overlap + chunk, dtype='<f4')
            if get_speech_timestamps(audio, VadOptions(min_speech_duration_ms=250)):
                return True
            overlap = chunk[-16000 * 4:]  # preserve speech across chunk boundaries
        if process.wait() != 0:
            raise RuntimeError('Audio stream could not be decoded')
        return False
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _fmt_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _transcribe_sync(mp4_path: str, model_name: str, language: str | None) -> tuple[str, str]:
    if not _has_speech(mp4_path):
        raise NoSpeechError('Nutq aniqlanmadi; video saqlandi, Whisper modeli yuklanmadi.')
    if model_name.startswith('large') and os.getenv('WHISPER_ALLOW_LARGE', 'false').lower() != 'true':
        raise RuntimeError('Large model bu serverda o‘chirilgan. Small yoki Medium tanlang.')
    base = mp4_path[:-4]          # strip ".mp4"
    txt_path = base + ".txt"
    srt_path = base + ".srt"

    # faster-whisper decodes audio internally via ffmpeg — no temp WAV needed.
    # vad_filter skips silent segments before inference — speeds up long recordings
    # with dead time (waiting room, pauses, end-of-meeting silence).
    model = _get_model(model_name)
    segments, _ = model.transcribe(
        mp4_path,
        language=language or None,
        vad_filter=True,
        initial_prompt=WHISPER_INITIAL_PROMPT or None,
        hotwords=WHISPER_HOTWORDS or None,
    )

    txt_lines: list[str] = []
    srt_blocks: list[str] = []
    for i, seg in enumerate(segments, 1):
        text = seg.text.strip()
        if not text:
            continue
        txt_lines.append(text)
        srt_blocks.append(
            f"{i}\n"
            f"{_fmt_srt_time(seg.start)} --> {_fmt_srt_time(seg.end)}\n"
            f"{text}\n"
        )

    if not txt_lines:
        raise NoSpeechError('Ovozdan tushunarli nutq aniqlanmadi. Video saqlandi.')
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(txt_lines))
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt_blocks))

    return txt_path, srt_path


async def transcribe(
    mp4_path: str,
    model_name: str = "small",
    language: str | None = None,
) -> tuple[str, str]:
    # Exit after each job to release all model RAM. Cancelling / shutting down
    # also terminates ffmpeg/VAD/model descendants, not just a Python thread.
    env = {**os.environ, 'OMP_NUM_THREADS': str(_CPU_THREADS),
           'OPENBLAS_NUM_THREADS': str(_CPU_THREADS), 'MKL_NUM_THREADS': str(_CPU_THREADS),
           'ZOOMY_WORKER_PARENT_PID': str(os.getpid())}
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).resolve()), '--worker', mp4_path, model_name, language or '',
        env=env, start_new_session=True,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(process.communicate(),
            timeout=max(10, int(os.getenv('WHISPER_JOB_TIMEOUT') or '1800')))
        if process.returncode != 0:
            raise RuntimeError(f'Whisper jarayoni to‘xtadi (exit {process.returncode}); video saqlandi.')
        result = json.loads(out.decode())
        if result['status'] == 'no_speech':
            raise NoSpeechError(result['message'])
        if result['status'] != 'ok':
            raise RuntimeError(result['message'])
        return result['txt'], result['srt']
    except asyncio.TimeoutError:
        raise RuntimeError('Whisper vaqt chegarasidan oshdi; video saqlandi.') from None
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()


if __name__ == '__main__':
    if sys.platform.startswith('linux') and os.getenv('ZOOMY_WORKER_PARENT_PID'):
        import ctypes
        # If the bot itself is SIGKILLed, do not leave an inference process
        # using CPU/RAM for the rest of the job on the SSH/services host.
        if ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            raise RuntimeError('Could not configure worker parent-death signal')
        if os.getppid() != int(os.environ['ZOOMY_WORKER_PARENT_PID']):
            raise SystemExit(1)
    os.nice(10)
    try:
        if len(sys.argv) != 5 or sys.argv[1] != '--worker':
            raise ValueError('Use the transcription worker API')
        txt, srt = _transcribe_sync(sys.argv[2], sys.argv[3], sys.argv[4] or None)
        result = {'status': 'ok', 'txt': txt, 'srt': srt}
    except NoSpeechError as exc:
        result = {'status': 'no_speech', 'message': str(exc)}
    except Exception as exc:
        result = {'status': 'error', 'message': str(exc)}
    print(json.dumps(result))
