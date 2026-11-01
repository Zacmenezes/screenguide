"""`layer:usage`: records tokens, cost and latency, one record per model call."""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

USAGE_LOG_ENV = "SCREENGUIDE_USAGE_LOG"


def default_usage_log() -> Path:
    override = os.environ.get(USAGE_LOG_ENV)
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / "screenguide" / "usage"


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


class UsageTracker(Protocol):
    def record(self, record: UsageRecord) -> None: ...


class JsonlUsageTracker:
    """Appends each record as one JSON line."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def record(self, record: UsageRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(asdict(record)) + "\n")
