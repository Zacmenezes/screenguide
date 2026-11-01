"""`layer:detection`: finds elements and their bboxes."""

from dataclasses import dataclass
from typing import Protocol

from screenguide.capture import Screenshot


@dataclass(frozen=True)
class BBox:
    """A box in the coordinates of the captured image."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class Element:
    label: str
    bbox: BBox


class ElementDetector(Protocol):
    def detect(self, screenshot: Screenshot) -> list[Element]: ...


class StubDetector:
    """Stub: returns the same boxes for any image."""

    def detect(self, screenshot: Screenshot) -> list[Element]:
        return [
            Element("File", BBox(x=10, y=8, width=40, height=20)),
            Element("Settings", BBox(x=60, y=8, width=70, height=20)),
            Element("Help", BBox(x=140, y=8, width=40, height=20)),
        ]
