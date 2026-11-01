"""`layer:session`: holds goal and history and crosses the layers for each step."""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from screenguide.capture import ScreenCapturer
from screenguide.contract import (
    InvalidModelResponse,
    ModelRefusal,
    ModelResponse,
    parse_model_response,
)
from screenguide.detection import BBox, ElementDetector
from screenguide.grounding import GroundingStrategy, UnknownElementError
from screenguide.model import STOP_MAX_TOKENS, STOP_REFUSAL, ModelProvider, ModelRequest
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
        notify: Callable[[str], None] = lambda message: None,
    ) -> None:
        self.goal = goal
        self.history: list[str] = []
        self.session_id = uuid.uuid4().hex
        self._capturer = capturer
        self._detector = detector
        self._grounding = grounding
        self._provider = provider
        self._tracker = tracker
        self._notify = notify

    def next_step(self) -> Step:
        """Produce one step from one capture.

        An invalid or truncated reply gets one retry. Raises `InvalidModelResponse` if the retry
        fails too, and `ModelRefusal` as soon as the model declines.
        """
        screenshot = self._capturer.capture()
        elements = self._detector.detect(screenshot)
        marked = self._grounding.mark(screenshot, elements)
        request = ModelRequest(self.goal, tuple(self.history), marked)

        try:
            response, bbox = self._ask(request, attempt=1)
        except InvalidModelResponse as exc:
            logger.warning("invalid reply session_id=%s attempt=1: %s", self.session_id, exc)
            response, bbox = self._ask(request, attempt=2)

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

    def _ask(self, request: ModelRequest, attempt: int) -> tuple[ModelResponse, BBox | None]:
        image = request.marked.image
        if self._provider.external:
            self._notify(
                f"Sending a screenshot ({image.width}x{image.height}) to external provider "
                f"{self._provider.name} ({self._provider.model})."
            )

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
                cache_creation_input_tokens=reply.cache_creation_input_tokens,
                cache_read_input_tokens=reply.cache_read_input_tokens,
                image_tokens=reply.image_tokens,
                image_width=image.width,
                image_height=image.height,
                latency_ms=latency_ms,
                attempt=attempt,
            )
        )

        if reply.stop_reason == STOP_REFUSAL:
            raise ModelRefusal(reply.refusal_detail or "no reason given")
        if reply.stop_reason == STOP_MAX_TOKENS:
            raise InvalidModelResponse("reply was cut off at the token limit")

        response = parse_model_response(reply.text)
        if response.element_id is None:
            return response, None
        try:
            return response, self._grounding.resolve(request.marked, response.element_id)
        except UnknownElementError as exc:
            raise InvalidModelResponse(str(exc)) from exc
