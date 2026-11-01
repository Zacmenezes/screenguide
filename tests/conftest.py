import shutil
import subprocess

import pytest

from screenguide.detection import BBox, Element

ELEMENTS = (
    Element(1, "File", BBox(x=10, y=8, width=40, height=20), 0.9),
    Element(2, "Settings", BBox(x=60, y=8, width=70, height=20), 0.9),
    Element(3, "Help", BBox(x=140, y=8, width=40, height=20), 0.9),
)


class FixedDetector:
    def __init__(self, elements=ELEMENTS):
        self._elements = list(elements)

    def detect(self, screenshot):
        return self._elements


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
    """A private session bus. It has no service directories, so no real service can start on it."""
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


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Keep every test out of the real home."""
    path = tmp_path / "home"
    monkeypatch.setenv("HOME", str(path))
    return path


@pytest.fixture(autouse=True)
def usage_log(tmp_path, monkeypatch):
    path = tmp_path / "usage"
    monkeypatch.setenv("SCREENGUIDE_USAGE_LOG", str(path))
    return path


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "screen.png"
    path.write_bytes(b"not decoded by the stub")
    return path


@pytest.fixture(autouse=True)
def fixed_detector(monkeypatch):
    """CLI tests cross every layer except OCR; `test_detection.py` overrides this to use it."""
    monkeypatch.setattr("screenguide.cli.RapidOcrDetector", FixedDetector)
