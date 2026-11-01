"""The `screenguide` command."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from screenguide.capture import FileCapturer
from screenguide.contract import InvalidModelResponse
from screenguide.detection import StubDetector
from screenguide.grounding import NumberingGrounding
from screenguide.model import StubProvider
from screenguide.session import GuideSession, Step
from screenguide.usage import JsonlUsageTracker, default_usage_log


def format_step(step: Step) -> str:
    bbox = "null"
    if step.bbox:
        box = step.bbox
        bbox = f"x={box.x} y={box.y} width={box.width} height={box.height}"
    return "\n".join(
        [
            f"target_label: {step.target_label if step.target_label is not None else 'null'}",
            f"instruction: {step.instruction}",
            f"bbox: {bbox}",
            f"done: {str(step.done).lower()}",
            f"confidence: {step.confidence}",
        ]
    )


def run(image: Path, goal: str) -> int:
    session = GuideSession(
        goal=goal,
        capturer=FileCapturer(image),
        detector=StubDetector(),
        grounding=NumberingGrounding(),
        provider=StubProvider(),
        tracker=JsonlUsageTracker(default_usage_log()),
    )
    try:
        step = session.next_step()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except InvalidModelResponse as exc:
        print(f"error: invalid model response: {exc}", file=sys.stderr)
        return 1
    print(format_step(step))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screenguide")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="produce one step for an image and a goal")
    run_parser.add_argument("--image", type=Path, required=True, help="image file to read")
    run_parser.add_argument("--goal", required=True, help="what the user wants to do")

    args = parser.parse_args(argv)
    return run(args.image, args.goal)
