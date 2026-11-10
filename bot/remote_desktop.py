"""Temporary Zoom login desktop, reachable only through an SSH tunnel."""
from contextlib import contextmanager
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time


def _wait_port(port: int, process: subprocess.Popen) -> None:
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("Remote desktop process exited; inspect the terminal output")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Remote desktop did not start on port {port}")


@contextmanager
def remote_desktop(enabled: bool, display: int = 90, web_port: int = 6080,
                   vnc_port: int = 5901):
    if not enabled:
        yield
        return
    root = Path(__file__).resolve().parents[1]
    assets = root / ".runtime/root/usr/share/novnc"
    for program in ("Xvfb", "x11vnc"):
        if not shutil.which(program):
            raise RuntimeError("Run bash scripts/setup-runtime.sh and source scripts/runtime-env.sh first")
    if not (assets / "vnc.html").is_file():
        raise RuntimeError("noVNC assets missing; run scripts/setup-runtime.sh")
    if Path(f"/tmp/.X11-unix/X{display}").exists() or Path(f"/tmp/.X{display}-lock").exists():
        raise RuntimeError(f"Display :{display} is in use; choose another --display")
    # Reject occupied ports instead of accidentally using another desktop.
    for port in (web_port, vnc_port):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    previous = os.environ.get("DISPLAY")
    processes = []
    with tempfile.TemporaryDirectory(prefix="zoomy-login-", dir="/tmp") as temp:
        password = secrets.token_hex(4)  # VNC uses at most eight characters.
        password_file = Path(temp) / "vnc-password"
        fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(password + "\n")
        try:
            os.environ["DISPLAY"] = f":{display}"
            proc = subprocess.Popen([
                "Xvfb", f":{display}", "-screen", "0", "1280x900x24",
                "-nolisten", "tcp", "-noreset"], stdout=subprocess.DEVNULL)
            processes.append(proc)
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError("Xvfb could not start")
                if Path(f"/tmp/.X11-unix/X{display}").exists():
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Xvfb startup timed out")
            proc = subprocess.Popen([
                "x11vnc", "-display", f":{display}", "-listen", "127.0.0.1",
                "-rfbport", str(vnc_port), "-passwdfile", str(password_file),
                "-forever", "-shared", "-quiet", "-noxdamage"], stdout=subprocess.DEVNULL)
            processes.append(proc)
            _wait_port(vnc_port, proc)
            proc = subprocess.Popen([
                sys.executable, "-m", "websockify", "--web", str(assets),
                f"127.0.0.1:{web_port}", f"127.0.0.1:{vnc_port}"], stdout=subprocess.DEVNULL)
            processes.append(proc)
            _wait_port(web_port, proc)
            print(f"\nOn your computer run: ssh -N -L {web_port}:127.0.0.1:{web_port} YOUR_SSH_HOST")
            print(f"Then open http://127.0.0.1:{web_port}/vnc.html and click Connect.")
            print(f"Temporary VNC password: {password}\n", flush=True)
            yield
        finally:
            for proc in reversed(processes):
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            if previous is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = previous
