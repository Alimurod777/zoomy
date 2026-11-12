"""Load .env as data (Mongo URIs may contain &) and launch the bot."""
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

def load_configuration(path):
    load_dotenv(path, override=True)
    # Resolve configured relative paths against the project, regardless of CWD.
    for name in ('RECORDINGS_DIR', 'ZOOM_STATE_PATH', 'YOUTUBE_TOKEN_PATH'):
        value = os.getenv(name)
        if value:
            location = Path(value).expanduser()
            if not location.is_absolute():
                location = ROOT / location
            os.environ[name] = str(location.resolve())
    for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        os.environ.setdefault(name, '2')
    if os.getenv('TELEGRAM_TOKEN', '') in ('', 'your_bot_token_here'):
        raise ValueError('Set TELEGRAM_TOKEN and your Telegram IDs in .env first.')

def main():
    try:
        load_configuration(ROOT / '.env')
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    mongo_host = urlparse(os.getenv('MONGODB_URI', '')).hostname
    if os.getenv('MONGODB_LOCAL_ENABLED', 'false') == 'true' and mongo_host in (None, 'localhost', '127.0.0.1', '::1'):
        subprocess.run([sys.executable, str(ROOT / 'scripts/local-mongo.py'), 'start'], check=True)
    if subprocess.run(['pactl', 'info'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        subprocess.run(['pulseaudio', '--start', '--exit-idle-time=-1', '--log-level=warning'], check=True)
    os.execv(sys.executable, [sys.executable, str(ROOT / 'bot/bot.py')])

if __name__ == '__main__':
    raise SystemExit(main())
