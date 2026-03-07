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

        if posted_path.exists():
            async with aiofiles.open(posted_path, "r") as f:
                self.posted_screenshots = set(json.loads(await f.read()))

        if caption_path.exists():
            async with aiofiles.open(caption_path, "r") as f:
                self.caption_history = json.loads(await f.read())

        logger.info("persistence.files.loaded",
                    posted=len(self.posted_screenshots),
                    patterns=len(self.caption_history))

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

    # ── File persistence helpers ─────────────────────────────────────────

    async def _save_posted_file(self) -> None:
        path = self.data_dir / "posted_history.json"
        async with aiofiles.open(path, "w") as f:
            await f.write(json.dumps(list(self.posted_screenshots), indent=2))

    async def _save_caption_file(self) -> None:
        path = self.data_dir / "caption_history.json"
        async with aiofiles.open(path, "w") as f:
            await f.write(json.dumps(self.caption_history, indent=2))

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
