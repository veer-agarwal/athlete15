"""Unit tests for src/llm.py: think-tag stripping and generate_sleep_note.

No network. requests.post is mocked with unittest.mock.patch so the HTTP call
itself is never made.

Run with:  pytest tests/test_llm.py
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from src import config, llm


# --- _strip_think ------------------------------------------------------------------


def test_closed_think_block_is_removed():
    text = "<think>reasoning about the numbers</think>Recovery is 72 percent."
    assert llm._strip_think(text) == "Recovery is 72 percent."


def test_multiline_think_content_is_removed():
    """The regex must use DOTALL so newlines inside the block do not stop it."""
    text = "<think>\nline one\nline two\n</think>\nSleep was 7h 40m."
    assert llm._strip_think(text) == "\nSleep was 7h 40m."


def test_multiple_think_blocks_all_removed():
    text = "<think>a</think>Start. <think>b\nc</think>End."
    assert llm._strip_think(text) == "Start. End."


def test_unclosed_think_truncates_everything_from_the_tag_onward():
    """Ran out of tokens mid-reasoning: nothing after the tag is briefing text."""
    text = "Recovery is 72 percent. <think>still reasoning and never stops"
    assert llm._strip_think(text) == "Recovery is 72 percent. "


def test_unclosed_think_at_the_very_start_leaves_nothing():
    text = "<think>never closes at all"
    assert llm._strip_think(text) == ""


def test_text_with_no_think_tags_passes_through_unchanged():
    text = "Recovery is 72 percent. Sleep was 7h 40m."
    assert llm._strip_think(text) == text


def test_empty_string_passes_through():
    assert llm._strip_think("") == ""


# --- generate_sleep_note: request shape -----------------------------------------


def _fake_response(body: dict) -> MagicMock:
    response = MagicMock()
    response.json.return_value = body
    response.raise_for_status.return_value = None
    return response


@patch("src.llm.requests.post")
def test_posts_to_the_configured_url(mock_post):
    mock_post.return_value = _fake_response({"response": "Sleep and recovery were both about average."})

    llm.generate_sleep_note(6.2, 54, 6.5, 60)

    args, kwargs = mock_post.call_args
    assert args[0] == f"{config.OLLAMA_URL}/api/generate"


@patch("src.llm.requests.post")
def test_request_body_shape(mock_post):
    mock_post.return_value = _fake_response({"response": "Slightly below your usual recovery."})

    llm.generate_sleep_note(6.2, 54, 6.5, 60)

    _, kwargs = mock_post.call_args
    body = kwargs["json"]
    assert body["model"] == config.OLLAMA_MODEL
    assert body["system"] == llm.SLEEP_NOTE_SYSTEM
    assert body["think"] is False
    assert body["stream"] is False
    assert body["options"] == {"num_ctx": 2048, "temperature": 0.3}
    assert kwargs["timeout"] == 120


@patch("src.llm.requests.post")
def test_prompt_contains_the_four_formatted_numbers(mock_post):
    mock_post.return_value = _fake_response({"response": "ok sentence here"})

    llm.generate_sleep_note(6.25, 54.4, 6.567, 60.1)

    _, kwargs = mock_post.call_args
    prompt = kwargs["json"]["prompt"]
    # sleep hours: .1f, recovery: .0f
    assert "6.2" in prompt
    assert "54" in prompt
    assert "6.6" in prompt
    assert "60" in prompt


# --- generate_sleep_note: response handling -------------------------------------


@patch("src.llm.requests.post")
def test_returns_whitespace_collapsed_one_line(mock_post):
    mock_post.return_value = _fake_response(
        {"response": "  <think>reasoning</think>Recovery\nis   72\npercent.  \n"}
    )

    assert llm.generate_sleep_note(6.2, 54, 6.5, 60) == "Recovery is 72 percent."


@patch("src.llm.requests.post")
def test_request_exception_returns_none(mock_post):
    mock_post.side_effect = requests.ConnectionError("no route to host")

    assert llm.generate_sleep_note(6.2, 54, 6.5, 60) is None


@patch("src.llm.requests.post")
def test_non_2xx_returns_none(mock_post):
    response = _fake_response({"response": "ok"})
    response.raise_for_status.side_effect = requests.HTTPError("500 server error")
    mock_post.return_value = response

    assert llm.generate_sleep_note(6.2, 54, 6.5, 60) is None


@patch("src.llm.requests.post")
def test_non_json_body_returns_none(mock_post):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.side_effect = ValueError("not json")
    mock_post.return_value = response

    assert llm.generate_sleep_note(6.2, 54, 6.5, 60) is None


@patch("src.llm.requests.post")
def test_empty_response_returns_none(mock_post):
    mock_post.return_value = _fake_response({"response": ""})

    assert llm.generate_sleep_note(6.2, 54, 6.5, 60) is None


@patch("src.llm.requests.post")
def test_whitespace_only_response_returns_none(mock_post):
    mock_post.return_value = _fake_response({"response": "   \n  \t  "})

    assert llm.generate_sleep_note(6.2, 54, 6.5, 60) is None


@patch("src.llm.requests.post")
def test_missing_response_key_returns_none(mock_post):
    mock_post.return_value = _fake_response({})

    assert llm.generate_sleep_note(6.2, 54, 6.5, 60) is None


@patch("src.llm.requests.post")
def test_response_over_max_words_returns_none(mock_post):
    over_limit = " ".join(["word"] * (llm.MAX_NOTE_WORDS + 1))
    mock_post.return_value = _fake_response({"response": over_limit})

    assert llm.generate_sleep_note(6.2, 54, 6.5, 60) is None


@patch("src.llm.requests.post")
def test_response_at_exactly_max_words_is_accepted(mock_post):
    at_limit = " ".join(["word"] * llm.MAX_NOTE_WORDS)
    mock_post.return_value = _fake_response({"response": at_limit})

    result = llm.generate_sleep_note(6.2, 54, 6.5, 60)
    assert result == at_limit
