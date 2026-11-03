import json

import pytest

from conftest import FixedDetector
from screenguide.capture import FileCapturer
from screenguide.cli import main
from screenguide.contract import InvalidModelResponse, ModelRefusal
from screenguide.detection import BBox
from screenguide.grounding import SetOfMarkGrounding
from screenguide.model import HistoryEntry, ModelProviderError, ModelReply, ModelRequest, Said
from screenguide.pricing import PricingTable
from screenguide.session import Answer, GuideSession, Step
from screenguide.usage import JsonlUsageTracker


class FixedProvider:
    name = "fixed"
    model = "fixed-1"
    external = False

    def __init__(self, element_id=2, text=None, input_tokens=None):
        self.requests: list[ModelRequest] = []
        self._input_tokens = input_tokens
        self._text = text or json.dumps(
            {
                "element_id": element_id,
                "target_label": "Help",
                "instruction": "Click Help, at the right of the menu.",
                "done": False,
                "confidence": 1,
            }
        )

    def complete(self, request):
        self.requests.append(request)
        return ModelReply(text=self._text, input_tokens=self._input_tokens)


class ScriptedProvider:
    """Plays one reply per call."""

    name = "scripted"
    model = "scripted-1"
    external = True

    def __init__(self, *replies):
        self.calls = 0
        self._replies = replies

    def complete(self, request):
        self.calls += 1
        return self._replies[self.calls - 1]


GOOD = ModelReply(
    text=json.dumps(
        {
            "element_id": 2,
            "target_label": "Settings",
            "instruction": "Click Settings, in the middle of the menu.",
            "done": False,
            "confidence": 0.9,
        }
    ),
    input_tokens=10,
    output_tokens=5,
    stop_reason="end_turn",
)


def make_session(image, usage_log, provider, **kwargs):
    return GuideSession(
        goal="Open settings",
        capturer=FileCapturer(image),
        detector=FixedDetector(),
        grounding=SetOfMarkGrounding(),
        provider=provider,
        tracker=JsonlUsageTracker(usage_log, PricingTable({})),
        **kwargs,
    )


def read_records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_cli_crosses_every_layer_and_prints_the_step(image, usage_log, capsys):
    assert main(["run", "--image", str(image), "--goal", "Open settings"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "target_label: File",
        "instruction: Click File, the first text on the screen.",
        "bbox: x=10 y=8 width=40 height=20",
        "done: false",
        "confidence: 0.8",
    ]


def test_tokens_the_provider_does_not_report_are_logged_as_null(image, usage_log):
    make_session(image, usage_log, FixedProvider()).next_step()

    record = read_records(usage_log)[0]
    assert record["input_tokens"] is None
    assert record["output_tokens"] is None


def test_cli_appends_one_usage_record_per_run_with_unknowns_as_null(image, usage_log):
    args = ["run", "--image", str(image), "--goal", "Open settings"]
    main(args)
    main(args)

    records = read_records(usage_log)
    assert len(records) == 2
    record = records[0]
    assert (record["provider"], record["model"]) == ("stub", "stub")
    assert record["input_tokens"] == 1000
    assert record["output_tokens"] == 100
    assert record["image_width"] == 200
    assert record["image_height"] == 100
    assert record["cost_usd"] is None
    assert record["latency_ms"] >= 0
    assert record["timestamp"]
    assert record["session_id"]


def test_stub_provider_points_at_nothing_when_no_text_was_found(image, usage_log, monkeypatch):
    monkeypatch.setattr("screenguide.cli.RapidOcrDetector", lambda: FixedDetector(()))

    assert main(["run", "--image", str(image), "--goal", "Open settings"]) == 0


def test_cli_reports_a_missing_image(tmp_path, usage_log, capsys):
    code = main(["run", "--image", str(tmp_path / "absent.png"), "--goal", "Open settings"])

    assert code == 1
    assert "absent.png" in capsys.readouterr().err
    assert not usage_log.exists()


def test_session_sends_goal_and_numbered_boxes_and_resolves_the_chosen_one(image, usage_log):
    provider = FixedProvider(element_id=3, input_tokens=120)

    step = make_session(image, usage_log, provider).next_step()

    request = provider.requests[0]
    assert request.goal == "Open settings"
    assert list(request.marked.elements) == [1, 2, 3]
    assert request.marked.image.data != image.read_bytes()
    assert "2: Settings" in request.marked.id_list
    assert step.bbox == BBox(x=140, y=8, width=40, height=20)
    assert read_records(usage_log)[0]["input_tokens"] == 120


def test_null_element_id_gives_a_step_without_bbox(image, usage_log):
    text = json.dumps(
        {
            "element_id": None,
            "target_label": "Gear icon",
            "instruction": "Click the gear icon, top right.",
            "done": False,
            "confidence": 0.4,
        }
    )

    step = make_session(image, usage_log, FixedProvider(text=text)).next_step()

    assert step.bbox is None
    assert step.instruction == "Click the gear icon, top right."


@pytest.mark.parametrize("provider", [FixedProvider(text="not json"), FixedProvider(element_id=99)])
def test_invalid_reply_raises_and_is_still_recorded(image, usage_log, provider):
    session = make_session(image, usage_log, provider)

    with pytest.raises(InvalidModelResponse):
        session.next_step()

    assert [record["attempt"] for record in read_records(usage_log)] == [1, 2]
    assert session.history == []


@pytest.mark.parametrize(
    "bad",
    [
        ModelReply(text="not json"),
        ModelReply(text='{"element_id": 1, "tar', stop_reason="max_tokens"),
        ModelReply(text=GOOD.text, stop_reason="max_tokens"),
        ModelReply(text=GOOD.text.replace('"element_id": 2', '"element_id": 99')),
    ],
)
def test_bad_reply_gets_one_retry_and_each_attempt_is_recorded(image, usage_log, bad):
    provider = ScriptedProvider(bad, GOOD)

    step = make_session(image, usage_log, provider).next_step()

    assert step.target_label == "Settings"
    assert provider.calls == 2
    assert [record["attempt"] for record in read_records(usage_log)] == [1, 2]


def test_retry_does_not_capture_the_screen_again(image, usage_log):
    session = make_session(image, usage_log, ScriptedProvider(ModelReply(text="no"), GOOD))
    captures = []
    capture = session._capturer.capture
    session._capturer.capture = lambda: captures.append(1) or capture()

    session.next_step()

    assert len(captures) == 1


def test_refusal_is_not_retried_and_keeps_its_reason(image, usage_log):
    refusal = ModelReply(text="", stop_reason="refusal", refusal_detail="cyber: declined")
    provider = ScriptedProvider(refusal, GOOD)

    with pytest.raises(ModelRefusal, match="cyber: declined"):
        make_session(image, usage_log, provider).next_step()

    assert provider.calls == 1
    assert len(read_records(usage_log)) == 1


def test_usage_record_keeps_every_reported_field_and_the_sent_resolution(image, usage_log):
    reply = ModelReply(
        text=GOOD.text,
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=2,
    )

    make_session(image, usage_log, ScriptedProvider(reply)).next_step()

    record = read_records(usage_log)[0]
    assert record["cache_creation_input_tokens"] == 3
    assert record["cache_read_input_tokens"] == 2
    assert record["image_tokens"] is None
    assert (record["image_width"], record["image_height"]) == (200, 100)
    assert record["latency_ms"] >= 0


def test_user_is_told_before_each_send_to_an_external_provider(image, usage_log):
    events = []

    class Provider(ScriptedProvider):
        def complete(self, request):
            events.append("send")
            return super().complete(request)

    session = make_session(
        image, usage_log, Provider(ModelReply(text="no"), GOOD), notify=events.append
    )
    session.next_step()

    assert [event == "send" for event in events] == [False, True, False, True]
    assert "external provider scripted" in events[0]


def test_local_provider_sends_no_notice(image, usage_log):
    events = []

    make_session(image, usage_log, FixedProvider(), notify=events.append).next_step()

    assert events == []


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (ModelReply(text="", stop_reason="refusal"), "Try another model"),
        (ModelReply(text="no"), "after one retry"),
    ],
)
def test_cli_reports_refusal_and_invalid_reply(image, capsys, monkeypatch, reply, expected):
    monkeypatch.setattr(
        "screenguide.cli.AnthropicProvider", lambda model, effort: ScriptedProvider(reply, reply)
    )

    assert main(["run", "--image", str(image), "--goal", "Open settings"]) == 1
    assert expected in capsys.readouterr().err


def test_cli_reports_a_provider_error(image, capsys, monkeypatch):
    class Failing(ScriptedProvider):
        def complete(self, request):
            raise ModelProviderError("Anthropic credentials: no credentials found.")

    monkeypatch.setattr("screenguide.cli.AnthropicProvider", lambda model, effort: Failing())

    assert main(["run", "--image", str(image), "--goal", "Open settings"]) == 1
    assert "no credentials found" in capsys.readouterr().err


def test_cli_passes_model_and_effort_from_config(image, monkeypatch):
    seen = []
    monkeypatch.setenv("SCREENGUIDE_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("SCREENGUIDE_EFFORT", "low")

    def provider(model, effort):
        seen.append((model, effort))
        return ScriptedProvider(GOOD)

    monkeypatch.setattr("screenguide.cli.AnthropicProvider", provider)

    assert main(["run", "--image", str(image), "--goal", "Open settings"]) == 0
    assert seen == [("claude-sonnet-5-5", "low")]


ANSWER = ModelReply(text=json.dumps({"answer": "It opens the settings page."}), input_tokens=8)
CORRECTED = ModelReply(
    text=json.dumps(
        {
            "element_id": 1,
            "target_label": "File",
            "instruction": "The File menu is closed: click File again, at the top left.",
            "done": False,
            "confidence": 0.7,
        }
    )
)


class RecordingProvider(ScriptedProvider):
    def __init__(self, *replies):
        super().__init__(*replies)
        self.requests: list[ModelRequest] = []

    def complete(self, request):
        self.requests.append(request)
        return super().complete(request)


def counting_captures(session):
    captures = []
    capture = session._capturer.capture
    session._capturer.capture = lambda: captures.append(1) or capture()
    return captures


def test_message_goes_out_with_goal_history_and_a_fresh_capture(image, usage_log):
    provider = RecordingProvider(GOOD, ANSWER)
    session = make_session(image, usage_log, provider)
    captures = counting_captures(session)
    session.next_step()

    session.ask("What does this button do?")

    assert len(captures) == 2
    first, second = provider.requests
    assert first.message is None
    assert second.message == "What does this button do?"
    assert second.goal == "Open settings"
    assert second.history == (
        HistoryEntry(Said.STEP, "Click Settings, in the middle of the menu."),
    )
    assert second.marked.elements == first.marked.elements


def test_text_answer_and_message_join_the_history_for_later_steps(image, usage_log):
    provider = RecordingProvider(GOOD, ANSWER, GOOD)
    session = make_session(image, usage_log, provider)
    session.next_step()

    reply = session.ask("What does this button do?")
    session.next_step()

    assert reply == Answer("It opens the settings page.")
    expected = (
        HistoryEntry(Said.STEP, "Click Settings, in the middle of the menu."),
        HistoryEntry(Said.MESSAGE, "What does this button do?"),
        HistoryEntry(Said.ANSWER, "It opens the settings page."),
    )
    assert tuple(session.history[:3]) == expected
    assert provider.requests[2].history == expected
    assert provider.requests[2].message is None


def test_corrected_step_is_a_step_and_joins_the_history_after_the_message(image, usage_log):
    session = make_session(image, usage_log, ScriptedProvider(GOOD, CORRECTED))
    session.next_step()

    step = session.ask("I already did this")

    assert isinstance(step, Step)
    assert step.target_label == "File"
    assert step.bbox == BBox(x=10, y=8, width=40, height=20)
    assert step.screenshot is not None
    assert session.history[1:] == [
        HistoryEntry(Said.MESSAGE, "I already did this"),
        HistoryEntry(Said.STEP, "The File menu is closed: click File again, at the top left."),
    ]


@pytest.mark.parametrize("bad", [ModelReply(text="not json"), ModelReply(text='{"answer": ""}')])
def test_invalid_reply_to_a_message_gets_one_retry_and_both_are_recorded(image, usage_log, bad):
    session = make_session(image, usage_log, ScriptedProvider(bad, ANSWER))
    captures = counting_captures(session)

    assert session.ask("Is this right?") == Answer("It opens the settings page.")

    assert len(captures) == 1
    assert [record["attempt"] for record in read_records(usage_log)] == [1, 2]


def test_failed_message_leaves_the_history_as_it_was(image, usage_log):
    session = make_session(image, usage_log, ScriptedProvider(GOOD, *[ModelReply(text="no")] * 2))
    session.next_step()

    with pytest.raises(InvalidModelResponse):
        session.ask("Is this right?")

    assert [entry.said for entry in session.history] == [Said.STEP]
    assert [record["attempt"] for record in read_records(usage_log)] == [1, 1, 2]


def test_refused_message_is_not_retried(image, usage_log):
    refusal = ModelReply(text="", stop_reason="refusal", refusal_detail="cyber: declined")
    provider = ScriptedProvider(refusal, ANSWER)

    with pytest.raises(ModelRefusal):
        make_session(image, usage_log, provider).ask("Is this right?")

    assert provider.calls == 1
