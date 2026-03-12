from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from enhanced_steam_bot.publisher import (
    process_image_for_instagram,
    upload_to_0x0,
    upload_to_catbox,
    upload_to_imgbb,
)


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test image hosting uploads for ImgBB, catbox.moe, and 0x0.st"
    )
    parser.add_argument(
        "source",
        help="Local image path or remote image URL",
    )
    args = parser.parse_args()

    load_dotenv()

    temp_file: Path | None = None
    source = args.source.strip()

    if _is_url(source):
        print("Preparing image from URL...")
        temp_file = await process_image_for_instagram(source)
        image_path = temp_file
    else:
        image_path = Path(source)
        if not image_path.exists():
            raise FileNotFoundError(f"File not found: {image_path}")

    print(f"Using image: {image_path}")
    print()

    try:
        imgbb_key = os.getenv("IMGBB_API_KEY", "").strip()
        if imgbb_key:
            try:
                print("Testing ImgBB...")
                url = await upload_to_imgbb(image_path, imgbb_key)
                print(f"ImgBB OK: {url}")
            except Exception as e:
                print(f"ImgBB FAILED: {e}")
        else:
            print("ImgBB SKIPPED: IMGBB_API_KEY not set")

        try:
            print("Testing catbox.moe...")
            url = await upload_to_catbox(image_path)
            print(f"catbox OK: {url}")
        except Exception as e:
            print(f"catbox FAILED: {e}")

        try:
            print("Testing 0x0.st...")
            url = await upload_to_0x0(image_path)
            print(f"0x0 OK: {url}")
        except Exception as e:
            print(f"0x0 FAILED: {e}")
    finally:
        if temp_file and temp_file.exists():
            temp_file.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
