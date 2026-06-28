"""Active-recall engine -- pure logic, no I/O, unit-tested.

This is the brain of the "variety pack": given a card's SRS state it picks a
review *mode* (how the card is presented), and it grades typed answers in an
accent/typo-tolerant way. Both are pure functions so they can be unit-tested
without a DB, a bot, or Gemini.

The SM-2 scheduler in srs.py is untouched -- we only vary the *presentation*,
never the spacing.
"""

from __future__ import annotations

import re
import unicodedata

# --- review modes --------------------------------------------------------------
# Each value doubles as the callback / state tag, so keep them short & stable.
RECOGNITION = "recognition"      # DE -> EN, self-rated reveal (the original mode)
MULTIPLE_CHOICE = "mc"           # DE -> pick the English (low-effort "bad day")
CLOZE = "cloze"                  # example sentence with the word blanked; type it
PRODUCTION = "production"        # EN clue -> type the German word
USE_SENTENCE = "use"             # write your own sentence with the word (Gemini checks)

ALL_MODES = (RECOGNITION, MULTIPLE_CHOICE, CLOZE, PRODUCTION, USE_SENTENCE)

# Modes that need the user to type free text (routed through the text dispatcher).
TYPED_MODES = (CLOZE, PRODUCTION, USE_SENTENCE)

# A card is "hard" (kept on easy modes) when its ease has been driven down.
HARD_EASE = 1.8


# --- mistake taxonomy (shared by the mistake-pattern engine) -------------------
# A fixed list so counts aggregate cleanly across writing / lessons / drills.
CATEGORIES = [
    "Cases (Akkusativ/Dativ)",
    "Gender & articles",
    "Word order",
    "Verb conjugation",
    "Tense & aspect",
    "Prepositions",
    "Adjective endings",
    "Plurals",
    "Pronouns",
    "Spelling",
    "Word choice",
    "Other",
]

# Keyword -> canonical category, used to snap whatever Gemini returns onto the
# fixed list above (the model is told the list, but we never trust it blindly).
_CATEGORY_ALIASES = {
    "akkusativ": "Cases (Akkusativ/Dativ)",
    "dativ": "Cases (Akkusativ/Dativ)",
    "accusative": "Cases (Akkusativ/Dativ)",
    "dative": "Cases (Akkusativ/Dativ)",
    "case": "Cases (Akkusativ/Dativ)",
    "kasus": "Cases (Akkusativ/Dativ)",
    "gender": "Gender & articles",
    "article": "Gender & articles",
    "artikel": "Gender & articles",
    "genus": "Gender & articles",
    "der die das": "Gender & articles",
    "word order": "Word order",
    "wortstellung": "Word order",
    "syntax": "Word order",
    "position": "Word order",
    "conjugation": "Verb conjugation",
    "konjugation": "Verb conjugation",
    "verb form": "Verb conjugation",
    "verb ending": "Verb conjugation",
    "tense": "Tense & aspect",
    "aspect": "Tense & aspect",
    "perfekt": "Tense & aspect",
    "praeteritum": "Tense & aspect",
    "präteritum": "Tense & aspect",
    "past": "Tense & aspect",
    "preposition": "Prepositions",
    "präposition": "Prepositions",
    "praeposition": "Prepositions",
    "adjective": "Adjective endings",
    "adjektiv": "Adjective endings",
    "ending": "Adjective endings",
    "plural": "Plurals",
    "pronoun": "Pronouns",
    "pronomen": "Pronouns",
    "spelling": "Spelling",
    "rechtschreibung": "Spelling",
    "typo": "Spelling",
    "orthograph": "Spelling",
    "vocab": "Word choice",
    "word choice": "Word choice",
    "wortwahl": "Word choice",
    "wrong word": "Word choice",
    "lexical": "Word choice",
}


def canonical_category(raw) -> str:
    """Snap a free-text category onto the fixed taxonomy (default 'Other')."""
    if not raw:
        return "Other"
    text = str(raw).strip()  # str() guards a non-string field from Gemini
    if not text:
        return "Other"
    for cat in CATEGORIES:  # exact match wins
        if text.lower() == cat.lower():
            return cat
    low = text.lower()
    # Longest alias first so 'preposition' wins over the substring 'position'.
    for key, cat in sorted(_CATEGORY_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if key in low:
            return cat
    return "Other"


# --- text normalisation & fuzzy matching --------------------------------------

# Map German umlauts / eszett to their canonical ASCII digraphs so that a user
# who types "ueben" matches "üben" and vice-versa. casefold() already turns
# ß -> ss, so we only need the lowercase vowels here.
_UMLAUTS = {"ä": "ae", "ö": "oe", "ü": "ue"}
_LEADING_ARTICLES = {
    "der", "die", "das", "den", "dem", "des",
    "ein", "eine", "einen", "einem", "einer", "eines",
    "the", "a", "an", "to",
}


def fold(text: str) -> str:
    """Lowercase + fold umlauts/eszett to ASCII digraphs."""
    out = text.casefold()  # ß -> ss, lowercases everything incl. Ä -> ä
    for u, repl in _UMLAUTS.items():
        out = out.replace(u, repl)
    # Drop any remaining diacritics (é, à ...) that slip in from English.
    out = "".join(
        c for c in unicodedata.normalize("NFKD", out) if not unicodedata.combining(c)
    )
    return out


def normalize(text: str, *, strip_articles: bool = False) -> str:
    """Canonical comparison form: folded, punctuation removed, single-spaced."""
    folded = fold(text or "")
    folded = re.sub(r"[^0-9a-z\s]", " ", folded)  # keep letters/digits/space
    tokens = folded.split()
    if strip_articles and len(tokens) > 1 and tokens[0] in _LEADING_ARTICLES:
        tokens = tokens[1:]
    return " ".join(tokens)


def levenshtein(a: str, b: str) -> int:
    """Classic edit distance (insert/delete/substitute = 1)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def _tolerance(n: int) -> int:
    """How many edits to forgive for a target of length n (typo tolerance)."""
    if n <= 3:
        return 0
    if n <= 8:
        return 1
    return 2


def check_answer(
    expected: str,
    given: str,
    *,
    accept: list[str] | None = None,
    article_insensitive: bool = False,
) -> bool:
    """True if `given` matches `expected` (or any `accept` variant), forgiving
    umlaut spelling and small typos. Used for cloze / production grading."""
    if not (given or "").strip():
        return False
    candidates = [expected] + list(accept or [])
    given_n = normalize(given, strip_articles=article_insensitive)
    if not given_n:
        return False
    for cand in candidates:
        cand_n = normalize(cand, strip_articles=article_insensitive)
        if not cand_n:
            continue
        if given_n == cand_n:
            return True
        # Fuzzy (typo/umlaut) tolerance only for single-word answers. Multi-word
        # answers (drill sentences) must match exactly, so a "fix the error" drill
        # can't be marked correct while still containing the error it targets.
        if " " not in cand_n and levenshtein(given_n, cand_n) <= _tolerance(len(cand_n)):
            return True
    return False


# --- cloze / english helpers ---------------------------------------------------

def make_cloze(sentence: str, surface: str, blank: str = "_____") -> str | None:
    """Replace the first whole-word occurrence of `surface` with a blank.

    Returns None when the surface form can't be located so the caller can fall
    back to another mode instead of showing a broken card.
    """
    surface = (surface or "").strip()
    sentence = sentence or ""
    if not surface or not sentence:
        return None
    # Whole-word match with German-aware boundaries (treat umlauts as letters).
    pattern = re.compile(
        r"(?<![\wäöüßÄÖÜ])" + re.escape(surface) + r"(?![\wäöüßÄÖÜ])",
        re.IGNORECASE,
    )
    new, count = pattern.subn(blank, sentence, count=1)
    if count:
        return new
    # Loose fallback: plain case-insensitive substring.
    idx = sentence.lower().find(surface.lower())
    if idx >= 0:
        return sentence[:idx] + blank + sentence[idx + len(surface):]
    return None


def clean_english(back: str) -> str:
    """The bare English meaning from a card back, dropping the '(...)' note.

    Card backs look like 'I have decided to learn German. (sich entscheiden ...)'.
    For multiple-choice options / production clues we want just the meaning.
    """
    text = (back or "").strip()
    cut = text.find(" (")
    if cut > 0:
        text = text[:cut]
    return text.strip().rstrip(".") or (back or "").strip()


# --- mode selection ------------------------------------------------------------

def choose_mode(
    *,
    card_id: int,
    repetitions: int,
    ease_factor: float,
    low_energy: bool,
    gemini_ok: bool,
    enriched: bool,
    deck_size: int,
) -> str:
    """Pick the presentation mode for a card from its SRS state.

    Policy (the "variety pack"):
      - bad-day / low-energy  -> multiple choice for everything (keep the streak).
      - brand-new cards       -> recognition (first, gentle exposure).
      - hard cards (low ease) -> recognition (don't punish a struggling card).
      - young (1-2 reps)      -> cloze / multiple choice.
      - growing (3-4 reps)    -> production / cloze.
      - mature (5+ reps)      -> use-in-a-sentence / production.
    Unavailable modes (no Gemini, not enriched, tiny deck) are dropped, falling
    back to recognition so the session never breaks.
    """
    mc_ok = deck_size >= 4
    if low_energy:
        # Bad-day mode is minimal-effort: multiple choice, or recognition when the
        # deck is too small to build distractors -- never a typed mode.
        return MULTIPLE_CHOICE if mc_ok else RECOGNITION
    if repetitions <= 0:
        return RECOGNITION
    if ease_factor and ease_factor < HARD_EASE:
        return RECOGNITION

    if repetitions <= 2:
        candidates = [CLOZE, MULTIPLE_CHOICE]
    elif repetitions <= 4:
        candidates = [PRODUCTION, CLOZE]
    else:
        candidates = [USE_SENTENCE, PRODUCTION]

    available = [m for m in candidates if _mode_available(m, gemini_ok, enriched, mc_ok)]
    if not available:
        return RECOGNITION
    # Deterministic but varied: rotates across cards and as a card matures.
    return available[(card_id + repetitions) % len(available)]


def _mode_available(mode: str, gemini_ok: bool, enriched: bool, mc_ok: bool) -> bool:
    if mode == MULTIPLE_CHOICE:
        return mc_ok
    if mode in (CLOZE, PRODUCTION):
        return enriched
    if mode == USE_SENTENCE:
        return gemini_ok and enriched
    return True
