"""Qt offscreen smoke tests: the panel renders, with no real display."""

import io
import os
import signal

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PIL import Image  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from screenguide.capture import Screenshot  # noqa: E402
from screenguide.cli import main  # noqa: E402
from screenguide.detection import BBox  # noqa: E402
from screenguide.panel import (  # noqa: E402
    ACCENT,
    BACKGROUND,
    StepPanel,
    demo_steps,
    render_crop,
)
from screenguide.session import Step  # noqa: E402
from screenguide.step_view import crop_view  # noqa: E402

BOX = BBox(900, 500, 60, 20)


@pytest.fixture(scope="module", autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def screenshot():
    output = io.BytesIO()
    Image.new("RGB", (1920, 1080), "white").save(output, format="PNG")
    return Screenshot(data=output.getvalue())


def step(bbox=BOX, label="Layout", done=False):
    return Step(label, "Click the Layout tab, between Design and References.", bbox, done, 0.8)


def test_panel_stays_on_top_with_a_see_through_background():
    panel = StepPanel("Goal")

    assert panel.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert panel.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert 0 < BACKGROUND.alpha() < 255


def test_panel_renders_instruction_crop_and_next(screenshot):
    panel = StepPanel("Increase the margin")
    panel.show_step(step(), screenshot)
    panel.show()

    assert ACCENT in panel.instruction.text()
    assert ">Layout</span>" in panel.instruction.text()
    assert panel.crop.isVisible()
    assert panel.crop.pixmap().width() == 520
    assert panel.next_button.isVisible() and panel.next_button.isEnabled()
    assert not panel.grab().isNull()


def test_step_without_a_box_leaves_no_empty_crop_area(screenshot):
    panel = StepPanel("Goal")
    panel.show()
    panel.show_step(step(), screenshot)
    with_crop = panel.height()

    panel.show_step(step(bbox=None, label=None), screenshot)

    assert not panel.crop.isVisible()
    assert panel.crop.pixmap().isNull()
    assert with_crop - panel.height() >= 200
    assert panel.instruction.text().startswith("Click the Layout")


def test_crop_comes_back_after_a_step_without_a_box(screenshot):
    panel = StepPanel("Goal")
    panel.show()
    panel.show_step(step(bbox=None), screenshot)
    panel.show_step(step(), screenshot)

    assert panel.crop.isVisible()
    assert not panel.crop.pixmap().isNull()


def test_goal_is_shown_as_plain_text():
    panel = StepPanel("<b>Goal</b>")

    assert panel._goal.textFormat() == Qt.TextFormat.PlainText


def test_next_button_emits_the_signal():
    panel = StepPanel("Goal")
    presses = []
    panel.next_requested.connect(lambda: presses.append(True))

    panel.next_button.click()

    assert presses == [True]


def test_no_next_step_once_done(screenshot):
    panel = StepPanel("Goal")
    panel.show_step(step(bbox=None, label=None, done=True), screenshot)

    assert not panel.next_button.isEnabled()


def test_arrow_is_drawn_at_its_computed_place_on_top_of_the_right_pixels():
    image = Image.new("RGB", (1920, 1080), "white")
    image.paste((255, 0, 0), (900, 500, 960, 520))
    view = crop_view(BOX, image.size)

    rendered = render_crop(image, view).toImage()

    assert (rendered.width(), rendered.height()) == (view.width, view.height)
    middle_x = round((view.tip.x + view.tail.x) / 2)
    middle_y = round((view.tip.y + view.tail.y) / 2)
    assert rendered.pixelColor(middle_x, middle_y) == QColor(ACCENT)
    # The target's red, away from the arrow, shows the crop came from the right place.
    assert rendered.pixelColor(round(view.tip.x) - 50, round(view.tip.y)) == QColor(255, 0, 0)
    assert rendered.pixelColor(2, 2) == QColor(255, 255, 255)


def test_crop_is_rendered_sharp_for_the_display():
    image = Image.new("RGB", (3840, 2160), "white")

    pixmap = render_crop(image, crop_view(BOX, image.size), 2.0)

    assert pixmap.width() == 1040
    assert pixmap.deviceIndependentSize().width() == 520


def test_demo_steps_cover_each_case_inside_any_image():
    for size in ((1920, 1080), (40, 10)):
        steps = demo_steps(size)

        assert [s.done for s in steps] == [False, False, False, True]
        assert [s.bbox is None for s in steps] == [False, False, True, True]
        for s in steps[:2]:
            assert s.bbox.x + s.bbox.width <= size[0]
            assert s.bbox.y + s.bbox.height <= size[1]


@pytest.fixture
def quiet_cli(monkeypatch):
    """`panel` without blocking on the event loop or changing this process's Ctrl-C handling."""
    shown = []
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    monkeypatch.setattr(QApplication, "exec", lambda self=None: 0)
    monkeypatch.setattr(StepPanel, "show", lambda self: shown.append(self))
    return shown


def test_panel_command_shows_the_demo_and_walks_through_its_steps(image, quiet_cli):
    assert main(["panel", "--image", str(image), "--demo-step"]) == 0

    panel = quiet_cli[0]
    assert ">Target</span>" in panel.instruction.text()
    assert not panel.crop.isHidden()
    panel.next_button.click()
    assert ">Corner</span>" in panel.instruction.text()
    panel.next_button.click()
    assert panel.crop.isHidden()
    panel.next_button.click()
    assert not panel.next_button.isEnabled()


def test_panel_command_needs_the_demo_flag(image):
    with pytest.raises(SystemExit):
        main(["panel", "--image", str(image)])


def test_panel_command_reports_a_missing_image(tmp_path, capsys):
    assert main(["panel", "--image", str(tmp_path / "none.png"), "--demo-step"]) == 1
    assert capsys.readouterr().err.startswith("error:")


def test_panel_command_reports_an_unreadable_image(tmp_path, capsys):
    path = tmp_path / "broken.png"
    path.write_bytes(b"not an image")

    assert main(["panel", "--image", str(path), "--demo-step"]) == 1
    assert capsys.readouterr().err.startswith("error:")


def test_panel_command_reports_a_missing_x_display(image, monkeypatch, capsys):
    monkeypatch.setenv("QT_QPA_PLATFORM", "xcb")
    monkeypatch.delenv("DISPLAY", raising=False)

    assert main(["panel", "--image", str(image), "--demo-step"]) == 1
    assert "DISPLAY" in capsys.readouterr().err
