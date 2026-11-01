"""Configuration: `~/.config/screenguide/config.toml`, overridden by `SCREENGUIDE_*` variables."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """A configuration file exists but cannot be used."""


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


def load_config() -> Config:
    path = config_dir() / "config.toml"
    file_values = load_toml(path)

    usage_log = _path_setting(path, file_values, "usage_log")
    return Config(
        usage_log=usage_log or Path.home() / ".local" / "share" / "screenguide" / "usage",
        screenshot_dir=_path_setting(path, file_values, "screenshot_dir"),
    )


def _path_setting(path: Path, file_values: dict[str, Any], key: str) -> Path | None:
    value = os.environ.get(f"SCREENGUIDE_{key.upper()}") or file_values.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{path}: {key} must be a string")
    return Path(value).expanduser()
