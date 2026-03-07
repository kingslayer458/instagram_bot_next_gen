"""
Image processing (Instagram-ready) and multi-strategy Instagram publishing.
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid
from pathlib import Path
from typing import Optional

import aiofiles
import aiohttp
import structlog
from PIL import Image

from .config import Settings

logger = structlog.get_logger()

TEMP_DIR = Path("./temp")
TEMP_DIR.mkdir(exist_ok=True)


# ── Image processing ─────────────────────────────────────────────────────

async def process_image_for_instagram(image_url: str) -> Path:
    """
    Download, resize/crop to Instagram-safe aspect ratio, save as JPEG.
    Returns path to processed temp file.
    """
    logger.info("image.downloading")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    async with aiohttp.ClientSession() as session:
        async with session.get(image_url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Image download failed: {resp.status}")
            data = await resp.read()

    img = Image.open(io.BytesIO(data))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    w, h = img.size
    aspect = w / h
    logger.info("image.original", width=w, height=h, aspect=f"{aspect:.2f}")

    if aspect > 1.91:
        # Too wide → landscape 1.91:1
        tw, th = 1080, round(1080 / 1.91)
    elif aspect < 0.8:
        # Too tall → portrait 4:5
        tw, th = 1080, 1350
    else:
        tw = min(1080, w)
        th = round(tw / aspect)

    # Resize with high-quality resampling
    img = img.resize((tw, th), Image.LANCZOS)

    out_path = TEMP_DIR / f"processed_{uuid.uuid4().hex[:8]}.jpg"
    img.save(str(out_path), "JPEG", quality=85, progressive=True, optimize=True)
    logger.info("image.processed", width=tw, height=th, path=str(out_path))
    return out_path


# ── Image hosting services ───────────────────────────────────────────────

async def upload_to_imgbb(image_path: Path, api_key: str) -> str:
    """Upload to ImgBB, return direct URL."""
    logger.info("upload.imgbb")
    async with aiofiles.open(image_path, "rb") as f:
        raw = await f.read()

    import base64
    b64 = base64.b64encode(raw).decode()

    async with aiohttp.ClientSession() as session:
        data = aiohttp.FormData()
        data.add_field("image", b64)
        async with session.post(
            f"https://api.imgbb.com/1/upload?key={api_key}",
            data=data,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            result = await resp.json()
            if not result.get("success"):
                raise RuntimeError(f"ImgBB error: {result.get('error', {}).get('message', 'unknown')}")
            return result["data"]["url"]


async def upload_to_0x0(image_path: Path) -> str:
    """Upload to 0x0.st, return direct URL."""
    logger.info("upload.0x0")
    async with aiofiles.open(image_path, "rb") as f:
        raw = await f.read()

    async with aiohttp.ClientSession() as session:
        data = aiohttp.FormData()
        data.add_field("file", raw, filename="screenshot.jpg", content_type="image/jpeg")
        async with session.post("https://0x0.st", data=data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"0x0.st error: {resp.status}")
            url = (await resp.text()).strip()
            if not url.startswith("https://"):
                raise RuntimeError(f"Invalid 0x0.st response: {url}")
            return url


# ── Steam URL parameter tricks ───────────────────────────────────────────

def _steam_url_variants(original_url: str) -> list[str]:
    """Generate Instagram-compatible Steam CDN URL variants."""
    if "steamuserimages" not in original_url and "steamusercontent" not in original_url:
        return [original_url]

    base = original_url.split("?")[0]
    return [
        f"{base}?imw=1080&imh=1080&ima=fit&impolicy=Letterbox&imcolor=%23000000&letterbox=true",
        f"{base}?imw=1080&imh=565&ima=fit&impolicy=Letterbox&imcolor=%23000000&letterbox=true",
        f"{base}?imw=1080&imh=1350&ima=fit&impolicy=Letterbox&imcolor=%23000000&letterbox=true",
        f"{base}?imw=800&imh=800&ima=fit&impolicy=Letterbox&imcolor=%23000000&letterbox=true",
    ]


# ── Instagram Graph API publisher ────────────────────────────────────────

class InstagramPublisher:
    """Publishes images to Instagram via the Meta Graph API with multi-strategy fallback."""

    API_VERSION = "v21.0"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._base = f"https://graph.facebook.com/{self.API_VERSION}"

    async def publish(self, image_url: str, caption: str) -> str:
        """
        Try multiple upload strategies and return the Instagram post ID.
        Strategy order:
          1. Direct Steam URL variants
          2. Process locally → upload to image host → publish URL
          3. Original URL as last resort
        """
        # Validate token first
        await self._validate_token()

        # Strategy 1: Direct Steam URL tricks
        try:
            logger.info("publish.strategy1_steam_urls")
            post_id = await self._try_steam_variants(image_url, caption)
            logger.info("publish.strategy1_success", post_id=post_id)
            return post_id
        except Exception as e:
            logger.warning("publish.strategy1_failed", error=str(e))

        # Strategy 2: Process + host externally
        processed_path: Optional[Path] = None
        try:
            logger.info("publish.strategy2_process_and_host")
            processed_path = await process_image_for_instagram(image_url)
            hosted_url = await self._upload_to_host(processed_path)
            post_id = await self._create_and_publish(hosted_url, caption)
            logger.info("publish.strategy2_success", post_id=post_id)
            return post_id
        except Exception as e:
            logger.warning("publish.strategy2_failed", error=str(e))
        finally:
            if processed_path and processed_path.exists():
                processed_path.unlink(missing_ok=True)

        # Strategy 3: Original URL
        try:
            logger.info("publish.strategy3_original_url")
            post_id = await self._create_and_publish(image_url, caption)
            logger.info("publish.strategy3_success", post_id=post_id)
            return post_id
        except Exception as e:
            raise RuntimeError(f"All upload strategies failed. Last error: {e}")

    # ── Internal ─────────────────────────────────────────────────────────

    async def _validate_token(self) -> None:
        url = f"{self._base}/me?access_token={self.settings.instagram_access_token}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(f"Invalid token: {data['error']['message']}")

    async def _try_steam_variants(self, image_url: str, caption: str) -> str:
        variants = _steam_url_variants(image_url)
        for i, url in enumerate(variants):
            try:
                return await self._create_and_publish(url, caption)
            except Exception:
                continue
        raise RuntimeError("All Steam URL variants failed")

    async def _upload_to_host(self, path: Path) -> str:
        """Try available image hosting services."""
        if self.settings.imgbb_api_key:
            try:
                return await upload_to_imgbb(path, self.settings.imgbb_api_key)
            except Exception as e:
                logger.warning("upload.imgbb_failed", error=str(e))

        try:
            return await upload_to_0x0(path)
        except Exception as e:
            raise RuntimeError(f"All image hosts failed. Last: {e}")

    async def _create_and_publish(self, image_url: str, caption: str) -> str:
        """Two-step Instagram publish: create media container, then publish."""
        async with aiohttp.ClientSession() as session:
            # Step 1: Create media container
            create_url = f"{self._base}/{self.settings.instagram_page_id}/media"
            payload = {
                "image_url": image_url,
                "caption": caption,
                "access_token": self.settings.instagram_access_token,
            }
            async with session.post(create_url, json=payload) as resp:
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(f"Media create failed: {data['error']['message']}")
                creation_id = data["id"]

            # Step 2: Publish
            publish_url = f"{self._base}/{self.settings.instagram_page_id}/media_publish"
            payload = {
                "creation_id": creation_id,
                "access_token": self.settings.instagram_access_token,
            }
            async with session.post(publish_url, json=payload) as resp:
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(f"Publish failed: {data['error']['message']}")
                return data["id"]
