"""
Persistence layer – supports PostgreSQL (production) and local JSON (dev).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import aiofiles
import structlog

logger = structlog.get_logger()


class PersistenceManager:
    """Manages posted-screenshot history and caption-pattern tracking."""

    def __init__(self, database_url: Optional[str] = None, data_dir: str = "./data"):
        self.database_url = database_url
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.posted_screenshots: set[str] = set()
        self.caption_history: dict[str, int] = {}
        self.scraped_queue: list[dict] = []
        self.failed_queue: list[dict] = []

    # ── Bootstrap ────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        if self.database_url:
            await self._init_postgres()
        else:
            await self._init_files()

    async def _init_postgres(self) -> None:
        import asyncpg

        conn = await asyncpg.connect(self.database_url)
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS posted_screenshots (
                    screenshot_url VARCHAR(500) PRIMARY KEY,
                    posted_at      TIMESTAMPTZ DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS caption_history (
                    pattern   VARCHAR(200) PRIMARY KEY,
                    use_count INTEGER DEFAULT 1,
                    updated   TIMESTAMPTZ DEFAULT now()
                );
            """)
            rows = await conn.fetch("SELECT screenshot_url FROM posted_screenshots")
            self.posted_screenshots = {r["screenshot_url"] for r in rows}

            rows = await conn.fetch("SELECT pattern, use_count FROM caption_history")
            self.caption_history = {r["pattern"]: r["use_count"] for r in rows}

            logger.info("persistence.postgres.loaded",
                        posted=len(self.posted_screenshots),
                        patterns=len(self.caption_history))
        finally:
            await conn.close()

    async def _init_files(self) -> None:
        posted_path = self.data_dir / "posted_history.json"
        caption_path = self.data_dir / "caption_history.json"
        queue_path = self.data_dir / "scraped_queue.json"
        failed_path = self.data_dir / "failed_queue.json"

        if posted_path.exists():
            async with aiofiles.open(posted_path, "r") as f:
                self.posted_screenshots = set(json.loads(await f.read()))

        if caption_path.exists():
            async with aiofiles.open(caption_path, "r") as f:
                self.caption_history = json.loads(await f.read())

        if queue_path.exists():
            async with aiofiles.open(queue_path, "r") as f:
                self.scraped_queue = json.loads(await f.read())

        if failed_path.exists():
            async with aiofiles.open(failed_path, "r") as f:
                self.failed_queue = json.loads(await f.read())

        if self._prune_scraped_queue(self.posted_screenshots):
            await self._save_scraped_queue_file()

        if self._prune_failed_queue(self.posted_screenshots):
            await self._save_failed_queue_file()

        logger.info("persistence.files.loaded",
                    posted=len(self.posted_screenshots),
                    patterns=len(self.caption_history),
                    scraped_queue=len(self.scraped_queue),
                    failed_queue=len(self.failed_queue))

    # ── Mutations ────────────────────────────────────────────────────────

    def is_posted(self, url: str) -> bool:
        return url in self.posted_screenshots

    async def mark_posted(self, url: str) -> None:
        self.posted_screenshots.add(url)
        if self.database_url:
            import asyncpg
            conn = await asyncpg.connect(self.database_url)
            try:
                await conn.execute(
                    "INSERT INTO posted_screenshots (screenshot_url) VALUES ($1) ON CONFLICT DO NOTHING",
                    url,
                )
            finally:
                await conn.close()
        else:
            await self._save_posted_file()

    async def track_caption_pattern(self, pattern: str) -> None:
        self.caption_history[pattern] = self.caption_history.get(pattern, 0) + 1
        if self.database_url:
            import asyncpg
            conn = await asyncpg.connect(self.database_url)
            try:
                await conn.execute("""
                    INSERT INTO caption_history (pattern, use_count) VALUES ($1, 1)
                    ON CONFLICT (pattern) DO UPDATE SET use_count = caption_history.use_count + 1, updated = now()
                """, pattern)
            finally:
                await conn.close()
        else:
            await self._save_caption_file()

    def get_overused_patterns(self, threshold: int = 2) -> list[str]:
        return sorted(
            [p for p, c in self.caption_history.items() if c > threshold],
            key=lambda p: self.caption_history[p],
            reverse=True,
        )[:10]

    async def get_cached_screenshot(self, posted: set[str]) -> Optional[dict]:
        if self._prune_scraped_queue(posted):
            await self._save_scraped_queue_file()
        return self.scraped_queue[0] if self.scraped_queue else None

    async def pop_cached_screenshot(self, posted: set[str]) -> Optional[dict]:
        if self._prune_scraped_queue(posted):
            await self._save_scraped_queue_file()
        if not self.scraped_queue:
            return None
        screenshot = self.scraped_queue.pop(0)
        await self._save_scraped_queue_file()
        return screenshot

    async def replace_scraped_queue(self, screenshots: list[dict]) -> None:
        self.scraped_queue = list(screenshots)
        await self._save_scraped_queue_file()

    async def get_failed_screenshot(self, posted: set[str]) -> Optional[dict]:
        if self._prune_failed_queue(posted):
            await self._save_failed_queue_file()
        return self.failed_queue[0] if self.failed_queue else None

    async def pop_failed_screenshot(self, posted: set[str]) -> Optional[dict]:
        if self._prune_failed_queue(posted):
            await self._save_failed_queue_file()
        if not self.failed_queue:
            return None
        screenshot = self.failed_queue.pop(0)
        await self._save_failed_queue_file()
        return screenshot

    async def add_failed_screenshot(self, screenshot: dict) -> None:
        page_url = screenshot.get("page_url")
        if not page_url:
            return
        if any(item.get("page_url") == page_url for item in self.failed_queue):
            return
        self.failed_queue.append(screenshot)
        await self._save_failed_queue_file()

    async def consume_scraped_screenshot(self, url: str) -> None:
        filtered = [item for item in self.scraped_queue if item.get("page_url") != url]
        if len(filtered) != len(self.scraped_queue):
            self.scraped_queue = filtered
            await self._save_scraped_queue_file()

    def _prune_scraped_queue(self, posted: set[str]) -> bool:
        filtered = [item for item in self.scraped_queue if item.get("page_url") not in posted]
        if len(filtered) != len(self.scraped_queue):
            self.scraped_queue = filtered
            return True
        return False

    def _prune_failed_queue(self, posted: set[str]) -> bool:
        filtered = [item for item in self.failed_queue if item.get("page_url") not in posted]
        if len(filtered) != len(self.failed_queue):
            self.failed_queue = filtered
            return True
        return False

    # ── File persistence helpers ─────────────────────────────────────────

    async def _save_posted_file(self) -> None:
        path = self.data_dir / "posted_history.json"
        async with aiofiles.open(path, "w") as f:
            await f.write(json.dumps(list(self.posted_screenshots), indent=2))

    async def _save_caption_file(self) -> None:
        path = self.data_dir / "caption_history.json"
        async with aiofiles.open(path, "w") as f:
            await f.write(json.dumps(self.caption_history, indent=2))

    async def _save_scraped_queue_file(self) -> None:
        path = self.data_dir / "scraped_queue.json"
        async with aiofiles.open(path, "w") as f:
            await f.write(json.dumps(self.scraped_queue, indent=2))

    async def _save_failed_queue_file(self) -> None:
        path = self.data_dir / "failed_queue.json"
        async with aiofiles.open(path, "w") as f:
            await f.write(json.dumps(self.failed_queue, indent=2))

    # ── Admin ────────────────────────────────────────────────────────────

    async def reset_posted(self) -> None:
        self.posted_screenshots.clear()
        if self.database_url:
            import asyncpg
            conn = await asyncpg.connect(self.database_url)
            try:
                await conn.execute("TRUNCATE posted_screenshots")
            finally:
                await conn.close()
        else:
            await self._save_posted_file()
        logger.info("persistence.posted.reset")

    async def reset_captions(self) -> None:
        self.caption_history.clear()
        if self.database_url:
            import asyncpg
            conn = await asyncpg.connect(self.database_url)
            try:
                await conn.execute("TRUNCATE caption_history")
            finally:
                await conn.close()
        else:
            await self._save_caption_file()
        logger.info("persistence.captions.reset")

    async def reset_scraped_queue(self) -> None:
        self.scraped_queue.clear()
        await self._save_scraped_queue_file()
        logger.info("persistence.scraped_queue.reset")

    async def reset_failed_queue(self) -> None:
        self.failed_queue.clear()
        await self._save_failed_queue_file()
        logger.info("persistence.failed_queue.reset")
