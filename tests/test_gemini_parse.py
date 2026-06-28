"""Tests for gemini._extract_json -- the JSON choke point all flows depend on.

These run without any API: _extract_json is pure. It must always return a dict
or raise GeminiError, so flows (which catch only GeminiError/GeminiNotConfigured)
degrade gracefully instead of crashing on AttributeError/JSONDecodeError.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gemini  # noqa: E402


def test_plain_object_parses():
    assert gemini._extract_json('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_fenced_object_parses():
    assert gemini._extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_object_embedded_in_prose_parses():
    assert gemini._extract_json('Sure! {"a": 1} hope that helps') == {"a": 1}


def test_top_level_array_raises_geminierror():
    # Legal JSON, but a caller's .get() would AttributeError -> must be GeminiError.
    with pytest.raises(gemini.GeminiError):
        gemini._extract_json('[{"type": "fill"}]')


def test_scalar_raises_geminierror():
    with pytest.raises(gemini.GeminiError):
        gemini._extract_json("42")


def test_malformed_braces_raise_geminierror():
    # The brace-fallback substring is itself invalid JSON.
    with pytest.raises(gemini.GeminiError):
        gemini._extract_json("noise {not: valid, json} more")


def test_no_json_raises_geminierror():
    with pytest.raises(gemini.GeminiError):
        gemini._extract_json("absolutely no json here")


def test_empty_raises_geminierror():
    with pytest.raises(gemini.GeminiError):
        gemini._extract_json("")
