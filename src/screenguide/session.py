"""`layer:session`: holds goal and history and crosses the layers for each step."""

import logging
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from screenguide.capture import ScreenCapturer, Screenshot
from screenguide.contract import (
    AnswerResponse,
    InvalidModelResponse,
    ModelRefusal,
    ModelResponse,
    parse_message_reply,
    parse_model_response,
)
from screenguide.detection import BBox, ElementDetector
from screenguide.grounding import GroundingStrategy, UnknownElementError
from screenguide.model import (
    STOP_MAX_TOKENS,
    STOP_REFUSAL,
    HistoryEntry,
    ModelProvider,
    ModelRequest,
    Said,
    steps_shown,
)
from screenguide.usage import UsageRecord, UsageTracker

logger = logging.getLogger(__name__)

Reply = ModelResponse | AnswerResponse
Parser = Callable[[str], Reply]


@dataclass(frozen=True)
class Step:
    target_label: str | None
    instruction: str
    bbox: BBox | None
    done: bool
    confidence: float
    element_id: int | None = None  # The box number the model chose.
    # The capture `bbox` refers to, so that the panel can crop it.
    screenshot: Screenshot | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Answer:
    """A text reply to a message: the current step still stands."""

    text: str


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
        history: Iterable[str] = (),
        session_id: str | None = None,
    ) -> None:
        """`history` preloads the steps already shown; `session_id` groups the usage records."""
        self.goal = goal
        self.history: list[HistoryEntry] = list(steps_shown(history))
        self.session_id = session_id or uuid.uuid4().hex
        self._capturer = capturer
        self._detector = detector
        self._grounding = grounding
        self._provider = provider
        self._tracker = tracker
        self._notify = notify

    def steps(self) -> Iterator[Step]:
        """Yield steps until one is `done`. Nothing is captured until the caller asks for a step."""
        while True:
            step = self.next_step()
            yield step
            if step.done:
                return

    def next_step(self) -> Step:
        """Produce one step from one capture.

        An invalid or truncated reply gets one retry. Raises `InvalidModelResponse` if the retry
        fails too, and `ModelRefusal` as soon as the model declines.
        """
        screenshot, request = self._request()
        response, bbox = self._ask_twice(request, parse_model_response)
        return self._shown(response, bbox, screenshot)

    def ask(self, message: str) -> Step | Answer:
        """Send a message about the current step along with a fresh capture.

        The reply is a text answer, or a step that replaces the current one. Retries and errors
        are as for `next_step`. The message and the reply join the history only once both exist.
        """
        screenshot, request = self._request(message)
        response, bbox = self._ask_twice(request, parse_message_reply)
        self.history.append(HistoryEntry(Said.MESSAGE, message))
        if isinstance(response, AnswerResponse):
            self.history.append(HistoryEntry(Said.ANSWER, response.answer))
            logger.info("answer session_id=%s", self.session_id)
            return Answer(response.answer)
        return self._shown(response, bbox, screenshot)

    def _request(self, message: str | None = None) -> tuple[Screenshot, ModelRequest]:
        screenshot = self._capturer.capture()
        elements = self._detector.detect(screenshot)
        marked = self._grounding.mark(screenshot, elements)
        return screenshot, ModelRequest(self.goal, tuple(self.history), marked, message)

    def _ask_twice(self, request: ModelRequest, parse: Parser) -> tuple[Reply, BBox | None]:
        try:
            return self._ask(request, parse, attempt=1)
        except InvalidModelResponse as exc:
            logger.warning("invalid reply session_id=%s attempt=1: %s", self.session_id, exc)
            return self._ask(request, parse, attempt=2)

    def _shown(self, response: ModelResponse, bbox: BBox | None, screenshot: Screenshot) -> Step:
        self.history.append(HistoryEntry(Said.STEP, response.instruction))
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
            element_id=response.element_id,
            screenshot=screenshot,
        )

    def _ask(self, request: ModelRequest, parse: Parser, attempt: int) -> tuple[Reply, BBox | None]:
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

        response = parse(reply.text)
        if isinstance(response, AnswerResponse) or response.element_id is None:
            return response, None
        try:
            return response, self._grounding.resolve(
                request.marked, response.element_id, response.target_label
            )
        except UnknownElementError as exc:
            raise InvalidModelResponse(str(exc)) from exc
