import asyncio
import json
import logging
import os
import tempfile
from typing import TYPE_CHECKING

import matcher

if TYPE_CHECKING:
    import re

log = logging.getLogger("forwardbot")


class ConfigStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.keywords: set[str] = set()
        self.chat_ids: set[int] = set()
        self.chat_usernames: set[str] = set()
        self.pattern: "re.Pattern | None" = None
        self._save_lock = asyncio.Lock()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            log.info("no config at %s — starting empty", self.path)
            data = {"keywords": [], "chats": []}
        except json.JSONDecodeError as e:
            log.error(
                "config.json is malformed (%s) — refusing to overwrite; "
                "fix the file and restart",
                e,
            )
            raise
        self.keywords = {matcher.normalize(k) for k in data.get("keywords", []) if k}
        chats_raw = data.get("chats", [])
        self.chat_ids = {c for c in chats_raw if isinstance(c, int)}
        self.chat_usernames = {
            c.lstrip("@").lower() for c in chats_raw if isinstance(c, str) and c
        }
        self.rebuild_pattern()

    def rebuild_pattern(self) -> None:
        self.pattern = matcher.compile_pattern(self.keywords)

    def _snapshot(self) -> dict:
        return {
            "keywords": sorted(self.keywords),
            "chats": sorted(self.chat_ids) + sorted(f"@{u}" for u in self.chat_usernames),
        }

    async def save(self) -> None:
        snapshot = self._snapshot()
        async with self._save_lock:
            await asyncio.to_thread(_save_sync, snapshot, self.path)


def _save_sync(data: dict, path: str) -> None:
    dir_ = os.path.dirname(path) or "."
    os.makedirs(dir_, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
