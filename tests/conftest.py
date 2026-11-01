import pytest


@pytest.fixture(autouse=True)
def usage_log(tmp_path, monkeypatch):
    """Keep every test out of the real home."""
    path = tmp_path / "usage"
    monkeypatch.setenv("SCREENGUIDE_USAGE_LOG", str(path))
    return path


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "screen.png"
    path.write_bytes(b"not decoded by the stub")
    return path
