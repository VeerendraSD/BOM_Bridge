"""
Only the deterministic fallback path is tested here -- it's the one this
suite can verify without network access or an API key. The LLM path is
exercised manually (see README) since it needs ANTHROPIC_API_KEY set.
"""
import os

from app.genai_assist import draft_ecr_description


def test_falls_back_to_template_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    text, source = draft_ecr_description("HOUSING-001", "Cast Iron Housing", "switch to ductile iron")
    assert source == "template"
    assert "HOUSING-001" in text
    assert "ductile iron" in text


def test_empty_rough_note_returns_empty_draft():
    text, source = draft_ecr_description("HOUSING-001", "Cast Iron Housing", "   ")
    assert text == ""
    assert source == "template"
