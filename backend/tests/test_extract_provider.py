"""Provider selection and cross-provider fallback in call_llm_extract().

Mocked at the _call_gemini_extract/_call_openai_extract boundary -- these tests never
hit a real LLM API, so they stay fast, free, and don't compete with GEMINI_API_KEY's
20-requests/day free-tier ceiling (discovered in practice; see context/phase3.md D7/D8).
"""

from __future__ import annotations

import pytest

from monitor_records import extract
from monitor_records.extract import ExtractedEvent, call_llm_extract


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("LLM_PROVIDER", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def _fake_extraction(tag: str) -> ExtractedEvent:
    return ExtractedEvent(is_material_event=True, confidence=0.9, subject=tag)


def test_default_prefers_openai_when_both_keys_present(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "AQ-test")
    monkeypatch.setattr(extract, "_call_openai_extract", lambda *a: _fake_extraction("openai"))
    monkeypatch.setattr(extract, "_call_gemini_extract", lambda *a: _fake_extraction("gemini"))

    result = call_llm_extract("matter", "t", None, None, "text")
    assert result.subject == "openai"


def test_falls_back_to_gemini_when_only_gemini_key_present(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AQ-test")
    monkeypatch.setattr(extract, "_call_gemini_extract", lambda *a: _fake_extraction("gemini"))

    result = call_llm_extract("matter", "t", None, None, "text")
    assert result.subject == "gemini"


def test_explicit_provider_override_is_honored(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "AQ-test")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setattr(extract, "_call_openai_extract", lambda *a: _fake_extraction("openai"))
    monkeypatch.setattr(extract, "_call_gemini_extract", lambda *a: _fake_extraction("gemini"))

    result = call_llm_extract("matter", "t", None, None, "text")
    assert result.subject == "gemini"


def test_unknown_provider_override_raises(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    with pytest.raises(RuntimeError, match="unknown LLM_PROVIDER"):
        call_llm_extract("matter", "t", None, None, "text")


def test_primary_failure_falls_back_to_the_other_configured_provider(monkeypatch):
    """This is the exact scenario that happened in practice: the primary provider
    (OpenAI, by default) fails -- e.g. a rate limit -- and a second key is configured."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "AQ-test")

    def _openai_fails(*a):
        raise RuntimeError("429 rate limited")

    monkeypatch.setattr(extract, "_call_openai_extract", _openai_fails)
    monkeypatch.setattr(extract, "_call_gemini_extract", lambda *a: _fake_extraction("gemini"))

    result = call_llm_extract("matter", "t", None, None, "text")
    assert result.subject == "gemini"


def test_explicit_pin_does_not_auto_fallback_even_if_it_fails(monkeypatch):
    """An explicit LLM_PROVIDER pin (e.g. to deliberately test/compare one provider)
    must be honored exactly, not silently overridden by the other provider's
    availability -- auto-fallback only applies when the caller didn't pin anything."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "AQ-test")
    monkeypatch.setenv("LLM_PROVIDER", "openai")

    def _openai_fails(*a):
        raise RuntimeError("boom")

    monkeypatch.setattr(extract, "_call_openai_extract", _openai_fails)
    monkeypatch.setattr(extract, "_call_gemini_extract", lambda *a: _fake_extraction("gemini"))

    with pytest.raises(RuntimeError, match="boom"):
        call_llm_extract("matter", "t", None, None, "text")


def test_both_providers_failing_raises_a_combined_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "AQ-test")

    def _fails(name):
        def _f(*a):
            raise RuntimeError(f"{name} down")
        return _f

    monkeypatch.setattr(extract, "_call_openai_extract", _fails("openai"))
    monkeypatch.setattr(extract, "_call_gemini_extract", _fails("gemini"))

    with pytest.raises(RuntimeError, match="both LLM providers failed"):
        call_llm_extract("matter", "t", None, None, "text")


def test_no_keys_configured_still_raises_a_clear_error(monkeypatch):
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY not set"):
        call_llm_extract("matter", "t", None, None, "text")
