"""Per-tier prompt selection.

One system prompt serving both a frontier API model and a 3B-class local
model is poorly matched to whichever end it wasn't tuned for. Long,
negation-heavy rule lists are the first thing a small model drops, while a
small-model-tuned prompt — worked examples, blunt repeated emphasis —
reads as over-explained to a frontier model.

PromptSet carries everything that varies by tier; see frontier.py / small.py
for the actual prompt content. select_prompts() resolves which PromptSet a
connection uses: an explicit config override, else the provider adapter's
own judgement of its model (ChatProvider.prompt_tier).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import frontier, small


@dataclass(frozen=True)
class PromptSet:
    persona_system: str  # conversation_service.py's present_hunk/respond_to_reviewer
    briefing_system: str  # briefing_service.py's analyze_hunk


FRONTIER = PromptSet(
    persona_system=frontier.PERSONA_SYSTEM,
    briefing_system=frontier.BRIEFING_SYSTEM,
)
SMALL = PromptSet(
    persona_system=small.PERSONA_SYSTEM,
    briefing_system=small.BRIEFING_SYSTEM,
)


def select_prompts(conversation_config: dict, provider_tier: str) -> PromptSet:
    """conversation_config is config.yaml's `conversation:` block;
    provider_tier is the adapter's prompt_tier() for the configured model.
    `prompt_tier: small | frontier` in config always wins; `auto` (the
    default, and the fallback for any other value) defers to the adapter."""
    tier = conversation_config.get("prompt_tier", "auto")
    if tier not in ("small", "frontier"):
        tier = provider_tier
    return FRONTIER if tier == "frontier" else SMALL
