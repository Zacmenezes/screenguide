"""`layer:ui`: the floating bar and the panel that shows the guide above the other windows."""

import logging
import math
import os
import signal
from collections.abc import Callable
from enum import Enum

from PIL import Image
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QColor,
    QFontDatabase,
    QImage,
    QMouseEvent,
    QMoveEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QPolygonF,
    QRegion,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from screenguide.capture import Screenshot
from screenguide.detection import BBox, decode_image
from screenguide.monitors import WHOLE_DESKTOP, Monitor, MonitorChoice
from screenguide.session import Step
from screenguide.step_view import CROP_WIDTH, CropView, crop_view, highlight_instruction

logger = logging.getLogger(__name__)

MARGIN = 20
PANEL_WIDTH = CROP_WIDTH + 2 * MARGIN
BACKGROUND = QColor(24, 26, 30, 215)
ACCENT = "#5eead4"
ARROW_OUTLINE = QColor(0, 0, 0)
ARROW_HEAD = 16.0
STYLE = f"""
    QLabel {{ color: #f4f4f5; background: transparent; }}
    QLabel#goal {{ color: #a1a1aa; }}
    QLabel#instruction {{ font-size: 15px; }}
    QLabel#status {{ color: {ACCENT}; font-size: 15px; font-weight: 600; }}
    QLabel#error {{ color: #fca5a5; }}
    QLabel#notice, QLabel#source {{ color: #a1a1aa; font-size: 12px; }}
    QLineEdit {{
        color: #f4f4f5; background: rgba(255, 255, 255, 18);
        border: 1px solid #52525b; border-radius: 8px; padding: 7px 10px;
    }}
    QPushButton {{
        color: {ACCENT}; background: transparent; font-weight: 600;
        border: 1px solid #52525b; border-radius: 8px; padding: 7px 14px;
    }}
    QPushButton:hover {{ background: rgba(255, 255, 255, 25); }}
    QPushButton:disabled {{ color: #71717a; }}
    QPushButton#close {{ color: #d4d4d8; font-weight: 400; border-radius: 12px; padding: 0; }}
    QPushButton#newTask {{ color: #d4d4d8; font-weight: 400; padding: 3px 10px; }}
    QPushButton[bar="true"] {{
        color: #f4f4f5; border: none; border-radius: 14px; padding: 5px 12px;
    }}
    QPushButton[bar="true"]:hover {{ background: rgba(255, 255, 255, 25); }}
    QPushButton#guideMe {{ background: rgba(255, 255, 255, 30); }}
    QPushButton#menu::menu-indicator {{ image: none; width: 0; }}
    QMenu {{
        color: #f4f4f5; background: #18181b; border: 1px solid #3f3f46; padding: 4px;
    }}
    QMenu::item {{ padding: 5px 22px 5px 22px; border-radius: 6px; }}
    QMenu::item:selected {{ background: #3f3f46; }}
    QMenu::item:disabled {{ color: #71717a; }}
    QMenu::separator {{ height: 1px; background: #3f3f46; margin: 4px 6px; }}
"""
USAGE_STYLE = """
    QWidget { background: #18181b; }
    QPlainTextEdit { color: #f4f4f5; border: none; }
"""
# Between the bar and the panel. Clicks there reach the window behind.
BAR_GAP = 8
# From the top of the monitor the window first opens on.
TOP_OFFSET = 8


class Pick(Enum):
    """Capture monitor entries that are not a single monitor."""

    FOLLOW_PANEL = "Follow panel"
    WHOLE_DESKTOP = "Whole desktop"


def monitor_label(monitor: Monitor) -> str:
    """Connector and name, as `screenguide monitors` shows them."""
    return f"{monitor.connector}  {monitor.display_name}".rstrip()


class _Rounded(QWidget):
    """A see-through backdrop with rounded corners; `radius=None` makes it a pill."""

    def __init__(self, radius: float | None) -> None:
        super().__init__()
        self._radius = radius

    def paintEvent(self, event: QPaintEvent) -> None:
        radius = self.height() / 2 if self._radius is None else self._radius
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(BACKGROUND)
        painter.drawRoundedRect(self.rect(), radius, radius)


class StepPanel(QWidget):
    """The floating bar and, below it, the panel, in one window that stays on top.

    Shows what it is told and reports what the user does, leaving the rest to its owner. The panel
    folds away under the bar; folding it ends nothing.
    """

    goal_submitted = Signal(str)
    next_requested = Signal()
    # New Task or ×: the user is done with the current task.
    task_ended = Signal()
    usage_requested = Signal()
    quit_requested = Signal()
    closed = Signal()

    def __init__(self, goal: str = "", monitor: str | None = None) -> None:
        super().__init__(
            None,
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(STYLE)
        self.setWindowTitle("screenguide")
        self.setFixedWidth(PANEL_WIDTH)
        self._placed = False
        self._usage_window: UsageWindow | None = None

        # The menu's monitor entries are filled by `set_monitors`.
        self._monitors: list[Monitor] = []
        self._selection: Pick | str = monitor or Pick.FOLLOW_PANEL
        self.monitor_menu = QMenu("Capture monitor")
        self.monitor_menu.setEnabled(False)
        self._monitor_group = QActionGroup(self)
        self._monitor_group.triggered.connect(self._on_monitor_picked)
        self.menu = QMenu()
        self.menu.addMenu(self.monitor_menu)
        self.usage_action = self.menu.addAction("Usage")
        self.usage_action.triggered.connect(self.usage_requested)
        self.menu.addSeparator()
        self.quit_action = self.menu.addAction("Quit")
        self.quit_action.triggered.connect(self.quit_requested)

        self.guide_me_button = QPushButton("Guide me  +", objectName="guideMe")
        self.guide_me_button.clicked.connect(self.unfold)
        self.hide_button = QPushButton("Hide")
        self.hide_button.clicked.connect(self.fold)
        self.menu_button = QPushButton("⋮", objectName="menu")
        self.menu_button.setToolTip("Menu")
        self.menu_button.setMenu(self.menu)
        for button in (self.guide_me_button, self.hide_button, self.menu_button):
            button.setProperty("bar", True)
        self.bar = _Rounded(radius=None)
        bar = QHBoxLayout(self.bar)
        bar.setContentsMargins(6, 5, 6, 5)
        bar.setSpacing(4)
        bar.addWidget(self.guide_me_button)
        bar.addWidget(self.hide_button)
        bar.addWidget(self.menu_button)

        self._close = QPushButton("×", objectName="close")
        self._close.setFixedSize(24, 24)
        self._close.setToolTip("End the task and fold the panel")
        self._close.clicked.connect(self._end_and_fold)
        self._goal = QLabel(goal, objectName="goal", wordWrap=True)
        self._goal.setTextFormat(Qt.TextFormat.PlainText)
        self.new_task_button = QPushButton("New Task", objectName="newTask")
        self.new_task_button.clicked.connect(self.task_ended)
        header = QHBoxLayout()
        header.addWidget(self._close, alignment=Qt.AlignmentFlag.AlignTop)
        header.addWidget(self._goal, stretch=1)
        header.addWidget(self.new_task_button, alignment=Qt.AlignmentFlag.AlignTop)

        self.capture_source = QLabel(objectName="source")
        self.capture_source.setTextFormat(Qt.TextFormat.PlainText)
        self.capture_source.hide()

        self.goal_input = QLineEdit(placeholderText="What do you want to do?")
        self.goal_input.returnPressed.connect(self._submit_goal)
        self.start_button = QPushButton("Start")
        self.start_button.clicked.connect(self._submit_goal)
        self._entry = QWidget()
        entry = QHBoxLayout(self._entry)
        entry.setContentsMargins(0, 0, 0, 0)
        entry.addWidget(self.goal_input, stretch=1)
        entry.addWidget(self.start_button)

        self.status = QLabel(objectName="status")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.error = QLabel(objectName="error", wordWrap=True)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.notice = QLabel(objectName="notice", wordWrap=True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.instruction = QLabel(objectName="instruction", wordWrap=True)
        self.instruction.setTextFormat(Qt.TextFormat.RichText)
        self.crop = QLabel()
        self.crop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.next_button = QPushButton("Next step")
        self.next_button.clicked.connect(self.next_requested)

        self.body = _Rounded(radius=14)
        self._body_layout = QVBoxLayout(self.body)
        self._body_layout.setContentsMargins(MARGIN, MARGIN - 6, MARGIN, MARGIN - 6)
        self._body_layout.setSpacing(12)
        self._body_layout.addLayout(header)
        self._body_layout.addWidget(self.capture_source)
        self._body_layout.addWidget(self._entry)
        self._body_layout.addWidget(self.status)
        self._body_layout.addWidget(self.error)
        self._body_layout.addWidget(self.instruction)
        self._body_layout.addWidget(self.crop)
        self._body_layout.addWidget(self.notice)
        self._body_layout.addWidget(self.next_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(BAR_GAP)
        self._layout.addWidget(self.bar, alignment=Qt.AlignmentFlag.AlignHCenter)
        self._layout.addWidget(self.body)
        self._show_only()
        # Qt settles the window's screen after the move event, so the move alone reads it too soon.
        self.create()
        self.windowHandle().screenChanged.connect(lambda _screen: self._show_capture_source())

    def appear(self) -> None:
        """Show the window in front, ready for the goal if the panel is asking for one."""
        if not self._placed:
            self._place_at_top_centre()
        self.show()
        self.raise_()
        self.activateWindow()
        self._focus_goal()

    def toggle(self) -> None:
        """Hide the whole window, bar included, or bring it back as it was. Hiding ends nothing."""
        if self.isVisible():
            self.hide()
        else:
            self.appear()

    @property
    def folded(self) -> bool:
        return self.body.isHidden()

    def fold(self) -> None:
        """Leave only the bar on screen. Whatever the panel was doing carries on behind it."""
        self.body.hide()
        self._fit()

    def unfold(self) -> None:
        """Bring the panel back under the bar, as it was."""
        self.body.show()
        self._fit()
        self.activateWindow()
        self._focus_goal()

    def set_goal(self, goal: str) -> None:
        self._goal.setText(goal)

    def show_goal_entry(self) -> None:
        self._show_only(self._entry)
        self.goal_input.setFocus()
        self._fit()

    def show_thinking(self) -> None:
        self.status.setText("Thinking…")
        self._show_only(self.status)
        self._fit()

    def show_notice(self, message: str) -> None:
        self.notice.setText(message)
        self.notice.show()
        self._fit()

    def show_error(self, message: str) -> None:
        self.error.setText(message)
        self.next_button.setText("Retry")
        self.next_button.setEnabled(True)
        self._show_only(self.error, self.next_button)
        self._fit()

    def show_step(self, step: Step, screenshot: Screenshot) -> None:
        self.instruction.setText(highlight_instruction(step.instruction, step.target_label, ACCENT))
        self.status.setText("Done" if step.done else "")
        self.next_button.setText("Next step")
        self.next_button.setEnabled(not step.done)
        self._show_only(
            self.instruction,
            *([self.status] if step.done else [self.next_button]),
            *([self.crop] if step.bbox is not None else []),
        )
        if step.bbox is None:
            self.crop.clear()
        else:
            image = decode_image(screenshot)
            view = crop_view(step.bbox, image.size)
            self.crop.setPixmap(render_crop(image, view, self.devicePixelRatioF()))
        self._fit()

    def show_usage(self, summary: str) -> None:
        """Show the usage summary in its own small window, read-only."""
        if self._usage_window is None:
            self._usage_window = UsageWindow(self)
        self._usage_window.show_summary(summary)

    def set_monitors(self, monitors: list[Monitor]) -> None:
        """List these monitors in the Capture monitor menu, keeping what is selected."""
        self._monitors = list(monitors)
        connectors = [monitor.connector for monitor in monitors]
        entries: list[tuple[str, Pick | str]] = [(Pick.FOLLOW_PANEL.value, Pick.FOLLOW_PANEL)]
        for monitor in sorted(monitors, key=lambda item: item.connector):
            entries.append((monitor_label(monitor), monitor.connector))
        if isinstance(self._selection, str) and self._selection not in connectors:
            # Kept, so the choice stays visible and the next capture reports it missing.
            entries.append((f"{self._selection}  (not connected)", self._selection))
        entries.append((Pick.WHOLE_DESKTOP.value, Pick.WHOLE_DESKTOP))

        for action in self._monitor_group.actions():
            self._monitor_group.removeAction(action)
        self.monitor_menu.clear()
        for text, data in entries:
            action = self.monitor_menu.addAction(text)
            action.setData(data)
            action.setCheckable(True)
            action.setChecked(data == self._selection)
            self._monitor_group.addAction(action)
        self.monitor_menu.setEnabled(True)
        self.capture_source.show()
        self._show_capture_source()
        self._fit()

    def monitor_entries(self) -> list[tuple[str, bool]]:
        """The Capture monitor menu as shown: each entry's text and whether it is checked."""
        return [(action.text(), action.isChecked()) for action in self.monitor_menu.actions()]

    def select_monitor(self, selection: Pick | str) -> None:
        """Pick an entry of the Capture monitor menu, as the user would."""
        for action in self._monitor_group.actions():
            if action.data() == selection:
                action.trigger()
                return
        raise ValueError(f"no capture monitor entry for {selection!r}")

    def monitor_choice(self) -> MonitorChoice:
        """The monitor the next capture is from, read from the panel as it is now."""
        if self._selection is Pick.WHOLE_DESKTOP:
            return WHOLE_DESKTOP
        if self._selection is Pick.FOLLOW_PANEL:
            return MonitorChoice(self.screen_name(), follows_panel=True)
        return MonitorChoice(self._selection)

    def screen_name(self) -> str:
        """The panel's screen as Qt names it. Through XWayland on GNOME, a Mutter connector."""
        return self.screen().name()

    def _on_monitor_picked(self, action: QAction) -> None:
        self._selection = action.data()
        self._show_capture_source()

    def _show_capture_source(self) -> None:
        choice = self.monitor_choice()
        monitor = next(
            (item for item in self._monitors if item.connector == choice.connector), None
        )
        if choice.connector is None:
            source = "the whole desktop, every monitor"
        elif monitor is None and choice.follows_panel:
            source = f"screen {choice.connector}, which matches no monitor"
        elif monitor is None:
            source = f"{choice.connector}, not connected"
        else:
            source = monitor_label(monitor) + (
                ", where the panel is" if choice.follows_panel else ""
            )
        self.capture_source.setText(f"Next capture: {source}")

    def _end_and_fold(self) -> None:
        self.task_ended.emit()
        self.fold()

    def _focus_goal(self) -> None:
        if not self.folded and not self._entry.isHidden():
            self.goal_input.setFocus()

    def _place_at_top_centre(self) -> None:
        area = self.screen().availableGeometry()
        self.move(area.x() + (area.width() - self.width()) // 2, area.y() + TOP_OFFSET)
        self._placed = True

    def _show_only(self, *shown: QWidget) -> None:
        for widget in (
            self._entry,
            self.status,
            self.error,
            self.instruction,
            self.crop,
            self.notice,
            self.next_button,
        ):
            widget.setVisible(widget in shown)

    def _fit(self) -> None:
        # Left to the next event loop pass, the layout's old minimum height would block the shrink.
        for layout in (self._body_layout, self._layout):
            layout.invalidate()
            layout.activate()
        self.resize(PANEL_WIDTH, self._layout.totalHeightForWidth(PANEL_WIDTH))
        self._update_mask()

    def _update_mask(self) -> None:
        # Only the bar and the panel take clicks; around and between them, they reach the window
        # behind, as the eye expects from what is drawn there.
        region = QRegion(self.bar.geometry())
        if not self.folded:
            region = region.united(QRegion(self.body.geometry()))
        self.setMask(region)

    def _submit_goal(self) -> None:
        goal = self.goal_input.text().strip()
        if goal:
            self.goal_submitted.emit(goal)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_mask()

    def moveEvent(self, event: QMoveEvent) -> None:
        # Dragged to another monitor, the panel follows it with the next capture.
        super().moveEvent(event)
        self._show_capture_source()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.closed.emit()
        super().closeEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # With no title bar, the bar's and the panel's backgrounds are what move the window, both
        # together, off whatever it covers.
        if event.button() == Qt.MouseButton.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()


class UsageWindow(QWidget):
    """The `screenguide usage` summary, read-only, above the panel it was opened from."""

    def __init__(self, parent: QWidget) -> None:
        # A window of the panel's own, so it stays above it and ends with it.
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("screenguide usage")
        self.setStyleSheet(USAGE_STYLE)
        self.text = QPlainTextEdit(readOnly=True)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self.text)
        self.resize(640, 420)

    def show_summary(self, summary: str) -> None:
        self.text.setPlainText(summary)
        self.show()
        self.raise_()
        self.activateWindow()


def render_crop(image: Image.Image, view: CropView, pixel_ratio: float = 1.0) -> QPixmap:
    box = view.region
    size = (round(view.width * pixel_ratio), round(view.height * pixel_ratio))
    region = image.crop((box.x, box.y, box.x + box.width, box.y + box.height)).convert("RGB")
    enlarged = region.resize(size, Image.Resampling.LANCZOS)
    # QImage does not own the bytes it is given, so the pixmap has to be made while they live.
    pixels = enlarged.tobytes()
    qimage = QImage(pixels, size[0], size[1], 3 * size[0], QImage.Format.Format_RGB888)
    pixmap = QPixmap.fromImage(qimage)
    pixmap.setDevicePixelRatio(pixel_ratio)
    _draw_arrow(pixmap, view)
    return pixmap


def _draw_arrow(pixmap: QPixmap, view: CropView) -> None:
    tip = QPointF(view.tip.x, view.tip.y)
    tail = QPointF(view.tail.x, view.tail.y)
    length = math.hypot(view.tail.x - view.tip.x, view.tail.y - view.tip.y)
    if length == 0:
        return
    along = (tail - tip) / length
    across = QPointF(-along.y(), along.x())
    head = min(ARROW_HEAD, length)
    base = tip + along * head
    wings = QPolygonF([tip, base + across * head * 0.55, base - across * head * 0.55])

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # A dark outline around a light fill stays visible on any screenshot.
    for colour, width in ((ARROW_OUTLINE, 6.0), (QColor(ACCENT), 3.0)):
        painter.setPen(QPen(colour, width, c=Qt.PenCapStyle.RoundCap))
        painter.drawLine(base, tail)
    painter.setPen(QPen(ARROW_OUTLINE, 1.5))
    painter.setBrush(QColor(ACCENT))
    painter.drawPolygon(wings)
    painter.end()


DEMO_GOAL = "Demo: see how a step looks"


def demo_steps(image_size: tuple[int, int]) -> list[Step]:
    """Made-up steps that show each case on any image: centre, corner, no box and done."""
    width, height = image_size
    box_width, box_height = min(120, width), min(32, height)
    centre = BBox((width - box_width) // 2, (height - box_height) // 2, box_width, box_height)
    corner = BBox(0, 0, box_width, box_height)
    return [
        Step("Target", "Click Target, in the middle of the screen.", centre, False, 1.0),
        Step("Corner", "Click Corner, at the top left of the screen.", corner, False, 1.0),
        Step(None, "Click the gear icon, at the far right of the toolbar.", None, False, 1.0),
        Step(None, "All set: that was the last step.", None, True, 1.0),
    ]


def start_app() -> QApplication:
    """The application the panel runs in, on a platform that can keep it on top."""
    # GNOME Wayland lets no client keep itself on top, so the panel goes through XWayland.
    platform = os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    if platform == "xcb" and not os.environ.get("DISPLAY"):
        raise OSError("the panel needs X11 or XWayland, but DISPLAY is not set")
    # Qt's event loop would otherwise hold back Ctrl-C until the window closes.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    app = QApplication.instance() or QApplication([])
    logger.info("panel platform=%s", QApplication.platformName())
    return app


def run_demo(screenshot: Screenshot, usage_summary: Callable[[], str]) -> int:
    """Show the demo steps until Quit. No session runs and nothing is captured."""
    steps = iter(demo_steps(decode_image(screenshot).size))
    app = start_app()
    panel = StepPanel(DEMO_GOAL)
    panel.next_requested.connect(lambda: panel.show_step(next(steps), screenshot))
    panel.usage_requested.connect(lambda: panel.show_usage(usage_summary()))
    panel.quit_requested.connect(app.quit)
    panel.show_step(next(steps), screenshot)
    panel.appear()
    return app.exec()
