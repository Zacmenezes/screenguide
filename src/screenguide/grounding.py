"""`layer:grounding`: numbers the boxes and maps the chosen number back to a bbox."""

from dataclasses import dataclass
from typing import Protocol

from screenguide.capture import Screenshot
from screenguide.detection import BBox, Element


@dataclass(frozen=True)
class MarkedScreen:
    """The image the model sees and the numbered elements it can choose from."""

    image: Screenshot
    elements: dict[int, Element]


class GroundingStrategy(Protocol):
    def mark(self, screenshot: Screenshot, elements: list[Element]) -> MarkedScreen: ...

    def resolve(self, marked: MarkedScreen, element_id: int) -> BBox | None:
        """Return the bbox for a chosen number, or `None` if no box has that number."""
        ...


class NumberingGrounding:
    """Stub: uses the detector's numbers and leaves the image undrawn."""

    def mark(self, screenshot: Screenshot, elements: list[Element]) -> MarkedScreen:
        numbered = {element.id: element for element in elements}
        return MarkedScreen(image=screenshot, elements=numbered)

    def resolve(self, marked: MarkedScreen, element_id: int) -> BBox | None:
        element = marked.elements.get(element_id)
        return element.bbox if element else None
