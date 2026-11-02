"""`screenguide eval` over the stub capturer, detector and provider: no key and no display."""

import json

import pytest

from screenguide.cli import main
from screenguide.detection import BBox
from screenguide.evaluation import intersects, load_cases
from screenguide.model import ModelProviderError, ModelReply
from screenguide.usage import read_usage_log

SETTINGS_BBOX = {"x": 60, "y": 8, "width": 70, "height": 20}  # Box 2 of the fixed detector.


def reply(element_id, input_tokens=1000, output_tokens=100):
    return ModelReply(
        text=json.dumps(
            {
                "element_id": element_id,
                "target_label": "Settings",
                "instruction": "Click Settings, in the middle of the menu.",
                "done": False,
                "confidence": 0.7,
            }
        ),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stop_reason="end_turn",
    )


class GoalProvider:
    """Answers by the case's goal, so each case can script its own replies."""

    name = "anthropic"
    model = "claude-test"
    external = True

    def __init__(self, credentials=True):
        self.calls = []
        self.histories = []
        self.created_with = []
        self._credentials = credentials
        self._retry_seen = False

    def __call__(self, model, effort):
        self.created_with.append((model, effort))
        return self

    def check_credentials(self):
        if not self._credentials:
            raise ModelProviderError("Anthropic credentials: no credentials found.")

    def complete(self, request):
        self.calls.append(request.goal)
        self.histories.append(request.history)
        if request.goal == "hit":
            return reply(2)
        if request.goal == "miss":
            return reply(1)
        if request.goal == "no box":
            return reply(None)
        if request.goal == "unknown tokens":
            return reply(2, input_tokens=None)
        if request.goal == "retry":
            first, self._retry_seen = not self._retry_seen, True
            return (
                ModelReply(text="not json", input_tokens=10, output_tokens=1) if first else reply(2)
            )
        raise ModelProviderError("Anthropic API error 529: overloaded")


@pytest.fixture
def provider(monkeypatch):
    provider = GoalProvider()
    monkeypatch.setattr("screenguide.cli.AnthropicProvider", provider)
    return provider


@pytest.fixture
def pricing(home):
    path = home / ".config" / "screenguide" / "pricing.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[anthropic."claude-test"]\ninput_usd_per_mtok = 1.0\noutput_usd_per_mtok = 5.0\n'
    )


def write_cases(directory, *cases):
    path = directory / "cases.jsonl"
    lines = [case if isinstance(case, str) else json.dumps(case) for case in cases]
    path.write_text("\n".join(lines) + "\n")
    return path


def case(goal, history=(), bbox=SETTINGS_BBOX, image="screen.png"):
    return {"image": image, "goal": goal, "history": list(history), "correct_bbox": bbox}


def read_run(directory):
    (path,) = (directory / "runs").iterdir()
    settings, *results = [json.loads(line) for line in path.read_text().splitlines()]
    return path, settings, results


def test_each_case_is_scored_and_the_run_file_holds_settings_and_results(
    image, provider, pricing, capsys
):
    cases = write_cases(
        image.parent, case("hit", ["Click File, top left."]), case("miss"), case("no box")
    )

    assert main(["eval", str(cases)]) == 0

    path, settings, results = read_run(image.parent)
    assert path.name.endswith("Z.jsonl")
    assert settings == {
        "session_id": settings["session_id"],
        "started_at": settings["started_at"],
        "provider": "anthropic",
        "model": "claude-test",
        "effort": None,
        "grounding": "set-of-mark",
        "image_long_edge": 2576,
        "cases_file": str(cases.resolve()),
        "git_commit": settings["git_commit"],
    }
    cost = (1000 * 1.0 + 100 * 5.0) / 1_000_000
    hit, miss, no_box = results
    assert hit == {
        "line": 1,
        "outcome": "hit",
        "element_id": 2,
        "bbox": SETTINGS_BBOX,
        "confidence": 0.7,
        "attempts": 1,
        "input_tokens": 1000,
        "output_tokens": 100,
        "cost_usd": pytest.approx(cost),
        "latency_ms": hit["latency_ms"],
        "error": None,
    }
    assert hit["latency_ms"] >= 0
    assert (miss["line"], miss["outcome"], miss["element_id"]) == (2, "miss", 1)
    assert miss["bbox"] == {"x": 10, "y": 8, "width": 40, "height": 20}
    assert (no_box["outcome"], no_box["element_id"], no_box["bbox"]) == ("no_box", None, None)
    assert provider.histories[0] == ("Click File, top left.",)

    out = capsys.readouterr().out
    assert "cases run: 3\nhits: 1\nhit rate: 33.3%" in out
    assert f"total cost: ${3 * cost:.4f}" in out
    assert f"run file: {path}" in out


def test_an_error_is_a_miss_and_the_run_goes_on(image, provider):
    cases = write_cases(image.parent, case("overloaded"), case("hit"))

    assert main(["eval", str(cases)]) == 0

    _, _, (error, hit) = read_run(image.parent)
    assert error["outcome"] == "error"
    assert error["error"] == "provider error: Anthropic API error 529: overloaded"
    assert (error["attempts"], error["input_tokens"], error["cost_usd"]) == (0, 0, 0)
    assert hit["outcome"] == "hit"


def test_invalid_reply_gets_its_retry_and_both_calls_are_summed(image, provider, pricing):
    cases = write_cases(image.parent, case("retry"))

    assert main(["eval", str(cases)]) == 0

    _, _, (result,) = read_run(image.parent)
    assert result["outcome"] == "hit"
    assert result["attempts"] == 2
    assert (result["input_tokens"], result["output_tokens"]) == (1010, 101)
    assert result["cost_usd"] == pytest.approx((1010 * 1.0 + 101 * 5.0) / 1_000_000)


def test_unknown_tokens_leave_the_sum_and_the_cost_null(image, provider, pricing, capsys):
    cases = write_cases(image.parent, case("unknown tokens"))

    assert main(["eval", str(cases)]) == 0

    _, _, (result,) = read_run(image.parent)
    assert (result["input_tokens"], result["output_tokens"]) == (None, 100)
    assert result["cost_usd"] is None
    assert "total cost: unknown" in capsys.readouterr().out


def test_malformed_lines_and_missing_images_are_reported_and_skipped(image, provider, capsys):
    cases = write_cases(
        image.parent,
        "{not json",
        case("hit", image="absent.png"),
        case("hit", bbox={"x": 1, "y": 2, "width": 0, "height": 4}),
        "",
        case("hit"),
    )

    assert main(["eval", str(cases)]) == 0

    err = capsys.readouterr().err
    assert f"{cases}:1: skipped: not valid JSON" in err
    assert f"{cases}:2: skipped: image not found" in err
    assert f"{cases}:3: skipped: correct_bbox" in err
    _, _, results = read_run(image.parent)
    assert [result["line"] for result in results] == [5]


def test_calls_go_to_the_usage_log_under_one_session_per_run(image, provider, usage_log):
    cases = write_cases(image.parent, case("hit"), case("retry"))

    assert main(["eval", str(cases)]) == 0

    _, settings, _ = read_run(image.parent)
    records, _ = read_usage_log(usage_log)
    assert len(records) == 3
    assert {record.session_id for record in records} == {settings["session_id"]}


def test_overrides_and_limit_apply_to_this_run_only(image, provider):
    cases = write_cases(image.parent, case("hit"), case("miss"), case("no box"))

    args = ["eval", str(cases), "--model", "claude-other", "--effort", "low"]
    assert main([*args, "--image-long-edge", "100", "--limit", "2"]) == 0

    assert provider.created_with == [("claude-other", "low")]
    assert provider.calls == ["hit", "miss"]
    _, settings, results = read_run(image.parent)
    assert (settings["effort"], settings["image_long_edge"]) == ("low", 100)
    assert len(results) == 2


@pytest.mark.parametrize("value", ["0", "-1", "two"])
def test_limit_must_be_a_positive_integer(image, provider, value):
    with pytest.raises(SystemExit):
        main(["eval", str(write_cases(image.parent, case("hit"))), "--limit", value])


def test_missing_credentials_stop_the_run_before_the_first_case(image, monkeypatch, capsys):
    provider = GoalProvider(credentials=False)
    monkeypatch.setattr("screenguide.cli.AnthropicProvider", provider)
    cases = write_cases(image.parent, case("hit"))

    assert main(["eval", str(cases)]) == 1

    assert provider.calls == []
    assert not (image.parent / "runs").exists()
    assert "no credentials found" in capsys.readouterr().err


def test_terminal_says_what_goes_where_before_the_first_call(image, provider, capsys):
    cases = write_cases(image.parent, case("hit"), case("miss"))

    assert main(["eval", str(cases)]) == 0

    err = capsys.readouterr().err
    assert "Sending 2 screenshots" in err
    assert "anthropic (claude-test)" in err
    assert err.index("Sending") < err.index("line 1:")


def test_a_missing_cases_file_is_an_error(tmp_path, provider, capsys):
    assert main(["eval", str(tmp_path / "absent.jsonl")]) == 1
    assert "absent.jsonl" in capsys.readouterr().err


def test_image_and_history_are_read_relative_to_the_cases_file(image, tmp_path):
    cases = write_cases(tmp_path, case("hit", ["one", "two"], image=image.name))
    (loaded,), skipped = load_cases(cases)

    assert skipped == []
    assert loaded.image == image
    assert loaded.history == ("one", "two")
    assert loaded.correct_bbox == BBox(**SETTINGS_BBOX)


@pytest.mark.parametrize(
    ("other", "expected"),
    [
        (BBox(5, 5, 10, 10), True),
        (BBox(0, 0, 10, 10), True),
        (BBox(10, 0, 10, 10), False),  # Shares only an edge.
        (BBox(0, 10, 10, 10), False),
        (BBox(20, 20, 5, 5), False),
    ],
)
def test_a_hit_needs_an_intersection_with_positive_area(other, expected):
    assert intersects(BBox(0, 0, 10, 10), other) is expected
