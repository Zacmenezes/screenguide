"""`layer:detection`: finds elements and their bboxes."""

import io
import logging
import math
import time
from dataclasses import dataclass, replace
from typing import Any, Protocol

from PIL import Image, ImageDraw, UnidentifiedImageError

from screenguide.capture import Screenshot

logger = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.5
# Fragments merge when the gap between them is under this fraction of the text height. Word gaps
# are about a third of the height; neighbouring menu items are close to a full height apart.
MAX_WORD_GAP = 0.5
MAX_HEIGHT_RATIO = 1.5

_PACKAGES = '"rapidocr>=3.10,<4" "onnxruntime>=1.20"'
INSTALL_HINT = f"install the OCR engine with: pip install {_PACKAGES}"
REINSTALL_HINT = f"reinstall the OCR engine with: pip install --force-reinstall {_PACKAGES}"


class DetectionError(RuntimeError):
    """The engine or its model is unavailable, or the image cannot be read."""


@dataclass(frozen=True)
class BBox:
    """A box in the coordinates of the captured image."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class Element:
    id: int
    text: str
    bbox: BBox
    confidence: float


class ElementDetector(Protocol):
    def detect(self, screenshot: Screenshot) -> list[Element]: ...


class RapidOcrDetector:
    """Finds text with RapidOCR. The engine loads on the first detection and is then reused."""

    def __init__(self, engine: Any = None) -> None:
        self._engine = engine

    def detect(self, screenshot: Screenshot) -> list[Element]:
        image = _decode(screenshot)
        engine = self._load_engine()
        started = time.perf_counter()
        try:
            result = engine(image)
        except Exception as exc:  # The engine loads its models here and has no error type.
            raise DetectionError(f"OCR failed ({exc}): {REINSTALL_HINT}") from exc

        fragments = []
        for quad, text, score in zip(
            result.boxes if result.boxes is not None else (),
            result.txts or (),
            result.scores or (),
            strict=True,
        ):
            if text.strip() and score >= MIN_CONFIDENCE:
                fragments.append(_fragment(quad, text.strip(), float(score), image))

        elements = _number(_merge(fragments))
        logger.info(
            "detected fragments=%d elements=%d ocr_ms=%.0f",
            len(fragments),
            len(elements),
            (time.perf_counter() - started) * 1000,
        )
        return elements

    def _load_engine(self) -> Any:
        if self._engine is None:
            try:
                # RapidOCR itself reports a missing onnxruntime only on the first call.
                import onnxruntime  # noqa: F401
                from rapidocr import RapidOCR

                self._engine = RapidOCR(params={"Global.log_level": "error"})
            except ImportError as exc:
                raise DetectionError(f"OCR engine not found ({exc}): {INSTALL_HINT}") from exc
            except Exception as exc:  # Raised for a missing or damaged model, among others.
                raise DetectionError(f"cannot load OCR ({exc}): {REINSTALL_HINT}") from exc
        return self._engine


def annotate(screenshot: Screenshot, elements: list[Element]) -> bytes:
    """Return a PNG of the screenshot with each element's box and id drawn on it."""
    image = _decode(screenshot)
    draw = ImageDraw.Draw(image)
    for element in elements:
        box = element.bbox
        draw.rectangle(
            (box.x, box.y, box.x + box.width, box.y + box.height), outline=(255, 0, 80), width=2
        )
        draw.text((box.x, max(box.y - 11, 0)), str(element.id), fill=(255, 0, 80))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _decode(screenshot: Screenshot) -> Image.Image:
    try:
        return Image.open(io.BytesIO(screenshot.data)).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise DetectionError("cannot read the image: not a valid image file") from exc


def _fragment(quad: Any, text: str, score: float, image: Image.Image) -> Element:
    """Build an unnumbered element from a four-point box, kept inside the image."""
    left = max(math.floor(min(point[0] for point in quad)), 0)
    top = max(math.floor(min(point[1] for point in quad)), 0)
    right = min(math.ceil(max(point[0] for point in quad)), image.width)
    bottom = min(math.ceil(max(point[1] for point in quad)), image.height)
    return Element(0, text, BBox(left, top, right - left, bottom - top), score)


def _merge(fragments: list[Element]) -> list[Element]:
    """Join fragments that read as one label: same line, same text size, a word gap apart."""
    merged: list[Element] = []
    for fragment in sorted(fragments, key=lambda item: item.bbox.x):
        for index, element in enumerate(merged):
            if _same_label(element.bbox, fragment.bbox):
                merged[index] = _join(element, fragment)
                break
        else:
            merged.append(fragment)
    return merged


def _same_label(left: BBox, right: BBox) -> bool:
    shorter, taller = sorted((left.height, right.height))
    if shorter == 0 or taller / shorter > MAX_HEIGHT_RATIO:
        return False
    overlap = min(left.y + left.height, right.y + right.height) - max(left.y, right.y)
    gap = right.x - (left.x + left.width)
    return overlap >= shorter / 2 and gap <= shorter * MAX_WORD_GAP and right.x >= left.x


def _join(left: Element, right: Element) -> Element:
    a, b = left.bbox, right.bbox
    x, y = min(a.x, b.x), min(a.y, b.y)
    far_x = max(a.x + a.width, b.x + b.width)
    far_y = max(a.y + a.height, b.y + b.height)
    return Element(
        0,
        f"{left.text} {right.text}",
        BBox(x, y, far_x - x, far_y - y),
        min(left.confidence, right.confidence),
    )


def _number(elements: list[Element]) -> list[Element]:
    """Number from 1, top to bottom and then left to right."""
    ordered = sorted(elements, key=lambda item: (item.bbox.y, item.bbox.x))
    return [replace(element, id=number) for number, element in enumerate(ordered, start=1)]
