import asyncio
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import TYPE_CHECKING

from telethon import TelegramClient, events, utils
from telethon.errors import FloodWaitError

try:  # name present in current Telethon; guard so an import change can't crash us
    from telethon.errors import ChatForwardsRestrictedError
except ImportError:  # pragma: no cover
    ChatForwardsRestrictedError = None

import matcher

if TYPE_CHECKING:
    from config_store import ConfigStore

log = logging.getLogger("forwardbot")

# --- Process-wide state (not config) ---
# forwards_blocked / watched_ids / _id_by_username are mutated in place, never
# reassigned, so references imported elsewhere stay valid.
forwards_blocked: set[int] = set()          # marked chat ids that forbid forwarding
watched_ids: set[int] = set()               # resolved marked ids of every watched chat
_id_by_username: dict[str, int] = {}        # username(lower) -> marked id (for removal)
control_id: "int | None" = None             # resolved marked id of the control chat
_last_seen_id: dict[int, int] = {}          # marked chat id -> highest message id handled (poller)
_notify_token: str = ""                      # BotFather token; empty disables notifications

KNOWN = {
    "add_keyword",
    "remove_keyword",
    "add_chat",
    "remove_chat",
    "list_keywords",
    "list_chats",
    "help",
    "start",
}

HELP_TEXT = (
    "📋 fowardBot — commands\n"
    "\n"
    "🔑 KEYWORDS (whole-word, case-insensitive)\n"
    "/add_keyword <phrase> — watch a keyword/phrase.\n"
    "   Add several at once: one per line.\n"
    "/remove_keyword <phrase> — stop watching it (one per line for several).\n"
    "/list_keywords — show all keywords.\n"
    "\n"
    "📡 WATCHED CHATS\n"
    "/add_chat <id or @username> — watch a chat/channel.\n"
    "   Add several: separate with spaces, commas, or newlines.\n"
    "/remove_chat <id or @username> — stop watching (several supported).\n"
    "/list_chats — show watched chats (⚠️ = forwarding restricted).\n"
    "\n"
    "🛠 UTILITY\n"
    "/chatid — reply with the current chat's numeric id.\n"
    "/help — show this message.\n"
    "\n"
    "ℹ️ NOTES\n"
    "• Matching posts are forwarded here automatically (within the poll interval).\n"
    "• You must be a member of a chat for it to be watched.\n"
    "• Multi-word phrases keep their spaces, e.g. \"rio de janeiro\".\n"
    "• Tip: channel IDs are negative (e.g. -1001234567890)."
)


class ProcessingSeen:
    """Bounded FIFO; oldest key (by insertion order) is evicted when size exceeds maxlen."""

    def __init__(self, maxlen: int = 4096) -> None:
        self._maxlen = maxlen
        self._od: "OrderedDict[tuple[int, int], None]" = OrderedDict()

    def check_and_add(self, key: "tuple[int, int]") -> bool:
        if key in self._od:
            return True
        self._od[key] = None
        if len(self._od) > self._maxlen:
            self._od.popitem(last=False)
        return False

    def __len__(self) -> int:
        return len(self._od)


processing_seen = ProcessingSeen()


def _is_forwards_restricted(exc: Exception) -> bool:
    if ChatForwardsRestrictedError is not None and isinstance(exc, ChatForwardsRestrictedError):
        return True
    blob = f"{type(exc).__name__} {exc}".upper()
    return "FORWARDS_RESTRICTED" in blob or "CHAT_FORWARDS_RESTRICTED" in blob


def set_notify_token(token: str) -> None:
    """Enable bot-ping notifications (empty token = disabled)."""
    global _notify_token
    _notify_token = (token or "").strip()


def _ping_text(hit: str, payload: str) -> str:
    snippet = " ".join(payload.split())[:140]
    return f"🔔 {hit} — {snippet}" if snippet else f"🔔 {hit}"


async def notify(text: str) -> None:
    """Post `text` to the control chat via a BotFather bot so YOU get a push.

    A userbot's own forwards never notify the account that sent them, so a
    separate bot (admin in the control chat) sends this alert. No-op unless
    NOTIFY_BOT_TOKEN is set and the control chat resolved to an id a bot can
    post to (a channel/group, not 'me').
    """
    if not _notify_token or control_id is None:
        return
    try:
        await asyncio.to_thread(_notify_sync, text)
    except Exception as e:
        log.warning("notify failed (%s) — check NOTIFY_BOT_TOKEN and that the bot is admin", e)


def _notify_sync(text: str) -> None:
    url = f"https://api.telegram.org/bot{_notify_token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": control_id, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        raise RuntimeError(f"Bot API {e.code}: {body}") from None


async def safe_forward(message, dest) -> None:
    """Forward `message` to `dest`, tolerating rate limits and content protection."""
    cid = message.chat_id
    if cid in forwards_blocked:
        return
    try:
        await message.forward_to(dest)
        return
    except FloodWaitError as e:
        wait = getattr(e, "seconds", 5)
        log.warning("FloodWait %ss — sleeping then retrying once", wait)
        await asyncio.sleep(wait + 1)
    except Exception as e:
        if _is_forwards_restricted(e):
            if cid not in forwards_blocked:
                forwards_blocked.add(cid)
                log.warning(
                    "chat %s forbids forwarding; suppressing further warnings from this chat",
                    cid,
                )
            return
        log.exception("forward failed (chat=%s msg=%s)", cid, message.id)
        return
    # FloodWait retry — wrapped so a second exception doesn't propagate
    try:
        await message.forward_to(dest)
    except Exception:
        log.exception("forward retry failed (chat=%s msg=%s) — giving up", cid, message.id)


async def refresh_watched_ids(client: TelegramClient, store: "ConfigStore") -> None:
    """Resolve the watchlist to marked ids for O(1) matching against event.chat_id.

    Numeric entries are already marked ids; @usernames are resolved to ids (and
    cached). Failures are logged, not fatal.
    """
    ids: set[int] = set(store.chat_ids)
    by_username: dict[str, int] = {}
    for uname in store.chat_usernames:
        try:
            ent = await client.get_entity(uname)
            mid = utils.get_peer_id(ent)
            ids.add(mid)
            by_username[uname] = mid
        except Exception as e:
            log.warning(
                "watchlist: could NOT resolve @%s (%s: %s) — are you a member?",
                uname,
                type(e).__name__,
                e,
            )
    watched_ids.clear()
    watched_ids.update(ids)
    _id_by_username.clear()
    _id_by_username.update(by_username)
    log.info("watchlist: monitoring %d chat(s)", len(watched_ids))


async def warmup(client: TelegramClient, store: "ConfigStore", control_chat) -> None:
    """Prime entity cache, resolve the control chat, and resolve the watchlist.

    Telethon delivers channel updates reliably on its own, so unlike the old
    Pyrogram path this is not load-bearing for update delivery — but priming the
    dialog/entity cache makes get_entity cheap and lets us build watched_ids.
    """
    global control_id
    try:
        n = 0
        async for _ in client.iter_dialogs():
            n += 1
        log.info("warmup: primed entity cache from %d dialogs", n)
    except Exception:
        log.exception("warmup: iter_dialogs failed — entity cache may be cold")

    try:
        ent = await client.get_entity(control_chat)
        control_id = utils.get_peer_id(ent)
        log.info("warmup: control chat resolved id=%s", control_id)
    except Exception as e:
        log.warning(
            "warmup: could NOT resolve control chat %r (%s: %s)",
            control_chat,
            type(e).__name__,
            e,
        )

    await refresh_watched_ids(client, store)


async def poll_loop(client: TelegramClient, store: "ConfigStore", control_chat, interval: int = 45) -> None:
    """Read each watched chat's newest messages on a timer and forward matches.

    Telegram does not reliably *push* real-time updates for high-traffic
    subscribed channels to this session (verified across both Pyrogram and
    Telethon — only a trickle of non-message updates arrives), but reading a
    chat's recent history on demand works perfectly. So we poll: every
    `interval` seconds, fetch messages newer than the last id we handled and
    forward any keyword match. The push handler still runs too; the shared
    `processing_seen` dedup means a message delivered by both paths forwards once.
    """
    log.info("poller: started (every %ss)", interval)
    while True:
        try:
            await _poll_once(client, store, control_chat)
        except Exception:
            log.exception("poller: cycle failed")
        await asyncio.sleep(interval)


async def _poll_once(client: TelegramClient, store: "ConfigStore", control_chat) -> None:
    for cid in list(watched_ids):
        if cid == control_id:
            continue
        last = _last_seen_id.get(cid)
        if last is None:
            # First time seeing this chat: record where we are and forward
            # nothing, so we never replay the existing backlog.
            try:
                baseline = await client.get_messages(cid, limit=1)
                _last_seen_id[cid] = baseline[0].id if baseline else 0
            except FloodWaitError as e:
                await asyncio.sleep(getattr(e, "seconds", 5) + 1)
            except Exception as e:
                log.warning("poller: baseline read failed for %s (%s)", cid, e)
            continue
        try:
            msgs = await client.get_messages(cid, limit=30, min_id=last)
        except FloodWaitError as e:
            await asyncio.sleep(getattr(e, "seconds", 5) + 1)
            continue
        except Exception as e:
            log.warning("poller: read failed for %s (%s)", cid, e)
            continue
        if not msgs:
            continue
        new_max = last
        for m in reversed(msgs):  # get_messages is newest-first; process in order
            if m.id <= last:
                continue
            new_max = max(new_max, m.id)
            payload = m.raw_text or ""
            if not payload:
                continue
            hit = matcher.matches(payload, store.pattern)
            if not hit:
                continue
            if processing_seen.check_and_add((cid, m.id)):
                continue
            await safe_forward(m, control_chat)
            log.info("forwarded (poll) chat=%s msg=%s via keyword=%r", cid, m.id, hit)
            await notify(_ping_text(hit, payload))
        _last_seen_id[cid] = new_max


def register(client: TelegramClient, store: "ConfigStore", control_chat) -> None:
    diag_on = os.getenv("DIAG", "").strip() not in ("", "0", "false", "False")

    # --- Command dispatcher: /commands typed in the control chat (in or out) ---
    @client.on(events.NewMessage(chats=control_chat))
    async def on_command(event):
        text = event.raw_text or ""
        if not text.startswith("/"):
            return
        if text.split(maxsplit=1)[0][1:].lower() not in KNOWN:
            return
        try:
            parts = text.split(maxsplit=1)
            name = parts[0][1:].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""
            await _dispatch(name, arg, event, store, client)
        except Exception as e:
            log.warning("command handler error", exc_info=True)
            await event.reply(f"❌ {type(e).__name__}: {e}")

    # --- Forwarder: incoming messages from watched chats that match a keyword ---
    @client.on(events.NewMessage(incoming=True))
    async def on_monitored(event):
        cid = event.chat_id
        if cid == control_id or cid not in watched_ids:
            return
        payload = event.raw_text or ""
        if not payload:
            return
        hit = matcher.matches(payload, store.pattern)
        if not hit:
            return
        key = (cid, event.id)
        if processing_seen.check_and_add(key):
            return
        await safe_forward(event.message, control_chat)
        log.info("forwarded chat=%s msg=%s via keyword=%r", cid, event.id, hit)
        await notify(_ping_text(hit, payload))

    # --- /chatid helper: works in ANY chat, only your own (outgoing) messages ---
    @client.on(events.NewMessage(outgoing=True, pattern=r"^/chatid(@\w+)?\s*$"))
    async def on_chatid(event):
        await event.reply(f"chat id: {event.chat_id}")

    # --- DIAG (enable with DIAG=1): log every incoming message the bot receives ---
    if diag_on:

        @client.on(events.NewMessage(incoming=True))
        async def _diag(event):
            cid = event.chat_id
            payload = event.raw_text or ""
            hit = matcher.matches(payload, store.pattern)
            log.info(
                "DIAG chat_id=%s watched=%s match=%r text=%r",
                cid,
                cid in watched_ids,
                hit,
                payload[:100],
            )


async def _dispatch(name: str, arg: str, event, store: "ConfigStore", client: TelegramClient) -> None:
    if name in ("help", "start"):
        await event.reply(HELP_TEXT)
        return

    if name == "add_keyword":
        items = _split_keywords(arg)
        if not items:
            await event.reply(
                "❌ usage: /add_keyword <phrase>\n(one keyword per line to add several)"
            )
            return
        added, dupes = [], []
        for raw in items:
            kw = matcher.normalize(raw)
            if not kw:
                continue
            if kw in store.keywords:
                dupes.append(kw)
            else:
                store.keywords.add(kw)
                added.append(kw)
        if added:
            store.rebuild_pattern()
            await store.save()
        lines = []
        if added:
            lines.append(f"✅ added {len(added)}: " + ", ".join(repr(k) for k in added))
        if dupes:
            lines.append(
                f"⚠️ already present {len(dupes)}: " + ", ".join(repr(k) for k in dupes)
            )
        lines.append(f"{len(store.keywords)} keywords total")
        await event.reply("\n".join(lines))

    elif name == "remove_keyword":
        items = _split_keywords(arg)
        if not items:
            await event.reply(
                "❌ usage: /remove_keyword <phrase>\n(one keyword per line to remove several)"
            )
            return
        removed, missing = [], []
        for raw in items:
            kw = matcher.normalize(raw)
            if not kw:
                continue
            if kw in store.keywords:
                store.keywords.discard(kw)
                removed.append(kw)
            else:
                missing.append(kw)
        if removed:
            store.rebuild_pattern()
            await store.save()
        lines = []
        if removed:
            lines.append(f"✅ removed {len(removed)}: " + ", ".join(repr(k) for k in removed))
        if missing:
            lines.append(f"⚠️ not found {len(missing)}: " + ", ".join(repr(k) for k in missing))
        lines.append(f"{len(store.keywords)} keywords total")
        await event.reply("\n".join(lines))

    elif name == "add_chat":
        items = _split_chats(arg)
        if not items:
            await event.reply(
                "❌ usage: /add_chat <id or @username>\n"
                "(separate several with spaces, commas, or newlines)"
            )
            return
        added, dupes, errors = [], [], []
        for raw in items:
            parsed = _parse_chat_arg(raw)
            if isinstance(parsed, str) and parsed.startswith("⚠️"):
                errors.append(f"{raw}: {parsed}")
                continue
            if isinstance(parsed, int):
                display = str(parsed)
                if parsed in store.chat_ids:
                    dupes.append(display)
                    continue
                store.chat_ids.add(parsed)
            else:
                display = f"@{parsed}"
                if parsed in store.chat_usernames:
                    dupes.append(display)
                    continue
                store.chat_usernames.add(parsed)
            added.append((parsed, display))
        if added:
            await store.save()
        # Resolve each newly added chat so it starts being monitored immediately.
        resolved, unresolved = [], []
        for parsed, display in added:
            try:
                ent = await client.get_entity(parsed)
                mid = utils.get_peer_id(ent)
                watched_ids.add(mid)
                if isinstance(parsed, str):
                    _id_by_username[parsed] = mid
                title = utils.get_display_name(ent) or display
                resolved.append(f"{display} ({title})" if title else display)
            except Exception as e:
                unresolved.append(f"{display} ({type(e).__name__})")
        lines = []
        if resolved:
            lines.append(f"✅ watching {len(resolved)}: " + ", ".join(resolved))
        if unresolved:
            lines.append(
                f"⚠️ added but couldn't resolve {len(unresolved)}: "
                + ", ".join(unresolved)
                + " — has this account joined?"
            )
        if dupes:
            lines.append(f"⚠️ already watching {len(dupes)}: " + ", ".join(dupes))
        if errors:
            lines.append("❌ " + "\n❌ ".join(errors))
        await event.reply("\n".join(lines) if lines else "⚠️ nothing to add")

    elif name == "remove_chat":
        items = _split_chats(arg)
        if not items:
            await event.reply(
                "❌ usage: /remove_chat <id or @username>\n"
                "(separate several with spaces, commas, or newlines)"
            )
            return
        removed, missing, errors = [], [], []
        for raw in items:
            parsed = _parse_chat_arg(raw)
            if isinstance(parsed, str) and parsed.startswith("⚠️"):
                errors.append(f"{raw}: {parsed}")
                continue
            if isinstance(parsed, int):
                display = str(parsed)
                if parsed not in store.chat_ids:
                    missing.append(display)
                    continue
                store.chat_ids.discard(parsed)
                watched_ids.discard(parsed)
                forwards_blocked.discard(parsed)
            else:
                display = f"@{parsed}"
                if parsed not in store.chat_usernames:
                    missing.append(display)
                    continue
                store.chat_usernames.discard(parsed)
                mid = _id_by_username.pop(parsed, None)
                if mid is not None:
                    watched_ids.discard(mid)
                    forwards_blocked.discard(mid)
            removed.append(display)
        if removed:
            await store.save()
        lines = []
        if removed:
            lines.append(f"✅ removed {len(removed)}: " + ", ".join(removed))
        if missing:
            lines.append(f"⚠️ not in watchlist {len(missing)}: " + ", ".join(missing))
        if errors:
            lines.append("❌ " + "\n❌ ".join(errors))
        await event.reply("\n".join(lines) if lines else "⚠️ nothing to remove")

    elif name == "list_keywords":
        body = "\n".join(sorted(store.keywords)) if store.keywords else "(empty)"
        await event.reply(body)

    elif name == "list_chats":
        lines = []
        for cid in sorted(store.chat_ids):
            suffix = " ⚠️ forwards restricted" if cid in forwards_blocked else ""
            lines.append(f"{cid}{suffix}")
        for uname in sorted(store.chat_usernames):
            mid = _id_by_username.get(uname)
            suffix = " ⚠️ forwards restricted" if mid in forwards_blocked else ""
            lines.append(f"@{uname}{suffix}")
        await event.reply("\n".join(lines) if lines else "(empty)")


def _split_keywords(arg: str) -> "list[str]":
    """One keyword per line — preserves spaces so multi-word phrases survive."""
    return [line.strip() for line in arg.split("\n") if line.strip()]


def _split_chats(arg: str) -> "list[str]":
    """Chat ids/usernames never contain spaces, so split on any whitespace or comma."""
    return [s for s in re.split(r"[\s,]+", arg) if s]


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
