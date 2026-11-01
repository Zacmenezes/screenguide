"""`layer:capture`: returns the screen image, always on command."""

import logging
import os
import secrets
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlsplit

from jeepney import (
    DBusAddress,
    HeaderFields,
    MatchRule,
    MessageType,
    message_bus,
    new_method_call,
)
from jeepney.io.blocking import Proxy, open_dbus_connection

from screenguide.monitors import MonitorError, crop_to_monitor, find_monitor, read_monitors

logger = logging.getLogger(__name__)

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_INTERFACE = "org.freedesktop.portal.Screenshot"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"

# Long enough for the user to answer a permission dialog.
RESPONSE_TIMEOUT_SECONDS = 120.0
CALL_TIMEOUT_SECONDS = 10.0

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class CaptureError(RuntimeError):
    """The screen could not be captured. The message is written for the user."""


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


class PortalCapturer:
    """Captures through `org.freedesktop.portal.Screenshot`.

    The portal hands over a file, so the capture is read into memory and that file is removed.
    The portal returns the whole desktop as one image. With a monitor chosen, that image is cropped
    to it before anything else sees or saves it; with none, the whole desktop is the capture.
    """

    def __init__(
        self,
        save_dir: Path | None = None,
        response_timeout: float = RESPONSE_TIMEOUT_SECONDS,
        monitor: str | None = None,
    ) -> None:
        self._save_dir = save_dir
        self._response_timeout = response_timeout
        self._monitor = monitor

    def capture(self) -> Screenshot:
        # Checked first, so a wrong name or a missing layout API never takes a screenshot.
        monitors = read_monitors() if self._monitor else []
        chosen = find_monitor(self._monitor, monitors) if self._monitor else None

        path = _path_from_uri(_request_screenshot(self._response_timeout))
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CaptureError(f"cannot read the portal's screenshot: {exc}") from exc
        finally:
            _remove_portal_file(path)
        if not data:
            raise CaptureError(f"the portal returned an empty screenshot: {path}")

        suffix = path.suffix
        if chosen is not None:
            # The answer to a permission dialog can take long enough for the layout to change.
            if read_monitors() != monitors:
                raise MonitorError("the monitor layout changed during the capture; try again")
            data = crop_to_monitor(data, chosen, monitors)
            suffix = ".png"

        if self._save_dir is not None:
            _save(data, self._save_dir, suffix)
        width, height = _png_size(data)
        return Screenshot(data=data, width=width, height=height)


def _request_screenshot(response_timeout: float) -> str:
    """Ask the portal for a screenshot and return the URI of the file it wrote."""
    try:
        connection = open_dbus_connection(bus="SESSION")
    except (KeyError, OSError, ValueError) as exc:
        raise CaptureError(f"cannot reach the D-Bus session bus: {exc!r}") from exc

    with connection:
        token = f"screenguide_{secrets.token_hex(8)}"
        sender = connection.unique_name.removeprefix(":").replace(".", "_")
        handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        rule = MatchRule(type="signal", interface=REQUEST_INTERFACE, member="Response", path=handle)

        # Subscribed before the call, so a fast reply cannot be missed.
        Proxy(message_bus, connection).AddMatch(rule)
        with connection.filter(rule) as responses:
            call = new_method_call(
                DBusAddress(PORTAL_PATH, bus_name=PORTAL_NAME, interface=PORTAL_INTERFACE),
                "Screenshot",
                "sa{sv}",
                ("", {"handle_token": ("s", token), "interactive": ("b", False)}),
            )
            try:
                reply = connection.send_and_get_reply(call, timeout=CALL_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                raise CaptureError("the screenshot portal did not answer") from exc
            if reply.header.message_type == MessageType.error:
                error_name = reply.header.fields.get(HeaderFields.error_name, "unknown error")
                raise CaptureError(
                    f"the screenshot portal is not available ({error_name}); "
                    "check that xdg-desktop-portal is installed and running"
                )
            try:
                response = connection.recv_until_filtered(responses, timeout=response_timeout)
            except TimeoutError as exc:
                _close_request(connection, handle)
                raise CaptureError(
                    f"no answer from the screenshot portal after {response_timeout:g} seconds"
                ) from exc

    return _uri_from_response(*response.body)


def _close_request(connection, handle: str) -> None:
    """Withdraw a request nobody answered, so its dialog does not stay on screen."""
    address = DBusAddress(handle, bus_name=PORTAL_NAME, interface=REQUEST_INTERFACE)
    try:
        connection.send_and_get_reply(new_method_call(address, "Close"), timeout=1)
    except (OSError, TimeoutError):
        logger.warning("could not close portal request %s", handle)


def _uri_from_response(code: int, results: dict) -> str:
    if code == 1:
        raise CaptureError("screenshot denied: the permission request was refused or cancelled")
    if code != 0:
        # A permission stored as denied ends here too, not in code 1.
        raise CaptureError(
            f"screenshot denied or failed (response code {code}): "
            "check that screen capture is allowed for this app"
        )
    signature, uri = results.get("uri", ("s", ""))
    if signature != "s" or not uri:
        raise CaptureError("the screenshot portal reported success without an image")
    return uri


def _path_from_uri(uri: str) -> Path:
    parts = urlsplit(uri)
    if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
        raise CaptureError(f"the screenshot portal returned an unsupported location: {uri}")
    return Path(unquote(parts.path))


def _remove_portal_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("screenshot left on disk at %s: %s", path, exc)


def _save(data: bytes, directory: Path, suffix: str) -> None:
    """Write a private copy of the capture."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"screenshot-{stamp}{suffix or '.png'}"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(data)
    logger.info("screenshot saved path=%s", path)


def _png_size(data: bytes) -> tuple[int | None, int | None]:
    """Read width and height from a PNG header; any other data stays unmeasured."""
    if data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR" or len(data) < 24:
        return None, None
    width, height = struct.unpack(">II", data[16:24])
    return width, height
