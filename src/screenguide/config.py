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


def load_config() -> Config:
    path = config_dir() / "config.toml"
    file_values = load_toml(path)

    usage_log = os.environ.get("SCREENGUIDE_USAGE_LOG") or file_values.get("usage_log")
    if usage_log is None:
        return Config(usage_log=Path.home() / ".local" / "share" / "screenguide" / "usage")
    if not isinstance(usage_log, str):
        raise ConfigError(f"{path}: usage_log must be a string")
    return Config(usage_log=Path(usage_log).expanduser())
