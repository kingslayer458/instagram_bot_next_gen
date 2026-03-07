"""
AI Caption Engine – multi-provider, vision-first, with candidate ranking.

Enhancement over the JS version:
  1. Generates N caption candidates and picks the best via a scoring pass.
  2. Vision analysis extracts mood, dominant colors, and scene description.
  3. Mood-aware hashtag selection.
  4. Structured output parsing (no fragile regex on AI responses).
  5. Full async with proper timeout handling.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import random
import re
from datetime import datetime
from typing import Optional

import aiohttp
import structlog
from PIL import Image

from .config import AIProvider, CaptionVariety, Settings
from .scraper import Screenshot

logger = structlog.get_logger()

# ── Daily themes ─────────────────────────────────────────────────────────

DAILY_THEMES = {
    0: {"name": "Sunday Showcase", "hashtags": ["#sundayshowcase", "#bestshots", "#weekendvibes"]},
    1: {"name": "Modded Monday", "hashtags": ["#moddedmonday", "#gamemod", "#community"]},
    2: {"name": "Texture Tuesday", "hashtags": ["#texturetuesday", "#graphics", "#visualfeast"]},
    3: {"name": "Wildlife Wednesday", "hashtags": ["#wildlifewednesday", "#naturegaming", "#exploration"]},
    4: {"name": "Throwback Thursday", "hashtags": ["#throwbackthursday", "#retrogaming", "#nostalgia"]},
    5: {"name": "Featured Friday", "hashtags": ["#featuredfriday", "#community", "#highlight"]},
    6: {"name": "Screenshot Saturday", "hashtags": ["#screenshotsaturday", "#photomode", "#creative"]},
}

# ── Game → hashtag mapping ───────────────────────────────────────────────

GAME_HASHTAGS: dict[str, list[str]] = {
    "cyberpunk": ["#cyberpunk2077", "#nightcity", "#futuristic", "#neon"],
    "witcher": ["#thewitcher3", "#geralt", "#fantasy", "#magic"],
    "gta": ["#gtav", "#grandtheftauto", "#rockstargames", "#openworld"],
    "skyrim": ["#skyrim", "#elderscrolls", "#dragonborn", "#fantasy"],
    "fallout": ["#fallout4", "#wasteland", "#postapocalyptic", "#survival"],
    "destiny": ["#destiny2", "#guardian", "#bungie", "#space"],
    "minecraft": ["#minecraft", "#minecraftbuilds", "#pixelart", "#creative"],
    "rdr2": ["#reddeadredemption2", "#rdr2", "#western", "#outlaw"],
    "valorant": ["#valorant", "#riotgames", "#tactical", "#esports"],
    "elden ring": ["#eldenring", "#fromsoftware", "#souls", "#openworld"],
    "baldur": ["#baldursgate3", "#larian", "#dnd", "#rpg"],
    "starfield": ["#starfield", "#bethesda", "#space", "#exploration"],
    "black myth": ["#blackmythwukong", "#wukong", "#actionrpg"],
    "helldivers": ["#helldivers2", "#coop", "#action"],
    "default": ["#steam", "#gaming", "#pcgaming", "#screenshot", "#gamer", "#videogames"],
}

VARIETY_HASHTAGS = [
    "#steamcommunity", "#pcmasterrace", "#videogames", "#gamedev",
    "#indiegaming", "#gameart", "#photomode", "#gamephotography",
    "#virtualphoto", "#digitalart", "#epicshot", "#gamingmoments",
    "#virtualphotography", "#gameaesthetics", "#gamingscreenshots",
]


# ── Vision analysis result ───────────────────────────────────────────────

class VisionAnalysis:
    """Structured result from vision model analysis."""

    def __init__(self, mood: str = "", colors: list[str] | None = None,
                 scene: str = "", suggested_hashtags: list[str] | None = None,
                 caption_candidates: list[str] | None = None):
        self.mood = mood
        self.colors = colors or []
        self.scene = scene
        self.suggested_hashtags = suggested_hashtags or []
        self.caption_candidates = caption_candidates or []


# ── Main engine ──────────────────────────────────────────────────────────

class CaptionEngine:
    """Multi-provider AI caption generator with vision analysis & candidate ranking."""

    def __init__(self, settings: Settings, overused_patterns: list[str] | None = None):
        self.settings = settings
        self.overused_patterns = overused_patterns or []

    # ── Public API ───────────────────────────────────────────────────────

    async def generate(self, screenshot: Screenshot) -> tuple[str, list[str]]:
        """
        Returns (caption_text, hashtag_list).
        Tries vision → text AI → static fallback chain.
        """
        caption = ""
        vision: Optional[VisionAnalysis] = None

        # 1️⃣  Vision analysis (Gemini only for multimodal)
        if self.settings.enable_vision_analysis and self.settings.gemini_api_key:
            try:
                vision = await self._vision_analyze(screenshot)
                screenshot.mood = vision.mood
                screenshot.dominant_colors = vision.colors
                screenshot.scene_description = vision.scene
            except Exception as e:
                logger.warning("caption.vision_failed", error=str(e))

        # 2️⃣  Caption generation
        if vision and vision.caption_candidates:
            caption = await self._pick_best_candidate(vision.caption_candidates, screenshot)
        elif self.settings.enable_ai_captions:
            try:
                caption = await self._generate_text_ai_caption(screenshot, vision)
            except Exception as e:
                logger.warning("caption.ai_failed", error=str(e))

        # 3️⃣  Fallback
        if not caption and self.settings.fallback_to_static:
            caption = self._generate_static_caption(screenshot)
        elif not caption:
            caption = "🎮 Amazing gaming moment captured! Follow for more screenshots ✨"

        # 4️⃣  Hashtags
        hashtags = self._build_hashtags(screenshot, vision)

        return caption, hashtags

    # ── Vision analysis ──────────────────────────────────────────────────

    async def _vision_analyze(self, screenshot: Screenshot) -> VisionAnalysis:
        """Call Gemini Vision to analyze the screenshot image."""
        logger.info("caption.vision_start")
        image_b64 = await self._download_and_encode(screenshot.image_url)

        today = datetime.now().weekday()
        theme = DAILY_THEMES.get(today, DAILY_THEMES[6])
        avoid = ", ".join(self.overused_patterns[:10]) if self.overused_patterns else "none"

        prompt = f"""Analyze this gaming screenshot and return a JSON object with these fields:
{{
  "mood": "one-word mood (e.g., epic, serene, intense, mysterious, melancholic, vibrant)",
  "colors": ["top 3 dominant color names"],
  "scene": "1 sentence describing what is visible in the image",
  "suggested_hashtags": ["3-5 hashtags that match the visual content, without the # symbol"],
  "captions": [
    "caption option 1 (150-200 chars, vivid, with 2-3 emojis, no hashtags)",
    "caption option 2 (different style from option 1)",
    "caption option 3 (different style from options 1 and 2)"
  ]
}}

Context:
- Game: {screenshot.game_name or 'Unknown'}
- Quality: {screenshot.quality_estimate}
- Daily theme: {theme['name']}
- Title: {screenshot.title or 'None'}
- Avoid these overused patterns: {avoid}

Caption style variations to use across the 3 options:
1. Artistic/aesthetic (focus on composition and beauty)
2. Atmospheric/emotional (mood and feeling)
3. Exciting/action (energy and hype)

Return ONLY the JSON, no markdown fences, no preamble."""

        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
                ],
            }],
            "generationConfig": {
                "temperature": 0.9,
                "topK": 40,
                "topP": 0.95,
                "maxOutputTokens": 1500,
            },
        }

        model = self.settings.ai_model or "gemini-2.5-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.settings.gemini_api_key}"

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Gemini Vision {resp.status}: {body[:200]}")
                data = await resp.json()

        raw = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        logger.debug("caption.vision_raw", length=len(raw), first_100=raw[:100], last_100=raw[-100:])
        return self._parse_vision_response(raw)

    def _parse_vision_response(self, raw: str) -> VisionAnalysis:
        """Parse the structured JSON from Gemini Vision."""
        # Strip markdown fences if present
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")
        # Also strip any leading/trailing text before/after the JSON
        # Find the first { and last }
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            cleaned = cleaned[start:end + 1]
        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning("caption.vision_parse_failed",
                          error=str(e),
                          raw_length=len(raw),
                          cleaned_length=len(cleaned),
                          raw_start=raw[:100],
                          raw_end=raw[-100:] if len(raw) > 100 else raw)
            return VisionAnalysis()

        return VisionAnalysis(
            mood=obj.get("mood", ""),
            colors=obj.get("colors", []),
            scene=obj.get("scene", ""),
            suggested_hashtags=obj.get("suggested_hashtags", []),
            caption_candidates=obj.get("captions", []),
        )

    # ── Candidate ranking ────────────────────────────────────────────────

    async def _pick_best_candidate(self, candidates: list[str], screenshot: Screenshot) -> str:
        """
        NEW: Score caption candidates and pick the best one.
        Uses a lightweight AI call if scoring is enabled, otherwise picks randomly.
        """
        if not candidates:
            return ""

        # Clean all candidates
        cleaned = []
        for c in candidates:
            c = c.strip()
            c = re.sub(r"#\w+", "", c).strip()  # remove hashtags
            if len(c) > 200:
                c = c[:197] + "..."
            if c:
                cleaned.append(c)

        if not cleaned:
            return ""

        if not self.settings.enable_caption_scoring or len(cleaned) == 1:
            return random.choice(cleaned)

        # Lightweight scoring via AI
        try:
            return await self._score_candidates_with_ai(cleaned, screenshot)
        except Exception as e:
            logger.warning("caption.scoring_failed", error=str(e))
            return random.choice(cleaned)

    async def _score_candidates_with_ai(self, candidates: list[str], screenshot: Screenshot) -> str:
        """Ask the AI to pick the best caption from candidates."""
        numbered = "\n".join(f"{i+1}. {c}" for i, c in enumerate(candidates))
        prompt = f"""Pick the single BEST Instagram caption for a {screenshot.game_name or 'gaming'} screenshot.
Criteria: engaging, natural, unique, drives comments.

Candidates:
{numbered}

Reply with ONLY the number (e.g., "2"). Nothing else."""

        try:
            result = await self._call_ai_text(prompt, max_tokens=10, temperature=0.3)
            result = result.strip()
            # Extract first digit
            m = re.search(r"(\d+)", result)
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(candidates):
                    logger.info("caption.scored", winner=idx + 1, total=len(candidates))
                    return candidates[idx]
        except Exception:
            pass

        return random.choice(candidates)

    # ── Text-only AI caption ─────────────────────────────────────────────

    async def _generate_text_ai_caption(self, screenshot: Screenshot, vision: Optional[VisionAnalysis] = None) -> str:
        today = datetime.now().weekday()
        theme = DAILY_THEMES.get(today, DAILY_THEMES[6])

        vision_context = ""
        if vision:
            vision_context = f"""
Visual analysis:
- Mood: {vision.mood}
- Dominant colors: {', '.join(vision.colors)}
- Scene: {vision.scene}
"""

        prompt = f"""Create an engaging Instagram caption for a gaming screenshot.

Game: {screenshot.game_name or 'Unknown'}
Title: {screenshot.title or 'N/A'}
Quality: {screenshot.quality_estimate}
Theme: {theme['name']}
{vision_context}
Requirements:
- 1-3 sentences, max 200 characters
- Vivid and specific
- 2-3 emojis
- End with a question or call-to-action
- NO hashtags
- Sound natural and authentic

Write the caption now:"""

        result = await self._call_ai_text(prompt, max_tokens=200, temperature=0.85)
        result = re.sub(r"#\w+", "", result).strip()
        if len(result) > 200:
            result = result[:197] + "..."
        return result

    # ── Multi-provider AI text call ──────────────────────────────────────

    async def _call_ai_text(self, prompt: str, max_tokens: int = 200, temperature: float = 0.8) -> str:
        """Route to the configured AI provider."""
        provider = self.settings.ai_provider

        # Try primary, then fallback chain
        providers_to_try = [provider]
        for p in [AIProvider.GEMINI, AIProvider.OPENAI, AIProvider.ANTHROPIC]:
            if p != provider and self._has_key(p):
                providers_to_try.append(p)

        for p in providers_to_try:
            try:
                return await self._call_provider(p, prompt, max_tokens, temperature)
            except Exception as e:
                logger.warning("caption.provider_failed", provider=p.value, error=str(e))

        raise RuntimeError("All AI providers failed")

    def _has_key(self, provider: AIProvider) -> bool:
        return bool({
            AIProvider.GEMINI: self.settings.gemini_api_key,
            AIProvider.OPENAI: self.settings.openai_api_key,
            AIProvider.ANTHROPIC: self.settings.anthropic_api_key,
        }.get(provider))

    async def _call_provider(self, provider: AIProvider, prompt: str, max_tokens: int, temperature: float) -> str:
        if provider == AIProvider.GEMINI:
            return await self._call_gemini_text(prompt, max_tokens, temperature)
        elif provider == AIProvider.OPENAI:
            return await self._call_openai(prompt, max_tokens, temperature)
        elif provider == AIProvider.ANTHROPIC:
            return await self._call_anthropic(prompt, max_tokens, temperature)
        raise ValueError(f"Unknown provider: {provider}")

    async def _call_gemini_text(self, prompt: str, max_tokens: int, temperature: float) -> str:
        model = self.settings.ai_model or "gemini-2.5-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.settings.gemini_api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Gemini {resp.status}")
                data = await resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    async def _call_openai(self, prompt: str, max_tokens: int, temperature: float) -> str:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.settings.ai_model if "gpt" in (self.settings.ai_model or "") else "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "You are a gaming social media expert. Write engaging Instagram captions."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"OpenAI {resp.status}")
                data = await resp.json()
        return data["choices"][0]["message"]["content"]

    async def _call_anthropic(self, prompt: str, max_tokens: int, temperature: float) -> str:
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self.settings.anthropic_api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": self.settings.ai_model if "claude" in (self.settings.ai_model or "") else "claude-haiku-4-5-20251001",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Anthropic {resp.status}")
                data = await resp.json()
        return data["content"][0]["text"]

    # ── Static fallback ──────────────────────────────────────────────────

    def _generate_static_caption(self, screenshot: Screenshot) -> str:
        today = datetime.now().weekday()
        theme = DAILY_THEMES.get(today, DAILY_THEMES[6])
        game = screenshot.game_name or "this game"

        templates = [
            f"🎮 {theme['name']} featuring this stunning {game} moment! ✨",
            f"When {game} delivers visuals like this... pure art! 🎨 What's your favorite screenshot?",
            f"📸 Caught this perfect {game} scene! The atmosphere is captivating 🌟",
            f"✨ {theme['name']} brings you this breathtaking {game} vista!",
            f"🌅 Sometimes you just have to stop and appreciate the artistry in {game}",
            f"The lighting in this {game} shot is phenomenal! 💫 Screenshot goals!",
            f"🎯 {theme['name']} highlight: When {game} creates moments this beautiful!",
            f"The mood in this {game} screenshot hits different... 🌙",
            f"⚡ Epic {game} moment captured at just the right second!",
            f"🔥 Peak {game} excitement right here! These moments are why we game!",
        ]
        return random.choice(templates)

    # ── Hashtag generation ───────────────────────────────────────────────

    def _build_hashtags(self, screenshot: Screenshot, vision: Optional[VisionAnalysis] = None, max_tags: int = 30) -> list[str]:
        tags: set[str] = set()

        # Base tags
        for t in GAME_HASHTAGS["default"]:
            tags.add(t)

        # Game-specific
        if screenshot.game_name:
            name_lower = screenshot.game_name.lower()
            for key, game_tags in GAME_HASHTAGS.items():
                if key != "default" and key in name_lower:
                    for t in game_tags[:5]:
                        tags.add(t)
                    break

        # Daily theme
        today = datetime.now().weekday()
        theme = DAILY_THEMES.get(today, DAILY_THEMES[6])
        for t in theme["hashtags"]:
            tags.add(t)

        # Quality tags
        q = screenshot.quality_estimate
        if "Ultra" in q:
            tags.update(["#4k", "#ultrahd", "#maxsettings"])
        elif "Very High" in q:
            tags.update(["#highres", "#crisp"])
        elif "High" in q:
            tags.update(["#hd", "#quality"])

        # Mood-based tags (NEW – from vision)
        if screenshot.mood:
            mood_tags = {
                "epic": ["#epicmoment", "#legendary"],
                "serene": ["#peaceful", "#relaxing"],
                "intense": ["#intense", "#adrenaline"],
                "mysterious": ["#mysterious", "#atmospheric"],
                "melancholic": ["#moody", "#atmospheric"],
                "vibrant": ["#colorful", "#vibrant"],
                "dark": ["#dark", "#gothic"],
                "beautiful": ["#beautiful", "#stunning"],
            }
            for key, mt in mood_tags.items():
                if key in screenshot.mood.lower():
                    tags.update(mt)
                    break

        # Vision-suggested tags
        if vision and vision.suggested_hashtags:
            for t in vision.suggested_hashtags[:5]:
                tag = t if t.startswith("#") else f"#{t}"
                tag = tag.lower().replace(" ", "")
                tags.add(tag)

        # Fill with variety tags
        shuffled = VARIETY_HASHTAGS.copy()
        random.shuffle(shuffled)
        for t in shuffled:
            if len(tags) >= max_tags:
                break
            tags.add(t)

        return list(tags)[:max_tags]

    # ── Image download / encode ──────────────────────────────────────────

    @staticmethod
    async def _download_and_encode(image_url: str) -> str:
        """Download image, resize for API, return base64."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Image download failed: {resp.status}")
                data = await resp.read()

        img = Image.open(io.BytesIO(data))
        img.thumbnail((800, 600), Image.LANCZOS)

        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()
