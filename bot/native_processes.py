"""Ignore already exited native processes when checking the per-user singleton."""
from pathlib import Path


def living_pids(values, proc_root=Path('/proc')):
    result=[]
    for value in values:
        pid=str(value)
        if not pid.isdecimal():
            continue
        try:
            state=(proc_root/pid/'stat').read_text().rsplit(') ',1)[1].split()[0]
        except FileNotFoundError:
            continue
        except (OSError,IndexError):
            result.append(pid)  # Unreadable is not proof that a process exited.
            continue
        if state not in {'Z','X'}:
            result.append(pid)
    return result
