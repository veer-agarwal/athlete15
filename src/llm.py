"""Local inference via Ollama. Phase 7.

Ollama runs natively on Windows and exposes an HTTP API on port 11434. Nothing here
should ever call a hosted model.

The briefing task is summarization over about twenty lines of structured facts.
That is well within an 8B model at Q4 on an 8GB card, and nothing is waiting on
tokens at 6:30 AM, so throughput barely matters here.

Keep the system prompt strict. Small models drift into advice and filler unless
told not to.
"""

from src import config

SYSTEM_PROMPT = """You write a concise morning briefing from structured data.

Rules:
- Report only what is in the data. Never invent numbers.
- No training advice, no recommendations, no encouragement.
- No preamble. Start with the first fact.
- Plain text. No markdown, no emoji, no em dashes.
- Under 120 words.
"""


def generate(context: str) -> str:
    """Send the context block to Ollama and return the briefing text.

    Fall back to the plain template from brief.py if Ollama is unreachable. A
    briefing that arrives unpolished beats one that does not arrive.
    """
    raise NotImplementedError("phase 7")
