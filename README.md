# fowardBot

A Telegram MTProto **userbot** that watches a set of chats/channels for keyword matches and forwards matching messages to a control chat you choose (your Saved Messages, or a private channel/group you own). Configured entirely via slash-commands — no web UI, no BotFather token required.

---

## Stack

| Layer               | Technology                                                                                      |
| ------------------- | ----------------------------------------------------------------------------------------------- |
| Language            | Python 3.11                                                                                     |
| Telegram client     | [Telethon](https://github.com/LonamiWebs/Telethon) 1.36 (MTProto userbot)                       |
| Crypto acceleration | [cryptg](https://github.com/cher-nov/cryptg) 0.4 (optional C extension, much faster encryption) |
| Containerisation    | Docker + Docker Compose                                                                         |
| Base image          | `python:3.11-slim` (Debian Bookworm)                                                            |
| Deployment target   | Oracle Cloud Always Free ARM64 (Ampere A1), but runs on any `linux/amd64` or `linux/arm64` host |

---

## How it works

1. The bot connects to Telegram as **your own account** (userbot, not a bot account).
2. For every chat/channel on its watchlist, it **polls the recent history every `POLL_INTERVAL` seconds** (default 45s) and checks new messages.
3. When a message text or caption contains a watched keyword (whole-word, case-insensitive), the original message is **natively forwarded** to your control chat — sender attribution and media are preserved.
4. You control the keyword list and watched chats by typing slash-commands in the control chat.

### Why it polls instead of relying on live updates

A userbot session does **not** reliably receive real-time *push* updates for high-traffic broadcast channels — Telegram simply doesn't send `UpdateNewChannelMessage` for them to this kind of session (this was verified across two MTProto libraries: the update stream stays empty for these channels while the account is subscribed and the channels are actively posting). **Reading a chat's recent history on demand, however, works perfectly.** So the bot polls each watched chat's history on a short timer — this is the reliable delivery path. A live event handler also runs as a bonus; a shared dedup window means a message seen by both paths is forwarded only once.

Trade-off: forwarding latency is up to `POLL_INTERVAL` seconds (default 45s) rather than instant. Lower it for faster forwarding at the cost of more API calls.

---

## Prerequisites

- A Telegram account, **already subscribed to the channels you want to watch** (a userbot only sees chats your account is a member of).
- A Telegram API application (free — see below).
- Docker and Docker Compose installed on the host.

---

## Getting your API credentials

1. Open [https://my.telegram.org](https://my.telegram.org) and log in with your phone number.
2. Go to **API development tools**.
3. Create a new application (name and platform don't matter).
4. Note the **App api_id** (integer) and **App api_hash** (32-character hex string).

> These credentials identify your application to Telegram. Keep them secret — never commit them to version control.

---

## Setup

### 1. Get the code

```bash
git clone <repo-url> fowardBot   # or copy the folder to the host (see Oracle deploy below)
cd fowardBot
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` and fill it in:

```env
API_ID=12345678
API_HASH=0123456789abcdef0123456789abcdef
LOG_LEVEL=INFO
CONTROL_CHAT=me
```

| Variable        | Description                                                                                                                                      |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `API_ID`        | Integer from my.telegram.org                                                                                                                     |
| `API_HASH`      | 32-char hex string from my.telegram.org                                                                                                          |
| `LOG_LEVEL`     | `DEBUG`, `INFO`, `WARNING`, or `ERROR` (default: `INFO`)                                                                                         |
| `CONTROL_CHAT`  | Where commands are read and forwards are sent. Accepts `me`/`self` = Saved Messages (default), a numeric chat id (e.g. `-1001234567890`), or a bare `@username`. See below. |
| `POLL_INTERVAL` | Seconds between history polls of each watched chat (default: `45`). Lower = faster forwarding, more API calls.                                   |
| `DIAG`          | Set to `1` to enable per-message diagnostic logging (default: off). See [Diagnostics](#diagnostics).                                             |

### Using a dedicated chat instead of Saved Messages

By default the bot reads commands from and forwards matches to your **Saved Messages** (`CONTROL_CHAT=me`). To keep it out of your Saved Messages, point it at a private channel or group you own:

1. Create a **private channel** (or group) in Telegram with your account.
2. With the bot already running, send `/chatid` in that channel. The bot replies with the numeric id (e.g. `-1001234567890`).
3. Set `CONTROL_CHAT=-1001234567890` in `.env` and restart: `docker compose up -d`.

From then on, send all `/add_keyword`, `/add_chat`, etc. commands in that channel, and matches are forwarded there too. The `/chatid` command works in any chat at any time.

> The bot must be able to post in the target chat — automatic if you created it (you're the owner/admin).

### 3. Build the Docker image

```bash
docker compose build
```

The build uses a multi-stage Dockerfile: the builder stage produces wheels (compiling `cryptg`'s C extension if no prebuilt wheel is available for the platform); the runtime stage installs only those wheels, so no compiler toolchain ends up in the final image.

> **Note:** `cryptg` is optional acceleration. If it ever fails to build, remove the `cryptg==0.4.0` line from `requirements.txt` and rebuild — Telethon falls back to pure-Python crypto automatically (works identically, just slightly slower).

### 4. First-run interactive authentication

Required once. Telethon verifies your phone number and stores a session file.

```bash
docker compose run --rm forwardbot
```

Telethon will prompt you interactively:

```
Please enter your phone (or bot token): +<countrycode><number>
Please enter the code you received: <code from the Telegram app>
Please enter your password: <your 2FA password, if enabled>
```

When you see `up — forwarding watched keywords to control chat`, authentication succeeded and the session is written to the `fowardBot_session` Docker volume (`userbot_tl.session`). Press **Ctrl-C** to stop — the `--rm` flag removes the temporary container but the volume (and your session) is preserved.

> **Important:** Complete this step **before** `docker compose up -d`. If you skip it, the container exits immediately with an error asking you to run this command first.

### 5. Start the bot

```bash
docker compose up -d
docker compose logs -f forwardbot
```

You should see `watchlist: monitoring N chat(s)`, `up — forwarding …`, and `poller: started`. Within ~2 minutes `docker compose ps` reports the container as `healthy`.

---

## Re-authentication

If you ever need to log in again (session expired, volume deleted, account re-login):

```bash
docker compose down
docker volume rm fowardBot_session   # config (keywords/chats) is preserved
docker compose run --rm forwardbot   # re-authenticate interactively
docker compose up -d
```

---

## Commands

All commands are sent as messages in your **control chat** (Saved Messages by default, or the channel set in `CONTROL_CHAT`).

### Keywords

| Command                    | Description                                                                                                                                      |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `/add_keyword <phrase>`    | Add a keyword or phrase to watch. Multi-word phrases are supported (e.g. `/add_keyword Rio de Janeiro`). Case-insensitive — stored in lowercase. **Add several at once:** put one keyword per line (see below). |
| `/remove_keyword <phrase>` | Remove a keyword. Case-insensitive — `/remove_keyword RIO DE JANEIRO` removes `rio de janeiro`. Accepts one-per-line for bulk removal.            |
| `/list_keywords`           | List all active keywords.                                                                                                                        |

### Watched chats

| Command                          | Description                                                                                                                       |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `/add_chat <id or @username>`    | Add a chat to the watchlist. Use the numeric ID (e.g. `-1001234567890`) or `@username`. It's resolved immediately and the poller picks it up on the next cycle — no restart needed. **Add several at once:** separate them with spaces, commas, or newlines. |
| `/remove_chat <id or @username>` | Remove a chat from the watchlist. Accepts several (space/comma/newline separated).                                               |
| `/list_chats`                    | List all watched chats. Entries with forwarding restrictions are marked with ⚠️.                                                  |

> **Tip:** Supergroup and channel IDs are **negative**. If you pass a 13-digit ID starting with `100` and forget the leading `-`, the bot warns you and suggests the corrected value instead of silently watching the wrong chat.

### Utility

| Command   | Description                                                                                                                                                          |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/chatid` | Reply with the current chat's numeric id — handy for discovering a private channel/group id to put in `CONTROL_CHAT`. Works in **any** chat, and only your own (outgoing) messages trigger it, so nobody else can. |

### Examples

Single item per command:

```
/add_keyword RTX 5090
/add_chat @somedealschannel
/remove_chat @somedealschannel
```

Add **many keywords** in one message — one per line (Shift+Enter for a newline, Enter to send):

```
/add_keyword Monitor BenQ Mobiuz EX3410R
RTX 5090
Rio de Janeiro
```

Add **many chats** in one message — spaces, commas, or newlines all work:

```
/add_chat @durov @telegram -1001234567890
```

The bot replies with a summary, e.g. `✅ added 3: …` plus any that were duplicates or couldn't be resolved.

> **Membership prerequisite:** The bot only sees chats your Telegram account is a member of. If `/add_chat` replies that it couldn't resolve a chat, join/follow that channel in Telegram first, then re-add it.

---

## Keyword matching rules

- **Whole-word only.** `/add_keyword car` matches `"buy a car today"` but not `"cartoon"` or `"scarlet"`.
- **Case-insensitive.** Keywords are stored lowercase; matching uses `re.IGNORECASE`.
- **Multi-word phrases.** `/add_keyword Rio de Janeiro` matches the exact phrase, including internal spaces.
- **Punctuation-ending keywords** like `C++` or `.NET` are supported correctly (uses lookaround assertions instead of `\b`).
- **Applied to text and captions.** Media messages without a caption are ignored.
- Keyword changes take effect immediately — no restart needed.

---

## Project structure

```
fowardBot/
├── main.py            # Entry point: config, client, handlers, poller, heartbeat
├── handlers.py        # Commands, forwarder, warmup, and the history poller
├── config_store.py    # Config persistence (atomic JSON save, asyncio-safe)
├── matcher.py         # Keyword regex builder and match evaluator
├── Dockerfile         # Multi-stage build (builder + runtime)
├── docker-compose.yml # Single-service stack with named volumes
├── requirements.txt   # telethon==1.36.0, cryptg==0.4.0
├── .env.example       # Environment variable template
├── .gitignore         # Excludes .env, *.session, config/config.json
└── scripts/           # Server provisioning helpers
    ├── cloud-init.yml   # Oracle Cloud instance-creation script
    ├── cloud-init.sh    # Shell form of the cloud-init provisioning
    ├── first-login.sh   # Post-SSH setup (build, auth, start)
    └── init.sh          # Single-script bootstrap alternative
```

Two Docker named volumes are created automatically by Compose:

| Volume              | Contents                                       |
| ------------------- | ---------------------------------------------- |
| `fowardBot_session` | Telethon session file (`userbot_tl.session`)   |
| `fowardBot_config`  | Bot configuration (`config.json`)              |

Both volumes persist across container restarts and image rebuilds.

---

## Deploying to a fresh Oracle Cloud VM

Provisioning is scripted (Docker, firewall, swap, auto-updates, project dir).

| Script                   | When                                                           | How                                 |
| ------------------------ | -------------------------------------------------------------- | ----------------------------------- |
| `scripts/cloud-init.yml` | **During instance creation** — before you have SSH access     | Paste/upload in the Oracle Cloud UI |
| `scripts/first-login.sh` | **After your first SSH login**                                 | `bash ~/fowardBot/scripts/first-login.sh` |
| `scripts/init.sh`        | Alternative: one-shot bootstrap of an already-running instance | `sudo bash scripts/init.sh`         |

**Step 1 — instance creation:** when creating the Ampere A1 instance (Ubuntu 24.04), under **Advanced options → Initialization script** paste the contents of `scripts/cloud-init.yml`. It installs Docker, opens only SSH on the firewall, adds 2 GB swap (safety buffer for the `cryptg` wheel build), enables fail2ban + unattended-upgrades, and creates `~/fowardBot`.

**Step 2 — copy the code and run setup:**

```bash
# From your local machine — copy the project to the instance
scp -r ./fowardBot ubuntu@<instance-ip>:~/

# On the instance
bash ~/fowardBot/scripts/first-login.sh
```

`first-login.sh` confirms Docker is up, creates `.env` (prompting for `API_ID`/`API_HASH`), builds the image, runs the interactive Telegram auth, then starts the bot detached.

> **Note — keywords/chats don't transfer automatically.** The watchlist lives in the `fowardBot_config` volume on whatever machine created it. On a fresh VM you start empty: re-add via `/add_keyword` and `/add_chat`, or copy `config.json` into the new volume (see below).

---

## Operational notes

### Forwarding latency

Forwarding happens within `POLL_INTERVAL` seconds (default 45s) of a post. Set `POLL_INTERVAL` lower in `.env` and restart for faster delivery, higher for fewer API calls.

### No backlog spam

On startup (and when you add a new chat) the poller **baselines** each chat — it records the latest message id and forwards nothing older. Only messages posted after that point are ever forwarded.

### Forwarding restrictions

Some channels enable "Restrict saving content". When the bot tries to forward such a message, Telegram rejects it (`ChatForwardsRestrictedError`). The bot logs one warning per channel, suppresses further warnings, and marks the channel with ⚠️ in `/list_chats`. Re-adding the chat lets it try once more.

### Diagnostics

Set `DIAG=1` in `.env` and restart to log every message the live event handler receives. Note: since the bot relies on **polling** (live push is unreliable for these channels), the most useful signal is the poller's own log line:

```
forwarded (poll) chat=-100… msg=12345 via keyword='cupom'
```

If forwards seem sparse, it's almost always because posts don't contain your exact (whole-word) keywords — check `/list_keywords`.

### Migrating the watchlist between machines

`config.json` lives in the `fowardBot_config` volume. To copy it to another host:

```bash
# On the source host
docker run --rm -v fowardBot_config:/c -v "$PWD":/out alpine cp /c/config.json /out/config.json
# scp config.json to the new host, then on the new host (before/after first start):
docker run --rm -v fowardBot_config:/c -v "$PWD":/in alpine cp /in/config.json /c/config.json
docker compose restart forwardbot
```

### Health check

The container exposes a health check based on a heartbeat file at `/sessions/heartbeat`, touched at startup and refreshed every 60 seconds. If it's missing or older than 3 minutes, Docker marks the container unhealthy. Check with `docker compose ps`.

### Persistence across restarts

Keywords and watched chats are saved to `config.json` after every mutating command and survive restarts and rebuilds. The in-memory dedup window (prevents double-forwarding) resets on restart — expected and harmless.
