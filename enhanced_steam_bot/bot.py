#!/usr/bin/env python3
"""
Enhanced Steam → Instagram Bot (Python)
────────────────────────────────────────
Async, multi-provider AI with vision analysis, candidate ranking,
mood-aware hashtags, and structured logging.

Usage:
  python -m enhanced_steam_bot              # Start scheduled posting
  python -m enhanced_steam_bot post         # Single post now
  python -m enhanced_steam_bot test         # Dry-run test
  python -m enhanced_steam_bot test-vision  # Test vision analysis only
  python -m enhanced_steam_bot status       # Show bot status
  python -m enhanced_steam_bot reset-history
  python -m enhanced_steam_bot reset-captions
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime

import structlog
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import Settings
from .persistence import PersistenceManager
from .scraper import SteamScraper
from .caption_engine import CaptionEngine
from .publisher import InstagramPublisher

# ── Logging setup ────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(colors=True),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),
)
logger = structlog.get_logger()
console = Console()


# ── Health check server ──────────────────────────────────────────────────

async def _run_health_server(port: int, bot: "SteamInstagramBot"):
    from aiohttp import web

    async def health_handler(request):
        elapsed = time.monotonic() - bot._start_time
        days, rem = divmod(int(elapsed), 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        return web.json_response({
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "uptime": " ".join(parts),
            "bot_status": bot.get_status(),
        })

    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("health.started", port=port)


# ── Bot orchestrator ─────────────────────────────────────────────────────

class SteamInstagramBot:
    """Top-level orchestrator that ties scraping, AI, and publishing together."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._start_time = time.monotonic()
        self.persistence = PersistenceManager(database_url=settings.database_url)
        self.scraper = SteamScraper(settings)
        self.publisher = InstagramPublisher(settings)

    async def initialize(self) -> None:
        await self.persistence.initialize()
        for w in self.settings.validate_ai_config():
            logger.warning("config.ai_warning", msg=w)
        logger.info("bot.initialized",
                    steam_users=len(self.settings.steam_user_ids),
                    ai_provider=self.settings.ai_provider.value,
                    vision=self.settings.enable_vision_analysis)

    def get_status(self) -> dict:
        status = {
            "posted_count": len(self.persistence.posted_screenshots),
            "caption_patterns": len(self.persistence.caption_history),
            "steam_users": len(self.settings.steam_user_ids),
            "schedule": self.settings.posting_schedule,
            "ai_provider": self.settings.ai_provider.value,
            "ai_model": self.settings.ai_model,
            "vision_enabled": self.settings.enable_vision_analysis,
            "caption_scoring": self.settings.enable_caption_scoring,
            "mood_detection": self.settings.enable_mood_detection,
            "caption_variety": self.settings.caption_variety.value,
        }
        status["proxy"] = self.scraper._proxy.status()
        return status

    # ── Core workflow ────────────────────────────────────────────────────

    async def select_best_screenshot(self):
        """Fetch from all users, return the best unposted screenshot."""
        screenshots = await self.scraper.fetch_all_users(self.persistence.posted_screenshots)
        if not screenshots:
            return None

        unposted = [s for s in screenshots if not self.persistence.is_posted(s.page_url)]
        if not unposted:
            logger.warning("bot.all_posted")
            return None

        # Sort by most recently extracted first (freshness), then score
        unposted.sort(key=lambda s: s.extracted_at, reverse=True)
        best = unposted[0]
        logger.info("bot.selected",
                    game=best.game_name or "Unknown",
                    quality=best.quality_estimate,
                    score=best.score)
        return best

    async def execute_posting(self) -> bool:
        """Run a single posting cycle. Returns True on success."""
        logger.info("bot.posting_start")

        screenshot = await self.select_best_screenshot()
        if not screenshot:
            logger.warning("bot.no_screenshots")
            return False

        # Generate caption & hashtags
        overused = self.persistence.get_overused_patterns()
        engine = CaptionEngine(self.settings, overused_patterns=overused)
        caption_text, hashtags = await engine.generate(screenshot)

        full_caption = f"{caption_text}\n\n{' '.join(hashtags)}"
        logger.info("bot.caption_ready",
                    preview=caption_text[:80],
                    hashtag_count=len(hashtags))

        # Publish
        post_id = await self.publisher.publish(screenshot.image_url, full_caption)

        # Persist
        await self.persistence.mark_posted(screenshot.page_url)
        pattern = self._extract_pattern(caption_text)
        await self.persistence.track_caption_pattern(pattern)

        logger.info(
            "bot.posted",
            post_id=post_id,
            game=screenshot.game_name,
            method=self.publisher.last_publish_method,
        )
        return True

    @staticmethod
    def _extract_pattern(caption: str) -> str:
        import re
        cleaned = re.sub(r"[^\w\s]", " ", caption.lower())
        stop = {"this", "that", "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by"}
        words = [w for w in cleaned.split() if len(w) > 2 and w not in stop]
        return " ".join(words[:3])

    # ── Scheduled mode ───────────────────────────────────────────────────

    async def run_scheduled(self) -> None:
        """Start the APScheduler cron loop + health server."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = AsyncIOScheduler()
        parts = self.settings.posting_schedule.split()
        trigger = CronTrigger(
            minute=parts[0], hour=parts[1], day=parts[2],
            month=parts[3], day_of_week=parts[4],
        )
        scheduler.add_job(self.execute_posting, trigger, id="posting", misfire_grace_time=300)
        scheduler.start()

        logger.info("bot.scheduler_started", cron=self.settings.posting_schedule)

        await _run_health_server(self.settings.port, self)

        # Keep running
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            scheduler.shutdown()
            logger.info("bot.shutdown")


# ── CLI ──────────────────────────────────────────────────────────────────

def _print_banner():
    console.print(Panel.fit(
        "[bold cyan]Enhanced Steam → Instagram Bot[/bold cyan]\n"
        "[dim]Async Python · Multi-AI Vision · Candidate Ranking[/dim]",
        border_style="cyan",
    ))


async def _cmd_post(bot: SteamInstagramBot):
    success = await bot.execute_posting()
    if success:
        method = bot.publisher.last_publish_method or "unknown"
        console.print(f"[green]Posted successfully using {method}.[/green]")
    else:
        console.print("[red]❌ No screenshots available to post.[/red]")


async def _cmd_test(bot: SteamInstagramBot):
    console.print("[yellow]🧪 Running test (dry run)...[/yellow]")
    screenshot = await bot.select_best_screenshot()
    if not screenshot:
        console.print("[red]❌ No screenshots found.[/red]")
        return

    table = Table(title="Selected Screenshot")
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Game", screenshot.game_name or "Unknown")
    table.add_row("Quality", screenshot.quality_estimate)
    table.add_row("Score", f"{screenshot.score:.1f}")
    table.add_row("Title", screenshot.title or "N/A")
    table.add_row("Image URL", screenshot.image_url[:80] + "...")
    console.print(table)

    overused = bot.persistence.get_overused_patterns()
    engine = CaptionEngine(bot.settings, overused_patterns=overused)
    caption_text, hashtags = await engine.generate(screenshot)

    console.print(f"\n[bold]Caption:[/bold] {caption_text}")
    console.print(f"\n[bold]Hashtags ({len(hashtags)}):[/bold] {' '.join(hashtags[:15])}...")
    if screenshot.mood:
        console.print(f"[bold]Mood:[/bold] {screenshot.mood}")
    if screenshot.dominant_colors:
        console.print(f"[bold]Colors:[/bold] {', '.join(screenshot.dominant_colors)}")


async def _cmd_test_vision(bot: SteamInstagramBot):
    if not bot.settings.gemini_api_key:
        console.print("[red]❌ GEMINI_API_KEY required for vision testing.[/red]")
        return

    console.print("[yellow]👁️ Testing pure vision analysis...[/yellow]")

    # Quick mode: fetch just one screenshot directly instead of scraping everything
    console.print("[dim]Fetching a single screenshot for quick testing...[/dim]")
    screenshot = await _quick_fetch_one_screenshot(bot)

    if not screenshot:
        console.print("[yellow]Quick fetch failed, falling back to full scrape...[/yellow]")
        screenshot = await bot.select_best_screenshot()

    if not screenshot:
        console.print("[red]❌ No screenshots found.[/red]")
        return

    console.print(f"[green]Screenshot found:[/green] {screenshot.game_name or 'Unknown'} — {screenshot.image_url[:80]}...")

    engine = CaptionEngine(bot.settings)
    vision = await engine._vision_analyze(screenshot)

    table = Table(title="Vision Analysis")
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Mood", vision.mood)
    table.add_row("Colors", ", ".join(vision.colors))
    table.add_row("Scene", vision.scene)
    table.add_row("Suggested Tags", ", ".join(vision.suggested_hashtags))
    for i, c in enumerate(vision.caption_candidates):
        table.add_row(f"Caption {i+1}", c)
    console.print(table)


async def _quick_fetch_one_screenshot(bot: SteamInstagramBot):
    """Fetch a single screenshot detail without full scraping — for fast testing."""
    import re as _re

    steam_id = bot.settings.steam_user_ids[0]
    base_url = f"https://steamcommunity.com/profiles/{steam_id}/screenshots/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html",
    }

    try:
        async with __import__('aiohttp').ClientSession() as session:
            # Get the first page of screenshots
            async with session.get(base_url, headers=headers,
                                   timeout=__import__('aiohttp').ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()

            # Extract first detail URL
            urls = _re.findall(
                r'href="((?:https://steamcommunity\.com)?/sharedfiles/filedetails/\?id=\d+)"', html
            )
            if not urls:
                return None

            detail_url = urls[0]
            if detail_url.startswith("/"):
                detail_url = f"https://steamcommunity.com{detail_url}"

            # Fetch detail page
            async with session.get(detail_url, headers=headers,
                                   timeout=__import__('aiohttp').ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                detail_html = await resp.text()

            # Use the scraper's extraction logic
            scraper = bot.scraper
            image_url = scraper._extract_image_url(detail_html)
            if not image_url:
                return None

            import html as html_mod
            image_url = html_mod.unescape(image_url)

            if "steamuserimages" in image_url or "steamusercontent" in image_url:
                base = image_url.split("?")[0]
                image_url = f"{base}?imw=5000&imh=5000&ima=fit&impolicy=Letterbox&imcolor=%23000000&letterbox=false"

            game_m = (_re.search(r'class="apphub_AppName[^"]*">([^<]+)<', detail_html)
                      or _re.search(r'<div class="screenshotAppName">([^<]+)</div>', detail_html))
            title_m = (_re.search(r'<div class="screenshotName">([^<]+)</div>', detail_html)
                       or _re.search(r'<meta property="og:title" content="[^"]*::\s*([^"]+)"', detail_html))

            from .scraper import Screenshot
            return Screenshot(
                page_url=detail_url,
                image_url=image_url,
                game_name=game_m.group(1).strip() if game_m else None,
                title=title_m.group(1).strip() if title_m else None,
                quality_estimate=scraper._classify_quality(image_url),
                steam_user=steam_id,
            )
    except Exception as e:
        logger.warning("test_vision.quick_fetch_failed", error=str(e))
        return None


async def _cmd_status(bot: SteamInstagramBot):
    status = bot.get_status()
    table = Table(title="Bot Status")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")
    for k, v in status.items():
        table.add_row(k.replace("_", " ").title(), str(v))
    console.print(table)


async def main():
    _print_banner()

    settings = Settings()
    bot = SteamInstagramBot(settings)
    await bot.initialize()

    command = sys.argv[1] if len(sys.argv) > 1 else "run"

    commands = {
        "post": lambda: _cmd_post(bot),
        "test": lambda: _cmd_test(bot),
        "test-vision": lambda: _cmd_test_vision(bot),
        "status": lambda: _cmd_status(bot),
        "reset-history": lambda: bot.persistence.reset_posted(),
        "reset-captions": lambda: bot.persistence.reset_captions(),
        "clear-cache": lambda: asyncio.coroutine(lambda: bot.scraper.clear_cache())(),
        "run": lambda: bot.run_scheduled(),
    }

    handler = commands.get(command)
    if handler is None:
        console.print(f"[red]Unknown command: {command}[/red]")
        console.print("Commands: run, post, test, test-vision, status, reset-history, reset-captions, clear-cache")
        sys.exit(1)

    await handler()


def entry():
    """Package entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    entry()
