import asyncio
import logging
import os
import pathlib
import sys

import pyrogram
from pyrogram import Client

import handlers
from config_store import ConfigStore

# --- Logging ---
level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=level,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("forwardbot")

# --- Constants ---
SESSION_FILE = pathlib.Path("/sessions/userbot.session")
HB_PATH = pathlib.Path("/sessions/heartbeat")
CONFIG_PATH = "/app/config/config.json"

# --- Session pre-flight ---
if not SESSION_FILE.exists() and not sys.stdin.isatty():
    log.error(
        "no session file at %s and no TTY for interactive auth; "
        "run `docker compose run --rm forwardbot` first",
        SESSION_FILE,
    )
    sys.exit(2)

# --- Config + Client ---
store = ConfigStore(CONFIG_PATH)
store.load()

app = Client(
    name="userbot",
    api_id=int(os.environ["API_ID"]),
    api_hash=os.environ["API_HASH"],
    workdir="/sessions",
)

handlers.register(app, store)


# --- Heartbeat ---
async def heartbeat_loop() -> None:
    while True:
        HB_PATH.touch()
        log.debug(
            "heartbeat: processing_seen=%d forwards_blocked=%d",
            len(handlers.processing_seen),
            len(handlers.forwards_blocked),
        )
        await asyncio.sleep(60)


async def amain() -> None:
    HB_PATH.touch()  # materialise immediately so healthcheck has a file at t=0
    async with app:
        asyncio.create_task(heartbeat_loop())
        await pyrogram.idle()


app.run(amain())
