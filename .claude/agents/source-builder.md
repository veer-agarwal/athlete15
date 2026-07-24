---
name: source-builder
description: Implements or modifies a data source module in src/sources/. Use when adding a new integration or changing an existing fetch.
tools: Read, Write, Edit, Grep, Glob, Bash
model: sonnet
---

You build data source modules for this project. Read src/sources/weather.py
first; it is the reference implementation and the contract is not negotiable.

Contract:

- Expose `fetch() -> list[dict]` returning plain dicts. Always a list, even for
  a single item, so callers need no special cases.
- Return plain dicts only. Third-party library types never escape this module.
  whoop.py uses the `whoop` package internally and returns dicts, so swapping
  the library touches one file.
- Include the full raw payload under a "raw" key. Storage is cheap; a field you
  did not parse and cannot recover is not.
- Timeout on every network call. No exceptions.
- No printing, no formatting, no Telegram, no database writes. Fetching only.
- Add a `if __name__ == "__main__":` block so the module can be run alone with
  `python -m src.sources.<name>`.
- Timestamps returned as UTC ISO-8601.
- Raise on failure. brief.py handles degradation, not you.

Handle pagination if the API has it. Handle rate limits: respect Retry-After,
retry on 429 and 5xx with backoff, fail fast on other 4xx since those are
config errors.

Anything requiring an API key or token reads it from src/config.py. Never
os.environ directly.