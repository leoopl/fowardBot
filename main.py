import asyncio
import logging
import os
import pathlib
import sys

from telethon import TelegramClient

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
# Telethon is chatty at INFO/DEBUG; keep its own logger quiet unless we asked
# for DEBUG, so our own forwardbot lines stay readable.
if level != "DEBUG":
    logging.getLogger("telethon").setLevel(logging.WARNING)

# --- Constants ---
# New Telethon session (its file format differs from the old Pyrogram one, so we
# use a distinct name; a fresh login is required once).
SESSION = "/sessions/userbot_tl"
SESSION_FILE = pathlib.Path(SESSION + ".session")
HB_PATH = pathlib.Path("/sessions/heartbeat")
CONFIG_PATH = "/app/config/config.json"


def _parse_control_chat(raw: str) -> "int | str":
    """Resolve CONTROL_CHAT into a value Telethon accepts as an entity.

    "me"/"self" (or empty) → Saved Messages. A numeric string → int chat id
    (e.g. a private channel like -1001234567890). Anything else → the raw
    string (a @username).
    """
    raw = (raw or "me").strip()
    if raw.lower() in ("me", "self"):
        return "me"
    try:
        return int(raw)
    except ValueError:
        return raw


CONTROL_CHAT = _parse_control_chat(os.getenv("CONTROL_CHAT", "me"))
log.info("control chat: %s", CONTROL_CHAT)

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

client = TelegramClient(
    SESSION,
    int(os.environ["API_ID"]),
    os.environ["API_HASH"],
)


# --- Heartbeat ---
async def heartbeat_loop() -> None:
    while True:
        HB_PATH.touch()
        log.debug(
            "heartbeat: processing_seen=%d forwards_blocked=%d watched=%d",
            len(handlers.processing_seen),
            len(handlers.forwards_blocked),
            len(handlers.watched_ids),
        )
        await asyncio.sleep(60)


async def amain() -> None:
    HB_PATH.touch()  # materialise immediately so healthcheck has a file at t=0
    await client.start()  # interactive auth on first run (phone + code + 2FA)
    await handlers.warmup(client, store, CONTROL_CHAT)
    handlers.register(client, store, CONTROL_CHAT)
    asyncio.create_task(heartbeat_loop())
    # Telegram doesn't reliably push real-time updates for these channels to a
    # userbot session, so poll their history as the reliable delivery path.
    poll_interval = int(os.getenv("POLL_INTERVAL", "45"))
    asyncio.create_task(handlers.poll_loop(client, store, CONTROL_CHAT, poll_interval))
    log.info("up — forwarding watched keywords to control chat (poll every %ss)", poll_interval)
    await client.run_until_disconnected()


client.loop.run_until_complete(amain())
