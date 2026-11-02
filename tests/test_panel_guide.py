"""The panel driving a real `GuideSession`, with the capture and the model stubbed."""

import gc
import io
import json
import os
import threading
import time

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PIL import Image  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from conftest import ELEMENTS, FixedDetector  # noqa: E402
from screenguide import panel_guide  # noqa: E402
from screenguide.capture import CaptureError, PortalCapturer, Screenshot  # noqa: E402
from screenguide.cli import main  # noqa: E402
from screenguide.grounding import SetOfMarkGrounding  # noqa: E402
from screenguide.model import STOP_REFUSAL, ModelProviderError, ModelReply  # noqa: E402
from screenguide.monitors import MonitorChoice  # noqa: E402
from screenguide.panel import Pick, StepPanel  # noqa: E402
from screenguide.panel_guide import GuideController, State  # noqa: E402
from screenguide.pricing import PricingTable  # noqa: E402
from screenguide.session import GuideSession  # noqa: E402
from screenguide.usage import JsonlUsageTracker, UsageRecord  # noqa: E402
from test_monitors import MIXED, Requests  # noqa: E402


def reply(label="Settings", element_id=2, done=False):
    return json.dumps(
        {
            "element_id": None if done else element_id,
            "target_label": None if done else label,
            "instruction": f"Click {label}, in the top menu.",
            "done": done,
            "confidence": 0.9,
        }
    )


class Provider:
    name = "scripted"
    model = "scripted-1"
    external = True

    def __init__(self, *replies):
        self.requests = []
        self._replies = list(replies)
        self.release = threading.Event()
        self.release.set()

    def complete(self, request):
        self.requests.append(request)
        self.release.wait(5)
        played = self._replies.pop(0)
        if isinstance(played, Exception):
            raise played
        if isinstance(played, ModelReply):
            return played
        return ModelReply(text=played, input_tokens=10, output_tokens=5)


class Capturer:
    """Records whether the panel was on screen at each capture, and can fail on demand."""

    def __init__(self, panel):
        self.panel = panel
        self.opacity_at_capture = []
        self.failures = []

    def capture(self):
        self.opacity_at_capture.append(self.panel.windowOpacity())
        if self.failures:
            raise self.failures.pop(0)
        output = io.BytesIO()
        Image.new("RGB", (800, 600), "white").save(output, format="PNG")
        return Screenshot(data=output.getvalue())


@pytest.fixture(scope="module", autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def qt_garbage_stays_on_the_ui_thread():
    """Collect here, so the collector cannot destroy a test's widgets later on a worker thread."""
    gc.collect()
    yield
    for thread in threading.enumerate():
        if thread is not threading.main_thread():
            thread.join(5)
    QApplication.processEvents()
    gc.collect()


@pytest.fixture(autouse=True)
def short_settle(monkeypatch):
    monkeypatch.setattr(panel_guide, "LEAVE_SCREEN_MS", 1)


@pytest.fixture(autouse=True)
def layout(monkeypatch):
    """The layout `screenguide panel` lists, faked so no test reads the real one."""
    monkeypatch.setattr("screenguide.cli.read_monitors", lambda: MIXED)


@pytest.fixture
def screen(monkeypatch):
    """The panel's screen, as Qt would name it; tests drag the panel by changing it."""
    where = {"name": "DP-4"}
    monkeypatch.setattr(StepPanel, "screen_name", lambda self: where["name"])
    return where


class Guide:
    def __init__(self, provider, usage_log, factory=None):
        self.panel = StepPanel()
        self.capturer = Capturer(self.panel)
        self.provider = provider
        self.goals = []

        def build(goal, wrap, notify, choose_monitor):
            self.goals.append(goal)
            return GuideSession(
                goal=goal,
                capturer=wrap(self.capturer),
                detector=FixedDetector(ELEMENTS),
                grounding=SetOfMarkGrounding(),
                provider=provider,
                tracker=JsonlUsageTracker(usage_log, PricingTable({})),
                notify=notify,
            )

        self.controller = GuideController(self.panel, factory or build)
        self.panel.show()

    def start(self, goal="Open settings"):
        self.panel.goal_input.setText(goal)
        self.panel.start_button.click()

    def wait_for(self, *states):
        wait_until(lambda: self.controller.state in states)


def wait_until(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        QApplication.processEvents()
        time.sleep(0.001)
    QApplication.processEvents()


@pytest.fixture
def guide(usage_log):
    return lambda *replies: Guide(Provider(*replies), usage_log)


def test_starts_idle_asking_for_a_goal(guide):
    g = guide()

    assert g.controller.state is State.IDLE
    assert g.panel.goal_input.isVisible()
    assert not g.panel.next_button.isVisible()
    assert g.goals == []


def test_blank_goal_starts_nothing(guide):
    g = guide()

    g.start("   ")

    assert g.controller.state is State.IDLE
    assert not g.panel.start_button.isHidden()


def test_enter_in_the_goal_field_starts_too(guide):
    g = guide(reply())

    g.panel.goal_input.setText("  Open settings ")
    g.panel.goal_input.returnPressed.emit()
    g.wait_for(State.STEP)

    assert g.goals == ["Open settings"]


def test_goal_moves_to_the_header_and_a_step_appears_with_its_crop(guide):
    g = guide(reply())

    g.start()
    assert g.controller.state is State.THINKING
    assert g.panel.status.text().startswith("Thinking")
    assert not g.panel.goal_input.isVisible()
    g.wait_for(State.STEP)

    assert g.panel._goal.text() == "Open settings"
    assert ">Settings</span>" in g.panel.instruction.text()
    assert g.panel.crop.isVisible() and not g.panel.crop.pixmap().isNull()
    assert g.panel.next_button.isVisible() and g.panel.next_button.isEnabled()
    assert not g.panel.status.isVisible()


def test_whole_guide_reaches_done_with_one_capture_per_step(guide):
    g = guide(reply("File", 1), reply("Help", 3), reply("All set", done=True))

    g.start()
    g.wait_for(State.STEP)
    assert ">File</span>" in g.panel.instruction.text()

    g.panel.next_button.click()
    assert g.controller.state is State.THINKING
    g.wait_for(State.STEP)
    assert ">Help</span>" in g.panel.instruction.text()

    g.panel.next_button.click()
    g.wait_for(State.DONE)

    assert g.panel.status.isVisible() and g.panel.status.text() == "Done"
    assert not g.panel.next_button.isVisible() and not g.panel.next_button.isEnabled()
    assert not g.panel.crop.isVisible()
    assert len(g.capturer.opacity_at_capture) == 3
    assert g.provider.requests[2].history == (
        "Click File, in the top menu.",
        "Click Help, in the top menu.",
    )
    assert len(g.goals) == 1


def test_panel_is_invisible_at_every_capture_and_returns_afterwards(guide):
    g = guide(reply(), reply("All set", done=True))

    g.start()
    g.wait_for(State.STEP)
    assert g.panel.windowOpacity() == 1.0

    g.panel.next_button.click()
    g.wait_for(State.DONE)

    assert g.capturer.opacity_at_capture == [0.0, 0.0]
    assert g.panel.windowOpacity() == 1.0
    assert g.panel.isVisible()


def test_capture_waits_until_the_panel_had_time_to_leave_the_screen(guide, monkeypatch):
    monkeypatch.setattr(panel_guide, "LEAVE_SCREEN_MS", 80)
    g = guide(reply())
    hid = []
    original = g.capturer.capture
    g.capturer.capture = lambda: (hid.append(time.monotonic()), original())[1]

    started = time.monotonic()
    g.start()
    g.wait_for(State.STEP)

    assert hid[0] - started >= 0.07


def test_window_stays_responsive_and_shows_the_notice_during_the_model_call(guide):
    g = guide(reply())
    g.provider.release.clear()
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start(1)

    g.start()
    wait_until(lambda: g.provider.requests)
    wait_until(lambda: g.panel.notice.isVisible())
    ticks.clear()
    wait_until(lambda: len(ticks) >= 5)

    assert g.controller.state is State.THINKING
    assert g.panel.windowOpacity() == 1.0
    assert "external provider scripted (scripted-1)" in g.panel.notice.text()
    timer.stop()
    g.provider.release.set()
    g.wait_for(State.STEP)
    assert not g.panel.notice.isVisible()


def test_local_provider_shows_no_notice(guide):
    g = guide(reply())
    g.provider.external = False

    g.start()
    g.wait_for(State.STEP)

    assert g.panel.notice.text() == ""


def test_next_while_thinking_starts_no_second_step(guide):
    g = guide(reply(), reply("Help", 3))
    g.provider.release.clear()

    g.start()
    wait_until(lambda: g.provider.requests)
    g.panel.next_requested.emit()
    g.panel.goal_submitted.emit("Another goal")
    g.provider.release.set()
    g.wait_for(State.STEP)

    assert len(g.provider.requests) == 1
    assert g.goals == ["Open settings"]


@pytest.mark.parametrize(
    ("replies", "expected"),
    [
        (["not json", "still not json"], "invalid model response after one retry"),
        (
            [ModelReply(text="", stop_reason=STOP_REFUSAL, refusal_detail="cyber")],
            "The model declined this request: cyber",
        ),
        ([ModelProviderError("Anthropic API error 500: boom")], "Anthropic API error 500: boom"),
    ],
    ids=["invalid", "refusal", "provider"],
)
def test_failure_shows_in_the_panel_and_retry_recovers(guide, replies, expected):
    g = guide(*replies, reply())

    g.start()
    g.wait_for(State.ERROR)

    assert expected in g.panel.error.text()
    assert g.panel.error.isVisible()
    assert g.panel.next_button.text() == "Retry" and g.panel.next_button.isEnabled()
    assert g.panel.isVisible()
    assert not g.panel.instruction.isVisible()

    g.panel.next_button.click()
    assert g.controller.state is State.THINKING
    g.wait_for(State.STEP)

    assert g.panel.next_button.text() == "Next step"
    assert not g.panel.error.isVisible()
    assert len(g.goals) == 1
    assert g.provider.requests[-1].history == ()


def test_error_text_from_outside_is_shown_as_plain_text(guide):
    g = guide()
    g.capturer.failures.append(CaptureError("<b>denied</b>"))

    g.start()
    g.wait_for(State.ERROR)

    assert g.panel.error.text() == "<b>denied</b>"
    assert g.panel.error.textFormat().name == "PlainText"


def test_failed_capture_brings_the_panel_back(guide):
    g = guide(reply())
    g.capturer.failures.append(CaptureError("screenshot denied"))

    g.start()
    g.wait_for(State.ERROR)

    assert g.capturer.opacity_at_capture == [0.0]
    assert g.panel.windowOpacity() == 1.0
    assert "screenshot denied" in g.panel.error.text()


def test_session_that_could_not_be_built_is_retried(usage_log):
    attempts = []
    panel_holder = {}

    def factory(goal, wrap, notify, choose_monitor):
        attempts.append(goal)
        if len(attempts) == 1:
            raise OSError("no config")
        return GuideSession(
            goal=goal,
            capturer=wrap(Capturer(panel_holder["panel"])),
            detector=FixedDetector(ELEMENTS),
            grounding=SetOfMarkGrounding(),
            provider=Provider(reply()),
            tracker=JsonlUsageTracker(usage_log, PricingTable({})),
            notify=notify,
        )

    g = Guide(None, usage_log, factory)
    panel_holder["panel"] = g.panel

    g.start()
    g.wait_for(State.ERROR)
    assert "no config" in g.panel.error.text()

    g.panel.next_button.click()
    g.wait_for(State.STEP)
    assert attempts == ["Open settings", "Open settings"]


def test_unexpected_exception_does_not_leave_the_panel_thinking(usage_log):
    def factory(goal, wrap, notify, choose_monitor):
        raise ValueError("a bug")

    g = Guide(None, usage_log, factory)

    g.start()
    g.wait_for(State.ERROR)

    assert "a bug" in g.panel.error.text()


def test_closing_during_a_step_does_not_bring_the_panel_back(guide):
    g = guide(reply())
    g.provider.release.clear()

    g.start()
    wait_until(lambda: g.provider.requests)
    g.panel.close()
    g.provider.release.set()
    let_late_results_arrive()

    assert not g.panel.isVisible()
    assert g.controller.state is State.IDLE


def let_late_results_arrive():
    """A worker's late result has to arrive, and be dropped, before the test looks."""
    for thread in threading.enumerate():
        if thread is not threading.main_thread():
            thread.join(5)
    deadline = time.monotonic() + 0.05
    wait_until(lambda: time.monotonic() > deadline)


def test_hide_and_guide_me_keep_the_task_and_a_step_that_came_meanwhile(guide):
    g = guide(reply("File", 1), reply("Help", 3))
    g.provider.release.clear()
    g.start()
    wait_until(lambda: g.provider.requests)

    g.panel.hide_button.click()
    assert g.panel.folded and g.panel.isVisible()
    g.provider.release.set()
    g.wait_for(State.STEP)
    assert not g.panel.instruction.isVisible()

    g.panel.guide_me_button.click()
    assert ">File</span>" in g.panel.instruction.text() and g.panel.instruction.isVisible()
    assert g.panel._goal.text() == "Open settings"

    g.panel.hide_button.click()
    g.panel.guide_me_button.click()
    g.panel.next_button.click()
    g.wait_for(State.STEP)
    assert g.provider.requests[-1].history == ("Click File, in the top menu.",)
    assert len(g.goals) == 1


def test_captures_while_folded_leave_the_bar_out_too(guide):
    g = guide(reply())
    g.panel.fold()

    g.start()
    g.wait_for(State.STEP)

    assert g.capturer.opacity_at_capture == [0.0]
    assert g.panel.windowOpacity() == 1.0


@pytest.mark.parametrize("end", ["new_task", "close"])
def test_ending_the_task_drops_its_late_step_and_starts_afresh(guide, end):
    g = guide(reply("File", 1), reply("Help", 3))
    g.provider.release.clear()
    g.start()
    wait_until(lambda: g.provider.requests)

    (g.panel.new_task_button if end == "new_task" else g.panel._close).click()

    assert g.controller.state is State.IDLE
    assert g.panel._goal.text() == "" and g.panel.goal_input.text() == ""
    assert g.panel.folded == (end == "close")
    g.provider.release.set()
    let_late_results_arrive()
    assert g.controller.state is State.IDLE
    assert g.panel.instruction.text() == ""
    assert g.panel.notice.isHidden()

    g.panel.guide_me_button.click()
    assert g.panel.focusWidget() is g.panel.goal_input
    g.start("Change the theme")
    g.wait_for(State.STEP)
    assert ">Help</span>" in g.panel.instruction.text()
    assert g.goals == ["Open settings", "Change the theme"]
    assert g.provider.requests[-1].goal == "Change the theme"
    assert g.provider.requests[-1].history == ()


class ByGoal(Provider):
    """Answers each goal with its own label, whichever worker gets to it first."""

    def __init__(self, labels):
        super().__init__()
        self._labels = labels

    def complete(self, request):
        self.requests.append(request)
        self.release.wait(5)
        return ModelReply(text=reply(self._labels[request.goal]), input_tokens=10, output_tokens=5)


def test_a_late_step_from_the_old_task_does_not_replace_the_new_one(usage_log):
    g = Guide(ByGoal({"Open settings": "File", "Change the theme": "Help"}), usage_log)
    g.provider.release.clear()
    g.start()
    wait_until(lambda: len(g.provider.requests) == 1)

    g.panel.new_task_button.click()
    g.start("Change the theme")
    wait_until(lambda: len(g.provider.requests) == 2)
    g.provider.release.set()
    let_late_results_arrive()

    assert g.controller.state is State.STEP
    assert ">Help</span>" in g.panel.instruction.text()
    assert g.panel.windowOpacity() == 1.0


def test_a_late_error_from_the_old_task_is_not_shown(guide):
    g = guide(ModelProviderError("Anthropic API error 500: boom"))
    g.provider.release.clear()
    g.start()
    wait_until(lambda: g.provider.requests)

    g.panel.new_task_button.click()
    g.provider.release.set()
    let_late_results_arrive()

    assert g.controller.state is State.IDLE
    assert not g.panel.error.isVisible() and g.panel.goal_input.isVisible()


def test_a_task_ended_before_its_capture_captures_nothing(usage_log):
    started = threading.Event()
    go_on = threading.Event()
    sessions = []

    def factory(goal, wrap, notify, choose_monitor):
        started.set()
        go_on.wait(5)
        g.capturer = Capturer(g.panel)
        session = GuideSession(
            goal=goal,
            capturer=wrap(g.capturer),
            detector=FixedDetector(ELEMENTS),
            grounding=SetOfMarkGrounding(),
            provider=Provider(reply()),
            tracker=JsonlUsageTracker(usage_log, PricingTable({})),
            notify=notify,
        )
        sessions.append(session)
        return session

    g = Guide(None, usage_log, factory)
    g.start()
    assert started.wait(5)
    g.panel.new_task_button.click()
    go_on.set()
    let_late_results_arrive()

    assert g.capturer.opacity_at_capture == []
    assert g.panel.windowOpacity() == 1.0
    assert g.controller.state is State.IDLE


def test_monitor_picked_in_the_menu_reaches_the_next_capture(
    monkeypatch, tmp_path, usage_log, screen
):
    g = PortalGuide(monkeypatch, tmp_path, usage_log, [reply(), reply()])
    g.start()
    g.wait_for(State.STEP)

    entry = next(a for a in g.panel.monitor_menu.actions() if a.text().startswith("DP-3"))
    entry.trigger()
    assert g.panel.capture_source.text().startswith("Next capture: DP-3")
    g.panel.next_button.click()
    g.wait_for(State.THINKING)
    g.wait_for(State.STEP)

    assert g.sizes == [(3200, 1350), (2400, 1350)]


def test_panel_command_guides_with_the_real_wiring(image, monkeypatch):
    """`screenguide panel` builds the session through the same path as the terminal."""
    capturer_kwargs = []

    class Portal:
        def __init__(self, **kwargs):
            capturer_kwargs.append(kwargs)

        def capture(self):
            return Screenshot(data=image.read_bytes())

    monkeypatch.setattr("screenguide.cli.PortalCapturer", Portal)
    monkeypatch.setattr("signal.signal", lambda *args: None)
    shown = []
    monkeypatch.setattr(StepPanel, "show", lambda self: shown.append(self) or self.setVisible(True))

    def run_until_step(self=None):
        panel = shown[0]
        panel.goal_input.setText("Open settings")
        panel.start_button.click()
        wait_until(lambda: panel.next_button.isVisible())
        return 0

    monkeypatch.setattr(QApplication, "exec", run_until_step)

    assert main(["panel", "--monitor", "DP-3"]) == 0
    assert capturer_kwargs[0]["choose_monitor"]() == MonitorChoice("DP-3")
    assert ">File</span>" in shown[0].instruction.text()


class PortalGuide(Guide):
    """The panel driving the real `PortalCapturer`, with the portal and Mutter faked."""

    def __init__(self, monkeypatch, tmp_path, usage_log, replies, monitor=None):
        self.requests = Requests(monkeypatch, tmp_path)
        self.sizes = []

        def build(goal, wrap, notify, choose_monitor):
            portal = PortalCapturer(choose_monitor=choose_monitor)

            class Measured:
                def capture(inner):
                    screenshot = portal.capture()
                    self.sizes.append((screenshot.width, screenshot.height))
                    return screenshot

            return GuideSession(
                goal=goal,
                capturer=wrap(Measured()),
                detector=FixedDetector(ELEMENTS),
                grounding=SetOfMarkGrounding(),
                provider=self.provider,
                tracker=JsonlUsageTracker(usage_log, PricingTable({})),
                notify=notify,
            )

        self.panel = StepPanel(monitor=monitor)
        self.panel.set_monitors(MIXED)
        self.provider = Provider(*replies)
        self.goals = []
        self.controller = GuideController(self.panel, build)
        self.panel.show()


def test_captures_follow_the_panel_to_another_monitor(monkeypatch, tmp_path, usage_log, screen):
    g = PortalGuide(monkeypatch, tmp_path, usage_log, [reply(), reply()])

    g.start()
    g.wait_for(State.STEP)
    screen["name"] = "DP-3"
    g.panel.next_button.click()
    g.wait_for(State.THINKING)
    g.wait_for(State.STEP)

    assert g.sizes == [(3200, 1350), (2400, 1350)]


def test_pinned_monitor_stays_when_the_panel_moves(monkeypatch, tmp_path, usage_log, screen):
    g = PortalGuide(monkeypatch, tmp_path, usage_log, [reply()])
    g.panel.select_monitor("DP-3")
    screen["name"] = "eDP-1"

    g.start()
    g.wait_for(State.STEP)

    assert g.sizes == [(2400, 1350)]


def test_whole_desktop_chosen_in_the_panel(monkeypatch, tmp_path, usage_log, screen):
    g = PortalGuide(monkeypatch, tmp_path, usage_log, [reply()])
    g.panel.select_monitor(Pick.WHOLE_DESKTOP)

    g.start()
    g.wait_for(State.STEP)

    assert g.sizes == [(5120, 2700)]


@pytest.mark.parametrize(
    ("monitor", "screen_name", "message"),
    [
        ("HDMI-9", "DP-4", "monitor HDMI-9 is not connected"),
        (None, "XWAYLAND0", "cannot tell which monitor the panel is on"),
    ],
    ids=["pinned-disconnected", "screen-not-in-layout"],
)
def test_missing_monitor_is_an_error_in_the_panel_and_sends_nothing(
    monkeypatch, tmp_path, usage_log, screen, monitor, screen_name, message
):
    g = PortalGuide(monkeypatch, tmp_path, usage_log, [reply()], monitor=monitor)
    screen["name"] = screen_name

    g.start()
    g.wait_for(State.ERROR)

    assert message in g.panel.error.text()
    assert g.requests.screenshots == 0
    assert g.provider.requests == []
    assert g.panel.windowOpacity() == 1.0


@pytest.mark.parametrize(
    ("environment", "argv", "expected"),
    [
        ({}, [], "Follow panel"),
        ({"SCREENGUIDE_MONITOR": "DP-3"}, [], "DP-3  Some display"),
        ({"SCREENGUIDE_MONITOR": "DP-3"}, ["--monitor", "eDP-1"], "eDP-1  Built-in display"),
    ],
    ids=["no-choice-follows", "from-config", "option-over-config"],
)
def test_starting_choice_comes_from_option_or_config(
    real_loop, monkeypatch, environment, argv, expected
):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    seen = []
    real_loop(
        lambda panel: seen.extend(text for text, checked in panel.monitor_entries() if checked)
    )

    assert main(["panel", *argv]) == 0
    assert seen == [expected]


@pytest.mark.parametrize(
    "extra",
    [["--demo-step"], ["--monitor", "DP-3", "--demo-step", "--image", "x.png"]],
    ids=["demo-without-image", "monitor-with-demo"],
)
def test_panel_command_rejects_mixed_options(extra):
    with pytest.raises(SystemExit):
        main(["panel", *extra])


def test_hiding_and_showing_again_keeps_the_session(guide):
    g = guide(reply("File", 1), reply("Help", 3))
    g.start()
    g.wait_for(State.STEP)
    instruction = g.panel.instruction.text()

    g.panel.toggle()
    assert not g.panel.isVisible()
    assert g.controller.state is State.STEP

    g.panel.toggle()
    assert g.panel.isVisible()
    assert g.panel.instruction.text() == instruction
    assert g.panel._goal.text() == "Open settings"

    g.panel.next_button.click()
    g.wait_for(State.STEP)
    assert g.provider.requests[-1].history == ("Click File, in the top menu.",)
    assert len(g.goals) == 1


def test_step_that_arrives_while_hidden_waits_for_the_panel_to_come_back(guide):
    g = guide(reply())
    g.provider.release.clear()
    g.start()
    wait_until(lambda: g.provider.requests)

    g.panel.toggle()
    g.provider.release.set()
    g.wait_for(State.STEP)

    assert not g.panel.isVisible()
    g.panel.toggle()
    assert g.panel.isVisible() and g.panel.windowOpacity() == 1.0
    assert ">Settings</span>" in g.panel.instruction.text()


def goal_field_has_focus(panel):
    """The widget that takes the keys once the window is active.

    The offscreen platform never activates a window again after hiding it, so tests check this
    rather than `hasFocus`. On xcb under GNOME, the reactivated panel gives the keys to that field.
    """
    return panel.focusWidget() is panel.goal_input


def test_showing_focuses_the_goal_field_and_keeps_what_was_typed(guide):
    g = guide()
    g.panel.goal_input.setText("Open sett")
    g.panel.start_button.setFocus()
    g.panel.toggle()
    QApplication.processEvents()

    g.panel.toggle()

    assert goal_field_has_focus(g.panel)
    assert g.panel.goal_input.text() == "Open sett"


def test_show_command_never_hides(guide):
    g = guide()

    panel_guide.obey(g.panel, "show")
    panel_guide.obey(g.panel, "show")
    assert g.panel.isVisible()
    panel_guide.obey(g.panel, "toggle")
    assert not g.panel.isVisible()
    panel_guide.obey(g.panel, "show")
    assert g.panel.isVisible()


@pytest.fixture
def real_loop(monkeypatch):
    """`screenguide panel` or `toggle` as launched, with `exec` running `body` instead."""
    monkeypatch.setattr("signal.signal", lambda *args: None)
    shown = []
    original_init = StepPanel.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        shown.append(self)

    monkeypatch.setattr(StepPanel, "__init__", init)

    def run(body):
        monkeypatch.setattr(QApplication, "exec", lambda self=None: body(shown[0]) or 0)

    return run


@pytest.mark.parametrize("command", ["toggle", "panel"])
def test_first_launch_shows_the_panel_and_later_launches_toggle_it(real_loop, command):
    seen = {}

    def body(panel):
        seen["first"] = panel.isVisible() and goal_field_has_focus(panel)
        panel.goal_input.setText("Open settings")
        panel.start_button.setFocus()

        assert main(["toggle"]) == 0
        wait_until(lambda: not panel.isVisible())
        assert main(["toggle"]) == 0
        wait_until(panel.isVisible)
        assert goal_field_has_focus(panel)
        assert main(["panel"]) == 0
        assert main(["toggle"]) == 0
        wait_until(lambda: not panel.isVisible())
        assert main(["panel"]) == 0
        wait_until(panel.isVisible)
        seen["goal"] = panel.goal_input.text()

    real_loop(body)

    assert main([command]) == 0
    assert seen == {"first": True, "goal": "Open settings"}


def test_later_launch_only_forwards_and_says_its_monitor_does_not_apply(capsys):
    from screenguide.instance import claim_or_send, receive

    listener = claim_or_send("show")
    try:
        assert main(["panel", "--monitor", "DP-3"]) == 0
        assert main(["toggle"]) == 0
        time.sleep(0.05)
        assert receive(listener) == ["show", "toggle"]
    finally:
        listener.close()
    assert "already running" in capsys.readouterr().err


def test_instance_is_released_when_the_panel_ends(real_loop):
    from screenguide.instance import claim_or_send

    real_loop(lambda panel: None)
    assert main(["toggle"]) == 0

    listener = claim_or_send("show")
    assert listener is not None
    listener.close()


def test_toggle_hides_the_whole_window_bar_included_folded_or_not(guide):
    g = guide()
    g.panel.fold()

    panel_guide.obey(g.panel, "toggle")
    assert not g.panel.isVisible() and not g.panel.bar.isVisible()

    panel_guide.obey(g.panel, "toggle")
    assert g.panel.isVisible() and g.panel.bar.isVisible() and g.panel.folded


def test_usage_entry_shows_the_usage_log_summary(real_loop, usage_log):
    JsonlUsageTracker(usage_log, PricingTable({})).record(
        UsageRecord(
            timestamp="2026-11-02T10:00:00+00:00",
            session_id="abcdef0123",
            provider="anthropic",
            model="claude-test",
            input_tokens=1200,
            output_tokens=80,
            image_width=800,
            image_height=600,
            latency_ms=900.0,
        )
    )
    seen = []

    def body(panel):
        panel.usage_action.trigger()
        seen.append(panel._usage_window.text.toPlainText())

    real_loop(body)

    assert main(["panel"]) == 0
    assert "claude-test" in seen[0] and "1200" in seen[0].replace(",", "")


def test_quit_ends_the_app(monkeypatch):
    """`screenguide panel` with its real event loop, left only through ⋮ → Quit."""
    monkeypatch.setattr("signal.signal", lambda *args: None)
    panels = []
    original_init = StepPanel.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        panels.append(self)
        QTimer.singleShot(0, self.quit_action.trigger)

    monkeypatch.setattr(StepPanel, "__init__", init)
    # Fails the test rather than hanging it if Quit does nothing.
    QTimer.singleShot(5000, lambda: QApplication.exit(1))

    assert main(["panel"]) == 0
    assert len(panels) == 1
