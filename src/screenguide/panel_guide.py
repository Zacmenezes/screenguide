"""`layer:ui`: runs a guide session from the panel, with slow work off the UI thread."""

import logging
import socket
import threading
from collections.abc import Callable
from enum import Enum

from PySide6.QtCore import QObject, QSocketNotifier, QTimer, Signal

from screenguide.capture import ScreenCapturer, Screenshot
from screenguide.errors import GUIDE_ERRORS, describe_guide_error
from screenguide.instance import receive
from screenguide.monitors import Monitor, MonitorChoice, MonitorError
from screenguide.panel import StepPanel, start_app
from screenguide.session import GuideSession, Step

logger = logging.getLogger(__name__)

# How long the compositor gets to draw the panel as gone before the capture.
LEAVE_SCREEN_MS = 100

CapturerWrapper = Callable[[ScreenCapturer], ScreenCapturer]
Notify = Callable[[str], None]
ChooseMonitor = Callable[[], MonitorChoice]
SessionFactory = Callable[[str, CapturerWrapper, Notify, ChooseMonitor], GuideSession]


class State(Enum):
    IDLE = "idle"
    THINKING = "thinking"
    STEP = "step"
    DONE = "done"
    ERROR = "error"


class _OffScreenCapturer:
    """On the worker thread: has the UI thread hide the panel, captures, then brings it back.

    Hiding means full transparency. On GNOME, unmapping the window instead (`hide()`) lost it: after
    a few hide/show cycles it stayed mapped and painted but the compositor no longer displayed it.
    """

    def __init__(
        self,
        inner: ScreenCapturer,
        leave: Callable[[threading.Event], None],
        come_back: Callable[[], None],
    ) -> None:
        self._inner = inner
        self._leave = leave
        self._come_back = come_back

    def capture(self) -> Screenshot:
        gone = threading.Event()
        self._leave(gone)
        gone.wait()
        try:
            return self._inner.capture()
        finally:
            self._come_back()


class GuideController(QObject):
    """Moves the panel through idle, thinking, step, done and error.

    The worker thread reaches the panel only through these signals, which Qt delivers on the UI
    thread. One worker runs at a time, because a new one starts only outside `THINKING`.
    """

    _leave_screen = Signal(object)
    _come_back = Signal()
    _notice = Signal(str)
    _step_ready = Signal(object)
    _failed = Signal(str)

    def __init__(self, panel: StepPanel, build_session: SessionFactory) -> None:
        super().__init__()
        self.state = State.IDLE
        self._panel = panel
        self._build_session = build_session
        self._session: GuideSession | None = None
        self._goal = ""
        self._closed = False
        self._monitor_choice = panel.monitor_choice()

        self._leave_screen.connect(self._on_leave_screen)
        self._come_back.connect(self._on_come_back)
        self._notice.connect(self._on_notice)
        self._step_ready.connect(self._on_step_ready)
        self._failed.connect(self._on_failed)
        panel.goal_submitted.connect(self._on_goal)
        panel.next_requested.connect(self._on_next)
        panel.closed.connect(self._on_closed)

        panel.show_goal_entry()

    def _on_goal(self, goal: str) -> None:
        if self.state is State.IDLE:
            self._goal = goal
            self._panel.set_goal(goal)
            self._request_step()

    def _on_next(self) -> None:
        if self.state in (State.STEP, State.ERROR):
            self._request_step()

    def _request_step(self) -> None:
        self.state = State.THINKING
        self._panel.show_thinking()
        # Daemon: closing the panel ends the process without waiting for a call in flight.
        threading.Thread(target=self._produce_step, daemon=True).start()

    def _produce_step(self) -> None:
        try:
            if self._session is None:
                self._session = self._build_session(
                    self._goal, self._off_screen, self._notice.emit, self._chosen_monitor
                )
            self._step_ready.emit(self._session.next_step())
        except GUIDE_ERRORS as exc:
            logger.warning("step failed: %s", exc)
            self._failed.emit(describe_guide_error(exc))
        except Exception as exc:
            # Nothing else would report it, and the panel would stay on "Thinking" for good.
            logger.exception("step failed")
            self._failed.emit(f"unexpected error: {exc}")

    def _off_screen(self, capturer: ScreenCapturer) -> ScreenCapturer:
        return _OffScreenCapturer(capturer, self._leave_screen.emit, self._come_back.emit)

    def _chosen_monitor(self) -> MonitorChoice:
        """On the worker thread, during a capture: the choice read as the panel left the screen."""
        return self._monitor_choice

    def _on_leave_screen(self, gone: threading.Event) -> None:
        # Read here, on the UI thread, before `gone` lets the capture start.
        self._monitor_choice = self._panel.monitor_choice()
        logger.info(
            "capture monitor=%s follows_panel=%s",
            self._monitor_choice.connector,
            self._monitor_choice.follows_panel,
        )
        self._panel.setWindowOpacity(0.0)
        QTimer.singleShot(LEAVE_SCREEN_MS, gone.set)

    def _on_come_back(self) -> None:
        self._panel.setWindowOpacity(1.0)

    def _on_notice(self, message: str) -> None:
        if not self._closed:
            self._panel.show_notice(message)

    def _on_step_ready(self, step: Step) -> None:
        if self._closed:
            return
        self.state = State.DONE if step.done else State.STEP
        self._panel.show_step(step, step.screenshot)

    def _on_failed(self, message: str) -> None:
        if self._closed:
            return
        self.state = State.ERROR
        self._panel.show_error(message)

    def _on_closed(self) -> None:
        self._closed = True


def obey(panel: StepPanel, command: str) -> None:
    """Carry out a command that a later `screenguide` launch sent to this instance."""
    logger.info("instance command received command=%s", command)
    if command == "toggle":
        panel.toggle()
    else:
        panel.appear()


def list_monitors_into(panel: StepPanel, list_monitors: Callable[[], list[Monitor]]) -> None:
    """Fill the panel's selector. Without a layout, the selector still offers its fixed entries."""
    try:
        monitors = list_monitors()
    except MonitorError as exc:
        logger.warning("monitors not listed: %s", exc)
        monitors = []
    panel.set_monitors(monitors)


def run_panel(
    build_session: SessionFactory,
    listener: socket.socket,
    list_monitors: Callable[[], list[Monitor]],
    monitor: str | None = None,
) -> int:
    """Show the panel and guide one task, until the panel is closed.

    `monitor` is the starting choice of monitor; with none, captures follow the panel. Meanwhile,
    every command a later launch sends through `listener` shows or hides the panel.
    """
    app = start_app()
    panel = StepPanel(monitor=monitor)
    list_monitors_into(panel, list_monitors)
    app.screenAdded.connect(lambda _screen: list_monitors_into(panel, list_monitors))
    app.screenRemoved.connect(lambda _screen: list_monitors_into(panel, list_monitors))
    # Kept in names: Qt holds no reference to them, and they have to outlive `exec`.
    controller = GuideController(panel, build_session)
    notifier = QSocketNotifier(listener.fileno(), QSocketNotifier.Type.Read)
    notifier.activated.connect(lambda: [obey(panel, command) for command in receive(listener)])
    panel.appear()
    logger.info("panel started state=%s", controller.state.value)
    try:
        return app.exec()
    finally:
        # The caller closes the socket next; a notifier still watching it would complain.
        notifier.setEnabled(False)
