"""`PortalCapturer` against a fake portal on a private D-Bus daemon."""

import shutil
import stat
import struct
import subprocess
import threading

import pytest
from jeepney import (
    DBusAddress,
    HeaderFields,
    MessageType,
    message_bus,
    new_method_return,
    new_signal,
)
from jeepney.io.blocking import Proxy, open_dbus_connection

from screenguide.capture import CaptureError, PortalCapturer
from screenguide.cli import main

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 800, 600) + b"rest"


BUS_CONFIG = """<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>"""


@pytest.fixture
def bus(monkeypatch, tmp_path):
    """A private session bus. It has no service directories, so no real portal can start on it."""
    if not shutil.which("dbus-daemon"):
        pytest.skip("dbus-daemon is not installed")
    config = tmp_path / "bus.conf"
    config.write_text(BUS_CONFIG.format(socket=tmp_path / "bus"))
    daemon = subprocess.Popen(
        ["dbus-daemon", f"--config-file={config}", "--nofork", "--print-address"],
        stdout=subprocess.PIPE,
        text=True,
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", daemon.stdout.readline().strip())
    yield
    daemon.terminate()
    daemon.wait()


class FakePortal:
    """Answers `Screenshot` with a configured `Response`, or leaves the request unanswered."""

    def __init__(self, code=0, results=None, answer=True):
        self.code = code
        self.results = {} if results is None else results
        self.answer = answer
        self.options = None
        self.closed = threading.Event()
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
            Proxy(message_bus, connection).RequestName("org.freedesktop.portal.Desktop")
            self._ready.set()
            while not self._stop.is_set():
                try:
                    message = connection.receive(timeout=0.05)
                except TimeoutError:
                    continue
                if message.header.message_type == MessageType.method_call:
                    self._handle(connection, message)

    def _handle(self, connection, message):
        member = message.header.fields[HeaderFields.member]
        if member == "Close":
            self.closed.set()
            connection.send(new_method_return(message))
            return
        _parent, self.options = message.body
        sender = message.header.fields[HeaderFields.sender][1:].replace(".", "_")
        token = self.options["handle_token"][1]
        handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        connection.send(new_method_return(message, "o", (handle,)))
        if self.answer:
            address = DBusAddress(handle, interface="org.freedesktop.portal.Request")
            connection.send(new_signal(address, "Response", "ua{sv}", (self.code, self.results)))


def uri_result(uri):
    return {"uri": ("s", uri)}


@pytest.fixture
def portal_file(tmp_path):
    path = tmp_path / "Screenshot from today.png"
    path.write_bytes(PNG)
    return path


def test_capture_returns_the_image_in_memory_and_removes_the_portal_file(bus, portal_file):
    uri = "file://" + str(portal_file).replace(" ", "%20")

    with FakePortal(results=uri_result(uri)) as portal:
        screenshot = PortalCapturer().capture()

    assert (screenshot.data, screenshot.width, screenshot.height) == (PNG, 800, 600)
    assert not portal_file.exists()
    assert portal.options["interactive"] == ("b", False)


def test_save_option_keeps_a_private_copy(bus, portal_file, tmp_path):
    save_dir = tmp_path / "saved"

    with FakePortal(results=uri_result(portal_file.as_uri())):
        PortalCapturer(save_dir=save_dir).capture()

    saved = list(save_dir.iterdir())
    assert [path.read_bytes() for path in saved] == [PNG]
    assert saved[0].suffix == ".png"
    assert stat.S_IMODE(saved[0].stat().st_mode) == 0o600


def test_image_that_is_not_png_stays_unmeasured(bus, portal_file):
    portal_file.write_bytes(b"something else")

    with FakePortal(results=uri_result(portal_file.as_uri())):
        screenshot = PortalCapturer().capture()

    assert (screenshot.width, screenshot.height) == (None, None)


def test_denial_gives_a_clear_error(bus):
    with FakePortal(code=1), pytest.raises(CaptureError, match="denied"):
        PortalCapturer().capture()


def test_stored_denial_or_failure_reports_the_code(bus):
    with FakePortal(code=2), pytest.raises(CaptureError, match="denied or failed.*code 2"):
        PortalCapturer().capture()


def test_absent_portal_gives_a_clear_error(bus):
    with pytest.raises(CaptureError, match="not available.*xdg-desktop-portal"):
        PortalCapturer().capture()


def test_absent_session_bus_gives_a_clear_error(monkeypatch):
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)

    with pytest.raises(CaptureError, match="session bus"):
        PortalCapturer().capture()


def test_unanswered_request_times_out_and_is_closed(bus):
    with FakePortal(answer=False) as portal:
        with pytest.raises(CaptureError, match="no answer"):
            PortalCapturer(response_timeout=0.1).capture()
        assert portal.closed.wait(5)


@pytest.mark.parametrize(
    "results",
    [{}, uri_result("https://example.com/x.png"), uri_result("file://elsewhere/x.png")],
)
def test_unusable_location_gives_a_clear_error(bus, results):
    with FakePortal(results=results), pytest.raises(CaptureError):
        PortalCapturer().capture()


def test_missing_or_empty_file_gives_a_clear_error(bus, portal_file, tmp_path):
    uri = (tmp_path / "absent.png").as_uri()
    with FakePortal(results=uri_result(uri)), pytest.raises(CaptureError, match="cannot read"):
        PortalCapturer().capture()

    portal_file.write_bytes(b"")
    uri = portal_file.as_uri()
    with FakePortal(results=uri_result(uri)), pytest.raises(CaptureError, match="empty"):
        PortalCapturer().capture()
    assert not portal_file.exists()


def test_cli_capture_records_the_measured_image(bus, portal_file, usage_log, capsys):
    with FakePortal(results=uri_result(portal_file.as_uri())):
        assert main(["run", "--capture", "--goal", "Open settings"]) == 0

    assert '"image_width": 800' in usage_log.read_text()
    assert "target_label: Settings" in capsys.readouterr().out


def test_cli_reports_denial_without_recording_usage(bus, usage_log, capsys):
    with FakePortal(code=1):
        assert main(["run", "--capture", "--goal", "Open settings"]) == 1

    assert "denied" in capsys.readouterr().err
    assert not usage_log.exists()


def test_cli_requires_exactly_one_source(image):
    with pytest.raises(SystemExit):
        main(["run", "--goal", "Open settings"])
    with pytest.raises(SystemExit):
        main(["run", "--capture", "--image", str(image), "--goal", "Open settings"])
