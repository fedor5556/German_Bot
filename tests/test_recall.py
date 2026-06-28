"""Unit tests for the active-recall engine (pure logic)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import recall  # noqa: E402


# --- normalisation / fuzzy matching -------------------------------------------

def test_umlaut_folding_matches_ascii_digraphs():
    assert recall.normalize("über") == recall.normalize("ueber")
    assert recall.normalize("Straße") == recall.normalize("strasse")
    assert recall.normalize("schön") == "schoen"


def test_check_answer_is_accent_and_typo_tolerant():
    # umlaut spelling variants
    assert recall.check_answer("gewöhnt", "gewoehnt")
    assert recall.check_answer("gewöhnt", "gewohnt")  # one missing letter -> within tolerance
    # exact + case
    assert recall.check_answer("Tisch", "tisch")
    # clearly wrong
    assert not recall.check_answer("Tisch", "Stuhl")
    # empty answer never passes
    assert not recall.check_answer("Tisch", "   ")


def test_check_answer_short_words_need_exact_match():
    # 2-3 char targets get zero tolerance, so a different short word fails.
    assert not recall.check_answer("am", "an")
    assert recall.check_answer("am", "am")


def test_check_answer_article_insensitive():
    assert recall.check_answer("der Tisch", "Tisch", article_insensitive=True)
    assert recall.check_answer("Tisch", "der Tisch", article_insensitive=True)
    assert not recall.check_answer("der Tisch", "Tisch", article_insensitive=False)


def test_check_answer_accept_list():
    assert recall.check_answer("gegangen", "gehen", accept=["gehen"])


# --- cloze / english helpers ---------------------------------------------------

def test_make_cloze_blanks_the_word():
    out = recall.make_cloze("Ich bin nach Hause gegangen.", "gegangen")
    assert out == "Ich bin nach Hause _____."


def test_make_cloze_case_insensitive_first_occurrence():
    out = recall.make_cloze("Gestern war ein guter Tag, ein Tag zum Feiern.", "Tag")
    assert out.count("_____") == 1
    assert out.startswith("Gestern war ein guter _____")


def test_make_cloze_returns_none_when_absent():
    assert recall.make_cloze("Ich bin müde.", "Banane") is None


def test_clean_english_drops_parenthetical():
    assert recall.clean_english("I have decided to learn German. (sich entscheiden = to decide)") \
        == "I have decided to learn German"
    assert recall.clean_english("It depends") == "It depends"


# --- category taxonomy ---------------------------------------------------------

def test_canonical_category_snaps_to_taxonomy():
    assert recall.canonical_category("Akkusativ") == "Cases (Akkusativ/Dativ)"
    assert recall.canonical_category("wrong gender / article") == "Gender & articles"
    assert recall.canonical_category("Perfekt tense") == "Tense & aspect"
    assert recall.canonical_category("something weird") == "Other"
    assert recall.canonical_category(None) == "Other"
    # an exact taxonomy label round-trips
    assert recall.canonical_category("Prepositions") == "Prepositions"


def test_canonical_category_preposition_not_shadowed_by_position():
    # Regression: the 'position' alias (Word order) must not swallow 'preposition'.
    assert recall.canonical_category("preposition error") == "Prepositions"
    assert recall.canonical_category("wrong preposition") == "Prepositions"
    assert recall.canonical_category("Praeposition") == "Prepositions"
    assert recall.canonical_category("Two-way prepositions") == "Prepositions"
    # genuine word-order strings still resolve correctly
    assert recall.canonical_category("wrong word order") == "Word order"


def test_canonical_category_coerces_non_string():
    # A wrong-typed field from Gemini must not crash.
    assert recall.canonical_category(["x"]) == "Other"
    assert recall.canonical_category(123) == "Other"


def test_check_answer_multiword_requires_exact():
    # Regression: a 'fix' drill sentence one edit away (the very error) must fail.
    expected = "Ich habe gestern einen Film gesehen."
    assert not recall.check_answer(expected, "Ich habe gestern eine Film gesehen.")
    assert recall.check_answer(expected, "Ich habe gestern einen Film gesehen.")
    # umlaut folding still works across a whole sentence (exact-equality branch)
    assert recall.check_answer("Ich bin müde heute.", "Ich bin muede heute.")


# --- mode selection ------------------------------------------------------------

def _mode(card_id=1, reps=0, ease=2.5, low=False, gem=True, enriched=True, deck=40):
    return recall.choose_mode(
        card_id=card_id, repetitions=reps, ease_factor=ease, low_energy=low,
        gemini_ok=gem, enriched=enriched, deck_size=deck,
    )


def test_new_card_is_recognition():
    assert _mode(reps=0) == recall.RECOGNITION


def test_hard_card_stays_recognition():
    assert _mode(reps=5, ease=1.5) == recall.RECOGNITION


def test_low_energy_is_multiple_choice():
    assert _mode(reps=4, low=True) == recall.MULTIPLE_CHOICE


def test_low_energy_falls_back_when_deck_tiny():
    # No room for 3 distractors -> can't do MC -> recognition.
    assert _mode(reps=4, low=True, deck=2) == recall.RECOGNITION


def test_young_card_uses_typed_or_mc():
    assert _mode(reps=1) in (recall.CLOZE, recall.MULTIPLE_CHOICE)


def test_mature_card_uses_production_or_use():
    assert _mode(reps=6) in (recall.USE_SENTENCE, recall.PRODUCTION)


def test_no_gemini_and_unenriched_degrades_to_recognition_or_mc():
    # Without enrichment, cloze/production/use are impossible; only MC or recognition.
    for reps in (1, 3, 6):
        m = _mode(reps=reps, gem=False, enriched=False)
        assert m in (recall.RECOGNITION, recall.MULTIPLE_CHOICE)


def test_unenriched_mature_card_drops_to_production_unavailable():
    # reps 3-4 candidates are [production, cloze], both need enrichment -> recognition.
    assert _mode(reps=3, enriched=False, deck=2) == recall.RECOGNITION
