"""`layer:model`: calls the model; returns the reply and the usage the API reports."""

import json
from dataclasses import dataclass
from typing import Protocol

from screenguide.grounding import MarkedScreen


@dataclass(frozen=True)
class ModelRequest:
    goal: str
    history: tuple[str, ...]
    marked: MarkedScreen


@dataclass(frozen=True)
class ModelReply:
    """Raw reply text plus tokens as reported; a field not reported stays `None`."""

    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelProvider(Protocol):
    name: str
    model: str

    def complete(self, request: ModelRequest) -> ModelReply: ...


class StubProvider:
    """Stub: gives the same reply and made-up token counts to any request."""

    name = "stub"
    model = "stub"

    def complete(self, request: ModelRequest) -> ModelReply:
        return ModelReply(
            text=json.dumps(
                {
                    "element_id": 2,
                    "target_label": "Settings",
                    "instruction": "Click Settings in the top menu, between File and Help.",
                    "done": False,
                    "confidence": 0.8,
                }
            ),
            input_tokens=1000,
            output_tokens=100,
        )
