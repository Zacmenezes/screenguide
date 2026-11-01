import json
import shutil
import subprocess

import pytest
from PIL import Image

from screenguide.detection import BBox, Element
from screenguide.model import ModelReply

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


class StubProvider:
    """Stands in for the real provider in CLI tests: picks the first box, with made-up tokens."""

    name = "stub"
    external = False

    def __init__(self, model="stub", effort=None):
        self.model = model

    def complete(self, request):
        elements = request.marked.elements
        element_id = min(elements, default=None)
        label = elements[element_id].text if element_id is not None else None
        return ModelReply(
            text=json.dumps(
                {
                    "element_id": element_id,
                    "target_label": label,
                    "instruction": f"Click {label}, the first text on the screen."
                    if label
                    else "No text was found on the screen.",
                    "done": False,
                    "confidence": 0.8,
                }
            ),
            input_tokens=1000,
            output_tokens=100,
        )


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
    Image.new("RGB", (200, 100), "white").save(path)
    return path


@pytest.fixture(autouse=True)
def stub_provider(monkeypatch):
    """CLI tests never reach the network; `test_model.py` tests the real provider's mapping."""
    monkeypatch.setattr("screenguide.cli.AnthropicProvider", lambda model, effort: StubProvider())


@pytest.fixture(autouse=True)
def fixed_detector(monkeypatch):
    """CLI tests cross every layer except OCR; `test_detection.py` overrides this to use it."""
    monkeypatch.setattr("screenguide.cli.RapidOcrDetector", FixedDetector)
