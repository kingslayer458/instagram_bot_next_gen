# Enhanced Steam → Instagram Bot (Python)

Async Python bot that scrapes Steam Community screenshots, scores the best candidates, generates an Instagram-ready caption, and publishes the post through the Instagram Graph API.

This document explains:

- what the bot does
- how the internal workflow works
- how to configure and run it
- how proxy, AI, and publishing fallbacks work
- how to troubleshoot common failures

## 1. What the bot does

At a high level, the bot performs one posting cycle like this:

1. Read configuration from `.env`
2. Load posting history and caption history
3. Scrape screenshots from one or more Steam profiles
4. Filter out already-posted screenshots
5. Score the remaining screenshots and select the best one
6. Generate a caption using AI, with fallback behavior if AI is unavailable
7. Try to publish the image to Instagram using multiple upload strategies
8. Save the posted result to history so it is not reused

The project is fully async and is designed to run either:

- as a one-time command
- or as a scheduled background bot inside Docker

## 2. Main features

- Async Steam scraping with multiple workers
- Optional proxy rotation for Steam requests
- AI caption generation with provider fallback
- Vision analysis support for richer captions and hashtags
- Instagram publish fallback chain for hard-to-upload images
- JSON persistence by default, optional PostgreSQL support
- Cron-based scheduling through APScheduler
- Structured logs for easier debugging

## 3. Project structure

```text
enhanced_steam_bot/
├── __init__.py
├── __main__.py          Entry point for python -m enhanced_steam_bot
├── bot.py               Main orchestrator, commands, scheduler, health server
├── config.py            Environment loading and validation
├── persistence.py       JSON/PostgreSQL storage helpers
├── scraper.py           Steam scraping, worker pool, proxy handling
├── caption_engine.py    AI caption generation and scoring
├── publisher.py         Image processing and Instagram publishing
└── ...

data/
├── caption_history.json
└── posted_history.json
```

## 4. How it works internally

### 4.1 Configuration loading

`config.py` uses `pydantic-settings` and `python-dotenv`.

What happens at startup:

- `.env` is loaded into environment variables
- settings are parsed into strongly typed fields
- invalid values fail early
- proxy lists can come from either:
  - `WEBSHARE_API_KEY`
  - `PROXY_URLS`
  - `PROXY_1`, `PROXY_2`, etc.

This keeps configuration predictable and avoids runtime surprises.

### 4.2 Scraping flow

The scraping logic lives mainly in `scraper.py`.

For each configured Steam user:

1. Open the screenshots page
2. Crawl page variants to collect screenshot detail URLs
3. Use a worker pool to fetch detail pages in parallel
4. Extract the best image URL from each detail page
5. Estimate quality and attach metadata like game name and title
6. Score each screenshot
7. Return the best candidates that have not already been posted

Important scraper behavior:

- Requests are rate-limited using configurable delays
- Multiple workers improve throughput
- Proxy rotation can be enabled for Steam requests only
- 403/429 responses trigger rotation and retry logic

### 4.3 Proxy selection and fallback

Proxy behavior is implemented in `scraper.py`.

Startup order:

1. If `PROXY_ENABLED=false`, no proxy is used
2. If `WEBSHARE_API_KEY` is set, the bot fetches proxies from the Webshare API
3. If Webshare fails or returns nothing, the bot falls back to static proxies from env vars
4. If neither source is available, it runs direct without proxies

Relevant logs:

- `webshare.fetched`
- `webshare.fetch_failed`
- `proxy.fallback_to_env_vars`
- `proxy.none_available`
- `proxy.init`
- `proxy.rotated`
- `proxy.marked_bad`

The scraper also explicitly extracts proxy credentials and passes them as `proxy_auth`, which helps avoid `407 Proxy Authentication Required` errors.

### 4.4 Caption generation flow

The AI pipeline lives in `caption_engine.py`.

Typical flow:

1. Take the selected screenshot and metadata
2. Optionally run vision analysis on the image
3. Generate multiple caption candidates
4. Score/rank the candidates
5. Pick the best caption
6. Build hashtags using game, mood, image content, and variety rules
7. Fall back to static captions if AI fails and fallback is enabled

Supported providers:

- Gemini
- OpenAI
- Anthropic

Provider selection is controlled by `AI_PROVIDER`, but the design allows fallback behavior when a provider or key is unavailable.

### 4.5 Image publishing flow

Publishing logic lives in `publisher.py`.

It uses a multi-strategy approach because Instagram often rejects or delays processing certain remote image URLs.

Publishing strategy order:

1. Try direct Steam CDN URL variants
2. Download and process the image locally, then upload it to an external host
3. Try the original source URL as a last resort

When using external image hosting, the host fallback order is:

1. `ImgBB` if `IMGBB_API_KEY` is configured
2. `catbox.moe`
3. `0x0.st`

For Instagram itself, the publish step is:

1. Create a media container
2. Poll the container status until it is ready
3. Publish it only after readiness

This fixes the common Graph API error:

- `Media ID is not available`

The current wait logic is:

- 3 attempts
- 10 seconds between attempts

### 4.6 Persistence

By default the bot uses JSON files in `data/`:

- `posted_history.json`
- `caption_history.json`

This allows the bot to remember:

- which screenshots have already been posted
- which caption styles or patterns were used recently

If `DATABASE_URL` is configured, PostgreSQL can be used instead.

### 4.7 Scheduling and run modes

`bot.py` supports both manual and scheduled execution.

- `post` runs one posting cycle immediately
- default run mode starts the scheduler and health server
- cron syntax is configured with `POSTING_SCHEDULE`

Example:

```text
POSTING_SCHEDULE=0 */9 * * *
```

This means the bot attempts a post every 9 hours.

## 5. Setup guide

### 5.1 Requirements

- Python 3.10+
- Docker and Docker Compose if running in containers
- Instagram Business account access with Graph API credentials
- At least one AI API key if AI captions are enabled

### 5.2 Local setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Copy the example config:

```bash
copy .env.example .env
```

3. Edit `.env` and set at minimum:

- `INSTAGRAM_ACCESS_TOKEN`
- `INSTAGRAM_PAGE_ID`
- `STEAM_USER_IDS`

4. Run a dry test:

```bash
python -m enhanced_steam_bot test
```

5. Run one real post:

```bash
python -m enhanced_steam_bot post
```

6. Run scheduled mode:

```bash
python -m enhanced_steam_bot
```

### 5.3 Docker setup

Build and run with Docker Compose:

```bash
docker compose build
docker compose up -d
```

Useful commands:

```bash
docker compose logs -f
docker compose run --rm bot post
docker compose run --rm bot test
docker compose run --rm bot status
```

If code changes are not reflected, rebuild the image:

```bash
docker compose build --no-cache
```

## 6. Command reference

| Command | Purpose |
|---|---|
| `python -m enhanced_steam_bot` | Start scheduled mode |
| `python -m enhanced_steam_bot run` | Explicit scheduled mode |
| `python -m enhanced_steam_bot post` | Run one full posting cycle |
| `python -m enhanced_steam_bot test` | Dry-run scrape and caption generation |
| `python -m enhanced_steam_bot test-vision` | Test vision analysis only |
| `python -m enhanced_steam_bot status` | Show loaded configuration/state |
| `python -m enhanced_steam_bot reset-history` | Clear posted screenshot history |
| `python -m enhanced_steam_bot reset-captions` | Clear caption history |
| `python -m enhanced_steam_bot clear-cache` | Clear in-memory scraper cache |

Docker equivalents:

| Command | Purpose |
|---|---|
| `docker compose run --rm bot post` | Run one post |
| `docker compose run --rm bot test` | Dry run |
| `docker compose run --rm bot test-vision` | Vision test |
| `docker compose run --rm bot status` | View status |

## 7. Configuration guide

### Required settings

| Variable | Purpose |
|---|---|
| `INSTAGRAM_ACCESS_TOKEN` | Meta Graph API long-lived token |
| `INSTAGRAM_PAGE_ID` | Instagram Business account ID |
| `STEAM_USER_IDS` | One or more Steam64 IDs |

### AI settings

| Variable | Purpose |
|---|---|
| `ENABLE_AI_CAPTIONS` | Enable AI caption generation |
| `ENABLE_VISION_ANALYSIS` | Enable image analysis |
| `AI_PROVIDER` | Primary provider: `gemini`, `openai`, `anthropic` |
| `AI_MODEL` | Model name |
| `GEMINI_API_KEY` | Gemini API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `CAPTION_CANDIDATES` | Number of generated candidate captions |
| `ENABLE_CAPTION_SCORING` | Rank candidates before choosing |
| `ENABLE_SMART_HASHTAGS` | Build richer hashtags |
| `ENABLE_MOOD_DETECTION` | Use mood from vision analysis |

### Publishing settings

| Variable | Purpose |
|---|---|
| `IMGBB_API_KEY` | Preferred image-host key |
| `MAX_CAPTION_LENGTH` | Instagram caption length guard |

### Scraper tuning

| Variable | Purpose |
|---|---|
| `MAX_SCREENSHOTS_PER_USER` | Upper limit per Steam profile |
| `BATCH_SIZE` | Batch sizing for processing |
| `MAX_RETRIES` | Retry count for detail fetches |
| `STEAM_PAGE_DELAY` | Delay between page fetches |
| `STEAM_DETAIL_DELAY` | Delay between detail requests |
| `STEAM_USER_DELAY` | Delay between users |
| `PARALLEL_WORKERS` | Number of concurrent workers |

### Proxy settings

| Variable | Purpose |
|---|---|
| `PROXY_ENABLED` | Enable proxy use for Steam scraping |
| `WEBSHARE_API_KEY` | Fetch proxies from Webshare API |
| `PROXY_ROTATION_INTERVAL` | Seconds before rotating proxies |
| `PROXY_URLS` | Comma-separated static proxy list |
| `PROXY_1`, `PROXY_2`, ... | Legacy static proxy entries |

## 8. Recommended first run

Use this order for a clean setup:

1. Fill in `.env`
2. Run `status`
3. Run `test`
4. Review logs
5. Run `post`
6. Enable scheduled mode only after a successful manual post

Recommended checks:

- confirm Steam screenshots are being fetched
- confirm captions are generated
- confirm image hosting fallback is not failing
- confirm Instagram publish succeeds
- confirm `posted_history.json` is updated

## 9. How to read the logs

The project uses structured logging through `structlog`.

Examples you may see:

### Scraper logs

- `steam.estimated`
- `steam.page_scraped`
- `steam.parallel_start`
- `steam.detail_ok`
- `steam.progress`
- `steam.detail_error`

### Proxy logs

- `webshare.fetched`
- `webshare.fetch_failed`
- `proxy.fallback_to_env_vars`
- `proxy.init`
- `proxy.rotated`
- `proxy.marked_bad`

### Publisher logs

- `publish.strategy1_steam_urls`
- `publish.strategy1_failed`
- `publish.strategy2_process_and_host`
- `upload.imgbb_failed`
- `upload.catbox_failed`
- `publish.strategy2_success`
- `publish.poll_status`
- `publish.strategy3_original_url`

These logs are usually enough to tell which phase failed.

## 10. Troubleshooting

### Problem: `Media ID is not available`

Cause:

- Instagram has not finished processing the media container yet

Current fix:

- the bot polls container readiness before publishing

If it still happens:

- rebuild the Docker image
- confirm the updated code is inside the running container

### Problem: `407 Proxy Authentication Required`

Cause:

- invalid proxy credentials
- expired static proxies
- proxy auth not being passed correctly

What is already implemented:

- explicit `proxy_auth` handling
- proxy rotation on bad responses
- Webshare-first fallback logic

What to check:

- `WEBSHARE_API_KEY`
- whether `webshare.fetched` appears in logs
- whether old `PROXY_N` values are still being used as fallback

### Problem: image host upload fails

Current host chain:

1. ImgBB
2. catbox.moe
3. 0x0.st

If one host fails, the next is tried.

Check logs for:

- `upload.imgbb_failed`
- `upload.catbox_failed`
- `publish.strategy2_failed`

### Problem: code changes are not taking effect in Docker

Cause:

- container is running an old image

Fix:

```bash
docker compose build --no-cache
docker compose run --rm bot post
```

## 11. Operational notes

- Start with slow scrape timings, then tune carefully
- Enable proxies only if needed for Steam rate limits
- Prefer Webshare API over hardcoded static proxies
- Keep `IMGBB_API_KEY` configured if possible for the most stable hosting path
- Test manual posting after any change to publishing or proxy code
- Do not enable scheduled posting until one manual post succeeds end-to-end

## 12. Summary of the fallback chains

### Proxy source fallback

```text
WEBSHARE_API_KEY -> static PROXY_URLS / PROXY_N -> direct connection
```

### Image host fallback

```text
ImgBB -> catbox.moe -> 0x0.st
```

### Publish strategy fallback

```text
Steam CDN variants -> process and host externally -> original image URL
```

### Caption fallback

```text
primary AI provider -> other provider / alternate path -> static caption fallback
```

## 13. Example workflow

Example manual run:

1. `docker compose run --rm bot test`
2. verify it finds screenshots
3. verify logs show proxy source
4. `docker compose run --rm bot post`
5. verify the Instagram post appears
6. verify history file is updated

That is the safest way to operate the bot after config or code changes.
