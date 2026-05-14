# fowardBot

A Telegram MTProto userbot that watches a configurable set of chats for keyword matches and forwards them to your own Saved Messages. Configured entirely via slash-commands sent to Saved Messages — no web UI, no BotFather token required.

---

## Stack

| Layer               | Technology                                                                                      |
| ------------------- | ----------------------------------------------------------------------------------------------- |
| Language            | Python 3.11                                                                                     |
| Telegram client     | [Pyrogram](https://github.com/pyrogram/pyrogram) 2.0.106 (MTProto userbot)                      |
| Crypto acceleration | [TgCrypto](https://github.com/pyrogram/tgcrypto) 1.2.5 (optional C extension, 5–10× faster)     |
| Containerisation    | Docker + Docker Compose                                                                         |
| Base image          | `python:3.11-slim` (Debian Bookworm)                                                            |
| Deployment target   | Oracle Cloud Always Free ARM64 (Ampere A1), but runs on any `linux/amd64` or `linux/arm64` host |

---

## How it works

1. The bot connects to Telegram as **your own account** (userbot, not a bot account).
2. It listens for new messages in every chat you add to its watchlist.
3. When a message text or caption contains a watched keyword (whole-word, case-insensitive), the original message is **natively forwarded** to your Saved Messages — sender attribution and media are preserved.
4. You control the keyword list and watched chats by typing slash-commands in your own Saved Messages chat.

---

## Prerequisites

- A Telegram account.
- A Telegram API application (free — see next section).
- Docker and Docker Compose installed on the host (see below for Oracle Cloud quick-start).

---

## Oracle Cloud Always Free — server setup

Deployment is split into two scripts:

| Script                   | When                                                           | How                                 |
| ------------------------ | -------------------------------------------------------------- | ----------------------------------- |
| `scripts/cloud-init.yml` | **During instance creation** — before you even have SSH access | Paste/upload in the Oracle Cloud UI |
| `scripts/first-login.sh` | **After your first SSH login**                                 | `bash scripts/first-login.sh`       |

### Step 1 — cloud-init (instance creation)

When creating your instance on Oracle Cloud, expand **Advanced options** → **Initialization script** and upload or paste the contents of `scripts/cloud-init.yml`.

The script runs automatically on first boot and provisions the instance without any interaction:

| What                | Detail                                                                 |
| ------------------- | ---------------------------------------------------------------------- |
| System update       | Full `apt upgrade`                                                     |
| Docker CE           | From Docker's official apt repository (not the Ubuntu snap)            |
| Docker daemon       | Log rotation (20 MB × 5 files) and `live-restore`                      |
| Docker group        | Adds the `ubuntu` user to the `docker` group                           |
| 2 GB swap           | Safety buffer for the `docker compose build` TgCrypto compilation step |
| UFW firewall        | SSH allowed; all other inbound denied; outbound unrestricted           |
| fail2ban            | Bans IPs after 5 failed SSH attempts                                   |
| Unattended upgrades | Automatic security patches                                             |
| Project directory   | Creates `~/fowardBot` owned by `ubuntu`                                |

The script writes `~/.cloud-init-done` when it finishes. `first-login.sh` checks for this marker.

### Step 2 — first-login (after SSH)

Once the instance is running and you've SSH'd in, copy your project files and run the setup script:

```bash
# From your local machine — copy the project to the instance
scp -r ./fowardBot ubuntu@<instance-ip>:~/

# On the instance
bash ~/fowardBot/scripts/first-login.sh
```

The script will:

1. Confirm cloud-init completed and Docker is reachable.
2. Verify all project files are present (prompts you to copy/clone if not).
3. Create `.env` and interactively prompt for `API_ID` and `API_HASH`.
4. Run `docker compose build`.
5. Run `docker compose run --rm forwardbot` for Telegram authentication (phone number + OTP).
6. Start the bot detached with `docker compose up -d` and print recent logs.

---

## Oracle Cloud Always Free — server setup

If you're deploying on a fresh Oracle Cloud ARM64 instance (Ubuntu 24.04, `VM.Standard.A1.Flex`), run the bootstrap script once as root. It installs Docker, configures the firewall, enables automatic security updates, and creates the project directory.

```bash
# On the instance, after SSH-ing in:
sudo bash scripts/init.sh
```

What it does:

| Step                | Action                                                           |
| ------------------- | ---------------------------------------------------------------- |
| System update       | `apt upgrade` all packages                                       |
| Docker CE           | Installs from Docker's official repository (not the Ubuntu snap) |
| Docker daemon       | Configures log rotation (20 MB × 5 files) and `live-restore`     |
| Docker group        | Adds the `ubuntu` user to the `docker` group                     |
| Swap                | Creates a 2 GB swap file (safety buffer for the 6 GB instance)   |
| Firewall (UFW)      | Allows SSH only; all inbound denied; outbound unrestricted       |
| fail2ban            | Protects SSH from brute-force attempts                           |
| Unattended upgrades | Applies security patches automatically                           |
| Project directory   | Creates `~/fowardBot` owned by the `ubuntu` user                 |

After the script finishes, follow the printed "Next steps" to copy your files and authenticate.

---

## Getting your API credentials

1. Open [https://my.telegram.org](https://my.telegram.org) and log in with your phone number.
2. Go to **API development tools**.
3. Create a new application (name and platform don't matter).
4. Note the **App api_id** (integer) and **App api_hash** (32-character hex string).

> These credentials identify your application to Telegram. Keep them secret — never commit them to version control.

---

## Setup

### 1. Clone the repository

```bash
git clone <repo-url> fowardBot
cd fowardBot
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` and fill in your credentials:

```env
API_ID=12345678
API_HASH=0123456789abcdef0123456789abcdef
LOG_LEVEL=INFO
```

| Variable    | Description                                              |
| ----------- | -------------------------------------------------------- |
| `API_ID`    | Integer from my.telegram.org                             |
| `API_HASH`  | 32-char hex string from my.telegram.org                  |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` (default: `INFO`) |

### 3. Build the Docker image

```bash
docker compose build
```

The build uses a multi-stage Dockerfile: the builder stage compiles TgCrypto from source; the runtime stage installs only the pre-built wheels, so no compiler toolchain ends up in the final image.

> **ARM64 note:** If the build fails compiling TgCrypto, remove the `TgCrypto==1.2.5` line from `requirements.txt` and rebuild. Pyrogram will fall back to its pure-Python crypto path automatically — the bot works identically, just slightly slower.

### 4. First-run interactive authentication

This step is required once. Pyrogram needs to verify your phone number and store a session file.

```bash
docker compose run --rm forwardbot
```

Pyrogram will prompt you interactively:

```
Enter phone number or bot token: +<countrycode><number>
Is "+..." correct? (y/N): y
Enter confirmation code: <code from the Telegram app>
Enter password: <your 2FA password, if enabled>
```

After authentication succeeds the session is written to the `fowardBot_session` Docker volume and the bot starts. Press **Ctrl-C** to stop — the `--rm` flag removes the temporary container but the volume (and your session) is preserved.

> **Important:** Complete this step **before** `docker compose up -d`. If you skip it, the container will exit immediately with an error asking you to run this command first.

### 5. Start the bot

```bash
docker compose up -d
```

Check logs to confirm a clean start:

```bash
docker compose logs -f forwardbot
```

You should see Pyrogram's "Started" banner with no auth prompts. Within ~2 minutes `docker compose ps` will report the container as `healthy`.

---

## Re-authentication

If you ever need to log in again (session expired, volume deleted, account re-login):

```bash
# Stop the bot
docker compose down

# Remove only the session volume (config is preserved)
docker volume rm fowardBot_session

# Re-authenticate interactively
docker compose run --rm forwardbot

# Restart detached
docker compose up -d
```

---

## Commands

All commands are sent as messages in your own **Saved Messages** chat in Telegram.

### Keywords

| Command                    | Description                                                                                                                                      |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `/add_keyword <phrase>`    | Add a keyword or phrase to watch. Multi-word phrases are supported (e.g. `/add_keyword Rio de Janeiro`). Case-insensitive — stored in lowercase. |
| `/remove_keyword <phrase>` | Remove a keyword. Case-insensitive — `/remove_keyword RIO DE JANEIRO` removes `rio de janeiro`.                                                  |
| `/list_keywords`           | List all active keywords.                                                                                                                        |

### Watched chats

| Command                          | Description                                                                                                                       |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `/add_chat <id or @username>`    | Add a chat to the watchlist. Use the numeric ID (e.g. `-1001234567890`) for groups and channels, or `@username` for public chats. |
| `/remove_chat <id or @username>` | Remove a chat from the watchlist.                                                                                                 |
| `/list_chats`                    | List all watched chats. Entries with forwarding restrictions are marked with ⚠️.                                                  |

### Examples

```
/add_keyword Monitor BenQ Mobiuz EX3410R
/add_keyword RTX 5090
/list_keywords

/add_chat -1001234567890
/add_chat @somebuyandsellgroup
/list_chats

/remove_keyword RTX 5090
/remove_chat @somebuyandsellgroup
```

> **Membership prerequisite:** The bot only receives messages from chats your Telegram account is a member of. Adding a chat you haven't joined will silently produce no events. Open Telegram and join/follow the target chat before running `/add_chat`.

---

## Keyword matching rules

- **Whole-word only.** `/add_keyword car` matches `"buy a car today"` but not `"cartoon"` or `"scarlet"`.
- **Case-insensitive.** Keywords are stored lowercase; matching uses `re.IGNORECASE`.
- **Multi-word phrases.** `/add_keyword Rio de Janeiro` matches the exact phrase, including internal spaces.
- **Punctuation-ending keywords** like `C++` or `.NET` are supported correctly (uses lookaround assertions instead of `\b`).
- **Applied to text and captions.** Media messages without a caption are ignored.
- Changes take effect immediately — no restart needed.

---

## Project structure

```
fowardBot/
├── main.py            # Entry point: config, client, handlers, heartbeat
├── handlers.py        # Command dispatcher + forwarder callbacks
├── config_store.py    # Config persistence (atomic JSON save, asyncio-safe)
├── matcher.py         # Keyword regex builder and match evaluator
├── Dockerfile         # Multi-stage build (builder + runtime)
├── docker-compose.yml # Single-service stack with named volumes
├── requirements.txt   # pyrogram==2.0.106, TgCrypto==1.2.5
├── .env.example       # Environment variable template
└── .gitignore         # Excludes .env, *.session, config/config.json
```

Two Docker named volumes are created automatically by Compose:

| Volume              | Contents                                  |
| ------------------- | ----------------------------------------- |
| `fowardBot_session` | Pyrogram session file (`userbot.session`) |
| `fowardBot_config`  | Bot configuration (`config.json`)         |

Both volumes persist across container restarts and image rebuilds.

---

## Operational notes

### Forwarding restrictions

Some Telegram channels have "Restrict saving content" enabled. When the bot tries to forward a message from such a channel, Telegram rejects it. The bot logs one warning per channel and suppresses further warnings — it does not spam the logs or crash. The channel is marked with ⚠️ in `/list_chats`.

If you remove and re-add a restricted channel, the bot will try forwarding once more before suppressing again.

### Edited messages

The bot ignores edits. Pyrogram's `on_message` handler only fires on `UpdateNewMessage`; edited messages arrive as a separate `UpdateEditMessage` update that the bot never registers a handler for. Only the original send of a message is checked against keywords.

### Log levels

Set `LOG_LEVEL=DEBUG` in `.env` for verbose output including filter decisions and heartbeat stats. Restore to `INFO` for production.

### Health check

The container exposes a health check based on a heartbeat file at `/sessions/heartbeat`. The file is touched at startup and refreshed every 60 seconds by a background task. If the file is missing or older than 3 minutes, Docker marks the container unhealthy. Check with `docker compose ps`.

### Persistence across restarts

Keywords and watched chats are saved to `config.json` in the `fowardBot_config` volume after every mutating command. They survive container restarts and image rebuilds. The in-memory deduplication window (prevents double-forwarding on network reconnects) resets on restart — this is expected and harmless.
