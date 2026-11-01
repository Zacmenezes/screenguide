"""The model response contract: parsing and validation of what the model returns."""

import json
from dataclasses import dataclass


class InvalidModelResponse(ValueError):
    """The model's reply does not honour the response contract."""


@dataclass(frozen=True)
class ModelResponse:
    element_id: int | None
    target_label: str | None
    instruction: str
    done: bool
    confidence: float


def parse_model_response(text: str) -> ModelResponse:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidModelResponse(f"reply is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise InvalidModelResponse("reply is not a JSON object")

    fields = ("element_id", "target_label", "instruction", "done", "confidence")
    missing = [name for name in fields if name not in raw]
    if missing:
        raise InvalidModelResponse(f"missing fields: {', '.join(missing)}")

    # bool is a subclass of int, so `true` has to be ruled out by hand.
    element_id = raw["element_id"]
    if element_id is not None and type(element_id) is not int:
        raise InvalidModelResponse("element_id must be an integer or null")

    target_label = raw["target_label"]
    if target_label is not None and not isinstance(target_label, str):
        raise InvalidModelResponse("target_label must be a string or null")

    instruction = raw["instruction"]
    if not isinstance(instruction, str) or not instruction.strip():
        raise InvalidModelResponse("instruction must be a non-empty string")

    done = raw["done"]
    if not isinstance(done, bool):
        raise InvalidModelResponse("done must be a boolean")
    if done and element_id is not None:
        raise InvalidModelResponse("element_id must be null when done is true")

    confidence = raw["confidence"]
    if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
        raise InvalidModelResponse("confidence must be a number between 0 and 1")

    return ModelResponse(element_id, target_label, instruction, done, float(confidence))
