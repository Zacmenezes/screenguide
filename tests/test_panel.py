"""Qt offscreen smoke tests: the panel renders, with no real display."""

import io
import os
import signal

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PIL import Image  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from screenguide.capture import Screenshot  # noqa: E402
from screenguide.cli import main  # noqa: E402
from screenguide.detection import BBox  # noqa: E402
from screenguide.monitors import WHOLE_DESKTOP, Monitor, MonitorChoice  # noqa: E402
from screenguide.panel import (  # noqa: E402
    ACCENT,
    BACKGROUND,
    TOP_OFFSET,
    Pick,
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
    assert panel.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert panel.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert 0 < BACKGROUND.alpha() < 255


def test_bar_sits_centred_above_the_panel_in_the_same_window():
    panel = StepPanel("Goal")
    panel.appear()

    bar, body = panel.bar.geometry(), panel.body.geometry()
    assert panel.bar.window() is panel and panel.body.window() is panel
    assert bar.bottom() < body.top()
    assert abs(bar.center().x() - panel.width() // 2) <= 1
    assert [b.text() for b in (panel.guide_me_button, panel.hide_button, panel.menu_button)] == [
        "Guide me  +",
        "Hide",
        "⋮",
    ]
    assert panel.new_task_button.isVisible()


def test_window_first_opens_at_the_top_centre_of_its_monitor():
    panel = StepPanel("Goal")
    panel.appear()

    area = panel.screen().availableGeometry()
    assert abs(panel.geometry().center().x() - area.center().x()) <= 1
    assert panel.y() == area.y() + TOP_OFFSET

    panel.move(area.x(), area.y() + 300)
    panel.hide()
    panel.appear()
    assert panel.pos() == area.topLeft() + QPoint(0, 300)


def test_dragging_from_the_bar_or_the_panel_moves_the_whole_window(monkeypatch):
    panel = StepPanel("Goal")
    panel.appear()
    moves = []
    monkeypatch.setattr(panel.windowHandle(), "startSystemMove", lambda: moves.append(True) or True)

    for widget in (panel.bar, panel.body):
        QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(4, widget.height() // 2))

    assert len(moves) == 2


def test_hide_folds_the_panel_under_the_bar_and_guide_me_brings_it_back(screenshot):
    panel = StepPanel("Goal")
    panel.show_step(step(), screenshot)
    panel.appear()
    unfolded = panel.height()

    panel.hide_button.click()

    assert panel.folded and panel.isVisible()
    assert panel.bar.isVisible() and not panel.body.isVisible()
    assert panel.height() == panel.bar.height()

    panel.guide_me_button.click()

    assert not panel.folded and panel.instruction.isVisible()
    assert panel.height() == unfolded


def test_guide_me_focuses_the_goal_entry():
    panel = StepPanel()
    panel.show_goal_entry()
    panel.appear()
    panel.fold()
    panel.start_button.setFocus()

    panel.guide_me_button.click()

    assert panel.focusWidget() is panel.goal_input


def test_new_task_and_close_end_the_task_and_close_also_folds():
    panel = StepPanel("Goal")
    panel.appear()
    ended = []
    panel.task_ended.connect(lambda: ended.append(True))

    panel.new_task_button.click()
    assert ended == [True] and not panel.folded

    panel._close.click()
    assert ended == [True, True] and panel.folded and panel.isVisible()


def test_usage_and_quit_are_menu_entries():
    panel = StepPanel("Goal")
    asked = []
    panel.usage_requested.connect(lambda: asked.append("usage"))
    panel.quit_requested.connect(lambda: asked.append("quit"))

    panel.usage_action.trigger()
    panel.quit_action.trigger()

    assert asked == ["usage", "quit"]
    assert [a.text() for a in panel.menu.actions() if a.text()] == [
        "Capture monitor",
        "Usage",
        "Quit",
    ]


def test_usage_window_shows_the_summary_read_only():
    panel = StepPanel("Goal")

    panel.show_usage("by day\n2026-11-02  1 call")
    panel.show_usage("by day\n2026-11-02  2 calls")

    window = panel._usage_window
    assert window.isVisible() and window.text.isReadOnly()
    assert window.text.toPlainText().endswith("2 calls")
    assert window.parent() is panel


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


# Faked layout: connectors and names as Mutter reports them, nothing else is read.
MONITORS = [
    Monitor("eDP-1", "Built-in display", 1920, 1200, 1.25, 2560, 1200, 1536, 960),
    Monitor("DP-3", 'Dell Inc. 24"', 1920, 1080, 1.0, 303, 0, 1920, 1080),
]


@pytest.fixture
def screen(monkeypatch):
    """The panel's screen, as Qt would name it; tests move the panel by changing it."""
    where = {"name": "DP-3"}
    monkeypatch.setattr(StepPanel, "screen_name", lambda self: where["name"])
    return where


def checked(panel):
    return [text for text, is_checked in panel.monitor_entries() if is_checked]


def test_monitor_choices_wait_until_there_are_monitors_to_list():
    panel = StepPanel("Goal")
    panel.show()

    assert not panel.monitor_menu.isEnabled()
    assert not panel.capture_source.isVisible()

    panel.set_monitors(MONITORS)
    assert panel.monitor_menu.isEnabled() and panel.capture_source.isVisible()


def test_menu_lists_follow_each_monitor_by_connector_and_name_and_the_desktop(screen):
    panel = StepPanel("Goal")
    panel.set_monitors(MONITORS)

    assert panel.monitor_entries() == [
        ("Follow panel", True),
        ('DP-3  Dell Inc. 24"', False),
        ("eDP-1  Built-in display", False),
        ("Whole desktop", False),
    ]
    assert panel.monitor_menu in [action.menu() for action in panel.menu.actions()]


def test_picking_a_menu_entry_checks_it_alone(screen):
    panel = StepPanel("Goal")
    panel.set_monitors(MONITORS)

    entry = next(a for a in panel.monitor_menu.actions() if a.text().startswith("eDP-1"))
    entry.trigger()

    assert checked(panel) == ["eDP-1  Built-in display"]
    assert panel.monitor_choice() == MonitorChoice("eDP-1")

    panel.set_monitors(MONITORS)
    assert checked(panel) == ["eDP-1  Built-in display"]


def test_following_reads_the_panel_screen_each_time_it_is_asked(screen):
    panel = StepPanel("Goal")
    panel.set_monitors(MONITORS)
    panel.show()

    assert panel.monitor_choice() == MonitorChoice("DP-3", follows_panel=True)
    assert panel.capture_source.text() == 'Next capture: DP-3  Dell Inc. 24", where the panel is'

    screen["name"] = "eDP-1"
    panel.move(panel.x() + 10, panel.y())
    QApplication.processEvents()

    assert panel.monitor_choice() == MonitorChoice("eDP-1", follows_panel=True)
    assert panel.capture_source.text() == (
        "Next capture: eDP-1  Built-in display, where the panel is"
    )


def test_screen_that_matches_no_monitor_is_shown(screen):
    screen["name"] = "XWAYLAND0"
    panel = StepPanel("Goal")
    panel.set_monitors(MONITORS)

    assert panel.capture_source.text() == "Next capture: screen XWAYLAND0, which matches no monitor"


def test_pinning_stays_put_until_follow_is_picked_again(screen):
    panel = StepPanel("Goal")
    panel.set_monitors(MONITORS)

    panel.select_monitor("eDP-1")
    screen["name"] = "DP-3"
    assert panel.monitor_choice() == MonitorChoice("eDP-1")
    assert panel.capture_source.text() == "Next capture: eDP-1  Built-in display"

    panel.select_monitor(Pick.FOLLOW_PANEL)
    assert panel.monitor_choice() == MonitorChoice("DP-3", follows_panel=True)


def test_whole_desktop_is_an_explicit_choice(screen):
    panel = StepPanel("Goal")
    panel.set_monitors(MONITORS)

    panel.select_monitor(Pick.WHOLE_DESKTOP)

    assert panel.monitor_choice() == WHOLE_DESKTOP
    assert panel.capture_source.text() == "Next capture: the whole desktop, every monitor"


def test_starting_monitor_is_selected(screen):
    panel = StepPanel("Goal", monitor="eDP-1")
    panel.set_monitors(MONITORS)

    assert checked(panel) == ["eDP-1  Built-in display"]
    assert panel.monitor_choice() == MonitorChoice("eDP-1")


def test_disconnected_pinned_monitor_stays_selected_and_says_so(screen):
    panel = StepPanel("Goal", monitor="HDMI-9")
    panel.set_monitors(MONITORS)

    assert checked(panel) == ["HDMI-9  (not connected)"]
    assert panel.monitor_choice() == MonitorChoice("HDMI-9")
    assert panel.capture_source.text() == "Next capture: HDMI-9, not connected"

    panel.set_monitors(MONITORS[:1])
    panel.select_monitor("eDP-1")
    panel.set_monitors(MONITORS[1:])
    assert checked(panel) == ["eDP-1  (not connected)"]
