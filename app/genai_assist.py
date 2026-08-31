"""
Optional generative-AI assist: draft an ECR change description from a part
and a rough engineer's note, using the Claude API. One real LLM call, wired
into one real workflow step, with a deterministic fallback so the app still
runs for anyone without an API key configured.

Model choice: Claude Opus 5 (`claude-opus-5`) by default. This is a short,
low-stakes drafting task, so `output_config.effort="low"` keeps it fast and
cheap without switching to a smaller model -- effort tuning trades thoroughness
for cost within one model; picking a weaker model would trade quality instead.
Override the model via the ANTHROPIC_MODEL env var if you want to compare.
"""
import os

try:
    import anthropic
    _ANTHROPIC_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - keeps the app usable if the package is missing
    _ANTHROPIC_SDK_AVAILABLE = False

MODEL_ID = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")

_SYSTEM_PROMPT = (
    "You write concise, formal engineering change request (ECR) descriptions "
    "for a PLM system used by a machine-tool manufacturer. Given a part and a "
    "rough note from an engineer, produce a single well-formed paragraph "
    "(2-4 sentences) suitable to paste directly into an ECR description field: "
    "what is changing, and briefly why. No preamble, no markdown, no heading -- "
    "just the paragraph."
)


def _template_fallback(part_number, part_description, rough_note):
    """Deterministic fallback used with no API key, or if the API call fails."""
    return (
        f"Change requested for {part_number} ({part_description}): {rough_note.strip().rstrip('.')}. "
        f"Requested for engineering review and revision update."
    )


def draft_ecr_description(part_number, part_description, rough_note):
    """
    Returns (description_text, source) where source is "llm" or "template",
    so the UI can be honest about which one produced the draft.
    """
    if not rough_note or not rough_note.strip():
        return "", "template"

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not _ANTHROPIC_SDK_AVAILABLE or not api_key:
        return _template_fallback(part_number, part_description, rough_note), "template"

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL_ID,
            max_tokens=512,  # a short paragraph -- deliberately capped well under the default
            output_config={"effort": "low"},  # simple drafting task, not reasoning-heavy
            system=_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Part: {part_number} -- {part_description}\nEngineer's rough note: {rough_note}",
            }],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if text:
            return text, "llm"
        return _template_fallback(part_number, part_description, rough_note), "template"
    except anthropic.APIError:
        # Any API failure (auth, rate limit, connection, ...) -- degrade gracefully.
        return _template_fallback(part_number, part_description, rough_note), "template"
