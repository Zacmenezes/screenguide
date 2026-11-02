import json

import pytest

from screenguide.contract import (
    AnswerResponse,
    InvalidModelResponse,
    parse_message_reply,
    parse_model_response,
)

VALID = {
    "element_id": 12,
    "target_label": "Layout",
    "instruction": "Click the Layout tab in the top menu, between Design and References.",
    "done": False,
    "confidence": 0.8,
}


def test_valid_response_is_parsed():
    response = parse_model_response(json.dumps(VALID))

    assert response.element_id == 12
    assert response.target_label == "Layout"
    assert response.instruction == VALID["instruction"]
    assert response.done is False
    assert response.confidence == 0.8


def test_null_element_id_is_accepted():
    response = parse_model_response(json.dumps({**VALID, "element_id": None}))

    assert response.element_id is None


def test_done_with_null_element_and_label_is_accepted():
    reply = {**VALID, "element_id": None, "target_label": None, "done": True}

    assert parse_model_response(json.dumps(reply)).done is True


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        json.dumps({k: v for k, v in VALID.items() if k != "instruction"}),
        json.dumps({**VALID, "element_id": "12"}),
        json.dumps({**VALID, "element_id": True}),
        json.dumps({**VALID, "element_id": 1.5}),
        json.dumps({**VALID, "target_label": 3}),
        json.dumps({**VALID, "instruction": "  "}),
        json.dumps({**VALID, "done": "false"}),
        json.dumps({**VALID, "done": True}),
        json.dumps({**VALID, "confidence": 1.5}),
        json.dumps({**VALID, "confidence": True}),
        json.dumps({**VALID, "confidence": None}),
        '{"element_id": 1, "target_label": "a", "instruction": "b", '
        '"done": false, "confidence": NaN}',
    ],
)
def test_invalid_response_is_rejected(text):
    with pytest.raises(InvalidModelResponse):
        parse_model_response(text)


def test_message_reply_is_an_answer_or_a_step():
    answer = parse_message_reply(json.dumps({"answer": "It opens the page layout options."}))
    step = parse_message_reply(json.dumps(VALID))

    assert answer == AnswerResponse("It opens the page layout options.")
    assert step.element_id == 12 and step.instruction == VALID["instruction"]


@pytest.mark.parametrize(
    "reply",
    [
        {"answer": ""},
        {"answer": "   "},
        {"answer": None},
        {"answer": "Yes.", "done": False},
        {**VALID, "answer": "Yes."},
        {**VALID, "confidence": 2},
        [],
    ],
)
def test_invalid_message_replies_are_rejected(reply):
    with pytest.raises(InvalidModelResponse):
        parse_message_reply(json.dumps(reply))


def test_a_step_reply_cannot_be_an_answer():
    with pytest.raises(InvalidModelResponse):
        parse_model_response(json.dumps({"answer": "Yes."}))
