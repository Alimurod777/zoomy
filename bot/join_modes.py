"""Per-user join preferences, frozen when a recording is constructed."""
import json
import os
from pathlib import Path
from auth_store import atomic_state_file

MODES = {'auto','web-guest','web-auth','app-guest','app-auth','web','desktop'}
PATH = Path(__file__).resolve().parents[1]/'.runtime/zoom-join-preferences.json'


def _load(path):
    # A corrupt or hand-edited file must not block every recording.
    try:
        values=json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return values if isinstance(values, dict) else {}


def mode_for(user_id, path=None):
    value=_load(path or PATH).get(str(user_id))
    mode=value or os.getenv('ZOOM_JOIN_MODE') or 'auto'
    if mode not in MODES:
        raise ValueError('Invalid Zoom join mode')
    return mode


def set_mode(user_id, mode, path=None):
    if mode not in MODES:
        raise ValueError('Invalid Zoom join mode')
    path=Path(path or PATH)
    values=_load(path)
    values[str(user_id)]=mode
    atomic_state_file(path,values)
