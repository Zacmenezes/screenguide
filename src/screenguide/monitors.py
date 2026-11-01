"""`layer:capture`: the monitor layout from Mutter, and cropping a desktop image to one monitor."""

import io
import logging
from dataclasses import dataclass
from fractions import Fraction

from jeepney import DBusAddress, HeaderFields, MessageType, new_method_call
from jeepney.io.blocking import open_dbus_connection
from PIL import Image

logger = logging.getLogger(__name__)

DISPLAY_CONFIG_NAME = "org.gnome.Mutter.DisplayConfig"
DISPLAY_CONFIG_PATH = "/org/gnome/Mutter/DisplayConfig"
CALL_TIMEOUT_SECONDS = 10.0

# Values of Mutter's `layout-mode`. An absent property means logical.
LAYOUT_LOGICAL = 1
LAYOUT_PHYSICAL = 2

# How far the desktop's height may stray from the image's, in image pixels, from rounding alone.
ALIGNMENT_TOLERANCE_PIXELS = 1


class MonitorError(RuntimeError):
    """A monitor could not be listed or cropped to. The message is written for the user."""


@dataclass(frozen=True)
class Monitor:
    """An active monitor. `x`, `y`, `width` and `height` are in Mutter's layout units."""

    connector: str
    display_name: str
    mode_width: int
    mode_height: int
    scale: float
    x: int
    y: int
    width: int
    height: int


def read_monitors() -> list[Monitor]:
    """Return the active monitors, as `org.gnome.Mutter.DisplayConfig` reports them."""
    try:
        connection = open_dbus_connection(bus="SESSION")
    except (KeyError, OSError, ValueError) as exc:
        raise MonitorError(f"cannot reach the D-Bus session bus: {exc!r}") from exc

    address = DBusAddress(
        DISPLAY_CONFIG_PATH, bus_name=DISPLAY_CONFIG_NAME, interface=DISPLAY_CONFIG_NAME
    )
    with connection:
        try:
            reply = connection.send_and_get_reply(
                new_method_call(address, "GetCurrentState"), timeout=CALL_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            raise MonitorError("the monitor layout API did not answer") from exc
    if reply.header.message_type == MessageType.error:
        error_name = reply.header.fields.get(HeaderFields.error_name, "unknown error")
        raise MonitorError(
            f"the monitor layout is not available ({error_name}); "
            f"choosing a monitor needs GNOME's {DISPLAY_CONFIG_NAME}"
        )
    return parse_state(*reply.body)


def parse_state(_serial, monitors, logical_monitors, properties) -> list[Monitor]:
    """Turn the `GetCurrentState` reply into monitors. A mirrored connector gets its own entry."""
    layout_mode = properties.get("layout-mode", ("u", LAYOUT_LOGICAL))[1]
    if layout_mode not in (LAYOUT_LOGICAL, LAYOUT_PHYSICAL):
        raise MonitorError(f"unknown monitor layout mode: {layout_mode}")

    physical = {spec[0]: (modes, props) for spec, modes, props in monitors}
    result = []
    for x, y, scale, transform, _primary, specs, _props in logical_monitors:
        for spec in specs:
            connector = spec[0]
            modes, props = physical.get(connector, ([], {}))
            current = next((mode for mode in modes if _is_current(mode)), None)
            if current is None:
                raise MonitorError(f"the monitor layout gives no current mode for {connector}")
            mode_width, mode_height = current[1], current[2]
            width, height = mode_width, mode_height
            if transform % 2:  # 90 or 270 degrees, flipped or not.
                width, height = height, width
            if layout_mode == LAYOUT_LOGICAL:
                width, height = round(width / scale), round(height / scale)
            result.append(
                Monitor(
                    connector=connector,
                    display_name=props.get("display-name", ("s", ""))[1],
                    mode_width=mode_width,
                    mode_height=mode_height,
                    scale=scale,
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                )
            )
    return result


def _is_current(mode) -> bool:
    return mode[6].get("is-current", ("b", False))[1]


def find_monitor(connector: str, monitors: list[Monitor]) -> Monitor:
    for monitor in monitors:
        if monitor.connector == connector:
            return monitor
    available = ", ".join(sorted(monitor.connector for monitor in monitors)) or "none"
    raise MonitorError(
        f"monitor {connector} is not connected or not active (available: {available})"
    )


def crop_box(
    monitor: Monitor, monitors: list[Monitor], image_width: int, image_height: int
) -> tuple[int, int, int, int]:
    """Return the monitor's (left, top, right, bottom) in pixels of the whole-desktop image.

    The image is the desktop's bounding box at one uniform factor, so an edge can land between
    pixels. It rounds to the nearest pixel, which is where a real capture has the monitor's edge:
    at factor 1.25, x=303 and x=2223 are drawn from column 379 up to column 2779, not 2778.
    """
    left = min(item.x for item in monitors)
    top = min(item.y for item in monitors)
    desktop_width = max(item.x + item.width for item in monitors) - left
    desktop_height = max(item.y + item.height for item in monitors) - top

    x_factor = Fraction(image_width, desktop_width)
    y_factor = Fraction(image_height, desktop_height)
    if abs(desktop_height * x_factor - image_height) > ALIGNMENT_TOLERANCE_PIXELS:
        raise MonitorError(
            f"the {image_width}x{image_height} screenshot does not line up with the "
            f"{desktop_width}x{desktop_height} monitor layout, so it cannot be cropped"
        )
    return (
        round((monitor.x - left) * x_factor),
        round((monitor.y - top) * y_factor),
        round((monitor.x + monitor.width - left) * x_factor),
        round((monitor.y + monitor.height - top) * y_factor),
    )


def crop_to_monitor(data: bytes, monitor: Monitor, monitors: list[Monitor]) -> bytes:
    """Return the part of a whole-desktop image that shows the monitor, as a PNG."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            box = crop_box(monitor, monitors, image.width, image.height)
            cropped = image.crop(box)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise MonitorError(f"cannot decode the screenshot to crop it: {exc}") from exc
    output = io.BytesIO()
    cropped.save(output, format="PNG")
    logger.info("screenshot cropped monitor=%s box=%s", monitor.connector, box)
    return output.getvalue()


def format_monitors(monitors: list[Monitor]) -> str:
    """One line per monitor, led by the connector name that config and `--monitor` take."""
    width = max((len(monitor.connector) for monitor in monitors), default=0)
    return "\n".join(
        f"{monitor.connector:<{width}}  {monitor.mode_width}x{monitor.mode_height}"
        f"  scale {monitor.scale:g}  at {monitor.x},{monitor.y}  {monitor.display_name}".rstrip()
        for monitor in sorted(monitors, key=lambda item: item.connector)
    )
