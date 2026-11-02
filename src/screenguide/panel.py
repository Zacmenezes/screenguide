"""`layer:ui`: the panel that shows one step above the other windows."""

import logging
import math
import os
import signal

from PIL import Image
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import (
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
    QPushButton {{
        color: {ACCENT}; background: transparent; font-weight: 600;
        border: 1px solid #52525b; border-radius: 8px; padding: 7px 14px;
    }}
    QPushButton:hover {{ background: rgba(255, 255, 255, 25); }}
    QPushButton:disabled {{ color: #71717a; }}
    QPushButton#close {{ color: #d4d4d8; font-weight: 400; border-radius: 12px; padding: 0; }}
"""


class StepPanel(QWidget):
    """Shows a step and reports "Next step" presses. What a press does is up to its owner."""

    next_requested = Signal()

    def __init__(self, goal: str) -> None:
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
        self._layout.addWidget(self.instruction)
        self._layout.addWidget(self.crop)
        self._layout.addWidget(self.next_button, alignment=Qt.AlignmentFlag.AlignLeft)

    def show_step(self, step: Step, screenshot: Screenshot) -> None:
        self.instruction.setText(highlight_instruction(step.instruction, step.target_label, ACCENT))
        self.crop.setVisible(step.bbox is not None)
        if step.bbox is None:
            self.crop.clear()
        else:
            image = decode_image(screenshot)
            view = crop_view(step.bbox, image.size)
            self.crop.setPixmap(render_crop(image, view, self.devicePixelRatioF()))
        self.next_button.setEnabled(not step.done)
        # Left to the next event loop pass, the layout's old minimum height would block the shrink.
        self._layout.invalidate()
        self._layout.activate()
        self.resize(PANEL_WIDTH, self._layout.totalHeightForWidth(PANEL_WIDTH))

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


def run_demo(screenshot: Screenshot) -> int:
    """Show the demo steps until the panel is closed. No session runs and nothing is captured."""
    steps = iter(demo_steps(decode_image(screenshot).size))

    # GNOME Wayland lets no client keep itself on top, so the panel goes through XWayland.
    platform = os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    if platform == "xcb" and not os.environ.get("DISPLAY"):
        raise OSError("the panel needs X11 or XWayland, but DISPLAY is not set")
    # Qt's event loop would otherwise hold back Ctrl-C until the window closes.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    app = QApplication.instance() or QApplication([])
    panel = StepPanel(DEMO_GOAL)
    panel.next_requested.connect(lambda: panel.show_step(next(steps), screenshot))
    panel.show_step(next(steps), screenshot)
    panel.show()
    logger.info("panel platform=%s", QApplication.platformName())
    return app.exec()
