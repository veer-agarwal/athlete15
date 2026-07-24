"""Unit tests for src/notify.py: send() escaping, <pre> wrapping, and chunking.

No network. requests.post is mocked. Token and chat id are monkeypatched since
config reads them from .env at import time, and neither is guaranteed to be set
in a test environment.

Run with:  pytest tests/test_notify.py
"""

import re
from unittest.mock import MagicMock, patch

import pytest

from src import config, notify


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "faketoken")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "12345")


def _ok_response() -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"ok": True}
    return response


# --- _split --------------------------------------------------------------------


def test_split_returns_single_chunk_under_the_limit():
    assert notify._split("short text", limit=100) == ["short text"]


def test_split_respects_the_reduced_pre_limit():
    """MAX_MESSAGE_CHARS - 11 (the <pre></pre> overhead), not the raw 4096."""
    limit = notify.MAX_MESSAGE_CHARS - notify._PRE_OVERHEAD
    assert limit == notify.MAX_MESSAGE_CHARS - 11

    text = "\n".join(f"line {i}" for i in range(2000))
    chunks = notify._split(text, limit=limit)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= limit


def test_split_hard_slices_a_single_line_longer_than_the_limit():
    line = "x" * 5000
    chunks = notify._split(line, limit=100)
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks) == line


# --- send(): escaping and wrapping ----------------------------------------------


@patch("src.notify.requests.post")
def test_send_escapes_ampersand_lt_gt(mock_post):
    mock_post.return_value = _ok_response()

    notify.send("A & B < C > D")

    payload = mock_post.call_args.kwargs["json"]
    assert "A &amp; B &lt; C &gt; D" in payload["text"]


@patch("src.notify.requests.post")
def test_send_does_not_escape_quotes(mock_post):
    mock_post.return_value = _ok_response()

    notify.send('he said "hello" and it\'s fine')

    payload = mock_post.call_args.kwargs["json"]
    assert '"hello"' in payload["text"]
    assert "it's fine" in payload["text"]


@patch("src.notify.requests.post")
def test_send_wraps_text_in_pre_tags(mock_post):
    mock_post.return_value = _ok_response()

    notify.send("hello world")

    payload = mock_post.call_args.kwargs["json"]
    assert payload["text"] == "<pre>hello world</pre>"
    assert payload["parse_mode"] == "HTML"


@patch("src.notify.requests.post")
def test_send_single_chunk_uses_configured_chat_id(mock_post):
    mock_post.return_value = _ok_response()

    notify.send("hello")

    payload = mock_post.call_args.kwargs["json"]
    assert payload["chat_id"] == "12345"


# --- send(): chunking a long message ---------------------------------------------


@patch("src.notify.requests.post")
def test_send_long_text_splits_into_multiple_posts(mock_post):
    mock_post.return_value = _ok_response()

    long_text = "\n".join(f"line number {i} of the briefing" for i in range(200))
    assert len(long_text) > 4085  # actually exercises the chunking path

    notify.send(long_text)

    assert mock_post.call_count > 1


@patch("src.notify.requests.post")
def test_send_every_chunk_is_individually_pre_wrapped_and_under_the_limit(mock_post):
    mock_post.return_value = _ok_response()

    long_text = "\n".join(f"line number {i} of the briefing" for i in range(200))
    notify.send(long_text)

    for call in mock_post.call_args_list:
        text = call.kwargs["json"]["text"]
        assert text.startswith("<pre>")
        assert text.endswith("</pre>")
        # exactly one open and one close tag: no chunk shares a tag with another
        assert text.count("<pre>") == 1
        assert text.count("</pre>") == 1
        assert len(text) <= notify.MAX_MESSAGE_CHARS


@patch("src.notify.requests.post")
def test_send_escaping_happens_before_chunking(mock_post):
    """A run of & characters near a chunk boundary must not push that chunk's
    escaped length over the limit unaccounted for."""
    mock_post.return_value = _ok_response()

    long_text = "\n".join(f"a & b & c line {i}" for i in range(400))
    notify.send(long_text)

    for call in mock_post.call_args_list:
        assert len(call.kwargs["json"]["text"]) <= notify.MAX_MESSAGE_CHARS


# --- send(): missing configuration ------------------------------------------------


def test_send_raises_when_token_missing(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        notify.send("hello")


def test_send_raises_when_chat_id_missing(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    with pytest.raises(RuntimeError, match="TELEGRAM_CHAT_ID"):
        notify.send("hello")


@patch("src.notify.requests.post")
def test_send_raises_on_telegram_error_body(mock_post):
    response = MagicMock()
    response.json.return_value = {
        "ok": False, "error_code": 400, "description": "chat not found",
    }
    mock_post.return_value = response

    with pytest.raises(RuntimeError, match="chat not found"):
        notify.send("hello")
