<div align="center">

# 🎭 OpusFun

**A production-ready Telegram group fun bot — reaction GIFs, couple of the day, and owner broadcasting.**

Built with Python 3.11+ · Pyrofork · MongoDB

</div>

---

## Table of contents

- [What it does](#what-it-does)
- [Screenshots](#screenshots)
- [Requirements](#requirements)
- [Installation](#installation)
- [Environment configuration](#environment-configuration)
- [Media setup](#media-setup)
- [MongoDB setup](#mongodb-setup)
- [Running locally](#running-locally)
- [Deploying on a VPS](#deploying-on-a-vps)
- [Command reference](#command-reference)
- [Owner configuration](#owner-configuration)
- [Project structure](#project-structure)
- [Customisation](#customisation)
- [Troubleshooting](#troubleshooting)
- [Testing](#testing)

---

## What it does

| Feature | Details |
|---|---|
| **15 reaction commands** | `/slap`, `/hug`, `/kill`, `/fight`… each replies with a random GIF from its own folder and a randomly chosen caption. |
| **Reply-based targeting** | A reaction only ever targets the person you *replied to*. The bot never guesses a victim from raw text. |
| **Solo commands** | `/dance`, `/beep`, `/goodnight`… work standalone, with a separate set of captions. |
| **`/couple` of the day** | Picks two distinct human members, posts a random image, and locks the group for **6 hours**. The cooldown lives in MongoDB, so it survives restarts and redeploys. |
| **Owner broadcast** | Reply to *any* message type and send it to every user and group, with retries, flood control and a delivery report. |
| **Multi-owner** | Any number of owner IDs via one env var; authorisation is centralised in `is_owner()`. |
| **Full localisation** | Every user-visible string lives in `locales/en.json` — 83 keys, 24 of them with random variants so the bot doesn't feel repetitive. |
| **Safe by construction** | All names are HTML-escaped, secrets are redacted from logs, and every handler is wrapped in centralised error handling. |

Reaction wording is intentionally **playful and cartoonish** — `/kill` and `/fight` are slapstick, never graphic — and `/couple` is explicitly framed as a random daily joke, not a real relationship.

---

## Screenshots

> _Add your own captures here once the bot is running in your group._

| `/start` | Reactions | `/couple` |
|---|---|---|
| _screenshot placeholder_ | _screenshot placeholder_ | _screenshot placeholder_ |

---

## Requirements

- **Python 3.11 or newer** (the bot refuses to start on anything older)
- **A MongoDB database** — a free [Atlas](https://www.mongodb.com/atlas) M0 cluster is plenty
- **Telegram API credentials** from [my.telegram.org](https://my.telegram.org) → *API development tools*
- **A bot token** from [@BotFather](https://t.me/BotFather)

Runtime dependencies (`requirements.txt`): `pyrofork`, `TgCrypto`, `motor`, `python-dotenv`.

---

## Installation

```bash
git clone <your-repository-url> OpusFun
cd OpusFun

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

Then create your configuration from the template:

```bash
cp .env.example .env
nano .env
```

---

## Environment configuration

`.env` is the **only** place credentials live — nothing is ever hardcoded.

### Required

| Key | Description |
|---|---|
| `API_ID` | Numeric app ID from my.telegram.org |
| `API_HASH` | 32-character app hash from my.telegram.org |
| `BOT_TOKEN` | Token from @BotFather |
| `MONGO_URI` | Connection string, e.g. `mongodb+srv://user:pass@cluster.mongodb.net/` |
| `OWNER_IDS` | Comma-separated owner user IDs, e.g. `123456789,987654321` |

### Optional (sensible defaults)

| Key | Default | Description |
|---|---|---|
| `MONGO_DB_NAME` | `opusfun` | Database name |
| `BOT_NAME` | `OpusFun` | Display name used in messages |
| `START_IMG_URL` | _(empty)_ | Image shown by `/start`. Empty = text-only card |
| `SUPPORT_URL` / `UPDATE_URL` | _(empty)_ | Buttons on the start keyboard |
| `OWNER_URL` / `DEVELOPER_URL` | _(empty)_ | Buttons on the start keyboard |
| `COUPLE_COOLDOWN_HOURS` | `6` | Hours between `/couple` runs per group |
| `BROADCAST_CONCURRENCY` | `8` | Parallel sends during a broadcast |
| `BROADCAST_SLEEP` | `0.15` | Delay between sends, in seconds |
| `MEDIA_CACHE_TTL` | `300` | Seconds a folder listing is cached |
| `WORKERS` | `8` | Pyrofork update workers |
| `DEFAULT_LANGUAGE` | `en` | Locale file to load |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FILE` | `opusfun.log` | Rotating log file; empty disables file logging |

> Buttons whose URL is left empty are **automatically hidden**, so an unconfigured bot never shows a dead link.

**Finding your user ID:** message [@userinfobot](https://t.me/userinfobot) or send `/stats` after adding a temporary ID — the rejection is logged with the ID that tried.

---

## Media setup

Every reaction reads from its own folder under `assist/`. The folders ship empty — **you supply the GIFs**, which is what gives your bot its personality.

```
assist/
├── slap/   hug/   dance/   marriage/  kill/
├── beep/   laughing/  perpose/  sleeping/
├── goodnight/  goodmorning/  welcome/
├── prank/  fight/  kick/
├── cpl/    ← images for /couple
└── gif/    ← generic fallback pool
```

You can populate them in **two ways**, and both can be mixed in the same folder:

**1. Local files** — drop them straight in:

```bash
cp ~/Downloads/slap1.gif assist/slap/
```

Supported: `.gif`, `.mp4`, `.webm`, `.webp` (sent as animations) · `.jpg`, `.jpeg`, `.png` (photos) · `.mkv`, `.mov` (videos).

**2. Remote URLs or Telegram file IDs** — add them to that folder's `links.txt`, one per line:

```text
# assist/slap/links.txt
https://example.com/funny-slap.gif
CgACAgQAAxkBAAIBY2...          # a Telegram file_id
```

File IDs are the fastest option: Telegram already has the file, so nothing is uploaded. To get one, forward a GIF to a bot like [@RawDataBot](https://t.me/RawDataBot).

**Good to know**

- Folder listings are cached for `MEDIA_CACHE_TTL` seconds; new files appear without a restart.
- Empty or missing folders never cause an error — the bot sends the caption plus a gentle hint.
- Corrupt/zero-byte files and unsupported extensions are skipped with a warning.
- Recently used items are remembered per folder, so you won't see the same GIF twice in a row.

---

## MongoDB setup

1. Create a free cluster at [MongoDB Atlas](https://www.mongodb.com/atlas).
2. **Database Access** → add a user with *Read and write to any database*.
3. **Network Access** → add an IP. Use `0.0.0.0/0` if your VPS IP is dynamic.
4. **Connect** → *Drivers* → copy the connection string into `MONGO_URI`, replacing `<password>`.

The bot creates everything it needs on first run:

| Collection | Purpose |
|---|---|
| `users` | Everyone who has started the bot (broadcast audience) |
| `groups` | Every group the bot is in |
| `couple_cooldowns` | Per-chat `/couple` timestamps |
| `couples` | Chosen pairs, so a repeat `/couple` shows the same couple |

Indexes are created automatically. If your cluster forbids index creation, it's logged as a warning and the bot keeps running.

---

## Running locally

```bash
source .venv/bin/activate
python bot.py
```

A successful start looks like:

```
Connected to MongoDB database 'opusfun'
Media warmup: 47 file(s) across 17 folder(s)
OpusFun is online as @YourBotUsername
```

Stop with `Ctrl+C` — shutdown is graceful and closes the DB connection.

**Exit codes:** `2` = configuration problem · `3` = database unreachable · `1` = unexpected error.

---

## Deploying on a VPS

### systemd (recommended)

```bash
sudo nano /etc/systemd/system/opusfun.service
```

```ini
[Unit]
Description=OpusFun Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/OpusFun
ExecStart=/home/YOUR_USER/OpusFun/.venv/bin/python bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now opusfun
sudo journalctl -u opusfun -f      # live logs
```

### screen / tmux (quick and dirty)

```bash
screen -S opusfun
source .venv/bin/activate && python bot.py
# detach with Ctrl+A then D
```

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
```

```bash
docker build -t opusfun .
docker run -d --name opusfun --env-file .env --restart unless-stopped opusfun
```

> Keep `assist/` on a mounted volume if you want to add GIFs without rebuilding the image.

---

## Command reference

### Reactions — reply to someone

| Command | Aliases | Effect |
|---|---|---|
| `/slap` | | Cartoon slap |
| `/hug` | | Warm hug |
| `/kill` | | Slapstick, non-graphic |
| `/fight` | | Comic showdown |
| `/prank` | | Playful prank |
| `/kick` | | Comedic kick GIF — **never** actually removes anyone |
| `/marriage` | `/marry` | Joke wedding |
| `/perpose` | `/propose` | Joke proposal |

### Reactions — work solo

| Command | Aliases | Effect |
|---|---|---|
| `/dance` | | Dance break |
| `/beep` | `/rona`, `/cry` | Crying / rona reaction |
| `/laughing` | `/laugh`, `/hasi` | Laughing fit |
| `/sleeping` | `/sleep` | Sleepy mood |
| `/goodnight` | `/gn` | Good night |
| `/goodmorning` | `/gm` | Good morning |
| `/welcome` | | Welcome someone (groups only) |

Reply to a person to aim any of these at them instead.

### General

| Command | Scope | Description |
|---|---|---|
| `/start` | Anywhere | Intro card with buttons |
| `/help` | Anywhere | Grouped command list |
| `/couple` `/couples` | Groups | Couple of the day (6 h cooldown) |
| `/ping` `/alive` | Anywhere | Latency and uptime |

### Owner only

| Command | Description |
|---|---|
| `/broadcast` `/gcast` | Reply to a message to send it everywhere |
| `/cleanup` | Prune blocked users and dead groups |
| `/stats` `/status` | User/group counts, uptime, DB health |

**Broadcast flags:** `-u` users only · `-g` groups only · `-f` forward instead of copy · `-p` pin after sending.

```
/broadcast -u -p     (as a reply to the message you want to send)
```

The bot copies by default, so your message arrives clean, without a "forwarded from" header. Blocked users and deleted accounts are flagged inactive automatically and skipped next time.

---

## Owner configuration

Owners are defined **only** by `OWNER_IDS`:

```env
OWNER_IDS=123456789,987654321,555555555
```

Every privileged handler goes through the same `owner_only` decorator, which calls one central `is_owner()` check. Rejections are logged with the offending user ID. There is no hardcoded fallback owner anywhere in the codebase.

---

## Project structure

```
OpusFun/
├── bot.py                 # entrypoint: config → logging → start → signals
├── config/config.py       # env loading, validation, frozen Config object
├── core/
│   ├── bot.py             # the Pyrofork client and its lifecycle
│   ├── database.py        # async MongoDB layer
│   ├── i18n.py            # locale loading, random variants, placeholders
│   ├── helpers.py         # HTML escaping, mentions, formatting
│   ├── keyboards.py       # inline keyboard builders
│   ├── reactions.py       # the reaction registry (single source of truth)
│   ├── sender.py          # media sending with fallbacks
│   └── logger.py          # structured logging + secret redaction
├── plugins/
│   ├── start.py           # /start, /help, /stats
│   ├── reactions.py       # ONE handler for all 15 reactions
│   ├── couple.py          # /couple
│   ├── broadcast.py       # /broadcast, /cleanup
│   └── tracking.py        # passive user/group registration
├── utils/
│   ├── random_media.py    # folder scanning, caching, random pick
│   ├── cooldown.py        # in-memory + MongoDB cooldowns
│   └── decorators.py      # errors, auth, scope, anti-spam
├── locales/en.json        # every user-facing string
├── assist/                # your GIFs and images
└── tests/                 # 115 tests
```

**Adding a reaction takes one entry**, not a new handler. In `core/reactions.py`:

```python
ReactionSpec(command="poke", folder="poke", message_key="reactions.poke"),
```

Then add `reactions.poke` (and `help.desc.poke`) to `locales/en.json` and drop GIFs into `assist/poke/`. The command, its help entry and its BotFather registration all follow automatically.

---

## Customisation

**Change the wording.** Edit `locales/en.json`. Any string may be a list, and one entry is picked at random:

```json
"reactions": {
  "slap": [
    "{user1} slapped {user2} into next week! 💥",
    "{user1} gave {user2} a legendary slap! ✋"
  ]
}
```

Placeholders: `{user1}` (sender), `{user2}` (target), `{user}` (the relevant person in solo messages). They are pre-escaped clickable mentions — never insert raw names yourself.

**Add a language.** Copy `en.json` to e.g. `hi.json`, translate the values, and set `DEFAULT_LANGUAGE=hi`.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `[config] …` and exit code 2 | A required env var is missing or malformed. The message names the exact key. |
| `Could not connect to MongoDB` | Wrong `MONGO_URI`, or your IP isn't in the Atlas allow-list. The URI is never printed, so check it by hand. |
| Bot starts but ignores commands | It needs to *see* messages. Either give it admin rights, or disable Group Privacy: @BotFather → *Bot Settings* → *Group Privacy* → **Turn off**. |
| Reactions send text but no GIF | That `assist/` folder is empty. Add files or entries in `links.txt`. |
| `MEDIA_EMPTY` / `WEBPAGE_CURL_FAILED` | A URL in `links.txt` isn't directly downloadable. Use a direct file link or a `file_id`. |
| `/couple` says it's too early | Working as intended — 6 h per group. Lower `COUPLE_COOLDOWN_HOURS` for testing. |
| `/couple` can't find members | Telegram only exposes recently active members to bots. Make the bot an admin and let the group chat a little. |
| `/broadcast` says owner-only | Your ID isn't in `OWNER_IDS`, or you edited `.env` without restarting. |
| Broadcast is slow | Deliberate flood protection. Raise `BROADCAST_CONCURRENCY` and lower `BROADCAST_SLEEP` — carefully. |
| `database is locked` on shutdown | A stale `opusfun.session`. Stop the bot and delete that file. |
| Two bots on one token | Telegram allows one connection per token; run only one instance. |

Set `LOG_LEVEL=DEBUG` for detail. Tokens, API hashes and MongoDB passwords are redacted from logs — including inside tracebacks — so log files are safe to share when asking for help.

---

## Testing

```bash
pip install pytest pytest-asyncio mongomock_motor
python -m pytest -q
```

115 tests cover HTML-escaping, locale key/placeholder coverage for every command, media scanning and randomness, the persistent couple cooldown (including concurrency and restart survival), broadcast error handling, the decorator stack, and log redaction. They use an in-memory MongoDB double and never touch the network.

---

<div align="center">

Made for group chats that deserve better GIFs.

</div>
