"""Monitor layout and crop, with Mutter faked on a private D-Bus daemon or as a function."""

import io
import threading

import pytest
from jeepney import MessageType, message_bus, new_method_return
from jeepney.io.blocking import Proxy, open_dbus_connection
from PIL import Image

from screenguide import capture
from screenguide.capture import PortalCapturer
from screenguide.cli import main
from screenguide.config import load_config
from screenguide.monitors import (
    MonitorError,
    crop_box,
    crop_to_monitor,
    find_monitor,
    parse_state,
    read_monitors,
)

STATE_SIGNATURE = "ua((ssss)a(siiddada{sv})a{sv})a(iiduba(ssss)a{sv})a{sv}"


def spec(connector):
    return (connector, "vendor", "product", "serial")


def physical(connector, width, height, name="Some display", current=True):
    props = {"is-current": ("b", True)} if current else {}
    mode = ("mode", width, height, 60.0, 1.0, [1.0], props)
    return (spec(connector), [mode], {"display-name": ("s", name)})


def logical(connector, x, y, scale=1.0, transform=0):
    return (x, y, scale, transform, False, [spec(connector)], {})


# The maintainer's desktop: a 5120x2700 image from a 4096x2160 layout at the highest scale.
MIXED_STATE = (
    28,
    [
        physical("eDP-1", 1920, 1200, "Built-in display"),
        physical("DP-3", 1920, 1080),
        physical("DP-4", 2560, 1080),
    ],
    [logical("eDP-1", 2560, 1200, 1.25), logical("DP-4", 0, 1080), logical("DP-3", 303, 0)],
    {"layout-mode": ("u", 1)},
)
MIXED = parse_state(*MIXED_STATE)


def monitor(connector, monitors=MIXED):
    return find_monitor(connector, monitors)


def test_scale_shrinks_a_monitor_in_a_logical_layout():
    edp = monitor("eDP-1")

    assert (edp.x, edp.y, edp.width, edp.height) == (2560, 1200, 1536, 960)
    assert (edp.mode_width, edp.mode_height, edp.scale) == (1920, 1200, 1.25)
    assert edp.display_name == "Built-in display"


def test_physical_layout_ignores_scale():
    state = ([physical("A", 1920, 1200)], [logical("A", 0, 0, 2.0)], {"layout-mode": ("u", 2)})

    assert parse_state(0, *state)[0].width == 1920


def test_absent_layout_mode_means_logical():
    state = ([physical("A", 1920, 1200)], [logical("A", 0, 0, 2.0)], {})

    assert parse_state(0, *state)[0].width == 960


@pytest.mark.parametrize("transform", [1, 3, 5, 7])
def test_rotation_swaps_width_and_height(transform):
    state = ([physical("A", 1920, 1080)], [logical("A", 0, 0, 1.0, transform)], {})

    result = parse_state(0, *state)[0]
    assert (result.width, result.height) == (1080, 1920)


def test_connected_monitor_without_a_logical_monitor_is_not_active():
    state = ([physical("A", 800, 600), physical("B", 800, 600)], [logical("A", 0, 0)], {})

    assert [item.connector for item in parse_state(0, *state)] == ["A"]


@pytest.mark.parametrize(
    "state",
    [
        ([physical("A", 800, 600, current=False)], [logical("A", 0, 0)], {}),
        ([], [logical("A", 0, 0)], {}),
    ],
)
def test_logical_monitor_without_a_current_mode_is_an_error(state):
    with pytest.raises(MonitorError, match="no current mode for A"):
        parse_state(0, *state)


def test_unknown_layout_mode_is_an_error():
    with pytest.raises(MonitorError, match="layout mode"):
        parse_state(0, [], [], {"layout-mode": ("u", 9)})


@pytest.mark.parametrize(
    ("connector", "box"),
    [
        ("DP-4", (0, 1350, 3200, 2700)),
        ("eDP-1", (3200, 1500, 5120, 2700)),
        # 303 * 1.25 = 378.75 and 2223 * 1.25 = 2778.75: the edges a real capture showed.
        ("DP-3", (379, 0, 2779, 1350)),
    ],
)
def test_crop_box_on_mixed_scales(connector, box):
    assert crop_box(monitor(connector), MIXED, 5120, 2700) == box


def test_crop_box_at_uniform_scale_uses_the_layout_as_is():
    state = (
        [physical("A", 800, 600), physical("B", 800, 600)],
        [logical("A", 0, 0), logical("B", 800, 100)],
        {},
    )
    monitors = parse_state(0, *state)

    assert crop_box(monitor("B", monitors), monitors, 1600, 700) == (800, 100, 1600, 700)


def test_crop_box_measures_from_the_top_left_of_the_desktop():
    state = ([physical("A", 800, 600)], [logical("A", 100, 50)], {})
    monitors = parse_state(0, *state)

    assert crop_box(monitors[0], monitors, 800, 600) == (0, 0, 800, 600)


def test_image_that_does_not_line_up_with_the_layout_is_an_error():
    with pytest.raises(MonitorError, match="does not line up"):
        crop_box(monitor("DP-4"), MIXED, 5120, 2000)


def test_unknown_monitor_names_the_available_ones():
    with pytest.raises(MonitorError, match=r"HDMI-9 is not connected.*DP-3, DP-4, eDP-1"):
        find_monitor("HDMI-9", MIXED)


def desktop_png(width=5120, height=2700, color=(255, 0, 0)):
    """Black, as gaps between monitors are, with DP-4's area painted."""
    image = Image.new("RGB", (width, height))
    image.paste(color, (0, 1350, 3200, 2700))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_crop_keeps_only_the_monitor_pixels():
    cropped = Image.open(io.BytesIO(crop_to_monitor(desktop_png(), monitor("DP-4"), MIXED)))

    assert cropped.size == (3200, 1350)
    assert cropped.getcolors() == [(3200 * 1350, (255, 0, 0))]


def test_undecodable_image_is_an_error():
    with pytest.raises(MonitorError, match="cannot decode"):
        crop_to_monitor(b"not an image", monitor("DP-4"), MIXED)


class FakeDisplayConfig:
    def __init__(self, state):
        self.state = state
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve)

    def __enter__(self):
        self._thread.start()
        assert self._ready.wait(5)
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join(5)

    def _serve(self):
        with open_dbus_connection(bus="SESSION") as connection:
            Proxy(message_bus, connection).RequestName("org.gnome.Mutter.DisplayConfig")
            self._ready.set()
            while not self._stop.is_set():
                try:
                    message = connection.receive(timeout=0.05)
                except TimeoutError:
                    continue
                if message.header.message_type == MessageType.method_call:
                    connection.send(new_method_return(message, STATE_SIGNATURE, self.state))


def test_read_monitors_decodes_a_real_dbus_reply(bus):
    with FakeDisplayConfig(MIXED_STATE):
        assert read_monitors() == MIXED


def test_absent_layout_api_gives_a_clear_error(bus):
    with pytest.raises(MonitorError, match="not available.*Mutter"):
        read_monitors()


def test_absent_session_bus_gives_a_clear_error(monkeypatch):
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)

    with pytest.raises(MonitorError, match="session bus"):
        read_monitors()


def test_cli_lists_monitors_by_connector(bus, capsys):
    with FakeDisplayConfig(MIXED_STATE):
        assert main(["monitors"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == ["DP-3", "DP-4", "eDP-1"]
    assert lines[2] == "eDP-1  1920x1200  scale 1.25  at 2560,1200  Built-in display"


def test_cli_reports_an_absent_layout_api(bus, capsys):
    assert main(["monitors"]) == 1
    assert "not available" in capsys.readouterr().err


class Requests:
    """Stands in for the portal and for Mutter, counting what the capturer asks for."""

    def __init__(self, monkeypatch, tmp_path, layouts=None, image=None):
        self.screenshots = 0
        self.path = tmp_path / "portal.png"
        self.image = desktop_png() if image is None else image
        self.layouts = [MIXED] if layouts is None else layouts
        self.reads = 0
        monkeypatch.setattr(capture, "_request_screenshot", self._request)
        monkeypatch.setattr(capture, "read_monitors", self._read)

    def _request(self, _timeout):
        self.screenshots += 1
        self.path.write_bytes(self.image)
        return self.path.as_uri()

    def _read(self):
        layout = self.layouts[min(self.reads, len(self.layouts) - 1)]
        self.reads += 1
        if isinstance(layout, Exception):
            raise layout
        return layout


def test_chosen_monitor_is_cropped_and_measured(monkeypatch, tmp_path):
    Requests(monkeypatch, tmp_path)

    screenshot = PortalCapturer(monitor="DP-4").capture()

    assert (screenshot.width, screenshot.height) == (3200, 1350)
    assert Image.open(io.BytesIO(screenshot.data)).size == (3200, 1350)


def test_no_choice_keeps_the_whole_desktop_and_never_reads_the_layout(monkeypatch, tmp_path):
    requests = Requests(monkeypatch, tmp_path)

    screenshot = PortalCapturer().capture()

    assert screenshot.data == requests.image
    assert requests.reads == 0


def test_saving_stores_the_cropped_image(monkeypatch, tmp_path):
    Requests(monkeypatch, tmp_path)
    save_dir = tmp_path / "saved"

    screenshot = PortalCapturer(save_dir=save_dir, monitor="DP-4").capture()

    assert [path.read_bytes() for path in save_dir.iterdir()] == [screenshot.data]


def test_unknown_monitor_takes_no_screenshot(monkeypatch, tmp_path):
    requests = Requests(monkeypatch, tmp_path)

    with pytest.raises(MonitorError, match="HDMI-9"):
        PortalCapturer(monitor="HDMI-9").capture()

    assert requests.screenshots == 0


def test_unavailable_layout_takes_no_screenshot(monkeypatch, tmp_path):
    requests = Requests(monkeypatch, tmp_path, layouts=[MonitorError("no layout")])

    with pytest.raises(MonitorError, match="no layout"):
        PortalCapturer(monitor="DP-4").capture()

    assert requests.screenshots == 0


@pytest.mark.parametrize(
    ("layouts", "image", "message"),
    [
        ([MIXED, MIXED[:1]], None, "layout changed"),
        ([MIXED, MonitorError("layout gone")], None, "layout gone"),
        (None, desktop_png(height=2000), "does not line up"),
        (None, b"not an image", "cannot decode"),
    ],
)
def test_failure_after_the_screenshot_keeps_nothing(monkeypatch, tmp_path, layouts, image, message):
    requests = Requests(monkeypatch, tmp_path, layouts=layouts, image=image)
    save_dir = tmp_path / "saved"

    with pytest.raises(MonitorError, match=message):
        PortalCapturer(save_dir=save_dir, monitor="DP-4").capture()

    assert not save_dir.exists()
    assert not requests.path.exists()


def test_monitor_comes_from_file_or_environment(home, monkeypatch):
    assert load_config().monitor is None

    path = home / ".config" / "screenguide" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('monitor = "DP-3"\n')
    assert load_config().monitor == "DP-3"

    monkeypatch.setenv("SCREENGUIDE_MONITOR", "DP-4")
    assert load_config().monitor == "DP-4"


def test_run_option_overrides_configuration(monkeypatch, tmp_path, usage_log, capsys):
    Requests(monkeypatch, tmp_path)
    monkeypatch.setenv("SCREENGUIDE_MONITOR", "HDMI-9")
    monkeypatch.setenv("SCREENGUIDE_IMAGE_LONG_EDGE", "4000")  # Keeps the monitor's own width.

    assert main(["run", "--capture", "--monitor", "DP-4", "--goal", "Open settings"]) == 0

    assert '"image_width": 3200' in usage_log.read_text()


def test_cli_reports_a_chosen_monitor_that_is_absent(monkeypatch, tmp_path, usage_log, capsys):
    Requests(monkeypatch, tmp_path)

    assert main(["run", "--capture", "--monitor", "HDMI-9", "--goal", "Open settings"]) == 1

    assert "HDMI-9" in capsys.readouterr().err
    assert not usage_log.exists()


def test_configured_monitor_does_not_apply_to_an_image_file(monkeypatch, image, capsys):
    monkeypatch.setenv("SCREENGUIDE_MONITOR", "HDMI-9")

    assert main(["run", "--image", str(image), "--goal", "Open settings"]) == 0


def test_monitor_option_is_refused_with_an_image_file(image):
    with pytest.raises(SystemExit):
        main(["run", "--image", str(image), "--monitor", "DP-4", "--goal", "Open settings"])
