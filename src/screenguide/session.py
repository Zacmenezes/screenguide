"""`layer:session`: holds goal and history and crosses the layers for each step."""

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from screenguide.capture import ScreenCapturer
from screenguide.contract import InvalidModelResponse, parse_model_response
from screenguide.detection import BBox, ElementDetector
from screenguide.grounding import GroundingStrategy
from screenguide.model import ModelProvider, ModelRequest
from screenguide.usage import UsageRecord, UsageTracker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Step:
    target_label: str | None
    instruction: str
    bbox: BBox | None
    done: bool
    confidence: float


class GuideSession:
    def __init__(
        self,
        goal: str,
        capturer: ScreenCapturer,
        detector: ElementDetector,
        grounding: GroundingStrategy,
        provider: ModelProvider,
        tracker: UsageTracker,
    ) -> None:
        self.goal = goal
        self.history: list[str] = []
        self.session_id = uuid.uuid4().hex
        self._capturer = capturer
        self._detector = detector
        self._grounding = grounding
        self._provider = provider
        self._tracker = tracker

    def next_step(self) -> Step:
        """Produce one step, raising `InvalidModelResponse` if the reply breaks the contract."""
        screenshot = self._capturer.capture()
        elements = self._detector.detect(screenshot)
        marked = self._grounding.mark(screenshot, elements)
        request = ModelRequest(self.goal, tuple(self.history), marked)

        started = time.perf_counter()
        reply = self._provider.complete(request)
        latency_ms = (time.perf_counter() - started) * 1000

        # Recorded before validation: an invalid reply was still a paid call.
        self._tracker.record(
            UsageRecord(
                timestamp=datetime.now(UTC).isoformat(),
                session_id=self.session_id,
                provider=self._provider.name,
                model=self._provider.model,
                input_tokens=reply.input_tokens,
                output_tokens=reply.output_tokens,
                image_width=marked.image.width,
                image_height=marked.image.height,
                latency_ms=latency_ms,
            )
        )

        response = parse_model_response(reply.text)
        bbox = None
        if response.element_id is not None:
            bbox = self._grounding.resolve(marked, response.element_id)
            if bbox is None:
                raise InvalidModelResponse(f"no box is numbered {response.element_id}")

        self.history.append(response.instruction)
        logger.info(
            "step session_id=%s element_id=%s done=%s",
            self.session_id,
            response.element_id,
            response.done,
        )
        return Step(
            target_label=response.target_label,
            instruction=response.instruction,
            bbox=bbox,
            done=response.done,
            confidence=response.confidence,
        )
