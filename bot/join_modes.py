"""Per-user join preferences, frozen when a recording is constructed."""
import json
import os
from pathlib import Path
from auth_store import atomic_state_file

MODES = {'auto','web-guest','web-auth','app-guest','app-auth','web','desktop'}
PATH = Path(__file__).resolve().parents[1]/'.runtime/zoom-join-preferences.json'


def mode_for(user_id, path=None):
    try:
        value=json.loads(Path(path or PATH).read_text()).get(str(user_id))
    except FileNotFoundError:
        value=None
    mode=value or os.getenv('ZOOM_JOIN_MODE') or 'auto'
    if mode not in MODES:
        raise ValueError('Invalid Zoom join mode')
    return mode


def set_mode(user_id, mode, path=None):
    if mode not in MODES:
        raise ValueError('Invalid Zoom join mode')
    path=Path(path or PATH)
    values=json.loads(path.read_text()) if path.exists() else {}
    values[str(user_id)]=mode
    atomic_state_file(path,values)
