"""`layer:ui`: runs guide sessions from the panel, with slow work off the UI thread."""

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
from screenguide.session import Answer, GuideSession, Step

logger = logging.getLogger(__name__)

# How long the compositor gets to draw the window as gone before the capture.
LEAVE_SCREEN_MS = 100

CapturerWrapper = Callable[[ScreenCapturer], ScreenCapturer]
Notify = Callable[[str], None]
ChooseMonitor = Callable[[], MonitorChoice]
SessionFactory = Callable[[str, CapturerWrapper, Notify, ChooseMonitor], GuideSession]
# What a worker does with the session: the next step, or a message from the footer.
Work = Callable[[GuideSession], Step | Answer]


class State(Enum):
    IDLE = "idle"
    THINKING = "thinking"
    STEP = "step"
    DONE = "done"
    ERROR = "error"


class TaskDiscarded(Exception):
    """The task a worker was running for has ended; it must not capture or show anything more."""


class _Task:
    """One goal and its session, from the goal until New Task, × or the window closes."""

    def __init__(self, goal: str) -> None:
        self.goal = goal
        # Built and used only on the task's worker thread, of which one runs at a time.
        self.session: GuideSession | None = None
        # Read on the worker thread before each capture.
        self.discarded = threading.Event()


class _OffScreenCapturer:
    """On the worker thread: has the UI thread hide the window, captures, then brings it back.

    Captures take turns through `turn`: the screenshot portal answers one request at a time, and on
    GNOME a second one sent meanwhile goes unanswered until it times out. An ended task's worker can
    still be running when the next task's starts, so two could otherwise overlap.

    Hiding means full transparency. On GNOME, unmapping the window instead (`hide()`) lost it: after
    a few hide/show cycles it stayed mapped and painted but the compositor no longer displayed it.
    """

    def __init__(
        self,
        inner: ScreenCapturer,
        task: _Task,
        turn: threading.Lock,
        leave: Callable[[threading.Event], None],
        come_back: Callable[[], None],
    ) -> None:
        self._inner = inner
        self._task = task
        self._turn = turn
        self._leave = leave
        self._come_back = come_back

    def capture(self) -> Screenshot:
        with self._turn:
            # A discarded task sends no new screenshot anywhere.
            if self._task.discarded.is_set():
                raise TaskDiscarded
            gone = threading.Event()
            self._leave(gone)
            gone.wait()
            try:
                return self._inner.capture()
            finally:
                self._come_back()


class GuideController(QObject):
    """Moves the panel through idle, thinking, step, done and error, one task at a time.

    The worker thread reaches the panel only through these signals, which Qt delivers on the UI
    thread. Each carries the task it belongs to, and whatever belongs to a task that has ended is
    dropped. A task runs one worker at a time, because a new one starts only outside `THINKING`;
    a discarded task's worker may still be finishing while the next task's runs, so their captures
    take turns.
    """

    _leave_screen = Signal(object)
    _come_back = Signal()
    _notice = Signal(object, str)
    _reply_ready = Signal(object, object)
    _failed = Signal(object, str)

    def __init__(self, panel: StepPanel, build_session: SessionFactory) -> None:
        super().__init__()
        self.state = State.IDLE
        self._panel = panel
        self._build_session = build_session
        self._task: _Task | None = None
        self._capture_turn = threading.Lock()
        self._monitor_choice = panel.monitor_choice()
        # The state and view a text answer goes back to: the last step or error shown.
        self._settled: tuple[State, Callable[[], None]] | None = None

        self._leave_screen.connect(self._on_leave_screen)
        self._come_back.connect(self._on_come_back)
        self._notice.connect(self._on_notice)
        self._reply_ready.connect(self._on_reply)
        self._failed.connect(self._on_failed)
        panel.goal_submitted.connect(self._on_goal)
        panel.next_requested.connect(self._on_next)
        panel.message_sent.connect(self._on_message)
        panel.task_ended.connect(self._end_task)
        panel.closed.connect(self._end_task)

        panel.show_goal_entry()

    def _on_goal(self, goal: str) -> None:
        if self.state is State.IDLE:
            self._task = _Task(goal)
            self._panel.set_goal(goal)
            self._request_step()

    def _on_next(self) -> None:
        if self.state in (State.STEP, State.ERROR):
            self._request_step()

    def _on_message(self, message: str) -> None:
        # Like Next step, but also once done: the user may still ask, or say the task is not.
        if self.state in (State.STEP, State.DONE, State.ERROR):
            self._start(lambda session: session.ask(message))

    def _end_task(self) -> None:
        """Throw the current task away and ask for a new goal."""
        if self._task is not None:
            logger.info("task ended state=%s", self.state.value)
            self._task.discarded.set()
            self._task = None
            self._panel.goal_input.clear()
            self._panel.question_input.clear()
        self.state = State.IDLE
        self._settled = None
        self._panel.set_goal("")
        self._panel.show_goal_entry()

    def _request_step(self) -> None:
        self._start(lambda session: session.next_step())

    def _start(self, work: Work) -> None:
        self.state = State.THINKING
        self._panel.show_thinking()
        # Daemon: quitting ends the process without waiting for a call in flight.
        threading.Thread(target=self._produce, args=(self._task, work), daemon=True).start()

    def _produce(self, task: _Task, work: Work) -> None:
        try:
            if task.session is None:
                task.session = self._build_session(
                    task.goal,
                    lambda capturer: self._off_screen(capturer, task),
                    lambda message: self._notice.emit(task, message),
                    self._chosen_monitor,
                )
            self._reply_ready.emit(task, work(task.session))
        except TaskDiscarded:
            logger.info("step dropped: its task ended before the capture")
        except GUIDE_ERRORS as exc:
            logger.warning("step failed: %s", exc)
            self._failed.emit(task, describe_guide_error(exc))
        except Exception as exc:
            # Nothing else would report it, and the panel would stay on "Thinking" for good.
            logger.exception("step failed")
            self._failed.emit(task, f"unexpected error: {exc}")

    def _off_screen(self, capturer: ScreenCapturer, task: _Task) -> ScreenCapturer:
        return _OffScreenCapturer(
            capturer, task, self._capture_turn, self._leave_screen.emit, self._come_back.emit
        )

    def _chosen_monitor(self) -> MonitorChoice:
        """On the worker thread, during a capture: the choice read as the window left the screen."""
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

    def _current(self, task: _Task) -> bool:
        if task is self._task:
            return True
        logger.info("result dropped: its task ended")
        return False

    def _on_notice(self, task: _Task, message: str) -> None:
        if self._current(task):
            self._panel.show_notice(message)

    def _on_reply(self, task: _Task, reply: Step | Answer) -> None:
        if not self._current(task):
            return
        if isinstance(reply, Answer):
            # The step or error shown before the message stays, with the answer under it.
            self.state, show_again = self._settled
            show_again()
            self._panel.show_answer(reply)
            return
        self._settle(
            State.DONE if reply.done else State.STEP,
            lambda: self._panel.show_step(reply, reply.screenshot),
        )

    def _on_failed(self, task: _Task, message: str) -> None:
        if self._current(task):
            self._settle(State.ERROR, lambda: self._panel.show_error(message))

    def _settle(self, state: State, show: Callable[[], None]) -> None:
        self.state = state
        self._settled = (state, show)
        show()


def obey(panel: StepPanel, command: str) -> None:
    """Carry out a command that a later `screenguide` launch sent to this instance."""
    logger.info("instance command received command=%s", command)
    if command == "toggle":
        panel.toggle()
    else:
        panel.appear()


def list_monitors_into(panel: StepPanel, list_monitors: Callable[[], list[Monitor]]) -> None:
    """Fill the Capture monitor menu. Without a layout, it still offers its fixed entries."""
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
    usage_summary: Callable[[], str],
    monitor: str | None = None,
) -> int:
    """Show the bar and the panel and guide one task after another, until Quit.

    `monitor` is the starting choice of monitor; with none, captures follow the panel. Meanwhile,
    every command a later launch sends through `listener` shows or hides the window.
    """
    app = start_app()
    panel = StepPanel(monitor=monitor)
    panel.usage_requested.connect(lambda: panel.show_usage(usage_summary()))
    panel.quit_requested.connect(app.quit)
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
