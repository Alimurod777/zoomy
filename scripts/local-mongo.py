"""Local authenticated MongoDB for Alwaysdata; credentials stay in secrets/."""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from urllib.parse import quote

from pymongo import MongoClient
from pymongo.errors import OperationFailure

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime/mongodb"
CREDENTIALS = ROOT / "secrets/local-mongo.json"


def settings():
    return json.loads(CREDENTIALS.read_text())


def app_uri(config):
    return (f"mongodb://zoomy_app:{quote(config['app_password'], safe='')}@127.0.0.1:"
            f"{config['port']}/zoomy?authSource=zoomy")


def client(config, admin=False):
    return MongoClient(host="127.0.0.1", port=config["port"],
        username="zoomy_admin" if admin else "zoomy_app",
        password=config["admin_password"] if admin else config["app_password"],
        authSource="admin" if admin else "zoomy", serverSelectionTimeoutMS=5000)


def running(config):
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", config["port"])) == 0


def start(config):
    if running(config):
        # An occupied port may belong to another application; never replace it.
        return
    (RUNTIME / "data").mkdir(mode=0o700, exist_ok=True)
    subprocess.run([
        str(RUNTIME / "bin/mongod"), "--dbpath", str(RUNTIME / "data"),
        "--bind_ip", "127.0.0.1", "--port", str(config["port"]), "--auth",
        "--wiredTigerCacheSizeGB", "0.25", "--setParameter", "diagnosticDataCollectionEnabled=false",
        "--logpath", str(RUNTIME / "mongod.log"), "--logappend",
        "--pidfilepath", str(RUNTIME / "mongod.pid"), "--fork"], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def init():
    env_path = ROOT / ".env"
    contents = env_path.read_text()
    entries = dict(line.split("=", 1) for line in contents.splitlines()
                   if "=" in line and not line.lstrip().startswith("#"))
    configured_uri = os.getenv("MONGODB_URI") or entries.get("MONGODB_URI", "").strip()
    if configured_uri and entries.get("MONGODB_LOCAL_ENABLED") != "true":
        print("Existing MongoDB URI kept. Local database setup skipped.")
        return
    fresh = not CREDENTIALS.exists()
    if fresh:
        if (RUNTIME / "data/WiredTiger").exists():
            raise RuntimeError("Existing MongoDB data has no local credentials; refusing to replace it")
        config = {"port": int(os.getenv("MONGODB_LOCAL_PORT", "27018")),
                  "admin_password": secrets.token_urlsafe(32),
                  "app_password": secrets.token_urlsafe(32)}
        if running(config):
            raise RuntimeError("MongoDB port is occupied; choose MONGODB_LOCAL_PORT")
        CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(CREDENTIALS, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(config, f)
    else:
        config = settings()
    start(config)
    # --auth is enabled from the first start. Mongo's localhost exception permits
    # creating the first administrator; it closes as soon as that user exists.
    with MongoClient("127.0.0.1", config["port"], serverSelectionTimeoutMS=10000) as initial:
        try:
            initial.admin.command("createUser", "zoomy_admin", pwd=config["admin_password"],
                                  roles=[{"role": "root", "db": "admin"}])
        except OperationFailure as exc:
            if exc.code not in (13, 51003):  # Existing user or auth already active.
                raise
    with client(config, admin=True) as admin:
        existing = admin.zoomy.command("usersInfo", "zoomy_app")["users"]
        if not existing:
            admin.zoomy.command("createUser", "zoomy_app", pwd=config["app_password"],
                                roles=[{"role": "readWrite", "db": "zoomy"}])
    with client(config) as app:
        app.zoomy.command("ping")
        # Prove the least-privilege app user can actually write and read.
        app.zoomy.runtime_check.replace_one({"_id": "setup"}, {"ok": True}, upsert=True)
        assert app.zoomy.runtime_check.find_one({"_id": "setup"})["ok"]
        app.zoomy.runtime_check.delete_one({"_id": "setup"})
    lines = contents.splitlines()
    values = {"MONGODB_URI": app_uri(config), "MONGODB_LOCAL_ENABLED": "true"}
    if not entries.get("MONGODB_AUTH_KEY", "").strip():
        from cryptography.fernet import Fernet
        values["MONGODB_AUTH_KEY"] = Fernet.generate_key().decode()
    for key, value in values.items():
        if any(line.startswith(key + "=") for line in lines):
            lines = [key + "=" + value if line.startswith(key + "=") else line for line in lines]
        else:
            lines.append(key + "=" + value)
    # .env was already private, and contains the shared encryption key.
    fd = os.open(env_path, os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("Local MongoDB ready on loopback with authentication; private .env updated.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "start", "status", "stop"))
    action = parser.parse_args().action
    if action == "init":
        init()
        return
    config = settings()
    if action == "start":
        start(config)
        with client(config) as app:
            app.zoomy.command("ping")
        print("Local MongoDB ready.")
    elif action == "status":
        with client(config) as app:
            app.zoomy.command("ping")
        print("Local MongoDB authenticated connection: OK.")
    else:
        with client(config, admin=True) as admin:
            try:
                admin.admin.command("shutdown", force=False)
            except Exception:
                if running(config):
                    raise
        print("Local MongoDB stopped.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not expose passwords or a MongoDB URI in setup logs.
        print(f"MongoDB operation failed ({type(exc).__name__}). Inspect .runtime/mongodb/mongod.log.", file=sys.stderr)
        raise SystemExit(1)
