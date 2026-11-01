import io
from itertools import combinations

import pytest
from PIL import Image, ImageFont

from screenguide.capture import Screenshot
from screenguide.detection import BBox, DetectionError, Element
from screenguide.grounding import (
    DARK,
    FONT_SIZE,
    LIGHT,
    MarkedScreen,
    SetOfMarkGrounding,
    UnknownElementError,
    _layout_labels,
    _overlap,
)

ELEMENT = Element(7, "File", BBox(1000, 500, 200, 100), 0.9)


def screenshot(size=(2000, 1000), color="gray"):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return Screenshot(data=output.getvalue())


def pixels(marked: MarkedScreen) -> Image.Image:
    return Image.open(io.BytesIO(marked.image.data)).convert("RGB")


def test_uses_the_same_ids_in_the_elements_and_the_list():
    elements = [ELEMENT, Element(9, "Save as", BBox(10, 10, 50, 20), 0.8)]

    marked = SetOfMarkGrounding().mark(screenshot(), elements)

    assert list(marked.elements) == [7, 9]
    assert marked.id_list == "7: File\n9: Save as"


def test_chosen_number_maps_back_to_its_bbox():
    grounding = SetOfMarkGrounding()
    marked = grounding.mark(screenshot(), [ELEMENT])

    assert grounding.resolve(marked, 7) == ELEMENT.bbox


def test_unknown_number_is_an_explicit_error():
    grounding = SetOfMarkGrounding()
    marked = grounding.mark(screenshot(), [ELEMENT])

    with pytest.raises(UnknownElementError, match="99"):
        grounding.resolve(marked, 99)


def test_downscale_limits_the_long_edge_and_reports_the_size_sent():
    marked = SetOfMarkGrounding(max_long_edge=1000).mark(screenshot(), [ELEMENT])

    assert pixels(marked).size == (1000, 500)
    assert (marked.image.width, marked.image.height) == (1000, 500)


def test_long_edge_limit_applies_to_portrait_images():
    marked = SetOfMarkGrounding(max_long_edge=500).mark(screenshot((1000, 2000)), [])

    assert pixels(marked).size == (250, 500)


def test_smaller_image_is_not_upscaled():
    marked = SetOfMarkGrounding(max_long_edge=4000).mark(screenshot(), [])

    assert pixels(marked).size == (2000, 1000)


def test_downscale_round_trip_keeps_the_original_bbox_and_draws_at_the_scaled_spot():
    original = (1921, 1081)
    box = BBox(1000, 500, 300, 120)
    element = Element(1, "File", box, 0.9)
    grounding = SetOfMarkGrounding(max_long_edge=1000)

    marked = grounding.mark(screenshot(original), [element])

    assert grounding.resolve(marked, 1) == box
    image = pixels(marked)
    scale = original[0] / image.width
    middle = round((box.y + box.height / 2) / scale)
    drawn = [x for x in range(image.width) if image.getpixel((x, middle)) == LIGHT]
    assert abs(drawn[0] * scale - box.x) <= 2 * scale
    assert abs(drawn[-1] * scale - (box.x + box.width)) <= 2 * scale


def test_empty_element_list_gives_a_clean_image_and_resolves_nothing():
    grounding = SetOfMarkGrounding(max_long_edge=1000)

    marked = grounding.mark(screenshot(color="white"), [])

    assert marked.elements == {}
    assert marked.id_list == ""
    assert pixels(marked).getcolors() == [(500000, (255, 255, 255))]
    with pytest.raises(UnknownElementError):
        grounding.resolve(marked, 1)


def test_label_is_identical_and_two_toned_on_light_and_dark_backgrounds():
    """An opaque label carries its own contrast, whatever lies behind it."""
    grounding = SetOfMarkGrounding()
    element = Element(128, "File", BBox(100, 100, 80, 20), 0.9)
    box = {128: (100, 100, 180, 120)}
    label = _layout_labels(box, ImageFont.load_default(size=FONT_SIZE), (400, 200))[128]
    area = (label[0], label[1], label[2] + 1, label[3] + 1)

    light = pixels(grounding.mark(screenshot((400, 200), "white"), [element])).crop(area)
    dark = pixels(grounding.mark(screenshot((400, 200), "#1e1e22"), [element])).crop(area)

    assert light.tobytes() == dark.tobytes()
    colors = {color for _, color in light.getcolors(maxcolors=4096)}
    assert {LIGHT, DARK} <= colors


def test_box_outline_shows_on_light_and_dark_backgrounds():
    grounding = SetOfMarkGrounding()
    element = Element(1, "File", BBox(100, 100, 80, 20), 0.9)

    for color in ("white", "black"):
        image = pixels(grounding.mark(screenshot((400, 200), color), [element]))
        assert image.getpixel((100, 110)) == LIGHT
        assert image.getpixel((99, 110)) == (0, 0, 0)


def test_labels_on_a_dense_screen_stay_inside_and_do_not_cover_each_other():
    bounds = (800, 300)
    boxes = {
        n * 20 + m + 1: (10 + m * 78, 40 + n * 50, 80 + m * 78, 60 + n * 50)
        for n in range(5)
        for m in range(10)
    }

    labels = list(_layout_labels(boxes, ImageFont.load_default(size=FONT_SIZE), bounds).values())

    for label in labels:
        assert label[0] >= 0 and label[1] >= 0
        assert label[2] < bounds[0] and label[3] < bounds[1]
    for first, second in combinations(labels, 2):
        assert _overlap(first, second) == 0


def test_box_touching_the_edges_is_drawn_and_keeps_the_image_size():
    element = Element(1, "All", BBox(0, 0, 400, 200), 0.9)

    image = pixels(SetOfMarkGrounding().mark(screenshot((400, 200)), [element]))

    assert image.size == (400, 200)
    assert image.getpixel((399, 199)) == LIGHT


def test_unreadable_image_is_a_clear_error():
    with pytest.raises(DetectionError, match="cannot read the image"):
        SetOfMarkGrounding().mark(Screenshot(data=b"not an image"), [])
