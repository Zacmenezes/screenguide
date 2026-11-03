import io
import sys
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from screenguide.capture import Screenshot
from screenguide.cli import main
from screenguide.detection import BBox, DetectionError, Element, RapidOcrDetector, Word
from screenguide.grounding import SetOfMarkGrounding
from screenguide.step_view import crop_view

LABELS = {"File": (40, 30), "Save as": (200, 30), "Preferences": (420, 30), "Help": (40, 150)}


@pytest.fixture
def fixed_detector():
    """Overrides the conftest fixture: these tests exercise the real detector class."""


def render(background, foreground):
    """A generated screen with known labels, and the box each label was drawn in."""
    image = Image.new("RGB", (800, 240), background)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=28)
    boxes = {}
    for label, position in LABELS.items():
        draw.text(position, label, font=font, fill=foreground)
        boxes[label] = draw.textbbox(position, label, font=font)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), boxes


def quad(x, y, width, height):
    return [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]


class FakeEngine:
    """Returns the given fragments; a fourth item, if any, is the fragment's word results."""

    def __init__(self, *fragments):
        self.fragments = fragments

    def __call__(self, image, return_word_box=False):
        if not self.fragments:
            return SimpleNamespace(boxes=None, txts=None, scores=None, word_results=None)
        boxes, texts, scores = zip(*(fragment[:3] for fragment in self.fragments), strict=True)
        words = tuple(fragment[3] if len(fragment) > 3 else () for fragment in self.fragments)
        return SimpleNamespace(
            boxes=boxes, txts=texts, scores=scores, word_results=words if return_word_box else None
        )


def blank_screenshot():
    output = io.BytesIO()
    Image.new("RGB", (800, 600)).save(output, format="PNG")
    return Screenshot(data=output.getvalue())


def detect(*fragments):
    return RapidOcrDetector(engine=FakeEngine(*fragments)).detect(blank_screenshot())


def test_returns_id_text_bbox_and_confidence():
    assert detect((quad(10.4, 20.6, 50, 20), "File", 0.9)) == [
        Element(1, "File", BBox(10, 20, 51, 21), 0.9)
    ]


def test_merges_words_of_one_label_keeping_the_lowest_confidence():
    elements = detect(
        (quad(100, 10, 40, 20), "Save", 0.9),
        (quad(146, 10, 20, 20), "as", 0.8),
    )

    assert elements == [Element(1, "Save as", BBox(100, 10, 66, 20), 0.8)]


def test_merged_element_keeps_the_box_of_each_fragment():
    (element,) = detect(
        (quad(100, 10, 40, 20), "Save", 0.9),
        (quad(146, 10, 20, 20), "as", 0.8),
    )

    assert element.words == (Word("Save", BBox(100, 10, 40, 20)), Word("as", BBox(146, 10, 20, 20)))


def test_a_line_of_several_words_keeps_the_box_of_each_word():
    words = (("File", 0.9, quad(10, 5, 30, 20)), ("Edit", 0.9, quad(60, 5, 30, 21)))

    (element,) = detect((quad(10, 5, 78, 20), "File Edit", 0.9, words))

    assert element.bbox == BBox(10, 5, 78, 20)
    # "Edit" sticks out of its line by a few pixels and is cut to it.
    assert element.words == (Word("File", BBox(10, 5, 30, 20)), Word("Edit", BBox(60, 5, 28, 20)))


def test_words_that_do_not_spell_the_line_give_the_line_as_one_word():
    words = (("Fi", 0.9, quad(10, 5, 30, 20)),)

    (element,) = detect((quad(10, 5, 78, 20), "File Edit", 0.9, words))

    assert element.words == (Word("File Edit", BBox(10, 5, 78, 20)),)


def test_arrow_on_a_tab_merged_with_its_neighbours_lands_on_the_named_tab():
    """Regression for #49, with the fragments RapidOCR found on a real tab bar.

    The four tabs are a word gap apart, so they merge into one element; the arrow used to land
    on its centre, between "Domains" and "Access".
    """
    fragments = {
        "Issues": (474, 80, 539, 105),
        "Domains": (549, 78, 653, 105),
        "Access": (640, 76, 721, 106),
        "Settings": (729, 75, 813, 109),
    }
    screen = blank_screenshot()
    engine = FakeEngine(
        *(
            (quad(left, top, right - left, bottom - top), text, 1.0)
            for text, (left, top, right, bottom) in fragments.items()
        )
    )
    (element,) = RapidOcrDetector(engine=engine).detect(screen)
    grounding = SetOfMarkGrounding()
    marked = grounding.mark(screen, [element])

    view = crop_view(grounding.resolve(marked, element.id, "Settings"), (800, 600))

    left, top, right, bottom = fragments["Settings"]
    tip_x = view.region.x + view.tip.x * view.region.width / view.width
    tip_y = view.region.y + view.tip.y * view.region.height / view.height
    assert element.text == "Issues Domains Access Settings"
    assert left <= tip_x <= right and top <= tip_y <= bottom


@pytest.mark.parametrize(
    "second",
    [
        pytest.param((quad(160, 10, 40, 20), "Edit", 0.9), id="a full height away"),
        pytest.param((quad(145, 40, 40, 20), "Edit", 0.9), id="on the next line"),
        pytest.param((quad(145, 10, 40, 40), "Edit", 0.9), id="in a larger size"),
    ],
)
def test_keeps_separate_labels_apart(second):
    elements = detect((quad(100, 10, 40, 20), "File", 0.9), second)

    assert [element.text for element in elements] == ["File", "Edit"]


def test_discards_empty_and_low_confidence_results():
    elements = detect(
        (quad(0, 0, 40, 20), "   ", 0.9),
        (quad(0, 40, 40, 20), "noise", 0.49),
        (quad(0, 80, 40, 20), "Help", 0.5),
    )

    assert [element.text for element in elements] == ["Help"]


def test_numbers_from_one_in_reading_order():
    elements = detect(
        (quad(0, 100, 40, 20), "Third", 0.9),
        (quad(300, 10, 40, 20), "Second", 0.9),
        (quad(0, 10, 40, 20), "First", 0.9),
    )

    assert [(e.id, e.text) for e in elements] == [(1, "First"), (2, "Second"), (3, "Third")]


def test_boxes_stay_inside_the_image():
    assert detect((quad(-5, -5, 900, 700), "Edge", 0.9))[0].bbox == BBox(0, 0, 800, 600)


def test_no_text_gives_no_elements():
    assert detect() == []


def test_no_text_with_the_empty_word_result_the_engine_reports_gives_no_elements():
    def engine(image, **_):
        return SimpleNamespace(boxes=None, txts=None, scores=None, word_results=(("", 1.0, None),))

    assert RapidOcrDetector(engine=engine).detect(blank_screenshot()) == []


@pytest.mark.parametrize("module", ["rapidocr", "onnxruntime"])
def test_missing_engine_gives_install_instructions(monkeypatch, module):
    monkeypatch.setitem(sys.modules, module, None)

    with pytest.raises(DetectionError, match="pip install"):
        RapidOcrDetector().detect(blank_screenshot())


def test_model_that_fails_to_load_gives_install_instructions(monkeypatch):
    def fail(**_):
        raise FileNotFoundError("PP-OCRv6_det_small.onnx")

    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "rapidocr", SimpleNamespace(RapidOCR=fail))

    with pytest.raises(DetectionError, match=r"\.onnx.*--force-reinstall"):
        RapidOcrDetector().detect(blank_screenshot())


def test_model_that_fails_on_first_use_gives_install_instructions():
    def fail(image, **_):
        raise RuntimeError("Failed to download PP-OCRv6_det_small.onnx")

    with pytest.raises(DetectionError, match=r"\.onnx.*--force-reinstall"):
        RapidOcrDetector(engine=fail).detect(blank_screenshot())


def test_unreadable_image_gives_a_clear_error():
    with pytest.raises(DetectionError, match="cannot read the image"):
        RapidOcrDetector(engine=FakeEngine()).detect(Screenshot(data=b"not an image"))


def test_detect_command_reports_a_missing_engine(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "rapidocr", None)
    image = tmp_path / "screen.png"
    image.write_bytes(blank_screenshot().data)

    assert main(["detect", "--image", str(image), "--output", str(tmp_path / "out.png")]) == 1
    assert "pip install" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("background", "foreground"),
    [pytest.param("white", "black", id="light"), pytest.param("#1e1e22", "#d0d0d0", id="dark")],
)
def test_real_engine_finds_each_label_where_it_was_drawn(background, foreground):
    pytest.importorskip("rapidocr")
    pytest.importorskip("onnxruntime")
    data, drawn = render(background, foreground)

    elements = {e.text: e for e in RapidOcrDetector().detect(Screenshot(data=data))}

    assert set(elements) == set(LABELS)  # "Save as" is one element.
    for label, (left, top, right, bottom) in drawn.items():
        box = elements[label].bbox
        assert box.x <= left + 4 and box.x + box.width >= right - 4
        assert box.y <= top + 4 and box.y + box.height >= bottom - 4
        assert elements[label].confidence >= 0.5


def test_detect_command_writes_the_marked_image(tmp_path, capsys):
    pytest.importorskip("rapidocr")
    pytest.importorskip("onnxruntime")
    image, output = tmp_path / "screen.png", tmp_path / "marked.png"
    image.write_bytes(render("#1e1e22", "#d0d0d0")[0])

    assert main(["detect", "--image", str(image), "--output", str(output)]) == 0

    assert "'Save as'" in capsys.readouterr().out
    assert Image.open(output).size == (800, 240)
