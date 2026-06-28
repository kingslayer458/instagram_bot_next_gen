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
from .scraper import Screenshot, SteamScraper
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
        proxy_configured = bool(self.settings.webshare_api_key or self.settings.proxy_urls)
        status = {
            "posted_count": len(self.persistence.posted_screenshots),
            "caption_patterns": len(self.persistence.caption_history),
            "scraped_queue": len(self.persistence.scraped_queue),
            "failed_queue": len(self.persistence.failed_queue),
            "steam_users": len(self.settings.steam_user_ids),
            "schedule": self.settings.posting_schedule,
            "ai_provider": self.settings.ai_provider.value,
            "ai_model": self.settings.ai_model,
            "vision_enabled": self.settings.enable_vision_analysis,
            "caption_scoring": self.settings.enable_caption_scoring,
            "mood_detection": self.settings.enable_mood_detection,
            "caption_variety": self.settings.caption_variety.value,
            "proxy_configured": proxy_configured,
        }
        status["proxy"] = self.scraper._proxy.status()
        return status

    # ── Core workflow ────────────────────────────────────────────────────

    async def select_best_screenshot(self):
        """Return the next screenshot from the local queue, refreshing it only when empty."""
        cached = await self.persistence.get_cached_screenshot(self.persistence.posted_screenshots)
        if cached:
            best = Screenshot.from_dict(cached)
            logger.info("bot.selected_cached",
                        game=best.game_name or "Unknown",
                        quality=best.quality_estimate,
                        score=best.score)
            return best

        screenshots = await self.scraper.fetch_all_users(self.persistence.posted_screenshots)
        if not screenshots:
            return None

        await self.persistence.replace_scraped_queue([s.to_dict() for s in screenshots if not self.persistence.is_posted(s.page_url)])
        cached = await self.persistence.get_cached_screenshot(self.persistence.posted_screenshots)
        if not cached:
            logger.warning("bot.all_posted")
            return None

        best = Screenshot.from_dict(cached)
        logger.info("bot.selected_refreshed",
                    game=best.game_name or "Unknown",
                    quality=best.quality_estimate,
                    score=best.score)
        return best

    async def execute_posting(self, retry_failed: bool = False) -> bool:
        """Run a single posting cycle. Returns True on success."""
        logger.info("bot.posting_start")

        if retry_failed:
            cached = await self.persistence.pop_failed_screenshot(self.persistence.posted_screenshots)
            if not cached:
                logger.warning("bot.no_failed_screenshots")
                return False
            screenshot = Screenshot.from_dict(cached)
            logger.info("bot.selected_failed",
                        game=screenshot.game_name or "Unknown",
                        quality=screenshot.quality_estimate,
                        score=screenshot.score)
        else:
            cached = await self.persistence.pop_cached_screenshot(self.persistence.posted_screenshots)
            if cached:
                screenshot = Screenshot.from_dict(cached)
                logger.info("bot.selected_cached",
                            game=screenshot.game_name or "Unknown",
                            quality=screenshot.quality_estimate,
                            score=screenshot.score)
            else:
                screenshots = await self.scraper.fetch_all_users(self.persistence.posted_screenshots)
                if not screenshots:
                    logger.warning("bot.no_screenshots")
                    return False

                await self.persistence.replace_scraped_queue([s.to_dict() for s in screenshots if not self.persistence.is_posted(s.page_url)])
                cached = await self.persistence.pop_cached_screenshot(self.persistence.posted_screenshots)
                if not cached:
                    logger.warning("bot.all_posted")
                    return False

                screenshot = Screenshot.from_dict(cached)
                logger.info("bot.selected_refreshed",
                            game=screenshot.game_name or "Unknown",
                            quality=screenshot.quality_estimate,
                            score=screenshot.score)

        # Generate caption & hashtags
        overused = self.persistence.get_overused_patterns()
        engine = CaptionEngine(self.settings, overused_patterns=overused)
        caption_text, hashtags = await engine.generate(screenshot)

        full_caption = f"{caption_text}\n\n{' '.join(hashtags)}"
        logger.info("bot.caption_ready",
                    preview=caption_text[:80],
                    hashtag_count=len(hashtags))

        # Publish
        try:
            post_id = await self.publisher.publish(screenshot.image_url, full_caption)
        except Exception as e:
            if not retry_failed:
                await self.persistence.add_failed_screenshot(screenshot.to_dict())
            else:
                await self.persistence.add_failed_screenshot(screenshot.to_dict())
            logger.error("bot.publish_failed",
                         error=str(e),
                         game=screenshot.game_name or "Unknown")
            return False

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


async def _cmd_post(bot: SteamInstagramBot, retry_failed: bool = False):
    success = await bot.execute_posting(retry_failed=retry_failed)
    if success:
        method = bot.publisher.last_publish_method or "unknown"
        if retry_failed:
            console.print(f"[green]Retried and posted successfully using {method}.[/green]")
        else:
            console.print(f"[green]Posted successfully using {method}.[/green]")
    else:
        if retry_failed:
            console.print("[red]❌ No failed screenshots available or retry failed.[/red]")
        else:
            console.print("[red]❌ No screenshots available to post.[/red]")


async def _cmd_retry_failed(bot: SteamInstagramBot):
    console.print("[yellow]🔁 Retrying failed screenshot...[/yellow]")
    success = await bot.execute_posting(retry_failed=True)
    if success:
        method = bot.publisher.last_publish_method or "unknown"
        console.print(f"[green]Retried and posted successfully using {method}.[/green]")
    else:
        console.print("[red]❌ No failed screenshots available or retry failed.[/red]")


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


async def _cmd_move_failed_scraped(bot: SteamInstagramBot):
    """Move all failed screenshots back to scraped queue for retry."""
    current_failed = len(bot.persistence.failed_queue)
    if current_failed == 0:
        console.print("[yellow] No failed screenshots to recover.[/yellow]")
        return

    console.print(f"[yellow] Moving {current_failed} failed screenshot(s) back to scraped queue...[/yellow]")
    moved = await bot.persistence.move_failed_to_scraped()

    if moved > 0:
        console.print(f"[green] Successfully moved {moved} item(s) from failed_queue → scraped_queue.[/green]")
        new_status = bot.get_status()
        console.print(f"[dim]Scraped queue size: {new_status['scraped_queue']}[/dim]")
        console.print(f"[dim]Failed queue size: {new_status['failed_queue']}[/dim]")
    else:
        console.print("[red] No items were moved.[/red]")


async def main():
    _print_banner()

    settings = Settings()
    bot = SteamInstagramBot(settings)
    await bot.initialize()

    args = sys.argv[1:]
    command = args[0] if args else "run"
    retry_failed = any(arg in {"--retry", "--retry-failed", "retry"} for arg in args[1:])

    commands = {
        "post": lambda: _cmd_post(bot, retry_failed=retry_failed),
        "retry-failed": lambda: _cmd_retry_failed(bot),
        "test": lambda: _cmd_test(bot),
        "test-vision": lambda: _cmd_test_vision(bot),
        "status": lambda: _cmd_status(bot),
        "move-failed-scraped": lambda: _cmd_move_failed_scraped(bot),
        "reset-history": lambda: bot.persistence.reset_posted(),
        "reset-captions": lambda: bot.persistence.reset_captions(),
        "reset-queue": lambda: bot.persistence.reset_scraped_queue(),
        "reset-failed": lambda: bot.persistence.reset_failed_queue(),
        "clear-cache": lambda: bot.scraper.clear_cache(),
        "run": lambda: bot.run_scheduled(),
    }

    handler = commands.get(command)
    if handler is None:
        console.print(f"[red]Unknown command: {command}[/red]")
        console.print("Commands: run, post, retry-failed, test, test-vision, status, move-failed-scraped, reset-history, reset-captions, reset-queue, reset-failed, clear-cache")
        console.print("Flags: post --retry-failed")
        sys.exit(1)

    await handler()


def entry():
    """Package entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    entry()
