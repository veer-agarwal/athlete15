"""Assembles the morning briefing. Phase 2 onward.

Order of sections, per the target output:
    1. Recovery and sleep numbers
    2. Short narrative on last night's sleep
    3. Today's calendar
    4. Homework due
    5. Training status: yesterday's session, load trend, active injuries
    6. Weather, then news

RESILIENCE: wrap every source call. One dead API must degrade to a single line
saying so, not kill the whole message. You will notice a missing WHOOP line. You
will not notice a briefing that silently never arrived.

The briefing REPORTS. It does not coach. No training recommendations.
"""

from src.sources import weather, news


def build() -> str:
    """Assemble the full briefing text.

    Phase 2 version: weather and news only, plain template, no LLM.
    Later phases add sections and hand the assembled facts to llm.py for phrasing.
    """
    raise NotImplementedError("phase 2")


def build_context_block() -> str:
    """Structured facts to hand to the local model in phase 7.

    Keep this as compact labeled plain text, not JSON. Small models follow a simple
    labeled block more reliably than they follow nested structure.
    """
    raise NotImplementedError("phase 7")
