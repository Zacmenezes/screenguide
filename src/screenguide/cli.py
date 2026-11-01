"""The `screenguide` command."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from screenguide.capture import FileCapturer
from screenguide.config import ConfigError, load_config
from screenguide.contract import InvalidModelResponse
from screenguide.detection import StubDetector
from screenguide.grounding import NumberingGrounding
from screenguide.model import StubProvider
from screenguide.pricing import load_pricing
from screenguide.session import GuideSession, Step
from screenguide.usage import JsonlUsageTracker, read_usage_log, summarize


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
    try:
        session = GuideSession(
            goal=goal,
            capturer=FileCapturer(image),
            detector=StubDetector(),
            grounding=NumberingGrounding(),
            provider=StubProvider(),
            tracker=JsonlUsageTracker(load_config().usage_log, load_pricing()),
        )
        step = session.next_step()
    except (OSError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except InvalidModelResponse as exc:
        print(f"error: invalid model response: {exc}", file=sys.stderr)
        return 1
    print(format_step(step))
    return 0


def usage() -> int:
    try:
        records, malformed = read_usage_log(load_config().usage_log)
    except (OSError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(summarize(records, malformed))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screenguide")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="produce one step for an image and a goal")
    run_parser.add_argument("--image", type=Path, required=True, help="image file to read")
    run_parser.add_argument("--goal", required=True, help="what the user wants to do")
    commands.add_parser("usage", help="summarise usage by day, session, provider and model")

    args = parser.parse_args(argv)
    if args.command == "usage":
        return usage()
    return run(args.image, args.goal)
