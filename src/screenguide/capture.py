"""`layer:capture`: returns the screen image, always on command."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Screenshot:
    """An image held in memory. A dimension nobody has measured stays `None`."""

    data: bytes
    width: int | None = None
    height: int | None = None


class ScreenCapturer(Protocol):
    def capture(self) -> Screenshot: ...


class FileCapturer:
    """Stub: reads an image file instead of the screen, without decoding it."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def capture(self) -> Screenshot:
        return Screenshot(data=self._path.read_bytes())
