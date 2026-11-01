"""`layer:usage`: records tokens, cost and latency per model call, and summarises them."""

import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from screenguide.pricing import PricingTable


@dataclass(frozen=True)
class UsageRecord:
    """An unknown value stays `None`, logged as `null`, and is never estimated."""

    timestamp: str
    session_id: str
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    image_width: int | None
    image_height: int | None
    latency_ms: float
    cost_usd: float | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    image_tokens: int | None = None
    attempt: int = 1  # 1 for the first call of a step, 2 for its retry.


class UsageTracker(Protocol):
    def record(self, record: UsageRecord) -> None: ...


class JsonlUsageTracker:
    """Prices each record at the time of the call and appends it as one JSON line."""

    def __init__(self, path: Path, pricing: PricingTable) -> None:
        self._path = path
        self._pricing = pricing

    def record(self, record: UsageRecord) -> None:
        cost = self._pricing.cost(
            record.provider, record.model, record.input_tokens, record.output_tokens
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(asdict(replace(record, cost_usd=cost))) + "\n")


def read_usage_log(path: Path) -> tuple[list[UsageRecord], int]:
    """Return the records and how many lines were unreadable; a missing log has neither."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return [], 0
    records = []
    malformed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            records.append(_parse_record(line))
        except (ValueError, TypeError):
            malformed += 1
    return records, malformed


def _parse_record(line: str) -> UsageRecord:
    record = UsageRecord(**json.loads(line))
    datetime.fromisoformat(record.timestamp)
    for count in (
        record.input_tokens,
        record.output_tokens,
        record.cache_creation_input_tokens,
        record.cache_read_input_tokens,
        record.image_tokens,
    ):
        if count is not None and type(count) is not int:
            raise ValueError("token counts must be integers or null")
    if record.cost_usd is not None and type(record.cost_usd) not in (int, float):
        raise ValueError("cost_usd must be a number or null")
    return record


@dataclass
class _Totals:
    """Sums of what is known, plus how many records each sum leaves out."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    unknown_input: int = 0
    unknown_output: int = 0
    cost_usd: float = 0.0
    unknown_cost: int = 0

    def add(self, record: UsageRecord) -> None:
        self.calls += 1
        if record.input_tokens is None:
            self.unknown_input += 1
        else:
            self.input_tokens += record.input_tokens
        if record.output_tokens is None:
            self.unknown_output += 1
        else:
            self.output_tokens += record.output_tokens
        if record.cost_usd is None:
            self.unknown_cost += 1
        else:
            self.cost_usd += record.cost_usd


def _day(record: UsageRecord) -> str:
    """The calendar day on the user's clock, which is not always the UTC day logged."""
    return datetime.fromisoformat(record.timestamp).astimezone().date().isoformat()


GROUPINGS: tuple[tuple[str, Callable[[UsageRecord], str]], ...] = (
    ("day", _day),
    ("session", lambda record: record.session_id[:8]),
    ("provider", lambda record: record.provider),
    ("model", lambda record: f"{record.provider}/{record.model}"),
)


def _format_tokens(total: int, unknown: int, calls: int) -> str:
    if unknown == calls:
        return "unknown"
    return f"{total} + {unknown} unknown" if unknown else str(total)


def _format_cost(totals: _Totals) -> str:
    if totals.unknown_cost == totals.calls:
        return "unknown"
    cost = f"${totals.cost_usd:.4f}"
    return f"{cost} + {totals.unknown_cost} unknown" if totals.unknown_cost else cost


def _format_table(title: str, rows: dict[str, _Totals]) -> list[str]:
    table = [[title, "calls", "input", "output", "cost"]]
    for key, totals in rows.items():
        table.append(
            [
                key,
                str(totals.calls),
                _format_tokens(totals.input_tokens, totals.unknown_input, totals.calls),
                _format_tokens(totals.output_tokens, totals.unknown_output, totals.calls),
                _format_cost(totals),
            ]
        )
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    return [
        "  ".join(
            [row[0].ljust(widths[0])] + [row[i].rjust(widths[i]) for i in range(1, len(row))]
        ).rstrip()
        for row in table
    ]


def summarize(records: Iterable[UsageRecord], malformed: int = 0) -> str:
    """Render totals by day, session, provider and model, in order of first appearance."""
    records = list(records)
    notes = []
    if records:
        lines = []
        for title, key_of in GROUPINGS:
            rows: dict[str, _Totals] = {}
            for record in records:
                rows.setdefault(key_of(record), _Totals()).add(record)
            lines += [*_format_table(title, rows), ""]
        grand_total = _Totals()
        for record in records:
            grand_total.add(record)
        lines.append(f"total: {grand_total.calls} calls, {_format_cost(grand_total)}")
        if grand_total.unknown_cost:
            notes.append(
                f"note: cost is unknown for {grand_total.unknown_cost} of {grand_total.calls} "
                "calls and left out of the totals."
            )
    else:
        lines = ["No usage recorded yet."]
    if malformed:
        notes.append(f"note: skipped {malformed} unreadable log lines.")
    return "\n".join(lines + notes)
