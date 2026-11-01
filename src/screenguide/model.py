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
    """Stub: picks the first box, with made-up token counts; `null` if there are no boxes."""

    name = "stub"
    model = "stub"

    def complete(self, request: ModelRequest) -> ModelReply:
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
