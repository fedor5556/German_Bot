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
import recall

log = logging.getLogger(__name__)

# The fixed mistake taxonomy, rendered for prompts (the model must pick from it).
_CATEGORY_LIST = ", ".join(recall.CATEGORIES)

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
    """Parse a JSON *object* out of the model's reply, tolerating code fences.

    Always returns a dict or raises GeminiError -- callers immediately do
    data.get(...), so a top-level array/scalar (legal JSON) or a malformed
    outermost {...} must surface as a GeminiError the flows already catch, never
    as an AttributeError/JSONDecodeError that would abort the whole session.
    """
    if not text:
        raise GeminiError("empty response from Gemini")
    text = text.strip()
    # Strip ```json ... ``` fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # Last resort: grab the outermost {...}, which may itself be invalid.
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if not brace:
            raise GeminiError("could not parse JSON from Gemini response")
        try:
            obj = json.loads(brace.group(0))
        except json.JSONDecodeError:
            raise GeminiError("could not parse JSON from Gemini response")
    if not isinstance(obj, dict):
        raise GeminiError("Gemini returned non-object JSON")
    return obj


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
Each is an object {{"front": ..., "back": ..., "category": ...}} where "front" is \
the corrected German sentence or phrase as a cloze/example, "back" is the correct \
form plus a brief why in English, and "category" classifies the error using EXACTLY \
one of these labels: {categories}. Prefer full example sentences over isolated words.

If the text is already correct, set "mistakes" to [] and say so in "explanation"."""


LESSON_PROMPT = """You are a German tutor processing a learner's lesson notes \
(level A2 -> B1). Extract the highest-value flashcards: new vocabulary in context \
and any corrections of the learner's mistakes (their own mistakes are the most \
valuable cards).

Lesson notes:
\"\"\"{notes}\"\"\"

Return ONLY a JSON object with these keys:
- "summary": one short English sentence on what this lesson covered.
- "cards": an array of objects {{"front": ..., "back": ..., "kind": ..., "category": ...}}. \
"front" is German (prefer a full example sentence or cloze, not a bare word); "back" is \
the English meaning / completion plus a short note if useful. "kind" is "correction" if \
the card fixes a mistake the learner made, otherwise "vocab". For "correction" cards, \
"category" classifies the error using EXACTLY one of: {categories} (omit/empty for vocab). \
Aim for 5-15 strong cards."""


async def correct_writing(text: str, targets: list[str] | None = None) -> dict:
    """Return {corrected, explanation, mistakes:[{front,back,category}]}."""
    targets_str = ", ".join(targets) if targets else "(none specified)"
    prompt = CORRECTION_PROMPT.format(
        targets=targets_str, text=text.strip(), categories=_CATEGORY_LIST
    )
    data = await _generate(prompt)
    data.setdefault("corrected", text.strip())
    data.setdefault("explanation", "")
    mistakes = data.get("mistakes") or []
    clean = []
    for m in mistakes:
        if isinstance(m, dict) and m.get("front") and m.get("back"):
            m["category"] = recall.canonical_category(m.get("category"))
            clean.append(m)
    data["mistakes"] = clean
    return data


async def extract_lesson(notes: str) -> dict:
    """Return {summary, cards:[{front,back,kind,category}]}."""
    prompt = LESSON_PROMPT.format(notes=notes.strip(), categories=_CATEGORY_LIST)
    data = await _generate(prompt)
    data.setdefault("summary", "")
    cards = data.get("cards") or []
    clean = []
    for c in cards:
        if isinstance(c, dict) and c.get("front") and c.get("back"):
            c["kind"] = "correction" if str(c.get("kind", "")).lower() == "correction" else "vocab"
            c["category"] = recall.canonical_category(c.get("category")) if c["kind"] == "correction" else ""
            clean.append(c)
    data["cards"] = clean
    return data


# --- active-recall enrichment + drills -----------------------------------------

ENRICH_PROMPT = """You prepare a German flashcard for active recall. The card is:
FRONT (German): \"\"\"{front}\"\"\"
BACK (English meaning / note): \"\"\"{back}\"\"\"

Identify the single most useful German word or short fixed phrase this card teaches.

Return ONLY a JSON object with these keys:
- "word": that word/phrase in its dictionary form (e.g. the infinitive, or noun \
WITH its article like "der Tisch"). 1-3 words max.
- "clue_en": a concise English clue for it (a few words), no German.
- "answer": the EXACT surface form of that word/phrase as it appears in the FRONT \
sentence (it may be inflected, e.g. "gegangen"). Copy it verbatim from the FRONT.
- "cloze": the FRONT sentence with that exact surface form replaced by "_____" \
(five underscores). Change nothing else."""


FRESH_CLOZE_PROMPT = """Write ONE natural German example sentence (CEFR A2-B1) that \
uses the word/phrase below in a clear context. Then blank that word out.

Word/phrase: "{word}" ({clue})

Return ONLY a JSON object:
- "cloze": the sentence with the target word replaced by "_____" (five underscores).
- "answer": the exact surface form you removed (as it appeared, possibly inflected).
- "sentence": the full sentence with the word present (for the reveal)."""


USAGE_CHECK_PROMPT = """A German learner (A2 -> B1) was asked to use the word/phrase \
"{word}" in their own German sentence. They wrote:
\"\"\"{sentence}\"\"\"

Judge whether they used "{word}" correctly AND wrote a grammatical sentence.

Return ONLY a JSON object:
- "ok": true if the sentence is correct (or has only trivial slips) AND genuinely \
uses "{word}"; false otherwise.
- "feedback": 1-2 short English sentences -- praise if good, else the key fix and WHY.
- "corrected": the corrected German sentence (their version if already fine).
- "category": if not ok, classify the main error as EXACTLY one of: {categories}. \
Use "" (empty) when ok is true."""


DRILLS_PROMPT = """Create {n} short German grammar exercises for a learner moving \
A2 -> B1, focused on this point: "{topic}".

Mix these exercise types:
- "fill": a sentence with a "_____" blank to complete.
- "transform": rewrite a given sentence per an instruction (e.g. into Perfekt).
- "fix": a sentence containing ONE deliberate error for the learner to correct.

Return ONLY a JSON object with key "exercises": an array of {n} objects, each:
- "type": one of "fill", "transform", "fix".
- "prompt": the task shown to the learner. Put any English instruction first, then \
the German on a new line. Be self-contained.
- "answer": the single best correct German answer (the full expected response).
- "accept": an array of other acceptable correct answers (may be empty).
- "explanation": one short English sentence on the rule being practised."""


async def enrich_card(front: str, back: str) -> dict:
    """Derive {word, clue_en, cloze, answer} for cloze/production modes.

    Falls back to a locally-built cloze when the model's cloze looks wrong, so a
    cached enrichment is always self-consistent.
    """
    prompt = ENRICH_PROMPT.format(front=front.strip(), back=back.strip())
    data = await _generate(prompt)
    # str() guards against a well-shaped object with a wrong-typed field
    # (e.g. "word": ["a","b"]) raising AttributeError on .strip().
    word = str(data.get("word") or "").strip()
    clue = str(data.get("clue_en") or "").strip()
    answer = str(data.get("answer") or "").strip()
    cloze = str(data.get("cloze") or "").strip()
    if not word or not clue:
        raise GeminiError("enrichment missing word/clue")
    # Trust our own cloze builder over the model's when we have the surface form.
    if answer:
        built = recall.make_cloze(front, answer)
        if built:
            cloze = built
    if "_____" not in cloze:
        raise GeminiError("enrichment produced no usable cloze")
    return {"word": word, "clue_en": clue, "answer": answer or word, "cloze": cloze}


async def fresh_cloze(word: str, clue: str) -> dict:
    """Generate a brand-new cloze sentence for a word (the anti-memorisation win)."""
    prompt = FRESH_CLOZE_PROMPT.format(word=word.strip(), clue=(clue or "").strip())
    data = await _generate(prompt)
    cloze = str(data.get("cloze") or "").strip()
    answer = str(data.get("answer") or "").strip()
    if "_____" not in cloze or not answer:
        raise GeminiError("fresh cloze malformed")
    return {"cloze": cloze, "answer": answer, "sentence": str(data.get("sentence") or "").strip()}


async def check_usage(word: str, sentence: str) -> dict:
    """Judge a learner's 'use it in a sentence' attempt."""
    prompt = USAGE_CHECK_PROMPT.format(
        word=word.strip(), sentence=sentence.strip(), categories=_CATEGORY_LIST
    )
    data = await _generate(prompt)
    ok = bool(data.get("ok"))
    return {
        "ok": ok,
        "feedback": str(data.get("feedback") or "").strip(),
        "corrected": str(data.get("corrected") or sentence.strip()).strip(),
        "category": "" if ok else recall.canonical_category(data.get("category")),
    }


VERIFY_PROMPT = """A German learner answered a grammar exercise.
TASK: \"\"\"{prompt}\"\"\"
A correct reference answer: \"\"\"{answer}\"\"\"
The learner wrote: \"\"\"{given}\"\"\"

Is the learner's answer correct German that satisfies the task? It need not match the \
reference word-for-word -- accept any answer that is grammatical and meaning-equivalent. \
Minor spelling slips are fine.

Return ONLY a JSON object:
- "ok": true or false.
- "feedback": one short English sentence (praise, or the key fix and why)."""


async def verify_drill_answer(prompt: str, answer: str, given: str) -> dict:
    """Semantic check for a drill answer where many forms may be correct."""
    p = VERIFY_PROMPT.format(prompt=prompt.strip(), answer=answer.strip(), given=given.strip())
    data = await _generate(p)
    return {"ok": bool(data.get("ok")), "feedback": (data.get("feedback") or "").strip()}


async def generate_drills(topic: str, n: int = 4) -> list[dict]:
    """Return a list of {type, prompt, answer, accept[], explanation} exercises."""
    n = max(3, min(5, int(n)))
    prompt = DRILLS_PROMPT.format(topic=topic.strip(), n=n)
    data = await _generate(prompt)
    raw = data.get("exercises") or []
    out = []
    for ex in raw:
        if not isinstance(ex, dict) or not ex.get("prompt") or not ex.get("answer"):
            continue
        accept = ex.get("accept") or []
        out.append(
            {
                "type": str(ex.get("type", "fill")).lower(),
                "prompt": str(ex["prompt"]).strip(),
                "answer": str(ex["answer"]).strip(),
                "accept": [str(a).strip() for a in accept if str(a).strip()],
                "explanation": str(ex.get("explanation", "")).strip(),
            }
        )
    if not out:
        raise GeminiError("no usable drills returned")
    return out
