"""The real provider against a mocked client: no key and no network."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import httpx2
import pytest

from screenguide.capture import Screenshot
from screenguide.contract import (
    ANSWER_SCHEMA,
    MESSAGE_REPLY_SCHEMA,
    RESPONSE_SCHEMA,
    parse_model_response,
)
from screenguide.detection import BBox, Element
from screenguide.grounding import MarkedScreen
from screenguide.model import (
    SYSTEM_PROMPT,
    AnthropicProvider,
    HistoryEntry,
    ModelProviderError,
    ModelRequest,
    Said,
    steps_shown,
)

REQUEST = ModelRequest(
    goal="Open settings",
    history=steps_shown(["Click File, top left."]),
    marked=MarkedScreen(
        image=Screenshot(data=b"png-bytes", width=200, height=100),
        elements={7: Element(7, "Settings", BBox(x=1, y=2, width=3, height=4), 0.9)},
    ),
)


def make_message(content=None, stop_reason="end_turn", stop_details=None, **usage):
    values = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        **usage,
    }
    return SimpleNamespace(
        content=content if content is not None else [SimpleNamespace(type="text", text="{}")],
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=SimpleNamespace(**values),
        _request_id="req_1",
    )


def make_provider(message=None, error=None, effort=None):
    client = MagicMock()
    client.messages.create.return_value = message or make_message()
    client.messages.create.side_effect = error
    return AnthropicProvider("claude-haiku-5-5", effort, client=client), client.messages.create


def status_error(cls, status):
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://api.test"))
    return cls("nope", response=response, body=None)


def test_request_carries_image_ids_goal_history_and_the_contract_schema():
    provider, create = make_provider()

    provider.complete(REQUEST)

    kwargs = create.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-5-5"
    assert kwargs["output_config"] == {"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}}
    image, text = kwargs["messages"][0]["content"]
    assert image["source"]["media_type"] == "image/png"
    assert base64.standard_b64decode(image["source"]["data"]) == b"png-bytes"
    assert "Open settings" in text["text"]
    assert "Click File, top left." in text["text"]
    assert "7: Settings" in text["text"]


def test_system_prompt_reaches_the_request_and_rules_out_stand_in_boxes():
    provider, create = make_provider()

    provider.complete(REQUEST)

    system = create.call_args.kwargs["system"]
    assert system == SYSTEM_PROMPT
    assert "element_id null, the icon's name in target_label" in system
    assert "Never use a nearby box as a stand-in" in system


def test_sampling_parameters_and_prefill_are_not_sent():
    provider, create = make_provider()

    provider.complete(REQUEST)

    kwargs = create.call_args.kwargs
    assert not {"temperature", "top_p", "top_k"} & kwargs.keys()
    assert kwargs["messages"][-1]["role"] == "user"


def test_effort_is_sent_only_when_configured():
    provider, create = make_provider(effort="low")
    provider.complete(REQUEST)
    assert create.call_args.kwargs["output_config"]["effort"] == "low"

    provider, create = make_provider()
    provider.complete(REQUEST)
    assert "effort" not in create.call_args.kwargs["output_config"]


def test_schema_requires_every_contract_field_and_allows_null_element_id():
    assert RESPONSE_SCHEMA["properties"]["element_id"]["type"] == ["integer", "null"]
    assert set(RESPONSE_SCHEMA["required"]) == set(RESPONSE_SCHEMA["properties"])
    assert RESPONSE_SCHEMA["additionalProperties"] is False
    sample = {
        "element_id": None,
        "target_label": None,
        "instruction": "All set.",
        "done": True,
        "confidence": 1,
    }
    assert parse_model_response(json.dumps(sample)).element_id is None


def test_usage_comes_from_the_response_and_image_tokens_stay_null():
    message = make_message(cache_creation_input_tokens=5, cache_read_input_tokens=6)
    provider, _ = make_provider(message)

    reply = provider.complete(REQUEST)

    assert (reply.input_tokens, reply.output_tokens) == (100, 20)
    assert (reply.cache_creation_input_tokens, reply.cache_read_input_tokens) == (5, 6)
    assert reply.image_tokens is None


def test_cache_tokens_the_api_does_not_report_stay_null():
    message = make_message(cache_creation_input_tokens=None, cache_read_input_tokens=None)
    provider, _ = make_provider(message)

    reply = provider.complete(REQUEST)

    assert reply.cache_creation_input_tokens is None
    assert reply.cache_read_input_tokens is None


def test_text_is_read_past_thinking_blocks():
    content = [
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="text", text='{"a": 1}'),
    ]
    provider, _ = make_provider(make_message(content))

    assert provider.complete(REQUEST).text == '{"a": 1}'


def test_reply_with_only_thinking_has_empty_text_and_keeps_the_stop_reason():
    content = [SimpleNamespace(type="thinking", thinking="")]
    provider, _ = make_provider(make_message(content, stop_reason="max_tokens"))

    reply = provider.complete(REQUEST)

    assert (reply.text, reply.stop_reason) == ("", "max_tokens")


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        (SimpleNamespace(category="cyber", explanation="Declined."), "cyber: Declined."),
        (SimpleNamespace(category=None, explanation=None), None),
        (None, None),
    ],
)
def test_refusal_keeps_its_category_and_explanation(details, expected):
    message = make_message([], stop_reason="refusal", stop_details=details)
    provider, _ = make_provider(message)

    reply = provider.complete(REQUEST)

    assert reply.stop_reason == "refusal"
    assert reply.refusal_detail == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (status_error(anthropic.AuthenticationError, 401), "ANTHROPIC_API_KEY"),
        (status_error(anthropic.RateLimitError, 429), "429"),
        (status_error(anthropic.BadRequestError, 400), "400"),
        (
            anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.test")),
            "could not reach",
        ),
        (TypeError("Could not resolve authentication method."), "ANTHROPIC_API_KEY"),
    ],
)
def test_api_failures_become_a_clear_provider_error(error, expected):
    provider, _ = make_provider(error=error)

    with pytest.raises(ModelProviderError, match=expected):
        provider.complete(REQUEST)


def test_unrelated_type_error_is_not_mistaken_for_missing_credentials():
    provider, _ = make_provider(error=TypeError("unexpected keyword"))

    with pytest.raises(TypeError):
        provider.complete(REQUEST)


def test_real_sdk_without_credentials_gives_the_credentials_help(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    provider = AnthropicProvider("claude-haiku-5-5")

    with pytest.raises(ModelProviderError, match="ANTHROPIC_API_KEY"):
        provider.complete(REQUEST)


def test_real_sdk_without_credentials_fails_the_check_without_a_request(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ModelProviderError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider("claude-haiku-5-5").check_credentials()


def test_real_sdk_with_a_key_passes_the_check(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    AnthropicProvider("claude-haiku-5-5").check_credentials()


MESSAGE_REQUEST = ModelRequest(
    goal=REQUEST.goal,
    history=(
        HistoryEntry(Said.STEP, "Click File, top left."),
        HistoryEntry(Said.MESSAGE, "Where is it?"),
        HistoryEntry(Said.ANSWER, "Above the toolbar."),
    ),
    marked=REQUEST.marked,
    message="I already did this",
)


def test_message_request_carries_the_message_and_allows_an_answer_or_a_step():
    provider, create = make_provider()

    provider.complete(MESSAGE_REQUEST)

    kwargs = create.call_args.kwargs
    schema = kwargs["output_config"]["format"]["schema"]
    assert schema == MESSAGE_REPLY_SCHEMA
    assert schema["anyOf"] == [ANSWER_SCHEMA, RESPONSE_SCHEMA]
    image, text = kwargs["messages"][0]["content"]
    assert image["type"] == "image"
    assert "I already did this" in text["text"]
    assert "language of the goal" in text["text"]


def test_history_marks_who_said_each_entry():
    provider, create = make_provider()

    provider.complete(MESSAGE_REQUEST)

    text = create.call_args.kwargs["messages"][0]["content"][1]["text"]
    assert "1. Step you gave: Click File, top left." in text
    assert "2. Message from the person: Where is it?" in text
    assert "3. Your answer: Above the toolbar." in text


def test_step_request_without_a_message_mentions_none():
    provider, create = make_provider()

    provider.complete(REQUEST)

    text = create.call_args.kwargs["messages"][0]["content"][1]["text"]
    assert "Message from the person" not in text


def test_system_prompt_says_to_check_the_screen_against_the_message():
    assert '{"answer": "..."}' in SYSTEM_PROMPT
    assert "does not show its result" in SYSTEM_PROMPT
    assert "instead of repeating the same instruction" in SYSTEM_PROMPT
    assert "Write it in the language of the goal" in SYSTEM_PROMPT
