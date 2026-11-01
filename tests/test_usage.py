import json
import time
from dataclasses import asdict

import pytest

from screenguide.cli import main
from screenguide.usage import UsageRecord, read_usage_log, summarize


def make_record(**overrides):
    values = {
        "timestamp": "2026-03-02T12:00:00+00:00",
        "session_id": "aaaaaaaa1111",
        "provider": "anthropic",
        "model": "m",
        "input_tokens": 1000,
        "output_tokens": 100,
        "image_width": None,
        "image_height": None,
        "latency_ms": 5.0,
        "cost_usd": 0.5,
    }
    return UsageRecord(**{**values, **overrides})


def write_log(path, *records):
    path.write_text("".join(json.dumps(asdict(record)) + "\n" for record in records))


def table(output, title):
    """The rows of one table, as lists of cells without its header."""
    lines = output.split("\n")
    start = next(i for i, line in enumerate(lines) if line.split()[:1] == [title])
    rows = []
    for line in lines[start + 1 :]:
        if not line:
            return rows
        rows.append(line.split())
    return rows


@pytest.fixture
def local_time_is_three_hours_behind_utc(monkeypatch):
    monkeypatch.setenv("TZ", "<-03>3")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_summary_groups_by_day_session_provider_and_model():
    output = summarize(
        [
            make_record(),
            make_record(timestamp="2026-03-03T12:00:00+00:00", model="n", cost_usd=0.25),
            make_record(
                timestamp="2026-03-03T13:00:00+00:00",
                session_id="bbbbbbbb2222",
                provider="ollama",
                model="m",
                cost_usd=0.0,
            ),
        ]
    )

    assert table(output, "day") == [
        ["2026-03-02", "1", "1000", "100", "$0.5000"],
        ["2026-03-03", "2", "2000", "200", "$0.2500"],
    ]
    assert table(output, "session") == [
        ["aaaaaaaa", "2", "2000", "200", "$0.7500"],
        ["bbbbbbbb", "1", "1000", "100", "$0.0000"],
    ]
    assert table(output, "provider") == [
        ["anthropic", "2", "2000", "200", "$0.7500"],
        ["ollama", "1", "1000", "100", "$0.0000"],
    ]
    assert table(output, "model") == [
        ["anthropic/m", "1", "1000", "100", "$0.5000"],
        ["anthropic/n", "1", "1000", "100", "$0.2500"],
        ["ollama/m", "1", "1000", "100", "$0.0000"],
    ]
    assert "total: 3 calls, $0.7500" in output
    assert "note:" not in output


def test_day_follows_the_local_clock(local_time_is_three_hours_behind_utc):
    output = summarize([make_record(timestamp="2026-03-02T01:00:00+00:00")])

    assert table(output, "day")[0][0] == "2026-03-01"


def test_unknown_cost_is_left_out_of_totals_with_a_note():
    output = summarize([make_record(), make_record(model="n", cost_usd=None)])

    assert table(output, "model") == [
        ["anthropic/m", "1", "1000", "100", "$0.5000"],
        ["anthropic/n", "1", "1000", "100", "unknown"],
    ]
    assert table(output, "provider") == [
        ["anthropic", "2", "2000", "200", "$0.5000", "+", "1", "unknown"],
    ]
    assert "total: 2 calls, $0.5000 + 1 unknown" in output
    assert "note: cost is unknown for 1 of 2 calls and left out of the totals." in output


def test_all_unknown_is_not_shown_as_zero():
    output = summarize([make_record(input_tokens=None, output_tokens=None, cost_usd=None)])

    assert table(output, "model") == [["anthropic/m", "1", "unknown", "unknown", "unknown"]]
    assert "total: 1 calls, unknown" in output


def test_partly_unreported_tokens_are_marked():
    output = summarize([make_record(), make_record(input_tokens=None)])

    assert table(output, "model") == [
        ["anthropic/m", "2", "1000", "+", "1", "unknown", "200", "$1.0000"],
    ]


def test_missing_log_reads_as_empty(usage_log):
    assert read_usage_log(usage_log) == ([], 0)


def test_cli_usage_with_a_missing_log(usage_log, capsys):
    assert main(["usage"]) == 0
    assert capsys.readouterr().out == "No usage recorded yet.\n"
    assert not usage_log.exists()


def test_cli_usage_with_an_empty_log(usage_log, capsys):
    usage_log.write_text("")

    assert main(["usage"]) == 0
    assert capsys.readouterr().out == "No usage recorded yet.\n"


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[]",
        '{"timestamp": "2026-03-02T12:00:00+00:00"}',
        json.dumps({**asdict(make_record()), "timestamp": "yesterday"}),
        json.dumps({**asdict(make_record()), "input_tokens": "many"}),
        json.dumps({**asdict(make_record()), "cost_usd": "free"}),
        json.dumps({**asdict(make_record()), "surprise": 1}),
    ],
)
def test_unreadable_line_is_skipped_and_counted(usage_log, capsys, line):
    write_log(usage_log, make_record())
    with usage_log.open("a") as log:
        log.write(line + "\n")

    assert main(["usage"]) == 0

    output = capsys.readouterr().out
    assert "total: 1 calls, $0.5000" in output
    assert "note: skipped 1 unreadable log lines." in output


def test_stub_runs_reach_the_summary_with_cost_unknown_without_a_price(image, capsys):
    main(["run", "--image", str(image), "--goal", "Open settings"])
    main(["run", "--image", str(image), "--goal", "Open settings"])
    capsys.readouterr()

    assert main(["usage"]) == 0

    output = capsys.readouterr().out
    assert table(output, "model") == [["stub/stub", "2", "2000", "200", "unknown"]]
    assert "note: cost is unknown for 2 of 2 calls and left out of the totals." in output
