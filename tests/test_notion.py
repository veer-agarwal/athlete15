"""Unit tests for the Notion property extractors and page parsing.

No network. The realistic payload lives in tests/fixtures/notion_page.json and
was captured from a live query of the actual coursework database, so the nesting
here is the nesting Notion really sends, not a guess at it. The edge case
payloads are constructed by hand to match the documented shapes.

Run with:  pytest tests/test_notion.py
"""

import json
from pathlib import Path

import pytest

from src.sources import notion

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "notion_page.json"


@pytest.fixture
def real_page() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


# --- extractors against the real captured payload ---------------------------------


def test_real_page_parses_end_to_end(real_page):
    task = notion._parse_page(real_page)
    assert task["title"] == "Pset 3, op-amp circuits"
    assert task["course"] == "ECE-UY 2004"
    assert task["due_date"] == "2026-07-24"
    assert task["status"] == "In progress"
    assert task["type"] == "pset"
    assert task["id"] == real_page["id"]
    assert task["url"] == real_page["url"]
    assert task["raw"] is real_page


def test_title_from_real_payload(real_page):
    assert notion.extract_title(real_page["properties"]["Name"]) == (
        "Pset 3, op-amp circuits"
    )


# --- title ------------------------------------------------------------------------


def test_multi_part_title_is_joined():
    """Notion splits a title on every formatting change. One bolded word in the
    middle arrives as three fragments, and dropping any of them corrupts the
    visible task name."""
    prop = {
        "type": "title",
        "title": [
            {"type": "text", "plain_text": "Pset 3, due "},
            {"type": "text", "plain_text": "Friday", "annotations": {"bold": True}},
            {"type": "text", "plain_text": " in class"},
        ],
    }
    assert notion.extract_title(prop) == "Pset 3, due Friday in class"


def test_empty_title_is_empty_string_not_none():
    """Callers concatenate the title into a display line, so '' beats None."""
    assert notion.extract_title({"type": "title", "title": []}) == ""


def test_missing_title_property_entirely():
    assert notion.extract_title(None) == ""


# --- select and status ------------------------------------------------------------


def test_empty_select_is_none():
    """A task with no course set sends select: null, not an absent key."""
    assert notion.extract_select({"type": "select", "select": None}) is None


def test_select_with_value():
    prop = {"type": "select", "select": {"id": "abc", "name": "ECE-UY 2004", "color": "blue"}}
    assert notion.extract_select(prop) == "ECE-UY 2004"


def test_empty_status_is_none():
    assert notion.extract_status({"type": "status", "status": None}) is None


def test_status_with_value():
    prop = {"type": "status", "status": {"id": "x", "name": "In progress", "color": "blue"}}
    assert notion.extract_status(prop) == "In progress"


# --- date -------------------------------------------------------------------------


def test_empty_date_is_none_not_keyerror():
    """The spec case: a task with no Due date returns None."""
    assert notion.extract_date({"type": "date", "date": None}) is None


def test_date_only():
    prop = {"type": "date", "date": {"start": "2026-07-29", "end": None, "time_zone": None}}
    assert notion.extract_date(prop) == "2026-07-29"


def test_date_with_time_keeps_the_full_string():
    prop = {"type": "date", "date": {"start": "2026-07-29T17:00:00.000-04:00", "end": None}}
    assert notion.extract_date(prop) == "2026-07-29T17:00:00.000-04:00"


# --- rich text --------------------------------------------------------------------


def test_rich_text_joined():
    prop = {
        "type": "rich_text",
        "rich_text": [
            {"plain_text": "see "},
            {"plain_text": "lecture 12"},
        ],
    }
    assert notion.extract_rich_text(prop) == "see lecture 12"


def test_empty_rich_text_is_none():
    assert notion.extract_rich_text({"type": "rich_text", "rich_text": []}) is None


# --- the all-null task ------------------------------------------------------------


def test_task_with_every_property_null(real_page):
    """Every value empty and every extractor still answers. Built from the real
    page so the property ids and types stay authentic; only the values are
    blanked, which is exactly what Notion sends for a row created empty."""
    page = json.loads(json.dumps(real_page))  # deep copy, fixture stays pristine
    page["properties"]["Name"]["title"] = []
    page["properties"]["Course"]["select"] = None
    page["properties"]["Due"]["date"] = None
    page["properties"]["Status"]["status"] = None
    page["properties"]["Type"]["select"] = None

    task = notion._parse_page(page)
    assert task["title"] == ""
    assert task["course"] is None
    assert task["due_date"] is None
    assert task["status"] is None
    assert task["type"] is None
    assert task["id"] == page["id"]


def test_page_with_no_properties_at_all():
    """Beyond null values: the keys themselves absent. Never a KeyError."""
    task = notion._parse_page({"id": "deadbeef", "url": None, "properties": {}})
    assert task["title"] == ""
    assert task["course"] is None
    assert task["due_date"] is None
    assert task["status"] is None
    assert task["type"] is None


# --- pagination and ordering (mocked _api, still no network) ----------------------


def _page_named(name: str, due: str | None) -> dict:
    return {
        "id": f"id-{name}",
        "url": None,
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": name}]},
            "Due": {"type": "date", "date": {"start": due} if due else None},
        },
    }


def test_fetch_follows_the_cursor(monkeypatch):
    """Two pages of results must both land, with the cursor echoed back."""
    calls = []

    def fake_api(method, path, payload=None):
        calls.append(dict(payload))
        if payload.get("start_cursor"):
            return {"results": [_page_named("second", "2026-08-01")], "has_more": False}
        return {
            "results": [_page_named("first", "2026-07-25")],
            "has_more": True,
            "next_cursor": "cursor-1",
        }

    monkeypatch.setattr(notion, "_api", fake_api)
    monkeypatch.setattr(notion, "_data_source_id", lambda: "ds-1")

    tasks = notion.fetch()
    assert [t["title"] for t in tasks] == ["first", "second"]
    assert calls[1]["start_cursor"] == "cursor-1"
    # filter and sorts must survive into the second request
    assert calls[1]["filter"] == calls[0]["filter"]


def test_fetch_sorts_dated_ascending_undated_last(monkeypatch):
    pages = [
        _page_named("undated", None),
        _page_named("late", "2026-08-03"),
        _page_named("soon", "2026-07-25"),
    ]
    monkeypatch.setattr(
        notion, "_api", lambda *a, **k: {"results": pages, "has_more": False}
    )
    monkeypatch.setattr(notion, "_data_source_id", lambda: "ds-1")

    tasks = notion.fetch()
    assert [t["title"] for t in tasks] == ["soon", "late", "undated"]


# --- retry policy (mocked requests, still no network) -----------------------------


class _FakeResponse:
    def __init__(self, status: int, body: dict | None = None, headers: dict | None = None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def test_429_waits_what_retry_after_says(monkeypatch):
    responses = iter([
        _FakeResponse(429, headers={"Retry-After": "3"}),
        _FakeResponse(200, body={"ok": True}),
    ])
    sleeps = []
    monkeypatch.setattr(notion.requests, "request", lambda *a, **k: next(responses))
    monkeypatch.setattr(notion.time, "sleep", sleeps.append)

    assert notion._api("POST", "/x") == {"ok": True}
    assert sleeps == [3.0]


def test_5xx_retries_then_succeeds(monkeypatch):
    responses = iter([
        _FakeResponse(502),
        _FakeResponse(502),
        _FakeResponse(200, body={"ok": True}),
    ])
    monkeypatch.setattr(notion.requests, "request", lambda *a, **k: next(responses))
    monkeypatch.setattr(notion.time, "sleep", lambda _s: None)

    assert notion._api("POST", "/x") == {"ok": True}


def test_5xx_exhausted_raises(monkeypatch):
    monkeypatch.setattr(notion.requests, "request", lambda *a, **k: _FakeResponse(503))
    monkeypatch.setattr(notion.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError, match="server error 503"):
        notion._api("POST", "/x")


def test_4xx_fails_fast_without_retry(monkeypatch):
    """A config error retried three times is three times the wait for the same
    answer. One attempt, no sleep."""
    attempts = []

    def fake_request(*a, **k):
        attempts.append(1)
        return _FakeResponse(400, body={"code": "validation_error", "message": "bad filter"})

    sleeps = []
    monkeypatch.setattr(notion.requests, "request", fake_request)
    monkeypatch.setattr(notion.time, "sleep", sleeps.append)

    with pytest.raises(RuntimeError, match="validation_error"):
        notion._api("POST", "/x")
    assert len(attempts) == 1
    assert sleeps == []


def test_404_names_the_sharing_problem(monkeypatch):
    """The 404 message must point at Connections, because that is what a Notion
    404 nearly always means and 'not found' sends you hunting a typo instead."""
    monkeypatch.setattr(notion.requests, "request", lambda *a, **k: _FakeResponse(404, body={}))

    with pytest.raises(RuntimeError, match="not shared with the integration"):
        notion._api("GET", "/databases/whatever")
