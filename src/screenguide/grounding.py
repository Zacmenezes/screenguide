"""`layer:grounding`: numbers the boxes and maps the chosen number back to a bbox."""

import io
import logging
from collections.abc import Collection
from dataclasses import dataclass
from typing import Protocol

from PIL import Image, ImageDraw, ImageFont

from screenguide.capture import Screenshot
from screenguide.detection import BBox, Element, decode_image, match_key

logger = logging.getLogger(__name__)

# Yellow next to black keeps both boxes and labels visible on any background: one colour always
# contrasts with what is behind it. These are pixels of the marked image, whatever its scale.
LIGHT = (255, 230, 0)
DARK = (0, 0, 0)
FONT_SIZE = 16
LABEL_PADDING = 3

Rect = tuple[int, int, int, int]  # left, top, right, bottom


class UnknownElementError(LookupError):
    """No box has the chosen number."""


@dataclass(frozen=True)
class MarkedScreen:
    """The image the model sees and the numbered elements it can choose from.

    Each element keeps its bbox in original-image coordinates, whatever the image's scale.
    """

    image: Screenshot
    elements: dict[int, Element]

    @property
    def id_list(self) -> str:
        """One `id: text` line per box, for when a number cannot be read in the image."""
        return "\n".join(f"{id_}: {element.text}" for id_, element in self.elements.items())


class GroundingStrategy(Protocol):
    name: str  # Names the strategy in evaluation runs.

    def mark(self, screenshot: Screenshot, elements: list[Element]) -> MarkedScreen: ...

    def resolve(
        self, marked: MarkedScreen, element_id: int, target_label: str | None = None
    ) -> BBox:
        """Return the bbox for a chosen number; raise `UnknownElementError` if there is none.

        When the box holds several words and `target_label` names some of them, the bbox is
        theirs, so that the arrow points at the target and not at the middle of its line.
        """
        ...


class SetOfMarkGrounding:
    """Draws numbered boxes on the image, downscaled first so they stay readable at sent size."""

    name = "set-of-mark"

    def __init__(self, max_long_edge: int | None = None) -> None:
        self._max_long_edge = max_long_edge

    def mark(self, screenshot: Screenshot, elements: list[Element]) -> MarkedScreen:
        original = decode_image(screenshot)
        image = _downscale(original, self._max_long_edge)
        _draw_marks(image, _scaled_boxes(elements, original.size, image.size))
        logger.info(
            "marked elements=%d original=%dx%d sent=%dx%d",
            len(elements),
            *original.size,
            *image.size,
        )
        output = io.BytesIO()
        image.save(output, format="PNG")
        return MarkedScreen(
            image=Screenshot(data=output.getvalue(), width=image.width, height=image.height),
            elements={element.id: element for element in elements},
        )

    def resolve(
        self, marked: MarkedScreen, element_id: int, target_label: str | None = None
    ) -> BBox:
        try:
            element = marked.elements[element_id]
        except KeyError:
            raise UnknownElementError(f"no box is numbered {element_id}") from None
        return _named_words(element, target_label) or element.bbox


def _named_words(element: Element, target_label: str | None) -> BBox | None:
    """The box of the one run of words that `target_label` names, if it is not all of them."""
    wanted = match_key(target_label or "")
    words = element.words
    if not wanted or len(words) < 2:
        return None
    runs = [
        (start, end)
        for start in range(len(words))
        for end in range(start + 1, len(words) + 1)
        if "".join(match_key(word.text) for word in words[start:end]) == wanted
    ]
    if len(runs) != 1 or runs[0] == (0, len(words)):
        return None
    start, end = runs[0]
    boxes = [word.bbox for word in words[start:end]]
    left = min(box.x for box in boxes)
    top = min(box.y for box in boxes)
    right = max(box.x + box.width for box in boxes)
    bottom = max(box.y + box.height for box in boxes)
    return BBox(left, top, right - left, bottom - top)


def _downscale(image: Image.Image, max_long_edge: int | None) -> Image.Image:
    if max_long_edge is None or max(image.size) <= max_long_edge:
        return image.copy()
    scale = max_long_edge / max(image.size)
    size = (max(round(image.width * scale), 1), max(round(image.height * scale), 1))
    return image.resize(size, Image.Resampling.LANCZOS)


def _scaled_boxes(
    elements: list[Element], original: tuple[int, int], size: tuple[int, int]
) -> dict[int, Rect]:
    """Each element's box in the pixels of the marked image, always inside it."""
    x_scale, y_scale = size[0] / original[0], size[1] / original[1]
    boxes = {}
    for element in elements:
        box = element.bbox
        left = min(round(box.x * x_scale), size[0] - 1)
        top = min(round(box.y * y_scale), size[1] - 1)
        right = min(round((box.x + box.width) * x_scale), size[0] - 1)
        bottom = min(round((box.y + box.height) * y_scale), size[1] - 1)
        boxes[element.id] = (left, top, right, bottom)
    return boxes


def _draw_marks(image: Image.Image, boxes: dict[int, Rect]) -> None:
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=FONT_SIZE)

    # Every box goes first, so that no line crosses a number.
    for left, top, right, bottom in boxes.values():
        draw.rectangle((left - 1, top - 1, right + 1, bottom + 1), outline=DARK)
        draw.rectangle((left, top, right, bottom), outline=LIGHT)

    for element_id, label in _layout_labels(boxes, font, image.size).items():
        draw.rectangle(label, fill=LIGHT, outline=DARK)
        center = ((label[0] + label[2]) / 2, (label[1] + label[3]) / 2)
        draw.text(center, str(element_id), fill=DARK, font=font, anchor="mm")


def _layout_labels(
    boxes: dict[int, Rect], font: ImageFont.ImageFont, bounds: tuple[int, int]
) -> dict[int, Rect]:
    """Choose where each box's number goes, in the pixels of the marked image."""
    labels: dict[int, Rect] = {}
    for element_id, box in boxes.items():
        ink = font.getbbox(str(element_id), anchor="lt")
        size = (ink[2] - ink[0] + 2 * LABEL_PADDING, ink[3] - ink[1] + 2 * LABEL_PADDING)
        labels[element_id] = _place_label(box, size, bounds, labels.values(), boxes.values())
    return labels


def _place_label(
    box: Rect,
    size: tuple[int, int],
    bounds: tuple[int, int],
    placed: Collection[Rect],
    boxes: Collection[Rect],
) -> Rect:
    """Choose the spot around `box` that covers the fewest numbers, then the least of any box."""
    left, top, right, bottom = box
    width, height = size
    spots = (
        (left, top - height),
        (left, bottom + 1),
        (right - width, top - height),
        (right - width, bottom + 1),
        (right + 1, top),
        (left - width, top),
    )
    candidates = [_rect_inside(x, y, size, bounds) for x, y in spots]
    return min(
        candidates,
        key=lambda rect: (
            sum(_overlap(rect, other) for other in placed),
            sum(_overlap(rect, other) for other in boxes),
        ),
    )


def _rect_inside(x: int, y: int, size: tuple[int, int], bounds: tuple[int, int]) -> Rect:
    x = max(min(x, bounds[0] - size[0]), 0)
    y = max(min(y, bounds[1] - size[1]), 0)
    return (x, y, x + size[0] - 1, y + size[1] - 1)


def _overlap(a: Rect, b: Rect) -> int:
    width = min(a[2], b[2]) - max(a[0], b[0]) + 1
    height = min(a[3], b[3]) - max(a[1], b[1]) + 1
    return max(width, 0) * max(height, 0)
