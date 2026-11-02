"""What the panel shows for a step, worked out without a display: crop, arrow and instruction."""

import html
import re
from dataclasses import dataclass

from screenguide.detection import BBox

# The largest the crop is drawn, in panel pixels.
CROP_WIDTH = 520
CROP_HEIGHT = 200
# The smallest region shown, so that a small target appears at twice its size with context around.
MIN_REGION_WIDTH = CROP_WIDTH // 2
MIN_REGION_HEIGHT = CROP_HEIGHT // 2
# How much wider and taller than its target a large region is.
CONTEXT = 1.5
# Along each axis, in panel pixels.
ARROW_REACH = 40
# Room kept between the arrow and the crop's edge, for its outline.
ARROW_MARGIN = 3
# How far into the target the tip goes, as a fraction of its height.
TIP_INSET = 0.25


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class CropView:
    """A region of the captured image and the arrow on top of it, once drawn at `width`x`height`."""

    region: BBox
    width: int
    height: int
    tip: Point
    tail: Point


def crop_view(bbox: BBox, image_size: tuple[int, int]) -> CropView:
    target = _clamp_to_image(bbox, image_size)
    region = _region_around(target, image_size)
    scale = min(CROP_WIDTH / region.width, CROP_HEIGHT / region.height)
    width = max(round(region.width * scale), 1)
    height = max(round(region.height * scale), 1)
    tip, tail = _arrow(target, region, scale, (width, height))
    return CropView(region, width, height, tip, tail)


def _clamp_to_image(bbox: BBox, image_size: tuple[int, int]) -> BBox:
    image_width, image_height = image_size
    x = min(max(bbox.x, 0), image_width - 1)
    y = min(max(bbox.y, 0), image_height - 1)
    right = min(bbox.x + bbox.width, image_width)
    bottom = min(bbox.y + bbox.height, image_height)
    return BBox(x, y, max(right - x, 1), max(bottom - y, 1))


def _region_around(target: BBox, image_size: tuple[int, int]) -> BBox:
    """A region with the crop's proportions, centred on the target unless an edge is in the way."""
    width = max(target.width * CONTEXT, MIN_REGION_WIDTH)
    height = max(target.height * CONTEXT, MIN_REGION_HEIGHT)
    aspect = CROP_WIDTH / CROP_HEIGHT
    width, height = max(width, height * aspect), max(height, width / aspect)

    width = min(round(width), image_size[0])
    height = min(round(height), image_size[1])
    x = _slide_inside(target.x + target.width / 2 - width / 2, width, image_size[0])
    y = _slide_inside(target.y + target.height / 2 - height / 2, height, image_size[1])
    return BBox(x, y, width, height)


def _slide_inside(start: float, length: int, limit: int) -> int:
    return max(min(round(start), limit - length), 0)


def _arrow(target: BBox, region: BBox, scale: float, size: tuple[int, int]) -> tuple[Point, Point]:
    """An arrow that ends just inside the target and comes from the side with the most room."""
    width, height = size
    x = (target.x - region.x + target.width / 2) * scale
    middle = (target.y - region.y + target.height / 2) * scale
    half_height = target.height * scale / 2
    inset = target.height * scale * TIP_INSET

    from_right = x <= width / 2
    from_below = middle <= height / 2
    y = middle + half_height - inset if from_below else middle - half_height + inset

    room_x = (width - x if from_right else x) - ARROW_MARGIN
    room_y = (height - y if from_below else y) - ARROW_MARGIN
    reach = max(min(ARROW_REACH, room_x, room_y), 0)
    tail = Point(x + reach if from_right else x - reach, y + reach if from_below else y - reach)
    return Point(x, y), tail


def highlight_instruction(instruction: str, target_label: str | None, colour: str) -> str:
    """The instruction as rich text, with the label in colour where it appears as whole words.

    Both come from the model, so all of it is escaped.
    """
    match = None
    if target_label and target_label.strip():
        pattern = rf"(?<!\w){re.escape(target_label.strip())}(?!\w)"
        match = re.search(pattern, instruction, re.IGNORECASE)
    if match is None:
        return html.escape(instruction)
    return (
        f"{html.escape(instruction[: match.start()])}"
        f'<span style="color: {colour}; font-weight: 600;">{html.escape(match.group())}</span>'
        f"{html.escape(instruction[match.end() :])}"
    )
