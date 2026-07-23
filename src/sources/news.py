"""News headlines via RSS.

YOUR TURN. weather.py is the worked example. Build this one yourself.

Use the feedparser library (already in requirements.txt). It handles both RSS and
Atom, so you do not need to care which format a given site publishes.

    import feedparser
    parsed = feedparser.parse(url)
    parsed.entries          # list of entries
    entry.title
    entry.link
    entry.published_parsed  # a time.struct_time, or missing entirely

Things to work out as you go:

1. config.NEWS_FEEDS is a list. You need headlines from all of them, interleaved
   or concatenated, capped at config.NEWS_MAX_ITEMS total.
2. Some entries have no published date. Decide what to do rather than crashing.
3. feedparser does not raise on a dead feed. It sets parsed.bozo. Check it.
4. Return the same shape every source returns: list[dict].

Suggested dict keys: title, link, source, published.
"""

from src import config


def fetch() -> list[dict]:
    """Return recent headlines across all configured feeds.

    Returns:
        A list of dicts with keys: title, link, source, published.
    """
    raise NotImplementedError("phase 1: write this one yourself")


def format_lines(items: list[dict]) -> list[str]:
    """Return one short string per headline for the briefing."""
    raise NotImplementedError


if __name__ == "__main__":
    for item in fetch():
        print(item)
