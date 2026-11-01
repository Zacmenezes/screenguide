"""Configuration: `~/.config/screenguide/config.toml`, overridden by `SCREENGUIDE_*` variables."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """A configuration file exists but cannot be used."""


# Claude 4.7 and later, Haiku 5.5 included, accept 2576 px on the long edge, per the Vision docs.
DEFAULT_IMAGE_LONG_EDGE = 2576
DEFAULT_MODEL = "claude-haiku-5-5"


def config_dir() -> Path:
    return Path.home() / ".config" / "screenguide"


def load_toml(path: Path) -> dict[str, Any]:
    """Read a TOML file; a missing file reads as empty."""
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except FileNotFoundError:
        return {}
    except ValueError as exc:  # Bad TOML or bad UTF-8.
        raise ConfigError(f"{path}: {exc}") from exc


@dataclass(frozen=True)
class Config:
    usage_log: Path
    screenshot_dir: Path | None = None  # Screenshots stay in memory unless this is set.
    monitor: str | None = None  # Connector name. The capture is the whole desktop unless set.
    image_long_edge: int = DEFAULT_IMAGE_LONG_EDGE  # Of the image sent to the model, in pixels.
    model: str = DEFAULT_MODEL
    effort: str | None = None  # `output_config.effort`. The model's own default unless set.


def load_config() -> Config:
    path = config_dir() / "config.toml"
    file_values = load_toml(path)

    usage_log = _path_setting(path, file_values, "usage_log")
    return Config(
        usage_log=usage_log or Path.home() / ".local" / "share" / "screenguide" / "usage",
        screenshot_dir=_path_setting(path, file_values, "screenshot_dir"),
        monitor=_string_setting(path, file_values, "monitor"),
        image_long_edge=_positive_int_setting(path, file_values, "image_long_edge")
        or DEFAULT_IMAGE_LONG_EDGE,
        model=_string_setting(path, file_values, "model") or DEFAULT_MODEL,
        effort=_string_setting(path, file_values, "effort"),
    )


def _path_setting(path: Path, file_values: dict[str, Any], key: str) -> Path | None:
    value = _string_setting(path, file_values, key)
    return None if value is None else Path(value).expanduser()


def _positive_int_setting(path: Path, file_values: dict[str, Any], key: str) -> int | None:
    value = os.environ.get(f"SCREENGUIDE_{key.upper()}") or file_values.get(key)
    if value is None:
        return None
    try:
        number = int(value) if isinstance(value, str) else value
    except ValueError:
        number = None
    if type(number) is not int or number <= 0:
        raise ConfigError(f"{path}: {key} must be a positive integer")
    return number


def _string_setting(path: Path, file_values: dict[str, Any], key: str) -> str | None:
    value = os.environ.get(f"SCREENGUIDE_{key.upper()}") or file_values.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{path}: {key} must be a string")
    return value
