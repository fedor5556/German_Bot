"""Gemini API wrapper -- corrections + lesson extraction.

Design notes baked in from the plan/guide:
- Lazy SDK import: this module imports fine even when google-genai or the key is
  absent; calls just raise GeminiNotConfigured so flows can degrade gracefully.
- The SDK call is blocking, so it runs in a worker thread (asyncio.to_thread)
  to avoid stalling PTB's event loop.
- Retry with exponential backoff on transient errors (Phase 5).
- Gemini is asked for strict JSON; we parse defensively.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import config

log = logging.getLogger(__name__)

_client = None  # cached genai.Client


class GeminiNotConfigured(RuntimeError):
    """Raised when no API key / SDK is available."""


class GeminiError(RuntimeError):
    """Raised after retries are exhausted."""


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not config.gemini_configured():
        raise GeminiNotConfigured("GEMINI_API_KEY is not set in .env")
    try:
        from google import genai  # lazy import
    except ImportError as exc:  # SDK not installed
        raise GeminiNotConfigured("google-genai SDK is not installed") from exc
    _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def _extract_json(text: str) -> dict:
    """Parse a JSON object out of the model's reply, tolerating code fences."""
    if not text:
        raise GeminiError("empty response from Gemini")
    text = text.strip()
    # Strip ```json ... ``` fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Last resort: grab the outermost {...}.
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            return json.loads(brace.group(0))
        raise GeminiError("could not parse JSON from Gemini response")


def _generate_sync(prompt: str) -> str:
    """Blocking call with retry + exponential backoff. Runs in a worker thread."""
    client = _get_client()
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            resp = client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            return resp.text or ""
        except Exception as exc:  # noqa: BLE001 - SDK raises varied error types
            last_exc = exc
            log.warning("Gemini call failed (attempt %d/4): %s", attempt + 1, exc)
            if attempt < 3:
                import time

                time.sleep(delay)
                delay *= 2
    raise GeminiError(f"Gemini failed after retries: {last_exc}")


async def _generate(prompt: str) -> dict:
    raw = await asyncio.to_thread(_generate_sync, prompt)
    return _extract_json(raw)


CORRECTION_PROMPT = """You are a patient German tutor. Your learner is around A2 \
moving toward B1. They wrote the German text below as their own attempt -- never \
rewrite it from scratch, just correct what is there.

Target words they were asked to use: {targets}

Their text:
\"\"\"{text}\"\"\"

Return ONLY a JSON object with these keys:
- "corrected": the corrected German text (keep their meaning and length).
- "explanation": 1-3 short English sentences explaining the most important fixes \
and WHY (grammar reason), not every tiny change.
- "mistakes": an array (possibly empty) of flashcards, one per meaningful mistake. \
Each is an object {{"front": ..., "back": ...}} where "front" is the corrected \
German sentence or phrase as a cloze/example, and "back" is the correct form plus \
a brief why in English. Prefer full example sentences over isolated words.

If the text is already correct, set "mistakes" to [] and say so in "explanation"."""


LESSON_PROMPT = """You are a German tutor processing a learner's lesson notes \
(level A2 -> B1). Extract the highest-value flashcards: new vocabulary in context \
and any corrections of the learner's mistakes (their own mistakes are the most \
valuable cards).

Lesson notes:
\"\"\"{notes}\"\"\"

Return ONLY a JSON object with these keys:
- "summary": one short English sentence on what this lesson covered.
- "cards": an array of objects {{"front": ..., "back": ...}}. "front" is German \
(prefer a full example sentence or cloze, not a bare word); "back" is the English \
meaning / completion plus a short note if useful. Aim for 5-15 strong cards."""


async def correct_writing(text: str, targets: list[str] | None = None) -> dict:
    """Return {corrected, explanation, mistakes:[{front,back}]}."""
    targets_str = ", ".join(targets) if targets else "(none specified)"
    prompt = CORRECTION_PROMPT.format(targets=targets_str, text=text.strip())
    data = await _generate(prompt)
    data.setdefault("corrected", text.strip())
    data.setdefault("explanation", "")
    mistakes = data.get("mistakes") or []
    data["mistakes"] = [
        m for m in mistakes if isinstance(m, dict) and m.get("front") and m.get("back")
    ]
    return data


async def extract_lesson(notes: str) -> dict:
    """Return {summary, cards:[{front,back}]}."""
    prompt = LESSON_PROMPT.format(notes=notes.strip())
    data = await _generate(prompt)
    data.setdefault("summary", "")
    cards = data.get("cards") or []
    data["cards"] = [
        c for c in cards if isinstance(c, dict) and c.get("front") and c.get("back")
    ]
    return data
