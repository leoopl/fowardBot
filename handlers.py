import asyncio
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING

from pyrogram import Client, filters
from pyrogram.errors import ChatForwardsRestricted, FloodWait

import matcher

if TYPE_CHECKING:
    from config_store import ConfigStore

log = logging.getLogger("forwardbot")

# Module-level process-wide state (not config — intentionally not in ConfigStore).
# forwards_blocked must never be reassigned (only .add/.discard/.clear) so that
# imported references stay valid.
forwards_blocked: set[int | str] = set()


class ProcessingSeen:
    """Bounded FIFO; oldest key (by insertion order) is evicted when size exceeds maxlen."""

    def __init__(self, maxlen: int = 4096) -> None:
        self._maxlen = maxlen
        self._od: "OrderedDict[tuple[int, int], None]" = OrderedDict()

    def check_and_add(self, key: tuple[int, int]) -> bool:
        if key in self._od:
            return True
        self._od[key] = None
        if len(self._od) > self._maxlen:
            self._od.popitem(last=False)
        return False

    def __len__(self) -> int:
        return len(self._od)


processing_seen = ProcessingSeen()


async def safe_forward(message) -> None:
    cid = message.chat.id
    uname = (message.chat.username or "").lower()
    if cid in forwards_blocked or (uname and uname in forwards_blocked):
        return
    try:
        await message.forward("me")
        return
    except FloodWait as e:
        wait = getattr(e, "value", getattr(e, "x", 5))
        log.warning("FloodWait %ss — sleeping then retrying once", wait)
        await asyncio.sleep(wait + 1)
    except ChatForwardsRestricted:
        # cid/uname already computed above
        new = cid not in forwards_blocked
        forwards_blocked.add(cid)
        if uname:
            forwards_blocked.add(uname)
        if new:
            log.warning(
                "chat %s (@%s) forbids forwarding; suppressing further warnings from this chat",
                cid,
                uname or "—",
            )
        return
    except Exception:
        log.exception("forward failed (chat=%s msg=%s)", message.chat.id, message.id)
        return
    # FloodWait retry — wrapped so a second exception doesn't propagate
    try:
        await message.forward("me")
    except Exception:
        log.exception(
            "forward retry failed (chat=%s msg=%s) — giving up",
            message.chat.id,
            message.id,
        )


def register(app: Client, store: "ConfigStore") -> None:
    # --- watchlist filter (closes over store) ---
    async def _watchlist(_, __, message):
        chat = message.chat
        if chat.id in store.chat_ids:
            return True
        uname = chat.username
        return uname is not None and uname.lower() in store.chat_usernames

    watchlist_filter = filters.create(_watchlist)

    # --- command_prefix_filter (closes over KNOWN set) ---
    KNOWN = {
        "add_keyword",
        "remove_keyword",
        "add_chat",
        "remove_chat",
        "list_keywords",
        "list_chats",
    }

    async def _is_known_command(_, __, m):
        if not m.text or not m.text.startswith("/"):
            return False
        head = m.text.split(maxsplit=1)[0][1:].lower()
        return head in KNOWN

    command_prefix_filter = filters.create(_is_known_command)

    # --- Handler A: command dispatcher (group 0) ---
    @app.on_message(
        filters.chat("me") & command_prefix_filter,
        group=0,
    )
    async def on_command(client, message):
        try:
            head, _, tail = message.text.partition(" ")
            name = head[1:].lower()
            arg = tail.strip()
            await _dispatch(name, arg, message, store)
        except Exception as e:
            log.warning("command handler error", exc_info=True)
            await message.reply(f"❌ {type(e).__name__}: {e}")

    # --- Handler B: forwarder (group 1) ---
    @app.on_message(
        filters.incoming
        & ~filters.service
        & ~filters.chat("me")
        & watchlist_filter
        & (filters.text | filters.caption),
        group=1,
    )
    async def on_monitored(client, message):
        payload = message.text or message.caption
        hit = matcher.matches(payload, store.pattern)
        if not hit:
            return
        key = (message.chat.id, message.id)
        if processing_seen.check_and_add(key):
            return
        await safe_forward(message)
        log.info(
            "forwarded chat=%s msg=%s via keyword=%r", message.chat.id, message.id, hit
        )


async def _dispatch(name: str, arg: str, message, store: "ConfigStore") -> None:
    if name == "add_keyword":
        if not arg:
            await message.reply("❌ usage: /add_keyword <phrase>")
            return
        kw = matcher.normalize(arg)
        if kw in store.keywords:
            await message.reply(f"⚠️ already in list: {kw!r}")
            return
        store.keywords.add(kw)
        store.rebuild_pattern()
        await store.save()
        await message.reply(f"✅ added {kw!r} — {len(store.keywords)} keywords total")

    elif name == "remove_keyword":
        if not arg:
            await message.reply("❌ usage: /remove_keyword <phrase>")
            return
        kw = matcher.normalize(arg)
        if kw not in store.keywords:
            await message.reply(f"⚠️ not found: {kw!r}")
            return
        store.keywords.discard(kw)
        store.rebuild_pattern()
        await store.save()
        await message.reply(f"✅ removed {kw!r}")

    elif name == "add_chat":
        if not arg:
            await message.reply("❌ usage: /add_chat <id or @username>")
            return
        parsed = _parse_chat_arg(arg)
        if isinstance(parsed, str) and parsed.startswith("⚠️"):
            await message.reply(parsed)
            return
        if isinstance(parsed, int):
            store.chat_ids.add(parsed)
            display = str(parsed)
        else:
            store.chat_usernames.add(parsed)
            display = f"@{parsed}"
        await store.save()
        await message.reply(f"✅ watching {display}")

    elif name == "remove_chat":
        if not arg:
            await message.reply("❌ usage: /remove_chat <id or @username>")
            return
        parsed = _parse_chat_arg(arg)
        if isinstance(parsed, str) and parsed.startswith("⚠️"):
            await message.reply(parsed)
            return
        if isinstance(parsed, int):
            if parsed not in store.chat_ids:
                await message.reply(f"⚠️ not in watchlist: {parsed}")
                return
            store.chat_ids.discard(parsed)
            forwards_blocked.discard(parsed)
            # also clear any username form that safe_forward may have stored
            display = str(parsed)
        else:
            if parsed not in store.chat_usernames:
                await message.reply(f"⚠️ not in watchlist: @{parsed}")
                return
            store.chat_usernames.discard(parsed)
            forwards_blocked.discard(parsed)
            display = f"@{parsed}"
        await store.save()
        await message.reply(f"✅ removed {display}")

    elif name == "list_keywords":
        if store.keywords:
            body = "\n".join(sorted(store.keywords))
        else:
            body = "(empty)"
        await message.reply(body)

    elif name == "list_chats":
        lines = []
        for cid in sorted(store.chat_ids):
            suffix = " ⚠️ forwards restricted" if cid in forwards_blocked else ""
            lines.append(f"{cid}{suffix}")
        for uname in sorted(store.chat_usernames):
            suffix = " ⚠️ forwards restricted" if uname in forwards_blocked else ""
            lines.append(f"@{uname}{suffix}")
        await message.reply("\n".join(lines) if lines else "(empty)")


def _parse_chat_arg(arg: str) -> "int | str":
    """Return int for numeric ids, lowercased @-stripped str for usernames.

    Returns a string starting with '⚠️' if the argument looks like a
    supergroup/channel id missing its leading '-'.
    """
    try:
        n = int(arg)
    except ValueError:
        return arg.lstrip("@").lower()
    # Narrow guard: 13-digit positive integer starting with "100" is almost
    # certainly a supergroup/channel id with the leading '-' stripped.
    s = arg.lstrip("-")
    if n > 0 and s.startswith("100") and len(s) == 13:
        return f"⚠️ supergroup/channel IDs are negative — did you mean -{s}?"
    return n
