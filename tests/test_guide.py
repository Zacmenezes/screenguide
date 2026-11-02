"""The multi-step loop: `screenguide guide` with every layer stubbed except the session."""

import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from screenguide.capture import Screenshot
from screenguide.cli import main
from screenguide.model import AnthropicProvider, ModelProviderError, ModelReply, steps_shown
from screenguide.monitors import MonitorChoice


def reply(label, done=False):
    return json.dumps(
        {
            "element_id": None if done else 2,
            "target_label": None if done else label,
            "instruction": f"{label}, in the middle of the menu.",
            "done": done,
            "confidence": 0.9,
        }
    )


STEPS = [reply("First"), reply("Second"), reply("All set", done=True)]


class ScriptedProvider:
    """Plays one reply per call, keeps each request, and raises any exception it is scripted."""

    name = "scripted"
    model = "scripted-1"
    external = False

    def __init__(self, *replies):
        self.requests = []
        self._replies = replies

    def complete(self, request):
        self.requests.append(request)
        played = self._replies[len(self.requests) - 1]
        if isinstance(played, Exception):
            raise played
        return ModelReply(text=played, input_tokens=10, output_tokens=5)


class CountingCapturer:
    """Hands out a different screenshot on each capture, so a resent one would be visible."""

    def __init__(self, source):
        self.captures = 0
        self._source = source

    def capture(self):
        self.captures += 1
        image = Image.open(self._source).convert("RGB")
        image.putpixel((0, 0), (self.captures, 0, 0))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return Screenshot(data=buffer.getvalue())


@pytest.fixture
def capturer(image, monkeypatch):
    capturer = CountingCapturer(image)
    monkeypatch.setattr("screenguide.cli.PortalCapturer", lambda **kwargs: capturer)
    return capturer


def guide(monkeypatch, provider, keys="\n\n"):
    monkeypatch.setattr("screenguide.cli.AnthropicProvider", lambda model, effort: provider)
    monkeypatch.setattr("sys.stdin", io.StringIO(keys))
    return main(["guide", "--goal", "Open settings"])


def read_records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_run_loops_until_done_with_one_capture_per_step(monkeypatch, capsys, capturer):
    provider = ScriptedProvider(*STEPS)

    # More keys than steps: the loop has to stop on `done`, not on running out of input.
    assert guide(monkeypatch, provider, keys="\n\n\n\n") == 0

    out = capsys.readouterr().out
    assert "Step 1\ntarget_label: First" in out
    assert "Step 2\ntarget_label: Second" in out
    assert "Step 3\ntarget_label: null" in out
    assert "done: true" in out
    assert capturer.captures == 3
    assert len(provider.requests) == 3


def test_no_prompt_follows_the_done_step(monkeypatch, capsys, capturer):
    guide(monkeypatch, ScriptedProvider(reply("All set", done=True)))

    assert "press Enter" not in capsys.readouterr().out


@pytest.mark.parametrize("keys", ["q\n", " Q \n", ""], ids=["q", "padded-upper-q", "closed-input"])
def test_quitting_stops_before_the_next_capture(monkeypatch, capturer, keys):
    provider = ScriptedProvider(*STEPS)

    assert guide(monkeypatch, provider, keys=keys) == 0

    assert capturer.captures == 1
    assert len(provider.requests) == 1


def test_ctrl_c_at_the_prompt_stops_the_run(monkeypatch, capturer):
    def interrupted(prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupted)

    assert guide(monkeypatch, ScriptedProvider(*STEPS)) == 130
    assert capturer.captures == 1


def test_history_is_the_earlier_instructions_as_text_with_only_the_current_screen(
    monkeypatch, capturer
):
    provider = ScriptedProvider(*STEPS)

    guide(monkeypatch, provider)

    first, second, third = provider.requests
    assert first.history == ()
    assert second.history == steps_shown(["First, in the middle of the menu."])
    assert third.history == steps_shown(
        ["First, in the middle of the menu.", "Second, in the middle of the menu."]
    )
    assert {request.goal for request in provider.requests} == {"Open settings"}
    assert len({request.marked.image.data for request in provider.requests}) == 3


def test_api_request_carries_one_image_per_step_however_long_the_history(monkeypatch, capturer):
    client = MagicMock()
    client.messages.create.side_effect = [
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason="end_turn",
            stop_details=None,
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            _request_id="req",
        )
        for text in STEPS
    ]

    guide(monkeypatch, AnthropicProvider("claude-haiku-5-5", client=client))

    for call in client.messages.create.call_args_list:
        messages = call.kwargs["messages"]
        assert len(messages) == 1
        assert [block["type"] for block in messages[0]["content"]] == ["image", "text"]
    last_text = client.messages.create.call_args.kwargs["messages"][0]["content"][1]["text"]
    assert "1. Step you gave: First, in the middle of the menu." in last_text
    assert "2. Step you gave: Second, in the middle of the menu." in last_text


def test_every_usage_record_of_a_run_shares_one_session_id(monkeypatch, usage_log, capturer):
    guide(monkeypatch, ScriptedProvider(*STEPS))
    guide(monkeypatch, ScriptedProvider(*STEPS))

    ids = [record["session_id"] for record in read_records(usage_log)]
    assert len(ids) == 6
    assert len(set(ids[:3])) == 1
    assert len(set(ids[3:])) == 1
    assert ids[0] != ids[3]


def test_retry_within_a_run_stays_in_the_session_and_adds_no_history(
    monkeypatch, usage_log, capturer
):
    provider = ScriptedProvider(STEPS[0], "not json", STEPS[1], STEPS[2])

    assert guide(monkeypatch, provider) == 0

    records = read_records(usage_log)
    assert [record["attempt"] for record in records] == [1, 1, 2, 1]
    assert len({record["session_id"] for record in records}) == 1
    assert len(provider.requests[3].history) == 2
    assert capturer.captures == 3


def test_provider_error_midway_ends_the_run_and_keeps_what_was_recorded(
    monkeypatch, usage_log, capsys, capturer
):
    provider = ScriptedProvider(STEPS[0], ModelProviderError("Anthropic API error 500: boom"))

    assert guide(monkeypatch, provider) == 1

    captured = capsys.readouterr()
    assert "target_label: First" in captured.out
    assert "error: Anthropic API error 500: boom" in captured.err
    assert len(read_records(usage_log)) == 1


def test_monitor_option_reaches_the_capturer(image, monkeypatch):
    seen = []

    def portal(**kwargs):
        seen.append(kwargs["choose_monitor"]())
        return CountingCapturer(image)

    monkeypatch.setattr("screenguide.cli.PortalCapturer", portal)
    monkeypatch.setattr(
        "screenguide.cli.AnthropicProvider",
        lambda model, effort: ScriptedProvider(reply("All set", done=True)),
    )

    assert main(["guide", "--goal", "Open settings", "--monitor", "DP-3"]) == 0
    assert seen == [MonitorChoice("DP-3")]
