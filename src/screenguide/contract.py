"""The model response contract: parsing and validation of what the model returns."""

import json
from dataclasses import dataclass

# Numeric bounds are left out because structured outputs reject them; the parser checks them.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "element_id": {"type": ["integer", "null"]},
        "target_label": {"type": ["string", "null"]},
        "instruction": {"type": "string"},
        "done": {"type": "boolean"},
        "confidence": {"type": "number"},
    },
    "required": ["element_id", "target_label", "instruction", "done", "confidence"],
    "additionalProperties": False,
}

# A reply to a message from the footer: a text answer, or a step that replaces the current one.
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
MESSAGE_REPLY_SCHEMA = {"anyOf": [ANSWER_SCHEMA, RESPONSE_SCHEMA]}


class InvalidModelResponse(ValueError):
    """The model's reply does not honour the response contract."""


class ModelRefusal(Exception):
    """The model declined the request. Retrying the same request will not help."""


@dataclass(frozen=True)
class ModelResponse:
    element_id: int | None
    target_label: str | None
    instruction: str
    done: bool
    confidence: float


@dataclass(frozen=True)
class AnswerResponse:
    answer: str


def parse_model_response(text: str) -> ModelResponse:
    return _parse_step(_load(text))


def parse_message_reply(text: str) -> AnswerResponse | ModelResponse:
    """A reply to a message: `{"answer": ...}`, or a step in the response contract."""
    raw = _load(text)
    if "answer" not in raw:
        return _parse_step(raw)
    if set(raw) != {"answer"}:
        raise InvalidModelResponse("an answer carries no other fields")
    answer = raw["answer"]
    if not isinstance(answer, str) or not answer.strip():
        raise InvalidModelResponse("answer must be a non-empty string")
    return AnswerResponse(answer)


def _load(text: str) -> dict:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidModelResponse(f"reply is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise InvalidModelResponse("reply is not a JSON object")
    return raw


def _parse_step(raw: dict) -> ModelResponse:
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
