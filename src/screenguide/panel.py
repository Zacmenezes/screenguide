"""`layer:ui`: the panel that shows the guide above the other windows."""

import logging
import math
import os
import signal

from PIL import Image
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QImage,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from screenguide.capture import Screenshot
from screenguide.detection import BBox, decode_image
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
    QLabel#notice {{ color: #a1a1aa; font-size: 12px; }}
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
"""


class StepPanel(QWidget):
    """Shows what it is told and reports what the user does, leaving the rest to its owner."""

    goal_submitted = Signal(str)
    next_requested = Signal()
    closed = Signal()

    def __init__(self, goal: str = "") -> None:
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

        self._close = QPushButton("×", objectName="close")
        self._close.setFixedSize(24, 24)
        self._close.clicked.connect(self.close)
        self._goal = QLabel(goal, objectName="goal", wordWrap=True)
        self._goal.setTextFormat(Qt.TextFormat.PlainText)
        header = QHBoxLayout()
        header.addWidget(self._close, alignment=Qt.AlignmentFlag.AlignTop)
        header.addWidget(self._goal, stretch=1)

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

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(MARGIN, MARGIN - 6, MARGIN, MARGIN - 6)
        self._layout.setSpacing(12)
        self._layout.addLayout(header)
        self._layout.addWidget(self._entry)
        self._layout.addWidget(self.status)
        self._layout.addWidget(self.error)
        self._layout.addWidget(self.instruction)
        self._layout.addWidget(self.crop)
        self._layout.addWidget(self.notice)
        self._layout.addWidget(self.next_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self._show_only()

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
        self._layout.invalidate()
        self._layout.activate()
        self.resize(PANEL_WIDTH, self._layout.totalHeightForWidth(PANEL_WIDTH))

    def _submit_goal(self) -> None:
        goal = self.goal_input.text().strip()
        if goal:
            self.goal_submitted.emit(goal)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.closed.emit()
        super().closeEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(BACKGROUND)
        painter.drawRoundedRect(self.rect(), 14, 14)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # With no title bar, the panel's background is what moves it off whatever it covers.
        if event.button() == Qt.MouseButton.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()


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


def run_demo(screenshot: Screenshot) -> int:
    """Show the demo steps until the panel is closed. No session runs and nothing is captured."""
    steps = iter(demo_steps(decode_image(screenshot).size))
    app = start_app()
    panel = StepPanel(DEMO_GOAL)
    panel.next_requested.connect(lambda: panel.show_step(next(steps), screenshot))
    panel.show_step(next(steps), screenshot)
    panel.show()
    return app.exec()
