# Enhanced Steam → Instagram Bot (Python)

Async Python rewrite of the Steam screenshot → Instagram posting bot, with significantly enhanced AI capabilities.

## What Changed from the JS Version

### Architecture
- **Fully async** – `aiohttp` + `asyncio` replaces blocking `node-fetch` calls
- **Pydantic Settings** – all config validated at startup with clear error messages
- **Structured logging** – `structlog` with ISO timestamps replaces `console.log`
- **Clean separation** – 5 focused modules instead of one monolithic class
- **Type hints** everywhere for IDE support and maintainability

### AI Enhancements
| Feature | JS Version | Python Version |
|---|---|---|
| Vision analysis | Single caption output | Structured JSON: mood, colors, scene, hashtags, 3 caption candidates |
| Caption generation | Single attempt | N candidates generated → scored → best selected |
| Mood detection | ❌ | ✅ Extracted from vision, feeds into hashtag selection |
| Smart hashtags | Game-name matching only | Game + mood + vision-suggested + quality-based |
| Provider fallback | Manual try/catch | Automatic chain: primary → secondary → tertiary |
| Caption repetition | Pattern string tracking | Pattern tracking + AI-scored deduplication |
| Scoring | Random selection | AI ranks candidates on engagement potential |
| Prompt engineering | Flat text prompt | Structured JSON output prompt with style variations |

### Other Improvements
- **Pillow** replaces `sharp` for image processing (pure Python, no native deps)
- **asyncpg** for non-blocking PostgreSQL
- **APScheduler** for cron (more reliable than `node-cron`)
- **Rich** CLI output with tables and panels
- **tenacity** retry decorator available for custom retry logic
- Rate-limit delays are configurable via env vars
- Health check uses `aiohttp.web` (lightweight, async)

## Project Structure

```
enhanced_steam_bot/
├── __init__.py
├── __main__.py          # python -m enhanced_steam_bot
├── bot.py               # Orchestrator + CLI
├── config.py            # Pydantic Settings (validates .env)
├── persistence.py       # PostgreSQL / JSON file storage
├── scraper.py           # Async Steam Community scraper
├── caption_engine.py    # Multi-provider AI + vision + ranking
├── publisher.py         # Image processing + Instagram Graph API
├── requirements.txt
├── .env.example
└── README.md
```

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your credentials

# 3. Run
python -m enhanced_steam_bot              # Scheduled mode
python -m enhanced_steam_bot post         # Post once now
python -m enhanced_steam_bot test         # Dry run
python -m enhanced_steam_bot test-vision  # Test vision AI only
python -m enhanced_steam_bot status       # Show config
```

## Commands

| Command | Description |
|---|---|
| `run` (default) | Start cron scheduler + health check server |
| `post` | Execute a single posting cycle |
| `test` | Fetch best screenshot, generate caption, print results (no posting) |
| `test-vision` | Run Gemini Vision analysis on best screenshot |
| `status` | Print current bot configuration |
| `reset-history` | Clear posted-screenshot tracking |
| `reset-captions` | Clear caption-pattern history |
| `clear-cache` | Clear in-memory screenshot cache |

## AI Flow

```
┌─────────────┐
│ Screenshot   │
│ selected     │
└──────┬──────┘
       │
       ▼
┌─────────────────────────────────────────┐
│ 1. Vision Analysis (Gemini)             │
│    → mood, colors, scene, 3 captions    │
│    → suggested hashtags                 │
└──────┬──────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────┐
│ 2. Caption Scoring                      │
│    AI ranks 3 candidates → picks best   │
└──────┬──────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────┐
│ 3. Hashtag Assembly                     │
│    base + game + theme + quality        │
│    + mood + vision-suggested + variety   │
└──────┬──────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────┐
│ 4. Publish with fallback chain          │
│    Steam URLs → Process+Host → Original │
└─────────────────────────────────────────┘
```

## Environment Variables

See `.env.example` for the full list. Key additions over the JS version:

- `ENABLE_MOOD_DETECTION` – Extract mood from vision for smarter hashtags
- `ENABLE_SMART_HASHTAGS` – Use AI-suggested hashtags from image content
- `ENABLE_CAPTION_SCORING` – AI ranks caption candidates before posting
- `CAPTION_CANDIDATES` – Number of caption variants to generate (1-5)
- `STEAM_PAGE_DELAY` / `STEAM_DETAIL_DELAY` / `STEAM_USER_DELAY` – Fine-tune rate limiting
