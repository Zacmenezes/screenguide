"""Crop geometry, arrow placement and the highlighted instruction, with no display and no Qt."""

import subprocess
import sys

import pytest

from screenguide.detection import BBox
from screenguide.step_view import (
    CROP_HEIGHT,
    CROP_WIDTH,
    MIN_REGION_WIDTH,
    crop_view,
    highlight_instruction,
)

SCREEN = (1920, 1080)


def in_crop(view, bbox):
    """The bbox in the coordinates of the crop as drawn."""
    scale = view.width / view.region.width
    left = (bbox.x - view.region.x) * scale
    top = (bbox.y - view.region.y) * scale
    return left, top, left + bbox.width * scale, top + bbox.height * scale


def test_small_target_is_enlarged_with_context_around_it():
    bbox = BBox(900, 500, 60, 20)
    view = crop_view(bbox, SCREEN)

    assert view.region.width == MIN_REGION_WIDTH
    assert view.width / view.region.width == 2
    assert view.region.x < bbox.x and view.region.x + view.region.width > bbox.x + bbox.width
    assert view.region.y < bbox.y and view.region.y + view.region.height > bbox.y + bbox.height
    assert (view.width, view.height) == (CROP_WIDTH, CROP_HEIGHT)


def test_target_is_centred_away_from_the_edges():
    view = crop_view(BBox(900, 500, 60, 20), SCREEN)

    assert view.region.x + view.region.width / 2 == 930
    assert view.region.y + view.region.height / 2 == 510


@pytest.mark.parametrize(
    "bbox",
    [
        BBox(0, 0, 40, 20),
        BBox(1880, 0, 40, 20),
        BBox(0, 1060, 40, 20),
        BBox(1880, 1060, 40, 20),
    ],
    ids=["top-left", "top-right", "bottom-left", "bottom-right"],
)
def test_region_near_a_corner_slides_inside_the_image_and_keeps_its_size(bbox):
    view = crop_view(bbox, SCREEN)
    region = view.region

    assert region.x >= 0 and region.y >= 0
    assert region.x + region.width <= SCREEN[0] and region.y + region.height <= SCREEN[1]
    assert (view.width, view.height) == (CROP_WIDTH, CROP_HEIGHT)
    assert region.x <= bbox.x and region.x + region.width >= bbox.x + bbox.width
    assert region.y <= bbox.y and region.y + region.height >= bbox.y + bbox.height


def test_region_never_exceeds_an_image_smaller_than_it():
    view = crop_view(BBox(10, 10, 20, 10), (100, 40))

    assert view.region == BBox(0, 0, 100, 40)


def test_target_wider_than_the_minimum_region_stays_whole():
    bbox = BBox(100, 500, 1000, 40)
    view = crop_view(bbox, SCREEN)

    assert view.region.x <= bbox.x
    assert view.region.x + view.region.width >= bbox.x + bbox.width
    assert view.width <= CROP_WIDTH and view.height <= CROP_HEIGHT


def test_tall_narrow_image_fits_the_crop_area():
    view = crop_view(BBox(0, 500, 50, 400), (50, 2000))

    assert view.height == CROP_HEIGHT
    assert view.width < CROP_WIDTH


def test_box_reaching_past_the_image_is_clamped():
    view = crop_view(BBox(1900, 1070, 100, 50), SCREEN)

    assert view.region.x + view.region.width == SCREEN[0]
    assert view.region.y + view.region.height == SCREEN[1]


@pytest.mark.parametrize(
    ("bbox", "size"),
    [
        (BBox(900, 500, 60, 20), SCREEN),
        (BBox(0, 0, 40, 20), SCREEN),
        (BBox(1880, 0, 40, 20), SCREEN),
        (BBox(0, 1060, 40, 20), SCREEN),
        (BBox(1880, 1060, 40, 20), SCREEN),
        (BBox(100, 500, 1000, 40), SCREEN),
        (BBox(0, 0, 200, 100), (200, 100)),
        (BBox(0, 500, 50, 400), (50, 2000)),
        (BBox(0, 2, 300, 6), (300, 10)),
    ],
)
def test_arrow_tip_is_on_the_element_and_the_whole_arrow_inside_the_crop(bbox, size):
    view = crop_view(bbox, size)
    left, top, right, bottom = in_crop(view, bbox)

    assert left <= view.tip.x <= right
    assert top <= view.tip.y <= bottom
    for point in (view.tip, view.tail):
        assert 0 <= point.x <= view.width
        assert 0 <= point.y <= view.height


@pytest.mark.parametrize(
    ("bbox", "right", "below"),
    [
        (BBox(0, 0, 40, 20), True, True),
        (BBox(1880, 0, 40, 20), False, True),
        (BBox(0, 1060, 40, 20), True, False),
        (BBox(1880, 1060, 40, 20), False, False),
    ],
)
def test_arrow_comes_from_the_side_with_room(bbox, right, below):
    view = crop_view(bbox, SCREEN)

    assert (view.tail.x > view.tip.x) is right
    assert (view.tail.y > view.tip.y) is below


def test_arrow_has_length_in_a_normal_crop():
    view = crop_view(BBox(900, 500, 60, 20), SCREEN)

    assert abs(view.tail.x - view.tip.x) == 40
    assert abs(view.tail.y - view.tip.y) == 40


def test_highlight_colours_the_label_inside_the_instruction():
    text = highlight_instruction("Click the Layout tab, near Design.", "Layout", "#123456")

    assert text == (
        'Click the <span style="color: #123456; font-weight: 600;">Layout</span> tab, near Design.'
    )


def test_highlight_keeps_the_instruction_casing():
    assert ">layout</span>" in highlight_instruction("Open the layout tab.", "Layout", "red")


def test_highlight_skips_a_label_inside_another_word():
    text = highlight_instruction("Open Profile, then File.", "File", "red")

    assert text == 'Open Profile, then <span style="color: red; font-weight: 600;">File</span>.'


@pytest.mark.parametrize("label", [None, "", "  ", "Absent"])
def test_instruction_without_its_label_is_plain(label):
    assert highlight_instruction("Click the gear icon.", label, "red") == "Click the gear icon."


def test_model_text_cannot_inject_markup():
    text = highlight_instruction("Press <b>a</b> & <img src=x> then C++.", "C++", "red")

    assert "<b>" not in text and "<img" not in text
    assert "&lt;b&gt;a&lt;/b&gt; &amp;" in text
    assert ">C++</span>" in text


def test_label_with_markup_is_escaped():
    text = highlight_instruction("Open <Tools> now.", "<Tools>", "red")

    assert ">&lt;Tools&gt;</span>" in text


def test_geometry_does_not_load_qt():
    code = "import sys, screenguide.step_view; sys.exit('PySide6' in sys.modules)"

    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
