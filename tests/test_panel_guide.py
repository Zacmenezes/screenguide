"""The panel driving a real `GuideSession`, with the capture and the model stubbed."""

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
from screenguide.capture import CaptureError, Screenshot  # noqa: E402
from screenguide.cli import main  # noqa: E402
from screenguide.grounding import SetOfMarkGrounding  # noqa: E402
from screenguide.model import STOP_REFUSAL, ModelProviderError, ModelReply  # noqa: E402
from screenguide.panel import StepPanel  # noqa: E402
from screenguide.panel_guide import GuideController, State  # noqa: E402
from screenguide.pricing import PricingTable  # noqa: E402
from screenguide.session import GuideSession  # noqa: E402
from screenguide.usage import JsonlUsageTracker  # noqa: E402


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
def short_settle(monkeypatch):
    monkeypatch.setattr(panel_guide, "LEAVE_SCREEN_MS", 1)


class Guide:
    def __init__(self, provider, usage_log, factory=None):
        self.panel = StepPanel()
        self.capturer = Capturer(self.panel)
        self.provider = provider
        self.goals = []

        def build(goal, wrap, notify):
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

    def factory(goal, wrap, notify):
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
    def factory(goal, wrap, notify):
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
    # The worker's late result has to arrive and be dropped.
    deadline = time.monotonic() + 0.1
    wait_until(lambda: time.monotonic() > deadline)

    assert not g.panel.isVisible()
    assert g.controller.state is State.THINKING


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
    assert capturer_kwargs[0]["monitor"] == "DP-3"
    assert ">File</span>" in shown[0].instruction.text()


@pytest.mark.parametrize(
    "extra",
    [["--demo-step"], ["--monitor", "DP-3", "--demo-step", "--image", "x.png"]],
    ids=["demo-without-image", "monitor-with-demo"],
)
def test_panel_command_rejects_mixed_options(extra):
    with pytest.raises(SystemExit):
        main(["panel", *extra])
