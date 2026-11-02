"""Evaluation: runs saved cases through the app's pipeline and scores the chosen element.

A case is one step: a screenshot, the goal and the instructions given so far. A hit is a chosen
bbox that intersects the case's `correct_bbox` with positive area.
"""

import json
import logging
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from screenguide.contract import InvalidModelResponse, ModelRefusal
from screenguide.detection import BBox
from screenguide.errors import GUIDE_ERRORS
from screenguide.model import ModelProviderError
from screenguide.pricing import PricingTable
from screenguide.session import GuideSession
from screenguide.usage import UsageRecord, UsageTracker

logger = logging.getLogger(__name__)

HIT = "hit"
MISS = "miss"
NO_BOX = "no_box"
ERROR = "error"


class CaseError(ValueError):
    """A line of the cases file cannot be used."""


@dataclass(frozen=True)
class Case:
    line: int
    image: Path
    goal: str
    history: tuple[str, ...]
    correct_bbox: BBox


@dataclass(frozen=True)
class SkippedLine:
    line: int
    reason: str


@dataclass(frozen=True)
class CaseResult:
    """One line of the run file. Unknown values stay `None`."""

    line: int
    outcome: str
    element_id: int | None
    bbox: dict[str, int] | None
    confidence: float | None
    attempts: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    latency_ms: float
    error: str | None = None


def load_cases(path: Path) -> tuple[list[Case], list[SkippedLine]]:
    """Read one case per line. `image` is relative to the cases file. Blank lines are ignored."""
    cases = []
    skipped = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            cases.append(_parse_case(number, line, path.parent))
        except CaseError as exc:
            skipped.append(SkippedLine(number, str(exc)))
    return cases, skipped


def _parse_case(number: int, line: str, base: Path) -> Case:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise CaseError(f"not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise CaseError("not a JSON object")

    image, goal = raw.get("image"), raw.get("goal")
    if not isinstance(image, str) or not image.strip():
        raise CaseError("image must be a non-empty string")
    if not isinstance(goal, str) or not goal.strip():
        raise CaseError("goal must be a non-empty string")
    history = raw.get("history", [])
    if not isinstance(history, list) or not all(isinstance(step, str) for step in history):
        raise CaseError("history must be a list of strings")

    image_path = base / image
    if not image_path.is_file():
        raise CaseError(f"image not found: {image_path}")
    return Case(number, image_path, goal, tuple(history), _parse_bbox(raw.get("correct_bbox")))


def _parse_bbox(raw: Any) -> BBox:
    fields = ("x", "y", "width", "height")
    if not isinstance(raw, dict) or any(type(raw.get(name)) is not int for name in fields):
        raise CaseError("correct_bbox must hold integer x, y, width and height")
    bbox = BBox(*(raw[name] for name in fields))
    if bbox.x < 0 or bbox.y < 0 or bbox.width <= 0 or bbox.height <= 0:
        raise CaseError("correct_bbox needs x and y of at least 0 and a positive size")
    return bbox


def intersects(a: BBox, b: BBox) -> bool:
    """Whether the boxes share an area larger than zero; touching edges do not count."""
    return min(a.x + a.width, b.x + b.width) > max(a.x, b.x) and min(
        a.y + a.height, b.y + b.height
    ) > max(a.y, b.y)


class CaseUsage:
    """Keeps one case's records, priced, and passes each one on to the usage log."""

    def __init__(self, log: UsageTracker, pricing: PricingTable) -> None:
        self.records: list[UsageRecord] = []
        self._log = log
        self._pricing = pricing

    def record(self, record: UsageRecord) -> None:
        cost = self._pricing.cost(
            record.provider, record.model, record.input_tokens, record.output_tokens
        )
        self.records.append(replace(record, cost_usd=cost))
        self._log.record(record)


NewSession = Callable[[Case, UsageTracker], GuideSession]


def run_case(
    case: Case, new_session: NewSession, log: UsageTracker, pricing: PricingTable
) -> CaseResult:
    """Produce one step for `case` and score it. A failed step is an `error`, not a raise."""
    usage = CaseUsage(log, pricing)
    session = new_session(case, usage)
    try:
        step = session.next_step()
    except GUIDE_ERRORS as exc:
        logger.warning("case line=%d failed: %s", case.line, exc)
        return _result(case, usage, ERROR, error=_describe(exc))

    response_bbox = None
    if step.bbox is None:
        outcome = NO_BOX
    else:
        response_bbox = asdict(step.bbox)
        outcome = HIT if intersects(step.bbox, case.correct_bbox) else MISS
    return _result(
        case,
        usage,
        outcome,
        element_id=step.element_id,
        bbox=response_bbox,
        confidence=step.confidence,
    )


def _describe(exc: Exception) -> str:
    if isinstance(exc, ModelRefusal):
        return f"refusal: {exc}"
    if isinstance(exc, InvalidModelResponse):
        return f"invalid reply after retry: {exc}"
    if isinstance(exc, ModelProviderError):
        return f"provider error: {exc}"
    return f"{type(exc).__name__}: {exc}"


def _result(case: Case, usage: CaseUsage, outcome: str, **fields: Any) -> CaseResult:
    records = usage.records
    return CaseResult(
        line=case.line,
        outcome=outcome,
        element_id=fields.get("element_id"),
        bbox=fields.get("bbox"),
        confidence=fields.get("confidence"),
        attempts=len(records),
        input_tokens=_sum(record.input_tokens for record in records),
        output_tokens=_sum(record.output_tokens for record in records),
        cost_usd=_sum(record.cost_usd for record in records),
        latency_ms=sum(record.latency_ms for record in records),
        error=fields.get("error"),
    )


def _sum(values: Iterable[float | None]) -> Any:
    """The sum, or `None` if any part is unknown. No calls cost nothing."""
    total: Any = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


@dataclass
class RunSummary:
    cases: int = 0
    hits: int = 0
    cost_usd: float = 0.0
    unknown_cost: int = 0

    def add(self, result: CaseResult) -> None:
        self.cases += 1
        self.hits += result.outcome == HIT
        if result.cost_usd is None:
            self.unknown_cost += 1
        else:
            self.cost_usd += result.cost_usd

    def format(self) -> str:
        rate = f"{self.hits / self.cases:.1%}" if self.cases else "n/a"
        cost = f"${self.cost_usd:.4f}"
        if self.cases and self.unknown_cost == self.cases:
            cost = "unknown"
        elif self.unknown_cost:
            cost += f" + {self.unknown_cost} cases of unknown cost"
        return f"cases run: {self.cases}\nhits: {self.hits}\nhit rate: {rate}\ntotal cost: {cost}"


def run_eval(
    cases: Iterable[Case],
    new_session: NewSession,
    log: UsageTracker,
    pricing: PricingTable,
    settings: dict[str, Any],
    run_file: TextIO,
    progress: Callable[[CaseResult], None] = lambda result: None,
) -> RunSummary:
    """Write `settings` as the first line, then one line per case as soon as it is scored."""
    run_file.write(json.dumps(settings) + "\n")
    run_file.flush()
    summary = RunSummary()
    for case in cases:
        result = run_case(case, new_session, log, pricing)
        run_file.write(json.dumps(asdict(result)) + "\n")
        run_file.flush()
        summary.add(result)
        progress(result)
    return summary


def open_run_file(runs_dir: Path, now: datetime | None = None) -> tuple[Path, TextIO]:
    """Create `<runs_dir>/<UTC timestamp>.jsonl`, never overwriting an earlier run."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{stamp}.jsonl"
    suffix = 1
    while True:
        try:
            return path, path.open("x", encoding="utf-8")
        except FileExistsError:
            suffix += 1
            path = runs_dir / f"{stamp}-{suffix}.jsonl"


def git_commit() -> str | None:
    """The commit of the code being evaluated, or `None` outside a git checkout."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None
