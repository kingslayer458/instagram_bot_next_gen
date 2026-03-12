"""
Async Steam Community screenshot scraper.
Extracts the highest-quality screenshots from public profiles.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import structlog

from .config import Settings

logger = structlog.get_logger()


# ── Webshare API ─────────────────────────────────────────────────────────

async def fetch_webshare_proxies(api_key: str) -> list[str]:
    """Fetch the full proxy list from the Webshare.io API.

    Returns a list of ``http://user:pass@host:port`` URLs ready for aiohttp-socks.
    Paginates automatically until all proxies are collected.
    """
    proxies: list[str] = []
    url: str | None = "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=1&page_size=100"
    headers = {"Authorization": f"Token {api_key}"}

    async with aiohttp.ClientSession() as session:
        while url:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Webshare API error {resp.status}: {text[:200]}")
                data = await resp.json()

            for entry in data.get("results", []):
                host = entry["proxy_address"]
                port = entry["port"]
                user = entry["username"]
                pwd = entry["password"]
                proxies.append(f"http://{user}:{pwd}@{host}:{port}")

            url = data.get("next")  # paginated — None when done

    logger.info("webshare.fetched", proxy_count=len(proxies))
    return proxies


# ── Proxy helpers ────────────────────────────────────────────────────────

def _proxy_kwargs(proxy_url: Optional[str]) -> dict:
    """Build aiohttp-compatible proxy kwargs with explicit auth extraction.

    aiohttp sometimes fails to extract credentials from the proxy URL,
    resulting in 407 Proxy Authentication Required.  This helper parses
    them out and returns ``{proxy: ..., proxy_auth: ...}`` ready to
    unpack into ``session.get(**_proxy_kwargs(url))``.
    """
    if not proxy_url:
        return {}
    m = re.match(r"https?://([^:]+):([^@]+)@(.+)", proxy_url)
    if m:
        user, pwd, host_port = m.group(1), m.group(2), m.group(3)
        scheme = "http://" if proxy_url.startswith("http://") else "https://"
        return {
            "proxy": f"{scheme}{host_port}",
            "proxy_auth": aiohttp.BasicAuth(user, pwd),
        }
    return {"proxy": proxy_url}


# ── Proxy rotator ────────────────────────────────────────────────────────

class ProxyRotator:
    """Thread-safe proxy rotator that cycles IPs on a time interval.

    - Each call to ``get()`` returns the current proxy URL.
    - After ``rotation_interval`` seconds the pointer advances to the next proxy.
    - Workers share a single rotator so the whole bot appears to change IP together.
    - If no proxies are configured, ``get()`` returns ``None`` (direct connection).
    """

    def __init__(self, proxy_urls: list[str], rotation_interval: float = 10.0):
        self._proxies: list[str] = list(proxy_urls)
        self._interval = rotation_interval
        self._index = 0
        self._last_rotate = time.monotonic()
        self._lock = asyncio.Lock()
        self._enabled = bool(self._proxies)

        if self._enabled:
            logger.info("proxy.init",
                        pool_size=len(self._proxies),
                        rotate_every=f"{self._interval}s")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def get(self) -> Optional[str]:
        """Return current proxy URL, rotating if interval elapsed."""
        if not self._enabled:
            return None

        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_rotate
            if elapsed >= self._interval:
                steps = int(elapsed // self._interval)
                old_idx = self._index
                self._index = (self._index + steps) % len(self._proxies)
                self._last_rotate = now
                if self._index != old_idx:
                    logger.info("proxy.rotated",
                                from_idx=old_idx, to_idx=self._index,
                                proxy=self._mask(self._proxies[self._index]),
                                elapsed=f"{elapsed:.1f}s")
            return self._proxies[self._index]

    async def mark_bad(self, proxy_url: str) -> None:
        """Mark a proxy as bad — rotate away from it immediately."""
        if not self._enabled:
            return
        async with self._lock:
            if self._proxies[self._index] == proxy_url and len(self._proxies) > 1:
                old = self._index
                self._index = (self._index + 1) % len(self._proxies)
                self._last_rotate = time.monotonic()
                logger.warning("proxy.marked_bad",
                               from_idx=old, to_idx=self._index,
                               bad_proxy=self._mask(proxy_url))

    def status(self) -> dict:
        """Return proxy pool status for health endpoint."""
        if not self._enabled:
            return {"enabled": False}
        return {
            "enabled": True,
            "pool_size": len(self._proxies),
            # "current_index": self._index,
            # "current_proxy": self._mask(self._proxies[self._index]),
            "rotate_interval": self._interval,
        }

    @staticmethod
    def _mask(url: str) -> str:
        """Mask credentials in proxy URL for safe logging."""
        # http://user:pass@host:port → http://***@host:port
        m = re.match(r"(https?://)([^@]+)@(.+)", url)
        if m:
            return f"{m.group(1)}***@{m.group(3)}"
        return url

# ── Data models ──────────────────────────────────────────────────────────

@dataclass
class Screenshot:
    page_url: str
    image_url: str
    quality_estimate: str = "Standard Quality"
    title: Optional[str] = None
    game_name: Optional[str] = None
    steam_user: Optional[str] = None
    extracted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    original_url: Optional[str] = None
    score: float = 0.0

    # NEW: vision-enriched fields
    mood: Optional[str] = None
    dominant_colors: Optional[list[str]] = None
    scene_description: Optional[str] = None


# ── Quality scoring weights ──────────────────────────────────────────────

QUALITY_WEIGHTS = {
    "Ultra High Quality": 15,
    "Very High Quality": 12,
    "High Quality": 8,
    "Standard Quality": 4,
}

POPULAR_GAMES = {
    "cyberpunk", "witcher", "gta", "skyrim", "fallout", "destiny",
    "minecraft", "rdr2", "valorant", "csgo", "apex", "overwatch",
    "cod", "fortnite", "wow", "lol", "dota", "assassin", "horizon",
    "god of war", "spider", "halo", "gears", "far cry", "watch dogs",
    "tomb raider", "final fantasy", "dark souls", "elden ring", "sekiro",
    "bloodborne", "baldur", "starfield", "palworld", "lethal company",
    "helldivers", "wuthering", "zenless", "black myth",
}


def score_screenshot(ss: Screenshot) -> float:
    """Score a screenshot for posting priority."""
    score = 10.0
    score += QUALITY_WEIGHTS.get(ss.quality_estimate, 4)

    if ss.game_name:
        score += 5
        name_lower = ss.game_name.lower()
        if any(g in name_lower for g in POPULAR_GAMES):
            score += 10

    if ss.title and len(ss.title) > 5:
        score += 3

    try:
        hours_old = (datetime.now(timezone.utc) - datetime.fromisoformat(ss.extracted_at)).total_seconds() / 3600
        if hours_old < 24:
            score += 5
    except Exception:
        pass

    return score


# ── Headers ──────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

# ── User-Agent pool for parallel workers (each worker gets a unique identity) ─

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
]


def _make_worker_headers(worker_id: int) -> dict[str, str]:
    """Create unique headers for a parallel worker."""
    ua = _USER_AGENTS[worker_id % len(_USER_AGENTS)]
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Referer": "https://steamcommunity.com/",
    }

# ── View types to maximise coverage ──────────────────────────────────────

VIEW_TYPES = [
    "",
    "?tab=all",
    "?tab=public",
    "?appid=0",
    "?p=1&sort=newestfirst",
    "?p=1&sort=oldestfirst",
    "?p=1&sort=mostrecent",
    "?p=1&view=grid",
    "?p=1&view=list",
    "?p=1&appid=0&sort=newestfirst",
    "?p=1&appid=0&sort=oldestfirst",
    "?p=1&browsefilter=myfiles",
]

# ── Regex patterns for screenshot-page extraction ────────────────────────

PAGE_URL_PATTERNS = [
    re.compile(r'href="((?:https://steamcommunity\.com)?/sharedfiles/filedetails/\?id=\d+)"'),
    re.compile(r"href='((?:https://steamcommunity\.com)?/sharedfiles/filedetails/\?id=\d+)'"),
    re.compile(r'SharedFileBindMouseHover\(\s*"(\d+)"'),
    re.compile(r'src="https://steamuserimages[^"]+/([0-9a-f]+)/"'),
    re.compile(r'data-screenshot-id="(\d+)"'),
    re.compile(r"onclick=\"ViewScreenshot\('(\d+)'\)\""),
    re.compile(r"ShowModalContent\( 'shared_file_(\d+)'"),
]

# ── Image extraction methods (ordered by quality) ────────────────────────

IMAGE_EXTRACTORS: list[tuple[str, re.Pattern]] = [
    ("ActualMedia_link", re.compile(r'<a[^>]+href="([^"]+)"[^>]*>\s*<img[^>]+id="ActualMedia"')),
    ("ActualMedia", re.compile(r'<img[^>]+id="ActualMedia"[^>]+src="([^"]+)"')),
    ("og:image", re.compile(r'<meta property="og:image" content="([^"]+)">')),
    ("image_src", re.compile(r'<link rel="image_src" href="([^"]+)">')),
    ("ScreenshotImage", re.compile(r'ScreenshotImage[^"]+"([^"]+)"')),
    ("detailsImage", re.compile(r'<img[^>]+class="screenshotDetailsImage"[^>]+src="([^"]+)"')),
    ("steamusercontent", re.compile(r'src="(https://images\.steamusercontent\.com/ugc/[^"]+)"')),
    ("steamuserimages", re.compile(r'src="(https://steamuserimages[^"]+\.jpg[^"]*)"')),
    ("any_img", re.compile(r'<img[^>]+src="(https://[^"]+\.(?:jpg|png|jpeg))[^"]*"', re.IGNORECASE)),
]

PRIVACY_MARKERS = [
    "The specified profile is private",
    "This profile is private",
    "The specified profile could not be found",
    "This user has not yet set up their Steam Community profile",
    "profile is set to private",
    "No screenshots",
]


class SteamScraper:
    """Async Steam Community screenshot scraper with aggressive caching."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: dict[str, tuple[float, list[Screenshot]]] = {}  # steam_id -> (ts, screenshots)
        self._cache_ttl = 3600  # 1 hour
        # Proxy rotator is set up lazily via _ensure_proxies()
        self._proxy = ProxyRotator([], 10.0)
        self._proxies_initialised = False

    async def _ensure_proxies(self) -> None:
        """Lazy one-shot proxy init: Webshare API key takes priority, then env vars."""
        if self._proxies_initialised:
            return
        self._proxies_initialised = True

        if not self.settings.proxy_enabled:
            return

        proxy_urls: list[str] = []

        # Prefer Webshare API if a key is configured
        if self.settings.webshare_api_key:
            try:
                proxy_urls = await fetch_webshare_proxies(self.settings.webshare_api_key)
            except Exception as e:
                logger.error("webshare.fetch_failed", error=str(e))

        # Fall back to static env-var proxies if Webshare returned nothing
        if not proxy_urls and self.settings.proxy_urls:
            logger.info("proxy.fallback_to_env_vars", count=len(self.settings.proxy_urls))
            proxy_urls = self.settings.proxy_urls

        if proxy_urls:
            self._proxy = ProxyRotator(proxy_urls, self.settings.proxy_rotation_interval)
        else:
            logger.warning("proxy.none_available")

    # ── Public API ───────────────────────────────────────────────────────

    async def fetch_all_users(self, posted: set[str]) -> list[Screenshot]:
        """Fetch screenshots from all configured Steam users, return sorted by score."""
        await self._ensure_proxies()
        all_screenshots: list[Screenshot] = []

        for steam_id in self.settings.steam_user_ids:
            screenshots = await self.fetch_user_screenshots(steam_id, posted)
            all_screenshots.extend(screenshots)
            if steam_id != self.settings.steam_user_ids[-1]:
                logger.info("steam.user_delay", seconds=self.settings.steam_user_delay)
                await asyncio.sleep(self.settings.steam_user_delay)

        all_screenshots.sort(key=lambda s: s.score, reverse=True)
        logger.info("steam.total_fetched", count=len(all_screenshots))
        return all_screenshots

    async def fetch_user_screenshots(self, steam_id: str, posted: set[str]) -> list[Screenshot]:
        """Fetch all unposted screenshots for a single Steam user."""
        # Check cache
        if steam_id in self._cache:
            ts, cached = self._cache[steam_id]
            if time.time() - ts < self._cache_ttl:
                filtered = [s for s in cached if s.page_url not in posted]
                logger.info("steam.cache_hit", steam_id=steam_id, count=len(filtered))
                return filtered

        logger.info("steam.fetching", steam_id=steam_id)
        page_urls = await self._collect_page_urls(steam_id, posted)
        logger.info("steam.pages_found", steam_id=steam_id, count=len(page_urls))

        screenshots = await self._extract_screenshots(steam_id, page_urls)
        for ss in screenshots:
            ss.score = score_screenshot(ss)

        screenshots.sort(key=lambda s: s.score, reverse=True)
        self._cache[steam_id] = (time.time(), screenshots)
        logger.info("steam.user_done", steam_id=steam_id, count=len(screenshots))
        return [s for s in screenshots if s.page_url not in posted]

    # ── Internal: collect page URLs ──────────────────────────────────────

    async def _collect_page_urls(self, steam_id: str, posted: set[str]) -> set[str]:
        urls: set[str] = set()
        base = f"https://steamcommunity.com/profiles/{steam_id}/screenshots"

        async with aiohttp.ClientSession(headers=HEADERS) as session:
            # Initial profile check
            try:
                proxy = await self._proxy.get()
                async with session.get(base, **_proxy_kwargs(proxy)) as resp:
                    if resp.status != 200:
                        logger.warning("steam.profile_error", steam_id=steam_id, status=resp.status)
                        return urls
                    html = await resp.text()
            except Exception as e:
                logger.error("steam.profile_fetch_failed", steam_id=steam_id, error=str(e))
                return urls

            if any(marker in html for marker in PRIVACY_MARKERS):
                logger.warning("steam.profile_private", steam_id=steam_id)
                return urls

            # Estimate total
            total = self._estimate_total(html)
            max_page = max(6, (total // 30) + 10)
            logger.info("steam.estimated", steam_id=steam_id, total=total, max_page=max_page)

            # Crawl each view type
            for vt_idx, vt in enumerate(VIEW_TYPES, 1):
                logger.info("steam.crawl_view", view=vt_idx, total_views=len(VIEW_TYPES), filter=vt or "default", urls_so_far=len(urls))
                empty_streak = 0
                for page in range(1, max_page + 1):
                    sep = "&" if "?" in vt else "?"
                    page_url = f"{base}{vt}{sep}p={page}"

                    try:
                        proxy = await self._proxy.get()
                        async with session.get(page_url, **_proxy_kwargs(proxy)) as resp:
                            if resp.status == 429 or resp.status == 403:
                                logger.warning("steam.rate_limited", status=resp.status)
                                if proxy:
                                    await self._proxy.mark_bad(proxy)
                                await asyncio.sleep(30 * 60)
                                continue
                            if resp.status != 200:
                                continue
                            html = await resp.text()
                    except aiohttp.ClientProxyConnectionError as e:
                        logger.warning("steam.proxy_error", proxy=ProxyRotator._mask(proxy) if proxy else "direct", error=str(e))
                        if proxy:
                            await self._proxy.mark_bad(proxy)
                        continue
                    except Exception as e:
                        logger.error("steam.page_error", url=page_url, error=str(e))
                        continue

                    new_count = self._extract_page_urls(html, urls, posted)
                    logger.info("steam.page_scraped", page=page, new_urls=new_count, total_urls=len(urls))
                    if new_count == 0:
                        empty_streak += 1
                        if empty_streak >= 3:
                            logger.info("steam.view_done", reason="3 empty pages", filter=vt or "default")
                            break
                    else:
                        empty_streak = 0

                    jitter = random.uniform(0, self.settings.steam_page_delay * 0.5)
                    await asyncio.sleep(self.settings.steam_page_delay + jitter)

        return urls

    def _extract_page_urls(self, html: str, urls: set[str], posted: set[str]) -> int:
        """Extract screenshot page URLs from an HTML page, return count of new URLs found."""
        new_count = 0

        # Standard URL patterns (first 2)
        for pattern in PAGE_URL_PATTERNS[:2]:
            for match in pattern.finditer(html):
                url = match.group(1)
                if url.startswith("/"):
                    url = f"https://steamcommunity.com{url}"
                if url not in urls and url not in posted:
                    urls.add(url)
                    new_count += 1

        # ID-based patterns
        for pattern in PAGE_URL_PATTERNS[2:]:
            for match in pattern.finditer(html):
                sid = match.group(1)
                url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={sid}"
                if url not in urls and url not in posted:
                    urls.add(url)
                    new_count += 1

        return new_count

    def _estimate_total(self, html: str) -> int:
        patterns = [
            re.compile(r"(\d+) screenshots", re.IGNORECASE),
            re.compile(r"Screenshots \((\d+)\)", re.IGNORECASE),
            re.compile(r"Showing (\d+) screenshots", re.IGNORECASE),
        ]
        for p in patterns:
            m = p.search(html)
            if m:
                return int(m.group(1))

        thumb_count = len(re.findall(r'<div class="imageWallRow">', html))
        return max(thumb_count * 10, 1000) if thumb_count else 1000

    # ── Internal: extract image details (parallel workers) ────────────────

    async def _extract_screenshots(self, steam_id: str, page_urls: set[str]) -> list[Screenshot]:
        """Fetch screenshot details using parallel workers.

        Each worker runs an independent aiohttp session with a unique User-Agent.
        Simple per-worker delay with jitter to avoid synchronized bursts.
        """
        urls_list = list(page_urls)
        if not urls_list:
            return []

        num_workers = min(self.settings.parallel_workers, len(urls_list))
        total = len(urls_list)
        delay = float(self.settings.steam_detail_delay)

        screenshots: list[Screenshot] = []
        results_lock = asyncio.Lock()
        completed = 0

        logger.info("steam.parallel_start", total_urls=total, workers=num_workers,
                    delay=delay)

        async def _worker(worker_id: int, url_queue: asyncio.Queue) -> None:
            """Long-lived worker coroutine — pulls URLs from the queue."""
            nonlocal completed
            headers = _make_worker_headers(worker_id)

            async with aiohttp.ClientSession(headers=headers) as session:
                while True:
                    try:
                        url = url_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return

                    # Per-worker staggered delay (jitter avoids synchronized bursts)
                    jitter = random.uniform(0, delay * 0.4)
                    await asyncio.sleep(delay * 0.3 + jitter + worker_id * 0.5)

                    # Get current proxy (rotates automatically every N seconds)
                    proxy = await self._proxy.get()

                    # Fetch
                    ss = await self._fetch_detail(session, url, steam_id, worker_id, proxy)

                    # Record result
                    async with results_lock:
                        completed += 1
                        if ss:
                            screenshots.append(ss)
                            logger.info("steam.detail_ok",
                                        worker=worker_id,
                                        progress=f"{completed}/{total}",
                                        game=ss.game_name or "Unknown",
                                        quality=ss.quality_estimate,
                                        total_extracted=len(screenshots))
                        elif completed % 20 == 0 or completed == total:
                            logger.info("steam.progress",
                                        completed=completed, total=total,
                                        extracted=len(screenshots))

                    url_queue.task_done()

        # Fill the work queue
        url_queue: asyncio.Queue[str] = asyncio.Queue()
        for url in urls_list:
            url_queue.put_nowait(url)

        # Launch persistent workers
        logger.info("steam.workers_launching", workers=num_workers, queue_size=url_queue.qsize())
        worker_tasks = [
            asyncio.create_task(_worker(wid, url_queue))
            for wid in range(num_workers)
        ]
        await asyncio.gather(*worker_tasks, return_exceptions=True)

        logger.info("steam.parallel_done", extracted=len(screenshots), total_urls=total)
        return screenshots

    async def _fetch_detail(
        self,
        session: aiohttp.ClientSession,
        url: str,
        steam_id: str,
        worker_id: int,
        proxy: Optional[str] = None,
    ) -> Optional[Screenshot]:
        """Fetch a single screenshot detail page with simple retry on 429."""
        for attempt in range(self.settings.max_retries):
            try:
                current_proxy = proxy or await self._proxy.get()
                async with session.get(url, **_proxy_kwargs(current_proxy),
                                       timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 429 or resp.status == 403:
                        wait = 30 + random.uniform(5, 15) * (attempt + 1)
                        logger.warning("steam.worker_429", worker=worker_id,
                                       wait=f"{wait:.0f}s", attempt=attempt + 1,
                                       proxy=ProxyRotator._mask(current_proxy) if current_proxy else "direct")
                        # Rotate away from the blocked proxy
                        if current_proxy:
                            await self._proxy.mark_bad(current_proxy)
                            proxy = await self._proxy.get()  # use new proxy on retry
                        await asyncio.sleep(wait)
                        continue
                    if resp.status != 200:
                        return None
                    html = await resp.text()
                break
            except aiohttp.ClientProxyConnectionError as e:
                logger.warning("steam.proxy_conn_error", worker=worker_id,
                               proxy=ProxyRotator._mask(current_proxy) if current_proxy else "direct",
                               error=str(e), attempt=attempt + 1)
                if current_proxy:
                    await self._proxy.mark_bad(current_proxy)
                    proxy = await self._proxy.get()
                await asyncio.sleep(2 ** attempt)
                continue
            except asyncio.TimeoutError:
                logger.warning("steam.worker_timeout", worker=worker_id,
                               url=url[:80], attempt=attempt + 1)
                await asyncio.sleep(2 ** attempt)
                continue
            except Exception as e:
                logger.error("steam.detail_error", worker=worker_id,
                             url=url[:80], error=str(e))
                return None
        else:
            return None  # all retries exhausted

        image_url = self._extract_image_url(html)
        if not image_url:
            return None

        # Decode HTML entities (Steam uses &amp; in meta tags)
        image_url = html_mod.unescape(image_url)

        # Force max quality for Steam CDN (both old and new domains)
        if "steamuserimages" in image_url or "steamusercontent" in image_url:
            base = image_url.split("?")[0]
            image_url = f"{base}?imw=5000&imh=5000&ima=fit&impolicy=Letterbox&imcolor=%23000000&letterbox=false"

        quality = self._classify_quality(image_url)

        # Game name: try new apphub_AppName first, then legacy screenshotAppName
        game_m = (re.search(r'class="apphub_AppName[^"]*">([^<]+)<', html)
                  or re.search(r'<div class="screenshotAppName">([^<]+)</div>', html))
        # Title: try screenshotName first, then og:title
        title_m = (re.search(r'<div class="screenshotName">([^<]+)</div>', html)
                   or re.search(r'<meta property="og:title" content="[^"]*::\s*([^"]+)"', html))

        return Screenshot(
            page_url=url,
            image_url=image_url,
            quality_estimate=quality,
            title=title_m.group(1).strip() if title_m else None,
            game_name=game_m.group(1).strip() if game_m else None,
            steam_user=steam_id,
            original_url=image_url,
        )

    def _extract_image_url(self, html: str) -> Optional[str]:
        for name, pattern in IMAGE_EXTRACTORS:
            m = pattern.search(html)
            if m:
                url = m.group(1)
                if name in ("ActualMedia", "ActualMedia_link") and "?" not in url:
                    url = f"{url}?imw=5000&imh=5000&ima=fit&impolicy=Letterbox&imcolor=%23000000&letterbox=false"
                if name == "detailsImage":
                    url = url.split("?")[0]
                return url
        return None

    @staticmethod
    def _classify_quality(url: str) -> str:
        if any(k in url for k in ("original", "imw=5000", "5000", "3840x2160")):
            return "Ultra High Quality"
        if "2560x1440" in url:
            return "Very High Quality"
        if any(k in url for k in ("1920x1080", "imw=1024")):
            return "High Quality"
        return "Standard Quality"

    def clear_cache(self) -> None:
        self._cache.clear()
        logger.info("steam.cache_cleared")
