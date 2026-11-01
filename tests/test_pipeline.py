import json

import pytest

from conftest import FixedDetector
from screenguide.capture import FileCapturer
from screenguide.cli import main
from screenguide.contract import InvalidModelResponse
from screenguide.detection import BBox
from screenguide.grounding import NumberingGrounding
from screenguide.model import ModelReply, ModelRequest
from screenguide.pricing import PricingTable
from screenguide.session import GuideSession
from screenguide.usage import JsonlUsageTracker


class FixedProvider:
    name = "fixed"
    model = "fixed-1"

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


def make_session(image, usage_log, provider):
    return GuideSession(
        goal="Open settings",
        capturer=FileCapturer(image),
        detector=FixedDetector(),
        grounding=NumberingGrounding(),
        provider=provider,
        tracker=JsonlUsageTracker(usage_log, PricingTable({})),
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
    assert record["image_width"] is None
    assert record["image_height"] is None
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
    assert request.marked.image.data == image.read_bytes()
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

    assert len(read_records(usage_log)) == 1
    assert session.history == []
