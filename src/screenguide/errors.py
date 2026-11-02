"""What can stop a guide session, in words for the user. Shared by the terminal and the panel."""

from screenguide.capture import CaptureError
from screenguide.config import ConfigError
from screenguide.contract import InvalidModelResponse, ModelRefusal
from screenguide.detection import DetectionError
from screenguide.model import ModelProviderError
from screenguide.monitors import MonitorError

GUIDE_ERRORS = (
    OSError,
    ConfigError,
    CaptureError,
    MonitorError,
    DetectionError,
    ModelProviderError,
    InvalidModelResponse,
    ModelRefusal,
)


def describe_guide_error(exc: Exception) -> str:
    if isinstance(exc, ModelRefusal):
        return (
            f"The model declined this request: {exc}\n"
            "Try another model: set `model` in ~/.config/screenguide/config.toml "
            "or SCREENGUIDE_MODEL."
        )
    if isinstance(exc, InvalidModelResponse):
        return f"invalid model response after one retry: {exc}"
    return str(exc)
