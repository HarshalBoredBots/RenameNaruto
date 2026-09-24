<p align="center">
  <img src="https://graph.org/file/2c62c5c158d42b0dc8deb.jpg" width="120"/>
  <h1 align="center">Rimuru Rename Bot</h1>
  <p align="center">Intelligent Telegram file renaming assistant powered by Great Sage</p>
</p>

---

## Features

| Feature | Description |
|---|---|
| Manual Rename | Send a file → type a new name → get renamed file |
| Auto Rename | Template-based automatic naming using episode/season/quality/audio extraction |
| Queue System | 4 concurrent jobs — extras wait in a numbered queue with live cancel |
| Metadata Injection | FFmpeg stream-copy embeds custom title, artist, author, track labels |
| Custom Thumbnail | Per-user cover art applied to every renamed file |
| Caption Template | `{filename}` `{filesize}` `{duration}` variables |
| Prefix / Suffix | Prepend / append text in both manual and auto mode |
| Dump Channel | Auto-forward renamed files to any channel |
| Log Channel | Every rename logged with old → new caption |
| Leaderboard | Today / Weekly / Monthly / All-Time rankings |
| Rename History | Last 20 files per user |
| MediaInfo | Full stream analysis |
| Screenshot Grid | Evenly-spaced frame capture |
| Sample Clip | Short preview clip extraction |
| Steal Thumbnail | Extract embedded cover art |
| Premium System | Per-user plans with daily auto-rename limit (30 free / unlimited premium) |
| Large File (>2 GB) | Optional userbot support up to 4 GB for premium users |

---

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `API_ID` | Telegram API ID — [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | Telegram API Hash — same source |
| `BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) |
| `ADMIN` | Your Telegram user ID. Multiple IDs space-separated |
| `DB_URL` | MongoDB URI e.g. `mongodb+srv://user:pass@cluster.mongodb.net` |
| `DB_NAME` | MongoDB database name e.g. `RenameBot` |
| `LOG_CHANNEL` | Channel ID for rename logs (must start with `-100`) |
| `BIN_CHANNEL` | Channel ID for file copies (must start with `-100`) |

### Optional

| Variable | Default | Description |
|---|---|---|
| `STRING_SESSION` | _(none)_ | Pyrogram string session — enables 2–4 GB file support for premium users |
| `START_PIC` | _(none)_ | Image URL or file_id shown on /start |
| `FORCE_SUB` | _(none)_ | Channel username (without @) — users must join before using the bot |

#### Generating STRING_SESSION

```python
from pyrogram import Client
import asyncio

async def main():
    async with Client("session", api_id=YOUR_API_ID, api_hash="YOUR_API_HASH") as app:
        print(await app.export_session_string())

asyncio.run(main())
```

> The account must be a **Telegram Premium** account to upload files larger than 2 GB.

---

## Deploy on Koyeb (Free Plan)

Koyeb runs Docker containers — no buildpacks needed.

### Step 1 — Prepare

1. Fork this repo to your GitHub account
2. Get credentials:
   - [my.telegram.org](https://my.telegram.org) → API ID + API Hash
   - [@BotFather](https://t.me/BotFather) → `/newbot` → Bot Token
   - [@userinfobot](https://t.me/userinfobot) → Your user ID
   - [MongoDB Atlas](https://www.mongodb.com/atlas) → free cluster → connection string
   - Two private Telegram channels with bot as admin → channel IDs (start with `-100`)

### Step 2 — Create Koyeb Service

1. Go to [koyeb.com](https://www.koyeb.com) → Sign up → **Create Service**
2. Select **GitHub** → connect your account → choose your forked repo
3. Koyeb auto-detects the `Dockerfile` — no configuration needed
4. Under **Environment Variables** add:

| Name | Value |
|---|---|
| `API_ID` | your api_id |
| `API_HASH` | your api_hash |
| `BOT_TOKEN` | your bot token |
| `ADMIN` | your user ID |
| `DB_URL` | your mongodb+srv:// string |
| `DB_NAME` | `RenameBot` |
| `LOG_CHANNEL` | `-100xxxxxxxxxx` |
| `BIN_CHANNEL` | `-100xxxxxxxxxx` |

5. Under **Ports** — set `8000` as the exposed port (health check)
6. Under **Health Check** — set path `/` method `GET`
7. Click **Deploy**

### Step 3 — Verify

Watch the build logs. When you see `Bot started`, send `/start` to your bot.

> **Koyeb free plan limits:** 1 service, 512 MB RAM, 0.1 vCPU, 2 GB bandwidth/month.
> For heavy usage upgrade to a paid plan or use Railway/Heroku.

---

## BotFather Commands

Open [@BotFather](https://t.me/BotFather) → `/setcommands` → select your bot → paste:

```
start - Start the bot
help - Command guide and feature overview
mode - Switch between Manual and Auto Rename
autorename - Set or view the Auto Rename template
setsource - Choose where to extract metadata from
setmedia - Set preferred output type for auto rename
autoqueue - View and manage your rename queue
set_caption - Set a custom caption template
see_caption - View current caption template
del_caption - Remove caption template
set_prefix - Set a filename prefix
see_prefix - View current prefix
del_prefix - Remove prefix
set_suffix - Set a filename suffix
see_suffix - View current suffix
del_suffix - Remove suffix
metadata - Configure metadata injection
view_thumb - Preview your saved thumbnail
del_thumb - Remove your saved thumbnail
leaderboard - Rename leaderboard
history - Your last 20 renamed files
premium - View your plan and feature access
dump - Configure dump channel
mi - Get MediaInfo for a file
ping - Check bot latency
donate - Support the developer
```

---

## Auto Rename — Quick Start

```
1. /mode          → tap Auto Rename
2. /autorename My Show S{season}E{episode} [{quality}]
3. /setsource     → pick: File Name / Caption / Both
4. /setmedia      → pick output type (optional)
5. Send files — renamed automatically
```

| Placeholder | Extracts from | Example |
|---|---|---|
| `{season}` | `S2`, `Season 2`, `S02E07` | `2` |
| `{episode}` | `E07`, `EP07`, `S02E07` | `07` |
| `{quality}` | `1080p`, `720p`, `4K`, `WEBRip` | `1080p` |
| `{audio}` | `Hindi`, `English`, `Dual`, `AAC` | `Hindi` |

---

## Project Structure

```
├── bot.py                  # Entry point
├── config.py               # Env vars and UI strings
├── Dockerfile              # Docker build (used by Koyeb)
├── requirements.txt        # Python dependencies
├── route.py                # Health-check endpoint (GET /)
├── messages.py             # Log message templates
├── helper/
│   ├── database.py         # MongoDB read/write
│   ├── ffmpeg.py           # FFmpeg wrappers
│   ├── ui.py               # Rimuru UI theme engine
│   ├── userbot.py          # Userbot client for >2 GB files
│   └── utils.py            # humanbytes, convert, etc.
└── plugins/
    ├── auto_rename.py      # Auto rename system + queue
    ├── file_rename.py      # Manual rename pipeline
    ├── caption.py          # Caption commands
    ├── prefix_suffix.py    # Prefix/suffix commands
    ├── thumbnail.py        # Thumbnail commands
    ├── metadata.py         # Metadata injection
    ├── leaderboard.py      # Leaderboard and history
    ├── premium.py          # Premium system
    ├── help_menu.py        # Tiered help menu
    ├── start_&_cb.py       # /start and callbacks
    ├── user_settings.py    # Dump channel settings
    ├── mediainfo.py        # MediaInfo report
    ├── media_settings.py   # Screenshot/sample settings
    ├── image_tools.py      # Screenshot, sample, upscale, steal thumb
    ├── admin_panel.py      # Admin commands
    ├── task_status.py      # Active job monitor
    └── force_subs.py       # Force subscribe gate
```

---

## Credits

Developed by [@naruto0927](https://t.me/naruto0927)

> Do not resell or redistribute without permission.
