"""Outgoing-only MTProto client; PTB owns incoming Telegram updates."""
from pyrogram import Client


class UploadOnlyClient(Client):
    async def handle_updates(self, updates):
        # no_updates=True stops dispatching, but Pyrofork 2.3.69 still starts
        # untracked handle_updates tasks that write peers to SQLite. Such a
        # task can outlive disconnect() and access the closed database. Upload
        # RPC methods fetch their own response peers synchronously; unsolicited
        # updates are unnecessary for this outgoing-only connection.
        return
