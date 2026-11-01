from pathlib import Path

import pytest

from screenguide.cli import main
from screenguide.config import ConfigError, load_config


@pytest.fixture
def config_file(home):
    path = home / ".config" / "screenguide" / "config.toml"
    path.parent.mkdir(parents=True)
    return path


def test_missing_file_means_defaults(home, monkeypatch):
    monkeypatch.delenv("SCREENGUIDE_USAGE_LOG")

    assert load_config().usage_log == home / ".local" / "share" / "screenguide" / "usage"


def test_file_overrides_default(config_file, monkeypatch):
    monkeypatch.delenv("SCREENGUIDE_USAGE_LOG")
    config_file.write_text('usage_log = "/from/file"\n')

    assert load_config().usage_log == Path("/from/file")


def test_environment_overrides_file(config_file, monkeypatch):
    config_file.write_text('usage_log = "/from/file"\n')
    monkeypatch.setenv("SCREENGUIDE_USAGE_LOG", "/from/env")

    assert load_config().usage_log == Path("/from/env")


def test_home_is_expanded_in_paths(home, config_file, monkeypatch):
    monkeypatch.delenv("SCREENGUIDE_USAGE_LOG")
    config_file.write_text('usage_log = "~/usage"\n')

    assert load_config().usage_log == home / "usage"


@pytest.mark.parametrize("content", ["not = = toml", "usage_log = 3\n"])
def test_unusable_file_is_a_config_error(config_file, monkeypatch, content):
    monkeypatch.delenv("SCREENGUIDE_USAGE_LOG")
    config_file.write_text(content)

    with pytest.raises(ConfigError, match="config.toml"):
        load_config()


def test_cli_reports_an_unusable_config_file(config_file, image, capsys):
    config_file.write_text("not = = toml")

    assert main(["usage"]) == 1
    assert main(["run", "--image", str(image), "--goal", "Open settings"]) == 1
    assert "config.toml" in capsys.readouterr().err
