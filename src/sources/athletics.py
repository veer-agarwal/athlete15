"""NYU Athletics schedule via iCal. Phase 8, build around November.

Men's volleyball is a spring sport. Through the summer and most of the fall this
feed will be empty or stale, which makes it impossible to test against. That is why
it is last. Facility training and captain's practices are logged manually through
training.py in the meantime.

Finding the feed: on the team schedule page, look for a subscribe or calendar icon.
If there is none, open browser dev tools, click any "add to calendar" control, and
watch the network tab for a request ending in .ics or containing format=ical.

Use the icalendar library rather than parsing by hand. RFC 5545 has line folding,
escaped characters, timezone parameters, and recurrence rules, and hand rolled
parsers get all four wrong eventually.
"""

from src import config


def fetch() -> list[dict]:
    """Return upcoming events from the athletics calendar.

    Returns dicts with keys: uid, summary, location, start_utc, end_utc.
    """
    raise NotImplementedError("phase 8")
